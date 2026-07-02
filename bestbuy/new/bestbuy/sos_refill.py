import argparse
import csv
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from .bestbuy_orchestrator import (
    CATEGORY_SEARCH_TERMS,
    HHP_TRENDING_PAGE_PAYLOAD_ENV,
    STEPS,
    apply_run_path_env,
)
from .step00_config import DEFAULT_BESTBUY_RUN_ROOT, bestbuy_category, has_target_url
from .step00_fulfillment_graphql import parse_fulfillment_response


PYTHON = sys.executable

DEFAULT_STEP_NAMES = [
    "main_list",
    "main_targets",
    "bsr_list",
    "bsr_rank",
    "final_targets",
    "detail_html",
    "review20",
    "availability_backfill",
    "status_check",
    "db_prepare",
    "db_load",
    "item_mst_load",
]

JOIN_STEP_NAMES = ["promotion_deals", "trending_deals"]

TABLE_ENV_KEYS = [
    "BESTBUY_OUTPUT_TABLE",
    "BESTBUY_PRODUCT_LIST_TABLE",
    "BESTBUY_OUTPUT_TABLE_TV",
    "BESTBUY_OUTPUT_TABLE_HHP",
    "BESTBUY_OUTPUT_TABLE_REF",
    "BESTBUY_OUTPUT_TABLE_LDY",
    "BESTBUY_PRODUCT_LIST_TABLE_TV",
    "BESTBUY_PRODUCT_LIST_TABLE_HHP",
    "BESTBUY_PRODUCT_LIST_TABLE_REF",
    "BESTBUY_PRODUCT_LIST_TABLE_LDY",
]

PRESERVE_SKIP_FIELDS = {
    "id",
    "batch_id",
    "page_type",
    "main_rank",
    "bsr_rank",
    "trend_rank",
    "promotion_position",
    "calendar_week",
    "crawl_datetime",
    "crawl_strdatetime",
    "pick_up_availability",
    "fastest_delivery",
    "delivery_availability",
}
TIMESTAMP_FIELDS = {"crawl_datetime", "crawl_strdatetime"}
AVAILABILITY_FIELDS = ("pick_up_availability", "fastest_delivery", "delivery_availability")


class StepFailure(RuntimeError):
    def __init__(self, step, returncode):
        self.step = step
        self.returncode = returncode
        super().__init__(f"step {step.key} {step.name} failed exit_code={returncode}")


def step_by_name(name):
    for step in STEPS:
        if step.name == name:
            return step
    raise RuntimeError(f"unknown step name: {name}")


