"""Amazon login helpers used before Amazon TV PDP collection.

The browser finishes login and then stays in the same session. No function
in this module builds or opens an Amazon review-page URL.
"""

import time

HOME_URL = "https://www.amazon.com"
SIGNOUT_URL = (
    "https://www.amazon.com/gp/flex/sign-out.html"
    "?path=%2F&signIn=1&useRedirectOnSuccess=1&action=sign-out"
)
AUTH_COOKIES = {"at-main", "sess-at-main"}


def load_amazon_login_credentials(config_module=None):
    """Read amazon.config.AMAZON_LOGIN_2 without printing credentials."""
    if config_module is None:
        try:
            from amazon import config as config_module
        except ImportError as exc:
            raise RuntimeError(
                "amazon/config.py could not be imported."
            ) from exc
    source = "AMAZON_LOGIN_2"
    value = getattr(config_module, source, None)
    if not isinstance(value, dict):
        raise RuntimeError(
            "amazon/config.py에 AMAZON_LOGIN_2 {'email', 'password'} 설정이 없습니다."
        )
    email = str(value.get("email") or "").strip()
    password = str(value.get("password") or "")
    if not email or not password:
        raise RuntimeError(
            f"amazon/config.py의 {source} email/password가 비어 있습니다."
        )
    return {"email": email, "password": password, "source": source}


class DrissionLoginAdapter:
    def __init__(self, page):
        self.browser = page

    @property
    def url(self):
        return self.browser.url or ""

    @property
    def html(self):
        return self.browser.html or ""

    def get(self, url):
        self.browser.get(url)

    def find(self, selector, timeout=3):
        return self.browser.ele(f"css:{selector}", timeout=timeout)

    def find_all(self, selector):
        try:
            return self.browser.eles(f"css:{selector}") or []
        except Exception:
            return []

    def find_xpath(self, xpath, timeout=3):
        return self.browser.ele(f"xpath:{xpath}", timeout=timeout)

    @staticmethod
    def text(element):
        return (element.text or "").strip()

    @staticmethod
    def click(element):
        element.click()

    @staticmethod
    def input(element, value):
        element.clear()
        element.input(value)

    def cookie_names(self):
        try:
            cookies = self.browser.cookies(all_info=True)
        except Exception:
            return set()
        if isinstance(cookies, dict):
            return set(cookies)
        return {
            row.get("name") for row in cookies or []
            if isinstance(row, dict) and row.get("name")
        }


