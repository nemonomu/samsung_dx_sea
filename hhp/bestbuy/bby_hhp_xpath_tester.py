"""
BestBuy HHP XPath Tester (DrissionPage)

List / Detail 페이지에서 XPath가 실제로 매칭되는지 확인하는 도구.
상세페이지는 페이지 모드 선택 후 추가 처리 번호를 하나만 골라 테스트한다.
"""

import os
import random
import sys
import time
import traceback

from lxml import html

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.setup import setup_environment

setup_environment(__file__)
from common.bestbuy_base import BestBuyBaseCrawler


# =============================================================================
# List page XPath
# =============================================================================
# LIST_BASE_CONTAINER = "//li[contains(@class, 'product-list-item') or contains(@class, 'slContainer')]"
LIST_BASE_CONTAINER = "//div[@data-testid='Discover-Background-Hardline-Gradient-TestID']//div[@data-testid='badge-product-card']"

LIST_FIELD_XPATHS = {
    # main/bsr xpath
    # "product_url": ".//a[@class='product-list-item-link']/@href",
    # "retailer_sku_name": ".//*[contains(@class, 'product-title')]",
    # "offer": ".//div[@data-testid='plus-x-offers']//span[@class='font-sans text-default text-style-body-md-400']",
    # "pick_up_availability": ".//div[@class='fulfillment']//span[contains(text(), 'Pickup') or contains(text(), 'Pick up')]",
    # "fastest_delivery": ".//div[@class='fulfillment']//span[contains(., 'Shipping') or contains(., 'Get it') or contains(., 'FREE')]",
    # "sku_status": ".//div[@class='sponsored']",
    # "promotion_type": ".//span[@data-testid='button-label']/text()",
    
    # trend xpath
    "product_url": ".//a[contains(@href, '/product/') and .//h3]/@href",
    "retailer_sku_name": ".//a[contains(@href, '/product/') and .//h3]//h3",
}

LIST_PAGE_XPATHS = {
    "pagenation": "//div[contains(@class, 'show-more-progressive-container')] | //button[@data-testid='show-more-progressive-button']",
    "pagenation_fallback1": "//div[contains(@class, 'pagination-container')]",
}


