"""
Amazon HHP BSR 페이지 크롤러 (DrissionPage 기반)

================================================================================
실행 모드
================================================================================
- 개별 실행: test_mode=True (기본값)
- 통합 크롤러: test_mode 및 batch_id를 파라미터로 전달

================================================================================
주요 기능
================================================================================
- BSR 페이지에서 제품 리스트 수집 (bsr_rank 포함)
- 테스트 모드: test_count 설정값만큼 수집
- 운영 모드: max_products 설정값만큼 수집
- Amazon 복구 기능 포함

================================================================================
저장 테이블
================================================================================
- amazon_hhp_product_list (제품 목록)
"""

import sys
import os
import time
import random
import traceback
from datetime import datetime

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


class AmazonBSRCrawler(AmazonBaseCrawler):
    """
    Amazon HHP BSR 페이지 크롤러 (DrissionPage 기반)
    """

    def __init__(self, test_mode=True, batch_id=None):
        """초기화: test_mode: 테스트(True)/운영 모드(False), batch_id: 통합 크롤러에서 전달"""
        super().__init__()
        self.test_mode = test_mode
        self.account_name = 'Amazon'
        self.amazon_zip_code = '10001'
        self.page_type = 'bsr'
        self.item_mst_table = 'hhp_item_mst'
        self.batch_id = batch_id
        self.calendar_week = None
        self.url_template = None

        # DrissionPage 객체
        self.page = None

        self.test_count = 1  # 테스트 모드
        self.max_products = 100  # 운영 모드
        self.max_pages = 2  # 최대 페이지 수

        # 캐시 기반 중복 관리 (item 기준)
        self.db_item_map = {}  # {item: DB row id} - Main에서 저장된 row
        self.saved_items = set()  # 중복 item 체크용 (페이지 간 중복 방지)

        # 스크린샷 캡처 설정
        self.capture_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'capture')

        # 통계 변수
        self.stats = {
            'collected': 0,         # 수집 진행한 갯수
            'duplicates': 0,        # 중복 item 제거 갯수
            'url_missing': 0,       # product_url 누락 갯수
            'updated': 0,           # UPDATE 갯수
            'inserted': 0,          # INSERT 갯수
            'skipped_by_target': 0  # target 도달 후 미검사 갯수
        }

    def run(self):
        """실행: initialize() → 페이지별 crawl_page() → save_products() → 리소스 정리"""
        try:
            if not self.initialize():
                print("[ERROR] Initialization failed")
                return False

            total_insert = 0
            total_update = 0
            target_products = self.test_count if self.test_mode else self.max_products
            self.current_rank = 0
            page_num = 1

            while (total_insert + total_update) < target_products and page_num <= self.max_pages:
                products = self.crawl_page(page_num)

                if not products:
                    if page_num == 1:
                        page_url = (
                            self.url_template
                            .replace('&pg={page}', '')
                            .replace('&page={page}', '')
                            .replace('{page}', str(page_num))
                        )
                    else:
                        page_url = self.url_template.replace('{page}', str(page_num))
                    for restart_count in range(1, 2):
                        print(f"[WARNING] Page {page_num}: 0 products found, restarting browser with page URL ({restart_count}/1)")
                        if not self.restart_browser(page_url):
                            break

                        products = self.crawl_page(page_num)
                        if products:
                            break

                if not products:
                    print(f"[ERROR] No products found at page {page_num}")
                    return False
                else:
                    result = self.save_products(products)
                    total_insert += result['insert']
                    total_update += result['update']

                    if (total_insert + total_update) >= target_products:
                        break

                time.sleep(random.uniform(8, 12))
                page_num += 1

            print(f"[DONE] Page: {page_num}, Update: {total_update}, Insert: {total_insert}, batch_id: {self.batch_id}")
            return True

        except Exception as e:
            print(f"[ERROR] Crawler failed: {e}")
            traceback.print_exc()
            return False

        finally:
            # 통계 출력
            print(f"\n{'='*60}")
            print("[통계]")
            print(f"  수집: {self.stats['collected']}")
            print(f"  중복제거: {self.stats['duplicates']}")
            print(f"  URL없음: {self.stats['url_missing']}")
            print(f"  UPDATE: {self.stats['updated']}")
            print(f"  INSERT: {self.stats['inserted']}")
            if self.stats['skipped_by_target'] > 0:
                print(f"  미검사: {self.stats['skipped_by_target']} (target 도달 후 나머지)")
            print(f"{'='*60}")

            # 브라우저 리소스 정리
            if self.page:
                try:
                    self.page.quit()
                except Exception:
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
        if not self.load_xpaths(self.account_name, self.page_type, 'SEA', 'HHP'):
            print(f"[ERROR] Initialize failed: XPath load failed (account={self.account_name}, page_type={self.page_type})")
            return False

        # 4. URL 템플릿 로드
        self.url_template = self.load_page_urls(self.account_name, self.page_type, 'SEA', 'HHP')
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

        # 7. DB에서 기존 item 캐시 로드 (Main에서 저장된 URL → item 매핑)
        self.db_item_map = self.build_db_item_cache('amazon_hhp_product_list')

        print(f"[INFO] Initialize completed: batch_id={self.batch_id}, calendar_week={self.calendar_week}")
        return True

    def crawl_page(self, page_number):
        """페이지 크롤링: 페이지 로드 → Amazon 복구 → 스크롤 → HTML 파싱(50개 검증) → 제품 데이터 추출"""
        try:
            base_container_xpath = self.xpaths.get('base_container', {}).get('xpath')
            if not base_container_xpath:
                print("[ERROR] base_container XPath not found")
                raise ValueError("base_container XPath not found")

            # 첫 페이지는 page/pg 파라미터 없이 URL 생성
            if page_number == 1:
                url = (
                    self.url_template
                    .replace('&pg={page}', '')
                    .replace('&page={page}', '')
                    .replace('{page}', str(page_number))
                )
            else:
                url = self.url_template.replace('{page}', str(page_number))
            
            self.page.get(url)
            time.sleep(random.uniform(8, 12))

            if not self.recover_amazon_pages():
                print(f"[SKIP] Skipping page {page_number} due to Amazon recovery failure")
                raise RuntimeError(f"Amazon recovery failed at page {page_number}")

            # 50개 검증 (최대 3회 재시도: 파싱 → 부족하면 스크롤 → 재파싱)
            expected_products = 50
            base_containers = self.find_product_containers(base_container_xpath, page_number, expected_products)

            print(f"[INFO] Page {page_number}: {len(base_containers)} products found")

            # 1페이지 캡처
            if page_number == 1:
                print("[INFO] Page 1: 캡처 모드 실행")
                self.capture_page_with_scroll()

            products = []
            for idx, item in enumerate(base_containers, 1):
                try:
                    product_data = {
                        'account_name': self.account_name,
                        'page_type': self.page_type,
                        'retailer_sku_name': self.safe_extract_chain(item, 'retailer_sku_name'),
                        'bsr_rank': 0,
                        'page_number': page_number,
                        'product_url': self.extract_product_url(item, 'https://www.amazon.com'),
                        'calendar_week': self.calendar_week,
                        'crawl_strdatetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'batch_id': self.batch_id,
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
        """DB 저장: 캐시 기반 중복 체크 → bsr_rank 기준 → 페이지 단위 UPDATE/INSERT."""
        if not products:
            print("[WARNING] save_products가 빈 products로 호출됨")
            return {'insert': 0, 'update': 0}

        # 페이지 단위 통계 스냅샷 (delta 계산용)
        page_attempted = len(products)
        page_dup_start = self.stats['duplicates']
        page_url_missing_start = self.stats['url_missing']
        page_number = products[0].get('page_number', '?')

        # 수집 갯수 통계
        self.stats['collected'] += len(products)

        products_to_update = []
        products_to_insert = []
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

            # 이미 수집한 item → 스킵 (페이지 간 중복)
            if item and item in self.saved_items:
                print(f"[SKIP] 중복 item: item={item}, name={retailer_sku_name if retailer_sku_name else 'N/A'}")
                self.stats['duplicates'] += 1
                continue

            if item:
                self.saved_items.add(item)

            # bsr_rank 즉시 할당 (target 초과 시 중단)
            target = self.test_count if self.test_mode else self.max_products
            self.current_rank += 1
            if self.current_rank > target:
                self.stats['skipped_by_target'] += len(products) - idx
                break
            product['bsr_rank'] = self.current_rank

            # 추출 결과 로그 버퍼링 (SKIP 로그 다음에 일괄 출력)
            extracted_logs.append(
                f"[{self.current_rank}] item = {item}\n"
                f"  └─ retailer_sku_name: {retailer_sku_name or '-'}"
            )

            # DB에 있으면 UPDATE 대기열에 추가 (item 기준 매칭, id로 UPDATE)
            if item and item in self.db_item_map:
                row_id = self.db_item_map[item]
                products_to_update.append((
                    product['bsr_rank'],
                    product['page_number'],
                    row_id,
                ))
            else:
                # DB에 없으면 INSERT 대기열에 추가
                products_to_insert.append(product)

        # 추출 결과 로그 일괄 출력
        for log in extracted_logs:
            print(log)

        # 페이지 단위 통계 출력
        page_dup = self.stats['duplicates'] - page_dup_start
        page_url_missing = self.stats['url_missing'] - page_url_missing_start
        update_count = len(products_to_update)
        insert_count = len(products_to_insert)
        print()
        print(f"[Page {page_number} 통계] 시도: {page_attempted} | 중복: {page_dup} | URL없음: {page_url_missing} | UPDATE: {update_count} | INSERT: {insert_count}")
        print()

        if not products_to_insert and not products_to_update:
            print("[INFO] 필터링 후 저장/업데이트 대상 없음")
            return {'insert': 0, 'update': 0}

        update_query = """
            UPDATE amazon_hhp_product_list
            SET bsr_rank = %s, bsr_page_number = %s
            WHERE id = %s
        """

        insert_query = """
            INSERT INTO amazon_hhp_product_list (
                account_name, page_type, retailer_sku_name,
                bsr_rank, bsr_page_number, product_url,
                calendar_week, crawl_strdatetime, batch_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """

        def product_to_tuple(product):
            return (
                product['account_name'],
                product['page_type'],
                product['retailer_sku_name'],
                product['bsr_rank'],
                product['page_number'],
                product['product_url'],
                product['calendar_week'],
                product['crawl_strdatetime'],
                product['batch_id']
            )

        cursor = None
        try:
            cursor = self.db_conn.cursor()
            if products_to_update:
                cursor.executemany(update_query, products_to_update)

            if products_to_insert:
                insert_values = [product_to_tuple(product) for product in products_to_insert]
                cursor.executemany(insert_query, insert_values)

            self.db_conn.commit()

        except Exception as e:
            self.db_conn.rollback()
            print(f"[ERROR] Failed to save products: {e}")
            traceback.print_exc()
            raise

        finally:
            if cursor:
                cursor.close()

        self.stats['updated'] += update_count
        self.stats['inserted'] += insert_count

        return {'insert': insert_count, 'update': update_count}

def main():
    """개별 실행 진입점 (테스트 모드)"""
    crawler = AmazonBSRCrawler(test_mode=True)
    crawler.run()
    input("\n엔터키를 누르면 종료합니다...")


if __name__ == '__main__':
    main()
