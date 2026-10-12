"""DRYER contract and public-product rules. No configuration or network I/O."""
import html
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

TABLE = "public.ldy_dryer_retail"
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


def classify_product_name(value):
    """Exclude only a clearly named non-target; keep laundry combinations."""
    name = text(value)
    normalized = re.sub(r"[\u2010-\u2015]", "-", name).casefold()

    def decision(action, reason):
        return {"action": action, "reason": reason, "source": "product_name", "evidence": name[:300]}

    # The accessory must be the item being sold, not something included with it.
    accessory = re.search(
        r"\b(?:laundry\s+)?pedestals?\b|\b(?:stacking|stack\s+bracket|recess)\s+kits?\b|"
        r"\bwall\s+guards?\b|\b(?:dryer|drying)\s+(?:racks?|lint\s+filters?|drum\s+belts?|"
        r"vent\s+(?:kits?|hoses?)|power\s+cords?)\b", normalized)
    if accessory:
        prefix = normalized[:accessory.start()]
        appliance = re.search(r"\b(?:dryers?|washers?|wash\s*tower|laundry\s+(?:tower|center)|drying\s+(?:machine|cabinet))\b", prefix)
        included = re.search(r"\b(?:with|includes?|including|featuring)\b", prefix)
        bundle = (re.search(r"\b(?:and|plus)\b|&", prefix) and re.search(
            r"\b(?:electric|gas|clothes|tumble)\b.*\bdryer\b|\bwash\s*tower\b|\blaundry\s+(?:tower|center)\b", prefix))
        included_with_appliance = appliance and (included or bundle)
        if not included_with_appliance:
            return decision("exclude", "accessory")
    non_laundry = re.search(r"\b(?:hand|hair)\s*-?\s*dryers?\b", normalized)
    if non_laundry:
        prefix = normalized[:non_laundry.start()]
        if not (re.search(r"\b(?:clothes|laundry|electric|gas)\b.*\bdryer\b", prefix)
                and re.search(r"\b(?:with|includes?|including|and|plus)\b", prefix)):
            return decision("exclude", "non_laundry_dryer")
    dryer = re.search(r"\bdryers?\b|\bdrying\s+(?:machine|cabinet)\b|\bwash\s*tower\b", normalized)
    if dryer:
        if (re.search(r"\b(?:electric|gas|clothes|laundry|tumble|condensation|heat\s*pump|vented|ventless)\b", normalized)
                or re.search(r"\b(?:wash\s*tower|laundry\s+(?:tower|center)|drying\s+(?:machine|cabinet))\b", normalized)):
            return decision("keep", "laundry_drying")
        return decision("uncertain", "unclassified_product")
    if re.search(r"\bwashers?\b|\bwashing\s+machines?\b", normalized):
        if re.search(r"\b(?:dry|drying|combo)\b|\ball[- ]in[- ]one\b", normalized):
            return decision("uncertain", "possible_laundry_drying")
        return decision("exclude", "washer_only")
    return decision("uncertain", "unclassified_product")


