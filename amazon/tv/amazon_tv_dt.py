"""
Amazon TV Detail 페이지 크롤러

================================================================================
실행 모드
================================================================================
- 개별 실행: batch_id=None (하드코딩된 batch_id 사용)
- 통합 크롤러: batch_id를 파라미터로 전달

================================================================================
주요 기능
================================================================================
- product_list 테이블에서 해당 batch_id의 제품 URL 조회
- 각 제품 상세 페이지에서 리뷰, 별점, 스펙 등 추출
- Main/BSR에서 수집한 모든 제품 처리

================================================================================
저장 테이블
================================================================================
- tv_retail_com (상세 정보 + 리뷰)
"""

import sys
import os
import time
import random
import shutil
import subprocess
import traceback
import re
from datetime import datetime
from lxml import html

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

from common.amazon_base import AmazonBaseCrawler

# 신뢰 프로필 (리뷰 로그인 게이트 통행권) — detail 스테이지 전용.
# 일반 사용 Chrome 프로필의 사본으로 브라우저를 띄우면 게이트를 통과한다
# (2026-07-05 RDP 실측). 사본의 유효기간이 ~2일에 불과해(2026-07-08 관측)
# detail 런 시작 시마다 원본에서 자동 리프레시한다 — 원본은 크롤링에 쓰이지
# 않아 정상 사용 토큰 회전으로 신뢰가 유지되는 "통행증 발급처" 역할.
# 원본/사본 둘 다 없으면 기존 기본 프로필로 동작 (하위 호환).
TRUSTED_PROFILE_DIR = r'C:\chrome_profile_amzn'
TRUSTED_PROFILE_SOURCE = os.path.join(
    os.environ.get('LOCALAPPDATA', ''), 'Google', 'Chrome', 'User Data')
# 사본에 필요한 최소 파일 (Cookies가 핵심 — 세션 신원)
_TRUSTED_PROFILE_FILES = [
    ('Local State',),
    ('Default', 'Preferences'),
    ('Default', 'Network', 'Cookies'),
]


def refresh_trusted_profile(source_dir=None, dest_dir=None):
    """신뢰 프로필 사본을 원본 Chrome 프로필에서 리프레시.

    Cookies가 Chrome 실행 중 잠금이면 esentutl /vss로 우회 복사한다
    (make_trusted_profile.bat과 동일 로직의 Python 구현).

    Returns:
        bool: 핵심 파일(Cookies)까지 갱신 성공 여부. 실패해도 기존 사본은 보존됨.
    """
    source_dir = source_dir or TRUSTED_PROFILE_SOURCE
    dest_dir = dest_dir or TRUSTED_PROFILE_DIR

    if not os.path.exists(os.path.join(source_dir, 'Local State')):
        print(f"[INFO] 신뢰 프로필 원본 없음({source_dir}) → 리프레시 생략")
        return False

    cookies_ok = False
    for parts in _TRUSTED_PROFILE_FILES:
        src = os.path.join(source_dir, *parts)
        dst = os.path.join(dest_dir, *parts)
        if not os.path.exists(src):
            print(f"[WARNING] 신뢰 프로필 원본 파일 없음: {src}")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        is_cookies = parts[-1] == 'Cookies'
        try:
            shutil.copy2(src, dst)
            if is_cookies:
                cookies_ok = True
        except (PermissionError, OSError):
            # Chrome 실행 중 잠금 — VSS 스냅샷 복사로 우회
            try:
                result = subprocess.run(
                    ['esentutl', '/y', src, '/vss', '/d', dst],
                    capture_output=True, timeout=120,
                )
                if result.returncode == 0 and is_cookies:
                    cookies_ok = True
                elif result.returncode != 0:
                    print(f"[WARNING] 신뢰 프로필 VSS 복사 실패({parts[-1]}): rc={result.returncode}")
            except Exception as e:
                print(f"[WARNING] 신뢰 프로필 복사 실패({parts[-1]}): {e}")

    if cookies_ok:
        print(f"[INFO] 신뢰 프로필 리프레시 완료: {source_dir} → {dest_dir}")
    else:
        print("[WARNING] 신뢰 프로필 Cookies 갱신 실패 — 기존 사본으로 진행")
    return cookies_ok

# 리뷰 로그인 게이트("account verification") 대응 정책 — 감지/기록/스킵만 한다.
# 실측 근거(2026-07-04~05):
#   - 브라우저 재시작(쿠키 유지)으로 안 풀림 (7/4 19:07 recovery, 재시작 직후에도 지속)
#   - 쿠키 초기화는 역효과 (신선한 익명 세션이 게이트의 표적 — 삭제 즉시 게이트)
#   - 시간 경과로만 해제 (7/4 22:51, 3.7시간 후 정상)
# → 런 중 재시작·대기는 시간 낭비라 하지 않고, 리뷰만 건너뛰고 나머지 필드를
#   계속 수집한다. 리뷰 백필은 STEP 4(시간차 프로브 + dt_update 모드 4) 담당.


