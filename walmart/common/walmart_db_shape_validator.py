from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


EXPECTED_DB_COLUMNS = [
    "item", "count_of_reviews", "star_rating", "count_of_star_ratings",
    "final_sku_price", "original_sku_price", "savings", "discount_type",
    "sku_popularity", "number_of_ppl_purchased_yesterday",
    "number_of_ppl_added_to_carts", "model_year", "screen_size",
    "retailer_sku_name_similar", "detailed_review_content",
    "page_type", "retailer_sku_name", "product_url", "offer",
    "pick_up_availability", "fastest_delivery", "delivery_availability",
    "sku_status", "available_quantity_for_purchase", "inventory_status",
    "main_rank", "bsr_rank", "calendar_week", "crawl_datetime",
    "account_name", "batch_id", "country",
]

DATE_IGNORED_COLUMNS = {"calendar_week", "crawl_datetime", "batch_id"}
INTEGER_COLUMNS = {
    "count_of_reviews",
    "count_of_star_ratings",
    "number_of_ppl_purchased_yesterday",
    "number_of_ppl_added_to_carts",
    "available_quantity_for_purchase",
    "main_rank",
    "bsr_rank",
}
PRICE_COLUMNS = {"final_sku_price", "original_sku_price", "savings"}
LABEL_ONLY_AVAILABILITY = {
    "shipping available",
    "pickup available",
    "delivery available",
    "not available",
}


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def normalized_columns(row: Dict[str, str]) -> List[str]:
    return list(row.keys())


def issue(issues: List[Dict[str, str]], row: Dict[str, str], column: str, code: str, value: Any) -> None:
    issues.append({
        "item": (row.get("item") or "").strip(),
        "column": column,
        "issue": code,
        "value": "" if value is None else str(value),
    })


def valid_price(value: str, allow_blank: bool = True) -> bool:
    text = (value or "").strip()
    if not text:
        return allow_blank
    return bool(re.fullmatch(r"\$[0-9][0-9,]*(?:\.[0-9]{2})", text))


def valid_int(value: str, allow_blank: bool = True) -> bool:
    text = (value or "").strip()
    if not text:
        return allow_blank
    return bool(re.fullmatch(r"\d+", text))


