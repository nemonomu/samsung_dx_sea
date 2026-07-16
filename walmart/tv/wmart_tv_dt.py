"""
Walmart TV Detail 페이지 크롤러

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
import traceback
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from lxml import html
from DrissionPage import ChromiumPage

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

from common.walmart_base import WalmartBaseCrawler
from walmart.tv.wmart_tv_next_data import (
    WalmartNextDataClient,
    build_item_url,
    build_review_url,
    format_reviews,
    normalize_int,
    parse_detail_product,
    parse_discount_type_from_html,
    parse_review_page,
)


# ================================================================================
# 브랜드별 SKU 정규식 패턴
# 각 항목: (브랜드명, 매칭패턴, 'contains'/'regex', [(정규식, 'name'/'url'), ...])
#   - 'contains': 단순 부분 문자열 매칭 (대소문자 무관)
#   - 'regex': 단어 경계 등 정규식 매칭 (case-insensitive)
#   - 패턴 리스트는 순서대로 시도, 첫 매칭값 반환
# ================================================================================
WALMART_TV_BRAND_SKU_PATTERNS = [
    ('Samsung', 'samsung', 'contains', [
        (r'([UQS][A-Z0-9]{8,15})', 'name'),
        (r'-([UQS][A-Z0-9]{8,15})-', 'url'),
        (r'(\d{2,3}[UQS]\d{4,5}[A-Z]?)', 'name'),
        (r'-(\d{2,3}[UQS]\d{4,5}[A-Z]?)-', 'url'),
    ]),
    ('LG', r'\bLG\b', 'regex', [
        (r'(\d{2}[A-Z]+\d{2,4}[A-Z]*\d?[A-Z]+(?:-[A-Z]{2,4})?|OLED\d{2,3}[A-Z]\d[A-Z]+)', 'name'),
        (r'[-/](\d{2}[A-Z]+\d{2,4}[A-Z]*\d?[A-Z]*(?:-[A-Z]{2,4})?|OLED\d{2,3}[A-Z]\d[A-Z]+)[-/]', 'url'),
    ]),
    ('onn', r'\bonn\b', 'regex', [
        (r'(TV[A-Z]+\d+|\d{9}|\d{2}[A-Z]\d[A-Z]\d|[A-Z]{2,3}\d{2,3}[A-Z]?-\d{3,5})', 'name'),
        (r'-(TV[A-Z]+\d+|\d{9}|\d{2}[A-Z]\d[A-Z]\d|[A-Z]{2,3}\d{2,3}[A-Z]?-\d{3,5})-', 'url'),
    ]),
    ('VIZIO', 'vizio', 'contains', [
        (r'((?:[VDMPO]\d|OLED)[A-Za-z0-9]+-[A-Z]?\d{1,4}|[VDMPO][A-Za-z]+\d+[A-Za-z]*-\d{1,4}|(?:[VDMPO]\d|OLED)\d*[A-Za-z]+\d+[A-Z]*)', 'name'),
        (r'[-/]((?:[VDMPO]\d|OLED)[A-Za-z0-9]+-[A-Z]?\d{1,4}|[VDMPO][A-Za-z]+\d+[A-Za-z]*-\d{1,4}|(?:[VDMPO]\d|OLED)\d*[A-Za-z]+\d+[A-Z]*)[-/]', 'url'),
    ]),
    ('Westinghouse', 'westinghouse', 'contains', [
        (r'(W[A-Z]\d+[A-Z]+\d+)', 'name'),
        (r'-(W[A-Z]\d+[A-Z]+\d+)-', 'url'),
    ]),
    ('TCL', 'tcl', 'contains', [
        (r'(\d{2,3}[A-Z]{1,2}\d{1,3}[A-Z0-9]{1,5})', 'name'),
        (r'-(\d{2,3}[A-Z]{1,2}\d{1,3}[A-Z0-9]{1,5})-', 'url'),
    ]),
    ('Naxa', 'naxa', 'contains', [
        (r'(N[A-Z]{1,2}-\d{3,5})', 'name'),
        (r'-(N[A-Z]{1,2}-\d{3,5})-', 'url'),
    ]),
    ('Philips', 'philips', 'contains', [
        (r'(\d{0,3}[A-Z]+\d+[A-Z]?/[A-Z]?\d+)', 'name'),
        (r'-(\d{0,3}[A-Z]+\d+[A-Z]?/[A-Z]?\d+)-', 'url'),
    ]),
    ('Hiro', 'hiro', 'contains', [
        (r'(H\d{2}[A-Z0-9]{3,5})', 'name'),
        (r'-(H\d{2}[A-Z0-9]{3,5})-', 'url'),
    ]),
    ('Hisense', 'hisense', 'contains', [
        (r'(\d{2,3}[A-Z]{1,2}\d[A-Z0-9]{1,5}|\d{2,3}[A-Z]{2})', 'name'),
        (r'-(\d{2,3}[A-Z]{1,2}\d[A-Z0-9]{1,5}|\d{2,3}[A-Z]{2})-', 'url'),
    ]),
    ('JVC', 'jvc', 'contains', [
        (r'(LT-\d{2,3}[A-Z]+\d+)', 'name'),
        (r'-(LT-\d{2,3}[A-Z]+\d+)-', 'url'),
    ]),
    ('Element', 'element', 'contains', [
        (r'(E[A-Z]?\d+[A-Z]+\d+[A-Z]+)', 'name'),
        (r'-(E[A-Z]?\d+[A-Z]+\d+[A-Z]+)-', 'url'),
    ]),
    ('Supersonic', 'supersonic', 'contains', [
        (r'(S[Cc]-\d{3,5}[A-Za-z]*)', 'name'),
        (r'[-/](S[Cc]-\d{3,5}[A-Za-z]*)[-/]', 'url'),
    ]),
    ('SANSUI', 'sansui', 'contains', [
        (r'(S\d{2,3}[A-Z]+)', 'name'),
        (r'-(S\d{2,3}[A-Z]+)[-/]', 'url'),
    ]),
    ('RCA', r'\bRCA\b', 'regex', [
        # 1순위: TC- 시리즈
        (r'(TC-[A-Z]+\d+[A-Z]?-[A-Z]+\d+)', 'name'),
        (r'[-/](TC-[A-Z]+\d+[A-Z]?-[A-Z]+\d+)[-/]', 'url'),
        # 2순위: J 시리즈
        (r'(J\d{2}[A-Z]+\d+[A-Z]?)', 'name'),
        (r'[-/](J\d{2}[A-Z]+\d+[A-Z]?)[-/]', 'url'),
        # 3순위: DHT 시리즈
        (r'(DHT\d{3,5}[A-Z]?)', 'name'),
        (r'[-/](DHT\d{3,5}[A-Z]?)[-/]', 'url'),
        # 4순위: R 시리즈
        (r'(R[A-Z]{2,4}\d{3,5}[A-Z]?(?:-[A-Z]{1,3})?)', 'name'),
        (r'[-/](R[A-Z]{2,4}\d{3,5}[A-Z]?(?:-[A-Z]{1,3})?)[-/]', 'url'),
    ]),
    ('Sharp', 'sharp', 'contains', [
        (r'([\dA-Z][TCMPB]-?[A-Z\d]{6,12})', 'name'),
        (r'[-/]([\dA-Z][TCMPB]-?[A-Z\d]{6,12})[-/]', 'url'),
    ]),
    ('Emerson', 'emerson', 'contains', [
        (r'(E[A-Z]{1,3}-\d{3,5})', 'name'),
        (r'-(E[A-Z]{1,3}-\d{3,5})-', 'url'),
    ]),
    ('NavaTV', 'navatv', 'contains', [
        (r'(NVTV\d{2,4}[A-Z]+)', 'name'),
        (r'-(NVTV\d{2,4}[A-Z]+)-', 'url'),
    ]),
    ('Sony', 'sony', 'contains', [
        (r'([KX][A-Z]{1,2}-?\d{2,3}[A-Z]+\d*[A-Z]?\d?|[KX]-\d{2,3}[A-Z]+\d*[A-Z]?\d?|[KX]\d{2,3}[A-Z]+\d+[A-Z]?\d?)', 'name'),
        (r'[-/]([KX][A-Z]{1,2}-?\d{2,3}[A-Z]+\d*[A-Z]?\d?|[KX]-\d{2,3}[A-Z]+\d*[A-Z]?\d?|[KX]\d{2,3}[A-Z]+\d+[A-Z]?\d?)[-/]', 'url'),
    ]),
    ('GPX', r'\bGPX\b', 'regex', [
        (r'(T[A-Z]{1,2}\d{3,5}[A-Z]{0,2})', 'name'),
        (r'[-/](T[A-Z]{1,2}\d{3,5}[A-Z]{0,2})[-/]', 'url'),
    ]),
    ('Tyler', 'tyler', 'contains', [
        (r'(TTV\d{3}-\d{1,2})', 'name'),
        (r'-(TTV\d{3}-\d{1,2})-', 'url'),
    ]),
    ('Trexonic', 'trexonic', 'contains', [
        (r'(\d{8,10}[A-Z]?)', 'name'),
        (r'-(\d{8,10}[A-Z]?)-', 'url'),
    ]),
    ('Elecsung', 'elecsung', 'contains', [
        (r'(ELE[A-Z]+\d{2,3}[A-Z]+)', 'name'),
        (r'-(ELE[A-Z]+\d{2,3}[A-Z]+)-', 'url'),
    ]),
    ('JENSEN', 'jensen', 'contains', [
        (r'(JTV\d{4}[A-Z]*)', 'name'),
        (r'-(JTV\d{4}[A-Z]*)-', 'url'),
    ]),
    ('SYLVOX', 'sylvox', 'contains', [
        (r'([A-Z]{2}\d{2,3}[A-Z]\d[A-Z]+)', 'name'),
        (r'[-/]([A-Z]{2}\d{2,3}[A-Z]\d[A-Z]+)[-/]', 'url'),
    ]),
    ('iLive', 'ilive', 'contains', [
        (r'(IT[A-Z]+\d{3,5}[A-Z]?)', 'name'),
        (r'[-/](IT[A-Z]+\d{3,5}[A-Z]?)[-/]', 'url'),
    ]),
    ('Sceptre', 'sceptre', 'contains', [
        (r'([A-Z]\d{3,4}[A-Z]{1,3}-[A-Z]{1,5})', 'name'),
        (r'[-/]([A-Z]\d{3,4}[A-Z]{1,3}-[A-Z]{1,5})[-/]', 'url'),
    ]),
    ('Roku', r'\broku\s+\d', 'regex', [
        (r'(\d{2,3}R\d[A-Z]\d)', 'name'),
        (r'-(\d{2,3}R\d[A-Z]\d)-', 'url'),
    ]),
]


class WalmartTVDetailCrawler(WalmartBaseCrawler):
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
        'count_of_reviews',
        'star_rating',
        'count_of_star_ratings',
        'final_sku_price',
        'original_sku_price',
        'savings',
        'discount_type',
        'sku_popularity',
        'number_of_ppl_purchased_yesterday',
        'number_of_ppl_added_to_carts',
        'model_year',
        'screen_size',
        'retailer_sku_name_similar',
        'detailed_review_content',
    ]

    # product_list에서 전달받는 메타 필드 (INSERT만 사용 — UPDATE는 기존 row 그대로 유지)
    PASSTHROUGH_FIELDS = [
        'page_type',
        'retailer_sku_name',
        'product_url',
        'offer',
        'pick_up_availability',
        'fastest_delivery',
        'delivery_availability',
        'sku_status',
        'available_quantity_for_purchase',
        'inventory_status',
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
    Walmart TV Detail 페이지 크롤러
    """

    def __init__(self, batch_id=None, test_mode=False):
        """초기화. batch_id: 통합 크롤러에서 전달, test_mode: 테스트 모드 여부"""
        super().__init__()
        self.account_name = 'Walmart'
        self.walmart_zip_code = '11581'
        self.page_type = 'detail'
        self.batch_id = batch_id
        self.test_mode = test_mode

        # DrissionPage 드라이버 (Selenium driver 대신 사용)
        self.page = None
        self.next_data_client = WalmartNextDataClient()
        self.skip_walmart_search = True
        self.detail_next_data_workers = self._env_int('WALMART_TV_DETAIL_WORKERS', 4)
        self.detail_next_data_chunk_size = self._env_int('WALMART_TV_DETAIL_CHUNK_SIZE', 40)
        self.similar_json_fallback_enabled = self._env_bool('WALMART_TV_SIMILAR_JSON_FALLBACK', True)
        self.similar_json_wait_ms = self._env_int('WALMART_TV_SIMILAR_JSON_WAIT_MS', 6000, minimum=0)
        self.parallel_miss_reasons = {}


        # SPEC DIFF 누적 (run() 끝에 일괄 출력용)
        # 각 entry: {'item': str, 'mst_sku': str|None, 'extracted_sku': str|None,
        #            'brand_sku': str|None, 'page_sku': str|None, 'brand_name': str|None,
        #            'mst_screen_size': str|None, 'extracted_screen_size': str|None,
        #            'name_screen_size': str|None, 'page_screen_size': str|None}
        self.spec_diffs = []
        self.detail_report = {
            'product': 'TV',
            'main_records': 0,
            'bsr_records': 0,
            'detail_records': 0,
            'saved_records': 0,
            'redirects': [],
            'run_errors': [],
        }

    def _record_saved(self, detail=False):
        self.detail_report['saved_records'] += 1
        if detail:
            self.detail_report['detail_records'] += 1

    def _record_run_error(self, stage, product, message):
        self.detail_report['run_errors'].append({
            'stage': stage,
            'url': product.get('product_url') if product else None,
            'message': str(message),
        })

    def _record_redirect(self, product, message):
        self.detail_report['redirects'].append({
            'stage': 'detail',
            'url': product.get('product_url') if product else None,
            'message': str(message),
            'decision': 'listing_only',
        })

    def run(self):
        """실행: initialize() → load_product_list() → 제품별 detail 수집/저장 → 리소스 정리"""
        try:
            if not self.initialize():
                print("[ERROR] Initialization failed")
                return False

            product_list = self.load_product_list()
            if not product_list:
                print("[ERROR] No products found")
                return False

            self.detail_report['main_records'] = sum(
                1 for product in product_list
                if str(product.get('page_type') or '').lower() == 'main'
            )
            # bsr_records: bsr_rank이 부여된 제품 수 (page_type 무관).
            # BSR 100위는 main에도 있으면 page_type='main'으로 UPDATE되므로
            # page_type=='bsr'(BSR 전용)만 세면 안 되고 bsr_rank 보유 여부로 센다.
            self.detail_report['bsr_records'] = sum(
                1 for product in product_list
                if product.get('bsr_rank') not in (None, '', 0, '0')
            )

            total_saved = 0
            indexed_products = list(enumerate(product_list, 1))
            total_products = len(product_list)

            for chunk_start in range(0, len(indexed_products), self.detail_next_data_chunk_size):
                chunk = indexed_products[chunk_start:chunk_start + self.detail_next_data_chunk_size]
                fast_results = self.collect_detail_next_data_parallel(chunk)

                for i, product in chunk:
                    combined_data = None
                    try:
                        retailer_sku_name = product.get('retailer_sku_name') or 'N/A'
                        product_url = product.get('product_url', 'N/A')
                        url_display = product_url[:80] + '...' if len(product_url) > 80 else product_url
                        print(f"\n[{i}/{total_products}] {retailer_sku_name}")
                        print(f"  URL: {url_display}")

                        combined_data = fast_results.get(i)
                        if combined_data:
                            review_count = self._formatted_review_count(combined_data.get('detailed_review_content'))
                            print(
                                f"  [NEXT_DATA parallel HIT] source={combined_data.get('_detail_source')}, "
                                f"item={combined_data.get('item') or '-'}, "
                                f"price={combined_data.get('final_sku_price') or '-'}, reviews={review_count}"
                            )
                        else:
                            miss_reason = self.parallel_miss_reasons.get(i)
                            if miss_reason:
                                print(f"  [NEXT_DATA parallel MISS] reason={miss_reason}; browser fallback")
                            combined_data = self.crawl_detail(product, skip_fast=True)

                        if combined_data:
                            detail_loaded = combined_data is not product
                            if self.save_detail_result(combined_data):
                                total_saved += 1
                                self._record_saved(detail=detail_loaded)

                        if combined_data and combined_data is not product and combined_data.get('_detail_source') in ('direct', 'zenrows_static', 'zenrows_js'):
                            time.sleep(random.uniform(0.05, 0.15))
                        else:
                            time.sleep(random.uniform(2, 4))

                    except Exception as e:
                        error_msg = str(e).lower()
                        print(f"[ERROR] Product {i} failed: {e}")

                        # DOM 타임아웃 → 브라우저 재시작만 하고 해당 제품은 스킵 (재시도 안 함)
                        if "dom timeout" in error_msg:
                            print(f"[INFO] DOM 타임아웃 - 브라우저 재시작 후 다음 제품으로")
                            self.restart_browser()
                            if self.save_detail_result(product):
                                total_saved += 1
                                self._record_saved()
                            self._record_run_error('detail', product, e)
                            continue

                        if "redirect detected" in error_msg:
                            print("[INFO] 리다이렉트 감지 - product_list 기본 정보만 저장")
                            if self.save_detail_result(product):
                                total_saved += 1
                                self._record_saved()
                            self._record_redirect(product, e)
                            continue

                        # 일반 타임아웃 또는 페이지 로드 실패 → 브라우저 재시작 후 재시도
                        if "timeout" in error_msg or "time out" in error_msg or "url unchanged" in error_msg:
                            print(f"[INFO] 브라우저 재시작 후 재시도")
                            if self.restart_browser():
                                try:
                                    combined_data = self.crawl_detail(product, skip_fast=True)
                                    if combined_data:
                                        detail_loaded = combined_data is not product
                                        if self.save_detail_result(combined_data):
                                            total_saved += 1
                                            self._record_saved(detail=detail_loaded)
                                    print(f"[SUCCESS] 재시도 성공: {retailer_sku_name[:30]}")
                                    continue
                                except Exception as retry_e:
                                    print(f"[ERROR] 재시도 실패: {retry_e}")
                                    self._record_run_error('detail_retry', product, retry_e)

                        # 모든 에러 발생 시 product_list 기본 정보는 저장
                        if self.save_detail_result(product):
                            total_saved += 1
                            self._record_saved()
                        self._record_run_error('detail', product, e)
                        continue

            table_name = 'test_tv_retail_com' if self.test_mode else 'tv_retail_com'
            print(f"[DONE] Processed: {len(product_list)}, Saved: {total_saved}, Table: {table_name}, batch_id: {self.batch_id}")

            # ===== SPEC DIFF 일괄 출력 (마스터 vs 페이지 추출 값이 다른 item들) =====
            if self.spec_diffs:
                print(f"\n{'=' * 80}")
                print(f"[SPEC DIFF] 마스터 vs 페이지 추출 값 불일치: 총 {len(self.spec_diffs)}건")
                print(f"{'=' * 80}")
                for d in self.spec_diffs:
                    parts = [f"item={d['item']}"]
                    if d['mst_sku'] != d.get('extracted_sku'):
                        brand_info = f" (brand={d['brand_name']})" if d.get('brand_name') else ''
                        parts.append(
                            f"sku: mst={d['mst_sku']!r} / extracted={d.get('extracted_sku')!r}"
                            f" [brand={d.get('brand_sku')!r} / page={d.get('page_sku')!r}]{brand_info}"
                        )
                    if d['mst_screen_size'] != d.get('extracted_screen_size'):
                        parts.append(
                            f"screen_size: mst={d['mst_screen_size']!r} / extracted={d.get('extracted_screen_size')!r}"
                            f" [name={d.get('name_screen_size')!r} / page={d.get('page_screen_size')!r}]"
                        )
                    print("  " + " | ".join(parts))
                print(f"{'=' * 80}\n")
            else:
                print(f"\n[SPEC DIFF] 마스터 vs 페이지 추출 값 불일치 없음")

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

    def initialize(self):
        """초기화: batch_id 설정 → DB 연결 → XPath 로드 → DrissionPage 설정 → 로그 정리"""
        # 1. batch_id 설정
        if not self.batch_id:
            self.batch_id = 't_w_20260512_211946'

        # 2. DB 연결
        if not self.connect_db():
            return False

        # 3. XPath 로드
        if not self.load_xpaths(self.account_name, self.page_type, 'SEA', 'TV'):
            return False

        # 4. NextData HTTP client ready. Browser opens only as lazy fallback.
        self.next_data_client = WalmartNextDataClient()
        self.skip_walmart_search = True

        # 5. Log cleanup
        self.cleanup_old_logs()

        print(f"[INFO] batch_id: {self.batch_id}")
        return True

    def load_product_list(self):
        """wmart_tv_product_list 테이블에서 제품 URL 및 기본 정보 조회"""
        try:
            cursor = self.db_conn.cursor()

            query = """
                SELECT
                    pl.retailer_sku_name,
                    pl.offer, pl.pick_up_availability, pl.fastest_delivery,
                    pl.delivery_availability, pl.sku_status,
                    pl.available_quantity_for_purchase, pl.inventory_status,
                    pl.main_rank, pl.bsr_rank, pl.product_url, pl.calendar_week,
                    pl.crawl_datetime, pl.page_type
                FROM wmart_tv_product_list pl
                WHERE pl.account_name = %s
                  AND pl.batch_id = %s
                  AND pl.product_url IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM tv_retail_com rc
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
                    'offer': row[1],
                    'pick_up_availability': row[2],
                    'fastest_delivery': row[3],
                    'delivery_availability': row[4],
                    'sku_status': row[5],
                    'available_quantity_for_purchase': row[6],
                    'inventory_status': row[7],
                    'main_rank': row[8],
                    'bsr_rank': row[9],
                    'product_url': row[10],
                    'calendar_week': row[11],
                    'crawl_datetime': row[12],
                    'page_type': row[13],
                }
                product_list.append(product)

            print(f"[INFO] Loaded {len(product_list)} products")
            return product_list

        except Exception as e:
            print(f"[ERROR] Failed to load product list: {e}")
            traceback.print_exc()
            return []

    def ensure_db_connection(self):
        """Reconnect if PostgreSQL closed an idle long-running detail crawl connection."""
        try:
            if self.db_conn and not self.db_conn.closed:
                return True
        except Exception:
            pass

        print("[WARNING] DB connection closed; reconnecting...")
        return self.connect_db()

    def ensure_browser_ready(self):
        """Prepare the existing DrissionPage flow only when HTTP collection fails."""
        if self.page is not None:
            return True
        if not self.setup_browser():
            return False
        self.skip_walmart_search = True
        self.initialize_session()
        return True

    @staticmethod
    def _formatted_review_count(detailed_review_content):
        if not detailed_review_content:
            return 0
        return detailed_review_content.count(' ||| ') + 1

    SKU_POPULARITY_ALLOWED_VALUES = {
        'overall pick',
        'best seller',
        'rollback',
        'clearance',
        'reduced price',
        'flash deal',
        'sale',
        'popular pick',
    }

    @staticmethod
    def _money_value(value):
        if value in (None, ''):
            return None
        text = str(value).strip()
        match = re.search(r'\$\s*\d[\d,]*(?:\.\d{1,2})?', text)
        if not match:
            return None
        return match.group(0).replace('$ ', '$')

    @classmethod
    def _has_required_price(cls, value):
        return cls._money_value(value) is not None

    @staticmethod
    def _format_count_value(value):
        count = normalize_int(value)
        return f'{count:,}' if count is not None else None

    @classmethod
    def _normalize_sku_popularity(cls, value):
        if not value:
            return None
        values = []
        seen = set()
        for part in re.split(r',|\|\|\|', str(value)):
            text = ' '.join(part.split())
            if not text:
                continue
            key = text.lower()
            if key not in cls.SKU_POPULARITY_ALLOWED_VALUES:
                continue
            if key in seen:
                continue
            seen.add(key)
            values.append(text)
        return ', '.join(values) if values else None

    @staticmethod
    def _normalize_discount_type(value):
        text = ' '.join(str(value or '').split())
        return 'Price when purchased online' if text == 'Price when purchased online' else None

    @staticmethod
    def _normalize_similar_value(value):
        text = str(value or '').strip()
        if not text:
            return None
        polluted_patterns = (
            r'\.(?:jpe?g|png|webp|gif|avif)(?:\?|$)',
            r'\b(?:\d-Year Plan|Pro TV Mounting|Protection Plan)\b',
            r'\b(?:Picture Quality|Ease Of Setup|Value For Money|Sound Quality|Ease Of Use|Controls|Apps)\b',
            r'\b(?:Refurbished TVs|Certified Refurbished|Walmart Restored|Small Vizio|\d+ Inch TVs)\b',
        )
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in polluted_patterns):
            return None
        return text

    def _normalize_detail_fields(self, product):
        if not product:
            return product

        for field in ('count_of_reviews', 'count_of_star_ratings'):
            formatted = self._format_count_value(product.get(field))
            if formatted is not None:
                product[field] = formatted

        if not product.get('star_rating') and normalize_int(product.get('count_of_reviews')) == 0:
            product['star_rating'] = 'No ratings yet'
        if not product.get('count_of_star_ratings') and product.get('count_of_reviews') is not None:
            product['count_of_star_ratings'] = product.get('count_of_reviews')

        if product.get('final_sku_price'):
            product['final_sku_price'] = self._money_value(product.get('final_sku_price'))
        if product.get('original_sku_price'):
            product['original_sku_price'] = self._money_value(product.get('original_sku_price'))
        if product.get('savings'):
            product['savings'] = self._money_value(product.get('savings'))

        product['discount_type'] = self._normalize_discount_type(product.get('discount_type'))
        product['sku_popularity'] = self._normalize_sku_popularity(product.get('sku_popularity'))

        for field in ('number_of_ppl_purchased_yesterday', 'number_of_ppl_added_to_carts'):
            value = normalize_int(product.get(field))
            product[field] = str(value) if value is not None else None

        product['retailer_sku_name_similar'] = self._normalize_similar_value(
            product.get('retailer_sku_name_similar')
        )
        return product

    @staticmethod
    def _env_int(name, default, minimum=1):
        try:
            value = int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default
        return max(minimum, value)

    @staticmethod
    def _env_bool(name, default=False):
        value = os.getenv(name)
        if value is None:
            return bool(default)
        return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}

    def _log_next_data_attempts(self, label, item, page_number, result):
        attempts = (result or {}).get('attempts') or []
        if not attempts:
            return
        summary = ' -> '.join(
            f"{attempt.get('source')}:{attempt.get('status')}/blocked={attempt.get('blocked')}"
            for attempt in attempts
        )
        page_suffix = f" page{page_number}" if page_number else ''
        print(f"  [NEXT_DATA {label}] {item or '-'}{page_suffix}: {summary}")

    def _fill_similar_from_json_response(self, parsed, product_url, item, client, log=True):
        if parsed.get('retailer_sku_name_similar') or not self.similar_json_fallback_enabled:
            return

        result = client.fetch_similar_product_names(
            product_url,
            current_item=item,
            wait_ms=self.similar_json_wait_ms,
        )
        meta = result.get('meta') or {}
        similar = result.get('names')
        if similar:
            parsed['retailer_sku_name_similar'] = similar
            if log:
                print(
                    f"  [NEXT_DATA similar HIT] source={meta.get('source')}, "
                    f"products={meta.get('similar_count')}, "
                    f"xhr={meta.get('xhr_count')}, elapsed={meta.get('elapsed_sec')}s"
                )
            return

        if log:
            print(
                f"  [NEXT_DATA similar MISS] source={meta.get('source')}, "
                f"status={meta.get('status')}, xhr={meta.get('xhr_count')}, "
                f"error={meta.get('error') or '-'}"
            )

    def load_mst_specs_cache(self, products):
        items = []
        seen = set()
        for product in products:
            item = self.extract_item(product.get('product_url'))
            if not item:
                continue
            item_key = str(item)
            if item_key in seen:
                continue
            seen.add(item_key)
            items.append(item_key)

        if not items:
            return {}

        try:
            if not self.ensure_db_connection():
                return {}

            placeholders = ', '.join(['%s'] * len(items))
            cursor = self.db_conn.cursor()
            cursor.execute(f"""
                SELECT item, screen_size, sku
                FROM tv_item_mst
                WHERE account_name = %s
                  AND is_product = TRUE
                  AND item IN ({placeholders})
            """, [self.account_name, *items])
            rows = cursor.fetchall()
            cursor.close()
            return {
                str(row[0]): (row[1], row[2])
                for row in rows
            }
        except Exception as e:
            print(f"  [WARNING] load_mst_specs_cache failed: {e}")
            try:
                if self.db_conn and not self.db_conn.closed:
                    self.db_conn.rollback()
            except Exception:
                pass
            return {}

    def _crawl_detail_next_data_worker(self, index, product, mst_specs):
        diagnostics = []
        try:
            client = WalmartNextDataClient()
            combined_data = self.crawl_detail_next_data(
                product,
                next_data_client=client,
                mst_specs=mst_specs,
                record_errors=False,
                collect_spec_diff=False,
                log=False,
                diagnostics=diagnostics,
            )
            reason = None if combined_data else self._parallel_miss_reason(diagnostics)
            return index, combined_data, None, reason, diagnostics
        except Exception as e:
            diagnostics.append({
                'stage': 'worker_exception',
                'product': product,
                'message': str(e),
            })
            return index, None, e, 'worker_exception', diagnostics

    def _parallel_miss_reason(self, diagnostics):
        stages = [
            str(item.get('stage') or '')
            for item in (diagnostics or [])
        ]
        if any(stage in ('detail_next_data_review_incomplete', 'review_next_data_incomplete') for stage in stages):
            return 'review_incomplete'
        if any(stage.startswith('review_page') for stage in stages):
            return 'review_page_missing'
        if 'detail_next_data_price_missing' in stages:
            return 'price_missing'
        if 'detail_next_data_redirect_mismatch' in stages:
            return 'redirect_mismatch'
        if 'detail_next_data_item_missing' in stages:
            return 'item_missing'
        if 'detail_next_data_no_candidate_url' in stages:
            return 'no_candidate_url'
        if 'detail_next_data_no_next_data' in stages:
            return 'no_next_data'
        if 'worker_exception' in stages:
            return 'worker_exception'
        return 'unknown'

    def _parallel_miss_message(self, diagnostics):
        for item in reversed(diagnostics or []):
            message = str(item.get('message') or '').strip()
            if message:
                return message
        return ''

    def collect_detail_next_data_parallel(self, indexed_products):
        if not indexed_products:
            self.parallel_miss_reasons = {}
            return {}

        workers = min(self.detail_next_data_workers, len(indexed_products))
        products = [product for _, product in indexed_products]
        mst_specs = self.load_mst_specs_cache(products)
        results = {}
        miss_counts = Counter()
        miss_examples = {}
        self.parallel_miss_reasons = {}

        print(
            f"[INFO] NextData detail parallel fetch: "
            f"{len(indexed_products)} products, workers={workers}"
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(self._crawl_detail_next_data_worker, index, product, mst_specs): (index, product)
                for index, product in indexed_products
            }
            for future in as_completed(future_map):
                index, product = future_map[future]
                try:
                    result_index, combined_data, error, reason, diagnostics = future.result()
                except Exception as e:
                    result_index, combined_data, error, reason, diagnostics = index, None, e, 'worker_exception', [
                        {'stage': 'worker_exception', 'product': product, 'message': str(e)}
                    ]

                if error:
                    self._record_run_error('detail_next_data_parallel', product, error)

                if combined_data:
                    results[result_index] = combined_data
                    continue

                reason = reason or self._parallel_miss_reason(diagnostics)
                self.parallel_miss_reasons[result_index] = reason
                miss_counts[reason] += 1
                if reason not in miss_examples:
                    item = self.extract_item(product.get('product_url'))
                    name = product.get('retailer_sku_name') or ''
                    message = self._parallel_miss_message(diagnostics)
                    miss_examples[reason] = (
                        f"#{result_index} item={item or '-'} "
                        f"name={name[:60]} "
                        f"message={message[:120]}"
                    )

        print(f"[INFO] NextData detail parallel result: {len(results)}/{len(indexed_products)} loaded")
        if miss_counts:
            summary = ', '.join(
                f"{reason}={count}"
                for reason, count in miss_counts.most_common()
            )
            print(f"[INFO] NextData parallel miss reason summary: {summary}")
            examples = ' | '.join(
                f"{reason}: {example}"
                for reason, example in miss_examples.items()
            )
            print(f"[INFO] NextData parallel miss examples: {examples}")
        return results

    def _apply_fast_artifacts(self, combined_data):
        if not combined_data:
            return

        spec_diff = combined_data.pop('_spec_diff', None)
        if spec_diff:
            self.spec_diffs.append(spec_diff)

        fast_errors = combined_data.pop('_fast_errors', None) or []
        for error in fast_errors:
            self._record_run_error(
                error.get('stage') or 'detail_next_data',
                error.get('product') or combined_data,
                error.get('message') or '',
            )

    def save_detail_result(self, combined_data):
        if not combined_data:
            return False

        self._apply_fast_artifacts(combined_data)
        self._normalize_detail_fields(combined_data)
        self.upsert_item_mst(combined_data)
        return self.save_to_retail_com(combined_data)

    def collect_reviews_next_data(
        self,
        item,
        count_of_reviews,
        inline_reviews,
        product,
        next_data_client=None,
        record_errors=True,
        error_collector=None,
        log=True,
    ):
        client = next_data_client or self.next_data_client

        def add_error(stage, message):
            if record_errors:
                self._record_run_error(stage, product, message)
            elif error_collector is not None:
                error_collector.append({
                    'stage': stage,
                    'product': product,
                    'message': str(message),
                })

        review_total = normalize_int(count_of_reviews) or 0
        if review_total <= 0:
            return None, count_of_reviews, True

        inline_reviews = inline_reviews or []
        review_texts = []

        page_limit = 2 if review_total >= 20 else 1
        retry_total = self._env_int('WALMART_TV_REVIEW_NEXTDATA_RETRIES', 1)
        extra_page_limit = self._env_int('WALMART_TV_REVIEW_NEXTDATA_EXTRA_PAGES', 2, minimum=0)
        max_page = page_limit + extra_page_limit if review_total >= 20 else page_limit

        for page_number in range(1, max_page + 1):
            page_added = False
            last_reason = 'no __NEXT_DATA__'

            for retry_index in range(1, retry_total + 1):
                review_url = build_review_url(item, page_number)
                result = client.fetch_next_data(
                    review_url,
                    direct_retries=1,
                    use_zenrows=True,
                    js_render_fallback=True,
                )
                if log:
                    self._log_next_data_attempts('review', item, page_number, result)

                next_data = result.get('next_data')
                if not next_data:
                    if log and retry_index < retry_total:
                        print(f"  [NEXT_DATA review] page{page_number}: empty result, retrying ({retry_index + 1}/{retry_total})")
                    continue

                parsed = parse_review_page(next_data, limit=10)
                page_reviews = parsed.get('reviews') or []
                if page_number == 1:
                    count_of_reviews = parsed.get('total_review_count') or count_of_reviews

                if page_reviews:
                    review_texts.extend(page_reviews)
                    page_added = True
                    break

                last_reason = 'no parsed reviews'
                if log and retry_index < retry_total:
                    print(f"  [NEXT_DATA review] page{page_number}: no parsed reviews, retrying ({retry_index + 1}/{retry_total})")

            if not page_added and page_number == 1 and inline_reviews:
                review_texts.extend(inline_reviews)
                page_added = True
                if log:
                    print(f"  [NEXT_DATA review] page1: fallback reused {len(inline_reviews)} inline reviews from detail payload")

            if not page_added:
                add_error(f'review_page{page_number}_next_data', last_reason)

            detailed_review_content = format_reviews(review_texts, limit=20)
            collected_reviews = self._formatted_review_count(detailed_review_content)
            if page_number >= page_limit and (review_total < 20 or collected_reviews >= 20):
                break

        detailed_review_content = format_reviews(review_texts, limit=20)
        collected_reviews = self._formatted_review_count(detailed_review_content)
        complete = review_total < 20 or collected_reviews >= 20

        if not complete:
            message = f'expected 20 reviews from {review_total}, collected {collected_reviews}'
            if log:
                print(f"  [NEXT_DATA review WARNING] {message}")
            add_error('review_next_data_incomplete', message)

        return detailed_review_content, count_of_reviews, complete

    def crawl_detail_next_data(
        self,
        product,
        next_data_client=None,
        mst_specs=None,
        record_errors=True,
        collect_spec_diff=True,
        log=True,
        diagnostics=None,
    ):
        client = next_data_client or self.next_data_client
        fast_errors = []
        diagnostics_list = diagnostics if diagnostics is not None else []

        def add_diagnostic(stage, message):
            diagnostics_list.append({
                'stage': stage,
                'product': product,
                'message': str(message),
            })

        def add_error(stage, message):
            add_diagnostic(stage, message)
            if record_errors:
                self._record_run_error(stage, product, message)
            else:
                fast_errors.append({
                    'stage': stage,
                    'product': product,
                    'message': str(message),
                })

        product_url = product.get('product_url')
        requested_item = self.extract_item(product_url)
        candidate_urls = []
        for url in (build_item_url(requested_item), product_url):
            if url and url not in candidate_urls:
                candidate_urls.append(url)

        if not candidate_urls:
            add_diagnostic('detail_next_data_no_candidate_url', 'no candidate URL')
            return None

        for url in candidate_urls:
            result = client.fetch_next_data(
                url,
                direct_retries=1,
                use_zenrows=True,
                js_render_fallback=True,
            )
            if log:
                self._log_next_data_attempts('detail', requested_item, None, result)

            next_data = result.get('next_data')
            if not next_data:
                attempts = result.get('attempts') or []
                attempt_summary = ' -> '.join(
                    f"{attempt.get('source')}:{attempt.get('status')}/blocked={attempt.get('blocked')}"
                    for attempt in attempts
                )
                add_diagnostic('detail_next_data_no_next_data', attempt_summary or 'no __NEXT_DATA__')
                continue

            parsed = parse_detail_product(next_data, html_text=result.get('html'))
            if not parsed.get('discount_type'):
                parsed['discount_type'] = parse_discount_type_from_html(result.get('html'))

            item = parsed.get('item') or requested_item
            if requested_item and item and str(requested_item) != str(item):
                listing_name = product.get('retailer_sku_name')
                detail_name = parsed.get('retailer_sku_name')
                if not self.redirect_product_names_match(listing_name, detail_name):
                    message = f"{requested_item} -> {item}"
                    add_diagnostic('detail_next_data_redirect_mismatch', message)
                    if log:
                        print(f"  [NEXT_DATA detail] redirect mismatch rejected: {requested_item} -> {item}")
                    continue

            if not item:
                add_diagnostic('detail_next_data_item_missing', 'item missing in parsed detail')
                continue

            if not self._has_required_price(parsed.get('final_sku_price')):
                add_diagnostic('detail_next_data_price_missing', f'item={item}')
                if log:
                    print(f"  [NEXT_DATA detail] price missing for item={item}; trying next candidate/browser fallback")
                continue

            spec_product = product.copy()
            if parsed.get('retailer_sku_name'):
                spec_product['retailer_sku_name'] = parsed.get('retailer_sku_name')

            if mst_specs is not None:
                mst_screen_size, mst_sku = mst_specs.get(str(item), (None, None))
            else:
                mst_screen_size, mst_sku = self.get_tv_specs_from_mst(item)

            sku, sku_source, brand_sku, page_sku, brand_name = self.extract_sku(
                spec_product,
                product_url,
                None,
                mst_sku,
                log=log,
            )
            screen_size, spec_source, name_screen_size, page_screen_size = self.extract_screen_size(
                spec_product,
                None,
                mst_screen_size,
                log=log,
            )
            detailed_review_content, count_of_reviews, review_complete = self.collect_reviews_next_data(
                item,
                parsed.get('count_of_reviews'),
                parsed.get('inline_reviews') or [],
                product,
                next_data_client=client,
                record_errors=record_errors,
                error_collector=fast_errors,
                log=log,
            )
            if not review_complete:
                add_diagnostic(
                    'detail_next_data_review_incomplete',
                    f"item={item}, expected={parsed.get('count_of_reviews')}, collected={self._formatted_review_count(detailed_review_content)}",
                )
                if log:
                    print(
                        f"  [NEXT_DATA detail] review incomplete for item={item}; "
                        "browser fallback required"
                    )
                return None

            self._fill_similar_from_json_response(parsed, url, item, client, log=log)

            source = result.get('source') or 'next_data'
            combined_data = product.copy()
            combined_data.update({
                'item': item,
                'sku': sku,
                'count_of_reviews': count_of_reviews or parsed.get('count_of_reviews') or '0',
                'star_rating': parsed.get('star_rating'),
                'count_of_star_ratings': parsed.get('count_of_star_ratings') or count_of_reviews,
                'final_sku_price': parsed.get('final_sku_price'),
                'original_sku_price': parsed.get('original_sku_price'),
                'savings': parsed.get('savings'),
                'discount_type': parsed.get('discount_type'),
                'sku_popularity': parsed.get('sku_popularity'),
                'number_of_ppl_purchased_yesterday': parsed.get('number_of_ppl_purchased_yesterday'),
                'number_of_ppl_added_to_carts': parsed.get('number_of_ppl_added_to_carts'),
                'model_year': self.extract_model_year(spec_product.get('retailer_sku_name')),
                'screen_size': screen_size,
                'retailer_sku_name_similar': parsed.get('retailer_sku_name_similar'),
                'detailed_review_content': detailed_review_content,
                '_detail_source': source,
            })

            extracted_sku = brand_sku or page_sku
            extracted_screen_size = name_screen_size or page_screen_size
            has_sku_diff = (mst_sku or extracted_sku) and mst_sku != extracted_sku
            has_screen_size_diff = (mst_screen_size or extracted_screen_size) and mst_screen_size != extracted_screen_size
            if has_sku_diff or has_screen_size_diff:
                spec_diff = {
                    'item': item,
                    'mst_sku': mst_sku,
                    'extracted_sku': extracted_sku,
                    'brand_sku': brand_sku,
                    'page_sku': page_sku,
                    'brand_name': brand_name,
                    'mst_screen_size': mst_screen_size,
                    'extracted_screen_size': extracted_screen_size,
                    'name_screen_size': name_screen_size,
                    'page_screen_size': page_screen_size,
                }
                if collect_spec_diff:
                    self.spec_diffs.append(spec_diff)
                else:
                    combined_data['_spec_diff'] = spec_diff

            if fast_errors:
                combined_data['_fast_errors'] = fast_errors

            if log:
                review_count = self._formatted_review_count(detailed_review_content)
                print(f"  [NEXT_DATA detail OK] source={source}, item={item}, price={combined_data.get('final_sku_price') or '-'}, reviews={review_count}")
            return combined_data

        return None

    def crawl_detail(self, product, skip_fast=False):
        """상세 페이지 크롤링: 페이지 로드 → 데이터 추출 → 스펙 추출 → 유사제품 추출 → 리뷰 추출 (DrissionPage 사용)"""
        try:
            product_url = product.get('product_url')
            if not product_url:
                print(f"  [SKIP] product_url 없음 → 크롤링/저장 건너뜀")
                return None


            if not skip_fast:
                fast_data = self.crawl_detail_next_data(product)
                if fast_data:
                    return fast_data

            if self.page is None and not self.ensure_browser_ready():
                print("[ERROR] Browser fallback setup failed")
                return product

            self.load_detail_page(product_url)

            # CAPTCHA/Sorry 페이지 사전 처리
            blocking_result = self.handle_detail_blocking_pages(product)
            if blocking_result:
                return blocking_result

            page_html = self.page.run_js('return document.documentElement.outerHTML')
            tree = html.fromstring(page_html)

            # item ID 추출 (페이지 로드 후 추출 - 에러 시 item NULL로 식별)
            item = self.extract_item(product_url)
            print(f"[item] item ID 추출 {'완료' if item else '실패'}")

            # ========== 1단계: 상단 정보 ==========
            # ========== 1-1단계: 상단 리뷰 정보 추출 ==========
            no_ratings_yet, header_star_rating, header_count_of_star_ratings = self.extract_rating_from_header(tree)

            # ========== 1-2단계: 가격 ==========
            final_sku_price = self.extract_final_sku_price(tree)
            original_sku_price = None
            savings = None
            if final_sku_price and '$' in final_sku_price:
                original_sku_price = self.safe_extract_chain(tree, 'original_sku_price')
                savings = self.safe_extract_chain(tree, 'savings')

            if not self._has_required_price(final_sku_price):
                message = 'browser fallback final_sku_price missing'
                print(f"  [price ERROR] {message}")
                self._record_run_error('detail_price_missing_browser', product, message)
                return product

            # ========== 1-3단계: 추가 필드 추출 ==========
            number_of_ppl_purchased_yesterday = self.convert_first_number(tree, 'number_of_ppl_purchased_yesterday')
            number_of_ppl_added_to_carts = self.convert_first_number(tree, 'number_of_ppl_added_to_carts')
            sku_popularity = self.safe_extract_chain_join(tree, 'sku_popularity', separator=", ")
            discount_type = self.safe_extract_chain(tree, 'discount_type')
            model_year = self.extract_model_year(product.get('retailer_sku_name'))  # 모델 연도 추출 (제품명 정규식)

            # ========== 2단계: TV 스펙 (모달) ==========
            mst_screen_size, mst_sku = self.get_tv_specs_from_mst(item)
            modal_tree = None

            # 스펙 버튼 탐색/클릭 → 모달 열기 → sku/screen_size 추출
            if self.scroll_find_element('spec_toggle_button', max_scrolls=10, label='스펙 버튼 탐색', click=True):
                modal_tree = self.open_spec_modal()
                if modal_tree is not None:
                    # 스펙 모달창 닫기 (ESC 키)
                    try:
                        self.page.actions.key_down('Escape').key_up('Escape')
                        time.sleep(0.5)
                        print(f"  [모달 닫기] ESC")
                    except Exception as e:
                        print(f"  [WARNING] 모달 닫기 ESC 실패: {e}")

            sku, sku_source, brand_sku, page_sku, brand_name = self.extract_sku(
                product,
                product_url,
                modal_tree,
                mst_sku,
            )
            screen_size, spec_source, name_screen_size, page_screen_size = self.extract_screen_size(
                product,
                modal_tree,
                mst_screen_size,
            )

            # ========== 3단계: 유사 제품 ==========
            # 섹션 탐색(스크롤 fallback) → 절대경로 XPath로 카드 이름 한번에 추출 → ' ||| '로 join
            self.scroll_find_element('similar_products_section', max_scrolls=5, label='유사제품 섹션 탐색')

            # HTML 재파싱 후 절대경로로 카드 이름 일괄 추출
            page_html = self.page.run_js('return document.documentElement.outerHTML')
            tree = html.fromstring(page_html)
            retailer_sku_name_similar = self.safe_extract_chain_join(
                tree, 'similar_product_name', separator=' ||| '
            )

            # ========== 4단계: 리뷰 관련 필드 ==========
            count_of_reviews = None
            star_rating = None
            count_of_star_ratings = None

            if no_ratings_yet:
                # "No ratings yet" - 리뷰 없음
                count_of_reviews = '0'
                star_rating = 'No ratings yet'
                count_of_star_ratings = '0'
            else:
                # 1. 스크롤 전 상단(header_rating)에서 추출한 값 우선 사용
                star_rating = header_star_rating
                count_of_star_ratings = header_count_of_star_ratings

                # 2. 리뷰 섹션 컨테이너 탐색 (1차 DOM → 2차 스크롤 fallback) — lazy load 유도
                self.scroll_find_element('review_section', max_scrolls=5, label='리뷰 섹션 탐색')

                # 3. 하단 추출 시도 (retry 3회)
                fallback_used = False
                for retry in range(3):
                    page_html = self.page.run_js('return document.documentElement.outerHTML')
                    tree = html.fromstring(page_html)

                    # 상단 추출 실패 시 하단 개별 XPath로 star_rating/count_of_star_ratings 재추출
                    if star_rating is None or count_of_star_ratings is None:
                        if not fallback_used:
                            print(f"  [하단 fallback] star_rating/count_of_star_ratings 개별 XPath 시도")
                            fallback_used = True
                        star_rating = self.extract_star_rating(tree)
                        count_of_star_ratings = self.extract_ratings_count(tree)

                    count_of_reviews = self.extract_review_count(tree)

                    # 3개 필드 모두 추출 성공 시 종료
                    if count_of_reviews is not None and star_rating is not None and count_of_star_ratings is not None:
                        break

                    # 실패 시 재시도 전 대기
                    if retry < 2:
                        time.sleep(random.uniform(1, 2))

            # ========== 5단계: 리뷰 더보기 버튼 클릭 및 상세 리뷰 추출 ==========
            detailed_review_content = None
            if no_ratings_yet:
                print(f"  [리뷰 0건 - 상세 리뷰 추출 스킵]")
            else:
                detailed_review_content, count_of_reviews = self.extract_detailed_reviews(
                    item,
                    count_of_reviews,
                )

            # 결합된 데이터
            if normalize_int(count_of_reviews) >= 20 and self._formatted_review_count(detailed_review_content) < 20:
                collected_reviews = self._formatted_review_count(detailed_review_content)
                message = f'browser fallback expected 20 reviews from {count_of_reviews}, collected {collected_reviews}'
                print(f"  [review ERROR] {message}")
                self._record_run_error('review_browser_incomplete', product, message)
                return product

            combined_data = product.copy()
            combined_data.update({
                'item': item,
                'sku': sku,
                'count_of_reviews': count_of_reviews,
                'star_rating': star_rating,
                'count_of_star_ratings': count_of_star_ratings,
                'final_sku_price': final_sku_price,
                'original_sku_price': original_sku_price,
                'savings': savings,
                'discount_type': discount_type,
                'sku_popularity': sku_popularity,
                'number_of_ppl_purchased_yesterday': number_of_ppl_purchased_yesterday,
                'number_of_ppl_added_to_carts': number_of_ppl_added_to_carts,
                'model_year': model_year,
                'screen_size': screen_size,
                'retailer_sku_name_similar': retailer_sku_name_similar,
                'detailed_review_content': detailed_review_content,
            })

            # 마스터 vs 추출값(brand_sku 또는 page_sku) 비교 — 다르면 누적 (run() 끝에 일괄 출력)
            extracted_sku = brand_sku or page_sku   # 마스터 없을 때 들어갈 값
            extracted_screen_size = name_screen_size or page_screen_size   # 마스터 없을 때 들어갈 값
            has_sku_diff = (mst_sku or extracted_sku) and mst_sku != extracted_sku
            has_screen_size_diff = (mst_screen_size or extracted_screen_size) and mst_screen_size != extracted_screen_size
            if has_sku_diff or has_screen_size_diff:
                self.spec_diffs.append({
                    'item': item,
                    'mst_sku': mst_sku,
                    'extracted_sku': extracted_sku,
                    'brand_sku': brand_sku,
                    'page_sku': page_sku,
                    'brand_name': brand_name,
                    'mst_screen_size': mst_screen_size,
                    'extracted_screen_size': extracted_screen_size,
                    'name_screen_size': name_screen_size,
                    'page_screen_size': page_screen_size,
                })

            # ──── 결과 요약 (트리 구조) ────
            print(f"\n──── 결과 요약 ────")
            similar_count = (retailer_sku_name_similar.count(' ||| ') + 1) if retailer_sku_name_similar else 0
            review_count = (detailed_review_content.count(' ||| ') + 1) if detailed_review_content else 0
            if sku_source == "브랜드" and brand_name:
                sku_display = f"{sku} (출처: 브랜드/{brand_name})"
            else:
                sku_display = f"{sku} (출처: {sku_source})" if sku and sku_source else (sku or '-')
            screen_size_display = f"{screen_size} (출처: {spec_source})" if screen_size and spec_source else (screen_size or '-')

            print(f"  ├─ item: {item or '-'}")
            print(f"  ├─ sku: {sku_display}")
            print(f"  ├─ final_sku_price: {final_sku_price or '-'}")
            print(f"  ├─ original_sku_price: {original_sku_price or '-'}")
            print(f"  ├─ savings: {savings or '-'}")
            print(f"  ├─ count_of_reviews: {count_of_reviews or '0'}")
            print(f"  ├─ star_rating: {star_rating or '-'}")
            print(f"  ├─ count_of_star_ratings: {count_of_star_ratings or '-'}")
            print(f"  ├─ number_of_ppl_purchased_yesterday: {number_of_ppl_purchased_yesterday or '-'}")
            print(f"  ├─ number_of_ppl_added_to_carts: {number_of_ppl_added_to_carts or '-'}")
            print(f"  ├─ sku_popularity: {sku_popularity or '-'}")
            print(f"  ├─ discount_type: {discount_type or '-'}")
            print(f"  ├─ model_year: {model_year or '-'}")
            print(f"  ├─ screen_size: {screen_size_display}")
            print(f"  ├─ retailer_sku_name_similar: {'찾음 (' + str(similar_count) + '개)' if retailer_sku_name_similar else '없음'}")
            print(f"  └─ detailed_review_content: {review_count}개")

            return combined_data

        except Exception as e:
            error_msg = str(e).lower()
            print(f"[ERROR] Detail crawl failed: {e}")

            # 타임아웃/리다이렉트는 run()의 전용 분기에서 처리한다.
            if (
                "timeout" in error_msg
                or "time out" in error_msg
                or "redirect detected" in error_msg
            ):
                raise

            return product

    def save_to_retail_com(self, product):
        """DB 저장: 1개씩 INSERT.

        컬럼 구성: EXTRACTED_FIELDS (추출) + PASSTHROUGH_FIELDS (전달) + SAVE_META_FIELDS (저장 메타)
        새 추출 필드 추가 시 EXTRACTED_FIELDS 리스트에만 추가하면 INSERT/UPDATE 모두 자동 반영.
        """
        if not product:
            return False

        try:
            if not self.ensure_db_connection():
                return False

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
            try:
                if self.db_conn and not self.db_conn.closed:
                    self.db_conn.rollback()
            except Exception:
                pass
            return False

    def get_tv_specs_from_mst(self, item):
        """마스터 테이블에서 TV 스펙 및 SKU 조회"""
        if not item:
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

    def upsert_item_mst(self, product):
        """tv_item_mst 테이블에 INSERT 또는 UPDATE
        - 조회 결과 없음 → INSERT (sku, screen_size)
        - 조회 결과 있음 → 기존 값이 NULL/빈값인 필드만 UPDATE
        """
        item = product.get('item')
        if not item:
            return

        try:
            if not self.ensure_db_connection():
                return

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

                if not (existing_sku or '') and new_sku:
                    updates.append("sku = %s")
                    params.append(new_sku)
                if not existing_screen_size and new_screen_size:
                    updates.append("screen_size = %s")
                    params.append(new_screen_size)

                if updates:
                    # 업데이트할 필드와 값 저장 (로그용)
                    updated_info = []
                    if not (existing_sku or '') and new_sku:
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
            try:
                if self.db_conn and not self.db_conn.closed:
                    self.db_conn.rollback()
            except Exception:
                pass

    def extract_sku_by_brand(self, retailer_sku_name, product_url):
        """브랜드별 정규식으로 SKU 추출 (1차 retailer_sku_name → 2차 product_url)

        WALMART_TV_BRAND_SKU_PATTERNS에 정의된 브랜드별 패턴 사용:
        - 브랜드 식별: 'contains'(부분 문자열) 또는 'regex'(정규식, 대소문자 무관)
        - 매칭된 브랜드의 패턴 리스트를 순서대로 시도
        - 각 패턴은 (정규식, 'name' 또는 'url') 형식 — name 패턴은 retailer_sku_name에, url 패턴은 product_url에 적용

        Args:
            retailer_sku_name (str): 제품명
            product_url (str): 제품 URL

        Returns:
            tuple: (추출된 sku, 매칭된 브랜드명) — 매칭 실패 시 (None, None)
                   브랜드는 매칭됐으나 정규식 실패 시 (None, brand_name)
        """
        if not retailer_sku_name:
            return None, None

        name_lower = retailer_sku_name.lower()

        for brand, pattern, mode, regexes in WALMART_TV_BRAND_SKU_PATTERNS:
            # 브랜드 검출
            if mode == 'contains':
                if pattern.lower() not in name_lower:
                    continue
            elif mode == 'regex':
                if not re.search(pattern, retailer_sku_name, re.IGNORECASE):
                    continue
            else:
                continue

            # 매칭된 브랜드의 패턴 순차 시도
            for regex, source_type in regexes:
                source = retailer_sku_name if source_type == 'name' else product_url
                if not source:
                    continue
                match = re.search(regex, source)
                if match:
                    return match.group(1), brand

            # 브랜드 매칭됐으나 패턴 모두 실패
            return None, brand

        return None, None

    def extract_sku(self, product, product_url, modal_tree, mst_sku, log=True):
        """SKU 최종값 결정: 마스터 → 브랜드 정규식 → 모달."""
        brand_sku, brand_name = self.extract_sku_by_brand(
            product.get('retailer_sku_name'),
            product_url,
        )
        if log:
            if brand_name:
                if brand_sku:
                    print(f"  [sku 브랜드 추출] {brand_name} → {brand_sku}")
                else:
                    print(f"  [sku 브랜드 추출] {brand_name} 매칭됐으나 정규식 실패")
            else:
                print(f"  [sku 브랜드 추출] 브랜드 매칭 없음 → page_sku로 fallback")

        page_sku = self.safe_extract_chain(modal_tree, 'sku') if modal_tree is not None else None

        sku = None
        sku_source = None
        if mst_sku:
            sku = mst_sku
            sku_source = "마스터"
        elif brand_sku:
            sku = brand_sku
            sku_source = "브랜드"
        elif page_sku:
            sku = page_sku
            sku_source = "모달"

        return sku, sku_source, brand_sku, page_sku, brand_name

    def extract_model_year(self, retailer_sku_name):
        """제품명에서 모델 연도 추출
        - (2025) 또는 (2025 Model) → 2025
        - 2025 Model → 2025
        - Smart TV 2025 (끝 4자리) → 2025
        """
        if not retailer_sku_name:
            return None
        try:
            # Pattern 1: (2025) 또는 (2025 Model)
            match = re.search(r'\((\d{4})(?:\s*Model)?\)', retailer_sku_name)
            if match:
                year = int(match.group(1))
                if 2015 <= year <= 2030:
                    return year

            # Pattern 2: 2025 Model (괄호 없음)
            match = re.search(r'(\d{4})\s*Model', retailer_sku_name)
            if match:
                year = int(match.group(1))
                if 2015 <= year <= 2030:
                    return year

            # Pattern 3: 끝의 4자리 연도 (Smart TV 2025)
            match = re.search(r'\b(20[12]\d)\s*$', retailer_sku_name.strip())
            if match:
                year = int(match.group(1))
                return year

            return None
        except Exception as e:
            print(f"  [WARNING] extract_model_year failed: {e}")
            return None

    def extract_screen_size_by_regex(self, text, pattern, label):
        """텍스트에서 screen_size 숫자를 추출해 'N inches' 형식으로 정규화한다."""
        if not text:
            return None
        try:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                size_number = match.group(1)
                if 10 <= float(size_number) <= 150:
                    return f"{size_number} inches"
            return None
        except Exception as e:
            print(f"  [WARNING] {label} failed: {e}")
            return None

    def extract_screen_size(self, product, modal_tree, mst_screen_size, log=True):
        """screen_size 최종값 결정: 상품명 정규식 → 모달 → 마스터."""
        name_screen_size = self.extract_screen_size_by_regex(
            product.get('retailer_sku_name'),
            r'(\d+\.?\d*)(?:[\s-]*inch(?:es)?|["“”″])',
            'extract_screen_size_name',
        )
        if name_screen_size and log:
            print(f"  [screen_size 상품명 추출] {name_screen_size}")

        page_screen_size = None
        if modal_tree is not None:
            page_screen_size_raw = self.safe_extract_chain(modal_tree, 'screen_size')
            page_screen_size = self.extract_screen_size_by_regex(
                page_screen_size_raw,
                r'([\d.]+)\s*(?:in(?:ch(?:es)?)?|")?',
                'extract_screen_size_modal',
            )

        screen_size = None
        spec_source = None
        if name_screen_size:
            screen_size = name_screen_size
            spec_source = "상품명"
        elif page_screen_size:
            screen_size = page_screen_size
            spec_source = "모달"
        elif mst_screen_size:
            screen_size = mst_screen_size
            spec_source = "마스터"

        return screen_size, spec_source, name_screen_size, page_screen_size


def main():
    """개별 실행 진입점 (테스트 모드, 기본 배치 ID 사용)"""
    crawler = WalmartTVDetailCrawler(batch_id=None, test_mode=True)
    crawler.run()
    input("Press Enter to exit...")


if __name__ == '__main__':
    main()
