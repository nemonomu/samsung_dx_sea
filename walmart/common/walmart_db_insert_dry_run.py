from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple


FINAL_DIR = Path(
    r"C:\Users\gomguard\Documents\퀵오일\삼성전자\samsung_dx_retail_com\samsung_dx_retail_com"
    r"\log\walmart_neo_full_detail_review_337_final_20260607"
)

FINAL_DIR = Path(
    r"C:\Users\gomguard\Documents\퀵오일\삼성전자\samsung_dx_retail_com\samsung_dx_retail_com"
    r"\log\walmart_neo_full_detail_review_337_final_20260607"
)

FINAL_DIR = Path(
    r"C:\Users\gomguard\Documents\퀵오일\삼성전자\samsung_dx_retail_com\samsung_dx_retail_com"
    r"\log\walmart_neo_full_detail_review_337_final_20260607"
)

LISTING_FULL_DIR = FINAL_DIR.parent / "walmart_neo_listing_full_20260606_step5_300check"
MAIN_RANK_LIMIT = 300
BSR_RANK_LIMIT = 100

EXTRACTED_FIELDS = [
    "item",
    "count_of_reviews",
    "star_rating",
    "count_of_star_ratings",
    "final_sku_price",
    "original_sku_price",
    "savings",
    "discount_type",
    "sku_popularity",
    "number_of_ppl_purchased_yesterday",
    "number_of_ppl_added_to_carts",
    "model_year",
    "screen_size",
    "retailer_sku_name_similar",
    "detailed_review_content",
]

PASSTHROUGH_FIELDS = [
    "page_type",
    "retailer_sku_name",
    "product_url",
    "offer",
    "pick_up_availability",
    "fastest_delivery",
    "delivery_availability",
    "sku_status",
    "available_quantity_for_purchase",
    "inventory_status",
    "main_rank",
    "bsr_rank",
    "calendar_week",
]

META_FIELDS = [
    "crawl_datetime",
    "account_name",
    "batch_id",
    "country",
]

EXPECTED_DB_COLUMNS = EXTRACTED_FIELDS + PASSTHROUGH_FIELDS + META_FIELDS

EXCLUDED_COLUMNS = {
    "shipping_info",
    "retailer_membership_discounts",
    "summarized_review_content",
    "top_mentions",
    "recommendation_intent",
    "rank_1",
    "rank_2",
    "promotion_position",
    "trend_rank",
    "promotion_type",
    "number_of_units_purchased_past_month",
    "id",
    "redirect",
    "estimated_annual_electricity_use",
}

INTEGER_COLUMNS = {
    "count_of_reviews",
    "count_of_star_ratings",
    "main_rank",
    "bsr_rank",
    "number_of_ppl_purchased_yesterday",
    "number_of_ppl_added_to_carts",
}

TEXT_NOT_NULL_IN_PRACTICE = {
    "item",
    "account_name",
    "page_type",
    "retailer_sku_name",
    "product_url",
    "final_sku_price",
    "screen_size",
    "calendar_week",
    "crawl_datetime",
    "batch_id",
    "country",
}


def read_csv(path: Path) -> List[Dict[str, str]]:
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


def normalize_int_text(value: Any) -> str:
    if blank(value):
        return ""
    text = str(value).strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmM]?)", text)
    if not match:
        return text
    number = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "k":
        number *= 1000
    elif suffix == "m":
        number *= 1000000
    return str(int(number))


def normalize_row(row: Dict[str, str]) -> Dict[str, str]:
    out = {column: (row.get(column) or "").strip() for column in EXPECTED_DB_COLUMNS}
    for column in INTEGER_COLUMNS:
        out[column] = normalize_int_text(out.get(column))
    if out.get("discount_type", "").upper() == "UNKNOWN":
        out["discount_type"] = ""
    return out