def product_selection(target, product=None):
    """Use only the product's own API data, never matching/related items."""
    if product is None:
        return classify_product_name(target.get("product_name") or target.get("retailer_sku_name"))
    name = text((product.get("name") or {}).get("short"))
    result = classify_product_name(name)
    result["source"] = "detail_product_name"
    if result["action"] != "uncertain" or result["reason"] == "possible_laundry_drying":
        return result
    specs = [spec for group in product.get("specificationGroups") or []
             for spec in group.get("specifications") or []]
    spec_decisions = []
    for spec in specs:
        label = text(spec.get("displayName")).casefold()
        if label in {"product name", "product type", "appliance type"}:
            result = classify_product_name(spec.get("value"))
            result["source"] = "own_specifications"
            if result["action"] != "uncertain":
                spec_decisions.append(result)
    # Matching Washer Type/Model is deliberately not evidence of a washer item.
    if any(text(s.get("value")) and text(s.get("displayName")).casefold() in
           {"dryer heating source", "dryer capacity", "washer/dryer combo type"} for s in specs):
        spec_decisions.append({"action": "keep", "reason": "laundry_drying", "source": "own_specifications"})
    if spec_decisions:
        if len({d["action"] for d in spec_decisions}) == 1:
            return spec_decisions[0]
        return {"action": "uncertain", "reason": "conflicting_product_type", "source": "own_specifications"}
    for entry in owned_description_texts(product):
        # Only an opening statement identifying this product can exclude it.
        opening = re.split(r"[.!?]\s+", entry, maxsplit=1)[0][:300]
        if (re.match(r"^(?:this|our)\b", opening, re.I)
                and not re.search(r"\b(?:matching|compatible)\b|\bpair\s+with\b|\bfor\s+use\s+with\b", opening, re.I)):
            result = classify_product_name(opening)
            result["source"] = "own_description"
            if result["action"] != "uncertain":
                return result
    return {"action": "uncertain", "reason": "unclassified_product", "source": "detail_api",
            "evidence": name[:300]}


def ordered_listing_rows(rows, kind):
    """Preserve each search order and the existing LDY container policy."""
    from . import step07_final_targets as legacy
    allowed = {"organic_product", "sponsored_ingrid"} if kind == "main" else {"organic_product"}
    rows = [row for row in rows if row.get("container_type", "organic_product" if kind == "main" else "") in allowed]
    if kind == "main":
        return sorted(rows, key=lambda row: (legacy.int_value(row.get("global_visual_rank")),
            legacy.int_value(row.get("page")), legacy.int_value(row.get("visual_rank"))))
    return sorted(rows, key=lambda row: legacy.int_value(row.get("global_organic_rank") or row.get("visual_rank")))


def _loading_matches(value, own_dryer, separate_units=False):
    """Resolve each load phrase locally, without borrowing a washer's attribute."""
    value = re.sub(r"[\u2010-\u2015]", "-", text(value))
    matches = {}
    for sentence in re.split(r"[.!?;]\s+", value):
        for match in re.finditer(r"\b(front|top)[ -]*load(?:ing)?\b", sentence, re.I):
            before, after = sentence[:match.start()], sentence[match.end():]
            # Reject a referenced appliance, not an entire sentence mentioning one.
            if re.search(r"\b(?:matching|compatible|pairs?\s+with|for\s+use\s+with)\b[^.!?;]{0,60}$", before, re.I):
                continue
            joint = re.match(r"\s+washers?\s*(?:and|&|/|\+)\s*dryers?\b", after, re.I)
            following = re.match(r"(?:(?!\b(?:washers?|washing\s+machines?)\b)[^.!?;]){0,70}\bdryers?\b", after, re.I)
            preceding = re.search(r"\bdryers?\b(?:(?!\b(?:washers?|washing\s+machines?)\b)[^.!?;]){0,70}$", before, re.I)
            owners = list(re.finditer(r"\b(?:washers?|washing\s+machines?|dryers?)\b", before[-70:], re.I))
            washer_owner = owners and not re.fullmatch(r"dryers?", owners[-1].group(), re.I)
            structure = re.match(
                r"\s+(?:design|(?:transparent\s+)?(?:lid|door)|(?:laundry\s+)?pair|machine|"
                r"all[- ]in[- ]one\s+(?:units?|machines?))\b", after, re.I)
            design = own_dryer and not washer_owner and structure
            if separate_units and structure and not re.fullmatch(r"\s+(?:laundry\s+)?pair", structure.group(), re.I):
                # An unqualified machine/lid on a two-unit tower may be its washer.
                design = None
            if washer_owner and structure:
                following = None
            next_owner = re.search(r"\b(?:washers?|washing\s+machines?|dryers?)\b", after[:70], re.I)
            if next_owner and not re.fullmatch(r"dryers?", next_owner.group(), re.I) and not joint:
                # A dryer elsewhere in the sentence cannot make "top load washer"
                # a dryer attribute. A later compatibility clause may follow its lid.
                compatibility = design and re.search(r"\b(?:pairs?|matches|compatible|compatibility)\b",
                                                      after[design.end():next_owner.start()], re.I)
                preceding = None
                if not compatibility:
                    design = None
            if joint or following or preceding or design:
                rule = "joint_washer_dryer" if joint else "own_dryer_phrase" if following or preceding else "own_unit_design"
                matches.setdefault(match.group(1).lower(), {"evidence": sentence[:300], "rule": rule})
    return matches


