from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


MAIN_RANK_LIMIT = 300
BSR_RANK_LIMIT = 100
EXTRACTED_FIELDS = [
    "item", "count_of_reviews", "star_rating", "count_of_star_ratings",
    "final_sku_price", "original_sku_price", "savings", "discount_type",
    "sku_popularity", "number_of_ppl_purchased_yesterday",
    "number_of_ppl_added_to_carts", "model_year", "screen_size",
    "retailer_sku_name_similar", "detailed_review_content",
]
PASSTHROUGH_FIELDS = [
    "page_type", "retailer_sku_name", "product_url", "offer",
    "pick_up_availability", "fastest_delivery", "delivery_availability",
    "sku_status", "available_quantity_for_purchase", "inventory_status",
    "main_rank", "bsr_rank", "calendar_week",
]
META_FIELDS = ["crawl_datetime", "account_name", "batch_id", "country"]
EXPECTED_DB_COLUMNS = EXTRACTED_FIELDS + PASSTHROUGH_FIELDS + META_FIELDS
INTEGER_COLUMNS = {
    "count_of_reviews", "count_of_star_ratings", "main_rank", "bsr_rank",
    "number_of_ppl_purchased_yesterday", "number_of_ppl_added_to_carts",
    "available_quantity_for_purchase",
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


def current_calendar_week() -> str:
    return f"w{datetime.now().isocalendar().week}"


def calendar_week_from_datetime(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return current_calendar_week()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d_%H%M%S", "%Y%m%d%H%M%S"):
        try:
            return f"w{datetime.strptime(text, fmt).isocalendar().week}"
        except ValueError:
            continue
    return current_calendar_week()


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


def rank_value(raw: Dict[str, str], field: str) -> int | None:
    value = normalize_int_text(raw.get(field))
    if not value or not re.fullmatch(r"\d+", value):
        return None
    return int(value)


def top_ranked(rows: List[Dict[str, str]], field: str, limit: int) -> List[Dict[str, str]]:
    candidates = [raw for raw in rows if rank_value(raw, field) is not None and raw.get("item")]
    candidates.sort(key=lambda raw: rank_value(raw, field) or 999999)
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for raw in candidates:
        item = (raw.get("item") or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(raw)
        if len(out) >= limit:
            break
    return out


def build_listing_fallback(out_dir: Path) -> Dict[str, Dict[str, str]]:
    source_rows = read_csv(out_dir / "all_items.csv")
    merged: Dict[str, Dict[str, str]] = {}

    def merge_fields(current: Dict[str, str], row: Dict[str, str]) -> None:
        for field in [
            "retailer_sku_name", "product_url", "offer", "pick_up_availability",
            "fastest_delivery", "delivery_availability", "sku_status",
            "available_quantity_for_purchase", "inventory_status",
            "number_of_ppl_purchased_yesterday", "number_of_ppl_added_to_carts",
        ]:
            if blank(current.get(field)) and not blank(row.get(field)):
                current[field] = row[field]

    for idx, raw in enumerate(top_ranked(source_rows, "main_rank", MAIN_RANK_LIMIT), 1):
        row = normalize_row(raw)
        item = row.get("item", "")
        current = merged.setdefault(item, {})
        current["page_type"] = "main"
        current["main_rank"] = str(idx)
        merge_fields(current, row)
    for idx, raw in enumerate(top_ranked(source_rows, "bsr_rank", BSR_RANK_LIMIT), 1):
        row = normalize_row(raw)
        item = row.get("item", "")
        current = merged.setdefault(item, {})
        if blank(current.get("page_type")):
            current["page_type"] = "bsr"
        current["bsr_rank"] = str(idx)
        merge_fields(current, row)
    return merged


def merge_listing(row: Dict[str, str], listing: Dict[str, str]) -> None:
    if not listing:
        return
    for field in ["page_type", "main_rank", "bsr_rank"]:
        row[field] = listing.get(field, "")
    for field in [
        "retailer_sku_name", "product_url", "offer", "pick_up_availability",
        "fastest_delivery", "delivery_availability", "sku_status",
        "available_quantity_for_purchase", "inventory_status",
        "number_of_ppl_purchased_yesterday", "number_of_ppl_added_to_carts",
    ]:
        if blank(row.get(field)) and not blank(listing.get(field)):
            row[field] = listing[field]


def merge_detail(row: Dict[str, str], detail: Dict[str, str]) -> None:
    for field in [
        "star_rating", "count_of_star_ratings", "count_of_reviews", "final_sku_price",
        "original_sku_price", "savings", "discount_type", "sku_popularity",
        "model_year", "screen_size", "retailer_sku_name_similar", "offer",
    ]:
        if blank(row.get(field)) and not blank(detail.get(field)):
            row[field] = detail[field]


def apply_meta_defaults(row: Dict[str, str], meta_defaults: Dict[str, str]) -> None:
    for field, value in meta_defaults.items():
        if blank(row.get(field)) and value:
            row[field] = value
    if blank(row.get("calendar_week")):
        row["calendar_week"] = meta_defaults.get("calendar_week") or current_calendar_week()


def transform_source(
    out_dir: Path,
    source_name: str,
    output_name: str,
    listing_by_item: Dict[str, Dict[str, str]],
    detail_by_item: Dict[str, Dict[str, str]],
    meta_defaults: Dict[str, str],
) -> List[Dict[str, str]]:
    rows = read_csv(out_dir / source_name)
    allowed = set(listing_by_item)
    out: List[Dict[str, str]] = []
    for row in rows:
        norm = normalize_row(row)
        item = norm.get("item", "")
        if item not in allowed:
            continue
        merge_listing(norm, listing_by_item.get(item, {}))
        if source_name.startswith("db_insert_review"):
            merge_detail(norm, detail_by_item.get(item, {}))
        apply_meta_defaults(norm, meta_defaults)
        norm = normalize_row(norm)
        out.append(norm)
    write_csv(out_dir / output_name, out, EXPECTED_DB_COLUMNS)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--crawl-datetime", default="")
    parser.add_argument("--account-name", default="Walmart")
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--country", default="SEA")
    args = parser.parse_args()
    out_dir = args.out_dir.resolve()
    meta_defaults = {
        "crawl_datetime": args.crawl_datetime,
        "account_name": args.account_name,
        "batch_id": args.batch_id,
        "country": args.country,
        "calendar_week": calendar_week_from_datetime(args.crawl_datetime),
    }
    listing_by_item = build_listing_fallback(out_dir)
    detail_rows = [normalize_row(row) for row in read_csv(out_dir / "db_insert_detail_items.csv")]
    detail_by_item = {row.get("item", ""): row for row in detail_rows if row.get("item")}
    review_out = transform_source(out_dir, "db_insert_review_items.csv", "db_insert_review_items_wmart_dt_shape.csv", listing_by_item, detail_by_item, meta_defaults)
    detail_out = transform_source(out_dir, "db_insert_detail_items.csv", "db_insert_detail_items_wmart_dt_shape.csv", listing_by_item, {}, meta_defaults)
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "out_dir": str(out_dir),
        "allowed_item_count": len(listing_by_item),
        "review_output_rows": len(review_out),
        "detail_output_rows": len(detail_out),
        "main_rank_count": sum(1 for row in review_out if row.get("main_rank")),
        "bsr_rank_count": sum(1 for row in review_out if row.get("bsr_rank")),
    }
    (out_dir / "db_shape_transform_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
