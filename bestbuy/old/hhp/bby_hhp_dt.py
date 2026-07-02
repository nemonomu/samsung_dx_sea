"""
BestBuy HHP Detail 페이지 크롤러

================================================================================
실행 모드
================================================================================
- 개별 실행: batch_id=None (하드코딩된 batch_id 사용)
- 통합 크롤러: batch_id를 파라미터로 전달

================================================================================
주요 기능
================================================================================
- bby_hhp_product_list 테이블에서 해당 batch_id의 제품 URL 조회
- 각 제품 상세 페이지에서 리뷰, 별점, 스펙 등 추출
- Main/BSR/Trend에서 수집한 모든 제품 처리

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

from common.bestbuy_base import BestBuyBaseCrawler


class BestBuyHHPDetailCrawler(BestBuyBaseCrawler):
    # ========================================================================
    # hhp_retail_com 컬럼 매핑 (INSERT/UPDATE 공통 단일 소스)
    # ========================================================================
    # crawl_detail이 combined_data에 채우는 추출 필드들 - INSERT/UPDATE 모두 사용.
    # 새 추출 필드 추가 시
    #   1) crawl_detail에서 combined_data에 값 추가
    #   2) EXTRACTED_FIELDS 리스트에 추가
    # -> INSERT(dt)와 UPDATE(dt_update) 모두 자동 반영됨.
    EXTRACTED_FIELDS = [
        'item',
        'count_of_reviews',
        'star_rating',
        'count_of_star_ratings',
        'final_sku_price',
        'original_sku_price',
        'savings',
        'trade_in',
        'recommendation_intent',
        'hhp_storage',
        'hhp_color',
        'hhp_carrier',
        'detailed_review_content',
        'retailer_sku_name_similar',
    ]

    # product_list에서 전달받는 메타 필드 (INSERT만 사용 - UPDATE는 기존 row 그대로 유지)
    PASSTHROUGH_FIELDS = [
        'page_type',
        'retailer_sku_name',
        'product_url',
        'offer',
        'pick_up_availability',
        'fastest_delivery',
        'sku_status',
        'main_rank',
        'bsr_rank',
        'trend_rank',
        'promotion_type',
        'calendar_week',
    ]

    # DB 저장 시 코드가 직접 채우는 메타 필드
    SAVE_META_FIELDS = {
        'country': 'SEA',
        'product': 'HHP',
        'crawl_strdatetime': 'CURRENT_TIMESTAMP',
        'account_name': 'account_name',
        'batch_id': 'batch_id',
    }

    """
    BestBuy HHP Detail 페이지 크롤러
    """

    def __init__(self, batch_id=None, test_mode=False):
        """초기화. batch_id: 통합 크롤러에서 전달, test_mode: 테스트 모드 여부"""
        super().__init__()
        self.account_name = 'Bestbuy'
        self.page_type = 'detail'
        self.bestbuy_zip_code = '10010'
        self.bestbuy_search_keyword = 'cellphone'
        self.item_mst_table = 'hhp_item_mst'
        self.batch_id = batch_id
        self.test_mode = test_mode

        # DrissionPage 드라이버 (Selenium driver 대신 사용)
        self.page = None

        # SKU DIFF 누적 (run() 끝에 일괄 출력용)
        # 각 entry: {'item': str, 'mst_sku': str|None, 'extracted_sku': str|None}
        self.sku_diffs = []

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
                    print(f"\n{'='*70}")
                    print(f"[{i}/{len(product_list)}] {retailer_sku_name[:60]}")
                    print(f"{'='*70}")

                    combined_data = self.crawl_detail(product)
                    if combined_data:
                        self.upsert_item_mst(combined_data)
                        if self.save_to_retail_com(combined_data):
                            total_saved += 1

                    time.sleep(random.uniform(5, 8))

                except Exception as e:
                    error_msg = str(e).lower()
                    print(f"[ERROR] Product {i} failed: {e}")

                    # 에러 페이지 감지 시 현재 상품 포함 남은 상품은 크롤링하지 않고 기본 저장만 수행
                    if "error page detected" in error_msg:
                        remaining_products = product_list[i - 1:]
                        print(f"[WARNING] 에러 페이지 감지 - 남은 {len(remaining_products)}개 상품 기본 저장 후 크롤링 중단")
                        for remaining_product in remaining_products:
                            if self.save_to_retail_com(remaining_product):
                                total_saved += 1
                        break

                    # 타임아웃 또는 페이지 로드 실패시 브라우저 재시작 후 재시도
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
                            except Exception as retry_e:
                                print(f"[ERROR] 재시도 실패: {retry_e}")
                    continue

            table_name = 'test_hhp_retail_com' if self.test_mode else 'hhp_retail_com'
            print(f"[DONE] Processed: {len(product_list)}, Saved: {total_saved}, Table: {table_name}, batch_id: {self.batch_id}")

            # ===== SKU DIFF 일괄 출력 (마스터 vs 페이지 추출 sku가 다른 item들) =====
            if self.sku_diffs:
                print(f"\n{'=' * 80}")
                print(f"[SKU DIFF] 마스터 vs 페이지 추출 sku 불일치 총 {len(self.sku_diffs)}건")
                print(f"{'=' * 80}")
                for d in self.sku_diffs:
                    parts = [f"item={d['item']}"]
                    parts.append(f"sku: mst={d.get('mst_sku')!r} / extracted={d.get('extracted_sku')!r}")
                    print("  " + " | ".join(parts))
                print(f"{'=' * 80}\n")
            else:
                print(f"\n[SKU DIFF] 마스터 vs 페이지 추출 sku 불일치 없음")

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
            self.batch_id = 't_b_20260524_115755'

        # 2. DB 연결
        if not self.connect_db():
            return False

        # 3. XPath 로드
        if not self.load_xpaths(self.account_name, self.page_type, 'SEA', 'HHP'):
            return False

        # 4. 브라우저 설정 및 BestBuy ZIP 코드 세션 초기화
        if not self.setup_bestbuy_browser():
            print("[ERROR] Initialize failed: BestBuy browser setup failed")
            return False

        # 5. 로그 정리
        self.cleanup_old_logs()

        print(f"[INFO] batch_id: {self.batch_id}")
        return True

    def load_product_list(self):
        """bby_hhp_product_list 조회: batch_id 기준으로 제품 URL 및 기본 정보 조회"""
        try:
            cursor = self.db_conn.cursor()

            query = """
                SELECT
                    page_type, retailer_sku_name, offer,
                    pick_up_availability, fastest_delivery,
                    sku_status, promotion_type, main_rank, bsr_rank, trend_rank,
                    product_url, calendar_week
                FROM bby_hhp_product_list
                WHERE account_name = %s AND batch_id = %s AND product_url IS NOT NULL
                ORDER BY id
            """

            cursor.execute(query, (self.account_name, self.batch_id))
            rows = cursor.fetchall()
            cursor.close()

            product_list = []
            for row in rows:
                product = {
                    'account_name': self.account_name,
                    'page_type': row[0],
                    'retailer_sku_name': row[1],
                    'offer': row[2],
                    'pick_up_availability': row[3],
                    'fastest_delivery': row[4],
                    'sku_status': row[5],
                    'promotion_type': row[6],
                    'main_rank': row[7],
                    'bsr_rank': row[8],
                    'trend_rank': row[9],
                    'product_url': row[10],
                    'calendar_week': row[11]
                }
                product_list.append(product)

            print(f"[INFO] Loaded {len(product_list)} products")
            return product_list

        except Exception as e:
            print(f"[ERROR] Failed to load product list: {e}")
            return []

    def crawl_detail(self, product):
        """상세 페이지 크롤링: 페이지 로드 → 스크롤 전 추출 → 추출 → 유사제품 추출 → 리뷰 추출 → product_list + detail 데이터 결합 (DrissionPage 사용)"""
        try:
            product_url = product.get('product_url')
            if not product_url:
                return product

            self.load_detail_page(product_url)

            page_html = self.page.run_js('return document.documentElement.outerHTML')
            tree = html.fromstring(page_html)

            # 원본 URL에서 item 추출 (페이지 로드 실패해도 정확한 item 유지)
            item = self.extract_item(product_url)

            # ========== 1단계: 별점/리뷰수 상단 추출 ==========
            top_star_rating = self.safe_extract_chain(tree, 'top_star_rating')
            top_count_of_reviews = self.safe_extract_chain(tree, 'top_count_of_reviews')

            # ========== 2단계: 가격 / 원가 / 할인 추출 (상세페이지에서 정상 추출) ==========
            final_sku_price, original_sku_price, savings = self.extract_price_info(tree)

            # ========== 3단계: Model / SKU Number 추출 (SKU Number는 리뷰 URL용) ==========
            mst_sku = self.get_item_mst_sku(item)
            sku, sku_number = self.extract_sku_info(tree)
            trade_in = self.safe_extract_chain(tree, 'trade_in')

            # ========== 4단계: HHP 스펙 (모달) ==========
            tree = self.open_spec_modal()

            hhp_carrier = None
            hhp_color = None
            hhp_storage = None
            if tree is not None:
                for spec_attempt in range(1, 3):
                    hhp_carrier = self.extract_hhp_carrier(tree, product)
                    hhp_color = self.safe_extract_chain(tree, 'hhp_color')
                    hhp_storage = self.safe_extract_chain(tree, 'hhp_storage')

                    if hhp_carrier and hhp_color and hhp_storage:
                        break

                    if spec_attempt < 2:
                        time.sleep(1)
                        page_html = self.page.run_js('return document.documentElement.outerHTML')
                        tree = html.fromstring(page_html)

                missing_specs = []
                if not hhp_carrier:
                    missing_specs.append('hhp_carrier')
                if not hhp_color:
                    missing_specs.append('hhp_color')
                if not hhp_storage:
                    missing_specs.append('hhp_storage')
                if missing_specs:
                    print(f"  [WARN] HHP 스펙 일부 미추출: {', '.join(missing_specs)}")

            if tree is not None:
                self.close_spec_modal()

            # ========== 5단계: 유사 제품 추출 ==========
            self.scroll_find_element('similar_products_section', max_scrolls=5, label='유사제품 섹션 탐색')
            page_html = self.page.run_js('return document.documentElement.outerHTML')
            tree = html.fromstring(page_html)
            retailer_sku_name_similar = self.safe_extract_chain_join(
                tree, 'similar_product_name', separator=' ||| '
            )

            # ========== 6단계: 리뷰 섹션 스크롤 → 별점 / 리뷰수 / recommendation_intent 추출 ==========
            star_rating = None
            count_of_reviews = None
            count_of_star_ratings = None
            recommendation_intent = None

            # 리뷰 없음 여부 먼저 판별
            # 1) "not yet reviewed" 포함
            # 2) Syndicated 리뷰 (예: "45 reviews from ...") - BestBuy 자체 리뷰 아님
            is_no_reviews = self.is_no_review_product(top_count_of_reviews)

            if is_no_reviews:
                # 리뷰 없음 시 일괄 할당
                count_of_reviews = "0"
                star_rating = "Not yet reviewed"
                count_of_star_ratings = "0"
            else:
                # 1. 스크롤 전 상단에서 추출한 값 우선 사용
                star_rating = self.convert_first_number(top_star_rating)
                count_of_reviews = self.convert_first_number(top_count_of_reviews)
                count_of_star_ratings = count_of_reviews

                # 리뷰 섹션으로 스크롤
                if self.scroll_find_element(
                    'review_section',
                    max_scrolls=5,
                    label='리뷰 섹션 탐색',
                    scroll_px=(150, 200),
                ):
                    time.sleep(3)

                # 2. 하단 추출 시도 (retry 3회)
                fallback_used = False
                for retry in range(3):
                    page_html = self.page.run_js('return document.documentElement.outerHTML')
                    tree = html.fromstring(page_html)

                    # 상단 추출 실패 시 하단 개별 XPath로 star_rating/count_of_reviews 재추출
                    if star_rating is None or count_of_reviews is None:
                        if not fallback_used:
                            print(f"  [하단 fallback] star_rating/count_of_reviews 개별 XPath 시도")
                            fallback_used = True

                        if star_rating is None:
                            star_rating_raw = self.safe_extract_chain(tree, 'star_rating')
                            star_rating = self.convert_first_number(star_rating_raw)

                        if count_of_reviews is None:
                            count_of_reviews_raw = self.safe_extract_chain(tree, 'count_of_reviews')
                            count_of_reviews = self.convert_first_number(count_of_reviews_raw)

                    count_of_star_ratings = count_of_reviews

                    if recommendation_intent is None:
                        recommendation_intent = self.extract_recommendation_intent(tree, 'recommendation_intent')

                    if star_rating and count_of_reviews and count_of_star_ratings and recommendation_intent:
                        break

                    # 실패 시 재시도 대기
                    if retry < 2:
                        time.sleep(random.uniform(1, 2))
                    else:
                        missing = []
                        if not star_rating: missing.append('star_rating')
                        if not count_of_reviews: missing.append('count_of_reviews')
                        if not count_of_star_ratings: missing.append('count_of_star_ratings')
                        if not recommendation_intent: missing.append('recommendation_intent')
                        if missing:
                            print(f"  [WARN] 리뷰 데이터 일부 미추출: {', '.join(missing)}")

            # ========== 7단계: 리뷰 더보기 버튼 클릭 후 detailed_review_content 추출 ==========
            detailed_review_content = None
            if is_no_reviews:
                print(f"  [INFO] 리뷰 0건 - 상세 리뷰 추출 스킵")
            else:
                detailed_review_content, recommendation_intent = self.extract_detailed_reviews(
                    product,
                    sku_number,
                    recommendation_intent,
                )

            # 결합된 데이터
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
                'trade_in': trade_in,
                'recommendation_intent': recommendation_intent,
                'hhp_storage': hhp_storage,
                'hhp_color': hhp_color,
                'hhp_carrier': hhp_carrier,
                'detailed_review_content': detailed_review_content,
                'retailer_sku_name_similar': retailer_sku_name_similar,
            })

            # 마스터 sku vs 추출 sku 비교 후 다르면 누적 (run() 끝에 일괄 출력)
            if mst_sku and sku and mst_sku != sku:
                self.sku_diffs.append({
                    'item': item,
                    'mst_sku': mst_sku,
                    'extracted_sku': sku,
                })

            # 결과 요약 로그
            similar_count = (retailer_sku_name_similar.count(' ||| ') + 1) if retailer_sku_name_similar else 0
            review_count = (detailed_review_content.count(' ||| ') + 1) if detailed_review_content else 0

            print(f"  ├─ item: {item or '-'}")
            print(f"  ├─ sku: {sku or '-'}")
            print(f"  ├─ sku_number: {sku_number or '-'}")
            print(f"  ├─ final_sku_price: {final_sku_price or '-'}")
            print(f"  ├─ original_sku_price: {original_sku_price or '-'}")
            print(f"  ├─ savings: {savings or '-'}")
            print(f"  ├─ count_of_reviews: {count_of_reviews or '0'}")
            print(f"  ├─ star_rating: {star_rating or '-'}")
            print(f"  ├─ count_of_star_ratings: {count_of_star_ratings or '-'}")
            print(f"  ├─ recommendation_intent: {recommendation_intent or '-'}")
            print(f"  ├─ trade_in: {combined_data.get('trade_in') or '-'}")
            print(f"  ├─ hhp_carrier: {hhp_carrier or '-'}")
            print(f"  ├─ hhp_color: {hhp_color or '-'}")
            print(f"  ├─ hhp_storage: {hhp_storage or '-'}")
            print(f"  ├─ retailer_sku_name_similar: {'찾음 (' + str(similar_count) + '개)' if retailer_sku_name_similar else '없음'}")
            print(f"  └─ detailed_review_content: {review_count}개")

            return combined_data

        except Exception as e:
            error_msg = str(e).lower()
            print(f"[ERROR] Detail crawl failed: {e}")

            # 타임아웃/페이지 로드 실패/리다이렉트는 run()의 전용 분기에서 처리한다.
            if (
                "timeout" in error_msg
                or "time out" in error_msg
                or "url unchanged" in error_msg
                or "page load failed" in error_msg
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
            self.db_conn.rollback()
            return False

    def extract_hhp_carrier(self, tree, product):
        """Extract carrier with Unlocked fallback."""
        hhp_carrier = self.safe_extract_chain(tree, 'hhp_carrier')
        if hhp_carrier:
            return hhp_carrier

        retailer_sku_name = product.get('retailer_sku_name') or ''
        hhp_carrier_unlocked = self.safe_extract_chain(tree, 'hhp_carrier_unlocked')
        if (
            (hhp_carrier_unlocked or '').strip().lower() == 'yes'
            and 'unlocked' in retailer_sku_name.lower()
        ):
            print("  [INFO] hhp_carrier fallback: hhp_carrier_unlocked=Yes + 제품명에 Unlocked 포함")
            return 'Unlocked'

        return None

    def get_item_mst_sku(self, item):
        """item_mst 테이블에서 sku 조회"""
        if not item:
            return None
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(f"""
                SELECT sku
                FROM {self.item_mst_table}
                WHERE item = %s AND account_name = %s
            """, (item, self.account_name))
            row = cursor.fetchone()
            cursor.close()
            if row:
                return row[0]
            return None
        except Exception as e:
            print(f"[WARNING] get_item_mst_sku failed: {e}")
            return None

    def upsert_item_mst(self, product):
        """item_mst 테이블에 INSERT 또는 UPDATE
        - 조회 결과 없음 → INSERT (sku)
        - 조회 결과 있음 → 기존 sku가 NULL/빈값/no sku인 경우 추출 sku로 UPDATE
        """
        item = product.get('item')
        if not item:
            return

        try:
            cursor = self.db_conn.cursor()
            extracted_sku = product.get('sku')
            new_sku = extracted_sku or 'no sku'
            product_url = product.get('product_url')

            # 기존 데이터 조회
            cursor.execute(f"""
                SELECT sku FROM {self.item_mst_table}
                WHERE item = %s AND account_name = %s
            """, (item, self.account_name))

            row = cursor.fetchone()

            if row is None:
                # 조회 결과 없음 → INSERT
                cursor.execute(f"""
                    INSERT INTO {self.item_mst_table} (item, account_name, sku, product_url)
                    VALUES (%s, %s, %s, %s)
                """, (item, self.account_name, new_sku, product_url))
                self.db_conn.commit()
                insert_info = []
                if new_sku:
                    insert_info.append(f"sku={new_sku}")
                print(f"  [ITEM_MST] INSERT ({item}) - {', '.join(insert_info) if insert_info else '값 없음'}")
            else:
                # 기존 sku가 없거나 no sku인 경우, 추출 sku가 있으면 업데이트
                existing_sku = row[0]
                updates = []
                params = []
                updated_info = []

                if (not (existing_sku or '') or existing_sku == 'no sku') and extracted_sku:
                    updates.append("sku = %s")
                    params.append(extracted_sku)
                    updated_info.append(f"sku={extracted_sku}")

                if updates:
                    updates.append("product_url = %s")
                    params.append(product_url)
                    updates.append("updated_at = %s")
                    params.append(datetime.now())
                    params.extend([item, self.account_name])

                    cursor.execute(f"""
                        UPDATE {self.item_mst_table} SET {', '.join(updates)}
                        WHERE item = %s AND account_name = %s
                    """, params)
                    self.db_conn.commit()
                    print(f"  [ITEM_MST] UPDATE ({item}) - {', '.join(updated_info)}")
                else:
                    print(f"  [ITEM_MST] SKIP ({item}) - 업데이트할 필드 없음")

            cursor.close()

        except Exception as e:
            print(f"[ERROR] upsert_item_mst failed: {item}: {e}")
            self.db_conn.rollback()

BestBuyDetailCrawler = BestBuyHHPDetailCrawler


def main():
    """개별 실행 진입점"""
    import argparse
    parser = argparse.ArgumentParser(description='BestBuy HHP Detail Crawler')
    parser.add_argument('--batch-id', type=str, help='Batch ID')
    parser.add_argument('--test', action='store_true', help='테스트 모드')
    args = parser.parse_args()

    batch_id = args.batch_id
    test_mode = args.test if args.batch_id else True

    crawler = BestBuyHHPDetailCrawler(batch_id=batch_id, test_mode=test_mode)
    crawler.run()
    input("Press Enter to exit...")


if __name__ == '__main__':
    main()
