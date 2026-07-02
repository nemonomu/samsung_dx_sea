import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from requests import RequestException
from zenrows import ZenRowsClient

from .step00_config import DEFAULT_BESTBUY_RUN_ROOT, apply_bestbuy_location, bestbuy_store_id, bestbuy_zip_code
from .step08_detail_enrichment import (
    as_list,
    best_fulfillment_availability,
    best_shipping_availability,
    date_to_relative_or_phrase,
    delivery_text,
    fallback_review20_payload,
    fastest_delivery_from_get_it_fast,
    fastest_delivery_text,
    graphql_params,
    pickup_text,
    request_cost,
    target_url,
)


REQUEST_TIMEOUT = int(os.getenv("ZENROWS_TIMEOUT", "240"))
PROBE_ROOT = Path(os.getenv("BESTBUY_AVAILABILITY_PROBE_ROOT", DEFAULT_BESTBUY_RUN_ROOT / "availability_probe"))
PROBE_MODE = os.getenv("BESTBUY_AVAILABILITY_PROBE_MODE", "reference").strip().lower()
PROBE_MINIMAL = os.getenv("BESTBUY_AVAILABILITY_PROBE_MINIMAL", "0").lower() in {"1", "true", "yes", "y"}
PROBE_VARIANT = os.getenv(
    "BESTBUY_AVAILABILITY_PROBE_VARIANT",
    "detail_with_fulfillment" if PROBE_MINIMAL else "full",
).strip().lower()
PROBE_SKUS = [
    value.strip()
    for value in re.split(r"[\s,;]+", os.getenv("BESTBUY_AVAILABILITY_PROBE_SKUS", os.getenv("BESTBUY_DETAIL_SKUS", "6623791")))
    if value.strip()
]


PRODUCT_SCHEMA_WITH_FULFILLMENT_QUERY = (
    "query ProductSchemaAvailabilityProbe($skuId:String!$salesChannel:String!$fulfillmentInput:ProductFulfillmentInput!)"
    "{productBySkuId(skuId:$skuId){skuId bsin name{short}url{pdp}"
    "price(input:{salesChannel:$salesChannel}){customerPrice}"
    "fulfillmentOptions(input:$fulfillmentInput){buttonStates{buttonState displayText secondaryDisplayText}"
    "shippingDetails{shippingAvailability{shippingEligible defaultCustomerLosGroupId promiseByStreetDate "
    "customerLOSGroup{customerLosGroupId minLineItemMaxDate maxLineItemMaxDate name displayDateType price}}}"
    "deliveryDetails{deliveryAvailability{deliveryEligible deliverable deliverySlots{date}}}"
    "ispuDetails{ispuAvailability{pickupEligible instoreInventoryAvailable quantity minPickupInHours maxDate fulfillDate promiseByStreetDate}}}}}"
)

DETAIL_WITH_FULFILLMENT_QUERY = (
    "query ProductSchema_init($skuId:String!$salesChannel:String!$fulfillmentInput:ProductFulfillmentInput!)"
    "{productBySkuId(skuId:$skuId){bsin name{short}images{piscesHref}url{pdp}description{short}"
    "skuId manufacturer{modelNumber}color{displayName}brand reviewInfo{averageRating reviewCount recommendedPercent}"
    "specificationGroups{specifications{displayName value}}"
    "buyingOptions{description pdpUrl skuId type product{price(input:{salesChannel:$salesChannel}){customerPrice}}}"
    "reviews(filter:{pageSize:20}){results{rating title text userNickname}}"
    "fulfillmentOptions(input:$fulfillmentInput){buttonStates{buttonState displayText secondaryDisplayText}"
    "shippingDetails{shippingAvailability{shippingEligible defaultCustomerLosGroupId promiseByStreetDate "
    "customerLOSGroup{customerLosGroupId minLineItemMaxDate maxLineItemMaxDate name displayDateType price}}}"
    "deliveryDetails{deliveryAvailability{deliveryEligible deliverable deliverySlots{date} installationSlots{date}}}"
    "ispuDetails{ispuAvailability{pickupEligible instoreInventoryAvailable quantity minPickupInHours maxDate fulfillDate promiseByStreetDate}}}}}"
)

