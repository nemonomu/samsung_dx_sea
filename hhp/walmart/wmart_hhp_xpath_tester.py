"""
Walmart HHP XPath Tester (DrissionPage 버전)

================================================================================
주요 기능
================================================================================
1. URL 입력 후 페이지 로드
2. 상세(Detail) / 리스트(List) 페이지 모드 선택
3. 하단 스크롤 여부 선택
4. 상세 모드: 모달 클릭 여부 선택
   - n: DETAIL_XPATH_LIST 테스트
   - y: 스펙 모달 열고 MODAL_XPATH_LIST만 테스트 (sku/screen_size 등 모달 안 필드)
5. 리스트 모드: Base container로 아이템 추출 후 각 필드 테스트

================================================================================
사용법
================================================================================
python hhp/walmart/wmart_hhp_xpath_tester.py

================================================================================
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
from common.walmart_base import WalmartBaseCrawler


# ================================================================================
# XPath 목록 정의 - 상세 페이지용
# 비어있는 항목은 dx_xpath_selectors DB에서 가져오거나 새로 작성해서 채워서 테스트
# ================================================================================
DETAIL_XPATH_LIST = {
    # ===== 1단계: 상단 별점/리뷰 영역 (페이지 로드 직후, 스크롤 불필요) =====
    # header_rating으로 통합 텍스트 추출 → 정규식으로 별점/별점수 분리
    # 'no_ratings_yet': '//span[contains(text(), "No ratings yet")]',
    # 'header_rating': '//div[@data-testid="reviews-and-ratings"]//div[@role="group"]/@aria-label',

    # ===== 1-1단계: 가격 (페이지 로드 직후) =====
    # 'final_sku_price': '//span[@itemprop="price" and @data-seo-id="hero-price"]/text()',
    # 'final_sku_price_fallback': "//div[@data-testid='postpaid-price']//span[@data-fs-element='price']//span[contains(., '$')]/text()",
    # 'not_available': '//div[@data-testid="add-to-cart-section"]//div[contains(@class, "dark-gray") and contains(text(), "Not Available")]/text()',
    # 'see_more_seller_options': '//button[contains(text(), "See more seller options")]/text()',
    # 'original_sku_price': '//span[@data-seo-id="strike-through-price"]/text()',
    # 'savings': "//div[@data-testid='dollar-saving']//span[contains(@class, 'b') and contains(@style, 'color:#267A03') and contains(., '$')]/text()",

    # ===== 1-2단계: 구매/장바구니/인기도/할인 (페이지 로드 직후) =====
    # 'number_of_ppl_purchased_yesterday': "//div[@data-testid='flex-container']//div[@data-testid='module-2-badges']//span[contains(text(), 'bought since yesterday')]/text()",
    # 'number_of_ppl_added_to_carts': "//div[@data-testid='flex-container']//div[@data-testid='module-2-badges']//span[contains(text(), 'people') and contains(text(), 'carts')]/text()",
    # 'sku_popularity': "//div[@data-testid='flex-container']//div[@data-testid='module-2-badges']//span[not(contains(text(), 'bought since yesterday')) and not(contains(text(), 'carts'))]/text()",
    # 'discount_type': "//div[@data-testid='ip-legal-policy-component' and not(ancestor::div[contains(@class, 'sticky-buy-box-column')])]//span[@class='']/text()",

    # ===== 2단계: HHP 스펙 모달은 별도 MODAL_XPATH_LIST에서 테스트 (모달 열기 후만 유효) =====

    # ===== 3단계: 유사 제품 (스크롤 fallback용 섹션 + 절대경로로 카드 이름 추출) =====
    # 'similar_products_section': "//div[contains(@class, 'expand-collapse-header') and .//h2[contains(text(), 'Compare with similar items')]]",
    # 'similar_product_name': "//section[@data-dca-name='itemTile']//div[@role='group' and starts-with(@data-testid, 'product-tile-') and not(@data-testid='product-tile-1')]//span[@data-automation-id='product-title']/text()",

    # ===== 4단계: 하단 별점/리뷰 fallback 필드 (페이지 하단까지 스크롤 후 접근) =====
    # 'review_section': "//section[@id='item-review-section']//h2[contains(text(), 'Customer ratings')]",
    # 'star_rating': "//span[contains(@class, 'f-headline') and contains(text(), 'out of 5')]/text()",
    # 'count_of_star_ratings': "//section[@id='item-review-section']//span[contains(text(), 'rating') and following-sibling::*[contains(text(), 'review')]]/text()",
    # 'count_of_reviews': "//section[@id='item-review-section']//*[contains(text(), 'review') and preceding-sibling::span[contains(text(), 'rating')]]/text()",
    # 'detail_page_count_of_reviews': "//section[@id='item-review-section']//h3[contains(text(), 'Showing') and contains(text(), 'reviews')]/text()",
    # 'reviews_button': "//section[@id='item-review-section']//button[contains(text(), 'View all reviews') and not(contains(text(), '('))]",
    # 'reviews_button_fallback': "//section[@id='item-review-section']//button[contains(text(), 'View all reviews') and contains(text(), '(')]",

    # ===== 5단계: 리뷰 페이지 (reviews_button 클릭 → 리뷰 상세 페이지 이동 후 추출) =====
    'review_page_count_of_reviews': "//div[@role='heading' and @aria-level='2' and contains(text(), 'Showing') and contains(text(), 'reviews')]/text()",  # 리뷰 페이지에서만 접근 (K/M 변환 대체용)
    'detailed_review_content': "//span[contains(@class, 'tl-m') and contains(@class, 'db-m')]",
    'review_pagination': '//a[@data-automation-id="page-number" and text()="1"]',  
}

# ================================================================================
# XPath 목록 정의 - 모달 버튼 (클릭 액션 대상, 모달 열기 전 페이지에서 매칭)
# ================================================================================
MODAL_BUTTON_XPATH_LIST = {
    'spec_toggle_button': "//section[@id='specifications-wrapper']//button[@aria-label='Specifications']",
    'more_details_button': "//button[@aria-label='More details']",
}

# ================================================================================
# XPath 목록 정의 - 모달 안 추출용 (모달 열기 후에만 테스트)
# ================================================================================
MODAL_XPATH_LIST = {
    # 모달 열림 확인 — "More details" 제목이 있으면 모달이 열린 상태
    'modal_opened': "//h2[contains(@class, 'ModalPortal_title') and contains(text(), 'More details')]/text()",
    # 'Model' 매칭 (Model name 제외) + 모달 안으로 scoping
    'sku': "//div[contains(@class, 'ModalPortal')]//h3[contains(text(), 'Model') and not(contains(text(), 'name'))]/following-sibling::div/span/text()",
    'hhp_carrier': "//h3[contains(text(), 'Service provider')]/following-sibling::div/span/text()",
    'hhp_color': "//h3[text()='Color']/following-sibling::div/span/text()",
    'hhp_storage': "//h3[contains(text(), 'HD capacity')]/following-sibling::div/span/text()",
   
}

# ================================================================================
# XPath 목록 정의 - 리스트 페이지용 (main 수집 필드)
# ================================================================================
# 기본 사용할 XPath
LIST_BASE_CONTAINER = "//div[@data-testid='item-stack']/div[.//div[@data-item-id]]"

# 리스트 아이템 내 필드 XPath (상대 경로 - .// 로 시작)
# 비어있는 항목은 dx_xpath_selectors DB에 등록된 값으로 채워서 테스트하면 됨
LIST_FIELD_XPATHS = {
    "product_url": ".//a[@link-identifier]/@href",
    "retailer_sku_name": ".//*[@data-automation-id='product-title']/text()",
    "sku_status_1": ".//span[@data-testid='badgeTagComponent']//span[contains(., 'Rollback')]/text()",
    "sku_status_2": ".//div[@data-test-id='gpt-sponsored-tag-container']//div[text()='Sponsored']/text()",
    "delivery_availability": ".//div[@data-test-id='gpt-fulfillment-badges-container']//div[contains(@class, 'ff-text-wrapper') and contains(translate(., 'DELIVERY', 'delivery'), 'delivery')]",
    "fastest_delivery": ".//div[@data-test-id='gpt-fulfillment-badges-container']//div[contains(@class, 'ff-text-wrapper') and contains(translate(., 'SHIPPING', 'shipping'), 'shipping')]",
    "pick_up_availability": ".//div[@data-test-id='gpt-fulfillment-badges-container']//div[contains(@class, 'ff-text-wrapper') and contains(translate(., 'PICKUP', 'pickup'), 'pickup')]",
    "available_quantity_for_purchase": ".//div[@data-test-id='gpt-low-inventory-container']//span[contains(., 'Only') and contains(., 'left')]/text()",
    "inventory_status": ".//div[@data-test-id='gpt-low-inventory-container']//span[contains(., 'Low stock')]/text()",
    "offer": ".//span[contains(@class, 'dark-gray f7')][contains(., 'free offer')]/text()",
}


class WalmartHHPXPathTester(WalmartBaseCrawler):
    """Walmart HHP XPath 테스터 (DrissionPage 버전)"""

    def __init__(self):
        super().__init__()
        self.account_name = 'Walmart'
        self.walmart_zip_code = '11581'
        self.walmart_search_keyword = 'cellphone'
        self.page_type = 'xpath_tester'

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

    def test_url(self, url, mode, do_scroll=True, do_modal_click=False, do_similar_scroll=False, review_move_mode=None):
        """URL 테스트"""
        try:
            print(f"\n[INFO] 페이지 로딩 중: {url[:80]}...")
            self.page.get(url)
            time.sleep(random.uniform(3, 5))

            # CAPTCHA 체크
            self.handle_captcha()

            # 스크롤
            if mode != 'detail' and do_scroll:
                print("[INFO] 하단 스크롤 중...")
                self.scroll_to_bottom()

            # 모달 클릭 모드: 클릭 전 페이지에서 버튼 XPath 먼저 테스트
            if mode == 'detail' and do_modal_click:
                pre_tree = html.fromstring(self.page.html)
                self.test_detail_page(pre_tree, MODAL_BUTTON_XPATH_LIST, label='모달 버튼 XPath')

                # 모달 버튼 클릭 (spec_toggle_button → more_details_button)
                self.click_spec_modal()
                if do_similar_scroll or review_move_mode or do_scroll:
                    close_xpath = MODAL_XPATH_LIST.get('modal_close_button')
                    try:
                        close_button = self.page.ele(f'xpath:{close_xpath}', timeout=2) if close_xpath else None
                        if close_button:
                            close_button.click()
                            time.sleep(0.5)
                            print("  [modal] closed before section scroll")
                    except Exception as e:
                        print(f"  [modal] close skipped: {e}")

            # HTML 파싱 (모달 클릭 후 상태)
            if mode == 'detail' and do_similar_scroll:
                similar_section_xpath = DETAIL_XPATH_LIST.get('similar_products_section')
                if similar_section_xpath:
                    moved = self.scroll_find_element(
                        [('similar_products_section', similar_section_xpath)],
                        max_scrolls=10,
                        label='유사제품 섹션',
                    )
                    print(f"  [유사제품] similar_products_section 이동 {'성공' if moved else '실패'}")
                else:
                    print("  [유사제품] similar_products_section XPath 없음")

            if mode == 'detail' and review_move_mode == 'section':
                review_section_xpath = DETAIL_XPATH_LIST.get('review_section')
                if review_section_xpath:
                    moved = self.scroll_find_element(
                        [('review_section', review_section_xpath)],
                        max_scrolls=10,
                        label='리뷰 섹션',
                    )
                    print(f"  [리뷰] review_section 이동 {'성공' if moved else '실패'}")
                else:
                    print("  [리뷰] review_section XPath 없음")

            if mode == 'detail' and do_scroll:
                print("[INFO] ?섎떒 ?ㅽ겕濡?以?..")
                self.scroll_to_bottom()

            page_html = self.page.html
            tree = html.fromstring(page_html)

            # 모드별 테스트
            if mode == 'detail':
                if do_modal_click:
                    self.test_detail_page(tree, MODAL_XPATH_LIST, label='모달 안 XPath')
                else:
                    self.test_detail_page(tree, DETAIL_XPATH_LIST, label='상세페이지 XPath')
            else:
                self.test_list_page(tree)

        except Exception as e:
            print(f"[ERROR] 테스트 실패: {e}")
            traceback.print_exc()

    def click_spec_modal(self):
        """HHP 스펙 모달 열기 — spec_toggle_button → more_details_button"""
        spec_toggle_xpath = MODAL_BUTTON_XPATH_LIST['spec_toggle_button']
        more_details_xpath = MODAL_BUTTON_XPATH_LIST['more_details_button']

        # 1. Specifications 토글 버튼
        print("[INFO] spec_toggle_button 클릭 시도...")
        try:
            toggle_btn = self.page.ele(f'xpath:{spec_toggle_xpath}', timeout=5)
            if toggle_btn:
                toggle_btn.click()
                time.sleep(random.uniform(1, 2))
                print("  → 클릭 완료")
            else:
                print("  → 버튼 못 찾음 (이미 열려있거나 XPath 불일치)")
        except Exception as e:
            print(f"  → 클릭 실패: {e}")

        # 2. More details 버튼 (모달 오픈)
        print("[INFO] more_details_button 클릭 시도...")
        try:
            more_btn = self.page.ele(f'xpath:{more_details_xpath}', timeout=5)
            if more_btn:
                more_btn.click()
                time.sleep(random.uniform(1.5, 2.5))
                print("  → 클릭 완료 (모달 오픈)")
            else:
                print("  → 버튼 못 찾음")
        except Exception as e:
            print(f"  → 클릭 실패: {e}")

    def test_thumbnail_click(self, url):
        """썸네일 클릭 테스트 — 동영상 → 이미지 전환 확인"""
        try:
            print(f"\n[INFO] 페이지 로딩 중: {url[:80]}...")
            self.page.get(url)
            time.sleep(random.uniform(3, 5))

            THUMBNAIL_XPATH = '//button[@data-testid="item-page-vertical-carousel-hero-image-button"]'

            # 1. 전체 썸네일 목록
            all_thumbnails = self.page.eles(f'xpath:{THUMBNAIL_XPATH}')
            print(f"\n[결과] 썸네일 총 {len(all_thumbnails)}개 발견")

            for i, thumb in enumerate(all_thumbnails, 1):
                try:
                    img = thumb.ele('xpath:.//img')
                    alt = img.attr('alt') if img else '(img 없음)'
                    is_video = 'video' in alt.lower() if alt else False
                    print(f"  {i}. {'[동영상]' if is_video else '[이미지]'} {alt[:80]}")
                except Exception:
                    print(f"  {i}. (alt 추출 실패)")

            # 2. 첫 번째 썸네일 확인
            print(f"\n[테스트 1] 첫 번째 썸네일 확인")
            first = self.page.ele(f'xpath:{THUMBNAIL_XPATH}', timeout=3)
            if first:
                first_img = first.ele('xpath:.//img')
                first_alt = first_img.attr('alt') if first_img else ''
                is_first_video = 'video' in first_alt.lower()
                print(f"  → alt: {first_alt[:80]}")
                print(f"  → 동영상 여부: {is_first_video}")
            else:
                print(f"  → 썸네일 찾기 실패")
                return

            # 3. 이미지 썸네일 찾기
            IMG_XPATH = '//button[@data-testid="item-page-vertical-carousel-hero-image-button" and .//img[contains(@alt, "thumbnail image") and not(contains(@alt, "video"))]]'
            print(f"\n[테스트 2] 이미지 썸네일 찾기")
            img_thumbnail = self.page.ele(f'xpath:{IMG_XPATH}', timeout=3)
            if img_thumbnail:
                img_alt = img_thumbnail.ele('xpath:.//img').attr('alt') or ''
                print(f"  → 찾음: {img_alt[:80]}")
            else:
                print(f"  → 이미지 썸네일 찾기 실패")
                return

            # 4. 클릭 테스트 (3가지 방법)
            if is_first_video:
                print(f"\n[테스트 3-A] 일반 click()")
                try:
                    img_thumbnail.click()
                    time.sleep(1)
                    print(f"  → 완료 — 메인 이미지 변경 확인하세요")
                except Exception as e:
                    print(f"  → 실패: {e}")

                input("\n  Enter 누르면 다음 테스트...")

                print(f"\n[테스트 3-B] JS click (click(by_js=True))")
                try:
                    img_thumbnail.click(by_js=True)
                    time.sleep(1)
                    print(f"  → 완료 — 메인 이미지 변경 확인하세요")
                except Exception as e:
                    print(f"  → 실패: {e}")

                input("\n  Enter 누르면 다음 테스트...")

                print(f"\n[테스트 3-C] actions 마우스 이동 + 클릭")
                try:
                    self.page.actions.move_to(img_thumbnail).click()
                    time.sleep(1)
                    print(f"  → 완료 — 메인 이미지 변경 확인하세요")
                except Exception as e:
                    print(f"  → 실패: {e}")
            else:
                print(f"\n[테스트 3] 첫 썸네일이 이미지라 클릭 불필요")

        except Exception as e:
            print(f"[ERROR] 썸네일 테스트 실패: {e}")
            traceback.print_exc()

    def run(self):
        """Run the interactive XPath tester."""
        try:
            print("\n" + "=" * 70)
            print("Walmart HHP XPath Tester (DrissionPage)")
            print("=" * 70)
            print("\n[XPath 목록]")
            print(f"  - DETAIL_XPATH_LIST: {len(DETAIL_XPATH_LIST)}")
            print(f"  - LIST_FIELD_XPATHS: {len(LIST_FIELD_XPATHS)}")

            self.setup_browser()
            self.initialize_session()

            while True:
                print("\n" + "-" * 70)
                url = input("URL 입력 (종료: q): ").strip()

                if url.lower() == 'q':
                    print("[INFO] 종료합니다")
                    break

                if not url:
                    print("[WARNING] URL을 입력하세요")
                    continue

                if not url.startswith('http'):
                    url = 'https://' + url

                print("\n페이지 모드 선택:")
                print("  1. 상세페이지 (Detail)")
                print("  2. 리스트페이지 (List)")
                print("  3. 썸네일 클릭 테스트")
                mode_choice = input("선택 (1/2/3): ").strip()

                if mode_choice == '1':
                    mode = 'detail'
                elif mode_choice == '2':
                    mode = 'list'
                elif mode_choice == '3':
                    self.test_thumbnail_click(url)
                    continue
                else:
                    print("[WARNING] 잘못된 선택입니다")
                    continue

                do_modal_click = False
                do_similar_scroll = False
                review_move_mode = None
                do_scroll = True

                if mode == 'detail':
                    modal_choice = input("모달 버튼 클릭? (y/n) [기본: n]: ").strip().lower()
                    do_modal_click = modal_choice == 'y'

                    similar_choice = input("유사제품 섹션 스크롤? (y/n) [기본: n]: ").strip().lower()
                    do_similar_scroll = similar_choice == 'y'

                    review_choice = input("리뷰 섹션 스크롤? (y/n) [기본: n]: ").strip().lower()
                    if review_choice == 'y':
                        review_move_mode = 'section'

                    scroll_choice = input("하단까지 스크롤? (y/n) [기본: y]: ").strip().lower()
                    do_scroll = scroll_choice != 'n'
                else:
                    scroll_choice = input("하단까지 스크롤? (y/n) [기본: y]: ").strip().lower()
                    do_scroll = scroll_choice != 'n'

                self.test_url(
                    url,
                    mode,
                    do_scroll=do_scroll,
                    do_modal_click=do_modal_click,
                    do_similar_scroll=do_similar_scroll,
                    review_move_mode=review_move_mode,
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
    tester = WalmartHHPXPathTester()
    tester.run()
