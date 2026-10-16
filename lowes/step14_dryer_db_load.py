"""Dryer-only PostgreSQL loader. No environment/DB access occurs on import."""
import argparse
import csv
from datetime import datetime
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re

from .step00_dryer import (
    COLUMNS, DEFAULT_TABLE, TEST_TABLE, PRODUCTION_TABLE, coerce_db_value, table_parts, validate_rows,
)
from .step00_dryer_audit import difference


class DryerDBError(RuntimeError):
    def __init__(self, public_reason, differences=None):
        self.public_reason = public_reason
        self.differences = differences or []
        super().__init__(public_reason)


def connect_db():
    from .step00_config import db_config
    import psycopg2
    settings = db_config()
    if not settings or not all(settings.get(k) for k in ("host", "user")):
        raise DryerDBError("DB settings not configured")
    # Existing REF/LDY loading uses postgres when the database name is omitted.
    database = settings.get("database") or "postgres"
    # The loader passes secrets internally; no DSN/settings/exception text is printed.
    return psycopg2.connect(host=settings.get("host"), port=int(settings.get("port") or 5432),
                            user=settings.get("user"), password=settings.get("password"),
                            dbname=database, connect_timeout=10,
                            application_name="lowes_dryer")


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def inspect_columns(cur, schema, table):
    cur.execute(
        "SELECT column_name, data_type, is_identity, column_default, is_nullable "
        "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    result = {r[0]: {"type": r[1], "identity": r[2], "default": r[3], "nullable": r[4]}
              for r in cur.fetchall()}
    missing = set(COLUMNS) - result.keys()
    if missing:
        raise DryerDBError("missing dryer columns: " + ",".join(sorted(missing)))
    required_extra = [column for column, meta in result.items() if column not in COLUMNS
                      and meta["nullable"] == "NO" and meta["default"] is None and meta["identity"] != "YES"]
    if required_extra:
        raise DryerDBError("required table columns outside dryer contract: " + ",".join(sorted(required_extra)))
    return result


def prepare_insert(rows, metadata):
    generated_id = metadata["id"]["identity"] == "YES" or metadata["id"]["default"] is not None
    columns = [c for c in COLUMNS if c != "id" or not generated_id]
    values = []
    for row in rows:
        converted = []
        for column in columns:
            meta = metadata[column]
            try:
                value = coerce_db_value(column, row.get(column), meta["type"])
            except ValueError as exc:
                raise DryerDBError(str(exc)) from None
            if value is None and meta["nullable"] == "NO":
                raise DryerDBError("NULL violates table rule: " + column)
            converted.append(value)
        values.append(tuple(converted))
    return columns, values


def load_batch(rows, logger=None, table=DEFAULT_TABLE, allow_production=True, connection=None):
    counts = validate_rows(rows)
    schema, name = table_parts(table, allow_production)
    qualified = identifier(schema) + "." + identifier(name)
    batch_id = rows[0]["batch_id"]
    if logger:
        logger.info("[적재 시작] table=%s batch=%s products=%s", table, batch_id, len(rows))
    owned = connection is None
    conn = connection if connection is not None else connect_db()
    try:
        with conn:
            with conn.cursor() as cur:
                metadata = inspect_columns(cur, schema, name)
                columns, values = prepare_insert(rows, metadata)
                # Validate ALL values before the first mutation; same-batch reload is atomic.
                cur.execute("SET LOCAL lock_timeout = '30s'")
                cur.execute("SET LOCAL statement_timeout = '120s'")
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))", (table, batch_id))
                cur.execute(f"DELETE FROM {qualified} WHERE batch_id = %s AND account_name = %s",
                            (batch_id, "Lowes"))
                deleted = cur.rowcount
                sql = (f"INSERT INTO {qualified} ({', '.join(identifier(c) for c in columns)}) "
                       f"VALUES ({', '.join(['%s'] * len(columns))})")
                cur.executemany(sql, values)
                cur.execute(f"SELECT count(*) FROM {qualified} WHERE batch_id=%s AND account_name=%s",
                            (batch_id, "Lowes"))
                actual = cur.fetchone()[0]
                if actual != len(rows):
                    raise DryerDBError("DB batch count mismatch; transaction rolled back")
        result = {"table": table, "batch_id": batch_id, "deleted_same_batch": deleted,
                  "inserted": len(rows), "verified_count": actual, "counts": counts,
                  "id_generated": "id" not in columns,
                  "column_types": {c: metadata[c]["type"] for c in COLUMNS}}
        if logger:
            logger.info("[적재] table=%s batch=%s replaced=%s inserted=%s verified=%s",
                        table, batch_id, deleted, len(rows), actual)
        return result
    except Exception as exc:
        if logger:
            code = str(getattr(exc, "pgcode", "") or "")
            logger.error("[적재 실패] type=%s sqlstate=%s reason=%s",
                         type(exc).__name__, code if re.fullmatch(r"[A-Z0-9]{5}", code) else "-",
                         exc.public_reason if isinstance(exc, DryerDBError) else "transaction_failed")
        raise
    finally:
        if owned:
            conn.close()


