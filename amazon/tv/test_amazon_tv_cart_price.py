import unittest

from amazon.tv.amazon_tv_cart_price import (
    CartPriceParseError,
    active_cart_total_count,
    extract_active_cart_line,
    extract_ewc_cart_line,
    extract_pdp_customer_visible_price,
    has_hidden_cart_price_message,
    parse_html,
)
from amazon.tv.amazon_tv_cart_price_smoke import (
    _save_state,
    _visible_add_to_cart_button,
    _visible_page_markers,
    _visible_warranty_decline_button,
)


LOGGED_IN_HIDDEN_PDP = """
<html><body>
  <div class="priceToPayReplacementText">
    <span>To see our price, add this item to your cart. You can always remove it later.</span>
    <a>Why?</a>
  </div>
</body></html>
"""

LOGGED_OUT_PDP = """
<html><body><div id="corePriceDisplay_desktop_feature_div">
  <a>See price in cart</a>
</div></body></html>
"""

EWC_HTML = """
<html><body><div id="nav-flyout-ewc">
  <div class="a-row ewc-item" data-asin="B0DXMZQ3MN" data-quantity="1">
    <span class="ewc-unit-price ewc-wider-compact-view-only">
      <font>$2,997.95</font>
    </span>
  </div>
</div></body></html>
"""

MULTI_ITEM_CART_HTML = """
<html><body>
  <div id="sc-active-cart" data-cart-total-item-count="3">
    <div class="a-row sc-list-item" data-asin="B0DXMZR1K7" data-quantity="1">
      <div class="sc-apex-cart-price"><span class="a-price">
        <span class="a-offscreen">$2,997.99</span>
      </span></div>
    </div>
    <div class="a-row sc-list-item" data-asin="B0DXMZQ3MN" data-quantity="2">
      <div class="sc-apex-cart-price"><span class="a-price">
        <span class="a-offscreen">$2,997.95</span>
      </span></div>
    </div>
  </div>
  <span id="sc-subtotal-amount-activecart">$8,993.89</span>
</body></html>
"""

PDP_HIDDEN_FORM_HTML = """
<html><body>
  <form id="addToCart" action="/gp/product/handle-buy-box/ref=dp_start-bbf_1_glance">
    <input name="items[0.base][customerVisiblePrice][displayString]"
           value="$2,997.95">
    <input name="items[0.base][asin]" value="B0DXMZQ3MN">
  </form>
</body></html>
"""


