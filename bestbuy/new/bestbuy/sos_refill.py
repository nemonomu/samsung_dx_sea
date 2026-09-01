import argparse
import csv
import json
import os
import shutil
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
from .step00_config import (
    DEFAULT_BESTBUY_RUN_ROOT,
    PROMOTION_TV_EXPECTED_MIN_ROWS,
    PROMOTION_TV_HOME_THEATER_URL,
    bestbuy_category,
    has_target_url,
)
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
PROMOTION_OVERLAY_FIELDS = (
    "batch_id",
    "item",
    "page_type",
    "sku_id",
    "promotion_type",
    "promotion_position",
)
PROMOTION_ARTIFACT_PATHS = (
    ("final_output", Path("output/final_output.csv")),
    ("final_targets", Path("output/bestbuy_final_targets.csv")),
    ("product_list", Path("output/bestbuy_product_list.csv")),
    ("detail_rows", Path("detail/parsed/detail_enriched_rows.csv")),
)


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


def item_from_product_url(value):
    text = compact(value).split("?", 1)[0].rstrip("/")
    if "/sku/" not in text.lower():
        return ""
    before_sku = text[: text.lower().rfind("/sku/")].rstrip("/")
    item = before_sku.rsplit("/", 1)[-1].strip()
    return item if item.lower() not in {"", "product", "site"} else ""


def recovery_row_item(row):
    return compact(row.get("item") or row.get("bsin")) or item_from_product_url(
        row.get("product_url") or row.get("detail_url")
    )


def promotion_values_by_sku(rows):
    grouped = {}
    for row in rows:
        sku = compact(row.get("sku_id"))
        promotion_type = compact(row.get("promotion_type"))
        promotion_position = compact(row.get("promotion_position"))
        if not sku or not promotion_type or not promotion_position:
            continue
        pair = (promotion_type, promotion_position)
        pairs = grouped.setdefault(sku, [])
        if pair not in pairs:
            pairs.append(pair)
    return {
        sku: {
            "promotion_type": " ||| ".join(value[0] for value in values),
            "promotion_position": " ||| ".join(value[1] for value in values),
        }
        for sku, values in grouped.items()
    }


def build_promotion_overlay_rows(promotion_rows, final_rows, target_rows, batch_id):
    target_skus_by_key = {}
    for row in target_rows:
        sku = compact(row.get("sku_id"))
        if not sku:
            continue
        for key in row_match_keys(row):
            target_skus_by_key.setdefault(key, set()).add(sku)

    existing_by_sku = {}
    existing_sku_by_identity = {}
    ambiguous_existing_skus = set()
    for row in final_rows:
        row_batch_id = compact(row.get("batch_id"))
        if row_batch_id != batch_id:
            continue
        sku = compact(row.get("sku_id"))
        if not sku:
            matched_skus = set()
            for key in row_match_keys(row):
                matched_skus.update(target_skus_by_key.get(key, set()))
            if len(matched_skus) == 1:
                sku = next(iter(matched_skus))
            elif len(matched_skus) > 1:
                raise RuntimeError(f"ambiguous existing SKU match for item={recovery_row_item(row)}")
        item = recovery_row_item(row)
        if not sku or not item:
            continue
        page_type = compact(row.get("page_type")).lower() or "main"
        identity = (row_batch_id, item.lower(), page_type)
        identity_owner = existing_sku_by_identity.get(identity)
        if identity_owner and identity_owner != sku:
            raise RuntimeError(
                "ambiguous existing DB identity for promotion recovery: "
                f"batch_id={row_batch_id} item={item} page_type={page_type} "
                f"skus={identity_owner},{sku}"
            )
        existing_sku_by_identity[identity] = sku
        existing = existing_by_sku.get(sku)
        if existing and existing["identity"] != identity:
            ambiguous_existing_skus.add(sku)
            continue
        existing_by_sku[sku] = {
            "identity": identity,
            "batch_id": row_batch_id,
            "item": item,
            "page_type": page_type,
        }
    if ambiguous_existing_skus:
        raise RuntimeError(
            "ambiguous existing rows for promotion SKUs: " + ",".join(sorted(ambiguous_existing_skus))
        )

    promotion_by_sku = promotion_values_by_sku(promotion_rows)
    overlay_rows = []
    for sku, values in promotion_by_sku.items():
        existing = existing_by_sku.get(sku)
        if not existing:
            continue
        overlay_rows.append(
            {
                "batch_id": existing["batch_id"],
                "item": existing["item"],
                "page_type": existing["page_type"],
                "sku_id": sku,
                "promotion_type": values["promotion_type"],
                "promotion_position": values["promotion_position"],
            }
        )
    stats = {
        "collected_unique_skus": len(promotion_by_sku),
        "existing_skus": len(existing_by_sku),
        "matched_existing_skus": len(overlay_rows),
        "unmatched_promotion_skus": sorted(set(promotion_by_sku) - set(existing_by_sku)),
    }
    # Backward-compatible aliases retained for existing recovery manifests.
    stats["existing_main_skus"] = stats["existing_skus"]
    stats["matched_main_skus"] = stats["matched_existing_skus"]
    return overlay_rows, stats


