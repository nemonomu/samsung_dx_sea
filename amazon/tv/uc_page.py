"""
UCPage — undetected-chromedriver(selenium) 위에 DrissionPage ChromiumPage와
동일한 API 표면을 제공하는 얇은 어댑터 (Amazon TV UC 전환 전용).

목적:
  기존 크롤러/추출 로직(amazon_base·base_crawler의 검증된 메서드 18개)이
  self.page.get / .html / .ele / .run_js ... 를 그대로 호출하는데,
  self.page 를 이 UCPage 로 바꾸면 코드 재작성 없이 UC 위에서 동작한다.
  게이트 소진이 없는 UC(2026-07-08 실측 145/145 게이트0)로 파이프라인을 옮기되,
  검증된 크롤/복구/추출 로직은 손대지 않는 것이 설계 의도.

격리:
  이 파일은 amazon/tv/ 전용 신규 파일 — common/ 공유 base 는 건드리지 않는다.
  Walmart/HHP/기존 DrissionPage TV 경로에 영향 없음.

DrissionPage → selenium 대응 (Explore 전수조사 기반, 실제 사용 표면만):
  page.get(url)              → driver.get(url)
  page.html                  → driver.page_source
  page.url                   → driver.current_url
  page.title                 → driver.title
  page.refresh()             → driver.refresh()
  page.run_js(js, *a)        → driver.execute_script(js, *a)
  page.run_cdp(cmd, **kw)    → driver.execute_cdp_cmd(cmd, kw)
  page.ele(sel, timeout=n)   → WebDriverWait find (없으면 None) → UCElement
  page.quit()                → driver.quit()
  page.scroll.down(px)       → execute_script('window.scrollBy(0, px)')
  page.set.window.max()      → driver.maximize_window()
  page.get_screenshot(path,name) → driver.save_screenshot(fullpath)
  page.actions.move_to((x,y))→ ActionChains move
  page.cookies()             → driver.get_cookies()
  element.click(by_js=False) → el.click() / execute_script click
  element.text               → el.text
  element.attr(name)         → el.get_attribute(name)
  element.input(value)       → el.send_keys(value)
  element.clear()            → el.clear()
  element.scroll.to_see()    → execute_script scrollIntoView(el)
"""

import os
import re
import time

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    WebDriverException, NoSuchElementException, TimeoutException, StaleElementReferenceException,
)

# UC.__del__ 가 GC 시점에 quit() 재시도 → Windows OSError [WinError 6] 방지 (SIEL 패턴).
# finally 에서 명시적으로 quit() 하므로 __del__ 은 불필요.
uc.Chrome.__del__ = lambda self: None


def detect_chrome_major():
    """설치된 Chrome 메이저 버전 — 레지스트리(BLBeacon)와 디스크상 최신 폴더 중 최대값.

    BLBeacon은 자동 업데이트 후 갱신이 지연될 수 있어, UC version_main이 실제
    실행 바이너리와 어긋나면 즉시 차단된다(과거 MMKT UC 사례). 둘 중 최대 사용.
    """
    import subprocess
    versions = []
    try:
        r = subprocess.run(
            ['reg', 'query', r'HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon', '/v', 'version'],
            capture_output=True, text=True)
        m = re.search(r'(\d+)\.\d+\.\d+\.\d+', r.stdout or '')
        if m:
            versions.append(int(m.group(1)))
    except Exception:
        pass
    for base in (os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                 os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')):
        appdir = os.path.join(base, 'Google', 'Chrome', 'Application')
        try:
            for name in os.listdir(appdir):
                m = re.match(r'(\d+)\.\d+\.\d+\.\d+$', name)
                if m:
                    versions.append(int(m.group(1)))
        except Exception:
            pass
    return max(versions) if versions else None


def make_uc_driver(user_data_dir=None, headless=False, lang='en-US'):
    """UC 드라이버 생성 — SIEL make_driver 설정 + 신뢰 프로필 옵션.

    스로틀링 방지 플래그 + focus emulation 은 리뷰 lazy-load 안정화용(백그라운드
    창에서도 위젯 로딩 유지). version_main 은 실제 Chrome 메이저에 맞춘다.
    """
    opts = uc.ChromeOptions()
    if headless:
        opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument('--start-maximized')
    opts.add_argument(f'--lang={lang}')
    opts.add_argument('--disable-background-timer-throttling')
    opts.add_argument('--disable-backgrounding-occluded-windows')
    opts.add_argument('--disable-renderer-backgrounding')
    opts.add_argument('--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling')
    kwargs = {'options': opts}
    if user_data_dir:
        kwargs['user_data_dir'] = user_data_dir
    major = detect_chrome_major()
    if major:
        kwargs['version_main'] = major
        print(f"[INFO] UC Chrome major={major}")
    driver = uc.Chrome(**kwargs)
    try:
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)
    except WebDriverException:
        pass
    try:
        driver.execute_cdp_cmd('Emulation.setFocusEmulationEnabled', {'enabled': True})
    except WebDriverException:
        pass
    return driver


