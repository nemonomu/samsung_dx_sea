"""Walmart TV __NEXT_DATA__ HTTP helpers."""

import json
import re
import time
from datetime import datetime
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

import requests
from lxml import html as lxml_html

WALMART_BASE_URL = "https://www.walmart.com"
ZENROWS_API_URL = "https://api.zenrows.com/v1/"
PRODUCT_SCOPE_QUERY_KEYS = ("conditionGroupCode", "classType")
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


def format_star_rating(value):
    text = collapse_ws(value)
    if not text:
        return None
    if text.lower() == "no ratings yet":
        return "No ratings yet"
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return text
    try:
        return f"{float(match.group(0)):.1f}"
    except (TypeError, ValueError):
        return text


def parse_visible_rating_summary_from_html(html_text):
    if not html_text:
        return None, None
    try:
        tree = lxml_html.fromstring(html_text)
    except Exception:
        return None, None

    values = tree.xpath("//div[@data-testid='reviews-and-ratings']//div[@role='group']/@aria-label")
    for value in values:
        text = collapse_ws(value)
        match = re.search(
            r"(\d+(?:\.\d+)?)\s+out\s+of\s+5\s+stars?\s+rating[,\s]*([\d,]+)\s+ratings?",
            text,
            re.I,
        )
        if match:
            return format_star_rating(match.group(1)), format_count_text(match.group(2))
        match = re.search(
            r"(\d+(?:\.\d+)?)\s+stars?\s+out\s+of\s+([\d,]+)\s+reviews?",
            text,
            re.I,
        )
        if match:
            return format_star_rating(match.group(1)), format_count_text(match.group(2))

    if tree.xpath("//div[@data-testid='reviews-and-ratings']//span[contains(., 'No ratings yet')]"):
        return "No ratings yet", "0"
    return None, None


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


def product_scope_query_params(source_url):
    if not source_url:
        return {}
    canonical_keys = {key.lower(): key for key in PRODUCT_SCOPE_QUERY_KEYS}
    params = {}
    for key, value in parse_qsl(urlparse(str(source_url)).query, keep_blank_values=False):
        canonical_key = canonical_keys.get(key.lower())
        if canonical_key and value and canonical_key not in params:
            params[canonical_key] = value
    return params


def _build_scoped_url(base_url, source_url=None, extra_params=None):
    params = list(product_scope_query_params(source_url).items())
    params.extend(extra_params or [])
    return f"{base_url}?{urlencode(params)}" if params else base_url


def build_item_url(item, source_url=None):
    if not item:
        return None
    return _build_scoped_url(f"{WALMART_BASE_URL}/ip/{item}", source_url)


def build_review_url(item, page_number=1, source_url=None):
    if not item:
        return None
    page_number = max(1, int(page_number or 1))
    extra_params = [("page", str(page_number))] if page_number > 1 else []
    return _build_scoped_url(
        f"{WALMART_BASE_URL}/reviews/product/{item}",
        source_url,
        extra_params,
    )


def review_response_scope_error(next_data, item, source_url=None):
    query = (next_data or {}).get("query")
    if not isinstance(query, dict):
        return "review response query missing"

    response_item = collapse_ws(query.get("id"))
    if not response_item:
        return "review response item missing"
    if item and response_item != str(item):
        return f"review response item mismatch: expected={item}, actual={response_item}"

    for key, expected in product_scope_query_params(source_url).items():
        actual = collapse_ws(query.get(key))
        if actual != expected:
            return f"review response scope mismatch: {key} expected={expected}, actual={actual or '-'}"
    return None


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
    initial_data = (
        (next_data or {}).get("props", {}).get("pageProps", {}).get("initialData", {})
    )
    if not isinstance(initial_data, dict):
        return {}
    nested_data = initial_data.get("data")
    return nested_data if isinstance(nested_data, dict) and nested_data else initial_data


def read_zenrows_api_key(config_path=None):
    if config_path is None:
        config_path = Path(__file__).resolve().parents[1] / "config.py"
    text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"^\s*ZENROWS_API_KEY\s*=\s*[\"']?([^\"'\s#]+)", text, re.M)
    return match.group(1) if match else None


