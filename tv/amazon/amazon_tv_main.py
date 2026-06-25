"""
Amazon TV Main 페이지 크롤러 (DrissionPage 기반)
"""

import sys
import os
import time
import random
import traceback
from datetime import datetime

# 공통 환경 설정 (작업 디렉토리, 한글 출력, 경로 설정)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.setup import setup_environment
setup_environment(__file__)

from common.amazon_base import AmazonBaseCrawler


class AmazonTVMainCrawler(AmazonBaseCrawler):
    def __init__(self, test_mode=True, batch_id=None):
        """초기화. test_mode: 테스트(True)/운영 모드(False), batch_id: 통합 크롤러에서 전달"""
        super().__init__()
        self.test_mode = test_mode
        self.account_name = 'Amazon'
        self.amazon_zip_code = '10001'
        self.page_type = 'main'
        self.item_mst_table = 'tv_item_mst'  # is_product_excluded() 조회 테이블
        self.batch_id = batch_id
        self.calendar_week = None
        self.url_template = None

        # DrissionPage 객체 (setup_browser() 호출 후 채워짐)
        self.page = None 

        self.test_count = 1  # 테스트 모드
        self.max_products = 300  # 운영 모드
        self.max_pages = 20  # 최대 페이지 수
        self.current_rank = 0
        self.saved_items = set()  # 중복 item 체크용

        # 스크린샷 캡처 설정
        self.capture_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'capture')

        # 통계 변수
        self.stats = {
            'collected': 0,         # 수집 진행한 갯수
            'duplicates': 0,        # 중복 item 제거 갯수
            'url_missing': 0,       # product_url 없음 제외 갯수
            'non_product': 0,       # is_product=FALSE 제외 갯수
            'saved': 0,             # 저장 갯수
            'skipped_by_target': 0  # target 도달 후 미검사 갯수
        }

    def run(self):
        """실행: initialize() → 페이지별 crawl_page() → save_products() → 리소스 정리"""
        try:
            if not self.initialize():
                print("[ERROR] Initialization failed")
                return False

            total_products = 0
            target_products = self.test_count if self.test_mode else self.max_products
            self.current_rank = 0
            self.saved_items = set()  # 중복 item 체크용 초기화
            page_num = 1

            while total_products < target_products and page_num <= self.max_pages:
                products = self.crawl_page(page_num)

                if not products:
                    page_url = self.url_template.replace('{page}', str(page_num))

                    for restart_count in range(1, 2):
                        print(f"[WARNING] Page {page_num}: 0 products found, restarting browser with page URL ({restart_count}/1)")
                        if not self.restart_browser(page_url):  # 재시작 실패 시 아래의 0개 실패 처리로 이동
                            break

                        products = self.crawl_page(page_num)
                        if products:
                            break

                if not products:
                    print(f"[ERROR] No products found at page {page_num}")
                    return False
                else:
                    saved_count = self.save_products(products)
                    self.stats['saved'] += saved_count
                    total_products += saved_count

                    if total_products >= target_products:
                        break

                time.sleep(random.uniform(8, 12))
                page_num += 1

            print(f"[DONE] Page: {page_num}, Saved: {total_products}, batch_id: {self.batch_id}")
            return True

        except Exception as e:
            print(f"[ERROR] Crawler failed: {e}")
            traceback.print_exc()
            return False

        finally:
            # 통계 출력
            print(f"\n{'='*60}")
            print(f"[통계]")
            print(f"  수집: {self.stats['collected']}")
            print(f"  중복제거: {self.stats['duplicates']}")
            print(f"  URL없음: {self.stats['url_missing']}")
            print(f"  비제품: {self.stats['non_product']}")
            print(f"  저장: {self.stats['saved']}")
            if self.stats['skipped_by_target'] > 0:
                print(f"  미검사: {self.stats['skipped_by_target']} (target 도달 후 나머지)")
            print(f"{'='*60}")

            # 브라우저 리소스 정리
            if self.page:
                try:
                    self.page.quit()
                except:
                    pass
            if self.db_conn:
                self.db_conn.close()

    def initialize(self):
        """초기화: batch_id 설정 → DB 연결 → XPath 로드 → URL 템플릿 로드 → 브라우저 설정 → 로그 정리"""
        # 1. batch_id 설정
        if not self.batch_id:
            self.batch_id = self.generate_batch_id(self.account_name, test_mode=True)

        # 2. DB 연결
        if not self.connect_db():
            print("[ERROR] Initialize failed: DB connection failed")
            return False

        # 3. XPath 로드
        if not self.load_xpaths(self.account_name, self.page_type, 'SEA', 'TV'):
            print(f"[ERROR] Initialize failed: XPath load failed (account={self.account_name}, page_type={self.page_type})")
            return False

        # 4. URL 템플릿 로드
        self.url_template = self.load_page_urls(self.account_name, self.page_type, 'SEA', 'TV')
        if not self.url_template:
            print(f"[ERROR] Initialize failed: URL template load failed (account={self.account_name}, page_type={self.page_type})")
            return False

        # 5. 브라우저 설정 (DrissionPage)
        if not self.setup_browser():
            print("[ERROR] Initialize failed: Browser setup failed")
            return False

        # 6. calendar_week 생성 및 로그 정리
        self.calendar_week = self.generate_calendar_week()
        self.cleanup_old_logs()

        print(f"[INFO] Initialize completed: batch_id={self.batch_id}, calendar_week={self.calendar_week}")
        return True

    def crawl_page(self, page_number):
        """페이지 크롤링: 페이지 로드 → CAPTCHA 처리 → 스크롤 → HTML 파싱(16개 검증) → 제품 데이터 추출"""
        try:
            base_container_xpath = self.xpaths.get('base_container', {}).get('xpath')
            if not base_container_xpath:
                print("[ERROR] base_container XPath not found")
                raise ValueError("base_container XPath not found")

            url = self.url_template.replace('{page}', str(page_number))
            self.page.get(url)
            time.sleep(random.uniform(8, 12))

            if not self.recover_amazon_pages():
                print(f"[SKIP] Skipping page {page_number} due to Amazon recovery failure")
                raise RuntimeError(f"Amazon recovery failed at page {page_number}")


            # 16개 검증 (최대 3회 재시도: 파싱 → 부족하면 스크롤 → 재파싱)
            expected_products = 16
            base_containers = self.find_product_containers(base_container_xpath, page_number, expected_products)
            print(f"[INFO] Page {page_number}: {len(base_containers)} products found")

            # 1페이지 캡처
            if page_number == 1:
                print(f"[INFO] Page 1: 캡처 모드 실행")
                self.capture_page_with_scroll()

            products = []
            for idx, item in enumerate(base_containers, 1):
                try:
                    product_data = {
                        'account_name': self.account_name,
                        'page_type': self.page_type,
                        'retailer_sku_name': self.safe_extract_chain(item, 'retailer_sku_name'),
                        'number_of_units_purchased_past_month': self.convert_first_number(item, 'number_of_units_purchased_past_month'),
                        'delivery_availability': self.extract_delivery_field(item, 'delivery_availability', separator=' '),
                        'fastest_delivery': self.extract_delivery_field(item, 'fastest_delivery'),
                        'available_quantity_for_purchase': self.convert_first_number(item, 'available_quantity_for_purchase'),
                        'discount_type': self.safe_extract_chain(item, 'discount_type'),
                        'main_rank': 0,  # save_products()에서 재할당
                        'page_number': page_number,
                        'product_url': self.extract_product_url(item, 'https://www.amazon.com'),
                        'calendar_week': self.calendar_week,
                        'crawl_datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'batch_id': self.batch_id
                    }

                    products.append(product_data)

                except Exception as e:
                    print(f"[ERROR] Product {idx} extract failed: {e}")
                    traceback.print_exc()
                    raise

            print(f"[INFO] Page {page_number}: {len(products)} products")
            return products

        except Exception as e:
            print(f"[ERROR] Page {page_number} failed: {e}")
            traceback.print_exc()
            raise

    def save_products(self, products):
        """DB 저장: 페이지에서 추출한 제품을 한 번에 INSERT."""
        if not products:
            print("[WARNING] save_products가 빈 products로 호출됨")
            return 0

        # 페이지 단위 통계 스냅샷 (delta 계산용)
        page_attempted = len(products)
        page_dup_start = self.stats['duplicates']
        page_url_missing_start = self.stats['url_missing']
        page_nonprod_start = self.stats['non_product']
        page_number = products[0].get('page_number', '?')

        # 수집 갯수 통계
        self.stats['collected'] += len(products)

        # 중복 제거 및 rank 할당
        unique_products = []
        extracted_logs = []
        for idx, product in enumerate(products):

            retailer_sku_name = product.get('retailer_sku_name') or ''

            # product_url 누락 상품 제외
            product_url = product.get('product_url')
            if not product_url:
                self.stats['url_missing'] += 1
                continue

            # URL에서 item 추출
            item = self.extract_item(product_url)

            # 이미 수집한 item → 스킵 (페이지 간 중복, ASIN 기준)
            if item and item in self.saved_items:
                print(f"[SKIP] 중복 item: item={item}, name={retailer_sku_name if retailer_sku_name else 'N/A'}")
                self.stats['duplicates'] += 1
                continue

            if item:
                self.saved_items.add(item)

            # is_product=FALSE 체크 (비제품 제외)
            if self.is_product_excluded(item):
                print(f"[SKIP] 비제품(is_product=FALSE): item={item}, name={retailer_sku_name if retailer_sku_name else 'N/A'}")
                self.stats['non_product'] += 1
                continue

            # rank 할당 (중복/비제품 제외 후 실제 저장될 제품에만 순차적으로)
            target = self.test_count if self.test_mode else self.max_products
            self.current_rank += 1
            if self.current_rank > target:
                self.stats['skipped_by_target'] += len(products) - idx
                break
            product['main_rank'] = self.current_rank
            unique_products.append(product)

            # 추출 결과 로그 버퍼링 (SKIP 로그 다음에 일괄 출력)
            extracted_logs.append(
                f"[{self.current_rank}] item = {item}\n"
                f"  ├─ retailer_sku_name: {product['retailer_sku_name'] or '-'}\n"
                f"  ├─ number_of_units_purchased_past_month: {product['number_of_units_purchased_past_month'] or '-'}\n"
                f"  ├─ delivery_availability: {product['delivery_availability'] or '-'}\n"
                f"  ├─ fastest_delivery: {product['fastest_delivery'] or '-'}\n"
                f"  ├─ available_quantity_for_purchase: {product['available_quantity_for_purchase'] or '-'}\n"
                f"  └─ discount_type: {product['discount_type'] or '-'}"
            )

        # 추출 결과 로그 일괄 출력
        for log in extracted_logs:
            print(log)

        # 페이지 단위 통계 출력 (위아래 빈 줄로 시각적 분리)
        page_dup = self.stats['duplicates'] - page_dup_start
        page_url_missing = self.stats['url_missing'] - page_url_missing_start
        page_nonprod = self.stats['non_product'] - page_nonprod_start
        print()
        print(f"[Page {page_number} 통계] 시도: {page_attempted} | 중복: {page_dup} | URL없음: {page_url_missing} | 비제품: {page_nonprod} | 저장: {len(unique_products)}")
        print()

        if not unique_products:
            return 0

        insert_query = """
            INSERT INTO amazon_tv_product_list (
                account_name, page_type, retailer_sku_name,
                number_of_units_purchased_past_month,
                delivery_availability, fastest_delivery,
                available_quantity_for_purchase, discount_type,
                main_rank, main_page_number, product_url,
                calendar_week, crawl_datetime, batch_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """

        def product_to_tuple(product):
            return (
                product['account_name'],
                product['page_type'],
                product['retailer_sku_name'],
                product['number_of_units_purchased_past_month'],
                product['delivery_availability'],
                product['fastest_delivery'],
                product['available_quantity_for_purchase'],
                product['discount_type'],
                product['main_rank'],
                product['page_number'],
                product['product_url'],
                product['calendar_week'],
                product['crawl_datetime'],
                product['batch_id']
            )

        values_list = [product_to_tuple(product) for product in unique_products]

        cursor = None
        try:
            cursor = self.db_conn.cursor()
            cursor.executemany(insert_query, values_list)
            self.db_conn.commit()
        except Exception as e:
            self.db_conn.rollback()
            print(f"[ERROR] Failed to save products: {e}")
            traceback.print_exc()
            raise
        finally:
            if cursor:
                cursor.close()

        return len(unique_products)

def main():
    """개별 실행 진입점 (테스트 모드)"""
    crawler = AmazonTVMainCrawler(test_mode=True)
    crawler.run()
    input("\n엔터키를 누르면 종료합니다...")


if __name__ == '__main__':
    main()