class AmazonTVCartPriceParserTests(unittest.TestCase):
    def test_long_hidden_price_message_matches(self):
        self.assertTrue(
            has_hidden_cart_price_message(parse_html(LOGGED_IN_HIDDEN_PDP))
        )

    def test_generic_logged_out_message_does_not_match(self):
        self.assertFalse(has_hidden_cart_price_message(parse_html(LOGGED_OUT_PDP)))

    def test_ewc_price_is_scoped_to_exact_asin(self):
        line = extract_ewc_cart_line(parse_html(EWC_HTML), "B0DXMZQ3MN")
        self.assertEqual(line.asin, "B0DXMZQ3MN")
        self.assertEqual(line.quantity, 1)
        self.assertEqual(line.price, "$2,997.95")
        self.assertEqual(line.source, "ewc")

    def test_multi_item_cart_returns_only_requested_asin_price(self):
        tree = parse_html(MULTI_ITEM_CART_HTML)
        target = extract_active_cart_line(tree, "B0DXMZQ3MN")
        other = extract_active_cart_line(tree, "B0DXMZR1K7")
        self.assertEqual(target.quantity, 2)
        self.assertEqual(target.price, "$2,997.95")
        self.assertEqual(other.price, "$2,997.99")
        self.assertNotEqual(target.price, "$8,993.89")
        self.assertEqual(active_cart_total_count(tree), 3)

    def test_missing_asin_returns_none(self):
        self.assertIsNone(
            extract_active_cart_line(
                parse_html(MULTI_ITEM_CART_HTML), "B000000000"
            )
        )

    def test_duplicate_asin_rows_are_rejected(self):
        duplicate = MULTI_ITEM_CART_HTML.replace(
            "</div>\n  <span id=\"sc-subtotal",
            """
            <div class="a-row sc-list-item" data-asin="B0DXMZQ3MN" data-quantity="1">
              <div class="sc-apex-cart-price"><span class="a-offscreen">$1.00</span></div>
            </div>
            </div>
            <span id="sc-subtotal""",
            1,
        )
        with self.assertRaises(CartPriceParseError):
            extract_active_cart_line(parse_html(duplicate), "B0DXMZQ3MN")

    def test_invalid_price_is_rejected(self):
        invalid = EWC_HTML.replace("$2,997.95", "See price in cart")
        with self.assertRaises(CartPriceParseError):
            extract_ewc_cart_line(parse_html(invalid), "B0DXMZQ3MN")

    def test_pdp_customer_visible_price_is_scoped_to_exact_asin(self):
        tree = parse_html(PDP_HIDDEN_FORM_HTML)
        self.assertEqual(
            extract_pdp_customer_visible_price(tree, "B0DXMZQ3MN"),
            "$2,997.95",
        )
        self.assertIsNone(
            extract_pdp_customer_visible_price(tree, "B000000000")
        )

    def test_pdp_customer_visible_price_rejects_ambiguous_forms(self):
        duplicate = PDP_HIDDEN_FORM_HTML.replace(
            "</body>",
            PDP_HIDDEN_FORM_HTML.split("<body>", 1)[1].split("</body>", 1)[0]
            + "</body>",
        )
        with self.assertRaises(CartPriceParseError):
            extract_pdp_customer_visible_price(
                parse_html(duplicate), "B0DXMZQ3MN"
            )

    def test_visible_add_button_uses_drissionpage_states_api(self):
        class FakeStates:
            is_displayed = True

        class FakeButton:
            states = FakeStates()

        class FakePage:
            def eles(self, locator):
                self.locator = locator
                return [FakeButton()]

        page = FakePage()
        button = _visible_add_to_cart_button(page)
        self.assertIsInstance(button, FakeButton)
        self.assertEqual(
            page.locator,
            'css:input#add-to-cart-button[name="submit.add-to-cart"]',
        )

    def test_save_state_records_url_without_cart_mutation(self):
        class FakeStates:
            is_displayed = False

        class FakeElement:
            states = FakeStates()

        class FakePage:
            html = """
                <html><body>
                  <div data-asin="B0DXMZQ3MN">No Thanks</div>
                </body></html>
            """
            url = "https://www.amazon.com/example"

            def eles(self, locator, timeout=0):
                return [FakeElement()]

        class FakeCrawler:
            page = FakePage()

            def __init__(self):
                self.saved = []

            def save_debug_html(self, tag, max_files=3):
                self.saved.append((tag, max_files))
                return None

        crawler = FakeCrawler()
        tree = _save_state(crawler, "post_add_response", "B0DXMZQ3MN")
        self.assertEqual(tree.xpath("string(//*[@data-asin])").strip(), "No Thanks")
        self.assertEqual(crawler.saved, [("post_add_response", 10)])

    def test_visible_markers_ignore_hidden_html_templates(self):
        class FakeStates:
            def __init__(self, displayed):
                self.is_displayed = displayed

        class FakeElement:
            def __init__(self, displayed):
                self.states = FakeStates(displayed)

        class FakePage:
            def eles(self, locator, timeout=0):
                if locator == "css:#sc-buy-box-ptc-button-announce":
                    return [FakeElement(True)]
                if "Added to Cart" in locator:
                    return [FakeElement(False)]
                return []

        markers, errors = _visible_page_markers(FakePage())
        self.assertEqual(markers, ["proceed to checkout"])
        self.assertEqual(errors, [])

    def test_visible_warranty_decline_is_scoped_to_exact_asin(self):
        class FakeStates:
            is_displayed = True

        class FakeElement:
            states = FakeStates()

        class FakePage:
            html = """
                <html><body>
                  <input id="attach-baseAsin" value="B0DXMZQ3MN">
                  <div id="attach-warranty-pane" style="display:block">
                    <span id="attachSiNoCoverage">
                      <input class="a-button-input" type="submit">
                    </span>
                  </div>
                </body></html>
            """

            def eles(self, locator, timeout=0):
                return [FakeElement()]

        button = _visible_warranty_decline_button(
            FakePage(), "B0DXMZQ3MN"
        )
        self.assertIsInstance(button, FakeElement)

        FakePage.html = FakePage.html.replace("B0DXMZQ3MN", "B000000000")
        with self.assertRaises(RuntimeError):
            _visible_warranty_decline_button(FakePage(), "B0DXMZQ3MN")


if __name__ == "__main__":
    unittest.main()
