"""
Amazon TV 통합 크롤러 (운영용)

================================================================================
실행 흐름: Main → BSR → Detail
================================================================================
STEP 1. Main   - 검색 결과 페이지에서 제품 목록 수집 (최대 300개)
STEP 2. BSR    - Best Seller 페이지에서 제품 목록 수집 (2페이지)
STEP 3. Detail - 수집된 모든 제품의 상세 페이지 크롤링 + SKU/item 추출
STEP 4. Review Auto Recovery - 리뷰 수집률이 임계치(50%) 미만이면
        dt_update 모드 4(detailed_review_content IS NULL)로 자동 백필.
        Amazon 리뷰 섹션 신규 레이아웃 간헐 서빙으로 리뷰만 전멸하는 사례 대응.

================================================================================
주요 특징
================================================================================
- 동일한 batch_id로 전체 파이프라인 실행
- 각 크롤러 실패 시에도 다음 단계 계속 진행
- --resume-from 옵션으로 특정 단계부터 재개 가능

================================================================================
사용법
================================================================================
# 처음부터 실행
python amazon_tv_crawl.py

# 특정 단계부터 재시작
python amazon_tv_crawl.py --resume-from detail --batch-id a_20250123_143045

================================================================================
저장 테이블
================================================================================
- Main/BSR     → amazon_tv_product_list (제품 목록)
- Detail       → tv_retail_com (상세 정보 + 리뷰), tv_item_mst (제품 마스터)
"""

import sys
import os
import argparse
import traceback
import time
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime
import pytz

_project_root = os.path.abspath(os.path.dirname(__file__))
while _project_root and not os.path.exists(os.path.join(_project_root, 'common', 'setup.py')):
    _parent = os.path.dirname(_project_root)
    if _parent == _project_root:
        break
    _project_root = _parent
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
from common.setup import setup_environment
setup_environment(__file__)

from amazon.tv.amazon_tv_main import AmazonTVMainCrawler
from amazon.tv.amazon_tv_bsr import AmazonTVBSRCrawler
from amazon.tv.amazon_tv_dt import AmazonTVDetailCrawler
from amazon.tv.amazon_tv_dt_update import AmazonTVDetailUpdateCrawler
from common.base_crawler import BaseCrawler
from common.alert_hhp_monitor import format_elapsed_time
from config import EMAIL_CONFIG

# 리뷰 수집률이 이 비율 미만이면 auto recovery 실행 (STEP 4).
# 정상 배치는 ~85% 수준이고 리뷰 없는 상품(~30건)이 원래 NULL이므로 50%면 명백한 이상.
# Amazon이 리뷰 섹션 신규 레이아웃을 간헐 서빙하면 전체 NULL이 되는 사례 대응 (a_20260703_165616).
REVIEW_RECOVERY_THRESHOLD = 0.5
REVIEW_RECOVERY_MIN_ROWS = 10

# STEP 4 실행 전 게이트 프로브: detail 직후에는 리뷰 로그인 게이트가 아직 안 풀린
# 전례가 있어(2026-07-04 19:07 recovery 헛런), 상품 1개로 게이트 여부를 먼저 확인하고
# 게이트면 대기 후 재확인한다. 게이트는 세션 단위(all-or-nothing)라 1개 샘플로 충분.
RECOVERY_GATE_PROBE_MAX = 4          # 프로브 최대 횟수
RECOVERY_GATE_PROBE_WAIT_SEC = 1800  # 게이트 감지 시 재프로브까지 대기 (30분)


def email_config_value(cfg, *keys, default=None):
    for key in keys:
        value = cfg.get(key)
        if value not in (None, ''):
            return value
    return default


def email_recipients(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r'[;,]', value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]


def concise_elapsed_time(seconds):
    elapsed = format_elapsed_time(seconds)
    match = re.search(r'\(([^()]*)\)\s*$', elapsed)
    return match.group(1) if match else elapsed