def merge_detail_fallback(row: Dict[str, str], detail_row: Dict[str, str]) -> Dict[str, str]:
    if not detail_row:
        return row
    fallback_fields = [
        "star_rating",
        "count_of_star_ratings",
        "count_of_reviews",
        "final_sku_price",
        "original_sku_price",
        "savings",
        "discount_type",
        "sku_popularity",
        "model_year",
        "screen_size",
        "retailer_sku_name_similar",
        "offer",
    ]
    for field in fallback_fields:
        if blank(row.get(field)) and not blank(detail_row.get(field)):
            row[field] = detail_row.get(field, "")
    return row


def merge_listing_fallback(row: Dict[str, str], listing_row: Dict[str, str]) -> Dict[str, str]:
    if not listing_row:
        return row
    # Rank scope is defined by the final listing window, so these fields must
    # override stale pre-filter ranks from the source detail/review files.
    for field in ["page_type", "main_rank", "bsr_rank"]:
        row[field] = listing_row.get(field, "")
    fallback_fields = [
        "retailer_sku_name",
        "product_url",
        "offer",
        "pick_up_availability",
        "fastest_delivery",
        "delivery_availability",
        "sku_status",
        "available_quantity_for_purchase",
        "inventory_status",
        "number_of_ppl_purchased_yesterday",
        "number_of_ppl_added_to_carts",
    ]
    for field in fallback_fields:
        if blank(row.get(field)) and not blank(listing_row.get(field)):
            row[field] = listing_row.get(field, "")
    return row


