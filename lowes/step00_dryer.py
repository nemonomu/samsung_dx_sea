"""Pure dryer rules. Importing this module never reads settings or opens services."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
import re
from urllib.parse import urlencode, urljoin, urlsplit


BASE_URL = "https://www.lowes.com"
BSR_URL = BASE_URL + "/best-sellers/appliances/washers-dryers/dryers/4294857980"
TEST_TABLE = "public.ldy_dryer_retail_test"
PRODUCTION_TABLE = "public.ldy_dryer_retail"
COLUMNS = (
    "id", "country", "account_name", "item", "sku", "retailer_sku_name",
    "product_url", "count_of_reviews", "star_rating", "count_of_star_ratings",
    "final_sku_price", "original_sku_price", "savings", "main_rank", "bsr_rank",
    "calendar_week", "crawl_datetime", "loading_type", "capacity", "batch_id",
)


class SettingsError(ValueError):
    def __init__(self, public_reason):
        self.public_reason = public_reason
        super().__init__(public_reason)


@dataclass(frozen=True)
class Settings:
    main_limit: int = 300
    bsr_limit: int = 100
    page_size: int = 24
    max_main_pages: int = 30
    attempts: int = 3
    timeout: int = 45
    retry_wait: float = 3.0
    page_wait: float = 1.0
    seed_wait: float = 5.0
    headless: bool = False
    zip_code: str = "10010"
    state: str = "NY"
    listing_store: str = "1674"
    detail_store: str = "289"
    nearby_stores: str = "1674"
    nearby_store: str = "1674"
    store_name: str = "Brooklyn Lowe's"
    store_city: str = "Brooklyn"
    store_region: str = "4"
    alt_store: str = "1854"
    alt_store_name: str = "Zephyrhills Lowe's"
    alt_store_city: str = "Zephyrhills"
    alt_store_region: str = "27"
    alt_store_enabled: bool = True

    def __post_init__(self):
        if self.zip_code != "10010":
            raise SettingsError("dryer ZIP must be 10010")
        if self.main_limit < 1 or not 1 <= self.bsr_limit <= 100:
            raise SettingsError("main_limit must be positive; bsr_limit must be 1..100")
        if self.page_size != 24 or self.max_main_pages < 1 or self.attempts < 1:
            raise SettingsError("invalid pagination/retry settings")
        waits = (self.retry_wait, self.page_wait, self.seed_wait)
        if self.timeout < 1 or not all(math.isfinite(v) and v >= 0 for v in waits):
            raise SettingsError("invalid timeout/wait settings")
        for value in (self.listing_store, self.detail_store, self.nearby_store):
            if not str(value).isdigit():
                raise SettingsError("store identifiers must be numeric")
        if not re.fullmatch(r"\d+(?:,\d+)*", self.nearby_stores):
            raise SettingsError("nearby stores must be numeric")
        if self.alt_store and not str(self.alt_store).isdigit():
            raise SettingsError("alternate store identifier must be numeric")

    @classmethod
    def from_env(cls, env):
        def integer(name, default):
            try:
                return int(env.get(name, default))
            except (ValueError, TypeError):
                raise SettingsError(f"invalid setting: {name}") from None

        def number(name, default):
            try:
                return float(env.get(name, default))
            except (ValueError, TypeError):
                raise SettingsError(f"invalid setting: {name}") from None

        return cls(
            main_limit=integer("LOWES_DRYER_MAIN_LIMIT", 300),
            bsr_limit=integer("LOWES_DRYER_BSR_LIMIT", 100),
            max_main_pages=integer("LOWES_DRYER_MAX_MAIN_PAGES", 30),
            attempts=integer("LOWES_DRYER_MAX_ATTEMPTS", 3),
            timeout=integer("LOWES_DRYER_API_TIMEOUT", 45),
            retry_wait=number("LOWES_DRYER_RETRY_WAIT", 3),
            page_wait=number("LOWES_DRYER_PAGE_WAIT", 1),
            seed_wait=number("LOWES_DRYER_SEED_WAIT", 5),
            headless=env.get("LOWES_DRYER_HEADLESS", "0").lower() in {"1", "true", "yes"},
            # ZIP is an explicit dryer rule, independent of inherited LDY env values.
            listing_store=env.get("LOWES_API_STORE_ID", "1674"),
            detail_store=env.get("LOWES_API_STORE_ID", "289").lstrip("0") or "289",
            nearby_stores=env.get("LOWES_API_NEARBY_STORES", "1674"),
            nearby_store=env.get("LOWES_API_NEARBY_STORE", "1674"),
            state=env.get("LOWES_API_STORE_STATE", "NY"),
            store_name=env.get("LOWES_API_STORE_NAME", "Brooklyn Lowe's"),
            store_city=env.get("LOWES_API_STORE_CITY", "Brooklyn"),
            store_region=env.get("LOWES_API_STORE_REGION", "4"),
            alt_store=env.get("LOWES_API_ALT_STORE_ID", "1854"),
            alt_store_name=env.get("LOWES_API_ALT_STORE_NAME", "Zephyrhills Lowe's"),
            alt_store_city=env.get("LOWES_API_ALT_STORE_CITY", "Zephyrhills"),
            alt_store_region=env.get("LOWES_API_ALT_STORE_REGION", "27"),
            alt_store_enabled=env.get("LOWES_API_ALT_STORE_ENABLED", "1").lower()
            not in {"0", "false", "no"},
        )


def public_text(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return " ".join(str(value if value is not None else "").split())


def numeric_source(value, currency=False):
    """Keep valid public numeric scalars; never stringify arbitrary payload objects."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if currency:
        text = text.replace("$", "")
        if text.lower().startswith("save "):
            text = text[5:]
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return value if number.is_finite() and number >= 0 else None


