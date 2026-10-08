"""DRYER API-only run: shared BBY transport -> exact 20-column test table."""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .step00_dryer import FIELDS, TEST_TABLE, make_row, merge_targets, public_product

COLLECTOR_VERSION = 2
QUERY = """query DryerDetail($skuId:String!$productPriceInput:ProductPriceInput!){productBySkuId(skuId:$skuId){
skuId bsin name{short}description{short}features{description title}manufacturer{modelNumber}url{pdp}
reviewInfo{averageRating reviewCount}specificationGroups{specifications{displayName value}}
price(input:$productPriceInput){customerPrice regularPrice totalSavings}}}"""


class DryerError(RuntimeError):
    pass


def safe_reason(exc):
    reason = str(exc) if isinstance(exc, (DryerError, ValueError)) else ""
    return reason if re.fullmatch(r"[a-zA-Z0-9_]+", reason) else type(exc).__name__


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load_runtime(run_dir):
    # Configuration is loaded only by the existing approved loader.
    os.environ.update(BESTBUY_CATEGORY="DRYER", BESTBUY_URL_SOURCE="default",
        BESTBUY_RUN_ROOT=str(run_dir), BESTBUY_SEARCH_TERM="DRYER", BESTBUY_SEARCH_URL="",
        BESTBUY_LISTING_COLLECTION_MODE="browser_graphql", BESTBUY_DETAIL_FETCH_MODE="browser_graphql",
        BESTBUY_DETAIL_DIRECT_GRAPHQL="1", BESTBUY_DETAIL_PDP_FALLBACK="0")
    from . import step00_config as config
    from . import step01_main_list as listing
    from . import step08_detail_enrichment as detail
    # This entry point runs in its own Python process; existing LDY processes are unaffected.
    listing.CATEGORY = "DRYER"
    listing.SEARCH_TERM = "DRYER"
    listing.SEARCH_URL_TEMPLATE = ""
    listing.INCLUDE_SPONSORED_CAROUSEL = False
    return config, listing, detail


def api_post(helpers, payload):
    # Share LDY's connection, session bootstrap and transport recovery directly.
    # Discard legacy raw diagnostics; emit safe status codes from this runner only.
    count = len(payload) if isinstance(payload, list) else 1
    print(f"api_request operations={count} start", flush=True)
    started = time.perf_counter()
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status, _, body, _, _ = helpers.browser_graphql_post(
                payload, "https://www.bestbuy.com/?intl=nosplash")
    except Exception:
        raise DryerError("browser_api_unavailable") from None
    print(f"api_response status={int(status or 0)} elapsed={time.perf_counter() - started:.2f}s", flush=True)
    return int(status or 0), body


