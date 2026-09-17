"""REF/LDY PlusX labels from the same GraphQL inputs as Best Buy's PLP.

Rules audited against PLP chunks 9555-03e4c0abcd69bdaa and
2577.bd77f6fa706e7d58 on 2026-09-17. No DOM traversal or PDP navigation.
Missing responses are unknown, never zero or a legacy gift/hotOffer count.
"""

import hashlib
import json
import re
from datetime import datetime, timezone


SOURCE = "graphql_price_experience"
RULE_VERSION = "plp-plusx-20260917"
COMPONENTS = ("membership", "rebates", "spend_and_get", "top_offers", "cabo_suco", "gifts")
EVIDENCE_FIELD = "offer_graphql_json"


def uses_graphql_offers(category):
    return str(category or "").strip().upper() in {"REF", "LDY"}


def offer_evidence(row):
    value = row.get(EVIDENCE_FIELD)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    if not isinstance(value, dict):
        return {}
    sku = str(row.get("sku_id") or "")
    if not sku or str(value.get("sku_id") or "") != sku or value.get("source") != SOURCE:
        return {}
    if value.get("status") not in {"verified", "unverified"} or value.get("rule_version") != RULE_VERSION:
        return {}
    if value["status"] == "verified":
        parts = value.get("components")
        if (not isinstance(parts, dict) or set(parts) != set(COMPONENTS)
                or any(type(n) is not int or n < 0 for n in parts.values())
                or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("config_hash", "")))
                or not re.fullmatch(r"\d{5}", str(value.get("zip_code", "")))):
            return {}
        total = sum(parts.values())
        if value.get("count") != (str(total) if total else ""):
            return {}
    return value


def normalize_graphql_offer(row, sources=(), category=None):
    """Change only REF/LDY offer fields; require matching API proof through CSVs.

    Verified zero remains authoritative. A failed request remains blank; an
    unmarked gift/hot-offer count or old DOM evidence is never a fallback.
    """
    if not uses_graphql_offers(category or row.get("category_key")):
        return row
    proof = offer_evidence(row)
    if not proof:
        for source in sources:
            if str(source.get("sku_id") or row.get("sku_id") or "") != str(row.get("sku_id") or ""):
                continue
            candidate = offer_evidence({**source, "sku_id": row.get("sku_id")})
            if candidate:
                proof = candidate
                break
    if not proof:
        proof = {"sku_id": str(row.get("sku_id") or ""), "source": SOURCE,
                 "rule_version": RULE_VERSION, "status": "unverified", "reason": "graphql_evidence_missing"}
    row[EVIDENCE_FIELD] = json.dumps(proof, ensure_ascii=False, separators=(",", ":"))
    row["offer"] = row["offer_count"] = proof.get("count", "") if proof["status"] == "verified" else ""
    return row


def graphql_offer_count(row):
    proof = offer_evidence(row)
    return proof.get("count", "") if proof.get("status") == "verified" else ""


def partition_listing_errors(errors):
    """Ignore only known sibling subtrees that cannot feed this offer count.

    Inspect the field immediately below the listing product, not arbitrary path
    substrings. Errors on price/offers, a product/ancestor, or unknown paths still
    block certification. Open-box option products do not supply the main card's
    price.openBoxCondition or its offers.
    """
    blocking, unrelated = [], []
    for error in errors:
        path = error.get("path") if isinstance(error, dict) else None
        sibling = None
        if isinstance(path, list) and "product" in path:
            index = path.index("product") + 1
            if index < len(path):
                sibling = path[index]
        if sibling in ("fulfillmentOptions", "arModels", "openBoxOptions"):
            unrelated.append(error)
        else:
            blocking.append(error)
    return blocking, unrelated