def rating_source(value):
    value = numeric_source(value)
    if value is None:
        return None
    return value if Decimal(str(value).replace(",", "")) <= 5 else None


def review_count_source(value):
    value = numeric_source(value)
    if value is None:
        return None
    number = Decimal(str(value).replace(",", ""))
    return value if number == number.to_integral_value() else None


def first_value(*values):
    return next((v for v in values if v not in (None, "")), None)


def positive_id(value):
    value = public_text(value)
    return value if re.fullmatch(r"[1-9]\d*", value) else ""


def listing_url(offset, adjusted, settings):
    query = {"searchTerm": "dryer", "sortMethod": "sortBy_featured", "offset": offset,
             "nearByStores": settings.nearby_stores, "ac": "false",
             "algoRulesAppliedInPageLoad": "false"}
    if adjusted is not None:
        query["adjustedNextOffset"] = adjusted
    return "/search/products?" + urlencode(query, safe=",")


def bsr_url(offset, adjusted):
    return "/best-sellers/node/bspl?" + urlencode({
        "nValue": "4294857980", "sortMethod": "sortBy_bestSellers", "offset": offset,
        "adjustedNextOffset": adjusted if adjusted is not None else offset, "isBot": "yes",
    })


def detail_url(item, settings, store=None):
    if not positive_id(item):
        raise ValueError("missing product ID")
    store = str(store or settings.detail_store).zfill(4)
    return f"/wpd/{item}/productdetail/{store}/Guest/{settings.zip_code}?" + urlencode({
        "nearByStore": settings.nearby_store, "zipState": settings.state,
    })


def reviews_url(item):
    if not positive_id(item):
        raise ValueError("missing product ID")
    return f"/rnr/r/get-by-product/{item}?sortBy=newestFirst"