def loading_type(product, feature_texts=()):
    specs = [spec for group in product.get("specificationGroups") or []
             for spec in group.get("specifications") or []]
    values = []
    for spec in specs:
        label = text(spec.get("displayName")).casefold()
        if label in {"dryer load type", "dryer loading type", "load type", "loading type"}:
            value = text(spec.get("value"))
            if value:
                values.append(value)
    if values:
        if len({v.casefold() for v in values}) == 1:
            return values[0], {"source": "specifications", "evidence": values[0]}
        return "", {"source": "conflicting_specifications"}

    name = text((product.get("name") or {}).get("short"))
    labels = {text(spec.get("displayName")).casefold() for spec in specs if text(spec.get("value"))}
    own_dryer = bool(re.search(r"\bdryers?\b|\bdrying\s+(?:machine|cabinet)\b|\bwash\s*tower\b", name, re.I)) or any(
        label in {"dryer capacity", "dryer heating source"} for label in labels)
    separate_units = {"washer capacity", "dryer capacity"} <= labels or bool(
        re.search(r"\bwash\s*tower\b|\blaundry\s+(?:tower|center)\b", name, re.I))
    description = product.get("description") or {}
    entries = [("description." + key, description[key]) for key in ("short", "long")
               if isinstance(description, dict) and description.get(key)]
    for field in ("features", "featureGroups"):
        scoped = dict(product, description={}, features=[], featureGroups=[])
        scoped[field] = product.get(field)
        entries.extend((field, entry) for entry in owned_description_texts(scoped))
    entries.extend(("feature_texts", entry) for entry in feature_texts)
    matches = {}
    for field, entry in entries:
        for kind, evidence in _loading_matches(entry, own_dryer, separate_units).items():
            matches.setdefault(kind, dict(evidence, source_field=field))
    if len(matches) == 1:
        kind = next(iter(matches))
        return kind.title() + "load", dict(matches[kind], source="own_description_or_features")
    if matches:
        # A title must not override conflicting higher-priority own descriptions.
        return "", {"source": "conflicting_features"}
    matches = _loading_matches(name, own_dryer, separate_units)
    if len(matches) == 1:
        kind = next(iter(matches))
        return kind.title() + "load", dict(matches[kind], source="own_product_name", source_field="name.short")
    if matches:
        return "", {"source": "conflicting_product_name"}
    return "", {"source": "not_stated"}


def merge_targets(main_rows, bsr_rows, main_limit=0, bsr_limit=0):
    """Rank accepted products without gaps; retain LDY cross-list identity merge."""
    from . import step07_final_targets as legacy
    ranked_main = legacy.unique_main_rows(ordered_listing_rows(main_rows, "main"))
    selected_main = ranked_main
    if main_limit:
        selected_main = selected_main[:main_limit]

    selected_bsr, seen_items = [], set()
    for row in ordered_listing_rows(bsr_rows, "bsr"):
        sku_id = str(row.get("sku_id") or "").strip()
        if not sku_id or legacy.row_seen(row, seen_items):
            continue
        if bsr_limit and len(selected_bsr) >= bsr_limit:
            break
        selected_bsr.append(dict(row, bsr_rank=len(selected_bsr) + 1))
        legacy.remember_row(row, seen_items)

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
