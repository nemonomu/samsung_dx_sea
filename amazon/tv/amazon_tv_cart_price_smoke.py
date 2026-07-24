"""One-ASIN live smoke test for Amazon TV hidden cart prices.

Safety properties:
- no database connection or write;
- no review collection;
- no automatic Add-to-Cart retry;
- exact-ASIN cart lookup only;
- an existing cart item is read without changing its quantity;
- a newly added item is intentionally kept in the crawler account cart.

Run this only on the authorized RDP operations machine while no other Amazon
crawler/browser is using the trusted profile or debug port.
"""

import argparse
import os
import sys
import time


_project_root = os.path.abspath(os.path.dirname(__file__))
while _project_root and not os.path.exists(
    os.path.join(_project_root, "common", "setup.py")
):
    _parent = os.path.dirname(_project_root)
    if _parent == _project_root:
        break
    _project_root = _parent
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from common.setup import setup_environment

setup_environment(__file__)

from amazon.tv.amazon_tv_cart_price import (
    CartPriceParseError,
    active_cart_total_count,
    extract_active_cart_line,
    extract_ewc_cart_line,
    has_hidden_cart_price_message,
    normalize_asin,
    parse_html,
)


CART_URL = "https://www.amazon.com/gp/cart/view.html"
DEFAULT_ASIN = "B0DXMZQ3MN"


def _tree(page):
    return parse_html(page.html)


def _setup_logged_in_browser(crawler):
    # Keep crawler/config imports inside the explicitly authorized live path.
    # This lets --help and offline checks run without production credentials.
    from amazon.tv.amazon_login import ensure_amazon_login_dp
    from amazon.tv.amazon_tv_dt import TRUSTED_PROFILE_DIR, refresh_trusted_profile

    refresh_trusted_profile()
    cookies_path = os.path.join(
        TRUSTED_PROFILE_DIR, "Default", "Network", "Cookies"
    )
    if os.path.exists(cookies_path):
        crawler.browser_user_data_dir = TRUSTED_PROFILE_DIR

    crawler.skip_initial_zip = True
    try:
        if not crawler.setup_browser():
            raise RuntimeError("Amazon browser setup failed")
    finally:
        crawler.skip_initial_zip = False

    timeout = int(os.environ.get("AMAZON_LOGIN_TIMEOUT", "180"))
    if not ensure_amazon_login_dp(crawler.page, timeout_seconds=timeout):
        raise RuntimeError("Amazon login failed")
    if not crawler.set_amazon_zip_code(crawler.amazon_zip_code):
        raise RuntimeError("Amazon ZIP setup failed after login")


def _visible_add_to_cart_button(page):
    buttons = page.eles(
        'css:input#add-to-cart-button[name="submit.add-to-cart"]'
    ) or []
    visible = []
    for button in buttons:
        try:
            if button.is_displayed():
                visible.append(button)
        except Exception:
            continue
    if len(visible) != 1:
        raise RuntimeError(
            f"expected one visible Add-to-Cart button, found {len(visible)}"
        )
    return visible[0]


def _wait_for_ewc_line(page, asin, timeout_seconds=15):
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            line = extract_ewc_cart_line(_tree(page), asin)
            if line:
                return line
        except CartPriceParseError as exc:
            last_error = exc
        time.sleep(1)
    if last_error:
        print(f"[SMOKE] EWC parse warning: {last_error}")
    return None


def _load_active_cart(crawler, asin):
    from amazon.tv.amazon_login import is_amazon_login_verified_dp

    crawler.page.get(CART_URL)
    time.sleep(2)
    if not is_amazon_login_verified_dp(crawler.page):
        raise RuntimeError("Amazon login was not verified on the cart page")
    tree = _tree(crawler.page)
    return (
        extract_active_cart_line(tree, asin),
        active_cart_total_count(tree),
    )


