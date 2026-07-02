import csv
import atexit
import html
import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from threading import Lock

from bs4 import BeautifulSoup
from lxml import html as lxml_html
from requests import RequestException
from zenrows import ZenRowsClient

from .step00_browser_session import (
    add_intl_nosplash,
    browser_fetch_graphql,
    close_browser_page,
    create_browser_page,
    env_bool,
    env_int,
)
from .step00_config import (
    DEFAULT_BESTBUY_RUN_ROOT,
    KRW_PER_USD,
    apply_bestbuy_location,
    bestbuy_category,
    bestbuy_output_table,
    bestbuy_store_id,
    bestbuy_zip_code,
    db_config,
    old_pdp_url,
    rel_path,
)
from .step00_availability_policy import ALL_AVAILABILITY_FIELDS
from .step00_detail_benchmarks import append_detail_benchmark, write_detail_benchmarks
from .step00_parse_pdp import event_data, extract_apollo_payloads


def parse_float_sequence(value):
    result = []
    for token in re.split(r"[,\s]+", str(value or "").strip()):
        if not token.strip():
            continue
        try:
            result.append(float(token))
        except ValueError:
            continue
    return result


RUN_DATE = os.getenv("BESTBUY_RUN_DATE", datetime.now().strftime("%Y%m%d"))
CATEGORY = bestbuy_category()
RUN_ROOT = Path(os.getenv("BESTBUY_RUN_ROOT", DEFAULT_BESTBUY_RUN_ROOT))
DETAIL_ROOT = Path(os.getenv("BESTBUY_DETAIL_RUN_ROOT", RUN_ROOT / "detail"))
OUTPUT_ROOT = Path(os.getenv("BESTBUY_OUTPUT_ROOT", RUN_ROOT / "output"))
TARGET_CSV = Path(os.getenv("BESTBUY_DETAIL_TARGET_CSV", OUTPUT_ROOT / "bestbuy_final_targets.csv"))
SAMPLE_SCHEMA_CSV = Path(os.getenv("BESTBUY_OUTPUT_SCHEMA_CSV", "references/tv_retail_com_202605170513.csv"))
SELECTOR_TABLE = os.getenv("BESTBUY_SELECTOR_TABLE", "dx_xpath_selectors")
USE_DB_SELECTORS = os.getenv("BESTBUY_DETAIL_USE_DB_SELECTORS", "1").lower() in {"1", "true", "yes", "y"}
LIMIT = int(os.getenv("BESTBUY_DETAIL_LIMIT", "0"))
MAX_ATTEMPTS = int(os.getenv("BESTBUY_DETAIL_MAX_ATTEMPTS", "5"))
MAX_REVIEW_TEXTS = max(1, int(os.getenv("BESTBUY_REVIEW_TEXT_LIMIT", "20")))
AUTO_RETRY = os.getenv("BESTBUY_DETAIL_AUTO_RETRY", "1").lower() in {"1", "true", "yes", "y"}
DETAIL_RETRY_SLEEP_SECONDS = float(os.getenv("BESTBUY_DETAIL_RETRY_SLEEP_SECONDS", "2"))
DETAIL_RETRY_SLEEP_SEQUENCE = parse_float_sequence(os.getenv("BESTBUY_DETAIL_RETRY_SLEEP_SEQUENCE", ""))
DETAIL_RETRY_STATUS_CODES = {
    int(value)
    for value in re.split(r"[,\s]+", os.getenv("BESTBUY_DETAIL_RETRY_STATUS_CODES", "408,409,422,425,429,500,502,503,504"))
    if value.strip().isdigit()
}
RETRY_ONLY = os.getenv("BESTBUY_DETAIL_RETRY_ONLY", "0").lower() in {"1", "true", "yes", "y"}
RETRY_MISSING_SIMILAR = os.getenv("BESTBUY_DETAIL_RETRY_MISSING_SIMILAR", "0").lower() in {"1", "true", "yes", "y"}
REBUILD_ONLY = os.getenv("BESTBUY_DETAIL_REBUILD_ONLY", "0").lower() in {"1", "true", "yes", "y"}
FORCE_REFRESH = os.getenv("BESTBUY_DETAIL_FORCE_REFRESH", "0").lower() in {"1", "true", "yes", "y"}
REQUEST_TIMEOUT = int(os.getenv("ZENROWS_TIMEOUT", "240"))
FETCH_MODE = os.getenv("BESTBUY_DETAIL_FETCH_MODE", os.getenv("BESTBUY_FETCH_MODE", "zenrows")).strip().lower()
DETAIL_DIRECT_GRAPHQL = os.getenv("BESTBUY_DETAIL_DIRECT_GRAPHQL", "1").lower() in {"1", "true", "yes", "y"}
DETAIL_PDP_FALLBACK = os.getenv("BESTBUY_DETAIL_PDP_FALLBACK", "0").lower() in {"1", "true", "yes", "y"}
WORKERS = int(os.getenv("BESTBUY_DETAIL_WORKERS", "3"))
BROWSER_GRAPHQL_WAIT_SECONDS = max(
    0.0,
    float(os.getenv("BESTBUY_DETAIL_BROWSER_GRAPHQL_WAIT_SECONDS", "8")),
)
BROWSER_GRAPHQL_JS_TIMEOUT = max(1, int(os.getenv("BESTBUY_DETAIL_BROWSER_GRAPHQL_JS_TIMEOUT", "120")))
BROWSER_GRAPHQL_HEADLESS = env_bool("BESTBUY_DETAIL_BROWSER_GRAPHQL_HEADLESS", "1")
BROWSER_GRAPHQL_LOCAL_PORT = env_int("BESTBUY_DETAIL_BROWSER_GRAPHQL_LOCAL_PORT", "0")
STAGE = os.getenv("BESTBUY_DETAIL_STAGE", "detail").lower()
SAVE_HTML_MODE = os.getenv("BESTBUY_SAVE_HTML_MODE", "slim").lower()
DETAIL_SCROLL = os.getenv("BESTBUY_DETAIL_SCROLL", "1").lower() in {"1", "true", "yes", "y"}
DETAIL_SCROLL_NETWORK_IDLE = os.getenv("BESTBUY_DETAIL_SCROLL_NETWORK_IDLE", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
DETAIL_COMPARE_CAPTURE_HOOK = os.getenv("BESTBUY_DETAIL_COMPARE_CAPTURE_HOOK", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
DETAIL_COMPARE_SCROLL_SCAN = os.getenv("BESTBUY_DETAIL_COMPARE_SCROLL_SCAN", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
DETAIL_COMPARE_DOM_OBSERVER = os.getenv("BESTBUY_DETAIL_COMPARE_DOM_OBSERVER", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
DETAIL_COMPARE_FORCE_FETCH = os.getenv("BESTBUY_DETAIL_COMPARE_FORCE_FETCH", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
DETAIL_COMPARE_FORCE_FETCH_WAIT = int(os.getenv("BESTBUY_DETAIL_COMPARE_FORCE_FETCH_WAIT", "2500"))
DETAIL_JSON_RESPONSE = os.getenv("BESTBUY_DETAIL_JSON_RESPONSE", "0").lower() in {"1", "true", "yes", "y"}
DETAIL_JSON_WAIT = os.getenv("BESTBUY_DETAIL_JSON_WAIT", "10000")
DETAIL_REQUIRE_SIMILAR = (
    os.getenv("BESTBUY_DETAIL_REQUIRE_SIMILAR", "1" if DETAIL_JSON_RESPONSE else "0").lower()
    in {"1", "true", "yes", "y"}
)
DETAIL_RETRY_ON_MISSING_SIMILAR = os.getenv("BESTBUY_DETAIL_RETRY_ON_MISSING_SIMILAR", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
DETAIL_SIMILAR_MIN_NAMES = int(os.getenv("BESTBUY_DETAIL_SIMILAR_MIN_NAMES", "1"))
DETAIL_PRINT_MANIFEST_JSON = os.getenv("BESTBUY_DETAIL_PRINT_MANIFEST_JSON", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
DETAIL_LOG_FAILURE_LIMIT = int(os.getenv("BESTBUY_DETAIL_LOG_FAILURE_LIMIT", "3"))
DETAIL_SKU_BATCH_SIZE = max(1, int(os.getenv("BESTBUY_DETAIL_SKU_BATCH_SIZE", "1")))
DETAIL_SKU_BATCH_REFILL = os.getenv("BESTBUY_DETAIL_SKU_BATCH_REFILL", "1").lower() in {"1", "true", "yes", "y"}
DETAIL_SKU_BATCH_REFILL_SINGLE_FALLBACK = os.getenv(
    "BESTBUY_DETAIL_SKU_BATCH_REFILL_SINGLE_FALLBACK",
    "1",
).lower() in {"1", "true", "yes", "y"}
REVIEW20_BATCH_SIZE = max(
    1,
    int(os.getenv("BESTBUY_REVIEW20_BATCH_SIZE", os.getenv("BESTBUY_DETAIL_SKU_BATCH_SIZE", "1"))),
)
REVIEW20_BATCH_SINGLE_FALLBACK = os.getenv("BESTBUY_REVIEW20_BATCH_SINGLE_FALLBACK", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}

RAW_DETAIL_DIR = DETAIL_ROOT / "raw" / "detail_html"
RAW_REVIEW_DIR = DETAIL_ROOT / "raw" / "review20"
RAW_COMPARE_DIR = DETAIL_ROOT / "raw" / "compare"


def detail_retry_sleep_seconds(attempt):
    if DETAIL_RETRY_SLEEP_SEQUENCE:
        index = max(0, int(attempt) - 1)
        if index < len(DETAIL_RETRY_SLEEP_SEQUENCE):
            return max(0.0, DETAIL_RETRY_SLEEP_SEQUENCE[index])
        return max(0.0, DETAIL_RETRY_SLEEP_SEQUENCE[-1])
    return max(0.0, DETAIL_RETRY_SLEEP_SECONDS)


PARSED_DIR = DETAIL_ROOT / "parsed"
BENCHMARKS_DIR = DETAIL_ROOT / "benchmarks"
DETAIL_ROWS_CSV = PARSED_DIR / "detail_enriched_rows.csv"
FAILURES_CSV = PARSED_DIR / "detail_failures.csv"
DETAIL_BENCHMARKS_CSV = BENCHMARKS_DIR / "detail_benchmarks.csv"
FINAL_OUTPUT_CSV = Path(os.getenv("BESTBUY_FINAL_OUTPUT_CSV", OUTPUT_ROOT / "final_output.csv"))
PRODUCT_LIST_CSV = Path(os.getenv("BESTBUY_PRODUCT_LIST_OUTPUT", OUTPUT_ROOT / "bestbuy_product_list.csv"))
RETRY_MISSING_SIMILAR_SOURCE_CSV = os.getenv("BESTBUY_DETAIL_RETRY_MISSING_SIMILAR_SOURCE_CSV", "").strip()
MANIFEST_PATH = DETAIL_ROOT / "manifest_detail_enrichment.json"
FETCH_COMPARE = os.getenv("BESTBUY_DETAIL_FETCH_COMPARE", "0").lower() in {"1", "true", "yes", "y"}
FETCH_GET_IT_FAST = os.getenv("BESTBUY_DETAIL_FETCH_GET_IT_FAST", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
FETCH_FULFILLMENT_DYNAMIC = os.getenv("BESTBUY_DETAIL_FETCH_FULFILLMENT_DYNAMIC", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
RUN_BATCH_ID = os.getenv("BESTBUY_BATCH_ID") or f"b_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
TARGET_SKUS = {
    value.strip().lower()
    for value in re.split(r"[\s,;]+", os.getenv("BESTBUY_DETAIL_SKUS", ""))
    if value.strip()
}
BROWSER_GRAPHQL_PAGE = None
BROWSER_GRAPHQL_META = {}
BROWSER_GRAPHQL_CURRENT_URL = ""
BROWSER_GRAPHQL_LOCK = Lock()


def hhp_compare_v2_fallback_enabled():
    return CATEGORY == "HHP" and os.getenv("BESTBUY_HHP_COMPARE_V2_FALLBACK", "1").lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def hhp_trade_in_data_enabled():
    return CATEGORY == "HHP" and os.getenv("BESTBUY_HHP_TRADE_IN_DATA", "1").lower() in {
        "1",
        "true",
        "yes",
        "y",
    }

HHP_FINAL_FIELDS = [
    "id",
    "country",
    "product",
    "item",
    "account_name",
    "page_type",
    "count_of_reviews",
    "retailer_sku_name",
    "product_url",
    "star_rating",
    "count_of_star_ratings",
    "final_sku_price",
    "original_sku_price",
    "savings",
    "offer",
    "pick_up_availability",
    "fastest_delivery",
    "sku_status",
    "trade_in",
    "hhp_storage",
    "hhp_color",
    "hhp_carrier",
    "detailed_review_content",
    "recommendation_intent",
    "main_rank",
    "bsr_rank",
    "trend_rank",
    "retailer_sku_name_similar",
    "promotion_type",
    "calendar_week",
    "crawl_strdatetime",
    "batch_id",
]

LDY_FINAL_FIELDS = [
    "id",
    "country",
    "product",
    "item",
    "sku",
    "account_name",
    "page_type",
    "count_of_reviews",
    "retailer_sku_name",
    "product_url",
    "star_rating",
    "count_of_star_ratings",
    "final_sku_price",
    "original_sku_price",
    "savings",
    "offer",
    "pick_up_availability",
    "delivery_availability",
    "sku_status",
    "detailed_review_content",
    "recommendation_intent",
    "main_rank",
    "bsr_rank",
    "retailer_sku_name_similar",
    "ldy_capacity",
    "ldy_loading_type",
    "calendar_week",
    "crawl_strdatetime",
    "batch_id",
]

REF_FINAL_FIELDS = [
    "id",
    "country",
    "product",
    "item",
    "sku",
    "account_name",
    "page_type",
    "count_of_reviews",
    "retailer_sku_name",
    "product_url",
    "star_rating",
    "count_of_star_ratings",
    "final_sku_price",
    "original_sku_price",
    "savings",
    "offer",
    "pick_up_availability",
    "delivery_availability",
    "sku_status",
    "detailed_review_content",
    "recommendation_intent",
    "main_rank",
    "bsr_rank",
    "retailer_sku_name_similar",
    "ref_capacity",
    "ref_refrigerator_type",
    "calendar_week",
    "crawl_strdatetime",
    "batch_id",
]

FALLBACK_FINAL_FIELDS = {
    "HHP": HHP_FINAL_FIELDS,
    "LDY": LDY_FINAL_FIELDS,
    "REF": REF_FINAL_FIELDS,
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def batch_id_from_datetime(value):
    return f"b_{value.strftime('%Y%m%d_%H%M%S')}"


def page_type_from_target(target):
    source = target.get("target_source")
    if source == "bsr_only_backfill":
        return "bsr"
    if source == "promotion_backfill":
        return "promotion"
    if source == "trending_backfill":
        return "trend"
    return "main"


def compact_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def first_non_empty(*values):
    for value in values:
        if value not in ("", None, [], {}):
            return value
    return ""


def first_text_starting(prefix, *values):
    prefix_text = str(prefix or "").lower()
    for value in values:
        text = compact_text(value)
        if text and text.lower().startswith(prefix_text):
            return text
    return ""


def sku_from_product_url(url):
    match = re.search(r"/sku/(\d+)", str(url or ""))
    return match.group(1) if match else ""


def canonical_pdp_url(url):
    text = compact_text(url)
    if not text:
        return ""
    if "/sku/" in text:
        text = text.split("/sku/", 1)[0]
    return text.rstrip("/").lower()


def fetchable_pdp_url(url):
    text = compact_text(url)
    if not text:
        return ""
    if "/sku/" in text:
        text = text.split("/sku/", 1)[0]
    return text.rstrip("/")


def truthy(value):
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "y", "sponsored"}


def listing_sku_status(target):
    status = compact_text(target.get("sku_status"))
    if status:
        return status
    return "Sponsored" if truthy(target.get("is_sponsored")) else ""


def clean_hhp_carrier(value):
    text = compact_text(value)
    if not text:
        return ""
    lowered = text.lower()
    carriers = [
        ("Total by Verizon", ["total by verizon"]),
        ("Metro by T-Mobile", ["metro by t-mobile", "metropcs", "metro pcs", "metro"]),
        ("Unlocked", ["unlocked", "fully unlocked"]),
        ("AT&T", ["at&t", "att"]),
        ("Verizon", ["verizon"]),
        ("T-Mobile", ["t-mobile", "tmobile"]),
        ("Sprint", ["sprint"]),
        ("Boost Mobile", ["boost mobile"]),
        ("Cricket", ["cricket"]),
        ("Tracfone", ["tracfone"]),
        ("Google Fi", ["google fi"]),
        ("Consumer Cellular", ["consumer cellular"]),
        ("Mint Mobile", ["mint mobile", "mint"]),
        ("Ultra Mobile", ["ultra mobile"]),
        ("H2O Wireless", ["h2o wireless", "h2o"]),
        ("Ting Mobile", ["ting mobile", "ting"]),
        ("US Cellular", ["us cellular", "u.s. cellular"]),
        ("Simple Mobile", ["simple mobile"]),
        ("Straight Talk", ["straight talk"]),
        ("Total Wireless", ["total wireless"]),
        ("Visible", ["visible"]),
        ("Lively", ["lively sim", "lively mobile"]),
    ]
    found = []
    parts = [part.strip() for part in re.split(r"[,;/|]+", text) if part.strip()]
    scan_values = parts if len(parts) > 1 else [text]
    for scan_value in scan_values:
        scan_lowered = scan_value.lower()
        matches = []
        for canonical, needles in carriers:
            positions = [scan_lowered.find(needle) for needle in needles if needle in scan_lowered]
            if positions:
                matches.append((min(positions), canonical))
        for _, canonical in sorted(matches, key=lambda item: item[0]):
            if canonical == "Verizon" and "Total by Verizon" in found and "total by verizon" in scan_lowered:
                continue
            if canonical == "T-Mobile" and "Metro by T-Mobile" in found and "metro by t-mobile" in scan_lowered:
                continue
            if canonical not in found:
                found.append(canonical)
    return ", ".join(found) if found else text


def clean_hhp_carrier_compatibility(value):
    text = compact_text(value)
    if not text:
        return ""
    parts = [compact_text(part) for part in re.split(r"\s*,\s*|\s*\|\|\|\s*", text) if compact_text(part)]
    cleaned = []
    for part in parts:
        carrier = clean_hhp_carrier(part)
        if carrier and carrier not in cleaned:
            cleaned.append(carrier)
    return ", ".join(cleaned)


def hhp_carrier_field_name(value):
    name = compact_text(value).lower().replace("_", " ")
    if not name or "compatibility" in name:
        return False
    tail = name.rsplit(":", 1)[-1].strip()
    return tail in {"carrier", "wireless carrier"}


def hhp_title_mentions_unlocked(product_name):
    return bool(re.search(r"(?i)\bunlocked\b", compact_text(product_name)))


def hhp_spec_unlocked_yes(products):
    value = spec_value(products, "Unlocked")
    return compact_text(value).lower() in {"yes", "true", "1", "y"}


def clean_hhp_color(value):
    text = compact_text(value)
    if not text:
        return ""
    text = re.sub(
        r"(?i)\s*\((?:unlocked|verizon|at&t|att|t-mobile|tmobile|total wireless|tracfone|lively)\)\s*$",
        "",
        text,
    ).strip(" ,-")
    text = re.sub(
        r"(?i)\b(?:carrier\s+)?(?:unlocked|verizon|at&t|att|t-mobile|tmobile)\b\s*$",
        "",
        text,
    ).strip(" ,-")
    return text


def clean_hhp_storage(value):
    text = compact_text(value)
    if not text:
        return ""
    match = re.search(r"(?i)\b(\d+(?:\.\d+)?)\s*(TB|GB|terabytes?|gigabytes?)\b", text)
    if not match:
        return text
    number = match.group(1)
    unit = match.group(2).lower()
    if unit.startswith("tb") or unit.startswith("tera"):
        return f"{number} terabytes"
    return f"{number} gigabytes"


def hhp_variation_attrs_from_product(product, sku=""):
    sku = compact_text(sku)
    attrs = {"hhp_storage": "", "hhp_color": "", "hhp_carrier": ""}
    products = product if isinstance(product, list) else [product]
    candidates = []

    def collect_candidate(value):
        if isinstance(value, dict):
            variations = value.get("variations")
            if isinstance(variations, list) and variations:
                candidates.append(value)
            for child in value.values():
                collect_candidate(child)
        elif isinstance(value, list):
            for child in value:
                collect_candidate(child)

    collect_candidate(products)
    selected = []
    for candidate in candidates:
        candidate_sku = first_non_empty(
            candidate.get("sku"),
            first_path([candidate], ["product", "skuId"]),
            first_path([candidate], ["bsinProduct", "featuredSKU", "skuId"]),
            first_path([candidate], ["bsinProduct", "featuredSKU", "product", "skuId"]),
        )
        if sku and str(candidate_sku) == sku:
            selected.append(candidate)
    if not selected and len(candidates) == 1:
        selected = candidates

    for candidate in selected:
        for variation in as_list(candidate.get("variations")):
            if not isinstance(variation, dict):
                continue
            raw_name = compact_text(variation.get("rawName") or variation.get("displayName")).lower()
            value = compact_text(variation.get("value"))
            if not value:
                continue
            if hhp_carrier_field_name(raw_name):
                attrs["hhp_carrier"] = clean_hhp_carrier(value)
            elif "color" in raw_name:
                attrs["hhp_color"] = clean_hhp_color(value)
            elif "storage" in raw_name or "capacity" in raw_name:
                attrs["hhp_storage"] = clean_hhp_storage(value)
    return attrs


def hhp_attributes_from_product(product, product_name, sku=""):
    products = product if isinstance(product, list) else [product]
    product = products[-1] if products else {}
    attrs = {"hhp_storage": "", "hhp_color": "", "hhp_carrier": ""}
    color = first_path([product], ["color", "displayName"])
    if color:
        attrs["hhp_color"] = clean_hhp_color(color)
    spec_candidates = {
        "hhp_storage": ["Internal Storage", "Storage Capacity", "Built-In Storage", "Total Storage Capacity"],
        "hhp_color": ["Color", "Color Category"],
    }
    for field, names in spec_candidates.items():
        for name in names:
            value = spec_value(products, name)
            if value:
                if field == "hhp_storage":
                    attrs[field] = clean_hhp_storage(value)
                else:
                    attrs[field] = clean_hhp_color(value)
                break
    for name in ("Carrier", "Wireless Carrier"):
        value = spec_value(products, name)
        if value:
            attrs["hhp_carrier"] = clean_hhp_carrier(value)
            break
    variation_attrs = hhp_variation_attrs_from_product(products, sku or first_value(products, "skuId"))
    for field, value in variation_attrs.items():
        if value:
            attrs[field] = value
    if not attrs["hhp_carrier"]:
        if hhp_title_mentions_unlocked(product_name) and hhp_spec_unlocked_yes(products):
            attrs["hhp_carrier"] = "Unlocked"
    return attrs


def money(value):
    if value in ("", None):
        return ""
    text = str(value).strip()
    try:
        result = f"${float(text.replace('$', '').replace(',', '')):,.2f}"
        return result[:-3] if result.endswith(".00") else result
    except (TypeError, ValueError):
        return text


def money_int(value):
    if value in ("", None):
        return ""
    try:
        return f"${int(round(float(value))):,}"
    except (TypeError, ValueError):
        return str(value)


def numeric_money(value):
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def normalized_price_fields(final_price, original_price, savings=""):
    final_value = numeric_money(final_price)
    if final_value is None:
        return final_price, "", ""
    final_price = money(final_value)
    original_value = numeric_money(original_price)
    if original_value is None or original_value <= final_value:
        return final_price, "", ""
    original_price = money(original_value)
    expected_savings = original_value - final_value
    if expected_savings <= 0:
        return final_price, "", ""
    return final_price, original_price, money(expected_savings)


def price_output_fields(price, target, selector_values):
    final_price = first_non_empty(
        money(price.get("displayableCustomerPrice") or price.get("customerPrice") or target.get("customer_price")),
        selector_values.get("final_sku_price"),
        selector_values.get("final_sku_price_see_price_in_cart"),
        selector_values.get("final_sku_price_no_longer_available"),
    )
    original_price = first_non_empty(
        money(price.get("displayableRegularPrice") or price.get("regularPrice") or target.get("regular_price")),
        selector_values.get("original_sku_price"),
    )
    savings = first_non_empty(
        money(price.get("totalSavings") or target.get("total_savings")),
        selector_values.get("savings"),
    )
    return normalized_price_fields(final_price, original_price, savings)


def no_longer_available_price_fields(final_price, original_price="", savings="", unavailable=False):
    if not unavailable:
        return final_price, original_price, savings
    return "no longer available", "", ""


def numeric_rating(value):
    if value in ("", None):
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def has_not_yet_reviewed_text(*values):
    return any("not yet reviewed" in str(value or "").lower() for value in values)


def screen_size_from_name(name):
    text = compact_text(name)
    match = re.search(r'(?i)\b(\d{2,3}(?:\.\d+)?)\s*(?:"|”|″|inch(?:es)?|in\.|[^\w\s])?\s+class\b', text)
    if not match:
        return ""
    number = match.group(1)
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return f"{number} inches"


def int_commas(value):
    if value in ("", None):
        return ""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def date_to_phrase(prefix, date_value):
    if not date_value:
        return ""
    try:
        dt = datetime.fromisoformat(str(date_value)[:10])
    except ValueError:
        return ""
    return f"{prefix} {dt.strftime('%a, %b')} {dt.day}"


def date_to_relative_or_phrase(prefix, date_value):
    if not date_value:
        return ""
    try:
        dt = datetime.fromisoformat(str(date_value)[:10])
    except ValueError:
        return ""
    today = datetime.now().date()
    target = dt.date()
    if target == today:
        return f"{prefix} today"
    if (target - today).days == 1:
        return f"{prefix} tomorrow"
    return f"{prefix} {dt.strftime('%a, %b')} {dt.day}"


def fastest_delivery_date_phrase(date_value):
    if not date_value:
        return ""
    try:
        dt = datetime.fromisoformat(str(date_value)[:10])
    except ValueError:
        return ""
    today = datetime.now().date()
    target = dt.date()
    if target == today:
        return "Get it today"
    if (target - today).days == 1:
        return "Get it tomorrow"
    return f"Get it by {dt.strftime('%a, %b')} {dt.day}"


def with_free_suffix(value):
    text = compact_text(value)
    if not text:
        return ""
    if re.search(r"\bfree\b", text, re.I):
        return text
    return f"{text} \u2022 FREE"


def date_to_phrase_from_get_it_fast(prefix, value):
    if not isinstance(value, dict):
        return ""
    get_it_by = str(value.get("getItBy") or "").strip().lower()
    if get_it_by == "today":
        return f"{prefix} today"
    if get_it_by == "tomorrow":
        return f"{prefix} tomorrow"
    return date_to_relative_or_phrase(prefix, value.get("getItByDate"))


def fastest_delivery_from_get_it_fast(value):
    if not isinstance(value, dict):
        return ""
    get_it_by = str(value.get("getItBy") or "").strip().lower()
    if get_it_by == "today":
        return with_free_suffix("Get it today")
    if get_it_by == "tomorrow":
        return with_free_suffix("Get it tomorrow")
    return with_free_suffix(fastest_delivery_date_phrase(value.get("getItByDate")))


def get_it_fast_availability_values(item):
    data = item.get("data") if isinstance(item, dict) else {}
    connection = data.get("fulfillmentGetItFastOptions") if isinstance(data, dict) else {}
    if not isinstance(connection, dict):
        connection = {}
    shipping = connection.get("shippingCutOffDetails")
    if not isinstance(shipping, dict):
        shipping = {}
    stores = as_list(connection.get("storeCutOffDetails"))
    store = stores[0] if stores and isinstance(stores[0], dict) else {}
    return {
        "pick_up_availability": date_to_phrase_from_get_it_fast("Pick up", store),
        "fastest_delivery": fastest_delivery_from_get_it_fast(shipping),
        "delivery_availability": "",
    }


def html_match(pattern, html_text, flags=re.I | re.S):
    match = re.search(pattern, html_text, flags)
    return compact_text(match.group(1)) if match else ""


def recommendation_from_html(html_text):
    match = re.search(
        r"<span[^>]*>\s*(\d+%)\s*</span>\s*&nbsp;\s*would recommend to a friend",
        html_text,
        re.I | re.S,
    )
    if match:
        return f"{match.group(1)} would recommend to a friend"
    return html_match(r"(\d+%\s*would recommend to a friend)", html_text)


def fastest_delivery_from_html(html_text):
    for pattern in (r'aria-label="(Get it[^"]+)"', r">\s*(Get it[^<]+)</"):
        value = html_match(pattern, html_text)
        if value and value.lower().startswith("get"):
            return compact_text(value)
    return ""


def delivery_from_html(html_text):
    value = html_match(r'aria-label="(Delivery\s+As soon as[^"]+)"', html_text) or html_match(
        r">\s*(Delivery\s+as soon as[^<]+)</",
        html_text,
    )
    return compact_text(value).replace("Delivery As soon as", "Delivery as soon as")


def trade_in_from_html(html_text):
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    title_node = soup.find(attrs={"data-testid": "trade-in-check-your-value"})
    body_node = soup.find(attrs={"data-testid": "trade-in-save-when-you-trade"})
    if body_node is None:
        body_node = soup.find(attrs={"data-testid": re.compile(r"^trade-in-save-up-to-", re.I)})
    title = compact_text(title_node.get_text(" ", strip=True) if title_node else "")
    body = compact_text(body_node.get_text(" ", strip=True) if body_node else "")
    if title and body:
        return compact_text(f"{title} {body}")
    if title:
        return title
    text = compact_text(soup.get_text(" "))
    match = trade_in_text_match(text)
    return compact_text(match.group(1)) if match else ""


def trade_in_text_match(value):
    return re.search(
        r"(Check your trade-in value(?:\.\s*Save(?: up to)?(?:\s+\$[\d,]+(?:\.\d{2})?)?"
        r"(?:\s+when you trade in a similar device)?\.)?)",
        str(value or ""),
        re.I,
    )


def money_amount_text(value):
    text = compact_text(value)
    if not text:
        return ""
    match = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", text)
    if not match:
        return ""
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return ""
    return f"${amount:,.2f}"


def trade_in_from_offer_data(data, include_generic=True):
    if not isinstance(data, dict):
        return ""
    product = data.get("productBySkuId") if "productBySkuId" in data else data
    if not isinstance(product, dict):
        return ""
    offer = product.get("tradeInOffer")
    if isinstance(offer, dict):
        candidates = []
        for carrier_offer in as_list(offer.get("offerCarrierValue")):
            if isinstance(carrier_offer, dict):
                candidates.append(carrier_offer.get("carrierUpToValue"))
        candidates.extend([offer.get("value"), offer.get("disclaimer")])
        for candidate in candidates:
            amount = money_amount_text(candidate)
            if amount:
                return f"Check your trade-in value. Save up to {amount} when you trade in a similar device."
    if include_generic and product.get("isPurchaseWithTradeInEligible") is True:
        return "Check your trade-in value. Save when you trade in a similar device."
    return ""


def nested_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from nested_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_strings(child)


def trade_in_from_products(products):
    for product in reversed(products):
        if not isinstance(product, dict):
            continue
        amount_text = trade_in_from_offer_data(product, include_generic=False)
        if amount_text:
            return amount_text
        if product.get("isPurchaseWithTradeInEligible") is True:
            return "Check your trade-in value. Save when you trade in a similar device."
        for value in nested_strings(product.get("operationalAttributes") or {}):
            match = trade_in_text_match(value)
            if match:
                return compact_text(match.group(1))
    return ""


def clean_energy(value):
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return match.group(0) if match else ""


def quote_ident(value):
    return '"' + str(value).replace('"', '""') + '"'


@lru_cache(maxsize=32)
def detail_selectors(category):
    if not USE_DB_SELECTORS:
        return {}
    config = db_config()
    if not config:
        return {}
    try:
        import psycopg2

        conn = psycopg2.connect(
            host=config.get("host"),
            port=int(config.get("port") or 5432),
            user=config.get("user"),
            password=config.get("password"),
            dbname=config.get("database"),
            connect_timeout=10,
            options="-c statement_timeout=5000",
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT data_field, xpath
                    FROM public.{quote_ident(SELECTOR_TABLE)}
                    WHERE product_line = %s
                      AND account_name ILIKE %s
                      AND page_type = %s
                      AND is_active IS TRUE
                    ORDER BY id
                    """,
                    (str(category or "").upper(), "Bestbuy", "detail"),
                )
                rows = cur.fetchall()
        conn.close()
    except Exception:
        return {}

    selectors = {}
    for field, xpath in rows:
        field = str(field or "").strip()
        xpath = str(xpath or "").strip()
        if field and xpath:
            selectors.setdefault(field, []).append(xpath)
    return selectors


def xpath_text(node):
    if isinstance(node, str):
        return compact_text(node)
    if isinstance(node, bytes):
        return compact_text(node.decode("utf-8", errors="ignore"))
    if hasattr(node, "text_content"):
        return compact_text(node.text_content())
    return compact_text(node)


def eval_selector(document, xpath_expr):
    values = []
    for part in str(xpath_expr or "").split("|||"):
        expr = part.strip()
        if not expr:
            continue
        try:
            matches = document.xpath(expr)
        except Exception:
            continue
        if not isinstance(matches, list):
            matches = [matches]
        for match in matches:
            text = xpath_text(match)
            if text:
                values.append(text)
    return " ".join(dict.fromkeys(values))


def detail_selector_values(html_text):
    if not html_text:
        return {}
    selectors = detail_selectors(bestbuy_category())
    if not selectors:
        return {}
    try:
        document = lxml_html.fromstring(html_text)
    except Exception:
        return {}
    values = {}
    for field, xpaths in selectors.items():
        for xpath_expr in xpaths:
            value = eval_selector(document, xpath_expr)
            if value:
                values[field] = value
                break
    return values


def recommendation_phrase(value):
    value = compact_text(value)
    if not value:
        return ""
    if "would recommend" in value:
        return value
    match = re.search(r"\d+%", value)
    if match:
        return f"{match.group(0)} would recommend to a friend"
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return f"{value}% would recommend to a friend"
    return value


def request_cost(headers):
    raw = headers.get("X-Request-Cost") or headers.get("x-request-cost") or "0"
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def response_error(status, text, fallback):
    if status != 200:
        try:
            problem = json.loads(text or "{}")
        except ValueError:
            problem = {}
        if isinstance(problem, dict):
            code = problem.get("code")
            title = problem.get("title")
            if code or title:
                return "http_{}_{}{}".format(
                    status,
                    code or "error",
                    f": {title}" if title else "",
                )
        return f"http_{status}"
    return fallback


def detail_compare_capture_hook_script():
    return r"""
(() => {
  if (window.__bbyCompareCaptureInstalled) return;
  window.__bbyCompareCaptureInstalled = true;
  const captureId = "bby-compare-capture";
  const captures = [];
  const maxCaptures = 12;
  const maxBodyChars = 800000;

  function asText(value) {
    if (value == null) return "";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value); } catch (e) { return String(value); }
  }

  function isCandidate(url, requestBody, responseBody) {
    const urlText = asText(url);
    const blob = asText(requestBody) + "\n" + asText(responseBody);
    return urlText.indexOf("/gateway/graphql") !== -1 && (
      blob.indexOf("GetCompareProduct") !== -1 ||
      blob.indexOf("single-compare") !== -1 ||
      (
        blob.indexOf("\"productBySkuId\"") !== -1 &&
        blob.indexOf("\"recommendations\"") !== -1 &&
        blob.indexOf("\"subPlacements\"") !== -1
      )
    );
  }

  function publish() {
    try {
      window.__bbyCompareCaptureCount = captures.length;
      let el = document.getElementById(captureId);
      if (!el) {
        el = document.createElement("textarea");
        el.id = captureId;
        el.hidden = true;
        el.style.display = "none";
        (document.body || document.documentElement).appendChild(el);
      }
      el.textContent = JSON.stringify(captures.slice(-maxCaptures));
    } catch (e) {}
  }

  function pushCapture(source, url, method, requestBody, responseBody) {
    try {
      if (!isCandidate(url, requestBody, responseBody)) return;
      captures.push({
        source,
        url: asText(url),
        method: asText(method),
        requestBody: asText(requestBody).slice(0, 4000),
        body: asText(responseBody).slice(0, maxBodyChars)
      });
      publish();
    } catch (e) {}
  }

  const NativeResponse = window.Response;
  if (NativeResponse && NativeResponse.prototype) {
    const originalJson = NativeResponse.prototype.json;
    if (typeof originalJson === "function") {
      NativeResponse.prototype.json = function() {
        const response = this;
        return originalJson.apply(this, arguments).then((data) => {
          pushCapture("response.json", response.url || "", "", "", data);
          return data;
        });
      };
    }
    const originalText = NativeResponse.prototype.text;
    if (typeof originalText === "function") {
      NativeResponse.prototype.text = function() {
        const response = this;
        return originalText.apply(this, arguments).then((text) => {
          pushCapture("response.text", response.url || "", "", "", text);
          return text;
        });
      };
    }
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = function(input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const method = (init && init.method) || (input && input.method) || "GET";
      const requestBody = (init && init.body) || "";
      return originalFetch.apply(this, arguments).then((response) => {
        try {
          response.clone().text().then((text) => {
            pushCapture("fetch.clone", url, method, requestBody, text);
          }).catch(() => {});
        } catch (e) {}
        return response;
      });
    };
  }

  const NativeXhr = window.XMLHttpRequest;
  if (NativeXhr && NativeXhr.prototype) {
    const originalOpen = NativeXhr.prototype.open;
    const originalSend = NativeXhr.prototype.send;
    if (typeof originalOpen === "function" && typeof originalSend === "function") {
      NativeXhr.prototype.open = function(method, url) {
        this.__bbyCompareMethod = method;
        this.__bbyCompareUrl = url;
        return originalOpen.apply(this, arguments);
      };
      NativeXhr.prototype.send = function(body) {
        const xhr = this;
        const requestBody = body;
        try {
          xhr.addEventListener("loadend", () => {
            let responseBody = "";
            try { responseBody = xhr.responseText || ""; } catch (e) {}
            pushCapture("xhr", xhr.__bbyCompareUrl || "", xhr.__bbyCompareMethod || "", requestBody, responseBody);
          });
        } catch (e) {}
        return originalSend.apply(this, arguments);
      };
    }
  }
  publish();
})();
"""


def detail_compare_scroll_scan_script():
    return r"""
(() => {
  const points = [0.18, 0.24, 0.30, 0.36, 0.43, 0.53, 0.45, 0.37, 0.30, 0.24];
  const delay = 650;
  if (Array.isArray(window.__bbyCompareScanTimers)) {
    window.__bbyCompareScanTimers.forEach((timer) => clearTimeout(timer));
  }
  window.__bbyCompareScanTimers = [];
  const height = () => Math.max(
    document.documentElement.scrollHeight || 0,
    document.body ? document.body.scrollHeight || 0 : 0,
    window.innerHeight || 0
  );
  points.forEach((point, index) => {
    const timer = setTimeout(() => {
      const y = Math.max(0, Math.floor(height() * point));
      window.scrollTo(0, y);
      try { window.dispatchEvent(new Event("scroll")); } catch (e) {}
    }, index * delay);
    window.__bbyCompareScanTimers.push(timer);
  });
})();
"""


def detail_compare_dom_observer_script():
    return r"""
(() => {
  if (window.__bbyCompareDomObserverInstalled) return;
  window.__bbyCompareDomObserverInstalled = true;
  window.__bbyCompareDomObserverHits = 0;
  window.__bbyCompareJiggleCount = 0;
  const keywords = ["compare similar products", "compare similar", "similar products"];
  const selectors = "h1,h2,h3,h4,section,[data-testid],div";
  const maxTextLength = 1800;
  const debugId = "bby-compare-debug";
  const debug = window.__bbyCompareDebug || {
    events: [],
    compareTextFound: false,
    observerHits: 0,
    jiggleCount: 0,
    fallbackScrolls: 0,
    captureCount: 0,
    lastScrollY: 0,
    lastScrollPercent: 0,
    pageHeight: 0
  };
  window.__bbyCompareDebug = debug;

  function norm(value) {
    return String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
  }

  function pageHeight() {
    return Math.max(
      document.documentElement.scrollHeight || 0,
      document.body ? document.body.scrollHeight || 0 : 0,
      window.innerHeight || 0
    );
  }

  function publishDebug() {
    try {
      const height = pageHeight();
      debug.captureCount = Number(window.__bbyCompareCaptureCount || 0);
      debug.observerHits = Number(window.__bbyCompareDomObserverHits || 0);
      debug.jiggleCount = Number(window.__bbyCompareJiggleCount || 0);
      debug.lastScrollY = Math.max(0, Math.floor(window.scrollY || 0));
      debug.pageHeight = height;
      debug.lastScrollPercent = height ? Number((debug.lastScrollY / height).toFixed(4)) : 0;
      let el = document.getElementById(debugId);
      if (!el) {
        el = document.createElement("textarea");
        el.id = debugId;
        el.hidden = true;
        el.style.display = "none";
        (document.body || document.documentElement).appendChild(el);
      }
      el.textContent = JSON.stringify(debug);
    } catch (e) {}
  }

  function record(eventName, extra) {
    try {
      debug.events.push(Object.assign({ event: eventName, ts: Date.now() }, extra || {}));
      if (debug.events.length > 40) debug.events = debug.events.slice(-40);
      publishDebug();
    } catch (e) {}
  }

  function dispatchLazyEvents() {
    try { window.dispatchEvent(new Event("scroll")); } catch (e) {}
    try { window.dispatchEvent(new Event("resize")); } catch (e) {}
  }

  let lastJiggleAt = 0;
  function jiggleAround(baseY, reason) {
    const now = Date.now();
    if (now - lastJiggleAt < 1000) return;
    lastJiggleAt = now;
    const offsets = [0, 36, -32, 58, -18, 0];
    offsets.forEach((offset, index) => {
      setTimeout(() => {
        window.scrollTo(0, Math.max(0, Math.floor(baseY + offset)));
        window.__bbyCompareJiggleCount += 1;
        dispatchLazyEvents();
        record("jiggle", { offset, reason });
      }, 220 + index * 260);
    });
  }

  function findCompareNode() {
    let best = null;
    let bestScore = -1;
    for (const el of Array.from(document.querySelectorAll(selectors))) {
      const text = norm(el.innerText || el.textContent || "");
      if (!text || text.length > maxTextLength) continue;
      let score = 0;
      for (const keyword of keywords) {
        if (text.indexOf(keyword) !== -1) score += 1000 - Math.min(text.length, 900);
      }
      if (!score) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      if (/^H[1-4]$/.test(el.tagName)) score += 200;
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return best;
  }

  function scrollToNode(el) {
    if (!el) return false;
    const top = el.getBoundingClientRect().top + window.scrollY - Math.floor((window.innerHeight || 800) * 0.22);
    const baseY = Math.max(0, Math.floor(top));
    window.scrollTo(0, baseY);
    dispatchLazyEvents();
    try { el.setAttribute("data-bby-compare-observer-hit", String(Date.now())); } catch (e) {}
    window.__bbyCompareDomObserverHits += 1;
    debug.compareTextFound = true;
    record("compare_text_found", {
      tag: el.tagName || "",
      text: norm(el.innerText || el.textContent || "").slice(0, 160),
      y: baseY
    });
    jiggleAround(baseY, "compare_text_found");
    return true;
  }

  window.__bbyCompareScrollToText = function(fallbackFraction) {
    const node = findCompareNode();
    if (scrollToNode(node)) return true;
    window.scrollTo(0, Math.max(0, Math.floor(pageHeight() * Number(fallbackFraction || 0.32))));
    debug.fallbackScrolls += 1;
    dispatchLazyEvents();
    record("fallback_scroll", { fraction: Number(fallbackFraction || 0.32) });
    return false;
  };

  window.__bbyCompareStartScan = function(points, delay) {
    const scanPoints = Array.isArray(points) && points.length ? points : [0.18,0.24,0.30,0.36,0.43,0.53,0.62,0.72,0.58,0.44,0.32,0.24];
    const scanDelay = Number(delay || 650);
    if (Array.isArray(window.__bbyCompareScanTimers)) {
      window.__bbyCompareScanTimers.forEach((timer) => clearTimeout(timer));
    }
    window.__bbyCompareScanTimers = [];
    scanPoints.forEach((point, index) => {
      const timer = setTimeout(() => {
        if (window.__bbyCompareScrollToText(point)) return;
        window.scrollTo(0, Math.max(0, Math.floor(pageHeight() * point)));
        try { window.dispatchEvent(new Event("scroll")); } catch (e) {}
      }, index * scanDelay);
      window.__bbyCompareScanTimers.push(timer);
    });
  };

  let debounce = null;
  const pulse = () => window.__bbyCompareScrollToText(0.32);
  try {
    const observer = new MutationObserver(() => {
      clearTimeout(debounce);
      debounce = setTimeout(pulse, 160);
    });
    observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    window.__bbyCompareDomObserver = observer;
  } catch (e) {}
  [600, 1600, 3200, 5600, 8600, 12500, 16500, 20500].forEach((ms) => setTimeout(pulse, ms));
  publishDebug();
})();
"""


def detail_scroll_to_text_script(keywords, fallback_fraction):
    return f"""
(() => {{
  const keywords = {json.dumps([keyword.lower() for keyword in keywords])};
  const fallbackFraction = {float(fallback_fraction)};
  const maxTextLength = 1800;
  const selectors = "h1,h2,h3,h4,section,[data-testid],div";
  function norm(value) {{
    return String(value || "").replace(/\\s+/g, " ").trim().toLowerCase();
  }}
  function height() {{
    return Math.max(
      document.documentElement.scrollHeight || 0,
      document.body ? document.body.scrollHeight || 0 : 0,
      window.innerHeight || 0
    );
  }}
  let best = null;
  let bestScore = -1;
  for (const el of Array.from(document.querySelectorAll(selectors))) {{
    const text = norm(el.innerText || el.textContent || "");
    if (!text || text.length > maxTextLength) continue;
    let score = 0;
    for (const keyword of keywords) {{
      if (text.indexOf(keyword) !== -1) score += 1000 - Math.min(text.length, 900);
    }}
    if (!score) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    if (/^H[1-4]$/.test(el.tagName)) score += 200;
    if (score > bestScore) {{
      bestScore = score;
      best = el;
    }}
  }}
  if (best) {{
    const top = best.getBoundingClientRect().top + window.scrollY - Math.floor((window.innerHeight || 800) * 0.22);
    window.scrollTo(0, Math.max(0, Math.floor(top)));
    try {{ best.setAttribute("data-bby-compare-scroll-hit", keywords.join("|")); }} catch (e) {{}}
  }} else {{
    window.scrollTo(0, Math.max(0, Math.floor(height() * fallbackFraction)));
  }}
  try {{ window.dispatchEvent(new Event("scroll")); }} catch (e) {{}}
}})();
"""


def detail_compare_force_fetch_script(fallback_sku=""):
    payload_json = json.dumps(compare_product_payload("__BBY_SKU__"), ensure_ascii=False, separators=(",", ":"))
    fallback_sku_json = json.dumps(str(fallback_sku or ""), ensure_ascii=False)
    return f"""
(() => {{
  if (window.__bbyCompareForceFetchStarted) return;
  window.__bbyCompareForceFetchStarted = true;

  const captureId = "bby-compare-capture";
  const debugId = "bby-compare-force-fetch-debug";
  const payloadTemplate = {payload_json};
  const fallbackSku = {fallback_sku_json};

  function readJson(text, fallback) {{
    try {{ return JSON.parse(text); }} catch (e) {{ return fallback; }}
  }}

  function ensureTextarea(id) {{
    let el = document.getElementById(id);
    if (!el) {{
      el = document.createElement("textarea");
      el.id = id;
      el.style.display = "none";
      el.setAttribute("aria-hidden", "true");
      (document.body || document.documentElement).appendChild(el);
    }}
    return el;
  }}

  function publishDebug(data) {{
    try {{
      const el = ensureTextarea(debugId);
      const text = JSON.stringify(Object.assign({{ at: new Date().toISOString() }}, data));
      el.value = text;
      el.textContent = text;
    }} catch (e) {{}}
  }}

  function publishCapture(entry) {{
    try {{
      const el = ensureTextarea(captureId);
      let existing = readJson(el.value || el.textContent || "[]", []);
      if (existing && !Array.isArray(existing) && Array.isArray(existing.captures)) existing = existing.captures;
      if (!Array.isArray(existing)) existing = [];
      existing.push(entry);
      existing = existing.slice(-12);
      const text = JSON.stringify(existing);
      el.value = text;
      el.textContent = text;
      window.__bbyCompareCaptureCount = existing.length;
    }} catch (e) {{
      publishDebug({{ ok: false, error: "publishCapture:" + String(e) }});
    }}
  }}

  function skuFromPage() {{
    try {{
      const meta = document.querySelector('meta[name="analytics-metadata"]');
      const raw = meta && meta.getAttribute("content");
      if (raw) {{
        const parsed = JSON.parse(raw.replace(/&quot;/g, '"'));
        const sku = parsed && parsed.product && parsed.product.skuId;
        if (sku) return String(sku);
      }}
    }} catch (e) {{}}
    try {{
      const candidates = [
        document.querySelector("[data-sku-id]"),
        document.querySelector("[data-sku]"),
        document.querySelector("[data-product-sku]")
      ];
      for (const el of candidates) {{
        if (!el) continue;
        const sku = el.getAttribute("data-sku-id") || el.getAttribute("data-sku") || el.getAttribute("data-product-sku");
        if (sku) return String(sku);
      }}
    }} catch (e) {{}}
    try {{
      const text = (document.body && document.body.innerText) || "";
      const match = text.match(/\\bSKU\\s*:?\\s*([0-9]{{4,}})\\b/i);
      if (match) return match[1];
    }} catch (e) {{}}
    try {{
      const match = String(location.href).match(/\\/sku\\/([0-9]{{4,}})/i);
      if (match) return match[1];
    }} catch (e) {{}}
    try {{
      const scripts = Array.from(document.scripts || []);
      for (const script of scripts) {{
        const text = script && script.textContent || "";
        if (!text || text.indexOf("skuId") === -1) continue;
        const match = text.match(/["']skuId["']\\s*:\\s*["']?([0-9]{{4,}})["']?/i);
        if (match) return match[1];
      }}
    }} catch (e) {{}}
    if (fallbackSku && /^[0-9]{{4,}}$/.test(String(fallbackSku))) return String(fallbackSku);
    return "";
  }}

  const skuId = skuFromPage();
  if (!skuId) {{
    publishDebug({{ ok: false, error: "sku_not_found" }});
    return;
  }}

  const payload = JSON.parse(JSON.stringify(payloadTemplate));
  payload.variables.skuId = skuId;
  const requestBody = JSON.stringify(payload);

  fetch("/gateway/graphql", {{
    method: "POST",
    credentials: "include",
    headers: {{
      "accept": "application/graphql-response+json,application/json;q=0.9",
      "content-type": "application/json",
      "x-client-id": "pdp-web",
      "x-requested-for-operation-name": "GetCompareProduct"
    }},
    body: requestBody
  }})
    .then(async response => {{
      const responseBody = await response.text();
      publishCapture({{
        source: "inrender_get_compare",
        method: "POST",
        status_code: response.status,
        url: "/gateway/graphql",
        requestBody,
        body: responseBody
      }});
      publishDebug({{
        ok: response.ok,
        skuId,
        status: response.status,
        bodyLength: responseBody.length
      }});
    }})
    .catch(error => {{
      publishDebug({{ ok: false, skuId, error: String(error) }});
    }});
}})();
"""


def detail_js_instructions(attempt=1, sku=""):
    compare_keywords = ["Compare similar products", "Compare similar", "Similar products"]
    settle = [{"wait_event": "networkalmostidle"}] if DETAIL_SCROLL_NETWORK_IDLE else []
    instructions = [
        *([{"evaluate": detail_compare_capture_hook_script()}] if DETAIL_COMPARE_CAPTURE_HOOK else []),
        *([{"evaluate": detail_compare_dom_observer_script()}] if DETAIL_COMPARE_DOM_OBSERVER else []),
        {"wait": 1200},
        *(
            [
                {"evaluate": detail_compare_force_fetch_script(sku)},
                {"wait": DETAIL_COMPARE_FORCE_FETCH_WAIT},
            ]
            if DETAIL_COMPARE_FORCE_FETCH
            else []
        ),
        *(
            [
                {"evaluate": "window.__bbyCompareStartScan && window.__bbyCompareStartScan([0.18,0.24,0.30,0.36,0.43,0.53,0.62,0.72,0.58,0.44,0.32,0.24],650);"},
                {"wait": 6500},
            ]
            if DETAIL_COMPARE_SCROLL_SCAN
            else []
        ),
        *settle,
        {"evaluate": "window.__bbyCompareScrollToText ? window.__bbyCompareScrollToText(0.32) : null;"},
        {"wait": 3000},
        *settle,
        {"evaluate": "window.__bbyCompareScrollToText ? window.__bbyCompareScrollToText(0.43) : null;"},
        {"wait": 2200},
        *settle,
        {"evaluate": "window.__bbyCompareScrollToText ? window.__bbyCompareScrollToText(0.25) : null;"},
        {"wait": 1800},
        *settle,
    ]

    instructions.extend(
        [
            {"scroll_y": 1800},
            {"wait": 800},
            {"scroll_y": 1800},
            {"wait": 800},
            {"scroll_y": 2200},
            {"wait": 900},
            {"scroll_y": 2200},
            {"wait": 900},
            {"scroll_y": 2200},
            {"wait": 900},
            {"wait": 1500},
        ]
    )
    return instructions


def detail_params(attempt=1, sku=""):
    params = {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "us",
    }
    if DETAIL_SCROLL:
        params["js_instructions"] = json.dumps(detail_js_instructions(attempt, sku))
    elif DETAIL_JSON_RESPONSE:
        params["wait"] = DETAIL_JSON_WAIT
    if DETAIL_JSON_RESPONSE:
        params["json_response"] = "true"
    return params


def graphql_params():
    return {
        "custom_headers": "true",
        "premium_proxy": "true",
        "proxy_country": "us",
        "js_render": "true",
    }


def fetch_transports():
    if FETCH_MODE in {"zenrows", "zr"}:
        return ["zenrows"]
    if FETCH_MODE in {"browser_graphql", "browser"}:
        return ["browser_graphql"]
    raise RuntimeError(
        "Best Buy detail collection supports BESTBUY_FETCH_MODE=zenrows or browser_graphql."
    )


def browser_graphql_enabled():
    return FETCH_MODE in {"browser_graphql", "browser"}


def open_detail_browser_page():
    global BROWSER_GRAPHQL_PAGE, BROWSER_GRAPHQL_META
    if BROWSER_GRAPHQL_PAGE is not None:
        return BROWSER_GRAPHQL_PAGE
    BROWSER_GRAPHQL_PAGE, BROWSER_GRAPHQL_META = create_browser_page(
        run_root=DETAIL_ROOT,
        name=f"detail_{STAGE}_browser_graphql",
        headless=BROWSER_GRAPHQL_HEADLESS,
        local_port=BROWSER_GRAPHQL_LOCAL_PORT,
    )
    return BROWSER_GRAPHQL_PAGE


def close_detail_browser_page():
    global BROWSER_GRAPHQL_PAGE, BROWSER_GRAPHQL_CURRENT_URL
    close_browser_page(BROWSER_GRAPHQL_PAGE)
    BROWSER_GRAPHQL_PAGE = None
    BROWSER_GRAPHQL_CURRENT_URL = ""


def browser_graphql_post(payload, referer_url):
    global BROWSER_GRAPHQL_CURRENT_URL
    if BROWSER_GRAPHQL_PAGE is None:
        raise RuntimeError("browser_graphql page is not initialized")
    with BROWSER_GRAPHQL_LOCK:
        browser_url = add_intl_nosplash(referer_url or BROWSER_GRAPHQL_CURRENT_URL or "https://www.bestbuy.com/")
        navigated = False
        if not BROWSER_GRAPHQL_CURRENT_URL:
            BROWSER_GRAPHQL_PAGE.get(browser_url)
            BROWSER_GRAPHQL_CURRENT_URL = browser_url
            navigated = True
        if navigated and BROWSER_GRAPHQL_WAIT_SECONDS:
            time.sleep(BROWSER_GRAPHQL_WAIT_SECONDS)
        start = time.perf_counter()
        envelope = browser_fetch_graphql(
            BROWSER_GRAPHQL_PAGE,
            payload,
            timeout=BROWSER_GRAPHQL_JS_TIMEOUT,
        )
        elapsed = round(time.perf_counter() - start, 3)
    status_code = int(envelope.get("status") or 0)
    text = str(envelope.get("body") or "")
    try:
        response_json = json.loads(text)
    except ValueError:
        response_json = {}
    headers = {
        "content-type": envelope.get("contentType", ""),
        "transport": "browser_graphql",
        "browser_url": BROWSER_GRAPHQL_CURRENT_URL or browser_url,
        "browser_referer_url": browser_url,
        "browser_navigated": "1" if navigated else "0",
    }
    return status_code, text, response_json, headers, elapsed


def load_csv(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, preferred=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = set()
    for row in rows:
        keys.update(row)
    fieldnames = [key for key in (preferred or []) if key in keys]
    fieldnames.extend(sorted(keys - set(fieldnames)))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def csv_fields(path, rows):
    header = []
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                pass
    keys = list(header)
    seen = set(header)
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def read_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return {}


PRODUCT_LIST_DETAIL_FIELD_SOURCES = {
    "retailer_sku_name": ("retailer_sku_name",),
    "final_sku_price": ("final_sku_price",),
    "savings": ("savings",),
    "comparable_pricing": ("original_sku_price", "comparable_pricing"),
    "offer": ("offer", "offer_count"),
    "pick_up_availability": ("pick_up_availability",),
    "fastest_delivery": ("fastest_delivery",),
    "delivery_availability": ("delivery_availability",),
    "sku_status": ("sku_status",),
    "promotion_type": ("promotion_type",),
    "product_url": ("product_url",),
    "calendar_week": ("calendar_week",),
    "batch_id": ("batch_id",),
}
if CATEGORY == "TV":
    PRODUCT_LIST_DETAIL_FIELD_SOURCES["crawl_datetime"] = ("crawl_datetime",)
else:
    PRODUCT_LIST_DETAIL_FIELD_SOURCES["crawl_strdatetime"] = ("crawl_strdatetime",)


PRODUCT_LIST_CLEARABLE_PRICE_FIELDS = {"savings", "comparable_pricing"}


PRESERVE_EXISTING_AVAILABILITY = os.getenv(
    "BESTBUY_DETAIL_PRESERVE_EXISTING_AVAILABILITY",
    "1",
).lower() in {"1", "true", "yes", "y"}


def row_sku_key(row):
    return first_non_empty(
        row.get("sku_id"),
        row.get("sku"),
        sku_from_product_url(row.get("product_url")),
        row.get("item"),
        row.get("bsin"),
    )


def row_identity_keys(row):
    keys = []
    for value in (
        row.get("sku_id"),
        row.get("sku"),
        sku_from_product_url(row.get("product_url")),
        row.get("item"),
        row.get("bsin"),
        canonical_pdp_url(row.get("product_url")),
    ):
        text = compact_text(value)
        if text and text not in keys:
            keys.append(text)
    return keys


def add_availability_source(existing_by_key, source_rows):
    for row in source_rows:
        has_availability = any(compact_text(row.get(field)) for field in ALL_AVAILABILITY_FIELDS)
        if not has_availability:
            continue
        for key in row_identity_keys(row):
            existing_by_key.setdefault(key, row)


def preserve_existing_availability(rows):
    if not PRESERVE_EXISTING_AVAILABILITY or not FINAL_OUTPUT_CSV.exists() or not rows:
        return 0
    existing_by_key = {}
    add_availability_source(existing_by_key, load_csv(FINAL_OUTPUT_CSV))
    if PRODUCT_LIST_CSV.exists():
        add_availability_source(existing_by_key, load_csv(PRODUCT_LIST_CSV))

    updated = 0
    for row in rows:
        existing = None
        for key in row_identity_keys(row):
            existing = existing_by_key.get(key)
            if existing:
                break
        if not existing:
            continue
        row_changed = False
        for field in ALL_AVAILABILITY_FIELDS:
            if not compact_text(row.get(field)) and compact_text(existing.get(field)):
                row[field] = existing.get(field, "")
                row_changed = True
        if row_changed:
            updated += 1
    return updated


def update_product_list_from_detail_rows(detail_rows):
    product_rows = load_csv(PRODUCT_LIST_CSV)
    if not product_rows or not detail_rows:
        return {"rows": 0, "updated": 0, "fields": 0}

    detail_by_sku = {}
    for row in detail_rows:
        sku = row_sku_key(row)
        if sku:
            detail_by_sku.setdefault(str(sku), row)

    updated = 0
    changed_fields = 0
    for row in product_rows:
        sku = row_sku_key(row)
        detail = detail_by_sku.get(str(sku)) if sku else None
        if not detail:
            continue
        row_changed = False
        for field, sources in PRODUCT_LIST_DETAIL_FIELD_SOURCES.items():
            value = first_non_empty(*(detail.get(source) for source in sources))
            if field in PRODUCT_LIST_CLEARABLE_PRICE_FIELDS and compact_text(detail.get("final_sku_price")):
                desired = value or ""
                if compact_text(row.get(field)) != compact_text(desired):
                    row[field] = desired
                    row_changed = True
                    changed_fields += 1
                continue
            if value and compact_text(row.get(field)) != compact_text(value):
                row[field] = value
                row_changed = True
                changed_fields += 1
        if row_changed:
            updated += 1

    fields = csv_fields(PRODUCT_LIST_CSV, product_rows)
    if CATEGORY == "TV":
        fields = [field for field in fields if field != "crawl_strdatetime"]
        for row in product_rows:
            row.pop("crawl_strdatetime", None)
    current_fields = csv_fields(PRODUCT_LIST_CSV, product_rows)
    if changed_fields or fields != current_fields:
        write_csv(PRODUCT_LIST_CSV, product_rows, fields)
    return {"rows": len(product_rows), "updated": updated, "fields": changed_fields}


def safe_part(value, default="na"):
    value = re.sub(r"[^0-9A-Za-z_-]+", "_", str(value or "").strip()).strip("_")
    return value or default


def detail_rank(target):
    if isinstance(target, dict):
        return safe_part(target.get("main_rank") or target.get("final_target_rank") or target.get("bsr_rank") or target.get("rank") or "na")
    return "na"


def existing_detail_dirs(sku):
    pattern = f"*_{safe_part(sku)}_*"
    dirs = []
    for path in RAW_DETAIL_DIR.glob(pattern):
        if path.is_dir() and (path / f"{sku}_meta.json").exists():
            dirs.append(path)
    return sorted(
        dirs,
        key=lambda path: (
            0 if path.name.endswith("_success") else 1 if path.name.endswith("_fail") else 2,
            path.name,
        ),
    )


def legacy_detail_paths(sku):
    return {
        "html": RAW_DETAIL_DIR / f"{sku}.html",
        "headers": RAW_DETAIL_DIR / f"{sku}_headers.json",
        "apollo": RAW_DETAIL_DIR / f"{sku}_apollo.json",
        "json_response": RAW_DETAIL_DIR / f"{sku}_json_response.json",
        "json_response_summary": RAW_DETAIL_DIR / f"{sku}_json_response_summary.json",
        "fulfillment_response": RAW_DETAIL_DIR / f"{sku}_fulfillment_response.json",
        "fulfillment_meta": RAW_DETAIL_DIR / f"{sku}_fulfillment_meta.json",
        "meta": RAW_DETAIL_DIR / f"{sku}_meta.json",
    }


def detail_folder(sku, target=None, status=None):
    sku_part = safe_part(sku)
    desired = None
    if status:
        desired = RAW_DETAIL_DIR / f"{detail_rank(target)}_{sku_part}_{safe_part(status)}"

    existing = existing_detail_dirs(sku)
    if desired:
        if existing and desired not in existing:
            if not desired.exists():
                existing[0].rename(desired)
            else:
                for old_dir in existing:
                    if old_dir == desired:
                        continue
                    for old_file in old_dir.iterdir():
                        new_file = desired / old_file.name
                        if not new_file.exists():
                            old_file.rename(new_file)
        desired.mkdir(parents=True, exist_ok=True)
        if status == "success":
            for old_dir in existing_detail_dirs(sku):
                if old_dir != desired and old_dir.name.endswith("_fail"):
                    shutil.rmtree(old_dir, ignore_errors=True)
        return desired
    if existing:
        return existing[0]
    return None


def existing_review_dirs(sku):
    pattern = f"*_{safe_part(sku)}_*"
    dirs = []
    for path in RAW_REVIEW_DIR.glob(pattern):
        if path.is_dir() and (path / f"{sku}_meta.json").exists():
            dirs.append(path)
    return sorted(
        dirs,
        key=lambda path: (
            0 if path.name.endswith("_success") else 1 if path.name.endswith("_fail") else 2,
            path.name,
        ),
    )


def review_folder(sku, target=None, status=None):
    sku_part = safe_part(sku)
    desired = None
    if status:
        desired = RAW_REVIEW_DIR / f"{detail_rank(target)}_{sku_part}_{safe_part(status)}"

    existing = existing_review_dirs(sku)
    if desired:
        if existing and desired not in existing:
            if not desired.exists():
                existing[0].rename(desired)
            else:
                for old_dir in existing:
                    if old_dir == desired:
                        continue
                    for old_file in old_dir.iterdir():
                        new_file = desired / old_file.name
                        if not new_file.exists():
                            old_file.rename(new_file)
        desired.mkdir(parents=True, exist_ok=True)
        if status == "success":
            for old_dir in existing_review_dirs(sku):
                if old_dir != desired and old_dir.name.endswith("_fail"):
                    shutil.rmtree(old_dir, ignore_errors=True)
        return desired
    if existing:
        return existing[0]
    return None


def existing_compare_dirs(sku):
    pattern = f"*_{safe_part(sku)}_*"
    dirs = []
    for path in RAW_COMPARE_DIR.glob(pattern):
        if path.is_dir() and (path / f"{sku}_meta.json").exists():
            dirs.append(path)
    return sorted(
        dirs,
        key=lambda path: (
            0 if path.name.endswith("_success") else 1 if path.name.endswith("_fail") else 2,
            path.name,
        ),
    )


def compare_folder(sku, target=None, status=None):
    sku_part = safe_part(sku)
    desired = None
    if status:
        desired = RAW_COMPARE_DIR / f"{detail_rank(target)}_{sku_part}_{safe_part(status)}"

    existing = existing_compare_dirs(sku)
    if desired:
        if existing and desired not in existing:
            if not desired.exists():
                existing[0].rename(desired)
            else:
                for old_dir in existing:
                    if old_dir == desired:
                        continue
                    for old_file in old_dir.iterdir():
                        new_file = desired / old_file.name
                        if not new_file.exists():
                            old_file.rename(new_file)
        desired.mkdir(parents=True, exist_ok=True)
        if status == "success":
            for old_dir in existing_compare_dirs(sku):
                if old_dir != desired and old_dir.name.endswith("_fail"):
                    shutil.rmtree(old_dir, ignore_errors=True)
        return desired
    if existing:
        return existing[0]
    return None


def detail_paths(sku):
    folder = detail_folder(sku)
    if folder:
        return {
            "html": folder / f"{sku}.html",
            "headers": folder / f"{sku}_headers.json",
            "apollo": folder / f"{sku}_apollo.json",
            "json_response": folder / f"{sku}_json_response.json",
            "json_response_summary": folder / f"{sku}_json_response_summary.json",
            "fulfillment_response": folder / f"{sku}_fulfillment_response.json",
            "fulfillment_meta": folder / f"{sku}_fulfillment_meta.json",
            "meta": folder / f"{sku}_meta.json",
        }
    legacy = legacy_detail_paths(sku)
    if any(path.exists() for path in legacy.values()):
        return legacy
    folder = RAW_DETAIL_DIR / f"na_{safe_part(sku)}_pending"
    return {
        "html": folder / f"{sku}.html",
        "headers": folder / f"{sku}_headers.json",
        "apollo": folder / f"{sku}_apollo.json",
        "json_response": folder / f"{sku}_json_response.json",
        "json_response_summary": folder / f"{sku}_json_response_summary.json",
        "fulfillment_response": folder / f"{sku}_fulfillment_response.json",
        "fulfillment_meta": folder / f"{sku}_fulfillment_meta.json",
        "meta": folder / f"{sku}_meta.json",
    }


def detail_paths_for_status(sku, target, success):
    folder = detail_folder(sku, target, "success" if success else "fail")
    return {
        "html": folder / f"{sku}.html",
        "headers": folder / f"{sku}_headers.json",
        "apollo": folder / f"{sku}_apollo.json",
        "json_response": folder / f"{sku}_json_response.json",
        "json_response_summary": folder / f"{sku}_json_response_summary.json",
        "fulfillment_response": folder / f"{sku}_fulfillment_response.json",
        "fulfillment_meta": folder / f"{sku}_fulfillment_meta.json",
        "meta": folder / f"{sku}_meta.json",
    }


def review_paths(sku):
    folder = review_folder(sku)
    if folder:
        return {
            "request": folder / f"{sku}_request.json",
            "response_txt": folder / f"{sku}_response.txt",
            "response_json": folder / f"{sku}_response.json",
            "headers": folder / f"{sku}_headers.json",
            "meta": folder / f"{sku}_meta.json",
        }
    legacy = {
        "request": RAW_REVIEW_DIR / f"{sku}_request.json",
        "response_txt": RAW_REVIEW_DIR / f"{sku}_response.txt",
        "response_json": RAW_REVIEW_DIR / f"{sku}_response.json",
        "headers": RAW_REVIEW_DIR / f"{sku}_headers.json",
        "meta": RAW_REVIEW_DIR / f"{sku}_meta.json",
    }
    if any(path.exists() for path in legacy.values()):
        return legacy
    folder = RAW_REVIEW_DIR / f"na_{safe_part(sku)}_pending"
    return {
        "request": folder / f"{sku}_request.json",
        "response_txt": folder / f"{sku}_response.txt",
        "response_json": folder / f"{sku}_response.json",
        "headers": folder / f"{sku}_headers.json",
        "meta": folder / f"{sku}_meta.json",
    }


def review_paths_for_status(sku, target, success):
    folder = review_folder(sku, target, "success" if success else "fail")
    return {
        "request": folder / f"{sku}_request.json",
        "response_txt": folder / f"{sku}_response.txt",
        "response_json": folder / f"{sku}_response.json",
        "headers": folder / f"{sku}_headers.json",
        "meta": folder / f"{sku}_meta.json",
    }


def compare_paths(sku):
    folder = compare_folder(sku)
    if folder:
        return {
            "request": folder / f"{sku}_request.json",
            "response_txt": folder / f"{sku}_response.txt",
            "response_json": folder / f"{sku}_response.json",
            "headers": folder / f"{sku}_headers.json",
            "meta": folder / f"{sku}_meta.json",
        }
    folder = RAW_COMPARE_DIR / f"na_{safe_part(sku)}_pending"
    return {
        "request": folder / f"{sku}_request.json",
        "response_txt": folder / f"{sku}_response.txt",
        "response_json": folder / f"{sku}_response.json",
        "headers": folder / f"{sku}_headers.json",
        "meta": folder / f"{sku}_meta.json",
    }


def compare_paths_for_status(sku, target, success):
    folder = compare_folder(sku, target, "success" if success else "fail")
    return {
        "request": folder / f"{sku}_request.json",
        "response_txt": folder / f"{sku}_response.txt",
        "response_json": folder / f"{sku}_response.json",
        "headers": folder / f"{sku}_headers.json",
        "meta": folder / f"{sku}_meta.json",
    }


def target_url(target, sku):
    if target.get("target_source") == "promotion_backfill" and sku:
        return old_pdp_url(sku)
    url = fetchable_pdp_url(target.get("product_url"))
    # PDP URL fallback is intentionally disabled for sponsored enrichment.
    # Sponsored rows should be resolved first via productsBySkuIds in step02.
    # Keep this only as a last-resort detail/review fallback for explicit PDP runs.
    return url or old_pdp_url(sku)


def has_product_schema(html_text):
    return "ProductSchema_init" in html_text and "productBySkuId" in html_text


def apollo_payloads_json(html_text):
    try:
        return extract_apollo_payloads(html_text)
    except Exception:
        return []


def slim_html(html_text):
    soup = BeautifulSoup(html_text or "", "html.parser")
    for tag in soup.find_all(["style", "link", "svg", "noscript", "iframe"]):
        tag.decompose()
    for script in list(soup.find_all("script")):
        script_type = (script.get("type") or "").lower()
        text = script.string or script.get_text() or ""
        keep = "ApolloSSRDataTransport" not in text and script_type == "application/ld+json"
        if not keep:
            script.decompose()
            continue
        for attr in list(script.attrs):
            if attr not in {"type", "id"}:
                del script.attrs[attr]
    return str(soup)


def stored_html(html_text):
    if SAVE_HTML_MODE == "none":
        return ""
    if SAVE_HTML_MODE == "full":
        return html_text
    return slim_html(html_text)


def write_detail_artifacts(paths, html_text, headers):
    payloads = apollo_payloads_json(html_text)
    paths["apollo"].write_text(json.dumps(payloads, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    stored = stored_html(html_text)
    if SAVE_HTML_MODE == "none":
        if paths["html"].exists():
            paths["html"].unlink()
    else:
        paths["html"].write_text(stored, encoding="utf-8", errors="replace")
    paths["headers"].write_text(json.dumps(headers, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "html_mode": SAVE_HTML_MODE,
        "full_bytes": len(html_text or ""),
        "stored_bytes": len(stored or ""),
        "apollo_payload_count": len(payloads),
    }


def parse_json_value(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        return json.loads(value)
    except ValueError:
        return {}


def body_preview(value, limit=500):
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return compact_text(text)[:limit]


def strict_compare_response_names(body_data, sku="", allowed_sku_ids=None):
    if allowed_sku_ids is None:
        allowed_sku_ids = {str(sku)} if sku else set()
    if isinstance(body_data, list):
        names = []
        for item in body_data:
            for name in strict_compare_response_names(item, sku, allowed_sku_ids):
                if name and name not in names:
                    names.append(name)
        return names
    if not isinstance(body_data, dict):
        return []

    data = body_data.get("data")
    if not isinstance(data, dict):
        return []
    if not isinstance(data.get("recommendations"), dict) and not isinstance(data.get("recommendationsV2"), dict):
        return []

    product = data.get("productBySkuId")
    if product is not None and not isinstance(product, dict):
        return []
    if isinstance(product, dict) and allowed_sku_ids and str(product.get("skuId") or "") not in allowed_sku_ids:
        return []
    return compare_recommendation_names(data)


def strict_compare_response_data(body_data, sku="", allowed_sku_ids=None):
    if allowed_sku_ids is None:
        allowed_sku_ids = {str(sku)} if sku else set()
    if isinstance(body_data, list):
        for item in body_data:
            data = strict_compare_response_data(item, sku, allowed_sku_ids)
            if data:
                return data
        return {}
    if not isinstance(body_data, dict):
        return {}

    data = body_data.get("data")
    if not isinstance(data, dict):
        return {}
    if not isinstance(data.get("recommendations"), dict) and not isinstance(data.get("recommendationsV2"), dict):
        return {}

    product = data.get("productBySkuId")
    if product is not None and not isinstance(product, dict):
        return {}
    if isinstance(product, dict) and allowed_sku_ids and str(product.get("skuId") or "") not in allowed_sku_ids:
        return {}
    return data


def compare_capture_entries_from_html(html_text):
    if not isinstance(html_text, str) or "bby-compare-capture" not in html_text:
        return []
    entries = []
    pattern = re.compile(
        r"<textarea\b(?=[^>]*\bid=[\"']bby-compare-capture[\"'])[^>]*>(.*?)</textarea>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html_text):
        raw = html.unescape(match.group(1) or "").strip()
        data = parse_json_value(raw)
        if isinstance(data, list):
            entries.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            captures = data.get("captures")
            if isinstance(captures, list):
                entries.extend(item for item in captures if isinstance(item, dict))
            else:
                entries.append(data)
    return entries


def compare_debug_from_html(html_text):
    if not isinstance(html_text, str) or "bby-compare-debug" not in html_text:
        return {}
    pattern = re.compile(
        r"<textarea\b(?=[^>]*\bid=[\"']bby-compare-debug[\"'])[^>]*>(.*?)</textarea>",
        re.IGNORECASE | re.DOTALL,
    )
    matches = list(pattern.finditer(html_text))
    if not matches:
        return {}
    raw = html.unescape(matches[-1].group(1) or "").strip()
    data = parse_json_value(raw)
    return data if isinstance(data, dict) else {}


def compare_force_fetch_debug_from_html(html_text):
    if not isinstance(html_text, str) or "bby-compare-force-fetch-debug" not in html_text:
        return {}
    pattern = re.compile(
        r"<textarea\b(?=[^>]*\bid=[\"']bby-compare-force-fetch-debug[\"'])[^>]*>(.*?)</textarea>",
        re.IGNORECASE | re.DOTALL,
    )
    matches = list(pattern.finditer(html_text))
    if not matches:
        return {}
    raw = html.unescape(matches[-1].group(1) or "").strip()
    data = parse_json_value(raw)
    return data if isinstance(data, dict) else {}


def sku_ids_from_html_text(html_text):
    if not isinstance(html_text, str) or not html_text:
        return set()
    sku_ids = set()
    for pattern in (
        r'"skuId"\s*:\s*"([0-9]{4,})"',
        r"&quot;skuId&quot;\s*:\s*&quot;([0-9]{4,})&quot;",
        r"\bSKU\s*:?\s*([0-9]{4,})\b",
        r"/sku/([0-9]{4,})",
    ):
        for match in re.finditer(pattern, html_text, re.IGNORECASE):
            sku_ids.add(str(match.group(1)))
    return sku_ids


def json_response_compare_summary(json_data, sku=""):
    xhr = (json_data.get("xhr") or []) if isinstance(json_data, dict) else []
    if not isinstance(xhr, list):
        xhr = []
    html_text = ""
    if isinstance(json_data, dict):
        html_text = json_data.get("html") or json_data.get("content") or ""
    allowed_sku_ids = detail_resolved_sku_ids(sku) if sku else set()
    allowed_sku_ids.update(sku_ids_from_html_text(html_text))
    hits = []
    names = []
    for request in xhr:
        if not isinstance(request, dict):
            continue
        request_blob = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        url = str(request.get("url") or "")
        if "GetCompareProduct" not in request_blob and "/gateway/graphql" not in url:
            continue
        body = request.get("body")
        body_data = parse_json_value(body)
        has_get_compare = (
            "GetCompareProduct" in request_blob
            or "single-compare" in request_blob
            or "ProductCarousel_Recommendations" in request_blob
            or "pdp-compare" in request_blob
        )
        request_names = strict_compare_response_names(body_data, sku, allowed_sku_ids)
        is_compare_response = bool(request_names)
        if not has_get_compare and not is_compare_response:
            request_names = []
        for name in request_names:
            if name and name not in names:
                names.append(name)
        hits.append(
            {
                "method": request.get("method", ""),
                "status_code": request.get("status_code", ""),
                "url": url,
                "has_get_compare": has_get_compare,
                "is_compare_response": is_compare_response,
                "name_count": len(request_names),
                "names": request_names[:5],
                "body_preview": body_preview(body),
            }
        )
    capture_entries = compare_capture_entries_from_html(html_text)
    compare_debug = compare_debug_from_html(html_text)
    force_fetch_debug = compare_force_fetch_debug_from_html(html_text)
    for entry in capture_entries:
        body = entry.get("body")
        body_data = parse_json_value(body)
        entry_blob = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        has_get_compare = (
            "GetCompareProduct" in entry_blob
            or "single-compare" in entry_blob
            or "ProductCarousel_Recommendations" in entry_blob
            or "pdp-compare" in entry_blob
        )
        entry_names = strict_compare_response_names(body_data, sku, allowed_sku_ids)
        is_compare_response = bool(entry_names)
        if not has_get_compare and not is_compare_response:
            continue
        for name in entry_names:
            if name and name not in names:
                names.append(name)
        hits.append(
            {
                "source": entry.get("source", "html_capture"),
                "method": entry.get("method", ""),
                "status_code": "html_capture",
                "url": entry.get("url", ""),
                "has_get_compare": has_get_compare,
                "is_compare_response": is_compare_response,
                "name_count": len(entry_names),
                "names": entry_names[:5],
                "body_preview": body_preview(body),
            }
        )
    return {
        "strict_compare_parser": True,
        "xhr_count": len(xhr),
        "html_compare_capture_count": len(capture_entries),
        "html_compare_debug": compare_debug,
        "html_compare_force_fetch_debug": force_fetch_debug,
        "graphql_or_compare_hit_count": len(hits),
        "compare_name_count": len(names),
        "compare_names": names[:20],
        "hits": hits[:20],
    }


def apollo_payload_from_graphql_response(payloads, response_json):
    if isinstance(payloads, dict):
        payloads = [payloads]
    response_items = response_json if isinstance(response_json, list) else [response_json]
    events = []
    for index, payload in enumerate(payloads or []):
        event_id = f"direct-graphql-{index + 1}"
        events.append(
            {
                "type": "started",
                "options": {
                    "variables": payload.get("variables", {}),
                    "errorPolicy": "ignore",
                    "fetchPolicy": "cache-first",
                    "query": payload.get("query", ""),
                    "notifyOnNetworkStatusChange": False,
                    "nextFetchPolicy": None,
                },
                "id": event_id,
            }
        )
        if index < len(response_items) and isinstance(response_items[index], dict):
            events.append({"type": "next", "value": response_items[index], "id": event_id})
        events.append({"type": "completed", "id": event_id})
    return [{"rehydrate": {}, "events": events}]


def write_direct_detail_artifacts(paths, payload, response_json, response_text, headers):
    apollo_payloads = apollo_payload_from_graphql_response(payload, response_json)
    paths["apollo"].write_text(
        json.dumps(apollo_payloads, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    if SAVE_HTML_MODE == "none":
        if paths["html"].exists():
            paths["html"].unlink()
        stored = ""
    else:
        stored = ""
        paths["html"].write_text(stored, encoding="utf-8")
    paths["headers"].write_text(json.dumps(headers, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "html_mode": "direct_graphql",
        "full_bytes": len(response_text or ""),
        "stored_bytes": len(stored or ""),
        "apollo_payload_count": len(apollo_payloads),
    }


def graphql_batch_response_item(response_json, index):
    if isinstance(response_json, list):
        return response_json[index] if index < len(response_json) and isinstance(response_json[index], dict) else {}
    if index == 0 and isinstance(response_json, dict):
        return response_json
    return {}


def compare_recommendations_from_response(response_json):
    data = response_json.get("data") if isinstance(response_json, dict) else {}
    return (
        first_path([data], ["recommendations", "subPlacements", 0, "recommendations"])
        or first_path([data], ["recommendationsV2", "subPlacements", 0, "recommendations"])
        or []
    )


def write_compare_response_artifacts(paths, payload, response_json, response_text, headers):
    paths["response_txt"].write_text(response_text or "", encoding="utf-8", errors="replace")
    if response_json:
        paths["response_json"].write_text(json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["headers"].write_text(json.dumps(headers, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["request"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_review_response_artifacts(paths, payload, response_json, response_text, headers):
    paths["response_txt"].write_text(response_text or "", encoding="utf-8", errors="replace")
    if response_json:
        paths["response_json"].write_text(json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["headers"].write_text(json.dumps(headers, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["request"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def detail_success(sku):
    paths = detail_paths(sku)
    meta = read_json(paths["meta"])
    if meta.get("success") is True and (paths["html"].exists() or paths["apollo"].exists()):
        return True
    return False


def review_success(sku):
    paths = review_paths(sku)
    meta = read_json(paths["meta"])
    if meta.get("success") is True and paths["response_json"].exists():
        return True
    if review_result_count(paths["response_json"]) is not None:
        return True
    return False


def review_result_count(path):
    data = read_json(path)
    return review_result_count_from_json(data)


def review_results_from_json(data):
    product = ((data.get("data") or {}).get("productBySkuId") or {}) if isinstance(data, dict) else {}
    reviews = (product.get("reviews") or {}).get("results") if isinstance(product, dict) else None
    return reviews if isinstance(reviews, list) else None


def review_result_count_from_json(data):
    reviews = review_results_from_json(data)
    if isinstance(reviews, list):
        return len(reviews)
    return None


def review_text_count_from_reviews(reviews):
    if not isinstance(reviews, list):
        return None
    return sum(1 for review in reviews[:MAX_REVIEW_TEXTS] if compact_text((review or {}).get("text")))


def review_text_count_from_json(data):
    reviews = review_results_from_json(data)
    return review_text_count_from_reviews(reviews)


def review_text_count_from_content(value):
    return len(re.findall(r"(?i)(?:^|\s)review\d+\s*-", str(value or "")))


def attempts(meta_path):
    return int(read_json(meta_path).get("attempt", 0) or 0)


def next_attempt(meta_path, url):
    meta = read_json(meta_path)
    previous_url = str(meta.get("url") or "").strip()
    if previous_url and previous_url != str(url or "").strip():
        return 1
    return int(meta.get("attempt", 0) or 0) + 1


def attempt_cap_blocks_retry(attempt):
    return not FORCE_REFRESH and not RETRY_ONLY and attempt > MAX_ATTEMPTS


def target_match_keys(row):
    keys = set()
    for field in ("sku_id", "bsin", "item"):
        value = str(row.get(field) or "").strip().lower()
        if value:
            keys.add(value)
    sku = sku_from_product_url(row.get("product_url")).lower()
    if sku:
        keys.add(sku)
    url = canonical_pdp_url(row.get("product_url"))
    if url:
        keys.add(url)
    return keys


def missing_similar_match_keys():
    rows = []
    source_path = None
    candidate_paths = []
    if RETRY_MISSING_SIMILAR_SOURCE_CSV:
        candidate_paths.append(Path(RETRY_MISSING_SIMILAR_SOURCE_CSV))
    candidate_paths.extend([FINAL_OUTPUT_CSV, DETAIL_ROWS_CSV])
    for path in candidate_paths:
        if path.exists():
            rows = load_csv(path)
            source_path = path
            if rows:
                break
    if not rows:
        raise RuntimeError(
            "BESTBUY_DETAIL_RETRY_MISSING_SIMILAR=1 requires existing final_output.csv or detail_enriched_rows.csv"
        )

    missing_rows = [row for row in rows if not str(row.get("retailer_sku_name_similar") or "").strip()]
    if missing_rows and len(missing_rows) == len(rows) and not truthy(
        os.getenv("BESTBUY_DETAIL_RETRY_MISSING_SIMILAR_ALLOW_ALL")
    ):
        raise RuntimeError(
            "All existing rows are missing retailer_sku_name_similar; refusing broad retry. "
            "Set BESTBUY_DETAIL_RETRY_MISSING_SIMILAR_ALLOW_ALL=1 to override."
        )
    keys = set()
    for row in missing_rows:
        keys.update(target_match_keys(row))
    print(
        format_log_line(
            "detail:retry_scope",
            source=rel_path(source_path),
            missing_rows=len(missing_rows),
            match_keys=len(keys),
        ),
        flush=True,
    )
    return keys


def target_rows(apply_filters=True):
    rows = load_csv(TARGET_CSV)
    unique = []
    seen = set()
    for row in rows:
        sku = str(row.get("sku_id") or "").strip()
        if not sku or sku in seen:
            continue
        seen.add(sku)
        unique.append(row)
    if apply_filters and TARGET_SKUS:
        unique = [
            row
            for row in unique
            if str(row.get("sku_id") or "").strip().lower() in TARGET_SKUS
            or str(row.get("bsin") or "").strip().lower() in TARGET_SKUS
            or str(row.get("item") or "").strip().lower() in TARGET_SKUS
            or sku_from_product_url(row.get("product_url")).lower() in TARGET_SKUS
        ]
    if apply_filters and RETRY_MISSING_SIMILAR:
        missing_keys = missing_similar_match_keys()
        unique = [row for row in unique if target_match_keys(row) & missing_keys]
    if apply_filters and RETRY_ONLY:
        if STAGE == "detail":
            unique = [row for row in unique if not detail_success(row["sku_id"])]
        elif STAGE == "review":
            unique = [
                row
                for row in unique
                if review_needs_retry(row)
            ]
        else:
            unique = [
                row
                for row in unique
                if not detail_success(row["sku_id"])
                or review_needs_retry(row)
            ]
    if apply_filters and LIMIT:
        unique = unique[:LIMIT]
    return unique


def find_started_operation(html_text, operation_name):
    for payload in extract_apollo_payloads(html_text):
        for event in payload.get("events", []):
            if event.get("type") != "started":
                continue
            options = event.get("options", {})
            query = options.get("query") or ""
            if query.startswith(f"query {operation_name}") or f"query {operation_name}(" in query:
                return {
                    "operationName": operation_name,
                    "variables": options.get("variables", {}),
                    "query": query,
                }
    return None


def find_started_operation_from_payloads(payloads, operation_name):
    for payload in payloads:
        for event in payload.get("events", []):
            if event.get("type") != "started":
                continue
            options = event.get("options", {})
            query = options.get("query") or ""
            if query.startswith(f"query {operation_name}") or f"query {operation_name}(" in query:
                return {
                    "operationName": operation_name,
                    "variables": options.get("variables", {}),
                    "query": query,
                }
    return None


def operation_name(event):
    options = event.get("options", {}) if isinstance(event, dict) else {}
    query = options.get("query") or ""
    if not isinstance(query, str):
        return ""
    match = re.search(r"\bquery\s+([A-Za-z0-9_]+)", query)
    return match.group(1) if match else ""


def event_variables(event):
    options = event.get("options", {}) if isinstance(event, dict) else {}
    variables = options.get("variables") or {}
    return variables if isinstance(variables, dict) else {}


def product_short_name(product):
    if not isinstance(product, dict):
        return ""
    name = product.get("name")
    if isinstance(name, dict):
        return first_non_empty(
            name.get("short"),
            name.get("title"),
            name.get("displayName"),
            name.get("name"),
        )
    return first_non_empty(
        name,
        product.get("shortName"),
        product.get("displayName"),
        product.get("title"),
        product.get("productName"),
        product.get("retailer_sku_name"),
    )


def product_names_from_value(value):
    names = []

    def add(name):
        name = compact_text(name)
        if name and name not in names:
            names.append(name)

    def visit(item):
        if isinstance(item, dict):
            add(product_short_name(item))
            for key in (
                "item",
                "product",
                "recommendedProduct",
                "catalogProduct",
                "sku",
                "node",
            ):
                child = item.get(key)
                if isinstance(child, (dict, list)):
                    visit(child)
            for key in ("items", "products", "nodes", "results"):
                child = item.get(key)
                if isinstance(child, list):
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return names


def detail_resolved_sku_ids(sku):
    sku_ids = {str(sku)}
    for payload in detail_payloads(sku):
        for event in payload.get("events", []):
            variables = event_variables(event)
            variable_sku = variables.get("skuId")
            if variable_sku not in (None, ""):
                sku_ids.add(str(variable_sku))
            data = event_data(event)
            product = data.get("productBySkuId") if isinstance(data, dict) else None
            if isinstance(product, dict) and product.get("skuId") not in (None, ""):
                sku_ids.add(str(product.get("skuId")))
    return sku_ids


def event_data(event):
    value = event.get("value") if isinstance(event, dict) else {}
    data = value.get("data") if isinstance(value, dict) and "data" in value else None
    if isinstance(data, dict):
        return data
    result = event.get("result") if isinstance(event, dict) else {}
    data = result.get("data") if isinstance(result, dict) and "data" in result else None
    return data if isinstance(data, dict) else {}


def review20_payload(html_text):
    payload = find_started_operation(html_text, "ProductSchema_init")
    if not payload:
        return None
    apply_bestbuy_location(payload.get("variables", {}))
    payload["query"] = payload["query"].replace("reviews(filter:{pageSize:5})", "reviews(filter:{pageSize:20})")
    payload["query"] = ensure_recommended_percent_query(payload.get("query") or "")
    payload["query"] = ensure_dotcom_display_status_query(payload.get("query") or "")
    return payload


def ensure_recommended_percent_query(query):
    if not query or "recommendedPercent" in query:
        return query
    return re.sub(
        r"(reviewInfo\s*\{\s*averageRating\s+reviewCount)(?!\s+recommendedPercent)",
        r"\1 recommendedPercent",
        query,
    )


def ensure_dotcom_display_status_query(query):
    if not query or "dotComDisplayStatus" in query:
        return query
    return re.sub(
        r"(productBySkuId\s*\([^)]*\)\s*\{)",
        r"\1dotComDisplayStatus ",
        query,
        count=1,
    )


PRODUCT_SCHEMA_REVIEW20_QUERY = (
    "query ProductSchema_init($skuId:String!$salesChannel:String!)"
    "{...ProductSchema_Fragment}"
    "fragment ProductSchema_Fragment on Query{productBySkuId(skuId:$skuId){bsin name{short}images{piscesHref}"
    "url{pdp}description{short}skuId dotComDisplayStatus manufacturer{modelNumber}color{displayName}brand badgesV2{label} "
    "operationalAttributes{displayName values} whatItIs "
    "isPurchaseWithTradeInEligible connectionType{code} "
    "productVariationDetailDisplay{type title variationTypes{definition displayName rawName}"
    "productVariations{shortName color colorCategory sku variations{rawName value}"
    "product{name{short}skuId}}}"
    "reviewInfo{averageRating reviewCount recommendedPercent}"
    "specificationGroups{specifications{displayName value}}"
    "buyingOptions{description pdpUrl skuId type product{price(input:{salesChannel:$salesChannel}){customerPrice}}}"
    "reviews(filter:{pageSize:20}){results{rating title text userNickname}}}}"
)


PRODUCT_SCHEMA_GET_IT_FAST_QUERY = (
    "query ProductSchemaGetItFastProbe($skuId:String!$destinationZipCode:String$locationId:String)"
    "{productBySkuId(skuId:$skuId){skuId bsin name{short}url{pdp}}"
    "fulfillmentGetItFastOptions(input:{destinationZipCode:$destinationZipCode locationId:$locationId})"
    "{shippingCutOffDetails{getItBy getItByDate destinationZipCode}"
    "storeCutOffDetails{getItBy getItByDate minPickupHours locationId}}}"
)


PDP_FULFILLMENT_DYNAMIC_QUERY = """
query FulfillmentOptionHook_FulfillmentDynamicQuery($skuId:String!$fulfillmentInput:ProductFulfillmentInput!$productPriceInput:ProductItemPriceInput!$openBoxCondition:Int){productBySkuId(skuId:$skuId openBoxCondition:$openBoxCondition){skuId ...FullfillmentProductBySkuIdFragment fulfillmentOptions(input:$fulfillmentInput){...FullfillmentOptionsFragment}badgesV2{label}}}fragment FullfillmentProductBySkuIdFragment on Product{brand brandId classification{class{id}}isSmallMediumBusiness releaseDateDisplayValue whatItIs eligibleGatedEventCustomerSegments{canPurchaseNow}isConstrainedHighVelocity inStoreServiceType buyingOptions{type product{openBoxCondition openBoxOptions{code}inStoreServiceType price(input:$productPriceInput){openBoxCondition}primaryImage{piscesHref}name{short}}pdpUrl}price(input:$productPriceInput){customerPrice mobileContracts{isDefaultContract purchaseType numberOfPayments}}waitlists{enrollmentPaused id name type}...MpFragment}fragment MpFragment on Product{bsinProduct{bsin products{openBoxCondition condition{type}seller{classification}skuId}}bsin seller{classification id}}fragment FullfillmentOptionsFragment on FulfillmentOptionsList{buttonStates{...ButtonStatesFragment}shippingDetails{...ShippingDetailsFragment}deliveryDetails{...DeliveryDetailsFragment}ispuDetails{...IspuDetailsFragment}}fragment ButtonStatesFragment on ButtonState{buttonState condition displayText secondaryButtonState secondaryDisplayText planButtonState hyperlinkUrl}fragment ShippingDetailsFragment on FulfillmentShippingDetail{destinationZipCode shippingAvailability{backordered condition customerLOSGroup{customerLosGroupId displayDateType maxLineItemMaxDate minLineItemMaxDate name price}levelOfServices{code id isLessThanTruckload isScheduleParcelDelivery}defaultCustomerLosGroupId downloadEligible emailEligible fulfillByVendor preorderable promiseByStreetDate whenAvailableFlag shippingEligible restrictions{category}}sku}fragment DeliveryDetailsFragment on FulfillmentDeliveryDetail{deliveryAvailability{salLocationId deliverable deliveryEligible forceSkipScheduling homeDeliveryDisplayDateType condition deliverySlots{date}deliveryServices{eligible levelsOfService{offerUnitPrice unitPrice}serviceType}installationSlots{date}backordered restrictions{category}}destinationZipCode}fragment IspuDetailsFragment on InStorePickupDetail{sku ispuAvailability{...IspuAvailabilityFragment}nearbyLocation{availability{maxDate pickupEligible quantity}distance store{...IspuStoreFragment}}nearbyLocations{availability{fulfillmentType maxDate minPickupInHours}store{...IspuStoreFragment}}sku store{...IspuStoreFragment}}fragment IspuAvailabilityFragment on InStorePickupAvailability{backordered condition displayDateType downloadEligible emailEligible fulfillDate fulfillmentType instoreInventoryAvailable inStoreOnly maxDate minPickupInHours pickupEligible preorderable promiseByStreetDate whenAvailableFlag quantity restrictions{category}inStoreServices{installationSlots{date}}}fragment IspuStoreFragment on FulfillmentPickUpStore{name storeId zip}
""".strip()


def fallback_review20_payload(sku):
    variables = {
        "skuId": str(sku),
        "salesChannel": "LargeView",
    }
    apply_bestbuy_location(variables)
    return {
        "operationName": "ProductSchema_init",
        "variables": variables,
        "query": PRODUCT_SCHEMA_REVIEW20_QUERY,
    }


def get_it_fast_payload(sku):
    variables = {
        "skuId": str(sku),
        "destinationZipCode": bestbuy_zip_code(),
        "locationId": bestbuy_store_id(),
    }
    apply_bestbuy_location(variables)
    return {
        "operationName": "ProductSchemaGetItFastProbe",
        "variables": variables,
        "query": PRODUCT_SCHEMA_GET_IT_FAST_QUERY,
    }


def fulfillment_dynamic_input(option_marker=None):
    zip_code = str(bestbuy_zip_code())
    store_id = str(bestbuy_store_id())
    variables = {
        "shipping": {
            "destinationZipCode": zip_code,
            "effectivePlanPaidMembership": "NULL",
        },
        "delivery": {
            "destinationZipCode": zip_code,
            "deliveryDateOption": "EARLIEST_AVAILABLE_DATE",
            "effectivePlanPaidMembership": "NULL",
        },
        "inStorePickup": {
            "storeId": store_id,
            "searchNearby": True,
            "showNearbyLocations": False,
        },
        "profileCode": None,
        "buttonState": {
            "fulfillmentOption": option_marker,
            "context": "PDP",
            "destinationZipCode": zip_code,
            "storeId": store_id,
            "effectivePlanPaidMembership": "NULL",
        },
    }
    return apply_bestbuy_location(variables)


def fulfillment_product_price_input():
    return {
        "customerAttributes": "",
        "salesChannel": "LargeView",
        "customerId": None,
        "planPaidMemberType": "NULL",
        "ct": "",
        "isStoreAgent": False,
        "locationId": "",
    }


def fulfillment_dynamic_payload(sku):
    return {
        "operationName": "FulfillmentOptionHook_FulfillmentDynamicQuery",
        "variables": {
            "skuId": str(sku),
            "fulfillmentInput": fulfillment_dynamic_input(None),
            "productPriceInput": fulfillment_product_price_input(),
        },
        "extensions": {"clientLibrary": {"name": "@apollo/client", "version": "4.1.6"}},
        "query": PDP_FULFILLMENT_DYNAMIC_QUERY,
    }


COMPARE_PRODUCT_QUERY = """
query GetCompareProduct($placement: String!, $site: String!, $limit: Int!, $skuId: String!) {
  productBySkuId(skuId: $skuId) {
    description { long }
    name { short }
    primaryImage { piscesHref }
    reviewInfo { averageRating reviewCount conFeatures { name } proFeatures { name } }
    specificationGroups { name specifications { definition displayName value } }
    url { relativePdp }
    skuId
    openBoxCondition
  }
  recommendations(filter: {placement: $placement, site: $site, limit: $limit, skus: [$skuId]}) {
    subPlacements {
      recommendations {
        ep
        id
        item {
          ... on Product {
            description { long }
            name { short }
            primaryImage { piscesHref }
            reviewInfo { averageRating reviewCount conFeatures { name } proFeatures { name } }
            specificationGroups { name specifications { definition displayName value } }
            url { relativePdp }
            skuId
            openBoxCondition
          }
        }
      }
      ep
      id
      name
    }
  }
}
""".strip()


def compare_product_payload(sku):
    return {
        "operationName": "GetCompareProduct",
        "variables": {
            "placement": "single-compare",
            "site": "dotcom-l",
            "limit": 3,
            "skuId": str(sku),
        },
        "extensions": {"clientLibrary": {"name": "@apollo/client", "version": "4.1.6"}},
        "query": COMPARE_PRODUCT_QUERY,
    }


COMPARE_RECOMMENDATIONS_V2_QUERY = """
query ProductCarousel_Recommendations(
  $placement: String!,
  $site: String!,
  $skus: [String!],
  $limit: Int!,
  $partyId: String,
  $ut: String,
  $vt: String,
  $storeIdRecs: [String!],
  $pageType: String,
  $filterValue: [String!],
  $filterType: String!,
  $referer: String
) {
  recommendationsV2(
    identity: {partyId: $partyId, ut: $ut, vt: $vt}
    filter: {type: $filterType, values: $filterValue}
    input: {
      placement: $placement,
      site: $site,
      skus: $skus,
      limit: $limit,
      storeIds: $storeIdRecs,
      pageType: $pageType,
      referer: $referer
    }
  ) {
    subPlacements {
      id
      name
      ep
      title
      recommendations {
        id
        ep
        rank
        item {
          ... on Product {
            skuId
            name { short title }
            url { relativePdp skuSpecificUrl }
            primaryImage { piscesHref }
            reviewInfo { averageRating reviewCount }
            openBoxCondition
          }
        }
      }
    }
  }
}
""".strip()


def compare_recommendations_v2_payload(sku):
    return {
        "operationName": "ProductCarousel_Recommendations",
        "variables": {
            "placement": "pdp-compare",
            "site": "dotcom-l",
            "skus": [str(sku)],
            "limit": 15,
            "partyId": None,
            "ut": "",
            "vt": "",
            "storeIdRecs": [str(bestbuy_store_id())],
            "pageType": "Product Detail Page",
            "filterValue": [],
            "filterType": "",
            "referer": "www.bestbuy.com",
        },
        "extensions": {"clientLibrary": {"name": "@apollo/client", "version": "4.1.6"}},
        "query": COMPARE_RECOMMENDATIONS_V2_QUERY,
    }


TRADE_IN_DATA_QUERY = """
query GetTradeInData($skuId: String!) {
  productBySkuId(skuId: $skuId) {
    tradeInOffer {
      purchaseSku
      value
      tradeInSku
      disclaimer
      offerCarrierValue {
        carrierCode
        carrierLink
        carrierUpToValue
        disclaimerCallouts
        purchaseType
      }
    }
  }
}
""".strip()


def trade_in_data_payload(sku):
    return {
        "operationName": "GetTradeInData",
        "variables": {"skuId": str(sku)},
        "extensions": {"clientLibrary": {"name": "@apollo/client", "version": "4.1.6"}},
        "query": TRADE_IN_DATA_QUERY,
    }


def detail_batch_payloads_for_sku(sku):
    payloads = []
    indices = {}
    detail_payload = fallback_review20_payload(sku)
    review_payload = fallback_review20_payload(sku)
    indices["detail"] = len(payloads)
    payloads.append(detail_payload)
    indices["review"] = len(payloads)
    payloads.append(review_payload)
    if FETCH_COMPARE:
        indices["compare"] = len(payloads)
        payloads.append(compare_product_payload(sku))
        if hhp_compare_v2_fallback_enabled():
            indices["compare_v2"] = len(payloads)
            payloads.append(compare_recommendations_v2_payload(sku))
    if hhp_trade_in_data_enabled():
        indices["trade_in"] = len(payloads)
        payloads.append(trade_in_data_payload(sku))
    if FETCH_FULFILLMENT_DYNAMIC:
        indices["fulfillment_dynamic"] = len(payloads)
        payloads.append(fulfillment_dynamic_payload(sku))
    elif FETCH_GET_IT_FAST:
        indices["get_it_fast"] = len(payloads)
        payloads.append(get_it_fast_payload(sku))
    return payloads, indices


def detail_batch_request_entries(targets):
    request_payload = []
    entries = []
    for target in targets:
        sku = str(target.get("sku_id") or "").strip()
        if not sku:
            continue
        payloads, relative_indices = detail_batch_payloads_for_sku(sku)
        base_index = len(request_payload)
        request_payload.extend(payloads)
        entries.append(
            {
                "target": target,
                "sku": sku,
                "pdp_url": target_url(target, sku),
                "payloads": payloads,
                "indices": {stage: base_index + index for stage, index in relative_indices.items()},
            }
        )
    return request_payload, entries


def detail_status_code_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def detail_batch_success_count(response_json, entries, status_code):
    if detail_status_code_int(status_code) != 200:
        return 0
    count = 0
    for entry in entries:
        indices = entry.get("indices") or {}
        sku = str(entry.get("sku") or "")
        detail_response_json = graphql_batch_response_item(response_json, indices.get("detail", -1))
        product = (
            ((detail_response_json.get("data") or {}).get("productBySkuId") or {})
            if isinstance(detail_response_json, dict)
            else {}
        )
        if isinstance(product, dict) and str(product.get("skuId") or "") == sku:
            count += 1
    return count


def retryable_detail_batch_result(status_code, response_json, entries, error=""):
    if detail_batch_success_count(response_json, entries, status_code) > 0:
        return False
    if status_code == "ERR" or error:
        return True
    return detail_status_code_int(status_code) in DETAIL_RETRY_STATUS_CODES


def detail_payloads(sku):
    paths = detail_paths(sku)
    if paths.get("apollo") and paths["apollo"].exists():
        try:
            data = json.loads(paths["apollo"].read_text(encoding="utf-8-sig"))
            if isinstance(data, list):
                return data
        except ValueError:
            pass
    html_path = paths["html"]
    html_text = html_path.read_text(encoding="utf-8", errors="replace") if html_path.exists() else ""
    return apollo_payloads_json(html_text)


def review20_payload_for_sku(sku):
    payload = find_started_operation_from_payloads(detail_payloads(sku), "ProductSchema_init")
    if not payload:
        return fallback_review20_payload(sku)
    apply_bestbuy_location(payload.get("variables", {}))
    payload["query"] = payload["query"].replace("reviews(filter:{pageSize:5})", "reviews(filter:{pageSize:20})")
    payload["query"] = ensure_recommended_percent_query(payload.get("query") or "")
    payload["query"] = ensure_dotcom_display_status_query(payload.get("query") or "")
    return payload


def fetch_detail(client, target):
    sku = str(target.get("sku_id") or "").strip()
    pdp_url = target_url(target, sku)
    current_paths = detail_paths(sku)
    if not FORCE_REFRESH and detail_success(sku):
        return read_json(current_paths["meta"])
    attempt = next_attempt(current_paths["meta"], pdp_url)
    meta = {"sku_id": sku, "stage": "detail", "url": pdp_url, "attempt": attempt, "started_at": now()}
    if attempt_cap_blocks_retry(attempt):
        paths = detail_paths_for_status(sku, target, False)
        meta.update({"success": False, "error": "max_attempts_exceeded"})
        paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return meta

    detail_payload = fallback_review20_payload(sku)
    review_payload = fallback_review20_payload(sku)
    compare_payload = compare_product_payload(sku) if FETCH_COMPARE else None
    compare_v2_payload = compare_recommendations_v2_payload(sku) if FETCH_COMPARE and hhp_compare_v2_fallback_enabled() else None
    trade_in_payload = trade_in_data_payload(sku) if hhp_trade_in_data_enabled() else None
    fulfillment_dynamic_payload_obj = fulfillment_dynamic_payload(sku) if FETCH_FULFILLMENT_DYNAMIC else None
    get_it_fast_batch_payload = get_it_fast_payload(sku) if FETCH_GET_IT_FAST and not FETCH_FULFILLMENT_DYNAMIC else None
    request_payload = [detail_payload, review_payload]
    compare_index = None
    compare_v2_index = None
    trade_in_index = None
    if compare_payload:
        compare_index = len(request_payload)
        request_payload.append(compare_payload)
    if compare_v2_payload:
        compare_v2_index = len(request_payload)
        request_payload.append(compare_v2_payload)
    if trade_in_payload:
        trade_in_index = len(request_payload)
        request_payload.append(trade_in_payload)
    fulfillment_dynamic_index = None
    if fulfillment_dynamic_payload_obj:
        fulfillment_dynamic_index = len(request_payload)
        request_payload.append(fulfillment_dynamic_payload_obj)
    get_it_fast_index = None
    if get_it_fast_batch_payload:
        get_it_fast_index = len(request_payload)
        request_payload.append(get_it_fast_batch_payload)
    operation_names = [payload.get("operationName", "") for payload in request_payload]
    paths = detail_paths_for_status(sku, target, False)
    for transport in fetch_transports():
        if transport == "zenrows" and not client:
            continue
        start = time.perf_counter()
        try:
            json_response_summary = {}
            compare_debug = {}
            compare_name_count = 0
            compare_ok = True
            get_it_fast_values = {}
            trade_in_data_text = ""
            if DETAIL_DIRECT_GRAPHQL:
                response = client.post(
                    "https://www.bestbuy.com/gateway/graphql",
                    params=graphql_params(),
                    headers={
                        "accept": "application/json, text/plain, */*",
                        "content-type": "application/json",
                        "origin": "https://www.bestbuy.com",
                        "referer": pdp_url,
                    },
                    data=json.dumps(request_payload),
                    timeout=REQUEST_TIMEOUT,
                )
                text = response.text
                response_json = {}
                try:
                    response_json = response.json()
                except ValueError:
                    pass
                detail_response_json = graphql_batch_response_item(response_json, 0)
                review_response_json = graphql_batch_response_item(response_json, 1)
                compare_response_json = graphql_batch_response_item(response_json, compare_index) if compare_index is not None else {}
                compare_v2_response_json = (
                    graphql_batch_response_item(response_json, compare_v2_index) if compare_v2_index is not None else {}
                )
                trade_in_response_json = (
                    graphql_batch_response_item(response_json, trade_in_index) if trade_in_index is not None else {}
                )
                fulfillment_dynamic_response_json = (
                    graphql_batch_response_item(response_json, fulfillment_dynamic_index)
                    if fulfillment_dynamic_index is not None
                    else {}
                )
                get_it_fast_response_json = (
                    graphql_batch_response_item(response_json, get_it_fast_index) if get_it_fast_index is not None else {}
                )
                get_it_fast_values = get_it_fast_availability_values(get_it_fast_response_json)
                trade_in_data_text = (
                    trade_in_from_offer_data(trade_in_response_json.get("data") or {}, include_generic=False)
                    if isinstance(trade_in_response_json, dict)
                    else ""
                )
                product = ((response_json.get("data") or {}).get("productBySkuId") or {}) if isinstance(response_json, dict) else {}
                if not product:
                    product = ((detail_response_json.get("data") or {}).get("productBySkuId") or {}) if isinstance(detail_response_json, dict) else {}
                success = response.status_code == 200 and isinstance(product, dict) and str(product.get("skuId") or "") == str(sku)
                paths = detail_paths_for_status(sku, target, success)
                direct_response_summary = {
                    "status_code": response.status_code,
                    "operation_names": operation_names,
                    "batch_count": len(response_json) if isinstance(response_json, list) else (1 if isinstance(response_json, dict) else 0),
                    "fulfillment_in_batch": bool(fulfillment_dynamic_payload_obj or get_it_fast_batch_payload),
                    "product_fulfillment_options_in_batch": bool(fulfillment_dynamic_payload_obj),
                    "fulfillment_dynamic_in_batch": bool(fulfillment_dynamic_payload_obj),
                    "fulfillment_dynamic_has_options": bool(
                        first_path(
                            [
                                ((fulfillment_dynamic_response_json.get("data") or {}).get("productBySkuId") or {})
                                if isinstance(fulfillment_dynamic_response_json, dict)
                                else {}
                            ],
                            ["fulfillmentOptions"],
                        )
                    ),
                    "compare_v2_in_batch": bool(compare_v2_payload),
                    "compare_v2_name_count": len(compare_recommendation_names(compare_v2_response_json.get("data") or {}))
                    if isinstance(compare_v2_response_json, dict)
                    else 0,
                    "trade_in_data_in_batch": bool(trade_in_payload),
                    "trade_in_data_text": trade_in_data_text,
                    "get_it_fast_in_batch": bool(get_it_fast_batch_payload),
                    "get_it_fast_value_count": sum(1 for value in get_it_fast_values.values() if value),
                    "errors": [
                        item.get("errors")
                        for item in (response_json if isinstance(response_json, list) else [response_json])
                        if isinstance(item, dict) and item.get("errors")
                    ],
                }
                paths["json_response"].write_text(
                    json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                paths["json_response_summary"].write_text(
                    json.dumps(direct_response_summary, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                endpoint_result = {
                    "payloads": [],
                    "responses": [],
                    "entries": [],
                    "response_texts": [],
                    "success_count": 0,
                    "value_count": 0,
                    "error_count": 0,
                }
                artifact_payloads = list(request_payload)
                artifact_responses = list(response_json) if isinstance(response_json, list) else [response_json]
                artifact_text = text or ""
                operation_names = [payload.get("operationName", "") for payload in artifact_payloads]
                artifact_meta = write_direct_detail_artifacts(
                    paths,
                    artifact_payloads,
                    artifact_responses,
                    artifact_text,
                    dict(response.headers),
                )
                if paths.get("fulfillment_response"):
                    paths["fulfillment_response"].write_text(
                        json.dumps(
                            [fulfillment_dynamic_response_json]
                            if fulfillment_dynamic_payload_obj
                            else endpoint_result.get("entries") or [],
                            indent=2,
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                if paths.get("fulfillment_meta"):
                    fulfillment_dynamic_has_options = bool(
                        first_path(
                            [
                                ((fulfillment_dynamic_response_json.get("data") or {}).get("productBySkuId") or {})
                                if isinstance(fulfillment_dynamic_response_json, dict)
                                else {}
                            ],
                            ["fulfillmentOptions"],
                        )
                    )
                    paths["fulfillment_meta"].write_text(
                        json.dumps(
                            {
                                "sku_id": sku,
                                "stage": "fulfillment_collected_in_detail"
                                if fulfillment_dynamic_payload_obj
                                else "fulfillment_not_collected_in_detail",
                                "url": pdp_url,
                                "enabled": bool(fulfillment_dynamic_payload_obj),
                                "transport": transport if fulfillment_dynamic_payload_obj else "none",
                                "extra_network_call": False,
                                "variant_count": 1
                                if fulfillment_dynamic_payload_obj
                                else len(endpoint_result.get("entries") or []),
                                "success_count": 1
                                if fulfillment_dynamic_has_options
                                else endpoint_result.get("success_count", 0),
                                "value_count": 1
                                if fulfillment_dynamic_has_options
                                else endpoint_result.get("value_count", 0),
                                "error_count": endpoint_result.get("error_count", 0),
                                "options": ["FulfillmentOptionHook_FulfillmentDynamicQuery"]
                                if fulfillment_dynamic_payload_obj
                                else [],
                                "finished_at": now(),
                            },
                            indent=2,
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                review_count = review_result_count_from_json(review_response_json)
                review_text_count = review_text_count_from_json(review_response_json)
                expected_text_count = expected_review_text_count(target, sku)
                review_ok = (
                    success
                    and review_count is not None
                    and review_text_count_is_sufficient(review_text_count, expected_text_count)
                )
                review_error = ""
                if review_count is None:
                    review_error = "review20_missing"
                elif not review_ok:
                    review_error = f"review20_partial_{review_text_count or 0}_of_{expected_text_count}"
                review_paths_current = review_paths_for_status(sku, target, review_ok)
                write_review_response_artifacts(review_paths_current, review_payload, review_response_json, text, dict(response.headers))
                review_meta = {
                    "sku_id": sku,
                    "stage": "review20",
                    "url": pdp_url,
                    "attempt": attempt,
                    "started_at": meta["started_at"],
                    "success": review_ok,
                    "status_code": response.status_code,
                    "transport": transport,
                    "fetch_mode": FETCH_MODE,
                    "detail_mode": "direct_graphql_batch",
                    "elapsed_seconds": 0,
                    "x_request_cost": 0,
                    "bytes": len(text or ""),
                    "review_count_returned": review_count if review_count is not None else 0,
                    "review_text_count_returned": review_text_count if review_text_count is not None else 0,
                    "review_text_count_expected": expected_text_count if expected_text_count is not None else "",
                    "finished_at": now(),
                    "error": "" if review_ok else review_error,
                }
                review_paths_current["meta"].write_text(json.dumps(review_meta, indent=2, ensure_ascii=False), encoding="utf-8")
                if compare_payload:
                    recommendations = compare_recommendations_from_response(compare_response_json)
                    fallback_names = (
                        compare_recommendation_names(compare_v2_response_json.get("data") or {})
                        if isinstance(compare_v2_response_json, dict)
                        else []
                    )
                    compare_ok = response.status_code == 200 and (
                        isinstance(recommendations, list) or isinstance(fallback_names, list)
                    )
                    compare_paths_current = compare_paths_for_status(sku, target, compare_ok)
                    write_compare_response_artifacts(compare_paths_current, compare_payload, compare_response_json, text, dict(response.headers))
                    compare_meta = {
                        "sku_id": sku,
                        "stage": "compare",
                        "url": pdp_url,
                        "attempt": attempt,
                        "started_at": meta["started_at"],
                        "success": compare_ok,
                        "status_code": response.status_code,
                        "transport": transport,
                        "fetch_mode": FETCH_MODE,
                        "detail_mode": "direct_graphql_batch",
                        "elapsed_seconds": 0,
                        "x_request_cost": 0,
                        "bytes": len(text or ""),
                        "recommendation_count": max(
                            len(recommendations) if isinstance(recommendations, list) else 0,
                            len(fallback_names),
                        ),
                        "fallback_recommendation_count": len(fallback_names),
                        "fallback_recommendation_names": fallback_names[:15],
                        "finished_at": now(),
                        "error": "" if compare_ok else "compare_recommendations_missing",
                    }
                    compare_paths_current["meta"].write_text(json.dumps(compare_meta, indent=2, ensure_ascii=False), encoding="utf-8")
                error = "" if success else response_error(response.status_code, text, "detail_graphql_missing_product")
            elif DETAIL_PDP_FALLBACK:
                print(
                    format_log_line(
                        "detail:fetch",
                        sku=sku,
                        mode="pdp_render",
                        attempt=attempt,
                        transport=transport,
                    ),
                    flush=True,
                )
                response = client.get(pdp_url, params=detail_params(attempt, sku), timeout=REQUEST_TIMEOUT)
                response_text = response.text
                html_text = response_text
                if DETAIL_JSON_RESPONSE:
                    json_response_data = parse_json_value(response_text)
                    html_text = json_response_data.get("html") or json_response_data.get("content") or ""
                    json_response_summary = json_response_compare_summary(json_response_data, sku)
                compare_name_count = int(json_response_summary.get("compare_name_count", 0) or 0)
                compare_debug = json_response_summary.get("html_compare_debug") or {}
                compare_ok = (not DETAIL_REQUIRE_SIMILAR) or compare_name_count >= DETAIL_SIMILAR_MIN_NAMES
                success = response.status_code == 200 and has_product_schema(html_text)
                paths = detail_paths_for_status(sku, target, success)
                artifact_meta = write_detail_artifacts(paths, html_text, dict(response.headers))
                if DETAIL_JSON_RESPONSE:
                    paths["json_response"].write_text(response_text, encoding="utf-8", errors="replace")
                    paths["json_response_summary"].write_text(
                        json.dumps(json_response_summary, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                error = response_error(response.status_code, html_text, "detail_html_missing_product_schema")
            else:
                raise RuntimeError("Set BESTBUY_DETAIL_DIRECT_GRAPHQL=1 or BESTBUY_DETAIL_PDP_FALLBACK=1")
            status = response.status_code
            meta.update(
                {
                    "success": success,
                    "status_code": status,
                    "transport": transport,
                    "fetch_mode": FETCH_MODE,
                    "detail_mode": "direct_graphql_batch" if DETAIL_DIRECT_GRAPHQL else "pdp_render",
                    "batched_operations": ",".join(operation_names),
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "x_request_cost": request_cost(response.headers),
                    "bytes": artifact_meta["full_bytes"],
                    "stored_bytes": artifact_meta["stored_bytes"],
                    "html_mode": artifact_meta["html_mode"],
                    "apollo_payload_count": artifact_meta["apollo_payload_count"],
                    "json_response": DETAIL_JSON_RESPONSE,
                    "json_response_xhr_count": json_response_summary.get("xhr_count", 0),
                    "json_response_html_capture_count": json_response_summary.get("html_compare_capture_count", 0),
                    "json_response_compare_debug_compare_text_found": bool(
                        compare_debug.get("compareTextFound")
                    )
                    if isinstance(compare_debug, dict)
                    else False,
                    "json_response_compare_debug_observer_hits": compare_debug.get("observerHits", 0)
                    if isinstance(compare_debug, dict)
                    else 0,
                    "json_response_compare_debug_jiggle_count": compare_debug.get("jiggleCount", 0)
                    if isinstance(compare_debug, dict)
                    else 0,
                    "json_response_compare_debug_last_scroll_percent": compare_debug.get("lastScrollPercent", 0)
                    if isinstance(compare_debug, dict)
                    else 0,
                    "json_response_graphql_hit_count": json_response_summary.get("graphql_or_compare_hit_count", 0),
                    "json_response_compare_name_count": compare_name_count,
                    "json_response_compare_required": DETAIL_REQUIRE_SIMILAR,
                    "json_response_compare_min_names": DETAIL_SIMILAR_MIN_NAMES,
                    "json_response_compare_ok": compare_ok,
                    "fulfillment_endpoint_enabled": False,
                    "fulfillment_direct_batch_enabled": bool(fulfillment_dynamic_payload_obj or get_it_fast_batch_payload),
                    "fulfillment_dynamic_batch_enabled": bool(fulfillment_dynamic_payload_obj),
                    "fulfillment_get_it_fast_batch_enabled": bool(get_it_fast_batch_payload),
                    "fulfillment_get_it_fast_value_count": sum(1 for value in get_it_fast_values.values() if value)
                    if DETAIL_DIRECT_GRAPHQL
                    else 0,
                    "hhp_trade_in_data_batch_enabled": bool(trade_in_payload),
                    "hhp_trade_in_data_text": trade_in_data_text,
                    "fulfillment_extra_network_calls": 0,
                    "fulfillment_endpoint_variant_count": 0,
                    "fulfillment_endpoint_success_count": 0,
                    "fulfillment_endpoint_value_count": 0,
                    "fulfillment_endpoint_error_count": 0,
                    "finished_at": now(),
                    "error": "" if success else error,
                }
            )
        except RequestException as exc:
            paths = detail_paths_for_status(sku, target, False)
            meta.update(
                {
                    "success": False,
                    "status_code": "ERR",
                    "transport": transport,
                    "fetch_mode": FETCH_MODE,
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "x_request_cost": 0,
                    "finished_at": now(),
                    "error": str(exc),
                }
            )
        if meta.get("success"):
            break
    paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def fetch_detail_sku_batch(client, targets, force_retry=False, max_batch_attempts=None, retry_label=""):
    if not targets:
        return {}
    run_attempt_limit = max(1, int(max_batch_attempts or MAX_ATTEMPTS))
    prepared_targets = []
    metas = {}
    for target in targets:
        sku = str(target.get("sku_id") or "").strip()
        if not sku:
            continue
        pdp_url = target_url(target, sku)
        current_paths = detail_paths(sku)
        if not force_retry and not FORCE_REFRESH and detail_success(sku):
            meta = read_json(current_paths["meta"])
            annotate_detail_final_compare(meta, sku)
            metas[sku] = meta
            continue
        attempt = next_attempt(current_paths["meta"], pdp_url)
        meta = {"sku_id": sku, "stage": "detail", "url": pdp_url, "attempt": attempt, "started_at": now()}
        if not force_retry and attempt_cap_blocks_retry(attempt):
            paths = detail_paths_for_status(sku, target, False)
            meta.update({"success": False, "error": "max_attempts_exceeded", "finished_at": now()})
            paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            metas[sku] = meta
            continue
        prepared_targets.append(target)
        metas[sku] = meta

    request_payload, entries = detail_batch_request_entries(prepared_targets)
    if not entries:
        return metas
    for transport in fetch_transports():
        if transport == "zenrows" and not client:
            continue
        response = None
        response_json = {}
        text = ""
        headers = {}
        error = ""
        batch_started = time.perf_counter()
        batch_attempts = 0
        total_batch_cost = 0.0
        attempt_status_codes = []
        attempt_costs = []
        attempt_errors = []
        for batch_attempt in range(1, run_attempt_limit + 1):
            batch_attempts = batch_attempt
            start = time.perf_counter()
            response = None
            response_json = {}
            text = ""
            headers = {}
            error = ""
            try:
                if transport == "browser_graphql":
                    status_code, text, response_json, headers, browser_elapsed = browser_graphql_post(
                        request_payload,
                        entries[0]["pdp_url"],
                    )
                    response = None
                    start = time.perf_counter() - browser_elapsed
                else:
                    response = client.post(
                        "https://www.bestbuy.com/gateway/graphql",
                        params=graphql_params(),
                        headers={
                            "accept": "application/json, text/plain, */*",
                            "content-type": "application/json",
                            "origin": "https://www.bestbuy.com",
                            "referer": entries[0]["pdp_url"],
                        },
                        data=json.dumps(request_payload),
                        timeout=REQUEST_TIMEOUT,
                    )
                    status_code = response.status_code
                    text = response.text
                    headers = dict(response.headers)
                    try:
                        response_json = response.json()
                    except ValueError:
                        response_json = {}
            except RequestException as exc:
                error = str(exc)
                status_code = "ERR"
            except RuntimeError as exc:
                error = str(exc)
                status_code = "ERR"

            attempt_cost = request_cost(headers)
            total_batch_cost += attempt_cost
            attempt_status_codes.append(str(status_code))
            attempt_costs.append(str(attempt_cost))
            if error:
                attempt_errors.append(error)
            if (
                not AUTO_RETRY
                or batch_attempt >= run_attempt_limit
                or not retryable_detail_batch_result(status_code, response_json, entries, error)
            ):
                break
            retry_sleep = detail_retry_sleep_seconds(batch_attempt)
            if retry_sleep > 0:
                time.sleep(retry_sleep)

        elapsed = round(time.perf_counter() - batch_started, 3)
        batch_cost = total_batch_cost
        split_cost = batch_cost / len(entries) if entries else 0
        status_code = attempt_status_codes[-1] if attempt_status_codes else "ERR"
        response_count = len(response_json) if isinstance(response_json, list) else (1 if isinstance(response_json, dict) else 0)
        for batch_index, entry in enumerate(entries, 1):
            target = entry["target"]
            sku = entry["sku"]
            meta = metas.get(sku) or {
                "sku_id": sku,
                "stage": "detail",
                "url": entry["pdp_url"],
                "attempt": 1,
                "started_at": now(),
            }
            attempt_cap = MAX_ATTEMPTS if not force_retry else max(MAX_ATTEMPTS, int(meta.get("attempt") or 1))
            attempt_value = min(attempt_cap, int(meta.get("attempt") or 1) + max(0, batch_attempts - 1))
            indices = entry["indices"]
            detail_response_json = graphql_batch_response_item(response_json, indices.get("detail", -1))
            review_response_json = graphql_batch_response_item(response_json, indices.get("review", -1))
            compare_response_json = graphql_batch_response_item(response_json, indices.get("compare", -1))
            compare_v2_response_json = graphql_batch_response_item(response_json, indices.get("compare_v2", -1))
            trade_in_response_json = graphql_batch_response_item(response_json, indices.get("trade_in", -1))
            fulfillment_dynamic_response_json = graphql_batch_response_item(response_json, indices.get("fulfillment_dynamic", -1))
            get_it_fast_response_json = graphql_batch_response_item(response_json, indices.get("get_it_fast", -1))
            get_it_fast_values = get_it_fast_availability_values(get_it_fast_response_json)
            trade_in_data_text = (
                trade_in_from_offer_data(trade_in_response_json.get("data") or {}, include_generic=False)
                if isinstance(trade_in_response_json, dict)
                else ""
            )
            product = ((detail_response_json.get("data") or {}).get("productBySkuId") or {}) if isinstance(detail_response_json, dict) else {}
            success = detail_status_code_int(status_code) == 200 and isinstance(product, dict) and str(product.get("skuId") or "") == str(sku)
            paths = detail_paths_for_status(sku, target, success)
            entry_responses = [
                graphql_batch_response_item(response_json, indices[stage])
                for stage in (
                    "detail",
                    "review",
                    "compare",
                    "compare_v2",
                    "trade_in",
                    "fulfillment_dynamic",
                    "get_it_fast",
                )
                if stage in indices
            ]
            entry_payloads = entry["payloads"]
            operation_names = [payload.get("operationName", "") for payload in entry_payloads]
            artifact_meta = write_direct_detail_artifacts(paths, entry_payloads, entry_responses, text, headers)
            paths["json_response"].write_text(
                json.dumps(entry_responses, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            paths["json_response_summary"].write_text(
                json.dumps(
                    {
                        "status_code": status_code,
                        "operation_names": operation_names,
                        "batch_count": len(entry_responses),
                        "sku_batch_size": len(entries),
                        "sku_batch_index": batch_index,
                        "batch_operation_count": len(request_payload),
                        "batch_response_count": response_count,
                        "response_indices": indices,
                        "compare_v2_in_batch": "compare_v2" in indices,
                        "compare_v2_name_count": len(compare_recommendation_names(compare_v2_response_json.get("data") or {}))
                        if isinstance(compare_v2_response_json, dict)
                        else 0,
                        "trade_in_data_in_batch": "trade_in" in indices,
                        "trade_in_data_text": trade_in_data_text,
                        "fulfillment_in_batch": "fulfillment_dynamic" in indices or "get_it_fast" in indices,
                        "product_fulfillment_options_in_batch": "fulfillment_dynamic" in indices,
                        "fulfillment_dynamic_in_batch": "fulfillment_dynamic" in indices,
                        "fulfillment_dynamic_has_options": bool(
                            first_path(
                                [
                                    ((fulfillment_dynamic_response_json.get("data") or {}).get("productBySkuId") or {})
                                    if isinstance(fulfillment_dynamic_response_json, dict)
                                    else {}
                                ],
                                ["fulfillmentOptions"],
                            )
                        ),
                        "get_it_fast_in_batch": "get_it_fast" in indices,
                        "get_it_fast_value_count": sum(1 for value in get_it_fast_values.values() if value),
                        "errors": [
                            item.get("errors")
                            for item in entry_responses
                            if isinstance(item, dict) and item.get("errors")
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            if paths.get("fulfillment_response"):
                paths["fulfillment_response"].write_text(
                    json.dumps(
                        [fulfillment_dynamic_response_json] if "fulfillment_dynamic" in indices else [],
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            if paths.get("fulfillment_meta"):
                fulfillment_dynamic_has_options = bool(
                    first_path(
                        [
                            ((fulfillment_dynamic_response_json.get("data") or {}).get("productBySkuId") or {})
                            if isinstance(fulfillment_dynamic_response_json, dict)
                            else {}
                        ],
                        ["fulfillmentOptions"],
                    )
                )
                paths["fulfillment_meta"].write_text(
                    json.dumps(
                        {
                            "sku_id": sku,
                            "stage": "fulfillment_collected_in_detail"
                            if "fulfillment_dynamic" in indices
                            else "fulfillment_not_collected_in_detail",
                            "url": entry["pdp_url"],
                            "enabled": "fulfillment_dynamic" in indices,
                            "transport": transport if "fulfillment_dynamic" in indices else "none",
                            "extra_network_call": False,
                            "variant_count": 1 if "fulfillment_dynamic" in indices else 0,
                            "success_count": 1 if fulfillment_dynamic_has_options else 0,
                            "value_count": 1 if fulfillment_dynamic_has_options else 0,
                            "error_count": 0,
                            "options": ["FulfillmentOptionHook_FulfillmentDynamicQuery"]
                            if "fulfillment_dynamic" in indices
                            else [],
                            "finished_at": now(),
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            review_count = review_result_count_from_json(review_response_json)
            review_text_count = review_text_count_from_json(review_response_json)
            expected_text_count = expected_review_text_count(target, sku)
            review_ok = (
                success
                and review_count is not None
                and review_text_count_is_sufficient(review_text_count, expected_text_count)
            )
            review_error = ""
            if review_count is None:
                review_error = "review20_missing"
            elif not review_ok:
                review_error = f"review20_partial_{review_text_count or 0}_of_{expected_text_count}"
            review_paths_current = review_paths_for_status(sku, target, review_ok)
            write_review_response_artifacts(review_paths_current, entry_payloads[1], review_response_json, text, headers)
            review_paths_current["meta"].write_text(
                json.dumps(
                    {
                        "sku_id": sku,
                        "stage": "review20",
                        "url": entry["pdp_url"],
                        "attempt": attempt_value,
                        "started_at": meta.get("started_at"),
                        "success": review_ok,
                        "status_code": status_code,
                        "transport": transport,
                        "fetch_mode": FETCH_MODE,
                        "detail_mode": "direct_graphql_sku_batch",
                        "elapsed_seconds": 0,
                        "x_request_cost": 0,
                        "bytes": len(text or ""),
                        "review_count_returned": review_count if review_count is not None else 0,
                        "review_text_count_returned": review_text_count if review_text_count is not None else 0,
                        "review_text_count_expected": expected_text_count if expected_text_count is not None else "",
                        "finished_at": now(),
                        "error": "" if review_ok else review_error,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            compare_ok = False
            compare_count = 0
            if FETCH_COMPARE and "compare" in indices:
                recommendations = compare_recommendations_from_response(compare_response_json)
                fallback_names = (
                    compare_recommendation_names(compare_v2_response_json.get("data") or {})
                    if isinstance(compare_v2_response_json, dict)
                    else []
                )
                compare_ok = detail_status_code_int(status_code) == 200 and (
                    isinstance(recommendations, list) or isinstance(fallback_names, list)
                )
                compare_count = len(recommendations) if isinstance(recommendations, list) else 0
                compare_paths_current = compare_paths_for_status(sku, target, compare_ok)
                compare_payload_index = next(
                    (idx for idx, payload in enumerate(entry_payloads) if payload.get("operationName") == "GetCompareProduct"),
                    2,
                )
                write_compare_response_artifacts(
                    compare_paths_current,
                    entry_payloads[compare_payload_index],
                    compare_response_json,
                    text,
                    headers,
                )
                compare_paths_current["meta"].write_text(
                    json.dumps(
                        {
                            "sku_id": sku,
                            "stage": "compare",
                            "url": entry["pdp_url"],
                            "attempt": attempt_value,
                            "started_at": meta.get("started_at"),
                            "success": compare_ok,
                            "status_code": status_code,
                            "transport": transport,
                            "fetch_mode": FETCH_MODE,
                            "detail_mode": "direct_graphql_sku_batch",
                            "elapsed_seconds": 0,
                            "x_request_cost": 0,
                            "bytes": len(text or ""),
                            "recommendation_count": max(compare_count, len(fallback_names)),
                            "fallback_recommendation_count": len(fallback_names),
                            "fallback_recommendation_names": fallback_names[:15],
                            "finished_at": now(),
                            "error": "" if compare_ok else "compare_recommendations_missing",
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            meta.update(
                {
                    "success": success,
                    "attempt": attempt_value,
                    "status_code": status_code,
                    "transport": transport,
                    "fetch_mode": FETCH_MODE,
                    "detail_mode": "direct_graphql_sku_batch",
                    "fetched_this_run": True,
                    "sku_batch_size": len(entries),
                    "sku_batch_index": batch_index,
                    "sku_batch_retry_label": retry_label,
                    "sku_batch_operation_count": len(request_payload),
                    "batched_operations": ",".join(operation_names),
                    "elapsed_seconds": elapsed,
                    "x_request_cost": split_cost,
                    "x_request_cost_total": split_cost,
                    "batch_x_request_cost": batch_cost,
                    "run_attempts": batch_attempts,
                    "attempt_status_codes": ",".join(attempt_status_codes),
                    "attempt_costs": ",".join(attempt_costs),
                    "attempt_errors": " | ".join(attempt_errors[-3:]),
                    "bytes": artifact_meta["full_bytes"],
                    "stored_bytes": artifact_meta["stored_bytes"],
                    "html_mode": artifact_meta["html_mode"],
                    "apollo_payload_count": artifact_meta["apollo_payload_count"],
                    "json_response": DETAIL_JSON_RESPONSE,
                    "json_response_compare_name_count": 0,
                    "fulfillment_endpoint_enabled": False,
                    "fulfillment_direct_batch_enabled": "fulfillment_dynamic" in indices or "get_it_fast" in indices,
                    "fulfillment_dynamic_batch_enabled": "fulfillment_dynamic" in indices,
                    "fulfillment_get_it_fast_batch_enabled": "get_it_fast" in indices,
                    "fulfillment_get_it_fast_value_count": sum(1 for value in get_it_fast_values.values() if value),
                    "hhp_trade_in_data_batch_enabled": "trade_in" in indices,
                    "hhp_trade_in_data_text": trade_in_data_text,
                    "fulfillment_extra_network_calls": 0,
                    "fulfillment_endpoint_variant_count": 0,
                    "fulfillment_endpoint_success_count": 0,
                    "fulfillment_endpoint_value_count": 0,
                    "fulfillment_endpoint_error_count": 0,
                    "finished_at": now(),
                    "error": "" if success else (error or response_error(status_code, text, "detail_graphql_missing_product")),
                }
            )
            annotate_detail_final_compare(meta, sku)
            paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            metas[sku] = meta
        if any(meta.get("success") for meta in metas.values()):
            break
    return metas


def target_needs_detail_batch_refill(target):
    sku = str(target.get("sku_id") or "").strip()
    if not sku:
        return False
    if not detail_success(sku):
        return True
    if review20_required_for_target(target, sku) and review_meta_needs_detail_batch_refill(target, sku):
        return True
    if FETCH_COMPARE and not compare_success(sku) and not compare_success_with_zero_recommendations(sku):
        return True
    return False


def review_meta_needs_detail_batch_refill(target, sku):
    review_info = (first_value(products_from_detail(sku), "reviewInfo") or {})
    if is_external_review_source(target, review_info):
        return False
    expected_count = expected_review_count(target, sku)
    if expected_count in (None, 0):
        return False
    if not review_success(sku):
        return True
    expected_text_count = min(expected_count, MAX_REVIEW_TEXTS)
    return not review_text_count_is_sufficient(review20_text_count(sku), expected_text_count)


def detail_batch_chunks(targets, size):
    size = max(1, int(size or 1))
    for offset in range(0, len(targets), size):
        yield offset, targets[offset : offset + size]


def fetch_review20(client, target, force_retry=False, retry_label=""):
    sku = str(target.get("sku_id") or "").strip()
    pdp_url = target_url(target, sku)
    current_paths = review_paths(sku)
    if not force_retry and not FORCE_REFRESH and not review_needs_retry(target):
        return read_json(current_paths["meta"])
    attempt = next_attempt(current_paths["meta"], pdp_url)
    meta = {"sku_id": sku, "stage": "review20", "url": pdp_url, "attempt": attempt, "started_at": now()}
    if retry_label:
        meta["retry_label"] = retry_label
    if not force_retry and attempt_cap_blocks_retry(attempt):
        paths = review_paths_for_status(sku, target, False)
        meta.update({"success": False, "error": "max_attempts_exceeded"})
        paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return meta

    payload = review20_payload_for_sku(sku)
    if not payload:
        paths = review_paths_for_status(sku, target, False)
        meta.update({"success": False, "error": "ProductSchema_init not found", "finished_at": now()})
        paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return meta
    paths = review_paths_for_status(sku, target, False)
    paths["request"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    for transport in fetch_transports():
        if transport == "zenrows" and not client:
            continue
        start = time.perf_counter()
        try:
            if transport == "browser_graphql":
                status_code, text, response_json, headers, elapsed = browser_graphql_post(payload, pdp_url)
                response_headers = headers
                response_status = status_code
            else:
                response = client.post(
                    "https://www.bestbuy.com/gateway/graphql",
                    params=graphql_params(),
                    headers={
                        "accept": "application/json, text/plain, */*",
                        "content-type": "application/json",
                        "origin": "https://www.bestbuy.com",
                        "referer": pdp_url,
                    },
                    data=json.dumps(payload),
                    timeout=REQUEST_TIMEOUT,
                )
                text = response.text
                response_headers = dict(response.headers)
                response_status = response.status_code
                response_json = {}
                elapsed = round(time.perf_counter() - start, 3)
        except RequestException as exc:
            paths = review_paths_for_status(sku, target, False)
            meta.update(
                {
                    "success": False,
                    "status_code": "ERR",
                    "transport": transport,
                    "fetch_mode": FETCH_MODE,
                    "fetched_this_run": True,
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "x_request_cost": 0,
                    "finished_at": now(),
                    "error": str(exc),
                }
            )
            paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            continue
        except RuntimeError as exc:
            paths = review_paths_for_status(sku, target, False)
            meta.update(
                {
                    "success": False,
                    "status_code": "ERR",
                    "transport": transport,
                    "fetch_mode": FETCH_MODE,
                    "fetched_this_run": True,
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "x_request_cost": 0,
                    "finished_at": now(),
                    "error": str(exc),
                }
            )
            paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            continue
        review_count = 0
        review_text_count = 0
        expected_text_count = expected_review_text_count(target, sku)
        error = ""
        if not response_json and transport != "browser_graphql":
            try:
                response_json = response.json()
            except ValueError as exc:
                error = str(exc)
        if response_json:
            count = review_result_count_from_json(response_json)
            review_count = count if count is not None else 0
            text_count = review_text_count_from_json(response_json)
            review_text_count = text_count if text_count is not None else 0
            if response_json.get("errors"):
                error = json.dumps(response_json.get("errors"), ensure_ascii=False, separators=(",", ":"))
        has_review_list = review_result_count_from_json(response_json) is not None
        enough_review_text = review_text_count_is_sufficient(review_text_count, expected_text_count)
        success = detail_status_code_int(response_status) == 200 and has_review_list and enough_review_text
        if has_review_list and not enough_review_text:
            error = f"review20_partial_{review_text_count}_of_{expected_text_count}"
        paths = review_paths_for_status(sku, target, success)
        if response_json:
            paths["response_json"].write_text(
                json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        paths["response_txt"].write_text(text, encoding="utf-8", errors="replace")
        paths["headers"].write_text(
            json.dumps(response_headers, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        paths["request"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        meta.update(
            {
                "success": success,
                "status_code": response_status,
                "transport": transport,
                "fetch_mode": FETCH_MODE,
                "fetched_this_run": True,
                "elapsed_seconds": elapsed,
                "x_request_cost": request_cost(response_headers),
                "bytes": len(text or ""),
                "review_count_returned": review_count,
                "review_text_count_returned": review_text_count,
                "review_text_count_expected": expected_text_count if expected_text_count is not None else "",
                "finished_at": now(),
                "error": error if not success else "",
            }
        )
        if meta.get("success"):
            break
    paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def fetch_review20_batch(client, targets):
    metas = {}
    entries = []
    request_payload = []
    for target in targets:
        sku = str(target.get("sku_id") or "").strip()
        pdp_url = target_url(target, sku)
        current_paths = review_paths(sku)
        if not FORCE_REFRESH and not review_needs_retry(target):
            metas[sku] = read_json(current_paths["meta"])
            continue
        attempt = next_attempt(current_paths["meta"], pdp_url)
        meta = {
            "sku_id": sku,
            "stage": "review20",
            "url": pdp_url,
            "attempt": attempt,
            "started_at": now(),
            "review_batch_size": len(targets),
        }
        payload = review20_payload_for_sku(sku)
        if not payload:
            paths = review_paths_for_status(sku, target, False)
            meta.update({"success": False, "error": "ProductSchema_init not found", "finished_at": now()})
            paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            metas[sku] = meta
            continue
        paths = review_paths_for_status(sku, target, False)
        paths["request"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        entries.append(
            {
                "target": target,
                "sku": sku,
                "pdp_url": pdp_url,
                "payload": payload,
                "index": len(request_payload),
                "attempt": attempt,
                "meta": meta,
            }
        )
        request_payload.append(payload)

    if not entries:
        return metas

    headers = {}
    response_json = {}
    text = ""
    status_code = "ERR"
    elapsed = 0.0
    batch_cost = 0.0
    error = ""
    transport_used = ""
    for transport in fetch_transports():
        if transport == "zenrows" and not client:
            continue
        transport_used = transport
        start = time.perf_counter()
        try:
            referer_url = entries[0]["pdp_url"]
            if transport == "browser_graphql":
                status_code, text, response_json, headers, elapsed = browser_graphql_post(request_payload, referer_url)
            else:
                response = client.post(
                    "https://www.bestbuy.com/gateway/graphql",
                    params=graphql_params(),
                    headers={
                        "accept": "application/json, text/plain, */*",
                        "content-type": "application/json",
                        "origin": "https://www.bestbuy.com",
                        "referer": referer_url,
                    },
                    data=json.dumps(request_payload),
                    timeout=REQUEST_TIMEOUT,
                )
                text = response.text
                headers = dict(response.headers)
                status_code = response.status_code
                elapsed = round(time.perf_counter() - start, 3)
                try:
                    response_json = response.json()
                except ValueError as exc:
                    response_json = {}
                    error = str(exc)
            batch_cost = request_cost(headers)
        except (RequestException, RuntimeError) as exc:
            elapsed = round(time.perf_counter() - start, 3)
            error = str(exc)
            response_json = {}
            text = ""
            headers = {}
            status_code = "ERR"
        if status_code != "ERR":
            break

    split_cost = batch_cost / len(entries) if entries else 0.0
    fallback_targets = []
    for batch_index, entry in enumerate(entries, 1):
        target = entry["target"]
        sku = entry["sku"]
        payload = entry["payload"]
        item_json = graphql_batch_response_item(response_json, entry["index"])
        item_text = json.dumps(item_json, ensure_ascii=False) if item_json else text
        review_count = 0
        review_text_count = 0
        expected_text_count = expected_review_text_count(target, sku)
        item_error = error
        if item_json:
            count = review_result_count_from_json(item_json)
            review_count = count if count is not None else 0
            text_count = review_text_count_from_json(item_json)
            review_text_count = text_count if text_count is not None else 0
            if item_json.get("errors"):
                item_error = json.dumps(item_json.get("errors"), ensure_ascii=False, separators=(",", ":"))
        has_review_list = review_result_count_from_json(item_json) is not None
        enough_review_text = review_text_count_is_sufficient(review_text_count, expected_text_count)
        success = detail_status_code_int(status_code) == 200 and has_review_list and enough_review_text
        if has_review_list and not enough_review_text:
            item_error = f"review20_partial_{review_text_count}_of_{expected_text_count}"
        elif not has_review_list and not item_error:
            item_error = "review20_missing"
        paths = review_paths_for_status(sku, target, success)
        write_review_response_artifacts(paths, payload, item_json, item_text, headers)
        meta = entry["meta"]
        meta.update(
            {
                "success": success,
                "status_code": status_code,
                "transport": transport_used,
                "fetch_mode": FETCH_MODE,
                "detail_mode": "review20_sku_batch",
                "fetched_this_run": True,
                "batch_fetched_this_run": True,
                "review_batch_size": len(entries),
                "review_batch_index": batch_index,
                "sku_batch_size": len(entries),
                "sku_batch_index": batch_index,
                "elapsed_seconds": elapsed,
                "x_request_cost": split_cost,
                "x_request_cost_total": split_cost,
                "batch_x_request_cost": batch_cost,
                "bytes": len(text or ""),
                "review_count_returned": review_count,
                "review_text_count_returned": review_text_count,
                "review_text_count_expected": expected_text_count if expected_text_count is not None else "",
                "finished_at": now(),
                "error": item_error if not success else "",
            }
        )
        paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        metas[sku] = meta
        if not success:
            fallback_targets.append(target)

    if REVIEW20_BATCH_SINGLE_FALLBACK and fallback_targets:
        print(
            format_log_line(
                "review20:fallback",
                batch_size=len(entries),
                targets=len(fallback_targets),
            ),
            flush=True,
        )
        for target in fallback_targets:
            sku = str(target.get("sku_id") or "").strip()
            original_entry = next((entry for entry in entries if entry["sku"] == sku), {})
            fallback_meta = fetch_review20(client, target, force_retry=True, retry_label="batch_single_fallback")
            fallback_meta["single_fallback_fetched_this_run"] = bool(fallback_meta.get("fetched_this_run"))
            fallback_meta["fallback_from_review_batch"] = True
            fallback_meta["batch_fetched_this_run"] = True
            fallback_meta["batch_x_request_cost"] = batch_cost
            fallback_meta["sku_batch_size"] = len(entries)
            fallback_meta["sku_batch_index"] = int(original_entry.get("index", 0)) + 1 if original_entry else ""
            try:
                review_paths(sku)["meta"].write_text(json.dumps(fallback_meta, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            metas[sku] = fallback_meta
    return metas


def compare_success(sku):
    meta = read_json(compare_paths(sku)["meta"])
    return bool(meta.get("success"))


def compare_success_with_zero_recommendations(sku):
    meta = read_json(compare_paths(sku)["meta"])
    if not meta.get("success"):
        return False
    try:
        recommendation_count = int(meta.get("recommendation_count"))
        fallback_count = int(meta.get("fallback_recommendation_count"))
    except (TypeError, ValueError):
        return False
    return recommendation_count == 0 and fallback_count == 0


def fetch_compare(client, target):
    sku = str(target.get("sku_id") or "").strip()
    pdp_url = target_url(target, sku)
    current_paths = compare_paths(sku)
    if compare_success(sku):
        return read_json(current_paths["meta"])
    attempt = next_attempt(current_paths["meta"], pdp_url)
    meta = {"sku_id": sku, "stage": "compare", "url": pdp_url, "attempt": attempt, "started_at": now()}
    if attempt_cap_blocks_retry(attempt):
        paths = compare_paths_for_status(sku, target, False)
        meta.update({"success": False, "error": "max_attempts_exceeded"})
        paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return meta

    payload = compare_product_payload(sku)
    paths = compare_paths_for_status(sku, target, False)
    paths["request"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    for transport in fetch_transports():
        if transport == "zenrows" and not client:
            continue
        start = time.perf_counter()
        try:
            response = client.post(
                "https://www.bestbuy.com/gateway/graphql",
                params=graphql_params(),
                headers={
                    "accept": "application/json, text/plain, */*",
                    "content-type": "application/json",
                    "origin": "https://www.bestbuy.com",
                    "referer": pdp_url,
                },
                data=json.dumps(payload),
                timeout=REQUEST_TIMEOUT,
            )
            text = response.text
            response_json = {}
            error = ""
            try:
                response_json = response.json()
            except ValueError:
                error = "invalid_json"
            data = response_json.get("data") if isinstance(response_json, dict) else {}
            recommendations = first_path([data], ["recommendations", "subPlacements", 0, "recommendations"]) or []
            success = response.status_code == 200 and isinstance(data, dict) and isinstance(recommendations, list)
            paths = compare_paths_for_status(sku, target, success)
            paths["response_txt"].write_text(text, encoding="utf-8", errors="replace")
            if response_json:
                paths["response_json"].write_text(json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8")
            paths["headers"].write_text(json.dumps(dict(response.headers), indent=2, ensure_ascii=False), encoding="utf-8")
            paths["request"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            meta.update(
                {
                    "success": success,
                    "status_code": response.status_code,
                    "transport": transport,
                    "fetch_mode": FETCH_MODE,
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "x_request_cost": request_cost(response.headers),
                    "bytes": len(text or ""),
                    "recommendation_count": len(recommendations) if isinstance(recommendations, list) else 0,
                    "finished_at": now(),
                    "error": "" if success else (error or "compare_recommendations_missing"),
                }
            )
        except RequestException as exc:
            paths = compare_paths_for_status(sku, target, False)
            meta.update(
                {
                    "success": False,
                    "status_code": "ERR",
                    "transport": transport,
                    "fetch_mode": FETCH_MODE,
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "x_request_cost": 0,
                    "finished_at": now(),
                    "error": str(exc),
                }
            )
        if meta.get("success"):
            break
    paths["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def fetch_with_retries(fetcher, success_key, client, target):
    total_cost = 0.0
    meta = {}
    run_attempts = 0
    while True:
        meta = fetcher(client, target)
        run_attempts += 1
        total_cost += float(meta.get("x_request_cost") or 0)
        if meta.get(success_key) or not AUTO_RETRY or run_attempts >= MAX_ATTEMPTS:
            break
        retry_sleep = detail_retry_sleep_seconds(run_attempts)
        if retry_sleep > 0:
            time.sleep(retry_sleep)
    meta["x_request_cost_total"] = total_cost
    meta["run_attempts"] = run_attempts
    return meta


def annotate_detail_final_compare(meta, sku):
    try:
        count = len(compare_similar_names_from_detail(sku))
    except Exception:
        count = 0
    meta["final_compare_name_count"] = count
    meta["final_compare_ok"] = count >= DETAIL_SIMILAR_MIN_NAMES
    return count


def detail_needs_similar_retry(meta):
    if not DETAIL_JSON_RESPONSE or not DETAIL_REQUIRE_SIMILAR or not DETAIL_RETRY_ON_MISSING_SIMILAR:
        return False
    if meta.get("success") is not True:
        return False
    count = int(meta.get("final_compare_name_count") or 0)
    return count < DETAIL_SIMILAR_MIN_NAMES


def fetch_detail_with_retries(client, target):
    sku = str(target.get("sku_id") or "").strip()
    total_cost = 0.0
    meta = {}
    run_attempts = 0
    while True:
        meta = fetch_detail(client, target)
        run_attempts += 1
        total_cost += float(meta.get("x_request_cost") or 0)
        annotate_detail_final_compare(meta, sku)
        missing_similar = detail_needs_similar_retry(meta)
        if missing_similar:
            meta["similar_retry_reason"] = f"final_compare_name_count<{DETAIL_SIMILAR_MIN_NAMES}"
        if (meta.get("success") and not missing_similar) or not AUTO_RETRY or run_attempts >= MAX_ATTEMPTS:
            break
        retry_sleep = detail_retry_sleep_seconds(run_attempts)
        if retry_sleep > 0:
            time.sleep(retry_sleep)
    meta["x_request_cost_total"] = total_cost
    meta["run_attempts"] = run_attempts
    if meta:
        try:
            detail_paths(sku)["meta"].write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return meta


def fetch_review20_with_retries(client, target):
    return fetch_with_retries(fetch_review20, "success", client, target)


def fetch_compare_with_retries(client, target):
    return fetch_with_retries(fetch_compare, "success", client, target)


def products_from_detail(sku):
    products = []
    allowed_sku_ids = detail_resolved_sku_ids(sku)
    for payload in detail_payloads(sku):
        for event in payload.get("events", []):
            data = event_data(event)
            product = data.get("productBySkuId") if isinstance(data, dict) else None
            if isinstance(product, dict) and str(product.get("skuId")) in allowed_sku_ids:
                products.append(product)
    review_data = read_json(review_paths(sku)["response_json"])
    review_product = ((review_data.get("data") or {}).get("productBySkuId") or {}) if isinstance(review_data, dict) else {}
    if isinstance(review_product, dict) and str(review_product.get("skuId") or "") in allowed_sku_ids:
        products.append(review_product)
    return products


def product_model_number(products):
    for product in products if isinstance(products, list) else [products]:
        if not isinstance(product, dict):
            continue
        manufacturer = product.get("manufacturer") or {}
        if isinstance(manufacturer, dict):
            model = compact_text(manufacturer.get("modelNumber"))
            if model:
                return model
        model = compact_text(product.get("modelNumber"))
        if model:
            return model
    return ""


def get_it_fast_values_from_detail(sku):
    values = {
        "pick_up_availability": "",
        "fastest_delivery": "",
        "delivery_availability": "",
    }
    if not FETCH_GET_IT_FAST:
        return values
    for payload in detail_payloads(sku):
        for event in payload.get("events", []):
            data = event_data(event)
            if not (isinstance(data, dict) and "fulfillmentGetItFastOptions" in data):
                continue
            candidate = get_it_fast_availability_values({"data": data})
            for key in values:
                if not values[key] and candidate.get(key):
                    values[key] = candidate[key]
    return values


def compare_similar_names_from_detail(sku):
    paths = compare_paths(sku)
    response_json = read_json(paths["response_json"])
    data = response_json.get("data") if isinstance(response_json, dict) else {}
    compare_meta = read_json(paths.get("meta"))

    if not isinstance(data, dict) or not data:
        data = compare_data_from_detail_payloads(sku)

    source_names = compare_recommendation_names(data) if isinstance(data, dict) else []
    if not source_names and isinstance(compare_meta, dict):
        fallback_names = compare_meta.get("fallback_recommendation_names")
        if isinstance(fallback_names, list):
            source_names = [compact_text(name) for name in fallback_names if compact_text(name)]
    if not source_names:
        source_names = recommendation_names_from_detail_payloads(sku)
    if not source_names:
        source_names = compare_names_from_json_response(sku)
    if not source_names:
        source_names = compare_names_from_detail_html(sku)
    if not source_names:
        return []

    names = []
    current = data.get("productBySkuId") if isinstance(data, dict) else {}
    current_name = product_short_name(current) if isinstance(current, dict) else ""
    if not current_name:
        current_name = product_short_name((products_from_detail(sku) or [{}])[-1])
    if current_name:
        names.append(current_name)

    for name in source_names:
        if name and name not in names:
            names.append(name)
    return names


def compare_names_from_json_response(sku):
    paths = detail_paths(sku)
    json_data = read_json(paths.get("json_response"))
    if json_data:
        summary = json_response_compare_summary(json_data, sku)
        try:
            paths["json_response_summary"].write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass
        names = summary.get("compare_names") if isinstance(summary, dict) else []
        return [name for name in names if name] if isinstance(names, list) else []

    summary = read_json(paths.get("json_response_summary"))
    if not isinstance(summary, dict) or summary.get("strict_compare_parser") is not True:
        return []
    names = summary.get("compare_names") if isinstance(summary, dict) else []
    if isinstance(names, list) and names:
        return [name for name in names if name]
    return []


def compare_data_from_json_response(sku):
    paths = detail_paths(sku)
    json_data = read_json(paths.get("json_response"))
    if not isinstance(json_data, dict):
        return {}
    html_text = json_data.get("html") or json_data.get("content") or ""
    allowed_sku_ids = detail_resolved_sku_ids(sku)
    allowed_sku_ids.update(sku_ids_from_html_text(html_text))

    xhr = json_data.get("xhr") or []
    if isinstance(xhr, list):
        for request in xhr:
            if not isinstance(request, dict):
                continue
            request_blob = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
            url = str(request.get("url") or "")
            if "GetCompareProduct" not in request_blob and "/gateway/graphql" not in url:
                continue
            data = strict_compare_response_data(parse_json_value(request.get("body")), sku, allowed_sku_ids)
            if data:
                return data

    for entry in compare_capture_entries_from_html(html_text):
        entry_blob = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        if (
            "GetCompareProduct" not in entry_blob
            and "single-compare" not in entry_blob
            and "ProductCarousel_Recommendations" not in entry_blob
            and "pdp-compare" not in entry_blob
        ):
            continue
        data = strict_compare_response_data(parse_json_value(entry.get("body")), sku, allowed_sku_ids)
        if data:
            return data
    return {}


def compare_current_product_from_detail(sku):
    paths = compare_paths(sku)
    response_json = read_json(paths["response_json"])
    data = response_json.get("data") if isinstance(response_json, dict) else {}
    if not isinstance(data, dict) or not data:
        data = compare_data_from_detail_payloads(sku)
    if not isinstance(data, dict) or not data:
        data = compare_data_from_json_response(sku)
    product = data.get("productBySkuId") if isinstance(data, dict) else {}
    if not isinstance(product, dict):
        return {}
    allowed_sku_ids = detail_resolved_sku_ids(sku)
    if allowed_sku_ids and str(product.get("skuId") or "") not in allowed_sku_ids:
        return {}
    return product


def compare_recommendation_names(data):
    names = []
    if not isinstance(data, dict):
        return names
    for root_key in ("recommendations", "recommendationsV2"):
        subplacements = (((data.get(root_key) or {}).get("subPlacements")) or [])
        for subplacement in subplacements:
            if not isinstance(subplacement, dict):
                continue
            for recommendation in subplacement.get("recommendations") or []:
                if not isinstance(recommendation, dict):
                    continue
                for name in product_names_from_value(recommendation):
                    if name and name not in names:
                        names.append(name)
    return names


def recommendation_names_from_data(data):
    names = []
    if not isinstance(data, dict):
        return names

    def visit(value):
        if isinstance(value, dict):
            subplacements = value.get("subPlacements")
            if isinstance(subplacements, list):
                for subplacement in subplacements:
                    if not isinstance(subplacement, dict):
                        continue
                    for recommendation in subplacement.get("recommendations") or []:
                        if not isinstance(recommendation, dict):
                            continue
                        for name in product_names_from_value(recommendation):
                            if name and name not in names:
                                names.append(name)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    return names


def recommendation_names_from_detail_payloads(sku):
    names = []
    for payload in detail_payloads(sku):
        for event in payload.get("events", []):
            for name in recommendation_names_from_data(event_data(event)):
                if name and name not in names:
                    names.append(name)
    return names


def trade_in_from_detail_payloads(sku):
    fallback = ""
    for payload in detail_payloads(sku):
        for event in payload.get("events", []):
            if event.get("type") not in {"next", "data"}:
                continue
            data = event_data(event)
            if not isinstance(data, dict):
                continue
            amount_text = trade_in_from_offer_data(data, include_generic=False)
            if amount_text:
                return amount_text
            if not fallback:
                fallback = trade_in_from_offer_data(data, include_generic=True)
            for value in nested_strings(data):
                match = trade_in_text_match(value)
                if match:
                    return compact_text(match.group(1))
    return fallback


def compare_names_from_detail_html(sku):
    paths = detail_paths(sku)
    html_path = paths.get("html")
    if not html_path or not html_path.exists():
        return []
    html_text = html_path.read_text(encoding="utf-8", errors="replace")
    if not html_text or "GPC-sku-card" not in html_text:
        return []

    soup = BeautifulSoup(html_text, "html.parser")
    names = []
    for card in soup.find_all(attrs={"data-testid": "GPC-sku-card"}):
        name_node = card.find("h3")
        if not name_node:
            name_link = card.find("a", attrs={"aria-label": True})
            name = name_link.get("aria-label") if name_link else ""
        else:
            name = name_node.get_text(" ", strip=True)
        name = compact_text(name)
        if name and name not in names:
            names.append(name)
    return names


def compare_data_from_detail_payloads(sku):
    allowed_sku_ids = detail_resolved_sku_ids(sku)
    for payload in detail_payloads(sku):
        compare_event_ids = set()
        for event in payload.get("events", []):
            if event.get("type") != "started":
                continue
            op_name = operation_name(event)
            variables = event_variables(event)
            if op_name == "GetCompareProduct" and str(variables.get("skuId") or "") in allowed_sku_ids:
                compare_event_ids.add(str(event.get("id") or ""))
            elif op_name == "ProductCarousel_Recommendations":
                skus = variables.get("skus")
                if isinstance(skus, str):
                    request_skus = {skus}
                elif isinstance(skus, list):
                    request_skus = {str(value) for value in skus if value not in (None, "")}
                else:
                    request_skus = set()
                if request_skus & allowed_sku_ids:
                    compare_event_ids.add(str(event.get("id") or ""))
        if not compare_event_ids:
            continue
        for event in payload.get("events", []):
            if event.get("type") not in {"next", "data"} or str(event.get("id") or "") not in compare_event_ids:
                continue
            data = event_data(event)
            current = data.get("productBySkuId") if isinstance(data, dict) else {}
            if isinstance(current, dict) and str(current.get("skuId") or "") not in allowed_sku_ids:
                continue
            if compare_recommendation_names(data):
                return data
    return {}


def first_value(products, key):
    for product in reversed(products):
        value = product.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""


def first_path(products, path):
    for product in reversed(products):
        current = product
        ok = True
        for part in path:
            if isinstance(part, int):
                if isinstance(current, list) and len(current) > part:
                    current = current[part]
                else:
                    ok = False
                    break
            elif isinstance(current, dict) and part in current:
                current = current[part]
            else:
                ok = False
                break
        if ok and current not in (None, "", [], {}):
            return current
    return ""


def as_list(value):
    if isinstance(value, list):
        return value
    if value in (None, "", [], {}):
        return []
    return [value]


def best_path(products, path, required_keys=()):
    values = []
    for product in products:
        current = product
        ok = True
        for part in path:
            if isinstance(part, int):
                if isinstance(current, list) and len(current) > part:
                    current = current[part]
                else:
                    ok = False
                    break
            elif isinstance(current, dict) and part in current:
                current = current[part]
            else:
                ok = False
                break
        if ok and isinstance(current, dict):
            score = sum(1 for key in required_keys if current.get(key) not in (None, "", [], {}))
            values.append((score, current))
    return sorted(values, key=lambda item: item[0], reverse=True)[0][1] if values else {}


def fulfillment_availabilities(products, detail_key, availability_key):
    for product in products:
        options = product.get("fulfillmentOptions") if isinstance(product, dict) else {}
        if not isinstance(options, dict):
            continue
        for detail in as_list(options.get(detail_key)):
            if not isinstance(detail, dict):
                continue
            for availability in as_list(detail.get(availability_key)):
                if isinstance(availability, dict):
                    yield availability


def best_fulfillment_availability(products, detail_key, availability_key, required_keys=()):
    values = []
    for availability in fulfillment_availabilities(products, detail_key, availability_key):
        score = sum(1 for key in required_keys if availability.get(key) not in (None, "", [], {}))
        values.append((score, availability))
    return sorted(values, key=lambda item: item[0], reverse=True)[0][1] if values else {}


def best_shipping_availability(products):
    values = []
    for product in products:
        details = first_path([product], ["fulfillmentOptions", "shippingDetails"]) or []
        for detail in as_list(details):
            if not isinstance(detail, dict):
                continue
            for shipping in as_list(detail.get("shippingAvailability")):
                if not isinstance(shipping, dict) or not shipping.get("shippingEligible"):
                    continue
                groups = as_list(shipping.get("customerLOSGroup"))
                default_group_id = shipping.get("defaultCustomerLosGroupId")
                score = 1
                if groups:
                    score += 1
                if default_group_id not in (None, ""):
                    score += 3
                if any(isinstance(group, dict) and group.get("price") in (0, 0.0, "0", "0.0") for group in groups):
                    score += 1
                values.append((score, shipping))
    return sorted(values, key=lambda item: item[0], reverse=True)[0][1] if values else {}


def best_price(products):
    best = {}
    best_score = -1
    candidates = []
    for product in products:
        price = product.get("price")
        if isinstance(price, dict):
            candidates.append(price)
        for option in product.get("buyingOptions") or []:
            if not isinstance(option, dict):
                continue
            option_product = option.get("product") if isinstance(option.get("product"), dict) else {}
            option_price = option_product.get("price") if isinstance(option_product.get("price"), dict) else {}
            if option_price:
                candidates.append(option_price)
    for price in candidates:
        score = sum(
            1
            for key in ("displayableCustomerPrice", "customerPrice", "displayableRegularPrice", "regularPrice", "totalSavings")
            if price.get(key) not in (None, "", [], {})
        )
        if score > best_score:
            best = price
            best_score = score
    return best


def spec_value(products, display_name):
    for product in reversed(products):
        for group in product.get("specificationGroups") or []:
            for spec in group.get("specifications") or []:
                if (spec.get("displayName") or "").lower() == display_name.lower():
                    return spec.get("value", "")
    return ""


def spec_value_by_names(products, display_names):
    wanted = {str(name or "").strip().lower() for name in display_names if str(name or "").strip()}
    for product in reversed(products):
        for group in product.get("specificationGroups") or []:
            for spec in group.get("specifications") or []:
                name = str(spec.get("displayName") or "").strip().lower()
                if name in wanted:
                    return spec.get("value", "")
    return ""


def spec_value_by_group_and_names(products, group_names, display_names):
    wanted_groups = {str(name or "").strip().lower() for name in group_names if str(name or "").strip()}
    wanted_names = {str(name or "").strip().lower() for name in display_names if str(name or "").strip()}
    if not wanted_groups or not wanted_names:
        return ""
    for product in reversed(products):
        for group in product.get("specificationGroups") or []:
            group_name = str(group.get("name") or "").strip().lower()
            if group_name not in wanted_groups:
                continue
            for spec in group.get("specifications") or []:
                name = str(spec.get("displayName") or "").strip().lower()
                if name in wanted_names:
                    return spec.get("value", "")
    return ""


def spec_value_containing(products, *needles):
    needles = [str(value or "").strip().lower() for value in needles if str(value or "").strip()]
    if not needles:
        return ""
    for product in reversed(products):
        for group in product.get("specificationGroups") or []:
            for spec in group.get("specifications") or []:
                name = str(spec.get("displayName") or "").strip().lower()
                if all(needle in name for needle in needles):
                    return spec.get("value", "")
    return ""


def ref_capacity_from_name(product_name):
    text = str(product_name or "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:cu\.?\s*ft\.?|cuft|cubic\s*feet)", text, re.I)
    if not match:
        return ""
    amount = match.group(1).rstrip("0").rstrip(".")
    return f"{amount} cubic feet"


def ref_type_from_name(product_name):
    text = str(product_name or "").lower()
    if "drawer" in text:
        return "Drawer"
    if "undercounter" in text or "under counter" in text:
        return "Undercounter"
    return ""


def ldy_attributes_from_product(products, product_name):
    capacity = first_non_empty(
        spec_value_by_names(products, ["Capacity"]),
        spec_value_by_group_and_names(
            products,
            ["Capacity"],
            [
                "Washer Capacity",
                "Washer Capacity (cu. ft.)",
                "Washer Dryer Capacity",
                "Washer Dryer Capacity (cu. ft.)",
            ],
        ),
    )
    loading_type = spec_value_by_names(products, ["Washer Load Type"])
    return {
        "ldy_capacity": capacity,
        "ldy_loading_type": loading_type,
    }


def ref_attributes_from_product(products, product_name):
    capacity = first_non_empty(
        spec_value_by_names(
            products,
            [
                "Total Capacity",
                "Total Capacity (cu. ft.)",
                "Total Interior Capacity",
                "Total Volume",
                "Refrigerator Capacity",
            ],
        ),
        spec_value_containing(products, "total", "capacity"),
        ref_capacity_from_name(product_name),
        spec_value_by_names(products, ["Capacity"]),
    )
    refrigerator_type = first_non_empty(
        spec_value_by_names(
            products,
            [
                "Refrigerator Style",
                "Refrigerator Type",
                "Configuration",
                "Product Type",
                "Appliance Type",
            ],
        ),
        spec_value_containing(products, "refrigerator", "type"),
        spec_value_containing(products, "refrigerator", "style"),
        ref_type_from_name(product_name),
    )
    return {
        "ref_capacity": capacity,
        "ref_refrigerator_type": refrigerator_type,
    }


def offer_count(products):
    for product in reversed(products):
        price = product.get("price") if isinstance(product, dict) else {}
        price = price if isinstance(price, dict) else {}
        gift_skus = price.get("giftSkus")
        if isinstance(gift_skus, list) and gift_skus:
            return str(len(gift_skus))
    for product in reversed(products):
        offers = product.get("offers") if isinstance(product, dict) and isinstance(product.get("offers"), dict) else {}
        sku_offers = offers.get("offers") if isinstance(offers, dict) else []
        if not isinstance(sku_offers, list):
            continue
        hot_offer_count = sum(
            1
            for offer in sku_offers
            if isinstance(offer, dict)
            and offer.get("hotOffer") is True
            and str(offer.get("offerType") or "").strip().lower() != "financing"
        )
        if hot_offer_count:
            return str(hot_offer_count)
    return ""


HHP_PROMOTION_TYPES = {
    "best selling",
    "bundle and save",
    "overall pick",
    "pre-owned",
    "top rated",
    "trade-in offer",
    "trending deal",
}


def hhp_promotion_type(products, html_text):
    if CATEGORY != "HHP":
        return ""
    names = []
    for product in products:
        for badge in (product.get("badges") or []) + (product.get("badgesV2") or []):
            if not isinstance(badge, dict):
                continue
            name = compact_text(badge.get("displayName") or badge.get("label"))
            if name and name.lower() in HHP_PROMOTION_TYPES and name not in names:
                names.append(name)
    if not names and html_text:
        for value in re.findall(
            r'data-component-name="Badge"[^>]*>.*?data-testid="button-label"[^>]*>(.*?)</span>',
            html_text,
            re.I | re.S,
        ):
            name = compact_text(html.unescape(re.sub(r"<[^>]+>", " ", value)))
            if name and name.lower() in HHP_PROMOTION_TYPES and name not in names:
                names.append(name)
    return names[0] if names else ""


def recommendation(products):
    value = first_path(products, ["reviewInfo", "recommendedPercent"])
    return f"{value}% would recommend to a friend" if value not in ("", None) else ""


def review_count_number(*values):
    for value in values:
        if value in ("", None):
            continue
        text = re.sub(r"[^0-9]", "", str(value))
        if text:
            return int(text)
    return None


def target_identity_keys(target):
    keys = [
        str(target.get("sku_id") or "").strip(),
        sku_from_product_url(target.get("product_url")),
        canonical_pdp_url(target.get("product_url") or target.get("detail_url")),
        str(target.get("item") or target.get("bsin") or "").strip(),
    ]
    return [key.lower() for key in keys if key]


@lru_cache(maxsize=1)
def output_review_counts():
    counts = {}
    for path in (DETAIL_ROWS_CSV, FINAL_OUTPUT_CSV):
        for row in load_csv(path):
            count = review_count_number(row.get("count_of_reviews"), row.get("count_of_star_ratings"))
            if count is None:
                continue
            for key in target_identity_keys(row):
                counts[key] = max(counts.get(key, 0), count)
    return counts


def expected_review_count_from_outputs(target):
    counts = output_review_counts()
    values = [counts.get(key) for key in target_identity_keys(target) if key in counts]
    return max(values) if values else None


def expected_review_count(target, sku=None):
    counts = []
    target_count = review_count_number(
        target.get("count_of_reviews"),
        target.get("review_count"),
        target.get("count_of_star_ratings"),
    )
    if target_count is not None:
        counts.append(target_count)
    if sku:
        for product in products_from_detail(sku):
            review_info = product.get("reviewInfo") if isinstance(product, dict) else {}
            count = review_count_number(review_info.get("reviewCount")) if isinstance(review_info, dict) else None
            if count is not None:
                counts.append(count)
    output_count = expected_review_count_from_outputs(target)
    if output_count is not None:
        counts.append(output_count)
    return max(counts) if counts else None


def expected_review_text_count(target, sku=None):
    count = expected_review_count(target, sku)
    if count is None:
        return None
    return min(max(0, count), MAX_REVIEW_TEXTS)


def review20_text_count(sku):
    json_count = review_text_count_from_json(read_json(review_paths(sku)["response_json"]))
    if json_count is not None:
        return json_count
    return review_text_count_from_content(review20_content(sku))


def review_text_count_is_sufficient(actual_count, expected_count):
    if expected_count in (None, 0):
        return True
    return actual_count is not None and actual_count >= expected_count


@lru_cache(maxsize=1)
def output_review_blank_keys():
    keys = set()
    for path in (DETAIL_ROWS_CSV, FINAL_OUTPUT_CSV):
        for row in load_csv(path):
            if review_output_needs_attention(row):
                keys.update(target_identity_keys(row))
    return keys


def output_review_needs_retry(target):
    blank_keys = output_review_blank_keys()
    return any(key in blank_keys for key in target_identity_keys(target))


def review_output_needs_attention(row):
    return bool(review_output_attention_reason(row))


def review_output_attention_reason(row):
    review_count = review_count_number(row.get("count_of_reviews"), row.get("count_of_star_ratings"))
    star_rating = compact_text(row.get("star_rating"))
    if not star_rating or star_rating.lower() == "not yet reviewed" or review_count in (None, 0):
        return ""
    expected_count = min(review_count, MAX_REVIEW_TEXTS)
    actual_count = review_text_count_from_content(row.get("detailed_review_content"))
    if actual_count < expected_count:
        return f"review20_partial_{actual_count}_of_{expected_count}"
    if not compact_text(row.get("recommendation_intent")):
        return "missing_recommendation_intent"
    return ""


def review_info_from_review_response(sku):
    data = read_json(review_paths(sku)["response_json"])
    product = ((data.get("data") or {}).get("productBySkuId") or {}) if isinstance(data, dict) else {}
    review_info = product.get("reviewInfo") if isinstance(product, dict) else {}
    return review_info if isinstance(review_info, dict) else {}


def review_has_recommended_percent(sku):
    if review_info_from_review_response(sku).get("recommendedPercent") not in ("", None):
        return True
    for product in products_from_detail(sku):
        review_info = product.get("reviewInfo") if isinstance(product, dict) else {}
        if isinstance(review_info, dict) and review_info.get("recommendedPercent") not in ("", None):
            return True
    return False


def review_needs_retry(target):
    sku = str(target.get("sku_id") or "").strip()
    if not sku:
        return False
    review_info = (first_value(products_from_detail(sku), "reviewInfo") or {})
    if is_external_review_source(target, review_info):
        return False
    if output_review_needs_retry(target):
        return True
    expected_count = expected_review_count(target, sku)
    if not review_success(sku):
        return expected_count is None or expected_count > 0
    if expected_count in (None, 0):
        return False
    expected_text_count = min(expected_count, MAX_REVIEW_TEXTS)
    if not review_text_count_is_sufficient(review20_text_count(sku), expected_text_count):
        return True
    return False


def has_external_review_text(*values):
    for value in values:
        text = str(value or "")
        if re.search(r"\breviews?\s+from\b", text, flags=re.IGNORECASE):
            return True
    return False


def syndicated_review_summary(review_info):
    if not isinstance(review_info, dict):
        return {}
    summary = review_info.get("syndicatedReviewSummary")
    return summary if isinstance(summary, dict) else {}


def is_external_review_source(target=None, review_info=None):
    target = target or {}
    summary = syndicated_review_summary(review_info)
    if summary:
        return True
    return has_external_review_text(
        target.get("count_of_reviews"),
        target.get("review_count"),
        target.get("count_of_star_ratings"),
        target.get("rating"),
    )


def review20_required_for_target(target, sku=None):
    if is_external_review_source(target):
        return False
    count = review_count_number(
        target.get("count_of_reviews"),
        target.get("review_count"),
        target.get("count_of_star_ratings"),
    )
    if count is None and sku and detail_success(sku):
        review_info = (first_value(products_from_detail(sku), "reviewInfo") or {})
        if isinstance(review_info, dict):
            if is_external_review_source(target, review_info):
                return False
            count = review_count_number(review_info.get("reviewCount"))
    return count is None or count > 0


def recommendation_intent_value(review_count, *values):
    if review_count == 0:
        return ""
    return first_non_empty(*(recommendation_phrase(value) for value in values))


def pickup_text(pickup):
    if not isinstance(pickup, dict) or not pickup.get("pickupEligible"):
        return ""
    return date_to_relative_or_phrase("Pick up", pickup.get("maxDate") or pickup.get("fulfillDate") or pickup.get("promiseByStreetDate"))


def delivery_text(delivery):
    if not isinstance(delivery, dict) or not delivery.get("deliveryEligible"):
        return ""
    slots = as_list(delivery.get("deliverySlots"))
    if slots:
        slot = slots[0] if isinstance(slots[0], dict) else {}
        return date_to_relative_or_phrase("Delivery as soon as", slot.get("date"))
    return ""


def fastest_delivery_text(shipping):
    if not isinstance(shipping, dict) or not shipping.get("shippingEligible"):
        return ""
    groups = as_list(shipping.get("customerLOSGroup"))
    if groups:
        group = groups[0] if isinstance(groups[0], dict) else {}
        default_group_id = shipping.get("defaultCustomerLosGroupId")
        for candidate in groups:
            if not isinstance(candidate, dict):
                continue
            if default_group_id not in (None, "") and str(candidate.get("customerLosGroupId")) == str(default_group_id):
                group = candidate
                break
        date_value = group.get("minLineItemMaxDate") or group.get("maxLineItemMaxDate")
        phrase = fastest_delivery_date_phrase(date_value)
        if phrase:
            if group.get("price") in (0, 0.0, "0", "0.0"):
                phrase = f"{phrase} \u2022 FREE"
            return phrase
    return fastest_delivery_date_phrase(shipping.get("promiseByStreetDate"))


def inventory_status_text(pickup, shipping, delivery):
    if isinstance(pickup, dict):
        if pickup.get("instoreInventoryAvailable") is True or pickup.get("pickupEligible") is True:
            return "In Stock"
    if isinstance(shipping, dict) and shipping.get("shippingEligible") is True:
        return "In Stock"
    if isinstance(delivery, dict) and delivery.get("deliveryEligible") is True:
        return "In Stock"
    explicit_false_values = []
    for value, key in ((pickup, "pickupEligible"), (shipping, "shippingEligible"), (delivery, "deliveryEligible")):
        if isinstance(value, dict) and key in value:
            explicit_false_values.append(value.get(key) is False)
    return "Out of Stock" if explicit_false_values and all(explicit_false_values) else ""


def review20_content(sku):
    path = review_paths(sku)["response_json"]
    reviews = []
    if path.exists():
        data = read_json(path)
        reviews = (((data.get("data") or {}).get("productBySkuId") or {}).get("reviews") or {}).get("results") or []
    if not reviews:
        for payload in detail_payloads(sku):
            for event in payload.get("events", []):
                data = event_data(event)
                product = data.get("productBySkuId") if isinstance(data, dict) else None
                if not isinstance(product, dict) or str(product.get("skuId") or "") != str(sku):
                    continue
                fallback_reviews = ((product.get("reviews") or {}).get("results") or [])
                if fallback_reviews:
                    reviews = fallback_reviews
                    break
            if reviews:
                break
    chunks = []
    for index, review in enumerate(reviews[:20], 1):
        text = compact_text(review.get("text"))
        if text:
            chunks.append(f"review{index} - {text}")
    return " ||| ".join(chunks)


def recommended_percent_from_detail(sku):
    review_value = review_info_from_review_response(sku).get("recommendedPercent")
    if review_value not in ("", None):
        return review_value
    for payload in detail_payloads(sku):
        stack = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                if str(current.get("skuId") or "") == str(sku):
                    review_info = current.get("reviewInfo") or {}
                    value = review_info.get("recommendedPercent")
                    if value not in ("", None):
                        return value
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
    return ""


def sample_fields():
    if CATEGORY in FALLBACK_FINAL_FIELDS:
        return FALLBACK_FINAL_FIELDS[CATEGORY]
    config = db_config()
    table_name = bestbuy_output_table()
    if config and table_name:
        try:
            import psycopg2

            conn = psycopg2.connect(
                host=config.get("host"),
                port=int(config.get("port") or 5432),
                user=config.get("user"),
                password=config.get("password"),
                dbname=config.get("database"),
                connect_timeout=10,
            )
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = %s
                        ORDER BY ordinal_position
                        """,
                        (table_name,),
                    )
                    fields = [row[0] for row in cur.fetchall()]
                    if fields:
                        return fields
        except Exception:
            pass
    with SAMPLE_SCHEMA_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        return next(csv.reader(f))


NO_LONGER_AVAILABLE_PHRASES = (
    "this item is no longer available in new condition",
    "this item is no longer available",
    "see similar items below",
)


def is_detail_no_longer_available_text(*values):
    haystack = compact_text(" ".join(str(value or "") for value in values)).lower()
    return any(phrase in haystack for phrase in NO_LONGER_AVAILABLE_PHRASES)


def is_detail_no_longer_available_product(product):
    if not isinstance(product, dict):
        return False
    return compact_text(product.get("dotComDisplayStatus")).lower() == "inactive"


def has_detail_no_longer_available_product(products):
    return any(is_detail_no_longer_available_product(product) for product in products)


def detail_no_longer_available(sku):
    paths = detail_paths(sku)
    html_path = paths.get("html")
    html_text = html_path.read_text(encoding="utf-8", errors="replace") if html_path and html_path.exists() else ""
    text_parts = [html_text]
    try:
        text_parts.append(BeautifulSoup(html_text or "", "html.parser").get_text(" "))
    except Exception:
        pass
    return is_detail_no_longer_available_text(*text_parts)


def output_row(target):
    sku = str(target.get("sku_id") or "").strip()
    detail_html_path = detail_paths(sku)["html"]
    html_text = detail_html_path.read_text(encoding="utf-8", errors="replace") if detail_html_path.exists() else ""
    selector_values = detail_selector_values(html_text)
    products = products_from_detail(sku)
    get_it_fast_values = get_it_fast_values_from_detail(sku)
    compare_similar_names = compare_similar_names_from_detail(sku)
    compare_current_product = compare_current_product_from_detail(sku)
    spec_products = products + ([compare_current_product] if compare_current_product else [])
    price = best_price(products)
    review_info = first_value(products, "reviewInfo") or {}
    review_count = review_count_number(review_info.get("reviewCount"), target.get("review_count"))
    external_reviews = is_external_review_source(target, review_info)
    not_yet_reviewed = external_reviews or has_not_yet_reviewed_text(
        selector_values.get("top_star_rating"),
        selector_values.get("star_rating"),
        target.get("rating"),
    )
    rating_value = first_non_empty(
        review_info.get("averageRating"),
        target.get("rating"),
        selector_values.get("top_star_rating"),
        selector_values.get("star_rating"),
    )
    rating_number = numeric_rating(rating_value)
    if review_count == 0 or (rating_number == 0 and review_count in (None, 0)):
        not_yet_reviewed = True
        review_count = 0
    no_longer_available = (
        has_detail_no_longer_available_product(products)
        or detail_no_longer_available(sku)
        or is_detail_no_longer_available_text(
            selector_values.get("final_sku_price_no_longer_available")
        )
    )
    final_price, original_price, savings = no_longer_available_price_fields(
        *price_output_fields(price, target, selector_values),
        unavailable=no_longer_available,
    )
    pickup = best_fulfillment_availability(
        products,
        "ispuDetails",
        "ispuAvailability",
        ("maxDate", "fulfillDate", "promiseByStreetDate"),
    )
    shipping = best_shipping_availability(products)
    delivery = best_fulfillment_availability(
        products,
        "deliveryDetails",
        "deliveryAvailability",
        ("deliverySlots",),
    )
    delivery_slots = as_list(delivery.get("deliverySlots")) if isinstance(delivery, dict) else []
    delivery_slot = delivery_slots[0].get("date") if delivery_slots and isinstance(delivery_slots[0], dict) else ""
    screen = spec_value(products, "Screen Size Class") or spec_value(products, "Screen Size")
    energy = spec_value(spec_products, "Estimated Annual Electricity Use")
    model_year = spec_value(spec_products, "Model Year")
    product_name = first_path(products, ["name", "short"]) or target.get("product_name", "")
    product_url = first_non_empty(
        target.get("product_url"),
        first_path(products, ["url", "skuSpecificUrl"]),
        first_path(products, ["url", "pdp"]),
    )
    bsin = first_value(products, "bsin") or target.get("bsin", "")
    hhp_attrs = hhp_attributes_from_product(products, product_name, sku) if CATEGORY == "HHP" else {}
    ldy_attrs = ldy_attributes_from_product(spec_products, product_name) if CATEGORY == "LDY" else {}
    ref_attrs = ref_attributes_from_product(spec_products, product_name) if CATEGORY == "REF" else {}

    crawl_dt = datetime.now()
    row = {
        "id": "",
        "product": (target.get("category_key") or CATEGORY).upper(),
        "item": bsin,
        "sku": product_model_number(products) if CATEGORY in {"REF", "LDY"} else "",
        "sku_id": sku,
        "account_name": "Bestbuy",
        "page_type": page_type_from_target(target),
        "count_of_reviews": "0"
        if not_yet_reviewed
        else int_commas(review_info.get("reviewCount") or target.get("review_count")),
        "retailer_sku_name": first_non_empty(product_name, selector_values.get("retailer_sku_name")),
        "product_url": product_url,
        "star_rating": "Not yet reviewed"
        if not_yet_reviewed
        else first_non_empty(
            rating_value,
            "Not yet reviewed",
        ),
        "count_of_star_ratings": "0"
        if not_yet_reviewed
        else int_commas(review_info.get("reviewCount") or target.get("review_count")),
        "screen_size": first_non_empty(screen, selector_values.get("screen_size")),
        "final_sku_price": final_price,
        "original_sku_price": original_price,
        "savings": savings,
        "offer": first_non_empty(target.get("offer"), target.get("offer_count"), offer_count(products)),
        "pick_up_availability": first_text_starting(
            "Pick up",
            pickup_text(pickup),
            get_it_fast_values.get("pick_up_availability"),
            selector_values.get("pick_up_availability"),
            target.get("pick_up_availability"),
        ),
        "fastest_delivery": first_text_starting(
            "Get",
            fastest_delivery_text(shipping),
            get_it_fast_values.get("fastest_delivery"),
            fastest_delivery_from_html(html_text),
            selector_values.get("fastest_delivery"),
            target.get("fastest_delivery"),
        ),
        "delivery_availability": first_text_starting(
            "Delivery",
            delivery_text(delivery),
            delivery_from_html(html_text),
            date_to_relative_or_phrase("Delivery as soon as", delivery_slot),
            selector_values.get("delivery_availability"),
            target.get("delivery_availability"),
        ),
        "available_quantity_for_purchase": first_non_empty(target.get("available_quantity_for_purchase"), pickup.get("quantity") if isinstance(pickup, dict) else ""),
        "inventory_status": first_non_empty(target.get("inventory_status"), inventory_status_text(pickup, shipping, delivery)),
        "sku_status": listing_sku_status(target),
        "trade_in": first_non_empty(
            selector_values.get("trade_in"),
            trade_in_from_html(html_text),
            trade_in_from_detail_payloads(sku),
            trade_in_from_products(products),
        ),
        "hhp_storage": hhp_attrs.get("hhp_storage", ""),
        "hhp_color": hhp_attrs.get("hhp_color", ""),
        "hhp_carrier": hhp_attrs.get("hhp_carrier", ""),
        "ldy_capacity": ldy_attrs.get("ldy_capacity", ""),
        "ldy_loading_type": ldy_attrs.get("ldy_loading_type", ""),
        "ref_capacity": ref_attrs.get("ref_capacity", ""),
        "ref_refrigerator_type": ref_attrs.get("ref_refrigerator_type", ""),
        "detailed_review_content": "" if not_yet_reviewed else review20_content(sku),
        "summarized_review_content": "",
        "top_mentions": "",
        "recommendation_intent": ""
        if not_yet_reviewed
        else recommendation_intent_value(
            review_count,
            selector_values.get("recommendation_intent"),
            selector_values.get("reviewpage_recommendation_intent_fallback"),
            selector_values.get("reviewpage_recommendation_intent_fallback2"),
            selector_values.get("reviewpage_recommendation_intent_fallback3"),
            selector_values.get("reviewpage_recommendation_intent_fallback4"),
            recommendation_from_html(html_text),
            recommended_percent_from_detail(sku),
            recommendation(products),
        ),
        "main_rank": target.get("main_rank", ""),
        "bsr_rank": target.get("bsr_rank", ""),
        "promotion_position": target.get("promotion_position", ""),
        "trend_rank": target.get("trend_rank", ""),
        "retailer_sku_name_similar": " ||| ".join(compare_similar_names[:4]),
        "estimated_annual_electricity_use": clean_energy(energy),
        "promotion_type": first_non_empty(hhp_promotion_type(products, html_text), target.get("promotion_type", "")),
        "calendar_week": f"w{crawl_dt.isocalendar().week}",
        "crawl_datetime": crawl_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "crawl_strdatetime": crawl_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "model_year": model_year,
        "batch_id": RUN_BATCH_ID,
        "country": "SEA",
    }
    if CATEGORY != "HHP":
        row["shipping_info"] = ""
    if CATEGORY == "TV":
        for field in ALL_AVAILABILITY_FIELDS:
            row[field] = ""
    for field, value in selector_values.items():
        row.setdefault(field, value)
    return row


def build_outputs(targets):
    rows = []
    failures = []
    for target in targets:
        sku = str(target.get("sku_id") or "").strip()
        dmeta = read_json(detail_paths(sku)["meta"])
        rmeta = read_json(review_paths(sku)["meta"])
        cmeta = read_json(compare_paths(sku)["meta"])
        row = output_row(target)
        rows.append(row)
        if not dmeta.get("success"):
            failures.append(
                {
                    "sku_id": sku,
                    "stage": "detail",
                    "attempt": dmeta.get("attempt", 0),
                    "status_code": dmeta.get("status_code", ""),
                    "error": dmeta.get("error", "missing_detail"),
                    "retryable": str(int(int(dmeta.get("attempt", 0) or 0) < MAX_ATTEMPTS)),
                }
            )
        if (
            DETAIL_REQUIRE_SIMILAR
            and dmeta.get("success")
            and not compact_text(row.get("retailer_sku_name_similar"))
            and not detail_no_longer_available(sku)
            and not compare_success_with_zero_recommendations(sku)
        ):
            failures.append(
                {
                    "sku_id": sku,
                    "stage": "detail_similar",
                    "attempt": dmeta.get("attempt", 0),
                    "status_code": dmeta.get("status_code", ""),
                    "error": dmeta.get("similar_retry_reason", "compare_response_not_captured_in_render_window"),
                    "retryable": str(int(int(dmeta.get("attempt", 0) or 0) < MAX_ATTEMPTS)),
                }
            )
        review_attention_error = review_output_attention_reason(row)
        if review_attention_error:
            failures.append(
                {
                    "sku_id": sku,
                    "stage": "review20",
                    "attempt": rmeta.get("attempt", 0),
                    "status_code": rmeta.get("status_code", ""),
                    "error": rmeta.get("error") or review_attention_error,
                    "retryable": str(int(int(rmeta.get("attempt", 0) or 0) < MAX_ATTEMPTS)),
                }
            )
        if FETCH_COMPARE and not cmeta.get("success"):
            failures.append(
                {
                    "sku_id": sku,
                    "stage": "compare",
                    "attempt": cmeta.get("attempt", 0),
                    "status_code": cmeta.get("status_code", ""),
                    "error": cmeta.get("error", "missing_compare"),
                    "retryable": str(int(int(cmeta.get("attempt", 0) or 0) < MAX_ATTEMPTS)),
                }
            )
    return rows, failures


def detail_fetch_mode_label():
    if DETAIL_DIRECT_GRAPHQL:
        return "direct_graphql_batch"
    if DETAIL_PDP_FALLBACK:
        return "pdp_render"
    return "disabled"


def compact_log_value(value, limit=140):
    text = compact_text(value)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def log_value(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return ""
    return compact_log_value(value, 180)


def format_log_line(tag, *items, **fields):
    parts = [f"[{tag}]"]
    parts.extend(str(item) for item in items if item not in (None, ""))
    for key, value in fields.items():
        if value in (None, "", [], {}):
            continue
        parts.append(f"{key}={log_value(value)}")
    return " ".join(parts)


def count_text(value):
    return str(value) if value not in (None, "", [], {}) else ""


def stage_text(name, meta, active=True, fetched=False, count_label="", count_value=""):
    if not active:
        return f"{name}=off"
    if not isinstance(meta, dict) or not meta:
        return f"{name}=missing"
    state = "ok" if meta.get("success") else "fail"
    details = ["fetch" if fetched else "cache"]
    count = count_text(count_value)
    if count_label and count:
        details.append(f"{count_label}:{count}")
    if state == "fail":
        status = count_text(meta.get("status_code"))
        error = compact_log_value(meta.get("error", ""), 80)
        if status:
            details.append(f"http:{status}")
        if error:
            details.append(error)
    return f"{name}={state}({','.join(details)})"


def process_log_line(index, total, sku, dmeta, rmeta, cmeta, fetched_detail=False, fetched_review=False, fetched_compare=False):
    detail_ok = bool(dmeta.get("success")) if isinstance(dmeta, dict) else False
    review_ok = bool(rmeta.get("success")) if isinstance(rmeta, dict) else False
    compare_ok = (not FETCH_COMPARE) or (bool(cmeta.get("success")) if isinstance(cmeta, dict) else False)
    status = "ok" if detail_ok and review_ok and compare_ok else "fail"
    similar_count = count_text(dmeta.get("final_compare_name_count", dmeta.get("json_response_compare_name_count", "")))
    item = f"{index}/{total}"
    return format_log_line(
        "detail:item",
        item,
        f"sku={sku}",
        f"status={status}",
        stage_text("detail", dmeta, True, fetched_detail),
        stage_text("review", rmeta, True, fetched_review, "reviews", rmeta.get("review_count_returned", "")),
        stage_text("compare", cmeta, FETCH_COMPARE, fetched_compare, "recs", cmeta.get("recommendation_count", "")),
        similar=similar_count,
    )


def failure_stage_counts(failures):
    counts = {}
    for failure in failures:
        stage = str(failure.get("stage") or "unknown")
        counts[stage] = counts.get(stage, 0) + 1
    return counts


def main():
    started_at = now()
    targets = target_rows(apply_filters=True)
    output_targets = target_rows(apply_filters=False)
    api_key = "" if REBUILD_ONLY else os.getenv("ZENROWS_API_KEY")
    client = ZenRowsClient(api_key) if api_key else None
    transports = [] if REBUILD_ONLY else fetch_transports()
    if not REBUILD_ONLY and "browser_graphql" in transports:
        open_detail_browser_page()
        atexit.register(close_detail_browser_page)
    can_fetch_network = not REBUILD_ONLY and (
        ("zenrows" in transports and client is not None) or "browser_graphql" in transports
    )

    RAW_DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    RAW_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    RAW_COMPARE_DIR.mkdir(parents=True, exist_ok=True)
    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    BENCHMARKS_DIR.mkdir(parents=True, exist_ok=True)
    if not REBUILD_ONLY and DETAIL_BENCHMARKS_CSV.exists():
        DETAIL_BENCHMARKS_CSV.unlink()

    if STAGE not in {"all", "detail", "review"}:
        raise RuntimeError("BESTBUY_DETAIL_STAGE must be one of: all, detail, review")

    fetch_mode_label = detail_fetch_mode_label()
    direct_compare = DETAIL_DIRECT_GRAPHQL and FETCH_COMPARE
    pdp_compare_js = DETAIL_PDP_FALLBACK and (
        DETAIL_COMPARE_CAPTURE_HOOK
        or DETAIL_COMPARE_SCROLL_SCAN
        or DETAIL_COMPARE_DOM_OBSERVER
        or DETAIL_COMPARE_FORCE_FETCH
    )
    if REBUILD_ONLY:
        network_mode = "cache_only"
    elif "browser_graphql" in transports:
        network_mode = "browser_graphql"
    elif can_fetch_network:
        network_mode = "zenrows"
    else:
        network_mode = "missing_api_key"
    use_detail_sku_batch = (
        can_fetch_network
        and DETAIL_DIRECT_GRAPHQL
        and DETAIL_SKU_BATCH_SIZE > 1
        and STAGE in {"all", "detail"}
    )
    use_review20_sku_batch = (
        can_fetch_network
        and DETAIL_DIRECT_GRAPHQL
        and REVIEW20_BATCH_SIZE > 1
        and STAGE == "review"
    )
    print(
        format_log_line(
            "detail:plan",
            category=CATEGORY,
            batch=RUN_BATCH_ID,
            stage=STAGE,
            mode=fetch_mode_label,
            targets=f"{len(targets)}/{len(output_targets)}",
            workers=WORKERS,
            sku_batch=DETAIL_SKU_BATCH_SIZE if use_detail_sku_batch else "",
            review_batch=REVIEW20_BATCH_SIZE if use_review20_sku_batch else "",
            force=FORCE_REFRESH,
            rebuild=REBUILD_ONLY,
        ),
        flush=True,
    )
    print(
        format_log_line(
            "detail:network",
            transport=network_mode,
            detail_call="gateway_graphql_sku_batch_post"
            if use_detail_sku_batch
            else ("gateway_graphql_post" if DETAIL_DIRECT_GRAPHQL else "pdp_render"),
            review_call="gateway_graphql_review_sku_batch_post"
            if use_review20_sku_batch
            else ("gateway_graphql_post" if DETAIL_DIRECT_GRAPHQL else "off"),
            compare="batched" if direct_compare else ("pdp_js" if pdp_compare_js else "off"),
            fulfillment="dynamic_batched"
            if FETCH_FULFILLMENT_DYNAMIC
            else ("get_it_fast_batched" if FETCH_GET_IT_FAST else "off"),
        ),
        flush=True,
    )

    if not can_fetch_network and not REBUILD_ONLY:
        # Cached parse-only mode is useful during local development.
        if STAGE == "detail":
            missing = [row.get("sku_id") for row in targets if not detail_success(row.get("sku_id"))]
        elif STAGE == "review":
            missing = [row.get("sku_id") for row in targets if not review_success(row.get("sku_id"))]
        else:
            missing = [
                row.get("sku_id")
                for row in targets
                if not detail_success(row.get("sku_id"))
                or (review20_required_for_target(row, row.get("sku_id")) and not review_success(row.get("sku_id")))
            ]
        if missing:
            raise RuntimeError("Set ZENROWS_API_KEY or provide cached detail/review files for all selected SKUs")

    benchmark_lock = Lock()

    def process_target(index, target):
        sku = str(target.get("sku_id") or "").strip()
        fetched_detail = False
        fetched_review = False
        fetched_compare = False
        if STAGE in {"all", "detail"}:
            should_fetch_detail = can_fetch_network and (FORCE_REFRESH or not detail_success(sku))
            dmeta = fetch_detail_with_retries(client, target) if should_fetch_detail else read_json(detail_paths(sku)["meta"])
            fetched_detail = bool(should_fetch_detail)
        else:
            dmeta = read_json(detail_paths(sku)["meta"])
        if STAGE in {"all", "review"}:
            should_fetch_review = can_fetch_network and (FORCE_REFRESH or review_needs_retry(target))
            rmeta = fetch_review20_with_retries(client, target) if should_fetch_review else read_json(review_paths(sku)["meta"])
            fetched_review = bool(should_fetch_review)
        else:
            rmeta = read_json(review_paths(sku)["meta"])
        if FETCH_COMPARE and STAGE in {"all", "detail"}:
            should_fetch_compare = can_fetch_network and dmeta.get("success") and not compare_success(sku)
            cmeta = fetch_compare_with_retries(client, target) if should_fetch_compare else read_json(compare_paths(sku)["meta"])
            fetched_compare = bool(should_fetch_compare)
        else:
            cmeta = read_json(compare_paths(sku)["meta"])
        with benchmark_lock:
            append_detail_benchmark(target, DETAIL_ROOT, DETAIL_BENCHMARKS_CSV)
        return index, sku, dmeta, rmeta, cmeta, fetched_detail, fetched_review, fetched_compare

    detail_cost = 0.0
    review_cost = 0.0
    compare_cost = 0.0
    detail_refill_cost = 0.0
    detail_refill_calls = 0
    detail_refill_target_count = 0
    # Actual ZenRows POST counts this run (not cost-derived estimates).
    # In batch mode one POST bundles detail+review+compare, so it counts once as detail_calls.
    detail_calls = 0
    review_calls = 0
    compare_calls = 0

    def add_batch_accounting(batch_metas):
        batch_cost = 0.0
        chunk_fetched = False
        for meta in batch_metas.values():
            if meta.get("fetched_this_run"):
                chunk_fetched = True
                batch_cost = max(batch_cost, float(meta.get("batch_x_request_cost") or 0))
        return batch_cost, int(chunk_fetched)

    def add_review_batch_accounting(batch_metas):
        batch_cost = 0.0
        batch_fetched = False
        fallback_cost = 0.0
        fallback_calls = 0
        for meta in batch_metas.values():
            if meta.get("batch_fetched_this_run"):
                batch_fetched = True
                batch_cost = max(batch_cost, float(meta.get("batch_x_request_cost") or 0))
            if meta.get("single_fallback_fetched_this_run"):
                fallback_calls += 1
                fallback_cost += float(meta.get("x_request_cost_total", meta.get("x_request_cost") or 0) or 0)
        return batch_cost + fallback_cost, int(batch_fetched) + fallback_calls

    if REBUILD_ONLY:
        print(format_log_line("detail:rebuild", output_targets=len(output_targets)), flush=True)
    elif use_detail_sku_batch:
        for offset, chunk in detail_batch_chunks(targets, DETAIL_SKU_BATCH_SIZE):
            batch_metas = fetch_detail_sku_batch(client, chunk)
            batch_cost, batch_calls = add_batch_accounting(batch_metas)
            detail_cost += batch_cost
            if batch_calls:
                # One batch POST returns detail+review+compare together for the whole chunk.
                detail_calls += batch_calls
            for local_index, target in enumerate(chunk, 1):
                index = offset + local_index
                sku = str(target.get("sku_id") or "").strip()
                dmeta = batch_metas.get(sku) or read_json(detail_paths(sku)["meta"])
                rmeta = read_json(review_paths(sku)["meta"])
                cmeta = read_json(compare_paths(sku)["meta"])
                fetched_detail = bool(dmeta.get("fetched_this_run"))
                with benchmark_lock:
                    append_detail_benchmark(target, DETAIL_ROOT, DETAIL_BENCHMARKS_CSV)
                print(
                    process_log_line(
                        index,
                        len(targets),
                        sku,
                        dmeta,
                        rmeta,
                        cmeta,
                        fetched_detail,
                        fetched_detail,
                        fetched_detail and FETCH_COMPARE,
                    ),
                    flush=True,
                )
        if DETAIL_SKU_BATCH_REFILL:
            refill_targets = [target for target in targets if target_needs_detail_batch_refill(target)]
            detail_refill_target_count = len(refill_targets)
            refill_sizes = [DETAIL_SKU_BATCH_SIZE]
            if DETAIL_SKU_BATCH_REFILL_SINGLE_FALLBACK and DETAIL_SKU_BATCH_SIZE > 1:
                refill_sizes.append(1)
            for refill_index, refill_size in enumerate(refill_sizes, 1):
                if not refill_targets:
                    break
                print(
                    format_log_line(
                        "detail:refill",
                        pass_no=refill_index,
                        chunk_size=refill_size,
                        targets=len(refill_targets),
                    ),
                    flush=True,
                )
                for offset, chunk in detail_batch_chunks(refill_targets, refill_size):
                    batch_metas = fetch_detail_sku_batch(
                        client,
                        chunk,
                        force_retry=True,
                        max_batch_attempts=MAX_ATTEMPTS,
                        retry_label=f"refill_{refill_size}",
                    )
                    batch_cost, batch_calls = add_batch_accounting(batch_metas)
                    detail_cost += batch_cost
                    detail_refill_cost += batch_cost
                    detail_calls += batch_calls
                    detail_refill_calls += batch_calls
                    for local_index, target in enumerate(chunk, 1):
                        index = offset + local_index
                        sku = str(target.get("sku_id") or "").strip()
                        dmeta = batch_metas.get(sku) or read_json(detail_paths(sku)["meta"])
                        rmeta = read_json(review_paths(sku)["meta"])
                        cmeta = read_json(compare_paths(sku)["meta"])
                        print(
                            process_log_line(
                                index,
                                len(refill_targets),
                                sku,
                                dmeta,
                                rmeta,
                                cmeta,
                                bool(dmeta.get("fetched_this_run")),
                                bool(dmeta.get("fetched_this_run")),
                                bool(dmeta.get("fetched_this_run")) and FETCH_COMPARE,
                            ),
                            flush=True,
                        )
                refill_targets = [target for target in refill_targets if target_needs_detail_batch_refill(target)]
            if detail_refill_target_count:
                print(
                    format_log_line(
                        "detail:refill_done",
                        initial_targets=detail_refill_target_count,
                        remaining=len(refill_targets),
                        calls=detail_refill_calls,
                        cost_usd=round(detail_refill_cost, 7),
                    ),
                    flush=True,
                )
    elif use_review20_sku_batch:
        for offset, chunk in detail_batch_chunks(targets, REVIEW20_BATCH_SIZE):
            batch_metas = fetch_review20_batch(client, chunk)
            batch_cost, batch_calls = add_review_batch_accounting(batch_metas)
            review_cost += batch_cost
            review_calls += batch_calls
            for local_index, target in enumerate(chunk, 1):
                index = offset + local_index
                sku = str(target.get("sku_id") or "").strip()
                dmeta = read_json(detail_paths(sku)["meta"])
                rmeta = batch_metas.get(sku) or read_json(review_paths(sku)["meta"])
                cmeta = read_json(compare_paths(sku)["meta"])
                fetched_review = bool(
                    rmeta.get("fetched_this_run")
                    or rmeta.get("batch_fetched_this_run")
                    or rmeta.get("single_fallback_fetched_this_run")
                )
                with benchmark_lock:
                    append_detail_benchmark(target, DETAIL_ROOT, DETAIL_BENCHMARKS_CSV)
                print(
                    process_log_line(
                        index,
                        len(targets),
                        sku,
                        dmeta,
                        rmeta,
                        cmeta,
                        False,
                        fetched_review,
                        False,
                    ),
                    flush=True,
                )
    elif WORKERS > 1 and len(targets) > 1:
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [executor.submit(process_target, index, target) for index, target in enumerate(targets, 1)]
            for future in as_completed(futures):
                index, sku, dmeta, rmeta, cmeta, fetched_detail, fetched_review, fetched_compare = future.result()
                if fetched_detail:
                    detail_calls += 1
                    detail_cost += float(dmeta.get("x_request_cost_total", dmeta.get("x_request_cost") or 0) or 0)
                if fetched_review:
                    review_calls += 1
                    review_cost += float(rmeta.get("x_request_cost_total", rmeta.get("x_request_cost") or 0) or 0)
                if fetched_compare:
                    compare_calls += 1
                    compare_cost += float(cmeta.get("x_request_cost_total", cmeta.get("x_request_cost") or 0) or 0)
                print(
                    process_log_line(
                        index,
                        len(targets),
                        sku,
                        dmeta,
                        rmeta,
                        cmeta,
                        fetched_detail,
                        fetched_review,
                        fetched_compare,
                    ),
                    flush=True,
                )
    else:
        for index, target in enumerate(targets, 1):
            index, sku, dmeta, rmeta, cmeta, fetched_detail, fetched_review, fetched_compare = process_target(index, target)
            if fetched_detail:
                detail_calls += 1
                detail_cost += float(dmeta.get("x_request_cost_total", dmeta.get("x_request_cost") or 0) or 0)
            if fetched_review:
                review_calls += 1
                review_cost += float(rmeta.get("x_request_cost_total", rmeta.get("x_request_cost") or 0) or 0)
            if fetched_compare:
                compare_calls += 1
                compare_cost += float(cmeta.get("x_request_cost_total", cmeta.get("x_request_cost") or 0) or 0)
            print(
                process_log_line(
                    index,
                    len(targets),
                    sku,
                    dmeta,
                    rmeta,
                    cmeta,
                    fetched_detail,
                    fetched_review,
                    fetched_compare,
                ),
                flush=True,
            )

    enriched_rows, failures = build_outputs(output_targets)
    write_csv(DETAIL_ROWS_CSV, enriched_rows)
    write_csv(FAILURES_CSV, failures, ["sku_id", "stage", "attempt", "status_code", "error", "retryable"])
    print(
        format_log_line(
            "detail:output",
            rows=len(enriched_rows),
            failures=len(failures),
            final=rel_path(FINAL_OUTPUT_CSV),
        ),
        flush=True,
    )
    if failures:
        counts = failure_stage_counts(failures)
        counts_text = ",".join(f"{stage}:{count}" for stage, count in sorted(counts.items()))
        print(format_log_line("detail:failures", total=len(failures), by_stage=counts_text), flush=True)
        for failure in failures[:DETAIL_LOG_FAILURE_LIMIT]:
            print(
                format_log_line(
                    "detail:failure",
                    sku=failure.get("sku_id"),
                    stage=failure.get("stage"),
                    status=failure.get("status_code"),
                    retryable=failure.get("retryable"),
                    error=compact_log_value(failure.get("error", ""), 100),
                ),
                flush=True,
            )
        remaining = len(failures) - DETAIL_LOG_FAILURE_LIMIT
        if remaining > 0:
            print(format_log_line("detail:failure", more=remaining, csv=rel_path(FAILURES_CSV)), flush=True)
    else:
        print(format_log_line("detail:failures", total=0), flush=True)
    fields = sample_fields()
    for row in enriched_rows:
        for field in fields:
            row.setdefault(field, "")
    preserved_availability = preserve_existing_availability(enriched_rows)
    final_rows = [{field: row.get(field, "") for field in fields} for row in enriched_rows]
    product_list_rows = []
    for enriched_row, final_row in zip(enriched_rows, final_rows):
        source_row = dict(enriched_row)
        source_row.update(final_row)
        product_list_rows.append(source_row)
    if preserved_availability:
        print(
            format_log_line("detail:preserve_availability", rows=preserved_availability),
            flush=True,
        )
    write_csv(FINAL_OUTPUT_CSV, final_rows, fields)
    product_list_update = update_product_list_from_detail_rows(product_list_rows)
    if product_list_update["fields"]:
        print(
            format_log_line(
                "detail:product_list",
                rows=product_list_update["rows"],
                updated=product_list_update["updated"],
                fields=product_list_update["fields"],
                csv=rel_path(PRODUCT_LIST_CSV),
            ),
            flush=True,
        )
    benchmark_rows = write_detail_benchmarks(TARGET_CSV, DETAIL_ROOT, DETAIL_BENCHMARKS_CSV)

    manifest = {
        "run_type": "step08_detail_enrichment",
        "started_at": started_at,
        "finished_at": now(),
        "target_csv": rel_path(TARGET_CSV),
        "limit": LIMIT,
        "retry_only": RETRY_ONLY,
        "retry_missing_similar": RETRY_MISSING_SIMILAR,
        "rebuild_only": REBUILD_ONLY,
        "force_refresh": FORCE_REFRESH,
        "stage": STAGE,
        "workers": WORKERS,
        "max_attempts": MAX_ATTEMPTS,
        "auto_retry": AUTO_RETRY,
        "retry_sleep_seconds": DETAIL_RETRY_SLEEP_SECONDS,
        "retry_sleep_sequence": DETAIL_RETRY_SLEEP_SEQUENCE,
        "target_skus": sorted(TARGET_SKUS),
        "detail_scroll": DETAIL_SCROLL,
        "detail_scroll_network_idle": DETAIL_SCROLL_NETWORK_IDLE,
        "detail_compare_capture_hook": DETAIL_COMPARE_CAPTURE_HOOK,
        "detail_compare_scroll_scan": DETAIL_COMPARE_SCROLL_SCAN,
        "detail_compare_dom_observer": DETAIL_COMPARE_DOM_OBSERVER,
        "detail_compare_force_fetch": DETAIL_COMPARE_FORCE_FETCH,
        "detail_compare_force_fetch_wait": DETAIL_COMPARE_FORCE_FETCH_WAIT,
        "detail_json_response": DETAIL_JSON_RESPONSE,
        "detail_json_wait": DETAIL_JSON_WAIT,
        "detail_require_similar": DETAIL_REQUIRE_SIMILAR,
        "detail_retry_on_missing_similar": DETAIL_RETRY_ON_MISSING_SIMILAR,
        "detail_similar_min_names": DETAIL_SIMILAR_MIN_NAMES,
        "detail_fulfillment_endpoint_fetch": False,
        "detail_fulfillment_direct_batch": FETCH_FULFILLMENT_DYNAMIC or FETCH_GET_IT_FAST,
        "detail_fulfillment_dynamic_batch": FETCH_FULFILLMENT_DYNAMIC,
        "detail_get_it_fast_batch": FETCH_GET_IT_FAST and not FETCH_FULFILLMENT_DYNAMIC,
        "use_db_selectors": USE_DB_SELECTORS,
        "fetch_mode": FETCH_MODE,
        "fetch_transports": fetch_transports(),
        "target_count": len(output_targets),
        "processed_count": len(targets),
        "success_count": len(enriched_rows),
        "failure_count": len(failures),
        "detail_cost_usd_this_run": detail_cost,
        "review_cost_usd_this_run": review_cost,
        "compare_cost_usd_this_run": compare_cost,
        "detail_refill_enabled": DETAIL_SKU_BATCH_REFILL,
        "detail_refill_target_count": detail_refill_target_count,
        "detail_refill_calls_this_run": detail_refill_calls,
        "detail_refill_cost_usd_this_run": detail_refill_cost,
        "total_cost_usd_this_run": detail_cost + review_cost + compare_cost,
        "total_cost_krw_1550_this_run": round((detail_cost + review_cost + compare_cost) * KRW_PER_USD, 2),
        "detail_calls_this_run": detail_calls,
        "review_calls_this_run": review_calls,
        "compare_calls_this_run": compare_calls,
        "total_calls_this_run": detail_calls + review_calls + compare_calls,
        "detail_rows_csv": rel_path(DETAIL_ROWS_CSV),
        "failures_csv": rel_path(FAILURES_CSV),
        "detail_benchmarks_csv": rel_path(DETAIL_BENCHMARKS_CSV),
        "detail_benchmark_rows": len(benchmark_rows),
        "final_output_csv": rel_path(FINAL_OUTPUT_CSV),
        "product_list_csv": rel_path(PRODUCT_LIST_CSV),
        "product_list_rows_updated": product_list_update["updated"],
        "product_list_fields_updated": product_list_update["fields"],
    }
    # detail/review are fetched in separate step08 invocations (step08=detail, step09=review)
    # that write the SAME manifest. Merge per-stage call+cost so the later (review) run does
    # not wipe the earlier (detail batch) numbers. run_root is per-date, so no stale carryover.
    runs_by_stage = dict(read_json(MANIFEST_PATH).get("runs_by_stage") or {})
    runs_by_stage[STAGE] = {
        "detail_calls": detail_calls,
        "review_calls": review_calls,
        "compare_calls": compare_calls,
        "detail_cost_usd": detail_cost,
        "review_cost_usd": review_cost,
        "compare_cost_usd": compare_cost,
    }
    manifest["runs_by_stage"] = runs_by_stage
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        format_log_line(
            "detail:done",
            processed=len(targets),
            output_rows=len(enriched_rows),
            failures=len(failures),
            cost_usd=round(detail_cost + review_cost + compare_cost, 7),
            manifest=rel_path(MANIFEST_PATH),
        ),
        flush=True,
    )
    if DETAIL_PRINT_MANIFEST_JSON:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
