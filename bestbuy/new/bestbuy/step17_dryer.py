"""DRYER API-only run: shared BBY transport -> existing 20-column retail table."""
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
from .step00_dryer import (FIELDS, TABLE, make_listing_row, make_row, merge_targets,
                          ordered_listing_rows, primary_url, product_selection, public_product)
from .step00_dryer_log import error_diagnostic, event, graphql_diagnostic, graphql_response_diagnostic, phase, run_logging, safe_legacy_output, trace_browser_calls

COLLECTOR_VERSION = 6
FILTER_POLICY = "laundry_exclusions_v1"
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


def collect_listing(runtime, run_dir, kind, max_pages, limit, batch_id="b_selection", batch_size=5, detail_state=None):
    _, listing, helpers = runtime
    from .step00_collection_recovery import MAX_LISTING_PASSES
    from .step01_listing_recovery import validate_page
    from .step07_final_targets import remember_row, row_seen
    detail_state = {} if detail_state is None else detail_state
    decisions = detail_state.setdefault("selection_decisions", {})
    cache = run_dir / f"{kind}_listing.json"
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if (saved.get("collector_version") == COLLECTOR_VERSION and saved.get("limit") == limit
                and saved.get("filter_policy") == FILTER_POLICY
                and (saved.get("complete") or saved.get("limit_reached"))):
            ranked = merge_targets(saved["rows"], []) if kind == "main" else merge_targets([], saved["rows"])
            if all(row.get("selection_action") in {"keep", "uncertain"} for row in saved["rows"]) and (
                    saved.get("complete") or limit > 0 and len(ranked) >= limit):
                event("listing_cache_reused", listing=kind, pages=saved.get("pages", 0), cached_rows=len(ranked))
                return saved["rows"]
    operation = listing.load_product_list_operation("PlpView_ProductList_Init")
    original_sort = listing.SEARCH_SORT
    listing.SEARCH_SORT = "Best-Selling" if kind == "bsr" else ""
    try:
        # Like LDY, discard a failed pass and restart at page one. Never splice list orders.
        for pass_number in range(1, MAX_LISTING_PASSES + 1):
            rows, seen_organic, seen_candidates, audit = [], set(), set(), []

            def record(target, decision):
                entry = {"listing": kind, "sku_id": str(target.get("sku_id") or ""),
                         "item": target.get("bsin") or target.get("item") or "",
                         "product_name": target.get("product_name") or target.get("retailer_sku_name") or "",
                         "product_url": target.get("product_url") or "", "page": target.get("page"),
                         "raw_visual_rank": target.get("global_visual_rank"),
                         "raw_organic_rank": target.get("global_organic_rank"), **decision}
                audit.append(entry)
                event("selection_decision", listing=kind, sku_id=entry["sku_id"], page=entry["page"],
                      action=decision["action"], reason=decision["reason"], source=decision["source"])

            def save(page, complete=False, reached=False, failure_reason=""):
                excluded = [entry for entry in audit if entry["action"] == "exclude"]
                reasons = {reason: sum(entry["reason"] == reason for entry in excluded)
                           for reason in sorted({entry["reason"] for entry in excluded})}
                summary = {"examined_count": len(audit), "selected_count": len(rows),
                           "excluded_count": len(excluded), "excluded_reasons": reasons,
                           "duplicate_count": sum(entry["action"] == "duplicate" for entry in audit),
                           "uncertain_retained_count": sum(row["selection_action"] == "uncertain" for row in rows),
                           "shortfall": max(0, limit - len(rows)) if limit else 0}
                write_json(run_dir / f"{kind}_selection.json", {"collector_version": COLLECTOR_VERSION,
                    "filter_policy": FILTER_POLICY, "listing": kind, "pass_number": pass_number,
                    "complete": complete, "limit_reached": reached, "summary": summary, "decisions": audit})
                write_json(cache, {"collector_version": COLLECTOR_VERSION, "filter_policy": FILTER_POLICY,
                    "limit": limit, "rows": rows if not failure_reason else [], "pages": page,
                    "complete": complete, "limit_reached": reached, "pass_number": pass_number,
                    "failure_reason": failure_reason, "selection_summary": summary})
                return summary
            event("listing_pass_start", listing=kind, attempt=pass_number, max_attempts=MAX_LISTING_PASSES,
                  sort="Best-Selling" if kind == "bsr" else "default", target_limit=limit or None)
            for page in range(1, max_pages + 1):
                event("listing_page_start", listing=kind, page=page, attempt=pass_number)
                payload = listing.prepare_product_list_payload(operation, page)
                status, body = api_post(helpers, payload)
                if status in {400, 401, 402, 403, 404, 429}:
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
                    event("listing_page_failed", listing=kind, page=page, attempt=pass_number,
                          http_status=status, reason=reason, parsed_rows=len(parsed))
                    save(page, failure_reason=reason)
                    if pass_number == MAX_LISTING_PASSES:
                        raise DryerError(f"{kind}_{reason}_page_{page}")
                    delay = listing.listing_retry_delay(pass_number)
                    event("listing_retry", listing=kind, next_attempt=pass_number + 1, failed_page=page,
                          reason=reason, sleep_s=delay, restart_from_page=1)
                    with phase("listing_retry_wait", listing=kind):
                        time.sleep(delay)
                    break
                seen_organic.update(organic)
                candidates = []
                for raw in ordered_listing_rows(parsed, kind):
                    target = listing_target(raw, helpers)
                    if not target["sku_id"]:
                        continue
                    if row_seen(target, seen_candidates):
                        record(target, {"action": "duplicate", "reason": "same_product", "source": "listing_identity"})
                        continue
                    remember_row(target, seen_candidates)
                    key = (str(target["sku_id"]), str(target.get("bsin") or target.get("item") or "").casefold())
                    decision = product_selection(target)
                    previous_decision = decisions.get(key)
                    if previous_decision and (previous_decision["action"] != "uncertain" or decision["action"] == "uncertain"):
                        decision = previous_decision
                    if decision["action"] == "exclude":
                        decisions[key] = decision
                        record(target, decision)
                    else:
                        candidates.append((target, key, decision))
                start = 0
                while start < len(candidates) and (not limit or len(rows) < limit):
                    size = min(batch_size, limit - len(rows)) if limit else batch_size
                    chunk = candidates[start:start + size]
                    start += len(chunk)
                    uncertain = [target for target, key, decision in chunk
                                 if decision["action"] == "uncertain" and key not in decisions]
                    if uncertain:
                        event("selection_detail_start", listing=kind, page=page, sku_ids=[t["sku_id"] for t in uncertain])
                        with phase("selection_detail", listing=kind, page=page, operations=len(uncertain)):
                            collect_details(runtime, run_dir, uncertain, batch_id, batch_size, detail_state,
                                            progress_stage="selection_detail")
                        if detail_state.get("stopped_reason"):
                            save(page, failure_reason=detail_state["stopped_reason"])
                            raise DryerError(detail_state["stopped_reason"])
                    for target, key, decision in chunk:
                        if decision["action"] == "uncertain" and key not in decisions:
                            capture = detail_state.get("captures", {}).get(str(target["sku_id"]))
                            decision = product_selection(target, capture["product"]) if capture else {
                                "action": "uncertain", "reason": "detail_unavailable", "source": "detail_api"}
                        decisions[key] = decision
                        record(target, decision)
                        if decision["action"] != "exclude":
                            rows.append(dict(target, selection_action=decision["action"],
                                             selection_reason=decision["reason"], selection_source=decision["source"]))
                reached = limit > 0 and len(rows) >= limit
                summary = save(page, complete=empty, reached=reached)
                progress(kind + "_list", len(rows), limit, page=page, parsed_rows=len(parsed),
                         candidate_count=len(rows), excluded_count=summary["excluded_count"],
                         duplicate_count=summary["duplicate_count"],
                         uncertain_retained_count=summary["uncertain_retained_count"], complete=empty)
                if reached or empty:
                    event("listing_selection_complete", listing=kind, pages=page,
                          stop_reason="target_reached" if reached else "products_exhausted", **summary)
                    if empty and limit and len(rows) < limit:
                        event("listing_shortfall", listing=kind, requested=limit, collected=len(rows),
                              shortfall=limit - len(rows), reason="products_exhausted")
                    return rows
                if listing.LISTING_PAGE_SLEEP_SECONDS:
                    with phase("listing_page_wait", listing=kind, sleep_s=listing.LISTING_PAGE_SLEEP_SECONDS):
                        time.sleep(listing.LISTING_PAGE_SLEEP_SECONDS)
            else:
                save(max_pages, failure_reason="max_pages_reached")
                raise DryerError(f"{kind}_max_pages_reached_collection_incomplete")
    finally:
        listing.SEARCH_SORT = original_sort


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