DETAIL_WITH_GET_IT_FAST_QUERY = (
    "query ProductSchemaGetItFastProbe($skuId:String!$destinationZipCode:String$locationId:String)"
    "{productBySkuId(skuId:$skuId){skuId bsin name{short}url{pdp}}"
    "fulfillmentGetItFastOptions(input:{destinationZipCode:$destinationZipCode locationId:$locationId})"
    "{shippingCutOffDetails{getItBy getItByDate destinationZipCode}"
    "storeCutOffDetails{getItBy getItByDate minPickupHours locationId}}}"
)


FULFILLMENT_DYNAMIC_QUERY = (
    "query FulfillmentOptionHook_FulfillmentDynamicQuery($skuId:String!$fulfillmentInput:ProductFulfillmentInput!"
    "$productPriceInput:ProductItemPriceInput!$openBoxCondition:Int){"
    "productBySkuId(skuId:$skuId openBoxCondition:$openBoxCondition){skuId bsin name{short}url{pdp}"
    "fulfillmentOptions(input:$fulfillmentInput){buttonStates{buttonState displayText secondaryDisplayText}"
    "shippingDetails{shippingAvailability{shippingEligible defaultCustomerLosGroupId promiseByStreetDate "
    "customerLOSGroup{customerLosGroupId minLineItemMaxDate maxLineItemMaxDate name displayDateType price}}}"
    "deliveryDetails{deliveryAvailability{deliveryEligible deliverable deliverySlots{date} installationSlots{date}}}"
    "ispuDetails{ispuAvailability{pickupEligible instoreInventoryAvailable quantity minPickupInHours maxDate fulfillDate promiseByStreetDate}}}}}"
)

PDP_RENDER_FULFILLMENT_DYNAMIC_QUERY = """
query FulfillmentOptionHook_FulfillmentDynamicQuery($skuId:String!$fulfillmentInput:ProductFulfillmentInput!$productPriceInput:ProductItemPriceInput!$openBoxCondition:Int){productBySkuId(skuId:$skuId openBoxCondition:$openBoxCondition){skuId ...FullfillmentProductBySkuIdFragment fulfillmentOptions(input:$fulfillmentInput){...FullfillmentOptionsFragment}badgesV2{label}}}fragment FullfillmentProductBySkuIdFragment on Product{brand brandId classification{class{id}}isSmallMediumBusiness releaseDateDisplayValue whatItIs eligibleGatedEventCustomerSegments{canPurchaseNow}isConstrainedHighVelocity inStoreServiceType buyingOptions{type product{openBoxCondition openBoxOptions{code}inStoreServiceType price(input:$productPriceInput){openBoxCondition}primaryImage{piscesHref}name{short}}pdpUrl}price(input:$productPriceInput){customerPrice mobileContracts{isDefaultContract purchaseType numberOfPayments}}waitlists{enrollmentPaused id name type}...MpFragment}fragment MpFragment on Product{bsinProduct{bsin products{openBoxCondition condition{type}seller{classification}skuId}}bsin seller{classification id}}fragment FullfillmentOptionsFragment on FulfillmentOptionsList{buttonStates{...ButtonStatesFragment}shippingDetails{...ShippingDetailsFragment}deliveryDetails{...DeliveryDetailsFragment}ispuDetails{...IspuDetailsFragment}}fragment ButtonStatesFragment on ButtonState{buttonState condition displayText secondaryButtonState secondaryDisplayText planButtonState hyperlinkUrl}fragment ShippingDetailsFragment on FulfillmentShippingDetail{destinationZipCode shippingAvailability{backordered condition customerLOSGroup{customerLosGroupId displayDateType maxLineItemMaxDate minLineItemMaxDate name price}levelOfServices{code id isLessThanTruckload isScheduleParcelDelivery}defaultCustomerLosGroupId downloadEligible emailEligible fulfillByVendor preorderable promiseByStreetDate whenAvailableFlag shippingEligible restrictions{category}}sku}fragment DeliveryDetailsFragment on FulfillmentDeliveryDetail{deliveryAvailability{salLocationId deliverable deliveryEligible forceSkipScheduling homeDeliveryDisplayDateType condition deliverySlots{date}deliveryServices{eligible levelsOfService{offerUnitPrice unitPrice}serviceType}installationSlots{date}backordered restrictions{category}}destinationZipCode}fragment IspuDetailsFragment on InStorePickupDetail{sku ispuAvailability{...IspuAvailabilityFragment}nearbyLocation{availability{maxDate pickupEligible quantity}distance store{...IspuStoreFragment}}nearbyLocations{availability{fulfillmentType maxDate minPickupInHours}store{...IspuStoreFragment}}sku store{...IspuStoreFragment}}fragment IspuAvailabilityFragment on InStorePickupAvailability{backordered condition displayDateType downloadEligible emailEligible fulfillDate fulfillmentType instoreInventoryAvailable inStoreOnly maxDate minPickupInHours pickupEligible preorderable promiseByStreetDate whenAvailableFlag quantity restrictions{category}inStoreServices{installationSlots{date}}}fragment IspuStoreFragment on FulfillmentPickUpStore{name storeId zip}
""".strip()

