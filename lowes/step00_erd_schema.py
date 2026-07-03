import os
import re
from datetime import datetime

from .step00_config import lowes_product_type, lowes_run_date


DEFAULT_CRAWL_DATETIME = datetime.now().isoformat(timespec="seconds")


CORE_OUTPUT_COLUMNS = [
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
    "pick_up_availability",
    "delivery_availability",
    "sku_status",
    "detailed_review_content",
    "recommendation_intent",
    "main_rank",
    "bsr_rank",
    "retailer_sku_name_similar",
]

COMMON_ERD_COLUMNS = [
    "calendar_week",
    "crawl_strdatetime",
    "batch_id",
    "discount_type",
    "sku_popularity",
    "sku",
    "number_of_units_purchased_past_week",
    "fastest_delivery",
    "available_quantity_for_purchase_pickup",
    "available_quantity_for_purchase_delivery",
    "available_quantity_for_purchase_fastdelivery",
    "summarized_review_content",
]

REF_ERD_COLUMNS = [
    "ref_capacity",
    "ref_refrigerator_type",
]

LDY_ERD_COLUMNS = [
    "ldy_capacity",
    "ldy_loading_type",
]

LEGACY_COMPAT_COLUMNS = []


def unique(values):
    result = []
    seen = set()
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def erd_field_order(product_type=None):
    product = (product_type or lowes_product_type()).strip().upper()
    category_columns = LDY_ERD_COLUMNS if product == "LDY" else REF_ERD_COLUMNS
    return unique(CORE_OUTPUT_COLUMNS + category_columns + COMMON_ERD_COLUMNS + LEGACY_COMPAT_COLUMNS)


LOWES_ERD_OUTPUT_COLUMNS = unique(
    CORE_OUTPUT_COLUMNS + COMMON_ERD_COLUMNS + REF_ERD_COLUMNS + LDY_ERD_COLUMNS + LEGACY_COMPAT_COLUMNS
)


def compact_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def retailer_sku_name_text(row):
    brand = first_value(row, "brand", "detail_product_brand", "product.brand")
    name = first_value(
        row,
        "retailer_sku_name",
        "description",
        "detail_title",
        "missing_price_detail_title",
        "product.description",
    )
    brand = compact_text(brand)
    name = compact_text(name)
    if not brand or not name:
        return name or brand

    brand_key = normalize_key(brand)
    name_key = normalize_key(name)
    if name_key == brand_key or name_key.startswith(f"{brand_key}_"):
        return name
    return f"{brand} {name}"


PURCHASED_UNITS_RE = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*[kKmM]?\+?\s+(?:bought|purchased|sold)\b(?:\s+last\s+week)?",
    re.I,
)


def purchased_units_phrase(value):
    text = compact_text(value)
    match = PURCHASED_UNITS_RE.search(text)
    return match.group(0) if match else ""


def first_value(row, *names):
    for name in names:
        value = row.get(name)
        if value not in ("", None):
            return value
    return ""


def as_bool(value):
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def normalize_key(value):
    return re.sub(r"[^0-9a-z]+", "_", str(value or "").strip().lower()).strip("_")


def value_by_keywords(row, keywords):
    wanted = [normalize_key(item) for item in keywords]
    for key, value in row.items():
        if value in ("", None):
            continue
        normalized = normalize_key(key)
        if all(word in normalized for word in wanted):
            return value
    return ""


def inventory_text(row, method):
    method = method.lower()
    for name in ("available_methods", "inventory_methods"):
        text = compact_text(row.get(name))
        if method in text.lower():
            return text
    return ""


def sku_status(row):
    explicit = first_value(row, "sku_status")
    if explicit:
        return explicit
    is_oos = as_bool(first_value(row, "detail_is_oos", "missing_price_detail_is_oos"))
    is_not_available = as_bool(
        first_value(row, "detail_is_not_available", "missing_price_detail_is_not_available")
    )
    is_buyable = as_bool(first_value(row, "is_buyable", "detail_is_buyable", "missing_price_detail_is_buyable"))
    unavailable = as_bool(
        first_value(row, "detail_unavailable_phrase", "missing_price_detail_unavailable_phrase")
    )
    if is_oos or is_not_available or unavailable:
        return "Out of stock"
    if is_buyable is True:
        return "Buyable"
    if is_buyable is False:
        return "Not buyable"
    return ""


def units_purchased_text(row):
    explicit = first_value(row, "number_of_units_purchased_past_week")
    if explicit:
        return purchased_units_phrase(explicit)
    haystack = " ".join(compact_text(value) for value in row.values() if value not in ("", None))
    match = PURCHASED_UNITS_RE.search(haystack)
    return match.group(0) if match else ""


