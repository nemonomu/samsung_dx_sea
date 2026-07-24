"""Pure HTML helpers for Amazon TV hidden cart prices.

This module does not navigate Amazon, click elements, or access the database.
It only parses a supplied PDP/cart HTML document and can scope a cart lookup
to the exact ASIN and merchant requested by the caller.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import parse_qs, urlparse

from lxml import html


ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
MONEY_RE = re.compile(r"^\$\d[\d,]*\.\d{2}$")
HIDDEN_CART_PRICE_MESSAGE = (
    "to see our price, add this item to your cart. "
    "you can always remove it later."
)
GENERIC_SEE_PRICE_IN_CART_MESSAGE = "see price in cart"


class CartPriceParseError(ValueError):
    """Raised when Amazon cart HTML is ambiguous or malformed."""


@dataclass(frozen=True)
class CartLine:
    asin: str
    quantity: int
    price: str
    source: str
    merchant_id: str | None = None


@dataclass(frozen=True)
class PdpOfferIdentity:
    asin: str
    merchant_id: str
    offer_listing_id: str


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


def normalize_amount_money(value):
    try:
        amount = Decimal(str(value or "").strip().replace(",", ""))
    except InvalidOperation as exc:
        raise CartPriceParseError(
            f"invalid numeric cart price: {value!r}"
        ) from exc
    if amount < 0 or amount.as_tuple().exponent < -2:
        raise CartPriceParseError(f"invalid numeric cart price: {value!r}")
    return f"${amount:,.2f}"


def normalize_merchant_id(value):
    merchant_id = str(value or "").strip().upper()
    if not merchant_id or not re.fullmatch(r"[A-Z0-9]{5,32}", merchant_id):
        raise CartPriceParseError(f"invalid merchant ID: {value!r}")
    return merchant_id


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


def has_generic_see_price_in_cart_message(tree):
    """Detect the exact generic hidden-price label in the PDP price table."""
    nodes = tree.xpath(
        "//table["
        "contains(concat(' ', normalize-space(@class), ' '), ' a-lineitem ')"
        "]//a"
    )
    for node in nodes:
        text = " ".join(node.text_content().split()).casefold()
        if text == GENERIC_SEE_PRICE_IN_CART_MESSAGE:
            return True
    return False


def hidden_pdp_price_trigger(tree, current_price):
    """Classify a recognized non-dollar hidden-price PDP state."""
    if current_price and "$" in str(current_price):
        return None

    normalized = " ".join(str(current_price or "").split()).casefold()
    if GENERIC_SEE_PRICE_IN_CART_MESSAGE in normalized:
        return "see_price_in_cart"
    if HIDDEN_CART_PRICE_MESSAGE in normalized:
        return "to_see_our_price_add_to_cart"
    if has_generic_see_price_in_cart_message(tree):
        return "see_price_in_cart"
    if has_hidden_cart_price_message(tree):
        return "to_see_our_price_add_to_cart"
    return None


def is_hidden_pdp_price_state(tree, current_price):
    """Return whether a non-dollar PDP value is a recognized hidden state."""
    return hidden_pdp_price_trigger(tree, current_price) is not None


def _matching_pdp_add_to_cart_form(tree, asin):
    asin = normalize_asin(asin)
    forms = tree.xpath(
        "//form[@id='addToCart' and "
        "contains(@action, '/gp/product/handle-buy-box')]"
    )
    matching = []
    for form in forms:
        form_asins = {
            str(value or "").strip().upper()
            for value in form.xpath(
                ".//input[@name='items[0.base][asin]']/@value"
            )
            if str(value or "").strip()
        }
        if asin not in form_asins:
            continue
        if form_asins != {asin}:
            raise CartPriceParseError(
                f"PDP addToCart form has ambiguous ASINs: {sorted(form_asins)}"
            )
        matching.append(form)

    if not matching:
        return None
    if len(matching) != 1:
        raise CartPriceParseError(
            f"PDP returned {len(matching)} addToCart forms for ASIN {asin}"
        )
    return matching[0]


def extract_pdp_offer_identity(tree, asin):
    """Return the exact current buy-box ASIN, merchant, and offer identity."""
    asin = normalize_asin(asin)
    form = _matching_pdp_add_to_cart_form(tree, asin)
    if form is None:
        return None

    merchant_ids = {
        normalize_merchant_id(value)
        for value in form.xpath(".//input[@name='merchantID']/@value")
        if str(value or "").strip()
    }
    if len(merchant_ids) != 1:
        raise CartPriceParseError(
            f"PDP returned {len(merchant_ids)} merchants for ASIN {asin}"
        )

    offer_ids = {
        str(value or "").strip()
        for value in form.xpath(
            ".//input[@name='items[0.base][offerListingId]' "
            "or @name='offerListingID']/@value"
        )
        if str(value or "").strip()
    }
    if len(offer_ids) != 1:
        raise CartPriceParseError(
            f"PDP returned {len(offer_ids)} offers for ASIN {asin}"
        )

    return PdpOfferIdentity(
        asin=asin,
        merchant_id=next(iter(merchant_ids)),
        offer_listing_id=next(iter(offer_ids)),
    )


def extract_pdp_customer_visible_price(tree, asin):
    """Extract the exact-ASIN customer-visible price from the PDP buy-box form.

    Amazon can render this value in the add-to-cart form even when the visible
    price area contains only a hidden-price message. Ambiguous forms, ASINs, or
    prices are rejected instead of guessing.
    """
    asin = normalize_asin(asin)
    form = _matching_pdp_add_to_cart_form(tree, asin)
    if form is None:
        return None

    prices = []
    for value in form.xpath(
        ".//input["
        "@name='items[0.base][customerVisiblePrice][displayString]'"
        "]/@value"
    ):
        price = normalize_money(value)
        if price not in prices:
            prices.append(price)
    if len(prices) != 1:
        raise CartPriceParseError(
            f"PDP returned {len(prices)} customer-visible prices for ASIN {asin}"
        )
    return prices[0]


def resolve_hidden_pdp_price(tree, asin, current_price):
    """Use the exact-ASIN PDP form price only for a hidden-price PDP state.

    Returns ``(price, used_fallback)``. A normal dollar price is preserved.
    Long-form and exact price-table ``See price in cart`` states are eligible;
    unavailable and unrelated page text are not replaced.
    """
    if not is_hidden_pdp_price_state(tree, current_price):
        return current_price, False
    hidden_price = extract_pdp_customer_visible_price(tree, asin)
    if hidden_price is None:
        return current_price, False
    return hidden_price, True


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


def _cart_row_merchant_ids(row, asin):
    merchant_ids = set()
    for href in row.xpath(
        ".//a[contains(@href, '/gp/product/') and "
        "contains(@href, 'smid=')]/@href"
    ):
        try:
            parsed = urlparse(str(href))
            if f"/GP/PRODUCT/{asin}" not in parsed.path.upper():
                continue
            values = parse_qs(parsed.query).get("smid", [])
        except (TypeError, ValueError):
            values = []
        for value in values:
            merchant_ids.add(normalize_merchant_id(value))
    return merchant_ids


def extract_active_cart_offer_line(tree, asin, merchant_id):
    """Extract a cart price only for the exact active ASIN and merchant.

    The merchant is read from the active cart product link's ``smid`` query
    parameter and compared with the PDP buy-box form's ``merchantID``. Rows
    with a missing or different merchant are never used.
    """
    asin = normalize_asin(asin)
    merchant_id = normalize_merchant_id(merchant_id)
    rows = [
        row
        for row in tree.xpath(
            "//*[@id='sc-active-cart']"
            "//div[@data-asin and "
            "contains(concat(' ', normalize-space(@class), ' '), "
            "' sc-list-item ')]"
        )
        if str(row.get("data-asin") or "").strip().upper() == asin
    ]

    matching = []
    for row in rows:
        row_merchants = _cart_row_merchant_ids(row, asin)
        if len(row_merchants) > 1:
            raise CartPriceParseError(
                f"active cart row has ambiguous merchants for ASIN {asin}: "
                f"{sorted(row_merchants)}"
            )
        if row_merchants == {merchant_id}:
            matching.append(row)

    if not matching:
        return None
    if len(matching) != 1:
        raise CartPriceParseError(
            f"active cart returned {len(matching)} rows for ASIN {asin} "
            f"and merchant {merchant_id}"
        )

    row = matching[0]
    quantity = _row_quantity(row, "active cart offer", asin)
    price = _single_price(
        row,
        ".//div[contains(concat(' ', normalize-space(@class), ' '), "
        "' sc-apex-cart-price ')]"
        "//span[contains(concat(' ', normalize-space(@class), ' '), "
        "' a-offscreen ')]",
        "active cart offer",
        asin,
    )
    data_price = row.get("data-price")
    if str(data_price or "").strip():
        normalized_data_price = normalize_amount_money(data_price)
        if normalized_data_price != price:
            raise CartPriceParseError(
                f"active cart visible/data price mismatch for ASIN {asin}: "
                f"visible={price}, data={normalized_data_price}"
            )
    return CartLine(
        asin=asin,
        quantity=quantity,
        price=price,
        source="active_cart_offer",
        merchant_id=merchant_id,
    )


def build_cart_price_report_lines(resolutions, limit=50):
    """Build stable plain-text email lines for resolved cart-price offers."""
    unique = []
    positions = {}
    for resolution in resolutions or []:
        item = {
            "item": " ".join(str(resolution.get("item") or "-").split()),
            "price": " ".join(str(resolution.get("price") or "-").split()),
            "source": " ".join(str(resolution.get("source") or "-").split()),
            "trigger": " ".join(str(resolution.get("trigger") or "-").split()),
            "merchant": " ".join(
                str(resolution.get("merchant") or "-").split()
            ),
            "quantity": resolution.get("quantity"),
        }
        key = (item["item"], item["merchant"])
        if key in positions:
            unique[positions[key]] = item
        else:
            positions[key] = len(unique)
            unique.append(item)

    lines = [f"cart-price resolved: {len(unique)}"]
    for item in unique[:limit]:
        quantity = item["quantity"]
        quantity_text = "-" if quantity is None else str(quantity)
        lines.append(
            f"- item={item['item']} price={item['price']} "
            f"source={item['source']} trigger={item['trigger']} "
            f"merchant={item['merchant']} quantity={quantity_text}"
        )
    if len(unique) > limit:
        lines.append(f"- omitted: {len(unique) - limit}")
    return lines