def run_smoke(asin, product_url):
    from amazon.tv.amazon_login import is_amazon_login_verified_dp
    from amazon.tv.amazon_tv_dt import AmazonTVDetailCrawler

    crawler = AmazonTVDetailCrawler(
        batch_id="amazon_tv_cart_price_smoke",
        test_mode=True,
        require_amazon_login=False,
    )
    add_clicked = False
    try:
        _setup_logged_in_browser(crawler)
        asin = normalize_asin(asin)

        before_line, before_total = _load_active_cart(crawler, asin)
        print(f"[SMOKE] cart total before: {before_total}")
        if before_line:
            print(
                f"[SMOKE PASS] existing cart item: asin={before_line.asin}, "
                f"quantity={before_line.quantity}, price={before_line.price}, "
                "action=read-only"
            )
            return True

        crawler.page.get(product_url)
        time.sleep(2)
        if not is_amazon_login_verified_dp(crawler.page):
            raise RuntimeError("Amazon login was not verified on the PDP")

        loaded_asin = crawler.extract_item(crawler.page.url)
        if loaded_asin != asin:
            raise RuntimeError(
                f"PDP ASIN mismatch: expected={asin}, loaded={loaded_asin}"
            )
        if not has_hidden_cart_price_message(_tree(crawler.page)):
            raise RuntimeError("logged-in hidden cart-price message was not found")

        button = _visible_add_to_cart_button(crawler.page)
        print(f"[SMOKE] Add-to-Cart once: asin={asin}")
        if not crawler.click_element(button, label="Smoke Add-to-Cart"):
            raise RuntimeError("Add-to-Cart click failed")
        add_clicked = True

        ewc_line = _wait_for_ewc_line(crawler.page, asin)
        if ewc_line:
            print(
                f"[SMOKE] EWC price: asin={ewc_line.asin}, "
                f"quantity={ewc_line.quantity}, price={ewc_line.price}"
            )
        else:
            print("[SMOKE] EWC not available; verifying through the full cart")

        after_line, after_total = _load_active_cart(crawler, asin)
        if after_line is None:
            raise RuntimeError(
                "target ASIN was not found in the active cart after one Add"
            )
        if after_line.quantity != 1:
            raise RuntimeError(
                f"new target quantity must be 1, got {after_line.quantity}"
            )
        if before_total is not None and after_total != before_total + 1:
            raise RuntimeError(
                f"cart total did not increase by one: "
                f"before={before_total}, after={after_total}"
            )
        if ewc_line and ewc_line.price != after_line.price:
            raise RuntimeError(
                f"EWC/cart price mismatch: "
                f"ewc={ewc_line.price}, cart={after_line.price}"
            )

        print(
            f"[SMOKE PASS] newly added cart item: asin={after_line.asin}, "
            f"quantity={after_line.quantity}, price={after_line.price}, "
            f"cart_total={after_total}"
        )
        print("[SMOKE] The new item is intentionally kept in the cart.")
        print("[SMOKE] No database row was read or changed.")
        return True
    except Exception as exc:
        print(f"[SMOKE FAIL] {type(exc).__name__}: {exc}")
        if add_clicked:
            print(
                "[SMOKE WARNING] Add-to-Cart was clicked once. "
                "Inspect the target ASIN in the cart manually; "
                "the script will not click Add or Delete again."
            )
        return False
    finally:
        if crawler.page:
            try:
                crawler.page.quit()
            except Exception as exc:
                print(f"[SMOKE WARNING] Browser cleanup failed: {exc}")


def main():
    parser = argparse.ArgumentParser(
        description="One-ASIN Amazon TV hidden cart-price smoke test"
    )
    parser.add_argument("--asin", default=DEFAULT_ASIN)
    parser.add_argument(
        "--url",
        help="Exact Amazon PDP URL. Defaults to the canonical URL for --asin.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow one Add-to-Cart click when the ASIN is not already present.",
    )
    parser.add_argument(
        "--keep-in-cart",
        action="store_true",
        help="Acknowledge that a newly added item will remain in the cart.",
    )
    args = parser.parse_args()

    asin = normalize_asin(args.asin)
    product_url = args.url or f"https://www.amazon.com/dp/{asin}"
    if not args.live or not args.keep_in_cart:
        parser.error(
            "live cart mutation is disabled; pass both --live and "
            "--keep-in-cart after reviewing the smoke-test plan"
        )

    success = run_smoke(asin, product_url)
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
