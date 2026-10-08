"""DRYER contract and public-product rules. No configuration or network I/O."""
import html
import re
from urllib.parse import parse_qs, urlsplit

TEST_TABLE = "ldy_dryer_retail_test"
FIELDS = (
    "id", "country", "account_name", "item", "sku", "retailer_sku_name",
    "product_url", "count_of_reviews", "star_rating", "count_of_star_ratings",
    "final_sku_price", "original_sku_price", "savings", "main_rank", "bsr_rank",
    "calendar_week", "crawl_datetime", "batch_id", "loading_type", "capacity",
)


def text(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()


def is_standalone_dryer(name):
    name = text(name).lower()
    if not re.search(r"\bdryer\b", name):
        return False
    excluded = (
        r"\b(?:washer|washing machine|laundry center|combo|combination|bundle)\b",
        r"\b(?:hair|hand|shoe|boot|pet|food|nail|filament)[ -]+dryers?\b",
        r"\b(?:kit|cord|cable|pedestal|vent|duct|hose|parts?|accessor(?:y|ies))\b",
        r"\bdryer\s+rack\b|\brack\s+for\b.*\bdryer\b",
        r"\b(?:installation|repair|protection plan|dryer sheets?)\b",
        r"\bdryer\s+(?:cover|stand|adapter|outlet|connector|belt|filter|brush)\b",
    )
    return not any(re.search(pattern, name) for pattern in excluded)


def primary_url(value, sku_id):
    """Only public Best Buy URLs with the same retailer identifier are accepted."""
    value = text(value)
    if value.startswith("/"):
        value = "https://www.bestbuy.com" + value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc.lower() != "www.bestbuy.com":
        return ""
    query = parse_qs(parsed.query)
    found = re.search(r"/sku/(\d+)(?:/|$)", parsed.path)
    found_id = found.group(1) if found else (query.get("skuId") or [""])[0]
    if found_id and found_id != str(sku_id):
        return ""
    # Drop navigation/tracking parameters; never persist arbitrary URL queries.
    if "/product/" in parsed.path:
        return "https://www.bestbuy.com" + parsed.path
    return f"https://www.bestbuy.com/site/-/{sku_id}.p?skuId={sku_id}&intl=nosplash"


def public_product(product):
    """Allowlist the product itself, excluding offers, related items and HTTP data."""
    result = {}
    for key in ("skuId", "bsin", "name", "description", "manufacturer", "url",
                "reviewInfo", "specificationGroups", "price", "features", "featureGroups",
                "dotComDisplayStatus"):
        if key in product:
            result[key] = product[key]
    return result


def owned_description_texts(product):
    description = product.get("description") or {}
    if isinstance(description, dict):
        yield from (text(description.get(key)) for key in ("short", "long") if description.get(key))

    def feature_strings(value):
        if isinstance(value, str):
            yield text(value)
        elif isinstance(value, list):
            for entry in value:
                yield from feature_strings(entry)
        elif isinstance(value, dict):
            # A feature tree may contain a title/body, but not a related product tree.
            if value.get("skuId") and str(value["skuId"]) != str(product.get("skuId")):
                return
            if value.get("bsin") and str(value["bsin"]).lower() != str(product.get("bsin")).lower():
                return
            for key in ("title", "heading", "description", "text", "value", "features"):
                if key in value:
                    yield from feature_strings(value[key])
    for key in ("features", "featureGroups"):
        yield from feature_strings(product.get(key))


def loading_type(product, feature_texts=()):
    values = []
    for group in product.get("specificationGroups") or []:
        for spec in group.get("specifications") or []:
            label = text(spec.get("displayName")).casefold()
            if label in {"dryer load type", "dryer loading type", "load type", "loading type"}:
                value = text(spec.get("value"))
                if value:
                    values.append(value)
    if values:
        if len({v.casefold() for v in values}) == 1:
            return values[0], {"source": "specifications", "evidence": values[0]}
        return "", {"source": "conflicting_specifications"}

    matches = {}
    for entry in list(owned_description_texts(product)) + list(feature_texts):
        for sentence in re.split(r"[.!?;]\s+", text(entry)):
            for kind in ("front", "top"):
                term = rf"\b{kind}[ -]?load(?:ing)?\b"
                own_dryer = re.search(term + r"(?:(?!\bwasher\b)[^.!?;]){0,70}\bdryer\b", sentence, re.I)
                reverse = re.search(r"\bdryer\b[^.!?;]{0,70}" + term, sentence, re.I) if not re.search(r"\bwasher\b", sentence, re.I) else None
                design = re.search(term + r"\s+design\b", sentence, re.I) if not re.search(r"\bwasher\b", sentence, re.I) else None
                if own_dryer or reverse or design:
                    matches[kind] = sentence[:300]
    if len(matches) == 1:
        kind = next(iter(matches))
        return kind.title() + "load", {"source": "own_description_or_features", "evidence": matches[kind]}
    return "", {"source": "conflicting_features" if matches else "not_stated"}


def merge_targets(main_rows, bsr_rows, main_limit=0, bsr_limit=0):
    """Reuse LDY identity/rank rules; cap each dryer list before taking its union."""
    from . import step07_final_targets as legacy
    containers = {"organic_product", "sponsored_ingrid"}
    ranked_main = legacy.unique_main_rows([row for row in main_rows
        if row.get("container_type", "organic_product") in containers])
    selected_main = [row for row in ranked_main if is_standalone_dryer(row.get("product_name"))]
    if main_limit:
        selected_main = selected_main[:main_limit]

    selected_bsr, seen_skus, seen_items = [], set(), set()
    organic = sorted((row for row in bsr_rows if row.get("container_type") == "organic_product"),
        key=lambda row: legacy.int_value(row.get("global_organic_rank") or row.get("visual_rank")))
    for row in organic:
        sku_id = str(row.get("sku_id") or "").strip()
        if not sku_id or sku_id in seen_skus:
            continue
        seen_skus.add(sku_id)
        rank = len(seen_skus)
        if not is_standalone_dryer(row.get("product_name")) or legacy.row_seen(row, seen_items):
            continue
        selected_bsr.append(dict(row, bsr_rank=rank))
        legacy.remember_row(row, seen_items)
        if bsr_limit and len(selected_bsr) >= bsr_limit:
            break

    bsr_by_sku = {str(row["sku_id"]): row for row in selected_bsr}
    bsr_identity = {}
    for row in selected_bsr:
        for key in legacy.row_identity_keys(row):
            bsr_identity.setdefault(key, row)
    output, seen = [], set()
    for row in selected_main:
        bsr = legacy.lookup_bsr_row(row, bsr_by_sku, bsr_identity)
        output.append(dict(row, bsr_rank=bsr.get("bsr_rank", "")))
        legacy.remember_row(row, seen)
    for row in selected_bsr:
        if not legacy.row_seen(row, seen):
            output.append(dict(row, main_rank=""))
            legacy.remember_row(row, seen)
    return output


def dryer_capacity(product, helpers):
    # Use the same structured-spec lookup as LDY without washer-specific fallbacks.
    capacity = helpers.spec_value_by_names([product], ["Capacity"])
    if capacity not in ("", None):
        return capacity
    for group in product.get("specificationGroups") or []:
        for spec in group.get("specifications") or []:
            if re.fullmatch(r"dryer\s+capacity(?:\s*\([^)]*\))?", text(spec.get("displayName")), re.I):
                if spec.get("value") not in ("", None):
                    return spec["value"]
    return ""


def is_laundry_dryer(product, capacity):
    name = text((product.get("name") or {}).get("short"))
    # A bare "dryer" also matches hair dryers and unrelated goods. Require own
    # laundry-appliance evidence before writing a candidate into the test table.
    if re.search(r"\b(?:clothes|laundry|tumble)[ -]+dryer\b", name, re.I):
        return True
    volume = r"\b(?:cubic\s+feet|cu\.?\s*ft\.?)\b"
    if re.search(volume, text(capacity), re.I) or re.search(volume, name, re.I):
        return True
    for group in product.get("specificationGroups") or []:
        for spec in group.get("specifications") or []:
            label = text(spec.get("displayName")).casefold()
            if (label == "dryer heating source" or re.fullmatch(r"dryer capacity(?:\s*\([^)]*\))?", label)) and spec.get("value") not in ("",None):
                return True
    return False


def make_row(target, product, helpers, batch_id, crawl_time, feature_texts=()):
    sku_id = str(target["sku_id"])
    if str(product.get("skuId") or "") != sku_id:
        raise ValueError("product_identity_mismatch")
    name = text((product.get("name") or {}).get("short"))
    if not is_standalone_dryer(name):
        raise ValueError("detail_is_not_standalone_dryer")
    url = primary_url((product.get("url") or {}).get("pdp") or target.get("product_url"), sku_id)
    # Reuse the LDY model, capacity, rating/count and price formatters.
    model = helpers.product_model_number([product])
    capacity = dryer_capacity(product, helpers)
    review = product.get("reviewInfo") or {}
    count = helpers.review_count_number(review.get("reviewCount"))
    rating = review.get("averageRating")
    count_text = helpers.int_commas(count) if count is not None else ""
    own_price = product.get("price") or {}
    # All price fields come from this product's primary offer, with no other-seller fallback.
    final, original, saving = helpers.price_output_fields(own_price, {}, {})
    loading, evidence = loading_type(product, feature_texts)
    required = {"item": product.get("bsin"), "sku": model, "retailer_sku_name": name,
                "product_url": url, "final_sku_price": final}
    missing = [field for field, value in required.items() if not value]
    if missing:
        error = ValueError("missing_required_product_fields")
        error.missing_fields = missing
        raise error
    if not is_laundry_dryer(product, capacity):
        raise ValueError("laundry_dryer_type_not_verified")
    row = dict.fromkeys(FIELDS, "")
    row.update(country="SEA", account_name="Bestbuy", item=product["bsin"], sku=model,
        retailer_sku_name=name, product_url=url, count_of_reviews=count_text,
        star_rating="Not yet reviewed" if count == 0 else (rating if rating is not None else ""),
        count_of_star_ratings=count_text, final_sku_price=final, original_sku_price=original,
        savings=saving, main_rank=target.get("main_rank", ""), bsr_rank=target.get("bsr_rank", ""),
        calendar_week=f"w{crawl_time.isocalendar().week}",
        crawl_datetime=crawl_time.strftime("%Y-%m-%d %H:%M:%S"), batch_id=batch_id,
        loading_type=loading, capacity=capacity)
    evidence["sku_id"] = sku_id
    evidence["capacity_source"] = "specifications" if capacity else "not_stated"
    return row, evidence
