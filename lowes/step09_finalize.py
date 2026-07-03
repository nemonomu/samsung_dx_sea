"""Produce final_output.csv aligned with ERD column order + spec rules applied.

Inputs:
  - detail_enriched_rows.csv (step08 output)

Outputs:
  - final_output.csv (ERD-aligned, DB-ready)

Rules:
  - Price fallback: listing (selling_price/was_price/total_saving) -> detail (mfePrice)
  - Price format: drop .00 when integer, comma at 4 digits
  - Savings: no "Save " prefix (per spec)
  - account_name="Lowes", country="SEA" defaults
"""
import csv
import json
import os
from datetime import datetime
from pathlib import Path

from .step00_erd_schema import erd_field_order, output_page_type, retailer_sku_name_text
from .step00_config import (
    DEFAULT_LOWES_RUN_ROOT,
    lowes_product_type,
    lowes_run_date,
    rel_path,
)


PRODUCT_TYPE = (lowes_product_type() or "REF").upper()
RUN_DATE = lowes_run_date()
RUN_ROOT = Path(os.getenv("LOWES_RUN_ROOT", str(DEFAULT_LOWES_RUN_ROOT)))
DETAIL_CSV = Path(os.getenv("LOWES_DETAIL_CSV", str(RUN_ROOT / "detail" / "parsed" / "detail_enriched_rows.csv")))
OUT_CSV = Path(os.getenv("LOWES_FINAL_OUTPUT_CSV", str(RUN_ROOT / "output" / "final_output.csv")))
MANIFEST_PATH = Path(os.getenv("LOWES_FINALIZE_MANIFEST", str(RUN_ROOT / "output" / "manifest_finalize.json")))

FIELDNAMES = erd_field_order(PRODUCT_TYPE)
CATEGORY_COLUMNS = ["ldy_capacity", "ldy_loading_type"] if PRODUCT_TYPE == "LDY" else [
    "ref_capacity",
    "ref_refrigerator_type",
]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def to_number(value):
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("$", "").replace(",", "")
    if s.lower().startswith("save "):
        s = s[5:]
    try:
        return float(s)
    except ValueError:
        return None


def money(value):
    n = to_number(value)
    if n is None:
        return ""
    return f"${int(n):,}" if n.is_integer() else f"${n:,.2f}"


def format_rating(value):
    """0.0/1.0/2.0/3.0/4.0/5.0 -> '0'/'1'/.../'5'. 4.5 stays '4.5'."""
    if value in ("", None):
        return ""
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f.is_integer() else str(value).strip()


def first_present(*values):
    for v in values:
        if v not in ("", None):
            return v
    return ""


def calendar_week_now():
    return f"w{datetime.now().isocalendar().week:02d}"


def finalize_row(row, batch_id, crawl_dt):
    o = {col: "" for col in FIELDNAMES}
    o["account_name"] = "Lowes"
    o["country"] = "SEA"
    o["product"] = row.get("product", "") or PRODUCT_TYPE
    o["item"] = row.get("item", "") or row.get("omni_item_id", "") or row.get("item_number", "")
    o["calendar_week"] = row.get("calendar_week", "") or calendar_week_now()
    o["crawl_strdatetime"] = row.get("crawl_strdatetime", "") or row.get("crawl_datetime", "") or crawl_dt
    o["batch_id"] = row.get("batch_id", "") or batch_id
    o["page_type"] = output_page_type(row)
    o["product_url"] = row.get("product_url", "")
    o["main_rank"] = row.get("main_rank", "")
    o["bsr_rank"] = row.get("bsr_rank", "")

    o["retailer_sku_name"] = retailer_sku_name_text(row)

    # price fallback: listing → detail (mfePrice)
    listing_selling = row.get("selling_price", "") or row.get("final_sku_price", "")
    listing_was = row.get("was_price", "") or row.get("original_sku_price", "")
    listing_save = row.get("total_saving", "") or row.get("savings", "")
    detail_selling = row.get("_detail_selling_price", "")
    detail_was = row.get("_detail_was_price", "")
    detail_save = row.get("_detail_total_saving", "")

    selling = first_present(listing_selling, detail_selling)
    was = first_present(listing_was, detail_was)
    save = first_present(listing_save, detail_save)
    sn, wn = to_number(selling), to_number(was)
    if sn is not None and wn is not None and abs(sn - wn) < 0.01:
        was = ""
        save = ""
    o["final_sku_price"] = money(selling)
    o["original_sku_price"] = money(was)
    o["savings"] = money(save)

    # star_rating / count fallback: listing → detail.reviewStatistics
    # NULL = 수집 실패 (XHR error 등). 0 = 평가/리뷰 실재 없음 (page에서 0/Be the first).
    listing_rating = row.get("star_rating", "") or row.get("rating", "")
    listing_review_count = row.get("count_of_reviews", "") or row.get("review_count", "")
    detail_avg_rating = row.get("_pdp_average_rating", "")
    detail_total_reviews = row.get("_pdp_total_reviews", "")
    o["star_rating"] = format_rating(listing_rating if str(listing_rating).strip() else detail_avg_rating)
    fallback_count = listing_review_count if str(listing_review_count).strip() else detail_total_reviews
    o["count_of_reviews"] = fallback_count
    o["count_of_star_ratings"] = fallback_count
    o["discount_type"] = row.get("discount_type", "")
    o["sku_popularity"] = row.get("sku_popularity", "")
    o["sku_status"] = row.get("sku_status", "")

    for col in (
        "sku",
        "number_of_units_purchased_past_week",
        "pick_up_availability",
        "delivery_availability",
        "fastest_delivery",
        "available_quantity_for_purchase_pickup",
        "available_quantity_for_purchase_delivery",
        "available_quantity_for_purchase_fastdelivery",
        "recommendation_intent",
        "summarized_review_content",
        "detailed_review_content",
        "retailer_sku_name_similar",
    ):
        o[col] = row.get(col, "")

    for col in CATEGORY_COLUMNS:
        o[col] = row.get(col, "")
    return o


def main():
    started = now_iso()
    if not DETAIL_CSV.exists():
        raise SystemExit(f"detail CSV not found: {DETAIL_CSV}")
    with DETAIL_CSV.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"detail rows: {len(rows)}")

    now = datetime.now()
    batch_id = os.getenv("LOWES_BATCH_ID") or ("l_" + now.strftime("%y%m%d_%H%M%S"))
    crawl_dt = now.strftime("%Y-%m-%d %H:%M:%S")

    out_rows = [finalize_row(r, batch_id, crawl_dt) for r in rows]

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = FIELDNAMES
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)
    print(f"wrote {OUT_CSV}  rows={len(out_rows)}  columns={len(fieldnames)}")

    manifest = {
        "run_type": "step09_finalize",
        "started_at": started,
        "finished_at": now_iso(),
        "product_type": PRODUCT_TYPE,
        "run_date": RUN_DATE,
        "detail_csv": rel_path(DETAIL_CSV),
        "final_output_csv": rel_path(OUT_CSV),
        "rows": len(out_rows),
        "columns": len(fieldnames),
        "category_columns": CATEGORY_COLUMNS,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