def collect_listing(runtime, run_dir, kind, max_pages, limit):
    _, listing, helpers = runtime
    from .step00_collection_recovery import MAX_LISTING_PASSES
    from .step01_listing_recovery import validate_page
    cache = run_dir / f"{kind}_listing.json"
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if (saved.get("collector_version") == COLLECTOR_VERSION and saved.get("limit") == limit
                and (saved.get("complete") or saved.get("limit_reached"))):
            return saved["rows"]
    operation = listing.load_product_list_operation("PlpView_ProductList_Init")
    original_sort = listing.SEARCH_SORT
    listing.SEARCH_SORT = "Best-Selling" if kind == "bsr" else ""
    try:
        # Like LDY, discard a failed pass and restart at page one. Never splice list orders.
        for pass_number in range(1, MAX_LISTING_PASSES + 1):
            rows, seen_organic = [], set()
            for page in range(1, max_pages + 1):
                payload = listing.prepare_product_list_payload(operation, page)
                status, body = api_post(helpers, payload)
                if status in {400, 401, 402, 403, 404}:
                    raise DryerError(f"http_{status}")
                parsed = listing.parse_page_rows(page, body) if isinstance(body, dict) else []
                ok, reason, empty = validate_page(body,
                    {"status_code": status, "error": "", "parse_error": ""}, parsed)
                organic = [str(r["sku_id"]) for r in parsed if r.get("container_type") == "organic_product"]
                if ok and organic and all(sku in seen_organic for sku in organic):
                    ok, reason = False, "repeated_organic_page"
                if page == 1 and empty:
                    ok, reason = False, "empty_first_page"
                if not ok:
                    write_json(cache, {"collector_version": COLLECTOR_VERSION, "limit": limit,
                        "rows": [], "pages": page, "complete": False, "limit_reached": False,
                        "pass_number": pass_number, "failure_reason": reason})
                    if pass_number == MAX_LISTING_PASSES:
                        raise DryerError(f"{kind}_{reason}_page_{page}")
                    print(f"listing={kind} restart_pass={pass_number + 1} failed_page={page} reason={reason}", flush=True)
                    time.sleep(listing.listing_retry_delay(pass_number))
                    break
                seen_organic.update(organic)
                # Keep public target data and original positions; no raw request/response logs.
                rows.extend({key: row.get(key, "") for key in (
                    "sku_id", "bsin", "product_name", "product_url", "container_type", "is_sponsored",
                    "page", "visual_rank", "global_visual_rank", "organic_rank", "global_organic_rank")}
                    for row in parsed)
                selected = merge_targets(rows, []) if kind == "main" else merge_targets([], rows)
                reached = limit > 0 and len(selected) >= limit
                write_json(cache, {"collector_version": COLLECTOR_VERSION, "limit": limit,
                    "rows": rows, "pages": page, "complete": empty, "limit_reached": reached,
                    "pass_number": pass_number})
                print(f"listing={kind} page={page} dryer_candidates={len(selected)} complete={empty}", flush=True)
                if reached or empty:
                    return rows
                if listing.LISTING_PAGE_SLEEP_SECONDS:
                    time.sleep(listing.LISTING_PAGE_SLEEP_SECONDS)
            else:
                raise DryerError(f"{kind}_max_pages_reached_collection_incomplete")
    finally:
        listing.SEARCH_SORT = original_sort


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
            continue
        data = response.get("data")
        product = data.get("productBySkuId") if isinstance(data, dict) else None
        if not isinstance(product, dict) or str(product.get("skuId") or "") != sku_id:
            errors[sku_id] = "detail_product_identity_mismatch"
            continue
        expected_item = str(target.get("bsin") or "").strip().lower()
        if expected_item and str(product.get("bsin") or "").strip().lower() != expected_item:
            errors[sku_id] = "detail_product_identity_mismatch"
            continue
        if not all(key in product for key in ("features", "description", "specificationGroups")):
            errors[sku_id] = "detail_attribute_response_incomplete"
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
        timestamp = datetime.fromisoformat(capture["captured_at"])
        row, evidence = make_row(target, capture["product"], helpers, batch_id, timestamp)
        write_json(run_dir / "products" / f"{sku_id}.json", capture)
        successes[sku_id] = (row, evidence)
        failures.pop(sku_id, None)
        print(f"detail={len(successes)}/{len(targets)} sku_id={sku_id} success", flush=True)

    for target in targets:
        cache = run_dir / "products" / f"{target['sku_id']}.json"
        if cache.exists():
            try:
                accept(target, json.loads(cache.read_text(encoding="utf-8")))
                continue
            except Exception:
                pass  # Invalid/old captures are fetched again; identifiers are never replaced.
        pending.append(target)
    stopped = False
    for start in range(0, len(pending), batch_size):
        remaining = pending[start:start + batch_size]
        for attempt in range(1, max(1, helpers.MAX_ATTEMPTS) + 1):
            batch_error = ""
            try:
                captures, errors = collect_product_batch(runtime, remaining)
            except Exception as exc:
                captures = {}
                batch_error = safe_reason(exc)
                errors = {str(t["sku_id"]): batch_error for t in remaining}
            retry = []
            for target in remaining:
                sku_id = str(target["sku_id"])
                if sku_id in captures:
                    try:
                        accept(target, captures[sku_id])
                        continue
                    except Exception as exc:
                        errors[sku_id] = safe_reason(exc)
                failures[sku_id] = {"sku_id": sku_id, "stage": "detail", "reason": errors.get(sku_id, "detail_missing_response")}
                retry.append(target)
            remaining = retry
            if not remaining:
                break
            if batch_error in {"browser_api_unavailable", "http_400", "http_401", "http_402", "http_403", "http_404"}:
                stopped = True
                break
            if attempt < max(1, helpers.MAX_ATTEMPTS):
                time.sleep(helpers.detail_retry_sleep_seconds(attempt))
        if stopped:
            break
    ordered = [successes[str(t["sku_id"])] for t in targets if str(t["sku_id"]) in successes]
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
    keys = [(row["item"], row["sku"]) for row in rows]
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
    parser.add_argument("--main-limit", type=int, default=20, help="standalone dryers from default sort; 0=all")
    parser.add_argument("--bsr-limit", type=int, default=10, help="standalone dryers from Best-Selling sort; 0=all")
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
    manifest_path = run_dir / "dryer_manifest.json"
    manifest = {"collector_version": COLLECTOR_VERSION, "category": "DRYER", "table": "public." + TEST_TABLE,
        "search_term": "DRYER", "main_limit": args.main_limit, "bsr_limit": args.bsr_limit,
        "transport": "browser_graphql", "detail_batch_size": args.detail_batch_size,
        "status": "started", "db_loaded": False}
    has_previous = manifest_path.is_file()
    state_writable = not has_previous
    runtime = None
    print(f"run_dir={run_dir}", flush=True)
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8")) if has_previous else {}
        if previous and previous.get("collector_version") != COLLECTOR_VERSION:
            raise DryerError("resume_requires_current_api_run")
        if previous and any(previous.get(key) != manifest[key] for key in ("main_limit", "bsr_limit")):
            raise DryerError("resume_must_use_the_same_limits")
        batch_id = previous.get("batch_id") or "b_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        manifest["batch_id"] = batch_id
        runtime = load_runtime(run_dir)
        config, _, helpers = runtime
        location = {"zip_code": config.bestbuy_zip_code(), "store_id": config.bestbuy_store_id()}
        if any(key in previous and previous[key] != value for key, value in location.items()):
            raise DryerError("resume_location_must_match_original_run")
        if not args.no_load:
            connection = connect_db(config, readonly=True)
            try:
                with connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '10s'")
                        columns = inspect_table(cursor)
                        if columns:
                            validate_schema(columns)
            finally:
                connection.close()
        manifest.update(location)
        state_writable = True
        write_json(manifest_path, manifest)
        main_rows = collect_listing(runtime, run_dir, "main", args.max_pages, args.main_limit)
        bsr_rows = collect_listing(runtime, run_dir, "bsr", args.max_pages, args.bsr_limit)
        main_count = len(merge_targets(main_rows, [], args.main_limit, 0))
        bsr_count = len(merge_targets([], bsr_rows, 0, args.bsr_limit))
        targets = merge_targets(main_rows, bsr_rows, args.main_limit, args.bsr_limit)
        if not targets:
            raise DryerError("no_standalone_dryer_targets")
        manifest.update(main_target_count=main_count, bsr_target_count=bsr_count,
            overlap_count=main_count + bsr_count - len(targets), target_count=len(targets))
        write_json(run_dir / "targets.json", targets)
        write_json(manifest_path, manifest)
        print(f"targets main={main_count} bsr={bsr_count} overlap={manifest['overlap_count']} unique={len(targets)}", flush=True)
        output, evidence, failures = collect_details(runtime, run_dir, targets, batch_id, args.detail_batch_size)
        output_dir = run_dir / "output"
        output_dir.mkdir(exist_ok=True)
        with (output_dir / "final_output.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(output)
        write_json(output_dir / "attribute_evidence.json", evidence)
        write_json(output_dir / "failures.json", failures)
        manifest.update(collected_count=len(output), failure_count=len(failures),
            unattempted_count=len(targets) - len(output) - len(failures),
            null_counts={field: sum(row[field] in ("", None) for row in output) for field in FIELDS if field != "id"})
        if failures or len(output) != len(targets):
            raise DryerError("incomplete_details_db_load_skipped")
        if not args.no_load:
            manifest["inserted_count"] = load_test_table(config, output, batch_id)
            manifest["db_loaded"] = True
        manifest["status"] = "success"
        write_json(manifest_path, manifest)
        print(f"status=success collected={len(output)} db_loaded={manifest['db_loaded']}", flush=True)
        return 0
    except Exception as exc:
        reason = safe_reason(exc)
        manifest.update(status="failed", error=reason)
        if state_writable:
            write_json(manifest_path, manifest)
        print(f"status=failed reason={reason}", flush=True)
        return 1
    finally:
        if runtime is not None:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                runtime[2].close_detail_browser_page()


if __name__ == "__main__":
    sys.exit(main())
