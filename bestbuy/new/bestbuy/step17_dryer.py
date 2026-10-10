"""DRYER API-only run: shared BBY transport -> exact 20-column test table."""
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .step00_collection_recovery import MAX_ITEM_ATTEMPTS
from .step00_dryer import FIELDS, TEST_TABLE, make_listing_row, make_row, category_targets, primary_url, public_product
from .step00_dryer_log import error_diagnostic, event, graphql_diagnostic, graphql_response_diagnostic, phase, run_logging, safe_legacy_output, trace_browser_calls

COLLECTOR_VERSION = 5
CATEGORY_ID = "abcat0910004"
LISTING_SORT = "Best-Selling"
LISTING_PAGE_SIZE = 18
LISTING_IDENTITY = {"listing_mode": "category", "category_id": CATEGORY_ID,
                    "listing_sort": LISTING_SORT, "listing_page_size": LISTING_PAGE_SIZE}
LIST_QUERY = """query DryerCategoryList($input:SearchInput!$pagination:SearchPagination!$sort:SearchSort
$productPriceInput:ProductItemPriceInput!){search(input:$input,pagination:$pagination,sort:$sort){
numFound documents{__typename ... on SearchProduct{product{skuId bsin name{short}
manufacturer{modelNumber}url{pdp}reviewInfo{averageRating reviewCount}
price(input:$productPriceInput){customerPrice regularPrice totalSavings}}}}}}"""
QUERY = """query DryerDetail($skuId:String!$productPriceInput:ProductItemPriceInput!){productBySkuId(skuId:$skuId){
skuId bsin name{short}description{short long}features{description title}manufacturer{modelNumber}url{pdp}
reviewInfo{averageRating reviewCount}specificationGroups{specifications{displayName value}}
price(input:$productPriceInput){customerPrice regularPrice totalSavings}}}"""


class DryerError(RuntimeError):
    pass


def safe_reason(exc):
    if isinstance(exc, DryerError):
        reason = str(exc)
        return reason if re.fullmatch(r"[a-zA-Z0-9_]+", reason) else "dryer_error"
    if isinstance(exc, ValueError) and str(exc) == "product_identity_mismatch":
        return str(exc)
    return type(exc).__name__


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load_runtime(run_dir):
    # Configuration is loaded only by the existing approved loader.
    os.environ.update(BESTBUY_CATEGORY="DRYER", BESTBUY_URL_SOURCE="default",
        BESTBUY_RUN_ROOT=str(run_dir), BESTBUY_SEARCH_TERM="DRYER", BESTBUY_SEARCH_URL="",
        BESTBUY_OUTPUT_ROOT=str(run_dir / "output"), BESTBUY_DETAIL_RUN_ROOT=str(run_dir / "detail"),
        BESTBUY_FINAL_OUTPUT_CSV=str(run_dir / "output" / "final_output.csv"),
        BESTBUY_DETAIL_TARGET_CSV=str(run_dir / "output" / "bestbuy_final_targets.csv"),
        BESTBUY_LISTING_COLLECTION_MODE="browser_graphql", BESTBUY_DETAIL_FETCH_MODE="browser_graphql",
        BESTBUY_DETAIL_DIRECT_GRAPHQL="1", BESTBUY_DETAIL_PDP_FALLBACK="0",
        BESTBUY_BROWSER_GRAPHQL_HEADLESS="0", BESTBUY_DETAIL_BROWSER_GRAPHQL_HEADLESS="0",
        BESTBUY_BROWSER_GRAPHQL_LOCAL_PORT="0", BESTBUY_DETAIL_BROWSER_GRAPHQL_LOCAL_PORT="0")
    from . import step00_config as config
    from . import step01_main_list as listing
    from . import step08_detail_enrichment as detail
    # This entry point runs in its own Python process; existing LDY processes are unaffected.
    listing.CATEGORY = "DRYER"
    listing.SEARCH_TERM = "DRYER"
    listing.SEARCH_URL_TEMPLATE = ""
    listing.INCLUDE_SPONSORED_CAROUSEL = False
    listing.BROWSER_GRAPHQL_HEADLESS = False
    detail.BROWSER_GRAPHQL_HEADLESS = False
    return config, listing, detail