def listing_page(payload):
    """Only accept the validated list containers, never recommendation carousels."""
    if not isinstance(payload, dict):
        raise ValueError("invalid_listing_schema")
    candidates = [payload]
    normalized = payload.get("productListCommonNormalizedPageSpecificProducts")
    if isinstance(normalized, dict):
        candidates.append(normalized)
    empty_result = None
    for root in candidates:
        products = root.get("itemList")
        product_list = root.get("productList")
        if not isinstance(products, list) and isinstance(product_list, dict):
            products = product_list.get("products")
        if isinstance(products, list):
            if any(not isinstance(card, dict) or not isinstance(card.get("product"), dict)
                   for card in products):
                raise ValueError("invalid_product_cards")
            metadata = dict(root)
            if isinstance(product_list, dict):
                metadata.update({k: v for k, v in product_list.items() if k != "products"})
            result = (products, {
                "total": numeric_source(first_value(metadata.get("productCount"), metadata.get("itemCount"))),
                "adjusted_next": first_value(metadata.get("adjustedNextOffset"),
                                              payload.get("adjustedNextOffset")),
                "pagination": metadata.get("pagination") or payload.get("pagination") or {},
            })
            if products:
                return result
            empty_result = empty_result or result
    if empty_result is not None:
        return empty_result
    raise ValueError("missing_listing_container")


def next_offsets(offset, metadata, page_size=24, previous_adjusted=None):
    next_offset = offset + page_size
    raw = metadata.get("adjusted_next")
    if raw is None:
        return next_offset, next_offset
    try:
        adjusted = int(raw)
    except (ValueError, TypeError):
        raise ValueError("invalid_next_offset") from None
    # Displayed and adjusted (organic) offsets are independent cursors when ads occur.
    baseline = offset if previous_adjusted is None else previous_adjusted
    if adjusted <= baseline:
        raise ValueError("nonadvancing_next_offset")
    return next_offset, adjusted


def spec_entries(product):
    result = []
    for name in ("specs", "productSpecsData", "productSpecs"):
        entries = product.get(name) or []
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("key"):
                    result.append({"key": public_text(entry["key"]),
                                   "value": public_text(entry.get("value"))})
    return result


def normalized_key(value):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", public_text(value).lower()).split())


def normalize_capacity(value, key):
    value = public_text(value)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(.*)", value)
    if not match or Decimal(match[1]) <= 0:
        return None
    number = format(Decimal(match[1]).normalize(), "f")
    unit = normalized_key(match[2])
    key_unit = normalized_key(key)
    feet_units = {"cu ft", "cu feet", "cubic feet", "cufeet", "cuft"}
    if unit and unit not in feet_units:
        return None
    if unit in feet_units or any(u in key_unit for u in feet_units):
        return number + " Cu.Feet"
    # Do not invent units absent from both the value and specification label.
    return number


def dryer_specs(product):
    entries = spec_entries(product)
    output = {}
    audit = []
    for column, prefix in (("loading_type", "dryer load type"), ("capacity", "dryer capacity")):
        matches = [s for s in entries if normalized_key(s["key"]).startswith(prefix)]
        values = []
        for spec in matches:
            if column == "capacity":
                normalized = normalize_capacity(spec["value"], spec["key"])
            else:
                key = normalized_key(spec["value"])
                normalized = {"front load": "Front load", "top load": "Top load"}.get(key)
                if normalized is None and key and not re.search(r"\b(?:gas|electric)\b", key):
                    normalized = spec["value"]
            if normalized:
                values.append(normalized)
            audit.append({"column": column, "raw_key": spec["key"],
                          "raw_value": spec["value"], "normalized": normalized})
        unique = set(values)
        output[column] = next(iter(unique)) if len(unique) == 1 else None
        reason = "present" if len(unique) == 1 else (
            "conflicting_values" if len(unique) > 1 else "invalid_value" if matches else "absent")
        shared = [s for s in entries if normalized_key(s["key"]).startswith(
            ("washer dryer capacity", "washer and dryer capacity"))]
        if column == "capacity" and reason == "absent" and shared:
            reason = "shared_capacity_only"
            audit.extend({"column": column, "raw_key": s["key"], "raw_value": s["value"],
                          "normalized": None} for s in shared)
        output[column + "_reason"] = reason
    return output, audit


def category_text(value):
    if isinstance(value, dict):
        return " ".join(category_text(v) for k, v in value.items()
                        if isinstance(v, (dict, list)) or k in {
                            "name", "label", "displayName", "categoryName", "categoryId", "path", "id"})
    if isinstance(value, list):
        return " ".join(category_text(v) for v in value)
    return public_text(value)


