"""Load final_output.csv into the shared retailer-com table (ref_retail_com / ldy_retail_com).

Column mapping (our pipeline name -> DB name):
  product_type/LOWES_PRODUCT_TYPE -> product
  item/omni_item_id/item_number   -> item
  crawl_strdatetime/crawl_datetime -> crawl_strdatetime
  + calendar_week derived from current ISO week
  + account_name defaults to "Lowes"
"""
import csv
import json
import os
from datetime import datetime
from pathlib import Path

from .step00_config import (
    DEFAULT_LOWES_RUN_ROOT,
    db_config,
    lowes_output_table,
    lowes_product_type,
    lowes_run_date,
    rel_path,
)
from .step00_erd_schema import output_page_type, retailer_sku_name_text


RUN_DATE = lowes_run_date()
RUN_ROOT = Path(os.getenv("LOWES_RUN_ROOT", str(DEFAULT_LOWES_RUN_ROOT)))
OUTPUT_ROOT = Path(os.getenv("LOWES_OUTPUT_ROOT", str(RUN_ROOT / "output")))
FINAL_OUTPUT_CSV = Path(os.getenv("LOWES_FINAL_OUTPUT_CSV", str(OUTPUT_ROOT / "final_output.csv")))
MANIFEST_PATH = OUTPUT_ROOT / "db_load_manifest.json"
TARGET_SCHEMA = os.getenv("LOWES_DB_SCHEMA", "public").strip() or "public"
TARGET_TABLE = lowes_output_table()
PRODUCT_TYPE = (lowes_product_type() or "REF").upper()
DRY_RUN = os.getenv("LOWES_DB_LOAD_DRY_RUN", os.getenv("LOWES_DB_DRY_RUN", "0")).strip().lower() in {
    "1", "true", "yes", "y",
}


# DB column order — these are the columns we will attempt to write
# (only those that actually exist in the DB table will be used)
DB_INSERT_COLUMNS = [
    "country",
    "product",
    "item",
    "account_name",
    "page_type",
    "count_of_reviews",
    "retailer_sku_name",
    "product_url",
    "star_rating",
    "count_of_star_ratings",
    "final_sku_price",
    "original_sku_price",
    "savings",
    "pick_up_availability",
    "delivery_availability",
    "sku_status",
    "detailed_review_content",
    "recommendation_intent",
    "main_rank",
    "bsr_rank",
    "retailer_sku_name_similar",
    "calendar_week",
    "crawl_strdatetime",
    "batch_id",
    "discount_type",
    "sku_popularity",
    "sku",
    "number_of_units_purchased_past_week",
    "fastest_delivery",
    "available_quantity_for_purchase_pickup",
    "available_quantity_for_purchase_delivery",
    "available_quantity_for_purchase_fastdelivery",
    "summarized_review_content",
]

CATEGORY_INSERT_COLUMNS = {
    "REF": ["ref_capacity", "ref_refrigerator_type"],
    "LDY": ["ldy_capacity", "ldy_loading_type"],
}

INT_COLUMNS = {"main_rank", "bsr_rank"}


def now():
    return datetime.now().isoformat(timespec="seconds")


def quote_ident(value):
    return '"' + str(value).replace('"', '""') + '"'