def email_config_bool(value, default=False):
    if value in (None, ''):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'y', 'on')
    return bool(value)


def build_amazon_tv_email_report(crawl_results, detail_report, log_file, elapsed, failed_stages, error_message=None, review_recovery=None):
    detail_report = detail_report or {}
    redirects = detail_report.get('redirects') or []
    run_errors = detail_report.get('run_errors') or []
    recovery_triggered = bool(review_recovery and review_recovery.get('triggered'))
    recovery_failed = recovery_triggered and (
        not review_recovery.get('success') or review_recovery.get('still_low')
    )
    review_gated_count = detail_report.get('review_gated_count', 0) or 0
    review_gate_restarts = detail_report.get('review_gate_restarts', 0) or 0

    main_result = crawl_results.get('main') if crawl_results else None
    bsr_result = crawl_results.get('bsr') if crawl_results else None
    main_records = detail_report.get('main_records')
    bsr_records = detail_report.get('bsr_records')
    if main_records is None and isinstance(main_result, dict):
        main_records = main_result.get('records', 0)
    if bsr_records is None and isinstance(bsr_result, dict):
        bsr_records = bsr_result.get('records', 0)

    detail_records = detail_report.get('detail_records', 0)
    saved_records = detail_report.get('saved_records', 0)
    has_sos = bool(error_message or failed_stages)
    # detail 미수집(리스팅 정보만 저장) 비율이 높으면 경고로 승격 — CAPTCHA 등으로 상세가
    # 통째로 막힌 런이 조용히 '특이사항 없음'으로 넘어가는 사각지대 방지 (detail 0이거나 절반 이상 누락).
    undetailed_records = max(saved_records - detail_records, 0)
    detail_blocked = saved_records > 0 and (
        detail_records == 0 or undetailed_records * 2 >= saved_records
    )
    has_warning = bool(
        redirects or run_errors or detail_blocked or recovery_triggered
        or review_gated_count
    )
    severity = 'sos' if has_sos else ('warning' if has_warning else 'ok')

    lines = [
        'product: TV',
        f"main records: {main_records or 0}",
        f"bsr records: {bsr_records or 0}",
        f"detail records: {detail_records}",
        f"db insert rows: {saved_records}",
        f"elapsed: {concise_elapsed_time(elapsed)}",
        '',
    ]

    if severity == 'ok':
        lines.append('특이사항 없음')
        return '\n'.join(lines) + '\n', severity

    lines.append('SOS' if severity == 'sos' else 'WARNING')
    if error_message:
        lines.append(f'- fatal error: {error_message}')
    if failed_stages:
        lines.append(f"- failed stages: {', '.join(failed_stages)}")
    if detail_blocked:
        lines.append(
            f"- detail 미수집: {saved_records}건 중 {undetailed_records}건 상세 없이 저장 "
            f"(CAPTCHA/차단 의심)"
        )
    if review_gated_count:
        lines.append(
            f"- review 로그인 게이트: {review_gated_count}건 감지"
            + (f", 브라우저 재시작 {review_gate_restarts}회" if review_gate_restarts else "")
        )
    if recovery_triggered:
        before_total = review_recovery.get('before_total')
        before = review_recovery.get('before_with_review')
        after = review_recovery.get('after_with_review')
        probe_attempts = review_recovery.get('probe_attempts', 0)
        if review_recovery.get('gate_persisted'):
            lines.append(
                f"- review auto recovery: 게이트 프로브 {probe_attempts}회 전부 로그인 게이트 지속 "
                f"→ 백필 포기 (리뷰 {before}/{before_total}건 유지, 다음 배치에서 재시도)"
            )
        elif review_recovery.get('error'):
            lines.append(f"- review auto recovery: 실행 실패 ({review_recovery['error']})")
        else:
            probe_note = f" (게이트 프로브 {probe_attempts}회 후 진행)" if probe_attempts > 1 else ""
            lines.append(
                f"- review auto recovery: detailed_review_content "
                f"{before}/{before_total}건 → {after}/{before_total}건 "
                f"(복구 {review_recovery.get('recovered', 0)}건){probe_note}"
            )
        if recovery_failed and not review_recovery.get('gate_persisted'):
            lines.append(
                "  - 복구 후에도 리뷰 수집률 임계치 미달 — 로그인 게이트 지속 가능성, "
                "수동 확인 필요"
            )
    if run_errors:
        lines.append(f"- run errors: {len(run_errors)}건")
        for item in run_errors[:20]:
            lines.append(
                f"  - stage={item.get('stage')} url={item.get('url') or ''} "
                f"message={item.get('message')}"
            )
    if redirects:
        lines.append(f'- redirect=true: {len(redirects)}건')
        for item in redirects[:50]:
            decision = item.get('decision')
            url = item.get('url') or ''
            landing_url = item.get('landing_url') or ''
            lines.append(
                f"  - {url} ({decision}) "
                f"listed={item.get('asin')} landing={item.get('landing_asin')} "
                f"landing_url={landing_url}"
            )
    return '\n'.join(lines) + '\n', severity


