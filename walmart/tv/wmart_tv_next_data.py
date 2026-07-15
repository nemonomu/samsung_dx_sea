"""Walmart TV __NEXT_DATA__ HTTP helpers."""

import json
import re
import time
from datetime import datetime
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import urljoin

import requests
from lxml import html as lxml_html

WALMART_BASE_URL = "https://www.walmart.com"
ZENROWS_API_URL = "https://api.zenrows.com/v1/"
HTTP_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}


def collapse_ws(value):
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())
    return text or None


def normalize_int(value):
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).replace(",", "").replace("+", "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*([KkMm])?", text)
    if not match:
        return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    if suffix == "K":
        number *= 1000
    elif suffix == "M":
        number *= 1000000
    return int(number)


def normalize_count_text(value):
    count = normalize_int(value)
    return str(count) if count is not None else None


def format_count_text(value):
    count = normalize_int(value)
    return f"{count:,}" if count is not None else None


def format_money(value):
    if value in (None, ""):
        return None
    if isinstance(value, str):
        text = collapse_ws(value)
        if not text:
            return None
        if "$" in text:
            return text.replace("Now ", "").strip()
        try:
            value = float(text.replace(",", ""))
        except ValueError:
            return None
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return None


def item_id_from_url(url):
    if not url:
        return None
    for pattern in (r"/ip/(?:[^/?]+/)?(\d+)", r"%2F(\d+)%3F", r"/(\d+)(?:\?|$)"):
        match = re.search(pattern, str(url))
        if match:
            return match.group(1)
    return None


def absolute_walmart_url(url):
    if not url:
        return None
    text = str(url).strip()
    if text.startswith(("http://", "https://")):
        return text
    return urljoin(WALMART_BASE_URL, text)


def build_item_url(item):
    return f"{WALMART_BASE_URL}/ip/{item}" if item else None


def build_review_url(item, page_number=1):
    if not item:
        return None
    url = f"{WALMART_BASE_URL}/reviews/product/{item}"
    if int(page_number or 1) > 1:
        url += f"?page={int(page_number)}"
    return url


def build_listing_url(url_template, page_number, page_type=None):
    url = (url_template or "").replace("{page}", str(page_number))
    if page_type == "bsr" and int(page_number) == 1:
        url = url.replace("&page=1", "").replace("?page=1&", "?")
    return url


def is_blocked_html(html_text, final_url=None):
    lower = (html_text or "").lower()
    final_lower = (final_url or "").lower()
    return (
        "/blocked" in final_lower
        or "robot or human" in lower
        or "px-captcha" in lower
        or "validatecaptcha" in lower
        or ("captcha.js" in lower and "blocked?url=" in lower)
    )


def extract_next_data(html_text):
    if not html_text:
        return None
    match = re.search(r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", html_text, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(html_unescape(match.group(1)))
    except Exception:
        return None


def get_initial_data(next_data):
    return ((next_data or {}).get("props", {}).get("pageProps", {}).get("initialData", {}).get("data", {}))


def read_zenrows_api_key(config_path=None):
    if config_path is None:
        config_path = Path(__file__).resolve().parents[1] / "config.py"
    text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"^\s*ZENROWS_API_KEY\s*=\s*[\"']?([^\"'\s#]+)", text, re.M)
    return match.group(1) if match else None


