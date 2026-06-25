"""
Amazon Detail 페이지 크롤러

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
import traceback
import random
from datetime import datetime
from lxml import html

# 공통 환경 설정 (작업 디렉토리, 한글 출력, 경로 설정)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.setup import setup_environment
setup_environment(__file__)

from common.amazon_base import AmazonBaseCrawler

class AmazonDetailCrawler(AmazonBaseCrawler):
    """
    Amazon Detail 페이지 크롤러
    """

    # hhp_retail_com 컬럼 매핑 (TV Detail과 동일한 명명 체계)
    # crawl_detail이 combined_data에 채우는 추출 필드들 — INSERT/UPDATE 모두 사용.
    EXTRACTED_FIELDS = [
        'item',
        'count_of_reviews',
        'star_rating',
        'count_of_star_ratings',
        'sku_popularity',
        'bundle',
        'trade_in',
        'hhp_carrier',
        'hhp_storage',
        'hhp_color',
        'hhp_memory_ram',
        'final_sku_price',
        'original_sku_price',
        'summarized_review_content',
        'detailed_review_content',
    ]

    # product_list에서 전달받는 메타 필드 (INSERT만 사용 — UPDATE는 기존 row 유지)
    PASSTHROUGH_FIELDS = [
        'page_type',
        'retailer_sku_name',
        'product_url',
        'delivery_availability',
        'fastest_delivery',
        'available_quantity_for_purchase',
        'discount_type',
        'main_rank',
        'bsr_rank',
        'number_of_units_purchased_past_month',
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

    def __init__(self, batch_id=None, test_mode=False):
        """초기화. batch_id: 통합 크롤러에서 전달, test_mode: 테스트 모드 여부"""
        super().__init__()
        self.batch_id = batch_id
        self.account_name = 'Amazon'
        self.amazon_zip_code = '10001'
        self.page_type = 'detail'
        self.product_type = 'HHP'
        self.test_mode = test_mode
        self.page = None  # DrissionPage 브라우저 인스턴스

        # 스크린샷 캡처 설정
        self.capture_enabled = True  # False로 변경하면 캡처 비활성화
        self.capture_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'capture')
        # page_type별 캡처 제한 (main 10개, bsr 10개)
        self.capture_main_count = 0
        self.capture_bsr_count = 0
        self.capture_limit_per_type = 10
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
                    sku_name = product.get('retailer_sku_name') or 'N/A'
                    product_url = product.get('product_url')
                    # item은 product_list에 없으므로 URL에서 추출
                    item = self.extract_item(product.get('product_url')) or 'N/A'

                    # 상품 시작 헤더
                    print(f"\n{'='*70}")
                    print(f"[{i}/{len(product_list)}] {item}")
                    print(f"{'='*70}")
                    print(f"  상품명: {sku_name}")

                    combined_data = self.crawl_detail(product)
                    if combined_data:
                        self.upsert_item_mst(combined_data)
                        save_success = self.save_to_retail_com(combined_data)
                        if save_success:
                            total_saved += 1

                        print(f"  [저장] {'성공' if save_success else '실패'}")
                    else:
                        print(f"  [저장] 스킵 (데이터 없음)")

                    time.sleep(random.uniform(3, 5))

                except Exception as e:
                    error_msg = str(e).lower()
                    print(f"[ERROR] Product {i} failed: {e}")

                    if "dom timeout" in error_msg:
                        next_product = product_list[i] if i < len(product_list) else None
                        next_url = next_product.get('product_url') if next_product else None
                        print(f"[INFO] DOM 타임아웃 - 다음 상품 URL로 브라우저 재시작 후 현재 상품 스킵")
                        if self.save_to_retail_com(product):
                            total_saved += 1
                        self.restart_browser(next_url)
                        continue

                    if "redirect detected" in error_msg:
                        print("[INFO] 리다이렉트 감지 - product_list 기본 정보만 저장")
                        self.ensure_listing_item(product)
                        if self.save_to_retail_com(product):
                            total_saved += 1
                        continue

                    if "amazon recovery unresolved" in error_msg:
                        print("[INFO] Amazon 페이지 복구 실패 - product_list 기본 정보만 저장")
                        if self.save_to_retail_com(product):
                            total_saved += 1
                        continue

                    retry_success = False
                    for retry_attempt in range(1, 3):
                        print(f"[INFO] 문제 발생 URL로 브라우저 재시작 후 재시도 ({retry_attempt}/2)")
                        if not self.restart_browser(product_url):
                            continue

                        try:
                            combined_data = self.crawl_detail(product)
                            if combined_data:
                                self.upsert_item_mst(combined_data)
                                if self.save_to_retail_com(combined_data):
                                    total_saved += 1
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
                    continue

            # 최종 요약
            table_name = 'test_hhp_retail_com' if self.test_mode else 'hhp_retail_com'
            print(f"\n{'='*70}")
            print(f"[완료] 크롤링 종료")
            print(f"{'='*70}")
            print(f"  총 상품: {len(product_list)}건")
            print(f"  저장 성공: {total_saved}건")
            print(f"  저장 실패: {len(product_list) - total_saved}건")
            if self.capture_enabled:
                print(f"  캡처: main {self.capture_main_count}개, bsr {self.capture_bsr_count}개")
            print(f"  테이블: {table_name}")
            print(f"  배치 ID: {self.batch_id}")
            print(f"{'='*70}")

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

    def initialize(self):
        """초기화: batch_id 설정 → DB 연결 → XPath 로드 → WebDriver 설정 → 로그 정리"""
        # 1. batch_id 설정
        if not self.batch_id:
            self.batch_id = 't_a_20260525_220941'

        # 2. DB 연결
        if not self.connect_db():
            print("[ERROR] Initialize failed: DB connection failed")
            return False

        # 3. XPath 로드
        if not self.load_xpaths(self.account_name, self.page_type, 'SEA', 'HHP'):
            print(f"[ERROR] Initialize failed: XPath load failed (account={self.account_name}, page_type={self.page_type})")
            return False

        # 4. DrissionPage 브라우저 설정
        try:
            if not self.setup_browser():
                return False
        except Exception as e:
            print(f"[ERROR] Initialize failed: Amazon browser setup failed - {e}")
            traceback.print_exc()
            return False

        # 5. 로그 정리
        self.cleanup_old_logs()

        print(f"[INFO] Initialize completed: batch_id={self.batch_id}")
        return True

    def load_product_list(self):
        """product_list 조회: batch_id 기준으로 제품 URL 및 기본 정보 조회"""
        try:
            cursor = self.db_conn.cursor()

            query = """
                SELECT
                    account_name, page_type, retailer_sku_name,
                    number_of_units_purchased_past_month,
                    delivery_availability, fastest_delivery,
                    available_quantity_for_purchase, discount_type,
                    main_rank, bsr_rank, product_url, calendar_week, batch_id
                FROM amazon_hhp_product_list
                WHERE account_name = %s AND batch_id = %s AND product_url IS NOT NULL
                ORDER BY id
            """

            cursor.execute(query, (self.account_name, self.batch_id))
            rows = cursor.fetchall()
            cursor.close()

            products = []
            for row in rows:
                product = {
                    'account_name': self.account_name,
                    'page_type': row[1],
                    'retailer_sku_name': row[2],
                    'number_of_units_purchased_past_month': row[3],
                    'delivery_availability': row[4],
                    'fastest_delivery': row[5],
                    'available_quantity_for_purchase': row[6],
                    'discount_type': row[7],
                    'main_rank': row[8],
                    'bsr_rank': row[9],
                    'product_url': row[10],
                    'calendar_week': row[11],
                    'batch_id': row[12]
                }
                products.append(product)

            print(f"[INFO] Loaded {len(products)} products")
            return products

        except Exception as e:
            print(f"[ERROR] Failed to load product list: {e}")
            traceback.print_exc()
            return []

    def crawl_detail(self, product):
        """상세 페이지 크롤링: 페이지 로드 → 필드 추출 → 리뷰 추출 → product_list + detail 데이터 결합"""
        try:
            # ============================================================================================================
            # 상세페이지 진입 및 추출 준비
            # ============================================================================================================
            product_url = product.get('product_url')
            page_type = product.get('page_type')
            if not product_url:
                print(f"  [SKIP] product_url 없음 → 크롤링/저장 건너뜀")
                return product

            # 현재 URL 저장 (로드 전)
            previous_url = self.page.url if self.page else None

            self.page.get(product_url)
            time.sleep(random.uniform(1.5, 2.5))

            if not self.recover_amazon_pages():
                print(f"  [WARNING] Amazon 페이지 복구 실패 - 상품 스킵")
                raise Exception("Amazon recovery unresolved")
            current_url, is_redirect = self.validate_loaded_product_url(product_url, previous_url, product.get('retailer_sku_name'))

            page_html = self.page.html
            tree = html.fromstring(page_html)

            # 원본 URL에서 ASIN 추출 (페이지 로드 후 추출 - 에러 시 item NULL로 식별)
            item = self.extract_item(current_url if is_redirect else product_url)
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

                available_quantity_for_purchase_raw = self.safe_extract_chain(tree, 'available_quantity_for_purchase')
                available_quantity_for_purchase = self.convert_first_number(available_quantity_for_purchase_raw)
                if available_quantity_for_purchase:
                    product['available_quantity_for_purchase'] = available_quantity_for_purchase
                    detail_extracted_fields.add('available_quantity_for_purchase')

            # ========== 2단계: HHP 스펙 ==========
            hhp_carrier = self.safe_extract_chain(tree, 'hhp_carrier')
            # 상단 영역 필드: 추출되지 않은 필드만 최대 2회 스크롤하며 재시도
            top_fields, tree = self.scroll_extract(
                tree,
                {
                    'bundle': lambda t: self.safe_extract_chain_join(t, 'bundle', ' ||| '),
                    'trade_in': lambda t: self.safe_extract_chain_join(t, 'trade_in', ' '),
                },
                max_scrolls=2,
                scroll_px=(200, 300),
            )
            bundle = top_fields.get('bundle')
            trade_in = top_fields.get('trade_in')

            found_section = self.scroll_to_section(
                ['product_information_section', 'technical_details_section'],
                label='HHP 스펙 섹션',
            )
            if found_section == 'product_information_section':
                self.open_details_sections(['item_details_button', 'additional_details_button'])
            tree = html.fromstring(self.page.html)
            if capture_allowed:
                self.take_capture(item, 2)
            
            mst_sku = self.get_hhp_sku_from_mst(item)
            sku, sku_source, page_sku = self.extract_sku(tree, mst_sku, found_section, self.product_type)
            hhp_storage = self.safe_extract_chain(tree, 'hhp_storage')
            hhp_color = self.safe_extract_chain(tree, 'hhp_color')
            hhp_memory_ram = self.safe_extract_chain(tree, 'hhp_memory_ram')

            # ========== 3단계: 리뷰 관련 필드 ==========
            count_of_reviews = None
            self.move_to_review_section(has_review_link)
            if capture_allowed:
                self.take_capture(item, 3)
            tree = html.fromstring(self.page.html)

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
            if is_no_reviews:
                print(f"  [리뷰] 리뷰 없음")
            else:
                print(f"  [리뷰] 상품 상세페이지에서 추출 중...")
                detailed_review_content, extracted_count = self.extract_reviews_from_detail_page(tree, max_reviews=20)
                print(f"  [리뷰] 상품 상세페이지 추출 완료: {extracted_count}건")

            # 결합된 데이터
            detail_data = {
                'item': item,
                'sku': sku,
                'count_of_reviews': count_of_reviews,
                'star_rating': star_rating,
                'count_of_star_ratings': count_of_star_ratings,
                'sku_popularity': sku_popularity,
                'bundle': bundle,
                'trade_in': trade_in,
                'hhp_carrier': hhp_carrier,
                'hhp_storage': hhp_storage,
                'hhp_color': hhp_color,
                'hhp_memory_ram': hhp_memory_ram,
                'final_sku_price': final_sku_price,
                'original_sku_price': original_sku_price,
                'summarized_review_content': summarized_review_content,
                'detailed_review_content': detailed_review_content,
            }

            combined_data = {**product, **detail_data}

            if mst_sku and page_sku and mst_sku != page_sku:
                self.spec_diffs.append({
                    'item': item,
                    'mst_sku': mst_sku,
                    'page_sku': page_sku,
                })

            # ──── 결과 요약 (트리 구조) ────
            print(f"\n──── 결과 요약 ────")
            print(f"  ├─ item: {item or '-'}")
            print(f"  ├─ sku: {f'{sku} (출처: {sku_source})' if sku and sku_source else (sku or '-')}")
            print(f"  ├─ final_sku_price: {final_sku_price or '-'}")
            print(f"  ├─ original_sku_price: {original_sku_price or '-'}")
            print(f"  ├─ star_rating: {star_rating or '-'}")
            print(f"  ├─ count_of_star_ratings: {count_of_star_ratings or '-'}")
            print(f"  ├─ count_of_reviews: {count_of_reviews or '-'}")
            print(f"  ├─ sku_popularity: {sku_popularity or '-'}")
            if 'number_of_units_purchased_past_month' in detail_extracted_fields:
                print(f"  ├─ number_of_units_purchased_past_month: {product.get('number_of_units_purchased_past_month') or '-'}")
            if 'discount_type' in detail_extracted_fields:
                print(f"  ├─ discount_type: {product.get('discount_type') or '-'}")
            print(f"  ├─ bundle: {'있음' if bundle else '-'}")
            print(f"  ├─ trade_in: {trade_in or '-'}")
            print(f"  ├─ hhp_carrier: {hhp_carrier or '-'}")
            print(f"  ├─ hhp_color: {hhp_color or '-'}")
            print(f"  ├─ hhp_storage: {hhp_storage or '-'}")
            print(f"  ├─ hhp_memory_ram: {hhp_memory_ram or '-'}")
            if 'delivery_availability' in detail_extracted_fields:
                print(f"  ├─ delivery_availability: {product.get('delivery_availability') or '-'}")
            if 'fastest_delivery' in detail_extracted_fields:
                print(f"  ├─ fastest_delivery: {product.get('fastest_delivery') or '-'}")
            if 'available_quantity_for_purchase' in detail_extracted_fields:
                print(f"  ├─ available_quantity_for_purchase: {product.get('available_quantity_for_purchase') or '-'}")
            print(f"  ├─ summarized_review_content: {'있음' if summarized_review_content else '-'}")
            print(f"  └─ detailed_review_content: {extracted_count}개")
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

    def upsert_item_mst(self, product):
        """hhp_item_mst 테이블에 INSERT 또는 UPDATE
        - 조회 결과 없음 → INSERT (sku)
        - 조회 결과 있음 → 기존 sku가 NULL/빈값/no sku일 때만 UPDATE
        """
        item = product.get('item')
        if not item:
            return

        try:
            cursor = self.db_conn.cursor()
            product_url = product.get('product_url')
            new_sku = product.get('sku') or 'no sku'

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
                print(f"  [ITEM_MST] INSERT ({item}) - sku={new_sku or '(empty)'}")
            else:
                # 기존 sku가 없거나 no sku인 경우에만 업데이트
                existing_sku = row[0]
                existing_sku_value = str(existing_sku).strip() if existing_sku else ''
                can_update_sku = (
                    new_sku
                    and existing_sku_value.lower() in ('', 'no sku')
                    and existing_sku_value.lower() != new_sku.lower()
                )
                if can_update_sku:
                    cursor.execute("""
                        UPDATE hhp_item_mst
                        SET sku = %s, product_url = %s, updated_at = %s
                        WHERE item = %s AND account_name = %s
                    """, (new_sku, product_url, datetime.now(), item, self.account_name))
                    self.db_conn.commit()
                    print(f"  [ITEM_MST] UPDATE ({item}) - sku={new_sku}")

            cursor.close()

        except Exception as e:
            print(f"  [ERROR] ITEM_MST 저장 실패: {item}: {e}")
            self.db_conn.rollback()

    def get_hhp_sku_from_mst(self, item):
        """hhp_item_mst에서 기존 SKU를 조회한다."""
        if not item:
            return None

        try:
            cursor = self.db_conn.cursor()
            cursor.execute("""
                SELECT sku FROM hhp_item_mst
                WHERE item = %s AND account_name = %s
            """, (item, self.account_name))
            row = cursor.fetchone()
            cursor.close()
            return row[0] if row else None
        except Exception as e:
            print(f"  [WARNING] get_hhp_sku_from_mst failed: {e}")
            return None

    def save_to_retail_com(self, product):
        """DB 저장: 1개씩 저장"""
        if not product:
            return False

        self.ensure_listing_item(product)

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
                **save_meta,
                **{field: product.get(field) for field in self.EXTRACTED_FIELDS},
                **{field: product.get(field) for field in self.PASSTHROUGH_FIELDS},
            }

            columns = list(insert_data.keys())
            placeholders = ', '.join(['%s'] * len(columns))
            insert_query = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"
            values = list(insert_data.values())

            cursor.execute(insert_query, values)
            self.db_conn.commit()
            cursor.close()
            return True

        except Exception as e:
            print(f"  [ERROR] DB 저장 실패: {product.get('item')}: {e}")
            traceback.print_exc()
            self.db_conn.rollback()
            return False

def main():
    """개별 실행 진입점 (테스트 모드, 기본 배치 ID 사용)"""
    crawler = AmazonDetailCrawler(batch_id=None, test_mode=True)
    crawler.run()
    input("Press Enter to exit...")


if __name__ == '__main__':
    main()