class WalmartNextDataClient:
    def __init__(
        self,
        timeout=30,
        zenrows_timeout=120,
        config_path=None,
        direct_enabled=True,
    ):
        self.timeout = timeout
        self.zenrows_timeout = zenrows_timeout
        self.config_path = config_path
        self.direct_enabled = bool(direct_enabled)
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

    def fetch_zenrows_json_response(self, url, wait_ms=6000):
        if not self.zenrows_api_key:
            self.zenrows_api_key = read_zenrows_api_key(self.config_path)
        started = time.time()
        meta = {
            "source": "zenrows_json",
            "url": url,
            "status": None,
            "final_url": None,
            "elapsed_sec": None,
            "html_len": 0,
            "blocked": False,
            "error": None,
            "xhr_count": 0,
        }
        if not self.zenrows_api_key:
            meta["error"] = "ZENROWS_API_KEY not found"
            return meta, None
        try:
            wait_ms = max(0, int(wait_ms or 0))
        except (TypeError, ValueError):
            wait_ms = 6000
        params = {
            "apikey": self.zenrows_api_key,
            "url": url,
            "premium_proxy": "true",
            "proxy_country": "us",
            "js_render": "true",
            "json_response": "true",
            "wait": str(wait_ms),
        }
        try:
            response = requests.get(ZENROWS_API_URL, params=params, timeout=max(self.zenrows_timeout, 180))
            response_text = response.text or ""
            meta.update({
                "status": response.status_code,
                "final_url": url,
                "elapsed_sec": round(time.time() - started, 2),
                "html_len": len(response_text),
                "blocked": is_blocked_html(response_text, response.url),
            })
            try:
                payload = response.json()
            except Exception as exc:
                meta["error"] = f"json parse {type(exc).__name__}: {exc}"
                return meta, None
            if isinstance(payload, dict):
                meta["xhr_count"] = len(payload.get("xhr") or [])
            return meta, payload
        except Exception as exc:
            meta.update({"elapsed_sec": round(time.time() - started, 2), "error": f"{type(exc).__name__}: {exc}"})
            return meta, None

    def fetch_similar_product_names(self, url, current_item=None, wait_ms=6000, limit=30):
        meta, payload = self.fetch_zenrows_json_response(url, wait_ms=wait_ms)
        names = parse_similar_product_names_from_json_response(
            payload,
            current_item=current_item,
            limit=limit,
        )
        if names:
            meta["similar_count"] = names.count(" ||| ") + 1
        else:
            meta["similar_count"] = 0
        return {"names": names, "source": meta.get("source"), "meta": meta}

    def fetch_next_data(
        self,
        url,
        direct_retries=1,
        use_zenrows=True,
        js_render_fallback=True,
    ):
        attempts = []
        if self.direct_enabled:
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


AVAILABILITY_BADGE_SPECS = {
    "pick_up_availability": {
        "mem_id": "L1051",
        "labels": ("Free pickup as soon as", "Free pickup"),
    },
    "fastest_delivery": {
        "mem_id": "L1053",
        "labels": ("Free shipping, arrives", "Free shipping"),
    },
    "delivery_availability": {
        "mem_id": "L1052",
        "labels": ("Delivery as soon as",),
    },
}

AVAILABILITY_PHRASE_FIELDS = {
    "free pickup": "pick_up_availability",
    "free shipping": "fastest_delivery",
    "delivery": "delivery_availability",
}


def _availability_field(field_or_phrase):
    key = collapse_ws(field_or_phrase)
    if not key:
        return None
    if key in AVAILABILITY_BADGE_SPECS:
        return key
    return AVAILABILITY_PHRASE_FIELDS.get(key.lower())


def _availability_value_from_text(value, field_name):
    field_name = _availability_field(field_name)
    spec = AVAILABILITY_BADGE_SPECS.get(field_name)
    text = collapse_ws(value)
    if not spec or not text:
        return None

    for label in spec["labels"]:
        match = re.fullmatch(
            rf"{re.escape(label)}(?:\s+|,\s*)(.+)",
            text,
            re.I,
        )
        if match:
            return normalize_availability_value(match.group(1), field_name)
    return None


