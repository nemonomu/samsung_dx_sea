"""
Walmart Detail 페이지 크롤러

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
- hhp_retail_com (상세 정보 + 리뷰)
"""

import sys
import os
import time
import random
import traceback
from datetime import datetime
from lxml import html
from DrissionPage import ChromiumPage

# 공통 환경 설정 (작업 디렉토리, 한글 출력, 경로 설정)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.setup import setup_environment
setup_environment(__file__)

from common.walmart_base import WalmartBaseCrawler


class WalmartDetailCrawler(WalmartBaseCrawler):
    """
    Walmart Detail 페이지 크롤러
    """

    EXTRACTED_FIELDS = [
        'item',
        'count_of_reviews',
        'star_rating',
        'count_of_star_ratings',
        'number_of_ppl_purchased_yesterday',
        'number_of_ppl_added_to_carts',
        'sku_popularity',
        'savings',
        'discount_type',
        'final_sku_price',
        'original_sku_price',
        'hhp_storage',
        'hhp_color',
        'hhp_carrier',
        'retailer_sku_name_similar',
        'detailed_review_content',
    ]

    PASSTHROUGH_FIELDS = [
        'page_type',
        'retailer_sku_name',
        'product_url',
        'offer',
        'pick_up_availability',
        'fastest_delivery',
        'delivery_availability',
        'available_quantity_for_purchase',
        'inventory_status',
        'sku_status',
        'main_rank',
        'bsr_rank',
        'calendar_week',
    ]

    SAVE_META_FIELDS = {
        'country': 'SEA',
        'product': 'HHP',
        'account_name': 'account_name',
        'crawl_strdatetime': 'CURRENT_TIMESTAMP',
        'batch_id': 'batch_id',
    }

    def __init__(self, batch_id=None, test_mode=False):
        """초기화. batch_id: 통합 크롤러에서 전달, test_mode: 테스트 모드 여부"""
        super().__init__()
        self.account_name = 'Walmart'
        self.walmart_zip_code = '11581'
        self.walmart_search_keyword = 'cellphone'
        self.page_type = 'detail'
        self.batch_id = batch_id
        self.test_mode = test_mode

        # DrissionPage 드라이버 (Selenium driver 대신 사용)
        self.page = None

        # 스크린샷 캡처 설정
        self.capture_enabled = True  # False로 변경하면 캡처 비활성화
        self.capture_all = test_mode or (batch_id is None)  # 테스트/개별실행: 모든 제품, 운영: page_type별 제한
        self.capture_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'capture')
        # 운영 모드: page_type별 캡처 제한 (main 10개, bsr 10개)
        self.capture_main_count = 0
        self.capture_bsr_count = 0
        self.capture_limit_per_type = 10

        # SPEC DIFF 누적 (run() 끝에 일괄 출력용)
        # 각 entry: {'item': str, 'mst_sku': str|None, 'page_sku': str|None}
        self.spec_diffs = []

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

            total_saved = 0

            for i, product in enumerate(product_list, 1):
                try:
                    retailer_sku_name = product.get('retailer_sku_name') or 'N/A'
                    product_url = product.get('product_url', 'N/A')
                    url_display = product_url[:80] + '...' if len(product_url) > 80 else product_url
                    print(f"\n[{i}/{len(product_list)}] {retailer_sku_name}")
                    print(f"  URL: {url_display}")

                    combined_data = self.crawl_detail(product)
                    if combined_data:
                        self.upsert_item_mst(combined_data)
                        if self.save_to_retail_com(combined_data):
                            total_saved += 1

                    time.sleep(random.uniform(2, 4))

                except Exception as e:
                    error_msg = str(e).lower()
                    print(f"[ERROR] Product {i} failed: {e}")

                    # DOM 타임아웃 → 브라우저 재시작만 하고 해당 제품은 스킵 (재시도 안 함)
                    if "dom timeout" in error_msg:
                        print(f"[INFO] DOM 타임아웃 - 브라우저 재시작 후 다음 제품으로")
                        self.restart_browser()
                        if self.save_to_retail_com(product):
                            total_saved += 1
                        continue

                    if "redirect detected" in error_msg:
                        print("[INFO] 리다이렉트 감지 - product_list 기본 정보만 저장")
                        self.ensure_listing_item(product)
                        if self.save_to_retail_com(product):
                            total_saved += 1
                        continue

                    # 일반 타임아웃 또는 페이지 로드 실패 → 브라우저 재시작 후 재시도
                    if "timeout" in error_msg or "time out" in error_msg or "url unchanged" in error_msg:
                        print(f"[INFO] 브라우저 재시작 후 재시도")
                        if self.restart_browser():
                            try:
                                combined_data = self.crawl_detail(product)
                                if combined_data:
                                    self.upsert_item_mst(combined_data)
                                    if self.save_to_retail_com(combined_data):
                                        total_saved += 1
                                print(f"[SUCCESS] 재시도 성공: {retailer_sku_name[:30]}")
                                continue
                            except Exception as retry_e:
                                print(f"[ERROR] 재시도 실패: {retry_e}")

                    # 모든 에러 발생 시 product_list 기본 정보는 저장
                    if self.save_to_retail_com(product):
                        total_saved += 1
                    continue

            table_name = 'test_hhp_retail_com' if self.test_mode else 'hhp_retail_com'
            print(f"[DONE] Processed: {len(product_list)}, Saved: {total_saved}, Table: {table_name}, batch_id: {self.batch_id}")

            # ===== SPEC DIFF 일괄 출력 (마스터 vs 페이지 추출 값이 다른 item들) =====
            if self.spec_diffs:
                print(f"\n{'=' * 80}")
                print(f"[SPEC DIFF] 마스터 vs 페이지 추출 값 불일치: 총 {len(self.spec_diffs)}건")
                print(f"{'=' * 80}")
                for d in self.spec_diffs:
                    parts = [f"item={d['item']}"]
                    parts.append(
                        f"sku: mst={d['mst_sku']!r} / extracted={d.get('page_sku')!r}"
                        f" [page={d.get('page_sku')!r}]"
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
            self.batch_id = 't_w_20260525_000840'

        # 2. DB 연결
        if not self.connect_db():
            return False

        # 3. XPath 로드
        if not self.load_xpaths(self.account_name, self.page_type, 'SEA', 'HHP'):
            return False

        # 4. 브라우저 설정 (DrissionPage)
        if not self.setup_browser():
            print("[ERROR] Initialize failed: DrissionPage setup failed")
            return False

        # 5. 세션 초기화 (example.com → walmart.com → 검색)
        self.initialize_session()

        # 6. 로그 정리
        self.cleanup_old_logs()

        print(f"[INFO] batch_id: {self.batch_id}")
        return True

    def load_product_list(self):
        """wmart_hhp_product_list 테이블에서 제품 URL 및 기본 정보 조회"""
        try:
            cursor = self.db_conn.cursor()

            query = """
                SELECT
                    pl.retailer_sku_name,
                    pl.offer, pl.pick_up_availability, pl.fastest_delivery,
                    pl.delivery_availability, pl.sku_status,
                    pl.available_quantity_for_purchase, pl.inventory_status,
                    pl.main_rank, pl.bsr_rank, pl.product_url, pl.calendar_week,
                    pl.crawl_strdatetime, pl.page_type
                FROM wmart_hhp_product_list pl
                WHERE pl.account_name = %s
                  AND pl.batch_id = %s
                  AND pl.product_url IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM hhp_retail_com rc
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
                    'crawl_strdatetime': row[12],
                    'page_type': row[13]
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

    def crawl_detail(self, product):
        """상세 페이지 크롤링: 페이지 로드 → 데이터 추출 → 스펙 추출 → 유사제품 추출 → 리뷰 추출 (DrissionPage 사용)"""
        try:
            product_url = product.get('product_url')
            # take_capture()에 넘기기 위해 product_list의 page_type('main' 또는 'bsr')을 로컬 변수로 추출
            # 운영 모드에서 page_type별로 캡처 갯수를 제한(main 10개 / bsr 10개)하기 위함
            page_type = product.get('page_type')
            if not product_url:
                print(f"  [SKIP] product_url 없음 → 크롤링/저장 건너뜀")
                return product

            current_url, is_redirect = self.load_detail_page(product_url, product.get('retailer_sku_name'))

            # CAPTCHA/Sorry 페이지 사전 처리
            blocking_result = self.handle_detail_blocking_pages(product)
            if blocking_result:
                return blocking_result

            page_html = self.page.run_js('return document.documentElement.outerHTML')
            tree = html.fromstring(page_html)

            # item ID 추출 (페이지 로드 후 추출 - 에러 시 item NULL로 식별)
            item = self.extract_item(current_url if is_redirect else product_url)
            print(f"[item] item ID 추출 {'완료' if item else '실패'}")
            
            # 캡처 1: 상품 페이지 로드 후 (성공/실패 로그는 take_capture 내부에서 처리, 스킵 시 무로그)
            capture_allowed = self.should_take_capture(page_type)
            if capture_allowed and self.take_capture(item, 1):
                self.mark_capture_count(page_type)

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

            # ========== 1-3단계: 추가 필드 추출 ==========
            number_of_ppl_purchased_yesterday = self.convert_first_number(tree, 'number_of_ppl_purchased_yesterday')
            number_of_ppl_added_to_carts = self.convert_first_number(tree, 'number_of_ppl_added_to_carts')
            sku_popularity = self.safe_extract_chain_join(tree, 'sku_popularity', separator=", ")
            discount_type = self.safe_extract_chain(tree, 'discount_type')

            # ========== 2단계: HHP 스펙 (모달) ==========
            mst_sku = self.get_hhp_sku_from_mst(item)
            modal_tree = None

            # 스펙 버튼 탐색/클릭 → 모달 열기 → sku/carrier/color/storage 추출
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

            sku, sku_source, page_sku = self.extract_sku(modal_tree, mst_sku)

            hhp_carrier = self.safe_extract_chain(modal_tree, 'hhp_carrier') if modal_tree is not None else None
            hhp_color = self.safe_extract_chain(modal_tree, 'hhp_color') if modal_tree is not None else None
            hhp_storage = self.safe_extract_chain(modal_tree, 'hhp_storage') if modal_tree is not None else None

            # ========== 3단계: 유사 제품 ==========
            # 섹션 탐색(스크롤 fallback) → 절대경로 XPath로 카드 이름 한번에 추출 → ' ||| '로 join
            self.scroll_find_element('similar_products_section', max_scrolls=5, label='유사제품 섹션 탐색')

            # 캡처 2: 유사제품 섹션 (찾았으면 해당 위치, 못찾았으면 현재 위치)
            if capture_allowed:
                self.take_capture(item, 2)

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
            detailed_review_content, count_of_reviews = self.extract_detailed_reviews(
                item,
                count_of_reviews,
                capture_allowed,
            )

            # 결합된 데이터
            combined_data = product.copy()
            combined_data.update({
                'item': item,
                'sku': sku,
                'count_of_reviews': count_of_reviews,
                'star_rating': star_rating,
                'count_of_star_ratings': count_of_star_ratings,
                'number_of_ppl_purchased_yesterday': number_of_ppl_purchased_yesterday,
                'number_of_ppl_added_to_carts': number_of_ppl_added_to_carts,
                'sku_popularity': sku_popularity,
                'savings': savings,
                'discount_type': discount_type,
                'final_sku_price': final_sku_price,
                'original_sku_price': original_sku_price,
                'hhp_storage': hhp_storage,
                'hhp_color': hhp_color,
                'hhp_carrier': hhp_carrier,
                'retailer_sku_name_similar': retailer_sku_name_similar,
                'detailed_review_content': detailed_review_content,
            })

            # 마스터 vs 페이지 추출값 비교 — 다르면 누적 (run() 끝에 일괄 출력)
            has_sku_diff = (mst_sku or page_sku) and mst_sku != page_sku
            if has_sku_diff:
                self.spec_diffs.append({
                    'item': item,
                    'mst_sku': mst_sku,
                    'page_sku': page_sku,
                })

            # ──── 결과 요약 (트리 구조) ────
            print(f"\n──── 결과 요약 ────")
            similar_count = (retailer_sku_name_similar.count(' ||| ') + 1) if retailer_sku_name_similar else 0
            review_count = (detailed_review_content.count(' ||| ') + 1) if detailed_review_content else 0
            sku_display = f"{sku} (출처: {sku_source})" if sku and sku_source else (sku or '-')

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
            print(f"  ├─ hhp_carrier: {hhp_carrier or '-'}")
            print(f"  ├─ hhp_color: {hhp_color or '-'}")
            print(f"  ├─ hhp_storage: {hhp_storage or '-'}")
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

        self.ensure_listing_item(product)

        try:
            if not self.ensure_db_connection():
                return False

            cursor = self.db_conn.cursor()

            # 테스트 모드면 test_hhp_retail_com, 통합 크롤러면 hhp_retail_com
            table_name = 'test_hhp_retail_com' if self.test_mode else 'hhp_retail_com'

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

    def get_hhp_sku_from_mst(self, item):
        """마스터 테이블에서 HHP SKU만 조회한다."""
        if not item:
            return None

        try:
            if not self.ensure_db_connection():
                return None

            cursor = self.db_conn.cursor()
            cursor.execute("""
                SELECT sku FROM hhp_item_mst
                WHERE item = %s AND account_name = %s AND is_product = TRUE
            """, (item, self.account_name))
            row = cursor.fetchone()
            cursor.close()

            if row:
                return row[0]
            return None
        except Exception:
            return None

    def upsert_item_mst(self, product):
        """hhp_item_mst 테이블에 INSERT 또는 UPDATE
        - 조회 결과 없음 → INSERT (sku)
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

            # 기존 데이터 조회
            cursor.execute("""
                SELECT sku FROM hhp_item_mst
                WHERE item = %s AND account_name = %s
            """, (item, self.account_name))

            row = cursor.fetchone()

            if row is None:
                # 조회 결과 없음 → INSERT
                cursor.execute("""
                    INSERT INTO hhp_item_mst (item, account_name, sku, product_url)
                    VALUES (%s, %s, %s, %s)
                """, (item, self.account_name, new_sku, product_url))
                self.db_conn.commit()
                print(f"  → DB: ITEM_MST INSERT")
            else:
                # 기존 값이 없는 필드만 업데이트
                existing_sku = row[0]
                updates = []
                params = []

                if not (existing_sku or '') and new_sku:
                    updates.append("sku = %s")
                    params.append(new_sku)

                if updates:
                    # 업데이트할 필드와 값 저장 (로그용)
                    updated_info = []
                    if not (existing_sku or '') and new_sku:
                        updated_info.append(f"sku={new_sku}")

                    updates.append("product_url = %s")
                    params.append(product_url)
                    updates.append("updated_at = %s")
                    params.append(datetime.now())
                    params.extend([item, self.account_name])

                    cursor.execute(f"""
                        UPDATE hhp_item_mst SET {', '.join(updates)}
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

    def extract_sku(self, modal_tree, mst_sku):
        """SKU 최종값 결정: 마스터 → 모달."""
        page_sku = self.safe_extract_chain(modal_tree, 'sku') if modal_tree is not None else None

        sku = None
        sku_source = None
        if mst_sku:
            sku = mst_sku
            sku_source = "마스터"
        elif page_sku:
            sku = page_sku
            sku_source = "모달"

        return sku, sku_source, page_sku

def main():
    """개별 실행 진입점 (테스트 모드, 기본 배치 ID 사용)"""
    crawler = WalmartDetailCrawler(batch_id=None, test_mode=True)
    crawler.run()
    input("Press Enter to exit...")


if __name__ == '__main__':
    main()