def read_rows(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def as_int(value):
    try:
        if value in ("", None):
            return None
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def calendar_week_now():
    return f"w{datetime.now().isocalendar().week:02d}"


def map_row(row):
    """Map a pipeline CSV row to DB column names. Returns dict keyed by DB column name."""
    out = {}
    out["country"] = row.get("country") or "SEA"
    out["product"] = row.get("product") or row.get("product_type") or PRODUCT_TYPE
    out["account_name"] = row.get("account_name") or "Lowes"
    out["batch_id"] = row.get("batch_id", "")
    out["calendar_week"] = row.get("calendar_week") or calendar_week_now()
    out["crawl_strdatetime"] = row.get("crawl_strdatetime") or row.get("crawl_datetime", "")
    out["page_type"] = output_page_type(row)
    out["main_rank"] = as_int(row.get("main_rank"))
    out["bsr_rank"] = as_int(row.get("bsr_rank"))
    out["product_url"] = row.get("product_url", "")
    out["item"] = row.get("item") or row.get("omni_item_id", "") or row.get("item_number", "")
    out["sku"] = row.get("sku", "")
    out["retailer_sku_name"] = retailer_sku_name_text(row)
    out["final_sku_price"] = row.get("final_sku_price", "")
    out["original_sku_price"] = row.get("original_sku_price", "")
    out["savings"] = row.get("savings", "")
    out["star_rating"] = row.get("star_rating", "")
    out["count_of_reviews"] = row.get("count_of_reviews", "")
    out["count_of_star_ratings"] = row.get("count_of_star_ratings", "")
    out["discount_type"] = row.get("discount_type", "")
    out["sku_popularity"] = row.get("sku_popularity", "")
    out["sku_status"] = row.get("sku_status", "")
    out["number_of_units_purchased_past_week"] = row.get("number_of_units_purchased_past_week", "")
    out["pick_up_availability"] = row.get("pick_up_availability", "")
    out["delivery_availability"] = row.get("delivery_availability", "")
    out["fastest_delivery"] = row.get("fastest_delivery", "")
    out["available_quantity_for_purchase_pickup"] = row.get("available_quantity_for_purchase_pickup", "")
    out["available_quantity_for_purchase_delivery"] = row.get("available_quantity_for_purchase_delivery", "")
    out["available_quantity_for_purchase_fastdelivery"] = row.get("available_quantity_for_purchase_fastdelivery", "")
    out["recommendation_intent"] = row.get("recommendation_intent", "")
    out["summarized_review_content"] = row.get("summarized_review_content", "")
    out["detailed_review_content"] = row.get("detailed_review_content", "")
    out["retailer_sku_name_similar"] = row.get("retailer_sku_name_similar", "")
    out["ref_refrigerator_type"] = row.get("ref_refrigerator_type", "")
    out["ref_capacity"] = row.get("ref_capacity", "")
    out["ldy_loading_type"] = row.get("ldy_loading_type", "")
    out["ldy_capacity"] = row.get("ldy_capacity", "")
    return out


def empty_to_none(value, column):
    if column in INT_COLUMNS:
        return as_int(value)
    if value in ("", None):
        return None
    return value


def table_exists(cur):
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        )
        """,
        (TARGET_SCHEMA, TARGET_TABLE),
    )
    return bool(cur.fetchone()[0])


def existing_table_columns(cur):
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (TARGET_SCHEMA, TARGET_TABLE),
    )
    return {row[0] for row in cur.fetchall()}


def delete_existing_batch(cur, mapped_rows):
    batch_ids = sorted({str(r.get("batch_id") or "").strip() for r in mapped_rows if r.get("batch_id")})
    if not batch_ids:
        return 0
    cur.execute(
        f"DELETE FROM {quote_ident(TARGET_SCHEMA)}.{quote_ident(TARGET_TABLE)} "
        f"WHERE batch_id = ANY(%s) AND account_name = %s",
        (batch_ids, "Lowes"),
    )
    return cur.rowcount


def insert_rows(cur, mapped_rows):
    if not mapped_rows:
        return 0
    existing = existing_table_columns(cur)
    planned_columns = DB_INSERT_COLUMNS + CATEGORY_INSERT_COLUMNS.get(PRODUCT_TYPE, [])
    insert_columns = [c for c in planned_columns if c in existing]
    column_sql = ", ".join(quote_ident(c) for c in insert_columns)
    placeholders = ", ".join(["%s"] * len(insert_columns))
    sql = (
        f"INSERT INTO {quote_ident(TARGET_SCHEMA)}.{quote_ident(TARGET_TABLE)} "
        f"({column_sql}) VALUES ({placeholders})"
    )
    values = [
        tuple(empty_to_none(r.get(c), c) for c in insert_columns)
        for r in mapped_rows
    ]
    cur.executemany(sql, values)
    return len(values), insert_columns


def main():
    started_at = now()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if not FINAL_OUTPUT_CSV.exists():
        raise RuntimeError(f"final output CSV not found: {FINAL_OUTPUT_CSV}")

    rows = read_rows(FINAL_OUTPUT_CSV)
    mapped_rows = [map_row(r) for r in rows]

    if DRY_RUN:
        manifest = {
            "run_type": "step14_db_load",
            "started_at": started_at,
            "finished_at": now(),
            "run_date": RUN_DATE,
            "product_type": PRODUCT_TYPE,
            "final_output_csv": rel_path(FINAL_OUTPUT_CSV),
            "schema": TARGET_SCHEMA,
            "table": TARGET_TABLE,
            "csv_rows": len(rows),
            "mapped_rows": len(mapped_rows),
            "success": True,
            "skipped": True,
            "dry_run": True,
            "planned_columns": DB_INSERT_COLUMNS + CATEGORY_INSERT_COLUMNS.get(PRODUCT_TYPE, []),
            "sample_mapped_row": mapped_rows[0] if mapped_rows else None,
        }
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return

    config = db_config()
    if not config:
        raise RuntimeError("DB_CONFIG is missing")
    if not config.get("database"):
        config["database"] = "postgres"

    import psycopg2

    conn = psycopg2.connect(
        host=config.get("host"),
        port=int(config.get("port") or 5432),
        user=config.get("user"),
        password=config.get("password"),
        dbname=config.get("database"),
        connect_timeout=10,
    )
    with conn:
        with conn.cursor() as cur:
            if not table_exists(cur):
                raise RuntimeError(f"DB table not found: {TARGET_SCHEMA}.{TARGET_TABLE}")
            deleted = delete_existing_batch(cur, mapped_rows)
            inserted, columns_used = insert_rows(cur, mapped_rows)
    conn.close()

    manifest = {
        "run_type": "step14_db_load",
        "started_at": started_at,
        "finished_at": now(),
        "run_date": RUN_DATE,
        "product_type": PRODUCT_TYPE,
        "final_output_csv": rel_path(FINAL_OUTPUT_CSV),
        "schema": TARGET_SCHEMA,
        "table": TARGET_TABLE,
        "csv_rows": len(rows),
        "deleted_existing": deleted,
        "inserted": inserted,
        "inserted_columns": columns_used,
        "success": True,
        "skipped": False,
        "dry_run": False,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