# =============================================================================
# Detail page XPath keys
# =============================================================================
DETAIL_XPATHS = {
    # "top_star_rating": "//div[@data-component-name='ProductHeader']/following-sibling::div[@data-component-name='ReviewStatsContextualized']//span[contains(@class, 'font-weight-bold') and contains(@class, 'order-1')]",
    # "top_count_of_reviews": "//div[@data-component-name='ProductHeader']/following-sibling::div[@data-component-name='ReviewStatsContextualized']//span[(contains(@class, 'c-reviews') and contains(@class, 'order-2')) or contains(@class, 'c-ratings-reviews-mini')]",
    # "final_sku_price": "//div[@data-component-name='LargePrice']//div[@data-testid='price-block-customer-price']//span[not(contains(@class, 'sr-only'))]",
    # "no_longer_available": "//div[contains(@class, 'text-danger') and contains(text(), 'This item is no longer available')]",
    # "see_price_in_cart": "//div[@data-component-name='LargePrice']//span[@data-testid='price-block-error' and contains(text(), 'See price in cart')]",
    # "original_sku_price": "//span[@data-lu-target='comp_value']",
    # "savings": "//span[@data-testid='price-block-total-savings-text']",
    # "sku": "//div[contains(@class, 'disclaimer')]//div[contains(text(), 'Model:')]",
    # "sku_number": "//div[contains(@class, 'pr-150') and contains(text(), 'SKU:')]",
    # "trade_in": "//div[@data-testid='trade-in-cta-container']",
    "specs_button": "//button[contains(@class, 'show-full-specs-btn')]",
    "specs_modal_title": "//div[@data-testid='brix-sheet-header']//h2[@data-testid='brix-sheet-title' and normalize-space()='Specifications']",
    "close_button": "//button[@data-testid='brix-sheet-closeButton' or @aria-label='Close Sheet']",
    "hhp_carrier": "//div[@data-testid='brix-sheet-content']//li[.//h4[normalize-space()='Network']]//div[div[1][contains(@class, 'font-weight-medium') and normalize-space()='Carrier']]/div[2]",
    "hhp_carrier_unlocked": "//div[@data-testid='brix-sheet-content']//li[.//h4[normalize-space()='Network']]//div[div[1][contains(@class, 'font-weight-medium') and normalize-space()='Unlocked']]/div[2]",
    "hhp_color": "//div[@data-testid='brix-sheet-content']//li[.//h4[normalize-space()='General']]//div[div[1][contains(@class, 'font-weight-medium') and normalize-space()='Color']]/div[2]",
    "hhp_storage": "//div[@data-testid='brix-sheet-content']//li[.//h4[normalize-space()='Key Specs']]//div[div[1][contains(@class, 'font-weight-medium') and normalize-space()='Built-in Storage']]/div[2]",
    # "similar_products_section": "//div[@data-component-name='Compare']//h2[normalize-space()='Compare similar products']",
    # "similar_product_name": "//div[@data-component-name='Compare'][.//h2[normalize-space()='Compare similar products']]//div[contains(@class, 'product-title')]//*[self::a or self::span][normalize-space()]",
    # "review_section": "//h2[starts-with(@id, 'tabbed-customerreviews') and contains(text(), 'Reviews')]",
    # "star_rating": "//div[@data-component-name='RedesignCustomerRatingAndReviewsSummary']//span[contains(@class, 'heading-2') and contains(@class, 'font-weight-medium') and @aria-hidden='true']/text()",
    # "count_of_reviews": "//div[@data-component-name='RedesignCustomerRatingAndReviewsSummary']//div[contains(@class, 'v-text-dark-gray') and contains(@class, 'text-center') and contains(text(), 'reviews')]/text()",
    # "recommendation_intent": "//div[contains(., 'would recommend to a friend')]/span[@class='font-weight-bold']/text()",
    # "reviews_button": "//a[contains(@href, '/reviews') and .//span[contains(text(), 'See all customer reviews')]]",
    # "reviews_button_fallback1": "//a[contains(@href, '/reviews') and contains(., 'See all customer reviews')]",
    # "detailed_review_content": "//ul[@id='stand-alone-review-list']/li//p[starts-with(@id, 'ugc-line-clamp-reviews-')]/text()",
    # "detailed_review_content_fallback1": "//ul[@class='reviews-list']/li[@class='review-item']//div[@class='ugc-review-body']/p[@class='pre-white-space']/text()",
    # "reviewpage_recommendation_intent": "//div[span[contains(text(), 'would recommend to a friend')]]/span[contains(@class, 'font-500')]",
    # "reviewpage_recommendation_intent_fallback": "//svg[contains(@aria-label, 'would recommend')]/..//span[contains(@class, 'text-5')]",
    # "reviewpage_recommendation_intent_fallback2": "//div[contains(@class, 'recommendation-card')]//span[contains(@class, 'recommendation-percent')]",
    # "reviewpage_recommendation_intent_fallback3": "//div[contains(@class, 'recommendation-card')]//span[contains(@class, 'donut-percent-text')]/text()",
}