def read_rows(path, validate=True):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise DryerDBError("CSV header must contain exactly the 20 dryer columns in order")
        rows = [{k: None if v == "" else v for k, v in row.items()} for row in reader]
    if validate:
        validate_rows(rows)
    return rows


def db_value_matches(column, actual, expected, data_type):
    if actual is None or expected is None:
        return actual is None and expected is None
    if data_type in {"real", "double precision"} and column in {
        "final_sku_price", "original_sku_price", "savings",
    }:
        # Compare cents; float storage noise may not hide a changed cent.
        if not math.isfinite(float(actual)):
            return False
        return Decimal(str(actual)).quantize(Decimal("0.01")) == Decimal(str(expected)).quantize(Decimal("0.01"))
    if column in {"capacity", "star_rating"} and data_type in {"real", "double precision"}:
        tolerance = 1e-6 if data_type == "real" else 1e-12
        return math.isclose(float(actual), float(expected), rel_tol=tolerance, abs_tol=tolerance)
    if data_type in {"real", "double precision"}:
        # IDs, review counts, ranks and weeks must never use approximate comparison.
        return Decimal(str(actual)) == Decimal(str(expected))
    if isinstance(actual, datetime) and isinstance(expected, datetime):
        if expected.tzinfo is None and actual.tzinfo is not None:
            # A naive timestamp was inserted in the DB session timezone; psycopg
            # returns that same session's local clock with an offset attached.
            return actual.replace(tzinfo=None) == expected
    if data_type == "character" and isinstance(actual, str) and isinstance(expected, str):
        return actual.rstrip(" ") == expected.rstrip(" ")
    return actual == expected


def public_db_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "non_finite_number"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return "unsupported_db_value_type"


