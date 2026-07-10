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
REF_ITEM_MST_TABLE = os.getenv("LOWES_REF_ITEM_MST_TABLE", "ref_item_mst").strip() or "ref_item_mst"
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

REF_ITEM_MST_COLUMNS = [
    "sku",
    "ref_capacity",
    "ref_refrigerator_type",
    "product_url",
    "retailer_sku_name",
    "account_name",
]

INT_COLUMNS = {"main_rank", "bsr_rank"}
REF_ITEM_MST_FILL_COLUMNS = [
    "ref_capacity",
    "ref_refrigerator_type",
    "product_url",
    "retailer_sku_name",
]


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


def is_blank(value):
    return value in ("", None) or str(value).strip() == ""


def table_exists(cur, table_name=None):
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        )
        """,
        (TARGET_SCHEMA, table_name or TARGET_TABLE),
    )
    return bool(cur.fetchone()[0])


def existing_table_columns(cur, table_name=None):
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (TARGET_SCHEMA, table_name or TARGET_TABLE),
    )
    return {row[0] for row in cur.fetchall()}


def ensure_ref_item_mst_table(cur):
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(TARGET_SCHEMA)}")
    column_defs = ",\n          ".join(f"{quote_ident(column_name)} text" for column_name in REF_ITEM_MST_COLUMNS)
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(TARGET_SCHEMA)}.{quote_ident(REF_ITEM_MST_TABLE)} (
          id bigserial PRIMARY KEY,
          {column_defs}
        )
        """
    )
    existing = existing_table_columns(cur, REF_ITEM_MST_TABLE)
    for column_name in REF_ITEM_MST_COLUMNS:
        if column_name in existing:
            continue
        cur.execute(
            f"""
            ALTER TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(REF_ITEM_MST_TABLE)}
            ADD COLUMN IF NOT EXISTS {quote_ident(column_name)} text
            """
        )
    index_prefix = REF_ITEM_MST_TABLE[:45]
    indexes = [
        (f"idx_{index_prefix}_sku", "sku"),
        (f"idx_{index_prefix}_account_sku", "account_name, sku"),
    ]
    for index_name, column_sql in indexes:
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {quote_ident(index_name)}
            ON {quote_ident(TARGET_SCHEMA)}.{quote_ident(REF_ITEM_MST_TABLE)}
            USING btree ({", ".join(quote_ident(part.strip()) for part in column_sql.split(","))})
            """
        )


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


def build_ref_item_mst_rows(mapped_rows):
    if PRODUCT_TYPE != "REF":
        return []
    rows_by_key = {}
    for row in mapped_rows:
        sku = str(row.get("sku") or "").strip()
        if not sku:
            continue
        account_name = str(row.get("account_name") or "Lowes").strip() or "Lowes"
        key = (account_name, sku)
        current = rows_by_key.setdefault(
            key,
            {
                "sku": sku,
                "ref_capacity": "",
                "ref_refrigerator_type": "",
                "product_url": "",
                "retailer_sku_name": "",
                "account_name": account_name,
            },
        )
        for column in REF_ITEM_MST_COLUMNS:
            if column in {"sku", "account_name"}:
                continue
            value = str(row.get(column) or "").strip()
            if not value:
                continue
            current_value = str(current.get(column) or "").strip()
            if not current_value or len(value) > len(current_value):
                current[column] = value
    return list(rows_by_key.values())


def fetch_ref_item_mst_map(cur, mapped_rows):
    if PRODUCT_TYPE != "REF":
        return {}
    existing = existing_table_columns(cur, REF_ITEM_MST_TABLE)
    select_columns = [c for c in REF_ITEM_MST_COLUMNS if c in existing]
    if not {"sku", "account_name"}.issubset(select_columns):
        return {}
    keys = sorted({
        (str(row.get("account_name") or "Lowes").strip() or "Lowes", str(row.get("sku") or "").strip())
        for row in mapped_rows
        if str(row.get("sku") or "").strip()
    })
    master_rows = {}
    column_sql = ", ".join(quote_ident(c) for c in select_columns)
    for account_name, sku in keys:
        cur.execute(
            f"""
            SELECT {column_sql}
            FROM {quote_ident(TARGET_SCHEMA)}.{quote_ident(REF_ITEM_MST_TABLE)}
            WHERE account_name = %s AND sku = %s
            LIMIT 1
            """,
            (account_name, sku),
        )
        row = cur.fetchone()
        if row:
            master_rows[(account_name, sku)] = dict(zip(select_columns, row))
    return master_rows


def hydrate_rows_from_ref_item_mst(cur, mapped_rows):
    if PRODUCT_TYPE != "REF" or not mapped_rows:
        return 0
    master_rows = fetch_ref_item_mst_map(cur, mapped_rows)
    filled = 0
    for row in mapped_rows:
        sku = str(row.get("sku") or "").strip()
        account_name = str(row.get("account_name") or "Lowes").strip() or "Lowes"
        master = master_rows.get((account_name, sku))
        if not master:
            continue
        for column in REF_ITEM_MST_FILL_COLUMNS:
            value = master.get(column)
            if is_blank(row.get(column)) and not is_blank(value):
                row[column] = value
                filled += 1
    return filled


def sync_ref_item_mst_rows(cur, mst_rows):
    if PRODUCT_TYPE != "REF" or not mst_rows:
        return 0, 0, []
    if not table_exists(cur, REF_ITEM_MST_TABLE):
        raise RuntimeError(f"DB table not found: {TARGET_SCHEMA}.{REF_ITEM_MST_TABLE}")
    existing = existing_table_columns(cur, REF_ITEM_MST_TABLE)
    insert_columns = [c for c in REF_ITEM_MST_COLUMNS if c in existing]
    missing_required = {"sku", "account_name"} - set(insert_columns)
    if missing_required:
        raise RuntimeError(
            f"DB table missing required columns: {TARGET_SCHEMA}.{REF_ITEM_MST_TABLE} "
            f"{sorted(missing_required)}"
        )
    insert_column_sql = ", ".join(quote_ident(c) for c in insert_columns)
    insert_placeholders = ", ".join(["%s"] * len(insert_columns))
    insert_sql = (
        f"INSERT INTO {quote_ident(TARGET_SCHEMA)}.{quote_ident(REF_ITEM_MST_TABLE)} "
        f"({insert_column_sql}) VALUES ({insert_placeholders})"
    )
    inserted = 0
    updated_cells = 0
    for row in mst_rows:
        account_name = str(row.get("account_name") or "Lowes").strip() or "Lowes"
        sku = str(row.get("sku") or "").strip()
        if not sku:
            continue
        cur.execute(
            f"SELECT EXISTS (SELECT 1 FROM {quote_ident(TARGET_SCHEMA)}.{quote_ident(REF_ITEM_MST_TABLE)} "
            f"WHERE account_name = %s AND sku = %s)",
            (account_name, sku),
        )
        exists = bool(cur.fetchone()[0])
        if not exists:
            cur.execute(insert_sql, tuple(empty_to_none(row.get(c), c) for c in insert_columns))
            inserted += 1
            continue
        for column in REF_ITEM_MST_FILL_COLUMNS:
            if column not in existing:
                continue
            value = str(row.get(column) or "").strip()
            if not value:
                continue
            cur.execute(
                f"""
                UPDATE {quote_ident(TARGET_SCHEMA)}.{quote_ident(REF_ITEM_MST_TABLE)}
                SET {quote_ident(column)} = %s
                WHERE account_name = %s
                  AND sku = %s
                  AND ({quote_ident(column)} IS NULL OR btrim({quote_ident(column)}) = '')
                """,
                (value, account_name, sku),
            )
            updated_cells += cur.rowcount
    return inserted, updated_cells, insert_columns


def main():
    started_at = now()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if not FINAL_OUTPUT_CSV.exists():
        raise RuntimeError(f"final output CSV not found: {FINAL_OUTPUT_CSV}")

    rows = read_rows(FINAL_OUTPUT_CSV)
    mapped_rows = [map_row(r) for r in rows]
    ref_item_mst_rows = build_ref_item_mst_rows(mapped_rows)

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
            "ref_item_mst": {
                "table": REF_ITEM_MST_TABLE if PRODUCT_TYPE == "REF" else None,
                "planned_rows": len(ref_item_mst_rows),
                "planned_columns": REF_ITEM_MST_COLUMNS if PRODUCT_TYPE == "REF" else [],
                "sample_mapped_row": ref_item_mst_rows[0] if ref_item_mst_rows else None,
            },
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
            mst_filled_cells = 0
            mst_inserted = 0
            mst_updated_cells = 0
            mst_columns_used = []
            if PRODUCT_TYPE == "REF":
                ensure_ref_item_mst_table(cur)
                mst_filled_cells = hydrate_rows_from_ref_item_mst(cur, mapped_rows)
                ref_item_mst_rows = build_ref_item_mst_rows(mapped_rows)
            deleted = delete_existing_batch(cur, mapped_rows)
            inserted, columns_used = insert_rows(cur, mapped_rows)
            if PRODUCT_TYPE == "REF":
                mst_inserted, mst_updated_cells, mst_columns_used = sync_ref_item_mst_rows(cur, ref_item_mst_rows)
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
        "ref_item_mst": {
            "table": REF_ITEM_MST_TABLE if PRODUCT_TYPE == "REF" else None,
            "mapped_rows": len(ref_item_mst_rows),
            "filled_retail_cells_from_mst": mst_filled_cells if PRODUCT_TYPE == "REF" else 0,
            "inserted": mst_inserted if PRODUCT_TYPE == "REF" else 0,
            "updated_empty_cells": mst_updated_cells if PRODUCT_TYPE == "REF" else 0,
            "inserted_columns": mst_columns_used if PRODUCT_TYPE == "REF" else [],
        },
        "success": True,
        "skipped": False,
        "dry_run": False,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