class WalmartNextDataClient:
    def __init__(self, timeout=30, zenrows_timeout=120, config_path=None):
        self.timeout = timeout
        self.zenrows_timeout = zenrows_timeout
        self.config_path = config_path
        self.session = requests.Session()
        self.zenrows_api_key = None

    def fetch_direct_html(self, url, retries=1):
        last_meta = None
        last_html = ""
        for attempt in range(1, retries + 2):
            started = time.time()
            meta = {"source": "direct", "url": url, "status": None, "final_url": None, "elapsed_sec": None, "html_len": 0, "blocked": False, "error": None, "attempt": attempt}
            try:
                response = self.session.get(url, headers=HTTP_HEADERS, timeout=self.timeout, allow_redirects=True)
                html_text = response.text or ""
                meta.update({"status": response.status_code, "final_url": response.url, "elapsed_sec": round(time.time() - started, 2), "html_len": len(html_text), "blocked": is_blocked_html(html_text, response.url)})
                last_meta, last_html = meta, html_text
                if response.status_code == 200 and not meta["blocked"]:
                    return meta, html_text
                self.session = requests.Session()
            except Exception as exc:
                meta.update({"elapsed_sec": round(time.time() - started, 2), "error": f"{type(exc).__name__}: {exc}"})
                last_meta, last_html = meta, ""
                self.session = requests.Session()
        return last_meta, last_html

    def fetch_zenrows_html(self, url, js_render=False):
        if not self.zenrows_api_key:
            self.zenrows_api_key = read_zenrows_api_key(self.config_path)
        started = time.time()
        meta = {"source": "zenrows_js" if js_render else "zenrows_static", "url": url, "status": None, "final_url": None, "elapsed_sec": None, "html_len": 0, "blocked": False, "error": None}
        if not self.zenrows_api_key:
            meta["error"] = "ZENROWS_API_KEY not found"
            return meta, ""
        params = {"apikey": self.zenrows_api_key, "url": url, "premium_proxy": "true", "proxy_country": "us", "original_status": "true", "allowed_status_codes": "200,301,302,403,404,500,503"}
        if js_render:
            params.update({"js_render": "true", "wait": "3000"})
        try:
            response = requests.get(ZENROWS_API_URL, params=params, timeout=self.zenrows_timeout)
            html_text = response.text or ""
            meta.update({"status": response.status_code, "final_url": response.url, "elapsed_sec": round(time.time() - started, 2), "html_len": len(html_text), "blocked": is_blocked_html(html_text, response.url)})
            return meta, html_text
        except Exception as exc:
            meta.update({"elapsed_sec": round(time.time() - started, 2), "error": f"{type(exc).__name__}: {exc}"})
            return meta, ""

    def fetch_next_data(self, url, direct_retries=1, use_zenrows=True, js_render_fallback=True):
        attempts = []
        meta, html_text = self.fetch_direct_html(url, retries=direct_retries)
        if meta:
            attempts.append(meta)
        next_data = extract_next_data(html_text)
        if next_data and meta and not meta.get("blocked"):
            return {"next_data": next_data, "html": html_text, "source": meta.get("source"), "meta": meta, "attempts": attempts}
        if use_zenrows:
            for js_render in ([False, True] if js_render_fallback else [False]):
                meta, html_text = self.fetch_zenrows_html(url, js_render=js_render)
                attempts.append(meta)
                next_data = extract_next_data(html_text)
                if next_data and not meta.get("blocked"):
                    return {"next_data": next_data, "html": html_text, "source": meta.get("source"), "meta": meta, "attempts": attempts}
        return {"next_data": None, "html": "", "source": None, "meta": None, "attempts": attempts}


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _content_values(node):
    values = []
    if isinstance(node, dict):
        for key in ("text", "value", "contDesc"):
            text = collapse_ws(node.get(key))
            if text:
                values.append(text)
        for child in node.values():
            values.extend(_content_values(child))
    elif isinstance(node, list):
        for child in node:
            values.extend(_content_values(child))
    return values


def _unique_texts(values):
    seen = set()
    result = []
    for value in values:
        text = collapse_ws(value)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _badge_texts(item):
    return _unique_texts(_content_values({"badge": item.get("badge"), "badges": item.get("badges")}))


def _find_text_containing(texts, *needles):
    for text in texts:
        lower = text.lower()
        if all(needle.lower() in lower for needle in needles):
            return text
    return None


def _find_following_badge_value(item, phrase):
    groups = (((item.get("badges") or {}).get("groupsV2")) or [])
    phrase_lower = phrase.lower()
    for group in groups:
        for member in group.get("members") or []:
            values = []
            for content in member.get("content") or []:
                value = collapse_ws(content.get("value") or content.get("text") or content.get("contDesc"))
                if value:
                    values.append(value)
            for index, value in enumerate(values):
                if phrase_lower in value.lower():
                    for next_value in values[index + 1:]:
                        if next_value and next_value.lower() != value.lower():
                            return next_value
                    return value
    return None


def _extract_free_offer_count(item):
    text = _find_text_containing(_badge_texts(item), "free", "offer")
    return normalize_count_text(text)


def _extract_available_quantity(item):
    text = _find_text_containing(_badge_texts(item), "only", "left")
    return normalize_count_text(text)


def _extract_inventory_status(item):
    return _find_text_containing(_badge_texts(item), "low stock")


def _extract_sku_status(item):
    parts = []
    if item.get("isSponsoredFlag"):
        parts.append("Sponsored")
    for text in _badge_texts(item):
        if text.lower() == "rollback":
            parts.append("Rollback")
    return ", ".join(_unique_texts(parts)) if parts else None