# Defaults from the price-experience package, merged with live Platman JSON.
DEFAULTS = {
    "plusX.membershipUpsell.enabled": True,
    "plusX.ecoRebatesStandalone.enabled": False,
    "plusX.giftWithPurchase.enabled": True,
    "plusX.rebates.enabled": False,
    "plusX.rebates.excludeFromCount.enabled": False,
    "plusX.rebates.countByContent.enabled": True,
    "plusX.spendAndGet.enabled": False,
    "plusX.spendAndGetCarousel.enabled": False,
    "plusX.spendAndGetCarousel.rewardCabos.enabled": False,
    "plusX.topOffers.countByContent.enabled": False,
    "plusX.topOffers.countByContent.countTotal": False,
    "plusX.topOffers.enabled": False,
    "plusX.topOffers.hotOfferOnly.enabled": False,
    "plusX.activatedTopOffers.enabled": True,
    "plusX.caboSuco.enabled": False,
    "specialOffersList.offerTypes.allowed": ["PWP Global"],
    "specialOffersList": {"maxOffers": 20, "checkmarkMessagingRequired": False,
                          "filterFinanceMinPurchaseAmount": False, "viewableOfferLimit": 5},
}

CONFIG_QUERY = {
    "operationName": "PlatmanQuery", "variables": {"key": "price-experience-config"},
    "query": "query PlatmanQuery($key:String!){versionedJsonByKey(key:$key){versionedJsonId json}}",
}

class UnverifiedOffer(ValueError):
    pass


def required(obj, name, types, nullable=False):
    if not isinstance(obj, dict) or name not in obj:
        raise UnverifiedOffer("missing_" + name)
    value = obj[name]
    if value is None and nullable:
        return value
    if not isinstance(value, types):
        raise UnverifiedOffer("invalid_" + name)
    return value


def merge_config(value):
    if not isinstance(value, dict) or not value:
        raise UnverifiedOffer("missing_price_experience_config")
    config = {**DEFAULTS, **value}
    special = required(value, "specialOffersList", dict) if "specialOffersList" in value else {}
    config["specialOffersList"] = {**DEFAULTS["specialOffersList"], **special}
    for key, default in DEFAULTS.items():
        if isinstance(default, bool) and type(config[key]) is not bool:
            raise UnverifiedOffer("invalid_config_" + key)
    allowed = config["specialOffersList.offerTypes.allowed"]
    if not isinstance(allowed, list) or any(not isinstance(x, str) for x in allowed):
        raise UnverifiedOffer("invalid_allowed_offer_types")
    for key in ("maxOffers", "viewableOfferLimit"):
        value = config["specialOffersList"][key]
        if type(value) is not int or value < 0:
            raise UnverifiedOffer("invalid_config_" + key)
    for key in ("checkmarkMessagingRequired", "filterFinanceMinPurchaseAmount"):
        if type(config["specialOffersList"][key]) is not bool:
            raise UnverifiedOffer("invalid_config_" + key)
    return config


def js_truthy(value):
    # JS empty objects/arrays are truthy (Python's are not).
    return value is not None and value is not False and value != "" and value != 0


def eligible_offers(product, config):
    connection = required(product, "offers", dict, nullable=True)
    items = required(connection, "offers", list, nullable=True) if connection is not None else []
    result = []
    seen = {}
    for item in items or []:
        offer_id = required(item, "offerId", str, nullable=True)
        offer_type = required(item, "offerType", str, nullable=True)
        hot = required(item, "hotOffer", bool, nullable=True)
        complex_ = required(item, "complexMemberOffer", bool, nullable=True)
        signature = (offer_type, hot, complex_)
        if offer_id in seen and seen[offer_id] != signature:
            raise UnverifiedOffer("conflicting_offer_id")
        seen[offer_id] = signature
        if (offer_id is not None and offer_type in config["specialOffersList.offerTypes.allowed"]
                and not complex_ and (not config["plusX.topOffers.hotOfferOnly.enabled"] or hot)):
            result.append(item)
    return result


def content_count(timeline):
    """SiteControl: first column/first widget of each row, as in the PLP."""
    if timeline is None:
        return 0
    rows = required(timeline, "rows", list, nullable=True)
    count = 0
    for row in rows or []:
        columns = required(row, "columns", list, nullable=True)
        if not columns:
            continue
        widgets = required(columns[0], "widgets", list, nullable=True)
        if not widgets:
            continue
        widget = widgets[0]
        if not isinstance(widget, dict):
            raise UnverifiedOffer("invalid_content_widget")
        content = widget.get("content") or {}
        message = content.get("skuPromotionalMessage")
        if message is None:
            assets = widget.get("assets") or []
            message = ((assets[0].get("content") or {}).get("skuPromotionalMessage") if assets else None)
        count += int(js_truthy(message))
    return count


