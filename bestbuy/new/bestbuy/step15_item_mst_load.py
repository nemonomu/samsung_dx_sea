import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .step00_config import DEFAULT_BESTBUY_RUN_ROOT, bestbuy_category, db_config, rel_path


TARGET_SCHEMA = "public"
CATEGORY = bestbuy_category()
RUN_ROOT = Path(os.getenv("BESTBUY_RUN_ROOT", DEFAULT_BESTBUY_RUN_ROOT))
OUTPUT_ROOT = Path(os.getenv("BESTBUY_OUTPUT_ROOT", RUN_ROOT / "output"))
DETAIL_ROOT = Path(os.getenv("BESTBUY_DETAIL_RUN_ROOT", RUN_ROOT / "detail"))
FINAL_OUTPUT_CSV = Path(os.getenv("BESTBUY_FINAL_OUTPUT_CSV", OUTPUT_ROOT / "final_output.csv"))
FINAL_TARGETS_CSV = Path(os.getenv("BESTBUY_DETAIL_TARGET_CSV", OUTPUT_ROOT / "bestbuy_final_targets.csv"))
ITEM_MST_OUTPUT_CSV = Path(os.getenv("BESTBUY_ITEM_MST_OUTPUT_CSV", OUTPUT_ROOT / "item_mst.csv"))
MANIFEST_PATH = OUTPUT_ROOT / "item_mst_load_manifest.json"
DRY_RUN = os.getenv("BESTBUY_DB_LOAD_DRY_RUN", "0").lower() in {"1", "true", "yes", "y"}

ITEM_MST_TABLES = {
    "TV": os.getenv("BESTBUY_ITEM_MST_TABLE_TV", "tv_item_mst"),
    "HHP": os.getenv("BESTBUY_ITEM_MST_TABLE_HHP", "hhp_item_mst"),
}

COMMON_COLUMNS = [
    ("id", "serial PRIMARY KEY"),
    ("item", "text"),
    ("product_url", "text"),
    ("sku", "text"),
    ("account_name", "varchar(50)"),
    ("created_at", "timestamp"),
    ("updated_at", "timestamp"),
    ("is_product", "boolean"),
    ("is_checked", "boolean"),
]

