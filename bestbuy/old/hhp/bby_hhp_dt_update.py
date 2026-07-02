"""
BestBuy Detail Update 크롤러

================================================================================
실행 모드
================================================================================
- 개별 실행: python bby_hhp_dt_update.py --batch-id <batch_id> [--mode 1|2] [--start-id N]
- batch_id 필수 (인자 또는 stdin)
- mode: 1=item IS NULL (기본), 2=count_of_reviews IS NULL

================================================================================
주요 기능
================================================================================
- hhp_retail_com 테이블에서 조건에 맞는 레코드 조회 (디테일 수집 실패/미수집 건)
- 각 제품 상세 페이지에서 리뷰, 별점, 스펙 등 재추출
- 기존 레코드를 UPDATE (새로 추출한 필드만)

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

from bestbuy.old.hhp.bby_hhp_dt import BestBuyHHPDetailCrawler


class BestBuyDetailUpdateCrawler(BestBuyHHPDetailCrawler):
    """
    BestBuy Detail Update 크롤러 (item IS NULL 재수집)
    """

    # 조회 조건 모드
    MODE_ITEM_NULL = '1'          # item IS NULL (페이지 에러 재수집)
    MODE_REVIEW_NULL = '2'        # count_of_reviews IS NULL (리뷰 미수집 재수집)
    MODE_BOTH = '3'               # item IS NULL OR count_of_reviews IS NULL (둘 다)

    def __init__(self, batch_id=None, start_id=None, mode=None, test_mode=False):
        """초기화. batch_id: 필수, start_id: 특정 id 이후부터 조회, mode: 조회 조건"""
        super().__init__(batch_id=batch_id, test_mode=test_mode)
        self.start_id = start_id
        self.mode = mode or self.MODE_ITEM_NULL
        self.table_name = 'test_hhp_retail_com' if test_mode else 'hhp_retail_com'

    def initialize(self):
        """초기화: DB 연결 → XPath 로드 → DrissionPage 설정 → 로그 정리"""
        if not self.batch_id:
            print("[ERROR] batch_id가 필요합니다.")
            return False

        return super().initialize()

    def load_product_list(self):
        """hhp_retail_com 테이블에서 조건에 맞는 제품 조회 (UPDATE 대상)"""
        try:
            cursor = self.db_conn.cursor()

            # 모드별 조건
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
                    page_type, retailer_sku_name, final_sku_price, savings,
                    original_sku_price, offer,
                    pick_up_availability, fastest_delivery, delivery_availability,
                    sku_status, promotion_type, main_rank, bsr_rank, trend_rank,
                    product_url, calendar_week
                FROM {self.table_name}
                WHERE account_name = %s AND batch_id = %s AND product_url IS NOT NULL {condition}
            """
            params = [self.account_name, self.batch_id]

            if self.start_id:
                query += " AND id >= %s"
                params.append(self.start_id)

            query += " ORDER BY id"

            cursor.execute(query, params)
            rows = cursor.fetchall()
            cursor.close()

            product_list = []
            for row in rows:
                product = {
                    'id': row[0],  # UPDATE용 id
                    'account_name': self.account_name,
                    'page_type': row[1],
                    'retailer_sku_name': row[2],
                    'final_sku_price': row[3],
                    'savings': row[4],
                    'original_sku_price': row[5],
                    'offer': row[6],
                    'pick_up_availability': row[7],
                    'fastest_delivery': row[8],
                    'delivery_availability': row[9],
                    'sku_status': row[10],
                    'promotion_type': row[11],
                    'main_rank': row[12],
                    'bsr_rank': row[13],
                    'trend_rank': row[14],
                    'product_url': row[15],
                    'calendar_week': row[16]
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
            cursor.execute("""
                SELECT sku FROM hhp_item_mst
                WHERE item = %s AND account_name = %s
            """, (item, self.account_name))
            existing = cursor.fetchone()

            if not existing:
                # 신규 INSERT
                cursor.execute("""
                    INSERT INTO hhp_item_mst (item, account_name, sku, product_url, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (item, self.account_name, new_sku, product_url, datetime.now(), datetime.now()))
            else:
                # 기존 sku가 없거나 no sku인 경우, 추출 sku가 있으면 업데이트
                updates = []
                params = []
                old_sku = existing[0]

                if (not (old_sku or '') or old_sku == 'no sku') and extracted_sku:
                    updates.append("sku = %s")
                    params.append(extracted_sku)

                # product_url은 항상 업데이트
                if product_url:
                    updates.append("product_url = %s")
                    params.append(product_url)

                if updates:
                    updates.append("updated_at = %s")
                    params.append(datetime.now())
                    params.append(item)
                    params.append(self.account_name)
                    cursor.execute(f"""
                        UPDATE hhp_item_mst SET {', '.join(updates)}
                        WHERE item = %s AND account_name = %s
                    """, params)

            self.db_conn.commit()
            cursor.close()

        except Exception as e:
            print(f"[ERROR] upsert_item_mst failed: {item}: {e}")
            traceback.print_exc()
            self.db_conn.rollback()

    def update_retail_com(self, product):
        """DB 저장: id 기준으로 UPDATE (새로 추출한 필드만)"""
        if not product:
            return False

        row_id = product.get('id')
        if not row_id:
            print(f"[ERROR] DB update failed: id가 없음")
            return False

        try:
            cursor = self.db_conn.cursor()

            updates = []
            params = []

            # DT 크롤러가 정의한 추출 필드만 UPDATE한다.
            for key in self.EXTRACTED_FIELDS:
                value = product.get(key)
                if value is not None:
                    updates.append(f"{key} = %s")
                    params.append(value)

            # crawl_strdatetime은 항상 업데이트
            updates.append("crawl_strdatetime = %s")
            params.append(
                product.get('crawl_strdatetime')
                or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )

            if not updates:
                print(f"[WARNING] 업데이트할 컬럼 없음: id={row_id}")
                cursor.close()
                return False

            params.append(row_id)
            update_query = f"UPDATE {self.table_name} SET {', '.join(updates)} WHERE id = %s"

            cursor.execute(update_query, params)
            self.db_conn.commit()
            cursor.close()
            return True

        except Exception as e:
            print(f"[ERROR] DB update failed: id={row_id}, {e}")
            traceback.print_exc()
            self.db_conn.rollback()
            return False

    def run(self):
        """실행: initialize() → load_product_list() → 제품별 crawl_detail() → update_retail_com() → 리소스 정리"""
        try:
            # 로그 파일 생성
            self.start_logging(self.batch_id or 'dt_update')

            if not self.initialize():
                print("[ERROR] Initialization failed")
                return False

            product_list = self.load_product_list()
            if not product_list:
                print("[INFO] No products to update")
                return True  # 업데이트 대상 없음 (에러 아님)

            total_updated = 0
            RESTART_INTERVAL = 20  # 20개마다 브라우저 재시작
            first_error_logged = False  # 첫 에러 페이지 시각 로그 여부

            crawl_start_time = datetime.now()
            print(f"[RATE-LIMIT] 수집 시작: {crawl_start_time.strftime('%H:%M:%S')}")

            for i, product in enumerate(product_list, 1):
                try:
                    # 20개마다 브라우저 재시작 (메모리 정리, 타임아웃 방지)
                    if i > 1 and (i - 1) % RESTART_INTERVAL == 0:
                        print(f"\n[INFO] 브라우저 재시작 ({i-1}개 처리 완료, 메모리 정리)")
                        if not self.restart_browser():
                            print("[WARNING] 브라우저 재시작 실패, 계속 진행")

                    sku_name = product.get('retailer_sku_name') or 'N/A'
                    row_id = product.get('id')
                    print(f"\n{'='*70}")
                    print(f"[{i}/{len(product_list)}] (id={row_id}) {sku_name}")
                    print(f"{'='*70}")

                    combined_data = self.crawl_detail(product)
                    if combined_data:
                        self.upsert_item_mst(combined_data)
                        if self.update_retail_com(combined_data):
                            total_updated += 1

                    time.sleep(random.uniform(5, 8))

                except Exception as e:
                    error_msg = str(e).lower()
                    print(f"[ERROR] Product {i} failed: {e}")

                    # 에러 페이지 감지 → 브라우저 종료 + 20분 대기 후 재시작
                    if "error page detected" in error_msg:
                        if not first_error_logged:
                            elapsed = (datetime.now() - crawl_start_time).total_seconds()
                            print(f"[RATE-LIMIT] 첫 차단 발생: {datetime.now().strftime('%H:%M:%S')} (수집 시작 후 {int(elapsed)}초, {i-1}건 수집)")
                            first_error_logged = True
                        print(f"[INFO] 에러 페이지 감지 - 브라우저 종료 후 20분 대기")
                        if self.page:
                            try:
                                self.page.quit()
                                self.page = None
                            except Exception:
                                pass
                        wait_minutes = 20
                        for remaining in range(wait_minutes * 60, 0, -60):
                            print(f"[WAIT] {remaining // 60}분 남음...")
                            time.sleep(60)
                        print(f"[INFO] 대기 완료 - 브라우저 재시작")
                        self.restart_browser()
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

                    # 타임아웃 또는 페이지 로드 실패시 브라우저 종료 + 20분 대기 후 재시작
                    if "timeout" in error_msg or "time out" in error_msg or "url unchanged" in error_msg:
                        print(f"[INFO] 타임아웃 감지 - 브라우저 종료 후 20분 대기")
                        if self.page:
                            try:
                                self.page.quit()
                                self.page = None
                            except Exception:
                                pass
                        wait_minutes = 20
                        for remaining in range(wait_minutes * 60, 0, -60):
                            print(f"[WAIT] {remaining // 60}분 남음...")
                            time.sleep(60)
                        print(f"[INFO] 대기 완료 - 브라우저 재시작")
                        self.restart_browser()
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

            print(f"[DONE] Processed: {len(product_list)}, Updated: {total_updated}, batch_id: {self.batch_id}")
            return True

        except Exception as e:
            print(f"[ERROR] Crawler failed: {e}")
            traceback.print_exc()
            return False

        finally:
            self.stop_logging()
            if self.page:
                self.page.quit()
            if self.db_conn:
                self.db_conn.close()


def fetch_today_batch_ids(table_name='hhp_retail_com'):
    """오늘 날짜의 BestBuy batch_id 목록을 DB에서 조회"""
    import psycopg2
    from config import DB_CONFIG
    today_str = datetime.now().strftime('%Y%m%d')
    batch_prefix = 't_b_' if table_name.startswith('test_') else 'b_'
    try:
        conn = psycopg2.connect(**DB_CONFIG, database='postgres')
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT DISTINCT batch_id FROM {table_name} "
            "WHERE account_name = 'Bestbuy' AND batch_id LIKE %s "
            "ORDER BY batch_id DESC",
            (f'{batch_prefix}{today_str}%',)
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
    parser = argparse.ArgumentParser(description='BestBuy HHP Detail Update Crawler')
    parser.add_argument('--batch-id', type=str, help='Batch ID to process')
    parser.add_argument('--start-id', type=int, help='Start from this id (WHERE id >= start_id)')
    parser.add_argument('--mode', type=str, choices=['1', '2', '3'], help='1: item IS NULL, 2: count_of_reviews IS NULL, 3: both')
    args = parser.parse_args()

    print("\n실행 모드 선택:")
    print("  1. 테스트 (test_hhp_retail_com)")
    print("  2. 운영 (hhp_retail_com)")
    mode_table_input = input("선택 (기본: 1): ").strip()
    test_mode = mode_table_input != '2'
    table_name = 'test_hhp_retail_com' if test_mode else 'hhp_retail_com'

    batch_id = args.batch_id
    if not batch_id:
        today_batch_ids = fetch_today_batch_ids(table_name)
        if today_batch_ids:
            print(f"\n오늘({datetime.now().strftime('%Y-%m-%d')}) BestBuy batch_id 목록:")
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
            print(f"오늘({datetime.now().strftime('%Y-%m-%d')}) BestBuy batch_id가 없습니다.")
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

    print("\nHTTP 프로토콜 선택:")
    print("  1. HTTP/1.1 강제")
    print("  2. HTTP/2 (기본)")
    http_input = input("선택 (기본: 2): ").strip()
    use_http1 = http_input == '1'

    crawler = BestBuyDetailUpdateCrawler(batch_id=batch_id, start_id=start_id, mode=mode, test_mode=test_mode)
    crawler.use_http1 = use_http1
    crawler.run()
    input("Press Enter to exit...")


if __name__ == '__main__':
    main()