AVAILABILITY_FIELD_TOKENS = (
    "ProductFulfillmentInput",
    "fulfillmentOptions(input",
    "buttonStates",
    "shippingDetails",
    "deliveryDetails",
    "ispuDetails",
)
UI_AVAILABILITY_TEXTS = ("Get it", "Pick up", "Delivery as soon", "FREE")


def default_curl_reference_path():
    configured = os.getenv("BESTBUY_AVAILABILITY_CURL_REFERENCE", "").strip()
    if configured:
        return Path(configured)
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "references" / "curl_tv_network.html"
        if candidate.exists():
            return candidate
    return current.parents[1] / "references" / "curl_tv_network.html"


def curl_blocks_from_text(text):
    blocks = []
    current = []
    start_line = 0
    for line_number, line in enumerate(str(text or "").splitlines(), 1):
        if line.startswith('curl ^"'):
            if current:
                blocks.append((start_line, "\n".join(current)))
            current = [line]
            start_line = line_number
        elif current:
            current.append(line)
    if current:
        blocks.append((start_line, "\n".join(current)))
    return blocks


def readable_curl_text(block_text):
    text = str(block_text or "")
    return (
        text.replace('^\\^"', '"')
        .replace('^"', '"')
        .replace("^\\n", "\\n")
        .replace("^$", "$")
        .replace("^^", "^")
        .replace("^", "")
    )


def curl_block_url(block_text):
    first_line = str(block_text or "").splitlines()[0] if block_text else ""
    match = re.search(r'curl\s+"([^"]+)', readable_curl_text(first_line))
    return match.group(1) if match else ""


def curl_block_operation(block_text):
    match = re.search(r"x-requested-for-operation-name:\s*([^\^\"]+)", str(block_text or ""))
    return match.group(1).strip() if match else ""


def curl_block_data_len(block_text):
    match = re.search(r'--data-raw \^"(.*)"(?: &)?$', str(block_text or ""), re.S)
    return len(match.group(1)) if match else 0


def curl_block_data_raw(block_text):
    match = re.search(r'--data-raw \^"(.*)"(?: &)?$', str(block_text or ""), re.S)
    return match.group(1) if match else ""


def curl_block_json_payload(block_text):
    raw = curl_block_data_raw(block_text)
    if not raw:
        return {}
    text = readable_curl_text(raw)
    candidates = [
        text,
        text.replace('\\\\\\"', '\\"'),
        text.replace('\\\\"', '\\"'),
    ]
    for candidate in dict.fromkeys(candidates):
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        return value if isinstance(value, dict) else {}
    return {}


def fulfillment_endpoint_variables(url):
    if "variables=" not in str(url or ""):
        return {}
    try:
        from urllib.parse import parse_qs, urlparse

        raw = parse_qs(urlparse(url).query).get("variables", [""])[0]
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def offset_from_utc_text(value):
    match = re.fullmatch(r"UTC([+-])(\d{2}):?(\d{2})", str(value or "").strip())
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    hours = int(match.group(2))
    minutes = int(match.group(3))
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def event_local_date(event):
    device = event.get("device") if isinstance(event, dict) else {}
    timestamp = device.get("time") if isinstance(device, dict) else ""
    if not timestamp:
        return datetime.now().date()
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now().date()
    event_tz = offset_from_utc_text(device.get("timeZone") if isinstance(device, dict) else "")
    if event_tz and dt.tzinfo:
        dt = dt.astimezone(event_tz)
    return dt.date()


