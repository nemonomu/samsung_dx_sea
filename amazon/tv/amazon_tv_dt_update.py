"""
Amazon TV Detail 페이지 크롤러 (UPDATE 전용)

================================================================================
실행 모드
================================================================================
- 운영: python amazon_tv_dt_update.py --batch-id <batch_id> [--mode 1|2|3|4|5] [--start-id N]
- 테스트: python amazon_tv_dt_update.py --test --batch-id <batch_id> [--mode 1|2|3|4|5] [--start-id N]
- batch_id 필수 (인자 또는 stdin)
- mode: 1=item IS NULL (기본), 2=star_rating/count_of_star_ratings IS NULL, 3=둘 다,
        4=detailed_review_content IS NULL (리뷰 본문 백필),
        5=number_of_units_purchased_past_month IS NULL (구매수 뱃지 백필)

================================================================================
주요 기능
================================================================================
- {target_table} 에서 mode 조건에 맞는 row 조회 (추출 실패 재시도)
- 각 제품 상세 페이지에서 리뷰, 별점, 스펙 등 추출
- 기존 row를 id 기준으로 UPDATE

================================================================================
저장 테이블
================================================================================
- 운영: tv_retail_com (UPDATE)
- 테스트: test_tv_retail_com (UPDATE)  ← --test 플래그

================================================================================
구조 (상속)
================================================================================
AmazonTVDetailCrawler를 상속 — 추출 로직(crawl_detail, helpers, extract_*)을
부모 클래스에서 그대로 사용. 추출 로직 변경 시 부모(amazon_tv_dt.py)만 수정하면
이 UPDATE 크롤러에도 자동 반영됨.

UPDATE 모드 전용 오버라이드 항목:
- __init__: start_id, mode 파라미터 추가
- initialize: batch_id 필수 (부모는 기본값 fallback)
- load_product_list: tv_retail_com에서 mode 조건부 조회
- save_to_retail_com: id 기준 UPDATE (부모는 INSERT)
- run: 에러 fallback save 안 함 (UPDATE만 가능하므로 의미 없음)
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

from common.base_crawler import BaseCrawler
from amazon.tv.amazon_tv_dt import AmazonTVDetailCrawler


class AmazonTVDetailUpdateCrawler(AmazonTVDetailCrawler):
    """
    Amazon TV Detail UPDATE 크롤러.

    추출 로직은 AmazonTVDetailCrawler에서 상속받음.
    UPDATE 모드에서만 달라지는 부분(load·save·run·init)만 오버라이드.
    """

    MODE_ITEM_NULL = '1'
    MODE_REVIEW_NULL = '2'
    MODE_BOTH = '3'
    MODE_DETAIL_REVIEW_NULL = '4'
    MODE_UNITS_NULL = '5'
    UPDATE_META_FIELDS = {
        'crawl_datetime': 'CURRENT_TIMESTAMP',
    }

    def __init__(self, batch_id=None, start_id=None, mode=None, test_mode=False):
        """batch_id: 필수, start_id: 특정 id 이후부터 조회, mode: 1/2/3, test_mode: True면 test_tv_retail_com 사용"""
        super().__init__(batch_id=batch_id, test_mode=test_mode)
        self.require_amazon_login = True
        self.start_id = start_id
        self.mode = mode or self.MODE_ITEM_NULL

        # UPDATE 모드는 재추출 검증용이라 캡처 불필요
        self.capture_enabled = False
        # 신뢰 프로필 사용 (detail과 동일) — 리뷰 게이트 통과용.
        # 부모 initialize()가 매 런 원본에서 사본을 리프레시해 적용한다.
        self.use_trusted_profile = True

    @property
    def target_table(self):
        """test_mode에 따라 조회·UPDATE 대상 테이블 결정"""
        return 'test_tv_retail_com' if self.test_mode else 'tv_retail_com'

    def initialize(self):
        """UPDATE 모드는 batch_id 필수 — 부모의 기본값 fallback 비허용"""
        if not self.batch_id:
            print("[ERROR] batch_id가 필요합니다.")
            return False
        return super().initialize()

    def load_product_list(self):
        """tv_retail_com 테이블에서 mode 조건에 맞는 제품 조회 (UPDATE 대상)"""
        try:
            cursor = self.db_conn.cursor()

            # 모드별 조건 설정
            review_missing_condition = "(star_rating IS NULL OR count_of_star_ratings IS NULL)"
            if self.mode == self.MODE_BOTH:
                condition = f"AND (item IS NULL OR {review_missing_condition})"
                mode_desc = f"item IS NULL OR {review_missing_condition}"
            elif self.mode == self.MODE_REVIEW_NULL:
                condition = f"AND {review_missing_condition}"
                mode_desc = review_missing_condition
            elif self.mode == self.MODE_DETAIL_REVIEW_NULL:
                condition = "AND detailed_review_content IS NULL"
                mode_desc = "detailed_review_content IS NULL"
            elif self.mode == self.MODE_UNITS_NULL:
                condition = "AND number_of_units_purchased_past_month IS NULL"
                mode_desc = "number_of_units_purchased_past_month IS NULL"
            else:
                condition = "AND item IS NULL"
                mode_desc = "item IS NULL"

            query = f"""
                SELECT
                    id,
                    retailer_sku_name,
                    offer, pick_up_availability, fastest_delivery,
                    delivery_availability, sku_status,
                    main_rank, bsr_rank, product_url, calendar_week,
                    crawl_datetime, page_type,
                    number_of_units_purchased_past_month, discount_type
                FROM {self.target_table}
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
                    'id': row[0],  # UPDATE WHERE 조건용
                    'account_name': self.account_name,
                    'retailer_sku_name': row[1],
                    'offer': row[2],
                    'pick_up_availability': row[3],
                    'fastest_delivery': row[4],
                    'delivery_availability': row[5],
                    'sku_status': row[6],
                    'main_rank': row[7],
                    'bsr_rank': row[8],
                    'product_url': row[9],
                    'calendar_week': row[10],
                    'crawl_datetime': row[11],
                    'page_type': row[12],
                    # 기존 units 값을 실어, 재크롤 시 PDP에서 못 찾아도 NULL로
                    # 덮어쓰지 않고 보존한다 (crawl_detail은 fill-if-empty).
                    'number_of_units_purchased_past_month': row[13],
                    # listing-only fallback의 할인 유형도 PDP 재추출 실패 시 보존한다.
                    'discount_type': row[14],
                }
                product_list.append(product)

            print(f"[INFO] Loaded {len(product_list)} products ({mode_desc})")
            return product_list

        except Exception as e:
            print(f"[ERROR] Failed to load product list: {e}")
            traceback.print_exc()
            return []

    def save_to_retail_com(self, product):
        """DB 저장: id 기준 UPDATE (부모의 INSERT 대신).

        부모 클래스의 run()이 self.save_to_retail_com()을 polymorphic하게 호출하므로
        같은 이름으로 오버라이드하면 UPDATE 모드로 동작.
        """
        if not product:
            return False

        row_id = product.get('id')
        if not row_id:
            print(f"[ERROR] DB update failed: id가 없음")
            return False

        if (
            self.mode == self.MODE_DETAIL_REVIEW_NULL
            and not product.get('detailed_review_content')
        ):
            print(
                f"[WARNING] Mode 4 skipped: detailed_review_content "
                f"was not collected (id={row_id})"
            )
            return False

        try:
            cursor = self.db_conn.cursor()

            # 크롤링으로 추출한 모든 필드 — None이면 NULL로 UPDATE
            # (이번 실행에서 추출된 결과로 row의 추출 필드를 완전히 덮어씀)
            # 필드 리스트는 부모의 EXTRACTED_FIELDS 사용 → 부모에 새 필드 추가 시 자동 반영
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            update_meta = {
                field: now if source == 'CURRENT_TIMESTAMP' else source
                for field, source in self.UPDATE_META_FIELDS.items()
            }
            if self.mode == self.MODE_DETAIL_REVIEW_NULL:
                # Review backfill must not overwrite unrelated extracted fields.
                update_data = {
                    'detailed_review_content': product['detailed_review_content'],
                    **update_meta,
                }
            else:
                update_data = {
                    **{key: product.get(key) for key in self.EXTRACTED_FIELDS},
                    **update_meta,
                }

            updates = [f"{key} = %s" for key in update_data]
            params = list(update_data.values())
            params.append(row_id)
            update_query = f"UPDATE {self.target_table} SET {', '.join(updates)} WHERE id = %s"

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
        """UPDATE 모드 실행 — 에러 fallback save 안 함 (UPDATE만 가능하므로 의미 없음)"""
        try:
            # 로그 파일 시작 — {batch_id}_dt_update_{생성시각}.txt
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
                    product_url = product.get('product_url', 'N/A')
                    url_display = product_url[:80] + '...' if len(product_url) > 80 else product_url
                    print(f"\n[{i}/{len(product_list)}] {sku_name}")
                    print(f"  URL: {url_display}")

                    combined_data = self.crawl_detail(product)
                    if combined_data:
                        if combined_data.get('_detail_skip') == 'asin_mismatch':
                            print("[INFO] 리다이렉트 감지 - UPDATE 없이 현재 row 스킵")
                        else:
                            if combined_data is product:
                                raise RuntimeError(
                                    'Detail extraction incomplete - UPDATE skipped'
                                )
                            if self.mode != self.MODE_DETAIL_REVIEW_NULL:
                                self.upsert_item_mst(combined_data)
                            if self.save_to_retail_com(combined_data):
                                total_updated += 1

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
                                if combined_data.get('_detail_skip') == 'asin_mismatch':
                                    print("[INFO] 리다이렉트 감지 - UPDATE 없이 현재 row 스킵")
                                else:
                                    if combined_data is product:
                                        raise RuntimeError(
                                            'Detail extraction incomplete - UPDATE skipped'
                                        )
                                    if self.mode != self.MODE_DETAIL_REVIEW_NULL:
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

            print(f"[DONE] Processed: {len(product_list)}, Updated: {total_updated}, Table: {self.target_table}, batch_id: {self.batch_id}")

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
            self.stop_logging()