def send_amazon_tv_email_report(subject, body):
    cfg = dict(EMAIL_CONFIG or {})
    server = email_config_value(cfg, 'smtp_server', 'smtp_host', 'host')
    port = int(email_config_value(cfg, 'smtp_port', 'port', default=587))
    sender = email_config_value(cfg, 'sender_email', 'from_email', 'username', 'user')
    password = email_config_value(cfg, 'sender_password', 'password')
    recipients = email_recipients(email_config_value(
        cfg, 'receiver_email', 'receiver_emails', 'to_email', 'to'))
    use_ssl = email_config_bool(email_config_value(cfg, 'use_ssl', 'smtp_ssl'), default=(port == 465))
    use_tls = email_config_bool(email_config_value(cfg, 'use_tls', 'starttls'), default=(not use_ssl))
    username = email_config_value(cfg, 'smtp_username', 'username', 'user', default=sender)

    missing = [
        name for name, value in (
            ('smtp_server', server),
            ('sender_email', sender),
            ('receiver_email', recipients),
        )
        if not value
    ]
    if missing:
        print(f"[email] skipped: missing EMAIL_CONFIG keys: {', '.join(missing)}")
        return False

    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = str(sender)
    message['To'] = ', '.join(recipients)
    message.set_content(body)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(str(server), port, timeout=60) as smtp:
                if username and password:
                    smtp.login(str(username), str(password))
                smtp.send_message(message)
        else:
            with smtplib.SMTP(str(server), port, timeout=60) as smtp:
                if use_tls:
                    smtp.starttls()
                if username and password:
                    smtp.login(str(username), str(password))
                smtp.send_message(message)
    except Exception as e:
        print(f"[email] failed: {e}")
        return False

    print(f"[email] sent: {subject} -> {', '.join(recipients)}")
    return True