CATEGORY_COLUMNS = {
    "TV": [
        ("screen_size", "text"),
        ("estimated_annual_electricity_use", "text"),
    ],
    "HHP": [
        ("hhp_carrier", "text"),
        ("hhp_color", "text"),
        ("hhp_storage", "text"),
    ],
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def quote_ident(value):
    return '"' + str(value).replace('"', '""') + '"'


def compact_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def read_csv(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def item_from_product_url(value):
    text = compact_text(value)
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except Exception:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if "product" in parts:
        index = parts.index("product")
        if len(parts) > index + 2:
            return parts[index + 2].strip()
    if "/sku/" in text:
        before_sku = text.split("/sku/", 1)[0].rstrip("/")
        item = before_sku.rsplit("/", 1)[-1].strip()
        return item if item and item.lower() not in {"product", "site"} else ""
    return ""


def sku_id_from_product_url(value):
    text = compact_text(value)
    if not text:
        return ""
    match = re.search(r"/sku/(\d+)", text)
    if match:
        return match.group(1)
    match = re.search(r"/(\d{6,})\.p(?:\?|$)", text)
    if match:
        return match.group(1)
    try:
        query = parse_qs(urlparse(text).query)
    except Exception:
        return ""
    values = query.get("skuId") or query.get("skuid")
    return str(values[0]).strip() if values else ""


def table_columns(category):
    return COMMON_COLUMNS[:-1] + CATEGORY_COLUMNS.get(category, []) + [COMMON_COLUMNS[-1]]


def output_fields(category):
    return [name for name, _ in table_columns(category)]


def target_sku_lookup(rows):
    by_item = {}
    by_url_item = {}
    for row in rows:
        sku = compact_text(row.get("sku_id") or sku_id_from_product_url(row.get("product_url")))
        if not sku:
            continue
        item = compact_text(row.get("item") or row.get("bsin") or item_from_product_url(row.get("product_url")))
        if item:
            by_item.setdefault(item, sku)
        url_item = item_from_product_url(row.get("product_url"))
        if url_item:
            by_url_item.setdefault(url_item, sku)
    return by_item, by_url_item


def detail_dirs_for_sku(sku):
    root = DETAIL_ROOT / "raw" / "detail_html"
    if not root.exists():
        return []
    return sorted(root.glob(f"*_{sku}_*"))


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def product_top_model_from_data(data, sku_id):
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            product = current.get("productBySkuId")
            if isinstance(product, dict) and compact_text(product.get("skuId")) == compact_text(sku_id):
                manufacturer = product.get("manufacturer") or {}
                model = compact_text(manufacturer.get("modelNumber") if isinstance(manufacturer, dict) else "")
                if model:
                    return model
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return ""


def model_from_detail_top(sku_id):
    if not sku_id:
        return ""
    for directory in detail_dirs_for_sku(sku_id):
        for suffix in ("json_response.json", "apollo.json"):
            path = directory / f"{sku_id}_{suffix}"
            data = load_json(path)
            model = product_top_model_from_data(data, sku_id) if data is not None else ""
            if model:
                return model
    return ""


def item_mst_rows(category, final_rows, target_rows=None, timestamp=None):
    timestamp = timestamp or datetime.now().isoformat(timespec="seconds")
    target_by_item, target_by_url_item = target_sku_lookup(target_rows or [])
    rows = []
    seen_items = set()
    for row in final_rows:
        item = compact_text(row.get("item") or item_from_product_url(row.get("product_url")))
        if not item or item in seen_items:
            continue
        seen_items.add(item)
        product_url = compact_text(row.get("product_url"))
        url_item = item_from_product_url(product_url)
        sku_id = (
            compact_text(row.get("sku_id"))
            or target_by_item.get(item, "")
            or target_by_url_item.get(url_item, "")
            or sku_id_from_product_url(product_url)
        )
        out = {
            "id": "",
            "item": item,
            "product_url": product_url,
            "sku": model_from_detail_top(sku_id),
            "account_name": "Bestbuy",
            "created_at": timestamp,
            "updated_at": "",
            "is_product": "true",
            "is_checked": "false",
        }
        if category == "TV":
            out.update(
                {
                    "screen_size": row.get("screen_size", ""),
                    "estimated_annual_electricity_use": row.get("estimated_annual_electricity_use", ""),
                }
            )
        elif category == "HHP":
            out.update(
                {
                    "hhp_carrier": row.get("hhp_carrier", ""),
                    "hhp_color": row.get("hhp_color", ""),
                    "hhp_storage": row.get("hhp_storage", ""),
                }
            )
        rows.append(out)
    return rows


def existing_column_names(cur, table_name):
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (TARGET_SCHEMA, table_name),
    )
    return {row[0] for row in cur.fetchall()}


def table_exists(cur, table_name):
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        )
        """,
        (TARGET_SCHEMA, table_name),
    )
    return bool(cur.fetchone()[0])


def create_table(cur, table_name, columns):
    column_sql = ",\n  ".join(f"{quote_ident(name)} {data_type}" for name, data_type in columns)
    cur.execute(f"CREATE TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} (\n  {column_sql}\n)")
    cur.execute(
        f"CREATE INDEX {quote_ident(f'idx_{table_name[:45]}_item')} "
        f"ON {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} USING btree (item)"
    )
    cur.execute(
        f"CREATE INDEX {quote_ident(f'idx_{table_name[:37]}_item_account')} "
        f"ON {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} USING btree (item, account_name)"
    )


def ensure_table(cur, table_name, columns):
    created = False
    altered = []
    if not table_exists(cur, table_name):
        create_table(cur, table_name, columns)
        created = True
        return {"created": created, "altered_columns": altered}
    existing = existing_column_names(cur, table_name)
    for name, data_type in columns:
        if name == "id" or name in existing:
            continue
        cur.execute(
            f"ALTER TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} "
            f"ADD COLUMN {quote_ident(name)} {data_type}"
        )
        altered.append(name)
    return {"created": created, "altered_columns": altered}


def normalize_db_value(value, data_type):
    if value in ("", None):
        return None
    lowered = str(data_type or "").lower()
    if "boolean" in lowered:
        return str(value).strip().lower() in {"1", "true", "yes", "y"}
    return value


def is_missing_master_value(value):
    return compact_text(value).lower() in {"", "no sku", "none", "null", "[null]"}


def value_for_insert(row, name, data_type):
    value = row.get(name)
    if name == "account_name" and is_missing_master_value(value):
        value = "Bestbuy"
    elif name == "sku" and is_missing_master_value(value):
        value = "no sku"
    return normalize_db_value(value, data_type)


def missing_only_updates(row, existing_values, columns):
    updates = []
    protected = {"id", "item", "account_name", "created_at", "updated_at", "is_product", "is_checked"}
    for name, data_type in columns:
        if name in protected:
            continue
        incoming = row.get(name)
        if is_missing_master_value(incoming):
            continue
        if name == "sku":
            if is_missing_master_value(existing_values.get(name)):
                updates.append((name, data_type, incoming))
            continue
        if is_missing_master_value(existing_values.get(name)):
            updates.append((name, data_type, incoming))
    return updates


def load_rows(cur, table_name, columns, rows, dry_run=False):
    if dry_run:
        return {"inserted": len(rows), "updated": 0, "skipped_existing": 0, "dry_run": True}

    data_columns = [(name, data_type) for name, data_type in columns if name != "id"]
    insert_columns = data_columns
    select_columns = [
        (name, data_type)
        for name, data_type in data_columns
        if name not in {"item", "account_name", "created_at", "updated_at", "is_product", "is_checked"}
    ]
    inserted = 0
    updated = 0
    skipped_existing = 0
    for row in rows:
        item = compact_text(row.get("item"))
        account_name = compact_text(row.get("account_name")) or "Bestbuy"
        if not item:
            continue
        select_sql = ", ".join(quote_ident(name) for name, _ in select_columns)
        cur.execute(
            f"SELECT {select_sql} FROM {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} "
            f"WHERE item = %s AND account_name = %s LIMIT 1",
            (item, account_name),
        )
        existing = cur.fetchone()
        if existing:
            existing_values = {name: existing[index] for index, (name, _) in enumerate(select_columns)}
            update_columns = missing_only_updates(row, existing_values, data_columns)
            if not update_columns:
                skipped_existing += 1
                continue
            if any(name == "updated_at" for name, _ in data_columns):
                update_columns.append(("updated_at", "timestamp", datetime.now().isoformat(timespec="seconds")))
            assignments = ", ".join(f"{quote_ident(name)} = %s" for name, _, _ in update_columns)
            sql = (
                f"UPDATE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} "
                f"SET {assignments} WHERE item = %s AND account_name = %s"
            )
            values = [normalize_db_value(value, data_type) for name, data_type, value in update_columns]
            cur.execute(sql, (*values, item, account_name))
            updated += max(cur.rowcount, 0)
            continue
        column_sql = ", ".join(quote_ident(name) for name, _ in insert_columns)
        placeholders = ", ".join(["%s"] * len(insert_columns))
        sql = (
            f"INSERT INTO {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} "
            f"({column_sql}) VALUES ({placeholders})"
        )
        values = [value_for_insert(row, name, data_type) for name, data_type in insert_columns]
        cur.execute(sql, values)
        inserted += 1
    return {"inserted": inserted, "updated": updated, "skipped_existing": skipped_existing, "dry_run": False}


def main():
    started_at = now()
    category = CATEGORY.strip().upper()
    if category not in ITEM_MST_TABLES:
        manifest = {
            "run_type": "step15_item_mst_load",
            "started_at": started_at,
            "finished_at": now(),
            "category": category,
            "skipped": True,
            "reason": "item_mst schema is not configured for this category",
        }
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return

    final_rows = read_csv(FINAL_OUTPUT_CSV)
    target_rows = read_csv(FINAL_TARGETS_CSV)
    rows = item_mst_rows(category, final_rows, target_rows)
    fields = output_fields(category)
    write_csv(ITEM_MST_OUTPUT_CSV, rows, fields)

    config = db_config()
    if not config and not DRY_RUN:
        raise RuntimeError("DB_CONFIG is missing")

    table_name = ITEM_MST_TABLES[category]
    columns = table_columns(category)
    table_result = {"created": False, "altered_columns": []}
    load_result = {"inserted": len(rows), "updated": 0, "dry_run": True}
    if config:
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
                table_result = ensure_table(cur, table_name, columns)
                load_result = load_rows(cur, table_name, columns, rows, DRY_RUN)
        conn.close()

    manifest = {
        "run_type": "step15_item_mst_load",
        "started_at": started_at,
        "finished_at": now(),
        "category": category,
        "dry_run": DRY_RUN,
        "table": f"{TARGET_SCHEMA}.{table_name}",
        "csv": rel_path(ITEM_MST_OUTPUT_CSV),
        "source_csv": rel_path(FINAL_OUTPUT_CSV),
        "rows": len(rows),
        "table_prepare": table_result,
        "load": load_result,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