def days_out_text(prefix, base_date, days_out):
    if days_out in ("", None):
        return ""
    try:
        days = int(days_out)
    except (TypeError, ValueError):
        return ""
    target = base_date + timedelta(days=days)
    if days == 0:
        return f"{prefix} today"
    if days == 1:
        return f"{prefix} tomorrow"
    return f"{prefix} {target.strftime('%a, %b')} {target.day}"


def fastest_delivery_days_out_text(base_date, days_out):
    if days_out in ("", None):
        return ""
    try:
        days = int(days_out)
    except (TypeError, ValueError):
        return ""
    prefix = "Get it" if days in (0, 1) else "Get it by"
    return days_out_text(prefix, base_date, days)


def digital_fulfillment_event_rows(event, line):
    rows = []
    if not isinstance(event, dict):
        return rows
    interaction = event.get("interaction") if isinstance(event.get("interaction"), dict) else {}
    base_date = event_local_date(event)
    for sku in as_list(event.get("skus")):
        if not isinstance(sku, dict) or not isinstance(sku.get("fulfillment"), dict):
            continue
        rows.append(
            {
                "line": line,
                "event_name": interaction.get("name", ""),
                "sku": str(sku.get("id") or ""),
                "base_date": base_date.isoformat(),
                "fulfillment": sku.get("fulfillment") or {},
            }
        )
    return rows


def digital_fulfillment_rows_from_curl(text):
    rows = []
    parsed_event_count = 0
    event_count = 0
    for start_line, block_text in curl_blocks_from_text(text):
        url = curl_block_url(block_text)
        if "streams.bestbuy.com/customer/web-streams/v1/events/digital-experience-event" not in url:
            continue
        event_count += 1
        event = curl_block_json_payload(block_text)
        if not event:
            continue
        parsed_event_count += 1
        rows.extend(digital_fulfillment_event_rows(event, start_line))
    return rows, event_count, parsed_event_count


def fulfillment_row_score(row, fulfillment_type):
    fulfillment = row.get("fulfillment") if isinstance(row, dict) else {}
    if not isinstance(fulfillment, dict) or fulfillment.get("type") != fulfillment_type:
        return (-1, -1, -1)
    return (
        1 if fulfillment.get("daysOut") not in ("", None) else 0,
        1 if fulfillment.get("cost") not in ("", None) else 0,
        1 if fulfillment.get("isSelected") is True else 0,
    )


def best_digital_fulfillment_row(rows, sku, fulfillment_type):
    candidates = [
        row
        for row in rows
        if str(row.get("sku") or "") == str(sku) and (row.get("fulfillment") or {}).get("type") == fulfillment_type
    ]
    if not candidates:
        return {}
    return sorted(candidates, key=lambda row: fulfillment_row_score(row, fulfillment_type), reverse=True)[0]


def digital_event_availability_values(rows, sku):
    pickup = best_digital_fulfillment_row(rows, sku, "pickup")
    shipping = best_digital_fulfillment_row(rows, sku, "shipping")
    delivery = best_digital_fulfillment_row(rows, sku, "delivery")

    pickup_fulfillment = pickup.get("fulfillment") if isinstance(pickup, dict) else {}
    shipping_fulfillment = shipping.get("fulfillment") if isinstance(shipping, dict) else {}
    delivery_fulfillment = delivery.get("fulfillment") if isinstance(delivery, dict) else {}

    pickup_text_value = days_out_text(
        "Pick up",
        datetime.fromisoformat(pickup.get("base_date")).date() if pickup else datetime.now().date(),
        pickup_fulfillment.get("daysOut") if isinstance(pickup_fulfillment, dict) else "",
    )
    shipping_text_value = fastest_delivery_days_out_text(
        datetime.fromisoformat(shipping.get("base_date")).date() if shipping else datetime.now().date(),
        shipping_fulfillment.get("daysOut") if isinstance(shipping_fulfillment, dict) else "",
    )
    if shipping_text_value and isinstance(shipping_fulfillment, dict) and shipping_fulfillment.get("cost") in (0, 0.0, "0", "0.0"):
        shipping_text_value = f"{shipping_text_value} \u2022 FREE"
    delivery_text_value = days_out_text(
        "Delivery as soon as",
        datetime.fromisoformat(delivery.get("base_date")).date() if delivery else datetime.now().date(),
        delivery_fulfillment.get("daysOut") if isinstance(delivery_fulfillment, dict) else "",
    )
    return {
        "pick_up_availability": pickup_text_value,
        "fastest_delivery": shipping_text_value,
        "delivery_availability": delivery_text_value,
    }