def read_csv_table(path):
    path = Path(path)
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def prepare_promotion_artifact_updates(run_root, overlay_rows, batch_id):
    overlay_by_sku = {compact(row.get("sku_id")): row for row in overlay_rows}
    overlay_by_item = {}
    for overlay_row in overlay_rows:
        item = compact(overlay_row.get("item")).lower()
        existing = overlay_by_item.get(item)
        if existing and compact(existing.get("sku_id")) != compact(overlay_row.get("sku_id")):
            raise RuntimeError(f"ambiguous promotion overlay item: {item}")
        overlay_by_item[item] = overlay_row
    plans = []
    for label, relative_path in PROMOTION_ARTIFACT_PATHS:
        path = run_root / relative_path
        rows, fieldnames = read_csv_table(path)
        if not rows:
            continue
        matched_skus = set()
        changed_rows = 0
        planned_rows = []
        for source_row in rows:
            row = dict(source_row)
            row_batch_id = compact(row.get("batch_id"))
            if row_batch_id and row_batch_id != batch_id:
                planned_rows.append(row)
                continue
            sku = compact(row.get("sku_id"))
            overlay = overlay_by_sku.get(sku) if sku else None
            if not sku:
                overlay = overlay_by_item.get(recovery_row_item(row).lower())
            if not overlay:
                planned_rows.append(row)
                continue
            matched_skus.add(compact(overlay.get("sku_id")))
            before = (compact(row.get("promotion_type")), compact(row.get("promotion_position")))
            row["promotion_type"] = overlay["promotion_type"]
            row["promotion_position"] = overlay["promotion_position"]
            after = (compact(row.get("promotion_type")), compact(row.get("promotion_position")))
            if before != after:
                changed_rows += 1
            planned_rows.append(row)
        for field in ("promotion_type", "promotion_position"):
            if field not in fieldnames:
                fieldnames.append(field)
        plans.append(
            {
                "label": label,
                "path": path,
                "rows": planned_rows,
                "fieldnames": fieldnames,
                "original_rows": rows,
                "original_fieldnames": list(rows[0].keys()),
                "matched_rows": len(matched_skus),
                "changed_rows": changed_rows,
            }
        )

    final_plan = next((plan for plan in plans if plan["label"] == "final_output"), None)
    if not final_plan:
        raise RuntimeError("promotion recovery requires output/final_output.csv")
    if final_plan["matched_rows"] != len(overlay_rows):
        raise RuntimeError(
            "promotion overlay did not match every existing row "
            f"({final_plan['matched_rows']}/{len(overlay_rows)})"
        )
    return plans


