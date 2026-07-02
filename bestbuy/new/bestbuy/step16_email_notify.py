import csv
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from .step00_config import DEFAULT_BESTBUY_RUN_ROOT, KRW_PER_USD, bestbuy_category, rel_path


CATEGORY = bestbuy_category()
RUN_ROOT = Path(os.getenv("BESTBUY_RUN_ROOT", DEFAULT_BESTBUY_RUN_ROOT))
OUTPUT_ROOT = Path(os.getenv("BESTBUY_OUTPUT_ROOT", RUN_ROOT / "output"))
FINAL_OUTPUT_CSV = Path(os.getenv("BESTBUY_FINAL_OUTPUT_CSV", OUTPUT_ROOT / "final_output.csv"))
MANIFEST_PATH = OUTPUT_ROOT / "email_notify_manifest.json"

CRITICAL_COLUMN_ALIASES = {
    "retailer_sku_name": ["retailer_sku_name"],
    "sku": ["sku", "sku_id", "item"],
    "final_sku_price": ["final_sku_price"],
}

EXCLUDED_NULL_COLUMNS = {
    "TV": frozenset({
        "sku_popularity",
        "discount_type",
        "shipping_info",
        "available_quantity_for_purchase",
        "inventory_status",
        "retailer_membership_discounts",
        "summarized_review_content",
        "top_mentions",
        "rank_1",
        "rank_2",
        "number_of_ppl_purchased_yesterday",
        "number_of_ppl_added_to_carts",
        "number_of_units_purchased_past_month",
        "redirect",
    }),
    "HHP": frozenset(),
    "REF": frozenset(),
    "LDY": frozenset(),
}