def output_page_type(row):
    explicit = normalize_page_type(first_value(row, "page_type"))
    if explicit:
        return explicit
    selection_source = normalize_page_type(first_value(row, "selection_source"))
    if selection_source == "bsr":
        return "bsr"
    if selection_source == "main":
        return "main"
    if first_value(row, "main_rank"):
        return "main"
    if first_value(row, "bsr_rank"):
        return "bsr"
    return "main"


def normalize_page_type(value):
    text = compact_text(value).lower()
    if text in {"", "none", "null", "nan"}:
        return ""
    if text in {"main", "search"}:
        return "main"
    if text in {"bsr", "best_selling", "best-selling", "best selling"}:
        return "bsr"
    return ""


def apply_erd_columns(row, product_type=None):
    product = (product_type or os.getenv("LOWES_PRODUCT_TYPE") or lowes_product_type()).strip().upper()
    out = dict(row)
    out.setdefault("country", "SEA")
    out.setdefault("product", product)
    out.setdefault("account_name", "Lowes")
    out.setdefault("crawl_strdatetime", first_value(out, "crawl_strdatetime", "crawl_datetime") or DEFAULT_CRAWL_DATETIME)
    out["page_type"] = output_page_type(out)
    out.setdefault("item", first_value(out, "item", "omni_item_id", "item_number"))

    mappings = {
        "retailer_sku_name": retailer_sku_name_text(out),
        "final_sku_price": first_value(
            out,
            "final_sku_price",
            "final_selling_price",
            "selling_price",
            "resolved_selling_price",
            "missing_price_resolved_selling_price",
        ),
        "original_sku_price": first_value(
            out,
            "original_sku_price",
            "was_price",
            "detail_was_price",
            "missing_price_detail_was_price",
            "detail_retail_price",
            "missing_price_detail_retail_price",
        ),
        "savings": first_value(
            out,
            "savings",
            "total_saving",
            "manual_override_total_saving",
            "detail_savings_total",
            "missing_price_detail_savings_total",
        ),
        "star_rating": first_value(out, "star_rating", "rating", "detail_rating", "missing_price_detail_rating"),
        "count_of_reviews": first_value(
            out,
            "count_of_reviews",
            "review_count",
            "detail_review_count",
            "missing_price_detail_review_count",
        ),
        "count_of_star_ratings": first_value(
            out,
            "count_of_star_ratings",
            "review_count",
            "detail_review_count",
            "missing_price_detail_review_count",
        ),
        "discount_type": first_value(out, "discount_type", "display_type", "display_price_type", "promotion_labels"),
        "sku_popularity": first_value(out, "sku_popularity"),
        "sku_status": sku_status(out),
        "sku": first_value(out, "sku", "item_number", "detail_item_number", "omni_item_id"),
        "number_of_units_purchased_past_week": units_purchased_text(out),
        "pick_up_availability": first_value(out, "pick_up_availability") or inventory_text(out, "pickup"),
        "delivery_availability": first_value(out, "delivery_availability") or inventory_text(out, "delivery"),
        "fastest_delivery": first_value(out, "fastest_delivery") or inventory_text(out, "fast"),
        "available_quantity_for_purchase_pickup": first_value(out, "available_quantity_for_purchase_pickup"),
        "available_quantity_for_purchase_delivery": first_value(out, "available_quantity_for_purchase_delivery"),
        "available_quantity_for_purchase_fastdelivery": first_value(out, "available_quantity_for_purchase_fastdelivery"),
        "recommendation_intent": first_value(out, "recommendation_intent"),
        "summarized_review_content": first_value(out, "summarized_review_content"),
        "detailed_review_content": first_value(out, "detailed_review_content"),
        "retailer_sku_name_similar": first_value(out, "retailer_sku_name_similar"),
    }

    if product == "LDY":
        mappings["ldy_loading_type"] = first_value(out, "ldy_loading_type") or value_by_keywords(
            out, ["loading", "type"]
        )
        mappings["ldy_capacity"] = first_value(out, "ldy_capacity") or value_by_keywords(out, ["capacity"])
    else:
        mappings["ref_refrigerator_type"] = first_value(out, "ref_refrigerator_type") or value_by_keywords(
            out, ["refrigerator", "type"]
        )
        mappings["ref_capacity"] = first_value(out, "ref_capacity") or value_by_keywords(out, ["capacity"])

    for key, value in mappings.items():
        if out.get(key) in ("", None):
            out[key] = value

    return out