class AmazonTVDetailCrawler(AmazonBaseCrawler):
    # ========================================================================
    # tv_retail_com 컬럼 매핑 (INSERT/UPDATE 공통 단일 소스)
    # ========================================================================
    # crawl_detail이 combined_data에 채우는 추출 필드들 — INSERT/UPDATE 모두 사용.
    # 새 추출 필드 추가 시:
    #   1) crawl_detail에서 combined_data에 키 추가
    #   2) EXTRACTED_FIELDS 리스트에 추가
    # → INSERT(dt)와 UPDATE(dt_update) 모두 자동 반영됨
    EXTRACTED_FIELDS = [
        'item',
        'star_rating',
        'count_of_star_ratings',
        'final_sku_price',
        'original_sku_price',
        'discount_type',
        'sku_popularity',
        'number_of_units_purchased_past_month',
        'model_year',
        'screen_size',
        'summarized_review_content',
        'detailed_review_content',
    ]

    # product_list에서 전달받는 메타 필드 (INSERT만 사용 — UPDATE는 기존 row 그대로 유지)
    PASSTHROUGH_FIELDS = [
        'page_type',
        'retailer_sku_name',
        'product_url',
        'redirect',
        'fastest_delivery',
        'delivery_availability',
        'available_quantity_for_purchase',
        'main_rank',
        'bsr_rank',
        'calendar_week',
    ]

    # DB 저장 시 코드가 직접 채우는 메타 필드
    SAVE_META_FIELDS = {
        'crawl_datetime': 'CURRENT_TIMESTAMP',
        'account_name': 'account_name',
        'batch_id': 'batch_id',
        'country': 'SEA',
    }

    """
    Amazon TV Detail 페이지 크롤러
    """

    # ========================================================================
    # Init
    # ========================================================================
    def __init__(self, batch_id=None, test_mode=False):
        """초기화. batch_id: 통합 크롤러에서 전달, test_mode: 테스트 모드 여부"""
        super().__init__()
        self.account_name = 'Amazon'
        self.amazon_zip_code = '10001'
        self.product_type = 'TV'
        self.page_type = 'detail'
        self.batch_id = batch_id
        self.test_mode = test_mode
        self.item_mst_table = 'tv_item_mst'  # is_product_excluded 조회 테이블
        self.page = None  # DrissionPage 객체

        # 스크린샷 캡처 설정
        self.capture_enabled = True  # False로 변경하면 캡처 비활성화
        self.capture_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'capture')
        # page_type별 캡처 제한 (main 10개, bsr 10개)
        self.capture_main_count = 0
        self.capture_bsr_count = 0
        self.capture_limit_per_type = 10

        # SPEC DIFF 누적 (run() 끝에 일괄 출력용)
        # 각 entry: {'item': str, 'mst_sku': str|None, 'page_sku': str|None,
        #            'mst_screen_size': str|None, 'screen_size': str|None}
        self.spec_diffs = []
        self.detail_report = {
            'product': 'TV',
            'main_records': 0,
            'bsr_records': 0,
            'detail_records': 0,
            'saved_records': 0,
            'redirects': [],
            'run_errors': [],
            'review_gated_count': 0,     # 리뷰 로그인 게이트 감지 상품 수
            'review_gate_restarts': 0,   # 게이트로 인한 브라우저 재시작 횟수
        }
        self._first_detail_html_saved = False
        # 신뢰 프로필 사용 여부 — detail 전용 (dt_update는 __init__에서 False로 해제).
        # 실제 리프레시/적용은 initialize()에서 브라우저 실행 직전에 수행.
        self.use_trusted_profile = True

    def _normalize_redirect_name(self, value):
        """Compare redirect names by ignoring only whitespace runs and case."""
        return re.sub(r'\s+', ' ', value or '').strip().casefold()

    def _landing_product_title(self):
        try:
            tree = html.fromstring(self.page.html)
            title = tree.xpath('string(//*[@id="productTitle"])')
            return re.sub(r'\s+', ' ', title or '').strip() or None
        except Exception:
            return None

    def resolve_loaded_product_url_for_tv(self, product, product_url, previous_url=None):
        """SIEL-style redirect handling: ASIN mismatch + same name collects landing."""
        current_url = self.page.url

        if previous_url and current_url == previous_url:
            print("[WARNING] Page load failed: URL unchanged")
            raise Exception("Page load failed - URL unchanged")

        if 'amazon.com' not in current_url:
            print("[WARNING] Page load failed: not amazon.com")
            raise Exception("Page load failed - not amazon.com")

        original_item = self.extract_item(product_url)
        current_item = self.extract_item(current_url)
        product['redirect'] = False

        if original_item:
            product['item'] = original_item

        if original_item and current_item and original_item != current_item:
            listing_name = product.get('retailer_sku_name') or ''
            landing_name = self._landing_product_title()
            same_name_redirect = bool(
                listing_name
                and landing_name
                and self._normalize_redirect_name(listing_name) == self._normalize_redirect_name(landing_name)
            )
            product.update({
                'redirect': True,
                'landing_url': current_url,
                'landing_asin': current_item,
                '_original_asin': original_item,
                '_listing_retailer_sku_name': listing_name or None,
                '_landing_retailer_sku_name': landing_name,
            })

            if same_name_redirect:
                product['_redirect_decision'] = 'same_name_collect_landing'
                product['_redirect_use_landing'] = True
                product['item'] = current_item or original_item
                print(
                    "  [WARNING] detail redirect collect: listed ASIN != landing ASIN but names match "
                    f"listed={original_item} landing={current_item} landing_url={current_url}"
                )
                self.detail_report['redirects'].append({
                    'url': product_url,
                    'landing_url': current_url,
                    'asin': original_item,
                    'landing_asin': current_item,
                    'decision': product['_redirect_decision'],
                    'listing_name': listing_name,
                    'landing_name': landing_name,
                })
                return current_url, True

            product.update({
                '_detail_skip': 'asin_mismatch',
                '_redirect_decision': 'name_mismatch_listing_only',
            })
            print(
                "  [WARNING] detail skip: listed ASIN != landing ASIN "
                f"listed={original_item} landing={current_item} landing_url={current_url} "
                f"listing_name={listing_name!r} landing_name={landing_name!r}"
            )
            self.detail_report['redirects'].append({
                'url': product_url,
                'landing_url': current_url,
                'asin': original_item,
                'landing_asin': current_item,
                'decision': product['_redirect_decision'],
                'listing_name': listing_name,
                'landing_name': landing_name,
            })
            return current_url, False

        return current_url, False

    # ========================================================================
    # Run
    # ========================================================================
    def run(self):
        """실행: initialize() → load_product_list() → 제품별 crawl_detail() → save_to_retail_com() → 리소스 정리"""
        try:
            if not self.initialize():
                print("[ERROR] Initialization failed")
                return False

            product_list = self.load_product_list()
            if not product_list:
                print("[ERROR] No products found")
                return False
            self.detail_report['main_records'] = sum(
                1 for product in product_list if product.get('page_type') == 'main'
            )
            # bsr_records: bsr_rank이 부여된 제품 수 (page_type 무관).
            # BSR 100위는 main에도 있으면 page_type='main'으로 UPDATE되므로
            # page_type=='bsr'(BSR 전용)만 세면 안 되고 bsr_rank 보유 여부로 센다.
            self.detail_report['bsr_records'] = sum(
                1 for product in product_list
                if product.get('bsr_rank') not in (None, '', 0, '0')
            )

            total_saved = 0

            for i, product in enumerate(product_list, 1):
                try:
                    retailer_sku_name = product.get('retailer_sku_name') or 'N/A'
                    product_url = product.get('product_url')
                    print(f"\n[{i}/{len(product_list)}] {retailer_sku_name}")

                    combined_data = self.crawl_detail(product)
                    if combined_data:
                        detail_loaded = combined_data is not product
                        if detail_loaded:
                            self.detail_report['detail_records'] += 1
                        if combined_data.get('_detail_skip') == 'asin_mismatch':
                            print("[INFO] 리다이렉트 감지 - product_list 기본 정보만 저장")
                            if self.save_to_retail_com(combined_data):
                                total_saved += 1
                                self.detail_report['saved_records'] += 1
                        else:
                            self.upsert_item_mst(combined_data)
                            if self.save_to_retail_com(combined_data):
                                total_saved += 1
                                self.detail_report['saved_records'] += 1

                    time.sleep(random.uniform(2, 4))

                except Exception as e:
                    error_msg = str(e).lower()
                    print(f"[ERROR] Product {i} failed: {e}")
                    self.detail_report['run_errors'].append({
                        'stage': 'detail',
                        'message': str(e),
                        'url': product_url,
                    })

                    if "dom timeout" in error_msg:
                        next_product = product_list[i] if i < len(product_list) else None
                        next_url = next_product.get('product_url') if next_product else None
                        print(f"[INFO] DOM 타임아웃 - 다음 상품 URL로 브라우저 재시작 후 현재 상품 스킵")
                        if self.save_to_retail_com(product):
                            total_saved += 1
                            self.detail_report['saved_records'] += 1
                        self.restart_browser(next_url)
                        continue

                    if "redirect detected" in error_msg:
                        print("[INFO] 리다이렉트 감지 - product_list 기본 정보만 저장")
                        self.ensure_listing_item(product)
                        product['redirect'] = None
                        if self.save_to_retail_com(product):
                            total_saved += 1
                            self.detail_report['saved_records'] += 1
                        continue

                    if "amazon recovery unresolved" in error_msg:
                        print("[INFO] Amazon 페이지 복구 실패 - product_list 기본 정보만 저장")
                        if self.save_to_retail_com(product):
                            total_saved += 1
                            self.detail_report['saved_records'] += 1
                        continue

                    retry_success = False
                    for retry_attempt in range(1, 3):
                        print(f"[INFO] 문제 발생 URL로 브라우저 재시작 후 재시도 ({retry_attempt}/2)")
                        if not self.restart_browser(product_url):
                            continue

                        try:
                            combined_data = self.crawl_detail(product)
                            if combined_data:
                                detail_loaded = combined_data is not product
                                if detail_loaded:
                                    self.detail_report['detail_records'] += 1
                                if combined_data.get('_detail_skip') == 'asin_mismatch':
                                    print("[INFO] 리다이렉트 감지 - product_list 기본 정보만 저장")
                                    if self.save_to_retail_com(combined_data):
                                        total_saved += 1
                                        self.detail_report['saved_records'] += 1
                                else:
                                    self.upsert_item_mst(combined_data)
                                    if self.save_to_retail_com(combined_data):
                                        total_saved += 1
                                        self.detail_report['saved_records'] += 1
                            print(f"[SUCCESS] 재시도 성공")
                            retry_success = True
                            break
                        except Exception as retry_e:
                            print(f"[ERROR] 재시도 실패 ({retry_attempt}/2): {retry_e}")

                    if retry_success:
                        continue

                    # 모든 에러 발생 시 product_list 기본 정보는 저장
                    if self.save_to_retail_com(product):
                        total_saved += 1
                        self.detail_report['saved_records'] += 1
                    continue

            table_name = 'test_tv_retail_com' if self.test_mode else 'tv_retail_com'
            print(f"[DONE] Processed: {len(product_list)}, Saved: {total_saved}, Table: {table_name}, batch_id: {self.batch_id}")

            self.print_spec_diff_summary()

            return True

        except Exception as e:
            print(f"[ERROR] Crawler failed: {e}")
            traceback.print_exc()
            return False

        finally:
            if self.page:
                self.page.quit()
            if self.db_conn:
                self.db_conn.close()

    # ========================================================================
    # Run Preparation
    # ========================================================================
    def initialize(self):
        """초기화: batch_id 설정 → DB 연결 → XPath 로드 → DrissionPage 설정 → 로그 정리"""
        # 1. batch_id 설정
        if not self.batch_id:
            self.batch_id = 't_a_20260518_084638'

        # 2. DB 연결
        if not self.connect_db():
            return False

        # 3. XPath 로드
        if not self.load_xpaths(self.account_name, self.page_type, 'SEA', 'TV'):
            return False

        # 3.5. 신뢰 프로필 리프레시 + 적용 (detail 전용) — 사본 유효기간이 ~2일이라
        # 매 런 원본에서 새로 발급. 리프레시 실패 시 기존 사본, 그것도 없으면 기본 프로필.
        if getattr(self, 'use_trusted_profile', False):
            refresh_trusted_profile()
            cookies_path = os.path.join(TRUSTED_PROFILE_DIR, 'Default', 'Network', 'Cookies')
            if os.path.exists(cookies_path):
                self.browser_user_data_dir = TRUSTED_PROFILE_DIR
            else:
                print(f"[INFO] 신뢰 프로필 사본 없음({TRUSTED_PROFILE_DIR}) → 기본 프로필 사용")

        # 4. 브라우저 설정 (DrissionPage)
        try:
            if not self.setup_browser():
                return False
        except Exception as e:
            print(f"[ERROR] Initialize failed: Amazon browser setup failed - {e}")
            traceback.print_exc()
            return False

        # 5. 로그 정리
        self.cleanup_old_logs()

        print(f"[INFO] batch_id: {self.batch_id}")
        return True

    def load_product_list(self):
        """amazon_tv_product_list 테이블에서 제품 URL 및 기본 정보 조회.

        같은 batch로 이미 tv_retail_com에 저장된 product_url은 제외한다(skip-existing).
        - 신규 런: 해당 batch 저장 행이 없어 no-op → 전체 로드
        - 재개(--resume-from detail 같은 batch_id): 이미 저장된 제품은 건너뛰고 누락분만
          처리 → 중복 INSERT 없이 중단 지점부터 이어받기.
        """
        if not self.ensure_db_connection():
            print("[ERROR] load_product_list: no DB connection")
            return []
        try:
            cursor = self.db_conn.cursor()

            retail_table = 'test_tv_retail_com' if self.test_mode else 'tv_retail_com'
            query = f"""
                SELECT
                    pl.retailer_sku_name,
                    pl.fastest_delivery,
                    pl.delivery_availability, pl.number_of_units_purchased_past_month,
                    pl.available_quantity_for_purchase,
                    pl.main_rank, pl.bsr_rank, pl.product_url, pl.calendar_week,
                    pl.crawl_datetime, pl.page_type
                FROM amazon_tv_product_list pl
                WHERE pl.account_name = %s AND pl.batch_id = %s AND pl.product_url IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM {retail_table} rc
                      WHERE rc.account_name = pl.account_name
                        AND rc.batch_id = pl.batch_id
                        AND rc.product_url = pl.product_url
                  )
                ORDER BY pl.id
            """

            cursor.execute(query, (self.account_name, self.batch_id))
            rows = cursor.fetchall()
            cursor.close()

            product_list = []
            for row in rows:
                product = {
                    'account_name': self.account_name,
                    'retailer_sku_name': row[0],
                    'fastest_delivery': row[1],
                    'delivery_availability': row[2],
                    'number_of_units_purchased_past_month': row[3],
                    'available_quantity_for_purchase': row[4],
                    'main_rank': row[5],
                    'bsr_rank': row[6],
                    'product_url': row[7],
                    'calendar_week': row[8],
                    'crawl_datetime': row[9],
                    'page_type': row[10],
                }
                product_list.append(product)

            print(f"[INFO] Loaded {len(product_list)} products to crawl (already-saved in this batch excluded)")
            return product_list

        except Exception as e:
            print(f"[ERROR] Failed to load product list: {e}")
            traceback.print_exc()
            return []

    def normalize_available_quantity_for_purchase(self, raw_value):
        """Preserve Amazon availability text when it has no numeric quantity."""
        if not raw_value:
            return None

        text = str(raw_value).strip()
        if not text:
            return None

        quantity = self.convert_first_number(text)
        if quantity:
            return quantity

        if text.lower() == 'in stock':
            return 'In Stock'

        return None

    # ========================================================================
    # Detail Crawl
    # ========================================================================
    def _handle_review_gate(self, item):
        """리뷰 로그인 게이트 감지 시 대응: 기록/스냅샷만 하고 계속 진행.

        재시작·쿠키 초기화·대기는 실측상 게이트를 풀지 못해 하지 않는다
        (모듈 상단 정책 주석 참고). 리뷰 백필은 STEP 4 / dt_update 모드 4 담당.
        """
        self.detail_report['review_gated_count'] = self.detail_report.get('review_gated_count', 0) + 1
        if self.detail_report['review_gated_count'] == 1:
            self.save_debug_html(f'review_gate_{item or "unknown"}')

    def crawl_detail(self, product):
        """상세 페이지 크롤링: 페이지 로드 → 가격/상태 추출 → TV 스펙 추출 → 리뷰 추출"""
        try:
            # ============================================================================================================
            # 상세페이지 진입 및 추출 준비
            # ============================================================================================================
            product_url = product.get('product_url')
            page_type = product.get('page_type') # page_type별로 캡처 갯수를 제한(main 10개 / bsr 10개)
            if not product_url:
                print(f"  [SKIP] product_url 없음 → 크롤링/저장 건너뜀")
                return None

            # 현재 URL 저장 (로드 전)
            previous_url = self.page.url if self.page else None

            self.page.get(product_url)
            time.sleep(random.uniform(1.5, 2.5))

            if not self.recover_amazon_pages():
                print(f"  [WARNING] Amazon 페이지 복구 실패 - 제품 스킵")
                raise Exception("Amazon recovery unresolved")
            self.resolve_loaded_product_url_for_tv(product, product_url, previous_url)
            if product.get('_detail_skip') == 'asin_mismatch':
                return product
            product_url = product.get('product_url')

            page_html = self.page.run_js('return document.documentElement.outerHTML')
            tree = html.fromstring(page_html)

            # item ID 추출 (Amazon 공통 URL 파서 사용)
            item = product.get('item') or self.extract_item(product_url)
            print(f"[item] item ID 추출 {'완료' if item else '실패'}")

            # 캡처 1: 상품 페이지 로드 후 (성공/실패 로그는 take_capture 내부에서 처리, 스킵 시 무로그)
            capture_allowed = self.should_take_capture(page_type)
            if capture_allowed and self.take_capture(item, 1):
                self.mark_capture_count(page_type)

            # =====================================================================================================
            # 필드별 추출
            # =====================================================================================================

            # 3단계 리뷰 섹션 이동 전략 결정: 링크가 있으면 top 이동 후 클릭, 없으면 현재 위치에서 섹션 탐색
            review_link_xpath = self.xpaths.get('review_link', {}).get('xpath')
            has_review_link = bool(review_link_xpath and tree.xpath(review_link_xpath))

            # ========== 1단계: 가격/상태 필드 ==========
            final_sku_price = self.extract_final_sku_price(tree)
            original_sku_price = None
            if final_sku_price and '$' in final_sku_price:
                original_sku_price = self.extract_original_sku_price(tree, 'original_sku_price')
            sku_popularity = self.normalize_sku_popularity(
                self.safe_extract_chain(tree, 'sku_popularity')
            )
            detail_extracted_fields = set()
        
            if not product.get('delivery_availability'):
                delivery_availability = self.extract_delivery_field(tree, 'delivery_availability', separator=' ')
                if delivery_availability:
                    product['delivery_availability'] = delivery_availability
                    detail_extracted_fields.add('delivery_availability')

            if not product.get('fastest_delivery'):
                fastest_delivery = self.extract_delivery_field(tree, 'fastest_delivery', separator=' ')
                if fastest_delivery:
                    product['fastest_delivery'] = fastest_delivery
                    detail_extracted_fields.add('fastest_delivery')

            # BSR 상세 페이지에서 최신 배송/구매/할인/재고 정보를 보정
            if product.get('page_type') == 'bsr':
                number_of_units_purchased_past_month_raw = self.safe_extract_chain(tree, 'number_of_units_purchased_past_month')
                number_of_units_purchased_past_month = self.convert_first_number(number_of_units_purchased_past_month_raw)
                if number_of_units_purchased_past_month:
                    product['number_of_units_purchased_past_month'] = number_of_units_purchased_past_month
                    detail_extracted_fields.add('number_of_units_purchased_past_month')

                discount_type = self.safe_extract_chain_join(tree, 'discount_type', separator=' ')
                if discount_type:
                    product['discount_type'] = discount_type
                    detail_extracted_fields.add('discount_type')

            if not product.get('available_quantity_for_purchase'):
                available_quantity_for_purchase_raw = self.safe_extract_chain(tree, 'available_quantity_for_purchase')
                available_quantity_for_purchase = self.normalize_available_quantity_for_purchase(
                    available_quantity_for_purchase_raw
                )
                if available_quantity_for_purchase:
                    product['available_quantity_for_purchase'] = available_quantity_for_purchase
                    detail_extracted_fields.add('available_quantity_for_purchase')

            # ========== 2단계: TV 스펙 ==========
            found_section = self.scroll_to_section(
                ['product_information_section', 'technical_details_section'],
                label='TV 스펙 섹션',
            )
            if found_section == 'product_information_section':
                self.open_details_sections(['item_details_button', 'measurements_button'])
            tree = html.fromstring(self.page.html)
            if capture_allowed:
                self.take_capture(item, 2)

            # 마스터 테이블에서 기존 TV 스펙 조회
            mst_screen_size, mst_sku = self.get_tv_specs_from_mst(item)

            sku, sku_source, page_sku = self.extract_sku(tree, mst_sku, found_section, self.product_type)
            model_year = self.extract_model_year(tree)

            screen_size, spec_source = self.extract_screen_size(
                product.get('retailer_sku_name'),
                tree,
                mst_screen_size,
            )

            # ========== 3단계: 리뷰 관련 필드 ==========
            self.move_to_review_section(has_review_link)
            if capture_allowed:
                self.take_capture(item, 3)
            page_html = self.page.html
            tree = html.fromstring(page_html)

            # 진단용: 런당 첫 상품의 리뷰 섹션 시점 DOM 스냅샷 (장애 시 원인 즉시 확인용)
            if not self._first_detail_html_saved:
                self._first_detail_html_saved = True
                self.save_debug_html(f'detail_first_{item or "unknown"}')

            # 리뷰 없음 문구를 먼저 감지하고, 별점/별점 수는 한 번만 추출한다.
            no_review_keywords = ['no customer reviews', 'there are 0 customer reviews']
            page_text = tree.text_content().lower() if tree is not None else ''
            is_no_reviews = any(keyword in page_text for keyword in no_review_keywords)

            star_rating = self.extract_star_rating(tree, 'star_rating')
            count_of_star_ratings = self.extract_count_of_star_rating(tree, 'count_of_star_ratings')

            if is_no_reviews:
                # 리뷰 없음인데 둘 다 미추출이면 기본값 할당, 하나만 추출되면 실패값으로 그대로 둔다.
                if not star_rating and not count_of_star_ratings:
                    star_rating = 'No customer reviews'
                    count_of_star_ratings = '0'

            summarized_review_content = self.safe_extract_chain(tree, 'summarized_review_content')

            detailed_review_content = None
            extracted_count = 0
            review_gated = False
            if is_no_reviews:
                print(f"  [리뷰] 리뷰 없음")
            else:
                print(f"  [리뷰] 상품 상세페이지에서 추출 중...")
                detailed_review_content, extracted_count, review_gated = self.extract_reviews_with_retry(
                    tree, max_reviews=20, page_html=page_html
                )
                print(f"  [리뷰] 상품 상세페이지 추출 완료: {extracted_count}건")
                if review_gated:
                    self._handle_review_gate(item)

            # 결합된 데이터
            combined_data = product.copy()
            combined_data.update({
                'item': item,
                'redirect': product.get('redirect', False),
                'sku': sku,
                'star_rating': star_rating,
                'count_of_star_ratings': count_of_star_ratings,
                'final_sku_price': final_sku_price,
                'original_sku_price': original_sku_price,
                'discount_type': product.get('discount_type'),
                'sku_popularity': sku_popularity,
                'number_of_units_purchased_past_month': product.get('number_of_units_purchased_past_month'),
                'model_year': model_year,
                'screen_size': screen_size,
                'summarized_review_content': summarized_review_content,
                'detailed_review_content': detailed_review_content,
            })

            # 마스터 vs 이번 상세 크롤링 결과 비교 — 다르면 누적 (run() 끝에 일괄 출력)
            has_sku_diff = mst_sku and page_sku and mst_sku != page_sku
            has_screen_size_diff = mst_screen_size and screen_size and mst_screen_size != screen_size
            if has_sku_diff or has_screen_size_diff:
                self.spec_diffs.append({
                    'item': item,
                    'mst_sku': mst_sku,
                    'page_sku': page_sku,
                    'mst_screen_size': mst_screen_size,
                    'screen_size': screen_size,
                })

            # ──── 결과 요약 (트리 구조) ────
            print(f"\n──── 결과 요약 ────")
            print(f"  ├─ item: {item or '-'}")
            print(f"  ├─ sku: {f'{sku} (출처: {sku_source})' if sku and sku_source else (sku or '-')}")
            print(f"  ├─ final_sku_price: {final_sku_price or '-'}")
            print(f"  ├─ original_sku_price: {original_sku_price or '-'}")
            print(f"  ├─ star_rating: {star_rating or '-'}")
            print(f"  ├─ count_of_star_ratings: {count_of_star_ratings or '-'}")
            print(f"  ├─ sku_popularity: {sku_popularity or '-'}")
            if 'number_of_units_purchased_past_month' in detail_extracted_fields:
                print(f"  ├─ number_of_units_purchased_past_month: {product.get('number_of_units_purchased_past_month') or '-'}")
            if 'discount_type' in detail_extracted_fields:
                print(f"  ├─ discount_type: {product.get('discount_type') or '-'}")
            if 'delivery_availability' in detail_extracted_fields:
                print(f"  ├─ delivery_availability: {product.get('delivery_availability') or '-'}")
            if 'fastest_delivery' in detail_extracted_fields:
                print(f"  ├─ fastest_delivery: {product.get('fastest_delivery') or '-'}")
            if 'available_quantity_for_purchase' in detail_extracted_fields:
                print(f"  ├─ available_quantity_for_purchase: {product.get('available_quantity_for_purchase') or '-'}")
            print(f"  ├─ model_year: {model_year or '-'}")
            print(f"  ├─ screen_size: {f'{screen_size} (출처: {spec_source})' if screen_size and spec_source else (screen_size or '-')}")
            print(f"  ├─ summarized_review_content: {'있음' if summarized_review_content else '-'}")
            print(f"  └─ detailed_review_content: {extracted_count}개{' (로그인 게이트)' if review_gated else ''}")

            return combined_data

        except Exception as e:
            error_msg = str(e).lower()
            print(f"[ERROR] Detail crawl failed: {e}")
            
            # 타임아웃/리다이렉트/복구 실패는 run()의 전용 분기에서 처리한다.
            if (
                "timeout" in error_msg
                or "time out" in error_msg
                or "redirect detected" in error_msg
                or "amazon recovery unresolved" in error_msg
            ):
                raise

            return product

    # ========================================================================
    # Save
    # ========================================================================
    def upsert_item_mst(self, product):
        """tv_item_mst 테이블에 INSERT 또는 UPDATE
        - 조회 결과 없음 → INSERT (sku, screen_size)
        - 조회 결과 있음 → 기존 값이 NULL/빈값인 필드만 UPDATE
        """
        item = product.get('item')
        if not item:
            return

        if not self.ensure_db_connection():
            print(f"[ERROR] upsert_item_mst skipped (no DB): {item}")
            return

        try:
            cursor = self.db_conn.cursor()
            new_sku = product.get('sku') or 'no sku'
            product_url = product.get('product_url')
            new_screen_size = product.get('screen_size') or None

            # 기존 데이터 조회
            cursor.execute("""
                SELECT sku, screen_size FROM tv_item_mst
                WHERE item = %s AND account_name = %s
            """, (item, self.account_name))

            row = cursor.fetchone()

            if row is None:
                # 조회 결과 없음 → INSERT
                cursor.execute("""
                    INSERT INTO tv_item_mst (item, account_name, sku, product_url, screen_size)
                    VALUES (%s, %s, %s, %s, %s)
                """, (item, self.account_name, new_sku, product_url, new_screen_size))
                self.db_conn.commit()
                print(f"  → DB: ITEM_MST INSERT")
            else:
                # 기존 값이 없는 필드만 업데이트
                existing_sku, existing_screen_size = row
                updates = []
                params = []
                existing_sku_value = str(existing_sku).strip() if existing_sku else ''
                can_update_sku = (
                    new_sku
                    and existing_sku_value.lower() in ('', 'no sku')
                    and existing_sku_value.lower() != new_sku.lower()
                )

                if can_update_sku:
                    updates.append("sku = %s")
                    params.append(new_sku)
                if not existing_screen_size and new_screen_size:
                    updates.append("screen_size = %s")
                    params.append(new_screen_size)

                if updates:
                    # 업데이트할 필드와 값 저장 (로그용)
                    updated_info = []
                    if can_update_sku:
                        updated_info.append(f"sku={new_sku}")
                    if not existing_screen_size and new_screen_size:
                        updated_info.append(f"screen_size={new_screen_size}")

                    updates.append("product_url = %s")
                    params.append(product_url)
                    updates.append("updated_at = %s")
                    params.append(datetime.now())
                    params.extend([item, self.account_name])

                    cursor.execute(f"""
                        UPDATE tv_item_mst SET {', '.join(updates)}
                        WHERE item = %s AND account_name = %s
                    """, params)
                    self.db_conn.commit()
                    print(f"  → DB: ITEM_MST UPDATE ({', '.join(updated_info)})")
                else:
                    pass  # ITEM_MST 업데이트할 필드 없음

            cursor.close()

        except Exception as e:
            print(f"[ERROR] upsert_item_mst failed: {item}: {e}")
            self.safe_rollback()

    def save_to_retail_com(self, product):
        """DB 저장: 1개씩 INSERT.

        컬럼 구성: EXTRACTED_FIELDS (추출) + PASSTHROUGH_FIELDS (전달) + SAVE_META_FIELDS (저장 메타)
        새 추출 필드 추가 시 EXTRACTED_FIELDS 리스트에만 추가하면 INSERT/UPDATE 모두 자동 반영.
        """
        if not product:
            return False

        self.ensure_listing_item(product)
        if 'redirect' not in product:
            product['redirect'] = False

        if not self.ensure_db_connection():
            print(f"[ERROR] DB save skipped (no DB): {product.get('item')}")
            return False

        try:
            cursor = self.db_conn.cursor()

            # 테스트 모드면 test_tv_retail_com, 통합 크롤러면 tv_retail_com
            table_name = 'test_tv_retail_com' if self.test_mode else 'tv_retail_com'

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_meta = {
                field: (
                    now if source == 'CURRENT_TIMESTAMP'
                    else getattr(self, source) if hasattr(self, source)
                    else source
                )
                for field, source in self.SAVE_META_FIELDS.items()
            }
            insert_data = {
                **{field: product.get(field) for field in self.EXTRACTED_FIELDS},
                **{field: product.get(field) for field in self.PASSTHROUGH_FIELDS},
                **save_meta,
            }

            columns = list(insert_data.keys())
            values = list(insert_data.values())
            placeholders = ', '.join(['%s'] * len(columns))
            insert_query = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"

            cursor.execute(insert_query, values)
            self.db_conn.commit()
            cursor.close()
            return True

        except Exception as e:
            print(f"[ERROR] DB save failed: {product.get('item')}: {e}")
            traceback.print_exc()
            self.safe_rollback()
            return False

    # ========================================================================
    # Session / Defense
    # ========================================================================
    # ========================================================================
    # Item / MST
    # ========================================================================
    def get_tv_specs_from_mst(self, item):
        """마스터 테이블에서 TV 스펙 및 SKU 조회"""
        if not item:
            return None, None

        if not self.ensure_db_connection():
            print("  [WARNING] get_tv_specs_from_mst skipped (no DB)")
            return None, None

        try:
            cursor = self.db_conn.cursor()
            cursor.execute("""
                SELECT screen_size, sku FROM tv_item_mst
                WHERE item = %s AND account_name = %s AND is_product = TRUE
            """, (item, self.account_name))
            row = cursor.fetchone()
            cursor.close()

            if row:
                return row[0], row[1]
            return None, None
        except Exception as e:
            print(f"  [WARNING] get_tv_specs_from_mst failed: {e}")
            return None, None

    # ========================================================================
    # Extract Helpers
    # ========================================================================
    def extract_model_year(self, *trees):
        """DB XPath 기준으로 model_year 추출.

        1. Details tree → 상세페이지 본문 tree 순으로 XPath chain 추출
        2. "2022/2023"처럼 연도 범위이면 더 큰 연도 선택
        3. 4자리 연도만 최종 model_year로 반환
        """
        for tree in trees:
            if tree is None:
                continue

            try:
                year_text = self.safe_extract_chain(tree, 'model_year')
                if not year_text:
                    continue

                year_text = str(year_text).strip()

                if '/' in year_text:
                    year_numbers = [
                        int(year.strip())
                        for year in year_text.split('/')
                        if year.strip().isdigit() and len(year.strip()) == 4
                    ]
                    if year_numbers:
                        return str(max(year_numbers))

                match = re.search(r'\b(\d{4})\b', year_text)
                if match:
                    return match.group(1)

            except Exception as e:
                print(f"  [WARNING] extract_model_year failed: {e}")

        return None

    def extract_screen_size(self, retailer_sku_name, tree, mst_screen_size):
        """screen_size 최종값 결정.

        1. 상품명 정규식: SAMSUNG 77" Class / 50-Inch / 98" Q Series → N inches
        2. XPath 추출: 숫자만 추출해 N inches로 정규화
        3. 마스터 값 사용
        """
        if retailer_sku_name:
            screen_size = self.extract_screen_size_by_regex(
                retailer_sku_name,
                r'(\d+\.?\d*)(?:[\s-]*inch(?:es)?|["“”″])',
                'retailer_sku_name',
            )
            if screen_size:
                return screen_size, "제품명"

        if tree is not None:
            screen_size = self.safe_extract_chain(tree, 'screen_size')
            if screen_size:
                screen_size = self.extract_screen_size_by_regex(
                    screen_size,
                    r'([\d.]+)\s*(?:in(?:ch(?:es)?)?|")?',
                    'XPath',
                )
                if screen_size:
                    return screen_size, "Details"

        if mst_screen_size:
            return mst_screen_size, "마스터"

        return None, None

    def extract_screen_size_by_regex(self, text, pattern, source):
        """정규식으로 screen_size 숫자를 추출해 'N inches'로 반환한다."""
        try:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                size_number = match.group(1)
                if 10 <= float(size_number) <= 150:
                    return f"{size_number} inches"
        except Exception as e:
            print(f"  [WARNING] extract_screen_size {source} regex failed: {e}")
        return None

def main():
    """개별 실행 진입점 (테스트 모드, 기본 배치 ID 사용)"""
    crawler = AmazonTVDetailCrawler(batch_id=None, test_mode=True)
    crawler.run()
    input("Press Enter to exit...")


if __name__ == '__main__':
    main()