def get_it_fast_availability_values(item):
    data = item.get("data") if isinstance(item, dict) else {}
    connection = data.get("fulfillmentGetItFastOptions") if isinstance(data, dict) else {}
    if not isinstance(connection, dict):
        connection = {}
    shipping = connection.get("shippingCutOffDetails") if isinstance(connection.get("shippingCutOffDetails"), dict) else {}
    stores = as_list(connection.get("storeCutOffDetails"))
    store = stores[0] if stores and isinstance(stores[0], dict) else {}
    return {
        "pick_up_availability": date_to_phrase_from_get_it_fast("Pick up", store),
        "fastest_delivery": fastest_delivery_from_get_it_fast(shipping),
        "delivery_availability": "",
    }


def date_to_phrase_from_get_it_fast(prefix, value):
    if not isinstance(value, dict):
        return ""
    if str(value.get("getItBy") or "").strip().lower() == "today":
        return f"{prefix} today"
    if str(value.get("getItBy") or "").strip().lower() == "tomorrow":
        return f"{prefix} tomorrow"
    return date_to_relative_or_phrase(prefix, value.get("getItByDate"))


def analyze_curl_reference_text(text):
    graphql_posts = []
    fulfillment_endpoints = []
    telemetry_days_out_count = 0
    digital_fulfillment_rows, digital_event_count, digital_event_parsed_count = digital_fulfillment_rows_from_curl(text)
    for start_line, block_text in curl_blocks_from_text(text):
        url = curl_block_url(block_text)
        if "www.bestbuy.com/gateway/graphql" not in url:
            if "daysOut" in block_text:
                telemetry_days_out_count += block_text.count("daysOut")
            continue
        block = {
            "line": start_line,
            "url": url,
            "operation": curl_block_operation(block_text),
            "data_len": curl_block_data_len(block_text),
            "has_availability_fields": any(token in block_text for token in AVAILABILITY_FIELD_TOKENS),
            "ui_text_hits": [token for token in UI_AVAILABILITY_TEXTS if token in block_text],
        }
        if "/gateway/graphql/fulfillment" in url:
            variables = fulfillment_endpoint_variables(url)
            input_data = variables.get("fulfillmentOptionsInput", {}) if isinstance(variables, dict) else {}
            button_state = input_data.get("buttonState", {}) if isinstance(input_data, dict) else {}
            block["fulfillment_input"] = {
                "sku": input_data.get("sku", "") if isinstance(input_data, dict) else "",
                "condition": input_data.get("condition", "") if isinstance(input_data, dict) else "",
                "context": button_state.get("context", "") if isinstance(button_state, dict) else "",
                "button": button_state.get("fulfillmentOption", "") if isinstance(button_state, dict) else "",
                "has_shipping": bool(input_data.get("shipping")) if isinstance(input_data, dict) else False,
                "has_delivery": bool(input_data.get("delivery")) if isinstance(input_data, dict) else False,
                "has_pickup": bool(input_data.get("inStorePickup")) if isinstance(input_data, dict) else False,
            }
            fulfillment_endpoints.append(block)
        else:
            graphql_posts.append(block)
    return {
        "graphql_post_count": len(graphql_posts),
        "graphql_posts_with_availability_fields": [
            block for block in graphql_posts if block.get("has_availability_fields")
        ],
        "graphql_posts_with_ui_text": [block for block in graphql_posts if block.get("ui_text_hits")],
        "fulfillment_endpoint_count": len(fulfillment_endpoints),
        "fulfillment_endpoint_examples": fulfillment_endpoints[:10],
        "digital_event_count": digital_event_count,
        "digital_event_parsed_count": digital_event_parsed_count,
        "digital_fulfillment_event_count": len(digital_fulfillment_rows),
        "digital_fulfillment_examples": digital_fulfillment_rows,
        "telemetry_days_out_count": telemetry_days_out_count,
    }