def classification_evidence(product):
    """Allowlist only the actual product's identity/taxonomy/package fields."""
    keys = {"appliance type", "product type", "package quantity", "number of pieces",
            "bundle type", "package type", "dryer load type"}
    return {"description": public_text(product.get("description")),
            "name": public_text(product.get("name")),
            "pdURL": public_text(product.get("pdURL")),
            "categories": category_text(product.get("categories", {})),
            "package_fields": {key: product[key] for key in ("isBundle", "isPackage", "isCollection")
                               if isinstance(product.get(key), bool)},
            "type": public_text(product.get("type")),
            "package_components": {key: [{field: public_text(component.get(field))
                                           for field in ("omniItemId", "modelId", "description")}
                                          for component in product[key] if isinstance(component, dict)]
                                   for key in ("bundleProducts", "packageProducts", "bundleComponents", "packageComponents")
                                   if isinstance(product.get(key), list)},
            "specs": [entry for entry in spec_entries(product)
                      if normalized_key(entry["key"]) in keys
                      or normalized_key(entry["key"]).startswith(("dryer capacity", "washer dryer capacity",
                                                                  "washer and dryer capacity"))]}


def product_classification(product):
    """Classify the sold SKU, never recommendations or a parent laundry breadcrumb."""
    evidence = classification_evidence(product)
    raw_name = public_text(first_value(product.get("description"), product.get("name")))
    name = normalized_key(raw_name)
    # Optional/compatible/matching products do not belong to this sold SKU.
    note = re.search(r"\b(?:compatible with|works with|for use with|recommended|optional)\b", name)
    if note:
        name = name[:note.start()].strip()
    separate = re.search(r"\b(?:with|and|plus)\b[^,;]*\b(?:sold separately|not included)\b", name)
    if separate:
        name = name[:separate.start()].strip()
    evidence["classification_title"] = name
    categories = normalized_key(evidence["categories"])
    appliance = " ".join(normalized_key(s["value"]) for s in evidence["specs"]
                         if normalized_key(s["key"]) == "appliance type")
    def result(kind, reason):
        return {"kind": kind, "eligible": kind == "dryer", "reason": reason, "evidence": evidence}
    if "/collections/" in public_text(product.get("pdURL")):
        return result("bundle", "collection_card")
    if any(evidence["package_fields"].values()) or normalized_key(evidence["type"]) in {"bundle", "package", "collection", "set"}:
        return result("bundle", "explicit_package_flag")
    components = [entry for entries in evidence["package_components"].values() for entry in entries]
    identities = {entry["omniItemId"] or entry["modelId"] or entry["description"] for entry in components}
    identities.discard("")
    if len(identities) > 1:
        return result("bundle", "multiple_package_products")
    if re.search(r"\s\+\s", raw_name):
        companion_title = normalized_key(raw_name.split("+", 1)[1])
        if re.search(r"\b(?:dryer|washer|pedestal|power cord|refrigerator|freezer|microwave|dishwasher|oven|range|air purifier|air conditioner)\b", companion_title):
            return result("bundle", "multiple_products_in_title")
    for spec in evidence["specs"]:
        key, value = normalized_key(spec["key"]), normalized_key(spec["value"])
        if key in {"package quantity", "number of pieces"} and re.fullmatch(r"\d+", value) and int(value) > 1:
            return result("bundle", "multiple_piece_package")
        if key in {"bundle type", "package type", "product type"} and value in {"bundle", "package", "set", "laundry pair"}:
            return result("bundle", "explicit_package_spec")
    if re.search(r"\b(?:bundle|bundled|package|set)\b|\b[2-9]\s*(?:piece|pc)\b|\blaundry pair\b", name):
        return result("bundle", "package_in_product_title")
    companion = r"(?:pedestal|power cord|cord kit|installation kit|vent kit|stacking kit|washer|washing machine|refrigerator|freezer|microwave|dishwasher|oven|range|air purifier|air conditioner)"
    if re.search(r"\b(?:with|and|plus|including|includes)\s+(?:(?:an?|the|included|matching)\s+)?(?:\d+\s+)?" + companion + r"\b", name):
        return result("bundle", "dryer_with_other_product")
    title = name + " " + appliance
    if (re.search(r"\bwashers?\b.*\bdryers?\b|\bdryers?\b.*\bwashers?\b", title)
            or re.search(r"\blaundry (?:center|centre|tower)\b|\bwash ?tower\b", title)):
        return result("combo", "washer_dryer_combination")
    if re.search(r"all in one.*washer.*dryer|washer dryer combo|laundry center|laundry tower", categories):
        return result("combo", "integrated_product_category")
    if re.search(r"\b(?:hair dryer|stacking kit|dryer cord|dryer duct|dryer vent kit|pedestal)\b", name):
        return result("other", "accessory_or_non_laundry_dryer")
    if re.search(r"\bwashers?\b|\bwashing machines?\b", title) and not re.search(r"\bdryers?\b", title):
        return result("washer", "standalone_washer")
    specs, _ = dryer_specs(product)
    if re.search(r"\bdryers?\b", title) or specs["loading_type"] or specs["capacity"]:
        return result("dryer", "standalone_dryer_evidence")
    if re.search(r"\bwashing machines?\b", categories):
        return result("washer", "washing_machine_category")
    washer = bool(re.search(r"\bwashers?\b", categories))
    dryer = bool(re.search(r"\bdryers?\b", categories))
    if washer != dryer:
        return result("washer" if washer else "dryer", "exclusive_product_category")
    return result("unknown", "product_kind_unconfirmed")