def rebate_count(eco, zip_code):
    if eco is None:
        return 0
    area = required(eco, "area", dict, nullable=True)
    if area and area.get("zipCode") not in (None, zip_code):
        raise UnverifiedOffer("rebate_zip_mismatch")
    details = required(eco, "productRebateDetails", list, nullable=True)
    ids = set()
    for detail in details or []:
        programs = required(detail, "rebatePrograms", list, nullable=True)
        for item in programs or []:
            # Same validity filter as the site's eco rebate adapter.
            if (isinstance(item, dict)
                    and all(isinstance(item.get(k), str) for k in ("id", "name", "amountLabel", "formLabel"))
                    and all(isinstance(item.get(k), list) for k in ("offerTypes", "importantDetails"))):
                ids.add(item["id"])
    return len(ids)


def count_components(product, config, content, rebates, zip_code):
    if product.get("__typename") != "Product":
        raise UnverifiedOffer("unsupported_product_type")
    price = required(product, "price", dict)
    if price.get("__typename") != "ItemPrice":
        raise UnverifiedOffer("unsupported_price_type")
    components = dict.fromkeys(COMPONENTS, 0)
    if required(price, "showPlusOffers", bool, nullable=True) is False:
        return components
    if required(price, "openBoxCondition", (int, float), nullable=True) is not None:
        # Anonymous open-box cards do not display the new-product benefits.
        return components
    if required(price, "mobileContracts", list, nullable=True):
        raise UnverifiedOffer("unsupported_activated_device")
    if config["plusX.membershipUpsell.enabled"]:
        components["membership"] = int(js_truthy(required(price, "whatIfPrice", dict, nullable=True)))
    if config["plusX.spendAndGet.enabled"]:
        spend = required(price, "spendAndGetCabosAvailable", list, nullable=True)
        tiers = required(price, "tieredOffersTracking", list, nullable=True)
        components["spend_and_get"] = int(bool(spend or tiers))
    if config["plusX.caboSuco.enabled"]:
        components["cabo_suco"] = int(js_truthy(required(price, "customerSelectedDiscountsAvailable", dict, nullable=True)))
    if config["plusX.giftWithPurchase.enabled"]:
        cart = required(price, "priceWithCart", dict, nullable=True)
        gifts = required(cart, "giftSkus", list, nullable=True) if cart is not None else None
        if gifts is None:
            gifts = required(price, "giftSkus", list, nullable=True)
        if any(not isinstance(g, dict) or not g.get("skuId") for g in gifts or []):
            raise UnverifiedOffer("invalid_gift_skus")
        components["gifts"] = len(gifts or [])
    if config["plusX.topOffers.enabled"]:
        items = eligible_offers(product, config)
        if config["plusX.topOffers.countByContent.enabled"]:
            # SiteControl returns one timeline per distinct offer ID.
            total = sum(content[offer_id] for offer_id in {i["offerId"] for i in items})
        else:
            total = len(items)
        total = min(total, config["specialOffersList"]["viewableOfferLimit"])
        components["top_offers"] = (total if config["plusX.topOffers.countByContent.enabled"]
                                   and config["plusX.topOffers.countByContent.countTotal"]
                                   and not config["plusX.topOffers.hotOfferOnly.enabled"] else int(total > 0))
    if ((config["plusX.rebates.enabled"] or config["plusX.ecoRebatesStandalone.enabled"])
            and not config["plusX.rebates.excludeFromCount.enabled"]):
        eligible = required(price, "isEcoRebateEligible", bool, nullable=True)
        components["rebates"] = int(bool(eligible) and (
            rebates[product["skuId"]] > 0 if config["plusX.rebates.countByContent.enabled"] else True))
    return components


def browser_fetch(browser, payload, timeout):
    script = (
        "return fetch('/gateway/graphql', {method:'POST', credentials:'include',"
        "headers:{'accept':'application/json','content-type':'application/json'},"
        f"body:JSON.stringify({json.dumps(payload, ensure_ascii=False)})"
        "}).then(async r=>JSON.stringify({status:r.status,body:await r.text()}))"
        ".catch(e=>JSON.stringify({error:String(e)}));"
    )
    envelope = browser.run_js(script, timeout=timeout)
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    if not isinstance(envelope, dict) or envelope.get("status") != 200:
        raise UnverifiedOffer("offer_http_" + str(envelope.get("status", "ERR") if isinstance(envelope, dict) else "ERR"))
    body = envelope.get("body")
    return json.loads(body) if isinstance(body, str) else body


