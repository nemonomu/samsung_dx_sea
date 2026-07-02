import csv
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from zenrows import ZenRowsClient

from .step00_apollo import iter_apollo_push_payloads
from .step00_browser_session import (
    add_intl_nosplash,
    browser_fetch_graphql,
    browser_outer_html,
    close_browser_page,
    create_browser_page,
    env_bool,
    env_int,
)
from .step00_config import (
    DEFAULT_BESTBUY_RUN_ROOT,
    PROMOTION_LABELS,
    bestbuy_category,
    load_initial_urls,
    rel_path,
    target_url,
)
from .step00_parse_search import (
    delivery_availability_text,
    fastest_delivery_text,
    first_nested,
    listing_offer_count,
    money_text,
    pickup_availability_text,
    price_value,
    savings_money_text,
)

RUN_DATE = os.getenv("BESTBUY_RUN_DATE", datetime.now().strftime("%Y%m%d"))
RUN_ROOT = Path(os.getenv("BESTBUY_PROMOTION_RUN_ROOT", DEFAULT_BESTBUY_RUN_ROOT / "promotion"))
REQUEST_TIMEOUT = int(os.getenv("ZENROWS_TIMEOUT", "180"))
FETCH_MODE = os.getenv("BESTBUY_PROMOTION_FETCH_MODE", "browser_dom").strip().lower()
BROWSER_WAIT_SECONDS = max(0, int(os.getenv("BESTBUY_PROMOTION_BROWSER_WAIT_SECONDS", "8")))
BROWSER_JS_TIMEOUT = max(1, int(os.getenv("BESTBUY_PROMOTION_BROWSER_JS_TIMEOUT", "120")))
BROWSER_HEADLESS = env_bool("BESTBUY_PROMOTION_BROWSER_HEADLESS", "1")
BROWSER_LOCAL_PORT = env_int("BESTBUY_PROMOTION_BROWSER_LOCAL_PORT", "0")
PROMOTION_MAX_ATTEMPTS = max(1, int(os.getenv("BESTBUY_PROMOTION_MAX_ATTEMPTS", "5")))
PROMOTION_EXPECTED_MIN_ROWS = max(0, int(os.getenv("BESTBUY_PROMOTION_EXPECTED_MIN_ROWS", "18")))
PROMOTION_RETRY_SLEEP_SECONDS = float(os.getenv("BESTBUY_PROMOTION_RETRY_SLEEP_SECONDS", "2"))
PROMOTION_RETRY_STATUS_CODES = {
    int(value)
    for value in os.getenv(
        "BESTBUY_PROMOTION_RETRY_STATUS_CODES", "408,409,422,425,429,500,502,503,504"
    )
    .replace(",", " ")
    .split()
    if value.strip().isdigit()
}
PROMOTION_DOM_TYPE = os.getenv("BESTBUY_PROMOTION_DOM_TYPE", "DON'T-MISS DEALS ON TVs")
PROMOTION_DOM_HEADLINE = os.getenv("BESTBUY_PROMOTION_DOM_HEADLINE", "Don't-miss deals on TVs")
PROMOTION_DOM_SUBHEADLINE = os.getenv("BESTBUY_PROMOTION_DOM_SUBHEADLINE", "Big savings for a limited time")
PROMOTION_DOM_SELECTOR = os.getenv("BESTBUY_PROMOTION_DOM_SELECTOR", ".pl-flex-carousel")
ENDPOINT = os.getenv("BESTBUY_GRAPHQL_ENDPOINT", "https://www.bestbuy.com/gateway/graphql")
PLACEMENT = os.getenv("BESTBUY_PROMOTION_PLACEMENT", "all")
REFERER = os.getenv("BESTBUY_PROMOTION_REFERER", load_initial_urls().get("promotion_tv_home_theater", ""))
QUERY_TEMPLATE_HTML = Path(
    os.getenv("BESTBUY_PROMOTION_QUERY_TEMPLATE_HTML", "references/bestbuy_promotion_page_sample.html")
)
EXCLUDED_PROMOTION_TYPES = {
    value.strip().lower()
    for value in os.getenv("BESTBUY_PROMOTION_EXCLUDE_TYPES", "Featured deals").split("|")
    if value.strip()
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def find_started_operation_for_placement(html_text, placement):
    for payload in iter_apollo_push_payloads(html_text):
        for event in payload.get("events", []):
            if event.get("type") != "started":
                continue
            options = event.get("options") or {}
            variables = options.get("variables") or {}
            if variables.get("placement") == placement:
                query = options.get("query") or ""
                operation_name = query.split("{", 1)[0].replace("query", "", 1).strip().split("(", 1)[0]
                return {
                    "operationName": operation_name,
                    "variables": variables,
                    "query": query,
                }
    raise RuntimeError(f"Could not find operation for placement={placement}")


def promotion_type_for_placement(placement):
    return PROMOTION_LABELS.get(placement, placement)


def promotion_placement_excluded(placement):
    return promotion_type_for_placement(placement).strip().lower() in EXCLUDED_PROMOTION_TYPES


def extract_rows_from_response(response_json, placement):
    if promotion_placement_excluded(placement):
        return []
    promotion_type = promotion_type_for_placement(placement)
    rows = []
    deals = (((response_json.get("data") or {}).get("customer") or {}).get("deals") or {})
    for position, item in enumerate(deals.get("items") or [], 1):
        product = item.get("product") or item.get("featuredProduct") or {}
        sku_id = product.get("skuId")
        if not sku_id:
            continue
        name = product.get("name") or {}
        if isinstance(name, dict):
            name = name.get("short") or name.get("title") or ""
        url = product.get("url") or {}
        relative_url = url.get("relativePdp") if isinstance(url, dict) else ""
        price = product.get("price") if isinstance(product.get("price"), dict) else {}
        shipping = first_nested(product, ["fulfillmentOptions", "shippingDetails", "shippingAvailability"], {})
        delivery = first_nested(product, ["fulfillmentOptions", "deliveryDetails", "deliveryAvailability"], {})
        pickup = first_nested(product, ["fulfillmentOptions", "ispuDetails", "ispuAvailability"], {})
        customer_price = price_value(price, "displayableCustomerPrice", "customerPrice")
        regular_price = price_value(price, "displayableRegularPrice", "regularPrice")
        total_savings = price_value(price, "totalSavings")
        offer_count = listing_offer_count(product)
        rows.append(
            {
                "promotion_type": promotion_type,
                "promotion_placement": placement,
                "promotion_position": position,
                "sku_id": sku_id,
                "retailer_sku_name": name,
                "product_url": f"https://www.bestbuy.com{relative_url}" if relative_url else "",
                "customer_price": customer_price,
                "regular_price": regular_price,
                "total_savings": total_savings,
                "final_sku_price": money_text(customer_price),
                "original_sku_price": money_text(regular_price),
                "savings": savings_money_text(customer_price, regular_price, total_savings),
                "offer": offer_count,
                "offer_count": offer_count,
                "pick_up_availability": pickup_availability_text(pickup),
                "fastest_delivery": fastest_delivery_text(shipping, delivery),
                "delivery_availability": delivery_availability_text(delivery),
            }
        )
    return rows


def safe_part(value):
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value or "").strip()).strip("_") or "na"


