"""
Amazon Detail 페이지 크롤러 (UPDATE 전용)

- 개별 실행: python amazon/amazon_hhp_dt_update.py --batch-id <batch_id> [--mode 1|2] [--start-id N]
- batch_id 필수 (인자 또는 stdin)
- mode: 1=item IS NULL (기본), 2=star_rating/count_of_star_ratings IS NULL, 3=both

추출 로직은 AmazonDetailCrawler(amazon_hhp_dt.py)를 그대로 사용하고,
UPDATE 전용으로 달라지는 조회/저장/실행 흐름만 이 파일에서 오버라이드한다.
"""

import sys
import os
import time
import random
import traceback
from datetime import datetime

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

from common.base_crawler import BaseCrawler
from amazon.hhp.amazon_hhp_dt import AmazonDetailCrawler


class AmazonDetailUpdateCrawler(AmazonDetailCrawler):
    """Amazon HHP Detail UPDATE 크롤러."""

    MODE_ITEM_NULL = '1'
    MODE_REVIEW_NULL = '2'
    MODE_BOTH = '3'
    UPDATE_META_FIELDS = {
        'crawl_strdatetime': 'CURRENT_TIMESTAMP',
    }

    def __init__(self, batch_id=None, start_id=None, mode=None, test_mode=False):
        super().__init__(batch_id=batch_id, test_mode=test_mode)
        self.start_id = start_id
        self.mode = mode or self.MODE_ITEM_NULL
        self.capture_enabled = False

    @property
    def target_table(self):
        return 'test_hhp_retail_com' if self.test_mode else 'hhp_retail_com'

    def initialize(self):
        """UPDATE 모드는 batch_id 필수."""
        if not self.batch_id:
            print("[ERROR] batch_id가 필요합니다.")
            return False
        return super().initialize()

    def load_product_list(self):
        """hhp_retail_com 테이블에서 조건에 맞는 제품 조회 (UPDATE 대상)."""
        try:
            cursor = self.db_conn.cursor()

            review_missing_condition = "(star_rating IS NULL OR count_of_star_ratings IS NULL)"
            if self.mode == self.MODE_BOTH:
                condition = f"AND (item IS NULL OR {review_missing_condition})"
                mode_desc = f"item IS NULL OR {review_missing_condition}"
            elif self.mode == self.MODE_REVIEW_NULL:
                condition = f"AND {review_missing_condition}"
                mode_desc = review_missing_condition
            else:
                condition = "AND item IS NULL"
                mode_desc = "item IS NULL"

            query = f"""
                SELECT
                    id,
                    page_type, retailer_sku_name,
                    number_of_units_purchased_past_month, final_sku_price, original_sku_price,
                    delivery_availability, fastest_delivery,
                    available_quantity_for_purchase, discount_type,
                    main_rank, bsr_rank, product_url, calendar_week, batch_id
                FROM {self.target_table}
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

            products = []
            for row in rows:
                products.append({
                    'id': row[0],
                    'account_name': self.account_name,
                    'page_type': row[1],
                    'retailer_sku_name': row[2],
                    'number_of_units_purchased_past_month': row[3],
                    'final_sku_price': row[4],
                    'original_sku_price': row[5],
                    'delivery_availability': row[6],
                    'fastest_delivery': row[7],
                    'available_quantity_for_purchase': row[8],
                    'discount_type': row[9],
                    'main_rank': row[10],
                    'bsr_rank': row[11],
                    'product_url': row[12],
                    'calendar_week': row[13],
                    'batch_id': row[14],
                })

            print(f"[INFO] Loaded {len(products)} products ({mode_desc})")
            return products

        except Exception as e:
            print(f"[ERROR] Failed to load product list: {e}")
            traceback.print_exc()
            return []

    def save_to_retail_com(self, product):
        """DB 저장: id 기준 UPDATE. 추출 필드 목록은 부모의 EXTRACTED_FIELDS를 사용한다."""
        if not product:
            return False

        row_id = product.get('id')
        if not row_id:
            print("[ERROR] DB update failed: id가 없음")
            return False

        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            update_meta = {
                field: now if source == 'CURRENT_TIMESTAMP' else source
                for field, source in self.UPDATE_META_FIELDS.items()
            }
            update_data = {
                **{field: product.get(field) for field in self.EXTRACTED_FIELDS},
                **update_meta,
            }

            updates = [f"{field} = %s" for field in update_data]
            params = list(update_data.values())
            params.append(row_id)
            update_query = f"UPDATE {self.target_table} SET {', '.join(updates)} WHERE id = %s"

            cursor = self.db_conn.cursor()
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
        """UPDATE 모드 실행."""
        try:
            log_path = self.start_logging(f"{self.batch_id or 'unknown'}_dt_update")
            if log_path:
                print(f"[INFO] Log file: {log_path}")

            if not self.initialize():
                print("[ERROR] Initialization failed")
                return False

            product_list = self.load_product_list()
            if not product_list:
                print("[INFO] No products to update")
                return True

            total_updated = 0
            for i, product in enumerate(product_list, 1):
                try:
                    sku_name = product.get('retailer_sku_name') or 'N/A'
                    product_url = product.get('product_url') or 'N/A'
                    url_display = product_url[:80] + '...' if len(product_url) > 80 else product_url
                    print(f"\n[{i}/{len(product_list)}] {sku_name}")
                    print(f"  URL: {url_display}")

                    combined_data = self.crawl_detail(product)
                    if combined_data:
                        self.upsert_item_mst(combined_data)
                        update_success = self.save_to_retail_com(combined_data)
                        if update_success:
                            total_updated += 1
                        print(f"  [UPDATE] {'성공' if update_success else '실패'}")
                    else:
                        print("  [UPDATE] 스킵 (데이터 없음)")

                    time.sleep(random.uniform(2, 4))

                except Exception as e:
                    error_msg = str(e).lower()
                    print(f"[ERROR] Product {i} failed: {e}")

                    if "dom timeout" in error_msg:
                        next_product = product_list[i] if i < len(product_list) else None
                        next_url = next_product.get('product_url') if next_product else None
                        print("[INFO] DOM 타임아웃 - 다음 상품 URL로 브라우저 재시작 후 현재 상품 스킵")
                        self.restart_browser(next_url)
                        continue

                    if "redirect detected" in error_msg:
                        print("[INFO] 리다이렉트 감지 - UPDATE 없이 현재 row 스킵")
                        continue

                    if "amazon recovery unresolved" in error_msg:
                        print("[INFO] Amazon 페이지 복구 실패 - UPDATE 없이 현재 row 스킵")
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
                                    total_updated += 1
                            print(f"[SUCCESS] 재시도 성공")
                            retry_success = True
                            break
                        except Exception as retry_e:
                            print(f"[ERROR] 재시도 실패 ({retry_attempt}/2): {retry_e}")

                    if retry_success:
                        continue

                    print("[INFO] 재시도 실패 - UPDATE 없이 현재 row 스킵")
                    continue

            print(f"\n{'='*60}")
            print("[완료]")
            print(f"  처리: {len(product_list)}")
            print(f"  UPDATE: {total_updated}")
            print(f"  테이블: {self.target_table}")
            print(f"  batch_id: {self.batch_id}")
            print(f"{'='*60}")
            self.print_spec_diff_summary()
            return True

        except Exception as e:
            print(f"[ERROR] Crawler failed: {e}")
            traceback.print_exc()
            return False

        finally:
            self.stop_logging()
            if self.page:
                try:
                    self.page.quit()
                except Exception:
                    pass
            if self.db_conn:
                self.db_conn.close()


def fetch_today_batch_ids(test_mode=False):
    """오늘 날짜의 Amazon HHP batch_id 목록을 공통 함수로 조회."""
    return BaseCrawler.fetch_today_batch_ids(
        table_name='hhp_retail_com',
        account_name='Amazon',
        test_mode=test_mode,
    )


def main():
    """개별 실행 진입점."""
    import argparse

    parser = argparse.ArgumentParser(description='Amazon Detail Update Crawler')
    parser.add_argument('--batch-id', type=str, help='Batch ID to process')
    parser.add_argument('--start-id', type=int, help='Start from this id (WHERE id >= start_id)')
    parser.add_argument('--mode', type=str, choices=['1', '2', '3'], help='1: item IS NULL, 2: star_rating/count_of_star_ratings IS NULL, 3: both')
    parser.add_argument('--test', action='store_true', help='Test mode - test_hhp_retail_com 테이블에서 조회/UPDATE')
    args = parser.parse_args()

    test_mode = args.test
    if test_mode:
        print("[INFO] *** TEST 모드 *** - test_hhp_retail_com 테이블 사용")

    batch_id = args.batch_id
    if not batch_id:
        today_batch_ids = fetch_today_batch_ids(test_mode=test_mode)
        if today_batch_ids:
            print(f"\n오늘({datetime.now().strftime('%Y-%m-%d')}) Amazon batch_id 목록:")
            for i, bid in enumerate(today_batch_ids, 1):
                print(f"  {i}. {bid}")
            print("  0. 직접 입력")
            choice = input("\n번호 선택: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(today_batch_ids):
                batch_id = today_batch_ids[int(choice) - 1]
            elif choice == '0' or not choice.isdigit():
                batch_id = input("batch_id 입력: ").strip() if choice == '0' else choice
        else:
            print(f"오늘({datetime.now().strftime('%Y-%m-%d')}) Amazon batch_id가 없습니다.")
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
        print("  2: star_rating/count_of_star_ratings IS NULL (별점 미수집 재수집)")
        print("  3: both (item IS NULL OR star_rating/count_of_star_ratings IS NULL)")
        mode_input = input("모드 입력 (기본: 1): ").strip()
        mode = mode_input if mode_input in ('1', '2', '3') else '1'

    crawler = AmazonDetailUpdateCrawler(
        batch_id=batch_id,
        start_id=start_id,
        mode=mode,
        test_mode=test_mode,
    )
    crawler.run()
    input("Press Enter to exit...")


if __name__ == '__main__':
    main()