def _is_listing_product(item):
    if not isinstance(item, dict):
        return False
    url = item.get("canonicalUrl") or item.get("productPageUrl")
    return bool(item.get("name") and item_id_from_url(url or ""))


def _listing_items_from_next_data(next_data):
    initial_data = get_initial_data(next_data)
    stacks = (initial_data.get("searchResult") or {}).get("itemStacks") or []
    for stack in stacks:
        items = stack.get("items") if isinstance(stack, dict) else None
        if not isinstance(items, list):
            continue
        product_items = [item for item in items if _is_listing_product(item)]
        if product_items:
            return product_items
    best = []
    for node in _walk(next_data):
        items = node.get("items") if isinstance(node, dict) else None
        if not isinstance(items, list):
            continue
        product_items = [item for item in items if _is_listing_product(item)]
        if len(product_items) > len(best):
            best = product_items
    return best


def parse_listing_products(next_data, *, account_name, page_type, page_number, calendar_week, batch_id):
    products = []
    for item in _listing_items_from_next_data(next_data):
        product_url = absolute_walmart_url(item.get("canonicalUrl") or item.get("productPageUrl"))
        products.append({
            "account_name": account_name,
            "page_type": page_type,
            "retailer_sku_name": collapse_ws(item.get("name")),
            "offer": _extract_free_offer_count(item),
            "pick_up_availability": _find_following_badge_value(item, "Free pickup"),
            "fastest_delivery": _find_following_badge_value(item, "Free shipping"),
            "delivery_availability": _find_following_badge_value(item, "Delivery"),
            "sku_status": _extract_sku_status(item),
            "available_quantity_for_purchase": _extract_available_quantity(item),
            "inventory_status": _extract_inventory_status(item),
            "main_rank": 0,
            "bsr_rank": 0,
            "page_number": page_number,
            "product_url": product_url,
            "calendar_week": calendar_week,
            "crawl_datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "batch_id": batch_id,
        })
    return products


def _price_string(price_info):
    if not isinstance(price_info, dict):
        return None
    for key in ("currentPrice", "price", "itemPrice"):
        value = price_info.get(key)
        if isinstance(value, dict):
            for nested_key in ("priceString", "priceDisplay", "linePrice", "linePriceDisplay", "price"):
                formatted = format_money(value.get(nested_key))
                if formatted:
                    return formatted
        else:
            formatted = format_money(value)
            if formatted:
                return formatted
    for key in ("linePrice", "linePriceDisplay"):
        formatted = format_money(price_info.get(key))
        if formatted:
            return formatted
    estimated_total = ((price_info.get("additionalFees") or {}).get("estimatedTotalPrice") or {})
    if isinstance(estimated_total, dict):
        for nested_key in ("priceString", "price"):
            formatted = format_money(estimated_total.get(nested_key))
            if formatted:
                return formatted
    return None


def _was_price_string(price_info):
    if not isinstance(price_info, dict):
        return None
    for key in ("wasPrice", "comparisonPrice"):
        value = price_info.get(key)
        if isinstance(value, dict):
            for nested_key in ("priceString", "priceDisplay", "linePrice", "linePriceDisplay", "price"):
                formatted = format_money(value.get(nested_key))
                if formatted:
                    return formatted
        else:
            formatted = format_money(value)
            if formatted:
                return formatted
    return None


def _dollar_amount_string(value):
    text = collapse_ws(value)
    if text:
        match = re.search(r"\$\s*[\d,]+(?:\.\d{1,2})?", text)
        if match:
            return match.group(0).replace("$ ", "$")

    formatted = format_money(value)
    if formatted and formatted.startswith("$"):
        return formatted
    return None


def _savings_string(price_info):
    if not isinstance(price_info, dict):
        return None
    for key in ("savingsAmount", "savings", "savingsAmt"):
        value = price_info.get(key)
        if isinstance(value, dict):
            for nested_key in ("priceString", "priceDisplay", "linePrice", "linePriceDisplay", "price"):
                formatted = _dollar_amount_string(value.get(nested_key))
                if formatted and formatted not in ("$0.00", "$0"):
                    return formatted
        else:
            formatted = _dollar_amount_string(value)
            if formatted and formatted not in ("$0.00", "$0"):
                return formatted
    return None


def _review_texts_from_reviews_node(reviews_node, limit=10):
    rows = []
    if not isinstance(reviews_node, dict):
        return rows
    for review in reviews_node.get("customerReviews") or []:
        if not isinstance(review, dict):
            continue
        text = collapse_ws(review.get("reviewText") or review.get("text") or review.get("reviewBody"))
        if text:
            rows.append(text)
        if len(rows) >= limit:
            break
    return rows


