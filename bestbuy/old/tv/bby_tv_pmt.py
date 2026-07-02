"""
BestBuy TV Promotion 페이지 크롤러

================================================================================
실행 모드
================================================================================
- 개별 실행: test_mode=True (기본값)
- 통합 크롤러: test_mode 및 batch_id를 파라미터로 전달

================================================================================
주요 기능
================================================================================
- Promotion 페이지에서 제품 리스트 수집 (promotion_position, promotion_type 포함)
- promotion_type: 페이지 상단 배너 문구 (h2 + p 결합)
- 테스트 모드: test_count 설정값만큼 섹션 수집
- 운영 모드: 단일 페이지 전체 크롤링

================================================================================
저장 테이블
================================================================================
- bby_tv_product_list (제품 목록)
  - 기존 제품(main/bsr): promotion_position, promotion_type UPDATE
  - 신규 제품: INSERT
"""

import sys
import os
import time
import random
import traceback
import argparse
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


class BestBuyTVPromotionCrawler(BestBuyBaseCrawler):
    """
    BestBuy TV Promotion 페이지 크롤러
    """

    def __init__(self, test_mode=True, batch_id=None):
        """초기화. test_mode: 테스트(True)/운영 모드(False), batch_id: 통합 크롤러에서 전달"""
        super().__init__()
        self.test_mode = test_mode
        self.account_name = 'Bestbuy'
        self.page_type = 'promotion'
        self.bestbuy_zip_code = '10010'
        self.item_mst_table = 'tv_item_mst'
        self.batch_id = batch_id
        self.calendar_week = None
        self.url_template = None

        # DrissionPage 객체 (setup_browser() 호출 후 채워짐)
        self.page = None

        self.test_count = 1  # 테스트 모드

        # 캐시 기반 중복 관리 (item 기준)
        self.db_item_map = {}      # {item: DB row id} - Main에서 저장된 row
        self.saved_items = set()   # Promotion에서 수집한 item (중복 방지)

        # 통계 변수
        self.stats_by_type = {}    # {promotion_type: {'collected': 0, 'updated': 0, ...}}
        self.stats = {
            'collected': 0,         # 수집 진행한 갯수
            'duplicates': 0,        # 중복 item 제거 갯수
            'openbox_filtered': 0,  # Open Box 제외 갯수
            'url_missing': 0,       # product_url 누락 갯수
            'non_product': 0,       # is_product=FALSE 제외 갯수
            'updated': 0,           # UPDATE 갯수
            'inserted': 0           # INSERT 갯수
        }

    def run(self):
        """실행: initialize() → crawl_page() → save_products() → 리소스 정리"""
        try:
            if not self.initialize():
                print("[ERROR] Initialization failed")
                return False

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
            if self.stats_by_type:
                print("  promotion_type별:")
                for ptype, ts in self.stats_by_type.items():
                    print(f"    - {ptype}: 수집 {ts['collected']}, UPDATE {ts['updated']}, INSERT {ts['inserted']}")
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
        """페이지 크롤링: 페이지 로드 → 설문조사 팝업 처리 → 섹션별 제품 데이터 추출"""
        try:
            section_container_xpath = self.xpaths.get('section_container', {}).get('xpath')
            base_container_xpath = self.xpaths.get('base_container', {}).get('xpath')
            if not section_container_xpath:
                print("[ERROR] section_container XPath not found")
                raise ValueError("section_container XPath not found")
            if not base_container_xpath:
                print("[ERROR] base_container XPath not found")
                raise ValueError("base_container XPath not found")

            url = self.url_template
            self.page.get(url)
            time.sleep(random.uniform(3, 5))

            self.close_survey_popup()  # 설문조사 팝업 처리

            # Promotion은 페이지 전체 상품이 아니라 "섹션 → 섹션 안 상품" 구조다.
            # 따라서 section_container를 먼저 찾고, 각 섹션 내부에서 base_container를 다시 확인한다.
            sections = []
            sections_to_process = []
            for attempt in range(1, 4):
                page_html = self.page.html
                tree = html.fromstring(page_html)
                sections = tree.xpath(section_container_xpath)

                print(f"[INFO] Attempt {attempt}: Found {len(sections)} sections")
                if sections:
                    # 테스트 모드는 일부 섹션만, 운영 모드는 페이지의 모든 섹션을 검사한다.
                    candidate_sections = sections[:self.test_count] if self.test_mode else sections
                    total_items = 0
                    total_url_missing = 0

                    for sec_idx, section in enumerate(candidate_sections, 1):
                        # base_container는 페이지 전체가 아니라 현재 섹션 안에서만 찾는다.
                        items = section.xpath(base_container_xpath)
                        section_url_missing = sum(
                            1 for item in items
                            if not self.extract_product_url(item, 'https://www.bestbuy.com')
                        )
                        total_items += len(items)
                        total_url_missing += section_url_missing

                        if section_url_missing:
                            print(f"[WARNING] Page {page_number}: 섹션 {sec_idx} product_url 없음 {section_url_missing}/{len(items)}")

                    # 섹션 안 상품이 있고 URL 누락이 없으면 추출 가능한 상태로 판단한다.
                    if total_items > 0 and total_url_missing == 0:
                        sections_to_process = candidate_sections
                        break

                    # 섹션은 있지만 상품 컨테이너가 없거나 URL이 덜 로드된 경우 새로고침 재시도한다.
                    if total_items == 0:
                        print(f"[WARNING] Page {page_number}: promotion item 컨테이너 없음 ({attempt}/3)")
                    else:
                        print(f"[WARNING] Page {page_number}: product_url 누락 {total_url_missing}/{total_items}, refresh retry ({attempt}/3)")

                # 섹션 자체가 없거나, 섹션 안 상품/URL 로딩이 부족하면 최대 3회 새로고침한다.
                if attempt < 3:
                    self.page.refresh()
                    time.sleep(random.uniform(5, 8))
                    self.close_survey_popup()

            if not sections:
                print(f"[ERROR] Page {page_number}: No sections found after 3 refresh attempts")
                return []

            if not sections_to_process:
                print(f"[ERROR] Page {page_number}: promotion product_url 로드 실패")
                return []

            products = []
            for sec_idx, section in enumerate(sections_to_process, 1):
                # promotion_type은 섹션 단위 값이고, promotion_position은 섹션 안 상품 순번이다.
                promotion_type = self.extract_promotion_type(section)
                print(f"[INFO] 섹션 {sec_idx} promotion_type: {promotion_type}")

                items = section.xpath(base_container_xpath)
                print(f"[INFO] 섹션 {sec_idx}: {len(items)}개 아이템")

                for pos, item in enumerate(items, 1):
                    try:
                        product_data = {
                            'account_name': self.account_name,
                            'page_type': self.page_type,
                            'retailer_sku_name': self.safe_extract_chain(item, 'retailer_sku_name'),
                            'promotion_position': pos,
                            'promotion_type': promotion_type,
                            'offer': self.convert_first_number(item, 'offer'),
                            'product_url': self.extract_product_url(item, 'https://www.bestbuy.com'),
                            'calendar_week': self.calendar_week,
                            'crawl_datetime': (datetime.now()).strftime('%Y-%m-%d %H:%M:%S'),
                            'batch_id': self.batch_id
                        }

                        products.append(product_data)

                    except Exception as e:
                        print(f"[ERROR] 섹션 {sec_idx} 아이템 {pos} 추출 실패: {e}")
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

        def get_type_stats(ptype):
            """promotion_type별 통계 dict 반환 (없으면 생성)"""
            if ptype not in self.stats_by_type:
                self.stats_by_type[ptype] = {'collected': 0, 'updated': 0, 'inserted': 0}
            return self.stats_by_type[ptype]

        for product in products:
            retailer_sku_name = product.get('retailer_sku_name') or ''
            ptype = product.get('promotion_type') or 'Unknown'
            type_stats = get_type_stats(ptype)
            type_stats['collected'] += 1

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

            # 추출 결과 로그 버퍼링 (SKIP 로그 다음에 일괄 출력)
            extracted_logs.append(
                f"[{product['promotion_position']}] item = {item}\n"
                f"  ├─ retailer_sku_name: {product['retailer_sku_name'] or '-'}\n"
                f"  ├─ promotion_type: {product['promotion_type'] or '-'}\n"
                f"  └─ offer: {product['offer'] or '-'}"
            )

            # DB에 있으면 UPDATE 대기열에 추가 (item 기준 매칭, id로 UPDATE)
            if item and item in self.db_item_map:
                row_id = self.db_item_map[item]
                products_to_update.append((
                    product['promotion_position'],
                    product['promotion_type'],
                    product['offer'],
                    row_id,
                ))
                type_stats['updated'] += 1
            else:
                # DB에 없으면 INSERT 대기열에 추가
                products_to_insert.append(product)
                type_stats['inserted'] += 1

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
            SET promotion_position = %s, promotion_type = %s, offer = %s
            WHERE id = %s AND promotion_position IS NULL
        """

        insert_query = """
            INSERT INTO bby_tv_product_list (
                account_name, page_type, retailer_sku_name,
                promotion_position, promotion_type, offer,
                product_url, calendar_week, crawl_datetime, batch_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """

        def product_to_tuple(product):
            return (
                product['account_name'],
                product['page_type'],
                product['retailer_sku_name'],
                product['promotion_position'],
                product['promotion_type'],
                product['offer'],
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

    def extract_promotion_type(self, section):
        """섹션 요소에서 promotion_type 추출 (4개 XPath 시도 후 결합)"""
        type_fields = ['promotion_type_h2', 'promotion_type_h3', 'promotion_type_p', 'promotion_type_sub']
        parts = []
        for field in type_fields:
            text = self.safe_extract_chain(section, field)
            if text:
                parts.append(' '.join(text.split()).strip())
        return ' '.join(parts).strip() or None


def main():
    """개별 실행 진입점 (테스트 모드)"""
    parser = argparse.ArgumentParser(description='BestBuy TV Promotion Crawler')
    args = parser.parse_args()

    crawler = BestBuyTVPromotionCrawler(test_mode=True)
    crawler.run()
    input("\n[완료] 엔터를 누르면 종료됩니다...")


if __name__ == '__main__':
    main()
