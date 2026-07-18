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
from decimal import Decimal, InvalidOperation

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
    item_id_from_url,
    normalize_availability_value,
    normalize_int,
    parse_detail_product,
    parse_discount_type_from_html,
    parse_review_page,
    product_scope_query_params,
    repair_similar_text_encoding,
    review_response_scope_error,
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
    REVIEW_BODY_XPATH = (
        "//div[@data-testid='enhanced-review-content']"
        "//span[contains(concat(' ', normalize-space(@class), ' '), ' tl-m ') "
        "and contains(concat(' ', normalize-space(@class), ' '), ' db-m ')]"
    )
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
        'offer',
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

        self.next_data_client = WalmartNextDataClient()
        self.detail_next_data_workers = self._env_int('WALMART_TV_DETAIL_WORKERS', 4)
        self.detail_next_data_chunk_size = self._env_int('WALMART_TV_DETAIL_CHUNK_SIZE', 40)
        self.zenrows_recovery_workers = self._env_int(
            'WALMART_TV_ZENROWS_RECOVERY_WORKERS',
            self.detail_next_data_workers,
        )
        self.zenrows_recovery_attempts = min(
            10,
            self._env_int(
                'WALMART_TV_ZENROWS_RECOVERY_ATTEMPTS',
                10,
            ),
        )
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
            'target_records': 0,
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
            'decision': 'detail_not_saved',
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

            self.detail_report['target_records'] = len(product_list)

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
            deferred_misses = {}
            deferred_mst_specs = {}

            for chunk_start in range(0, len(indexed_products), self.detail_next_data_chunk_size):
                chunk = indexed_products[chunk_start:chunk_start + self.detail_next_data_chunk_size]
                fast_results, chunk_misses, mst_specs = self._collect_detail_initial_parallel(chunk)
                deferred_misses.update(chunk_misses)
                deferred_mst_specs.update(mst_specs)

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
                            miss_entry = chunk_misses.get(i) or {}
                            miss_reason = miss_entry.get('reason') or 'unknown'
                            print(
                                f"  [NEXT_DATA DEFERRED] reason={miss_reason}; "
                                "immediate retry exhausted, queued for final ZenRows recovery"
                            )
                            continue

                        if combined_data:
                            detail_loaded = combined_data is not product
                            if self.save_detail_result(combined_data):
                                total_saved += 1
                                self._record_saved(detail=detail_loaded)
                            else:
                                self._record_run_error('detail_save_rejected', product, 'validated detail row was not saved')

                        time.sleep(random.uniform(0.05, 0.15))

                    except Exception as e:
                        error_msg = str(e).lower()
                        print(f"[ERROR] Product {i} failed: {e}")

                        if "redirect detected" in error_msg:
                            print("[INFO] 리다이렉트 감지 - 검증된 detail row 없음, 저장하지 않음")
                            self._record_redirect(product, e)
                            continue

                        # 검증된 detail row가 없으면 불완전한 listing-only row를 저장하지 않는다.
                        self._record_run_error('detail', product, e)
                        continue

            unresolved = {}
            if deferred_misses:
                print(
                    f"\n[INFO] Initial detail pass complete: "
                    f"deferred={len(deferred_misses)}; starting final ZenRows recovery"
                )
                recovered, unresolved = self.collect_detail_zenrows_recovery_parallel(
                    deferred_misses,
                    deferred_mst_specs,
                )
                self.parallel_miss_reasons = {
                    index: entry.get('reason') or 'unknown'
                    for index, entry in unresolved.items()
                }

                for i, initial_entry in sorted(deferred_misses.items()):
                    product = initial_entry.get('product') or {}
                    retailer_sku_name = product.get('retailer_sku_name') or 'N/A'
                    product_url = product.get('product_url', 'N/A')
                    url_display = product_url[:80] + '...' if len(product_url) > 80 else product_url
                    print(f"\n[{i}/{total_products}] {retailer_sku_name}")
                    print(f"  URL: {url_display}")

                    combined_data = recovered.get(i)
                    if combined_data:
                        review_count = self._formatted_review_count(
                            combined_data.get('detailed_review_content')
                        )
                        print(
                            f"  [ZENROWS FINAL HIT] source={combined_data.get('_detail_source')}, "
                            f"item={combined_data.get('item') or '-'}, "
                            f"price={combined_data.get('final_sku_price') or '-'}, "
                            f"reviews={review_count}"
                        )
                        if self.save_detail_result(combined_data):
                            total_saved += 1
                            self._record_saved(detail=True)
                        else:
                            self._record_run_error(
                                'detail_save_rejected',
                                product,
                                'validated recovery row was not saved',
                            )
                        time.sleep(random.uniform(0.05, 0.15))
                        continue

                    unresolved_entry = unresolved.get(i) or initial_entry
                    miss_reason = unresolved_entry.get('reason') or 'unknown'
                    message = self._parallel_miss_message(
                        unresolved_entry.get('diagnostics')
                    )
                    detail = f"reason={miss_reason}"
                    if message:
                        detail = f"{detail}; {message}"
                    self._record_run_error(
                        'detail_zenrows_recovery_exhausted',
                        product,
                        detail,
                    )
                    print(
                        f"  [NEXT_DATA MISS] reason={miss_reason}; "
                        "final ZenRows recovery exhausted"
                    )
                    if self.save_listing_fallback(product, miss_reason):
                        total_saved += 1
                        self._record_saved(detail=False)
                        print("  [LISTING-ONLY SAVED] original listing row preserved")
                    else:
                        self._record_run_error(
                            'listing_fallback_save_failed',
                            product,
                            f'reason={miss_reason}',
                        )
            else:
                self.parallel_miss_reasons = {}

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
            if self.db_conn:
                self.db_conn.close()

    def initialize(self):
        """초기화: batch_id 설정 → DB 연결 → HTTP client 설정 → 로그 정리"""
        # 1. batch_id 설정
        if not self.batch_id:
            self.batch_id = 't_w_20260512_211946'

        # 2. DB 연결
        if not self.connect_db():
            return False

        # 3. NextData HTTP client ready. Detail collection does not use XPath or Chrome.
        self.next_data_client = WalmartNextDataClient()

        # 4. Log cleanup
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
                    pl.final_sku_price, pl.original_sku_price,
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
                    'final_sku_price': row[1],
                    'original_sku_price': row[2],
                    'offer': row[3],
                    'pick_up_availability': normalize_availability_value(
                        row[4],
                        'pick_up_availability',
                    ),
                    'fastest_delivery': normalize_availability_value(
                        row[5],
                        'fastest_delivery',
                    ),
                    'delivery_availability': normalize_availability_value(
                        row[6],
                        'delivery_availability',
                    ),
                    'sku_status': row[7],
                    'available_quantity_for_purchase': row[8],
                    'inventory_status': row[9],
                    'main_rank': row[10],
                    'bsr_rank': row[11],
                    'product_url': row[12],
                    'calendar_week': row[13],
                    'crawl_datetime': row[14],
                    'page_type': row[15],
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

    @staticmethod
    def _formatted_review_count(detailed_review_content):
        if not detailed_review_content:
            return 0
        return detailed_review_content.count(' ||| ') + 1

    def safe_extract_chain_list(self, element, base_field_name):
        if base_field_name == 'detailed_review_content':
            try:
                results = element.xpath(self.REVIEW_BODY_XPATH)
            except Exception:
                return [], None
            if results:
                return results, 'detailed_review_content_exact'
            return [], None

        return super().safe_extract_chain_list(element, base_field_name)

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

    @classmethod
    def _money_amount(cls, value):
        normalized = cls._money_value(value)
        if normalized is None:
            return None
        try:
            return Decimal(normalized.replace('$', '').replace(',', ''))
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _format_count_value(value):
        count = normalize_int(value)
        return f'{count:,}' if count is not None else None

    @staticmethod
    def _offer_count_from_text(value):
        text = ' '.join(str(value or '').split())
        if not text:
            return None
        match = re.search(r'(\d+)\s+free\s+offers?', text, re.IGNORECASE)
        if not match:
            return None
        count = normalize_int(match.group(1))
        return str(count) if count is not None else None

    @staticmethod
    def _format_star_rating_value(value):
        text = ' '.join(str(value or '').split())
        if not text:
            return None
        if text.lower() == 'no ratings yet':
            return 'No ratings yet'
        match = re.search(r'\d+(?:\.\d+)?', text)
        if not match:
            return text
        try:
            return f'{float(match.group(0)):.1f}'
        except (TypeError, ValueError):
            return text

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
    def _normalize_social_count(value, marker=None):
        count = normalize_int(value)
        return str(count) if count is not None else None

    @staticmethod
    def _normalize_similar_value(value):
        text = str(repair_similar_text_encoding(value) or '').strip()
        if not text:
            return None
        polluted_patterns = (
            r'\.(?:jpe?g|png|webp|gif|avif)(?:\?|$)',
            r'\b(?:\d-Year Plan|Pro TV Mounting|Protection Plan)\b',
            r'\b(?:Picture Quality|Ease Of Setup|Value For Money|Sound Quality|Ease Of Use|Controls|Apps)\b',
        )
        polluted_exact = {
            'refurbished tvs',
            'tvs - certified refurbished',
            'walmart restored vizio tvs',
            'vizio tvs',
            'vizio',
            'small vizio tv',
            'small vizio tvs',
            'vizio small tv',
            'vizio small tvs',
            'vizio 37',
            'vizio 39 led',
            'vizio tv 37',
            '43 inch tvs',
        }
        names = []
        seen = set()
        for part in text.split(' ||| '):
            name = ' '.join(part.split())
            if not name:
                continue
            if name.lower() in polluted_exact:
                continue
            if any(re.search(pattern, name, re.IGNORECASE) for pattern in polluted_patterns):
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        return ' ||| '.join(names) if names else None

    def _normalize_detail_fields(self, product):
        if not product:
            return product

        for field in ('count_of_reviews', 'count_of_star_ratings'):
            formatted = self._format_count_value(product.get(field))
            if formatted is not None:
                product[field] = formatted

        formatted_star_rating = self._format_star_rating_value(product.get('star_rating'))
        if formatted_star_rating:
            product['star_rating'] = formatted_star_rating
        elif normalize_int(product.get('count_of_reviews')) == 0:
            product['star_rating'] = 'No ratings yet'

        if not product.get('count_of_star_ratings') and normalize_int(product.get('count_of_reviews')) == 0:
            product['count_of_star_ratings'] = '0'

        if product.get('final_sku_price'):
            product['final_sku_price'] = self._money_value(product.get('final_sku_price'))
        if product.get('original_sku_price'):
            product['original_sku_price'] = self._money_value(product.get('original_sku_price'))
        if product.get('savings'):
            product['savings'] = self._money_value(product.get('savings'))
        final_price_amount = self._money_amount(product.get('final_sku_price'))
        original_price_amount = self._money_amount(product.get('original_sku_price'))
        if (
            final_price_amount is not None
            and original_price_amount is not None
            and final_price_amount == original_price_amount
        ):
            product['original_sku_price'] = None
            product['savings'] = None
        product['discount_type'] = self._normalize_discount_type(product.get('discount_type'))
        product['sku_popularity'] = self._normalize_sku_popularity(product.get('sku_popularity'))
        product['number_of_ppl_purchased_yesterday'] = self._normalize_social_count(
            product.get('number_of_ppl_purchased_yesterday'), 'bought'
        )
        product['number_of_ppl_added_to_carts'] = self._normalize_social_count(
            product.get('number_of_ppl_added_to_carts'), 'cart'
        )
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
        existing_similar = self._normalize_similar_value(parsed.get('retailer_sku_name_similar'))
        if existing_similar:
            parsed['retailer_sku_name_similar'] = existing_similar
            return
        if not self.similar_json_fallback_enabled:
            parsed['retailer_sku_name_similar'] = None
            return

        result = client.fetch_similar_product_names(
            product_url,
            current_item=item,
            wait_ms=self.similar_json_wait_ms,
        )
        meta = result.get('meta') or {}
        similar = self._normalize_similar_value(result.get('names'))
        if similar:
            parsed['retailer_sku_name_similar'] = similar
        if log:
            print(
                f"  [NEXT_DATA similar] source={result.get('source') or '-'}, "
                f"status={meta.get('status') or '-'}, xhr={meta.get('xhr_count') or 0}, "
                f"count={(similar.count(' ||| ') + 1) if similar else 0}"
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

    def _crawl_detail_next_data_worker(
        self,
        index,
        product,
        mst_specs,
        zenrows_only=False,
    ):
        diagnostics = []
        try:
            client = WalmartNextDataClient(direct_enabled=not zenrows_only)
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
        if any(stage.startswith('review_scope') for stage in stages):
            return 'review_scope_mismatch'
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

    def _print_parallel_miss_summary(self, label, misses):
        if not misses:
            return

        counts = Counter(
            (entry.get('reason') or 'unknown')
            for entry in misses.values()
        )
        summary = ', '.join(
            f"{reason}={count}"
            for reason, count in counts.most_common()
        )
        print(f"[INFO] {label} reason summary: {summary}")

        examples = []
        seen_reasons = set()
        for index, entry in misses.items():
            reason = entry.get('reason') or 'unknown'
            if reason in seen_reasons:
                continue
            seen_reasons.add(reason)
            product = entry.get('product') or {}
            item = self.extract_item(product.get('product_url'))
            name = product.get('retailer_sku_name') or ''
            message = self._parallel_miss_message(entry.get('diagnostics'))
            examples.append(
                f"{reason}: #{index} item={item or '-'} "
                f"name={name[:60]} message={message[:120]}"
            )
        if examples:
            print(f"[INFO] {label} examples: {' | '.join(examples)}")

    def _collect_detail_initial_parallel(self, indexed_products):
        if not indexed_products:
            return {}, {}, {}

        workers = min(self.detail_next_data_workers, len(indexed_products))
        products = [product for _, product in indexed_products]
        mst_specs = self.load_mst_specs_cache(products)
        results = {}
        initial_misses = {}
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

                if combined_data:
                    results[result_index] = combined_data
                    continue

                reason = reason or self._parallel_miss_reason(diagnostics)
                initial_misses[result_index] = {
                    'product': product,
                    'reason': reason,
                    'diagnostics': diagnostics,
                    'error': error,
                }

        print(f"[INFO] NextData detail parallel result: {len(results)}/{len(indexed_products)} loaded")
        self._print_parallel_miss_summary('NextData parallel MISS', initial_misses)

        if initial_misses:
            retry_workers = min(self.detail_next_data_workers, len(initial_misses))
            retry_misses = {}
            print(
                f"[INFO] Immediate detail retry: {len(initial_misses)} products, "
                f"workers={retry_workers}, attempts=1"
            )
            with ThreadPoolExecutor(max_workers=retry_workers) as executor:
                future_map = {
                    executor.submit(
                        self._crawl_detail_next_data_worker,
                        index,
                        entry.get('product') or {},
                        mst_specs,
                    ): (index, entry)
                    for index, entry in initial_misses.items()
                }
                for future in as_completed(future_map):
                    index, initial_entry = future_map[future]
                    product = initial_entry.get('product') or {}
                    try:
                        (
                            result_index,
                            combined_data,
                            error,
                            reason,
                            retry_diagnostics,
                        ) = future.result()
                    except Exception as e:
                        result_index = index
                        combined_data = None
                        error = e
                        reason = 'worker_exception'
                        retry_diagnostics = [{
                            'stage': 'worker_exception',
                            'product': product,
                            'message': str(e),
                        }]

                    if combined_data:
                        results[result_index] = combined_data
                        continue

                    diagnostics = list(initial_entry.get('diagnostics') or [])
                    for diagnostic in retry_diagnostics or []:
                        enriched = dict(diagnostic)
                        enriched['immediate_retry'] = 1
                        diagnostics.append(enriched)
                    final_reason = (
                        reason
                        or self._parallel_miss_reason(retry_diagnostics)
                        or initial_entry.get('reason')
                        or 'unknown'
                    )
                    retry_misses[result_index] = {
                        'product': product,
                        'reason': final_reason,
                        'diagnostics': diagnostics,
                        'error': error,
                    }

            recovered_count = len(initial_misses) - len(retry_misses)
            initial_misses = retry_misses
            print(
                f"[INFO] Immediate detail retry result: "
                f"{recovered_count} recovered, remaining={len(initial_misses)}"
            )
            self._print_parallel_miss_summary(
                'Immediate detail retry MISS',
                initial_misses,
            )

        return results, initial_misses, mst_specs

    def collect_detail_next_data_parallel(self, indexed_products):
        if not indexed_products:
            self.parallel_miss_reasons = {}
            return {}

        results, initial_misses, mst_specs = self._collect_detail_initial_parallel(
            indexed_products
        )

        if initial_misses:
            recovered, unresolved = self.collect_detail_zenrows_recovery_parallel(
                initial_misses,
                mst_specs,
            )
            results.update(recovered)
            self.parallel_miss_reasons = {
                index: entry.get('reason') or 'unknown'
                for index, entry in unresolved.items()
            }
            for entry in unresolved.values():
                product = entry.get('product') or {}
                reason = entry.get('reason') or 'unknown'
                message = self._parallel_miss_message(entry.get('diagnostics'))
                detail = f"reason={reason}"
                if message:
                    detail = f"{detail}; {message}"
                self._record_run_error(
                    'detail_zenrows_recovery_exhausted',
                    product,
                    detail,
                )
        else:
            unresolved = {}

        print(
            f"[INFO] NextData detail final result: "
            f"{len(results)}/{len(indexed_products)} loaded, "
            f"unresolved={len(unresolved)}"
        )
        return results

    def collect_detail_zenrows_recovery_parallel(self, misses, mst_specs):
        if not misses:
            return {}, {}

        recovered = {}
        pending = {
            index: {
                'product': entry.get('product') or {},
                'reason': entry.get('reason') or 'unknown',
                'diagnostics': list(entry.get('diagnostics') or []),
                'error': entry.get('error'),
            }
            for index, entry in misses.items()
        }
        print(
            f"[INFO] ZenRows MISS recovery queue: {len(misses)} products, "
            f"workers={min(self.zenrows_recovery_workers, len(misses))}, "
            f"rounds={self.zenrows_recovery_attempts}"
        )

        for attempt in range(1, self.zenrows_recovery_attempts + 1):
            if not pending:
                break

            round_size = len(pending)
            workers = min(self.zenrows_recovery_workers, round_size)
            round_unresolved = {}
            round_recovered = 0
            print(
                f"[INFO] ZenRows recovery round {attempt}/"
                f"{self.zenrows_recovery_attempts}: "
                f"products={round_size}, workers={workers}"
            )

            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {
                    executor.submit(
                        self._crawl_detail_next_data_worker,
                        index,
                        entry.get('product') or {},
                        mst_specs,
                        True,
                    ): (index, entry)
                    for index, entry in pending.items()
                }
                for future in as_completed(future_map):
                    index, initial_entry = future_map[future]
                    product = initial_entry.get('product') or {}
                    try:
                        (
                            result_index,
                            combined_data,
                            error,
                            reason,
                            recovery_diagnostics,
                        ) = future.result()
                    except Exception as e:
                        result_index = index
                        combined_data = None
                        error = e
                        reason = 'worker_exception'
                        recovery_diagnostics = [{
                            'stage': 'worker_exception',
                            'product': product,
                            'message': str(e),
                        }]

                    source = combined_data.get('_detail_source') if combined_data else None
                    if combined_data and source not in ('zenrows_static', 'zenrows_js'):
                        recovery_diagnostics = list(recovery_diagnostics or [])
                        recovery_diagnostics.append({
                            'stage': 'zenrows_recovery_invalid_source',
                            'product': product,
                            'message': f'unexpected recovery source={source}',
                        })
                        combined_data = None
                        error = RuntimeError(f'unexpected recovery source={source}')
                        reason = 'invalid_recovery_source'

                    if combined_data:
                        recovered[result_index] = combined_data
                        round_recovered += 1
                        print(
                            f"[INFO] ZenRows recovery HIT: #{result_index} "
                            f"item={combined_data.get('item') or '-'} "
                            f"source={source} round={attempt}"
                        )
                        continue

                    diagnostics = list(initial_entry.get('diagnostics') or [])
                    for diagnostic in recovery_diagnostics or []:
                        enriched = dict(diagnostic)
                        enriched['recovery_attempt'] = attempt
                        diagnostics.append(enriched)
                    final_reason = (
                        reason
                        or self._parallel_miss_reason(recovery_diagnostics)
                        or initial_entry.get('reason')
                        or 'unknown'
                    )
                    round_unresolved[result_index] = {
                        'product': product,
                        'reason': final_reason,
                        'diagnostics': diagnostics,
                        'error': error,
                    }

            pending = round_unresolved
            print(
                f"[INFO] ZenRows recovery round {attempt} result: "
                f"recovered={round_recovered}, remaining={len(pending)}"
            )
            if pending and attempt < self.zenrows_recovery_attempts:
                time.sleep(random.uniform(0.5, 1.5))

        print(
            f"[INFO] ZenRows MISS recovery result: "
            f"{len(recovered)}/{len(misses)} recovered"
        )
        self._print_parallel_miss_summary(
            'ZenRows recovery unresolved',
            pending,
        )
        return recovered, pending

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

    def build_listing_fallback_row(self, product):
        """Build a clean listing-only row without partial detail artifacts."""
        fallback = {field: None for field in self.EXTRACTED_FIELDS}
        fallback.update(dict(product or {}))
        fallback['item'] = (
            item_id_from_url(fallback.get('product_url') or '')
            or fallback.get('item')
        )
        for field_name in (
            'pick_up_availability',
            'fastest_delivery',
            'delivery_availability',
        ):
            fallback[field_name] = normalize_availability_value(
                fallback.get(field_name),
                field_name,
            )
        fallback['_detail_source'] = 'listing_fallback'
        return fallback

    def save_listing_fallback(self, product, reason=None):
        fallback = self.build_listing_fallback_row(product)
        fallback['_listing_fallback_reason'] = reason
        return self.save_to_retail_com(fallback)

    def save_detail_result(self, combined_data):
        if not combined_data:
            return False

        self._apply_fast_artifacts(combined_data)
        raw_star_rating = ' '.join(str(combined_data.get('star_rating') or '').split())
        raw_star_match = re.search(r'\d+(?:\.(\d+))?', raw_star_rating)
        if raw_star_match and len(raw_star_match.group(1) or '') > 1:
            print(
                f"  [SAVE SKIP] star_rating has unsupported raw precision: "
                f"item={combined_data.get('item') or '-'}, rating={raw_star_rating}"
            )
            return False
        self._normalize_detail_fields(combined_data)

        item_text = str(combined_data.get('item') or '').strip()
        if not item_text.isdigit():
            print(
                f"  [SAVE SKIP] invalid item: "
                f"{combined_data.get('item') or combined_data.get('product_url') or '-'}"
            )
            return False
        combined_data['item'] = item_text
        item = item_text

        if not self._has_required_price(combined_data.get('final_sku_price')):
            print(f"  [SAVE SKIP] final_sku_price missing: item={item}")
            return False

        final_price_amount = self._money_amount(combined_data.get('final_sku_price'))
        original_price_amount = self._money_amount(combined_data.get('original_sku_price'))
        if original_price_amount is not None and final_price_amount > original_price_amount:
            print(
                f"  [SAVE SKIP] final price exceeds original price: item={item}, "
                f"final={combined_data.get('final_sku_price')}, "
                f"original={combined_data.get('original_sku_price')}"
            )
            return False

        review_total = normalize_int(combined_data.get('count_of_reviews'))
        rating_total = normalize_int(combined_data.get('count_of_star_ratings'))
        star_rating = str(combined_data.get('star_rating') or '').strip()
        if review_total is None or rating_total is None or not star_rating:
            print(
                f"  [SAVE SKIP] rating summary incomplete: item={item}, "
                f"reviews={combined_data.get('count_of_reviews')}, "
                f"rating={combined_data.get('star_rating')}, ratings={combined_data.get('count_of_star_ratings')}"
            )
            return False

        if review_total > rating_total:
            print(
                f"  [SAVE SKIP] review count exceeds rating count: item={item}, "
                f"reviews={review_total}, ratings={rating_total}"
            )
            return False

        if star_rating.lower() == 'no ratings yet':
            if review_total != 0 or rating_total != 0:
                print(
                    f"  [SAVE SKIP] no-ratings label conflicts with counts: item={item}, "
                    f"reviews={review_total}, ratings={rating_total}"
                )
                return False
        else:
            if not re.fullmatch(r'\d+(?:\.\d+)?', star_rating):
                print(f"  [SAVE SKIP] invalid star_rating: item={item}, rating={star_rating}")
                return False
            star_value = float(star_rating)
            if not (0.0 <= star_value <= 5.0) or rating_total <= 0:
                print(
                    f"  [SAVE SKIP] star rating conflicts with rating count: item={item}, "
                    f"rating={star_rating}, ratings={rating_total}"
                )
                return False

        expected_review_count = min(review_total, 20)
        collected_review_count = self._formatted_review_count(
            combined_data.get('detailed_review_content')
        )
        if expected_review_count > collected_review_count:
            print(
                f"  [SAVE SKIP] detailed reviews incomplete: item={item}, "
                f"expected={expected_review_count}, collected={collected_review_count}"
            )
            return False

        if not self.upsert_item_mst(combined_data):
            print(f"  [SAVE SKIP] tv_item_mst write failed: item={item}")
            return False
        return self.save_to_retail_com(combined_data)

    def collect_reviews_next_data(
        self,
        item,
        count_of_reviews,
        inline_reviews,
        product,
        star_rating=None,
        count_of_star_ratings=None,
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

        review_summary = {
            'count_of_reviews': count_of_reviews,
            'star_rating': star_rating,
            'count_of_star_ratings': count_of_star_ratings,
        }
        review_total = normalize_int(count_of_reviews)
        rating_total = normalize_int(count_of_star_ratings)
        star_text = str(star_rating or '').strip().lower()
        if review_total == 0 and rating_total in (None, 0) and star_text in ('', 'no ratings yet'):
            return None, review_summary, True

        inline_reviews = inline_reviews or []
        review_texts = []
        product_url = product.get('product_url') if product else None
        scope_params = product_scope_query_params(product_url)

        retry_total = self._env_int('WALMART_TV_REVIEW_NEXTDATA_RETRIES', 1)
        page2_retry_total = self._env_int('WALMART_TV_REVIEW_NEXTDATA_PAGE2_RETRIES', 2)
        extra_page_limit = self._env_int('WALMART_TV_REVIEW_NEXTDATA_EXTRA_PAGES', 2, minimum=0)

        def review_limits(total):
            expected = min(total, 20)
            required_pages = max(1, (expected + 9) // 10)
            last_page = required_pages + extra_page_limit if total >= 20 else required_pages
            return expected, required_pages, last_page

        review_total_hint = review_total if review_total is not None and review_total > 0 else 1
        expected_review_count, page_limit, max_page = review_limits(review_total_hint)

        page_number = 1
        while page_number <= max_page:
            page_added = False
            last_reason = 'no __NEXT_DATA__'
            fatal_scope_error = None
            page_retry_total = page2_retry_total if page_number >= 2 else retry_total

            for retry_index in range(1, page_retry_total + 1):
                review_url = build_review_url(item, page_number, product_url)
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
                    if log and retry_index < page_retry_total:
                        print(f"  [NEXT_DATA review] page{page_number}: empty result, retrying ({retry_index + 1}/{page_retry_total})")
                    continue

                scope_error = review_response_scope_error(next_data, item, product_url)
                if scope_error:
                    last_reason = scope_error
                    fatal_scope_error = scope_error
                    if log and retry_index < page_retry_total:
                        print(
                            f"  [NEXT_DATA review] page{page_number}: "
                            f"{scope_error}, retrying ({retry_index + 1}/{page_retry_total})"
                        )
                    continue

                fatal_scope_error = None
                parsed = parse_review_page(next_data, limit=10)
                page_reviews = parsed.get('reviews') or []
                parsed_review_total = normalize_int(parsed.get('total_review_count'))
                parsed_rating_total = normalize_int(parsed.get('count_of_star_ratings'))
                parsed_star_rating = str(parsed.get('star_rating') or '').strip()
                parsed_summary_complete = (
                    parsed_review_total is not None
                    and parsed_rating_total is not None
                    and bool(parsed_star_rating)
                )
                if not parsed_summary_complete:
                    last_reason = (
                        "review response summary incomplete: "
                        f"reviews={parsed.get('total_review_count')}, "
                        f"rating={parsed.get('star_rating')}, "
                        f"ratings={parsed.get('count_of_star_ratings')}"
                    )
                    fatal_scope_error = last_reason
                    continue

                parsed_summary_key = (
                    parsed_review_total,
                    parsed_star_rating.lower(),
                    parsed_rating_total,
                )
                if page_number == 1:
                    current_rating_total = normalize_int(review_summary.get('count_of_star_ratings'))
                    current_star_rating = str(review_summary.get('star_rating') or '').strip()
                    current_summary_complete = (
                        normalize_int(review_summary.get('count_of_reviews')) is not None
                        and current_rating_total is not None
                        and bool(current_star_rating)
                    )
                    current_summary_key = (
                        normalize_int(review_summary.get('count_of_reviews')),
                        current_star_rating.lower(),
                        current_rating_total,
                    )
                    if current_summary_complete and current_summary_key != parsed_summary_key:
                        last_reason = (
                            "PDP/review summary mismatch: "
                            f"pdp={current_summary_key}, review={parsed_summary_key}, "
                            f"scope={scope_params or '{}'}"
                        )
                        fatal_scope_error = last_reason
                        continue

                    review_summary = {
                        'count_of_reviews': parsed.get('total_review_count'),
                        'star_rating': parsed.get('star_rating'),
                        'count_of_star_ratings': parsed.get('count_of_star_ratings'),
                    }
                    review_total = parsed_review_total
                    expected_review_count, page_limit, max_page = review_limits(review_total)
                else:
                    accepted_summary_key = (
                        normalize_int(review_summary.get('count_of_reviews')),
                        str(review_summary.get('star_rating') or '').strip().lower(),
                        normalize_int(review_summary.get('count_of_star_ratings')),
                    )
                    if accepted_summary_key != parsed_summary_key:
                        last_reason = (
                            "review page summary mismatch: "
                            f"page1={accepted_summary_key}, page{page_number}={parsed_summary_key}, "
                            f"scope={scope_params or '{}'}"
                        )
                        fatal_scope_error = last_reason
                        continue

                if review_total == 0:
                    page_added = True
                    break

                if page_reviews:
                    review_texts.extend(page_reviews)
                    page_added = True
                    break

                last_reason = 'no parsed reviews'
                if log and retry_index < page_retry_total:
                    print(f"  [NEXT_DATA review] page{page_number}: no parsed reviews, retrying ({retry_index + 1}/{page_retry_total})")

            if fatal_scope_error and not page_added:
                add_error('review_scope_mismatch', fatal_scope_error)
                if log:
                    print(f"  [NEXT_DATA review ERROR] {fatal_scope_error}")
                return None, review_summary, False

            if not page_added and page_number == 1 and inline_reviews:
                review_texts.extend(inline_reviews)
                page_added = True
                if log:
                    print(f"  [NEXT_DATA review] page1: fallback reused {len(inline_reviews)} inline reviews from detail payload")

            if not page_added:
                add_error(f'review_page{page_number}_next_data', last_reason)

            detailed_review_content = format_reviews(review_texts, limit=20)
            collected_reviews = self._formatted_review_count(detailed_review_content)
            if page_number >= page_limit and collected_reviews >= expected_review_count:
                break
            page_number += 1

        detailed_review_content = format_reviews(review_texts, limit=20)
        collected_reviews = self._formatted_review_count(detailed_review_content)
        complete = collected_reviews >= expected_review_count

        if not complete:
            message = f'expected {expected_review_count} reviews from {review_total}, collected {collected_reviews}'
            if log:
                print(f"  [NEXT_DATA review WARNING] {message}")
            add_error('review_next_data_incomplete', message)

        return detailed_review_content, review_summary, complete

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
        for url in (product_url, build_item_url(requested_item, product_url)):
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
                    print(f"  [NEXT_DATA detail] price missing for item={item}; trying next candidate")
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
                page_sku_value=parsed.get('sku'),
            )
            screen_size, spec_source, name_screen_size, page_screen_size = self.extract_screen_size(
                spec_product,
                None,
                mst_screen_size,
                log=log,
                page_screen_size_raw=parsed.get('screen_size'),
            )
            detailed_review_content, review_summary, review_complete = self.collect_reviews_next_data(
                item,
                parsed.get('count_of_reviews'),
                parsed.get('inline_reviews') or [],
                product,
                star_rating=parsed.get('star_rating'),
                count_of_star_ratings=parsed.get('count_of_star_ratings'),
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
                        "ZenRows recovery required"
                    )
                return None

            parsed.update(review_summary)
            self._fill_similar_from_json_response(parsed, url, item, client, log=log)

            source = result.get('source') or 'next_data'
            combined_data = product.copy()
            combined_data.update({
                'item': item,
                'sku': sku,
                'count_of_reviews': parsed.get('count_of_reviews') or '0',
                'star_rating': parsed.get('star_rating'),
                'count_of_star_ratings': parsed.get('count_of_star_ratings'),
                'offer': parsed.get('offer'),
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
        """Collect a validated detail row without opening a local browser."""
        product_url = product.get('product_url') if product else None
        if not product_url:
            print("  [SKIP] product_url missing; detail collection skipped")
            return None

        if not skip_fast:
            return self.collect_detail_next_data_parallel([(1, product)]).get(1)

        mst_specs = self.load_mst_specs_cache([product])
        misses = {
            1: {
                'product': product,
                'reason': 'zenrows_recovery_requested',
                'diagnostics': [],
                'error': None,
            }
        }
        recovered, unresolved = self.collect_detail_zenrows_recovery_parallel(
            misses,
            mst_specs,
        )
        combined_data = recovered.get(1)
        if combined_data:
            return combined_data

        unresolved_entry = unresolved.get(1) or misses[1]
        diagnostics = unresolved_entry.get('diagnostics') or []
        error = unresolved_entry.get('error')
        reason = unresolved_entry.get('reason') or 'unknown'
        message = self._parallel_miss_message(diagnostics)
        detail = f"reason={reason}"
        if message:
            detail = f"{detail}; {message}"
        if error and not message:
            detail = f"{detail}; {error}"
        self._record_run_error(
            'detail_zenrows_recovery_exhausted',
            product,
            detail,
        )
        return None

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
            return False

        try:
            if not self.ensure_db_connection():
                return False

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
            return True

        except Exception as e:
            print(f"[ERROR] upsert_item_mst failed: {item}: {e}")
            try:
                if self.db_conn and not self.db_conn.closed:
                    self.db_conn.rollback()
            except Exception:
                pass
            return False

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

    def extract_sku(self, product, product_url, modal_tree, mst_sku, log=True, page_sku_value=None):
        """SKU 최종값 결정: 마스터 → 브랜드 정규식 → PDP spec/모달."""
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

        page_sku = str(page_sku_value or '').strip() or None
        page_sku_source = "PDP spec" if page_sku else None
        if modal_tree is not None:
            page_sku = self.safe_extract_chain(modal_tree, 'sku')
            page_sku_source = "모달" if page_sku else None

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
            sku_source = page_sku_source

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

    def extract_screen_size(
        self,
        product,
        modal_tree,
        mst_screen_size,
        log=True,
        page_screen_size_raw=None,
    ):
        """screen_size 최종값 결정: 상품명 정규식 → PDP spec/모달 → 마스터."""
        name_screen_size = self.extract_screen_size_by_regex(
            product.get('retailer_sku_name'),
            r'(\d+\.?\d*)(?:[\s-]*inch(?:es)?|["“”″])',
            'extract_screen_size_name',
        )
        if name_screen_size and log:
            print(f"  [screen_size 상품명 추출] {name_screen_size}")

        page_screen_size_source = "PDP spec" if page_screen_size_raw else None
        if modal_tree is not None:
            page_screen_size_raw = self.safe_extract_chain(modal_tree, 'screen_size')
            page_screen_size_source = "모달" if page_screen_size_raw else None
        page_screen_size = None
        if page_screen_size_raw:
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
            spec_source = page_screen_size_source
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