def write_csv_rows_atomic(path, rows, fieldnames):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        write_csv_rows(temp_path, rows, fieldnames)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def backup_promotion_recovery_inputs(run_root, recovery_root, plans):
    before_root = recovery_root / "before"
    before_root.mkdir(parents=True, exist_ok=True)
    for plan in plans:
        source = plan["path"]
        destination = before_root / source.relative_to(run_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    canonical_promotion = run_root / "promotion"
    target_manifest = run_root / "output" / "bestbuy_final_targets.manifest.json"
    state = {
        "promotion_existed": canonical_promotion.exists(),
        "target_manifest_existed": target_manifest.exists(),
    }
    (before_root / "rollback_state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if canonical_promotion.exists():
        shutil.copytree(canonical_promotion, before_root / "promotion", dirs_exist_ok=True)
    if target_manifest.exists():
        destination = before_root / "output" / target_manifest.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target_manifest, destination)


def apply_prepared_promotion_updates(plans):
    results = []
    written = []
    try:
        for plan in plans:
            write_csv_rows_atomic(plan["path"], plan["rows"], plan["fieldnames"])
            written.append(plan)
            results.append(
                {
                    "artifact": plan["label"],
                    "path": str(plan["path"]),
                    "matched_rows": plan["matched_rows"],
                    "changed_rows": plan["changed_rows"],
                    "updated_columns": ["promotion_type", "promotion_position"],
                }
            )
    except Exception as exc:
        try:
            restore_prepared_promotion_updates(written)
        except Exception as rollback_exc:
            raise RuntimeError(
                f"promotion artifact update failed ({exc}); rollback failed: {rollback_exc}"
            ) from exc
        raise
    return results


def restore_prepared_promotion_updates(plans):
    rollback_errors = []
    for plan in reversed(plans):
        try:
            write_csv_rows_atomic(
                plan["path"],
                plan["original_rows"],
                plan["original_fieldnames"],
            )
        except Exception as exc:
            rollback_errors.append(f"{plan['label']}: {exc}")
    if rollback_errors:
        raise RuntimeError("; ".join(rollback_errors))


def publish_validated_promotion(staged_promotion_root, run_root):
    canonical_promotion = run_root / "promotion"
    canonical_promotion.mkdir(parents=True, exist_ok=True)
    summary_path = staged_promotion_root / "summary.json"
    if summary_path.exists():
        shutil.copy2(summary_path, canonical_promotion / "summary.json")
    for relative_path in (Path("parsed"), Path("raw/browser_dom")):
        source = staged_promotion_root / relative_path
        if source.exists():
            shutil.copytree(source, canonical_promotion / relative_path, dirs_exist_ok=True)


def restore_promotion_recovery_inputs(run_root, recovery_root, plans):
    restore_prepared_promotion_updates(plans)
    before_root = recovery_root / "before"
    state = read_json(before_root / "rollback_state.json")
    canonical_promotion = run_root / "promotion"
    promotion_backup = before_root / "promotion"
    if state:
        if canonical_promotion.exists():
            shutil.rmtree(canonical_promotion)
        if state.get("promotion_existed") and promotion_backup.exists():
            shutil.copytree(promotion_backup, canonical_promotion)
    elif promotion_backup.exists():
        shutil.copytree(promotion_backup, canonical_promotion, dirs_exist_ok=True)
    target_manifest = run_root / "output" / "bestbuy_final_targets.manifest.json"
    target_manifest_backup = before_root / "output" / "bestbuy_final_targets.manifest.json"
    if state and not state.get("target_manifest_existed"):
        if target_manifest.exists():
            target_manifest.unlink()
    elif target_manifest_backup.exists():
        shutil.copy2(
            target_manifest_backup,
            target_manifest,
        )


def rows_for_skus(rows, skus):
    wanted = {compact(sku) for sku in skus if compact(sku)}
    return [row for row in rows if compact(row.get("sku_id")) in wanted]


def write_recovery_subset(path, rows, source_rows):
    if not rows:
        return
    fieldnames = list(source_rows[0].keys()) if source_rows else list(rows[0].keys())
    write_csv_rows_atomic(path, rows, fieldnames)


def preserve_existing_artifacts_and_append_new_rows(plans, missing_skus, log_handle=None):
    expected = {compact(sku) for sku in missing_skus if compact(sku)}
    results = []
    for plan in plans:
        current_rows, current_fieldnames = read_csv_table(plan["path"])
        new_rows = rows_for_skus(current_rows, expected)
        actual = {compact(row.get("sku_id")) for row in new_rows if compact(row.get("sku_id"))}
        if actual != expected:
            raise RuntimeError(
                f"promotion {plan['label']} new-row preservation mismatch: "
                f"actual={sorted(actual)} expected={sorted(expected)}"
            )

        original_skus = {
            compact(row.get("sku_id"))
            for row in plan["original_rows"]
            if compact(row.get("sku_id"))
        }
        duplicate_skus = sorted(expected & original_skus)
        if duplicate_skus:
            raise RuntimeError(
                f"promotion {plan['label']} new SKUs already existed before recovery: {duplicate_skus}"
            )

        fieldnames = list(plan["original_fieldnames"])
        for field in current_fieldnames:
            if field not in fieldnames:
                fieldnames.append(field)
        rebuilt_rows = [dict(row) for row in plan["original_rows"]]
        rebuilt_rows.extend(dict(row) for row in new_rows)
        write_csv_rows_atomic(plan["path"], rebuilt_rows, fieldnames)
        result = {
            "artifact": plan["label"],
            "existing_rows_preserved": len(plan["original_rows"]),
            "new_rows_appended": len(new_rows),
        }
        results.append(result)
        message = (
            f"[sos:promotion-preserve] {plan['label']} "
            f"existing={len(plan['original_rows'])} new={len(new_rows)}"
        )
        print(message)
        if log_handle:
            log_handle.write(message + "\n")
    return results


def validate_new_promotion_rows(rows, missing_skus, batch_id, label):
    expected = {compact(sku) for sku in missing_skus if compact(sku)}
    actual = {compact(row.get("sku_id")) for row in rows if compact(row.get("sku_id"))}
    if actual != expected:
        raise RuntimeError(
            f"promotion {label} new-row SKU mismatch: actual={sorted(actual)} expected={sorted(expected)}"
        )
    for row in rows:
        if compact(row.get("batch_id")) != batch_id:
            raise RuntimeError(
                f"promotion {label} new row must reuse batch_id={batch_id}: {row.get('batch_id')}"
            )
        if compact(row.get("page_type")).lower() != "promotion":
            raise RuntimeError(
                f"promotion {label} new row must use page_type=promotion: {row.get('page_type')}"
            )


def run_new_promotion_sku_pipeline(
    category,
    run_root,
    batch_id,
    base,
    missing_skus,
    log_handle,
    recovery_root,
):
    missing_skus = sorted({compact(sku) for sku in missing_skus if compact(sku)})
    if not missing_skus:
        return {
            "missing_skus": [],
            "final_csv": "",
            "product_list_csv": "",
            "final_rows": [],
            "product_list_rows": [],
        }

    sku_filter = ",".join(missing_skus)
    scoped_base = dict(base)
    scoped_base["BESTBUY_DETAIL_SKUS"] = sku_filter
    run_step(step_by_name("final_targets"), category, scoped_base, log_handle)
    target_rows = read_csv_rows(run_root / "output" / "bestbuy_final_targets.csv")
    target_subset = rows_for_skus(target_rows, missing_skus)
    target_skus = {compact(row.get("sku_id")) for row in target_subset}
    if target_skus != set(missing_skus):
        raise RuntimeError(
            f"promotion final-target backfill mismatch: {sorted(target_skus)}/{missing_skus}"
        )
    for row in target_subset:
        if compact(row.get("target_source")) != "promotion_backfill":
            raise RuntimeError(
                f"new promotion target must use target_source=promotion_backfill: {row.get('target_source')}"
            )

    run_step(step_by_name("detail_html"), category, scoped_base, log_handle)
    run_step(step_by_name("review20"), category, scoped_base, log_handle)
    run_step(
        step_by_name("availability_backfill"),
        category,
        scoped_base,
        log_handle,
        env_overrides={"BESTBUY_AVAILABILITY_BACKFILL_SKUS": sku_filter},
    )

    missing_failures = rows_for_skus(
        read_csv_rows(run_root / "detail" / "parsed" / "detail_failures.csv"),
        missing_skus,
    )
    if missing_failures:
        failed = sorted({compact(row.get("sku_id")) for row in missing_failures})
        raise RuntimeError(f"new promotion SKU detail/review failed: {failed}")

    final_source = read_csv_rows(run_root / "output" / "final_output.csv")
    product_source = read_csv_rows(run_root / "output" / "bestbuy_product_list.csv")
    final_rows = rows_for_skus(final_source, missing_skus)
    product_rows = rows_for_skus(product_source, missing_skus)
    validate_new_promotion_rows(final_rows, missing_skus, batch_id, "final_output")
    validate_new_promotion_rows(product_rows, missing_skus, batch_id, "product_list")

    new_root = recovery_root / "new_rows"
    final_csv = new_root / "final_output.csv"
    product_list_csv = new_root / "bestbuy_product_list.csv"
    write_recovery_subset(final_csv, final_rows, final_source)
    write_recovery_subset(product_list_csv, product_rows, product_source)
    return {
        "missing_skus": missing_skus,
        "final_csv": str(final_csv),
        "product_list_csv": str(product_list_csv),
        "final_rows": final_rows,
        "product_list_rows": product_rows,
    }


def update_promotion_target_manifest(run_root, collected_unique_skus, matched_existing_skus, recovery_root):
    path = run_root / "output" / "bestbuy_final_targets.manifest.json"
    manifest = read_json(path)
    if not manifest:
        return {"skipped": True, "reason": "target manifest missing"}
    manifest["promotion_unique_count"] = collected_unique_skus
    manifest["promotion_recovery"] = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "matched_existing_skus": matched_existing_skus,
        "matched_main_skus": matched_existing_skus,
        "recovery_root": str(recovery_root),
        "updated_columns": ["promotion_type", "promotion_position"],
    }
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return {
        "path": str(path),
        "promotion_unique_count": collected_unique_skus,
        "matched_existing_skus": matched_existing_skus,
        "matched_main_skus": matched_existing_skus,
    }


