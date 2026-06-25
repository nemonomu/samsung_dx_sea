"""
BestBuy TV XPath Tester (DrissionPage 버전)

================================================================================
주요 기능
================================================================================
1. URL 입력 후 페이지 로드
2. 상세(Detail) / 리스트(List) 페이지 모드 선택
3. 상세 모드: 미리 정의된 XPath 목록 전체 자동 테스트
4. 리스트 모드: Base container로 아이템 추출 후 각 필드 테스트

================================================================================
사용법
================================================================================
python tv/bestbuy/bby_tv_xpath_tester.py

================================================================================
TV Main 필요 XPath 필드 (8개)
================================================================================
- base_container      : 각 상품 아이템 컨테이너 (절대경로)
- retailer_sku_name   : 상품명 (상대경로)
- product_url         : 상품 URL (상대경로)
- offer               : 할인/프로모션 (상대경로)
- pick_up_availability: 매장 픽업 가능 여부 (상대경로)
- shipping_availability: 배송 가능 여부 (상대경로)
- delivery_availability: 배달 가능 여부 (상대경로)
- sku_status          : Sponsored 여부 (상대경로)
"""

import sys
import os
import time
import random
import traceback
from lxml import html

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.setup import setup_environment
setup_environment(__file__)
from common.bestbuy_base import BestBuyBaseCrawler


# ================================================================================
# XPath 목록 정의 - 상세 페이지용
# ================================================================================
DETAIL_XPATHS = {
    # "top_star_rating": "//div[@data-component-name='ProductHeader']/following-sibling::div[@data-component-name='ReviewStatsContextualized']//span[contains(@class, 'font-weight-bold') and contains(@class, 'order-1')]",
    # "top_count_of_reviews": "//div[@data-component-name='ProductHeader']/following-sibling::div[@data-component-name='ReviewStatsContextualized']//span[(contains(@class, 'c-reviews') and contains(@class, 'order-2')) or contains(@class, 'c-ratings-reviews-mini')]",
    # "final_sku_price": "//div[@data-component-name='LargePrice']//div[@data-testid='price-block-customer-price']//span[not(contains(@class, 'sr-only'))]",
    # "see_price_in_cart": "//div[@data-component-name='LargePrice']//div[@data-testid='price-restricted-price-tap-for-price']//span[contains(text(), 'See price in cart')]",
    # "no_longer_available": "//div[contains(@class, 'text-danger') and contains(text(), 'This item is no longer available')]",
    # "original_sku_price": "//span[@data-lu-target='comp_value']",
    # "savings": "//span[@data-testid='price-block-total-savings-text']",
    # "sku": "//div[contains(@class, 'disclaimer')]//div[contains(text(), 'Model:')]",
    # "sku_number": "//div[contains(@class, 'pr-150') and contains(text(), 'SKU:')]",
    # "specs_button": "//div[@data-component-name='ProductSpecifications']//button[@data-testid='brix-button' and .//h3[contains(text(), 'Specifications')]]",
    # "specs_modal_title": "//div[@data-testid='brix-sheet-header']//h2[@data-testid='brix-sheet-title' and normalize-space()='Specifications']",
    # "close_button": "//button[@data-testid='brix-sheet-closeButton' or @aria-label='Close Sheet']",
    # "screen_size": "//div[text()='Screen Size Class']/following-sibling::div[contains(@class, 'flex font-500')]",
    # "screen_size_modal": "//div[@data-testid='brix-sheet-content']//li[.//h4[text()='Display']]//div[div[1][contains(@class, 'font-weight-medium') and text()='Screen Size']]/div[2]",
    # "estimated_annual_electricity_use": "//div[@data-testid='brix-sheet-content']//div[div[1][contains(@class, 'font-weight-medium') and contains(., 'Estimated Annual Electricity Use')]]/div[2]",
    # "model_year": "//div[@data-testid='brix-sheet-content']//div[div[1][contains(@class, 'font-weight-medium') and contains(., 'Model Year')]]/div[2]",
    # "similar_products_section": "//div[@data-component-name='Compare']//h2[normalize-space()='Compare similar products']",
    # "similar_product_name": "//div[@data-component-name='Compare'][.//h2[normalize-space()='Compare similar products']]//div[contains(@class, 'product-title')]//*[self::a or self::span][normalize-space()]",
    # "review_section": "//h2[starts-with(@id, 'tabbed-customerreviews') and contains(text(), 'Reviews')]",
    # "star_rating": "//div[@data-component-name='RedesignCustomerRatingAndReviewsSummary']//span[contains(@class, 'heading-2') and contains(@class, 'font-weight-medium') and @aria-hidden='true']/text()",
    # "count_of_reviews": "//div[@data-component-name='RedesignCustomerRatingAndReviewsSummary']//div[contains(@class, 'v-text-dark-gray') and contains(@class, 'text-center') and contains(text(), 'reviews')]/text()",
    # "recommendation_intent": "//div[contains(., 'would recommend to a friend')]/span[@class='font-weight-bold']/text()",
    # "reviews_button": "//a[contains(@href, '/reviews') and .//span[contains(text(), 'See all customer reviews')]]",
    # "reviews_button_fallback1": "//a[contains(@href, '/reviews') and contains(., 'See all customer reviews')]",
    "detailed_review_content": "//ul[@id='stand-alone-review-list']/li//p[starts-with(@id, 'ugc-line-clamp-reviews-')]/text()",
    "reviewpage_recommendation_intent": "//div[span[contains(text(), 'would recommend to a friend')]]/span[contains(@class, 'font-500')]",
    "reviewpage_recommendation_intent_fallback": "//svg[contains(@aria-label, 'would recommend')]/..//span[contains(@class, 'text-5')]",
    "reviewpage_recommendation_intent_fallback2": "//div[contains(@class, 'recommendation-card')]//span[contains(@class, 'recommendation-percent')]",
    "reviewpage_recommendation_intent_fallback3": "//div[contains(@class, 'recommendation-card')]//span[contains(@class, 'donut-percent-text')]/text()",
}

