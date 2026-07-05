"""
Amazon 공통 베이스 크롤러 (DrissionPage 기반)

================================================================================
구조
================================================================================
- BaseCrawler 를 상속해서 모든 Amazon 워커가 공유하는 메서드를 모아둠
- 각 워커(Main/BSR/Detail)는 AmazonBaseCrawler 를 상속해서 고유 로직만 구현
- 인스턴스 변수로 product_line 별 차이 흡수:
    self.item_mst_table   — is_product 조회 테이블 (예: 'tv_item_mst', 'hhp_item_mst')
    self.capture_base_dir — 캡처 저장 베이스 경로
    self.page_type        — 캡처 파일 prefix 로 사용 ('main', 'bsr', 'dt' 등)

================================================================================
포함된 공통 메서드
================================================================================
- 브라우저 관리: setup_browser, restart_browser, get_chrome_version
- 봇 감지/우회: is_sorry_page, check_and_handle_sorry_page,
                is_throttled, check_and_handle_throttling,
                add_random_mouse_movements, scroll_to_bottom
- 캡처:        capture_page_with_scroll, cleanup_old_capture_folders
- URL/변환:    extract_item, convert_first_number
- DB:          is_product_excluded
"""

import os
import re
import time
import random
import traceback
from DrissionPage import ChromiumPage, ChromiumOptions
from lxml import html

from common.base_crawler import BaseCrawler