def api_post(helpers, payload):
    # Share LDY's connection, session bootstrap and transport recovery directly.
    # Discard legacy raw diagnostics; emit safe status codes from this runner only.
    count = len(payload) if isinstance(payload, list) else 1
    event("api_request", operations=count, status="start")
    started = time.perf_counter()
    try:
        with phase("browser_api", operations=count), safe_legacy_output(), trace_browser_calls(helpers):
            status, _, body, _, _ = helpers.browser_graphql_post(payload, "https://www.bestbuy.com/?intl=nosplash")
    except Exception as exc:
        event("api_failed", operations=count, duration_s=round(time.perf_counter() - started, 2), **error_diagnostic(exc))
        raise DryerError("browser_api_unavailable") from exc
    http_status = int(status or 0)
    event("api_response", http_status=http_status, operations=count, duration_s=round(time.perf_counter() - started, 2))
    if http_status != 200:
        event("api_response_rejected", http_status=http_status, operations=count,
              **graphql_response_diagnostic(body))
    return http_status, body


def progress(stage, completed, total=0, **fields):
    event("progress", stage=stage, completed=completed, total=total or None,
          progress_pct=round(min(completed, total) * 100 / total, 1) if total else None, **fields)


def listing_target(row, helpers):
    """Save only own listing fields needed by the 20-column output and ranks."""
    target = {key: row.get(key, "") for key in (
        "sku_id", "bsin", "item", "product_name", "retailer_sku_name", "product_url",
        "container_type", "is_sponsored", "page", "visual_rank", "global_visual_rank",
        "organic_rank", "global_organic_rank", "customer_price", "regular_price", "total_savings",
        "final_sku_price", "original_sku_price", "savings", "rating", "review_count")}
    target["product_url"] = primary_url(target["product_url"], target["sku_id"])
    # The shared parser's raw_product_json contains this listing product only.
    # Read its model internally without retaining unrelated product fields.
    try:
        product = json.loads(row.get("raw_product_json") or "{}")
    except (TypeError, ValueError):
        product = {}
    target["model_number"] = helpers.product_model_number([product]) if (
        isinstance(product, dict) and str(product.get("skuId") or "") == str(target["sku_id"])) else ""
    return target


def category_payload(runtime, page):
    config, _, helpers = runtime
    variables = {"input": {"site": "WWW", "queryType": "BROWSE", "query": "categoryid$" + CATEGORY_ID},
        "pagination": {"pageNumber": page, "offset": LISTING_PAGE_SIZE}, "sort": {"sort": LISTING_SORT},
        "productPriceInput": helpers.fulfillment_product_price_input()}
    config.apply_bestbuy_location(variables)
    return {"operationName": "DryerCategoryList", "variables": variables, "query": LIST_QUERY}


def parse_category_page(runtime, page, body):
    """Only category search.documents are organic; recommendations/ads are ignored."""
    if not isinstance(body, dict):
        raise DryerError("invalid_response")
    if body.get("errors"):
        event("listing_response_rejected", **graphql_response_diagnostic(body))
        raise DryerError("graphql_listing_error")
    data = body.get("data")
    search = data.get("search") if isinstance(data, dict) else None
    documents = search.get("documents") if isinstance(search, dict) else None
    if not isinstance(documents, list):
        raise DryerError("missing_documents")
    _, listing, helpers = runtime
    rows, skipped = [], 0
    for position, document in enumerate(documents, 1):
        if not isinstance(document, dict):
            raise DryerError("missing_document")
        product = document.get("product")
        if document.get("__typename") not in (None, "SearchProduct"):
            skipped += 1
            continue
        if not isinstance(product, dict) or not str(product.get("skuId") or "").isdigit():
            raise DryerError("missing_product_identity")
        missing = [key for key in ("bsin", "name", "manufacturer", "url", "reviewInfo", "price") if key not in product]
        if missing:
            event("listing_response_rejected", sku_id=str(product["skuId"]), missing_fields=missing)
            raise DryerError("listing_product_fields_incomplete")
        global_rank = (page - 1) * LISTING_PAGE_SIZE + position
        occurrence = {"page": page, "visual_rank": position, "organic_rank": position,
            "container_type": "organic_product", "is_sponsored": False,
            "placement": "search.documents", "source_event_id": "dryer_category_api"}
        row = listing.parse_product_occurrence(product, occurrence,
            {"global_visual_rank": global_rank, "global_organic_rank": global_rank})
        rows.append(listing_target(row, helpers))
    return rows, not documents, skipped, search.get("numFound")