def normalize_availability_value(value, field_name):
    """Keep only a field's value, never a fulfillment label or another field."""
    field_name = _availability_field(field_name)
    if field_name not in AVAILABILITY_BADGE_SPECS:
        return None

    text = collapse_ws(value)
    if not text:
        return None

    inline_value = _availability_value_from_text(text, field_name)
    if inline_value:
        return inline_value

    lower = text.lower().strip(" ,.-")
    all_labels = {
        label.lower()
        for spec in AVAILABILITY_BADGE_SPECS.values()
        for label in spec["labels"]
    }
    invalid_values = all_labels | {
        "arrives",
        "as soon as",
        "at a",
        "delivery",
        "nearby store",
        "pickup at a",
    }
    if (
        lower in invalid_values
        or lower.startswith("pickup at a")
        or lower.startswith("as soon as")
        or lower.startswith("arrives")
    ):
        return None
    if any(label in lower for label in all_labels):
        return None
    return text


def _delivery_value_from_text(value, phrase):
    return _availability_value_from_text(value, phrase)


def _availability_member_values(member):
    values = []
    for content in member.get("content") or []:
        if not isinstance(content, dict):
            continue
        value = collapse_ws(content.get("value") or content.get("text"))
        if value:
            values.append(value)
    return values


def _value_from_availability_member(member, field_name):
    spec = AVAILABILITY_BADGE_SPECS[field_name]
    values = _availability_member_values(member)
    for index, value in enumerate(values):
        if not any(label.lower() in value.lower() for label in spec["labels"]):
            continue
        inline_value = _availability_value_from_text(value, field_name)
        if inline_value:
            return inline_value
        if index + 1 < len(values):
            return normalize_availability_value(values[index + 1], field_name)
        return None
    return None


def _find_following_badge_value(item, phrase):
    field_name = _availability_field(phrase)
    spec = AVAILABILITY_BADGE_SPECS.get(field_name)
    if not spec:
        return None

    groups = (((item.get("badges") or {}).get("groupsV2")) or [])
    members = [
        member
        for group in groups
        if isinstance(group, dict)
        for member in (group.get("members") or [])
        if isinstance(member, dict)
    ]

    # Verified Walmart fulfillment members: pickup=L1051,
    # delivery=L1052, shipping=L1053.
    for member in members:
        if str(member.get("memId") or "") == spec["mem_id"]:
            return _value_from_availability_member(member, field_name)

    # Fallback stays inside one member carrying this field's own label.
    for member in members:
        values = _availability_member_values(member)
        if any(
            label.lower() in value.lower()
            for value in values
            for label in spec["labels"]
        ):
            return _value_from_availability_member(member, field_name)
    return None


def _included_service_offer_count(item):
    for module in item.get("addOnServices") or []:
        if not isinstance(module, dict):
            continue
        if module.get("serviceType") != "SERVICES":
            continue
        group_count = 0
        for group in module.get("groups") or []:
            if not isinstance(group, dict) or group.get("groupType") != "INCLUDED_SERVICES":
                continue
            services = group.get("services") or []
            if any(isinstance(service, dict) and service.get("offerId") for service in services):
                group_count += 1
        if group_count <= 0:
            continue

        service_title = collapse_ws(module.get("serviceTitle")) or ""
        declared_match = re.search(
            r"(?<!\d)(\d{1,2})\s+free\s+offers?\b",
            service_title,
            re.I,
        )
        if declared_match:
            declared_count = normalize_int(declared_match.group(1))
            if declared_count != group_count:
                continue
        return str(group_count)
    return None


def _extract_offer_count(item):
    if not isinstance(item, dict):
        return None

    included_service_count = _included_service_offer_count(item)
    if included_service_count:
        return included_service_count

    for text in _badge_texts(item):
        match = re.search(r"(\d+)\s+free\s+offers?", text, re.I)
        if match:
            return normalize_count_text(match.group(1))
    return None


def _offer_count_from_text(value):
    text = " ".join(str(value or "").split())
    if not text:
        return None
    match = re.search(r"(?<!\d)(\d{1,2})\s+free\s+offers?(?:\b|,)", text, re.I)
    if not match:
        return None
    count = normalize_int(match.group(1))
    if count is None or count <= 0 or count > 99:
        return None
    return str(count)


def _is_buy_box_offer_context(node):
    current = node
    for _ in range(8):
        if current is None:
            break
        tag = str(getattr(current, "tag", "") or "").lower()
        if tag in {"script", "style", "noscript"}:
            return False
        attr_text = " ".join(
            str(current.get(name) or "")
            for name in ("id", "class", "data-testid", "aria-label")
        ).lower()
        if "buy-box-inner-container" in attr_text or "buy box" in attr_text or "buybox" in attr_text:
            return True
        current = current.getparent()
    return False