def probe_reference():
    reference_path = default_curl_reference_path()
    if not reference_path.exists():
        raise RuntimeError(f"Missing curl reference: {reference_path}")
    text = reference_path.read_text(encoding="utf-8", errors="replace")
    summary = analyze_curl_reference_text(text)
    run_dir = PROBE_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S") / "curl_reference"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "reference_path": str(reference_path),
                **summary,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    graphql_availability_count = len(summary["graphql_posts_with_availability_fields"])
    graphql_ui_text_count = len(summary["graphql_posts_with_ui_text"])
    print(
        "[availability_probe:reference] "
        f"file={reference_path} graphql_posts={summary['graphql_post_count']} "
        f"fulfillment_endpoint_calls={summary['fulfillment_endpoint_count']}",
        flush=True,
    )
    print(
        "[availability_probe:graphql] "
        f"availability_field_posts={graphql_availability_count} "
        f"ui_text_posts={graphql_ui_text_count} telemetry_daysOut={summary['telemetry_days_out_count']}",
        flush=True,
    )
    for block in summary["fulfillment_endpoint_examples"][:5]:
        info = block.get("fulfillment_input", {})
        print(
            "[availability_probe:fulfillment_endpoint] "
            f"line={block['line']} op={block.get('operation') or '-'} sku={info.get('sku')} "
            f"context={info.get('context')} button={info.get('button') or '-'} "
            f"shipping={info.get('has_shipping')} delivery={info.get('has_delivery')} pickup={info.get('has_pickup')}",
            flush=True,
        )
    digital_rows = summary.get("digital_fulfillment_examples") or []
    digital_skus = []
    for sku in PROBE_SKUS + sorted({row.get("sku") for row in digital_rows if row.get("sku")}):
        if sku and sku not in digital_skus:
            digital_skus.append(sku)
    for sku in digital_skus[:5]:
        values = digital_event_availability_values(digital_rows, sku)
        type_counts = {}
        for row in digital_rows:
            if row.get("sku") != sku:
                continue
            fulfillment_type = (row.get("fulfillment") or {}).get("type") or "-"
            type_counts[fulfillment_type] = type_counts.get(fulfillment_type, 0) + 1
        print(
            "[availability_probe:digital_event] "
            f"sku={sku} types="
            f"{','.join(f'{key}:{value}' for key, value in sorted(type_counts.items()))} "
            f"pickup={values.get('pick_up_availability', '')!r} "
            f"fastest={values.get('fastest_delivery', '')!r} "
            f"delivery={values.get('delivery_availability', '')!r}",
            flush=True,
        )
    print(f"[availability_probe:raw] {run_dir}", flush=True)
    return run_dir, summary


def now():
    return datetime.now().isoformat(timespec="seconds")


def fulfillment_input(option_marker=None):
    zip_code = bestbuy_zip_code()
    store_id = bestbuy_store_id()
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


def product_price_input():
    variables = {
        "customerAttributes": "",
        "salesChannel": "LargeView",
        "customerId": None,
        "planPaidMemberType": "NULL",
        "ct": "",
        "isStoreAgent": False,
        "locationId": "",
        "usePriceWithCart": True,
        "useCabo": True,
        "useSuco": True,
    }
    return variables


def product_schema_fulfillment_payload(sku):
    return {
        "operationName": "ProductSchemaAvailabilityProbe",
        "variables": {
            "skuId": str(sku),
            "salesChannel": "LargeView",
            "fulfillmentInput": fulfillment_input("PICKUP"),
        },
        "query": PRODUCT_SCHEMA_WITH_FULFILLMENT_QUERY,
    }


def detail_with_fulfillment_payload(sku):
    variables = {
        "skuId": str(sku),
        "salesChannel": "LargeView",
        "fulfillmentInput": fulfillment_input("PICKUP"),
    }
    apply_bestbuy_location(variables)
    return {
        "operationName": "ProductSchema_init",
        "variables": variables,
        "query": DETAIL_WITH_FULFILLMENT_QUERY,
    }


