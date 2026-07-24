"""One-ASIN live smoke test for Amazon TV hidden cart prices.

Safety properties:
- no database connection or write;
- no review collection;
- no automatic Add-to-Cart retry;
- no automatic protection-plan selection;
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
    extract_pdp_customer_visible_price,
    has_hidden_cart_price_message,
    normalize_asin,
    parse_html,
    resolve_hidden_pdp_price,
)


CART_URL = "https://www.amazon.com/gp/cart/view.html"
DEFAULT_ASIN = "B0DXMZQ3MN"
VISIBLE_MARKER_LOCATORS = (
    (
        "added to cart",
        'xpath://*[not(self::script) and normalize-space(text())="Added to Cart"]',
    ),
    (
        "add a protection plan",
        'xpath://*[contains(normalize-space(text()), "Add a protection plan")]',
    ),
    ("warranty pane", "css:#attach-warranty-pane"),
    (
        "no thanks",
        "xpath://*[not(self::script) and "
        "translate(normalize-space(text()), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
        "'no thanks']",
    ),
    (
        "continue shopping",
        'xpath://*[contains(normalize-space(text()), "Continue shopping")]',
    ),
    ("proceed to checkout", "css:#sc-buy-box-ptc-button-announce"),
    (
        "was removed from shopping cart",
        'xpath://a[contains(normalize-space(.), "was removed from Shopping Cart")]',
    ),
    (
        "sorry, we just need to make sure",
        'xpath://*[contains(normalize-space(text()), '
        '"Sorry, we just need to make sure")]',
    ),
)


def _tree(page):
    return parse_html(page.html)


def _new_crawler(mode):
    from amazon.tv.amazon_tv_dt import AmazonTVDetailCrawler

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return AmazonTVDetailCrawler(
        batch_id=f"amazon_tv_cart_price_{mode}_{timestamp}",
        test_mode=True,
        require_amazon_login=False,
    )


def _is_visibly_rendered(element):
    """Require both element visibility and a rendered layout rectangle."""
    return bool(element.states.is_displayed and element.states.has_rect)


def _visible_page_markers(page):
    """Return diagnostic markers that are actually displayed in the browser."""
    visible = []
    errors = []
    for marker, locator in VISIBLE_MARKER_LOCATORS:
        try:
            elements = page.eles(locator, timeout=0.2) or []
            if any(_is_visibly_rendered(element) for element in elements):
                visible.append(marker)
        except Exception as exc:
            errors.append(f"{marker}:{type(exc).__name__}")
    return visible, errors


def _save_state(crawler, tag, asin):
    """Save the current DOM and URL without clicking or changing the page."""
    page_html = crawler.page.html
    tree = parse_html(page_html)
    markers, marker_errors = _visible_page_markers(crawler.page)
    asin_occurrences = page_html.upper().count(asin)
    print(f"[SMOKE STATE] tag={tag}, url={crawler.page.url}")
    print(
        f"[SMOKE STATE] asin={asin}, html_occurrences={asin_occurrences}, "
        f"visible_markers={markers}, marker_errors={marker_errors}"
    )

    filepath = crawler.save_debug_html(tag, max_files=10)
    if filepath:
        url_path = os.path.splitext(filepath)[0] + ".url.txt"
        try:
            with open(url_path, "w", encoding="utf-8") as handle:
                handle.write(str(crawler.page.url))
            print(f"[SMOKE STATE] URL saved: {url_path}")
        except OSError as exc:
            print(f"[SMOKE WARNING] URL snapshot failed: {exc}")
    return tree


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
    visibility_errors = []
    for button in buttons:
        try:
            if _is_visibly_rendered(button):
                visible.append(button)
        except Exception as exc:
            visibility_errors.append(type(exc).__name__)
    if len(visible) != 1:
        raise RuntimeError(
            f"expected one visible Add-to-Cart button, found {len(visible)} "
            f"(candidates={len(buttons)}, "
            f"visibility_errors={visibility_errors})"
        )
    return visible[0]


def _visible_warranty_decline_button(page, asin):
    """Return the exact visible No-thanks control for the target product."""
    panes = page.eles("css:#attach-warranty-pane", timeout=0.2) or []
    visible_panes = [pane for pane in panes if _is_visibly_rendered(pane)]
    if not visible_panes:
        return None
    if len(visible_panes) != 1:
        raise RuntimeError(
            f"expected one visible warranty pane, found {len(visible_panes)}"
        )

    asin = normalize_asin(asin)
    base_asins = {
        str(value or "").strip().upper()
        for value in _tree(page).xpath(
            "//*[@id='attach-baseAsin']/@value"
        )
        if str(value or "").strip()
    }
    if base_asins != {asin}:
        raise RuntimeError(
            f"warranty pane ASIN mismatch: expected={asin}, "
            f"found={sorted(base_asins)}"
        )

    buttons = page.eles(
        "css:#attach-warranty-pane "
        "#attachSiNoCoverage input.a-button-input[type='submit']",
        timeout=0.2,
    ) or []
    visible_buttons = [button for button in buttons if _is_visibly_rendered(button)]
    if len(visible_buttons) != 1:
        raise RuntimeError(
            "expected one visible warranty No-thanks button, "
            f"found {len(visible_buttons)}"
        )
    return visible_buttons[0]


def _wait_for_add_response(page, asin, timeout_seconds=15):
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            line = extract_ewc_cart_line(_tree(page), asin)
            if line:
                return "ewc", line
            decline_button = _visible_warranty_decline_button(page, asin)
            if decline_button:
                return "warranty", decline_button
        except (CartPriceParseError, RuntimeError) as exc:
            last_error = exc
            break
        time.sleep(1)
    if last_error:
        raise last_error
    return None, None


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


def run_cart_inspection(asin):
    """Read the exact ASIN from the active cart without any cart mutation."""
    crawler = _new_crawler("inspect")
    try:
        _setup_logged_in_browser(crawler)
        asin = normalize_asin(asin)
        line, total = _load_active_cart(crawler, asin)
        tree = _save_state(crawler, "cart_inspect", asin)
        raw_occurrences = crawler.page.html.upper().count(asin)
        visible_markers, marker_errors = _visible_page_markers(crawler.page)
        removed_notice = "was removed from shopping cart" in visible_markers
        print(f"[SMOKE INSPECT] cart total: {total}")
        print(
            f"[SMOKE INSPECT] raw ASIN occurrences in HTML: {raw_occurrences}; "
            f"visible_removed_notice={removed_notice}; "
            f"marker_errors={marker_errors}"
        )
        if line:
            print(
                f"[SMOKE INSPECT PASS] active cart item: asin={line.asin}, "
                f"quantity={line.quantity}, price={line.price}, action=read-only"
            )
        else:
            print(
                f"[SMOKE INSPECT PASS] asin={asin} is absent from the active "
                "cart; action=read-only"
            )
        return True
    except Exception as exc:
        print(f"[SMOKE INSPECT FAIL] {type(exc).__name__}: {exc}")
        return False
    finally:
        if crawler.page:
            try:
                crawler.page.quit()
            except Exception as exc:
                print(f"[SMOKE WARNING] Browser cleanup failed: {exc}")


def run_pdp_price_inspection(asin, product_url):
    """Read the exact-ASIN hidden PDP form price without any cart mutation."""
    from amazon.tv.amazon_login import is_amazon_login_verified_dp

    crawler = _new_crawler("pdp_inspect")
    try:
        _setup_logged_in_browser(crawler)
        asin = normalize_asin(asin)
        crawler.page.get(product_url)
        time.sleep(2)
        if not is_amazon_login_verified_dp(crawler.page):
            raise RuntimeError("Amazon login was not verified on the PDP")
        loaded_asin = crawler.extract_item(crawler.page.url)
        if loaded_asin != asin:
            raise RuntimeError(
                f"PDP ASIN mismatch: expected={asin}, loaded={loaded_asin}"
            )
        tree = _save_state(crawler, "pdp_price_inspect", asin)
        price, used_hidden_pdp_price = resolve_hidden_pdp_price(
            tree, asin, None
        )
        if price is None or not used_hidden_pdp_price:
            raise RuntimeError(
                "authenticated hidden-price PDP fallback was not applicable"
            )
        print(
            f"[SMOKE PDP PASS] asin={asin}, price={price}, "
            "source=customerVisiblePrice, action=read-only"
        )
        print("[SMOKE] No Add, No-thanks, cart, or database action was used.")
        return True
    except Exception as exc:
        print(f"[SMOKE PDP FAIL] {type(exc).__name__}: {exc}")
        return False
    finally:
        if crawler.page:
            try:
                crawler.page.quit()
            except Exception as exc:
                print(f"[SMOKE WARNING] Browser cleanup failed: {exc}")


def run_smoke(asin, product_url):
    from amazon.tv.amazon_login import is_amazon_login_verified_dp

    crawler = _new_crawler("live")
    add_clicked = False
    no_thanks_clicked = False
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

        pdp_price = extract_pdp_customer_visible_price(_tree(crawler.page), asin)
        print(
            f"[SMOKE] PDP customer-visible price: asin={asin}, "
            f"price={pdp_price}"
        )

        button = _visible_add_to_cart_button(crawler.page)
        print(f"[SMOKE] Add-to-Cart once: asin={asin}")
        if not crawler.click_element(button, label="Smoke Add-to-Cart"):
            raise RuntimeError("Add-to-Cart click failed")
        add_clicked = True

        response_type, response = _wait_for_add_response(crawler.page, asin)
        _save_state(crawler, "post_add_response", asin)
        ewc_line = response if response_type == "ewc" else None
        if response_type == "warranty":
            print(f"[SMOKE] Warranty No-thanks once: asin={asin}")
            if not crawler.click_element(
                response, label="Smoke warranty No-thanks"
            ):
                raise RuntimeError("warranty No-thanks click failed")
            no_thanks_clicked = True
            ewc_line = _wait_for_ewc_line(crawler.page, asin)
            _save_state(crawler, "post_no_thanks_response", asin)

        if ewc_line:
            print(
                f"[SMOKE] EWC price: asin={ewc_line.asin}, "
                f"quantity={ewc_line.quantity}, price={ewc_line.price}"
            )
        else:
            print("[SMOKE] EWC not available; verifying through the full cart")

        after_line, after_total = _load_active_cart(crawler, asin)
        _save_state(crawler, "cart_after_add", asin)
        if after_line is None:
            raise RuntimeError(
                "target ASIN was not found in the active cart after one Add "
                f"(before_total={before_total}, after_total={after_total})"
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
        if pdp_price and pdp_price != after_line.price:
            raise RuntimeError(
                f"PDP/cart price mismatch: "
                f"pdp={pdp_price}, cart={after_line.price}"
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
        if no_thanks_clicked:
            print(
                "[SMOKE WARNING] Warranty No-thanks was clicked once; "
                "no protection plan was selected."
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
    parser.add_argument(
        "--inspect-cart-only",
        action="store_true",
        help="Read the exact ASIN from the cart without clicking Add or Delete.",
    )
    parser.add_argument(
        "--inspect-pdp-price-only",
        action="store_true",
        help="Read the exact-ASIN PDP form price without any cart mutation.",
    )
    args = parser.parse_args()

    asin = normalize_asin(args.asin)
    product_url = args.url or f"https://www.amazon.com/dp/{asin}"
    if args.inspect_pdp_price_only:
        if args.inspect_cart_only or args.live or args.keep_in_cart:
            parser.error(
                "--inspect-pdp-price-only cannot be combined with cart or "
                "live mutation flags"
            )
        success = run_pdp_price_inspection(asin, product_url)
        raise SystemExit(0 if success else 1)
    if args.inspect_cart_only:
        if args.live or args.keep_in_cart:
            parser.error(
                "--inspect-cart-only cannot be combined with --live or "
                "--keep-in-cart"
            )
        success = run_cart_inspection(asin)
        raise SystemExit(0 if success else 1)
    if not args.live or not args.keep_in_cart:
        parser.error(
            "live cart mutation is disabled; pass both --live and "
            "--keep-in-cart after reviewing the smoke-test plan"
        )

    success = run_smoke(asin, product_url)
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