def collect_graphql_offers(rows, payload, browser, timeout=30, fetch=None, listing_errors=()):
    """Reuse listing products; fetch config/support, never re-query SKU products.

    Keep request/response proof in the caller's page_offers.json. An error in a
    content/rebate alias invalidates only SKUs that depend on that alias.
    """
    fetch = fetch or (lambda request: browser_fetch(browser, request, min(timeout, 30)))
    report = {"source": SOURCE, "rule_version": RULE_VERSION,
              "observed_at": datetime.now(timezone.utc).isoformat(), "requests": [], "evidence": {},
              "product_source": "listing_graphql_raw_product_json"}
    skus = sorted({str(row.get("sku_id") or "") for row in rows})

    def request(value):
        response = {}
        for attempt in range(2):
            safe_variables = {k: v for k, v in value.get("variables", {}).items() if k != "priceInput"}
            if "priceInput" in value.get("variables", {}):
                safe_variables["priceInput"] = {k: v for k, v in value["variables"]["priceInput"].items()
                                               if k not in {"visitorId", "customerId", "customerAttributes"}}
            entry = {"operation": value["operationName"], "attempt": attempt + 1,
                     "query": value["query"], "variables": safe_variables}
            report["requests"].append(entry)
            try:
                response = fetch(value)
                entry["response"] = response
                if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
                    raise UnverifiedOffer("missing_graphql_data")
                if not response.get("errors"):
                    return response
            except Exception as exc:
                entry["error"] = str(exc)
                response = {}
        return response

    def alias(response, key):
        errors = response.get("errors") or []
        if any(not e.get("path") or e["path"][0] == key for e in errors):
            raise UnverifiedOffer("graphql_error_" + key)
        data = response.get("data") or {}
        if key not in data:
            raise UnverifiedOffer("missing_response_" + key)
        return data[key]

    def evidence(sku, **fields):
        return {"sku_id": sku, "source": SOURCE, "rule_version": RULE_VERSION,
                "observed_at": report["observed_at"], **fields}

    try:
        # Price/offer errors can masquerade as valid nulls. Unrelated sibling
        # fields may fail independently without invalidating the offer inputs.
        blocking, unrelated = partition_listing_errors(listing_errors)
        report["ignored_listing_errors"] = unrelated
        if blocking:
            report["listing_errors"] = blocking
            raise UnverifiedOffer("listing_graphql_errors")
        variables = payload.get("variables") or {}
        price_input = dict(required(variables, "productPriceInput", dict))
        if any(price_input.get(k) for k in ("customerId", "planPaidMemberType", "selectedCabo",
                                           "selectedSuco", "customerAttributes")) or variables.get("isBestbuyMember"):
            raise UnverifiedOffer("unsupported_personalized_context")
        zip_code = required(variables, "destinationZipCode", str)
        if not re.fullmatch(r"\d{5}", zip_code) or any(not re.fullmatch(r"\d+", sku) for sku in skus):
            raise UnverifiedOffer("invalid_sku_or_zip")
        config_response = request(CONFIG_QUERY)
        config_record = alias(config_response, "versionedJsonByKey")
        config = merge_config(required(config_record, "json", dict))
        config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        report.update(zip_code=zip_code, config_hash=config_hash,
                      config_version=config_record.get("versionedJsonId"))
        offer_input = {"salesChannel": price_input.get("salesChannel", "LargeView"),
                       "effectivePlanPaidMemberType": None,
                       **{k: config["specialOffersList"][k] for k in (
                           "maxOffers", "checkmarkMessagingRequired", "filterFinanceMinPurchaseAmount")}}
        if config["plusX.topOffers.enabled"]:
            listing_offer_input = required(variables, "skuOffersInput", dict)
            if any(listing_offer_input.get(k) != v for k, v in offer_input.items()):
                raise UnverifiedOffer("listing_offer_context_mismatch")
        by_sku, invalid = {}, {}
        for row in rows:
            sku = str(row.get("sku_id") or "")
            try:
                raw = row.get("raw_product_json")
                product = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(product, dict) or str(product.get("skuId") or "") != sku:
                    raise UnverifiedOffer("listing_product_missing_or_sku_mismatch")
                if sku in by_sku and by_sku[sku] != product:
                    raise UnverifiedOffer("conflicting_listing_product")
                by_sku[sku] = product
            except (ValueError, TypeError) as exc:
                invalid[sku] = str(exc)
        report["listing_product_count"] = len(by_sku)
        fields, offer_ids, rebate_skus = [], set(), set()
        for sku in skus:
            if sku in invalid:
                continue
            try:
                product = by_sku[sku]
                price = required(product, "price", dict)
                if price.get("showPlusOffers") is False or price.get("openBoxCondition") is not None:
                    continue
                if config["plusX.topOffers.enabled"] and config["plusX.topOffers.countByContent.enabled"]:
                    offer_ids.update(i["offerId"] for i in eligible_offers(product, config))
                if ((config["plusX.rebates.enabled"] or config["plusX.ecoRebatesStandalone.enabled"])
                        and not config["plusX.rebates.excludeFromCount.enabled"]
                        and config["plusX.rebates.countByContent.enabled"] and price.get("isEcoRebateEligible")):
                    rebate_skus.add(sku)
            except (KeyError, ValueError, TypeError) as exc:
                invalid[sku] = str(exc)
        offer_aliases = {offer_id: f"o{i}" for i, offer_id in enumerate(sorted(offer_ids))}
        for offer_id, key in offer_aliases.items():
            fields.append(f'{key}:siteControlTimeline(siteControlTimelineInput:{{page:{json.dumps(offer_id)},view:"native"}})'
                          '{rows{columns{widgets}}}')
        for sku in sorted(rebate_skus):
            fields.append(f'r{sku}:productBySkuId(skuId:{json.dumps(sku)})'
                          '{skuId ecoRebates(zipCode:' + json.dumps(zip_code) + ',useRetailProductsPlatform:false)'
                          '{area{zipCode} productRebateDetails{rebatePrograms'
                          '{id name amountLabel formLabel offerTypes importantDetails}}}}')
        support = request({"operationName": "OfferCountContent", "variables": {},
                           "query": "query OfferCountContent{" + " ".join(fields) + "}"}) if fields else {}
        content, rebates = {}, {}
        for offer_id, key in offer_aliases.items():
            try:
                content[offer_id] = content_count(alias(support, key))
            except (ValueError, TypeError, AttributeError):
                pass  # Missing proof is detected per SKU, below.
        for sku in rebate_skus:
            try:
                product = alias(support, "r" + sku)
                if not isinstance(product, dict) or str(product.get("skuId")) != sku:
                    raise UnverifiedOffer("rebate_sku_mismatch")
                rebates[sku] = rebate_count(required(product, "ecoRebates", dict, nullable=True), zip_code)
            except (ValueError, TypeError, AttributeError):
                pass
        for sku in skus:
            try:
                if sku in invalid:
                    raise UnverifiedOffer(invalid[sku])
                components = count_components(by_sku[sku], config, content, rebates, zip_code)
                count = sum(components.values())
                report["evidence"][sku] = evidence(sku, status="verified", count=str(count) if count else "",
                    components=components, config_hash=config_hash, zip_code=zip_code)
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                report["evidence"][sku] = evidence(sku, status="unverified", reason="incomplete_offer_proof:" + str(exc))
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        report["evidence"] = {sku: evidence(sku, status="unverified", reason=str(exc)) for sku in skus}
    for row in rows:
        proof = report["evidence"][str(row.get("sku_id") or "")]
        row["offer_graphql_json"] = json.dumps(proof, ensure_ascii=False, separators=(",", ":"))
        row["offer"] = row["offer_count"] = proof.get("count", "") if proof["status"] == "verified" else ""
    report["complete"] = all(e["status"] == "verified" for e in report["evidence"].values())
    report["reason"] = "graphql_verified" if report["complete"] else "graphql_offer_unverified"
    return report
