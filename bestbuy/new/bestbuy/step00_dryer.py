"""DRYER contract and public-product rules. No configuration or network I/O."""
import html
import re
from decimal import Decimal
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
            mentions_washer = re.search(r"\b(?:washer|washing machine)\b", sentence, re.I)
            for kind in ("front", "top"):
                term = rf"\b{kind}[ -]?load(?:ing)?\b"
                own_dryer = re.search(term + r"(?:(?!\b(?:washer|washing machine)\b)[^.!?;]){0,70}\bdryer\b", sentence, re.I)
                reverse = re.search(r"\bdryer\b[^.!?;]{0,70}" + term, sentence, re.I) if not mentions_washer else None
                design = re.search(term + r"\s+design\b", sentence, re.I) if not mentions_washer else None
                if own_dryer or reverse or design:
                    matches[kind] = sentence[:300]
    if len(matches) == 1:
        kind = next(iter(matches))
        return kind.title() + "load", {"source": "own_description_or_features", "evidence": matches[kind]}
    if matches:
        # A title must not override conflicting higher-priority own descriptions.
        return "", {"source": "conflicting_features"}
    name = text((product.get("name") or {}).get("short"))
    # This guards attribute ownership only; it does not remove a search result.
    if not re.search(r"\b(?:washer|washing machine|matching|compatible)\b", name, re.I):
        kinds = [kind for kind in ("front", "top")
                 if re.search(rf"\b{kind}[ -]?load(?:ing)?\b", name, re.I)]
        if len(kinds) == 1:
            return kinds[0].title() + "load", {"source": "own_product_name", "evidence": name[:300]}
        if kinds:
            return "", {"source": "conflicting_product_name"}
    return "", {"source": "not_stated"}


def category_targets(rows, main_limit=0, bsr_limit=100):
    """Reuse LDY identity deduplication; rank one organic Best-Selling list."""
    from . import step07_final_targets as legacy
    ranked = legacy.unique_main_rows([row for row in rows
        if row.get("container_type", "organic_product") == "organic_product"
        and not row.get("is_sponsored")])
    if main_limit:
        ranked = ranked[:main_limit]
    return [dict(row, bsr_rank=row["main_rank"] if not bsr_limit or row["main_rank"] <= bsr_limit else "")
            for row in ranked]


def dryer_capacity_with_evidence(product, helpers, feature_texts=()):
    # Use the same structured-spec lookup as LDY without washer-specific fallbacks.
    capacity = helpers.spec_value_by_names([product], ["Capacity"])
    if text(capacity):
        return capacity, {"source": "specifications"}
    for group in product.get("specificationGroups") or []:
        for spec in group.get("specifications") or []:
            label = text(spec.get("displayName"))
            if label.casefold() == "capacity" or re.fullmatch(r"dryer\s+capacity(?:\s*\([^)]*\))?", label, re.I):
                if text(spec.get("value")):
                    return spec["value"], {"source": "specifications"}

    def candidates(entries):
        found = {}
        for entry in entries:
            entry = text(entry)
            for match in re.finditer(r"\b(\d+(?:\.\d+)?)\s*(?:cubic\s+(?:feet|foot)|cu\.?\s*ft\.?)\b\.?", entry, re.I):
                # Keep the clause containing the volume; ignore matching washers.
                left = max(entry.rfind(separator, 0, match.start()) + len(separator)
                           if entry.rfind(separator, 0, match.start()) >= 0 else 0
                           for separator in (". ", "! ", "? ", "; "))
                ends = [entry.find(separator, match.end()) for separator in (". ", "! ", "? ", "; ")]
                right = min((end for end in ends if end >= 0), default=len(entry))
                clause = entry[left:right]
                if re.search(r"\b(?:washer|washing machine|matching|compatible)\b", clause, re.I):
                    continue
                number = Decimal(match.group(1))
                if number > 0:
                    found.setdefault(number, (match.group(0), clause[:300]))
        return found

    found = candidates(list(owned_description_texts(product)) + list(feature_texts))
    if len(found) == 1:
        value, evidence = next(iter(found.values()))
        return value, {"source": "own_description_or_features", "evidence": evidence}
    if found:
        return "", {"source": "conflicting_descriptions"}
    name = text((product.get("name") or {}).get("short"))
    found = candidates([name])
    if len(found) == 1:
        value, evidence = next(iter(found.values()))
        return value, {"source": "own_product_name", "evidence": evidence}
    return "", {"source": "conflicting_product_name" if found else "not_stated"}


