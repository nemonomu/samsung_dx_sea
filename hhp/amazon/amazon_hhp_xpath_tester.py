"""
Amazon HHP XPath Tester (DrissionPage)

1. Load a URL.
2. Choose detail/list mode.
3. Optionally scroll before XPath testing.
4. In detail mode, optionally test configured button/review navigation.
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
from common.amazon_base import AmazonBaseCrawler


DETAIL_XPATH_LIST = {
    # 'retailer_sku_name': "//div[@id='title_feature_div']//span[@id='productTitle']/text()",
    # 'final_sku_price': "//div[@id='apex_desktop'][not(.//div[@id='apex_desktop_newAccordionRow'])]//div[contains(@class, 'apex-core-price-identifier')]//span[contains(@class, 'priceToPay')]/span[@aria-hidden='true']",
    # 'final_sku_price_fallback': "//div[contains(@class, 'a-accordion-active')]//div[@id='apex_offerDisplay_desktop']//div[@id='corePrice_feature_div']//div[contains(@class, 'apex-core-price-identifier')]//span[contains(@class, 'a-offscreen')]/text()",
    # 'final_sku_price_fallback2': "//div[@id='apex_offerDisplay_desktop' and not(ancestor::div[@id='buyBoxAccordion'])]//div[@id='corePrice_feature_div']//div[contains(@class, 'apex-core-price-identifier')]//span[contains(@class, 'a-offscreen')]/text()",
    # 'final_sku_price_fallback_no_price_reason': "//div[@id='fod-cx-box']//span[@id='fod-cx-message-with-learn-more']/span[1]/text()",
    # 'final_sku_price_see_price_in_cart': "//table[@class='a-lineitem']//a[contains(text(), 'See price in cart')]/text()",
    # 'final_sku_price_unavailable': "//div[@id='outOfStock']//span[contains(@class, 'a-text-bold') and contains(., 'Currently unavailable')]/text()",
    #'original_sku_price': "//div[@id='apex_desktop'][not(.//div[@id='apex_desktop_newAccordionRow'])]//span[contains(@class, 'aok-offscreen') and contains(text(), 'List Price')]/text()",
    #'original_sku_price_fallback': "//div[@id='apex_desktop']//div[@id='apex_desktop_newAccordionRow' and not(contains(@style, 'display:none'))]//div[@id='corePriceDisplay_desktop_feature_div']//span[contains(@class, 'apex-basisprice-value')]//span[contains(@class, 'a-offscreen')]/text()",
    # 'discount_type': "//span[@id='dealBadgeSupportingText']/span/text()",
    # 'discount_type_fallback': "//div[@id='delightPricingBadge_feature_div']//span[contains(@class, 'delight-pricing-badge-label-text')]/text()",
    # 'sku_popularity': "//span[contains(@data-a-popover, 'amazons-choice-popover')]//span[@class='a-size-small']/text()",
    # 'number_of_units_purchased_past_month': "//span[@id='social-proofing-faceout-title-tk_bought']//span[@class='a-text-bold']/text()",
    # 'available_quantity_for_purchase': "//div[contains(@class, 'a-accordion-active')]//div[contains(@class, 'a-accordion-inner')]//div[@id='availabilityInsideBuyBox_feature_div']//div[@id='availability']/span[contains(@class, 'primary-availability-message')]/text()",
    # 'available_quantity_for_purchase_fallback': "//div[@id='availabilityInsideBuyBox_feature_div' and not(ancestor::div[@id='buyBoxAccordion'])]//div[@id='availability']/span[contains(@class, 'primary-availability-message')]/text()",
    # 'delivery_availability': "//div[@id='buyBoxAccordion']//div[contains(concat(' ', normalize-space(@class), ' '), ' a-accordion-active ')]//div[contains(concat(' ', normalize-space(@class), ' '), ' a-accordion-inner ')]//div[@id='deliveryBlockContainer']//div[@id='mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE']/span",
    # 'delivery_availability_fallback': "//div[@id='deliveryBlockContainer' and not(ancestor::div[@id='buyBoxAccordion'])]//div[@id='mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE']/span",
    # 'fastest_delivery': "//div[@id='buyBoxAccordion']//div[contains(concat(' ', normalize-space(@class), ' '), ' a-accordion-active ')]//div[contains(concat(' ', normalize-space(@class), ' '), ' a-accordion-inner ')]//div[@id='deliveryBlockContainer']//span[@data-csa-c-delivery-price='fastest']",
    # 'fastest_delivery_fallback': "//div[@id='deliveryBlockContainer' and not(ancestor::div[@id='buyBoxAccordion'])]//span[@data-csa-c-delivery-price='fastest']",
    # 'hhp_carrier': "//span[@id='inline-twister-expanded-dimension-text-service_provider']/text()",
    # 'bundle': "//div[@id='bundle-drawer-carousel']//li[contains(@class, 'bundle')]//a[@class='bundle-box-link']/@title",
    # 'trade_in': '//span[contains(text(), "Trade-in and save")]/following::div[@id="NO_INTENT_DOM_RENDER"][1]//div[@class="utxDynamicLongMessage"]/span/text() | //span[contains(text(), "Trade-in and save")]/following::div[@id="NO_INTENT_DOM_RENDER"][1]//div[@class="utxDynamicLongMessage"]//span[@class="a-offscreen"]/text()',
     'product_information_section': "//div[@id='prodDetails']",
     'technical_details_section': "//div[@id='tech' and .//h2//strong[contains(text(), 'Technical Details')]]",
     'item_details_button': "//div[@id='prodDetails']//a[@data-action='a-expander-toggle' and .//span[normalize-space(.)='Item details']]",
    'additional_details_button': "//div[@id='prodDetails']//a[@data-action='a-expander-toggle' and .//span[normalize-space(.)='Additional details']]",
    # 'item_model_number': "//div[@id='prodDetails']//table[contains(@class, 'prodDetTable')]//tr[th[contains(normalize-space(.), 'Item model number')]]/td",
     #'hhp_storage': "//div[@id='prodDetails']//table[contains(@class, 'prodDetTable')]//tr[th[contains(normalize-space(.), 'Memory Storage Capacity')]]/td/text()",
    # 'hhp_color': "//div[@id='prodDetails']//table[contains(@class, 'prodDetTable')]//tr[th[contains(normalize-space(.), 'Color')]]/td/text()",
    'hhp_memory_ram' : "//div[@id='prodDetails']//tr[th[normalize-space(.)='RAM Memory Installed']]/td[contains(@class,'prodDetAttrValue')]",
    # 'review_link': "//*[@id='acrCustomerReviewLink']",
    # 'review_section': "//div[@id='customer-reviews_feature_div']",
    # 'star_rating': "//div[@id='customerReviews']//span[@data-hook='rating-out-of-text']/text()",
    # 'count_of_star_ratings': "//div[@id='customerReviews']//span[@data-hook='total-review-count']/text()",
    # 'summarized_review_content': "//div[@data-testid='overall-summary']//span[contains(@class, 'aui-primitive')]",
    # 'review_container': "//div[@data-hook='reviewContainer']",
    # 'review_content': ".//div[@data-hook='reviewRichContentContainer']//p"
}
# main 베이스 컨테이너
# LIST_BASE_CONTAINER = "//div[@role='listitem' and @data-component-type='s-search-result' and @data-asin and contains(@class, 's-asin') and not(contains(@class, 'AdHolder')) and not(ancestor::li[contains(@class, 'a-carousel-card')]) and not(ancestor::*[contains(@data-uuid, 's-searchgrid-carousel')])]"
# bsr 베이스 컨테이너 
LIST_BASE_CONTAINER = "//li[@class='zg-no-numbers']"

LIST_FIELD_XPATHS = {
    # 'product_url': ".//a[.//h2]/@href",
    # 'retailer_sku_name': ".//a[.//h2]//h2//span/text()",
    # 'number_of_units_purchased_past_month': ".//span[contains(@class, 'a-color-secondary') and contains(text(), 'bought in past month')]",
    # 'delivery_availability': ".//div[@data-cy='delivery-recipe']//div[@data-cy='delivery-block']//div[contains(@class, 'udm-primary-delivery-message')]//text()[not(ancestor::style) and not(ancestor::script) and normalize-space()]",
    # 'fastest_delivery': ".//div[@data-cy='delivery-recipe']//div[@data-cy='delivery-block']//div[contains(@class, 'udm-secondary-delivery-message') and starts-with(., 'Or fastest')]",
    # 'available_quantity_for_purchase': ".//span[@class='a-size-base a-color-price' and (contains(text(), 'left in stock') or contains(text(), 'Only'))]",
    # 'discount_type': ".//div[contains(@class, 'a-row') and .//span[contains(@class, 'a-badge-text')]]//text()[not(ancestor::style) and not(ancestor::script) and normalize-space()]",

    # ===== BSR 용 =====
    'product_url': ".//a[@class='a-link-normal aok-block' and @role='link' and .//div[contains(@class, '_cDEzb_p13n-sc-css-line-clamp')]]/@href",
    'retailer_sku_name': ".//div[contains(@class, '_cDEzb_p13n-sc-css-line-clamp')]/text()",
    'bsr_rank': ".//span[contains(@class, 'zg-bdg-text')]/text()",
}

NO_REVIEW_KEYWORDS = [
    'no customer reviews',
    'there are 0 customer reviews',
]


class AmazonHHPXPathTester(AmazonBaseCrawler):
    """Amazon HHP XPath tester using the same flow as the TV tester."""

    def __init__(self):
        super().__init__()
        self.account_name = 'Amazon'
        self.amazon_zip_code = '10001'
        self.page_type = 'xpath_tester'
        self.capture_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'capture')
        self.page = None

    def setup_driver(self):
        """Use AmazonBaseCrawler browser setup."""
        if not self.setup_browser():
            raise RuntimeError("AmazonBaseCrawler browser setup failed")

    def extract_value(self, element):
        if hasattr(element, 'text_content'):
            return element.text_content().strip()
        return str(element).strip()

    def extract_all_values(self, elements):
        values = []
        for elem in elements:
            value = self.extract_value(elem)
            if value:
                values.append(value)
        return values

    def test_detail_page(self, tree, xpath_dict=None, label='상세페이지 XPath'):
        if xpath_dict is None:
            xpath_dict = DETAIL_XPATH_LIST

        print("\n" + "=" * 70)
        print(f"[{label} 테스트 결과]")
        print("=" * 70)

        if not xpath_dict:
            print(f"[INFO] {label}가 비어있습니다. XPath를 추가하세요.")
            return

        found_count = 0
        not_found_count = 0
        skipped_count = 0

        for field_name, xpath in xpath_dict.items():
            print(f"\n[{field_name}]")
            if not xpath:
                skipped_count += 1
                print("  XPath: (미입력 - 스킵)")
                continue

            print(f"  XPath: {xpath}")
            try:
                results = tree.xpath(xpath)
                if results:
                    found_count += 1
                    print(f"  매칭 개수: {len(results)}")
                    values = self.extract_all_values(results)
                    for i, value in enumerate(values, 1):
                        print(f"  [{i}]: {value[:100]}..." if len(value) > 100 else f"  [{i}]: {value}")
                else:
                    not_found_count += 1
                    print("  결과: (추출 실패 - 매칭 없음)")
            except Exception as e:
                not_found_count += 1
                print(f"  결과: (에러) {e}")

        print("\n" + "=" * 70)
        print(f"결과 요약: 발견 {found_count}개 / 미발견 {not_found_count}개 / 미입력 {skipped_count}개")
        print("=" * 70)

    def test_list_page(self, tree):
        print("\n" + "=" * 70)
        print("[리스트페이지 XPath 테스트 결과]")
        print("=" * 70)

        print("\n--- Base Container 테스트 ---")
        print(f"  XPath: {LIST_BASE_CONTAINER}")

        base_containers = tree.xpath(LIST_BASE_CONTAINER)
        print(f"  매칭 개수: {len(base_containers)}")

        if not base_containers:
            print("  [ERROR] base_container를 찾을 수 없습니다.")
            return

        print(f"\n--- 아이템별 필드 테스트 (전체 {len(base_containers)}개) ---")
        for idx, item in enumerate(base_containers, 1):
            print(f"\n{'=' * 50}")
            print(f"[아이템 {idx}]")
            print(f"{'=' * 50}")

            for field_name, xpath in LIST_FIELD_XPATHS.items():
                print(f"\n  [{field_name}]")
                if not xpath:
                    print("    XPath: (미입력 - 스킵)")
                    continue

                print(f"    XPath: {xpath}")
                try:
                    results = item.xpath(xpath)
                    if results:
                        print(f"    매칭 개수: {len(results)}")
                        values = self.extract_all_values(results)
                        for i, value in enumerate(values, 1):
                            value = value[:60] + "..." if len(value) > 60 else value
                            print(f"    [{i}]: {value or '(빈값)'}")
                    else:
                        print("    결과: (추출 실패)")
                except Exception as e:
                    print(f"    결과: (에러) {e}")

        print("\n" + "=" * 70)
        print("필드별 추출 성공률")
        print("-" * 70)
        for field_name, field_xpath in LIST_FIELD_XPATHS.items():
            if not field_xpath:
                print(f"  {field_name}: (미입력 - 스킵)")
                continue
            success_count = 0
            for item in base_containers:
                try:
                    if item.xpath(field_xpath):
                        success_count += 1
                except Exception:
                    pass
            rate = (success_count / len(base_containers)) * 100
            print(f"  {field_name}: {success_count}/{len(base_containers)} ({rate:.1f}%)")
        print("=" * 70)

    def test_no_review_text(self, tree):
        print("\n" + "=" * 70)
        print("[No Review Text 테스트]")
        print("=" * 70)

        try:
            page_text = tree.text_content().lower() if tree is not None else ''
            matched_keywords = [
                keyword for keyword in NO_REVIEW_KEYWORDS
                if keyword in page_text
            ]
            print(f"  Keywords: {NO_REVIEW_KEYWORDS}")
            print(f"  결과: FOUND {matched_keywords}" if matched_keywords else "  결과: NOT FOUND")
        except Exception as e:
            print(f"  결과: ERROR - {e}")

    def test_url(self, url, mode, do_scroll=True, do_click_buttons=False, review_move_mode=None, test_no_review_text=False):
        try:
            print(f"\n[INFO] 페이지 로딩 중: {url[:80]}...")
            self.page.get(url)
            time.sleep(random.uniform(3, 5))

            self.recover_amazon_pages()

            if do_scroll:
                print("[INFO] 하단 스크롤 중...")
                self.scroll_to_bottom()

            if mode == 'detail' and do_click_buttons:
                print("[INFO] 스펙 버튼 클릭 테스트 중...")
                self.xpaths = {
                    field_name: {'xpath': xpath}
                    for field_name, xpath in DETAIL_XPATH_LIST.items()
                    if xpath
                }
                found_section = self.scroll_to_section(
                    ['product_information_section', 'technical_details_section'],
                    label='HHP 스펙 섹션',
                )
                if found_section == 'product_information_section':
                    self.open_details_sections(['item_details_button', 'additional_details_button'])

            if mode == 'detail' and review_move_mode:
                self.xpaths = {
                    field_name: {'xpath': xpath}
                    for field_name, xpath in DETAIL_XPATH_LIST.items()
                    if xpath
                }

                if review_move_mode == 'link':
                    print("[INFO] 리뷰 링크 클릭 이동 테스트 중...")
                    review_link_xpath = self.xpaths.get('review_link', {}).get('xpath')
                    if not review_link_xpath:
                        print("  [리뷰] review_link XPath 없음")
                    else:
                        try:
                            self.page.run_js("window.scrollTo(0, 0)")
                            time.sleep(0.5)
                            review_link = self.page.ele(f'xpath:{review_link_xpath}', timeout=3)
                            if not review_link:
                                raise Exception("review_link not found")
                            review_link.click()
                            time.sleep(1)
                            print("  [리뷰] review_link 클릭 성공")
                        except Exception as e:
                            print(f"  [리뷰] review_link 클릭 실패: {e}")

                elif review_move_mode == 'section':
                    print("[INFO] 리뷰 섹션 스크롤 이동 테스트 중...")
                    moved = self.scroll_to_section('review_section', max_scrolls=10, label='리뷰 섹션')
                    print(f"  [리뷰] review_section 이동 {'성공' if moved else '실패'}")

            page_html = self.page.html
            tree = html.fromstring(page_html)

            if mode == 'detail' and test_no_review_text:
                self.test_no_review_text(tree)

            if mode == 'detail':
                self.test_detail_page(tree, DETAIL_XPATH_LIST, label='상세페이지 XPath')
            else:
                self.test_list_page(tree)

        except Exception as e:
            print(f"[ERROR] 테스트 실패: {e}")
            traceback.print_exc()

    def run(self):
        try:
            print("\n" + "=" * 70)
            print("Amazon HHP XPath Tester (DrissionPage)")
            print("=" * 70)
            print("\n[XPath 목록]")
            print(f"  - DETAIL_XPATH_LIST: {len(DETAIL_XPATH_LIST)}개")
            print(f"  - LIST_FIELD_XPATHS: {len(LIST_FIELD_XPATHS)}개")

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
                mode_choice = input("선택 (1/2): ").strip()

                if mode_choice == '1':
                    mode = 'detail'
                elif mode_choice == '2':
                    mode = 'list'
                else:
                    print("[WARNING] 잘못된 선택입니다.")
                    continue

                click_buttons = False
                review_move_mode = None
                test_no_review_text = False
                if mode == 'detail':
                    button_choice = input("스펙 버튼 클릭? (y/n) [기본: n]: ").strip().lower()
                    click_buttons = button_choice == 'y'

                    print("\n리뷰 이동 테스트:")
                    print("  0. 안함")
                    print("  1. review_link 클릭")
                    print("  2. review_section 스크롤")
                    review_choice = input("선택 (0/1/2) [기본: 0]: ").strip()
                    if review_choice == '1':
                        review_move_mode = 'link'
                    elif review_choice == '2':
                        review_move_mode = 'section'
                        test_no_review_text = True

                scroll_choice = input("하단 스크롤 로딩? (y/n) [기본: y]: ").strip().lower()
                do_scroll = scroll_choice != 'n'

                self.test_url(url, mode, do_scroll, click_buttons, review_move_mode, test_no_review_text)

            return True

        except Exception as e:
            print(f"[ERROR] {e}")
            traceback.print_exc()
            return False

        finally:
            if self.page:
                self.page.quit()


if __name__ == "__main__":
    tester = AmazonHHPXPathTester()
    tester.run()
