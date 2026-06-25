"""
BestBuy 크롤러 공통 헬퍼.

BestBuy Main / BSR / Detail 크롤러에서 함께 쓰는 브라우저 설정, URL 파싱,
마스터 비제품 제외 체크, 목록 페이지 초기화 로직을 관리한다.
"""

import random
import re
import time
import traceback

from DrissionPage import ChromiumPage, ChromiumOptions
from lxml import html

from common.base_crawler import BaseCrawler


class BestBuyBaseCrawler(BaseCrawler):
    """BestBuy 크롤러 공통 헬퍼."""

    # ========================================================================
    # Common
    # ========================================================================
    # 함수 목록:
    # - setup_bestbuy_browser: DrissionPage 브라우저 설정 및 ZIP 코드 세션 초기화
    # - restart_browser: 브라우저 재시작
    # - close_survey_popup: 설문조사 팝업 닫기
    # - load_detail_page: 상세 상품 페이지 로드 및 기본 검증
    # - is_bad_page: 에러/빈 페이지 여부 판단
    # - open_spec_modal: 스펙 버튼 클릭 후 모달 열림 확인
    # - set_store_zip_code: BestBuy ZIP 코드 설정
    # - click_bestbuy_your_store_button: Your store 버튼 클릭
    # - is_bestbuy_zip_already_set: ZIP 코드 설정 여부 확인
    # - click_bestbuy_store_by_zip: ZIP 코드가 포함된 매장 선택
    # - find_first_visible_element: 여러 locator 후보 중 첫 번째 요소 탐색
    # - click_first_visible_element: 여러 locator 후보 중 첫 번째 요소 클릭
    # - extract_item: BestBuy 상품 URL에서 item/SKU ID 추출
    # - convert_first_number: BestBuy 텍스트에서 첫 번째 숫자 추출
    # - extract_final_sku_price: BestBuy 가격 및 상태 fallback 추출
    # - extract_price_info: BestBuy 가격/원가/할인 추출
    def setup_bestbuy_browser(self):
        """DrissionPage 브라우저를 설정한다."""
        try:
            co = ChromiumOptions()
            if getattr(self, 'use_http1', False):
                co.set_argument('--disable-http2')
            self.page = ChromiumPage(co)
            print(f"[SUCCESS] DrissionPage setup complete{' (HTTP/1.1 강제)' if getattr(self, 'use_http1', False) else ''}")

            zip_code = getattr(self, 'bestbuy_zip_code', '10010')
            if zip_code:
                print("[INFO] BestBuy 세션 초기화 중...")
                self.page.get('https://www.bestbuy.com')
                time.sleep(random.uniform(5, 8))
                self.close_survey_popup()

                if not self.set_store_zip_code(zip_code):
                    print(f"[ERROR] BestBuy ZIP 코드 설정 실패: {zip_code}")
                    print("[ERROR] BestBuy session initialization failed")
                    return False

                print(f"[OK] BestBuy ZIP 코드 설정 완료: {zip_code}")
                print("[INFO] BestBuy 메인 페이지로 복귀합니다")
                self.page.get('https://www.bestbuy.com')
                time.sleep(random.uniform(3, 5))
                self.close_survey_popup()

            return True
        except Exception as e:
            print(f"[ERROR] DrissionPage setup failed: {e}")
            traceback.print_exc()
            return False

    def restart_browser(self, url=None):
        """브라우저를 재시작한다. url이 있으면 해당 URL로 이동한다."""
        try:
            print("[INFO] Closing browser...")
            if self.page:
                try:
                    self.page.quit()
                except Exception:
                    pass
                self.page = None

            time.sleep(random.uniform(3, 5))

            if not self.setup_bestbuy_browser():
                print("[ERROR] Browser restart failed")
                return False

            if url:
                print(f"[INFO] Accessing URL: {url}")
                self.page.get(url)
                time.sleep(random.uniform(3, 5))
                self.close_survey_popup()

            print("[SUCCESS] Browser restarted")
            return True

        except Exception as e:
            print(f"[ERROR] Browser restart failed: {e}")
            traceback.print_exc()
            return False

    def close_survey_popup(self):
        """설문조사 팝업이 보이면 'No, Thanks' 버튼을 클릭해 닫는다."""
        try:
            survey_no_button = self.page.ele('#survey_invite_no', timeout=2)
            if survey_no_button:
                survey_no_button.click()
                print("[INFO] Survey popup closed")
                time.sleep(1)
        except Exception:
            pass

    def load_detail_page(self, product_url, referer=None):
        """BestBuy 상세 상품 페이지를 로드하고 기본 URL/페이지 상태를 검증한다."""
        if not product_url:
            raise ValueError("product_url is required")

        if referer is None:
            keyword = getattr(self, 'bestbuy_search_keyword', 'tv')
            referer = f'https://www.bestbuy.com/site/searchpage.jsp?st={keyword}'

        if referer:
            self.page.set.headers({'Referer': referer})

        previous_url = self.page.url if self.page else None

        self.page.get(product_url)
        time.sleep(random.uniform(3, 5))

        current_url = self.page.url

        if previous_url and current_url == previous_url:
            print("[WARNING] 페이지 로드 실패 감지 (URL 변경 없음)")
            raise Exception("Page load failed - URL unchanged")

        if 'bestbuy.com' not in current_url:
            print("[WARNING] 페이지 로드 실패 감지 (bestbuy.com 아님)")
            raise Exception("Page load failed - URL validation failed")

        original_item = self.extract_item(product_url)
        current_item = self.extract_item(current_url)
        if original_item and current_item and original_item != current_item:
            print(f"[WARNING] Redirect detected: {original_item} -> {current_item}")
            raise Exception("Redirect detected")

        self.close_survey_popup()

        page_html = self.page.run_js('return document.documentElement.outerHTML')
        bad_type = self.is_bad_page(page_html)
        if bad_type:
            print(f"[WARNING] {'에러' if bad_type == 'error' else '빈'} 페이지 감지")
            raise Exception("Page load failed - error page detected")

    def is_bad_page(self, page_html):
        """에러 페이지 / 빈 페이지 여부를 판단한다."""
        if not page_html:
            return 'empty'

        page_html_lower = page_html.lower()
        error_keywords = [
            "this site can't be reached",
            "err_http2_protocol_error",
            "application error",
            "a client-side exception",
        ]

        if any(keyword in page_html_lower for keyword in error_keywords):
            return 'error'

        if len(page_html.strip()) < 500 or '<body></body>' in page_html_lower or '<body> </body>' in page_html_lower:
            return 'empty'

        return None

    def open_spec_modal(
        self,
        button_field='specs_button',
        modal_title_field='specs_modal_title',
        max_scrolls=5,
        max_modal_retries=3,
    ):
        """BestBuy 스펙 버튼을 클릭하고 모달이 열리면 modal_tree를 반환한다."""
        specs_button, _ = self.scroll_find_element(
            button_field,
            max_scrolls=max_scrolls,
            label='스펙 버튼 탐색',
            return_element=True,
        )
        if not specs_button:
            print("  [스펙 버튼 탐색] [FAIL] 스펙 버튼 못 찾음 - 스펙 모달 열기 스킵")
            return None

        try:
            self.page.run_js("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'})", specs_button)
            time.sleep(0.5)
            specs_button.click()
            print("  [스펙 버튼 탐색] [OK] 클릭 성공")
        except Exception as e:
            print(f"  [스펙 버튼 탐색] [FAIL] 클릭 실패: {e}")
            return None

        specs_modal_title_xpath = self.xpaths.get(modal_title_field, {}).get('xpath')
        for modal_attempt in range(1, max_modal_retries + 1):
            time.sleep(3)
            modal_html = self.page.run_js('return document.documentElement.outerHTML')
            modal_tree = html.fromstring(modal_html)

            if not specs_modal_title_xpath:
                return modal_tree

            if modal_tree.xpath(specs_modal_title_xpath):
                print(f"  [스펙 모달] [OK] 열림 확인 ({modal_attempt}회)")
                return modal_tree

            print(f"  [스펙 모달] [WAIT] 미열림 ({modal_attempt}/{max_modal_retries}회)")
            if modal_attempt < max_modal_retries:
                try:
                    specs_button.click()
                except Exception:
                    pass

        print(f"  [스펙 모달] [FAIL] {max_modal_retries}회 시도 실패 - 모달 데이터 추출 스킵")
        return None

    def close_spec_modal(self, close_button_field='close_button'):
        """BestBuy 스펙 모달을 닫는다."""
        try:
            close_button_xpath = self.xpaths.get(close_button_field, {}).get('xpath')
            if close_button_xpath:
                close_button = self.page.ele(f'xpath:{close_button_xpath}', timeout=2)
                if close_button:
                    close_button.click()
                    time.sleep(0.5)
                    return True
        except Exception:
            pass

        try:
            self.page.actions.key_down('ESCAPE').key_up('ESCAPE')
            time.sleep(0.5)
            return True
        except Exception:
            return False

    def set_store_zip_code(self, zip_code):
        """BestBuy 헤더의 Your store에서 ZIP 코드를 설정한다."""
        if not zip_code:
            print("[WARNING] ZIP 코드가 비어 있어 스토어 설정을 건너뜁니다")
            return False

        try:
            print(f"[INFO] BestBuy ZIP 코드 설정 시작: {zip_code}")

            if not self.click_bestbuy_your_store_button():
                return False

            if self.is_bestbuy_zip_already_set(zip_code):
                print(f"[OK] BestBuy ZIP 코드 이미 설정됨, 스킵: {zip_code}")
                return True

            self.click_first_visible_element([
                'css:#store-loc-overlay a[href*="store-locator"]',
                'xpath://div[@id="store-loc-overlay"]//a[contains(normalize-space(.), "Find Another Store")]',
                'xpath://a[contains(normalize-space(.), "Find Another Store")]',
            ], 'Find Another Store 버튼', timeout=2)

            time.sleep(2)

            zip_input = self.find_first_visible_element([
                'css:input[data-cy="ZipCodeInputComponent"]',
                'css:input.zip-code-input',
                'css:input[placeholder*="ZIP"]',
                'css:input[aria-label*="ZIP"]',
                'css:input[name*="zip"]',
                'css:input[id*="zip"]',
                'css:input[type="search"]',
                'css:input[type="text"]',
                'xpath://input[contains(translate(@placeholder, "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "ZIP")]',
                'xpath://input[contains(translate(@aria-label, "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "ZIP")]',
            ], timeout=5)

            if not zip_input:
                print("[ERROR] ZIP 코드 입력창을 찾지 못했습니다")
                return False

            try:
                self.page.run_js("""
                    arguments[0].value = '';
                    arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
                    arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
                """, zip_input)
            except Exception:
                try:
                    zip_input.clear()
                except Exception:
                    pass

            try:
                zip_input.input(zip_code)
                self.page.run_js("""
                    arguments[0].value = arguments[1];
                    arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
                    arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
                """, zip_input, zip_code)
            except Exception:
                self.page.run_js("""
                    arguments[0].value = arguments[1];
                    arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
                    arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
                """, zip_input, zip_code)

            print(f"[INFO] ZIP 코드 입력 완료: {zip_code}")
            time.sleep(0.5)

            if not self.click_first_visible_element([
                'css:button[data-cy="SubmitButton"]',
                'css:button.location-zip-code-form-update-btn',
                'css:button[type="submit"]',
                'xpath://button[contains(normalize-space(.), "Update")]',
                'xpath://button[contains(normalize-space(.), "Search")]',
                'xpath://button[contains(normalize-space(.), "Find")]',
            ], 'ZIP 검색 버튼', timeout=3):
                try:
                    zip_input.input('\n')
                    print("[INFO] ZIP 입력창에서 Enter 처리 완료")
                except Exception:
                    print("[WARNING] ZIP 검색 버튼을 찾지 못해 Enter 처리도 실패했습니다")
                    return False

            time.sleep(3)

            if not self.click_bestbuy_store_by_zip(zip_code):
                return False

            print("[INFO] BestBuy ZIP 코드 설정 흐름 완료")
            return True

        except Exception as e:
            print(f"[ERROR] BestBuy ZIP 코드 설정 실패: {e}")
            traceback.print_exc()
            return False

    def click_bestbuy_your_store_button(self, max_retries=3):
        """Your store 버튼은 늦게 뜰 수 있어 최대 3회 재시도한다."""
        locators = [
            'css:button.showing-your-store',
            'css:button[aria-controls="store-loc-overlay"]',
            'xpath://button[.//span[@data-testid="store-locator-your-store"]]',
            'xpath://button[.//span[@data-testid="store-locator-store-name"]]',
            'xpath://a[@href="/site/store-locator" and .//span[@data-testid="store-locator-your-store"]]',
            'xpath://a[.//span[@data-testid="store-locator-your-store"]]',
            'xpath://a[.//span[@data-testid="store-locator-store-locator"]]',
        ]

        for attempt in range(1, max_retries + 1):
            if self.click_first_visible_element(locators, 'Your store 버튼', timeout=4):
                return True

            if attempt < max_retries:
                print(f"[WARNING] Your store 버튼 클릭 재시도 ({attempt}/{max_retries})")
                time.sleep(random.uniform(2, 4))

        print("[ERROR] Your store 버튼 클릭 실패 - ZIP 코드 설정을 중단합니다")
        return False

    def is_bestbuy_zip_already_set(self, zip_code):
        """헤더/스토어 오버레이에서 ZIP 코드가 이미 설정되어 있는지 확인한다."""
        try:
            page_html = self.page.html or ''
            if f'Stores Near {zip_code}' in page_html:
                return True

            zip_text = self.find_first_visible_element([
                f'xpath://div[@id="store-loc-overlay"]//*[contains(normalize-space(.), "{zip_code}")]',
                f'xpath://header//*[contains(normalize-space(.), "{zip_code}")]',
                f'xpath://*[contains(@data-testid, "store-locator") and contains(normalize-space(.), "{zip_code}")]',
            ], timeout=2)
            return bool(zip_text)

        except Exception:
            return False

    def click_bestbuy_store_by_zip(self, zip_code):
        """스토어 목록에서 ZIP 코드가 포함된 카드의 Make This Your Store 버튼을 클릭한다."""
        store_button = self.find_first_visible_element([
            f'xpath:(//li[contains(@class, "store") and contains(., "{zip_code}")]//button[contains(normalize-space(.), "Make This Your Store")])[1]',
            f'xpath:(//li[@data-cy="LocationCardListItemComponent" and contains(., "{zip_code}")]//button[contains(normalize-space(.), "Make This Your Store")])[1]',
            f'xpath:(//*[contains(@data-cy, "store-details") and contains(., "{zip_code}")]//button[contains(normalize-space(.), "Make This Your Store")])[1]',
            f'xpath:(//*[contains(@data-testid, "extendedAddress") and contains(., "{zip_code}")]/ancestor::li[contains(@class, "store")]//button[contains(normalize-space(.), "Make This Your Store")])[1]',
        ], timeout=8)

        if not store_button:
            print(f"[ERROR] ZIP {zip_code} 매장의 Make This Your Store 버튼을 찾지 못했습니다")
            return False

        try:
            self.page.run_js("arguments[0].scrollIntoView({block: 'center'});", store_button)
            time.sleep(0.5)
        except Exception:
            pass

        try:
            store_button.click()
            print(f"[INFO] ZIP {zip_code} 매장 선택 버튼 클릭 완료")
            time.sleep(2)
            return True
        except Exception as e:
            print(f"[ERROR] ZIP {zip_code} 매장 선택 버튼 클릭 실패: {e}")
            return False

    def find_first_visible_element(self, locators, timeout=2):
        """여러 locator 후보 중 먼저 찾은 요소를 반환한다."""
        for locator in locators:
            try:
                element = self.page.ele(locator, timeout=timeout)
                if element:
                    return element
            except Exception:
                continue
        return None

    def click_first_visible_element(self, locators, label, timeout=2):
        """여러 locator 후보 중 먼저 찾은 요소를 클릭한다."""
        element = self.find_first_visible_element(locators, timeout=timeout)
        if not element:
            print(f"[WARNING] {label} 요소를 찾지 못했습니다")
            return False

        try:
            self.page.run_js("arguments[0].scrollIntoView({block: 'center'});", element)
            time.sleep(0.5)
        except Exception:
            pass

        try:
            element.click()
            print(f"[INFO] {label} 클릭 완료")
            time.sleep(1)
            return True
        except Exception as e:
            print(f"[WARNING] {label} 클릭 실패: {e}")
            return False

    def restart_browser_after_url_load_error(self, page_num, products, threshold=3, wait_minutes=20):
        """product_url 누락이 많으면 브라우저를 재시작하고 같은 페이지를 다시 시도하도록 알린다."""
        null_url_count = sum(1 for p in products if not p.get('product_url'))
        if null_url_count < threshold:
            return False

        print(f"[WARNING] Page {page_num}: product_url NULL {null_url_count}/{len(products)} — 에러 페이지로 판단")
        print(f"[INFO] 브라우저 종료 후 {wait_minutes}분 대기")
        if self.page:
            try:
                self.page.quit()
                self.page = None
            except Exception:
                pass

        for remaining in range(wait_minutes, 0, -1):
            print(f"[WAIT] {remaining}분 남음...")
            time.sleep(60)

        print(f"[INFO] 대기 완료 — 브라우저 재시작 후 Page {page_num} 재시도")
        self.setup_bestbuy_browser()
        return True

    def extract_item(self, product_url):
        """BestBuy 상품 URL에서 item/SKU ID를 추출한다."""
        if not product_url:
            return None

        try:
            cleaned_url = re.sub(r'/sku/\d+(/openbox\?.*)?$', '', product_url)
            cleaned_url = cleaned_url.split('?')[0]
            parts = cleaned_url.split('/')
            if not parts:
                return None
            item = parts[-1]
            if item.endswith('.p'):
                item = item[:-2]
            return item if item else None
        except Exception:
            return None

    def build_db_item_cache(self, product_list_table):
        """현재 batch_id의 product_url을 item으로 변환해 {item: row id} 캐시를 만든다."""
        try:
            cursor = self.db_conn.cursor()
            query = f"""
                SELECT id, product_url FROM {product_list_table}
                WHERE account_name = %s AND batch_id = %s
            """
            cursor.execute(query, (self.account_name, self.batch_id))
            rows = cursor.fetchall()
            cursor.close()

            db_item_map = {}
            for row_id, db_url in rows:
                if not db_url:
                    continue

                item = self.extract_item(db_url)
                if not item:
                    continue

                if item in db_item_map:
                    print(f"[WARNING] Duplicate item in DB batch: item={item}, keep_id={db_item_map[item]}, skip_id={row_id}")
                    continue

                db_item_map[item] = row_id

            print(f"[INFO] DB item cache loaded: {len(db_item_map)} items")
            return db_item_map

        except Exception as e:
            print(f"[WARNING] build_db_item_cache failed: {e}")
            return {}

    def convert_first_number(self, source, field_name=None, append_text=None, remove_comma=True):
        """
        첫 번째 숫자를 추출해 문자열로 반환한다.

        field_name을 전달하면 source에서 safe_extract_chain(source, field_name)으로 먼저 추출한다.
        append_text를 전달하면 추출한 숫자 뒤에 붙여 반환한다. 예: append_text=' inches'
        remove_comma=False를 전달하면 숫자 안의 쉼표를 유지한다.
        """
        raw = self.safe_extract_chain(source, field_name) if field_name else source
        if not raw:
            return None

        match = re.search(r'\d[\d,]*(?:\.\d+)?', raw)
        if not match:
            return None

        number_text = match.group(0)
        if remove_comma:
            number_text = number_text.replace(',', '')

        return f"{number_text}{append_text}" if append_text else number_text

    # ========================================================================
    # Main, Bsr
    # ========================================================================
    # 함수 목록:
    # - retry_empty_page: 상품 0개 로드 시 새로고침 재시도
    # - wait_for_product_urls_with_scroll: URL lazy-load 대응 제품 컨테이너 탐색
    # - is_pagination_visible: 페이지네이션 노출 여부 확인
    # - scroll_to_bottom: 목록 페이지 lazy-load 유도용 스크롤 헬퍼
    def retry_empty_page(self, base_container_xpath, page_number, max_retries=3):
        """상품 컨테이너가 0개이면 새로고침하며 재시도하고 최종 tree를 반환한다."""
        tree = None

        for refresh_attempt in range(1, max_retries + 1):
            page_html = self.page.html
            tree = html.fromstring(page_html)

            if len(tree.xpath(base_container_xpath)) == 0:
                print(f"[WARNING] Page {page_number}: 0 products found, refresh attempt {refresh_attempt}/{max_retries}")
                if refresh_attempt < max_retries:
                    self.page.refresh()
                    time.sleep(random.uniform(5, 8))
                    self.close_survey_popup()
                continue

            break

        return tree

    def wait_for_product_urls_with_scroll(self, base_container_xpath, page_number, max_scroll_attempts=30):
        """URL이 lazy-load되는 목록 페이지를 스크롤하며 URL이 로드된 제품 컨테이너를 반환한다."""
        current_position = 0
        base_containers = []
        bottom_wait_count = 0

        for scroll_attempt in range(1, max_scroll_attempts + 1):
            page_html = self.page.html
            tree = html.fromstring(page_html)
            base_containers = tree.xpath(base_container_xpath)

            null_url_count = 0
            for item in base_containers:
                product_url_raw = self.safe_extract_chain(item, 'product_url')
                if not product_url_raw or product_url_raw == '#':
                    null_url_count += 1

            total_found = len(base_containers)

            if total_found > 0 and null_url_count == 0:
                print(f"[INFO] Page {page_number}: All {total_found} URLs loaded successfully. Start extraction.")
                break

            print(f"[INFO] Page {page_number}: Parsed {total_found} products, {null_url_count} URLs missing. Scrolling... ({scroll_attempt}/{max_scroll_attempts})")

            is_pagination_visible = self.is_pagination_visible()
            total_height = self.page.run_js("return document.body.scrollHeight")

            if is_pagination_visible or current_position >= total_height:
                bottom_wait_count += 1
                if bottom_wait_count >= 3:
                    print(f"[INFO] Page {page_number}: Reached bottom {bottom_wait_count} times. Giving up scroll.")
                    break

                print(f"[WARNING] Page {page_number}: Reached bottom but {null_url_count} URLs missing. Scrolling back to TOP... ({bottom_wait_count}/3)")
                current_position = 0
                self.page.run_js("window.scrollTo(0, 0);")
                time.sleep(random.uniform(3, 5))
                continue

            scroll_step = random.randint(400, 600)
            current_position += scroll_step
            self.page.run_js(f"window.scrollTo(0, {current_position});")
            time.sleep(random.uniform(4, 6))

        print(f"[INFO] Page {page_number}: Final product containers: {len(base_containers)}")
        return base_containers

    def is_pagination_visible(self):
        """pagenation XPath chain에 등록된 요소가 현재 화면에 보이면 True를 반환한다."""
        candidates = self.get_chain_xpaths('pagenation') or self.get_chain_xpaths('pagination')
        if not candidates:
            return False

        for field_name, xpath in candidates:
            try:
                element = self.page.ele(f'xpath:{xpath}', timeout=0.5)
                if not element:
                    continue

                is_visible = self.page.run_js("""
                    var rect = arguments[0].getBoundingClientRect();
                    return (rect.top >= 0 && rect.top <= window.innerHeight);
                """, element)
                if is_visible:
                    return True
            except Exception:
                continue

        return False

    def scroll_to_bottom(self):
        """페이지네이션이 보이거나 바닥에 도달할 때까지 목록 페이지를 스크롤한다."""
        try:
            current_position = 0

            for _ in range(50):
                if self.is_pagination_visible():
                    break

                scroll_step = random.randint(400, 600)
                current_position += scroll_step
                self.page.run_js(f"window.scrollTo(0, {current_position});")
                time.sleep(random.uniform(1.5, 2.5))

                total_height = self.page.run_js("return document.body.scrollHeight")
                if current_position >= total_height:
                    break

            time.sleep(random.uniform(0, 4))

        except Exception as e:
            print(f"[ERROR] Scroll failed: {e}")
            traceback.print_exc()


    # ========================================================================
    # Detail
    # ========================================================================
    # 함수 목록:
    # - extract_savings: savings 텍스트에서 '$' 금액 추출
    # - extract_final_sku_price: 가격 및 상태 fallback 추출
    # - extract_price_info: 가격/원가/할인 추출
    # - extract_sku_info: Model/SKU Number 추출

    def extract_savings(self, source, field_name='savings'):
        """BestBuy savings 텍스트에서 '$'가 붙은 금액만 추출해 반환한다."""
        savings_raw = self.safe_extract_chain(source, field_name) if field_name else source
        if not savings_raw:
            return None

        match = re.search(r'\$\s*[\d,]+(?:\.\d{1,2})?', str(savings_raw))
        if not match:
            return None

        return re.sub(r'\$\s+', '$', match.group(0)).strip()

    def extract_final_sku_price(self, tree):
        """final_sku_price 추출 실패 시 BestBuy 상태별 fallback 값을 반환한다."""
        field_names = [
            'final_sku_price',
            'see_price_in_cart',
            'no_longer_available',
        ]

        for field_name in field_names:
            value = self.safe_extract_chain_join(tree, field_name, separator='')
            if not value:
                continue

            if field_name == 'no_longer_available':
                return 'no longer available'

            return value

        return None

    def extract_price_info(self, tree, max_attempts=3):
        """BestBuy 상세 페이지에서 가격/원가/할인 정보를 추출한다."""
        final_sku_price = None
        original_sku_price = None
        savings = None

        for price_attempt in range(1, max_attempts + 1):
            if price_attempt > 1:
                print(f"├─ Price 재시도 ({price_attempt}/{max_attempts})...")
                time.sleep(random.uniform(2, 3))
                page_html = self.page.run_js('return document.documentElement.outerHTML')
                tree = html.fromstring(page_html)

            final_sku_price = self.extract_final_sku_price(tree)
            if final_sku_price:
                if '$' in final_sku_price:
                    original_sku_price = self.safe_extract_chain(tree, 'original_sku_price')
                    savings = self.extract_savings(tree)
                break

        return final_sku_price, original_sku_price, savings

    def extract_sku_info(self, tree):
        """BestBuy 상세 페이지에서 Model SKU와 SKU Number를 추출한다."""
        sku_raw = self.safe_extract_chain(tree, 'sku')
        sku = sku_raw.replace('Model:', '').strip() if sku_raw else None

        sku_number_raw = self.safe_extract_chain(tree, 'sku_number')
        sku_number = ''.join(filter(str.isdigit, sku_number_raw)) if sku_number_raw else None

        return sku, sku_number

    def is_no_review_product(self, review_count_text):
        """BestBuy 자체 리뷰가 없는 상품인지 판별한다."""
        if not review_count_text:
            return False

        review_count_text = review_count_text.lower()

        if 'not yet reviewed' in review_count_text:
            return True
        elif 'from' in review_count_text:
            return True
        else:
            return False

    def extract_recommendation_intent(self, tree, field_name='recommendation_intent'):
        """BestBuy recommendation_intent를 추출해 저장 형식으로 정규화한다."""
        recommendation_intent_raw = self.safe_extract_chain(tree, field_name)
        if not recommendation_intent_raw:
            return None

        if '%' in recommendation_intent_raw:
            return f"{recommendation_intent_raw} would recommend to a friend"
        else:
            return f"{recommendation_intent_raw}% would recommend to a friend"

    def build_review_url(self, product_url, sku_number):
        """BestBuy 상품 URL과 SKU Number로 리뷰 URL을 생성한다."""
        if not product_url or not sku_number:
            return None

        if '/site/' in product_url and '/product/' not in product_url:
            url_parts = product_url.split('/site/')
            if len(url_parts) > 1:
                product_path = url_parts[1].rsplit('/', 1)[0]
                return f"https://www.bestbuy.com/site/reviews/{product_path}/{sku_number}"
        elif '/product/' in product_url:
            url_parts = product_url.split('/product/')
            if len(url_parts) > 1:
                product_part = url_parts[1].split('/sku/')[0]
                product_part = product_part.rsplit('/', 1)[0]
                return f"https://www.bestbuy.com/site/reviews/{product_part}/{sku_number}"

        return None

    def extract_detailed_reviews(self, product, sku_number, recommendation_intent):
        """리뷰 버튼 진입부터 상세 리뷰와 recommendation_intent fallback까지 처리한다."""
        detailed_review_content = None
        detail_page_url = self.page.url
        target_review_url = self.build_review_url(product.get('product_url'), sku_number)

        # 1. 리뷰 버튼 탐색/클릭
        review_button_candidates = self.get_chain_xpaths('reviews_button')
        if not review_button_candidates:
            print(f"[WARNING] DB에 'reviews_button' XPath가 등록되어 있지 않아 리뷰 버튼 클릭 및 상세 리뷰 추출이 스킵됨")
            return detailed_review_content, recommendation_intent

        review_button_found = self.scroll_find_element(
            review_button_candidates,
            max_scrolls=5,
            label='리뷰 버튼 탐색',
            click=True,
        )
        if review_button_found:
            print(f"  [review] 리뷰 페이지 진입 성공 (버튼 클릭)")
            time.sleep(1.5)

        # 2. 버튼 탐색 실패 시 리뷰 URL 직접 접속
        if not review_button_found and target_review_url:
            try:
                self.page.get(target_review_url)
                time.sleep(random.uniform(2, 3))
                review_button_found = True
                self.close_survey_popup()
                print(f"├─ 리뷰 URL 직접 접속 성공 (2차): {target_review_url}")
            except Exception as e:
                print(f"[WARNING] 리뷰 URL 직접 접속 실패 (2차): {e}")
                if "error page detected" in str(e).lower():
                    raise e
        elif not review_button_found and not target_review_url:
            print(f"[WARNING] 리뷰 URL 생성 실패 - URL 형식 미지원")

        if not review_button_found:
            print(f"├─ 리뷰 버튼 찾기 및 URL 직접 접속 모두 실패하여 진입 불가")
            return detailed_review_content, recommendation_intent

        # 3. 상세 리뷰 XPath 설정 확인
        if not self.get_chain_xpaths('detailed_review_content'):
            print(f"[WARNING] DB에 'detailed_review_content' XPath가 등록되어 있지 않아 상세 리뷰 본문 추출이 스킵됨")
            return detailed_review_content, recommendation_intent

        try:
            # 4. 리뷰 페이지 진입 검증
            time.sleep(2)
            current_review_url = self.page.url
            if target_review_url and current_review_url.split('?')[0] == detail_page_url.split('?')[0]:
                print("  [review] [WARNING] 변경없는 URL 감지 (상세페이지 잔류) - 리뷰 URL 강제 이동 시도")
                self.page.get(target_review_url)
                time.sleep(3)
                self.close_survey_popup()

                if self.page.url.split('?')[0] == detail_page_url.split('?')[0]:
                    print(f"├─ [WARNING] 강제 이동을 시도했으나 여전히 상세페이지에 잔류되어 있습니다.")
                    raise Exception("Page load failed - stuck on detail page after forced redirect")

            # 5. 상세 리뷰 파싱
            for retry in range(1, 4):
                if retry > 1:
                    print(f"├─ 상세 리뷰 파싱 0건: 렌더링/로딩 지연 의심 - 5초 대기 중... ({retry}/3회차)")
                    time.sleep(5)

                page_html = self.page.run_js('return document.documentElement.outerHTML')
                bad_type = self.is_bad_page(page_html)
                if bad_type:
                    print(f"├─ [WARNING] 접속된 리뷰 페이지가 {'에러' if bad_type == 'error' else '빈'} 페이지입니다. (전체 에러로 전환)")
                    raise Exception("Page load failed - error page detected")

                tree = html.fromstring(page_html)
                detailed_review_list, matched_xpath_name = self.safe_extract_chain_list(tree, 'detailed_review_content')

                if not detailed_review_list:
                    if retry == 3:
                        print(f"[DEBUG] 상세 리뷰 요소를 찾을 수 없음 (총 3회 시도 실패 / XPath 매칭 0건)")
                    continue

                total_reviews_found = len(detailed_review_list)
                formatted_reviews = []
                for idx, review in enumerate(detailed_review_list[:20], 1):
                    review_text = review.text_content() if hasattr(review, 'text_content') else review
                    cleaned_review = ' '.join(str(review_text).split())
                    formatted_reviews.append(f"review{idx} - {cleaned_review}")

                detailed_review_content = ' ||| '.join(formatted_reviews)
                print(f"  [review] 상세 리뷰 추출: {len(formatted_reviews)}개 (전체 {total_reviews_found}개 중) [XPath: {matched_xpath_name}]")

                if recommendation_intent is None:
                    recommendation_intent = self.extract_recommendation_intent(tree, 'reviewpage_recommendation_intent')
                    if recommendation_intent:
                        print(f"├─ recommendation_intent (리뷰페이지 fallback): {recommendation_intent}")

                break
        except Exception as e:
            if "Page load failed" in str(e):
                raise e
            print(f"[DEBUG] 리뷰 본문 데이터를 추출하는 중 예외 에러 발생: {e}")

        return detailed_review_content, recommendation_intent