def collect_listing(runtime, run_dir, max_pages, limit):
    _, listing, helpers = runtime
    from .step00_collection_recovery import MAX_LISTING_PASSES
    cache = run_dir / "category_listing.json"
    selection = {"collector_version": COLLECTOR_VERSION, "limit": limit, **LISTING_IDENTITY}
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if (all(saved.get(key) == value for key, value in selection.items())
                and (saved.get("complete") or saved.get("limit_reached"))):
            event("listing_cache_reused", listing="category", pages=saved.get("pages", 0), cached_rows=len(saved["rows"]))
            return saved["rows"]
    # Discard failed passes and restart page one; never mix ranking snapshots.
    for pass_number in range(1, MAX_LISTING_PASSES + 1):
        rows, seen_skus, skipped_total = [], set(), 0
        event("listing_pass_start", listing="category", attempt=pass_number, max_attempts=MAX_LISTING_PASSES,
              category_id=CATEGORY_ID, sort=LISTING_SORT, target_limit=limit or None)
        for page in range(1, max_pages + 1):
            event("listing_page_start", listing="category", page=page, attempt=pass_number)
            status, body = api_post(helpers, category_payload(runtime, page))
            if status in {400, 401, 402, 403, 404, 429}:
                raise DryerError(f"http_{status}")
            if isinstance(body, list) and len(body) == 1:
                body = body[0]
            parsed, empty, skipped, num_found = [], False, 0, None
            reason = f"http_{status}" if status != 200 else ""
            if not reason:
                try:
                    parsed, empty, skipped, num_found = parse_category_page(runtime, page, body)
                except DryerError as exc:
                    reason = safe_reason(exc)
            page_skus = [str(row["sku_id"]) for row in parsed]
            if not reason and page_skus and all(sku in seen_skus for sku in page_skus):
                reason = "repeated_organic_page"
            if not reason and page == 1 and empty:
                reason = "empty_first_page"
            if reason:
                event("listing_page_failed", listing="category", page=page, attempt=pass_number,
                      http_status=status, reason=reason, parsed_rows=len(parsed))
                write_json(cache, {**selection, "rows": [], "pages": page, "complete": False,
                    "limit_reached": False, "pass_number": pass_number, "failure_reason": reason})
                if pass_number == MAX_LISTING_PASSES:
                    raise DryerError(f"category_{reason}_page_{page}")
                delay = listing.listing_retry_delay(pass_number)
                event("listing_retry", listing="category", next_attempt=pass_number + 1, failed_page=page,
                      reason=reason, sleep_s=delay, restart_from_page=1)
                with phase("listing_retry_wait", listing="category"):
                    time.sleep(delay)
                break
            seen_skus.update(page_skus)
            rows.extend(parsed)
            skipped_total += skipped
            ranked_count = len(category_targets(rows))
            reached = limit > 0 and ranked_count >= limit
            write_json(cache, {**selection, "rows": rows, "pages": page, "complete": empty,
                "limit_reached": reached, "pass_number": pass_number, "num_found": num_found,
                "unique_product_count": ranked_count, "duplicate_count": len(rows) - ranked_count,
                "skipped_document_count": skipped_total})
            progress("category_list", min(ranked_count, limit) if limit else ranked_count, limit,
                page=page, parsed_rows=len(parsed), candidate_count=ranked_count,
                duplicate_count=len(rows) - ranked_count, skipped_documents=skipped,
                num_found=num_found, complete=empty)
            if reached or empty:
                if empty and limit and ranked_count < limit:
                    event("listing_exhausted", available_count=ranked_count, requested_count=limit)
                return rows
            if listing.LISTING_PAGE_SLEEP_SECONDS:
                with phase("listing_page_wait", listing="category", sleep_s=listing.LISTING_PAGE_SLEEP_SECONDS):
                    time.sleep(listing.LISTING_PAGE_SLEEP_SECONDS)
        else:
            raise DryerError("category_max_pages_reached_collection_incomplete")