class SeleniumLoginAdapter:
    def __init__(self, driver):
        self.browser = driver

    @property
    def url(self):
        return self.browser.current_url or ""

    @property
    def html(self):
        return self.browser.page_source or ""

    def get(self, url):
        self.browser.get(url)

    def find(self, selector, timeout=3):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        try:
            return WebDriverWait(self.browser, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
        except Exception:
            return None

    def find_all(self, selector):
        from selenium.webdriver.common.by import By
        try:
            return self.browser.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            return []

    def find_xpath(self, xpath, timeout=3):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        try:
            return WebDriverWait(self.browser, timeout).until(
                EC.presence_of_element_located((By.XPATH, xpath))
            )
        except Exception:
            return None

    @staticmethod
    def text(element):
        return (element.text or "").strip()

    @staticmethod
    def click(element):
        element.click()

    @staticmethod
    def input(element, value):
        element.clear()
        element.send_keys(value)

    def cookie_names(self):
        try:
            return {
                row.get("name") for row in self.browser.get_cookies()
                if row.get("name")
            }
        except Exception:
            return set()


def _first(adapter, selectors, timeout=3):
    for selector in selectors:
        element = adapter.find(selector, timeout=timeout)
        if element:
            return element
    return None


def _logged_in(adapter):
    """Loose check used only to decide whether an old session needs logout."""
    if _nav_login_state(adapter) is False:
        return False
    return bool(adapter.cookie_names() & AUTH_COOKIES)


def _nav_login_state(adapter):
    """Return True/False from visible nav, or None when nav is unavailable."""
    account = adapter.find("#nav-link-accountList", timeout=2)
    if not account:
        return None
    text = adapter.text(account).casefold()
    if not text:
        return None
    if "sign in" in text:
        return False
    if "hello" in text:
        return True
    return None


def _verified_login(adapter):
    """Strict final check: auth cookies present and no active auth challenge."""
    if not AUTH_COOKIES.issubset(adapter.cookie_names()):
        return False
    url = adapter.url.casefold()
    if "/ap/signin" in url or _challenge(adapter):
        return False
    return _nav_login_state(adapter) is not False


def _challenge(adapter):
    value = f"{adapter.url}\n{adapter.html}".casefold()
    return any(marker in value for marker in (
        "/ap/cvf", "/ap/mfa", "/ap/captcha", "auth-mfa-otpcode",
        "cvf-input-code", "enter the characters you see",
    ))


def _broken_signin_page(adapter):
    value = (adapter.url + " " + adapter.html).casefold()
    return (
        "looking for something?" in value
        and "not a functioning page" in value
    )


def _signin_surface_present(adapter):
    if _broken_signin_page(adapter):
        return False
    if _challenge(adapter):
        return True
    if _first(adapter, (
        "#ap_email",
        "#ap_password",
        "input[name='email']",
        "input[name='password']",
        "#ap-other-account",
        "[data-testid*='use-different-account']",
    ), timeout=0.5):
        return True
    return any(adapter.find_all(selector) for selector in (
        "div[data-a-input-name='accountSelectionSelect'] span.a-button-text",
        "div[data-testid*='account-list-item']",
        "div.cvf-account-switcher-account",
    ))


def _wait_signin_surface(adapter, timeout_seconds=10):
    deadline = time.time() + max(float(timeout_seconds), 1)
    while time.time() < deadline:
        if _signin_surface_present(adapter):
            return True
        if _broken_signin_page(adapter):
            return False
        time.sleep(0.5)
    return False


def _handle_continue_shopping(adapter):
    button = adapter.find_xpath(
        "//button[contains(normalize-space(.), 'Continue shopping')]",
        timeout=2,
    )
    if not button:
        return
    try:
        adapter.click(button)
        time.sleep(2)
        print("[LOGIN] Continue shopping page cleared.")
    except Exception:
        pass


def _open_signin_from_home(adapter):
    """Open sign-in by clicking Amazon home's own navigation element."""
    adapter.get(HOME_URL)
    time.sleep(3)
    _handle_continue_shopping(adapter)
    if _logged_in(adapter):
        print("[LOGIN] Previous account session is still active after sign-out.")
        return False

    account_link = _first(adapter, (
        "#nav-link-accountList",
        "a[data-nav-role='signin']",
    ), timeout=5)
    if not account_link:
        account_link = adapter.find_xpath(
            "//a[contains(@href, 'ap/signin')]",
            timeout=5,
        )
    if not account_link:
        print("[LOGIN] Amazon home Sign in element was not found.")
        return False

    try:
        adapter.click(account_link)
    except Exception as exc:
        print(f"[LOGIN] Amazon home Sign in click failed: {exc}")
        return False

    if not _wait_signin_surface(adapter):
        if _broken_signin_page(adapter):
            print("[LOGIN] Amazon returned a non-functioning sign-in page.")
        else:
            print("[LOGIN] Amazon sign-in form did not appear.")
        return False

    print("[LOGIN] Sign-in form reached through Amazon home navigation.")
    return True


def _wait_login(adapter, timeout_seconds):
    reported = False
    deadline = time.time() + max(int(timeout_seconds), 1)
    while time.time() < deadline:
        if _verified_login(adapter):
            return True
        if _challenge(adapter) and not reported:
            print("[LOGIN] OTP/CAPTCHA 확인 필요 — RDP 브라우저에서 수동 완료를 기다립니다.")
            reported = True
        time.sleep(2)
    return False


def _wait_challenge_clear(adapter, timeout_seconds):
    print("[LOGIN] OTP/CAPTCHA 확인 필요 — RDP 브라우저에서 수동 완료를 기다립니다.")
    deadline = time.time() + max(int(timeout_seconds), 1)
    while time.time() < deadline:
        if not _challenge(adapter):
            return True
        time.sleep(2)
    return False


def _find_login_2_account(adapter, email):
    """Find only the saved-account row that explicitly contains LOGIN_2 email."""
    for selector in (
        "div[data-a-input-name='accountSelectionSelect'] span.a-button-text",
        "div[data-testid*='account-list-item']",
        "div.cvf-account-switcher-account",
    ):
        for element in adapter.find_all(selector):
            if email.casefold() in adapter.text(element).casefold():
                return element
    return None


def _find_use_different_account(adapter):
    element = _first(adapter, (
        "#ap-other-account",
        "[data-testid*='use-different-account']",
        "[data-testid*='add-account']",
    ), timeout=1)
    if element:
        return element
    lower_text = (
        "translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    )
    return adapter.find_xpath(
        "(//*[self::a or self::button or self::span or @role='button']"
        f"[contains({lower_text}, 'use a different account') or "
        f"contains({lower_text}, 'add account')])[1]",
        timeout=2,
    )


def _finish_login(adapter):
    """Finish on Amazon home and require an explicit logged-in nav state."""
    adapter.get(HOME_URL)
    time.sleep(2)
    if not _verified_login(adapter) or _nav_login_state(adapter) is not True:
        print("[LOGIN] 홈 화면에서 로그인 상태를 재확인하지 못했습니다.")
        return False
    print("[LOGIN] Amazon 로그인 확인 완료")
    return True


def _login(adapter, credentials, timeout_seconds=180):
    print(f"[LOGIN] Amazon 계정 로그인 시작 ({credentials['source']})")
    adapter.get(HOME_URL)
    time.sleep(3)
    _handle_continue_shopping(adapter)
    if _verified_login(adapter) and _nav_login_state(adapter) is True:
        print("[LOGIN] Reusing existing authenticated Amazon session")
        return True

    if _logged_in(adapter):
        # The nav does not expose an email address, so an existing session
        # cannot be proven to be LOGIN_2. Re-authenticate instead of accepting
        # or switching to an arbitrary saved account.
        print("[LOGIN] 기존 세션 로그아웃 후 AMAZON_LOGIN_2로 재인증합니다.")
        adapter.get(SIGNOUT_URL)
        time.sleep(2)

    if not _open_signin_from_home(adapter):
        return False
    if _verified_login(adapter):
        print("[LOGIN] 기존 계정 세션을 종료하지 못해 LOGIN_2 인증을 중단합니다.")
        return False

    if _challenge(adapter):
        if not _wait_challenge_clear(adapter, timeout_seconds):
            print(f"[LOGIN] OTP/CAPTCHA 대기 시간 초과 ({timeout_seconds}초)")
            return False

    email_input = _first(adapter, (
        "#ap_email", "input[name='email']", "input[type='email']",
    ))
    account_bound_to_login_2 = False
    if not email_input:
        # Select only an account row that explicitly contains LOGIN_2 email.
        # If it is not present, choose the separate "different account" path
        # and type LOGIN_2 email ourselves. Never click the first saved account.
        account_choice = _find_login_2_account(adapter, credentials["email"])
        if account_choice:
            adapter.click(account_choice)
            account_bound_to_login_2 = True
            time.sleep(2)
        else:
            different_account = _find_use_different_account(adapter)
            if different_account:
                adapter.click(different_account)
                time.sleep(2)
        email_input = _first(adapter, (
            "#ap_email", "input[name='email']", "input[type='email']",
        ))

    if email_input:
        adapter.input(email_input, credentials["email"])
        account_bound_to_login_2 = True
        continue_button = _first(adapter, ("#continue", "input[type='submit']"))
        if not continue_button:
            print("[LOGIN] 이메일 Continue 버튼을 찾지 못했습니다.")
            return False
        adapter.click(continue_button)
        time.sleep(3)

    if not account_bound_to_login_2:
        print("[LOGIN] 계정 선택 화면에서 AMAZON_LOGIN_2를 식별하지 못했습니다.")
        return False

    password_input = _first(adapter, (
        "#ap_password", "input[name='password']", "input[type='password']",
    ), timeout=5)
    if not password_input:
        if _challenge(adapter):
            if not _wait_challenge_clear(adapter, timeout_seconds):
                print(f"[LOGIN] OTP/CAPTCHA 대기 시간 초과 ({timeout_seconds}초)")
                return False
            password_input = _first(adapter, (
                "#ap_password", "input[name='password']",
                "input[type='password']",
            ), timeout=5)
        if not password_input and _verified_login(adapter):
            return _finish_login(adapter)
    if not password_input:
        print("[LOGIN] 비밀번호 입력 필드를 찾지 못했습니다.")
        return False

    adapter.input(password_input, credentials["password"])
    submit = _first(adapter, ("#signInSubmit", "input[type='submit']"))
    if not submit:
        print("[LOGIN] Sign-In 버튼을 찾지 못했습니다.")
        return False
    adapter.click(submit)

    if not _wait_login(adapter, timeout_seconds):
        print(f"[LOGIN] Amazon 로그인 검증 실패 ({timeout_seconds}초 제한)")
        return False

    # Finish on home. The caller next opens only DB product detail URLs.
    return _finish_login(adapter)


def ensure_amazon_login_dp(page, timeout_seconds=180, config_module=None):
    credentials = load_amazon_login_credentials(config_module)
    return _login(DrissionLoginAdapter(page), credentials, timeout_seconds)


def ensure_amazon_logout_dp(page):
    """Force a logged-out baseline in the current DrissionPage session."""
    adapter = DrissionLoginAdapter(page)
    adapter.get(HOME_URL)
    time.sleep(2)
    if _logged_in(adapter) or adapter.cookie_names() & AUTH_COOKIES:
        adapter.get(SIGNOUT_URL)
        time.sleep(2)
    adapter.get(HOME_URL)
    time.sleep(2)
    return (
        not (adapter.cookie_names() & AUTH_COOKIES)
        and _nav_login_state(adapter) is not True
    )


def is_amazon_login_verified_dp(page):
    """Check the current DrissionPage session without triggering a login."""
    return _verified_login(DrissionLoginAdapter(page))


def ensure_amazon_login_selenium(driver, timeout_seconds=180, config_module=None):
    credentials = load_amazon_login_credentials(config_module)
    return _login(SeleniumLoginAdapter(driver), credentials, timeout_seconds)
