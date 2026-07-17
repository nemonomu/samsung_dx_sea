"""
Walmart TV 통합 크롤러 (운영용)

================================================================================
실행 흐름: Main → BSR → Detail
================================================================================
STEP 1. Main   - 검색 결과 페이지에서 제품 목록 수집 (최대 300개)
STEP 2. BSR    - Best Seller 페이지에서 제품 목록 수집 (최대 100개)
STEP 3. Detail - 수집된 모든 제품의 상세 페이지 크롤링

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
python wmart_tv_crawl.py

# 특정 단계부터 재시작
python wmart_tv_crawl.py --resume-from detail --batch-id w_20250123_143045
python wmart_tv_crawl.py --resume-from bsr --batch-id w_20250123_143045

================================================================================
저장 테이블
================================================================================
- Main/BSR     → wmart_tv_product_list (제품 목록)
- Detail       → tv_retail_com (상세 정보 + 리뷰)
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

# 공통 환경 설정 (작업 디렉토리, 한글 출력, 경로 설정)
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

from walmart.tv.wmart_tv_main import WalmartTVMainCrawler
from walmart.tv.wmart_tv_bsr import WalmartTVBSRCrawler
from walmart.tv.wmart_tv_dt import WalmartTVDetailCrawler
from common.base_crawler import BaseCrawler
from common.alert_hhp_monitor import format_elapsed_time
from config import EMAIL_CONFIG


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


def build_walmart_tv_email_report(crawl_results, detail_report, log_file, elapsed, failed_stages, error_message=None):
    detail_report = detail_report or {}
    redirects = detail_report.get('redirects') or []
    run_errors = detail_report.get('run_errors') or []

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
    target_records = detail_report.get('target_records')
    target_records = saved_records if target_records is None else target_records
    has_sos = bool(error_message or failed_stages)
    # detail 미수집(리스팅 정보만 저장) 비율이 높으면 경고로 승격 — CAPTCHA 등으로 상세가
    # 통째로 막힌 런이 조용히 'No issues'로 넘어가는 사각지대 방지 (detail 0이거나 절반 이상 누락).
    undetailed_records = max(saved_records - detail_records, 0)
    unsaved_records = max(target_records - saved_records, 0)
    missing_detail_records = max(target_records - detail_records, 0)
    detail_blocked = missing_detail_records > 0
    has_warning = bool(redirects or run_errors or detail_blocked)
    severity = 'sos' if has_sos else ('warning' if has_warning else 'ok')

    lines = [
        'product: TV',
        f"main records: {main_records or 0}",
        f"bsr records: {bsr_records or 0}",
        f"detail targets: {target_records}",
        f"detail records: {detail_records}",
        f"db insert rows: {saved_records}",
        f"elapsed: {concise_elapsed_time(elapsed)}",
        '',
    ]

    if severity == 'ok':
        lines.append('No issues')
        return '\n'.join(lines) + '\n', severity

    lines.append('SOS' if severity == 'sos' else 'WARNING')
    if error_message:
        lines.append(f'- fatal error: {error_message}')
    if failed_stages:
        lines.append(f"- failed stages: {', '.join(failed_stages)}")
    if detail_blocked:
        lines.append(
            f"- detail missing: {missing_detail_records}/{target_records} target URLs "
            f"did not produce validated detail rows "
            f"(unsaved={unsaved_records}, listing_only={undetailed_records})"
        )
    if run_errors:
        lines.append(f"- run errors: {len(run_errors)}")
        for item in run_errors[:20]:
            lines.append(
                f"  - stage={item.get('stage')} url={item.get('url') or ''} "
                f"message={item.get('message')}"
            )
    if redirects:
        lines.append(f"- redirects: {len(redirects)}")
        for item in redirects[:50]:
            lines.append(
                f"  - {item.get('url') or ''} ({item.get('decision')}) "
                f"message={item.get('message')}"
            )
    return '\n'.join(lines) + '\n', severity


def send_walmart_tv_email_report(subject, body):
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


class WalmartTVIntegratedCrawler:
    """Walmart TV 통합 크롤러 (운영용)"""

    def __init__(self, resume_from=None, batch_id=None):
        """
        Args:
            resume_from: 재시작 단계 ('main'/'bsr'/'detail'/None)
            batch_id: 재시작 시 사용할 배치 ID
        """
        self.account_name = 'Walmart'
        self.batch_id = batch_id
        self.start_time_kst = None
        self.start_time_server = None
        self.end_time = None
        self.resume_from = resume_from
        self.base_crawler = BaseCrawler()
        self.korea_tz = pytz.timezone('Asia/Seoul')

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
        print("Walmart TV Integrated Crawler (Production)")
        print("="*60)
        print(f"batch_id: {self.batch_id}")
        if log_file:
            print(f"log_file: {log_file}")
        if self.resume_from:
            print(f"resume_from: {self.resume_from}")

        crawl_results = {'main': None, 'bsr': None, 'detail': None}
        detail_report = None

        try:
            # 결과: {'stage': {'success': bool, 'duration': float}} 형태로 저장
            # STEP 1: Main
            if not self.resume_from or self.resume_from == 'main':
                print(f"\n[STEP 1/3] Main Crawler...")
                stage_start = time.time()
                try:
                    main_crawler = WalmartTVMainCrawler(test_mode=False, batch_id=self.batch_id)
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

            main_ok = (
                crawl_results['main'] == 'skipped'
                or (isinstance(crawl_results['main'], dict) and crawl_results['main'].get('success'))
            )

            # STEP 2: BSR
            if (not self.resume_from or self.resume_from in ['main', 'bsr']) and main_ok:
                print(f"\n[STEP 2/3] BSR Crawler...")
                stage_start = time.time()
                try:
                    bsr_crawler = WalmartTVBSRCrawler(test_mode=False, batch_id=self.batch_id)
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
                    print(f"\n[STEP 2/3] BSR Crawler - Main 실패로 SKIP")
                crawl_results['bsr'] = 'skipped'

            bsr_ok = (
                crawl_results['bsr'] == 'skipped'
                or (isinstance(crawl_results['bsr'], dict) and crawl_results['bsr'].get('success'))
            )

            # STEP 3: Detail
            if main_ok and bsr_ok:
                print(f"\n[STEP 3/3] Detail Crawler...")
                stage_start = time.time()
                try:
                    detail_crawler = WalmartTVDetailCrawler(batch_id=self.batch_id, test_mode=False)
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
                    print(f"\n[STEP 3/3] Detail Crawler - Main 실패로 SKIP")
                elif not bsr_ok:
                    print(f"\n[STEP 3/3] Detail Crawler - BSR 실패로 SKIP")
                crawl_results['detail'] = 'skipped'

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

            # 이메일 알림 발송
            failed_stages = [
                k for k, v in crawl_results.items()
                if isinstance(v, dict) and v.get('success') is False
            ]
            body, severity = build_walmart_tv_email_report(
                crawl_results=crawl_results,
                detail_report=detail_report,
                log_file=log_file,
                elapsed=elapsed,
                failed_stages=failed_stages,
            )
            subject = '[USA] WMART TV crawling report'
            if severity == 'sos':
                subject = 'SOS ' + subject
            elif severity == 'warning':
                subject = 'WARNING ' + subject
            send_walmart_tv_email_report(subject, body)

            # 로깅 종료
            self.base_crawler.stop_logging()

            return len(failed_stages) == 0

        except Exception as e:
            print(f"\n[ERROR] Integrated crawler failed: {e}")
            traceback.print_exc()

            # 예외 발생 시에도 이메일 알림 발송
            self.end_time = datetime.now()
            elapsed = (self.end_time - self.start_time_server).total_seconds() if self.start_time_server else 0
            body, severity = build_walmart_tv_email_report(
                crawl_results=crawl_results,
                detail_report=detail_report,
                log_file=log_file,
                elapsed=elapsed,
                failed_stages=['Fatal error'],
                error_message=str(e),
            )
            subject = '[USA] WMART TV crawling report'
            if severity == 'sos':
                subject = 'SOS ' + subject
            elif severity == 'warning':
                subject = 'WARNING ' + subject
            send_walmart_tv_email_report(subject, body)

            # 예외 발생 시에도 로깅 종료
            self.base_crawler.stop_logging()
            return False


def main():
    """운영용 통합 크롤러 진입점"""
    parser = argparse.ArgumentParser(description='Walmart TV Integrated Crawler (Production)')
    parser.add_argument('--resume-from', type=str, choices=['main', 'bsr', 'detail'])
    parser.add_argument('--batch-id', type=str)
    args = parser.parse_args()

    if args.resume_from and not args.batch_id:
        print("[ERROR] --batch-id is required when using --resume-from")
        exit(1)

    crawler = WalmartTVIntegratedCrawler(resume_from=args.resume_from, batch_id=args.batch_id)
    success = crawler.run()
    exit(0 if success else 1)


if __name__ == '__main__':
    main()