def placement_folder(placement, status=None):
    raw_root = RUN_ROOT / "raw"
    placement_part = safe_part(placement)
    if status:
        folder = raw_root / f"{placement_part}_{status}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    for suffix in ("success", "fail"):
        folder = raw_root / f"{placement_part}_{suffix}"
        if folder.exists():
            return folder
    return raw_root


def placement_artifact_paths(placement, status=None):
    folder = placement_folder(placement, status)
    placement_part = safe_part(placement)
    return {
        "folder": folder,
        "request": folder / f"{placement_part}_request.json",
        "response": folder / f"{placement_part}_response.txt",
        "headers": folder / f"{placement_part}_headers.json",
        "json": folder / f"{placement_part}_response.json",
    }


def attempt_status(status, attempt):
    return f"{status}_attempt_{attempt:02d}"


def cost_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def retryable_summary(summary):
    try:
        status_code = int(summary.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code in PROMOTION_RETRY_STATUS_CODES:
        return True
    return status_code == 200 and int(summary.get("row_count") or 0) == 0


def sleep_before_retry(attempt):
    if attempt < PROMOTION_MAX_ATTEMPTS and PROMOTION_RETRY_SLEEP_SECONDS > 0:
        time.sleep(PROMOTION_RETRY_SLEEP_SECONDS)


def run_one(client, html_text, placement, attempt=1):
    payload = find_started_operation_for_placement(html_text, placement)

    start = time.perf_counter()
    response = client.post(
        ENDPOINT,
        params={
            "custom_headers": "true",
            "premium_proxy": "true",
            "proxy_country": "us",
            "js_render": "true",
        },
        headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://www.bestbuy.com",
            "referer": REFERER,
        },
        data=json.dumps(payload),
        timeout=REQUEST_TIMEOUT,
    )
    elapsed = round(time.perf_counter() - start, 3)
    text = response.text
    status = "success" if response.status_code == 200 else "fail"
    paths = placement_artifact_paths(placement, attempt_status(status, attempt))
    paths["request"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["response"].write_text(text, encoding="utf-8", errors="replace")
    paths["headers"].write_text(json.dumps(dict(response.headers), indent=2, ensure_ascii=False), encoding="utf-8")

    response_json = {}
    parse_error = ""
    try:
        response_json = response.json()
        paths["json"].write_text(
            json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except ValueError as exc:
        parse_error = str(exc)

    rows = extract_rows_from_response(response_json, placement)
    return {
        "summary": {
            "started_at": now(),
            "placement": placement,
            "promotion_type": promotion_type_for_placement(placement),
            "attempt": attempt,
            "status_code": response.status_code,
            "elapsed_seconds": elapsed,
            "x_request_cost": response.headers.get("x-request-cost", ""),
            "bytes": len(text or ""),
            "parse_error": parse_error,
            "row_count": len(rows),
            "artifact_folder": rel_path(paths["folder"]),
            "response_json_path": rel_path(paths["json"]) if response_json else "",
        },
        "rows": rows,
    }


def run_batch(client, html_text, placements, attempt=1):
    payloads = [find_started_operation_for_placement(html_text, placement) for placement in placements]

    start = time.perf_counter()
    response = client.post(
        ENDPOINT,
        params={
            "custom_headers": "true",
            "premium_proxy": "true",
            "proxy_country": "us",
            "js_render": "true",
        },
        headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://www.bestbuy.com",
            "referer": REFERER,
        },
        data=json.dumps(payloads),
        timeout=REQUEST_TIMEOUT,
    )
    elapsed = round(time.perf_counter() - start, 3)
    text = response.text
    status = "success" if response.status_code == 200 else "fail"
    paths = placement_artifact_paths("all_batch", attempt_status(status, attempt))
    paths["request"].write_text(json.dumps(payloads, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["response"].write_text(text, encoding="utf-8", errors="replace")
    paths["headers"].write_text(json.dumps(dict(response.headers), indent=2, ensure_ascii=False), encoding="utf-8")

    parse_error = ""
    response_json = None
    try:
        response_json = response.json()
        paths["json"].write_text(
            json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except ValueError as exc:
        parse_error = str(exc)

    response_items = response_json if isinstance(response_json, list) else []
    all_rows = []
    summaries = []
    for index, placement in enumerate(placements):
        item_json = response_items[index] if index < len(response_items) and isinstance(response_items[index], dict) else {}
        rows = extract_rows_from_response(item_json, placement)
        all_rows.extend(rows)
        summaries.append(
            {
                "started_at": now(),
                "placement": placement,
                "promotion_type": promotion_type_for_placement(placement),
                "attempt": attempt,
                "status_code": response.status_code,
                "elapsed_seconds": elapsed,
                "x_request_cost": response.headers.get("x-request-cost", ""),
                "bytes": len(text or ""),
                "parse_error": parse_error,
                "row_count": len(rows),
                "artifact_folder": rel_path(paths["folder"]),
                "response_json_path": rel_path(paths["json"]) if response_json is not None else "",
                "batch_index": index,
            }
        )

    return {"summaries": summaries, "rows": all_rows}


def save_browser_graphql_artifacts(placement, attempt, payload, envelope, elapsed):
    status_code = int(envelope.get("status") or 0)
    text = str(envelope.get("body") or "")
    status = "success" if status_code == 200 else "fail"
    paths = placement_artifact_paths(placement, attempt_status(status, attempt))
    paths["request"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["response"].write_text(text, encoding="utf-8", errors="replace")
    paths["headers"].write_text(
        json.dumps(
            {
                "content-type": envelope.get("contentType", ""),
                "transport": "browser_graphql",
                "elapsed_seconds": elapsed,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    response_json = {}
    parse_error = ""
    try:
        response_json = json.loads(text)
        paths["json"].write_text(
            json.dumps(response_json, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except ValueError as exc:
        parse_error = str(exc)
    return paths, response_json, parse_error, text, status_code


def run_one_browser(page, html_text, placement, attempt=1):
    payload = find_started_operation_for_placement(html_text, placement)
    start = time.perf_counter()
    envelope = browser_fetch_graphql(page, payload, timeout=BROWSER_JS_TIMEOUT)
    elapsed = round(time.perf_counter() - start, 3)
    paths, response_json, parse_error, text, status_code = save_browser_graphql_artifacts(
        placement,
        attempt,
        payload,
        envelope,
        elapsed,
    )
    rows = extract_rows_from_response(response_json, placement)
    return {
        "summary": {
            "started_at": now(),
            "placement": placement,
            "promotion_type": promotion_type_for_placement(placement),
            "attempt": attempt,
            "status_code": status_code,
            "elapsed_seconds": elapsed,
            "x_request_cost": "0",
            "bytes": len(text or ""),
            "parse_error": parse_error,
            "row_count": len(rows),
            "artifact_folder": rel_path(paths["folder"]),
            "response_json_path": rel_path(paths["json"]) if response_json else "",
            "transport": "browser_graphql",
        },
        "rows": rows,
    }


def run_batch_browser(page, html_text, placements, attempt=1):
    payloads = [find_started_operation_for_placement(html_text, placement) for placement in placements]
    start = time.perf_counter()
    envelope = browser_fetch_graphql(page, payloads, timeout=BROWSER_JS_TIMEOUT)
    elapsed = round(time.perf_counter() - start, 3)
    paths, response_json, parse_error, text, status_code = save_browser_graphql_artifacts(
        "all_batch",
        attempt,
        payloads,
        envelope,
        elapsed,
    )
    response_items = response_json if isinstance(response_json, list) else []
    all_rows = []
    summaries = []
    for index, placement in enumerate(placements):
        item_json = response_items[index] if index < len(response_items) and isinstance(response_items[index], dict) else {}
        rows = extract_rows_from_response(item_json, placement)
        all_rows.extend(rows)
        summaries.append(
            {
                "started_at": now(),
                "placement": placement,
                "promotion_type": promotion_type_for_placement(placement),
                "attempt": attempt,
                "status_code": status_code,
                "elapsed_seconds": elapsed,
                "x_request_cost": "0",
                "bytes": len(text or ""),
                "parse_error": parse_error,
                "row_count": len(rows),
                "artifact_folder": rel_path(paths["folder"]),
                "response_json_path": rel_path(paths["json"]) if response_json is not None else "",
                "batch_index": index,
                "transport": "browser_graphql",
            }
        )

    return {"summaries": summaries, "rows": all_rows}


def batch_attempt_summary(result, attempt):
    summaries = result.get("summaries") or []
    first = summaries[0] if summaries else {}
    return {
        "mode": "batch",
        "attempt": attempt,
        "placements": [summary.get("placement") for summary in summaries],
        "status_code": first.get("status_code", ""),
        "x_request_cost": first.get("x_request_cost", ""),
        "bytes": first.get("bytes", ""),
        "row_count": len(result.get("rows") or []),
        "artifact_folder": first.get("artifact_folder", ""),
        "retryable": any(retryable_summary(summary) for summary in summaries),
    }


def single_attempt_summary(result, attempt):
    summary = result.get("summary") or {}
    return {
        "mode": "single",
        "attempt": attempt,
        "placement": summary.get("placement", ""),
        "status_code": summary.get("status_code", ""),
        "x_request_cost": summary.get("x_request_cost", ""),
        "bytes": summary.get("bytes", ""),
        "row_count": len(result.get("rows") or []),
        "artifact_folder": summary.get("artifact_folder", ""),
        "retryable": retryable_summary(summary),
    }


def dedupe_promotion_rows(rows):
    seen = set()
    deduped = []
    for row in rows:
        key = (row.get("promotion_placement") or "", row.get("sku_id") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def sku_from_product_url(url):
    text = str(url or "")
    for pattern in (r"/sku/(\d+)", r"[?&]skuId=(\d+)"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def bsin_from_product_url(url):
    try:
        path = urlsplit(str(url or "")).path
    except ValueError:
        path = str(url or "")
    match = re.search(r"/product/[^/]+/([^/]+)/sku/\d+", path)
    return match.group(1) if match else ""


def compact_spaces(value):
    return " ".join(str(value or "").split())


def money_number(value):
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value or ""))
    if not match:
        return ""
    return match.group(0).replace(",", "")


def price_after_phrase(text, phrases):
    for phrase in phrases:
        index = text.lower().find(phrase.lower())
        if index < 0:
            continue
        match = re.search(r"\$\s*\d[\d,]*(?:\.\d+)?", text[index:])
        if match:
            return money_number(match.group(0))
    return ""


def parse_dom_prices(text):
    text = compact_spaces(text)
    all_prices = [money_number(match.group(0)) for match in re.finditer(r"\$\s*\d[\d,]*(?:\.\d+)?", text)]
    all_prices = [value for value in all_prices if value]
    customer_price = price_after_phrase(text, ["Tech Fest Deal", "Deal"])
    if not customer_price and all_prices:
        customer_price = all_prices[0]

    regular_price = price_after_phrase(text, ["The price was", "Was", "Comp. Value", "Comparable value"])
    if not regular_price and customer_price:
        try:
            final_value = float(customer_price)
        except ValueError:
            final_value = None
        if final_value is not None:
            for price in reversed(all_prices):
                try:
                    candidate = float(price)
                except ValueError:
                    continue
                if candidate > final_value:
                    regular_price = price
                    break

    total_savings = ""
    try:
        if customer_price and regular_price:
            diff = float(regular_price) - float(customer_price)
            if diff > 0:
                total_savings = f"{diff:.2f}"
    except ValueError:
        total_savings = ""
    return customer_price, regular_price, total_savings


def clean_dom_name(link_text, card_text, image_alt):
    candidates = [link_text, image_alt, card_text]
    for candidate in candidates:
        text = compact_spaces(candidate)
        if not text:
            continue
        text = re.sub(r"^\d+%\s*off\s+", "", text, flags=re.IGNORECASE)
        text = re.split(r"\bTech Fest Deal\b|\$\s*\d", text, maxsplit=1, flags=re.IGNORECASE)[0]
        text = compact_spaces(text)
        if len(text) >= 12 and "add to cart" not in text.lower():
            return text
    return ""


def parse_dom_items(raw_items):
    grouped = {}
    for item in raw_items or []:
        href = str(item.get("href") or "").split("#", 1)[0]
        sku_id = sku_from_product_url(href)
        if not sku_id:
            continue
        entry = grouped.setdefault(
            sku_id,
            {
                "sku_id": sku_id,
                "bsin": bsin_from_product_url(href),
                "product_url": href,
                "link_text": "",
                "image_alt": "",
                "card_text": "",
            },
        )
        link_text = compact_spaces(item.get("linkText"))
        image_alt = compact_spaces(item.get("imageAlt"))
        card_text = compact_spaces(item.get("cardText"))
        if len(link_text) > len(entry["link_text"]):
            entry["link_text"] = link_text
        if len(image_alt) > len(entry["image_alt"]):
            entry["image_alt"] = image_alt
        if len(card_text) > len(entry["card_text"]):
            entry["card_text"] = card_text
        if not entry["bsin"]:
            entry["bsin"] = bsin_from_product_url(href)

    rows = []
    for position, entry in enumerate(grouped.values(), 1):
        customer_price, regular_price, total_savings = parse_dom_prices(entry["card_text"])
        rows.append(
            {
                "promotion_type": PROMOTION_DOM_TYPE,
                "promotion_placement": "browser_dom_carousel",
                "promotion_position": position,
                "sku_id": entry["sku_id"],
                "retailer_sku_name": clean_dom_name(entry["link_text"], entry["card_text"], entry["image_alt"]),
                "product_url": entry["product_url"],
                "customer_price": customer_price,
                "regular_price": regular_price,
                "total_savings": total_savings,
                "final_sku_price": money_text(customer_price),
                "original_sku_price": money_text(regular_price),
                "savings": savings_money_text(customer_price, regular_price, total_savings),
                "offer": "",
                "offer_count": "",
                "pick_up_availability": "",
                "fastest_delivery": "",
                "delivery_availability": "",
            }
        )
    return rows


def extract_browser_dom_items(page):
    js = r"""
const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
const norm = value => clean(value).replace(/[\u2018\u2019\u0060]/g, "'").toLowerCase();
const absolute = href => {
  try { return new URL(href || '', location.href).href.split('#')[0]; }
  catch (e) { return href || ''; }
};
const productLinkSelector = 'a[href*="/product/"][href*="/sku/"]';
const targetHeadline = norm('%PROMOTION_DOM_HEADLINE%');
const targetSubheadline = norm('%PROMOTION_DOM_SUBHEADLINE%');
const carouselSelectors = new Set([
  '%PROMOTION_DOM_SELECTOR%',
  '.pl-flex-carousel',
  '.pl-flex-carousel-slider',
  '.pl-flex-carousel-container'
]);

const carouselContainers = Array.from(carouselSelectors)
  .flatMap(selector => Array.from(document.querySelectorAll(selector)));
const uniqueContainers = Array.from(new Set(carouselContainers));
const scoreContainer = el => {
  const text = clean(el.innerText);
  const links = Array.from(el.querySelectorAll(productLinkSelector));
  const className = String(el.className || '');
  const dealHits = (text.match(/Tech Fest Deal/g) || []).length;
  let score = links.length + dealHits * 10;
  if (className.includes('pl-flex-carousel-slider')) score += 30;
  if (className.includes('pl-flex-carousel')) score += 20;
  return {el, text, links, score};
};

const allElements = Array.from(document.querySelectorAll('body *'));
const headingMatches = allElements
  .map((el, index) => ({el, index, text: norm(el.innerText)}))
  .filter(item =>
    item.text.includes(targetHeadline)
    && item.text.includes(targetSubheadline)
    && item.text.length <= 300
    && item.el.querySelectorAll(productLinkSelector).length === 0
  );

const sectionCandidates = [];
for (const heading of headingMatches) {
  for (const item of uniqueContainers.map(scoreContainer)) {
    const index = allElements.indexOf(item.el);
    if (index > heading.index && item.links.length >= 4) {
      sectionCandidates.push({...item, headingIndex: heading.index, containerIndex: index});
    }
  }
}
const chosen = sectionCandidates
  .sort((a, b) => a.headingIndex - b.headingIndex || a.containerIndex - b.containerIndex || b.links.length - a.links.length)[0];

// Legacy behavior scored every carousel on the page and picked the strongest one.
// It is intentionally disabled because the current TV deals page has multiple
// carousel-like sections; promotion rows must come from the target headline block.
if (!chosen) {
  return JSON.stringify({
    containerFound: false,
    items: [],
    containerText: '',
    targetHeadline: '%PROMOTION_DOM_HEADLINE%',
    targetSubheadline: '%PROMOTION_DOM_SUBHEADLINE%',
    headingMatches: headingMatches.length
  });
}
const items = [];
for (const link of chosen.links) {
  let card = link;
  for (let i = 0; i < 8 && card && card.parentElement; i++) {
    const parent = card.parentElement;
    const parentText = clean(parent.innerText);
    const parentLinks = parent.querySelectorAll(productLinkSelector).length;
    if (parentText && (parentText.includes('Tech Fest Deal') || parentText.includes('$')) && parentLinks <= 4) {
      card = parent;
      break;
    }
    card = parent;
  }
  const img = card ? card.querySelector('img[alt]') : null;
  items.push({
    href: absolute(link.getAttribute('href') || link.href),
    linkText: clean(link.innerText),
    imageAlt: clean(img ? img.getAttribute('alt') : ''),
    cardText: clean(card ? card.innerText : link.innerText)
  });
}
return JSON.stringify({
  containerFound: true,
  containerClass: String(chosen.el.className || ''),
  linkCount: chosen.links.length,
  itemCount: items.length,
  headingMatches: headingMatches.length,
  headingIndex: chosen.headingIndex,
  containerIndex: chosen.containerIndex,
  containerText: chosen.text.slice(0, 2000),
  items
});
"""
    js = (
        js.replace("%PROMOTION_DOM_SELECTOR%", PROMOTION_DOM_SELECTOR.replace("\\", "\\\\").replace("'", "\\'"))
        .replace("%PROMOTION_DOM_HEADLINE%", PROMOTION_DOM_HEADLINE.replace("\\", "\\\\").replace("'", "\\'"))
        .replace("%PROMOTION_DOM_SUBHEADLINE%", PROMOTION_DOM_SUBHEADLINE.replace("\\", "\\\\").replace("'", "\\'"))
    )
    raw = page.run_js(js, timeout=BROWSER_JS_TIMEOUT)
    if raw is None:
        return {"containerFound": False, "items": []}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"Promotion browser DOM extraction returned non-JSON: {exc}") from exc


def run_browser_dom():
    if not REFERER:
        raise RuntimeError("Set BESTBUY_PROMOTION_REFERER or target_urls.promotion before browser DOM collection")

    raw_dir = RUN_ROOT / "raw" / "browser_dom"
    raw_dir.mkdir(parents=True, exist_ok=True)
    page = None
    browser_meta = {}
    start = time.perf_counter()
    try:
        page, browser_meta = create_browser_page(
            run_root=RUN_ROOT,
            name="promotion_dom",
            headless=BROWSER_HEADLESS,
            local_port=BROWSER_LOCAL_PORT,
        )
        browser_url = add_intl_nosplash(REFERER)
        page.get(browser_url)
        if BROWSER_WAIT_SECONDS:
            time.sleep(BROWSER_WAIT_SECONDS)
        for y in (0, 500, 900, 1300, 1700):
            page.run_js(f"window.scrollTo(0, {y});", timeout=10)
            time.sleep(0.4)
        payload = extract_browser_dom_items(page)
        html_text = browser_outer_html(page, timeout=BROWSER_JS_TIMEOUT)
    finally:
        close_browser_page(page)

    elapsed = round(time.perf_counter() - start, 3)
    html_path = raw_dir / "promotion_page.html"
    payload_path = raw_dir / "promotion_dom_items.json"
    html_path.write_text(html_text, encoding="utf-8", errors="replace")
    payload_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    rows = parse_dom_items(payload.get("items") or [])
    summary = {
        "started_at": now(),
        "fetch_mode": "browser_dom",
        "url": REFERER,
        "browser_url": browser_url,
        "elapsed_seconds": elapsed,
        "x_request_cost": "0",
        "container_found": bool(payload.get("containerFound")),
        "raw_link_count": payload.get("linkCount", 0),
        "raw_item_count": payload.get("itemCount", 0),
        "row_count": len(rows),
        "html": rel_path(html_path),
        "dom_items": rel_path(payload_path),
        "browser": browser_meta,
    }
    return rows, summary


def run_batch_with_retries(client, html_text, placements, browser_page=None):
    attempts = []
    last_result = {"summaries": [], "rows": []}
    for attempt in range(1, PROMOTION_MAX_ATTEMPTS + 1):
        if browser_page is not None:
            last_result = run_batch_browser(browser_page, html_text, placements, attempt=attempt)
        else:
            last_result = run_batch(client, html_text, placements, attempt=attempt)
        attempt_info = batch_attempt_summary(last_result, attempt)
        attempts.append(attempt_info)
        if last_result.get("rows"):
            break
        if not attempt_info["retryable"]:
            break
        sleep_before_retry(attempt)
    return {
        "summaries": last_result.get("summaries") or [],
        "rows": last_result.get("rows") or [],
        "attempts": attempts,
        "call_count": len(attempts),
        "total_x_request_cost": sum(cost_float(attempt.get("x_request_cost")) for attempt in attempts),
    }


def run_one_with_retries(client, html_text, placement, browser_page=None):
    attempts = []
    last_result = {"summary": {}, "rows": []}
    for attempt in range(1, PROMOTION_MAX_ATTEMPTS + 1):
        if browser_page is not None:
            last_result = run_one_browser(browser_page, html_text, placement, attempt=attempt)
        else:
            last_result = run_one(client, html_text, placement, attempt=attempt)
        attempt_info = single_attempt_summary(last_result, attempt)
        attempts.append(attempt_info)
        if last_result.get("rows"):
            break
        if not attempt_info["retryable"]:
            break
        sleep_before_retry(attempt)
    return {
        "summary": last_result.get("summary") or {},
        "rows": last_result.get("rows") or [],
        "attempts": attempts,
        "call_count": len(attempts),
        "total_x_request_cost": sum(cost_float(attempt.get("x_request_cost")) for attempt in attempts),
    }


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "promotion_type",
                "promotion_placement",
                "promotion_position",
                "sku_id",
                "retailer_sku_name",
                "product_url",
                "customer_price",
                "regular_price",
                "total_savings",
                "final_sku_price",
                "original_sku_price",
                "savings",
                "offer",
                "offer_count",
                "pick_up_availability",
                "fastest_delivery",
                "delivery_availability",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def write_failure_skip_summary(exc):
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    out_csv = RUN_ROOT / "parsed" / "all_promotion_products.csv"
    write_rows(out_csv, [])
    summary = {
        "started_at": now(),
        "skipped": True,
        "collection_failed": True,
        "reason": "promotion collection failed; continuing pipeline with empty promotion rows",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "placements": [],
        "excluded_placements": [],
        "call_count": 0,
        "row_count": 0,
        "total_x_request_cost": 0,
        "summaries": [],
        "csv": rel_path(out_csv),
        "fetch_mode": FETCH_MODE,
        "expected_min_rows": PROMOTION_EXPECTED_MIN_ROWS,
        "below_expected_min_rows": bool(PROMOTION_EXPECTED_MIN_ROWS),
    }
    (RUN_ROOT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main():
    category = bestbuy_category()
    promotion_url = target_url("promotion", category=category)
    if category != "TV" or not promotion_url:
        reason = (
            "HHP promotion page is not collected"
            if category == "HHP"
            else (f"{category} promotion page is not collected" if category != "TV" else "no promotion URL for category")
        )
        summary = {
            "started_at": now(),
            "skipped": True,
            "reason": reason,
            "placements": [],
            "call_count": 0,
            "row_count": 0,
            "total_x_request_cost": 0,
        }
        RUN_ROOT.mkdir(parents=True, exist_ok=True)
        write_rows(RUN_ROOT / "parsed" / "all_promotion_products.csv", [])
        (RUN_ROOT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    if FETCH_MODE == "browser_dom":
        rows, dom_summary = run_browser_dom()
        slug = "all" if PLACEMENT.lower() == "all" else PLACEMENT
        out_csv = RUN_ROOT / "parsed" / f"{slug}_promotion_products.csv"
        write_rows(out_csv, rows)
        summary = {
            "started_at": now(),
            "placements": ["browser_dom_carousel"],
            "excluded_placements": [],
            "call_count": 1,
            "row_count": len(rows),
            "total_x_request_cost": 0,
            "max_attempts": 1,
            "expected_min_rows": PROMOTION_EXPECTED_MIN_ROWS,
            "fallback_to_single": False,
            "attempts": [],
            "summaries": [dom_summary],
            "csv": rel_path(out_csv),
            "fetch_mode": FETCH_MODE,
            "browser": dom_summary.get("browser", {}),
            "below_expected_min_rows": bool(PROMOTION_EXPECTED_MIN_ROWS and len(rows) < PROMOTION_EXPECTED_MIN_ROWS),
        }
        RUN_ROOT.mkdir(parents=True, exist_ok=True)
        (RUN_ROOT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    requested_placements = list(PROMOTION_LABELS) if PLACEMENT.lower() == "all" else [PLACEMENT]
    excluded_placements = [placement for placement in requested_placements if promotion_placement_excluded(placement)]
    excluded_set = set(excluded_placements)
    placements = [placement for placement in requested_placements if placement not in excluded_set]
    if not placements:
        summary = {
            "started_at": now(),
            "placements": [],
            "excluded_placements": excluded_placements,
            "call_count": 0,
            "row_count": 0,
            "total_x_request_cost": 0,
            "summaries": [],
            "csv": rel_path(RUN_ROOT / "parsed" / "all_promotion_products.csv"),
        }
        RUN_ROOT.mkdir(parents=True, exist_ok=True)
        write_rows(RUN_ROOT / "parsed" / "all_promotion_products.csv", [])
        (RUN_ROOT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    html_text = QUERY_TEMPLATE_HTML.read_text(encoding="utf-8", errors="ignore")
    browser_page = None
    browser_meta = {}
    client = None
    if FETCH_MODE == "browser_graphql":
        if not REFERER:
            raise RuntimeError("Set BESTBUY_PROMOTION_REFERER or target_urls.promotion before browser collection")
        browser_page, browser_meta = create_browser_page(
            run_root=RUN_ROOT,
            name="promotion_browser",
            headless=BROWSER_HEADLESS,
            local_port=BROWSER_LOCAL_PORT,
        )
        browser_page.get(add_intl_nosplash(REFERER))
        if BROWSER_WAIT_SECONDS:
            time.sleep(BROWSER_WAIT_SECONDS)
        browser_html = browser_outer_html(browser_page, timeout=BROWSER_JS_TIMEOUT)
        browser_html_path = RUN_ROOT / "raw" / "promotion_browser_page.html"
        browser_html_path.parent.mkdir(parents=True, exist_ok=True)
        browser_html_path.write_text(browser_html, encoding="utf-8", errors="replace")
        if browser_html:
            html_text = browser_html + "\n" + html_text
    elif FETCH_MODE in {"zenrows", "direct"}:
        api_key = os.getenv("ZENROWS_API_KEY")
        if not api_key:
            raise RuntimeError("Set ZENROWS_API_KEY in .env")
        client = ZenRowsClient(api_key)
    else:
        raise ValueError("BESTBUY_PROMOTION_FETCH_MODE must be browser_dom, browser_graphql, zenrows, or direct")

    fallback_to_single = False
    attempts = []
    total_x_request_cost = 0.0
    try:
        if PLACEMENT.lower() == "all":
            result = run_batch_with_retries(client, html_text, placements, browser_page=browser_page)
            all_rows = result["rows"]
            attempts.extend(result["attempts"])
            total_x_request_cost += result["total_x_request_cost"]
            latest_by_placement = {summary.get("placement"): summary for summary in result["summaries"]}
            collected_placements = {row.get("promotion_placement") for row in all_rows if row.get("promotion_placement")}
            missing_placements = [placement for placement in placements if placement not in collected_placements]
            if missing_placements:
                fallback_to_single = True
                for placement in missing_placements:
                    single_result = run_one_with_retries(client, html_text, placement, browser_page=browser_page)
                    all_rows.extend(single_result["rows"])
                    all_rows = dedupe_promotion_rows(all_rows)
                    attempts.extend(single_result["attempts"])
                    total_x_request_cost += single_result["total_x_request_cost"]
                    latest_by_placement[placement] = single_result["summary"]
            summaries = [latest_by_placement.get(placement, {}) for placement in placements]
            call_count = len(attempts)
        else:
            result = run_one_with_retries(client, html_text, placements[0], browser_page=browser_page)
            all_rows = result["rows"]
            summaries = [result["summary"]]
            attempts.extend(result["attempts"])
            total_x_request_cost += result["total_x_request_cost"]
            call_count = len(attempts)
    finally:
        close_browser_page(browser_page)

    slug = "all" if PLACEMENT.lower() == "all" else PLACEMENT
    out_csv = RUN_ROOT / "parsed" / f"{slug}_promotion_products.csv"
    write_rows(out_csv, all_rows)
    summary = {
        "started_at": now(),
        "placements": placements,
        "excluded_placements": excluded_placements,
        "call_count": call_count,
        "row_count": len(all_rows),
        "total_x_request_cost": round(total_x_request_cost, 7),
        "max_attempts": PROMOTION_MAX_ATTEMPTS,
        "expected_min_rows": PROMOTION_EXPECTED_MIN_ROWS if PLACEMENT.lower() == "all" else 0,
        "fallback_to_single": fallback_to_single,
        "attempts": attempts,
        "summaries": summaries,
        "csv": rel_path(out_csv),
        "fetch_mode": FETCH_MODE,
        "browser": browser_meta,
        "below_expected_min_rows": bool(
            PLACEMENT.lower() == "all"
            and PROMOTION_EXPECTED_MIN_ROWS
            and len(all_rows) < PROMOTION_EXPECTED_MIN_ROWS
        ),
    }
    (RUN_ROOT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        write_failure_skip_summary(exc)
