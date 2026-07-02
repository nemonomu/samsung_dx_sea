"""
Amazon TV XPath Tester (DrissionPage 버전)

================================================================================
주요 기능
================================================================================
1. URL 입력 후 페이지 로드
2. 상세(Detail) / 리스트(List) 페이지 모드 선택
3. 하단 스크롤 여부 선택
4. 상세 모드: DETAIL_XPATH_LIST 테스트
5. 리스트 모드: Base container로 아이템 추출 후 각 필드 테스트

================================================================================
사용법
================================================================================
python amazon/tv/amazon_tv_xpath_tester.py

================================================================================
"""

import sys
import os
import time
import random
import traceback
from lxml import html

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
from common.amazon_base import AmazonBaseCrawler


# ================================================================================
# XPath 목록 정의 - 상세 페이지용
# 비어있는 항목은 dx_xpath_selectors DB에서 가져오거나 새로 작성해서 채워서 테스트
# ================================================================================
DETAIL_XPATH_LIST = {
    # ===== DT 크롤러 추출 필드 =====
    # 'retailer_sku_name': "//div[@id='title_feature_div']//span[@id='productTitle']/text()",
    # 'final_sku_price': "//div[@id='apex_desktop'][not(.//div[@id='apex_desktop_newAccordionRow'])]//div[contains(@class, 'apex-core-price-identifier')]//span[contains(@class, 'priceToPay')]/span[@aria-hidden='true']",
    # 'final_sku_price_fallback': "//div[@id='apex_offerDisplay_desktop']//div[@id='corePrice_feature_div']//div[contains(@class, 'apex-core-price-identifier')]//span[contains(@class, 'a-offscreen')]/text()",
    # 'final_sku_price_no_price_reason': "//div[@id='fod-cx-box']//span[@id='fod-cx-message-with-learn-more']/span[1]/text()",
    # 'final_sku_price_see_price_in_cart': "//table[@class='a-lineitem']//a[contains(text(), 'See price in cart')]/text()",
    # 'final_sku_price_unavailable': "//div[@id='outOfStock']//span[contains(@class, 'a-text-bold') and contains(., 'Currently unavailable')]/text()",
    'original_sku_price': "//div[@id='apex_desktop']//div[@id='corePriceDisplay_desktop_feature_div'][not(ancestor::*[contains(translate(@style, ' ', ''), 'display:none')])]//span[contains(@class, 'apex-basisprice-value')]//span[contains(@class, 'a-offscreen')]/text()",
    # 'discount_type': "//span[@id='dealBadgeSupportingText']/span/text()",
    # 'sku_popularity': "//span[contains(@data-a-popover, 'amazons-choice-popover')]//span[@class='a-size-small']/text()",
    # 'number_of_units_purchased_past_month': "//span[@id='social-proofing-faceout-title-tk_bought']//span[@class='a-text-bold']/text()",
    # 'delivery_availability': "//div[@id='deliveryBlockContainer']//div[@id='mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE']/span",
    # 'fastest_delivery': "//div[@id='deliveryBlockContainer']//span[@data-csa-c-delivery-price='fastest']",
    # 'available_quantity_for_purchase': "//div[@id='availabilityInsideBuyBox_feature_div']//div[@id='availability']/span[contains(@class, 'primary-availability-message')]/text()",
    # 'product_information_section': "//div[@id='prodDetails']",
    # 'technical_details_section': "//div[@id='tech' and .//h2//strong[contains(text(), 'Technical Details')]]",
    # 'item_details_button': "//div[@id='prodDetails']//a[@data-action='a-expander-toggle' and .//span[contains(text(), 'Item details')]]",
    # 'measurements_button': "//div[@id='prodDetails']//a[@data-action='a-expander-toggle' and .//span[contains(text(), 'Measurements')]]",
    # 'mfr_part_number': "//div[@id='prodDetails']//tr[th[contains(text(), 'Mfr Part Number')]]/td",
    # 'manufacturer_part_number': "//div[@id='prodDetails']//tr[th[contains(text(), 'Manufacturer Part Number')]]/td",
    # 'model_number': "//div[@id='prodDetails']//tr[th[contains(text(), 'Model Number')]]/td",
    # 'model_name': "//div[@id='prodDetails']//tr[th[contains(text(), 'Model Name')]]/td",
    # 'sku_number': "//div[@id='tech' and .//h2//strong[contains(text(), 'Technical Details')]]//strong[contains(translate(text(), 'N', 'n'), 'SKU number')]/ancestor::tr/td[2]",
    # 'model_year': "//div[@id='prodDetails']//tr[th[contains(text(), 'Model Year')]]/td",
    # 'screen_size': "//div[@id='prodDetails']//tr[th[contains(text(), 'Screen Size')]]/td",
    # 'screen_size_fallback': "//div[@id='tech' and .//h2//strong[contains(text(), 'Technical Details')]]//strong[contains(text(), 'Screen Size')]/ancestor::tr/td[2]",
    # 'review_link': "//*[@id='acrCustomerReviewLink']",
    # 'review_section': "//div[@id='customer-reviews_feature_div']",
    # 'star_rating': "//div[@id='customerReviews']//span[@data-hook='rating-out-of-text']/text()",
    # 'count_of_star_ratings': "//div[@id='customerReviews']//span[@data-hook='total-review-count']/text()",
    # 'summarized_review_content': "//div[@data-testid='overall-summary']//span[contains(@class, 'aui-primitive')]",
    # 'review_container': "//div[@data-hook='reviewContainer']",
    # 'review_content': "//div[@data-hook='reviewContainer']//div[@data-hook='reviewRichContentContainer']//p"
}