DETAIL_XPATH_LIST = DETAIL_XPATHS


# ================================================================================
# XPath 목록 정의 - 리스트 페이지용 (TV Main/BSR)
# ================================================================================
# Base container XPath (각 상품 아이템)
# Sponsored: <li class="slContainer">, 일반: <li class="product-list-item">
LIST_BASE_CONTAINER = '//li[contains(@class, "product-list-item") or contains(@class, "slContainer")]'

# 리스트 아이템 내 필드 XPath (상대 경로 - .// 로 시작)
LIST_FIELD_XPATHS = {
    'retailer_sku_name': './/h3[contains(@class, "product-title")]',
    'product_url': './/a[@class="product-list-item-link"]/@href',
    'offer': './/div[@data-testid="plus-x-offers"]//span[contains(text(), "offer")]',
    'pick_up_availability': './/div[@class="fulfillment"]//span[contains(text(), "Pickup") or contains(text(), "Pick up")]',
    'fastest_delivery': './/div[@class="fulfillment"]//span[contains(text(), "Shipping") or contains(text(), "Get it")]',
    'delivery_availability': './/div[@class="fulfillment"]//span[contains(text(), "Delivery") or contains(text(), "delivery")]',
    'sku_status': './/div[@class="sponsored"]',
}

LIST_PAGE_XPATHS = {
    "pagenation": "//div[contains(@class, 'show-more-progressive-container')] | //button[@data-testid='show-more-progressive-button']",
    "pagenation_fallback1": "//div[contains(@class, 'pagination-container')]",
}


# ================================================================================
# XPath 목록 정의 - 트렌드 페이지용 (TV Trend)
# ================================================================================
# Base container XPath (각 상품 아이템 - 캐러셀 아이템)
TREND_BASE_CONTAINER = "//div[@data-testid='Discover-Background-Hardline-Gradient-TestID']//div[@data-testid='badge-product-card']"

# 트렌드 아이템 내 필드 XPath (상대 경로 - .// 로 시작)
TREND_FIELD_XPATHS = {
    'retailer_sku_name': ".//a[contains(@href, '/product/') and .//h3]//h3",
    'product_url': ".//a[contains(@href, '/product/') and .//h3]/@href",
}


# ================================================================================
# XPath 목록 정의 - 프로모션 페이지용 (TV Promotion)
# ================================================================================
# 섹션 컨테이너 (promotion_type별로 캐러셀이 묶여있는 단위)
PROMOTION_SECTION_CONTAINER = '//div[@data-testid="section"][.//div[@data-testid="hero-experience-deals-carousel-test-id"]]'