def product_kind(product):
    return product_classification(product)["kind"]


def parse_card(card, page, position, occurrence_rank):
    product = card["product"]
    location = card.get("location") if isinstance(card.get("location"), dict) else {}
    price = location.get("price") if isinstance(location.get("price"), dict) else {}
    item = positive_id(product.get("omniItemId"))
    path = public_text(product.get("pdURL"))
    url = urljoin(BASE_URL, path) if path else ""
    if url and urlsplit(url).hostname not in {"lowes.com", "www.lowes.com"}:
        raise ValueError("invalid_product_url")
    parsed = {
        "omni_item_id": item, "item": item, "model_id": public_text(product.get("modelId")),
        "brand": public_text(product.get("brand")), "description": public_text(product.get("description")),
        "product_url": url, "page": page, "rank_in_page": position,
        "original_rank": occurrence_rank,
        "selling_price": numeric_source(price.get("sellingPrice"), currency=True),
        "was_price": numeric_source(price.get("wasPrice"), currency=True),
        "total_saving": numeric_source(price.get("totalSaving"), currency=True),
        "rating": rating_source(product.get("rating")), "review_count": review_count_source(product.get("reviewCount")),
        "sponsored": str(product.get("sponsored", card.get("sponsored", False))).lower() in {"true", "1"}
        if isinstance(product.get("sponsored", card.get("sponsored", False)), (str, bool, int)) else False,
        "kind": product_kind(product), "_classification": dict(product_classification(product), source="listing"), "_listing_product": {
            "description": public_text(product.get("description")), "name": public_text(product.get("name")),
            "pdURL": path, "type": public_text(product.get("type")),
            **{key: product[key] for key in ("bundleProducts", "packageProducts", "bundleComponents", "packageComponents") if isinstance(product.get(key), list)},
            **{key: product[key] for key in ("isBundle", "isPackage", "isCollection") if isinstance(product.get(key), bool)},
            "categories": product.get("categories") or {}, "specs": spec_entries(product),
        },
    }
    public_numbers = (
        ("selling_price", price.get("sellingPrice"), "final_sku_price"),
        ("was_price", price.get("wasPrice"), "original_sku_price"),
        ("total_saving", price.get("totalSaving"), "savings"),
        ("rating", product.get("rating"), "star_rating"),
        ("review_count", product.get("reviewCount"), "count_of_reviews"),
    )
    parsed["_listing_invalid_columns"] = [
        column for source_key, raw, column in public_numbers
        if raw not in (None, "") and parsed[source_key] is None
    ]
    return parsed


