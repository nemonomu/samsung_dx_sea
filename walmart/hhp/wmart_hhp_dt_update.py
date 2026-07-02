"""
Walmart Detail 페이지 크롤러 (UPDATE 전용)

================================================================================
실행 모드
================================================================================
- 개별 실행: python wmart_hhp_dt_update.py --batch-id <batch_id> [--mode 1|2] [--start-id N]
- batch_id 필수 (인자 또는 stdin)
- mode: 1=item IS NULL (기본), 2=count_of_reviews IS NULL

================================================================================
주요 기능
================================================================================
- hhp_retail_com 테이블에서 item이 NULL인 제품만 조회 (추출 실패 재시도)
- 각 제품 상세 페이지에서 리뷰, 별점, 스펙 등 추출
- 기존 row를 id 기준으로 UPDATE

================================================================================
저장 테이블
================================================================================
- hhp_retail_com (UPDATE)
"""

import sys
import os
import time
import random
import traceback
import re
import subprocess
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
from common.data_extractor import extract_numeric_value
from walmart.hhp.wmart_hhp_dt import WalmartDetailCrawler


class WalmartDetailUpdateCrawler(WalmartDetailCrawler):
    """
    Walmart Detail 페이지 크롤러 (UPDATE 전용)
    """

    MODE_ITEM_NULL = '1'
    MODE_REVIEW_NULL = '2'
    MODE_BOTH = '3'
    UPDATE_META_FIELDS = {}

    def __init__(self, batch_id=None, start_id=None, mode=None):
        """초기화. batch_id: 필수, start_id: 특정 id 이후부터 조회, mode: 조회 조건"""
        super().__init__(batch_id=batch_id, test_mode=False)
        self.capture_enabled = False
        self.start_id = start_id
        self.mode = mode or self.MODE_ITEM_NULL

    def run(self):
        """실행: initialize() → load_product_list() → 제품별 crawl_detail() → update_retail_com() → 리소스 정리"""
        try:
            if not self.initialize():
                print("[ERROR] Initialization failed")
                return False

            product_list = self.load_product_list()
            if not product_list:
                print("[INFO] No products to update (item IS NULL)")
                return True

            total_updated = 0
            # RESTART_INTERVAL = 100  # 브라우저 재시작 비활성화

            for i, product in enumerate(product_list, 1):
                try:
                    # 브라우저 재시작 비활성화 (DrissionPage는 봇 감지 우회에 강함)
                    # if i > 1 and (i - 1) % RESTART_INTERVAL == 0:
                    #     print(f"\n[INFO] 브라우저 재시작 ({i-1}개 처리 완료, 메모리 정리)")
                    #     if not self.restart_browser():
                    #         print("[WARNING] 브라우저 재시작 실패, 계속 진행")

                    sku_name = product.get('retailer_sku_name') or 'N/A'
                    product_url = product.get('product_url', 'N/A')
                    url_display = product_url[:80] + '...' if len(product_url) > 80 else product_url
                    print(f"\n[{i}/{len(product_list)}] {sku_name}")
                    print(f"  URL: {url_display}")

                    combined_data = self.crawl_detail(product)
                    if combined_data:
                        self.upsert_item_mst(combined_data)
                        if self.update_retail_com(combined_data):
                            total_updated += 1

                    time.sleep(random.uniform(2, 4))

                except Exception as e:
                    error_msg = str(e).lower()
                    print(f"[ERROR] Product {i} failed: {e}")

                    # DOM 타임아웃 → 브라우저 재시작만 하고 해당 제품은 스킵 (재시도 안 함)
                    if "dom timeout" in error_msg:
                        print(f"[INFO] DOM 타임아웃 - 브라우저 재시작 후 다음 제품으로")
                        self.restart_browser()
                        continue

                    if "redirect detected" in error_msg:
                        print("[INFO] 리다이렉트 감지 - UPDATE 없이 현재 row 스킵")
                        continue

                    # 일반 타임아웃 또는 페이지 로드 실패 → 브라우저 재시작 후 재시도
                    if "timeout" in error_msg or "time out" in error_msg or "url unchanged" in error_msg:
                        print(f"[INFO] 브라우저 재시작 후 재시도")
                        if self.restart_browser():
                            try:
                                combined_data = self.crawl_detail(product)
                                if combined_data:
                                    self.upsert_item_mst(combined_data)
                                    if self.update_retail_com(combined_data):
                                        total_updated += 1
                                print(f"[SUCCESS] 재시도 성공: {sku_name[:30]}")
                            except Exception as retry_e:
                                print(f"[ERROR] 재시도 실패: {retry_e}")
                    continue

            print(f"[DONE] Processed: {len(product_list)}, Updated: {total_updated}, Table: hhp_retail_com, batch_id: {self.batch_id}")
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
        if not self.batch_id:
            print("[ERROR] batch_id가 필요합니다.")
            return False

        if not self.connect_db():
            return False
        if not self.load_xpaths(self.account_name, self.page_type, 'SEA', 'HHP'):
            return False

        # DrissionPage 설정 (Selenium 대신)
        try:
            self.setup_browser()
        except Exception as e:
            print(f"[ERROR] Initialize failed: DrissionPage setup failed - {e}")
            traceback.print_exc()
            return False

        # 세션 초기화 (example.com → walmart.com → 검색)
        self.initialize_session()

        self.cleanup_old_logs()

        print(f"[INFO] batch_id: {self.batch_id}")
        return True

    def crawl_detail(self, product):
        """UPDATE mode: skip fallback product from failed detail extraction."""
        result = super().crawl_detail(product)
        if result is product:
            print("[INFO] Detail extraction fallback - skip row without UPDATE")
            return None
        return result

    def load_product_list(self):
        """hhp_retail_com 테이블에서 조건에 맞는 제품 조회 (UPDATE 대상)"""
        try:
            cursor = self.db_conn.cursor()

            # 모드별 조건 설정
            if self.mode == self.MODE_BOTH:
                condition = "AND (item IS NULL OR count_of_reviews IS NULL)"
                mode_desc = "item IS NULL OR count_of_reviews IS NULL"
            elif self.mode == self.MODE_REVIEW_NULL:
                condition = "AND count_of_reviews IS NULL"
                mode_desc = "count_of_reviews IS NULL"
            else:
                condition = "AND item IS NULL"
                mode_desc = "item IS NULL"

            query = f"""
                SELECT
                    id,
                    retailer_sku_name, final_sku_price, original_sku_price,
                    offer, pick_up_availability, fastest_delivery,
                    delivery_availability, sku_status,
                    available_quantity_for_purchase, inventory_status,
                    main_rank, bsr_rank, product_url, calendar_week,
                    crawl_strdatetime, page_type
                FROM hhp_retail_com
                WHERE account_name = %s AND batch_id = %s AND product_url IS NOT NULL {condition}
                ORDER BY id
            """

            params = [self.account_name, self.batch_id]
            if self.start_id:
                query = query.replace("ORDER BY id", f"AND id >= {self.start_id} ORDER BY id")

            cursor.execute(query, params)
            rows = cursor.fetchall()
            cursor.close()

            product_list = []
            for row in rows:
                product = {
                    'id': row[0],  # UPDATE용 id
                    'account_name': self.account_name,
                    'retailer_sku_name': row[1],
                    'final_sku_price': row[2],
                    'original_sku_price': row[3],
                    'offer': row[4],
                    'pick_up_availability': row[5],
                    'fastest_delivery': row[6],
                    'delivery_availability': row[7],
                    'sku_status': row[8],
                    'available_quantity_for_purchase': row[9],
                    'inventory_status': row[10],
                    'main_rank': row[11],
                    'bsr_rank': row[12],
                    'product_url': row[13],
                    'calendar_week': row[14],
                    'crawl_strdatetime': row[15],
                    'page_type': row[16],
                }
                product_list.append(product)

            print(f"[INFO] Loaded {len(product_list)} products ({mode_desc})")
            return product_list

        except Exception as e:
            print(f"[ERROR] Failed to load product list: {e}")
            traceback.print_exc()
            return []

    def upsert_item_mst(self, product):
        """hhp_item_mst 테이블에 INSERT 또는 UPDATE
        - 조회 결과 없음 → INSERT (sku, hhp_carrier, hhp_color, hhp_storage)
        - 조회 결과 있음 → 기존 값이 NULL/빈값인 필드만 UPDATE
        """
        item = product.get('item')
        if not item:
            return

        try:
            cursor = self.db_conn.cursor()
            new_sku = product.get('sku') or ''
            product_url = product.get('product_url')
            new_carrier = product.get('hhp_carrier') or None
            new_color = product.get('hhp_color') or None
            new_storage = product.get('hhp_storage') or None

            # 기존 데이터 조회
            cursor.execute("""
                SELECT sku, hhp_carrier, hhp_color, hhp_storage FROM hhp_item_mst
                WHERE item = %s AND account_name = %s
            """, (item, self.account_name))

            row = cursor.fetchone()

            if row is None:
                # 조회 결과 없음 → INSERT
                cursor.execute("""
                    INSERT INTO hhp_item_mst (item, account_name, sku, product_url, hhp_carrier, hhp_color, hhp_storage)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (item, self.account_name, new_sku, product_url, new_carrier, new_color, new_storage))
                self.db_conn.commit()
                print(f"  → DB: ITEM_MST INSERT")
            else:
                # 기존 값이 없는 필드만 업데이트
                existing_sku, existing_carrier, existing_color, existing_storage = row
                updates = []
                params = []

                if not (existing_sku or '') and new_sku:
                    updates.append("sku = %s")
                    params.append(new_sku)
                if not (existing_carrier or '') and new_carrier:
                    updates.append("hhp_carrier = %s")
                    params.append(new_carrier)
                if not (existing_color or '') and new_color:
                    updates.append("hhp_color = %s")
                    params.append(new_color)
                if not (existing_storage or '') and new_storage:
                    updates.append("hhp_storage = %s")
                    params.append(new_storage)

                if updates:
                    # 업데이트할 필드와 값 저장 (로그용)
                    updated_info = []
                    if not (existing_sku or '') and new_sku:
                        updated_info.append(f"sku={new_sku}")
                    if not (existing_carrier or '') and new_carrier:
                        updated_info.append(f"carrier={new_carrier}")
                    if not (existing_color or '') and new_color:
                        updated_info.append(f"color={new_color}")
                    if not (existing_storage or '') and new_storage:
                        updated_info.append(f"storage={new_storage}")

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
            self.db_conn.rollback()

    def get_hhp_specs_from_mst(self, item):
        """마스터 테이블에서 HHP 스펙 및 SKU 조회"""
        if not item:
            return None, None, None, None

        try:
            cursor = self.db_conn.cursor()
            cursor.execute("""
                SELECT hhp_carrier, hhp_color, hhp_storage, sku FROM hhp_item_mst
                WHERE item = %s AND account_name = %s AND is_product = TRUE
            """, (item, self.account_name))
            row = cursor.fetchone()
            cursor.close()

            if row:
                return row[0], row[1], row[2], row[3]
            return None, None, None, None
        except Exception:
            return None, None, None, None

    def update_retail_com(self, product):
        """DB 저장: id 기준으로 UPDATE (기존 값이 있는 컬럼은 업데이트하지 않음)"""
        if not product:
            return False

        row_id = product.get('id')
        if not row_id:
            print(f"[ERROR] DB update failed: id가 없음")
            return False

        try:
            cursor = self.db_conn.cursor()

            # 동적 UPDATE: 기존 값이 없는 컬럼만 업데이트
            updates = []
            params = []

            # item은 항상 업데이트 (이 크롤러의 목적)
            if product.get('item'):
                updates.append("item = %s")
                params.append(product.get('item'))

            # 크롤링으로 새로 추출한 필드들 (기존 DB에 없던 값들이므로 항상 업데이트)
            update_fields = [
                ('final_sku_price', 'final_sku_price'),
                ('original_sku_price', 'original_sku_price'),
                ('count_of_reviews', 'count_of_reviews'),
                ('star_rating', 'star_rating'),
                ('count_of_star_ratings', 'count_of_star_ratings'),
                ('number_of_ppl_purchased_yesterday', 'number_of_ppl_purchased_yesterday'),
                ('number_of_ppl_added_to_carts', 'number_of_ppl_added_to_carts'),
                ('sku_popularity', 'sku_popularity'),
                ('savings', 'savings'),
                ('discount_type', 'discount_type'),
                ('hhp_storage', 'hhp_storage'),
                ('hhp_color', 'hhp_color'),
                ('hhp_carrier', 'hhp_carrier'),
                ('retailer_sku_name_similar', 'retailer_sku_name_similar'),
                ('detailed_review_content', 'detailed_review_content'),
            ]

            for db_col, key in update_fields:
                value = product.get(key)
                if value is not None:
                    updates.append(f"{db_col} = %s")
                    params.append(value)

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            update_meta = {
                field: now if source == 'CURRENT_TIMESTAMP' else source
                for field, source in self.UPDATE_META_FIELDS.items()
            }
            for db_col, value in update_meta.items():
                updates.append(f"{db_col} = %s")
                params.append(value)

            if not updates:
                print(f"[WARNING] 업데이트할 컬럼 없음: id={row_id}")
                cursor.close()
                return False

            params.append(row_id)
            update_query = f"UPDATE hhp_retail_com SET {', '.join(updates)} WHERE id = %s"

            cursor.execute(update_query, params)
            self.db_conn.commit()
            cursor.close()
            return True

        except Exception as e:
            print(f"[ERROR] DB update failed: id={row_id}, {e}")
            traceback.print_exc()
            self.db_conn.rollback()
            return False

    def restart_browser(self):
        """브라우저 재시작 (메모리 정리 + 좀비 프로세스 강제 종료)"""
        try:
            if self.page:
                try:
                    self.page.quit()
                except Exception:
                    pass
            # 좀비 크롬 프로세스 강제 종료
            subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'],
                           capture_output=True)
            time.sleep(2)
            self.setup_browser()
            print("[SUCCESS] Browser restarted")
            return True
        except Exception as e:
            print(f"[ERROR] Browser restart failed: {e}")
            return False

    def handle_sorry_page(self, max_button_attempts=3, max_refresh_attempts=5):
        """
        Sorry 페이지 감지 및 Try Again 버튼 클릭 처리

        Args:
            max_button_attempts: Try Again 버튼 클릭 최대 시도 횟수
            max_refresh_attempts: 버튼 실패 후 새로고침 최대 시도 횟수

        Returns:
            bool: 페이지가 정상으로 복구되면 True, 실패하면 False
        """
        try:
            # Walmart Sorry 페이지 실제 문구 (정확한 매칭)
            sorry_keywords = [
                "we're having technical issues",
                "we'll be back in a flash",
                "this page isn't available right now",
                "this page isn't available"
            ]

            # Sorry 페이지 체크 (정상 페이지면 바로 리턴)
            page_content = self.page.html.lower()
            if not any(keyword in page_content for keyword in sorry_keywords):
                return True

            # Sorry 페이지 감지됨 - 복구 시도
            # 1단계: Try Again 버튼 클릭 시도 (최대 max_button_attempts회)
            for attempt in range(max_button_attempts):
                print(f"[WARNING] Sorry 페이지 감지! (버튼 시도 {attempt + 1}/{max_button_attempts})")

                # Try Again 버튼 찾기 및 클릭 시도
                try_again_clicked = False
                try_again_selectors = [
                    "xpath://button[contains(text(), 'Try again')]",
                    "xpath://button[contains(text(), 'try again')]",
                    "xpath://button[contains(text(), 'Try Again')]",
                    "xpath://a[contains(text(), 'Try again')]",
                    "xpath://a[contains(text(), 'try again')]",
                    "xpath://button[contains(@class, 'retry')]",
                    "xpath://button[contains(@class, 'try-again')]",
                    "xpath://*[contains(text(), 'Try again') and (self::button or self::a)]",
                ]

                for selector in try_again_selectors:
                    try:
                        try_again_button = self.page.ele(selector, timeout=2)
                        if try_again_button:
                            print(f"[INFO] Try Again 버튼 발견")
                            try_again_button.click()
                            try_again_clicked = True
                            print("[OK] Try Again 버튼 클릭 완료")
                            time.sleep(random.uniform(3, 5))
                            break
                    except:
                        continue

                # 버튼 클릭 후 해결됐는지 체크
                if try_again_clicked:
                    page_content = self.page.html.lower()
                    if not any(keyword in page_content for keyword in sorry_keywords):
                        print("[OK] Sorry 페이지 해결됨 (버튼 클릭)")
                        return True
                else:
                    # 버튼을 못 찾았으면 새로고침 1회 시도
                    print("[INFO] Try Again 버튼을 찾지 못함, 새로고침 시도...")
                    self.page.refresh()
                    time.sleep(random.uniform(5, 8))

                    page_content = self.page.html.lower()
                    if not any(keyword in page_content for keyword in sorry_keywords):
                        print("[OK] Sorry 페이지 해결됨 (새로고침)")
                        return True

            # 2단계: 버튼 클릭으로 해결 안 되면 새로고침 추가 시도 (최대 max_refresh_attempts회)
            page_content = self.page.html.lower()
            if any(keyword in page_content for keyword in sorry_keywords):
                print(f"[WARNING] 버튼 클릭 실패, 새로고침 시도 시작 (최대 {max_refresh_attempts}회)...")

                for refresh_attempt in range(max_refresh_attempts):
                    print(f"[INFO] 새로고침 시도 {refresh_attempt + 1}/{max_refresh_attempts}...")
                    self.page.refresh()
                    time.sleep(random.uniform(5, 8))

                    page_content = self.page.html.lower()
                    if not any(keyword in page_content for keyword in sorry_keywords):
                        print(f"[OK] Sorry 페이지 해결됨 (새로고침 {refresh_attempt + 1}회)")
                        return True

            # 최종 확인
            page_content = self.page.html.lower()
            if any(keyword in page_content for keyword in sorry_keywords):
                print(f"[ERROR] Sorry 페이지 해결 실패 (버튼 {max_button_attempts}회 + 새로고침 {max_refresh_attempts}회 시도 후)")

                # 최종 실패 시에만 스크린샷 저장
                try:
                    capture_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'capture')
                    os.makedirs(capture_dir, exist_ok=True)
                    screenshot_path = os.path.join(capture_dir, f"sorry_page_failed_{int(time.time())}.png")
                    self.page.get_screenshot(path=screenshot_path)
                    print(f"[INFO] 스크린샷 저장됨: {screenshot_path}")
                except:
                    pass

                return False

            return True

        except Exception as e:
            print(f"[WARNING] Sorry page handling error: {e}")
            traceback.print_exc()
            return True  # 에러 발생해도 계속 진행

    def extract_rating_from_header(self, tree):
        """상단 reviews-and-ratings 영역에서 별점과 별점 수 추출
        예: '4.3 stars out of 8968 reviews' → ('4.3', '8968')
        """
        try:
            xpath = self.xpaths.get('header_rating', {}).get('xpath')
            if not xpath:
                return None, None
            results = tree.xpath(xpath)

            if results:
                text = results[0].strip()
                # 정규식: "4.3 stars out of 8968 reviews"
                match = re.match(r'([\d.]+)\s*stars?\s*out\s*of\s*([\d,]+)\s*reviews?', text, re.IGNORECASE)
                if match:
                    star_rating = match.group(1)  # "4.3"
                    try:
                        count_of_star_ratings = '{:,}'.format(int(match.group(2).replace(',', '')))  # "8,968"
                    except ValueError:
                        count_of_star_ratings = match.group(2)  # 원본 값 유지
                    return star_rating, count_of_star_ratings

            return None, None
        except Exception as e:
            print(f"[WARNING] extract_rating_from_header failed: {e}")
            traceback.print_exc()
            return None, None

    def extract_ratings_count(self, tree):
        """Walmart 별점 개수 추출 (예: '1,234 ratings' → '1,234', '12.5K ratings' → '12.5K')"""
        text = self.safe_extract(tree, 'count_of_star_ratings')
        if text:
            # 12.5K, 3.5K 등 K 포함 숫자 또는 1,234 등 쉼표 포함 숫자 추출
            match = re.search(r'([\d,]+\.?\d*K?)', text, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def extract_review_count(self, tree, use_review_page_xpath=False):
        """Walmart 리뷰 개수 추출 (예: '3,572 reviews' → '3,572', '3.5K reviews' → '3.5K')

        Args:
            tree: lxml HTML tree
            use_review_page_xpath: True면 리뷰 페이지 전용 XPath 사용
        """
        if use_review_page_xpath:
            # 리뷰 페이지 전용 XPath 사용
            text = self.safe_extract(tree, 'count_of_reviews_review_page')
            if text:
                # "Showing 1-10 of 17,541 reviews" 패턴에서 "of" 뒤의 숫자 추출
                match = re.search(r'of\s+([\d,]+)\s+reviews?', text, re.IGNORECASE)
                if match:
                    return match.group(1)
        else:
            text = self.safe_extract(tree, 'count_of_reviews')
            if text:
                # 3.5K, 12.5K 등 K 포함 숫자 또는 3,572 등 쉼표 포함 숫자 추출
                match = re.search(r'([\d,]+\.?\d*K?)', text, re.IGNORECASE)
                if match:
                    return match.group(1)
        return None

    def extract_star_rating(self, tree):
        """Walmart 별점 추출 (예: '4.5 out of 5 stars' → '4.5')"""
        text = self.safe_extract(tree, 'star_rating')
        return extract_numeric_value(text, include_comma=False, include_decimal=True)

    def extract_item(self, product_url):
        """URL에서 item ID 추출"""
        if not product_url:
            return None
        try:
            # /ip/product-name/12345 패턴
            ip_match = re.search(r'/ip/[^/]+/(\d+)', product_url)
            if ip_match:
                return ip_match.group(1)
            # URL 인코딩된 패턴 %2F12345%3F
            encoded_match = re.search(r'%2F(\d+)%3F', product_url)
            if encoded_match:
                return encoded_match.group(1)
            # URL 마지막 세그먼트에서 숫자 추출 (쿼리 파라미터 제거 후)
            url_without_params = product_url.split('?')[0]
            last_segment = url_without_params.rstrip('/').split('/')[-1]
            number_match = re.search(r'(\d+)$', last_segment)
            if number_match:
                return number_match.group(1)
        except Exception as e:
            print(f"[WARNING] Failed to extract item: {e}")
            traceback.print_exc()
        return None

def fetch_today_batch_ids():
    """오늘 날짜의 Walmart batch_id 목록을 DB에서 조회"""
    import psycopg2
    from config import DB_CONFIG
    today_str = datetime.now().strftime('%Y%m%d')
    try:
        conn = psycopg2.connect(**DB_CONFIG, database='postgres')
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT batch_id FROM hhp_retail_com "
            "WHERE account_name = 'Walmart' AND batch_id LIKE %s "
            "ORDER BY batch_id DESC",
            (f'w_{today_str}%',)
        )
        batch_ids = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return batch_ids
    except Exception as e:
        print(f"[WARN] batch_id 조회 실패: {e}")
        return []


def main():
    """개별 실행 진입점"""
    import argparse
    parser = argparse.ArgumentParser(description='Walmart Detail Update Crawler')
    parser.add_argument('--batch-id', type=str, help='Batch ID to process')
    parser.add_argument('--start-id', type=int, help='Start from this id (WHERE id >= start_id)')
    parser.add_argument('--mode', type=str, choices=['1', '2', '3'], help='1: item IS NULL, 2: count_of_reviews IS NULL, 3: both')
    args = parser.parse_args()

    batch_id = args.batch_id
    if not batch_id:
        today_batch_ids = fetch_today_batch_ids()
        if today_batch_ids:
            print(f"\n오늘({datetime.now().strftime('%Y-%m-%d')}) Walmart batch_id 목록:")
            for i, bid in enumerate(today_batch_ids, 1):
                print(f"  {i}. {bid}")
            print(f"  0. 직접 입력")
            choice = input("\n번호 선택 ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(today_batch_ids):
                batch_id = today_batch_ids[int(choice) - 1]
            elif choice == '0' or not choice.isdigit():
                if choice == '0':
                    batch_id = input("batch_id 입력: ").strip()
                else:
                    batch_id = choice
        else:
            print(f"오늘({datetime.now().strftime('%Y-%m-%d')}) Walmart batch_id가 없습니다.")
            batch_id = input("batch_id 직접 입력: ").strip()
        if not batch_id:
            print("[ERROR] batch_id가 필요합니다.")
            return

    start_id = args.start_id
    if not start_id:
        start_id_input = input("시작 id (엔터: 처음부터): ").strip()
        start_id = int(start_id_input) if start_id_input else None

    mode = args.mode
    if not mode:
        print("조회 모드 선택:")
        print("  1: item IS NULL (페이지 에러 재수집)")
        print("  2: count_of_reviews IS NULL (리뷰 미수집 재수집)")
        print("  3: 둘 다 (item IS NULL OR count_of_reviews IS NULL)")
        mode_input = input("모드 입력 (기본: 1): ").strip()
        mode = mode_input if mode_input in ('1', '2', '3') else '1'

    crawler = WalmartDetailUpdateCrawler(batch_id=batch_id, start_id=start_id, mode=mode)
    crawler.run()
    input("Press Enter to exit...")


WalmartDetailUpdateCrawler.extract_rating_from_header = WalmartBaseCrawler.extract_rating_from_header
WalmartDetailUpdateCrawler.extract_ratings_count = WalmartBaseCrawler.extract_ratings_count
WalmartDetailUpdateCrawler.extract_review_count = WalmartBaseCrawler.extract_review_count
WalmartDetailUpdateCrawler.extract_star_rating = WalmartBaseCrawler.extract_star_rating


if __name__ == '__main__':
    main()