def parse_offer_count_from_html(html_text):
    if not html_text:
        return None
    try:
        tree = lxml_html.fromstring(html_text)
    except Exception:
        return None
    for text_node in tree.xpath(
        "//text()[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'free offers')]"
    ):
        parent = text_node.getparent()
        if parent is None or not _is_buy_box_offer_context(parent):
            continue
        count = _offer_count_from_text(text_node)
        if count:
            return count
    return None


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


def _listing_object_item_id(item):
    if not isinstance(item, dict):
        return None
    for key in ("usItemId", "itemId", "id"):
        value = item.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text.isdigit():
            return text
    return None


def _is_listing_product(item):
    if not isinstance(item, dict):
        return False
    url = item.get("canonicalUrl") or item.get("productPageUrl")
    url_item = item_id_from_url(url or "")
    object_item = _listing_object_item_id(item)
    if object_item and url_item and object_item != url_item:
        return False
    return bool(item.get("name") and url_item)


def _canonical_listing_card_url(card):
    candidates = []
    for href in card.xpath(".//a[@href]/@href"):
        absolute_url = absolute_walmart_url(href)
        if not absolute_url:
            continue
        parsed = urlparse(absolute_url)
        if "/sp/track" in parsed.path:
            redirect_url = (parse_qs(parsed.query).get("rd") or [None])[0]
            canonical_url = absolute_walmart_url(redirect_url)
            is_tracking = True
        else:
            canonical_url = absolute_url
            is_tracking = False
        if item_id_from_url(canonical_url):
            candidates.append((is_tracking, canonical_url))
    if not candidates:
        return None
    candidates.sort(key=lambda row: row[0])
    return candidates[0][1]


def parse_listing_card_element(card):
    product_url = _canonical_listing_card_url(card)
    title_nodes = card.xpath(".//h3[@data-automation-id='product-title']")
    retailer_sku_name = collapse_ws(title_nodes[0].text_content()) if title_nodes else None
    offer = None
    for text_node in card.xpath(
        ".//text()[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'free offer')]"
    ):
        parent = text_node.getparent()
        if parent is None or str(parent.tag).lower() in {"script", "style", "noscript"}:
            continue
        offer = _offer_count_from_text(text_node)
        if offer:
            break
    badge_texts = _unique_texts(
        collapse_ws(node.text_content())
        for node in card.xpath(
            ".//span[@data-testid='badgeTagComponent'] | .//div[contains(@class, 'ff-text-wrapper')]"
        )
    )

    def delivery_value(field_name):
        spec = AVAILABILITY_BADGE_SPECS[field_name]
        for label in spec["labels"]:
            text_nodes = card.xpath(
                ".//text()[contains("
                "translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                f"'{label.lower()}')]"
            )
            for text_node in text_nodes:
                current = text_node.getparent()
                for _ in range(3):
                    if current is None:
                        break
                    container_text = collapse_ws(
                        " ".join(current.xpath(".//text()"))
                    )
                    lower = (container_text or "").lower()
                    present_fields = {
                        candidate_field
                        for candidate_field, candidate_spec in AVAILABILITY_BADGE_SPECS.items()
                        if any(
                            candidate_label.lower() in lower
                            for candidate_label in candidate_spec["labels"]
                        )
                    }
                    if present_fields == {field_name}:
                        parsed = normalize_availability_value(
                            container_text,
                            field_name,
                        )
                        if parsed:
                            return parsed
                    current = current.getparent()
        return None

    final_sku_price = None
    original_sku_price = None
    for price_node in card.xpath(".//*[@data-automation-id='product-price']"):
        price_text = collapse_ws(price_node.text_content())
        current_match = re.search(
            r"current price\s+(?:Now\s+)?(\$\s*[\d,]+(?:\.\d{1,2})?)",
            price_text or "",
            re.I,
        )
        was_match = re.search(
            r"\bWas\s+(\$\s*[\d,]+(?:\.\d{1,2})?)",
            price_text or "",
            re.I,
        )
        if current_match:
            final_sku_price = format_money(current_match.group(1))
        if was_match:
            original_sku_price = format_money(was_match.group(1))
        if final_sku_price:
            break

    sku_status_parts = []
    if card.xpath(".//*[normalize-space(.)='Sponsored']"):
        sku_status_parts.append("Sponsored")
    if card.xpath(".//*[normalize-space(.)='Rollback']"):
        sku_status_parts.append("Rollback")

    available_match = None
    for text in badge_texts:
        available_match = re.search(r"\bonly\s+([\d,]+)\s+left\b", text, re.I)
        if available_match:
            break
    available_quantity = normalize_count_text(available_match.group(1)) if available_match else None
    inventory_status = next(
        (text for text in badge_texts if text.lower() == "low stock"),
        None,
    )
    return {
        "retailer_sku_name": retailer_sku_name,
        "offer": offer,
        "final_sku_price": final_sku_price,
        "original_sku_price": original_sku_price,
        "pick_up_availability": delivery_value("pick_up_availability"),
        "fastest_delivery": delivery_value("fastest_delivery"),
        "delivery_availability": delivery_value("delivery_availability"),
        "sku_status": ", ".join(_unique_texts(sku_status_parts)) if sku_status_parts else None,
        "available_quantity_for_purchase": available_quantity,
        "inventory_status": inventory_status,
        "product_url": product_url,
    }