def verify_batch(rows, table=DEFAULT_TABLE, allow_production=True, connection=None):
    counts = validate_rows(rows)
    schema, name = table_parts(table, allow_production)
    qualified = identifier(schema) + "." + identifier(name)
    owned = connection is None
    conn = connection if connection is not None else connect_db()
    compare_columns = COLUMNS
    try:
        with conn:
            with conn.cursor() as cur:
                metadata = inspect_columns(cur, schema, name)
                generated_id = metadata["id"]["identity"] == "YES" or metadata["id"]["default"] is not None
                expected = {str(row["item"]): {column: coerce_db_value(column, row[column], metadata[column]["type"])
                            for column in compare_columns if column != "id" or not generated_id} for row in rows}
                cur.execute(f"SELECT {', '.join(identifier(c) for c in compare_columns)} FROM {qualified} "
                            "WHERE batch_id=%s AND account_name=%s", (rows[0]["batch_id"], "Lowes"))
                actual = cur.fetchall()
        differences, seen_items, seen_ids = [], set(), set()
        for values in actual:
            if len(values) != len(compare_columns):
                raise DryerDBError("unexpected DB row shape")
            saved = dict(zip(compare_columns, values))
            item = str(saved["item"])
            if not re.fullmatch(r"[1-9]\d*", item):
                differences.append(difference("-", "item", "numeric_product_id", "invalid_identifier", "invalid_db_item"))
                continue
            if item in seen_items:
                differences.append(difference(item, "item", "one_row", "duplicate_row", "duplicate_db_item"))
                continue
            seen_items.add(item)
            if item not in expected:
                differences.append(difference(item, "item", "absent", "present", "extra_db_product"))
                continue
            if generated_id:
                generated = saved["id"]
                if generated in (None, "") or generated in seen_ids:
                    differences.append(difference(item, "id", "unique_generated_id", "missing_or_duplicate", "generated_id_invalid"))
                seen_ids.add(generated)
            for column, value in expected[item].items():
                if not db_value_matches(column, saved[column], value, metadata[column]["type"]):
                    differences.append(difference(item, column, public_db_value(value), public_db_value(saved[column]), "csv_vs_db"))
        for item in sorted(set(expected) - seen_items):
            differences.append(difference(item, "item", "present", "absent", "missing_db_product"))
        if differences:
            raise DryerDBError("CSV/DB dryer value mismatch", differences)
        return {"verified": True, "table": table, "batch_id": rows[0]["batch_id"],
                "db_rows": len(actual), "counts": counts,
                "compared_columns": [column for column in COLUMNS if column != "id" or not generated_id],
                "id_generated": generated_id, "id_generation_verified": generated_id,
                "id_values_checked": len(seen_ids) if generated_id else 0}
    finally:
        if owned:
            conn.close()


def table_for_csv(path, batch_id, explicit=None, env=None):
    """Keep historical test CSVs on their recorded table unless overridden."""
    env = env if env is not None else {}
    chosen = explicit or env.get("LOWES_DRYER_TABLE")
    if not chosen:
        path = Path(path)
        recorded = set()
        legacy_settings = False
        if path.parent.name == "output":
            root = path.parent.parent
            for filename in ("manifest.json", "db_load_summary.json", "collection_summary.json"):
                metadata_path = root / filename
                if not metadata_path.exists():
                    continue
                try:
                    meta = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
                except (ValueError, OSError):
                    raise DryerDBError("dryer table metadata unreadable") from None
                if meta.get("batch_id") != batch_id:
                    raise DryerDBError("CSV/table metadata batch mismatch")
                candidates = (meta.get("table"), (meta.get("db") or {}).get("table"),
                              (meta.get("settings") or {}).get("target_table"))
                recorded.update(value for value in candidates if value)
                if filename == "collection_summary.json" and "target_table" not in (meta.get("settings") or {}):
                    legacy_settings = True
        if not recorded and legacy_settings:
            recorded.add(TEST_TABLE)
        if len(recorded) > 1:
            raise DryerDBError("conflicting dryer table metadata; select --table explicitly")
        chosen = next(iter(recorded), DEFAULT_TABLE)
    table_parts(chosen, allow_production=True)
    return chosen


def runtime_table(path, batch_id, explicit=None):
    from .step00_config import load_env
    load_env(Path(__file__).resolve().parent.parent / ".env")
    return table_for_csv(path, batch_id, explicit, os.environ)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Load or verify a dryer CSV batch")
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true", help="read-only DB comparison")
    parser.add_argument("--table", choices=[TEST_TABLE, PRODUCTION_TABLE],
                        help="override; otherwise LOWES_DRYER_TABLE, saved run table, production default")
    parser.add_argument("--allow-production", action="store_true",
                        help="legacy compatibility; production is now the default")
    args = parser.parse_args(argv)
    try:
        rows = read_rows(args.csv)
        table = runtime_table(args.csv, rows[0]["batch_id"], args.table)
        fn = verify_batch if args.verify_only else load_batch
        result = fn(rows, table=table, allow_production=table == PRODUCTION_TABLE)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__,
                          "reason": exc.public_reason if isinstance(exc, DryerDBError) else "validation_or_db_failed",
                          "differences": exc.differences if isinstance(exc, DryerDBError) else []},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
