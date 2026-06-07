from __future__ import annotations

import argparse
import csv
import sys
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

INTEGER_COLUMNS = {
    "count_of_reviews",
    "count_of_star_ratings",
    "number_of_ppl_purchased_yesterday",
    "number_of_ppl_added_to_carts",
    "available_quantity_for_purchase",
    "main_rank",
    "bsr_rank",
}


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def normalize_value(column: str, value: Any) -> Any:
    text = "" if value is None else str(value).strip()
    if text == "":
        return None
    if column in INTEGER_COLUMNS:
        return int(text)
    return text


def validate_rows(rows: List[Dict[str, str]]) -> None:
    if not rows:
        raise ValueError("CSV has no rows")
    headers = list(rows[0].keys())
    if headers != EXPECTED_DB_COLUMNS:
        raise ValueError(f"CSV columns do not match expected DB columns: {headers}")
    seen: set[str] = set()
    for idx, row in enumerate(rows, start=2):
        item = (row.get("item") or "").strip()
        if not item:
            raise ValueError(f"row {idx}: blank item")
        if item in seen:
            raise ValueError(f"row {idx}: duplicate item {item}")
        seen.add(item)
        if blank(row.get("final_sku_price")):
            raise ValueError(f"row {idx} item {item}: blank final_sku_price")
        for column in INTEGER_COLUMNS:
            value = (row.get(column) or "").strip()
            if value and not value.isdigit():
                raise ValueError(f"row {idx} item {item}: invalid integer {column}={value}")


def connect(project_root: Path):
    sys.path.insert(0, str(project_root))
    from config import DB_CONFIG  # type: ignore
    import psycopg2  # type: ignore

    return psycopg2.connect(**DB_CONFIG)


def insert_retail_rows(conn, rows: List[Dict[str, str]], table: str) -> int:
    columns = EXPECTED_DB_COLUMNS
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    values = [
        tuple(normalize_value(column, row.get(column)) for column in columns)
        for row in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, values)
    return len(values)


def upsert_item_mst(conn, rows: List[Dict[str, str]]) -> Dict[str, int]:
    inserted = 0
    updated = 0
    skipped = 0
    with conn.cursor() as cur:
        for row in rows:
            item = (row.get("item") or "").strip()
            account_name = (row.get("account_name") or "").strip()
            product_url = (row.get("product_url") or "").strip() or None
            screen_size = (row.get("screen_size") or "").strip() or None
            sku = (row.get("sku") or "").strip() or "no sku"
            if not item or not account_name:
                skipped += 1
                continue
            cur.execute(
                """
                SELECT sku, screen_size
                FROM tv_item_mst
                WHERE item = %s AND account_name = %s
                """,
                (item, account_name),
            )
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
                    """
                    INSERT INTO tv_item_mst (item, account_name, sku, product_url, screen_size)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (item, account_name, sku, product_url, screen_size),
                )
                inserted += 1
                continue
            existing_sku, existing_screen_size = existing
            updates: List[str] = []
            params: List[Any] = []
            if not (existing_sku or "") and sku:
                updates.append("sku = %s")
                params.append(sku)
            if not existing_screen_size and screen_size:
                updates.append("screen_size = %s")
                params.append(screen_size)
            if updates:
                updates.append("product_url = %s")
                params.append(product_url)
                updates.append("updated_at = %s")
                params.append(datetime.now())
                params.extend([item, account_name])
                cur.execute(
                    f"UPDATE tv_item_mst SET {', '.join(updates)} WHERE item = %s AND account_name = %s",
                    params,
                )
                updated += 1
            else:
                skipped += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--table", default="tv_retail_com")
    parser.add_argument("--skip-item-mst", action="store_true")
    parser.add_argument("--commit", action="store_true", help="Actually commit DB changes. Without this, rollback after validation.")
    args = parser.parse_args()

    rows = read_csv(args.csv.resolve())
    validate_rows(rows)
    conn = connect(args.project_root.resolve())
    try:
        inserted = insert_retail_rows(conn, rows, args.table)
        mst_result = {"inserted": 0, "updated": 0, "skipped": 0}
        if not args.skip_item_mst:
            mst_result = upsert_item_mst(conn, rows)
        if args.commit:
            conn.commit()
            mode = "committed"
        else:
            conn.rollback()
            mode = "rolled_back_dry_run"
        print({
            "mode": mode,
            "table": args.table,
            "retail_rows": inserted,
            "item_mst": mst_result,
            "csv": str(args.csv.resolve()),
        })
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