def find_reviews_node(value):
    best = None
    for node in _walk(value):
        reviews = node.get("customerReviews") if isinstance(node, dict) else None
        if isinstance(reviews, list):
            if best is None or len(reviews) > len(best.get("customerReviews") or []):
                best = node
    return best


def parse_review_page(next_data, limit=10):
    reviews_node = find_reviews_node(next_data)
    if not reviews_node:
        return {"reviews": [], "total_review_count": None, "star_rating": None}
    return {
        "reviews": _review_texts_from_reviews_node(reviews_node, limit=limit),
        "total_review_count": parse_text_review_count(reviews_node),
        "star_rating": collapse_ws(reviews_node.get("averageOverallRating") or reviews_node.get("roundedAverageOverallRating")),
    }


def format_reviews(review_texts, limit=20):
    rows = []
    seen = set()
    for text in review_texts:
        cleaned = collapse_ws(text)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(cleaned)
        if len(rows) >= limit:
            break
    if not rows:
        return None
    return " ||| ".join(f"review{index} - {text}" for index, text in enumerate(rows, 1))


SKU_POPULARITY_BADGE_VALUES = {
    "overall pick",
    "best seller",
    "rollback",
    "clearance",
    "reduced price",
    "flash deal",
    "sale",
    "popular pick",
}


def parse_text_review_count(*nodes):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in (
            "reviewsWithTextCount",
            "reviewWithTextCount",
            "textReviewCount",
            "customerReviewCount",
        ):
            value = format_count_text(node.get(key))
            if value is not None:
                return value
    return None


def parse_star_rating_count(*nodes):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in (
            "numberOfReviews",
            "totalReviewCount",
            "ratingCount",
            "ratingsCount",
            "totalRatings",
            "reviewsCount",
        ):
            value = format_count_text(node.get(key))
            if value is not None:
                return value
    return None


def parse_sku_popularity(product):
    values = []
    for text in _badge_texts(product):
        lower = text.lower()
        if any(token in lower for token in ("cart", "bought", "purchased", "pickup", "delivery", "shipping", "left")):
            continue
        if lower == "price when purchased online":
            continue
        if lower in SKU_POPULARITY_BADGE_VALUES:
            values.append(text)
    values = _unique_texts(values)
    return ", ".join(values) if values else None


def parse_social_counts(product):
    texts = _badge_texts(product)
    added_to_carts = None
    purchased_yesterday = None
    for text in texts:
        lower = text.lower()
        if "cart" in lower and added_to_carts is None:
            added_to_carts = normalize_count_text(text)
        if ("bought" in lower or "purchased" in lower) and purchased_yesterday is None:
            purchased_yesterday = normalize_count_text(text)
    return {
        "number_of_ppl_added_to_carts": added_to_carts,
        "number_of_ppl_purchased_yesterday": purchased_yesterday,
        "sku_popularity": parse_sku_popularity(product),
    }


def parse_discount_type(product, price_info=None):
    price_info = price_info if isinstance(price_info, dict) else product.get("priceInfo") or {}
    for text in _content_values(price_info):
        if collapse_ws(text) == "Price when purchased online":
            return "Price when purchased online"
    try:
        if "Price when purchased online" in json.dumps(price_info, ensure_ascii=False):
            return "Price when purchased online"
    except TypeError:
        pass
    return None


def parse_discount_type_from_html(html_text):
    text = html_unescape(html_text or "")
    return "Price when purchased online" if "Price when purchased online" in text else None


SIMILAR_HTML_XPATHS = (
    "//section[@data-dca-name='itemTile']//div[@role='group' and starts-with(@data-testid, 'product-tile-') and not(@data-testid='product-tile-1')]//span[@data-automation-id='product-title']/text()",
)
SIMILAR_MODULE_TITLES = {
    "see similar items",
    "compare with similar items",
}
SIMILAR_IMAGE_NAME_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)(?:$|\?)", re.I)
SIMILAR_UUID_IMAGE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"(?:\.[0-9a-f]+)?\.(?:jpe?g|png|webp|gif|avif)$",
    re.I,
)


def _similar_product_item_id(node):
    url = node.get("canonicalUrl") or node.get("productPageUrl") or node.get("productUrl") or ""
    item = node.get("usItemId") or node.get("itemId") or item_id_from_url(url)
    node_id = node.get("id")
    if not item and str(node_id or "").isdigit():
        item = node_id
    item = str(item).strip() if item is not None else None
    return item if item and item.isdigit() else None