COLLECTED_COUNT_WARNING_MIN = {
    "REF": 300,
    "LDY": 250,
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def truthy(value, default=False):
    text = str(value if value is not None else ("1" if default else "0")).strip().lower()
    return text in {"1", "true", "yes", "y"}


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


def blank(value):
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text.lower() in {"null", "[null]", "none", "nan"}


def as_float(value):
    if value in ("", None):
        return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return 0.0


def as_int(value):
    if value in ("", None):
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except ValueError:
        return 0


def money_krw(value):
    return f"{int(round(float(value or 0))):,}원"


def numeric_rank(value):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 0


def unique_csv_count(path, key):
    seen = set()
    for row in read_csv_rows(path):
        value = str(row.get(key) or "").strip()
        if value:
            seen.add(value)
    return len(seen)


def latest_manifest_cost(root):
    manifests = sorted(Path(root).glob("*/manifest.json"), key=lambda item: item.stat().st_mtime)
    total = 0.0
    for path in manifests:
        data = read_json(path)
        key = "total_x_request_cost" if data.get("total_x_request_cost") not in ("", None) else "x_request_cost"
        total += as_float(data.get(key))
    return total


STANDARD_ZENROWS_CALL_COST_USD = 0.0027996


def derive_per_call_cost(run_root):
    main = read_json(run_root / "main" / "manifest.json")
    calls = as_int(
        main.get("listing_request_calls")
        or main.get("total_request_calls")
        or main.get("actual_post_calls")
    )
    cost = as_float(main.get("total_x_request_cost"))
    if calls and cost:
        return cost / calls
    return STANDARD_ZENROWS_CALL_COST_USD


def estimate_calls_from_cost(cost_usd, per_call_cost):
    if cost_usd <= 0 or per_call_cost <= 0:
        return 0
    return int(round(cost_usd / per_call_cost))


DETAIL_STAGE_DIRS = {
    "detail": "detail_html",
    "review": "review20",
    "compare": "compare",
}


def detail_meta_paths(run_root, folder):
    raw_root = run_root / "detail" / "raw" / folder
    if not raw_root.exists():
        return []
    return [
        path
        for path in raw_root.rglob("*_meta.json")
        if not path.name.endswith("_fulfillment_meta.json")
    ]


def raw_stage_call_summary(run_root, folder, per_call_cost):
    batch_calls = 0
    batch_cost = 0.0
    distributed_cost = 0.0
    positive_cost_meta_count = 0
    for path in detail_meta_paths(run_root, folder):
        meta = read_json(path)
        if not meta or meta.get("success") is not True or meta.get("transport") == "none":
            continue
        if meta.get("fetched_this_run") is False:
            continue
        per_meta_cost = as_float(meta.get("x_request_cost_total") or meta.get("x_request_cost"))
        distributed_cost += per_meta_cost
        if per_meta_cost:
            positive_cost_meta_count += 1
        one_batch_cost = as_float(meta.get("batch_x_request_cost"))
        if one_batch_cost and as_int(meta.get("sku_batch_index")) == 1:
            batch_calls += 1
            batch_cost += one_batch_cost
    if batch_calls:
        return {"calls": batch_calls, "cost_usd": round(batch_cost, 7), "source": "raw_batch_meta"}
    if distributed_cost:
        return {
            "calls": estimate_calls_from_cost(distributed_cost, per_call_cost),
            "cost_usd": round(distributed_cost, 7),
            "source": "raw_meta_cost",
        }
    return {
        "calls": positive_cost_meta_count,
        "cost_usd": 0.0,
        "source": "raw_meta_count",
    }


def raw_detail_call_summary(run_root, per_call_cost):
    summary = {}
    for stage, folder in DETAIL_STAGE_DIRS.items():
        summary[stage] = raw_stage_call_summary(run_root, folder, per_call_cost)
    return summary


def manifest_call_counts(run_root):
    per_call_cost = derive_per_call_cost(run_root)
    listing_breakdown = []
    listing_total = 0

    for sub in ("main", "bsr"):
        data = read_json(run_root / sub / "manifest.json")
        if not data:
            continue
        calls = as_int(
            data.get("listing_request_calls")
            or data.get("total_request_calls")
            or data.get("actual_post_calls")
        )
        if calls:
            listing_total += calls
            listing_breakdown.append({"source": sub, "calls": calls})

    sponsored = read_json(run_root / "main" / "manifest_main_targets.json")
    if sponsored:
        calls = as_int(sponsored.get("sponsored_call_count"))
        if calls:
            listing_total += calls
            listing_breakdown.append({"source": "sponsored", "calls": calls})

    promotion = read_json(run_root / "promotion" / "summary.json")
    if promotion:
        calls = as_int(promotion.get("call_count"))
        if calls:
            listing_total += calls
            listing_breakdown.append({"source": "promotion", "calls": calls})

    trending_calls = 0
    for path in sorted((run_root / "trending").glob("summary*.json")):
        data = read_json(path)
        if not data or data.get("skipped"):
            continue
        calls = as_int(data.get("call_count"))
        if calls:
            trending_calls += calls
        else:
            attempt = as_int(data.get("attempt"))
            if attempt:
                trending_calls += attempt
            elif as_float(data.get("x_request_cost")) or as_float(data.get("total_x_request_cost")):
                trending_calls += 1
    if trending_calls:
        listing_total += trending_calls
        listing_breakdown.append({"source": "trending", "calls": trending_calls})

    detail_manifest = read_json(run_root / "detail" / "manifest_detail_enrichment.json")
    detail_total = 0
    detail_breakdown = []
    runs_by_stage = detail_manifest.get("runs_by_stage") if detail_manifest else None
    if isinstance(runs_by_stage, dict) and runs_by_stage:
        # Actual ZenRows POST counts, summed across stage runs (step08 detail batch + step09 review).
        # One batch POST bundles detail+review+compare and is counted once as detail_calls.
        stage_calls = {"detail": 0, "review": 0, "compare": 0}
        for entry in runs_by_stage.values():
            for stage in stage_calls:
                stage_calls[stage] += as_int((entry or {}).get(f"{stage}_calls"))
        for stage in ("detail", "review", "compare"):
            calls = stage_calls[stage]
            if calls:
                detail_total += calls
                detail_breakdown.append({"stage": stage, "calls": calls})
    if detail_total == 0 and detail_manifest:
        # Back-compat: older manifests have no per-stage call counts, estimate from cost.
        for stage in ("detail", "review", "compare"):
            cost = as_float(detail_manifest.get(f"{stage}_cost_usd_this_run"))
            calls = estimate_calls_from_cost(cost, per_call_cost)
            if calls:
                detail_total += calls
                detail_breakdown.append({"stage": stage, "calls": calls, "cost_usd": cost})
    if detail_total == 0:
        raw_summary = raw_detail_call_summary(run_root, per_call_cost)
        for stage in ("detail", "review", "compare"):
            calls = as_int(raw_summary.get(stage, {}).get("calls"))
            if calls:
                detail_total += calls
                detail_breakdown.append(
                    {
                        "stage": stage,
                        "calls": calls,
                        "cost_usd": raw_summary[stage].get("cost_usd", 0),
                        "source": raw_summary[stage].get("source", ""),
                    }
                )

    availability_total = 0
    for path in sorted((run_root / "availability_backfill").glob("*/manifest.json")):
        data = read_json(path)
        if not data:
            continue
        availability_total += as_int(data.get("call_count"))

    total = listing_total + detail_total + availability_total
    return {
        "total": total,
        "per_call_cost_usd": round(per_call_cost, 7),
        "listing": listing_total,
        "listing_breakdown": listing_breakdown,
        "detail": detail_total,
        "detail_breakdown": detail_breakdown,
        "availability": availability_total,
    }


def manifest_costs(run_root):
    paths_and_keys = [
        (run_root / "main" / "manifest.json", ["total_x_request_cost"]),
        (run_root / "bsr" / "manifest.json", ["total_x_request_cost"]),
        (run_root / "main" / "manifest_main_targets.json", ["sponsored_cost_usd"]),
        (run_root / "promotion" / "summary.json", ["total_x_request_cost"]),
    ]
    total = 0.0
    sources = []
    for path, keys in paths_and_keys:
        data = read_json(path)
        if not data:
            continue
        for key in keys:
            cost = as_float(data.get(key))
            if cost:
                total += cost
                sources.append({"path": rel_path(path), "key": key, "cost_usd": cost})
                break

    # detail/review are written across separate step08 runs into one manifest; the later
    # (review) run overwrites total_cost_usd_this_run. Sum per-stage cost to avoid undercount.
    detail_path = run_root / "detail" / "manifest_detail_enrichment.json"
    detail_data = read_json(detail_path)
    detail_runs = detail_data.get("runs_by_stage") if detail_data else None
    detail_cost_added = False
    if isinstance(detail_runs, dict) and detail_runs:
        detail_cost = 0.0
        for entry in detail_runs.values():
            for key in ("detail_cost_usd", "review_cost_usd", "compare_cost_usd"):
                detail_cost += as_float((entry or {}).get(key))
        if detail_cost:
            total += detail_cost
            detail_cost_added = True
            sources.append({"path": rel_path(detail_path), "key": "runs_by_stage", "cost_usd": round(detail_cost, 7)})
    elif detail_data:
        cost = as_float(detail_data.get("total_cost_usd_this_run"))
        if cost:
            total += cost
            detail_cost_added = True
            sources.append({"path": rel_path(detail_path), "key": "total_cost_usd_this_run", "cost_usd": cost})
    if not detail_cost_added:
        raw_summary = raw_detail_call_summary(run_root, derive_per_call_cost(run_root))
        raw_cost = round(sum(as_float(item.get("cost_usd")) for item in raw_summary.values()), 7)
        if raw_cost:
            total += raw_cost
            sources.append({"path": rel_path(run_root / "detail" / "raw"), "key": "raw_meta_cost", "cost_usd": raw_cost})

    for path in sorted((run_root / "trending").glob("summary*.json")):
        data = read_json(path)
        key = "total_x_request_cost" if data.get("total_x_request_cost") not in ("", None) else "x_request_cost"
        cost = as_float(data.get(key))
        if cost:
            total += cost
            sources.append({"path": rel_path(path), "key": key, "cost_usd": cost})

    availability_cost = latest_manifest_cost(run_root / "availability_backfill")
    if availability_cost:
        total += availability_cost
        sources.append(
            {
                "path": rel_path(run_root / "availability_backfill"),
                "key": "x_request_cost",
                "cost_usd": availability_cost,
            }
        )
    return round(total, 7), sources


def db_manifest(run_root):
    return read_json(run_root / "output" / "db_load_manifest.json")


def final_targets_manifest(run_root):
    return read_json(run_root / "output" / "bestbuy_final_targets.manifest.json")


def insert_columns(db_data, rows):
    columns = list((db_data.get("final_output") or {}).get("columns") or [])
    if columns:
        return [column for column in columns if column != "id"]
    if rows:
        return [column for column in rows[0].keys() if column != "id"]
    return []


def row_url(row):
    return str(row.get("product_url") or row.get("url") or "").strip()


def review_text_count(value):
    return len(re.findall(r"(?i)(?:^|\s)review\d+\s*-", str(value or "")))


def sku_id_from_row(row):
    text = " ".join([
        str(row.get("sku_id") or ""),
        str(row.get("sku") or ""),
        row_url(row),
    ])
    match = re.search(r"(?:skuId=|/sku/|/)(\d{5,})(?:[^\d]|$)", text)
    return match.group(1) if match else ""


def review_metrics_from_response(data):
    product = ((data.get("data") or {}).get("productBySkuId") or {}) if isinstance(data, dict) else {}
    review_info = product.get("reviewInfo") if isinstance(product, dict) else {}
    reviews = ((product.get("reviews") or {}).get("results") or []) if isinstance(product, dict) else []
    reviews = reviews if isinstance(reviews, list) else []
    return {
        "review_page_count": as_int((review_info or {}).get("reviewCount")),
        "review_rows": len(reviews),
        "review_text_count": sum(1 for review in reviews[:20] if str((review or {}).get("text") or "").strip()),
    }


def review_metrics_by_sku(run_root):
    raw_root = Path(run_root) / "detail" / "raw" / "review20"
    if not raw_root.exists():
        return {}
    metrics = {}
    for path in raw_root.rglob("*_response.json"):
        sku = path.name[: -len("_response.json")]
        current = review_metrics_from_response(read_json(path))
        if not sku or not current.get("review_page_count"):
            continue
        previous = metrics.get(sku)
        if not previous or current.get("review_text_count", 0) >= previous.get("review_text_count", 0):
            metrics[sku] = current
    return metrics


def review_completeness_issues(rows, run_root=None):
    metrics_by_sku = review_metrics_by_sku(run_root) if run_root else {}
    bad = []
    diffs = []
    for row in rows:
        sku_id = sku_id_from_row(row)
        metrics = metrics_by_sku.get(sku_id) or {}
        listing_count = as_int(row.get("count_of_reviews"))
        review_page_count = as_int(metrics.get("review_page_count")) or listing_count
        expected = min(review_page_count, 20)
        if expected <= 0:
            continue
        actual = review_text_count(row.get("detailed_review_content") or row.get("detailed_review_contents"))
        if actual < expected:
            bad.append((row, actual, expected))
        elif listing_count and review_page_count and listing_count != review_page_count:
            diffs.append((row, listing_count, review_page_count, actual, as_int(metrics.get("review_rows"))))
    issues = []
    notes = []
    if bad:
        examples = []
        for row, actual, expected in bad:
            url = row_url(row)
            item = str(row.get("item") or row.get("sku_id") or "").strip()
            label = f"{item} {actual}/{expected}".strip()
            if url:
                examples.append(f"{label} {url}".strip())
            else:
                examples.append(label)
        suffix = f", product_url: {' | '.join(examples)}" if examples else ""
        issues.append(f"detailed_review_content {len(bad)} rows partial{suffix}")
    if diffs:
        examples = []
        for row, listing_count, review_page_count, actual, review_rows in diffs:
            url = row_url(row)
            item = str(row.get("item") or row.get("sku_id") or "").strip()
            label = (
                f"{item} listing={listing_count} review_page={review_page_count} "
                f"content={actual} review_rows={review_rows}"
            ).strip()
            if url:
                examples.append(f"{label} {url}".strip())
            else:
                examples.append(label)
        notes.append(f"review count diff listing vs review page {len(diffs)} rows, product_url: {' | '.join(examples)}")
    return issues, notes


def first_present_column(rows, columns, candidates):
    available = set(columns)
    if rows:
        available.update(rows[0].keys())
    for candidate in candidates:
        if candidate in available:
            return candidate
    return ""


def all_null_column_issues(category, rows, columns):
    if not rows or not columns:
        return []
    excluded = EXCLUDED_NULL_COLUMNS.get(str(category or "").strip().upper(), frozenset())
    all_null = [
        column
        for column in columns
        if column != "id"
        and column not in excluded
        and all(blank(row.get(column)) for row in rows)
    ]
    if not all_null:
        return []
    return [f"전체 null 컬럼: {', '.join(all_null)}"]


def critical_null_issues(rows, columns):
    issues = []
    for logical_name, candidates in CRITICAL_COLUMN_ALIASES.items():
        column = first_present_column(rows, columns, candidates)
        if not column:
            issues.append(f"{logical_name} 컬럼이 final_output/db insert 대상에 없음")
            continue
        bad_rows = [row for row in rows if blank(row.get(column))]
        if not bad_rows:
            continue
        examples = []
        for row in bad_rows:
            url = row_url(row)
            item = str(row.get("item") or row.get("sku_id") or "").strip()
            if url:
                examples.append(f"{item} {url}".strip())
            elif item:
                examples.append(item)
        suffix = f", product_url: {' | '.join(examples)}" if examples else ""
        issues.append(f"{logical_name} {len(bad_rows)} rows null{suffix}")
    return issues


def db_count_issue(db_data, row_count):
    final = db_data.get("final_output") or {}
    expected = as_int(final.get("candidate_rows") or final.get("csv_rows") or row_count)
    inserted = as_int(final.get("inserted"))
    if inserted == 0 and "inserted" not in final:
        inserted = as_int(final.get("updated") or expected)
    if expected and inserted < expected:
        return f"DB insert rows 미달: {inserted}/{expected} success"
    return ""

def collected_count_issues(category, collected_count):
    minimum = COLLECTED_COUNT_WARNING_MIN.get(str(category or "").strip().upper())
    if minimum and as_int(collected_count) < minimum:
        return [f"collected_count {as_int(collected_count)}/{minimum}"]
    return []


def max_rank(rows, column):
    return max((numeric_rank(row.get(column)) for row in rows), default=0)


def rank_collection_counts(rows):
    return {
        "main_rank": len({numeric_rank(row.get("main_rank")) for row in rows if numeric_rank(row.get("main_rank")) > 0}),
        "bsr_rank": len({numeric_rank(row.get("bsr_rank")) for row in rows if numeric_rank(row.get("bsr_rank")) > 0}),
    }


def compact_ranges(values):
    values = sorted(set(values or []))
    if not values:
        return ""
    ranges = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(f"{start}" if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(f"{start}" if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)


def page_number_values(value):
    if isinstance(value, list):
        return [as_int(item) for item in value if as_int(item) > 0]
    if isinstance(value, str):
        return [as_int(item) for item in re.findall(r"\d+", value) if as_int(item) > 0]
    return []


def listing_expected_pages(manifest):
    return as_int(
        manifest.get("pages_requested")
        or manifest.get("search_pages")
        or manifest.get("page_count")
        or len(page_number_values(manifest.get("page_numbers")))
    )


def listing_fetch_issues_for_manifest(run_root, listing_id, label):
    manifest = read_json(run_root / listing_id / "manifest.json")
    if not manifest:
        return []
    issues = []
    expected_pages = listing_expected_pages(manifest)
    failed_page_source = (
        manifest.get("listing_recovery_still_failed_pages")
        if "listing_recovery_still_failed_pages" in manifest
        else manifest.get("failed_pages")
    )
    failed_pages = page_number_values(failed_page_source)
    if expected_pages:
        failed_pages = [page for page in failed_pages if page <= expected_pages]
    if failed_pages:
        issues.append(f"{label} listing failed pages {compact_ranges(failed_pages)}")

    page_count = as_int(manifest.get("page_count"))
    if expected_pages and page_count and page_count < expected_pages:
        issues.append(f"{label} listing pages {page_count}/{expected_pages}")
    return issues


def listing_fetch_issues(run_root):
    issues = []
    issues.extend(listing_fetch_issues_for_manifest(run_root, "main", "main"))
    issues.extend(listing_fetch_issues_for_manifest(run_root, "bsr", "bsr"))
    return issues


def collection_failed_issue(label, summary):
    if not summary or not truthy(summary.get("collection_failed")):
        return ""
    error_type = str(summary.get("error_type") or "").strip()
    error = " ".join(str(summary.get("error") or "").split())
    if len(error) > 180:
        error = error[:177].rstrip() + "..."
    detail = " ".join(part for part in [error_type, error] if part).strip()
    return f"{label} collection skipped" + (f": {detail}" if detail else "")


def listing_count_issues(category, run_root, rows, target_manifest):
    issues = []
    bsr_rank = max_rank(rows, "bsr_rank")
    if bsr_rank and bsr_rank < 100:
        issues.append(f"bsr_rank {bsr_rank}/100")

    category = category.upper()
    if category in {"TV", "HHP"}:
        for path in sorted((run_root / "trending").glob("summary*.json")):
            issue = collection_failed_issue("trending", read_json(path))
            if issue:
                issues.append(issue)
                break
        trend_count = as_int(target_manifest.get("trending_unique_count"))
        if not trend_count:
            trend_count = unique_csv_count(run_root / "trending" / "parsed" / "trending_products.csv", "sku_id")
        if trend_count < 10:
            issues.append(f"trend listing sku {trend_count}/10")

    if category == "TV":
        issue = collection_failed_issue("promotion", read_json(run_root / "promotion" / "summary.json"))
        if issue:
            issues.append(issue)
        promotion_count = as_int(target_manifest.get("promotion_unique_count"))
        if not promotion_count:
            promotion_count = unique_csv_count(
                run_root / "promotion" / "parsed" / "all_promotion_products.csv",
                "sku_id",
            )
        if promotion_count <= 0:
            issues.append(f"promotion listing sku {promotion_count}/18")
    return issues


def failure_detail_from_log(path, max_lines=8):
    if not str(path or "").strip():
        return ""
    path = Path(path or "")
    if not path.exists() or not path.is_file():
        return ""
    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
    interesting = [
        line
        for line in lines
        if any(token in line for token in ("[fail]", "RuntimeError:", "Error:", "Exception:", "status=", "cost="))
    ]
    selected = (interesting or lines)[-max_lines:]
    return " / ".join(selected)


def step_failure_issues(status, failed_step, failed_step_name, log_path):
    if str(status or "").strip().lower() not in {"failed", "fail", "error"}:
        return []
    label = " ".join(part for part in [f"step{failed_step}" if failed_step else "", failed_step_name] if part)
    label = label or "step failure"
    detail = failure_detail_from_log(log_path)
    return [f"{label} failed" + (f": {detail}" if detail else "")]


def build_subject(category, issues):
    product = str(category or CATEGORY).strip().upper()
    if issues:
        return f"[SEA] [Warning] BBY {product} crawled"
    return f"[SEA] BBY {product} crawled"


def build_body(collected_count, cost_krw, call_counts, issues, rank_counts=None, notes=None):
    total_calls = as_int((call_counts or {}).get("total"))
    per_call_krw = int(round(cost_krw / total_calls)) if total_calls else 0
    detail_by_stage = {
        item.get("stage"): as_int(item.get("calls"))
        for item in (call_counts or {}).get("detail_breakdown") or []
    }
    lines = [
        f"총 수집 {collected_count} sku",
        "",
        f"총 호출 비용 {money_krw(cost_krw)}(환율 {KRW_PER_USD:,}원 기준)",
        "",
        f"총 호출 수 {total_calls:,}회",
        f"평균 호출 비용 {per_call_krw:,}원",
        "",
        "호출 내역",
        f"  listing - {as_int((call_counts or {}).get('listing')):,}회",
        f"  detail/review/compare - {as_int((call_counts or {}).get('detail')):,}회 (1콜에 d/r/c 묶음)",
    ]
    if detail_by_stage.get("review"):
        lines.append(f"    └ 리뷰 별도 재수집 - {detail_by_stage['review']:,}회")
    if detail_by_stage.get("compare"):
        lines.append(f"    └ compare 별도 호출 - {detail_by_stage['compare']:,}회")
    lines.extend([
        f"  3종 availability - {as_int((call_counts or {}).get('availability')):,}회",
        "",
    ])
    if rank_counts is not None:
        lines.extend([
            "랭크 수집 현황",
            f"  main_rank - {as_int(rank_counts.get('main_rank')):,}/300",
            f"  bsr_rank - {as_int(rank_counts.get('bsr_rank')):,}/100",
            "",
        ])
    if notes:
        lines.append("참고사항")
        lines.extend(f"- {note}" for note in notes)
        lines.append("")
    if issues:
        lines.append("특이사항")
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("특이사항 없음")
    return "\n".join(lines)


def build_preflight_notification(category, issue, log_path=""):
    product = str(category or CATEGORY).strip().upper()
    detail = failure_detail_from_log(log_path)
    issue_text = str(issue or "pre-run failure").strip()
    if detail:
        issue_text = f"{issue_text}: {detail}"
    subject = f"[SEA] [Warning] BBY {product} crawled"
    body = "\n".join([
        "총 수집 0 sku",
        "",
        f"총 호출 비용 {money_krw(0)}(환율 {KRW_PER_USD:,}원 기준)",
        "",
        "총 호출 수 0회",
        "평균 호출 비용 0원",
        "",
        "호출 내역",
        "  listing - 0회",
        "  detail/review/compare - 0회(1콜에 d/r/c 묶음)",
        "  3종 availability - 0회",
        "",
        "특이사항",
        f"- {issue_text}",
    ])
    return {
        "subject": subject,
        "body": body,
        "issues": [issue_text],
        "metrics": {
            "collected_count": 0,
            "cost_usd": 0,
            "cost_krw": 0,
            "krw_per_usd": KRW_PER_USD,
            "final_output_rows": 0,
            "db_insert_columns": [],
            "cost_sources": [],
            "call_counts": {
                "listing": 0,
                "detail": 0,
                "availability": 0,
                "total": 0,
                "detail_breakdown": [],
            },
        },
    }


def build_notification(category, run_root, status="success", failed_step="", failed_step_name="", log_path=""):
    rows = read_csv_rows(FINAL_OUTPUT_CSV if run_root == RUN_ROOT else run_root / "output" / "final_output.csv")
    db_data = db_manifest(run_root)
    target_manifest = final_targets_manifest(run_root)
    columns = insert_columns(db_data, rows)
    final_db = db_data.get("final_output") or {}
    collected_count = as_int(final_db.get("inserted"))
    if collected_count == 0 and "inserted" not in final_db:
        collected_count = as_int(final_db.get("updated") or len(rows))
    if not collected_count:
        collected_count = len(rows)

    issues = []
    notes = []
    issues.extend(step_failure_issues(status, failed_step, failed_step_name, log_path))
    if not rows:
        issues.append("final_output.csv rows 0 또는 파일 없음")
    issues.extend(all_null_column_issues(category, rows, columns))
    issues.extend(critical_null_issues(rows, columns))
    review_issues, review_notes = review_completeness_issues(rows, run_root)
    issues.extend(review_issues)
    notes.extend(review_notes)
    count_issue = db_count_issue(db_data, len(rows))
    if count_issue:
        issues.append(count_issue)
    issues.extend(collected_count_issues(category, collected_count))
    issues.extend(listing_fetch_issues(run_root))
    issues.extend(listing_count_issues(category, run_root, rows, target_manifest))

    cost_usd, cost_sources = manifest_costs(run_root)
    cost_krw = round(cost_usd * KRW_PER_USD)
    call_counts = manifest_call_counts(run_root)
    rank_counts = rank_collection_counts(rows)
    subject = build_subject(category, issues)
    body = build_body(collected_count, cost_krw, call_counts, issues, rank_counts=rank_counts, notes=notes)
    return {
        "subject": subject,
        "body": body,
        "issues": issues,
        "notes": notes,
        "metrics": {
            "collected_count": collected_count,
            "cost_usd": cost_usd,
            "cost_krw": cost_krw,
            "krw_per_usd": KRW_PER_USD,
            "final_output_rows": len(rows),
            "db_insert_columns": columns,
            "cost_sources": cost_sources,
            "call_counts": call_counts,
            "rank_counts": rank_counts,
        },
    }


def first_env(names):
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def email_config():
    config = {
        "smtp_server": first_env(["BESTBUY_SMTP_SERVER", "SMTP_SERVER"]),
        "smtp_port": first_env(["BESTBUY_SMTP_PORT", "SMTP_PORT"]) or 587,
        "sender_email": first_env(["BESTBUY_EMAIL_FROM", "SMTP_EMAIL", "SMTP_SENDER_EMAIL"]),
        "sender_password": first_env(["BESTBUY_EMAIL_PASSWORD", "SMTP_PASSWORD", "SMTP_SENDER_PASSWORD"]),
        "receiver_email": first_env(["BESTBUY_EMAIL_TO", "ALERT_EMAIL", "SMTP_RECEIVER_EMAIL"]),
        "source": "env",
    }
    try:
        config["smtp_port"] = int(config["smtp_port"])
    except (TypeError, ValueError):
        config["smtp_port"] = 587
    return config


def recipient_list(value):
    return [item.strip() for item in re.split(r"[;,]", str(value or "")) if item.strip()]


def send_email(subject, body, config):
    missing = [
        name
        for name in ("smtp_server", "smtp_port", "sender_email", "sender_password", "receiver_email")
        if not config.get(name)
    ]
    if missing:
        return False, f"email config missing: {', '.join(missing)}"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["sender_email"]
    message["To"] = ", ".join(recipient_list(config["receiver_email"]))
    message.set_content(body, charset="utf-8")
    with smtplib.SMTP(config["smtp_server"], config["smtp_port"], timeout=30) as server:
        server.starttls()
        server.login(config["sender_email"], config["sender_password"])
        server.send_message(message)
    return True, ""


def write_manifest(payload):
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def print_json_safely(payload):
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    encoding = sys.stdout.encoding or os.device_encoding(1) or "utf-8"
    safe_text = text.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")
    print(safe_text)


def main():
    started_at = now()
    category = CATEGORY
    run_root = RUN_ROOT
    status = os.getenv("BESTBUY_NOTIFY_STATUS", "success").strip().lower() or "success"
    log_path = os.getenv("BESTBUY_NOTIFY_LOG", "")
    preflight_issue = os.getenv("BESTBUY_NOTIFY_PREFLIGHT_ISSUE", "").strip()
    if preflight_issue:
        notification = build_preflight_notification(category, preflight_issue, log_path=log_path)
    else:
        notification = build_notification(
            category,
            run_root,
            status=status,
            failed_step=os.getenv("BESTBUY_NOTIFY_FAILED_STEP", ""),
            failed_step_name=os.getenv("BESTBUY_NOTIFY_FAILED_STEP_NAME", ""),
            log_path=log_path,
        )
    config = email_config()
    enabled = truthy(os.getenv("BESTBUY_EMAIL_NOTIFY", "1"), default=True)
    dry_run = truthy(os.getenv("BESTBUY_EMAIL_DRY_RUN", "0"))
    sent = False
    skipped_reason = ""
    error = ""

    if not enabled:
        skipped_reason = "BESTBUY_EMAIL_NOTIFY=0"
    elif dry_run:
        skipped_reason = "BESTBUY_EMAIL_DRY_RUN=1"
    else:
        try:
            sent, skipped_reason = send_email(notification["subject"], notification["body"], config)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    manifest = {
        "run_type": "step16_email_notify",
        "started_at": started_at,
        "finished_at": now(),
        "category": category,
        "run_root": rel_path(run_root),
        "status": status,
        "subject": notification["subject"],
        "body": notification["body"],
        "issues": notification["issues"],
        "notes": notification.get("notes", []),
        "metrics": notification["metrics"],
        "email": {
            "enabled": enabled,
            "dry_run": dry_run,
            "sent": sent,
            "skipped_reason": skipped_reason,
            "error": error,
            "from": config.get("sender_email", ""),
            "to": config.get("receiver_email", ""),
            "config_source": config.get("source", ""),
        },
    }
    write_manifest(manifest)
    print_json_safely(manifest)


if __name__ == "__main__":
    main()