def collect_details(runtime, run_dir, targets, batch_id, batch_size, detail_state=None, progress_stage="detail"):
    _, _, helpers = runtime
    detail_state = {} if detail_state is None else detail_state
    reusable = detail_state.setdefault("captures", {})
    previous_failures = detail_state.setdefault("failures", {})
    successes, failures = {}, {}
    pending = []

    def accept(target, capture, save_capture=True):
        sku_id = str(target["sku_id"])
        if capture.get("collector_version") != COLLECTOR_VERSION:
            raise DryerError("capture_version_mismatch")
        if missing_attribute_fields(capture["product"]):
            raise DryerError("detail_attribute_response_incomplete")
        timestamp = datetime.fromisoformat(capture["captured_at"])
        row, evidence = make_row(target, capture["product"], helpers, batch_id, timestamp)
        if save_capture:
            write_json(run_dir / "products" / f"{sku_id}.json", capture)
        reusable[sku_id] = capture
        successes[sku_id] = (row, evidence)
        failures.pop(sku_id, None)
        previous_failures.pop(sku_id, None)
        progress(progress_stage, len(successes), len(targets), sku_id=sku_id,
                 failure_count=len(failures), pending_count=len(targets) - len(successes), status="success")
        for field, source in (("loading_type", evidence.get("source")), ("capacity", evidence.get("capacity_source"))):
            if row[field] in ("", None):
                event("attribute_missing", sku_id=sku_id, field=field, reason=source or "not_stated")
            elif field == "loading_type":
                event("attribute_collected", sku_id=sku_id, field=field, value=row[field], source=source,
                      source_field=evidence.get("source_field", "specificationGroups"),
                      rule=evidence.get("rule", "explicit_specification"), evidence=evidence.get("evidence", ""))

    for target in targets:
        sku_id = str(target["sku_id"])
        cache = run_dir / "products" / f"{target['sku_id']}.json"
        if sku_id in reusable or cache.exists():
            try:
                capture = reusable.get(sku_id) or json.loads(cache.read_text(encoding="utf-8"))
                accept(target, capture, save_capture=False)
                event("detail_cache_reused", sku_id=sku_id)
                continue
            except Exception as exc:
                reusable.pop(sku_id, None)
                event("detail_cache_rejected", sku_id=sku_id, reason=safe_reason(exc), **error_diagnostic(exc))
        if sku_id in previous_failures:
            failures[sku_id] = previous_failures[sku_id]
            if failures[sku_id].get("retry_exhausted"):
                event("detail_failure_reused", sku_id=sku_id, reason=failures[sku_id]["reason"])
                continue
        pending.append(target)
    stopped = bool(detail_state.get("stopped_reason"))
    for start in range(0, len(pending), batch_size):
        if stopped:
            break
        remaining = pending[start:start + batch_size]
        for attempt in range(1, MAX_ITEM_ATTEMPTS + 1):
            batch_error = ""
            diagnostics = {}
            event("detail_batch_start", batch=start // batch_size + 1, attempt=attempt,
                  purpose=progress_stage,
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
                previous_failures[sku_id] = failures[sku_id]
                event("detail_failed", **failures[sku_id])
                retry.append(target)
            remaining = retry
            if not remaining:
                break
            if batch_error in {"browser_api_unavailable", "http_400", "http_401", "http_402", "http_403", "http_404", "http_429"}:
                stopped = True
                detail_state["stopped_reason"] = batch_error
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
            progress(progress_stage, len(successes) + fallback_count, len(targets), sku_id=sku_id,
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
        FROM information_schema.columns WHERE table_schema=%s AND table_name=%s
        ORDER BY ordinal_position""", tuple(TABLE.split(".")))
    return cursor.fetchall()


def validate_schema(columns):
    if not columns:
        raise DryerError("dryer_table_not_found")
    if {entry[0] for entry in columns} != set(FIELDS):
        raise DryerError("dryer_table_must_have_exactly_20_requested_columns")
    identifier = next(entry for entry in columns if entry[0] == "id")
    if not (identifier[2] or identifier[3] == "YES"):
        raise DryerError("dryer_table_id_must_be_generated")


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


def load_table(config, rows, batch_id):
    if not rows or any(row.get("batch_id") != batch_id or row.get("account_name") != "Bestbuy" for row in rows):
        raise DryerError("invalid_dryer_batch")
    # Missing optional identifiers are NULL, not a shared product identity.
    keys = [str(row["item"]).casefold() for row in rows if row.get("item")]
    if len(keys) != len(set(keys)):
        raise DryerError("duplicate_product_identity_in_batch")
    connection = connect_db(config)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '60s'")
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (TABLE + ":Bestbuy:" + batch_id,))
                columns = inspect_table(cursor)
                validate_schema(columns)
                types = {entry[0]: entry[1] for entry in columns}
                fields = FIELDS[1:]
                values = [tuple(db_value(row[field], types[field], field) for field in fields) for row in rows]
                cursor.execute(f"DELETE FROM {TABLE} WHERE batch_id=%s AND account_name=%s", (batch_id, "Bestbuy"))
                sql = f'INSERT INTO {TABLE} (' + ",".join('"' + f + '"' for f in fields) + ') VALUES (' + ",".join(["%s"] * len(fields)) + ')'
                cursor.executemany(sql, values)
                cursor.execute(f"SELECT count(*) FROM {TABLE} WHERE batch_id=%s AND account_name=%s", (batch_id, "Bestbuy"))
                if cursor.fetchone()[0] != len(rows):
                    raise DryerError("inserted_dryer_batch_count_mismatch")
    finally:
        connection.close()
    return len(rows)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=f"DRYER API only; {TABLE} only")
    parser.add_argument("--main-limit", type=int, default=300, help="retained products after exclusion in default sort; 0=all")
    parser.add_argument("--bsr-limit", type=int, default=100, help="retained products after exclusion in Best-Selling sort; 0=all")
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
    except KeyboardInterrupt:
        print("status=interrupted reason=KeyboardInterrupt", flush=True)
        return 130
    except OSError:
        print("status=failed reason=run_log_not_writable", flush=True)
        return 1


def run(args, run_dir, logger):
    manifest_path = run_dir / "dryer_manifest.json"
    manifest = {"collector_version": COLLECTOR_VERSION, "category": "DRYER", "table": TABLE,
        "search_term": "DRYER", "main_limit": args.main_limit, "bsr_limit": args.bsr_limit,
        "filter_policy": FILTER_POLICY, "rank_mode": "filtered_products",
        "transport": "browser_graphql", "detail_batch_size": args.detail_batch_size,
        "status": "started", "db_loaded": False}
    has_previous = manifest_path.is_file()
    state_writable = not has_previous
    runtime = None
    event("run_start", run_dir=str(run_dir), main_limit=args.main_limit, bsr_limit=args.bsr_limit,
          detail_batch_size=args.detail_batch_size, no_load=args.no_load, resume=bool(args.resume))
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8")) if has_previous else {}
        if previous and previous.get("collector_version") != COLLECTOR_VERSION:
            raise DryerError("resume_requires_current_api_run")
        if previous.get("table") not in (None, TABLE):
            raise DryerError("resume_table_must_match_original_run")
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
            with phase("db_preflight", table=TABLE):
                connection = connect_db(config, readonly=True)
                try:
                    with connection:
                        with connection.cursor() as cursor:
                            cursor.execute("SET LOCAL statement_timeout = '10s'")
                            columns = inspect_table(cursor)
                            event("db_schema_checked", column_count=len(columns), table_exists=bool(columns),
                                  missing_fields=[field for field in FIELDS if field not in {entry[0] for entry in columns}] if columns else [],
                                  unexpected_column_count=len({entry[0] for entry in columns} - set(FIELDS)))
                            validate_schema(columns)
                finally:
                    connection.close()
        else:
            event("db_preflight_skipped", reason="no_load")
        detail_state = {}
        with phase("main_listing", target_limit=args.main_limit or None):
            main_rows = collect_listing(runtime, run_dir, "main", args.max_pages, args.main_limit,
                                        batch_id, args.detail_batch_size, detail_state)
        logger.clear_failure()
        with phase("bsr_listing", target_limit=args.bsr_limit or None):
            bsr_rows = collect_listing(runtime, run_dir, "bsr", args.max_pages, args.bsr_limit,
                                       batch_id, args.detail_batch_size, detail_state)
        logger.clear_failure()
        main_count = len(merge_targets(main_rows, [], args.main_limit, 0))
        bsr_count = len(merge_targets([], bsr_rows, 0, args.bsr_limit))
        targets = merge_targets(main_rows, bsr_rows, args.main_limit, args.bsr_limit)
        if not targets:
            raise DryerError("no_search_targets")
        manifest.update(main_target_count=main_count, bsr_target_count=bsr_count,
            overlap_count=main_count + bsr_count - len(targets), target_count=len(targets))
        selection_audits = {}
        for kind in ("main", "bsr"):
            audit_file = run_dir / f"{kind}_selection.json"
            if audit_file.exists():
                selection_audits[kind] = json.loads(audit_file.read_text(encoding="utf-8"))
        manifest["selection_summaries"] = {kind: audit["summary"] for kind, audit in selection_audits.items()}
        write_json(run_dir / "output" / "selection_audit.json", selection_audits)
        write_json(run_dir / "targets.json", targets)
        write_json(manifest_path, manifest)
        event("targets_ready", main_count=main_count, bsr_count=bsr_count, overlap_count=manifest["overlap_count"],
              target_count=len(targets))
        with phase("detail_collection", target_count=len(targets)):
            output, evidence, failures = collect_details(runtime, run_dir, targets, batch_id, args.detail_batch_size, detail_state)
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
            with phase("db_load", table=TABLE, expected_rows=len(output), batch_id=batch_id):
                manifest["inserted_count"] = load_table(config, output, batch_id)
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
        manifest["elapsed_s"] = round(time.monotonic() - logger.started, 2)
        mail_interrupted = False
        if not args.no_load and manifest["status"] != "interrupted":
            try:
                from .step18_dryer_email_notify import notify_run
                with phase("email_notify"):
                    manifest["email"] = notify_run(manifest, run_dir)
                write_json(run_dir / "output" / "email_notify_manifest.json", manifest["email"])
            except KeyboardInterrupt:
                mail_interrupted = True
                manifest["email"] = {"sent": False, "status": "interrupted"}
                event("email_notify_interrupted")
            except Exception as exc:
                manifest["email"] = {"sent": False, "status": "failed", **error_diagnostic(exc)}
                event("email_notify_failed", **error_diagnostic(exc))
        else:
            event("email_notify_skipped", reason="no_load" if args.no_load else "interrupted")
        manifest["total_elapsed_s"] = round(time.monotonic() - logger.started, 2)
        if state_writable:
            try:
                write_json(manifest_path, manifest)
            except Exception as exc:
                event("run_state_update_failed", **error_diagnostic(exc))
        event("run_finished", status=manifest["status"], db_loaded=manifest["db_loaded"],
              duration_s=round(time.monotonic() - logger.started, 2),
              email_sent=manifest.get("email", {}).get("sent", False))
        if mail_interrupted:
            raise KeyboardInterrupt


if __name__ == "__main__":
    sys.exit(main())