class UCElement:
    """selenium WebElement 를 DrissionPage 요소 API로 감싼다."""

    __slots__ = ('_el', '_driver')

    def __init__(self, el, driver):
        self._el = el
        self._driver = driver

    def click(self, by_js=False):
        """일반 클릭, 실패 시(또는 by_js=True) JS 클릭으로 폴백."""
        if by_js:
            self._driver.execute_script('arguments[0].click();', self._el)
            return
        try:
            self._el.click()
        except WebDriverException:
            self._driver.execute_script('arguments[0].click();', self._el)

    @property
    def text(self):
        try:
            return self._el.text or self._el.get_attribute('textContent') or ''
        except StaleElementReferenceException:
            return ''

    def attr(self, name):
        try:
            return self._el.get_attribute(name)
        except StaleElementReferenceException:
            return None

    def input(self, value):
        self._el.send_keys(value)

    def clear(self):
        self._el.clear()

    @property
    def scroll(self):
        return _ElementScroll(self._el, self._driver)


class _ElementScroll:
    __slots__ = ('_el', '_driver')

    def __init__(self, el, driver):
        self._el = el
        self._driver = driver

    def to_see(self):
        try:
            self._driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", self._el)
            time.sleep(0.2)
        except WebDriverException:
            pass


class _PageScroll:
    __slots__ = ('_driver',)

    def __init__(self, driver):
        self._driver = driver

    def down(self, pixels):
        try:
            self._driver.execute_script(f'window.scrollBy(0, {int(pixels)});')
        except WebDriverException:
            pass


class _WindowSetter:
    __slots__ = ('_driver',)

    def __init__(self, driver):
        self._driver = driver

    def max(self):
        try:
            self._driver.maximize_window()
        except WebDriverException:
            pass


class _SetNamespace:
    __slots__ = ('_driver',)

    def __init__(self, driver):
        self._driver = driver

    @property
    def window(self):
        return _WindowSetter(self._driver)


class _ActionsNamespace:
    __slots__ = ('_driver',)

    def __init__(self, driver):
        self._driver = driver

    def move_to(self, point):
        """랜덤 마우스 이동(봇 회피). point=(x, y) 뷰포트 절대좌표 근사."""
        try:
            x, y = point
            ActionChains(self._driver).move_by_offset(int(x), int(y)).perform()
            # 오프셋 복원 (다음 호출 누적 방지)
            ActionChains(self._driver).move_by_offset(-int(x), -int(y)).perform()
        except WebDriverException:
            pass


def _parse_selector(selector):
    """DrissionPage 셀렉터 접두사('xpath:', 'css:')를 selenium (By, value)로 변환."""
    if selector.startswith('xpath:'):
        return By.XPATH, selector[len('xpath:'):]
    if selector.startswith('css:'):
        return By.CSS_SELECTOR, selector[len('css:'):]
    if selector.startswith('x:'):
        return By.XPATH, selector[len('x:'):]
    if selector.startswith('c:'):
        return By.CSS_SELECTOR, selector[len('c:'):]
    # 접두사 없으면 xpath 기본 (기존 코드는 항상 접두사 사용 — 방어적 기본값)
    return By.XPATH, selector


class UCPage:
    """undetected-chromedriver driver 를 DrissionPage ChromiumPage API로 감싼다."""

    def __init__(self, driver):
        self.driver = driver

    # --- 네비게이션 / 상태 ---
    def get(self, url):
        self.driver.get(url)

    @property
    def html(self):
        try:
            return self.driver.page_source
        except WebDriverException:
            return ''

    @property
    def url(self):
        try:
            return self.driver.current_url
        except WebDriverException:
            return ''

    @property
    def title(self):
        try:
            return self.driver.title
        except WebDriverException:
            return ''

    def refresh(self):
        try:
            self.driver.refresh()
        except WebDriverException:
            pass

    # --- 스크립트 / CDP ---
    def run_js(self, script, *args):
        return self.driver.execute_script(script, *args)

    def run_cdp(self, cmd, **kwargs):
        return self.driver.execute_cdp_cmd(cmd, kwargs)

    # --- 요소 탐색 ---
    def ele(self, selector, timeout=None):
        """DrissionPage .ele 대응: 찾으면 UCElement, 못 찾으면 None (예외 없음)."""
        by, value = _parse_selector(selector)
        try:
            if timeout and timeout > 0:
                el = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((by, value)))
            else:
                el = self.driver.find_element(by, value)
            return UCElement(el, self.driver)
        except (TimeoutException, NoSuchElementException, WebDriverException):
            return None

    # --- 스크린샷 ---
    def get_screenshot(self, path=None, name=None):
        """DrissionPage get_screenshot 대응.

        - name 있으면 path=폴더 → join(path, name)
        - name 없으면 path=전체 파일경로 (base_crawler:449 방식)
        selenium save_screenshot 은 뷰포트 PNG (진단용 캡처라 파리티 무관).
        """
        if name:
            os.makedirs(path, exist_ok=True)
            full = os.path.join(path, name)
        else:
            full = path
            d = os.path.dirname(full)
            if d:
                os.makedirs(d, exist_ok=True)
        try:
            self.driver.save_screenshot(full)
        except WebDriverException:
            pass
        return full

    # --- 쿠키 ---
    def cookies(self):
        try:
            return self.driver.get_cookies()
        except WebDriverException:
            return []

    # --- 종료 ---
    def quit(self):
        try:
            self.driver.quit()
        except Exception:
            pass

    # --- 네임스페이스 (DrissionPage 체이닝 호환) ---
    @property
    def scroll(self):
        return _PageScroll(self.driver)

    @property
    def set(self):
        return _SetNamespace(self.driver)

    @property
    def actions(self):
        return _ActionsNamespace(self.driver)
