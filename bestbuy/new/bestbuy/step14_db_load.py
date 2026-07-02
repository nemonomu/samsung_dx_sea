import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .step00_availability_policy import active_availability_fields
from .step00_config import (
    DEFAULT_BESTBUY_RUN_ROOT,
    bestbuy_category,
    bestbuy_output_table,
    bestbuy_product_list_table,
    db_config,
    rel_path,
)


TARGET_SCHEMA = "public"
CATEGORY = bestbuy_category()
RUN_ROOT = Path(os.getenv("BESTBUY_RUN_ROOT", DEFAULT_BESTBUY_RUN_ROOT))
OUTPUT_ROOT = Path(os.getenv("BESTBUY_OUTPUT_ROOT", RUN_ROOT / "output"))
FINAL_OUTPUT_CSV = Path(os.getenv("BESTBUY_FINAL_OUTPUT_CSV", OUTPUT_ROOT / "final_output.csv"))
PRODUCT_LIST_CSV = Path(os.getenv("BESTBUY_PRODUCT_LIST_OUTPUT", OUTPUT_ROOT / "bestbuy_product_list.csv"))
MANIFEST_PATH = OUTPUT_ROOT / "db_load_manifest.json"
DRY_RUN = os.getenv("BESTBUY_DB_LOAD_DRY_RUN", "0").lower() in {"1", "true", "yes", "y"}
STRICT_COLUMNS = os.getenv("BESTBUY_DB_STRICT_COLUMNS", "1").lower() in {"1", "true", "yes", "y"}
UPDATE_SIMILAR_ONLY = os.getenv("BESTBUY_DB_UPDATE_SIMILAR_ONLY", "0").lower() in {"1", "true", "yes", "y"}
UPDATE_AVAILABILITY_ONLY = os.getenv("BESTBUY_DB_UPDATE_AVAILABILITY_ONLY", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
UPDATE_PROMOTION_ONLY = os.getenv("BESTBUY_DB_UPDATE_PROMOTION_ONLY", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
UPDATE_BATCH_ID = os.getenv("BESTBUY_DB_UPDATE_BATCH_ID", "").strip()
ROW_UPSERT_ONLY = os.getenv("BESTBUY_DB_ROW_UPSERT_ONLY", "0").lower() in {"1", "true", "yes", "y"}
ROW_UPSERT_NONBLANK_ONLY = os.getenv("BESTBUY_DB_ROW_UPSERT_NONBLANK_ONLY", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
ROW_UPSERT_ALLOW_ALL = os.getenv("BESTBUY_DB_ROW_UPSERT_ALLOW_ALL", "0").lower() in {"1", "true", "yes", "y"}
ROW_UPSERT_SKUS = {
    item.strip()
    for item in re.split(r"[\s,;]+", os.getenv("BESTBUY_DB_ROW_UPSERT_SKUS", ""))
    if item.strip()
}
ROW_UPSERT_ITEMS = {
    item.strip().lower()
    for item in re.split(r"[\s,;]+", os.getenv("BESTBUY_DB_ROW_UPSERT_ITEMS", ""))
    if item.strip()
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def quote_ident(value):
    return '"' + str(value).replace('"', '""') + '"'


def read_csv(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def compact_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def item_from_product_url(value):
    text = compact_text(value)
    if not text or "/sku/" not in text:
        return ""
    before_sku = text.split("/sku/", 1)[0].rstrip("/")
    item = before_sku.rsplit("/", 1)[-1].strip()
    return item if item and item.lower() not in {"product", "site"} else ""


def sku_id_from_product_url(value):
    text = compact_text(value)
    if not text:
        return ""
    match = re.search(r"(?:[?&]skuId=|/sku/|/site/-/)(\d+)", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def row_item(row):
    return compact_text(row.get("item") or item_from_product_url(row.get("product_url")))


def row_sku_id(row):
    return compact_text(row.get("sku_id") or sku_id_from_product_url(row.get("product_url")))


def table_columns(cur, table_name):
    cur.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (TARGET_SCHEMA, table_name),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def _format_star_rating(value):
    """0.0/1.0/2.0/3.0/4.0/5.0 -> '0'/'1'/.../'5'. 4.5 stays '4.5'."""
    if value in ("", None):
        return value
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return value
    return str(int(f)) if f.is_integer() else str(value).strip()


def normalize_value(value, data_type, column_name=""):
    if column_name == "star_rating":
        value = _format_star_rating(value)
    if value in ("", None):
        return None
    data_type = str(data_type or "").lower()
    if data_type in {"integer", "bigint", "smallint"}:
        try:
            return int(str(value).replace(",", "").strip())
        except ValueError:
            return None
    return value


def delete_existing_batch(cur, table_name, columns, rows):
    column_names = {name for name, _ in columns}
    if "batch_id" not in column_names:
        return 0
    batch_ids = sorted({str(row.get("batch_id") or "").strip() for row in rows if row.get("batch_id")})
    if not batch_ids:
        return 0
    cur.execute(
        f"DELETE FROM {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} WHERE batch_id = ANY(%s)",
        (batch_ids,),
    )
    return cur.rowcount


def insert_rows(cur, table_name, columns, rows):
    if not rows:
        return {"inserted": 0, "deleted_existing": 0, "columns": [], "missing_table_columns": []}
    missing = validate_insert_columns(table_name, columns, rows)
    insert_columns = [(name, data_type) for name, data_type in columns if name != "id"]
    csv_fields = set(rows[0].keys())
    insert_columns = [(name, data_type) for name, data_type in insert_columns if name in csv_fields]
    if not insert_columns:
        return {"inserted": 0, "deleted_existing": 0, "columns": [], "missing_table_columns": missing}

    deleted = delete_existing_batch(cur, table_name, columns, rows)
    column_sql = ", ".join(quote_ident(name) for name, _ in insert_columns)
    placeholders = ", ".join(["%s"] * len(insert_columns))
    sql = f"INSERT INTO {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} ({column_sql}) VALUES ({placeholders})"
    values = [
        tuple(normalize_value(row.get(name), data_type, name) for name, data_type in insert_columns)
        for row in rows
    ]
    cur.executemany(sql, values)
    return {
        "inserted": len(values),
        "deleted_existing": deleted,
        "columns": [name for name, _ in insert_columns],
        "missing_table_columns": missing,
    }


def plan_rows(columns, rows, table_name="<dry-run>"):
    if not rows:
        return {"inserted": 0, "deleted_existing": 0, "columns": [], "missing_table_columns": []}
    missing = validate_insert_columns(table_name, columns, rows)
    insert_columns = [(name, data_type) for name, data_type in columns if name != "id"]
    csv_fields = set(rows[0].keys())
    insert_columns = [(name, data_type) for name, data_type in insert_columns if name in csv_fields]
    return {
        "inserted": len(rows) if insert_columns else 0,
        "deleted_existing": 0,
        "columns": [name for name, _ in insert_columns],
        "missing_table_columns": missing,
    }


def fallback_csv_columns(rows):
    if not rows:
        return []
    return [(name, "text") for name in rows[0].keys()]


def csv_columns(rows):
    if not rows:
        return []
    return [name for name in rows[0].keys() if name != "id"]


def validate_insert_columns(table_name, columns, rows):
    missing = sorted(set(csv_columns(rows)) - {name for name, _ in columns})
    if missing and STRICT_COLUMNS:
        raise RuntimeError(
            f"DB table is missing columns required by CSV insert: {TARGET_SCHEMA}.{table_name} "
            f"missing={missing}"
        )
    return missing


def row_upsert_candidates(rows):
    if not ROW_UPSERT_SKUS and not ROW_UPSERT_ITEMS:
        return rows if ROW_UPSERT_ALLOW_ALL else []
    return [
        row
        for row in rows
        if row_sku_id(row) in ROW_UPSERT_SKUS or row_item(row).lower() in ROW_UPSERT_ITEMS
    ]


def row_match_clause(row, available_columns):
    batch_id = compact_text(row.get("batch_id"))
    if not batch_id or "batch_id" not in available_columns:
        return None
    item = row_item(row)
    if item and "item" in available_columns:
        return [("batch_id", batch_id), ("item", item)]
    sku_id = row_sku_id(row)
    if sku_id and "sku_id" in available_columns:
        return [("batch_id", batch_id), ("sku_id", sku_id)]
    product_url = compact_text(row.get("product_url"))
    if product_url and "product_url" in available_columns:
        return [("batch_id", batch_id), ("product_url", product_url)]
    return None


def row_upsert_rows(cur, table_name, columns, rows, dry_run=False):
    source_rows = read_csv(rows) if isinstance(rows, (str, Path)) else rows
    candidates = row_upsert_candidates(source_rows)
    if not candidates:
        return {
            "inserted": 0,
            "updated": 0,
            "candidate_rows": 0,
            "skipped_no_match_key": 0,
            "skipped_blank_update": 0,
            "columns": [],
            "missing_table_columns": [],
            "mode": "row_upsert_only",
            "row_upsert_sku_filter_count": len(ROW_UPSERT_SKUS),
            "row_upsert_item_filter_count": len(ROW_UPSERT_ITEMS),
            "row_upsert_allow_all": ROW_UPSERT_ALLOW_ALL,
        }
    missing = validate_insert_columns(table_name, columns, candidates)
    insert_columns = [(name, data_type) for name, data_type in columns if name != "id"]
    csv_fields = set(candidates[0].keys())
    insert_columns = [(name, data_type) for name, data_type in insert_columns if name in csv_fields]
    available = {name for name, _ in columns}
    inserted = 0
    updated = 0
    skipped_no_match = 0
    skipped_blank_update = 0
    touched_columns = set()

    for row in candidates:
        match = row_match_clause(row, available)
        if not match:
            skipped_no_match += 1
            continue
        match_columns = {name for name, _ in match}
        update_columns = []
        update_values = []
        for name, data_type in insert_columns:
            if name in match_columns:
                continue
            value = row.get(name)
            if ROW_UPSERT_NONBLANK_ONLY and value in ("", None):
                continue
            update_columns.append((name, data_type))
            update_values.append(normalize_value(value, data_type, name))
        if dry_run:
            if update_columns:
                updated += 1
                touched_columns.update(name for name, _ in update_columns)
            else:
                skipped_blank_update += 1
            continue

        where_sql = " AND ".join(f"{quote_ident(name)} = %s" for name, _ in match)
        where_values = [value for _, value in match]
        if update_columns:
            set_sql = ", ".join(f"{quote_ident(name)} = %s" for name, _ in update_columns)
            sql = (
                f"UPDATE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} "
                f"SET {set_sql} WHERE {where_sql}"
            )
            cur.execute(sql, (*update_values, *where_values))
            if cur.rowcount:
                updated += cur.rowcount
                touched_columns.update(name for name, _ in update_columns)
                continue
        else:
            cur.execute(
                f"SELECT 1 FROM {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} WHERE {where_sql} LIMIT 1",
                where_values,
            )
            if cur.fetchone():
                skipped_blank_update += 1
                continue
            skipped_blank_update += 1

        column_sql = ", ".join(quote_ident(name) for name, _ in insert_columns)
        placeholders = ", ".join(["%s"] * len(insert_columns))
        sql = f"INSERT INTO {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} ({column_sql}) VALUES ({placeholders})"
        values = tuple(normalize_value(row.get(name), data_type, name) for name, data_type in insert_columns)
        cur.execute(sql, values)
        inserted += 1
        touched_columns.update(name for name, _ in insert_columns)

    return {
        "inserted": inserted,
        "updated": updated,
        "candidate_rows": len(candidates),
        "skipped_no_match_key": skipped_no_match,
        "skipped_blank_update": skipped_blank_update,
        "columns": sorted(touched_columns),
        "missing_table_columns": missing,
        "mode": "row_upsert_only",
        "match_keys": ["batch_id", "item|sku_id|product_url"],
        "row_upsert_nonblank_only": ROW_UPSERT_NONBLANK_ONLY,
        "row_upsert_sku_filter_count": len(ROW_UPSERT_SKUS),
        "row_upsert_item_filter_count": len(ROW_UPSERT_ITEMS),
        "row_upsert_allow_all": ROW_UPSERT_ALLOW_ALL,
    }


def load_one(cur, csv_path, table_name, dry_run=False):
    rows = read_csv(csv_path)
    columns = table_columns(cur, table_name) if cur else fallback_csv_columns(rows)
    if not columns:
        raise RuntimeError(f"DB table not found or has no columns: {TARGET_SCHEMA}.{table_name}")
    if ROW_UPSERT_ONLY:
        result = row_upsert_rows(cur, table_name, columns, rows, dry_run)
    else:
        result = plan_rows(columns, rows, table_name) if dry_run else insert_rows(cur, table_name, columns, rows)
    result.update(
        {
            "csv": rel_path(csv_path),
            "table": f"{TARGET_SCHEMA}.{table_name}",
            "csv_rows": len(rows),
            "dry_run": dry_run,
        }
    )
    return result


def update_similar_only(cur, csv_path, table_name, dry_run=False):
    rows = [
        row
        for row in read_csv(csv_path)
        if str(row.get("batch_id") or "").strip()
        and str(row.get("item") or "").strip()
        and str(row.get("retailer_sku_name_similar") or "").strip()
    ]
    if not rows:
        return {
            "csv": rel_path(csv_path),
            "table": f"{TARGET_SCHEMA}.{table_name}",
            "csv_rows": 0,
            "candidate_rows": 0,
            "updated": 0,
            "dry_run": dry_run,
            "mode": "update_similar_only",
        }
    columns = table_columns(cur, table_name) if cur else fallback_csv_columns(rows)
    required = {"batch_id", "item", "retailer_sku_name_similar"}
    available = {name for name, _ in columns}
    missing = sorted(required - available)
    if missing:
        raise RuntimeError(f"DB table is missing columns for similar update: {missing}")
    if dry_run:
        updated = 0
    else:
        sql = (
            f"UPDATE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} "
            f"SET retailer_sku_name_similar = %s "
            f"WHERE batch_id = %s AND item = %s"
        )
        updated = 0
        for row in rows:
            cur.execute(
                sql,
                (
                    row.get("retailer_sku_name_similar"),
                    str(row.get("batch_id") or "").strip(),
                    str(row.get("item") or "").strip(),
                ),
            )
            updated += max(cur.rowcount, 0)
    return {
        "csv": rel_path(csv_path),
        "table": f"{TARGET_SCHEMA}.{table_name}",
        "csv_rows": len(read_csv(csv_path)),
        "candidate_rows": len(rows),
        "updated": updated,
        "dry_run": dry_run,
        "mode": "update_similar_only",
        "match_keys": ["batch_id", "item"],
        "updated_column": "retailer_sku_name_similar",
    }


def promotion_update_candidates(rows, batch_id=""):
    candidates = []
    for row in rows:
        item = row_item(row)
        row_batch_id = str(row.get("batch_id") or "").strip()
        if not row_batch_id or not item:
            continue
        if batch_id and row_batch_id != batch_id:
            continue
        if str(row.get("target_source") or "").strip() == "promotion_backfill":
            continue
        promotion_type = str(row.get("promotion_type") or "").strip()
        promotion_position = str(row.get("promotion_position") or "").strip()
        if not promotion_type and not promotion_position:
            continue
        normalized = dict(row)
        normalized["item"] = item
        candidates.append(normalized)
    return candidates


def update_promotion_only(cur, csv_path, table_name, dry_run=False, batch_id=UPDATE_BATCH_ID):
    source_rows = read_csv(csv_path)
    rows = promotion_update_candidates(source_rows, batch_id)
    if not rows:
        return {
            "csv": rel_path(csv_path),
            "table": f"{TARGET_SCHEMA}.{table_name}",
            "csv_rows": len(source_rows),
            "candidate_rows": 0,
            "updated": 0,
            "dry_run": dry_run,
            "mode": "update_promotion_only",
            "batch_id_filter": batch_id,
        }
    columns = table_columns(cur, table_name) if cur else fallback_csv_columns(rows)
    required = {"batch_id", "item", "promotion_type", "promotion_position"}
    available = {name for name, _ in columns}
    missing = sorted(required - available)
    if missing:
        raise RuntimeError(f"DB table is missing columns for promotion update: {missing}")
    if dry_run:
        updated = 0
    else:
        sql = (
            f"UPDATE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} "
            f"SET promotion_type = %s, promotion_position = %s "
            f"WHERE batch_id = %s AND item = %s"
        )
        updated = 0
        for row in rows:
            cur.execute(
                sql,
                (
                    row.get("promotion_type") or None,
                    normalize_value(row.get("promotion_position"), "integer", "promotion_position"),
                    str(row.get("batch_id") or "").strip(),
                    str(row.get("item") or "").strip(),
                ),
            )
            updated += max(cur.rowcount, 0)
    return {
        "csv": rel_path(csv_path),
        "table": f"{TARGET_SCHEMA}.{table_name}",
        "csv_rows": len(source_rows),
        "candidate_rows": len(rows),
        "updated": updated,
        "dry_run": dry_run,
        "mode": "update_promotion_only",
        "batch_id_filter": batch_id,
        "match_keys": ["batch_id", "item"],
        "updated_columns": ["promotion_type", "promotion_position"],
    }


def availability_update_candidates(rows, batch_id=""):
    candidates = []
    for row in rows:
        item = row_item(row)
        if not str(row.get("batch_id") or "").strip() or not item:
            continue
        if batch_id and str(row.get("batch_id") or "").strip() != batch_id:
            continue
        normalized = dict(row)
        normalized["item"] = item
        candidates.append(normalized)
    return candidates


def update_availability_only(cur, csv_path, table_name, dry_run=False, batch_id=UPDATE_BATCH_ID):
    fields = active_availability_fields(CATEGORY)
    source_rows = read_csv(csv_path)
    rows = availability_update_candidates(source_rows, batch_id)
    if not rows:
        return {
            "csv": rel_path(csv_path),
            "table": f"{TARGET_SCHEMA}.{table_name}",
            "csv_rows": len(source_rows),
            "candidate_rows": 0,
            "updated": 0,
            "dry_run": dry_run,
            "mode": "update_availability_only",
            "batch_id_filter": batch_id,
        }
    columns = table_columns(cur, table_name) if cur else fallback_csv_columns(rows)
    required = {"batch_id", "item", *fields}
    available = {name for name, _ in columns}
    missing = sorted(required - available)
    if missing:
        raise RuntimeError(f"DB table is missing columns for availability update: {missing}")
    if dry_run:
        updated = 0
    else:
        assignments = ", ".join(f"{quote_ident(field)} = %s" for field in fields)
        sql = (
            f"UPDATE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} "
            f"SET {assignments} "
            f"WHERE batch_id = %s AND item = %s"
        )
        updated = 0
        for row in rows:
            cur.execute(
                sql,
                (
                    *(row.get(field) for field in fields),
                    str(row.get("batch_id") or "").strip(),
                    str(row.get("item") or "").strip(),
                ),
            )
            updated += max(cur.rowcount, 0)
    return {
        "csv": rel_path(csv_path),
        "table": f"{TARGET_SCHEMA}.{table_name}",
        "csv_rows": len(source_rows),
        "candidate_rows": len(rows),
        "updated": updated,
        "dry_run": dry_run,
        "mode": "update_availability_only",
        "batch_id_filter": batch_id,
        "match_keys": ["batch_id", "item"],
        "updated_columns": fields,
    }


def main():
    started_at = now()
    config = db_config()
    if not config and not DRY_RUN:
        raise RuntimeError("DB_CONFIG is missing")

    final_table = bestbuy_output_table(CATEGORY)
    product_list_table = bestbuy_product_list_table(CATEGORY)
    conn = None
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
    if conn:
        with conn:
            with conn.cursor() as cur:
                if UPDATE_AVAILABILITY_ONLY:
                    final_result = update_availability_only(cur, FINAL_OUTPUT_CSV, final_table, DRY_RUN)
                    product_list_result = {"skipped": True, "reason": "update_availability_only"}
                elif UPDATE_PROMOTION_ONLY:
                    final_result = update_promotion_only(cur, FINAL_OUTPUT_CSV, final_table, DRY_RUN)
                    product_list_result = {"skipped": True, "reason": "update_promotion_only"}
                elif UPDATE_SIMILAR_ONLY:
                    final_result = update_similar_only(cur, FINAL_OUTPUT_CSV, final_table, DRY_RUN)
                    product_list_result = {"skipped": True, "reason": "update_similar_only"}
                else:
                    final_result = load_one(cur, FINAL_OUTPUT_CSV, final_table, DRY_RUN)
                    product_list_result = load_one(cur, PRODUCT_LIST_CSV, product_list_table, DRY_RUN)
        conn.close()
    else:
        if UPDATE_AVAILABILITY_ONLY:
            final_result = update_availability_only(None, FINAL_OUTPUT_CSV, final_table, DRY_RUN)
            product_list_result = {"skipped": True, "reason": "update_availability_only"}
        elif UPDATE_PROMOTION_ONLY:
            final_result = update_promotion_only(None, FINAL_OUTPUT_CSV, final_table, DRY_RUN)
            product_list_result = {"skipped": True, "reason": "update_promotion_only"}
        elif UPDATE_SIMILAR_ONLY:
            final_result = update_similar_only(None, FINAL_OUTPUT_CSV, final_table, DRY_RUN)
            product_list_result = {"skipped": True, "reason": "update_similar_only"}
        else:
            final_result = load_one(None, FINAL_OUTPUT_CSV, final_table, DRY_RUN)
            product_list_result = load_one(None, PRODUCT_LIST_CSV, product_list_table, DRY_RUN)

    manifest = {
        "run_type": "step14_db_load",
        "started_at": started_at,
        "finished_at": now(),
        "category": CATEGORY,
        "dry_run": DRY_RUN,
        "update_similar_only": UPDATE_SIMILAR_ONLY,
        "update_availability_only": UPDATE_AVAILABILITY_ONLY,
        "update_promotion_only": UPDATE_PROMOTION_ONLY,
        "row_upsert_only": ROW_UPSERT_ONLY,
        "row_upsert_nonblank_only": ROW_UPSERT_NONBLANK_ONLY,
        "row_upsert_sku_filter_count": len(ROW_UPSERT_SKUS),
        "row_upsert_item_filter_count": len(ROW_UPSERT_ITEMS),
        "update_batch_id": UPDATE_BATCH_ID,
        "run_root": rel_path(RUN_ROOT),
        "final_output": final_result,
        "product_list": product_list_result,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