def missing_attribute_fields(product):
    """Apply the same short/long/Features contract to API and saved products."""
    missing = [key for key in ("features", "description", "specificationGroups") if key not in product]
    description = product.get("description")
    if description is not None:
        missing.extend("description." + key for key in ("short", "long")
                       if not isinstance(description, dict) or key not in description)
    return missing


def collect_product_batch(runtime, targets):
    config, _, helpers = runtime
    payloads = []
    for target in targets:
        variables = {"skuId": str(target["sku_id"]), "productPriceInput": helpers.fulfillment_product_price_input()}
        config.apply_bestbuy_location(variables)
        payloads.append({"operationName": "DryerDetail", "variables": variables, "query": QUERY})
    status, body = api_post(helpers, payloads)
    if status != 200:
        raise DryerError(f"http_{status}")
    bodies = body if isinstance(body, list) else [body]
    if len(bodies) != len(targets):
        raise DryerError("detail_batch_response_count_mismatch")
    captures, errors = {}, {}
    for target, response in zip(targets, bodies):
        sku_id = str(target["sku_id"])
        if not isinstance(response, dict) or response.get("errors"):
            errors[sku_id] = "detail_graphql_not_verified"
            graphql_errors = response.get("errors") if isinstance(response, dict) else None
            event("detail_response_rejected", sku_id=sku_id, reason=errors[sku_id],
                  **graphql_diagnostic(graphql_errors))
            continue
        data = response.get("data")
        product = data.get("productBySkuId") if isinstance(data, dict) else None
        if not isinstance(product, dict) or str(product.get("skuId") or "") != sku_id:
            errors[sku_id] = "detail_product_identity_mismatch"
            event("detail_response_rejected", sku_id=sku_id, reason=errors[sku_id], identity_field="skuId")
            continue
        expected_item = str(target.get("bsin") or "").strip().lower()
        if expected_item and str(product.get("bsin") or "").strip().lower() != expected_item:
            errors[sku_id] = "detail_product_identity_mismatch"
            event("detail_response_rejected", sku_id=sku_id, reason=errors[sku_id], identity_field="bsin")
            continue
        missing = missing_attribute_fields(product)
        if missing:
            errors[sku_id] = "detail_attribute_response_incomplete"
            event("detail_response_rejected", sku_id=sku_id, reason=errors[sku_id], missing_fields=missing)
            continue
        captures[sku_id] = {"collector_version": COLLECTOR_VERSION, "product": public_product(product),
            "captured_at": datetime.now().isoformat(timespec="seconds"), "transport": "browser_graphql"}
    return captures, errors