def select_main(rows, limit):
    result, seen = [], set()
    for row in sorted(rows, key=lambda r: (r["page"], r["rank_in_page"])):
        item = row.get("omni_item_id")
        if not item or item in seen:
            continue
        seen.add(item)
        if row.get("kind") != "dryer" or "/collections/" in row.get("product_url", ""):
            continue
        selected = dict(row, main_rank=len(result) + 1, page_type="main", selection_source="main")
        result.append(selected)
        if len(result) >= limit:
            break
    return result


def select_bsr(rows, limit):
    result, seen = [], set()
    # The cutoff is applied to displayed occurrences BEFORE deduplication.
    for row in sorted(rows, key=lambda r: r["original_rank"]):
        if row["original_rank"] > limit:
            continue
        item = row.get("omni_item_id")
        if not item or item in seen:
            continue
        seen.add(item)
        if row.get("kind") != "dryer" or "/collections/" in row.get("product_url", ""):
            continue
        result.append(dict(row, bsr_rank=len(result) + 1,
                           original_bsr_rank=row["original_rank"], page_type="bsr"))
    return result


def detail_product(payload, item):
    if not isinstance(payload, dict):
        raise ValueError("invalid_detail_schema")
    node = (payload.get("productDetails") or {}).get(str(item))
    if not isinstance(node, dict) or not isinstance(node.get("product"), dict) or not node["product"]:
        raise ValueError("missing_detail_product")
    return node["product"], node


