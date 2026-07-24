"""Pure HTML helpers for Amazon TV hidden cart prices.

This module does not navigate Amazon, click elements, or access the database.
It only parses a supplied PDP/cart HTML document and scopes every cart lookup
to the exact ASIN requested by the caller.
"""

from dataclasses import dataclass
import re

from lxml import html


ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
MONEY_RE = re.compile(r"^\$\d[\d,]*\.\d{2}$")
HIDDEN_CART_PRICE_MESSAGE = (
    "to see our price, add this item to your cart. "
    "you can always remove it later."
)


class CartPriceParseError(ValueError):
    """Raised when Amazon cart HTML is ambiguous or malformed."""


@dataclass(frozen=True)
class CartLine:
    asin: str
    quantity: int
    price: str
    source: str


def parse_html(page_html):
    """Return an lxml tree for a non-empty HTML document."""
    if not page_html or not str(page_html).strip():
        raise CartPriceParseError("page HTML is empty")
    try:
        return html.fromstring(page_html)
    except Exception as exc:
        raise CartPriceParseError("page HTML could not be parsed") from exc


def normalize_asin(value):
    asin = str(value or "").strip().upper()
    if not ASIN_RE.fullmatch(asin):
        raise CartPriceParseError(f"invalid ASIN: {value!r}")
    return asin


def normalize_money(value):
    compact = re.sub(r"\s+", "", str(value or ""))
    if not MONEY_RE.fullmatch(compact):
        raise CartPriceParseError(f"invalid cart price: {value!r}")
    return compact


def has_hidden_cart_price_message(tree):
    """Detect only the logged-in long-form hidden-price PDP message.

    A generic logged-out ``See price in cart`` label does not match.
    Authentication still needs to be verified by the live browser helper
    before any Add-to-Cart action is allowed.
    """
    nodes = tree.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), "
        "' priceToPayReplacementText ')]"
    )
    for node in nodes:
        text = " ".join(node.text_content().split()).casefold()
        if HIDDEN_CART_PRICE_MESSAGE in text:
            return True
    return False


def active_cart_total_count(tree):
    nodes = tree.xpath("//*[@id='sc-active-cart']")
    if len(nodes) != 1:
        return None
    raw = nodes[0].get("data-cart-total-item-count")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _single_target_row(tree, row_xpath, asin, source):
    asin = normalize_asin(asin)
    rows = [
        row for row in tree.xpath(row_xpath)
        if str(row.get("data-asin") or "").strip().upper() == asin
    ]
    if not rows:
        return None
    if len(rows) != 1:
        raise CartPriceParseError(
            f"{source} returned {len(rows)} rows for ASIN {asin}"
        )
    return rows[0]


def _row_quantity(row, source, asin):
    raw = row.get("data-quantity")
    try:
        quantity = int(raw)
    except (TypeError, ValueError) as exc:
        raise CartPriceParseError(
            f"{source} quantity is invalid for ASIN {asin}: {raw!r}"
        ) from exc
    if quantity < 1:
        raise CartPriceParseError(
            f"{source} quantity must be positive for ASIN {asin}: {quantity}"
        )
    return quantity


def _single_price(row, price_xpath, source, asin):
    prices = []
    for value in row.xpath(price_xpath):
        raw = value.text_content() if hasattr(value, "text_content") else value
        if not str(raw or "").strip():
            continue
        price = normalize_money(raw)
        if price not in prices:
            prices.append(price)
    if len(prices) != 1:
        raise CartPriceParseError(
            f"{source} returned {len(prices)} prices for ASIN {asin}"
        )
    return prices[0]


def extract_ewc_cart_line(tree, asin):
    """Extract one exact-ASIN line from Amazon's PDP mini cart (EWC)."""
    asin = normalize_asin(asin)
    row = _single_target_row(
        tree,
        "//*[@id='nav-flyout-ewc']"
        "//div[@data-asin and "
        "contains(concat(' ', normalize-space(@class), ' '), ' ewc-item ')]",
        asin,
        "EWC",
    )
    if row is None:
        return None
    quantity = _row_quantity(row, "EWC", asin)
    price = _single_price(
        row,
        ".//span[contains(concat(' ', normalize-space(@class), ' '), "
        "' ewc-unit-price ')]",
        "EWC",
        asin,
    )
    return CartLine(asin=asin, quantity=quantity, price=price, source="ewc")


def extract_active_cart_line(tree, asin):
    """Extract one exact-ASIN unit price from the full active cart.

    The XPath is relative to the matching ASIN row, so subtotal, checkout
    totals, recommendations, and other product rows cannot be selected.
    """
    asin = normalize_asin(asin)
    row = _single_target_row(
        tree,
        "//*[@id='sc-active-cart']"
        "//div[@data-asin and "
        "contains(concat(' ', normalize-space(@class), ' '), "
        "' sc-list-item ')]",
        asin,
        "active cart",
    )
    if row is None:
        return None
    quantity = _row_quantity(row, "active cart", asin)
    price = _single_price(
        row,
        ".//div[contains(concat(' ', normalize-space(@class), ' '), "
        "' sc-apex-cart-price ')]"
        "//span[contains(concat(' ', normalize-space(@class), ' '), "
        "' a-offscreen ')]",
        "active cart",
        asin,
    )
    return CartLine(
        asin=asin,
        quantity=quantity,
        price=price,
        source="active_cart",
    )