def collect_details(runtime, run_dir, targets, batch_id, batch_size):
    _, _, helpers = runtime
    successes, failures = {}, {}
    pending = []

    def accept(target, capture):
        sku_id = str(target["sku_id"])
        if capture.get("collector_version") != COLLECTOR_VERSION:
            raise DryerError("capture_version_mismatch")
        if missing_attribute_fields(capture["product"]):
            raise DryerError("detail_attribute_response_incomplete")
        timestamp = datetime.fromisoformat(capture["captured_at"])
        row, evidence = make_row(target, capture["product"], helpers, batch_id, timestamp)
        write_json(run_dir / "products" / f"{sku_id}.json", capture)
        successes[sku_id] = (row, evidence)
        failures.pop(sku_id, None)
        progress("detail", len(successes), len(targets), sku_id=sku_id,
                 failure_count=len(failures), pending_count=len(targets) - len(successes), status="success")
        for field, source in (("loading_type", evidence.get("source")), ("capacity", evidence.get("capacity_source"))):
            if row[field] in ("", None):
                event("attribute_missing", sku_id=sku_id, field=field, reason=source or "not_stated")

    for target in targets:
        cache = run_dir / "products" / f"{target['sku_id']}.json"
        if cache.exists():
            try:
                accept(target, json.loads(cache.read_text(encoding="utf-8")))
                event("detail_cache_reused", sku_id=str(target["sku_id"]))
                continue
            except Exception as exc:
                event("detail_cache_rejected", sku_id=str(target["sku_id"]), reason=safe_reason(exc), **error_diagnostic(exc))
        pending.append(target)
    stopped = False
    for start in range(0, len(pending), batch_size):
        remaining = pending[start:start + batch_size]
        for attempt in range(1, MAX_ITEM_ATTEMPTS + 1):
            batch_error = ""
            diagnostics = {}
            event("detail_batch_start", batch=start // batch_size + 1, attempt=attempt,
                  max_attempts=MAX_ITEM_ATTEMPTS, sku_ids=[str(t["sku_id"]) for t in remaining],
                  collected_count=len(successes), target_count=len(targets))
            try:
                with phase("detail_request", batch=start // batch_size + 1, attempt=attempt, operations=len(remaining)):
                    captures, errors = collect_product_batch(runtime, remaining)
            except Exception as exc:
                captures = {}
                batch_error = safe_reason(exc)
                diagnostics = error_diagnostic(exc)
                errors = {str(t["sku_id"]): batch_error for t in remaining}
            retry = []
            for target in remaining:
                sku_id = str(target["sku_id"])
                sku_diagnostics = dict(diagnostics)
                if sku_id in captures:
                    try:
                        accept(target, captures[sku_id])
                        continue
                    except Exception as exc:
                        errors[sku_id] = safe_reason(exc)
                        sku_diagnostics = error_diagnostic(exc)
                failures[sku_id] = {"sku_id": sku_id, "stage": "detail", "reason": errors.get(sku_id, "detail_missing_response"),
                    "attempt": attempt, "max_attempts": MAX_ITEM_ATTEMPTS, "retry_exhausted": False,
                    "failed_at": datetime.now().isoformat(timespec="seconds"), **sku_diagnostics}
                event("detail_failed", **failures[sku_id])
                retry.append(target)
            remaining = retry
            if not remaining:
                break
            if batch_error in {"browser_api_unavailable", "http_400", "http_401", "http_402", "http_403", "http_404", "http_429"}:
                stopped = True
                event("detail_stopped", reason=batch_error, collected_count=len(successes), failure_count=len(failures),
                      unattempted_count=len(targets) - len(successes) - len(failures))
                break
            if attempt < MAX_ITEM_ATTEMPTS:
                delay = helpers.detail_retry_sleep_seconds(attempt)
                event("detail_retry", next_attempt=attempt + 1, sku_ids=[str(t["sku_id"]) for t in remaining], sleep_s=delay)
                with phase("detail_retry_wait", sleep_s=delay):
                    time.sleep(delay)
        if stopped:
            break
        for target in remaining:
            failures[str(target["sku_id"])]["retry_exhausted"] = True

    ordered, fallback_count = [], 0
    for target in targets:
        sku_id = str(target["sku_id"])
        if sku_id in successes:
            ordered.append(successes[sku_id])
        elif failures.get(sku_id, {}).get("retry_exhausted"):
            failure = failures[sku_id]
            row, evidence = make_listing_row(target, helpers, batch_id,
                datetime.fromisoformat(failure["failed_at"]), failure["reason"])
            ordered.append((row, evidence))
            fallback_count += 1
            event("detail_listing_row_retained", sku_id=sku_id, reason=failure["reason"],
                  attempt=failure["attempt"], null_fields=[field for field in FIELDS if field != "id" and row[field] in (None, "")])
            progress("detail", len(successes) + fallback_count, len(targets), sku_id=sku_id,
                     collected_count=len(successes), failure_count=len(failures), fallback_count=fallback_count,
                     pending_count=len(targets) - len(successes) - fallback_count, status="warning")
    return [row for row, _ in ordered], [e for _, e in ordered], list(failures.values())


def connect_db(config, readonly=False):
    settings = config.db_config()
    if not settings:
        raise DryerError("db_config_not_configured")
    import psycopg2
    connection = psycopg2.connect(host=settings.get("host"), port=int(settings.get("port") or 5432),
        user=settings.get("user"), password=settings.get("password"),
        dbname=settings.get("database"), connect_timeout=10)
    connection.set_session(readonly=readonly)
    return connection


def inspect_table(cursor):
    cursor.execute("""SELECT column_name, data_type, column_default IS NOT NULL, is_identity
        FROM information_schema.columns WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position""", (TEST_TABLE,))
    return cursor.fetchall()


def validate_schema(columns):
    if {entry[0] for entry in columns} != set(FIELDS):
        raise DryerError("test_table_must_have_exactly_20_requested_columns")
    identifier = next(entry for entry in columns if entry[0] == "id")
    if not (identifier[2] or identifier[3] == "YES"):
        raise DryerError("test_table_id_must_be_generated")


def dryer_table_columns():
    # Reuse LDY definitions for the requested subset; only three names differ.
    from .step13_db_prepare import LDY_COLUMNS
    columns = dict(LDY_COLUMNS)
    source_names = {"crawl_datetime": "crawl_strdatetime",
                    "loading_type": "ldy_loading_type", "capacity": "ldy_capacity"}
    return [(field, columns[source_names.get(field, field)]) for field in FIELDS]


def db_value(value, data_type, column_name=""):
    # Keep shared text, integer, blank and rating formatting identical to BBY.
    from .step14_db_load import normalize_value
    value = normalize_value(value, data_type, column_name)
    if value in ("", None):
        return None
    if data_type in {"numeric", "decimal", "real", "double precision"}:
        if value == "Not yet reviewed":
            return None
        return Decimal(str(value).replace("$", "").replace(",", ""))
    return value


def load_test_table(config, rows, batch_id):
    if not rows or any(row.get("batch_id") != batch_id or row.get("account_name") != "Bestbuy" for row in rows):
        raise DryerError("invalid_test_batch")
    # Missing optional identifiers are NULL, not a shared product identity.
    keys = [str(row["item"]).casefold() for row in rows if row.get("item")]
    if len(keys) != len(set(keys)):
        raise DryerError("duplicate_product_identity_in_batch")
    connection = connect_db(config)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '60s'")
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (TEST_TABLE + ":" + batch_id,))
                columns = inspect_table(cursor)
                if not columns:
                    definitions = []
                    for field, definition in dryer_table_columns():
                        definitions.append('"' + field + '" ' + definition)
                    cursor.execute('CREATE TABLE public.ldy_dryer_retail_test (' + ", ".join(definitions) + ')')
                    columns = inspect_table(cursor)
                validate_schema(columns)
                types = {entry[0]: entry[1] for entry in columns}
                fields = FIELDS[1:]
                values = [tuple(db_value(row[field], types[field], field) for field in fields) for row in rows]
                cursor.execute("DELETE FROM public.ldy_dryer_retail_test WHERE batch_id=%s AND account_name=%s", (batch_id, "Bestbuy"))
                sql = 'INSERT INTO public.ldy_dryer_retail_test (' + ",".join('"' + f + '"' for f in fields) + ') VALUES (' + ",".join(["%s"] * len(fields)) + ')'
                cursor.executemany(sql, values)
                cursor.execute("SELECT count(*) FROM public.ldy_dryer_retail_test WHERE batch_id=%s AND account_name=%s", (batch_id, "Bestbuy"))
                if cursor.fetchone()[0] != len(rows):
                    raise DryerError("inserted_test_batch_count_mismatch")
    finally:
        connection.close()
    return len(rows)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="DRYER API only; public.ldy_dryer_retail_test only")
    parser.add_argument("--main-limit", type=int, default=300, help="unique category products in Best-Selling order; 0=all")
    parser.add_argument("--bsr-limit", type=int, default=100, help="same ranks on the first N main products; 0=all main products")
    parser.add_argument("--detail-batch-size", type=int, default=5, help="SKUs per browser API batch")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--resume", type=Path, help="reuse captures and batch id from the same API run")
    parser.add_argument("--no-load", action="store_true", help="collect public CSV without writing DB")
    args = parser.parse_args(argv)
    if min(args.main_limit, args.bsr_limit) < 0 or args.max_pages < 1 or not 1 <= args.detail_batch_size <= 20:
        parser.error("limits must be >= 0, max-pages >= 1, and detail-batch-size between 1 and 20")
    return args