class AmazonTVIntegratedCrawler:
    """Amazon 통합 크롤러 (운영용)"""

    def __init__(self, resume_from=None, batch_id=None):
        """
        Args:
            resume_from: 재시작 단계 ('main'/'bsr'/'detail'/None)
            batch_id: 재시작 시 사용할 배치 ID
        """
        self.account_name = 'Amazon'
        self.batch_id = batch_id
        self.start_time_kst = None
        self.start_time_server = None
        self.end_time = None
        self.resume_from = resume_from
        self.base_crawler = BaseCrawler()
        self.korea_tz = pytz.timezone('Asia/Seoul')

    def get_review_coverage(self):
        """배치의 리뷰 수집 현황 조회: (저장 행 수, detailed_review_content 보유 행 수)"""
        checker = BaseCrawler()
        if not checker.connect_db():
            print("[WARNING] 리뷰 수집률 확인 실패: DB 연결 불가")
            return None, None
        try:
            cursor = checker.db_conn.cursor()
            cursor.execute("""
                SELECT COUNT(*), COUNT(detailed_review_content)
                FROM tv_retail_com
                WHERE account_name = %s AND batch_id = %s
            """, (self.account_name, self.batch_id))
            total, with_review = cursor.fetchone()
            cursor.close()
            return total, with_review
        except Exception as e:
            print(f"[WARNING] 리뷰 수집률 확인 실패: {e}")
            return None, None
        finally:
            try:
                checker.db_conn.close()
            except Exception:
                pass

    def _get_sample_product_url(self):
        """이 배치에서 게이트 프로브에 쓸 상품 URL 1개 조회."""
        checker = BaseCrawler()
        if not checker.connect_db():
            return None
        try:
            cursor = checker.db_conn.cursor()
            cursor.execute("""
                SELECT product_url FROM tv_retail_com
                WHERE account_name = %s AND batch_id = %s AND product_url IS NOT NULL
                LIMIT 1
            """, (self.account_name, self.batch_id))
            row = cursor.fetchone()
            cursor.close()
            return row[0] if row else None
        except Exception as e:
            print(f"[WARNING] 프로브용 상품 URL 조회 실패: {e}")
            return None
        finally:
            try:
                checker.db_conn.close()
            except Exception:
                pass

    def _probe_review_gate_once(self, url):
        """새 브라우저 세션으로 상품 1개를 열어 리뷰 게이트 여부 확인.

        Returns: True=게이트 감지, False=정상, None=판단 불가(프로브 실패)
        """
        crawler = AmazonTVDetailCrawler(batch_id=self.batch_id, test_mode=False)
        try:
            if not crawler.setup_browser():
                return None
            crawler.page.get(url)
            time.sleep(3)
            try:
                crawler.page.run_js(
                    "var e = document.getElementById('reviewsMedley')"
                    " || document.getElementById('customer-reviews_feature_div');"
                    " if (e) e.scrollIntoView({block: 'center', behavior: 'instant'});"
                )
            except Exception:
                pass
            time.sleep(3)
            return crawler.is_review_gated()
        except Exception as e:
            print(f"[STEP 4/4] 게이트 프로브 실패: {e}")
            return None
        finally:
            if crawler.page:
                try:
                    crawler.page.quit()
                except Exception:
                    pass

    def _wait_for_gate_release(self):
        """게이트가 풀릴 때까지 프로브+대기 반복.

        Returns: (proceed: bool, probe_attempts: int)
        - 게이트 미감지/판단불가 → True (recovery 진행)
        - RECOVERY_GATE_PROBE_MAX회 모두 게이트 → False (recovery 포기)
        """
        sample_url = self._get_sample_product_url()
        if not sample_url:
            print("[STEP 4/4] 프로브용 URL 없음 → 프로브 생략하고 recovery 진행")
            return True, 0

        for attempt in range(1, RECOVERY_GATE_PROBE_MAX + 1):
            gated = self._probe_review_gate_once(sample_url)
            if gated is True:
                print(f"[STEP 4/4] 게이트 프로브 {attempt}/{RECOVERY_GATE_PROBE_MAX}: "
                      f"로그인 게이트 지속")
                if attempt < RECOVERY_GATE_PROBE_MAX:
                    print(f"[STEP 4/4] {RECOVERY_GATE_PROBE_WAIT_SEC // 60}분 대기 후 재확인")
                    time.sleep(RECOVERY_GATE_PROBE_WAIT_SEC)
                continue
            if gated is False:
                print(f"[STEP 4/4] 게이트 프로브 {attempt}/{RECOVERY_GATE_PROBE_MAX}: "
                      f"게이트 해제 확인 → recovery 진행")
            else:
                print(f"[STEP 4/4] 게이트 프로브 판단 불가 → recovery 진행 (실행 중 게이트 감지가 재차 방어)")
            return True, attempt
        return False, RECOVERY_GATE_PROBE_MAX

    def run_review_auto_recovery(self, crawl_results):
        """STEP 4: 리뷰 수집률이 임계치 미만이면 dt_update 모드 4 백필을 자동 실행.

        Amazon이 리뷰 섹션 신규 레이아웃(인라인 리뷰 없음)을 간헐 서빙하면 별점/가격은
        정상인데 detailed_review_content만 전멸한다. 이때 새 브라우저 세션으로 재수집하면
        대부분 복구되므로, 수동 RDP 재실행 대신 오케스트레이터가 직접 1회 시도한다.

        Returns: dict | None — 리커버리 리포트 (미실행 시 None)
        """
        detail_ok = (
            isinstance(crawl_results.get('detail'), dict)
            and crawl_results['detail'].get('success')
        )
        if not detail_ok:
            return None

        total, with_review = self.get_review_coverage()
        if total is None:
            return None
        if total < REVIEW_RECOVERY_MIN_ROWS:
            return None
        if with_review >= total * REVIEW_RECOVERY_THRESHOLD:
            print(f"\n[STEP 4/4] Review Auto Recovery — 리뷰 수집 정상 "
                  f"({with_review}/{total}건) → SKIP")
            return None

        print(f"\n[STEP 4/4] Review Auto Recovery — 리뷰 {with_review}/{total}건, "
              f"임계치 {REVIEW_RECOVERY_THRESHOLD:.0%} 미달 → mode 4 백필 준비")
        stage_start = time.time()
        report = {
            'triggered': True,
            'before_total': total,
            'before_with_review': with_review,
        }

        # 게이트 프로브: detail 직후에는 로그인 게이트가 안 풀렸을 수 있어
        # 상품 1개로 확인 후 진행 (게이트 지속이면 대기→재확인, 끝까지 지속이면 포기)
        proceed, probe_attempts = self._wait_for_gate_release()
        report['probe_attempts'] = probe_attempts
        if not proceed:
            print("[WARNING] 게이트 프로브 전 회차 로그인 게이트 지속 — recovery 포기 "
                  "(다음 배치 STEP 4에서 재시도)")
            report.update({
                'success': False,
                'gate_persisted': True,
                'after_with_review': with_review,
                'recovered': 0,
                'still_low': True,
                'duration': time.time() - stage_start,
            })
            return report

        try:
            update_crawler = AmazonTVDetailUpdateCrawler(
                batch_id=self.batch_id,
                mode=AmazonTVDetailUpdateCrawler.MODE_DETAIL_REVIEW_NULL,
                test_mode=False,
            )
            success = update_crawler.run()
            after_total, after_with_review = self.get_review_coverage()
            report.update({
                'success': bool(success),
                'after_with_review': after_with_review,
                'recovered': (after_with_review or 0) - with_review,
                'still_low': (
                    after_with_review is not None
                    and after_with_review < total * REVIEW_RECOVERY_THRESHOLD
                ),
                'duration': time.time() - stage_start,
            })
            print(f"[STEP 4/4] Review Auto Recovery 완료 — "
                  f"리뷰 {with_review} → {after_with_review}/{total}건")
            if report['still_low']:
                print("[WARNING] 복구 후에도 리뷰 수집률 임계치 미달 — "
                      "Amazon 리뷰 레이아웃 변경 지속 가능성, 수동 확인 필요")
        except Exception as e:
            print(f"[ERROR] Review Auto Recovery 실패: {e}")
            traceback.print_exc()
            report.update({
                'success': False,
                'error': str(e),
                'duration': time.time() - stage_start,
            })
        return report

    def run(self):
        """통합 크롤러 실행. Returns: bool"""
        self.start_time_kst = datetime.now(self.korea_tz)
        self.start_time_server = datetime.now()

        # batch_id 생성 또는 재사용
        if not self.batch_id:
            self.batch_id = self.base_crawler.generate_batch_id(self.account_name)

        # 로깅 시작 (콘솔 출력을 파일에도 저장)
        log_file = self.base_crawler.start_logging(self.batch_id)

        print("\n" + "="*60)
        print("Amazon TV Integrated Crawler (Production)")
        print("="*60)
        print(f"batch_id: {self.batch_id}")
        if log_file:
            print(f"log_file: {log_file}")
        if self.resume_from:
            print(f"resume_from: {self.resume_from}")

        crawl_results = {'main': None, 'bsr': None, 'detail': None}
        detail_report = None
        review_recovery = None

        try:
            # 결과: {'stage': {'success': bool, 'duration': float}} 형태로 저장

            # STEP 1: Main
            if not self.resume_from or self.resume_from == 'main':
                print(f"\n[STEP 1/3] Main Crawler...")
                stage_start = time.time()
                try:
                    main_crawler = AmazonTVMainCrawler(test_mode=False, batch_id=self.batch_id)
                    success = main_crawler.run()
                    crawl_results['main'] = {
                        'success': success,
                        'duration': time.time() - stage_start,
                        'records': main_crawler.stats.get('saved', 0),
                        'stats': dict(main_crawler.stats),
                    }
                except Exception as e:
                    print(f"[ERROR] Main: {e}")
                    traceback.print_exc()
                    crawl_results['main'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                crawl_results['main'] = 'skipped'

            # Main 성공 여부 — resume_from으로 skip된 경우도 성공으로 간주 (이전 batch에서 main 성공했음을 가정)
            main_ok = (
                crawl_results['main'] == 'skipped'
                or (isinstance(crawl_results['main'], dict) and crawl_results['main'].get('success'))
            )

            # STEP 2: BSR — Main 성공 시에만 진행 (Main 실패면 amazon_tv_product_list가 비어있어 BSR 무의미)
            if (not self.resume_from or self.resume_from in ['main', 'bsr']) and main_ok:
                print(f"\n[STEP 2/3] BSR Crawler...")
                stage_start = time.time()
                try:
                    bsr_crawler = AmazonTVBSRCrawler(test_mode=False, batch_id=self.batch_id)
                    success = bsr_crawler.run()
                    crawl_results['bsr'] = {
                        'success': success,
                        'duration': time.time() - stage_start,
                        'records': bsr_crawler.stats.get('updated', 0) + bsr_crawler.stats.get('inserted', 0),
                        'stats': dict(bsr_crawler.stats),
                    }
                except Exception as e:
                    print(f"[ERROR] BSR: {e}")
                    traceback.print_exc()
                    crawl_results['bsr'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                if not main_ok:
                    print(f"\n[STEP 2/3] BSR Crawler — Main 실패로 SKIP")
                crawl_results['bsr'] = 'skipped'

            # BSR 성공 여부 — resume_from으로 skip된 경우도 성공으로 간주
            bsr_ok = (
                crawl_results['bsr'] == 'skipped'
                or (isinstance(crawl_results['bsr'], dict) and crawl_results['bsr'].get('success'))
            )

            # STEP 3: Detail — Main + BSR 모두 성공 시에만 진행 (이전 단계 실패 시 product_list 데이터 불완전)
            if (not self.resume_from or self.resume_from in ['main', 'bsr', 'detail']) and main_ok and bsr_ok:
                print(f"\n[STEP 3/3] Detail Crawler...")
                stage_start = time.time()
                try:
                    detail_crawler = AmazonTVDetailCrawler(batch_id=self.batch_id, test_mode=False)
                    success = detail_crawler.run()
                    detail_report = dict(detail_crawler.detail_report)
                    crawl_results['detail'] = {
                        'success': success,
                        'duration': time.time() - stage_start,
                        'records': detail_report.get('saved_records', 0),
                        'stats': detail_report,
                    }
                except Exception as e:
                    print(f"[ERROR] Detail: {e}")
                    traceback.print_exc()
                    crawl_results['detail'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                if not main_ok:
                    print(f"\n[STEP 3/3] Detail Crawler — Main 실패로 SKIP")
                elif not bsr_ok:
                    print(f"\n[STEP 3/3] Detail Crawler — BSR 실패로 SKIP")
                crawl_results['detail'] = 'skipped'

            # STEP 4: 리뷰 수집률 점검 후 필요 시 자동 백필 (dt_update 모드 4)
            review_recovery = self.run_review_auto_recovery(crawl_results)
            crawl_results['review_recovery'] = (
                {'success': bool(review_recovery.get('success')),
                 'duration': review_recovery.get('duration', 0)}
                if review_recovery else 'skipped'
            )

            # 결과 출력
            self.end_time = datetime.now()
            elapsed = (self.end_time - self.start_time_server).total_seconds()

            print("\n" + "="*60)
            print(f"완료 ({elapsed/60:.1f}분)")
            for step, result in crawl_results.items():
                if result == 'skipped':
                    status = "SKIP"
                elif isinstance(result, dict):
                    status = "OK" if result.get('success') else "FAIL"
                else:
                    status = "FAIL"
                print(f"  {step}: {status}")
            print("="*60)

            # 이메일 알림 발송 (review_recovery 실패는 SOS가 아니라 WARNING으로 리포트)
            failed_stages = [
                k for k, v in crawl_results.items()
                if k != 'review_recovery'
                and isinstance(v, dict) and v.get('success') is False
            ]
            body, severity = build_amazon_tv_email_report(
                crawl_results=crawl_results,
                detail_report=detail_report,
                log_file=log_file,
                elapsed=elapsed,
                failed_stages=failed_stages,
                review_recovery=review_recovery,
            )
            subject = '[USA] AMZN TV crawling report'
            if severity == 'sos':
                subject = 'SOS ' + subject
            elif severity == 'warning':
                subject = 'WARNING ' + subject
            send_amazon_tv_email_report(subject, body)

            # 로깅 종료
            self.base_crawler.stop_logging()

            success_count = sum(
                1 for k, r in crawl_results.items()
                if k != 'review_recovery'
                and isinstance(r, dict) and r.get('success') is True
            )
            return success_count > 0

        except Exception as e:
            print(f"\n[ERROR] Integrated crawler failed: {e}")
            traceback.print_exc()

            # 예외 발생 시에도 이메일 알림 발송
            self.end_time = datetime.now()
            elapsed = (self.end_time - self.start_time_server).total_seconds() if self.start_time_server else 0
            body, severity = build_amazon_tv_email_report(
                crawl_results=crawl_results,
                detail_report=detail_report,
                log_file=log_file,
                elapsed=elapsed,
                failed_stages=['Fatal error'],
                error_message=str(e),
                review_recovery=review_recovery,
            )
            subject = '[USA] AMZN TV crawling report'
            if severity == 'sos':
                subject = 'SOS ' + subject
            elif severity == 'warning':
                subject = 'WARNING ' + subject
            send_amazon_tv_email_report(subject, body)

            # 예외 발생 시에도 로깅 종료
            self.base_crawler.stop_logging()
            return False


def main():
    """운영용 통합 크롤러 진입점.

    사용 예:
      python amazon_tv_crawl.py                                                  # 처음부터 전체 실행
      python amazon_tv_crawl.py --resume-from bsr --batch-id a_xxx               # BSR부터 재개
    """
    parser = argparse.ArgumentParser(description='Amazon TV Integrated Crawler (Production)')
    parser.add_argument('--resume-from', type=str, choices=['main', 'bsr', 'detail'])
    parser.add_argument('--batch-id', type=str)
    args = parser.parse_args()

    if args.resume_from and not args.batch_id:
        print("[ERROR] --batch-id is required when using --resume-from")
        exit(1)

    crawler = AmazonTVIntegratedCrawler(
        resume_from=args.resume_from,
        batch_id=args.batch_id,
    )
    success = crawler.run()
    exit(0 if success else 1)


if __name__ == '__main__':
    main()