def read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def read_csv_rows(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_rows(path, rows, fieldnames):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def compact(value):
    return str(value or "").strip()


def canonical_url(value):
    text = compact(value).lower()
    if not text:
        return ""
    return text.split("?", 1)[0].rstrip("/")


def row_match_keys(row):
    keys = set()
    for field in ("item", "bsin", "sku_id"):
        value = compact(row.get(field)).lower()
        if value:
            keys.add(value)
    url = canonical_url(row.get("product_url") or row.get("detail_url"))
    if url:
        keys.add(url)
    return keys


def row_needs_critical_refill(row):
    return not compact(row.get("retailer_sku_name")) or not compact(row.get("final_sku_price"))


def dominant_nonblank(rows, field):
    counts = Counter(compact(row.get(field)) for row in rows if compact(row.get(field)))
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def int_value(value):
    try:
        return int(float(compact(value).replace(",", "")))
    except ValueError:
        return 0


def status_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def infer_batch_id(run_root):
    rows = read_csv_rows(run_root / "output" / "final_output.csv")
    counts = Counter(compact(row.get("batch_id")) for row in rows if compact(row.get("batch_id")))
    if counts:
        return counts.most_common(1)[0][0]
    manifest = read_json(run_root / "availability_backfill" / "manifest.json")
    if compact(manifest.get("batch_id")):
        return compact(manifest.get("batch_id"))
    return ""


def failed_listing_pages(run_root, run_id):
    rows = read_csv_rows(run_root / run_id / "benchmarks" / "page_benchmarks.csv")
    failed = []
    for row in rows:
        status = status_int(row.get("status_code"))
        total = int_value(row.get("total_occurrence_count"))
        organic = int_value(row.get("organic_count"))
        if status != 200 or total <= 0 or organic <= 0:
            failed.append(
                {
                    "page": int_value(row.get("page")),
                    "status_code": compact(row.get("status_code")),
                    "organic_count": organic,
                    "total_occurrence_count": total,
                    "cost": compact(row.get("x_request_cost")),
                    "attempts": compact(row.get("attempt_count")),
                }
            )
    return failed


def current_summary(run_root):
    final_rows = read_csv_rows(run_root / "output" / "final_output.csv")
    target_manifest = read_json(run_root / "output" / "bestbuy_final_targets.manifest.json")
    detail_failures = read_csv_rows(run_root / "detail" / "parsed" / "detail_failures.csv")
    return {
        "run_root": str(run_root),
        "batch_id": infer_batch_id(run_root),
        "final_rows": len(final_rows),
        "main_failed_pages": failed_listing_pages(run_root, "main"),
        "bsr_failed_pages": failed_listing_pages(run_root, "bsr"),
        "main_unique_count": int_value(target_manifest.get("main_unique_count")),
        "bsr_count": int_value(target_manifest.get("bsr_count")),
        "final_unique_sku_count": int_value(target_manifest.get("final_unique_sku_count")),
        "needs_more_main_candidates": bool(target_manifest.get("needs_more_main_candidates")),
        "detail_failure_count": len(detail_failures),
        "detail_failure_stages": dict(Counter(compact(row.get("stage")) or "unknown" for row in detail_failures)),
        "retailer_sku_name_nulls": sum(1 for row in final_rows if not compact(row.get("retailer_sku_name"))),
        "final_sku_price_nulls": sum(1 for row in final_rows if not compact(row.get("final_sku_price"))),
    }


def detail_refill_skus(existing_rows, run_root):
    target_rows = read_csv_rows(run_root / "output" / "bestbuy_final_targets.csv")
    existing_keys = set()
    problem_keys = set()
    for row in existing_rows:
        keys = row_match_keys(row)
        existing_keys.update(keys)
        if row_needs_critical_refill(row):
            problem_keys.update(keys)

    skus = []
    seen = set()
    for row in target_rows:
        sku = compact(row.get("sku_id"))
        if not sku or sku in seen:
            continue
        keys = row_match_keys(row)
        is_new_row = bool(keys) and not (keys & existing_keys)
        is_problem_row = bool(keys & problem_keys)
        listing_is_blank = not compact(row.get("product_name") or row.get("retailer_sku_name")) or not compact(
            row.get("customer_price") or row.get("final_sku_price")
        )
        if is_new_row or is_problem_row or listing_is_blank:
            skus.append(sku)
            seen.add(sku)
    return skus


def merge_existing_nonblank_values(existing_rows, current_path, label, log_handle=None):
    current_path = Path(current_path)
    current_rows = read_csv_rows(current_path)
    if not existing_rows or not current_rows:
        return {"rows_updated": 0, "fields_updated": 0}

    existing_by_key = {}
    for row in existing_rows:
        for key in row_match_keys(row):
            existing_by_key.setdefault(key, row)

    rows_updated = 0
    fields_updated = 0
    fieldnames = list(current_rows[0].keys())
    dominant_timestamps = {
        field: dominant_nonblank(existing_rows, field)
        for field in TIMESTAMP_FIELDS
        if field in fieldnames
    }
    for row in current_rows:
        old = None
        for key in row_match_keys(row):
            old = existing_by_key.get(key)
            if old:
                break
        if not old:
            old = {}
        row_changed = False
        for field, dominant_value in dominant_timestamps.items():
            desired = compact(old.get(field)) or dominant_value
            if desired and compact(row.get(field)) != desired:
                row[field] = desired
                row_changed = True
                fields_updated += 1
        for field in fieldnames:
            if field in TIMESTAMP_FIELDS:
                continue
            if field in PRESERVE_SKIP_FIELDS:
                continue
            if old and not compact(row.get(field)) and compact(old.get(field)):
                row[field] = old.get(field)
                row_changed = True
                fields_updated += 1
        if row_changed:
            rows_updated += 1

    if fields_updated:
        write_csv_rows(current_path, current_rows, fieldnames)
    message = f"[sos:preserve] {label} existing_nonblank rows_updated={rows_updated} fields_updated={fields_updated}"
    print(message)
    if log_handle:
        log_handle.write(message + "\n")
    return {"rows_updated": rows_updated, "fields_updated": fields_updated}


def build_target_sku_lookup(run_root):
    lookup = {}
    for row in read_csv_rows(run_root / "output" / "bestbuy_final_targets.csv"):
        sku = compact(row.get("sku_id"))
        if not sku:
            continue
        for key in row_match_keys(row):
            lookup.setdefault(key, sku)
    return lookup


def row_sku(row, lookup):
    sku = compact(row.get("sku_id"))
    if sku:
        return sku
    for key in row_match_keys(row):
        sku = lookup.get(key)
        if sku:
            return sku
    return ""


def cached_availability_values(run_root):
    values_by_sku = {}
    raw_roots = sorted((run_root / "availability_backfill").glob("*/raw"))
    response_count = 0
    for raw_root in raw_roots:
        for response_path in sorted(raw_root.glob("chunk_*/response.json")):
            try:
                data = json.loads(response_path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            parsed = parse_fulfillment_response(data)
            if parsed:
                response_count += 1
            for sku, values in parsed.items():
                values_by_sku.setdefault(str(sku), {}).update(values)
    return values_by_sku, response_count


def apply_cached_availability_values(run_root, log_handle=None):
    values_by_sku, response_count = cached_availability_values(run_root)
    if not values_by_sku:
        message = "[sos:availability_cache] reusable_skus=0"
        print(message)
        if log_handle:
            log_handle.write(message + "\n")
        return {"rows_updated": 0, "fields_updated": 0, "reusable_skus": 0}

    lookup = build_target_sku_lookup(run_root)
    targets = [
        ("final_output", run_root / "output" / "final_output.csv"),
        ("product_list", run_root / "output" / "bestbuy_product_list.csv"),
        ("detail_rows", run_root / "detail" / "parsed" / "detail_enriched_rows.csv"),
    ]
    total_rows_updated = 0
    total_fields_updated = 0
    for label, path in targets:
        rows = read_csv_rows(path)
        if not rows:
            continue
        fieldnames = list(rows[0].keys())
        rows_updated = 0
        fields_updated = 0
        for row in rows:
            sku = row_sku(row, lookup)
            values = values_by_sku.get(sku) or {}
            row_changed = False
            for field in AVAILABILITY_FIELDS:
                value = compact(values.get(field))
                if field in fieldnames and value and not compact(row.get(field)):
                    row[field] = value
                    row_changed = True
                    fields_updated += 1
            if row_changed:
                rows_updated += 1
        if fields_updated:
            write_csv_rows(path, rows, fieldnames)
        total_rows_updated += rows_updated
        total_fields_updated += fields_updated
        message = f"[sos:availability_cache] {label} rows_updated={rows_updated} fields_updated={fields_updated}"
        print(message)
        if log_handle:
            log_handle.write(message + "\n")

    summary = (
        f"[sos:availability_cache] raw_responses={response_count} reusable_skus={len(values_by_sku)} "
        f"rows_updated={total_rows_updated} fields_updated={total_fields_updated}"
    )
    print(summary)
    if log_handle:
        log_handle.write(summary + "\n")
    return {
        "rows_updated": total_rows_updated,
        "fields_updated": total_fields_updated,
        "reusable_skus": len(values_by_sku),
    }


def print_summary(summary, label):
    print(f"[sos:{label}] run_root={summary['run_root']}")
    print(
        "[sos:{label}] batch_id={batch} final_rows={rows} main_unique={main} "
        "bsr={bsr} final_unique={final_unique} needs_more_main={needs_more}".format(
            label=label,
            batch=summary.get("batch_id") or "",
            rows=summary.get("final_rows"),
            main=summary.get("main_unique_count"),
            bsr=summary.get("bsr_count"),
            final_unique=summary.get("final_unique_sku_count"),
            needs_more=str(summary.get("needs_more_main_candidates")).lower(),
        )
    )
    if summary["main_failed_pages"]:
        pages = ", ".join(f"{item['page']}:{item['status_code']}" for item in summary["main_failed_pages"])
        print(f"[sos:{label}] main_failed_pages={pages}")
    if summary["bsr_failed_pages"]:
        pages = ", ".join(f"{item['page']}:{item['status_code']}" for item in summary["bsr_failed_pages"])
        print(f"[sos:{label}] bsr_failed_pages={pages}")
    if summary["detail_failure_count"]:
        print(
            f"[sos:{label}] detail_failures={summary['detail_failure_count']} "
            f"by_stage={summary['detail_failure_stages']}"
        )
    if summary["retailer_sku_name_nulls"] or summary["final_sku_price_nulls"]:
        print(
            f"[sos:{label}] nulls retailer_sku_name={summary['retailer_sku_name_nulls']} "
            f"final_sku_price={summary['final_sku_price_nulls']}"
        )


def base_env(category, run_root, batch_id, preserve_table_env=False):
    env = os.environ.copy()
    if not preserve_table_env:
        for key in TABLE_ENV_KEYS:
            env.pop(key, None)
    env.update(
        {
            "BESTBUY_CATEGORY": category,
            "BESTBUY_RUN_ROOT": str(run_root),
            "BESTBUY_BATCH_ID": batch_id,
            "BESTBUY_FETCH_MODE": "browser_graphql",
            "BESTBUY_GRAPHQL_FETCH_MODE": "browser_graphql",
            "BESTBUY_DETAIL_FETCH_MODE": "browser_graphql",
            "BESTBUY_FORCE_RUN_PATH_ENV": "1",
            "BESTBUY_FORCE_STEP_ENV": "1",
            "BESTBUY_FETCH_SPONSORED_ENRICHMENT": "0",
            "BESTBUY_DB_UPDATE_SIMILAR_ONLY": "0",
            "BESTBUY_DB_UPDATE_AVAILABILITY_ONLY": "0",
            "BESTBUY_DB_ROW_UPSERT_ONLY": "1",
            "BESTBUY_DB_ROW_UPSERT_NONBLANK_ONLY": "1",
            "BESTBUY_DB_ROW_UPSERT_ALLOW_ALL": "0",
            "BESTBUY_DB_ROW_UPSERT_SKUS": "",
            "BESTBUY_DB_ROW_UPSERT_ITEMS": "",
            "BESTBUY_DB_LOAD_DRY_RUN": "0",
            "BESTBUY_DB_PREPARE_ADD_MISSING_COLUMNS": "1",
            "BESTBUY_S3_SYNC_SKIP": "1",
            "BESTBUY_LOCAL_CLEANUP_SKIP": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    apply_run_path_env(env)
    env["BESTBUY_AVAILABILITY_BACKFILL_BATCH_ID"] = batch_id
    return env


def step_env(step, category, base, refresh_all_availability=False):
    env = base.copy()
    env.update(step.env)
    if step.name in {"main_list", "bsr_list"} and category in CATEGORY_SEARCH_TERMS:
        env["BESTBUY_SEARCH_TERM"] = CATEGORY_SEARCH_TERMS[category]
    if step.name == "trending_deals" and category == "HHP":
        env.update(HHP_TRENDING_PAGE_PAYLOAD_ENV)
    if step.name in {"main_list", "bsr_list"}:
        env.update(
            {
                "BESTBUY_SANITIZE_PRODUCT_LIST_QUERY": "0",
                "BESTBUY_STRIP_PRODUCT_LIST_FULFILLMENT": "0",
                "BESTBUY_LISTING_COLLECTION_MODE": "browser_graphql",
                "BESTBUY_BROWSER_GRAPHQL_WAIT_SECONDS": "8",
                "BESTBUY_BROWSER_GRAPHQL_JS_TIMEOUT": "120",
                "BESTBUY_BROWSER_GRAPHQL_HEADLESS": "0",
                "BESTBUY_BROWSER_GRAPHQL_NAVIGATE_EACH_PAGE": "0",
                "BESTBUY_GRAPHQL_MODE_AUTO": "0",
                "BESTBUY_LISTING_SESSION_ENABLED": "0",
                "BESTBUY_LISTING_SESSION_BOOTSTRAP": "0",
                "BESTBUY_LISTING_SESSION_MAX_AGE_SECONDS": "480",
                "BESTBUY_LISTING_MAX_ATTEMPTS": "5",
                "BESTBUY_LISTING_RETRY_SLEEP_SECONDS": "2",
                "BESTBUY_LISTING_RETRY_MAX_SLEEP_SECONDS": "8",
                "BESTBUY_LISTING_RETRY_STATUS_CODES": "408,425,429,500,502,503,504",
            }
        )
    if step.name == "detail_html":
        env.update(
            {
                "BESTBUY_DETAIL_RETRY_ONLY": "0",
                "BESTBUY_DETAIL_REBUILD_ONLY": "0",
                "BESTBUY_DETAIL_AUTO_RETRY": "0",
                "BESTBUY_DETAIL_MAX_ATTEMPTS": "1",
                "BESTBUY_DETAIL_SKU_BATCH_SIZE": "5",
                "BESTBUY_DETAIL_SKU_BATCH_REFILL": "0",
                "BESTBUY_DETAIL_SKU_BATCH_REFILL_SINGLE_FALLBACK": "0",
                "BESTBUY_DETAIL_WORKERS": "1",
                "BESTBUY_DETAIL_FETCH_MODE": "browser_graphql",
                "BESTBUY_DETAIL_BROWSER_GRAPHQL_WAIT_SECONDS": "8",
                "BESTBUY_DETAIL_BROWSER_GRAPHQL_JS_TIMEOUT": "120",
                "BESTBUY_DETAIL_BROWSER_GRAPHQL_HEADLESS": "0",
                "BESTBUY_DETAIL_RETRY_STATUS_CODES": "408,409,422,425,429,500,502,503,504",
                "BESTBUY_DETAIL_RETRY_SLEEP_SECONDS": "0",
            }
        )
        if compact(base.get("BESTBUY_DETAIL_SKUS")):
            env["BESTBUY_DETAIL_SKUS"] = compact(base.get("BESTBUY_DETAIL_SKUS"))
    if step.name == "review20":
        env.update(
            {
                "BESTBUY_DETAIL_RETRY_ONLY": "1",
                "BESTBUY_DETAIL_REBUILD_ONLY": "0",
                "BESTBUY_DETAIL_AUTO_RETRY": "0",
                "BESTBUY_DETAIL_MAX_ATTEMPTS": "1",
                "BESTBUY_DETAIL_SKU_BATCH_REFILL": "0",
                "BESTBUY_DETAIL_SKU_BATCH_REFILL_SINGLE_FALLBACK": "0",
                "BESTBUY_DETAIL_WORKERS": "1",
                "BESTBUY_DETAIL_FETCH_MODE": "browser_graphql",
                "BESTBUY_DETAIL_BROWSER_GRAPHQL_WAIT_SECONDS": "8",
                "BESTBUY_DETAIL_BROWSER_GRAPHQL_JS_TIMEOUT": "120",
                "BESTBUY_DETAIL_BROWSER_GRAPHQL_HEADLESS": "0",
                "BESTBUY_REVIEW20_BATCH_SIZE": "5",
                "BESTBUY_REVIEW20_BATCH_SINGLE_FALLBACK": "1",
                "BESTBUY_DETAIL_RETRY_STATUS_CODES": "408,409,422,425,429,500,502,503,504",
                "BESTBUY_DETAIL_RETRY_SLEEP_SECONDS": "0",
            }
        )
    if step.name == "availability_backfill":
        candidate_mode = "all_rows" if refresh_all_availability else "blank_all"
        env.update(
            {
                "BESTBUY_AVAILABILITY_BACKFILL_CHUNK_SIZE": "5",
                "BESTBUY_AVAILABILITY_BACKFILL_ALLOW_MULTI_SKU": "1",
                "BESTBUY_AVAILABILITY_BACKFILL_SINGLE_SKU_FALLBACK": "1",
                "BESTBUY_AVAILABILITY_BACKFILL_FETCH_MODE": "browser_graphql",
                "BESTBUY_AVAILABILITY_BROWSER_GRAPHQL_WAIT_SECONDS": "5",
                "BESTBUY_AVAILABILITY_BROWSER_GRAPHQL_JS_TIMEOUT": "120",
                "BESTBUY_AVAILABILITY_BROWSER_GRAPHQL_HEADLESS": "0",
                "BESTBUY_AVAILABILITY_BACKFILL_CANDIDATE_MODE": candidate_mode,
                "BESTBUY_AVAILABILITY_BACKFILL_OVERWRITE": "1",
                "BESTBUY_AVAILABILITY_BACKFILL_CLEAR_EXISTING_FIELDS": "0",
                "BESTBUY_AVAILABILITY_BACKFILL_SKIP": "0",
                "BESTBUY_AVAILABILITY_BACKFILL_LIMIT": "0",
                "BESTBUY_AVAILABILITY_BACKFILL_TIMEOUT": "180",
                "ZENROWS_TIMEOUT": "180",
            }
        )
    apply_run_path_env(env)
    env["BESTBUY_AVAILABILITY_BACKFILL_BATCH_ID"] = base["BESTBUY_BATCH_ID"]
    return env


def should_skip_join_step(step, category):
    if step.name == "promotion_deals":
        return category == "HHP" or not has_target_url("promotion")
    if step.name == "trending_deals":
        return not has_target_url("trend")
    return False


def run_command(command, env, log_handle):
    print(f"[sos:run] {' '.join(command)}")
    log_handle.write(f"[sos:run] {' '.join(command)}\n")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert process.stdout is not None
    for line in process.stdout:
        echo_process_line(line)
        log_handle.write(line)
    return process.wait()


def echo_process_line(line):
    try:
        print(line, end="")
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        safe_line = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe_line, end="")


def run_step(step, category, base, log_handle, dry_run=False, refresh_all_availability=False):
    if should_skip_join_step(step, category):
        print(f"[sos:skip] step {step.key} {step.name}: not applicable for {category}")
        log_handle.write(f"[sos:skip] step {step.key} {step.name}: not applicable for {category}\n")
        return
    env = step_env(step, category, base, refresh_all_availability=refresh_all_availability)
    command = [PYTHON, "-m", step.module]
    interesting_env = {key: env[key] for key in sorted(step.env) if key in env}
    if step.name in {"main_list", "bsr_list", "detail_html", "review20", "availability_backfill"}:
        interesting_env.update(
            {
                key: env[key]
                for key in sorted(env)
                if key.startswith("BESTBUY_LISTING_RETRY")
                or key.startswith("BESTBUY_DETAIL_RETRY")
                or key
                in {
                    "BESTBUY_LISTING_MAX_ATTEMPTS",
                    "BESTBUY_DETAIL_MAX_ATTEMPTS",
                    "BESTBUY_DETAIL_SKU_BATCH_SIZE",
                }
            }
        )
    print(f"[sos:step] {step.key} {step.name}")
    if interesting_env:
        print("[sos:env] " + " ".join(f"{key}={value}" for key, value in interesting_env.items()))
    if dry_run:
        return
    returncode = run_command(command, env, log_handle)
    if returncode:
        raise StepFailure(step, returncode)


def notify(category, run_root, status, log_path, failed_step=None):
    env = os.environ.copy()
    env.update(
        {
            "BESTBUY_CATEGORY": category,
            "BESTBUY_RUN_ROOT": str(run_root),
            "BESTBUY_OUTPUT_ROOT": str(run_root / "output"),
            "BESTBUY_FINAL_OUTPUT_CSV": str(run_root / "output" / "final_output.csv"),
            "BESTBUY_NOTIFY_STATUS": status,
            "BESTBUY_NOTIFY_LOG": str(log_path),
        }
    )
    if failed_step:
        env["BESTBUY_NOTIFY_FAILED_STEP"] = failed_step.key
        env["BESTBUY_NOTIFY_FAILED_STEP_NAME"] = failed_step.name
    subprocess.run([PYTHON, "-m", "bestbuy.step16_email_notify"], check=False, env=env)


def selected_steps(refresh_join_sources=False, no_db_load=False):
    names = DEFAULT_STEP_NAMES[:]
    if refresh_join_sources:
        insert_at = names.index("final_targets")
        names[insert_at:insert_at] = JOIN_STEP_NAMES
    if no_db_load:
        names = [name for name in names if name not in {"db_prepare", "db_load", "item_mst_load"}]
    return [step_by_name(name) for name in names]


def db_safety_check(before, run_root, allow_detail_failures=False):
    after = current_summary(run_root)
    issues = []
    final_rows = read_csv_rows(run_root / "output" / "final_output.csv")
    if after["final_rows"] < before["final_rows"]:
        issues.append(f"final_rows decreased {after['final_rows']}/{before['final_rows']}")
    if after["final_unique_sku_count"] < before["final_unique_sku_count"]:
        issues.append(
            f"final_unique_sku_count decreased {after['final_unique_sku_count']}/{before['final_unique_sku_count']}"
        )
    if after["main_unique_count"] < before["main_unique_count"]:
        issues.append(f"main_unique_count decreased {after['main_unique_count']}/{before['main_unique_count']}")
    if after["bsr_count"] < before["bsr_count"]:
        issues.append(f"bsr_count decreased {after['bsr_count']}/{before['bsr_count']}")
    if after["main_failed_pages"]:
        pages = ",".join(str(item["page"]) for item in after["main_failed_pages"])
        issues.append(f"main listing still has failed/empty pages: {pages}")
    if after["bsr_failed_pages"]:
        pages = ",".join(str(item["page"]) for item in after["bsr_failed_pages"])
        issues.append(f"bsr listing still has failed/empty pages: {pages}")
    if not allow_detail_failures and after["detail_failure_count"]:
        issues.append(f"detail failures remain: {after['detail_failure_count']}")
    if after["retailer_sku_name_nulls"] > before["retailer_sku_name_nulls"]:
        issues.append(
            "retailer_sku_name nulls increased "
            f"{after['retailer_sku_name_nulls']}/{before['retailer_sku_name_nulls']}"
        )
    if after["final_sku_price_nulls"] > before["final_sku_price_nulls"]:
        issues.append(
            f"final_sku_price nulls increased {after['final_sku_price_nulls']}/{before['final_sku_price_nulls']}"
        )
    for field in ("main_rank", "bsr_rank"):
        duplicates = duplicate_numeric_values(final_rows, field)
        if duplicates:
            preview = ",".join(str(value) for value in duplicates[:10])
            issues.append(f"{field} duplicate values: {preview}")
    expected_rank_ranges = {"main_rank": 300, "bsr_rank": 100}
    for field, expected_max in expected_rank_ranges.items():
        missing = missing_numeric_rank_values(final_rows, field, expected_max)
        if missing:
            preview = ",".join(str(value) for value in missing[:20])
            issues.append(f"{field} missing values: {preview}")
    if issues:
        issue_text = "; ".join(issues)
        raise RuntimeError(f"SOS safety check blocked DB load: {issue_text}")
    return after


def duplicate_numeric_values(rows, field):
    counts = Counter()
    for row in rows:
        value = int_value(row.get(field))
        if value > 0:
            counts[value] += 1
    return sorted(value for value, count in counts.items() if count > 1)


def missing_numeric_rank_values(rows, field, expected_max):
    present = set()
    for row in rows:
        value = int_value(row.get(field))
        if 1 <= value <= expected_max:
            present.add(value)
    return [value for value in range(1, expected_max + 1) if value not in present]


def parse_args():
    parser = argparse.ArgumentParser(description="BestBuy SOS refill for an incomplete existing run folder")
    parser.add_argument("--category", default=os.getenv("BESTBUY_CATEGORY", bestbuy_category()), help="TV, HHP, REF, LDY")
    parser.add_argument(
        "--run-root",
        default=os.getenv("BESTBUY_RUN_ROOT", str(DEFAULT_BESTBUY_RUN_ROOT)),
        help="Existing run root to refill, e.g. C:\\...\\bestbuy\\data\\tv\\20260604",
    )
    parser.add_argument("--batch-id", default=os.getenv("BESTBUY_BATCH_ID", ""), help="Existing batch_id to replace")
    parser.add_argument("--refresh-join-sources", action="store_true", help="Also refetch promotion/trending sources")
    parser.add_argument(
        "--refresh-all-availability",
        action="store_true",
        help="Refetch availability for every row. Default only refills rows whose active availability fields are all blank.",
    )
    parser.add_argument("--no-db-load", action="store_true", help="Stop before DB prepare/load/item_mst")
    parser.add_argument("--no-notify", action="store_true", help="Do not send the existing BBY email notification")
    parser.add_argument("--analysis-only", action="store_true", help="Only print current run summary")
    parser.add_argument("--dry-run", action="store_true", help="Print selected commands without network/DB execution")
    parser.add_argument(
        "--allow-detail-failures",
        action="store_true",
        help="Allow DB load even when detail_failures.csv still has rows.",
    )
    parser.add_argument(
        "--preserve-table-env",
        action="store_true",
        help="Keep BESTBUY_OUTPUT_TABLE/BESTBUY_PRODUCT_LIST_TABLE env overrides instead of clearing them",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    category = compact(args.category).upper()
    run_root = Path(args.run_root)
    batch_id = compact(args.batch_id) or infer_batch_id(run_root)
    if not category:
        raise RuntimeError("category is required")
    if not run_root.exists():
        raise RuntimeError(f"run_root does not exist: {run_root}")
    if not batch_id and not args.analysis_only:
        raise RuntimeError("batch_id is required and could not be inferred from output/final_output.csv")

    before = current_summary(run_root)
    existing_rows = read_csv_rows(run_root / "output" / "final_output.csv")
    existing_product_rows = read_csv_rows(run_root / "output" / "bestbuy_product_list.csv")
    print_summary(before, "before")
    if args.analysis_only:
        return

    log_dir = run_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"sos_refill_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    base = base_env(category, run_root, batch_id, preserve_table_env=args.preserve_table_env)

    failed_step = None
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log_handle:
            log_handle.write(f"BestBuy SOS refill started category={category} batch_id={batch_id}\n")
            log_handle.write(f"run_root={run_root}\n")
            for step in selected_steps(args.refresh_join_sources, args.no_db_load):
                if step.name == "detail_html":
                    scoped_skus = detail_refill_skus(existing_rows, run_root)
                    target_rows_for_scope = read_csv_rows(run_root / "output" / "bestbuy_final_targets.csv")
                    scoped_sku_set = set(scoped_skus)
                    scoped_items = [
                        compact(row.get("item") or row.get("bsin"))
                        for row in target_rows_for_scope
                        if compact(row.get("sku_id")) in scoped_sku_set and compact(row.get("item") or row.get("bsin"))
                    ]
                    base["BESTBUY_DETAIL_SKUS"] = ",".join(scoped_skus)
                    base["BESTBUY_DB_ROW_UPSERT_SKUS"] = ",".join(scoped_skus)
                    base["BESTBUY_DB_ROW_UPSERT_ITEMS"] = ",".join(dict.fromkeys(scoped_items))
                    scope_message = f"[sos:scope] detail_skus={len(scoped_skus)}"
                    print(scope_message)
                    log_handle.write(scope_message + "\n")
                if step.name == "db_prepare":
                    if not args.dry_run:
                        merge_existing_nonblank_values(
                            existing_rows,
                            run_root / "output" / "final_output.csv",
                            "final_output",
                            log_handle,
                        )
                        merge_existing_nonblank_values(
                            existing_product_rows,
                            run_root / "output" / "bestbuy_product_list.csv",
                            "product_list",
                            log_handle,
                        )
                    checked = db_safety_check(before, run_root, allow_detail_failures=args.allow_detail_failures)
                    print_summary(checked, "pre_db")
                if step.name == "availability_backfill" and not args.dry_run:
                    apply_cached_availability_values(run_root, log_handle)
                run_step(
                    step,
                    category,
                    base,
                    log_handle,
                    dry_run=args.dry_run,
                    refresh_all_availability=args.refresh_all_availability,
                )
                if step.name == "review20":
                    if not args.dry_run:
                        merge_existing_nonblank_values(
                            existing_rows,
                            run_root / "output" / "final_output.csv",
                            "final_output",
                            log_handle,
                        )
                        merge_existing_nonblank_values(
                            existing_product_rows,
                            run_root / "output" / "bestbuy_product_list.csv",
                            "product_list",
                            log_handle,
                        )
            log_handle.write("BestBuy SOS refill completed\n")
    except StepFailure as exc:
        failed_step = exc.step
        if not args.no_notify and not args.dry_run:
            notify(category, run_root, "failed", log_path, failed_step)
        raise

    after = current_summary(run_root)
    print_summary(after, "after")
    print(f"[sos:done] log={log_path}")
    if not args.no_notify and not args.dry_run:
        notify(category, run_root, "success", log_path, failed_step)


if __name__ == "__main__":
    main()