# ================================================================================
# XPath 목록 정의 - 리스트 페이지용
# 비어있는 항목은 dx_xpath_selectors DB에서 가져오거나 새로 작성해서 채워서 테스트
# ================================================================================
LIST_BASE_CONTAINER = "//li[@class='zg-no-numbers']"

# List item field XPath (relative path, starts with .//)
# Fill in only the values while testing.
LIST_FIELD_XPATHS = {
    'product_url': './/a[@class="a-link-normal aok-block" and @role="link" and .//div[contains(@class, "_cDEzb_p13n-sc-css-line-clamp")]]/@href',
    'retailer_sku_name': ".//div[contains(@class, '_cDEzb_p13n-sc-css-line-clamp')]/text()",
    'bsr_rank': ".//span[contains(@class, 'zg-bdg-text')]/text()",
}

NO_REVIEW_KEYWORDS = [
    'no customer reviews',
    'there are 0 customer reviews',
]


class AmazonTVXPathTester(AmazonBaseCrawler):
    """Amazon TV XPath 테스터 (DrissionPage 버전)"""

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

    def test_detail_page(self, tree, xpath_dict=None, label='상세페이지 XPath'):
        """상세페이지 XPath 테스트"""
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
                print(f"  XPath: (미입력 - 스킵)")
                continue
            print(f"  XPath: {xpath}")
            try:
                results = tree.xpath(xpath)
                if results:
                    found_count += 1
                    print(f"  매칭 개수: {len(results)}개")
                    values = self.extract_all_values(results)
                    for i, value in enumerate(values, 1):
                        if len(value) > 100:
                            print(f"  [{i}]: {value[:100]}...")
                        else:
                            print(f"  [{i}]: {value}")
                else:
                    not_found_count += 1
                    print(f"  결과: (추출 실패 - 매칭 없음)")
            except Exception as e:
                not_found_count += 1
                print(f"  결과: (에러) {e}")

        print("\n" + "=" * 70)
        print(f"결과 요약: 발견 {found_count}개 / 미발견 {not_found_count}개 / 미입력 {skipped_count}개")
        print("=" * 70)

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

        # 2. 전체 아이템의 필드 테스트
        print(f"\n--- 아이템별 필드 테스트 (전체 {len(base_containers)}개) ---")

        for idx, item in enumerate(base_containers, 1):
            print(f"\n{'='*50}")
            print(f"[아이템 {idx}]")
            print(f"{'='*50}")

            for field_name, xpath in LIST_FIELD_XPATHS.items():
                print(f"\n  [{field_name}]")
                if not xpath:
                    print(f"    XPath: (미입력 - 스킵)")
                    continue
                print(f"    XPath: {xpath}")
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

        # 3. 필드별 성공률 요약
        print("\n" + "=" * 70)
        print("필드별 추출 성공률 (전체 아이템 기준)")
        print("-" * 70)

        for field_name, field_xpath in LIST_FIELD_XPATHS.items():
            if not field_xpath:
                print(f"  {field_name}: (미입력 - 스킵)")
                continue
            success_count = 0
            for item in base_containers:
                try:
                    results = item.xpath(field_xpath)
                    if results:
                        success_count += 1
                except:
                    pass

            rate = (success_count / len(base_containers)) * 100
            print(f"  {field_name}: {success_count}/{len(base_containers)} ({rate:.1f}%)")

        # 4. 최종 요약
        print("\n" + "=" * 70)
        print("최종 요약")
        print("=" * 70)
        print(f"  Base Container: {LIST_BASE_CONTAINER}")
        print(f"  발견된 아이템: {len(base_containers)}개")
        print("=" * 70)

    def test_no_review_text(self, tree):
        """Test no-review text using the whole current page text."""
        print("\n" + "=" * 70)
        print("[No Review Text Test]")
        print("=" * 70)

        try:
            page_text = tree.text_content().lower() if tree is not None else ''
            matched_keywords = [
                keyword for keyword in NO_REVIEW_KEYWORDS
                if keyword in page_text
            ]

            print(f"  Keywords: {NO_REVIEW_KEYWORDS}")
            if matched_keywords:
                print(f"  Result: FOUND {matched_keywords}")
            else:
                print("  Result: NOT FOUND")
        except Exception as e:
            print(f"  Result: ERROR - {e}")

    def test_url(self, url, mode, do_scroll=True, do_click_buttons=False, review_move_mode=None, test_no_review_text=False):
        """URL 테스트"""
        try:
            print(f"\n[INFO] 페이지 로딩 중: {url[:80]}...")
            self.page.get(url)
            time.sleep(random.uniform(3, 5))

            # CAPTCHA/blocked page recovery
            self.recover_amazon_pages()

            # 스크롤
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
                    label='TV 스펙 섹션',
                )
                if found_section == 'product_information_section':
                    self.open_details_sections(['item_details_button', 'measurements_button'])

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

            # HTML 파싱
            page_html = self.page.html
            tree = html.fromstring(page_html)

            if mode == 'detail' and test_no_review_text:
                self.test_no_review_text(tree)

            # 모드별 테스트
            if mode == 'detail':
                self.test_detail_page(tree, DETAIL_XPATH_LIST, label='상세페이지 XPath')
            else:
                self.test_list_page(tree)

        except Exception as e:
            print(f"[ERROR] 테스트 실패: {e}")
            traceback.print_exc()

    def run(self):
        """실행"""
        try:
            print("\n" + "=" * 70)
            print("Amazon TV XPath Tester (DrissionPage)")
            print("=" * 70)
            print("\n[XPath 목록]")
            print(f"  - DETAIL_XPATH_LIST: {len(DETAIL_XPATH_LIST)}개")
            print(f"  - LIST_FIELD_XPATHS: {len(LIST_FIELD_XPATHS)}개")

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

                # 스크롤 여부 선택
                scroll_choice = input("하단 스크롤 로딩? (y/n) [기본: y]: ").strip().lower()
                do_scroll = scroll_choice != 'n'

                # URL 테스트
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
    tester = AmazonTVXPathTester()
    tester.run()