def validate_reviews(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid_review_schema")
    stats = payload.get("reviewStatistics")
    if isinstance(stats, dict) and ("averageOverallRating" in stats or "totalReviewCount" in stats):
        for key in ("averageOverallRating", "totalReviewCount"):
            value = stats.get(key)
            if value in (None, ""):
                continue
            try:
                number = Decimal(str(value).replace(",", ""))
            except InvalidOperation:
                raise ValueError("invalid_review_statistics") from None
            if not number.is_finite() or number < 0:
                raise ValueError("invalid_review_statistics")
            if key == "averageOverallRating" and number > 5:
                raise ValueError("invalid_review_statistics")
            if key == "totalReviewCount" and number != number.to_integral_value():
                raise ValueError("invalid_review_statistics")
        return stats
    if payload.get("totalResults") in (0, "0"):
        return {"averageOverallRating": 0, "totalReviewCount": 0}
    raise ValueError("missing_review_statistics")


def enrich_row(source, product_payload, review_payload):
    row = dict(source)
    item = source["omni_item_id"]
    spec_source = "listing"
    product = source.get("_listing_product", {})
    if product_payload is not None:
        product, node = detail_product(product_payload, item)
        spec_source = "productdetail"
        model = public_text(product.get("modelId"))
        row["_detail_model_id"] = model
        if model and source.get("model_id") and model != source["model_id"]:
            row["_sku_conflict"] = True
        row["model_id"] = source.get("model_id") or model
        row["detail_product_brand"] = public_text(product.get("brand"))
        row["detail_product_description"] = public_text(product.get("description"))
        if not row.get("description"):
            row["description"] = row["detail_product_description"]
        mfe = node.get("mfePrice") if isinstance(node.get("mfePrice"), dict) else {}
        price = mfe.get("price") if isinstance(mfe.get("price"), dict) else {}
        additional = price.get("additionalData") if isinstance(price.get("additionalData"), dict) else {}
        savings = price.get("savings") if isinstance(price.get("savings"), dict) else {}
        if mfe.get("price") and not isinstance(mfe["price"], dict):
            row["_detail_price_invalid"] = True
        row.update(_detail_selling_price=numeric_source(additional.get("sellingPrice"), currency=True),
                   _detail_was_price=numeric_source(additional.get("wasPrice"), currency=True),
                   _detail_total_saving=numeric_source(savings.get("totalSaving"), currency=True))
        for key, raw in (("_detail_selling_price", additional.get("sellingPrice")),
                         ("_detail_was_price", additional.get("wasPrice")),
                         ("_detail_total_saving", savings.get("totalSaving"))):
            if raw not in (None, "") and row[key] is None:
                row["_detail_price_invalid"] = True
    specs, audit = dryer_specs(product)
    if product_payload is not None:
        listing_specs, listing_audit = dryer_specs(source.get("_listing_product", {}))
        for column in ("loading_type", "capacity"):
            if specs[column] is None and specs[column + "_reason"] in {"absent", "shared_capacity_only"}:
                if listing_specs[column] is not None:
                    specs[column] = listing_specs[column]
                    specs[column + "_reason"] = "listing_fallback"
                    audit.extend(dict(a, source="listing") for a in listing_audit if a["column"] == column)
    row.update(specs)
    row["sku"] = row.get("model_id") or None
    if review_payload is not None:
        stats = validate_reviews(review_payload)
        row["_pdp_average_rating"] = stats.get("averageOverallRating")
        row["_pdp_total_reviews"] = stats.get("totalReviewCount")
    # Preserve real numeric zero through legacy expressions that use `or`.
    for column in ("selling_price", "was_price", "total_saving", "rating", "review_count"):
        if row.get(column) == 0:
            row[column] = "0"
    for entry in audit:
        entry.setdefault("source", spec_source)
    return row, audit


def finalize_dryer(row, batch_id, crawl_dt, legacy_finalize):
    legacy = legacy_finalize(row, batch_id, crawl_dt)
    output = {column: legacy.get(column) for column in COLUMNS}
    output.update(id=None, country="SEA", account_name="Lowes", item=row["omni_item_id"],
                  sku=row.get("sku"), loading_type=row.get("loading_type"), capacity=row.get("capacity"),
                  crawl_datetime=legacy.get("crawl_strdatetime") or crawl_dt)
    return {key: None if value == "" else value for key, value in output.items()}


def validate_rows(rows, main_limit=None, bsr_limit=None, allow_empty=False):
    seen = set()
    ranks = {"main_rank": [], "bsr_rank": []}
    batches = set()
    for row in rows:
        if set(row) != set(COLUMNS):
            raise ValueError("expected exactly 20 dryer columns")
        item = positive_id(row.get("item"))
        if not item or item in seen:
            raise ValueError("missing or duplicate product ID")
        seen.add(item)
        if row.get("account_name") != "Lowes" or row.get("country") != "SEA":
            raise ValueError("invalid retailer/country")
        if not row.get("batch_id") or not row.get("crawl_datetime"):
            raise ValueError("missing batch/date")
        batches.add(row["batch_id"])
        if str(row.get("count_of_reviews") or 0) != str(row.get("count_of_star_ratings") or 0):
            raise ValueError("review/rating count mismatch")
        if ((row.get("count_of_reviews") in (None, "")) !=
                (row.get("count_of_star_ratings") in (None, ""))):
            raise ValueError("review/rating NULL mismatch")
        for column in ranks:
            if row.get(column) not in (None, ""):
                try:
                    rank = int(str(row[column]))
                except (TypeError, ValueError):
                    raise ValueError("invalid rank") from None
                if rank < 1:
                    raise ValueError("invalid rank")
                ranks[column].append(rank)
        if row.get("main_rank") in (None, "") and row.get("bsr_rank") in (None, ""):
            raise ValueError("product has neither rank")
    if (rows and len(batches) != 1) or (not rows and not allow_empty):
        raise ValueError("one nonempty batch is required")
    for column, values in ranks.items():
        if sorted(values) != list(range(1, len(values) + 1)):
            raise ValueError("duplicate or noncontiguous rank")
    if main_limit is not None and len(ranks["main_rank"]) > main_limit:
        raise ValueError("main limit exceeded")
    if bsr_limit is not None and len(ranks["bsr_rank"]) > bsr_limit:
        raise ValueError("BSR limit exceeded")
    return {"rows": len(rows), "main": len(ranks["main_rank"]), "bsr": len(ranks["bsr_rank"]),
            "shared": sum(r.get("main_rank") not in (None, "") and r.get("bsr_rank") not in (None, "") for r in rows),
            "loading_type_null": sum(r.get("loading_type") in (None, "") for r in rows),
            "capacity_null": sum(r.get("capacity") in (None, "") for r in rows),
            "sku_null": sum(r.get("sku") in (None, "") for r in rows)}


def table_parts(table, allow_production=False):
    if table == TEST_TABLE or (table == PRODUCTION_TABLE and allow_production):
        return table.split(".")
    raise ValueError("dryer table is not authorized; production requires explicit opt-in")


def coerce_db_value(column, value, data_type):
    if value in (None, ""):
        return None
    if column == "sku" and data_type not in {"text", "character varying", "character"}:
        raise ValueError("SKU column must preserve text model identifiers")
    if column == "item" and data_type in {"real", "double precision"}:
        raise ValueError("product IDs must not use floating point DB types")
    if data_type in {"smallint", "integer", "bigint", "numeric", "decimal", "real", "double precision"}:
        text = str(value).strip().replace("$", "").replace(",", "")
        if column == "capacity":
            text = re.sub(r"\s+Cu\.Feet$", "", text)
        if column == "calendar_week":
            text = text.removeprefix("w")
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"DB numeric conversion failed: {column}") from None
        if not number.is_finite():
            raise ValueError(f"DB numeric conversion failed: {column}")
        if data_type in {"smallint", "integer", "bigint"}:
            if number != number.to_integral_value():
                raise ValueError(f"DB integer conversion failed: {column}")
            return int(number)
        return number
    if data_type.startswith("timestamp"):
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            raise ValueError(f"DB timestamp conversion failed: {column}") from None
    if data_type in {"text", "character varying", "character"}:
        return str(value)
    raise ValueError(f"unsupported DB type for column: {column}")