def validate_row(row: Dict[str, str], source_name: str, row_no: int) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    for column in TEXT_NOT_NULL_IN_PRACTICE:
        if blank(row.get(column)):
            issues.append(
                {
                    "source": source_name,
                    "row_no": row_no,
                    "item": row.get("item", ""),
                    "severity": "warning",
                    "column": column,
                    "issue": "blank_practical_required_field",
                    "value": row.get(column, ""),
                }
            )
    for column in INTEGER_COLUMNS:
        value = row.get(column, "")
        if value and not re.fullmatch(r"\d+", value):
            issues.append(
                {
                    "source": source_name,
                    "row_no": row_no,
                    "item": row.get("item", ""),
                    "severity": "error",
                    "column": column,
                    "issue": "invalid_integer_for_db_insert",
                    "value": value,
                }
            )
    if row.get("page_type") not in {"main", "bsr"}:
        issues.append(
            {
                "source": source_name,
                "row_no": row_no,
                "item": row.get("item", ""),
                "severity": "error",
                "column": "page_type",
                "issue": "invalid_page_type",
                "value": row.get("page_type", ""),
            }
        )
    if row.get("calendar_week") and not re.fullmatch(r"w\d{1,2}", row["calendar_week"]):
        issues.append(
            {
                "source": source_name,
                "row_no": row_no,
                "item": row.get("item", ""),
                "severity": "error",
                "column": "calendar_week",
                "issue": "invalid_calendar_week",
                "value": row["calendar_week"],
            }
        )
    if row.get("batch_id") and not re.fullmatch(r"w_\d{8}_\d{6}", row["batch_id"]):
        issues.append(
            {
                "source": source_name,
                "row_no": row_no,
                "item": row.get("item", ""),
                "severity": "error",
                "column": "batch_id",
                "issue": "invalid_batch_id",
                "value": row["batch_id"],
            }
        )
    if row.get("crawl_datetime"):
        try:
            datetime.strptime(row["crawl_datetime"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            issues.append(
                {
                    "source": source_name,
                    "row_no": row_no,
                    "item": row.get("item", ""),
                    "severity": "error",
                    "column": "crawl_datetime",
                    "issue": "invalid_datetime_format",
                    "value": row["crawl_datetime"],
                }
            )
    return issues


def dry_run_source(
    source_path: Path,
    output_name: str,
    detail_fallback_by_item: Dict[str, Dict[str, str]] | None = None,
    listing_fallback_by_item: Dict[str, Dict[str, str]] | None = None,
    allowed_items: set[str] | None = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    rows = read_csv(source_path)
    source_headers = list(rows[0].keys()) if rows else []
    normalized: List[Dict[str, str]] = []
    fallback_used = 0
    skipped_not_in_rank_window = 0
    for row in rows:
        norm = normalize_row(row)
        item = norm.get("item", "")
        if allowed_items is not None and item not in allowed_items:
            skipped_not_in_rank_window += 1
            continue
        before = dict(norm)
        if listing_fallback_by_item:
            merge_listing_fallback(norm, listing_fallback_by_item.get(norm.get("item", ""), {}))
        if detail_fallback_by_item:
            merge_detail_fallback(norm, detail_fallback_by_item.get(norm.get("item", ""), {}))
            norm = normalize_row(norm)
        if norm != before:
            fallback_used += 1
        normalized.append(norm)
    output_path = FINAL_DIR / output_name
    write_csv(output_path, normalized, EXPECTED_DB_COLUMNS)

    issues: List[Dict[str, Any]] = []
    for idx, row in enumerate(normalized, start=2):
        issues.extend(validate_row(row, source_path.name, idx))

    duplicate_items = sorted(
        item for item in {row.get("item", "") for row in normalized} if item and sum(1 for r in normalized if r.get("item") == item) > 1
    )
    for item in duplicate_items:
        issues.append(
            {
                "source": source_path.name,
                "row_no": "",
                "item": item,
                "severity": "error",
                "column": "item",
                "issue": "duplicate_item_in_insert_candidate",
                "value": item,
            }
        )

    extra_headers = [header for header in source_headers if header not in EXPECTED_DB_COLUMNS]
    missing_headers = [header for header in EXPECTED_DB_COLUMNS if header not in source_headers]
    excluded_headers_present = [header for header in source_headers if header in EXCLUDED_COLUMNS]

    sample_insert_sql = (
        "INSERT INTO tv_retail_com ("
        + ", ".join(EXPECTED_DB_COLUMNS)
        + ") VALUES ("
        + ", ".join(["%s"] * len(EXPECTED_DB_COLUMNS))
        + ")"
    )
    return (
        {
            "source": str(source_path),
            "output": str(output_path),
            "source_rows": len(rows),
            "output_rows": len(normalized),
            "skipped_not_in_rank_window": skipped_not_in_rank_window,
            "source_columns": len(source_headers),
            "db_insert_columns": len(EXPECTED_DB_COLUMNS),
            "extra_source_columns_ignored": extra_headers,
            "missing_expected_columns": missing_headers,
            "excluded_columns_present_in_source": excluded_headers_present,
            "duplicate_item_count": len(duplicate_items),
            "detail_fallback_rows_used": fallback_used,
            "sample_insert_sql": sample_insert_sql,
            "first_item": normalized[0].get("item") if normalized else "",
        },
        issues,
    )


def build_listing_fallback_by_item() -> Dict[str, Dict[str, str]]:
    candidate_paths = [
        FINAL_DIR / "all_items.csv",
        LISTING_FULL_DIR / "all_items.csv",
        FINAL_DIR / "all_unique_items.csv",
        LISTING_FULL_DIR / "all_unique_items.csv",
    ]
    listing_path = next((path for path in candidate_paths if path.exists()), None)
    merged: Dict[str, Dict[str, str]] = {}
    if listing_path is None:
        return merged

    source_rows = read_csv(listing_path)

    def rank_value(raw: Dict[str, str], field: str) -> int | None:
        value = normalize_int_text(raw.get(field))
        if not value or not re.fullmatch(r"\d+", value):
            return None
        return int(value)

    def top_ranked(field: str, limit: int) -> List[Dict[str, str]]:
        candidates = [raw for raw in source_rows if rank_value(raw, field) is not None and raw.get("item")]
        candidates.sort(key=lambda raw: rank_value(raw, field) or 999999)
        selected: List[Dict[str, str]] = []
        seen: set[str] = set()
        for raw in candidates:
            item = (raw.get("item") or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            selected.append(raw)
            if len(selected) >= limit:
                break
        return selected

    def merge_listing_fields(current: Dict[str, str], row: Dict[str, str]) -> None:
        for field in [
            "retailer_sku_name",
            "product_url",
            "offer",
            "pick_up_availability",
            "fastest_delivery",
            "delivery_availability",
            "sku_status",
            "available_quantity_for_purchase",
            "inventory_status",
            "number_of_ppl_purchased_yesterday",
            "number_of_ppl_added_to_carts",
        ]:
            if blank(current.get(field)) and not blank(row.get(field)):
                current[field] = row[field]

    for normalized_rank, raw in enumerate(top_ranked("main_rank", MAIN_RANK_LIMIT), start=1):
        row = normalize_row(raw)
        item = row.get("item", "")
        current = merged.setdefault(item, {})
        current["page_type"] = "main"
        current["main_rank"] = str(normalized_rank)
        merge_listing_fields(current, row)

    for normalized_rank, raw in enumerate(top_ranked("bsr_rank", BSR_RANK_LIMIT), start=1):
        row = normalize_row(raw)
        item = row.get("item", "")
        current = merged.setdefault(item, {})
        if blank(current.get("page_type")):
            current["page_type"] = "bsr"
        current["bsr_rank"] = str(normalized_rank)
        merge_listing_fields(current, row)
    return merged


def main() -> int:
    detail_rows = [normalize_row(row) for row in read_csv(FINAL_DIR / "db_insert_detail_items.csv")]
    detail_fallback_by_item = {row.get("item", ""): row for row in detail_rows if row.get("item")}
    listing_fallback_by_item = build_listing_fallback_by_item()
    allowed_items = set(listing_fallback_by_item)
    sources = [
        (
            FINAL_DIR / "db_insert_review_items.csv",
            "db_insert_review_items_wmart_dt_shape.csv",
            detail_fallback_by_item,
            listing_fallback_by_item,
            allowed_items,
        ),
        (
            FINAL_DIR / "db_insert_detail_items.csv",
            "db_insert_detail_items_wmart_dt_shape.csv",
            None,
            listing_fallback_by_item,
            allowed_items,
        ),
    ]
    reports: List[Dict[str, Any]] = []
    all_issues: List[Dict[str, Any]] = []
    for source_path, output_name, detail_fallback, listing_fallback, allowed in sources:
        report, issues = dry_run_source(source_path, output_name, detail_fallback, listing_fallback, allowed)
        reports.append(report)
        all_issues.extend(issues)

    issue_path = FINAL_DIR / "db_insert_dry_run_issues.csv"
    issue_fields = ["source", "row_no", "item", "severity", "column", "issue", "value"]
    write_csv(issue_path, all_issues, issue_fields)

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "db_free_dry_run_no_connection",
        "table": "tv_retail_com",
        "source_of_truth": "wmart_origin_code/tv/wmart_tv_dt.py EXTRACTED_FIELDS + PASSTHROUGH_FIELDS + SAVE_META_FIELDS",
        "rank_scope": {
            "main_rank": f"top {MAIN_RANK_LIMIT} accepted listing items renumbered 1..{MAIN_RANK_LIMIT}",
            "bsr_rank": f"top {BSR_RANK_LIMIT} accepted listing items renumbered 1..{BSR_RANK_LIMIT}",
            "allowed_item_count": len(allowed_items),
        },
        "reports": reports,
        "issue_count": len(all_issues),
        "error_count": sum(1 for issue in all_issues if issue.get("severity") == "error"),
        "warning_count": sum(1 for issue in all_issues if issue.get("severity") == "warning"),
        "issues_csv": str(issue_path),
    }
    summary_path = FINAL_DIR / "db_insert_dry_run_report.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
