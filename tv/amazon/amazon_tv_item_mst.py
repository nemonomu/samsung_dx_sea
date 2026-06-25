"""
Amazon TV Item MST 크롤러 (sku 갱신용)

================================================================================
주요 기능
================================================================================
1) tv_item_mst에서 account_name='Amazon' 인 레코드 조회 (LIMIT 500)
2) product_url 접속 후 리다이렉트되는 경우가 잦아 원본 item과
   리다이렉트된 최종 URL의 item(ASIN) 추출 → 동일한 경우에만 추출 진행
   동일하지 않으면 추출을 건너뛰고 mismatch 목록에 적재 → 최종 반환
3) "Item details" 버튼 클릭 후 우선순위에 따라 부품/모델 번호 추출
   Mfr Part Number → Manufacturer Part Number → Model Number → SKU Number
4) 추출 값이 기존 sku와 다른 경우에만 UPDATE, 같으면 SKIP
5) 콘솔 출력만 (로그 파일 미생성)

================================================================================
조회/저장 테이블
================================================================================
- 조회/UPDATE 대상: tv_item_mst
"""

import sys
import os
import re
import time
import random
import traceback
from datetime import datetime
from lxml import html

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.setup import setup_environment
setup_environment(__file__)

from common.base_crawler import BaseCrawler
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains


def extract_asin_from_url(product_url):
    """URL에서 ASIN 추출 (amazon_hhp_dt.py 와 동일 로직)"""
    if not product_url:
        return None

    match = re.search(r'/dp/([A-Z0-9]{10})/', product_url)
    if match:
        return match.group(1)

    match = re.search(r'%2[fF]dp%2[fF]([A-Z0-9]{10})%', product_url)
    if match:
        return match.group(1)

    return None


# ============================================================================
# XPath 모음 — 1 key : 1 xpath. 테스트 중 수정 시 여기만 수정.
# 추출 흐름:
#   item_details_button 클릭 후 → 1단계 → 2단계 → 3단계
#   추출값에 BNDL_ 포함 시 → bndl_fallback_model_name 으로 재추출
# ============================================================================
XPATHS = {
    # 1·2단계 (Item details 섹션) — 버튼 클릭 + prodDetTable에서 추출
    'item_details_button': "//a[@data-action='a-expander-toggle' and .//span[contains(text(), 'Item details')]]",
    'mfr_part_number': "//div[contains(@class, 'a-expander-content-expanded')]//tr[th[contains(text(), 'Mfr Part Number')]]/td",
    'manufacturer_part_number': "//div[contains(@class, 'a-expander-content-expanded')]//tr[th[contains(text(), 'Manufacturer Part Number')]]/td",
    'model_number': "//div[contains(@class, 'a-expander-content-expanded')]//tr[th[contains(text(), 'Model Number')]]/td",
    'model_name': "//div[contains(@class, 'a-expander-content-expanded')]//tr[th[contains(text(), 'Model Name')]]/td",  # BNDL_ fallback

    # 3단계 (Technical Details 섹션) — 섹션 스크롤 후 <strong> 안 라벨 → 두 번째 <td>
    'technical_details_section': "//div[@id='tech']",
    'sku_number': "//div[@id='tech']//strong[contains(translate(text(), 'N', 'n'), 'SKU number')]/ancestor::tr/td[2]",
}