def _listing_items_from_next_data(next_data):
    """Return products only from Walmart's primary search-results stack."""
    initial_data = get_initial_data(next_data)
    stacks = (initial_data.get("searchResult") or {}).get("itemStacks") or []
    for stack in stacks:
        if not isinstance(stack, dict):
            continue
        title = collapse_ws(stack.get("title")) or ""
        if not title.lower().startswith("results for "):
            continue
        items = stack.get("items")
        if not isinstance(items, list):
            return []
        return [item for item in items if _is_listing_product(item)]
    return []


def parse_listing_products(next_data, *, account_name, page_type, page_number, calendar_week, batch_id):
    products = []
    for item in _listing_items_from_next_data(next_data):
        product_url = absolute_walmart_url(item.get("canonicalUrl") or item.get("productPageUrl"))
        final_sku_price, original_sku_price = _listing_price_values(
            item.get("priceInfo")
        )
        products.append({
            "account_name": account_name,
            "page_type": page_type,
            "retailer_sku_name": collapse_ws(item.get("name")),
            "offer": _extract_offer_count(item),
            "final_sku_price": final_sku_price,
            "original_sku_price": original_sku_price,
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


def _listing_price_values(price_info):
    if not isinstance(price_info, dict):
        return None, None

    final_sku_price = None
    for key in ("linePrice", "linePriceDisplay"):
        final_sku_price = format_money(price_info.get(key))
        if final_sku_price:
            break
    if not final_sku_price:
        final_sku_price = _price_string(price_info)
    return final_sku_price, _was_price_string(price_info)


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
        return {
            "reviews": [],
            "total_review_count": None,
            "star_rating": None,
            "count_of_star_ratings": None,
        }
    return {
        "reviews": _review_texts_from_reviews_node(reviews_node, limit=limit),
        "total_review_count": parse_text_review_count(reviews_node),
        "star_rating": format_star_rating(
            reviews_node.get("roundedAverageOverallRating")
            or reviews_node.get("averageOverallRating")
        ),
        "count_of_star_ratings": parse_star_rating_count(reviews_node),
    }


def format_reviews(review_texts, limit=20):
    rows = []
    for text in review_texts:
        cleaned = collapse_ws(text)
        if not cleaned:
            continue
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
    rating_keys = (
        "totalReviewCount",
        "ratingCount",
        "ratingsCount",
        "totalRatings",
    )
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in rating_keys:
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
    if not html_text:
        return None
    try:
        tree = lxml_html.fromstring(html_text)
    except Exception:
        return None
    values = tree.xpath(
        "//div[@data-testid='ip-legal-policy-component' "
        "and not(ancestor::div[contains(@class, 'sticky-buy-box-column')])]//span/text()"
    )
    for value in values:
        if collapse_ws(value) == "Price when purchased online":
            return "Price when purchased online"
    return None


SIMILAR_HTML_XPATHS = (
    "//*[@id='ip-carousel-Similar items you might like']//h3[@data-automation-id='product-title']/text()",
    "//*[@id='ip-carousel-Similar items you might like']//span[@data-automation-id='product-title']/text()",
    "//div[starts-with(@id, 'ip-carousel-') and contains(translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'similar items')]//h3[@data-automation-id='product-title']/text()",
    "//div[starts-with(@id, 'ip-carousel-') and contains(translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'similar items')]//span[@data-automation-id='product-title']/text()",
)
SIMILAR_MODULE_TITLES = {
    "see similar items",
    "similar items you might like",
    "compare with similar items",
}
SIMILAR_IMAGE_NAME_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)(?:$|\?)", re.I)
SIMILAR_UUID_IMAGE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"(?:\.[0-9a-f]+)?\.(?:jpe?g|png|webp|gif|avif)$",
    re.I,
)
SIMILAR_MOJIBAKE_REPLACEMENTS = (
    ("\u00e2\u20ac\u009d", "\u201d"),
)


