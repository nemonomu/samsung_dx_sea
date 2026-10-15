"""Offline validation of one dryer run; no settings loader, browser or DB access."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path

from .step00_dryer import price_combination_warnings, select_bsr, select_main, validate_rows
from .step00_dryer_audit import DryerAuditError, audit_summary, compare_csv, read_records, region_summary
from .step14_dryer_db_load import read_rows



def check_standalone_selection(root, records, rows, settings, summary):
    """Replay only saved occurrence files; no shopping API requests."""
    differences = []
    for record in records:
        if record["status"] == "final" and (record.get("product_kind") != "dryer"
                or not (record.get("classification") or {}).get("eligible")):
            differences.append({"item": record["item"], "column": "product_kind", "expected": "dryer",
                                "actual": record.get("product_kind"), "reason": "non_target_in_final"})
    for listing_type, filename, selector, limit, rank in (
        ("main", "main_occurrences.csv", select_main, settings["main_limit"], "main_rank"),
        ("bsr", "bsr_occurrences.csv", select_bsr, settings["bsr_limit"], "bsr_rank"),
    ):
        with (root / listing_type / filename).open(encoding="utf-8-sig", newline="") as handle:
            occurrences = list(csv.DictReader(handle))
        for occurrence in occurrences:
            for column in ("page", "rank_in_page", "original_rank"):
                occurrence[column] = int(occurrence[column])
        expected = {r["omni_item_id"]: str(r[rank]) for r in selector(occurrences, limit)}
        actual = {str(r["item"]): str(r[rank]) for r in rows if r.get(rank) not in (None, "")}
        for item in sorted(set(expected) | set(actual)):
            if expected.get(item) != actual.get(item):
                differences.append({"item": item, "column": rank, "expected": expected.get(item),
                                    "actual": actual.get(item), "reason": "selection_replay_mismatch"})
    path = root / "audit" / "excluded_items.jsonl"
    excluded = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    final_ids = {str(row["item"]) for row in rows}
    seen = set()
    for record in excluded:
        item = record.get("item")
        key = item or record.get("product_url")
        if key in seen or item in final_ids:
            differences.append({"item": item or "-", "column": "item", "expected": "excluded_once_only",
                                "actual": "duplicate_or_in_final", "reason": "invalid_exclusion"})
        seen.add(key)
        if not record.get("reason") or not record.get("classification", {}).get("evidence"):
            differences.append({"item": item or "-", "column": "exclusion_reason", "expected": "reason_and_raw_evidence",
                                "actual": "missing", "reason": "missing_exclusion_evidence"})
        bsr = record.get("occurrences", {}).get("bsr")
        if bsr and not 1 <= int(bsr["original_rank"]) <= settings["bsr_limit"]:
            differences.append({"item": item or "-", "column": "original_bsr_rank", "expected": "within_displayed_cutoff",
                                "actual": bsr["original_rank"], "reason": "bsr_cutoff_exceeded"})
    if len(excluded) != summary["exclusions"]["products"]:
        differences.append({"item": "-", "column": "exclusions", "expected": summary["exclusions"]["products"],
                            "actual": len(excluded), "reason": "exclusion_count_mismatch"})
    if differences:
        raise DryerAuditError("standalone_selection_mismatch", differences)
    return {"verified": True, "excluded_products": len(excluded),
            "by_reason": dict(Counter(record["reason"] for record in excluded)), "path": str(path)}


def check_run(root):
    root = Path(root)
    rows = read_rows(root / "output" / "final_output.csv", validate=False)
    summary = json.loads((root / "collection_summary.json").read_text(encoding="utf-8"))
    settings = summary["settings"]
    if settings["zip_code"] != "10010":
        raise ValueError("ZIP mismatch")
    audit_path = root / "audit" / "value_audit.jsonl"
    value_records = read_records(audit_path)
    value_check = compare_csv(value_records, rows)
    counts = validate_rows(rows, settings["main_limit"], settings["bsr_limit"], allow_empty=True)
    if counts != summary["counts"]:
        raise ValueError("CSV/summary mismatch")
    with (root / "audit" / "spec_audit.csv").open(encoding="utf-8-sig", newline="") as handle:
        audit = list(csv.DictReader(handle))
    failures = json.loads((root / "audit" / "failures.json").read_text(encoding="utf-8"))
    quality = json.loads((root / "audit" / "quality_warnings.json").read_text(encoding="utf-8"))
    one_audit_per_spec = {(a["item"], a["column"]): a for a in audit}
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    selection = check_standalone_selection(root, value_records, rows, settings, summary) if summary.get("selection_policy") == "standalone_dryer_v2" else {"verified": False, "reason": "legacy_selection_policy"}
    price_checks = [warning for row in rows for warning in price_combination_warnings(row)]
    if "price_checks" in summary and price_checks != summary["price_checks"]:
        raise DryerAuditError("price_check_summary_mismatch")
    return {"csv_valid": True, "batch_id": summary["batch_id"], "counts": counts,
            "selection": selection, "price_checks": price_checks, "region": region_summary(value_records),
            "value_audit": {**value_check, **audit_summary(value_records), "path": str(audit_path)},
            "bsr_displayed": summary["bsr_displayed"], "collection_status": summary["collection_status"],
            "run_status": manifest.get("status", "collection_only"), "db_loaded": manifest.get("db_loaded", False),
            "failed_stage": manifest.get("failed_stage"),
            "main_shortfall": summary["main_shortfall"],
            "spec_reasons": {c: dict(Counter(a["reason"] for a in one_audit_per_spec.values() if a["column"] == c))
                             for c in ("loading_type", "capacity")},
            "api_failure_products": len({f["item"] for f in failures}), "quality_warnings": len(quality),
            "failures_path": str(root / "audit" / "failures.json"),
            "spec_audit_path": str(root / "audit" / "spec_audit.csv")}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate dryer CSV and audit files offline")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--audit-only", action="store_true", help="inspect checkpoints even when no final CSV exists")
    args = parser.parse_args(argv)
    try:
        if args.audit_only:
            path = args.run_root / "audit" / "value_audit.jsonl"
            result = {"audit_readable": True, "path": str(path), **audit_summary(read_records(path))}
        else:
            result = check_run(args.run_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"csv_valid": False, "error_type": type(exc).__name__,
                          "reason": exc.public_reason if isinstance(exc, DryerAuditError) else "run_validation_failed",
                          "differences": exc.differences if isinstance(exc, DryerAuditError) else []}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
