"""
Bing News XPath 테스터 (undetected-chromedriver 버전)

================================================================================
사용법
================================================================================
1. test_xpaths에 테스트할 XPath 추가
2. 실행: python market_xpath_tester.py
3. URL 또는 키워드 입력 후 결과 확인
4. 엔터키로 종료

================================================================================
"""

import time
import random

from lxml import html
import undetected_chromedriver as uc
from selenium.webdriver.support.ui import WebDriverWait

# ============================================================================
# 설정
# ============================================================================

# 테스트할 XPath 목록 (필드명: [xpath 후보들])
TEST_XPATHS = {
    'article_time': [
        ".//div[contains(@class, 'source')]//span[@tabindex='0']",
    ],
    'article_title': [
        ".//a[@class='title']//h2",
    ],
}

# 컨테이너 XPath (뉴스 카드 리스트용)
CONTAINER_XPATH = "//div[contains(@class, 'news-card') and contains(@class, 'newsitem')]"


# ============================================================================
# 테스터 클래스
# ============================================================================

class BingNewsXPathTester:
    """undetected-chromedriver 기반 XPath 테스터"""

    def __init__(self):
        self.driver = None
        self.wait = None

    def setup_driver(self):
        """undetected-chromedriver 설정"""
        print("[INFO] undetected-chromedriver 설정 중...")

        options = uc.ChromeOptions()

        # 페이지 로드 전략
        options.page_load_strategy = 'none'

        # 기본 옵션
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--lang=en-US,en;q=0.9')
        options.add_argument('--start-maximized')

        # 알림 비활성화
        prefs = {
            "profile.default_content_setting_values.notifications": 2,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        }
        options.add_experimental_option("prefs", prefs)

        # undetected_chromedriver 사용
        self.driver = uc.Chrome(options=options)
        self.driver.set_page_load_timeout(120)
        self.wait = WebDriverWait(self.driver, 20)

        print("[INFO] undetected-chromedriver 설정 완료")

    def scroll_to_bottom(self):
        """스크롤: 300px씩 점진적 스크롤 → 페이지 하단까지 진행"""
        try:
            scroll_step = 300
            current_position = 0
            while True:
                current_position += scroll_step
                self.driver.execute_script(f"window.scrollTo(0, {current_position});")
                time.sleep(random.uniform(0.3, 0.7))
                total_height = self.driver.execute_script("return document.body.scrollHeight")
                if current_position >= total_height:
                    break
            time.sleep(random.uniform(1, 3))
        except Exception as e:
            print(f"[ERROR] Scroll failed: {e}")

    def check_and_handle_block(self):
        """차단/CAPTCHA 감지 및 처리 (실제로 감지된 경우에만 안내)"""
        try:
            page_content = self.driver.page_source.lower()

            # CAPTCHA 키워드 (press & hold 버튼)
            captcha_keywords = ['press & hold', 'press and hold']

            # 차단 키워드 (별도 처리)
            block_keywords = ['access denied', 'blocked', 'unusual activity']

            has_captcha = any(keyword in page_content for keyword in captcha_keywords)
            has_block = any(keyword in page_content for keyword in block_keywords)

            if has_captcha:
                print("[WARNING] CAPTCHA 감지! (Press & Hold)")
                print("[INFO] 브라우저에서 수동으로 해결 후 엔터를 누르세요...")
                input()
                time.sleep(2)
                return True
            elif has_block:
                print("[WARNING] 차단/에러 감지!")
                print("[INFO] 브라우저에서 수동으로 해결 후 엔터를 누르세요...")
                print("[TIP] 페이지가 정상이면 그냥 엔터를 누르세요.")
                input()
                time.sleep(2)
                return True

            return False  # 차단 없음

        except Exception as e:
            print(f"[WARNING] Block check error: {e}")
            return False

    def load_page(self, url):
        """페이지 로드"""
        print(f"[INFO] 페이지 로딩: {url}")
        self.driver.get(url)

        # 랜덤 대기 시간
        wait_time = random.uniform(3, 5)
        print(f"[INFO] 대기 중... ({wait_time:.1f}초)")
        time.sleep(wait_time)

        # 차단/CAPTCHA 감지 및 처리 (한 번만 체크)
        self.check_and_handle_block()

        # 페이지 하단까지 스크롤 (요소 로딩을 위해)
        print("[INFO] 하단까지 스크롤 중...")
        self.scroll_to_bottom()

        print("[INFO] 페이지 로딩 완료")
        return html.fromstring(self.driver.page_source)

    def extract_text(self, element):
        """요소에서 텍스트 추출"""
        return element.text_content().strip()

    def test_xpaths(self, tree, field_name, xpath_list):
        """XPath 목록 테스트 (상세 페이지용)"""
        print(f"\n{'='*80}")
        print(f"필드: {field_name}")
        print('='*80)

        for i, xpath in enumerate(xpath_list, 1):
            print(f"\n[{i}] XPath: {xpath}")
            try:
                elements = tree.xpath(xpath)
                if elements:
                    print(f"    ✓ 성공! 요소 {len(elements)}개 발견")
                    # 모든 요소의 전체 텍스트 출력
                    for j, elem in enumerate(elements):
                        # /text() XPath는 문자열을 반환하므로 타입 체크
                        if isinstance(elem, str):
                            text = elem.strip()
                        else:
                            text = self.extract_text(elem)
                        print(f"\n    --- 요소 {j+1} 전체 텍스트 ---")
                        print(f"    {text}")
                        print(f"    --- 끝 (길이: {len(text)}자) ---")
                else:
                    print(f"    ✗ 실패 - 요소 없음")
            except Exception as e:
                print(f"    ✗ 오류: {e}")

    def test_xpaths_with_container(self, tree, field_name, xpath_list, max_items=50):
        """XPath 목록 테스트 (리스트 페이지용 - 컨테이너 기반)"""
        print(f"\n{'='*80}")
        print(f"필드: {field_name} (컨테이너 기반)")
        print('='*80)

        # 컨테이너 찾기
        containers = tree.xpath(CONTAINER_XPATH)
        print(f"[INFO] 컨테이너 {len(containers)}개 발견")

        if not containers:
            print("    ✗ 컨테이너 없음")
            return

        for i, xpath in enumerate(xpath_list, 1):
            print(f"\n[{i}] XPath: {xpath}")
            success_count = 0

            for j, container in enumerate(containers[:max_items]):
                try:
                    elements = container.xpath(xpath)
                    if elements:
                        elem = elements[0]
                        if isinstance(elem, str):
                            text = elem.strip()
                        else:
                            text = self.extract_text(elem)
                        print(f"    컨테이너 {j+1}: {text}")
                        success_count += 1
                    else:
                        print(f"    컨테이너 {j+1}: (없음)")
                except Exception as e:
                    print(f"    컨테이너 {j+1}: 오류 - {e}")

            print(f"    → 성공률: {success_count}/{min(len(containers), max_items)}")

    def run(self):
        """테스터 실행"""
        try:
            self.setup_driver()

            while True:
                # URL 입력받기
                url = input("\nURL을 입력하세요 (엔터=종료): ").strip()
                if not url:
                    print("프로그램을 종료합니다.")
                    break

                tree = self.load_page(url)

                print("\n" + "="*60)
                print("XPath 테스트 결과")
                print("="*60)
                print(f"URL: {url}")

                # 컨테이너 기반으로 테스트 (리스트 페이지)
                for field_name, xpath_list in TEST_XPATHS.items():
                    self.test_xpaths_with_container(tree, field_name, xpath_list)

                print("\n" + "="*60)
                print("테스트 완료")
                print("="*60)

        except Exception as e:
            print(f"[ERROR] {e}")
            import traceback
            traceback.print_exc()
        finally:
            if self.driver:
                self.driver.quit()


if __name__ == "__main__":
    tester = BingNewsXPathTester()
    tester.run()