def detail_with_get_it_fast_payload(sku):
    variables = {
        "skuId": str(sku),
        "destinationZipCode": bestbuy_zip_code(),
        "locationId": bestbuy_store_id(),
    }
    apply_bestbuy_location(variables)
    return {
        "operationName": "ProductSchemaGetItFastProbe",
        "variables": variables,
        "query": DETAIL_WITH_GET_IT_FAST_QUERY,
    }


def fulfillment_dynamic_payload(sku, option_marker=None):
    return {
        "operationName": "FulfillmentOptionHook_FulfillmentDynamicQuery",
        "variables": {
            "skuId": str(sku),
            "fulfillmentInput": fulfillment_input(option_marker),
            "productPriceInput": product_price_input(),
        },
        "query": FULFILLMENT_DYNAMIC_QUERY,
    }


def fulfillment_dynamic_exact_payload(sku, option_marker=None):
    return {
        "operationName": "FulfillmentOptionHook_FulfillmentDynamicQuery",
        "variables": {
            "skuId": str(sku),
            "fulfillmentInput": fulfillment_input(option_marker),
            "productPriceInput": product_price_input(),
            "openBoxCondition": None,
        },
        "extensions": {"clientLibrary": {"name": "@apollo/client", "version": "4.1.6"}},
        "query": PDP_RENDER_FULFILLMENT_DYNAMIC_QUERY,
    }


def response_item(response_json, index):
    if isinstance(response_json, list) and index < len(response_json):
        item = response_json[index]
        return item if isinstance(item, dict) else {}
    if index == 0 and isinstance(response_json, dict):
        return response_json
    return {}


def product_from_response(item):
    data = item.get("data") if isinstance(item, dict) else {}
    product = data.get("productBySkuId") if isinstance(data, dict) else {}
    return product if isinstance(product, dict) else {}


def availability_values(product):
    products = [product] if isinstance(product, dict) else []
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
    return {
        "pick_up_availability": pickup_text(pickup),
        "fastest_delivery": fastest_delivery_text(shipping),
        "delivery_availability": delivery_text(delivery),
    }


def row_availability_values(item, label):
    if label == "detail_with_get_it_fast":
        return get_it_fast_availability_values(item)
    return availability_values(product_from_response(item))


def error_summary(item):
    errors = item.get("errors") if isinstance(item, dict) else []
    output = []
    for error in as_list(errors):
        if not isinstance(error, dict):
            continue
        output.append(
            {
                "message": error.get("message", ""),
                "path": ".".join(str(part) for part in as_list(error.get("path"))),
                "code": (error.get("extensions") or {}).get("code", ""),
            }
        )
    return output


