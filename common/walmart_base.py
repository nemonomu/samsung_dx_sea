"""
Walmart crawler base helpers.

Walmart 전용 공통 동작을 기능 섹션과 Main / BSR / Detail 섹션으로 나눠 관리한다.
TV Walmart 크롤러부터 이 베이스를 사용한다.

섹션 구성:
1. Browser: setup_browser
2. Bot Detection / Session: handle_captcha, initialize_session
3. URL / Conversion: extract_item
4. DB: 현재 Walmart 전용 구현 없음
5. Main: Main 전용 공통 후보
6. BSR: BSR 전용 공통 후보
7. Detail: Detail 전용 공통 후보
"""

import os
import random
import re
import subprocess
import time
import traceback
import gc
from datetime import datetime
from lxml import html
from PIL import Image, ImageDraw, ImageFont

from DrissionPage import ChromiumPage, ChromiumOptions

from common.base_crawler import BaseCrawler
from common.data_extractor import extract_numeric_value


class WalmartBaseCrawler(BaseCrawler):
    # ========================================================================
    # Browser
    # ========================================================================
    # 함수 목록:
    # - setup_browser: DrissionPage 브라우저 설정
    def setup_browser(self):
        """DrissionPage browser setup with retry for Chrome/9222 startup races."""
        last_error = None

        for attempt in range(1, 4):
            try:
                print(f"[INFO] DrissionPage setup... (attempt {attempt}/3)")

                co = ChromiumOptions()
                co.set_pref('profile.managed_default_content_settings.images', 1)
                co.set_pref('profile.managed_default_content_settings.media_stream', 2)
                co.set_pref('profile.managed_default_content_settings.plugins', 2)
                co.set_argument('--autoplay-policy=document-user-activation-required')
                self.page = ChromiumPage(co)
                self.page.run_cdp('Network.enable')
                self.page.run_cdp('Network.setBlockedURLs', urls=[
                    '*://play.eko.com/*',
                    '*://assets.eko.com/*',
                ])
                self.page.set.window.max()

                print("[OK] DrissionPage setup completed")
                return True

            except Exception as e:
                last_error = e
                self.page = None
                print(f"[ERROR] Failed to setup browser (attempt {attempt}/3): {e}")
                traceback.print_exc()

                if attempt < 3:
                    subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'], capture_output=True)
                    wait_seconds = 5 * attempt
                    print(f"[INFO] Waiting {wait_seconds}s before browser setup retry...")
                    time.sleep(wait_seconds)

        print(f"[ERROR] Browser setup failed after retries: {last_error}")
        return False

    # ========================================================================
    # Bot Detection / Session
    # ========================================================================
    # 함수 목록:
    # - add_random_mouse_movements는 BaseCrawler 공통 함수 사용
    # - handle_captcha: PRESS & HOLD CAPTCHA 감지 및 수동 처리 대기
    # - initialize_session: example.com → walmart.com → 검색 흐름으로 세션 초기화
    def handle_captcha(self, return_false_on_detect=False):
        """PRESS & HOLD CAPTCHA가 보이면 수동 처리 시간을 둔다.

        return_false_on_detect=True이면 CAPTCHA 감지 후 False를 반환한다.
        Detail처럼 CAPTCHA 감지 시 기본 정보만 저장하고 상세 추출을 멈춰야 할 때 사용한다.
        """
        try:
            print("[INFO] Checking for CAPTCHA...")
            time.sleep(1)
            captcha_info = self.page.run_js("""
                const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                        rect.width > 0 && rect.height > 0;
                };
                const normalize = (value) => (value || '').toLowerCase().replace(/\\s+/g, ' ').trim();
                const bodyText = normalize(document.body && document.body.innerText ? document.body.innerText : '');
                const title = normalize(document.title);
                const url = normalize(location.href);
                const bodyStrongKeywords = [
                    'captcha',
                    'robot or human',
                    'are you a robot',
                    'human verification',
                    'verify you are human'
                ];
                const pageMetaStrongKeywords = [
                    ...bodyStrongKeywords,
                    'access denied',
                    'blocked'
                ];
                const pressHoldKeywords = ['press & hold', 'press and hold'];
                const bodyStrongHit = bodyStrongKeywords.find((keyword) => bodyText.includes(keyword)) || '';
                const pageMetaStrongHit = pageMetaStrongKeywords.find((keyword) => {
                    return title.includes(keyword) || url.includes(keyword);
                }) || '';
                const pageStrongHit = bodyStrongHit || pageMetaStrongHit;

                const selector = [
                    'button',
                    '[role="button"]',
                    'input[type="button"]',
                    'input[type="submit"]',
                    '[class*="captcha" i]',
                    '[id*="captcha" i]',
                    '[class*="challenge" i]',
                    '[id*="challenge" i]',
                    'iframe'
                ].join(',');
                const elementHits = [];
                for (const el of document.querySelectorAll(selector)) {
                    if (!isVisible(el)) continue;
                    const ownText = normalize(
                        el.tagName === 'IFRAME'
                            ? [el.getAttribute('title'), el.getAttribute('src')].filter(Boolean).join(' ')
                            : (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '')
                    );
                    const classAndId = normalize(`${el.id || ''} ${el.className || ''} ${el.getAttribute('name') || ''}`);
                    const textHit = [...pageMetaStrongKeywords, ...pressHoldKeywords].find((keyword) => ownText.includes(keyword));
                    const attrHit = pageMetaStrongKeywords.find((keyword) => classAndId.includes(keyword));
                    if (textHit || attrHit) {
                        elementHits.push({
                            keyword: textHit || attrHit,
                            text: ownText.slice(0, 240),
                            tag: el.tagName.toLowerCase()
                        });
                    }
                }

                const pressHoldElementHit = elementHits.find((hit) => {
                    return pressHoldKeywords.includes(hit.keyword) && hit.text.length <= 120;
                });

                return {
                    bodyText,
                    pageStrongHit,
                    elementHits,
                    matchedKeyword: pageStrongHit || (pressHoldElementHit ? pressHoldElementHit.keyword : '')
                };
            """) or {}
            visible_text = (captcha_info.get('bodyText') if isinstance(captcha_info, dict) else '') or ''
            matched_keyword = (captcha_info.get('matchedKeyword') if isinstance(captcha_info, dict) else '') or ''
            if matched_keyword:
                print(f"[WARNING] CAPTCHA signal found in page: {matched_keyword!r}")
                keyword_index = visible_text.find(matched_keyword)
                if keyword_index >= 0:
                    context_start = max(0, keyword_index - 120)
                    context_end = min(len(visible_text), keyword_index + len(matched_keyword) + 120)
                    keyword_context = visible_text[context_start:context_end].replace('\n', ' ').replace('\r', ' ')
                    print(f"[DEBUG] CAPTCHA visible keyword context: {keyword_context}")
                element_hits = captcha_info.get('elementHits', []) if isinstance(captcha_info, dict) else []
                if element_hits:
                    print(f"[DEBUG] CAPTCHA element signals: {element_hits[:3]}")
                print("[INFO] CAPTCHA detection - waiting 60 seconds for manual intervention...")
                print("[INFO] Please solve CAPTCHA manually if present")

                try:
                    capture_root = getattr(self, 'capture_base_dir', os.path.dirname(os.path.abspath(__file__)))
                    capture_dir = os.path.join(capture_root, 'capture_error', getattr(self, 'batch_id', None) or 'unknown')
                    os.makedirs(capture_dir, exist_ok=True)
                    screenshot_path = os.path.join(capture_dir, f"captcha_{int(time.time())}.png")
                    self.page.get_screenshot(path=screenshot_path)
                    print(f"[INFO] CAPTCHA screenshot saved: {screenshot_path}")
                except Exception as e:
                    print(f"[WARNING] CAPTCHA 스크린샷 저장 실패: {e}")

                time.sleep(60)
                return not return_false_on_detect

            print("[INFO] No CAPTCHA detected")
            return True

        except Exception as e:
            print(f"[WARNING] CAPTCHA handling error: {e}")
            return True

    def initialize_session(self, zip_code=None):
        """세션 초기화: example.com → walmart.com → 검색 → 자연스러운 브라우징."""
        try:
            if zip_code is None:
                zip_code = getattr(self, 'walmart_zip_code', None)
            search_keyword = getattr(self, 'walmart_search_keyword', 'tv')

            print("[INFO] 세션 초기화 중...")

            print("[INFO] Step 1/4: 중립 사이트 방문...")
            self.page.get('https://www.example.com')
            time.sleep(random.uniform(2, 4))
            self.add_random_mouse_movements()

            print("[INFO] Step 2/4: Walmart 메인 페이지 방문...")
            self.page.get('https://www.walmart.com')
            time.sleep(random.uniform(8, 12))
            self.handle_captcha()

            if getattr(self, 'skip_walmart_search', False):
                if zip_code:
                    self.set_walmart_zip_code(zip_code)
                print("[INFO] Walmart search step skipped; crawler will load listing URL directly")
                print("[OK] Session initialized")
                return True

            self.add_random_mouse_movements()
            for _ in range(random.randint(2, 4)):
                scroll_y = random.randint(200, 500)
                self.page.run_js(f"window.scrollBy(0, {scroll_y})")
                time.sleep(random.uniform(1, 2))
                self.add_random_mouse_movements()

            self.page.run_js("window.scrollTo(0, 0)")
            time.sleep(random.uniform(2, 3))

            if zip_code:
                self.set_walmart_zip_code(zip_code)

            print(f"[INFO] Step 4/4: 검색창에서 '{search_keyword}' 검색 시도...")
            try:
                search_selectors = [
                    "input[type='search']",
                    "input[aria-label*='Search']",
                    "input[placeholder*='Search']",
                    "input[name='q']",
                ]

                search_box = None
                for selector in search_selectors:
                    try:
                        search_box = self.page.ele(f'css:{selector}', timeout=5)
                        if search_box:
                            print(f"[OK] 검색창 발견: {selector}")
                            break
                    except Exception:
                        continue

                if search_box:
                    search_box.click()
                    time.sleep(random.uniform(2, 3))
                    search_box.input(search_keyword, clear=True)
                    time.sleep(random.uniform(3, 5))
                    search_box.input('\n')
                    time.sleep(random.uniform(8, 12))
                    self.handle_captcha()

                    for _ in range(2):
                        self.page.run_js(f"window.scrollBy(0, {random.randint(200, 400)})")
                        time.sleep(random.uniform(1, 2))

                    print("[OK] 검색 완료")
                else:
                    print("[WARNING] 검색창을 찾지 못함, 검색 단계 건너뜀")

            except Exception as e:
                print(f"[WARNING] 검색 실패 (계속 진행): {e}")

            print("[OK] 세션 초기화 완료")
            return True

        except Exception as e:
            print(f"[WARNING] 세션 초기화 실패 (계속 진행): {e}")
            return True

    def set_walmart_zip_code(self, zip_code):
        try:
            print(f"[INFO] Step 3/4: Walmart zipcode setting: {zip_code}")

            if zip_code in (self.page.html or ''):
                print(f"[OK] Walmart zipcode 이미 설정됨, 스킵: {zip_code}")
                return True

            banner = self._find_first_element([
                'css:button[data-automation-id="fulfillment-banner"]',
                'xpath://button[contains(., "Pickup or delivery")]',
            ], timeout=5)
            if not banner:
                print("[WARNING] Walmart fulfillment banner not found")
                return False

            banner.click()
            time.sleep(random.uniform(2, 3))

            store_button = self._find_first_element([
                'xpath://button[contains(., "Sacramento Gerber Rd Supercenter")]',
                'xpath://button[contains(., "8915 GERBER ROAD")]',
                'xpath://button[contains(., "Supercenter") and contains(., "95829")]',
                'xpath:(//div[@data-automation-id="fulfillment-address"][contains(., "Supercenter") or contains(., "GERBER ROAD")]//button)[1]',
            ], timeout=5)
            if store_button:
                store_button.click()
                time.sleep(random.uniform(2, 3))

            zip_input = self._find_zip_input(timeout=4)
            if not zip_input:
                for selector in [
                    'xpath://button[contains(., "Change")]',
                    'xpath://button[contains(., "Edit")]',
                    'xpath://button[contains(., "Find a store")]',
                ]:
                    action_button = self._find_first_element([selector], timeout=2)
                    if action_button:
                        action_button.click()
                        time.sleep(random.uniform(1, 2))
                        zip_input = self._find_zip_input(timeout=3)
                        if zip_input:
                            break

            if not zip_input:
                print("[WARNING] Walmart zipcode input not found")
                return False

            zip_input.click()
            time.sleep(random.uniform(0.5, 1))
            zip_input.input(zip_code, clear=True)
            time.sleep(random.uniform(3, 5))

            first_store = self._find_store_result_by_zip(zip_code, timeout=8)
            if first_store:
                first_store.click()
                time.sleep(random.uniform(1, 2))
            else:
                zip_input.input('\n')
                time.sleep(random.uniform(3, 5))
                first_store = self._find_store_result_by_zip(zip_code, timeout=5)
                if first_store:
                    first_store.click()
                    time.sleep(random.uniform(1, 2))
                else:
                    print(f"[WARNING] Walmart store result not found for zipcode: {zip_code}")
                    return False

            save_button = self._find_first_element([
                'css:button[data-automation-id="save-label"]',
                'xpath://button[contains(., "Save")]',
            ], timeout=5)
            if save_button:
                save_button.click()
            else:
                print("[WARNING] Walmart store save button not found")
                return False

            time.sleep(random.uniform(4, 6))
            self.handle_captcha()

            if zip_code in (self.page.html or ''):
                print(f"[OK] Walmart zipcode set: {zip_code}")
                return True

            print(f"[WARNING] Walmart zipcode set result not confirmed: {zip_code}")
            return False

        except Exception as e:
            print(f"[WARNING] Walmart zipcode setting failed: {e}")
            return False

    def _find_first_element(self, selectors, timeout=3):
        for selector in selectors:
            try:
                element = self.page.ele(selector, timeout=timeout)
                if element:
                    return element
            except Exception:
                continue
        return None

    def _find_zip_input(self, timeout=3):
        return self._find_first_element([
            'css:input[data-automation-id="store-zip-code"]',
            'css:input[data-automation-id="zip-code-input"]',
            'xpath://input[contains(@aria-label, "Zip") or contains(@aria-label, "zip") or contains(@placeholder, "Zip") or contains(@placeholder, "zip") or contains(@name, "zip") or contains(@name, "postal")]',
            'xpath://input[@type="tel"]',
            'xpath://input[@inputmode="numeric"]',
        ], timeout=timeout)

    def _find_store_result_by_zip(self, zip_code, timeout=3):
        return self._find_first_element([
            f'xpath:(//label[@data-automation-id="pickup-store" and contains(., "{zip_code}")])[1]',
            f'xpath:(//label[@data-automation-id="pickup-store" and contains(., "{zip_code}")]//input[@name="pickup-store"])[1]',
            f'xpath:(//*[@role="option" and contains(., "{zip_code}")])[1]',
            f'xpath:(//div[@data-testid="store-chooser-contents"]//button[contains(., "{zip_code}")])[1]',
            f'xpath:(//div[@data-automation-id="store-chooser-contents"]//button[contains(., "{zip_code}")])[1]',
        ], timeout=timeout)

    # ========================================================================
    # URL / Conversion
    # ========================================================================
    # 함수 목록:
    # - extract_item: Walmart 상품 URL에서 item ID 추출
    # - convert_first_number: Walmart 텍스트에서 첫 번째 숫자 추출 및 K/M 변환
    def extract_item(self, product_url):
        """Walmart URL에서 item ID를 추출한다."""
        if not product_url:
            return None
        try:
            ip_match = re.search(r'/ip/[^/]+/(\d+)', product_url)
            if ip_match:
                return ip_match.group(1)

            encoded_match = re.search(r'%2F(\d+)%3F', product_url)
            if encoded_match:
                return encoded_match.group(1)

            url_without_params = product_url.split('?')[0]
            last_segment = url_without_params.rstrip('/').split('/')[-1]
            number_match = re.search(r'(\d+)$', last_segment)
            if number_match:
                return number_match.group(1)
        except Exception as e:
            print(f"[WARNING] Failed to extract item: {e}")
        return None

    def convert_first_number(self, source, field_name=None):
        """
        첫 번째 숫자를 추출해 문자열로 반환한다.

        field_name을 전달하면 source에서 safe_extract_chain(source, field_name)으로 먼저 추출한다.
        K/M 접미사가 숫자 바로 뒤에 붙은 경우에만 1,000 / 1,000,000 단위로 변환한다.
        """
        raw = self.safe_extract_chain(source, field_name) if field_name else source
        if not raw:
            return None

        match = re.search(r'(\d[\d,]*(?:\.\d+)?)\s*([KkMm])?', raw)
        if not match:
            return None

        number_text = match.group(1).replace(',', '')
        suffix = match.group(2).upper() if match.group(2) else None

        if suffix:
            number = float(number_text) if '.' in number_text else int(number_text)
            multiplier = 1000000 if suffix == 'M' else 1000
            return str(int(number * multiplier))

        return number_text

    # ========================================================================
    # DB
    # ========================================================================
    # 함수 목록:
    # - 현재 Walmart 전용 구현 함수 없음
    # - is_product_excluded는 BaseCrawler 공통 함수 사용 (self.item_mst_table 기준)

    # ========================================================================
    # Main
    # ========================================================================
    # 함수 목록:
    # - restart_browser: Main 페이지 진행 중 브라우저 재시작
    # - scroll_to_bottom: 상품 리스트 lazy-load를 위한 하단 스크롤
    # - find_product_containers: 상품 컨테이너 검증 및 부족 시 스크롤 재파싱
    # - cleanup_old_capture_folders: 오래된 캡처 폴더 삭제
    # - capture_page_with_scroll: 스크롤하면서 1페이지 전체 화면 캡처
    # - 아래 항목은 Main 전용 공통 후보이며, 중복 제거 시 이 섹션으로 이동 예정
    def restart_browser(self, url=None):
        """브라우저 재시작."""
        try:
            print("\n" + "="*60)
            print("[INFO] 브라우저 재시작 중...")
            print("="*60)

            if self.page:
                try:
                    self.page.quit()
                except:
                    pass
                self.page = None

            subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'], capture_output=True)
            print("[INFO] 브라우저 종료 완료, 잠시 대기...")
            time.sleep(random.uniform(6, 10))

            if not self.setup_browser():
                print("[ERROR] 브라우저 재시작 실패")
                return False

            self.initialize_session()

            if url:
                print(f"[INFO] Accessing URL: {url}")
                self.page.get(url)
                time.sleep(random.uniform(8, 12))
                self.handle_captcha()

            print("[OK] 브라우저 재시작 완료\n")
            return True

        except Exception as e:
            print(f"[ERROR] 브라우저 재시작 실패: {e}")
            traceback.print_exc()
            return False

    def scroll_to_bottom(self):
        """스크롤: 사람처럼 자연스러운 스크롤 패턴으로 페이지 하단까지 이동한다."""
        try:
            same_position_count = 0
            previous_position = self.page.run_js(
                "return window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0"
            ) or 0

            while True:
                current_position = self.page.run_js(
                    "return window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0"
                ) or 0
                total_height = self.page.run_js("return document.body.scrollHeight")
                viewport_height = self.page.run_js("return window.innerHeight")

                if current_position >= total_height - viewport_height - 5:
                    break

                if random.random() < 0.7:
                    scroll_step = random.randint(200, 400)
                elif random.random() < 0.5:
                    scroll_step = random.randint(400, 700)
                else:
                    scroll_step = random.randint(80, 150)

                self.page.run_js(f"window.scrollBy(0, {scroll_step})")

                if random.random() < 0.15:
                    time.sleep(random.uniform(2.5, 4.5))
                elif random.random() < 0.25:
                    time.sleep(random.uniform(0.5, 1.0))
                else:
                    time.sleep(random.uniform(1.0, 2.0))

                if random.random() < 0.05:
                    time.sleep(random.uniform(3, 6))

                new_position = self.page.run_js(
                    "return window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0"
                ) or 0
                if new_position <= previous_position:
                    same_position_count += 1
                else:
                    same_position_count = 0
                if same_position_count >= 3:
                    break
                previous_position = new_position

            self.page.run_js("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(random.uniform(1.5, 3.0))

        except Exception as e:
            print(f"[ERROR] Scroll failed: {e}")
            traceback.print_exc()

    def find_product_containers(self, base_container_xpath, page_number, expected_products=40, label=None):
        """상품 컨테이너 검증: 파싱 → 부족하면 스크롤 → 재파싱을 최대 3회 수행한다."""
        base_containers = []
        log_label = f"Page {page_number}" if label is None else label

        for attempt in range(1, 4):
            page_html = self.page.html
            tree = html.fromstring(page_html)
            base_containers = tree.xpath(base_container_xpath)

            if len(base_containers) >= expected_products:
                break

            if attempt < 3:
                print(f"[WARNING] {log_label}: {len(base_containers)}/{expected_products} products, retrying ({attempt}/3)...")
                self.scroll_to_bottom()
                time.sleep(random.uniform(3, 5))

        return base_containers

    def retry_first_page_url_load_if_empty(self, base_containers, skip_url_load, url, base_container_xpath, page_number, expected_products=40):
        """첫 페이지 검색 결과에서 상품이 없으면 URL 직접 접근 후 다시 상품 컨테이너를 찾는다."""
        print("[WARNING] Page 1: 0 products found, URL 직접 접근 재시도...")
        self.page.get(url)
        time.sleep(random.uniform(10, 15))
        self.add_random_mouse_movements()
        self.handle_captcha()
        time.sleep(random.uniform(3, 5))

        return self.find_product_containers(
            base_container_xpath,
            page_number,
            expected_products,
            label=f"Page {page_number} (retry)"
        )

    def extract_sku_status(self, item):
        """sku_status_1/2 값을 추출해 하나의 sku_status 문자열로 합친다."""
        sku_status_1 = self.safe_extract_chain(item, 'sku_status_1')
        sku_status_2 = self.safe_extract_chain(item, 'sku_status_2')
        sku_status_parts = [status for status in [sku_status_1, sku_status_2] if status]
        return ', '.join(sku_status_parts) if sku_status_parts else None

    def cleanup_old_capture_folders(self, days=2):
        """오래된 캡처 폴더 삭제 (기본 2일 이상)."""
        try:
            if not os.path.exists(self.capture_base_dir):
                return

            today = datetime.now().date()
            deleted_count = 0

            for folder_name in os.listdir(self.capture_base_dir):
                folder_path = os.path.join(self.capture_base_dir, folder_name)
                if not os.path.isdir(folder_path):
                    continue

                try:
                    date_part = folder_name.split('_')[1]
                    folder_date = datetime.strptime(date_part, '%Y%m%d').date()

                    if (today - folder_date).days >= days:
                        import shutil
                        shutil.rmtree(folder_path)
                        deleted_count += 1
                        print(f"  [캡처] 오래된 폴더 삭제: {folder_name}")
                except (IndexError, ValueError):
                    continue

            if deleted_count > 0:
                print(f"  [캡처] 총 {deleted_count}개 폴더 삭제 완료")
        except Exception as e:
            print(f"  [캡처] 폴더 정리 실패: {e}")

    def capture_page_with_scroll(self):
        """스크롤하면서 전체 화면 캡처 (1페이지 전용)."""
        try:
            self.cleanup_old_capture_folders(days=2)

            capture_dir = os.path.join(self.capture_base_dir, self.batch_id)
            os.makedirs(capture_dir, exist_ok=True)

            self.page.run_js("window.scrollTo(0, 0);")
            time.sleep(1)

            capture_num = 1
            last_height = 0
            page_type = getattr(self, 'page_type', 'page')

            while True:
                filename = f"{page_type}_{self.batch_id}_{capture_num}.jpg"
                filepath = os.path.join(capture_dir, filename)
                temp_filepath = filepath.replace('.jpg', '_temp.png')
                self.page.get_screenshot(path=temp_filepath)

                img = Image.open(temp_filepath)
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                current_url = self.page.url if self.page else ''

                try:
                    font = ImageFont.truetype("arial.ttf", 48)
                except Exception:
                    font = ImageFont.load_default()

                padding = 16

                if current_url:
                    url_bbox = ImageDraw.Draw(img).textbbox((0, 0), current_url, font=font)
                    header_height = url_bbox[3] - url_bbox[1] + padding * 2
                    new_img = Image.new('RGB', (img.width, img.height + header_height), 'white')
                    new_img.paste(img, (0, header_height))
                    img = new_img
                    draw = ImageDraw.Draw(img)
                    draw.text((padding, padding), current_url, fill='black', font=font)
                else:
                    draw = ImageDraw.Draw(img)

                ts_bbox = draw.textbbox((0, 0), timestamp, font=font)
                footer_height = ts_bbox[3] - ts_bbox[1] + padding * 2
                footer_img = Image.new('RGB', (img.width, img.height + footer_height), 'white')
                footer_img.paste(img, (0, 0))
                img = footer_img
                draw = ImageDraw.Draw(img)
                ts_x = img.width - (ts_bbox[2] - ts_bbox[0]) - padding
                ts_y = img.height - footer_height + padding
                draw.text((ts_x, ts_y), timestamp, fill='black', font=font)
                img = img.convert('RGB')
                img.save(filepath, 'JPEG', quality=85)
                del img
                gc.collect()

                if os.path.exists(temp_filepath):
                    os.remove(temp_filepath)
                print(f"  [캡처] {filename} 저장 완료")

                self.page.run_js("window.scrollBy(0, 800)")
                time.sleep(1)

                new_height = self.page.run_js("return window.pageYOffset")
                if new_height == last_height:
                    break
                last_height = new_height
                capture_num += 1

            print(f"  [캡처] 총 {capture_num}장 캡처 완료")
        except Exception as e:
            print(f"  [캡처] 실패: {e}")
            traceback.print_exc()

    # ========================================================================
    # BSR
    # ========================================================================
    # 함수 목록:
    # - scroll_to_bottom: Main/BSR 공통 스크롤 함수 사용
    # - capture_page_with_scroll: Main/BSR 공통 캡처 함수 사용
    # - build_db_item_cache: product_list 테이블에서 item 기준 캐시 생성
    # - 아래 항목은 BSR 전용 공통 후보이며, 중복 제거 시 이 섹션으로 이동 예정
    # BSR 전용 공통 후보:
    def build_db_item_cache(self, product_list_table, value_field='id'):
        """product_list 테이블에서 현재 batch_id의 URL을 조회해 item 기준 dict를 만든다.

        value_field:
        - 'id': {item: row_id}
        - 'product_url': {item: product_url}
        """
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                f"""
                SELECT id, product_url FROM {product_list_table}
                WHERE account_name = %s AND batch_id = %s
                ORDER BY id ASC
                """,
                (self.account_name, self.batch_id),
            )
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
                    print(f"[WARNING] Duplicate item in DB batch: item={item}, keep_value={db_item_map[item]}, skip_id={row_id}")
                    continue
                db_item_map[item] = row_id if value_field == 'id' else db_url

            print(f"[INFO] DB item cache loaded: {len(db_item_map)} items")
            return db_item_map

        except Exception as e:
            print(f"[WARNING] build_db_item_cache failed: {e}")
            return {}

    # ========================================================================
    # Detail
    # ========================================================================
    # 함수 목록:
    # - extract_rating_from_header
    # - extract_ratings_count
    # - extract_review_count_fallback
    # - extract_review_count
    # - extract_star_rating
    # - load_detail_page
    # - handle_detail_blocking_pages
    # - handle_sorry_page
    # - extract_final_sku_price
    # - click_to_navigate
    # - navigate_to_next_review_page
    # - open_spec_modal
    def extract_rating_from_header(self, tree):
        """상단 reviews-and-ratings 영역에서 별점과 별점 수 추출."""
        try:
            # 1. 리뷰 없음 텍스트 감지
            no_ratings_text = self.safe_extract_chain(tree, 'no_ratings_yet')
            if no_ratings_text:
                no_ratings_text_clean = no_ratings_text.replace('(', '').replace(')', '').strip()
                if 'No ratings yet' in no_ratings_text_clean:
                    return True, None, None

            # 2. header rating 추출 안되는 경우
            text = self.safe_extract_chain(tree, 'header_rating')
            if not text:
                return False, None, None
            
            # 3. header rating 추출되는 경우
            # 패턴1: "4.3 stars out of 8968 reviews"
            match = re.match(r'([\d.]+)\s*stars?\s*out\s*of\s*([\d,]+)\s*reviews?', text, re.IGNORECASE)
            # 패턴2: "4.3 out of 5 stars rating, 18177 ratings" (aria-label)
            if not match:
                match = re.match(r'([\d.]+)\s*out\s*of\s*\d+\s*stars?\s*rating[,\s]*([\d,]+)\s*ratings?', text, re.IGNORECASE)

            if match:
                star_rating = match.group(1)
                try:
                    count_of_star_ratings = '{:,}'.format(int(match.group(2).replace(',', '')))
                except ValueError:
                    count_of_star_ratings = match.group(2)
                return False, star_rating, count_of_star_ratings

            return False, None, None
        except Exception as e:
            print(f"[WARNING] extract_rating_from_header failed: {e}")
            traceback.print_exc()
            return False, None, None

    def extract_ratings_count(self, tree):
        """Walmart 별점 개수 추출 (예: '1,234 ratings' → '1,234', '12.5K ratings' → '12.5K')."""
        text = self.safe_extract_chain(tree, 'count_of_star_ratings')
        if text:
            match = re.search(r'([\d,]+\.?\d*K?)', text, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def extract_review_count_fallback(self, tree, xpath_key):
        """"Showing X-Y of N reviews" h3 패턴에서 N(정확한 정수) 추출
        Args:
            xpath_key: 'detail_page_count_of_reviews'(2차/상세) 또는 'review_page_count_of_reviews'(3차/리뷰페이지)
        """
        text = self.safe_extract_chain(tree, xpath_key)
        if not text:
            return None
        match = re.search(r'of\s+([\d,]+)\s+reviews?', text, re.IGNORECASE)
        return match.group(1) if match else None

    def extract_review_count(self, tree):
        """Walmart 리뷰 개수 추출 — 상세페이지에서 1차+2차 처리
        1차: count_of_reviews chain → 예: '3.5K reviews' → '3.5K'
        2차: 1차 결과에 K/M 포함 시 detail_page_count_of_reviews로 정확한 정수 재추출
             예: '18.7K' → "Showing 1-10 of 18,742 reviews" → '18,742'

        (3차는 caller가 리뷰페이지 진입 후 extract_review_count_fallback에 'review_page_count_of_reviews' 키로 호출)
        """
        # 1차: count_of_reviews chain (count_of_reviews_* 자동 fallback 포함)
        text = self.safe_extract_chain(tree, 'count_of_reviews')
        if not text:
            return None

        match = re.search(r'([\d,]+\.?\d*[KM]?)', text, re.IGNORECASE)
        if not match:
            return None

        primary_count = match.group(1)

        # 2차: K/M 포함 시 detail_page_count_of_reviews(상세페이지 키)로 정확한 정수 재추출
        if 'K' in primary_count.upper() or 'M' in primary_count.upper():
            exact_count = self.extract_review_count_fallback(tree, 'detail_page_count_of_reviews')
            if exact_count:
                print(f"  [2차 detail_page_count_of_reviews 상세] K/M({primary_count}) → 정수({exact_count}) 재추출 성공")
                return exact_count
            print(f"  [2차 detail_page_count_of_reviews 상세] 실패 → 1차 값 유지: {primary_count}")

        return primary_count

    def extract_star_rating(self, tree):
        """Walmart 별점 추출 (예: '4.5 out of 5 stars' → '4.5')"""
        # chain: star_rating → star_rating_* 자동 시도
        text = self.safe_extract_chain(tree, 'star_rating')
        return extract_numeric_value(text, include_comma=False, include_decimal=True)

    def extract_loaded_product_name(self):
        try:
            page_html = self.page.run_js('return document.documentElement.outerHTML')
            tree = html.fromstring(page_html)
        except Exception:
            return None

        return self.extract_first_text_from_tree(tree, [
            "//*[@data-testid='product-title']//text()",
            "//*[@data-automation-id='product-title']//text()",
            "//h1//text()",
        ])

    def load_detail_page(self, product_url, listing_product_name=None):
        """Walmart 상세 상품 페이지를 로드하고 기본 URL 검증 및 썸네일 전환을 수행한다."""
        # 현재 URL 저장 (로드 전)
        previous_url = self.page.url if self.page else None

        # 상품 상세 페이지 로드
        self.page.get(product_url)
        time.sleep(random.uniform(1.5, 2.5))

        # 로드 확인 (3가지 체크)
        current_url = self.page.url
        page_load_success = True

        # 체크 1: 이전 URL과 동일하면 로드 실패
        if previous_url and current_url == previous_url:
            print(f"[WARNING] 페이지 로드 실패 감지 (URL 변경 없음)")
            page_load_success = False

        # 체크 2: walmart.com 도메인인지 확인
        if page_load_success and 'walmart.com' not in current_url:
            print(f"[WARNING] 페이지 로드 실패 감지 (walmart.com 아님)")
            page_load_success = False

        if not page_load_success:
            raise Exception("Page load failed - URL validation failed")

        # 체크 3: 다른 item으로 리다이렉트되었는지 확인
        is_redirect = False
        original_item = self.extract_item(product_url)
        current_item = self.extract_item(current_url)
        if original_item and current_item and original_item != current_item:
            detail_product_name = self.extract_loaded_product_name()
            print(f"[WARNING] Redirect detected: {original_item} -> {current_item}")
            if self.redirect_product_names_match(listing_product_name, detail_product_name):
                print("[INFO] Redirect allowed: listing/detail product names match exactly")
                is_redirect = True
            else:
                print(f"[WARNING] Redirect rejected: listing={listing_product_name or '-'} | detail={detail_product_name or '-'}")
                raise Exception("Redirect detected")

        # 첫 번째 썸네일이 동영상이면 → 이미지 썸네일 JS 클릭하여 전환
        try:
            first_thumbnail = self.page.ele('xpath://button[@data-testid="item-page-vertical-carousel-hero-image-button"]', timeout=3)
            if first_thumbnail:
                first_alt = first_thumbnail.ele('xpath:.//img').attr('alt') or ''
                if 'video' in first_alt.lower():
                    img_thumbnail = self.page.ele('xpath://button[@data-testid="item-page-vertical-carousel-hero-image-button" and .//img[contains(@alt, "thumbnail image") and not(contains(@alt, "video"))]]', timeout=2)
                    if img_thumbnail:
                        img_thumbnail.click(by_js=True)
                        time.sleep(0.5)
                        print(f"  [동영상→이미지 썸네일 전환] 완료")
        except Exception as e:
            print(f"  [WARNING] 동영상→이미지 썸네일 전환 실패: {e}")

        return current_url, is_redirect

    def handle_detail_blocking_pages(self, product):
        """Detail 추출 전 CAPTCHA/Sorry 페이지를 처리한다."""
        try:
            if not self.handle_captcha(return_false_on_detect=True):
                print("[WARNING] CAPTCHA 감지 - 상세 필드 추출 없이 product 기본 정보만 저장")
                product['crawl_datetime'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                return product
        except TimeoutError:
            print(f"  [WARNING] CAPTCHA 체크 DOM 타임아웃 - 제품 스킵")
            raise Exception("DOM timeout - skip product")

        try:
            if not self.handle_sorry_page():
                print(f"  [WARNING] Sorry 페이지 해결 실패 - 제품 스킵")
                raise Exception("DOM timeout - sorry page unresolved")
        except TimeoutError:
            print(f"  [WARNING] Sorry 체크 DOM 타임아웃 - 제품 스킵")
            raise Exception("DOM timeout - skip product")

        return None

    # Walmart 'Sorry / technical issues' 페이지 실제 문구 (정확한 매칭)
    SORRY_KEYWORDS = (
        "we're having technical issues",
        "we'll be back in a flash",
        "this page isn't available right now",
        "this page isn't available",
    )

    def is_sorry_page(self, page_html=None):
        """Walmart Sorry 페이지 여부 검사. page_html을 주면 재-fetch 없이 그 HTML로 판단한다."""
        content = page_html if page_html is not None else self.page.html
        content = (content or '').lower()
        return any(keyword in content for keyword in self.SORRY_KEYWORDS)

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
            # Sorry 페이지 체크 (정상 페이지면 바로 리턴)
            if not self.is_sorry_page():
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
                    capture_root = getattr(self, 'capture_base_dir', os.path.dirname(os.path.abspath(__file__)))
                    capture_dir = os.path.join(capture_root, 'capture_error', getattr(self, 'batch_id', None) or 'unknown')
                    os.makedirs(capture_dir, exist_ok=True)
                    screenshot_path = os.path.join(capture_dir, f"sorry_page_failed_{int(time.time())}.png")
                    self.page.get_screenshot(path=screenshot_path)
                    print(f"[INFO] 스크린샷 저장됨: {screenshot_path}")
                except Exception as e:
                    print(f"[WARNING] Sorry 페이지 스크린샷 저장 실패: {e}")

                return False

            return True

        except Exception as e:
            print(f"[WARNING] Sorry page handling error: {e}")
            traceback.print_exc()
            return True  # 에러 발생해도 계속 진행

    def extract_final_sku_price(self, tree):
        """final_sku_price 추출 실패 시 Walmart 상태별 fallback 값을 반환한다."""
        field_names = [
            'final_sku_price',
            'not_available',
            'see_more_seller_options',
        ]
        for field_name in field_names:
            value = self.safe_extract_chain(tree, field_name)
            if not value:
                continue
            if field_name == 'final_sku_price':
                return value.replace('Now ', '').strip()
            return value


        return None

    def extract_detailed_reviews(self, item, count_of_reviews, capture_allowed=False):
        """리뷰 버튼 진입부터 상세 리뷰/리뷰 수 보정까지 공통 처리한다."""
        detailed_review_content = None
        review_click_method = None
        review_pages_crawled = 0

        # chain: reviews_button + 모든 reviews_button_* 키를 (이름, xpath) 튜플 리스트로 자동 수집
        reviews_button_xpaths = self.get_chain_xpaths('reviews_button')

        # count_of_reviews가 '0'이면 상세 리뷰 추출 스킵
        if count_of_reviews == '0' or count_of_reviews == 0:
            print(f"  [리뷰 0건 - 상세 리뷰 추출 스킵]")
            return detailed_review_content, count_of_reviews

        if not reviews_button_xpaths:
            print(f"  [리뷰 버튼 XPath 없음 - 상세 리뷰 추출 스킵]")
            return detailed_review_content, count_of_reviews

        # 클릭 전 URL 저장
        url_before_click = self.page.url

        # 1단계: 리뷰 버튼 탐색 — helper로 DOM 우선, 못 찾으면 스크롤
        review_button, matched_xpath_name = self.scroll_find_element(
            reviews_button_xpaths, max_scrolls=10, label='리뷰 버튼 탐색', return_element=True
        )

        # 2단계: 버튼 발견 시 클릭으로 리뷰 페이지 진입 (일반 → JS → href fallback)
        review_button_found = False
        if review_button:
            time.sleep(random.uniform(1.5, 2))
            review_button_found, review_click_method = self.click_to_navigate(
                review_button, url_before_click, label='리뷰 버튼', matched_xpath_name=matched_xpath_name
            )

        if not review_button_found:
            print(f"  [리뷰 버튼 미발견 - 상세 리뷰 추출 스킵]")
            return detailed_review_content, count_of_reviews

        print(f"  [리뷰 버튼 클릭] {review_click_method} → [OK] 진입 성공")

        try:
            # 캡처 3: 리뷰 페이지 진입 후
            if capture_allowed:
                self.take_capture(item, 3)

            all_reviews = []
            current_page = 1
            max_reviews = 20
            prev_first_review = None  # 페이지네이션 stale-read 방지: 직전 페이지 첫 리뷰

            while len(all_reviews) < max_reviews:
                # 페이지 진입/페이지네이션 직후: 고정 sleep(3~5) 대신 리뷰가 렌더될 때까지만 대기
                # (상한 5s = 기존 최대 대기와 동일 → 안정성 유지, 준비되면 즉시 진행)
                self.wait_for_review_page_ready(prev_first_review, timeout=5)

                # 전체 HTML은 한 번만 취득해 sorry 감지 + 파싱에 재사용.
                # sorry는 드무므로 감지됐을 때만 복구 루틴을 돌리고 최신 HTML을 다시 취득한다.
                page_html = self.page.run_js('return document.documentElement.outerHTML')
                if self.is_sorry_page(page_html):
                    if not self.handle_sorry_page():
                        print(f"  [WARNING] 리뷰 페이지 {current_page} Sorry 감지 - 리뷰 수집 중단")
                        break
                    page_html = self.page.run_js('return document.documentElement.outerHTML')

                tree = html.fromstring(page_html)

                # 페이지 1: 3차 count_of_reviews 재추출 (K/M 또는 미추출 케이스)
                if current_page == 1:
                    is_missing = not count_of_reviews
                    has_km = bool(count_of_reviews) and ('K' in str(count_of_reviews).upper() or 'M' in str(count_of_reviews).upper())
                    if is_missing or has_km:
                        exact_count = self.extract_review_count_fallback(tree, 'review_page_count_of_reviews')
                        reason = "미추출" if is_missing else f"K/M({count_of_reviews})"
                        if exact_count:
                            print(f"  [3차 review_page_count_of_reviews 리뷰페이지] {reason} → 정수({exact_count}) 재추출 성공")
                            count_of_reviews = exact_count
                        else:
                            print(f"  [3차 review_page_count_of_reviews 리뷰페이지] 실패 → 기존 값 유지: {count_of_reviews}")

                # chain_list: detailed_review_content + 모든 detailed_review_content_* 키 자동 시도
                # 반환값 (요소 리스트, 매칭된 xpath 키 이름) 중 매칭 이름은 함수 내부에서 이미 로그로 찍어주므로 _ 로 버림
                reviews_list, _ = self.safe_extract_chain_list(tree, 'detailed_review_content')
                page_review_count = 0

                if reviews_list:
                    # 다음 페이지 stale-read 판정 기준: 이 페이지의 첫 리뷰 텍스트
                    _first = reviews_list[0]
                    _first_text = _first.text_content() if hasattr(_first, 'text_content') else _first
                    prev_first_review = ' '.join(str(_first_text).split())
                    for review in reviews_list:
                        if len(all_reviews) >= max_reviews:
                            break

                        if hasattr(review, 'text_content'):
                            review_text = review.text_content()
                        else:
                            review_text = review

                        cleaned_review = ' '.join(review_text.split())
                        all_reviews.append(cleaned_review)
                        page_review_count += 1

                print(f"  [리뷰 추출] page{current_page}: {page_review_count}건 (누적 {len(all_reviews)})")

                if len(all_reviews) >= max_reviews:
                    break

                # 다음 페이지로 이동 (helper) — 실패 시 수집된 리뷰로 종료
                next_page_num = current_page + 1
                if not self.navigate_to_next_review_page(next_page_num):
                    break
                current_page = next_page_num

            review_pages_crawled = current_page
            if all_reviews:
                formatted_reviews = [f"review{idx} - {review}" for idx, review in enumerate(all_reviews, 1)]
                detailed_review_content = ' ||| '.join(formatted_reviews)
        except Exception as e:
            print(f"  [WARNING] Failed to extract reviews: {e}")

        review_count = detailed_review_content.count(' ||| ') + 1 if detailed_review_content else 0
        print(f"  → reviews: 버튼클릭 O ({review_click_method}), {review_pages_crawled}페이지, {review_count}개 수집")
        return detailed_review_content, count_of_reviews

    def wait_for_review_page_ready(self, prev_first_review=None, timeout=5, poll=0.3):
        """리뷰 페이지 진입/페이지네이션 직후, 리뷰 컨텐츠가 실제로 렌더될 때까지만 대기한다.

        기존 고정 sleep(3~5) 대체용. 상한(timeout)은 기존 최대 대기와 동일하게 두어
        느린 페이지도 동일하게 기다리므로 수집 안정성은 유지하고, 렌더가 끝나면 즉시 진행한다.
        prev_first_review가 주어지면(=페이지네이션) 첫 리뷰가 그 값과 달라질 때까지 기다려
        이전 페이지 내용을 다시 읽는 stale-read를 방지한다.

        Returns:
            bool: 리뷰 컨텐츠 감지 시 True, timeout까지 못 뜨면 False(호출부는 기존대로 진행).
        """
        review_xpaths = self.get_chain_xpaths('detailed_review_content')
        if not review_xpaths:
            # 리뷰 xpath가 없으면 판단 근거가 없으므로 기존 방식(고정 대기) 유지
            time.sleep(random.uniform(3, 5))
            return False

        end = time.time() + timeout
        while time.time() < end:
            for _name, xpath in review_xpaths:
                try:
                    elem = self.page.ele(f'xpath:{xpath}', timeout=0)
                except Exception:
                    elem = None
                if not elem:
                    continue
                try:
                    text = elem.text
                except Exception:
                    text = None
                norm = ' '.join(text.split()) if text else ''
                # 페이지1(prev 없음): 리뷰가 보이면 바로 준비 완료
                # 페이지네이션: 첫 리뷰가 이전 페이지와 달라졌을 때만 준비 완료
                if prev_first_review is None or (norm and norm != prev_first_review):
                    return True
                break  # 아직 이전 페이지 내용 → 더 기다림
            time.sleep(poll)
        return False

    def click_to_navigate(self, element, url_before, label, matched_xpath_name=None):
        """요소 클릭으로 페이지 이동 유도 (URL 변경으로 성공 판정)."""
        name_suffix = f", {matched_xpath_name}" if matched_xpath_name else ""

        try:
            element.click()
        except Exception as e:
            print(f"  [WARNING] {label} 일반클릭 실패: {e}")
        time.sleep(2)
        if self.page.url != url_before:
            time.sleep(random.uniform(2, 3))
            return True, f"일반클릭{name_suffix}"

        try:
            self.page.run_js('arguments[0].click()', element)
        except Exception as e:
            print(f"  [WARNING] {label} JS클릭 실패: {e}")
        time.sleep(2)
        if self.page.url != url_before:
            time.sleep(random.uniform(2, 3))
            return True, f"JS클릭{name_suffix}"

        try:
            href = element.attr('href')
            if href:
                if href.startswith('/'):
                    href = f"https://www.walmart.com{href}"
                self.page.get(href)
                time.sleep(random.uniform(2, 3))
                return True, f"href이동{name_suffix}"
        except Exception as e:
            print(f"  [WARNING] {label} href 이동 실패: {e}")

        return False, None

    def navigate_to_next_review_page(self, next_page_num):
        """리뷰 페이지네이션 — 다음 페이지로 이동."""
        review_pagination_template = self.xpaths.get('review_pagination', {}).get('xpath', '')
        if not review_pagination_template:
            return False
        next_page_xpath = review_pagination_template.replace('{page_num}', str(next_page_num))

        next_page_button, _ = self.scroll_find_element(
            [('review_pagination', next_page_xpath)],
            max_scrolls=2,
            label=f'페이지네이션 버튼(page {next_page_num}) 탐색',
            return_element=True,
            scroll_px=(100, 200),
        )
        if not next_page_button:
            return False

        try:
            time.sleep(random.uniform(0.5, 1))
            next_page_button.click()
            return True
        except Exception as e:
            print(f"  [WARNING] 페이지네이션 클릭/이동 실패 (page {next_page_num}): {e}")
            return False

    def open_spec_modal(self):
        """More details 버튼 클릭 후 모달 열림을 확인한다."""
        more_details_button_xpath = self.xpaths.get('more_details_button', {}).get('xpath')
        if not more_details_button_xpath:
            print(f"  [WARNING] DB에 'more_details_button' XPath 미등록 → 모달 열기 스킵")
            return None

        more_details_found = False
        last_md_error = None
        for retry in range(1, 4):
            try:
                more_details_button = self.page.ele(f'xpath:{more_details_button_xpath}', timeout=2)
                if more_details_button:
                    more_details_button.click()
                    more_details_found = True
                    print(f"  [More details 버튼] {retry}차 시도 → [OK] 클릭")
                    break
            except Exception as e:
                last_md_error = e
                time.sleep(0.5)

        if not more_details_found:
            if last_md_error:
                print(f"  [WARNING] More details 버튼 클릭 중 마지막 예외: {last_md_error}")
            print(f"  [More details 버튼] 3회 시도 → [FAIL] 클릭 실패")
            return None

        modal_opened_xpath = self.xpaths.get('modal_opened', {}).get('xpath')
        if not modal_opened_xpath:
            print(f"  [WARNING] DB에 'modal_opened' XPath 미등록 → 모달 열림 확인 실패")
            return None

        for attempt in range(1, 4):
            time.sleep(random.uniform(1, 2))
            modal_tree = html.fromstring(self.page.html)
            if modal_tree.xpath(modal_opened_xpath):
                print(f"  [모달 열림 확인] {attempt}차 시도 → [OK]")
                return modal_tree
            print(f"  [모달 열림 확인] {attempt}차 시도 → [FAIL] 1~2초 대기 후 재파싱")

        print(f"  [모달 열림 확인] 3회 시도 → [FAIL] 모달 열리지 않음")
        return None