class AmazonTVItemMstCrawler(BaseCrawler):
    """tv_item_mst 의 sku 컬럼을 Amazon 상세 페이지 정보로 갱신"""

    def __init__(self, limit=500):
        super().__init__()
        self.account_name = 'Amazon'
        self.limit = limit
        self.batch_id = f"amazon_tv_item_mst_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        # 원본/리다이렉트 ASIN 불일치 → 추출 건너뛴 item 누적
        self.mismatched_items = []

    # --------------------------------------------------------------------- init
    def initialize(self):
        if not self.connect_db():
            print("[ERROR] Initialize failed: DB connection failed")
            return False
        try:
            self.setup_driver()
        except Exception as e:
            print(f"[ERROR] Initialize failed: WebDriver setup failed - {e}")
            traceback.print_exc()
            return False
        if not self.set_zipcode():
            print("[ERROR] Initialize failed: Zipcode 설정 실패")
            return False
        print(f"[INFO] Initialize completed: batch_id={self.batch_id}")
        return True

    def set_zipcode(self, zipcode="10001", max_retries=3):
        """배송지 Zipcode 설정 (가격/재고 일관성을 위해)"""
        for attempt in range(max_retries):
            try:
                print(f"[INFO] Zipcode 설정 중: {zipcode} (시도 {attempt + 1}/{max_retries})")
                self.driver.get("https://www.amazon.com")
                time.sleep(random.uniform(3, 5))
                self.handle_continue_shopping()

                try:
                    delivery_link = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.ID, "nav-global-location-popover-link"))
                    )
                    delivery_link.click()
                    time.sleep(random.uniform(2, 3))
                except Exception as e:
                    print(f"[WARNING] 배송지 링크 클릭 실패: {e}")
                    self.driver.refresh()
                    time.sleep(random.uniform(3, 5))
                    continue

                try:
                    zipcode_input = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.ID, "GLUXZipUpdateInput"))
                    )
                    zipcode_input.clear()
                    zipcode_input.send_keys(zipcode)
                    time.sleep(random.uniform(1, 2))
                except Exception as e:
                    print(f"[WARNING] Zipcode 입력 실패: {e}")
                    continue

                try:
                    apply_button = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "#GLUXZipUpdate input[type='submit'], #GLUXZipUpdate-announce"))
                    )
                    apply_button.click()
                    time.sleep(random.uniform(2, 3))
                except Exception as e:
                    print(f"[WARNING] Apply 버튼 클릭 실패: {e}")
                    continue

                close_buttons = [
                    "//button[@name='glowDoneButton']",
                    "//button[contains(@class, 'a-popover-close')]",
                    "//input[@data-action='GLUXConfirmAction']",
                ]
                for xpath in close_buttons:
                    try:
                        btn = self.driver.find_element(By.XPATH, xpath)
                        if btn.is_displayed():
                            btn.click()
                            break
                    except Exception:
                        continue
                time.sleep(random.uniform(1, 2))

                print(f"[OK] Zipcode 설정 완료: {zipcode}")
                return True
            except Exception as e:
                print(f"[WARNING] Zipcode 설정 실패 (시도 {attempt + 1}): {e}")
                continue
        print("[ERROR] Zipcode 설정 실패 - 최대 재시도 횟수 초과")
        return False

    def check_and_handle_sorry_page(self, max_retries=3):
        """Sorry/Robot check 페이지 감지 + refresh 재시도.
        성공 (정상 페이지) 시 True, 끝까지 Sorry면 False.
        """
        for attempt in range(max_retries):
            page_source = self.driver.page_source.lower()
            title = self.driver.title.lower()

            is_sorry_page = (
                'sorry' in title or
                'robot check' in title or
                "sorry! we couldn't find" in page_source or
                'sorry, we just need to make sure' in page_source or
                '/error/logo' in page_source or
                'dogsofamazon' in page_source
            )

            if is_sorry_page:
                print(f"  [WARNING] Sorry 페이지 감지 (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(2, 3))
                    self.driver.refresh()
                    time.sleep(random.uniform(3, 5))
                    continue
                return False
            return True
        return False

    def handle_continue_shopping(self):
        try:
            page_html = self.driver.page_source.lower()
            captcha_keywords = ['captcha', 'robot', 'human verification', 'press & hold', 'press and hold']
            if not any(k in page_html for k in captcha_keywords):
                return True
            try:
                btn = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Continue shopping')]"))
                )
                if btn.is_displayed():
                    actions = ActionChains(self.driver)
                    actions.move_to_element(btn).pause(random.uniform(0.5, 1.0)).click().perform()
                    time.sleep(random.uniform(3, 5))
                    print("[INFO] Continue shopping 버튼 클릭 완료")
            except Exception:
                pass
            return True
        except Exception as e:
            print(f"[WARNING] handle_continue_shopping failed: {e}")
            return True

    # --------------------------------------------------------------------- DB
    def load_items(self):
        """tv_item_mst 에서 갱신 대상 조회"""
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                """
                SELECT id, item, sku, is_product, product_url
                FROM tv_item_mst_test
                WHERE account_name = %s
                  AND sku IS NULL
                ORDER BY id
                LIMIT %s
                """,
                (self.account_name, self.limit),
            )
            rows = cursor.fetchall()
            cursor.close()
            items = [
                {
                    'id': r[0],
                    'item': r[1],
                    'sku': r[2],
                    'is_product': r[3],
                    'product_url': r[4],
                }
                for r in rows
            ]
            print(f"[INFO] Loaded {len(items)} items from tv_item_mst")
            return items
        except Exception as e:
            print(f"[ERROR] Failed to load items: {e}")
            traceback.print_exc()
            return []

    def update_sku(self, row_id, new_sku):
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                """
                UPDATE tv_item_mst_test
                SET sku = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (new_sku, row_id),
            )
            self.db_conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"[ERROR] update_sku failed (id={row_id}): {e}")
            self.db_conn.rollback()
            return False

    # --------------------------------------------------------------------- 추출
    def click_item_details(self):
        """Item details 버튼 찾기 → 스크롤 → 클릭. (1·2단계 추출 준비)
        성공 시 True, 실패 시 False.
        """
        try:
            btn = WebDriverWait(self.driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, XPATHS['item_details_button']))
            )
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(random.uniform(0.4, 0.8))
            btn.click()
            time.sleep(random.uniform(1.0, 1.8))
            print("  [INFO] 'Item details' 버튼 클릭 완료")
            return True
        except Exception:
            print("  [INFO] 'Item details' 버튼 없음")
            return False

    def scroll_to_technical_details(self):
        """Technical Details 섹션 찾기 → 스크롤. (3단계 추출 준비, lazy-load 트리거)
        성공 시 True, 실패 시 False.
        """
        try:
            section = WebDriverWait(self.driver, 3).until(
                EC.presence_of_element_located((By.XPATH, XPATHS['technical_details_section']))
            )
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", section)
            time.sleep(random.uniform(0.8, 1.5))  # lazy-load 컨텐츠 로딩 대기
            print("  [INFO] Technical Details 섹션 발견 → 스크롤 완료")
            return True
        except Exception:
            print("  [INFO] Technical Details 섹션 없음")
            return False

    def _extract_by_xpath(self, tree, xpath):
        """단일 XPath 실행 후 정제된 텍스트 반환 (실패 시 None)"""
        try:
            results = tree.xpath(xpath)
            for el in results:
                text = el.text_content() if hasattr(el, 'text_content') else str(el)
                text = re.sub(r'\s+', ' ', text).strip().strip(':').strip()
                if text and text.lower() not in {'n/a', 'na', '-'}:
                    return text
        except Exception:
            pass
        return None

    def extract_part_number(self, tree, item_details_clicked=False, technical_details_found=False):
        """경로별 추출 (Item details와 Technical Details는 상호 배타적):
        - item_details_clicked=True  → 1·2단계 (Mfr / Manufacturer / Model Number)
        - technical_details_found=True → 3단계 (SKU Number)
        - 둘 다 False → 추출 안 함

        추출값에 'BNDL_' 포함 시 Model Name으로 재추출 (Item details 안에 있음).
        Returns: (label, value) 튜플 — 실패 시 (None, None).
        """
        if item_details_clicked:
            stages = [
                ('Mfr Part Number', 'mfr_part_number'),
                ('Manufacturer Part Number', 'manufacturer_part_number'),
                ('Model Number', 'model_number'),
            ]
        elif technical_details_found:
            stages = [
                ('SKU Number', 'sku_number'),
            ]
        else:
            return None, None

        for label, key in stages:
            value = self._extract_by_xpath(tree, XPATHS[key])
            if value:
                # BNDL_ 번들 코드 정제: Item details 경로 (1·2단계)에서만 적용. SKU Number는 제외.
                if item_details_clicked and 'BNDL_' in value:
                    model_name_value = self._extract_by_xpath(tree, XPATHS['model_name'])
                    if model_name_value:
                        print(f"  [INFO] BNDL_ 감지 → Model Name으로 재추출: '{value}' → '{model_name_value}'")
                        return 'Model Name (BNDL_ fallback)', model_name_value
                    print(f"  [WARNING] BNDL_ 감지했으나 Model Name 미발견 → 원본 유지: '{value}'")
                return label, value
        return None, None

    # --------------------------------------------------------------------- 핵심
    def process_one(self, row):
        """단일 row 처리 → 결과 코드 문자열 반환"""
        row_id = row['id']
        item = row['item']
        existing_sku = (row.get('sku') or '').strip()
        product_url = row.get('product_url')

        if not product_url:
            print(f"  [SKIP] product_url 없음 (item={item})")
            return 'skip_no_url'

        original_asin = extract_asin_from_url(product_url) or item
        try:
            self.driver.get(product_url)
            time.sleep(random.uniform(3, 5))
            self.handle_continue_shopping()
        except Exception as e:
            print(f"  [ERROR] 페이지 접근 실패: {e}")
            return 'error'

        # Sorry 페이지 감지 (3회 refresh 재시도 후에도 sorry면 적재)
        if not self.check_and_handle_sorry_page(max_retries=3):
            print("  [SKIP] Sorry 페이지 - 'sorry' 적재")
            self.update_sku(row_id, 'sorry')
            return 'sorry'

        # 리다이렉트 후 최종 URL의 ASIN 비교
        final_url = self.driver.current_url or ''
        final_asin = extract_asin_from_url(final_url)
        if not final_asin:
            print(f"  [SKIP] 최종 URL ASIN 추출 실패")
            self.mismatched_items.append({
                'id': row_id, 'item': item, 'reason': 'no_asin_in_final_url',
            })
            self.update_sku(row_id, 'redirect')
            return 'mismatch'

        if original_asin != final_asin:
            print(f"  [SKIP] ASIN 불일치: 원본={original_asin}, 리다이렉트={final_asin}")
            self.mismatched_items.append({
                'id': row_id, 'item': item,
                'reason': 'asin_mismatch', 'original_asin': original_asin, 'final_asin': final_asin,
            })
            self.update_sku(row_id, 'redirect')
            return 'mismatch'

        # 경로 결정 (상호 배타적):
        #   Item details 버튼 있음 → 클릭 → 1·2단계
        #   Item details 버튼 없음 → Technical Details 섹션 스크롤 → 3단계
        item_details_clicked = self.click_item_details()
        technical_details_found = False
        if not item_details_clicked:
            technical_details_found = self.scroll_to_technical_details()

        # HTML 파싱
        try:
            page_html = self.driver.page_source
            tree = html.fromstring(page_html)
        except Exception as e:
            print(f"  [ERROR] HTML 파싱 실패: {e}")
            return 'error'

        label, extracted = self.extract_part_number(
            tree,
            item_details_clicked=item_details_clicked,
            technical_details_found=technical_details_found,
        )
        if not extracted:
            print("  [SKIP] 우선순위 4개 라벨 모두 추출 실패")
            self.update_sku(row_id, 'no sku')
            return 'no_value'

        print(f"  [EXTRACT] {label} = '{extracted}' (기존 sku='{existing_sku}')")

        if extracted == existing_sku:
            print("  [SKIP] 기존 sku와 일치")
            return 'same'

        if self.update_sku(row_id, extracted):
            print(f"  [UPDATE] id={row_id}, sku: '{existing_sku}' → '{extracted}'")
            return 'update'
        return 'error'

    # --------------------------------------------------------------------- run
    def run(self):
        print("\n" + "=" * 60)
        print("Amazon TV Item MST Crawler")
        print("=" * 60)

        results = {
            'update': 0, 'same': 0, 'mismatch': 0,
            'no_value': 0, 'skip_no_url': 0, 'sorry': 0, 'error': 0,
        }

        try:
            if not self.initialize():
                return results, self.mismatched_items

            items = self.load_items()
            if not items:
                print("[INFO] 처리할 대상이 없습니다.")
                return results, self.mismatched_items

            for idx, row in enumerate(items, 1):
                print(f"\n[{idx}/{len(items)}] id={row['id']}, item={row['item']}")
                try:
                    code = self.process_one(row)
                except Exception as e:
                    print(f"  [ERROR] process_one 예외: {e}")
                    traceback.print_exc()
                    code = 'error'
                results[code] = results.get(code, 0) + 1
                time.sleep(random.uniform(2, 4))

            print("\n" + "=" * 60)
            print("Amazon TV Item MST Crawler 완료")
            print("=" * 60)
            for k, v in results.items():
                print(f"{k.upper():<13}: {v}")
            print(f"TOTAL        : {sum(results.values())}")
            print(f"\nASIN 불일치로 건너뛴 item ({len(self.mismatched_items)}건):")
            for m in self.mismatched_items:
                print(f"  - id={m['id']}, item={m['item']}, reason={m['reason']}")

            return results, self.mismatched_items

        except Exception as e:
            print(f"[ERROR] Run failed: {e}")
            traceback.print_exc()
            return results, self.mismatched_items
        finally:
            self.cleanup()

    def cleanup(self):
        try:
            if self.driver:
                self.driver.quit()
            if self.db_conn:
                self.db_conn.close()
            print("[INFO] Cleanup completed")
        except Exception as e:
            print(f"[WARNING] Cleanup failed: {e}")


# ============================================================================
# 메인
# ============================================================================
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Amazon TV Item MST Crawler (개별 실행)")
    print("=" * 60)
    crawler = AmazonTVItemMstCrawler(limit=500)
    results, mismatched = crawler.run()
    input("\n엔터를 눌러 종료하세요...")