class AmazonBaseCrawler(BaseCrawler):
    """Amazon TV/HHP 공통 DrissionPage 기반 베이스 크롤러.

    하위 클래스의 __init__에서 다음 인스턴스 변수를 설정해야 한다:
        self.item_mst_table   - is_product 조회 테이블명
        self.capture_base_dir - 캡처 파일 저장 기본 경로
        self.page_type        - 'main'/'bsr'/'dt' 등 캡처 파일 prefix
        self.account_name     - 'Amazon'
        self.page             - DrissionPage 객체
    """

    # ====================================================================
    # Browser
    # ====================================================================
    def click_element(self, element, label='element'):
        """Click an element, falling back to JS click when it has no visible box."""
        try:
            element.click()
            return True
        except Exception as e:
            print(f"[WARNING] {label} 일반 클릭 실패, JS 클릭 시도: {e}")
            try:
                element.click(by_js=True)
                return True
            except Exception as js_e:
                print(f"[WARNING] {label} JS 클릭 실패: {js_e}")
                return False

    def setup_browser(self):
        """DrissionPage 브라우저를 설정한다."""
        try:
            print("[INFO] DrissionPage 설정 중...")

            co = ChromiumOptions()
            # 신뢰 프로필 지정 (opt-in): 리뷰 로그인 게이트는 세션(쿠키 이력) 단위로
            # 걸리는데, 일반 사용 이력의 익명 프로필은 통과한다(2026-07-05 RDP 실측).
            # browser_user_data_dir에 그런 프로필의 "사본" 경로를 주면 그 세션 상태로
            # 크롤링한다. 원본 Chrome과의 충돌을 피하려고 반드시 사본 + 별도 포트 사용.
            trusted_profile = getattr(self, 'browser_user_data_dir', None)
            if trusted_profile:
                co.set_user_data_path(trusted_profile)
                co.auto_port()
                print(f"[INFO] 신뢰 프로필로 브라우저 실행: {trusted_profile}")
            # 이미지 허용, 미디어/플러그인 로드는 비활성화
            co.set_pref('profile.managed_default_content_settings.images', 1)
            co.set_pref('profile.managed_default_content_settings.media_stream', 2)
            co.set_pref('profile.managed_default_content_settings.plugins', 2)
            # 미디어 자동 재생 차단
            co.set_argument('--autoplay-policy=document-user-activation-required')
            # 백그라운드/비포커스 상태에서 JS 타이머·렌더러 스로틀링 방지 (SIEL 검증 세트).
            # RDP 창이 가려지거나 세션이 백그라운드일 때 lazy-load(리뷰 위젯 등)가
            # 멈춰서 추출이 비는 문제를 차단한다.
            co.set_argument('--disable-background-timer-throttling')
            co.set_argument('--disable-backgrounding-occluded-windows')
            co.set_argument('--disable-renderer-backgrounding')
            co.set_argument('--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling')
            self.page = ChromiumPage(co)
            self.page.set.window.max()
            # 포커스 없는 창에서도 focus 상태로 에뮬레이션 — lazy-load 트리거 유지 (SIEL 패턴)
            try:
                self.page.run_cdp('Emulation.setFocusEmulationEnabled', enabled=True)
            except Exception:
                pass

            # 주의: 쿠키/캐시 선제 삭제를 여기 넣지 말 것. 2026-07-05 실측 결과
            # amazon.com은 쿠키 없는 신선한 세션에 리뷰 로그인 게이트를 "즉시" 씌운다
            # (누적 프로필은 정상 → 쿠키 삭제 직후 게이트 → 워밍업 40초로도 미해제).
            # SIEL(UC, 매 런 임시 프로필)이 무사한 것은 amazon.in에 이 게이트가 없기 때문.

            print("[OK] DrissionPage 설정 완료")

            zip_code = getattr(self, 'amazon_zip_code', '10001')
            if not self.set_amazon_zip_code(zip_code):
                print("[ERROR] Amazon ZIP code 설정 실패")
                return False
            return True

        except Exception as e:
            print(f"[ERROR] Failed to setup browser: {e}")
            traceback.print_exc()
            return False

    def set_amazon_zip_code(self, zip_code='10001', max_retries=3):
        """Amazon 배송 지역 ZIP code를 설정한다."""
        for attempt in range(1, max_retries + 1):
            try:
                print(f"[INFO] Amazon ZIP code 설정 중: {zip_code} (시도 {attempt}/{max_retries})")
                self.page.get('https://www.amazon.com')
                time.sleep(random.uniform(3, 5))

                delivery_link = self.page.ele('css:#nav-global-location-popover-link', timeout=10)
                if not delivery_link:
                    print("[WARNING] 배송지 링크를 찾지 못했습니다.")
                    self.page.refresh()
                    time.sleep(random.uniform(3, 5))
                    continue

                current_location = self.get_element_text(delivery_link)
                if zip_code in current_location:
                    print(f"[OK] Amazon ZIP code 이미 설정됨: {zip_code} ({current_location})")
                    return True

                if not self.click_element(delivery_link, '배송지 링크'):
                    self.page.refresh()
                    time.sleep(random.uniform(3, 5))
                    continue
                time.sleep(random.uniform(2, 3))

                zip_input = self.page.ele('css:#GLUXZipUpdateInput', timeout=10)
                if not zip_input:
                    print("[WARNING] ZIP 입력 필드를 찾지 못했습니다.")
                    continue

                zip_input.clear()
                zip_input.input(zip_code)
                time.sleep(random.uniform(1, 2))

                apply_button = self.page.ele('css:#GLUXZipUpdate input[type="submit"]', timeout=5)
                if not apply_button:
                    apply_button = self.page.ele('css:#GLUXZipUpdate-announce', timeout=3)

                if not apply_button:
                    print("[WARNING] ZIP 적용 버튼을 찾지 못했습니다.")
                    continue

                if not self.click_element(apply_button, 'ZIP 적용 버튼'):
                    continue
                time.sleep(random.uniform(3, 5))

                close_selectors = [
                    'css:.a-popover-footer button[name="glowDoneButton"]',
                    'css:.a-popover-header button[data-action="a-popover-close"]',
                    'css:.a-popover-footer button',
                    'css:#GLUXConfirmClose',
                ]
                for selector in close_selectors:
                    close_button = self.page.ele(selector, timeout=2)
                    if close_button and self.click_element(close_button, 'ZIP 팝업 닫기 버튼'):
                        time.sleep(random.uniform(1, 2))
                        break

                print(f"[OK] Amazon ZIP code 설정 완료: {zip_code}")
                return True

            except Exception as e:
                print(f"[WARNING] Amazon ZIP code 설정 실패 (시도 {attempt}/{max_retries}): {e}")
                try:
                    self.page.refresh()
                    time.sleep(random.uniform(3, 5))
                except Exception:
                    pass

        return False

    def get_element_text(self, element):
        """DrissionPage element의 표시 텍스트를 안전하게 반환한다."""
        try:
            text = element.text
            return text.strip() if text else ''
        except Exception:
            return ''

    def restart_browser(self, url=None):
        """브라우저를 종료한 뒤 새로 실행한다. url이 있으면 해당 URL에 접근한다."""
        try:
            print("[INFO] Closing browser...")
            if self.page:
                try:
                    self.page.quit()
                except Exception:
                    pass
                self.page = None

            print("[INFO] Waiting before restart...")
            time.sleep(random.uniform(2, 5))

            print("[INFO] Starting new browser...")
            if not self.setup_browser():
                print("[ERROR] Browser restart failed")
                return False

            if url:
                print(f"[INFO] Accessing URL: {url}")
                self.page.get(url)
                time.sleep(random.uniform(8, 12))

            print("[SUCCESS] Browser restarted")
            return True
        except Exception as e:
            print(f"[ERROR] Browser restart failed: {e}")
            return False

    # ====================================================================
    # Bot Detection / Recovery
    # ====================================================================
    def handle_continue_shopping(self):
        """Continue shopping 버튼이 있으면 클릭한다."""
        try:
            continue_button = self.page.ele("xpath://button[contains(text(), 'Continue shopping')]", timeout=3)
            if not continue_button:
                return False

            continue_button.click()
            print("[INFO] Continue shopping 버튼 클릭")
            time.sleep(random.uniform(2, 4))
            return True
        except Exception as e:
            print(f"[WARNING] Continue shopping 처리 실패: {e}")
            return False

    def extract_loaded_product_name(self):
        try:
            tree = html.fromstring(self.page.html)
        except Exception:
            return None

        return self.extract_first_text_from_tree(tree, [
            "//span[@id='productTitle']//text()",
            "//*[@id='productTitle']//text()",
            "//h1//span//text()",
            "//h1//text()",
        ])

    def validate_loaded_product_url(self, product_url, previous_url=None, listing_product_name=None):
        """Loaded Amazon product URL validation."""
        current_url = self.page.url

        if previous_url and current_url == previous_url:
            print("[WARNING] Page load failed: URL unchanged")
            raise Exception("Page load failed - URL unchanged")

        if 'amazon.com' not in current_url:
            print("[WARNING] Page load failed: not amazon.com")
            raise Exception("Page load failed - not amazon.com")

        original_item = self.extract_item(product_url)
        current_item = self.extract_item(current_url)
        if original_item and current_item and original_item != current_item:
            detail_product_name = self.extract_loaded_product_name()
            print(f"[WARNING] Redirect detected: {original_item} -> {current_item}")
            if self.redirect_product_names_match(listing_product_name, detail_product_name):
                print("[INFO] Redirect allowed: listing/detail product names match exactly")
                return current_url, True
            print(f"[WARNING] Redirect rejected: listing={listing_product_name or '-'} | detail={detail_product_name or '-'}")
            raise Exception("Redirect detected")

        return current_url, False

    def is_sorry_page(self):
        """현재 페이지가 Sorry/Robot check 페이지인지 확인한다."""
        page_source = self.page.html.lower()
        title = self.page.title.lower() if self.page.title else ''

        title_markers = [
            'robot check',
        ]
        source_markers = [
            '/error/logo',                              # Amazon 에러 페이지 로고
            '/dogsofamazon',                            # Amazon 에러 페이지 링크
            "sorry! we couldn't find that page",
            'sorry, we just need to make sure',         # Robot 확인 페이지
            "we're having technical issues",
            "we'll be back in a flash",
            "this page isn't available right now",
            "this page isn't available",
        ]

        for marker in title_markers:
            if marker in title:
                return True
        for marker in source_markers:
            if marker in page_source:
                return True
        return False

    def handle_try_again(self):
        """Try Again 버튼이 있으면 클릭한다."""
        selectors = [
            "xpath://button[contains(text(), 'Try again')]",
            "xpath://button[contains(text(), 'try again')]",
            "xpath://button[contains(text(), 'Try Again')]",
            "xpath://a[contains(text(), 'Try again')]",
            "xpath://a[contains(text(), 'try again')]",
            "xpath://button[contains(@class, 'retry')]",
            "xpath://button[contains(@class, 'try-again')]",
            "xpath://*[contains(text(), 'Try again') and (self::button or self::a)]",
        ]

        for selector in selectors:
            try:
                try_again_button = self.page.ele(selector, timeout=2)
                if try_again_button:
                    try_again_button.click()
                    print("[INFO] Try Again 버튼 클릭")
                    time.sleep(random.uniform(3, 5))
                    return True
            except Exception:
                continue
        return False

    def check_and_handle_sorry_page(self, max_retries=None):
        """Handle the current Sorry/Robot page once."""
        if not self.is_sorry_page():
            return True

        print("[WARNING] Sorry/Robot check page detected")
        if self.handle_try_again():
            return True

        print("[INFO] Refreshing page...")
        self.page.refresh()
        time.sleep(random.uniform(4, 6))
        return True

    def is_throttled(self):
        """현재 페이지가 throttling 상태인지 확인한다."""
        page_source = self.page.html.lower()
        throttle_markers = [
            'request was throttled',
            'please wait a moment and refresh',
        ]
        for marker in throttle_markers:
            if marker in page_source:
                return True
        return False

    def check_and_handle_throttling(self):
        """Handle the current throttling page once."""
        if not self.is_throttled():
            return True

        print("[WARNING] Throttling detected")
        print("[INFO] Waiting before refresh...")
        time.sleep(random.uniform(15, 20))

        print("[INFO] Refreshing page...")
        self.page.refresh()
        time.sleep(random.uniform(8, 12))
        return True

    def recover_amazon_pages(self, max_cycles=3):
        """Recover Amazon interstitial pages in a fixed order."""
        for cycle in range(1, max_cycles + 1):
            handled = False

            if self.is_throttled():
                print(f"[WARNING] Amazon recovery cycle {cycle}: throttling")
                if not self.check_and_handle_throttling():
                    return False
                handled = True

            if self.is_sorry_page():
                print(f"[WARNING] Amazon recovery cycle {cycle}: sorry page")
                if not self.check_and_handle_sorry_page():
                    return False
                handled = True

            if self.handle_continue_shopping():
                handled = True

            if not handled:
                return True

        print(f"[ERROR] Amazon recovery did not stabilize after {max_cycles} cycles")
        return False

    def scroll_to_bottom(self, target_ratio=1.0):
        """페이지 상단에서 지정한 비율 위치까지 순차적으로 스크롤한다."""
        try:
            target_ratio = max(0.0, min(float(target_ratio), 1.0))
            self.page.run_js("window.scrollTo(0, 0)")
            time.sleep(0.5)

            stagnant_count = 0
            max_scrolls = 200

            for _ in range(max_scrolls):
                current_position = int(self.page.run_js("return window.scrollY || window.pageYOffset || 0") or 0)
                total_height = int(self.page.run_js("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)") or 0)
                viewport_height = int(self.page.run_js("return window.innerHeight || document.documentElement.clientHeight") or 0)
                max_position = max(int((total_height - viewport_height) * target_ratio), 0)

                if current_position >= max_position:
                    break

                scroll_step = random.randint(300, 400)
                next_position = min(current_position + scroll_step, max_position)
                self.page.run_js(f"window.scrollTo(0, {next_position})")
                time.sleep(random.uniform(0.4, 1.0))

                new_position = int(self.page.run_js("return window.scrollY || window.pageYOffset || 0") or 0)
                new_total_height = int(self.page.run_js("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)") or 0)

                if new_position <= current_position and new_total_height <= total_height:
                    stagnant_count += 1
                    if stagnant_count >= 3:
                        break
                else:
                    stagnant_count = 0

            if target_ratio >= 1.0:
                self.page.run_js("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.8)

        except Exception as e:
            print(f"[ERROR] Scroll failed: {e}")
            traceback.print_exc()

    # ====================================================================
    # URL / Conversion
    # ====================================================================
    def extract_item(self, url):
        """Amazon URL에서 item을 추출한다.

        - 일반 URL: /dp/{item}, /gp/product/{item}
        - 인코딩 URL: %2Fdp%2F{item}, %2Fgp%2Fproduct%2F{item}
        Returns: item 문자열 또는 None.
        """
        if not url:
            return None
        try:
            match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url, re.IGNORECASE)
            if match:
                return match.group(1).upper()
            match = re.search(r'%2F(?:dp|gp%2Fproduct)%2F([A-Z0-9]{10})', url, re.IGNORECASE)
            if match:
                return match.group(1).upper()
        except Exception:
            pass
        return None

    def convert_first_number(self, source, field_name=None):
        """첫 번째 숫자를 추출하고 K/M 접미사를 변환한다.

        field_name을 전달하면 source에서 safe_extract_chain(source, field_name)으로 먼저 추출한다.

        예:
        - '1K+ bought' -> '1000'
        - '3M+ bought' -> '3000000'
        - '500 bought' -> '500'
        """
        raw = self.safe_extract_chain(source, field_name) if field_name else source
        if not raw:
            return None
        match = re.search(r'(\d+)\s*([KkMm])?', raw)
        if not match:
            return None
        num = int(match.group(1))
        suffix = match.group(2).upper() if match.group(2) else None
        if suffix == 'M':
            return str(num * 1000000)
        if suffix == 'K':
            return str(num * 1000)
        return str(num)

    def extract_final_sku_price(self, tree):
        """final_sku_price 기본 XPath 실패 시 Amazon 상태별 XPath를 순서대로 확인한다."""
        field_names = [
            'final_sku_price',
            'final_sku_price_no_price_reason',
            'final_sku_price_see_price_in_cart',
            'final_sku_price_unavailable',
        ]
        for field_name in field_names:
            value = self.safe_extract_chain(tree, field_name)
            if value:
                return value
        return None

    def extract_original_sku_price(self, source, field_name=None):
        """Return the first dollar price from mixed Amazon price text."""
        value = self.safe_extract_chain(source, field_name) if field_name else source
        if not value:
            return None
        match = re.search(r'\$\s*[\d,]+(?:\.\d{1,2})?', str(value))
        if not match:
            return None
        return re.sub(r'\$\s+', '$', match.group(0)).strip()

    def extract_count_of_star_rating(self, source, field_name=None):
        """별점 수 추출: field_name이 있으면 chain 추출 후 쉼표 포함 숫자만 반환한다.
        예: '3,352 global ratings' -> '3,352'
        """
        text = self.safe_extract_chain(source, field_name) if field_name else source
        match = re.search(r'[\d,]+', text) if text else None
        return match.group(0) if match else None

    def extract_star_rating(self, source, field_name=None):
        """별점 추출: field_name이 있으면 chain 추출 후 소수점 포함 숫자만 반환한다.
        예: '4.2 out of 5 stars' -> '4.2', '4.2 out of 5' -> '4.2'
        """
        text = self.safe_extract_chain(source, field_name) if field_name else source
        match = re.search(r'\d+\.?\d*', text) if text else None
        return match.group(0) if match else None

    def normalize_sku_popularity(self, value):
        """sku_popularity는 Amazon's Choice인 경우만 저장한다."""
        if not value:
            return None
        normalized = re.sub(r'\s+', ' ', str(value)).strip()
        if normalized == "Amazon's Choice":
            return normalized
        return None

    def extract_sku(self, tree, mst_sku=None, section_name=None, product_type=None):
        """sku final value: master -> page extraction."""
        if product_type == 'TV':
            page_sku, page_source = self.extract_tv_sku(tree, section_name)
        elif product_type == 'HHP':
            page_sku, page_source = self.extract_hhp_sku(tree)
        else:
            page_sku = self.safe_extract_chain(tree, 'sku') if tree is not None else None
            page_source = 'Details' if page_sku else None

        mst_sku_value = str(mst_sku).strip() if mst_sku else ''
        if mst_sku_value and mst_sku_value.lower() != 'no sku':
            return mst_sku, "마스터", page_sku
        if page_sku:
            return page_sku, page_source, page_sku
        if product_type in ('TV', 'HHP'):
            return "no sku", "미추출", page_sku
        return None, None, page_sku

    def extract_tv_sku(self, tree, section_name=None):
        """TV SKU extraction by section."""
        if tree is None:
            return None, None

        if section_name == 'product_information_section':
            for label, field_name in [
                ('Mfr Part Number', 'mfr_part_number'),
                ('Manufacturer Part Number', 'manufacturer_part_number'),
                ('Model Number', 'model_number'),
            ]:
                value = self.safe_extract_chain(tree, field_name)
                if value:
                    value = re.sub(r'\s+', ' ', str(value)).strip().strip(':').strip()
                    if 'BNDL_' in value:
                        model_name = self.safe_extract_chain(tree, 'model_name')
                        if model_name:
                            model_name = re.sub(r'\s+', ' ', str(model_name)).strip().strip(':').strip()
                            print(f"  [SKU] BNDL_ detected - Model Name fallback: '{value}' -> '{model_name}'")
                            return model_name, 'Model Name (BNDL_ fallback)'
                        print(f"  [SKU] BNDL_ detected but Model Name not found - keep original: '{value}'")
                    return value, label

            return None, None

        if section_name == 'technical_details_section':
            page_sku = self.safe_extract_chain(tree, 'sku_number')
            if page_sku:
                page_sku = re.sub(r'\s+', ' ', str(page_sku)).strip().strip(':').strip()
                return page_sku, 'SKU Number'
            return None, None

        page_sku = self.safe_extract_chain(tree, 'sku')
        return (page_sku, 'Details') if page_sku else (None, None)

    def extract_hhp_sku(self, tree):
        """HHP SKU extraction."""
        if tree is None:
            return None, None

        page_sku = self.safe_extract_chain(tree, 'item_model_number')
        if page_sku:
            page_sku = re.sub(r'\s+', ' ', str(page_sku)).strip().strip(':').strip()
            return page_sku, 'Item Model Number'
        return None, None

    def extract_reviews_from_detail_page(self, tree, max_reviews=20):
        """상세페이지에서 리뷰 추출: review_container와 같은 suffix의 review_content를 매칭한다."""
        try:
            review_containers, matched_container_field = self.safe_extract_chain_list(tree, 'review_container')
            if not review_containers:
                print("[ERROR] review_container not matched in any chain xpath")
                return None, 0
            review_containers = review_containers[:max_reviews]

            suffix = matched_container_field[len('review_container'):]
            paired_content_field = 'review_content' + suffix

            if self.xpaths.get(paired_content_field, {}).get('xpath'):
                content_field = paired_content_field
            elif self.xpaths.get('review_content', {}).get('xpath'):
                print(f"[WARN] {paired_content_field} 미등록 → review_content 로 폴백")
                content_field = 'review_content'
            else:
                print("[ERROR] review_content / 페어 content XPath 모두 미등록")
                return None, 0

            cleaned_reviews = []
            for container in review_containers:
                text = self.safe_extract_join(container, content_field, separator=' ')
                if text:
                    cleaned = ' '.join(text.split())
                    if cleaned:
                        cleaned_reviews.append(cleaned)

            if not cleaned_reviews:
                return None, 0

            formatted_reviews = [f"review{idx} - {review}" for idx, review in enumerate(cleaned_reviews, 1)]
            return ' ||| '.join(formatted_reviews), len(cleaned_reviews)

        except Exception as e:
            print(f"[ERROR] Review extraction failed: {e}")
            traceback.print_exc()
            return None, 0

    # 리뷰 로그인 게이트 감지 마커 — 2026-07-03 배치 캡처에서 확인된 실물 문구
    # "Customer reviews require account verification. Sign in" (a_20260703_165616 참고)
    REVIEW_GATE_MARKERS = (
        'reviews require account verification',
        'account verification required',
    )

    def is_review_gated(self, page_html=None):
        """리뷰 로그인 게이트 감지.

        Amazon이 세션을 의심하면 리뷰 리스트 자리에 계정 인증 배너를 띄우고
        review 컨테이너를 아예 렌더하지 않는다. 별점/요약/가격은 정상이라
        게이트를 감지하지 못하면 detailed_review_content만 조용히 전멸한다.
        """
        try:
            source = (page_html if page_html is not None else self.page.html).lower()
        except Exception:
            return False
        return any(marker in source for marker in self.REVIEW_GATE_MARKERS)

    def extract_reviews_with_retry(self, tree, max_reviews=20, page_html=None):
        """리뷰 추출 + 게이트 감지 + lazy-load 3단 재시도 (SIEL detail.py 검증 패턴 이식).

        - 게이트 감지 시 재시도 없이 즉시 반환 (재시도 무의미 — 세션 단위 차단)
        - 1차 추출 실패 시: 리뷰 섹션 scrollIntoView → 3초 대기 → 재파싱/재추출
        - 재실패 시: 3초 추가 대기 → 최종 재추출
          (SIEL: "같은 product가 timing 따라 fill/null 변동. full-run 0/N 회귀 방지")

        Returns:
            (content, count, gated): 기존 extract_reviews_from_detail_page 반환값 + 게이트 여부
        """
        if page_html is None:
            try:
                page_html = self.page.html
            except Exception:
                page_html = ''

        if self.is_review_gated(page_html):
            print("  [리뷰] 로그인 게이트 감지 (account verification) → 추출 불가")
            return None, 0, True

        content, count = self.extract_reviews_from_detail_page(tree, max_reviews)
        if count:
            return content, count, False

        # lazy-load 재시도 1: 리뷰 섹션 중앙 스크롤 + settle
        try:
            self.page.run_js(
                "var e = document.getElementById('reviewsMedley')"
                " || document.getElementById('customer-reviews_feature_div');"
                " if (e) e.scrollIntoView({block: 'center', behavior: 'instant'});"
            )
        except Exception:
            pass
        time.sleep(3)
        try:
            tree = html.fromstring(self.page.html)
        except Exception:
            return content, count, False
        content, count = self.extract_reviews_from_detail_page(tree, max_reviews)
        if count:
            print("  [리뷰] lazy-load 재시도 1차 성공")
            return content, count, False

        # lazy-load 재시도 2: 추가 대기 후 최종 시도
        time.sleep(3)
        try:
            page_html = self.page.html
            tree = html.fromstring(page_html)
        except Exception:
            return content, count, False
        content, count = self.extract_reviews_from_detail_page(tree, max_reviews)
        if count:
            print("  [리뷰] lazy-load 재시도 2차 성공")
            return content, count, False

        # 최종 실패 — 스크롤/렌더 중 게이트가 새로 떴는지 재확인
        return content, count, self.is_review_gated(page_html)

    def save_debug_html(self, tag, max_files=3):
        """진단용 HTML 스냅샷을 logs/에 저장 (SIEL maybe_save_html 패턴).

        배치당 max_files개까지만 저장해 디스크 폭주를 막는다.
        장애 시 캡처 이미지 대신 실제 DOM으로 원인을 즉시 확인하는 용도.
        """
        try:
            if not hasattr(self, '_debug_html_count'):
                self._debug_html_count = 0
            if self._debug_html_count >= max_files:
                return None
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            logs_dir = os.path.join(project_root, 'logs')
            os.makedirs(logs_dir, exist_ok=True)
            filename = f"{self.batch_id or 'nobatch'}_{tag}.html"
            filepath = os.path.join(logs_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(self.page.html)
            self._debug_html_count += 1
            print(f"  [진단] HTML 스냅샷 저장: {filename}")
            return filepath
        except Exception as e:
            print(f"  [진단] HTML 스냅샷 저장 실패: {e}")
            return None

    def move_to_review_section(self, has_review_link):
        """리뷰 섹션 이동: top 리뷰 링크 우선, 실패 시 원래 위치에서 review_section 탐색."""
        original_position = 0
        try:
            original_position = self.page.run_js("return window.pageYOffset") or 0
        except Exception:
            pass

        if has_review_link:
            review_link_xpath = self.xpaths.get('review_link', {}).get('xpath')
            try:
                review_link = self.page.ele(f'xpath:{review_link_xpath}', timeout=3)
                if review_link:
                    self.page.run_js("window.scrollTo(0, 0)")
                    time.sleep(0.5)
                    review_link = self.page.ele(f'xpath:{review_link_xpath}', timeout=3)
                    review_link.click()
                    time.sleep(1)
                    print(f"  [INFO] review_link 클릭 → 리뷰 섹션 이동")
                    return True
            except Exception as e:
                print(f"  [INFO] review_link 클릭 실패 → review_section fallback 시도: {e}")

        try:
            self.page.run_js(f"window.scrollTo(0, {original_position})")
            time.sleep(0.5)
        except Exception:
            pass

        return bool(self.scroll_to_section('review_section', max_scrolls=10, label='리뷰 섹션'))

    # ====================================================================
    # Main
    # ====================================================================
    def find_product_containers(self, base_container_xpath, page_number, expected_products=40, scroll_ratio=1.0, label=None):
        """상품 컨테이너 검증 파싱 후 부족하면 스크롤 재파싱을 최대 3회 수행한다."""
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
                self.scroll_to_bottom(scroll_ratio)
                time.sleep(random.uniform(3, 5))

        return base_containers

    # ====================================================================
    # Details
    # ====================================================================
    def open_details_sections(self, button_field_names, label='Details 버튼'):
        """전달받은 XPath 키 목록의 버튼을 순서대로 클릭하고 최신 HTML tree를 반환한다."""
        try:
            if isinstance(button_field_names, str):
                button_field_names = [button_field_names]

            self.page.run_js("window.scrollTo(0, document.body.scrollHeight / 2);")
            time.sleep(0.5)

            for field_name in button_field_names:
                xpath = self.xpaths.get(field_name, {}).get('xpath')
                if not xpath:
                    print(f"  [버튼] {field_name} XPath 없음")
                    continue

                try:
                    button = self.page.ele(f'xpath:{xpath}', timeout=5)
                    if not button:
                        raise Exception(f"{field_name} button not found")
                    button.scroll.to_see()
                    time.sleep(0.5)
                    if button.attr('aria-expanded') == 'true':
                        print(f"  [버튼] {field_name} 이미 열림")
                        continue
                    button.click()
                    time.sleep(0.5)
                    print(f"  [버튼] {field_name} 클릭 성공")
                except Exception:
                    print(f"  [버튼] {field_name} 클릭 실패")

            return html.fromstring(self.page.html)

        except Exception as e:
            print(f"  [WARN] {label} 실패: {e}")
            return None

    def scroll_to_section(self, field_names, label='Details 섹션', max_scrolls=5, scroll_px=(250, 400)):
        """여러 섹션 후보를 DOM에서 먼저 찾고, 없으면 스크롤하며 찾아 이동한다. 찾은 필드명을 반환한다."""
        if isinstance(field_names, str):
            field_names = [field_names]

        candidates = []
        for field_name in field_names:
            xpath = self.xpaths.get(field_name, {}).get('xpath')
            if xpath:
                candidates.append((field_name, xpath))

        if not candidates:
            print(f"  [INFO] {label} XPath 없음")
            return None

        for field_name, xpath in candidates:
            try:
                section = self.page.ele(f'xpath:{xpath}', timeout=2)
                if section:
                    section.scroll.to_see()
                    time.sleep(random.uniform(0.8, 1.5))
                    print(f"  [INFO] {label} DOM 발견 → 이동 완료: {field_name}")
                    return field_name
            except Exception:
                pass

        scroll_min, scroll_max = scroll_px
        for scroll_count in range(1, max_scrolls + 1):
            pos_before = self.page.run_js("return window.pageYOffset")
            self.page.scroll.down(random.randint(scroll_min, scroll_max))
            time.sleep(random.uniform(0.5, 1))
            pos_after = self.page.run_js("return window.pageYOffset")

            for field_name, xpath in candidates:
                try:
                    section = self.page.ele(f'xpath:{xpath}', timeout=1)
                    if section:
                        section.scroll.to_see()
                        time.sleep(random.uniform(0.8, 1.5))
                        print(f"  [INFO] {label} 스크롤 {scroll_count}회 → 이동 완료: {field_name}")
                        return field_name
                except Exception:
                    pass

            if pos_after == pos_before:
                break

        print(f"  [INFO] {label} 없음")
        return None

    # ====================================================================
    # DB Helper
    # ====================================================================
    def print_spec_diff_summary(self):
        """Print master-vs-page extraction differences collected in spec_diffs."""
        spec_diffs = getattr(self, 'spec_diffs', [])

        if spec_diffs:
            print(f"\n{'=' * 80}")
            print(f"[SPEC DIFF] 마스터 vs 페이지 추출 값 불일치: 총 {len(spec_diffs)}건")
            print(f"{'=' * 80}")
            for d in spec_diffs:
                parts = [f"item={d['item']}"]
                if d.get('mst_sku') and d.get('page_sku') and d['mst_sku'] != d.get('page_sku'):
                    parts.append(
                        f"sku: mst={d['mst_sku']!r} / page={d.get('page_sku')!r}"
                    )
                if d.get('mst_screen_size') and d.get('screen_size') and d['mst_screen_size'] != d.get('screen_size'):
                    parts.append(
                        f"screen_size: mst={d['mst_screen_size']!r} / extracted={d.get('screen_size')!r}"
                    )
                print("  " + " | ".join(parts))
            print(f"{'=' * 80}\n")
        else:
            print("\n[SPEC DIFF] 마스터 vs 페이지 추출 값 불일치 없음")

    def extract_delivery_field(self, source, field_name, separator=None):
        """배송 관련 필드를 추출하고 저장 전 텍스트로 정제한다."""
        value = (
            self.safe_extract_chain_join(source, field_name, separator=separator)
            if separator
            else self.safe_extract_chain(source, field_name)
        )
        if not value:
            return None

        text = str(value).replace('Details', '').strip()
        if not text:
            return None

        lower_text = text.lower()
        if field_name == 'delivery_availability' and lower_text.startswith('join'):
            return None
        if field_name == 'fastest_delivery' and not lower_text.startswith('or fastest'):
            return None

        return text

    def build_db_item_cache(self, product_list_table):
        """Build {item: row_id} cache from a product_list table for current batch."""
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
                    print(f"[WARNING] Duplicate item in DB batch: item={item}, keep_id={db_item_map[item]}, skip_id={row_id}")
                    continue
                db_item_map[item] = row_id

            print(f"[INFO] DB item cache loaded: {len(db_item_map)} items")
            return db_item_map

        except Exception as e:
            print(f"[WARNING] build_db_item_cache failed: {e}")
            return {}