def promotion_container_found(summary):
    for item in summary.get("summaries") or []:
        if item.get("container_found"):
            return True
    return bool(summary.get("container_found"))


def validate_promotion_recovery(summary, rows, minimum_rows):
    if summary.get("collection_failed") or summary.get("skipped"):
        raise RuntimeError(f"promotion collection failed: {summary.get('reason') or summary.get('error') or 'unknown'}")
    if not promotion_container_found(summary):
        raise RuntimeError("promotion target container was not found")
    urls = []
    for item in summary.get("summaries") or []:
        urls.extend([compact(item.get("url")), compact(item.get("browser_url"))])
    if not any("pcmcat1690836748285" in value for value in urls):
        raise RuntimeError("promotion collector did not use the verified pcmcat1690836748285 page")
    promotion_by_sku = promotion_values_by_sku(rows)
    if minimum_rows and len(promotion_by_sku) < minimum_rows:
        raise RuntimeError(
            f"promotion collection below recovery minimum: {len(promotion_by_sku)}/{minimum_rows} unique SKUs"
        )
    promotion_types = sorted({compact(values.get("promotion_type")) for values in rows if compact(values.get("promotion_type"))})
    if len(promotion_types) != 1:
        raise RuntimeError(f"promotion recovery requires one live headline, found: {promotion_types}")
    dom_summaries = [item for item in summary.get("summaries") or [] if item.get("container_found")]
    dom_summary = dom_summaries[0] if dom_summaries else summary
    detected_type = compact(dom_summary.get("promotion_type"))
    if detected_type and promotion_types[0] != detected_type:
        raise RuntimeError(
            f"promotion headline mismatch: rows={promotion_types[0]} dom={detected_type}"
        )
    validation_errors = list(dom_summary.get("validation_errors") or [])
    if validation_errors:
        raise RuntimeError("promotion DOM validation failed: " + ", ".join(validation_errors))
    if dom_summary.get("stable") is False:
        raise RuntimeError("promotion card set did not stabilize")
    card_count = status_int(dom_summary.get("card_count"))
    if card_count and card_count != len(promotion_by_sku):
        raise RuntimeError(
            f"promotion DOM/SKU count mismatch: {card_count}/{len(promotion_by_sku)}"
        )
    return {
        "unique_skus": len(promotion_by_sku),
        "minimum_rows": minimum_rows,
        "promotion_type": promotion_types[0],
        "card_count": card_count or len(promotion_by_sku),
        "stable": True,
    }