# 섹션 내 promotion_type 추출 XPath (상대 경로)
PROMOTION_TYPE_XPATHS = {
    'promotion_type_h2': './/h2[contains(@class, "headline80")]',
    'promotion_type_h3': './/span[contains(@class, "hero-fluid-headline")]',
    'promotion_type_p':  './/p[contains(@class, "heading-4")]',
    'promotion_type_sub': './/span[contains(@class, "hero-fluid-subhead-2")]',
}

# 섹션 내 캐러셀 아이템 (상대 경로)
PROMOTION_BASE_CONTAINER = './/ul[contains(@class, "c-carousel-list")]/li[contains(@class, "c-carousel-item")]'

# 캐러셀 아이템 내 필드 XPath (상대 경로 - .// 로 시작)
PROMOTION_FIELD_XPATHS = {
    'retailer_sku_name': './/div[@data-testid="product-card-title"]/span',
    'product_url': '(.//a[contains(@href, "/product/")]/@href)[1]',
    'offer': './/div[contains(text(), "offer for you") or contains(text(), "offers for you")]',
}


class BestBuyTVXPathTester(BestBuyBaseCrawler):
    """BestBuy TV XPath 테스터 (DrissionPage 버전)"""

    def __init__(self):
        super().__init__()
        self.page = None
        self.bestbuy_zip_code = '10010'
        self.xpaths = self.build_xpath_map(DETAIL_XPATHS)

    def build_xpath_map(self, xpath_dict):
        return {
            field_name: {"xpath": xpath, "previous_xpath": None}
            for field_name, xpath in xpath_dict.items()
        }

    def build_list_xpath_map(self):
        return {
            "base_container": {"xpath": LIST_BASE_CONTAINER, "previous_xpath": None},
            **self.build_xpath_map(LIST_PAGE_XPATHS),
            **self.build_xpath_map(LIST_FIELD_XPATHS),
        }

    def setup_driver(self):
        """DrissionPage 브라우저 설정"""
        print("[INFO] DrissionPage 브라우저 설정 중...")
        try:
            if not self.setup_bestbuy_browser():
                raise RuntimeError("DrissionPage setup failed")
            print("[INFO] DrissionPage 브라우저 설정 완료")
        except Exception as e:
            print(f"[ERROR] DrissionPage 설정 실패: {e}")
            raise

    def click_specs_button(self):
        """스펙 버튼 클릭 → 모달 오픈 (screen_size / electricity / model_year 추출용)"""
        specs_button_xpath = DETAIL_XPATH_LIST.get('specs_button', '')
        if not specs_button_xpath:
            print("[INFO] specs_button XPath 미설정 - 스킵")
            return False
        try:
            # 1차: DOM에서 먼저 찾기
            specs_button = self.page.ele(f'xpath:{specs_button_xpath}', timeout=2)
            if not specs_button:
                # 2차: 스크롤하며 찾기
                for scroll_count in range(10):
                    self.page.run_js(f"window.scrollTo({{top: {500 + scroll_count * 300}, behavior: 'smooth'}});")
                    time.sleep(0.4)
                    specs_button = self.page.ele(f'xpath:{specs_button_xpath}', timeout=1)
                    if specs_button:
                        break
            if specs_button:
                self.page.run_js("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'})", specs_button)
                time.sleep(0.5)
                specs_button.click()
                time.sleep(1)
                print("[INFO] 스펙 버튼 클릭 완료")
                return True
            else:
                print("[WARNING] 스펙 버튼 못 찾음")
                return False
        except Exception as e:
            print(f"[ERROR] 스펙 버튼 클릭 실패: {e}")
            return False

    def scroll_to_review_section(self):
        """리뷰 섹션으로 스크롤 → 6단계 필드 테스트용"""
        review_section_xpath = DETAIL_XPATH_LIST.get('review_section', '')
        if not review_section_xpath:
            print("[INFO] review_section XPath 미설정 - 스킵")
            return False
        try:
            # 1차: DOM에서 먼저 찾기
            review_section = self.page.ele(f'xpath:{review_section_xpath}', timeout=2)
            if not review_section:
                # 2차: 스크롤하며 찾기
                for scroll_count in range(10):
                    self.page.run_js(f"window.scrollTo({{top: {500 + scroll_count * 300}, behavior: 'smooth'}});")
                    time.sleep(0.4)
                    review_section = self.page.ele(f'xpath:{review_section_xpath}', timeout=1)
                    if review_section:
                        break
            if review_section:
                self.page.run_js("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'})", review_section)
                time.sleep(3)
                print("[INFO] 리뷰 섹션 스크롤 완료 (3초 대기)")
                return True
            else:
                print("[WARNING] 리뷰 섹션 못 찾음")
                return False
        except Exception as e:
            print(f"[ERROR] 리뷰 섹션 스크롤 실패: {e}")
            return False

    def click_reviews_button(self):
        """리뷰 더보기 버튼 클릭 → 리뷰 상세 페이지 이동 (7단계 필드 테스트용)"""
        reviews_button_xpath = DETAIL_XPATH_LIST.get('reviews_button', '')
        if not reviews_button_xpath:
            print("[INFO] reviews_button XPath 미설정 - 스킵")
            return False
        # reviews_button + fallback XPaths
        fallback_str = DETAIL_XPATH_LIST.get('reviews_button_fallback', '')
        fallback_xpaths = [x.strip() for x in fallback_str.split('|||') if x.strip()] if fallback_str else []
        all_xpaths = [reviews_button_xpath] + fallback_xpaths
        try:
            # 1차: DOM에서 먼저 찾기
            for xpath in all_xpaths:
                review_button = self.page.ele(f'xpath:{xpath}', timeout=2)
                if review_button:
                    self.page.run_js("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'})", review_button)
                    time.sleep(0.5)
                    review_button.click()
                    time.sleep(2)
                    print(f"[INFO] 리뷰 버튼 클릭 완료 (xpath: {xpath[:60]}...)")
                    return True
            # 2차: 스크롤하며 찾기
            for scroll_count in range(10):
                self.page.run_js(f"window.scrollTo({{top: {500 + scroll_count * 300}, behavior: 'smooth'}});")
                time.sleep(0.4)
                for xpath in all_xpaths:
                    review_button = self.page.ele(f'xpath:{xpath}', timeout=1)
                    if review_button:
                        self.page.run_js("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'})", review_button)
                        time.sleep(0.5)
                        review_button.click()
                        time.sleep(2)
                        print(f"[INFO] 리뷰 버튼 클릭 완료 (스크롤 후, xpath: {xpath[:60]}...)")
                        return True
            print("[WARNING] 리뷰 버튼 못 찾음")
            return False
        except Exception as e:
            print(f"[ERROR] 리뷰 버튼 클릭 실패: {e}")
            return False

    def handle_block(self):
        """차단/CAPTCHA 체크"""
        page_html_lower = self.page.html.lower()
        block_phrases = [
            'access denied',
            'please verify you are a human',
            'unusual activity',
        ]
        if any(phrase in page_html_lower for phrase in block_phrases):
            print("[WARNING] 차단/CAPTCHA 감지 - 수동 해결 후 엔터를 누르세요...")
            input()
            time.sleep(2)
            return True
        return False

    def extract_value(self, element):
        """요소에서 값 추출"""
        if hasattr(element, 'text_content'):
            return element.text_content().strip()
        else:
            return str(element).strip()

    def extract_all_values(self, elements):
        """모든 요소의 값을 추출하여 리스트로 반환"""
        values = []
        for elem in elements:
            value = self.extract_value(elem)
            if value:
                values.append(value)
        return values

    def get_tree(self):
        page_html = self.page.run_js("return document.documentElement.outerHTML")
        return html.fromstring(page_html)

    def test_xpath_group(self, tree, xpath_dict, title):
        print("\n" + "=" * 70)
        print(f"[{title}]")
        print("=" * 70)

        found_count = 0
        not_found_count = 0

        for field_name, xpath in xpath_dict.items():
            print(f"\n[{field_name}]")
            if not xpath:
                print('  XPath: "" (not set)')
                not_found_count += 1
                continue

            print(f"  XPath: {xpath}")
            try:
                results = tree.xpath(xpath)
                if not results:
                    print("  Result: no match")
                    not_found_count += 1
                    continue

                found_count += 1
                print(f"  Match count: {len(results)}")
                values = self.extract_all_values(results)
                for idx, value in enumerate(values[:10], 1):
                    if len(value) > 120:
                        value = value[:120] + "..."
                    print(f"  [{idx}] {value}")
            except Exception as e:
                not_found_count += 1
                print(f"  Result: error - {e}")

        print("\n" + "-" * 70)
        print(f"Summary: found {found_count}, not_found {not_found_count}")

    def test_detail_field_subset(self, tree, title, field_names):
        fields = {
            field_name: DETAIL_XPATHS[field_name]
            for field_name in field_names
            if field_name in DETAIL_XPATHS
        }
        self.test_xpath_group(tree, fields, title)

    def test_spec_modal(self):
        modal_tree = self.open_spec_modal()
        if modal_tree is None:
            return

        self.test_detail_field_subset(
            modal_tree,
            "spec modal XPath",
            (
                "specs_modal_title",
                "close_button",
                "screen_size_modal",
                "estimated_annual_electricity_use",
                "model_year",
            ),
        )
        self.close_spec_modal()

    def move_to_similar_products(self):
        moved = self.scroll_find_element(
            "similar_products_section",
            max_scrolls=5,
            label="similar products section",
        )
        print(f"  [similar] similar_products_section move {'success' if moved else 'failed'}")
        return moved

    def test_similar_products(self):
        self.move_to_similar_products()
        tree = self.get_tree()
        self.test_detail_field_subset(
            tree,
            "similar products XPath",
            ("similar_products_section", "similar_product_name"),
        )

    def move_to_review_section(self):
        moved = self.scroll_find_element(
            "review_section",
            max_scrolls=5,
            label="review section",
            scroll_px=(150, 200),
        )
        print(f"  [review] review_section move {'success' if moved else 'failed'}")
        if moved:
            time.sleep(3)

    def test_detail_page(self, tree):
        """상세페이지 XPath 테스트"""
        print("\n" + "=" * 70)
        print("[상세페이지 XPath 테스트 결과]")
        print("=" * 70)

        if not DETAIL_XPATH_LIST:
            print("[INFO] DETAIL_XPATH_LIST가 비어있습니다. XPath를 추가하세요.")
            return

        for field_name, xpath in DETAIL_XPATH_LIST.items():
            print(f"\n[{field_name}]")
            if not xpath:
                print(f"  XPath: (미설정)")
                continue
            print(f"  XPath: {xpath}")
            try:
                results = tree.xpath(xpath)
                if results:
                    print(f"  매칭 개수: {len(results)}개")
                    values = self.extract_all_values(results)
                    for i, value in enumerate(values, 1):
                        if len(value) > 100:
                            print(f"  [{i}]: {value[:100]}...")
                            print(f"       (전체 길이: {len(value)})")
                        else:
                            print(f"  [{i}]: {value}")
                else:
                    print(f"  결과: (추출 실패 - 매칭 없음)")
            except Exception as e:
                print(f"  결과: (에러) {e}")

    def test_list_page(self, tree):
        """리스트페이지 XPath 테스트"""
        print("\n" + "=" * 70)
        print("[리스트페이지 XPath 테스트 결과]")
        print("=" * 70)

        # 1. Base container 테스트
        print(f"\n--- Base Container 테스트 ---")
        print(f"  XPath: {LIST_BASE_CONTAINER}")

        base_containers = tree.xpath(LIST_BASE_CONTAINER)
        print(f"  매칭 개수: {len(base_containers)}개")

        if not base_containers:
            print("  [ERROR] base_container를 찾을 수 없습니다.")
            return

        # 2. 첫 3개 아이템의 필드 테스트
        print(f"\n--- 아이템별 필드 테스트 ---")

        for idx, item in enumerate(base_containers, 1):
            print(f"\n{'='*50}")
            print(f"[아이템 {idx}]")
            print(f"{'='*50}")

            for field_name, xpath in LIST_FIELD_XPATHS.items():
                print(f"\n  [{field_name}]")
                print(f"    XPath: {xpath}")
                try:
                    results = item.xpath(xpath)
                    if results:
                        print(f"    매칭 개수: {len(results)}개")
                        values = self.extract_all_values(results)
                        for i, value in enumerate(values, 1):
                            if len(value) > 80:
                                print(f"    [{i}]: {value[:80]}...")
                            else:
                                print(f"    [{i}]: {value}")
                    else:
                        print(f"    결과: (추출 실패)")
                except Exception as e:
                    print(f"    결과: (에러) {e}")

    def test_trend_page(self, tree):
        """트렌드페이지 XPath 테스트"""
        print("\n" + "=" * 70)
        print("[트렌드페이지 XPath 테스트 결과]")
        print("=" * 70)

        # 1. Base container 테스트
        print(f"\n--- Base Container 테스트 ---")
        print(f"  XPath: {TREND_BASE_CONTAINER}")

        base_containers = tree.xpath(TREND_BASE_CONTAINER)
        print(f"  매칭 개수: {len(base_containers)}개")

        if not base_containers:
            print("  [ERROR] base_container를 찾을 수 없습니다.")
            return

        # 2. 첫 3개 아이템의 필드 테스트
        print(f"\n--- 아이템별 필드 테스트 ---")

        for idx, item in enumerate(base_containers, 1):
            print(f"\n{'='*50}")
            print(f"[아이템 {idx}]")
            print(f"{'='*50}")

            for field_name, xpath in TREND_FIELD_XPATHS.items():
                print(f"\n  [{field_name}]")
                print(f"    XPath: {xpath}")
                try:
                    results = item.xpath(xpath)
                    if results:
                        print(f"    매칭 개수: {len(results)}개")
                        values = self.extract_all_values(results)
                        for i, value in enumerate(values, 1):
                            if len(value) > 80:
                                print(f"    [{i}]: {value[:80]}...")
                            else:
                                print(f"    [{i}]: {value}")
                    else:
                        print(f"    결과: (추출 실패)")
                except Exception as e:
                    print(f"    결과: (에러) {e}")

    def test_promotion_page(self, tree):
        """프로모션페이지 XPath 테스트 (섹션 기반)"""
        print("\n" + "=" * 70)
        print("[프로모션페이지 XPath 테스트 결과]")
        print("=" * 70)

        # 1. 섹션 컨테이너 탐색
        print(f"\n--- 섹션 컨테이너 테스트 ---")
        print(f"  XPath: {PROMOTION_SECTION_CONTAINER}")
        sections = tree.xpath(PROMOTION_SECTION_CONTAINER)
        print(f"  매칭 개수: {len(sections)}개")

        if not sections:
            print("  [ERROR] 섹션을 찾을 수 없습니다.")
            return

        # 2. 섹션별 순회
        for sec_idx, section in enumerate(sections, 1):
            print(f"\n{'='*70}")
            print(f"[섹션 {sec_idx}]")
            print(f"{'='*70}")

            # promotion_type 추출 (XPath별로 각각 출력)
            print(f"\n  --- promotion_type 추출 ---")
            for field_name, xpath in PROMOTION_TYPE_XPATHS.items():
                print(f"    [{field_name}] XPath: {xpath}")
                try:
                    results = section.xpath(xpath)
                    values = self.extract_all_values(results) if results else []
                    if values:
                        print(f"    결과: {' '.join(values)[:100]}")
                    else:
                        print(f"    결과: (결과없음)")
                except Exception as e:
                    print(f"    결과: (에러) {e}")

            # 캐러셀 아이템 탐색
            print(f"\n  --- 캐러셀 아이템 ---")
            print(f"  XPath: {PROMOTION_BASE_CONTAINER}")
            items = section.xpath(PROMOTION_BASE_CONTAINER)
            print(f"  매칭 개수: {len(items)}개")

            if not items:
                print("  [WARNING] 캐러셀 아이템 없음 - 스킵")
                continue

            # 첫 2개 아이템 필드 테스트
            for idx, item in enumerate(items, 1):
                print(f"\n  [아이템 {idx}]")
                for field_name, xpath in PROMOTION_FIELD_XPATHS.items():
                    print(f"    [{field_name}] XPath: {xpath}")
                    try:
                        results = item.xpath(xpath)
                        values = self.extract_all_values(results) if results else []
                        if values:
                            print(f"    매칭 개수: {len(results)}개")
                            for i, v in enumerate(values, 1):
                                print(f"    [{i}]: {v[:80]}")
                        else:
                            print(f"    결과: (결과없음)")
                    except Exception as e:
                        print(f"    결과: (에러) {e}")

    def test_url(self, url, mode, do_scroll=True, detail_action="none"):
        """URL 테스트"""
        try:
            print(f"\n[INFO] 페이지 로딩 중: {url[:80]}...")

            if mode == 'detail':
                self.load_detail_page(url)

                if detail_action == "modal":
                    tree = self.get_tree()
                    self.test_detail_field_subset(tree, "spec button XPath", ("specs_button",))
                    self.test_spec_modal()
                    return
                elif detail_action == "similar":
                    self.test_similar_products()
                    return
                elif detail_action == "review_section":
                    self.move_to_review_section()

                if do_scroll and detail_action == "none":
                    self.scroll_to_bottom()
            else:
                if mode == 'list':
                    self.xpaths = self.build_list_xpath_map()
                self.page.get(url)
                self.close_survey_popup()
                time.sleep(random.uniform(8, 12))
                if do_scroll:
                    if mode == 'list' and LIST_BASE_CONTAINER:
                        print("[INFO] Using common URL lazy-load scroll function")
                        self.wait_for_product_urls_with_scroll(LIST_BASE_CONTAINER, 1)
                    else:
                        self.scroll_to_bottom()

            # 차단 체크
            self.handle_block()

            # HTML 파싱
            tree = self.get_tree()

            # 모드별 테스트
            if mode == 'detail':
                self.test_detail_page(tree)
            elif mode == 'trend':
                self.test_trend_page(tree)
            elif mode == 'promotion':
                self.test_promotion_page(tree)
            else:
                self.test_list_page(tree)

        except Exception as e:
            print(f"[ERROR] 테스트 실패: {e}")
            traceback.print_exc()

    def choose_detail_action(self):
        print("\n상세 추가 처리 선택:")
        print("  0. 추가 처리 없이 XPath 테스트")
        print("  1. 모달 버튼 클릭/모달 XPath 테스트")
        print("  2. 유사제품 섹션 스크롤 후 XPath 테스트")
        print("  3. 리뷰 섹션 스크롤 후 XPath 테스트")
        choice = input("선택 (0/1/2/3): ").strip()

        return {
            "0": "none",
            "1": "modal",
            "2": "similar",
            "3": "review_section",
        }.get(choice)

    def run(self):
        """실행"""
        try:
            print("\n" + "=" * 70)
            print("BestBuy TV XPath Tester (DrissionPage)")
            print("=" * 70)
            print("\n[XPath 목록]")
            print(f"  - DETAIL_XPATH_LIST: {len(DETAIL_XPATH_LIST)}개")
            print(f"  - LIST_FIELD_XPATHS: {len(LIST_FIELD_XPATHS)}개")
            print(f"  - TREND_FIELD_XPATHS: {len(TREND_FIELD_XPATHS)}개")
            print(f"  - PROMOTION_FIELD_XPATHS: {len(PROMOTION_FIELD_XPATHS)}개")

            # 드라이버 설정
            self.setup_driver()

            while True:
                print("\n" + "-" * 70)
                url = input("URL 입력 (종료: q): ").strip()

                if url.lower() == 'q':
                    print("[INFO] 종료합니다.")
                    break

                if not url:
                    print("[WARNING] URL을 입력하세요.")
                    continue

                if not url.startswith('http'):
                    url = 'https://' + url

                print("\n페이지 모드 선택:")
                print("  1. 상세페이지 (Detail) - DETAIL_XPATH_LIST 테스트")
                print("  2. 리스트페이지 (List) - LIST_FIELD_XPATHS 테스트")
                print("  3. 트렌드페이지 (Trend) - TREND_FIELD_XPATHS 테스트")
                print("  4. 프로모션페이지 (Promotion) - PROMOTION_FIELD_XPATHS 테스트")
                mode_choice = input("선택 (1/2/3/4): ").strip()

                if mode_choice == '1':
                    mode = 'detail'
                elif mode_choice == '2':
                    mode = 'list'
                elif mode_choice == '3':
                    mode = 'trend'
                elif mode_choice == '4':
                    mode = 'promotion'
                else:
                    print("[WARNING] 잘못된 선택입니다.")
                    continue

                detail_action = "none"
                if mode == "detail":
                    detail_action = self.choose_detail_action()
                    if not detail_action:
                        print("[WARNING] 잘못된 상세 추가 처리 선택입니다.")
                        continue

                # 스크롤 여부 선택
                scroll_choice = input("하단 스크롤 로딩? (y/n) [기본: y]: ").strip().lower()
                do_scroll = scroll_choice != 'n'

                # URL 테스트
                self.test_url(url, mode, do_scroll, detail_action=detail_action)

            return True

        except Exception as e:
            print(f"[ERROR] {e}")
            traceback.print_exc()
            return False

        finally:
            if self.page:
                self.page.quit()


if __name__ == "__main__":
    tester = BestBuyTVXPathTester()
    tester.run()