def price_combination_warnings(row):
    """Validate presence only; do not calculate or change any collected prices."""
    present = lambda value: value not in (None, "")
    selling, original, saving = (present(row.get(c)) for c in
                                  ("final_sku_price", "original_sku_price", "savings"))
    warnings = []
    if selling and original and not saving:
        warnings.append({"item": row["item"], "sku": row.get("sku"),
                         "reason": "missing_savings_with_both_prices", "column": "savings"})
    if saving and not selling:
        warnings.append({"item": row["item"], "sku": row.get("sku"),
                         "reason": "missing_selling_with_savings", "column": "final_sku_price"})
    return warnings


def region_evidence(payload, item, settings, store, api):
    """Public region metadata only. Store/local ZIP is not a delivery ZIP."""
    root = payload if isinstance(payload, dict) else {}
    details = root.get("productDetails") if isinstance(root.get("productDetails"), dict) else {}
    node = details.get(str(item)) if isinstance(details.get(str(item)), dict) else root
    location = node.get("location") if isinstance(node.get("location"), dict) else {}
    store_details = root.get("storeDetails") if isinstance(root.get("storeDetails"), dict) else {}
    def scalars(obj, names):
        return {key: public_text(obj[key]) for key in names
                if isinstance(obj.get(key), (str, int)) and not isinstance(obj.get(key), bool)}
    delivery = []
    for path, obj in (("root", root), ("product", node), ("location", location)):
        for key, value in scalars(obj, ("deliveryZipCode", "deliveryZip", "shippingZipCode")).items():
            delivery.append({"path": path + "." + key, "value": value})
    zips = {entry["value"] for entry in delivery}
    status = "unverified" if not zips else "delivery_zip_match" if zips == {settings.zip_code} else "delivery_zip_mismatch"
    return {"request": {"delivery_zip": settings.zip_code, "state": settings.state,
                         "store_id": str(store).zfill(4), "nearby_store": settings.nearby_store, "api": api},
            "response": {"delivery": delivery,
                         "location": scalars(location, ("zipcode", "zipCode", "storeNumber", "state")),
                         "store": scalars(store_details, ("id", "storeNumber", "zip", "zipCode", "state"))},
            "status": status,
            "reason": "no_explicit_delivery_zip" if not zips else "explicit_delivery_zip",
            "price_region_verified": False}