def _similar_product_name(node):
    name = collapse_ws(node.get("name") or node.get("title") or node.get("productName"))
    if not name:
        return None
    lower = name.lower()
    if len(name) < 8:
        return None
    if SIMILAR_IMAGE_NAME_RE.search(lower) or SIMILAR_UUID_IMAGE_RE.match(lower):
        return None
    return name


def _iter_similar_module_products(module):
    configs = module.get("configs") if isinstance(module, dict) else None
    if not isinstance(configs, dict):
        return
    for key in ("products", "items"):
        value = configs.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                if isinstance(item.get("product"), dict):
                    yield item["product"]
                else:
                    yield item


def _iter_similar_next_data_modules(next_data):
    data = get_initial_data(next_data)
    modules = ((data.get("contentLayout") or {}).get("modules") or [])
    for module in modules:
        if not isinstance(module, dict):
            continue
        if module.get("type") != "ItemCarousel":
            continue
        configs = module.get("configs") if isinstance(module.get("configs"), dict) else {}
        title = collapse_ws(configs.get("title") or module.get("name")) or ""
        title_key = title.lower()
        if title_key in SIMILAR_MODULE_TITLES:
            yield module


def _join_unique_similar_names(pairs, current_item=None, limit=30):
    names = []
    seen = set()
    current_item = str(current_item) if current_item else None
    for item, name in pairs:
        item = str(item).strip() if item is not None else None
        name = collapse_ws(name)
        if not name or not item or (current_item and item == current_item):
            continue
        key = (item, name.lower())
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= limit:
            break
    return " ||| ".join(names) if names else None


def parse_similar_product_names_from_html(html_text, limit=30):
    if not html_text:
        return None
    try:
        tree = lxml_html.fromstring(html_text)
    except Exception:
        return None
    for xpath in SIMILAR_HTML_XPATHS:
        pairs = []
        for index, value in enumerate(tree.xpath(xpath), 1):
            name = collapse_ws(value)
            if name:
                pairs.append((str(index), name))
        result = _join_unique_similar_names(pairs, limit=limit)
        if result:
            return result
    return None


def parse_similar_product_names(next_data, current_item=None, limit=30):
    pairs = []
    for module in _iter_similar_next_data_modules(next_data):
        for node in _iter_similar_module_products(module) or []:
            item = _similar_product_item_id(node)
            name = _similar_product_name(node)
            if item and name:
                pairs.append((item, name))
    return _join_unique_similar_names(pairs, current_item=current_item, limit=limit)


def parse_detail_product(next_data, html_text=None):
    initial_data = get_initial_data(next_data)
    product = initial_data.get("product") or {}
    reviews_node = initial_data.get("reviews") or find_reviews_node(initial_data) or {}
    price_info = product.get("priceInfo") or {}
    social = parse_social_counts(product)
    item = product.get("usItemId") or product.get("id") or item_id_from_url(product.get("canonicalUrl") or "")
    retailer_sku_name_similar = (
        parse_similar_product_names_from_html(html_text)
        or parse_similar_product_names(next_data, current_item=item)
    )
    count_of_reviews = parse_text_review_count(reviews_node, product)
    count_of_star_ratings = parse_star_rating_count(product, reviews_node) or count_of_reviews
    star_rating = collapse_ws(product.get("averageRating") or reviews_node.get("averageOverallRating")) or "No ratings yet"
    if count_of_reviews is None and count_of_star_ratings is None:
        count_of_reviews = "0"
        count_of_star_ratings = "0"
    return {
        "item": str(item) if item else None,
        "retailer_sku_name": collapse_ws(product.get("name")),
        "count_of_reviews": count_of_reviews,
        "star_rating": star_rating,
        "count_of_star_ratings": count_of_star_ratings,
        "final_sku_price": _price_string(price_info),
        "original_sku_price": _was_price_string(price_info),
        "savings": _savings_string(price_info),
        "discount_type": parse_discount_type(product, price_info),
        "sku_popularity": social["sku_popularity"],
        "number_of_ppl_purchased_yesterday": social["number_of_ppl_purchased_yesterday"],
        "number_of_ppl_added_to_carts": social["number_of_ppl_added_to_carts"],
        "inline_reviews": _review_texts_from_reviews_node(reviews_node, limit=10),
        "retailer_sku_name_similar": retailer_sku_name_similar,
    }
