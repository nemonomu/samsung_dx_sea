"""Offline validation of one dryer run; no settings loader, browser or DB access."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path

from .step00_dryer import validate_rows
from .step00_dryer_audit import DryerAuditError, audit_summary, compare_csv, read_records
from .step14_dryer_db_load import read_rows


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
    counts = validate_rows(rows, settings["main_limit"], settings["bsr_limit"])
    if counts != summary["counts"]:
        raise ValueError("CSV/summary mismatch")
    with (root / "audit" / "spec_audit.csv").open(encoding="utf-8-sig", newline="") as handle:
        audit = list(csv.DictReader(handle))
    failures = json.loads((root / "audit" / "failures.json").read_text(encoding="utf-8"))
    quality = json.loads((root / "audit" / "quality_warnings.json").read_text(encoding="utf-8"))
    one_audit_per_spec = {(a["item"], a["column"]): a for a in audit}
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    return {"csv_valid": True, "batch_id": rows[0]["batch_id"], "counts": counts,
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
