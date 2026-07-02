import csv
import atexit
import json
import os
import time
from datetime import datetime
from pathlib import Path

from requests import RequestException
from zenrows import ZenRowsClient

from .step00_browser_session import add_intl_nosplash, close_browser_page, create_browser_page, env_bool, env_int
from .step00_availability_policy import ALL_AVAILABILITY_FIELDS, active_availability_fields, inactive_availability_fields
from .step00_config import DEFAULT_BESTBUY_RUN_ROOT, KRW_PER_USD, bestbuy_category, rel_path
from .step00_fulfillment_graphql import (
    FULFILLMENT_ENDPOINT,
    fulfillment_url,
    fulfillment_variables,
    parse_fulfillment_response,
    request_cost,
    zenrows_params,
)
from .step08_detail_enrichment import TARGET_CSV, compact_text, write_csv


CATEGORY = bestbuy_category()
AVAILABILITY_FIELDS = ALL_AVAILABILITY_FIELDS
ACTIVE_AVAILABILITY_FIELDS = active_availability_fields(CATEGORY)
INACTIVE_AVAILABILITY_FIELDS = inactive_availability_fields(CATEGORY)
RUN_ROOT = Path(os.getenv("BESTBUY_RUN_ROOT", DEFAULT_BESTBUY_RUN_ROOT))
OUTPUT_ROOT = Path(os.getenv("BESTBUY_OUTPUT_ROOT", RUN_ROOT / "output"))
DETAIL_ROOT = Path(os.getenv("BESTBUY_DETAIL_RUN_ROOT", RUN_ROOT / "detail"))
FINAL_OUTPUT_CSV = Path(
    os.getenv("BESTBUY_AVAILABILITY_BACKFILL_FINAL_CSV", os.getenv("BESTBUY_FINAL_OUTPUT_CSV", OUTPUT_ROOT / "final_output.csv"))
)
DETAIL_ROWS_CSV = Path(
    os.getenv("BESTBUY_AVAILABILITY_BACKFILL_DETAIL_ROWS_CSV", DETAIL_ROOT / "parsed" / "detail_enriched_rows.csv")
)
PRODUCT_LIST_CSV = Path(os.getenv("BESTBUY_PRODUCT_LIST_OUTPUT", OUTPUT_ROOT / "bestbuy_product_list.csv"))
BACKFILL_ROOT = Path(os.getenv("BESTBUY_AVAILABILITY_BACKFILL_ROOT", RUN_ROOT / "availability_backfill"))
BACKFILL_BATCH_ID = os.getenv("BESTBUY_AVAILABILITY_BACKFILL_BATCH_ID", os.getenv("BESTBUY_BATCH_ID", "")).strip()
REQUESTED_CHUNK_SIZE = int(os.getenv("BESTBUY_AVAILABILITY_BACKFILL_CHUNK_SIZE", "1"))
ALLOW_MULTI_SKU_FULFILLMENT = os.getenv("BESTBUY_AVAILABILITY_BACKFILL_ALLOW_MULTI_SKU", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
CHUNK_SIZE = REQUESTED_CHUNK_SIZE if ALLOW_MULTI_SKU_FULFILLMENT else 1
SINGLE_SKU_FALLBACK = os.getenv("BESTBUY_AVAILABILITY_BACKFILL_SINGLE_SKU_FALLBACK", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
REQUEST_TIMEOUT = int(os.getenv("BESTBUY_AVAILABILITY_BACKFILL_TIMEOUT", os.getenv("ZENROWS_TIMEOUT", "120")))
DRY_RUN = os.getenv("BESTBUY_AVAILABILITY_BACKFILL_DRY_RUN", "0").lower() in {"1", "true", "yes", "y"}
SKIP = os.getenv("BESTBUY_AVAILABILITY_BACKFILL_SKIP", "0").lower() in {"1", "true", "yes", "y"}
LIMIT = int(os.getenv("BESTBUY_AVAILABILITY_BACKFILL_LIMIT", "0"))
SELECTED_SKUS = {
    value.strip()
    for value in os.getenv("BESTBUY_AVAILABILITY_BACKFILL_SKUS", "").replace(";", ",").split(",")
    if value.strip()
}
CANDIDATE_MODE = os.getenv("BESTBUY_AVAILABILITY_BACKFILL_CANDIDATE_MODE", "missing_any").strip().lower()
OVERWRITE = os.getenv("BESTBUY_AVAILABILITY_BACKFILL_OVERWRITE", "0").lower() in {"1", "true", "yes", "y"}
CLEAR_EXISTING_FIELDS = os.getenv("BESTBUY_AVAILABILITY_BACKFILL_CLEAR_EXISTING_FIELDS", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
FETCH_MODE = os.getenv(
    "BESTBUY_AVAILABILITY_BACKFILL_FETCH_MODE",
    os.getenv("BESTBUY_FETCH_MODE", "zenrows"),
).strip().lower()
BROWSER_WAIT_SECONDS = max(
    0.0,
    float(os.getenv("BESTBUY_AVAILABILITY_BROWSER_GRAPHQL_WAIT_SECONDS", "5")),
)
BROWSER_JS_TIMEOUT = max(1, int(os.getenv("BESTBUY_AVAILABILITY_BROWSER_GRAPHQL_JS_TIMEOUT", "120")))
BROWSER_HEADLESS = env_bool("BESTBUY_AVAILABILITY_BROWSER_GRAPHQL_HEADLESS", "1")
BROWSER_LOCAL_PORT = env_int("BESTBUY_AVAILABILITY_BROWSER_GRAPHQL_LOCAL_PORT", "0")
BROWSER_PAGE = None
BROWSER_META = {}


def now():
    return datetime.now().isoformat(timespec="seconds")


def safe_part(value, default="na"):
    text = compact_text(value)
    if not text:
        return default
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)
    return cleaned[:80] or default


def read_csv(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def csv_fields(path, rows):
    path = Path(path)
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


def norm_key(value):
    return compact_text(value).lower()


def canonical_url(value):
    text = compact_text(value)
    if not text:
        return ""
    if "?" in text:
        text = text.split("?", 1)[0]
    if "/sku/" in text:
        text = text.split("/sku/", 1)[0]
    return text.rstrip("/").lower()


def item_from_product_url(value):
    text = compact_text(value)
    if not text or "/sku/" not in text:
        return ""
    before_sku = text.split("/sku/", 1)[0].rstrip("/")
    item = before_sku.rsplit("/", 1)[-1].strip()
    return item if item and item.lower() not in {"product", "site"} else ""


def ensure_item_from_url(row):
    if not compact_text(row.get("item")):
        item = item_from_product_url(row.get("product_url"))
        if item:
            row["item"] = item


def all_availability_blank(row):
    return all(not compact_text(row.get(field)) for field in ACTIVE_AVAILABILITY_FIELDS)


def any_availability_blank(row):
    return any(not compact_text(row.get(field)) for field in ACTIVE_AVAILABILITY_FIELDS)


def backfill_candidate(row, batch_id):
    if compact_text(row.get("batch_id")) != batch_id:
        return False
    if CANDIDATE_MODE in {"missing_any", "any_blank", "missing"}:
        return any_availability_blank(row)
    if CANDIDATE_MODE in {"all", "all_rows"}:
        return True
    return all_availability_blank(row)


def add_lookup(mapping, key, sku):
    key = norm_key(key)
    sku = compact_text(sku)
    if key and sku and key not in mapping:
        mapping[key] = sku


def row_page_type(row):
    value = norm_key(row.get("page_type") or row.get("target_source"))
    aliases = {
        "bsr_only_backfill": "bsr",
        "promotion_backfill": "promotion",
        "trending_backfill": "trend",
    }
    return aliases.get(value, value)


def row_specific_lookup_keys(row):
    page_type = row_page_type(row)
    url = canonical_url(row.get("product_url"))
    if not page_type or not url:
        return []
    keys = []
    for field in ("trend_rank", "bsr_rank", "main_rank", "promotion_position", "final_target_rank"):
        value = norm_key(row.get(field))
        if value:
            keys.append(f"{page_type}|{field}|{value}|{url}")
    return keys


def build_sku_lookup(targets):
    lookup = {}
    for target in targets:
        sku = target.get("sku_id") or target.get("sku")
        for key in row_specific_lookup_keys(target):
            add_lookup(lookup, key, sku)
        add_lookup(lookup, target.get("sku_id"), sku)
        add_lookup(lookup, target.get("sku"), sku)
        add_lookup(lookup, target.get("item"), sku)
        add_lookup(lookup, target.get("bsin"), sku)
        add_lookup(lookup, item_from_product_url(target.get("product_url")), sku)
        add_lookup(lookup, target.get("product_url"), sku)
        add_lookup(lookup, canonical_url(target.get("product_url")), sku)
    return lookup


def sku_for_row(row, lookup):
    for key in row_specific_lookup_keys(row):
        direct = norm_key(key)
        if direct and direct in lookup:
            return lookup[direct]
    for key in (
        row.get("sku_id"),
        row.get("sku"),
        row.get("item"),
        row.get("bsin"),
        item_from_product_url(row.get("product_url")),
        row.get("product_url"),
        canonical_url(row.get("product_url")),
    ):
        direct = norm_key(key)
        if direct and direct in lookup:
            return lookup[direct]
        if direct.isdigit():
            return direct
    return ""


def unique_ordered(values):
    seen = set()
    result = []
    for value in values:
        value = compact_text(value)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def filter_row_to_sku_for_selected_skus(row_to_sku, selected_skus):
    selected = set(selected_skus)
    if not selected:
        return {}
    return {index: sku for index, sku in row_to_sku.items() if sku in selected}


def fulfillment_headers():
    return {
        "accept": "application/json, text/plain, */*",
        "referer": "https://www.bestbuy.com/",
        "x-client-id": "pdp-web",
        "x-requested-for-operation-name": "AIV_FulfillmentBatchCall",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def browser_graphql_enabled():
    return FETCH_MODE in {"browser_graphql", "browser"}


def open_availability_browser_page():
    global BROWSER_PAGE, BROWSER_META
    if BROWSER_PAGE is not None:
        return BROWSER_PAGE
    BROWSER_PAGE, BROWSER_META = create_browser_page(
        run_root=BACKFILL_ROOT,
        name="availability_browser_graphql",
        headless=BROWSER_HEADLESS,
        local_port=BROWSER_LOCAL_PORT,
    )
    BROWSER_PAGE.get(add_intl_nosplash("https://www.bestbuy.com/"))
    if BROWSER_WAIT_SECONDS:
        time.sleep(BROWSER_WAIT_SECONDS)
    return BROWSER_PAGE


def close_availability_browser_page():
    global BROWSER_PAGE
    close_browser_page(BROWSER_PAGE)
    BROWSER_PAGE = None


def browser_fetch_fulfillment(target_url):
    if BROWSER_PAGE is None:
        raise RuntimeError("availability browser_graphql page is not initialized")
    js = (
        "return fetch("
        + json.dumps(target_url)
        + ", {method:'GET', credentials:'include', headers:{"
        "'accept':'application/json, text/plain, */*',"
        "'x-client-id':'pdp-web',"
        "'x-requested-for-operation-name':'AIV_FulfillmentBatchCall'"
        "}}).then(async r=>{const t=await r.text();"
        "return JSON.stringify({status:r.status, contentType:r.headers.get('content-type'), body:t});"
        "}).catch(e=>JSON.stringify({error:String(e)}));"
    )
    started = time.perf_counter()
    raw = BROWSER_PAGE.run_js(js, timeout=BROWSER_JS_TIMEOUT)
    elapsed = round(time.perf_counter() - started, 3)
    if raw is None:
        raise RuntimeError("availability browser fetch returned empty result")
    envelope = json.loads(raw)
    if envelope.get("error"):
        raise RuntimeError(envelope["error"])
    text = str(envelope.get("body") or "")
    response_json = {}
    try:
        response_json = json.loads(text)
    except ValueError:
        pass
    return {
        "status_code": int(envelope.get("status") or 0),
        "text": text,
        "headers": {
            "content-type": envelope.get("contentType", ""),
            "transport": "browser_graphql",
            "x-request-cost": "0",
        },
        "response_json": response_json,
        "elapsed_seconds": elapsed,
    }


def fetch_chunk(client, chunk, chunk_dir):
    chunk_dir.mkdir(parents=True, exist_ok=True)
    variables = fulfillment_variables(chunk, context="PLP")
    target_url = fulfillment_url(chunk, context="PLP")
    (chunk_dir / "request.json").write_text(
        json.dumps(
            {
                "endpoint": FULFILLMENT_ENDPOINT,
                "url": target_url,
                "sku_count": len(chunk),
                "skus": chunk,
                "variables": variables,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if browser_graphql_enabled():
        result = browser_fetch_fulfillment(target_url)
        status_code = result["status_code"]
        response_text = result["text"]
        response_headers = result["headers"]
        response_json = result["response_json"]
        elapsed = result["elapsed_seconds"]
    else:
        started = time.perf_counter()
        response = client.get(
            target_url,
            params=zenrows_params(),
            headers=fulfillment_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        elapsed = round(time.perf_counter() - started, 3)
        status_code = response.status_code
        response_text = response.text
        response_headers = dict(response.headers)
        response_json = {}
        try:
            response_json = response.json()
        except ValueError:
            pass
    (chunk_dir / "response.txt").write_text(response_text, encoding="utf-8", errors="replace")
    (chunk_dir / "headers.json").write_text(
        json.dumps(response_headers, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if response_json:
        (chunk_dir / "response.json").write_text(
            json.dumps(response_json, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    values = parse_fulfillment_response(response_json)
    errors = response_json.get("errors") if isinstance(response_json, dict) else None
    return {
        "status_code": status_code,
        "elapsed_seconds": elapsed,
        "x_request_cost": request_cost(response_headers),
        "values": values,
        "error": json.dumps(errors, ensure_ascii=False)[:500] if errors else "",
    }


def fetch_availability(skus, raw_dir):
    api_key = "" if browser_graphql_enabled() else os.getenv("ZENROWS_API_KEY")
    if not api_key and not browser_graphql_enabled():
        raise RuntimeError("Set ZENROWS_API_KEY in .env")
    client = ZenRowsClient(api_key) if api_key else None
    if browser_graphql_enabled():
        open_availability_browser_page()
        atexit.register(close_availability_browser_page)
    values_by_sku = {}
    calls = []
    chunks = [skus[index : index + CHUNK_SIZE] for index in range(0, len(skus), CHUNK_SIZE)]

    def record_call(call_index, chunk, chunk_dir, status, elapsed, cost, error, returned, started_at, fallback_of=""):
        calls.append(
            {
                "chunk": call_index,
                "sku_count": len(chunk),
                "returned_sku_count": len(returned),
                "status_code": status,
                "elapsed_seconds": elapsed,
                "x_request_cost": cost,
                "started_at": started_at,
                "finished_at": now(),
                "error": error,
                "fallback_of": fallback_of,
                "request_path": rel_path(chunk_dir / "request.json"),
                "response_path": rel_path(chunk_dir / "response.json"),
            }
        )

    def missing_availability_skus(chunk, returned):
        missing = []
        for sku in chunk:
            values = returned.get(sku) or {}
            if not any(values.get(field) for field in ACTIVE_AVAILABILITY_FIELDS):
                missing.append(sku)
        return missing

    for index, chunk in enumerate(chunks, 1):
        chunk_dir = raw_dir / f"chunk_{index:03d}"
        status = "ERR"
        cost = 0.0
        elapsed = 0.0
        error = ""
        returned = {}
        started_at = now()
        try:
            result = fetch_chunk(client, chunk, chunk_dir)
            status = result["status_code"]
            cost = result["x_request_cost"]
            elapsed = result["elapsed_seconds"]
            error = result["error"]
            returned = result["values"]
            for sku, values in returned.items():
                values_by_sku.setdefault(sku, {}).update(values)
        except RequestException as exc:
            error = str(exc)
        except Exception as exc:
            error = str(exc)
        record_call(index, chunk, chunk_dir, status, elapsed, cost, error, returned, started_at)
        value_count = sum(1 for values in returned.values() for field in ACTIVE_AVAILABILITY_FIELDS if values.get(field))
        print(
            f"[availability_backfill:chunk] {index}/{len(chunks)} skus={len(chunk)} "
            f"status={status} returned={len(returned)} values={value_count} cost={cost}",
            flush=True,
        )
        fallback_skus = missing_availability_skus(chunk, returned)
        if SINGLE_SKU_FALLBACK and len(chunk) > 1 and fallback_skus:
            print(
                f"[availability_backfill:fallback] chunk={index} skus={len(fallback_skus)}",
                flush=True,
            )
            for fallback_index, sku in enumerate(fallback_skus, 1):
                fallback_chunk = [sku]
                fallback_dir = chunk_dir / "fallback" / f"sku_{safe_part(sku)}"
                fallback_status = "ERR"
                fallback_cost = 0.0
                fallback_elapsed = 0.0
                fallback_error = ""
                fallback_returned = {}
                fallback_started_at = now()
                try:
                    result = fetch_chunk(client, fallback_chunk, fallback_dir)
                    fallback_status = result["status_code"]
                    fallback_cost = result["x_request_cost"]
                    fallback_elapsed = result["elapsed_seconds"]
                    fallback_error = result["error"]
                    fallback_returned = result["values"]
                    for returned_sku, values in fallback_returned.items():
                        values_by_sku.setdefault(returned_sku, {}).update(values)
                except RequestException as exc:
                    fallback_error = str(exc)
                except Exception as exc:
                    fallback_error = str(exc)
                call_index = f"{index}.{fallback_index}"
                record_call(
                    call_index,
                    fallback_chunk,
                    fallback_dir,
                    fallback_status,
                    fallback_elapsed,
                    fallback_cost,
                    fallback_error,
                    fallback_returned,
                    fallback_started_at,
                    fallback_of=str(index),
                )
                fallback_value_count = sum(
                    1
                    for values in fallback_returned.values()
                    for field in ACTIVE_AVAILABILITY_FIELDS
                    if values.get(field)
                )
                print(
                    f"[availability_backfill:fallback_chunk] {call_index} sku={sku} "
                    f"status={fallback_status} returned={len(fallback_returned)} "
                    f"values={fallback_value_count} cost={fallback_cost}",
                    flush=True,
                )
    return values_by_sku, calls


def apply_values(rows, row_to_sku, values_by_sku):
    updated = 0
    changed_fields = 0
    for index, row in enumerate(rows):
        sku = row_to_sku.get(index)
        if not sku:
            continue
        values = values_by_sku.get(sku) or {}
        row_changed = False
        for field in AVAILABILITY_FIELDS:
            if field in INACTIVE_AVAILABILITY_FIELDS:
                if row.get(field, "") != "":
                    row[field] = ""
                    row_changed = True
                    changed_fields += 1
                continue
            value = values.get(field)
            if CLEAR_EXISTING_FIELDS:
                desired = value or ""
                if row.get(field, "") != desired:
                    row[field] = desired
                    row_changed = True
                    changed_fields += 1
                continue
            if value and (OVERWRITE or not compact_text(row.get(field))):
                row[field] = value
                row_changed = True
                changed_fields += 1
        if row_changed:
            updated += 1
    return updated, changed_fields


def main():
    started_at = now()
    if SKIP:
        run_dir = BACKFILL_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_type": "step08_availability_backfill",
            "started_at": started_at,
            "finished_at": now(),
            "skipped": True,
            "reason": "BESTBUY_AVAILABILITY_BACKFILL_SKIP=1",
            "batch_id": BACKFILL_BATCH_ID,
            "call_count": 0,
            "x_request_cost": 0,
            "estimated_krw_1550": 0,
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(
            f"[availability_backfill:skip] batch={BACKFILL_BATCH_ID} reason=BESTBUY_AVAILABILITY_BACKFILL_SKIP=1 "
            f"calls=0 raw={rel_path(run_dir)}",
            flush=True,
        )
        return
    final_rows = read_csv(FINAL_OUTPUT_CSV)
    detail_rows = read_csv(DETAIL_ROWS_CSV)
    product_list_rows = read_csv(PRODUCT_LIST_CSV)
    targets = read_csv(TARGET_CSV)
    if not final_rows:
        raise RuntimeError(f"final_output.csv not found or empty: {FINAL_OUTPUT_CSV}")
    if not targets:
        raise RuntimeError(f"target CSV not found or empty: {TARGET_CSV}")
    for row in final_rows:
        ensure_item_from_url(row)
    for row in detail_rows:
        ensure_item_from_url(row)

    lookup = build_sku_lookup(targets)
    batch_indexes = [index for index, row in enumerate(final_rows) if compact_text(row.get("batch_id")) == BACKFILL_BATCH_ID]
    candidate_indexes = [index for index in batch_indexes if backfill_candidate(final_rows[index], BACKFILL_BATCH_ID)]
    row_to_sku = {}
    missing_sku = []
    for index in candidate_indexes:
        sku = sku_for_row(final_rows[index], lookup)
        if sku:
            row_to_sku[index] = sku
        else:
            missing_sku.append(index)
    skus = unique_ordered(row_to_sku.values())
    if SELECTED_SKUS:
        skus = [sku for sku in skus if sku in SELECTED_SKUS]
    if LIMIT > 0:
        skus = skus[:LIMIT]
    row_to_sku = filter_row_to_sku_for_selected_skus(row_to_sku, skus)
    run_dir = BACKFILL_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_dir = run_dir / "raw"
    run_dir.mkdir(parents=True, exist_ok=True)

    estimated_calls = (len(skus) + CHUNK_SIZE - 1) // CHUNK_SIZE if skus else 0
    print(
        f"[availability_backfill:plan] batch={BACKFILL_BATCH_ID} final_rows={len(final_rows)} "
        f"batch_rows={len(batch_indexes)} blank_rows={len(candidate_indexes)} mapped_rows={len(row_to_sku)} skus={len(skus)} "
        f"chunk_size={CHUNK_SIZE} requested_chunk_size={REQUESTED_CHUNK_SIZE} "
        f"multi_sku={str(ALLOW_MULTI_SKU_FULFILLMENT).lower()} candidate_mode={CANDIDATE_MODE} "
        f"overwrite={str(OVERWRITE).lower()} clear_existing={str(CLEAR_EXISTING_FIELDS).lower()} "
        f"active_fields={','.join(ACTIVE_AVAILABILITY_FIELDS)} "
        f"limit={LIMIT} calls={estimated_calls} dry_run={str(DRY_RUN).lower()}",
        flush=True,
    )
    if missing_sku:
        print(f"[availability_backfill:missing_sku] rows={len(missing_sku)}", flush=True)

    values_by_sku = {}
    calls = []
    if skus and not DRY_RUN:
        values_by_sku, calls = fetch_availability(skus, raw_dir)

    detail_row_to_sku = {}
    for index, row in enumerate(detail_rows):
        if not backfill_candidate(row, BACKFILL_BATCH_ID):
            continue
        sku = sku_for_row(row, lookup)
        if sku:
            detail_row_to_sku[index] = sku
    detail_row_to_sku = filter_row_to_sku_for_selected_skus(detail_row_to_sku, skus)

    product_list_row_to_sku = {}
    for index, row in enumerate(product_list_rows):
        if not backfill_candidate(row, BACKFILL_BATCH_ID):
            continue
        sku = sku_for_row(row, lookup)
        if sku:
            product_list_row_to_sku[index] = sku
    product_list_row_to_sku = filter_row_to_sku_for_selected_skus(product_list_row_to_sku, skus)

    final_updated, final_changed_fields = apply_values(final_rows, row_to_sku, values_by_sku)
    detail_updated, detail_changed_fields = apply_values(detail_rows, detail_row_to_sku, values_by_sku)
    product_list_updated, product_list_changed_fields = apply_values(
        product_list_rows,
        product_list_row_to_sku,
        values_by_sku,
    )

    if not DRY_RUN:
        write_csv(FINAL_OUTPUT_CSV, final_rows, csv_fields(FINAL_OUTPUT_CSV, final_rows))
        if detail_rows:
            write_csv(DETAIL_ROWS_CSV, detail_rows, csv_fields(DETAIL_ROWS_CSV, detail_rows))
        if product_list_rows:
            write_csv(PRODUCT_LIST_CSV, product_list_rows, csv_fields(PRODUCT_LIST_CSV, product_list_rows))

    call_cost = round(sum(float(call.get("x_request_cost") or 0) for call in calls), 7)
    manifest = {
        "run_type": "step08_availability_backfill",
        "started_at": started_at,
        "finished_at": now(),
        "batch_id": BACKFILL_BATCH_ID,
        "dry_run": DRY_RUN,
        "target_csv": rel_path(TARGET_CSV),
        "final_output_csv": rel_path(FINAL_OUTPUT_CSV),
        "detail_rows_csv": rel_path(DETAIL_ROWS_CSV),
        "product_list_csv": rel_path(PRODUCT_LIST_CSV),
        "candidate_mode": CANDIDATE_MODE,
        "overwrite": OVERWRITE,
        "clear_existing_fields": CLEAR_EXISTING_FIELDS,
        "active_availability_fields": ACTIVE_AVAILABILITY_FIELDS,
        "inactive_availability_fields": INACTIVE_AVAILABILITY_FIELDS,
        "batch_final_rows": len(batch_indexes),
        "blank_final_rows": len(candidate_indexes),
        "mapped_final_rows": len(row_to_sku),
        "missing_sku_rows": len(missing_sku),
        "sku_count": len(skus),
        "limit": LIMIT,
        "selected_skus": sorted(SELECTED_SKUS),
        "chunk_size": CHUNK_SIZE,
        "requested_chunk_size": REQUESTED_CHUNK_SIZE,
        "multi_sku_fulfillment_enabled": ALLOW_MULTI_SKU_FULFILLMENT,
        "single_sku_fallback_enabled": SINGLE_SKU_FALLBACK,
        "fallback_call_count": sum(1 for call in calls if call.get("fallback_of")),
        "call_count": len(calls) if calls else estimated_calls,
        "returned_sku_count": len(values_by_sku),
        "final_rows_updated": final_updated,
        "final_fields_updated": final_changed_fields,
        "detail_rows_updated": detail_updated,
        "detail_fields_updated": detail_changed_fields,
        "product_list_rows_updated": product_list_updated,
        "product_list_fields_updated": product_list_changed_fields,
        "x_request_cost": call_cost,
        "estimated_krw_1550": round(call_cost * KRW_PER_USD, 2),
        "calls": calls,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"[availability_backfill:output] final_updated={final_updated} detail_updated={detail_updated} "
        f"product_list_updated={product_list_updated} "
        f"returned_skus={len(values_by_sku)} cost_usd={call_cost} raw={rel_path(run_dir)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