class BestBuyHHPXPathTester(BestBuyBaseCrawler):
    """BestBuy HHP XPath tester."""

    def __init__(self):
        super().__init__()
        self.account_name = "Bestbuy"
        self.page_type = "xpath_tester"
        self.bestbuy_zip_code = "10010"
        self.bestbuy_search_keyword = "cellphone"
        self.page = None
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
        print("[INFO] BestBuy browser setup...")
        if not self.setup_bestbuy_browser():
            raise RuntimeError("BestBuy browser setup failed")
        print("[INFO] BestBuy browser setup complete")

    def get_tree(self):
        page_html = self.page.run_js("return document.documentElement.outerHTML")
        return html.fromstring(page_html)

    def extract_value(self, element):
        if hasattr(element, "text_content"):
            return element.text_content().strip()
        return str(element).strip()

    def extract_all_values(self, elements):
        values = []
        for element in elements:
            value = self.extract_value(element)
            if value:
                values.append(value)
        return values

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

    def test_list_page(self, tree):
        self.xpaths = self.build_list_xpath_map()

        print("\n" + "=" * 70)
        print("[리스트페이지 XPath 테스트 결과]")
        print("=" * 70)

        print(f"\n--- Base Container 테스트 ---")
        print(f"  XPath: {LIST_BASE_CONTAINER}")

        base_containers = tree.xpath(LIST_BASE_CONTAINER)
        print(f"  매칭 개수: {len(base_containers)}개")

        if not base_containers:
            print("  [ERROR] base_container를 찾을 수 없습니다.")
            return

        print(f"\n--- 페이지 단위 필드 테스트 ---")
        for field_name, xpath in LIST_PAGE_XPATHS.items():
            print(f"\n  [{field_name}]")
            print(f"    XPath: {xpath}")
            try:
                results = tree.xpath(xpath)
                if results:
                    print(f"    매칭 개수: {len(results)}개")
                    values = self.extract_all_values(results)
                    for i, value in enumerate(values, 1):
                        if not value:
                            value = "(빈값)"
                        elif len(value) > 60:
                            value = value[:60] + "..."
                        print(f"    [{i}]: {value}")
                else:
                    print(f"    결과: (추출 실패)")
            except Exception as e:
                print(f"    결과: (에러) {e}")

        print(f"\n--- 아이템별 필드 테스트 (전체 {len(base_containers)}개) ---")

        for idx, item in enumerate(base_containers, 1):
            print(f"\n{'=' * 50}")
            print(f"[아이템 {idx}]")
            print(f"{'=' * 50}")

            for field_name, xpath in LIST_FIELD_XPATHS.items():
                print(f"\n  [{field_name}]")
                print(f"    XPath: {xpath}")
                if not xpath:
                    print("    결과: (XPath 미설정)")
                    continue
                try:
                    results = item.xpath(xpath)
                    if results:
                        print(f"    매칭 개수: {len(results)}개")
                        values = self.extract_all_values(results)
                        for i, value in enumerate(values, 1):
                            if not value:
                                value = "(빈값)"
                            elif len(value) > 60:
                                value = value[:60] + "..."
                            print(f"    [{i}]: {value}")
                    else:
                        print(f"    결과: (추출 실패)")
                except Exception as e:
                    print(f"    결과: (에러) {e}")

        print("\n" + "=" * 70)
        print("필드별 추출 성공률 (전체 아이템 기준)")
        print("-" * 70)

        for field_name, field_xpath in LIST_FIELD_XPATHS.items():
            success_count = 0
            for item in base_containers:
                try:
                    results = item.xpath(field_xpath)
                    if results:
                        success_count += 1
                except Exception:
                    pass

            rate = (success_count / len(base_containers)) * 100
            print(f"  {field_name}: {success_count}/{len(base_containers)} ({rate:.1f}%)")

        print("\n" + "=" * 70)
        print("최종 요약")
        print("=" * 70)
        print(f"  Base Container: {LIST_BASE_CONTAINER}")
        print(f"  발견된 아이템: {len(base_containers)}개")
        print("=" * 70)

    def load_url(self, url, mode):
        print(f"\n[INFO] Loading page: {url[:100]}...")
        if mode == "detail":
            self.load_detail_page(url)
        else:
            self.page.get(url)
            time.sleep(random.uniform(4, 6))
            self.close_survey_popup()

    def get_detail_xpath_candidates(self, base_field_name):
        candidates = []
        for field_name, xpath in DETAIL_XPATHS.items():
            if field_name == base_field_name or field_name.startswith(base_field_name + "_fallback"):
                if xpath:
                    candidates.append((field_name, xpath))
        return candidates

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
            ("specs_modal_title", "hhp_carrier", "hhp_carrier_unlocked", "hhp_color", "hhp_storage"),
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

    def move_to_review_button(self, click=False):
        candidates = self.get_detail_xpath_candidates("reviews_button")
        if not candidates:
            print("  [review] reviews_button XPath is not set")
            return False

        if click:
            found = self.scroll_find_element(
                candidates,
                max_scrolls=5,
                label="review button",
                click=True,
            )
            print(f"  [review] reviews_button click {'success' if found else 'failed'}")
            return found

        element, matched_name = self.scroll_find_element(
            candidates,
            max_scrolls=5,
            label="review button",
            return_element=True,
        )
        print(f"  [review] reviews_button move {'success' if element else 'failed'} (matched: {matched_name or '-'})")
        return bool(element)

    def test_review_page(self):
        if not self.move_to_review_button(click=True):
            print("  [review page] skipped")
            return

        time.sleep(2)
        tree = self.get_tree()
        self.test_detail_field_subset(
            tree,
            "review page XPath",
            ("detailed_review_content", "reviewpage_recommendation_intent"),
        )

    def test_url(
        self,
        url,
        mode,
        do_scroll=True,
        detail_action="none",
    ):
        try:
            self.xpaths = self.build_xpath_map(DETAIL_XPATHS)
            self.load_url(url, mode)

            if mode != "detail":
                self.xpaths = self.build_list_xpath_map()

                if do_scroll:
                    if LIST_BASE_CONTAINER:
                        print("[INFO] Using common URL lazy-load scroll function")
                        self.wait_for_product_urls_with_scroll(LIST_BASE_CONTAINER, 1)
                    else:
                        print("[INFO] base_container not set - using basic bottom scroll")
                        self.scroll_to_bottom()

                tree = self.get_tree()
                self.test_list_page(tree)
                return

            if detail_action == "modal":
                pre_tree = self.get_tree()
                self.test_detail_field_subset(pre_tree, "spec button XPath", ("specs_button",))
                self.test_spec_modal()

            elif detail_action == "similar":
                self.test_similar_products()
                return

            elif detail_action == "review_section":
                self.move_to_review_section()

            elif detail_action == "review_button":
                self.move_to_review_button(click=False)

            elif detail_action == "review_page":
                self.test_review_page()
                return

            if do_scroll and detail_action == "none":
                print("[INFO] Scrolling to bottom...")
                self.scroll_to_bottom()

            tree = self.get_tree()
            self.test_xpath_group(tree, DETAIL_XPATHS, "Detail XPath")

        except Exception as e:
            print(f"[ERROR] Test failed: {e}")
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
        try:
            print("\n" + "=" * 70)
            print("BestBuy HHP XPath Tester")
            print("=" * 70)
            print("LIST_BASE_CONTAINER: 1 key")
            print(f"LIST_PAGE_XPATHS: {len(LIST_PAGE_XPATHS)} keys")
            print(f"LIST_FIELD_XPATHS: {len(LIST_FIELD_XPATHS)} keys")
            print(f"DETAIL_XPATHS: {len(DETAIL_XPATHS)} keys")

            self.setup_driver()

            while True:
                print("\n" + "-" * 70)
                url = input("URL 입력 (종료: q): ").strip()

                if url.lower() == "q":
                    print("[INFO] 종료합니다.")
                    break

                if not url:
                    print("[WARNING] URL을 입력하세요.")
                    continue

                if not url.startswith("http"):
                    url = "https://" + url

                print("\n페이지 모드 선택:")
                print("  1. 상세페이지 (Detail)")
                print("  2. 리스트페이지 (List)")
                mode_choice = input("선택 (1/2): ").strip()

                if mode_choice == "1":
                    mode = "detail"
                elif mode_choice == "2":
                    mode = "list"
                else:
                    print("[WARNING] 잘못된 페이지 모드입니다.")
                    continue

                detail_action = "none"

                if mode == "detail":
                    detail_action = self.choose_detail_action()
                    if not detail_action:
                        print("[WARNING] 잘못된 상세 추가 처리 선택입니다.")
                        continue

                scroll_choice = input("하단 스크롤 로딩? (y/n) [기본: y]: ").strip().lower()
                do_scroll = scroll_choice != "n"

                self.test_url(
                    url,
                    mode,
                    do_scroll,
                    detail_action=detail_action,
                )

            return True

        except Exception as e:
            print(f"[ERROR] {e}")
            traceback.print_exc()
            return False

        finally:
            if self.page:
                self.page.quit()


if __name__ == "__main__":
    tester = BestBuyHHPXPathTester()
    tester.run()