def make_row(target, product, helpers, batch_id, crawl_time, feature_texts=()):
    feature_texts = tuple(feature_texts)
    sku_id = str(target["sku_id"])
    if str(product.get("skuId") or "") != sku_id:
        raise ValueError("product_identity_mismatch")
    expected_item = text(target.get("bsin") or target.get("item"))
    if expected_item and text(product.get("bsin")).casefold() != expected_item.casefold():
        raise ValueError("product_identity_mismatch")
    name = text((product.get("name") or {}).get("short")) or text(target.get("product_name") or target.get("retailer_sku_name"))
    product = dict(product, name={"short": name})
    url = primary_url((product.get("url") or {}).get("pdp"), sku_id) or primary_url(target.get("product_url"), sku_id)
    # Reuse the LDY model, capacity, rating/count and price formatters.
    model = helpers.product_model_number([product]) or target.get("model_number") or ""
    capacity, capacity_evidence = dryer_capacity_with_evidence(product, helpers, feature_texts)
    review = product.get("reviewInfo") or {}
    count = helpers.review_count_number(review.get("reviewCount"))
    if count is None:
        count = helpers.review_count_number(target.get("review_count"))
    rating = review.get("averageRating")
    if rating in (None, ""):
        rating = target.get("rating")
    count_text = helpers.int_commas(count) if count is not None else ""
    own_price = product.get("price") or {}
    # All price fields come from this product's primary offer, with no other-seller fallback.
    final, original, saving = helpers.price_output_fields(own_price, target, {})
    loading, evidence = loading_type(product, feature_texts)
    row = dict.fromkeys(FIELDS, "")
    row.update(country="SEA", account_name="Bestbuy", item=product.get("bsin") or "", sku=model,
        retailer_sku_name=name, product_url=url, count_of_reviews=count_text,
        star_rating="Not yet reviewed" if count == 0 else (rating if rating is not None else ""),
        count_of_star_ratings=count_text, final_sku_price=final, original_sku_price=original,
        savings=saving, main_rank=target.get("main_rank", ""), bsr_rank=target.get("bsr_rank", ""),
        calendar_week=f"w{crawl_time.isocalendar().week}",
        crawl_datetime=crawl_time.strftime("%Y-%m-%d %H:%M:%S"), batch_id=batch_id,
        loading_type=loading, capacity=capacity)
    evidence["sku_id"] = sku_id
    evidence["capacity_source"] = capacity_evidence["source"]
    if capacity_evidence.get("evidence"):
        evidence["capacity_evidence"] = capacity_evidence["evidence"]
    return row, evidence


def make_listing_row(target, helpers, batch_id, crawl_time, reason):
    """Retain observed listing values after exhausted detail retries, like LDY."""
    def observed(*keys):
        return next((target[key] for key in keys if target.get(key) not in (None, "")), None)

    listing_product = {
        "skuId": str(target["sku_id"]), "bsin": observed("bsin", "item"),
        "name": {"short": observed("product_name", "retailer_sku_name")},
        "manufacturer": {"modelNumber": observed("model_number", "sku")},
        "url": {"pdp": target.get("product_url")},
        "reviewInfo": {"reviewCount": observed("review_count", "count_of_reviews", "count_of_star_ratings"),
                       "averageRating": observed("rating", "star_rating")},
        "price": {"customerPrice": observed("customer_price", "final_sku_price"),
                  "regularPrice": observed("regular_price", "original_sku_price"),
                  "totalSavings": observed("total_savings", "savings")},
        "description": None, "features": [], "specificationGroups": [],
    }
    row, evidence = make_row(target, listing_product, helpers, batch_id, crawl_time)
    evidence.update(detail_status="failed", detail_reason=reason)
    if evidence["source"] == "not_stated":
        evidence["source"] = "detail_unavailable"
    if evidence["capacity_source"] == "not_stated":
        evidence["capacity_source"] = "detail_unavailable"
    return row, evidence