def fetch_today_batch_ids(test_mode=False):
    """오늘 날짜의 Amazon TV batch_id 목록을 공통 함수로 조회."""
    return BaseCrawler.fetch_today_batch_ids(
        table_name='tv_retail_com',
        account_name='Amazon',
        test_mode=test_mode,
    )


def main():
    """개별 실행 진입점"""
    import argparse
    parser = argparse.ArgumentParser(description='Amazon TV Detail Update Crawler')
    parser.add_argument('--batch-id', type=str, help='Batch ID to process')
    parser.add_argument('--start-id', type=int, help='Start from this id (WHERE id >= start_id)')
    parser.add_argument('--mode', type=str, choices=['1', '2', '3', '4', '5'], help='1: item IS NULL, 2: star_rating/count_of_star_ratings IS NULL, 3: both, 4: detailed_review_content IS NULL, 5: number_of_units_purchased_past_month IS NULL')
    parser.add_argument('--test', action='store_true', help='Test 모드 — test_tv_retail_com 테이블에서 조회·UPDATE')
    args = parser.parse_args()

    test_mode = args.test
    if test_mode:
        print("[INFO] *** TEST 모드 *** — test_tv_retail_com 테이블 사용")

    batch_id = args.batch_id
    if not batch_id:
        today_batch_ids = fetch_today_batch_ids(test_mode=test_mode)
        if today_batch_ids:
            mode_label = "TEST " if test_mode else ""
            print(f"\n오늘({datetime.now().strftime('%Y-%m-%d')}) {mode_label}Amazon batch_id 목록:")
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
        print("  3: 둘 다 (item IS NULL OR star_rating/count_of_star_ratings IS NULL)")
        print("  4: detailed_review_content IS NULL (리뷰 본문 미수집 재수집)")
        print("  5: number_of_units_purchased_past_month IS NULL (구매수 뱃지 재수집)")
        mode_input = input("모드 입력 (기본: 1): ").strip()
        mode = mode_input if mode_input in ('1', '2', '3', '4', '5') else '1'

    crawler = AmazonTVDetailUpdateCrawler(batch_id=batch_id, start_id=start_id, mode=mode, test_mode=test_mode)
    crawler.run()
    input("Press Enter to exit...")


if __name__ == '__main__':
    main()
