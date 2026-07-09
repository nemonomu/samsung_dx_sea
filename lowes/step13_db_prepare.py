import json
import os
from datetime import datetime
from pathlib import Path

from .step00_erd_schema import erd_field_order
from .step00_config import (
    DEFAULT_LOWES_RUN_ROOT,
    db_config,
    lowes_output_table,
    lowes_product_type,
    lowes_run_date,
    rel_path,
)


RUN_DATE = lowes_run_date()
RUN_ROOT = Path(os.getenv("LOWES_RUN_ROOT", str(DEFAULT_LOWES_RUN_ROOT)))
OUTPUT_ROOT = Path(os.getenv("LOWES_OUTPUT_ROOT", str(RUN_ROOT / "output")))
TARGET_SCHEMA = os.getenv("LOWES_DB_SCHEMA", "public").strip() or "public"
TARGET_TABLE = lowes_output_table()
PRODUCT_TYPE = lowes_product_type().upper()
TARGET_COLUMNS = erd_field_order(PRODUCT_TYPE)
REF_ITEM_MST_TABLE = os.getenv("LOWES_REF_ITEM_MST_TABLE", "ref_item_mst").strip() or "ref_item_mst"
REF_ITEM_MST_COLUMNS = [
    "sku",
    "ref_capacity",
    "ref_refrigerator_type",
    "product_url",
    "retailer_sku_name",
    "account_name",
]
DRY_RUN = os.getenv("LOWES_DB_PREPARE_DRY_RUN", os.getenv("LOWES_DB_DRY_RUN", "0")).strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def quote_ident(value):
    return '"' + str(value).replace('"', '""') + '"'


def existing_columns(cur):
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (TARGET_SCHEMA, TARGET_TABLE),
    )
    return {row[0] for row in cur.fetchall()}


def ensure_table(cur):
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(TARGET_SCHEMA)}")
    column_defs = []
    for column_name in TARGET_COLUMNS:
        data_type = "integer" if column_name in {"main_rank", "bsr_rank"} else "text"
        column_defs.append(f"{quote_ident(column_name)} {data_type}")
    column_sql = ",\n          ".join(column_defs)
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(TARGET_SCHEMA)}.{quote_ident(TARGET_TABLE)} (
          id bigserial PRIMARY KEY,
          {column_sql}
        )
        """
    )
    columns = existing_columns(cur)
    for column_name in TARGET_COLUMNS:
        if column_name in columns or column_name == "id":
            continue
        if column_name in {"main_rank", "bsr_rank"}:
            data_type = "integer"
        else:
            data_type = "text"
        cur.execute(
            f"""
            ALTER TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(TARGET_TABLE)}
            ADD COLUMN IF NOT EXISTS {quote_ident(column_name)} {data_type}
            """
        )
    index_prefix = TARGET_TABLE[:45]
    indexes = [
        (f"idx_{index_prefix}_batch", "batch_id"),
        (f"idx_{index_prefix}_item", "item"),
        (f"idx_{index_prefix}_main_rank", "main_rank"),
    ]
    for index_name, column_name in indexes:
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {quote_ident(index_name)}
            ON {quote_ident(TARGET_SCHEMA)}.{quote_ident(TARGET_TABLE)}
            USING btree ({quote_ident(column_name)})
            """
        )


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
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (TARGET_SCHEMA, REF_ITEM_MST_TABLE),
    )
    columns = {row[0] for row in cur.fetchall()}
    for column_name in REF_ITEM_MST_COLUMNS:
        if column_name in columns:
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


def main():
    started_at = now()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if DRY_RUN:
        manifest = {
            "run_type": "step13_db_prepare",
            "started_at": started_at,
            "finished_at": now(),
            "run_date": RUN_DATE,
            "product_type": lowes_product_type().upper(),
            "run_root": rel_path(RUN_ROOT),
            "output_root": rel_path(OUTPUT_ROOT),
            "schema": TARGET_SCHEMA,
            "table": TARGET_TABLE,
            "ref_item_mst_table": REF_ITEM_MST_TABLE if PRODUCT_TYPE == "REF" else None,
            "success": True,
            "skipped": True,
            "dry_run": True,
            "planned_columns": TARGET_COLUMNS,
            "planned_ref_item_mst_columns": REF_ITEM_MST_COLUMNS if PRODUCT_TYPE == "REF" else [],
        }
        (OUTPUT_ROOT / "manifest_db_prepare.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return

    config = db_config()
    if not config:
        raise RuntimeError("DB_CONFIG is missing")

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
            ensure_table(cur)
            if PRODUCT_TYPE == "REF":
                ensure_ref_item_mst_table(cur)
    conn.close()

    manifest = {
        "run_type": "step13_db_prepare",
        "started_at": started_at,
        "finished_at": now(),
        "run_date": RUN_DATE,
        "product_type": PRODUCT_TYPE,
        "run_root": rel_path(RUN_ROOT),
        "output_root": rel_path(OUTPUT_ROOT),
        "schema": TARGET_SCHEMA,
        "table": TARGET_TABLE,
        "ref_item_mst_table": REF_ITEM_MST_TABLE if PRODUCT_TYPE == "REF" else None,
        "success": True,
        "skipped": False,
        "dry_run": False,
    }
    (OUTPUT_ROOT / "manifest_db_prepare.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