def cached_availability_values(run_root):
    from .step00_fulfillment_graphql import parse_fulfillment_response

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
            "BESTBUY_DB_UPDATE_PROMOTION_ONLY": "0",
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


def run_step(
    step,
    category,
    base,
    log_handle,
    dry_run=False,
    refresh_all_availability=False,
    env_overrides=None,
):
    if should_skip_join_step(step, category):
        print(f"[sos:skip] step {step.key} {step.name}: not applicable for {category}")
        log_handle.write(f"[sos:skip] step {step.key} {step.name}: not applicable for {category}\n")
        return
    env = step_env(step, category, base, refresh_all_availability=refresh_all_availability)
    env.update(env_overrides or {})
    command = [PYTHON, "-m", step.module]
    interesting_keys = set(step.env) | set(env_overrides or {})
    interesting_env = {key: env[key] for key in sorted(interesting_keys) if key in env}
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


def write_promotion_recovery_manifest(path, manifest):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def run_promotion_only_recovery(category, run_root, batch_id, base, args, log_handle, recovery_root):
    if category != "TV":
        raise RuntimeError("--promotion-only is available only for TV")
    if args.promotion_min_rows < 0:
        raise RuntimeError("--promotion-min-rows cannot be negative")

    staged_promotion_root = recovery_root / "promotion"
    overlay_path = recovery_root / "overlay" / "promotion_updates.csv"
    db_output_root = recovery_root / "db"
    manifest_path = recovery_root / "manifest.json"
    manifest = {
        "run_type": "bestbuy_promotion_only_recovery",
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "category": category,
        "batch_id": batch_id,
        "run_root": str(run_root),
        "recovery_root": str(recovery_root),
        "source_url": PROMOTION_TV_HOME_THEATER_URL,
        "promotion_type": "dynamic_live_headline",
        "minimum_rows": args.promotion_min_rows,
        "db_load": not args.no_db_load,
        "updated_columns": ["promotion_type", "promotion_position"],
        "new_rows_allowed": True,
    }
    write_promotion_recovery_manifest(manifest_path, manifest)

    promotion_overrides = {
        "BESTBUY_PROMOTION_RUN_ROOT": str(staged_promotion_root),
        "BESTBUY_PROMOTION_REFERER": PROMOTION_TV_HOME_THEATER_URL,
        "BESTBUY_PROMOTION_EXPECTED_MIN_ROWS": str(args.promotion_min_rows),
        "BESTBUY_PROMOTION_FETCH_MODE": "browser_dom",
        "BESTBUY_PROMOTION_BROWSER_HEADLESS": "0",
    }
    db_overrides = {
        "BESTBUY_OUTPUT_ROOT": str(db_output_root),
        "BESTBUY_FINAL_OUTPUT_CSV": str(overlay_path),
        "BESTBUY_DB_UPDATE_PROMOTION_ONLY": "1",
        "BESTBUY_DB_UPDATE_AVAILABILITY_ONLY": "0",
        "BESTBUY_DB_UPDATE_SIMILAR_ONLY": "0",
        "BESTBUY_DB_UPDATE_BATCH_ID": batch_id,
        "BESTBUY_DB_ROW_UPSERT_ONLY": "0",
        "BESTBUY_DB_ROW_UPSERT_SKUS": "",
        "BESTBUY_DB_ROW_UPSERT_ITEMS": "",
    }
    rollback_plans = []
    backup_ready = False
    db_completed = False

    try:
        run_step(
            step_by_name("promotion_deals"),
            category,
            base,
            log_handle,
            dry_run=args.dry_run,
            env_overrides=promotion_overrides,
        )
        if args.dry_run:
            if not args.no_db_load:
                run_step(
                    step_by_name("db_load"),
                    category,
                    base,
                    log_handle,
                    dry_run=True,
                    env_overrides=db_overrides,
                )
            manifest.update(
                {
                    "status": "dry_run",
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "overlay_csv": str(overlay_path),
                }
            )
            write_promotion_recovery_manifest(manifest_path, manifest)
            return manifest

        summary_path = staged_promotion_root / "summary.json"
        promotion_csv = staged_promotion_root / "parsed" / "all_promotion_products.csv"
        summary = read_json(summary_path)
        promotion_rows = read_csv_rows(promotion_csv)
        validation = validate_promotion_recovery(summary, promotion_rows, args.promotion_min_rows)
        manifest["promotion_type"] = validation["promotion_type"]

        final_rows = read_csv_rows(run_root / "output" / "final_output.csv")
        target_rows = read_csv_rows(run_root / "output" / "bestbuy_final_targets.csv")
        overlay_rows, overlay_stats = build_promotion_overlay_rows(
            promotion_rows,
            final_rows,
            target_rows,
            batch_id,
        )
        missing_skus = overlay_stats["unmatched_promotion_skus"]
        write_csv_rows_atomic(overlay_path, overlay_rows, PROMOTION_OVERLAY_FIELDS)

        rollback_plans = prepare_promotion_artifact_updates(run_root, overlay_rows, batch_id)
        backup_promotion_recovery_inputs(run_root, recovery_root, rollback_plans)
        backup_ready = True

        new_rows_result = {
            "missing_skus": [],
            "final_csv": "",
            "product_list_csv": "",
            "final_rows": [],
            "product_list_rows": [],
        }
        artifact_preservation = []
        if missing_skus:
            publish_validated_promotion(staged_promotion_root, run_root)
            new_rows_result = run_new_promotion_sku_pipeline(
                category,
                run_root,
                batch_id,
                base,
                missing_skus,
                log_handle,
                recovery_root,
            )
            artifact_preservation = preserve_existing_artifacts_and_append_new_rows(
                rollback_plans,
                missing_skus,
                log_handle,
            )

        plans = prepare_promotion_artifact_updates(run_root, overlay_rows, batch_id)
        artifact_updates = apply_prepared_promotion_updates(plans)
        db_overrides.update(
            {
                "BESTBUY_DB_PROMOTION_NEW_FINAL_CSV": new_rows_result["final_csv"],
                "BESTBUY_DB_PROMOTION_NEW_PRODUCT_LIST_CSV": new_rows_result["product_list_csv"],
            }
        )

        db_result = {
            "final_output": {"skipped": True, "reason": "--no-db-load"},
            "product_list": {"skipped": True, "reason": "--no-db-load"},
        }
        if not args.no_db_load:
            try:
                db_output_root.mkdir(parents=True, exist_ok=True)
                run_step(
                    step_by_name("db_load"),
                    category,
                    base,
                    log_handle,
                    env_overrides=db_overrides,
                )
                db_manifest = read_json(db_output_root / "db_load_manifest.json")
                db_result = {
                    "final_output": db_manifest.get("final_output") or {},
                    "product_list": db_manifest.get("product_list") or {},
                }
                for label, result in db_result.items():
                    existing_result = result.get("existing") or result
                    new_result = result.get("new") or {}
                    candidate_rows = status_int(existing_result.get("candidate_rows"))
                    updated_rows = status_int(existing_result.get("updated"))
                    if candidate_rows != len(overlay_rows):
                        raise RuntimeError(
                            f"promotion {label} DB candidate mismatch: "
                            f"{candidate_rows}/{len(overlay_rows)}"
                        )
                    if updated_rows != len(overlay_rows):
                        raise RuntimeError(
                            f"promotion {label} DB update mismatch: {updated_rows}/{len(overlay_rows)}"
                        )
                    new_candidates = status_int(new_result.get("candidate_rows"))
                    new_written = status_int(new_result.get("inserted")) + status_int(new_result.get("updated"))
                    if new_candidates != len(missing_skus):
                        raise RuntimeError(
                            f"promotion {label} new-row DB candidate mismatch: "
                            f"{new_candidates}/{len(missing_skus)}"
                        )
                    if new_written != len(missing_skus):
                        raise RuntimeError(
                            f"promotion {label} new-row DB write mismatch: "
                            f"{new_written}/{len(missing_skus)}"
                        )
                db_completed = True
            except Exception as db_exc:
                try:
                    restore_promotion_recovery_inputs(run_root, recovery_root, rollback_plans)
                    backup_ready = False
                except Exception as rollback_exc:
                    raise RuntimeError(
                        f"promotion DB update failed ({db_exc}); artifact rollback failed: {rollback_exc}"
                    ) from db_exc
                raise

        publish_validated_promotion(staged_promotion_root, run_root)
        target_manifest_update = update_promotion_target_manifest(
            run_root,
            validation["unique_skus"],
            len(overlay_rows),
            recovery_root,
        )

        manifest.update(
            {
                "status": "completed",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "validation": validation,
                "overlay": overlay_stats,
                "new_rows": {
                    "count": len(missing_skus),
                    "skus": missing_skus,
                    "final_csv": new_rows_result["final_csv"],
                    "product_list_csv": new_rows_result["product_list_csv"],
                    "page_type": "promotion",
                    "batch_id": batch_id,
                },
                "overlay_csv": str(overlay_path),
                "artifact_updates": artifact_updates,
                "artifact_preservation": artifact_preservation,
                "target_manifest_update": target_manifest_update,
                "db_result": db_result,
                "promotion_summary": str(summary_path),
                "promotion_csv": str(promotion_csv),
            }
        )
        write_promotion_recovery_manifest(manifest_path, manifest)
        message = (
            f"[sos:promotion] collected={validation['unique_skus']} "
            f"matched_existing={len(overlay_rows)} new={len(missing_skus)} "
            f"db_updated={db_result['final_output'].get('updated', 'skipped')}"
        )
        print(message)
        log_handle.write(message + "\n")
        return manifest
    except Exception as exc:
        if backup_ready and not db_completed:
            try:
                restore_promotion_recovery_inputs(run_root, recovery_root, rollback_plans)
            except Exception as rollback_exc:
                exc = RuntimeError(f"{exc}; promotion artifact rollback failed: {rollback_exc}")
        manifest.update(
            {
                "status": "failed",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        write_promotion_recovery_manifest(manifest_path, manifest)
        raise


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
    parser.add_argument(
        "--promotion-only",
        action="store_true",
        help=(
            "Refetch TV promotion, update its two fields on existing rows, and add promotion-only SKUs "
            "with the existing batch_id and page_type=promotion"
        ),
    )
    parser.add_argument(
        "--promotion-min-rows",
        type=int,
        default=PROMOTION_TV_EXPECTED_MIN_ROWS,
        help=(
            "Optional operator floor for validated promotion rows; 0 uses the complete stable live card set "
            f"(default {PROMOTION_TV_EXPECTED_MIN_ROWS})"
        ),
    )
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
    if args.promotion_only and args.refresh_join_sources:
        raise RuntimeError("--promotion-only cannot be combined with --refresh-join-sources")
    if args.promotion_only and args.refresh_all_availability:
        raise RuntimeError("--promotion-only cannot be combined with --refresh-all-availability")

    before = current_summary(run_root)
    existing_rows = read_csv_rows(run_root / "output" / "final_output.csv")
    existing_product_rows = read_csv_rows(run_root / "output" / "bestbuy_product_list.csv")
    print_summary(before, "before")
    if args.analysis_only:
        return

    log_dir = run_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_prefix = "promotion_recovery" if args.promotion_only else "sos_refill"
    log_path = log_dir / f"{log_prefix}_{run_timestamp}.log"
    base = base_env(category, run_root, batch_id, preserve_table_env=args.preserve_table_env)

    if args.promotion_only:
        recovery_root = run_root / "promotion_recovery" / run_timestamp
        failed_step = step_by_name("promotion_deals")
        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as log_handle:
                log_handle.write(f"BestBuy promotion-only recovery started category={category} batch_id={batch_id}\n")
                log_handle.write(f"run_root={run_root}\n")
                try:
                    run_promotion_only_recovery(
                        category,
                        run_root,
                        batch_id,
                        base,
                        args,
                        log_handle,
                        recovery_root,
                    )
                except StepFailure as exc:
                    failed_step = exc.step
                    raise
                log_handle.write("BestBuy promotion-only recovery completed\n")
        except Exception:
            if not args.no_notify and not args.dry_run:
                notify(category, run_root, "failed", log_path, failed_step)
            raise
        after = current_summary(run_root)
        print_summary(after, "after")
        print(f"[sos:done] log={log_path}")
        print(f"[sos:done] recovery={recovery_root}")
        if not args.no_notify and not args.dry_run:
            notify(category, run_root, "success", log_path)
        return

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
