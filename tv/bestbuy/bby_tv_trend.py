"""
BestBuy TV Trend 페이지 크롤러

================================================================================
실행 모드
================================================================================
- 개별 실행: test_mode=True (기본값)
- 통합 크롤러: test_mode 및 batch_id를 파라미터로 전달

================================================================================
주요 기능
================================================================================
- Trend 페이지에서 제품 리스트 수집 (trend_rank 포함)
- 테스트 모드: test_count 설정값만큼 수집
- 운영 모드: 단일 페이지 전체 크롤링

================================================================================
저장 테이블
================================================================================
- bby_tv_product_list (제품 목록)
"""

import sys
import os
import time
import random
import traceback
import argparse
from datetime import datetime

# 공통 환경 설정 (작업 디렉토리, 한글 출력, 경로 설정)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.setup import setup_environment
setup_environment(__file__)

from common.bestbuy_base import BestBuyBaseCrawler


class BestBuyTVTrendCrawler(BestBuyBaseCrawler):
    """
    BestBuy TV Trend 페이지 크롤러
    """

    def __init__(self, test_mode=True, batch_id=None):
        """초기화. test_mode: 테스트(True)/운영 모드(False), batch_id: 통합 크롤러에서 전달"""
        super().__init__()
        self.test_mode = test_mode
        self.account_name = 'Bestbuy'
        self.page_type = 'trend'
        self.bestbuy_zip_code = '10010'
        self.item_mst_table = 'tv_item_mst'
        self.batch_id = batch_id
        self.calendar_week = None
        self.url_template = None

        # DrissionPage 객체 (setup_browser() 호출 후 채워짐)
        self.page = None

        self.test_count = 1  # 테스트 모드
        self.current_rank = 0

        # 캐시 기반 중복 관리 (item 기준)
        self.db_item_map = {}      # {item: DB row id} - Main에서 저장된 row
        self.saved_items = set()   # Trend에서 수집한 item (중복 방지)

        # 통계 변수
        self.stats = {
            'collected': 0,         # 수집 진행한 갯수
            'duplicates': 0,        # 중복 item 제거 갯수
            'openbox_filtered': 0,  # Open Box 제외 갯수
            'url_missing': 0,       # product_url 누락 갯수
            'non_product': 0,       # is_product=FALSE 제외 갯수
            'updated': 0,           # UPDATE 갯수
            'inserted': 0,          # INSERT 갯수
            'skipped_by_target': 0  # target 도달 후 미검사 갯수
        }

    def run(self):
        """실행: initialize() → crawl_page() → save_products() → 리소스 정리"""
        try:
            if not self.initialize():
                print("[ERROR] Initialization failed")
                return False

            self.current_rank = 0
            products = self.crawl_page(1)

            if not products:
                page_url = self.url_template

                for restart_count in range(1, 2):
                    print(f"[WARNING] Page 1: 0 products found, restarting browser with page URL ({restart_count}/1)")
                    if not self.restart_browser(page_url):  # 재시작 실패 시 아래의 0개 실패 처리로 이동
                        break

                    products = self.crawl_page(1)
                    if products:
                        break

            if not products:
                print("[ERROR] No products found at page 1")
                return False
            else:
                if self.restart_browser_after_url_load_error(1, products):
                    products = self.crawl_page(1)
                    if not products:
                        print("[ERROR] No products found at page 1")
                        return False

                result = self.save_products(products)

            print(f"[DONE] Page: 1, Update: {result['update']}, Insert: {result['insert']}, batch_id: {self.batch_id}")
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
            print(f"  OpenBox: {self.stats['openbox_filtered']}")
            print(f"  URL없음: {self.stats['url_missing']}")
            print(f"  비제품: {self.stats['non_product']}")
            print(f"  UPDATE: {self.stats['updated']}")
            print(f"  INSERT: {self.stats['inserted']}")
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
        """초기화: batch_id 설정 → DB 연결 → XPath 로드 → URL 템플릿 로드 → DrissionPage 설정 → 로그 정리"""
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
        if not self.setup_bestbuy_browser():
            print("[ERROR] Initialize failed: Browser setup failed")
            return False

        # 6. calendar_week 생성 및 로그 정리
        self.calendar_week = self.generate_calendar_week()
        self.cleanup_old_logs()

        # 7. DB에서 기존 item 캐시 로드 (Main에서 저장된 URL → item 매핑)
        self.db_item_map = self.build_db_item_cache('bby_tv_product_list')

        print(f"[INFO] Initialize completed: batch_id={self.batch_id}, calendar_week={self.calendar_week}")
        return True

    def crawl_page(self, page_number):
        """페이지 크롤링: 페이지 로드 → 설문조사 팝업 처리 → 제품 파싱 → URL 누락 시 스크롤 로딩 반복"""
        try:
            base_container_xpath = self.xpaths.get('base_container', {}).get('xpath')
            if not base_container_xpath:
                print("[ERROR] base_container XPath not found")
                raise ValueError("base_container XPath not found")

            url = self.url_template
            self.page.get(url)
            time.sleep(random.uniform(3, 5))

            self.close_survey_popup()  # 설문조사 팝업 처리

            # 1. 0개인 경우 로드 실패 예외처리 (최대 3회 새로고침)
            tree = self.retry_empty_page(base_container_xpath, page_number)

            # 새로고침 3회 후에도 0개이면 빈 리스트 반환
            if len(tree.xpath(base_container_xpath)) == 0:
                print(f"[ERROR] Page {page_number}: No products found after 3 refresh attempts")
                return []

            # 2. Trend는 고정 영역이므로 스크롤 없이 현재 HTML의 컨테이너를 사용
            base_containers = tree.xpath(base_container_xpath)

            # 3. 컨테이너를 Trend 로직에서 제품 dict로 변환
            products = []
            for idx, item in enumerate(base_containers, 1):
                try:
                    product_data = {
                        'account_name': self.account_name,
                        'page_type': self.page_type,
                        'retailer_sku_name': self.safe_extract_chain(item, 'retailer_sku_name'),
                        'trend_rank': 0,
                        'product_url': self.extract_product_url(item, 'https://www.bestbuy.com'),
                        'calendar_week': self.calendar_week,
                        'crawl_datetime': (datetime.now()).strftime('%Y-%m-%d %H:%M:%S'),
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
            return []

    def save_products(self, products):
        """DB 저장: 페이지에서 수집한 제품을 한 번에 저장한다."""
        if not products:
            print("[WARNING] save_products가 빈 products로 호출됨")
            return {'insert': 0, 'update': 0}

        # 페이지 단위 통계 스냅샷 (delta 계산용)
        page_attempted = len(products)
        page_dup_start = self.stats['duplicates']
        page_openbox_start = self.stats['openbox_filtered']
        page_url_missing_start = self.stats['url_missing']
        page_nonprod_start = self.stats['non_product']
        page_number = 1

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

            # openbox URL 제외
            if product_url and 'openbox' in product_url.lower():
                print(f"[SKIP] Open Box 상품 제외: {product_url}")
                self.stats['openbox_filtered'] += 1
                continue

            # URL에서 item 추출
            item = self.extract_item(product_url)

            # 이미 수집한 item → 스킵
            if item and item in self.saved_items:
                print(f"[SKIP] 중복 item: {retailer_sku_name if retailer_sku_name else 'N/A'}...")
                self.stats['duplicates'] += 1
                continue

            if item:
                self.saved_items.add(item)

            # is_product=FALSE 체크 (비제품 제외)
            if self.is_product_excluded(item):
                print(f"[SKIP] 비제품(is_product=FALSE): {retailer_sku_name if retailer_sku_name else 'N/A'}...")
                self.stats['non_product'] += 1
                continue

            # rank 할당 (중복 제거된 제품에만 순차적으로)
            target = self.test_count if self.test_mode else len(products)
            self.current_rank += 1
            if self.current_rank > target:
                self.stats['skipped_by_target'] += len(products) - idx
                break
            product['trend_rank'] = self.current_rank

            # 추출 결과 로그 버퍼링 (SKIP 로그 다음에 일괄 출력)
            extracted_logs.append(
                f"[{self.current_rank}] item = {item}\n"
                f"  ├─ retailer_sku_name: {product['retailer_sku_name'] or '-'}\n"
                f"  └─ trend_rank: {product['trend_rank'] or '-'}"
            )

            # DB에 있으면 UPDATE 대기열에 추가 (item 기준 매칭, id로 UPDATE)
            if item and item in self.db_item_map:
                row_id = self.db_item_map[item]
                products_to_update.append((
                    product['trend_rank'],
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
        page_openbox = self.stats['openbox_filtered'] - page_openbox_start
        page_url_missing = self.stats['url_missing'] - page_url_missing_start
        page_nonprod = self.stats['non_product'] - page_nonprod_start
        update_count = len(products_to_update)
        insert_count = len(products_to_insert)
        print()
        print(f"[Page {page_number} 통계] 시도: {page_attempted} | 중복: {page_dup} | OpenBox: {page_openbox} | URL없음: {page_url_missing} | 비제품: {page_nonprod} | UPDATE: {update_count} | INSERT: {insert_count}")
        print()

        if not products_to_insert and not products_to_update:
            print("[INFO] 필터링 후 저장/업데이트 대상 없음")
            return {'insert': 0, 'update': 0}

        update_query = """
            UPDATE bby_tv_product_list
            SET trend_rank = %s
            WHERE id = %s
        """

        insert_query = """
            INSERT INTO bby_tv_product_list (
                account_name, page_type, retailer_sku_name,
                trend_rank, product_url,
                calendar_week, crawl_datetime, batch_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s
            )
        """

        def product_to_tuple(product):
            return (
                product['account_name'],
                product['page_type'],
                product['retailer_sku_name'],
                product['trend_rank'],
                product['product_url'],
                product['calendar_week'],
                product['crawl_datetime'],
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
    parser = argparse.ArgumentParser(description='BestBuy TV Trend Crawler')
    args = parser.parse_args()

    crawler = BestBuyTVTrendCrawler(test_mode=True)
    crawler.run()
    input("\n[완료] 엔터를 누르면 종료됩니다...")


if __name__ == '__main__':
    main()