def repair_similar_text_encoding(value):
    if value is None:
        return None
    text = str(value)
    for polluted, repaired in SIMILAR_MOJIBAKE_REPLACEMENTS:
        text = text.replace(polluted, repaired)
    return text


def _similar_product_item_id(node):
    url = node.get("canonicalUrl") or node.get("productPageUrl") or node.get("productUrl") or ""
    item = node.get("usItemId") or node.get("itemId") or item_id_from_url(url)
    node_id = node.get("id")
    if not item and str(node_id or "").isdigit():
        item = node_id
    item = str(item).strip() if item is not None else None
    return item if item and item.isdigit() else None


def _similar_product_name(node):
    name = collapse_ws(repair_similar_text_encoding(
        node.get("name") or node.get("title") or node.get("productName")
    ))
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


def _similar_data_root(payload):
    data = get_initial_data(payload)
    if not isinstance(data, dict) or not (data.get("contentLayout") or {}).get("modules"):
        data = ((payload or {}).get("data") or {}) if isinstance(payload, dict) else {}
    return data if isinstance(data, dict) else {}


def _iter_similar_next_data_modules(next_data):
    data = _similar_data_root(next_data)
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
        name = collapse_ws(repair_similar_text_encoding(name))
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


def parse_similar_product_names_from_json_response(response_json, current_item=None, limit=30):
    if not isinstance(response_json, dict):
        return None

    for request in response_json.get("xhr") or []:
        if not isinstance(request, dict):
            continue
        request_url = request.get("url") or ""
        body = request.get("body") or ""
        if (
            "ItemByIdBtf" not in request_url
            and "orchestra/pdp/graphql" not in request_url
            and "Similar items" not in body
        ):
            continue
        try:
            payload = json.loads(body)
        except Exception:
            continue
        result = parse_similar_product_names(
            payload,
            current_item=current_item,
            limit=limit,
        )
        if result:
            return result

    return parse_similar_product_names_from_html(response_json.get("html") or "", limit=limit)


def _idml_spec_value(initial_data, display_name):
    idml = initial_data.get("idml") if isinstance(initial_data, dict) else None
    if not isinstance(idml, dict):
        return None
    target = str(display_name or "").strip().lower()
    for row in idml.get("specifications") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("name") or "").strip().lower() == target:
            value = collapse_ws(row.get("value"))
            if value:
                return value
    for group in idml.get("specificationsV2") or []:
        if not isinstance(group, dict):
            continue
        for row in group.get("specificationGroup") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("displayName") or "").strip().lower() != target:
                continue
            values = row.get("attributeValue") or []
            if isinstance(values, list) and values:
                return collapse_ws(values[0])
    return None


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
    visible_star_rating, visible_rating_count = parse_visible_rating_summary_from_html(html_text)
    count_of_reviews = parse_text_review_count(reviews_node, product)
    count_of_star_ratings = visible_rating_count or parse_star_rating_count(reviews_node)
    star_rating = (
        visible_star_rating
        or format_star_rating(product.get("averageRating") or reviews_node.get("roundedAverageOverallRating") or reviews_node.get("averageOverallRating"))
        or "No ratings yet"
    )
    if count_of_reviews is None and count_of_star_ratings is None:
        count_of_reviews = "0"
        count_of_star_ratings = "0"
    return {
        "item": str(item) if item else None,
        "retailer_sku_name": collapse_ws(product.get("name")),
        "offer": parse_offer_count_from_html(html_text),
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
        "sku": _idml_spec_value(initial_data, "Model"),
        "screen_size": _idml_spec_value(initial_data, "Screen size"),
        "retailer_sku_name_similar": retailer_sku_name_similar,
    }