def main(argv=None):
    args = parse_args(argv)
    run_dir = args.resume or Path(__file__).resolve().parent / "data" / "dryer" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if args.resume and not (run_dir / "dryer_manifest.json").is_file():
        print("status=failed reason=resume_manifest_not_found", flush=True)
        return 1
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        with run_logging(run_dir) as logger:
            return run(args, run_dir, logger)
    except OSError:
        print("status=failed reason=run_log_not_writable", flush=True)
        return 1


def run(args, run_dir, logger):
    manifest_path = run_dir / "dryer_manifest.json"
    manifest = {"collector_version": COLLECTOR_VERSION, "category": "DRYER", "table": "public." + TEST_TABLE,
        **LISTING_IDENTITY, "main_limit": args.main_limit, "bsr_limit": args.bsr_limit,
        "transport": "browser_graphql", "detail_batch_size": args.detail_batch_size,
        "status": "started", "db_loaded": False}
    has_previous = manifest_path.is_file()
    state_writable = not has_previous
    runtime = None
    event("run_start", run_dir=str(run_dir), main_limit=args.main_limit, bsr_limit=args.bsr_limit,
          detail_batch_size=args.detail_batch_size, no_load=args.no_load, resume=bool(args.resume), **LISTING_IDENTITY)
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8")) if has_previous else {}
        if previous and previous.get("collector_version") != COLLECTOR_VERSION:
            raise DryerError("resume_requires_current_api_run")
        if previous and any(previous.get(key) != value for key, value in LISTING_IDENTITY.items()):
            raise DryerError("resume_must_use_the_same_category_selection")
        if previous and any(previous.get(key) != manifest[key] for key in ("main_limit", "bsr_limit")):
            raise DryerError("resume_must_use_the_same_limits")
        batch_id = previous.get("batch_id") or "b_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        manifest["batch_id"] = batch_id
        os.environ["BESTBUY_BATCH_ID"] = batch_id
        with phase("runtime_initialize"), safe_legacy_output():
            runtime = load_runtime(run_dir)
        config, _, helpers = runtime
        event("runtime_ready", category="DRYER", transport="browser_graphql",
              browser_headless=bool(getattr(helpers, "BROWSER_GRAPHQL_HEADLESS", False)),
              connection_entry="home_origin", batch_id=batch_id)
        location = {"zip_code": config.bestbuy_zip_code(), "store_id": config.bestbuy_store_id()}
        if any(key in previous and previous[key] != value for key, value in location.items()):
            raise DryerError("resume_location_must_match_original_run")
        manifest.update(location)
        state_writable = True
        write_json(manifest_path, manifest)
        if not args.no_load:
            with phase("db_preflight", table="public." + TEST_TABLE):
                connection = connect_db(config, readonly=True)
                try:
                    with connection:
                        with connection.cursor() as cursor:
                            cursor.execute("SET LOCAL statement_timeout = '10s'")
                            columns = inspect_table(cursor)
                            event("db_schema_checked", column_count=len(columns), table_exists=bool(columns),
                                  missing_fields=[field for field in FIELDS if field not in {entry[0] for entry in columns}] if columns else [],
                                  unexpected_column_count=len({entry[0] for entry in columns} - set(FIELDS)))
                            if columns:
                                validate_schema(columns)
                finally:
                    connection.close()
        else:
            event("db_preflight_skipped", reason="no_load")
        with phase("category_listing", category_id=CATEGORY_ID, sort=LISTING_SORT,
                   target_limit=args.main_limit or None):
            category_rows = collect_listing(runtime, run_dir, args.max_pages, args.main_limit)
        logger.clear_failure()
        targets = category_targets(category_rows, args.main_limit, args.bsr_limit)
        if not targets:
            raise DryerError("no_category_targets")
        main_count = len(targets)
        bsr_count = sum(row["bsr_rank"] not in ("", None) for row in targets)
        event("bsr_ranks_assigned", source="same_category_listing", bsr_count=bsr_count,
              main_count=main_count, additional_api_requests=0)
        manifest.update(main_target_count=main_count, bsr_target_count=bsr_count,
            overlap_count=main_count + bsr_count - len(targets), target_count=len(targets))
        write_json(run_dir / "targets.json", targets)
        write_json(manifest_path, manifest)
        event("targets_ready", main_count=main_count, bsr_count=bsr_count, overlap_count=manifest["overlap_count"],
              target_count=len(targets))
        with phase("detail_collection", target_count=len(targets)):
            output, evidence, failures = collect_details(runtime, run_dir, targets, batch_id, args.detail_batch_size)
        fallback_count = sum(entry.get("detail_status") == "failed" for entry in evidence)
        collected_count = len(output) - fallback_count
        unattempted_count = len(targets) - collected_count - len(failures)
        ready = len(output) == len(targets) and unattempted_count == 0 and all(
            failure.get("retry_exhausted") for failure in failures)
        if ready:
            logger.clear_failure()
        output_dir = run_dir / "output"
        output_dir.mkdir(exist_ok=True)
        with (output_dir / "final_output.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(output)
        write_json(output_dir / "attribute_evidence.json", evidence)
        write_json(output_dir / "failures.json", failures)
        manifest.update(collected_count=collected_count, output_count=len(output), fallback_count=fallback_count,
            failure_count=len(failures), unattempted_count=unattempted_count, finalization_ready=ready,
            collection_status="complete_with_warnings" if ready and failures else "complete" if ready else "partial",
            null_counts={field: sum(row[field] in ("", None) for row in output) for field in FIELDS if field != "id"})
        write_json(manifest_path, manifest)
        event("collection_summary", target_count=len(targets), collected_count=collected_count,
              output_count=len(output), fallback_count=fallback_count, failure_count=len(failures),
              unattempted_count=manifest["unattempted_count"], null_counts=manifest["null_counts"])
        if not ready:
            logger.failure_stage = logger.failure_stage or "detail_collection"
            event("db_load_skipped", reason="incomplete_details", failure_count=len(failures),
                  unattempted_count=manifest["unattempted_count"])
            raise DryerError("incomplete_details_db_load_skipped")
        if not args.no_load:
            with phase("db_load", table="public." + TEST_TABLE, expected_rows=len(output), batch_id=batch_id):
                manifest["inserted_count"] = load_test_table(config, output, batch_id)
            manifest["db_loaded"] = True
            event("db_load_complete", inserted_count=manifest["inserted_count"], batch_id=batch_id)
        else:
            event("db_load_skipped", reason="no_load")
        manifest["status"] = "success_with_warnings" if failures else "success"
        write_json(manifest_path, manifest)
        event("run_complete", status=manifest["status"], collected_count=collected_count,
              output_count=len(output), fallback_count=fallback_count, failure_count=len(failures),
              db_loaded=manifest["db_loaded"], batch_id=batch_id)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        reason = safe_reason(exc)
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        manifest.update(status=status, error=reason, failure_stage=logger.failure_stage, error_diagnostics=error_diagnostic(exc))
        if state_writable:
            write_json(manifest_path, manifest)
        event("run_complete", status=status, reason=reason, failure_stage=logger.failure_stage,
              db_loaded=manifest["db_loaded"], **error_diagnostic(exc))
        return 130 if status == "interrupted" else 1
    finally:
        if runtime is not None:
            try:
                with phase("browser_cleanup"), safe_legacy_output():
                    runtime[2].close_detail_browser_page()
            except Exception as exc:
                event("browser_cleanup_failed", **error_diagnostic(exc))


if __name__ == "__main__":
    sys.exit(main())