def probe_sku(client, sku):
    pdp_url = f"https://www.bestbuy.com/site/-/{sku}.p?skuId={sku}&intl=nosplash"
    if PROBE_VARIANT == "control":
        request_payload = [fallback_review20_payload(sku)]
        labels = ["detail_control"]
    elif PROBE_VARIANT in {"detail_with_fulfillment", "minimal"}:
        request_payload = [detail_with_fulfillment_payload(sku)]
        labels = ["detail_with_fulfillment"]
    elif PROBE_VARIANT == "detail_with_get_it_fast":
        request_payload = [detail_with_get_it_fast_payload(sku)]
        labels = ["detail_with_get_it_fast"]
    elif PROBE_VARIANT == "fulfillment_dynamic_exact":
        request_payload = [fulfillment_dynamic_exact_payload(sku, None)]
        labels = ["fulfillment_dynamic_exact"]
    elif PROBE_VARIANT == "product_schema_fulfillment":
        request_payload = [product_schema_fulfillment_payload(sku)]
        labels = ["product_schema_fulfillment"]
    elif PROBE_VARIANT == "full":
        request_payload = [
            fallback_review20_payload(sku),
            product_schema_fulfillment_payload(sku),
            fulfillment_dynamic_payload(sku, None),
            fulfillment_dynamic_payload(sku, "PICKUP"),
            fulfillment_dynamic_payload(sku, "SHIPPING"),
            fulfillment_dynamic_payload(sku, "DELIVERY"),
        ]
        labels = [
            "detail_control",
            "product_schema_fulfillment",
            "fulfillment_dynamic_default",
            "fulfillment_dynamic_pickup",
            "fulfillment_dynamic_shipping",
            "fulfillment_dynamic_delivery",
        ]
    else:
        raise RuntimeError(
            "BESTBUY_AVAILABILITY_PROBE_VARIANT must be control, detail_with_fulfillment, "
            "detail_with_get_it_fast, fulfillment_dynamic_exact, product_schema_fulfillment, or full"
        )
    started_at = now()
    start = time.perf_counter()
    response = client.post(
        "https://www.bestbuy.com/gateway/graphql",
        params=graphql_params(),
        headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://www.bestbuy.com",
            "referer": pdp_url,
            "x-client-id": "pdp-web",
        },
        data=json.dumps(request_payload),
        timeout=REQUEST_TIMEOUT,
    )
    text = response.text
    try:
        response_json = response.json()
    except ValueError:
        response_json = {}
    elapsed = round(time.perf_counter() - start, 3)

    rows = []
    for index, label in enumerate(labels):
        item = response_item(response_json, index)
        product = product_from_response(item)
        values = row_availability_values(item, label)
        rows.append(
            {
                "index": index,
                "label": label,
                "operation": request_payload[index].get("operationName", ""),
                "has_product": bool(product),
                "has_fulfillment_options": isinstance(product.get("fulfillmentOptions"), dict),
                "value_count": sum(1 for value in values.values() if value),
                "values": values,
                "errors": error_summary(item),
            }
        )

    run_dir = PROBE_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S") / str(sku)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "request.json").write_text(json.dumps(request_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "response.txt").write_text(text, encoding="utf-8", errors="replace")
    (run_dir / "response.json").write_text(json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "sku_id": str(sku),
                "url": pdp_url,
                "endpoint": "https://www.bestbuy.com/gateway/graphql",
                "variant": PROBE_VARIANT,
                "http_call_count": 1,
                "status_code": response.status_code,
                "elapsed_seconds": elapsed,
                "x_request_cost": request_cost(response.headers),
                "started_at": started_at,
                "finished_at": now(),
                "rows": rows,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return run_dir, response.status_code, rows


def main():
    if PROBE_MODE in {"reference", "curl", "curl_reference", "audit"}:
        probe_reference()
        return
    if PROBE_MODE not in {"live", "network"}:
        raise RuntimeError("BESTBUY_AVAILABILITY_PROBE_MODE must be reference or live")
    api_key = os.getenv("ZENROWS_API_KEY")
    if not api_key:
        raise RuntimeError("Set ZENROWS_API_KEY in .env")
    if not PROBE_SKUS:
        raise RuntimeError("Set BESTBUY_AVAILABILITY_PROBE_SKUS or BESTBUY_DETAIL_SKUS")
    client = ZenRowsClient(api_key)
    for sku in PROBE_SKUS:
        try:
            run_dir, status_code, rows = probe_sku(client, sku)
        except RequestException as exc:
            print(f"[availability_probe:error] sku={sku} error={exc}", flush=True)
            continue
        print(
            f"[availability_probe:call] sku={sku} endpoint=gateway_graphql "
            f"variant={PROBE_VARIANT} http_calls=1 status={status_code}",
            flush=True,
        )
        for row in rows:
            values = row["values"]
            errors = row["errors"]
            error_text = ";".join(
                f"{error.get('path')}:{error.get('code')}" for error in errors if error.get("path") or error.get("code")
            )
            print(
                "[availability_probe:op] "
                f"{row['index']} {row['label']} product={row['has_product']} "
                f"fulfillment={row['has_fulfillment_options']} values={row['value_count']} "
                f"pickup={values.get('pick_up_availability', '')!r} "
                f"fastest={values.get('fastest_delivery', '')!r} "
                f"delivery={values.get('delivery_availability', '')!r} "
                f"errors={error_text}",
                flush=True,
            )
        print(f"[availability_probe:raw] {run_dir}", flush=True)


if __name__ == "__main__":
    main()