def valid_star(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    if text == "No ratings yet":
        return True
    return bool(re.fullmatch(r"\d(?:\.\d)?", text))


def review_separator_ok(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    if not text.startswith("review1 - "):
        return False
    return " ||| " in text or text.count("review") == 1


def rank_sequence(rows: List[Dict[str, str]], column: str) -> Dict[str, Any]:
    values = sorted({int(row[column]) for row in rows if valid_int(row.get(column, "")) and row.get(column)})
    expected = list(range(1, len(values) + 1))
    return {
        "count": len(values),
        "min": values[0] if values else None,
        "max": values[-1] if values else None,
        "continuous_from_1": values == expected,
        "missing": sorted(set(expected) - set(values))[:30],
    }


def sample_profile(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    profile: Dict[str, Any] = {
        "rows": len(rows),
        "columns": list(rows[0].keys()) if rows else [],
        "blank_counts": {},
        "nonblank_counts": {},
    }
    if not rows:
        return profile
    for column in rows[0].keys():
        blank_count = sum(1 for row in rows if blank(row.get(column)))
        profile["blank_counts"][column] = blank_count
        profile["nonblank_counts"][column] = len(rows) - blank_count
    return profile


def validate(target_csv: Path, sample_csv: Path | None, out_dir: Path, label: str) -> Dict[str, Any]:
    rows = read_csv(target_csv)
    sample_rows = read_csv(sample_csv) if sample_csv else []
    issues: List[Dict[str, str]] = []

    if rows:
        actual_columns = normalized_columns(rows[0])
        if actual_columns != EXPECTED_DB_COLUMNS:
            issues.append({
                "item": "",
                "column": "__columns__",
                "issue": "column_order_or_set_differs_from_expected",
                "value": json.dumps({"actual": actual_columns, "expected": EXPECTED_DB_COLUMNS}, ensure_ascii=False),
            })

    item_counts = Counter((row.get("item") or "").strip() for row in rows if (row.get("item") or "").strip())
    for item, count in item_counts.items():
        if count > 1:
            issues.append({"item": item, "column": "item", "issue": "duplicate_item", "value": str(count)})

    for row in rows:
        if blank(row.get("item")):
            issue(issues, row, "item", "blank_required", row.get("item"))
        if not valid_price(row.get("final_sku_price", ""), allow_blank=False):
            issue(issues, row, "final_sku_price", "blank_or_invalid_price_format", row.get("final_sku_price"))
        for column in PRICE_COLUMNS - {"final_sku_price"}:
            if not valid_price(row.get(column, ""), allow_blank=True):
                issue(issues, row, column, "invalid_price_format", row.get(column))
        if not valid_star(row.get("star_rating", "")):
            issue(issues, row, "star_rating", "invalid_display_rating_format", row.get("star_rating"))
        if re.fullmatch(r"\d+\.\d{2,}", (row.get("star_rating") or "").strip()):
            issue(issues, row, "star_rating", "long_decimal_not_page_display", row.get("star_rating"))
        for column in INTEGER_COLUMNS:
            if not valid_int(row.get(column, ""), allow_blank=True):
                issue(issues, row, column, "invalid_integer_format", row.get(column))
        if row.get("page_type") not in {"main", "bsr"}:
            issue(issues, row, "page_type", "invalid_page_type", row.get("page_type"))
        if not review_separator_ok(row.get("detailed_review_content", "")):
            issue(issues, row, "detailed_review_content", "unexpected_review_separator", row.get("detailed_review_content")[:200])
        for column in ["pick_up_availability", "fastest_delivery", "delivery_availability"]:
            text = (row.get(column) or "").strip().lower()
            if text in LABEL_ONLY_AVAILABILITY:
                issue(issues, row, column, "label_only_availability_not_sample_like", row.get(column))
        if (row.get("discount_type") or "").strip().upper() == "UNKNOWN":
            issue(issues, row, "discount_type", "unknown_should_be_blank", row.get("discount_type"))
        screen = (row.get("screen_size") or "").strip()
        if screen and not re.fullmatch(r"\d+(?:\.\d+)? inches", screen):
            issue(issues, row, "screen_size", "screen_size_not_inches_text", screen)

    rank_report = {
        "main_rank": rank_sequence(rows, "main_rank"),
        "bsr_rank": rank_sequence(rows, "bsr_rank"),
    }
    if not rank_report["main_rank"]["continuous_from_1"]:
        issues.append({"item": "", "column": "main_rank", "issue": "main_rank_not_continuous_from_1", "value": json.dumps(rank_report["main_rank"])})
    if not rank_report["bsr_rank"]["continuous_from_1"]:
        issues.append({"item": "", "column": "bsr_rank", "issue": "bsr_rank_not_continuous_from_1", "value": json.dumps(rank_report["bsr_rank"])})

    target_profile = sample_profile(rows)
    sample = sample_profile(sample_rows)
    field_audit_rows: List[Dict[str, Any]] = []
    for column in EXPECTED_DB_COLUMNS:
        if column in DATE_IGNORED_COLUMNS:
            verdict = "ignored_date_or_batch"
        else:
            verdict = "ok"
        field_audit_rows.append({
            "column": column,
            "target_blank_count": target_profile.get("blank_counts", {}).get(column, ""),
            "target_nonblank_count": target_profile.get("nonblank_counts", {}).get(column, ""),
            "sample_blank_count": sample.get("blank_counts", {}).get(column, ""),
            "sample_nonblank_count": sample.get("nonblank_counts", {}).get(column, ""),
            "verdict": verdict,
        })

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "target_csv": str(target_csv),
        "sample_csv": str(sample_csv) if sample_csv else "",
        "row_count": len(rows),
        "issue_count": len(issues),
        "issue_counts": dict(Counter(issue_row["issue"] for issue_row in issues)),
        "rank_report": rank_report,
        "date_columns_ignored": sorted(DATE_IGNORED_COLUMNS),
    }
    write_csv(out_dir / f"validation_field_audit_{label}.csv", field_audit_rows, [
        "column", "target_blank_count", "target_nonblank_count",
        "sample_blank_count", "sample_nonblank_count", "verdict",
    ])
    write_csv(out_dir / f"validation_issues_{label}.csv", issues, ["item", "column", "issue", "value"])
    (out_dir / f"validation_summary_{label}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-csv", type=Path, required=True)
    parser.add_argument("--sample-csv", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--label", default="run")
    args = parser.parse_args()

    out_dir = (args.out_dir or args.target_csv.parent).resolve()
    summary = validate(args.target_csv.resolve(), args.sample_csv.resolve() if args.sample_csv else None, out_dir, args.label)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["issue_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
