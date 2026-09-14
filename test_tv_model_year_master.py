"""Offline model-year regression tests; no config, browser or DB is loaded.

Run from the SEA root: python -B -m unittest test_tv_model_year_master -v
Load production function/class ASTs so tests can exercise real save paths
without importing the crawlers' environment/credential initialization.
"""
import ast
import contextlib
import copy
import csv
import io
import os
import re
import sqlite3
import traceback
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent


def source_namespace(relative, extra=None, class_only=False):
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)] if class_only else [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.Assign, ast.AnnAssign))
    ]
    namespace = {"re": re, "datetime": datetime, "traceback": traceback,
                 "Path": Path, "os": os, "csv": csv,
                 "WalmartBaseCrawler": object, "__file__": str(path)}
    namespace.update(extra or {})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


WALMART = source_namespace("walmart/tv/wmart_tv_dt.py", class_only=True)["WalmartTVDetailCrawler"]
WALMART_UPDATE = source_namespace(
    "walmart/tv/wmart_tv_dt_update.py", {"WalmartTVDetailCrawler": WALMART}, class_only=True
)["WalmartTVDetailUpdateCrawler"]
CONFIG = {"DEFAULT_BESTBUY_RUN_ROOT": ROOT / "unused-test-output",
          "bestbuy_category": lambda: "TV", "rel_path": str,
          "bestbuy_output_table": lambda category=None: "tv_retail_com",
          "bestbuy_product_list_table": lambda category=None: "bby_tv_product_list"}
MASTER = source_namespace("bestbuy/new/bestbuy/step15_item_mst_load.py", CONFIG)
LOAD = source_namespace("bestbuy/new/bestbuy/step14_db_load.py", {
    **CONFIG, "hydrate_tv_model_years": MASTER["hydrate_tv_model_years"]})
# These tests always use deterministic offline options, independent of shell flags.
LOAD.update(ROW_UPSERT_ONLY=False, STRICT_COLUMNS=True, ROW_UPSERT_ALLOW_ALL=True,
            ROW_UPSERT_SKUS=set(), ROW_UPSERT_ITEMS=set())


class Cursor:
    def __init__(self, master_years=(), existing=None, columns=None, master_rows=(), master_records=()):
        self.calls = []
        self.result = []
        self.master_years = list(master_years)
        self.existing = existing
        self.master_rows = list(master_rows)
        self.master_records = list(master_records)
        self.columns = columns or ["id", "item", "account_name", "is_product", "model_year"]
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "information_schema.columns" in sql:
            self.result = [(name,) for name in self.columns]
        elif "SELECT model_year FROM tv_item_mst" in sql:
            self.result = [(year,) for year in self.master_years]
        elif "SELECT item, model_year" in sql:
            self.result = list(self.master_rows)
        elif "SELECT id, model_year" in sql:
            self.result = list(self.master_records)
        elif "SELECT sku, screen_size" in sql:
            self.result = [] if self.existing is None else [self.existing]
        elif sql.lstrip().startswith("SELECT"):
            self.result = [] if self.existing is None else [self.existing]
        else:
            self.result = []

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0] if self.result else None

    def close(self):
        pass


def crawler(cursor, update=False):
    cls = WALMART_UPDATE if update else WALMART
    obj = cls.__new__(cls)
    obj.account_name = "Walmart"
    obj.test_mode = True
    obj.batch_id = "test-model-year"
    obj.ensure_db_connection = lambda: True
    obj.db_conn = Mock()
    obj.db_conn.cursor.return_value = cursor
    obj.db_conn.closed = False
    return obj


class WalmartModelYearTests(unittest.TestCase):
    def test_collection_wins_and_master_only_fills_missing_year(self):
        for incoming in (None, "", "null", "  ", 2024):
            with self.subTest(incoming=incoming), contextlib.redirect_stdout(io.StringIO()):
                cur = Cursor(master_years=[2025])
                row = {"item": "test-item", "model_year": incoming, "final_sku_price": "$123"}
                crawler(cur).apply_master_model_year(cur, row)
                self.assertEqual(row["model_year"], "2024" if incoming == 2024 else "2025")
                self.assertEqual(row["final_sku_price"], "$123")
                if incoming == 2024:
                    self.assertEqual(cur.calls, [])
                else:
                    self.assertEqual(cur.calls[0][1], ("test-item", "Walmart"))
                    self.assertIn("is_product = TRUE", cur.calls[0][0])

    def test_no_master_keeps_collected_year_or_null(self):
        for incoming, expected in [(2026, "2026"), (" 2025 ", "2025"), (None, None), ("unknown", None)]:
            cur = Cursor()
            row = {"item": "test-item", "model_year": incoming}
            crawler(cur).apply_master_model_year(cur, row)
            self.assertEqual(row["model_year"], expected)

    def test_missing_item_never_looks_up_another_product(self):
        cur = Cursor(master_years=[2025])
        row = {"model_year": None}
        crawler(cur).apply_master_model_year(cur, row)
        self.assertEqual(cur.calls, [])
        self.assertIsNone(row["model_year"])

    def test_conflicting_duplicate_masters_use_first_valid_in_id_order(self):
        cur = Cursor(master_years=[2024, 2025])
        row = {"item": "test-item"}
        crawler(cur).apply_master_model_year(cur, row)
        self.assertEqual(row["model_year"], "2024")
        self.assertIn("ORDER BY id DESC", cur.calls[0][0])

    def test_equivalent_duplicate_and_blank_master_rows_are_safe(self):
        cur = Cursor(master_years=[None, "2025", 2025])
        row = {"item": "test-item"}
        crawler(cur).apply_master_model_year(cur, row)
        self.assertEqual(row["model_year"], "2025")

    def test_new_master_insert_excludes_collected_year(self):
        cur = Cursor()
        obj = crawler(cur)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(obj.upsert_item_mst({"item": "test-item", "sku": "MODEL", "model_year": 2025}))
        sql, values = next(call for call in cur.calls if "INSERT INTO tv_item_mst" in call[0])
        self.assertNotIn("model_year", sql)
        self.assertIn("is_product", sql)
        self.assertIn("TRUE)", sql)
        self.assertNotIn("2025", values)

    def test_later_manual_edit_is_read_again_without_stale_worker_cache(self):
        cur = Cursor(master_years=["2025"])
        obj = crawler(cur)
        first = {"item": "test-item", "model_year": None}
        self.assertTrue(obj.save_to_retail_com(first))
        self.assertEqual(first["model_year"], "2025")
        cur.master_years = ["2026"]
        second = {"item": "test-item", "model_year": None}
        self.assertTrue(obj.save_to_retail_com(second))
        self.assertEqual(second["model_year"], "2026")

    def test_update_without_any_year_preserves_existing_retail_year(self):
        cur = Cursor()
        obj = crawler(cur, update=True)
        self.assertTrue(obj.save_to_retail_com({"id": 99, "item": "test-item", "model_year": None}))
        sql, values = cur.calls[-1]
        self.assertIn("model_year = COALESCE(%s, model_year)", sql)
        self.assertIsNone(values[obj.EXTRACTED_FIELDS.index("model_year")])

    def test_existing_master_year_and_sku_are_not_overwritten(self):
        cur = Cursor(existing=("KEEP-SKU", "55 inches"), master_records=[(3, "2024")])
        obj = crawler(cur)
        self.assertTrue(obj.upsert_item_mst({"item": "test-item", "sku": "NEW-SKU", "model_year": "2025"}))
        updates = [call for call in cur.calls if "UPDATE tv_item_mst" in call[0]]
        self.assertEqual(updates, [])

    def test_missing_crawl_does_not_write_master_year(self):
        cur = Cursor(existing=("KEEP-SKU", "55 inches"))
        self.assertTrue(crawler(cur).upsert_item_mst({"item": "test-item", "model_year": None}))
        self.assertFalse(any("UPDATE tv_item_mst" in sql for sql, _ in cur.calls))

    def test_insert_and_update_resolve_year_at_save_boundary(self):
        for update in (False, True):
            with self.subTest(update=update):
                cur = Cursor(master_years=[2025])
                obj = crawler(cur, update)
                row = {"id": 99, "item": "test-item", "model_year": None, "final_sku_price": "$123"}
                self.assertTrue(obj.save_to_retail_com(row))
                sql, values = cur.calls[-1]
                self.assertIn("test_tv_retail_com", sql)
                self.assertEqual(values[obj.EXTRACTED_FIELDS.index("model_year")], "2025")
                self.assertEqual(values[obj.EXTRACTED_FIELDS.index("final_sku_price")], "$123")
                self.assertNotIn("FOR SHARE", cur.calls[0][0])
                self.assertNotIn("FOR UPDATE", cur.calls[0][0])
                if update:
                    self.assertIn("model_year = COALESCE(%s, model_year)", sql)
                    self.assertIn("detailed_review_content = COALESCE", sql)
                    self.assertIn("final_sku_price = %s", sql)
                obj.db_conn.commit.assert_called_once()

    def test_master_query_failure_rolls_back_without_retail_write(self):
        cur = Cursor()
        cur.execute = Mock(side_effect=RuntimeError("simulated query failure"))
        obj = crawler(cur)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(obj.save_to_retail_com({"item": "test-item"}))
        self.assertFalse(any("INSERT INTO" in sql for sql, _ in cur.calls))
        obj.db_conn.rollback.assert_called_once()


class BestbuyModelYearTests(unittest.TestCase):
    def test_duplicate_listing_rows_never_export_master_year(self):
        for values in ([None, "2025"], ["2025", "2025"], ["2024", "2025"]):
            with patch.dict(MASTER, {"model_from_detail_top": lambda _: "KEEP-SKU"}), contextlib.redirect_stdout(io.StringIO()):
                rows = MASTER["item_mst_rows"]("TV", [{"item": "tv-item", "model_year": value} for value in values])
            self.assertEqual(len(rows), 1)
            self.assertNotIn("model_year", rows[0])

    def test_manual_year_remains_in_schema_but_not_master_export(self):
        with patch.dict(MASTER, {"model_from_detail_top": lambda _: "KEEP-SKU"}):
            rows = MASTER["item_mst_rows"]("TV", [{"item": "tv-item", "model_year": "2025"}])
        self.assertNotIn("model_year", rows[0])
        self.assertIn("model_year", dict(MASTER["table_columns"]("TV")))
        self.assertNotIn("model_year", MASTER["output_fields"]("TV"))
        self.assertNotIn("model_year", MASTER["output_fields"]("HHP"))

    def test_generic_missing_only_updater_does_not_manage_year_overwrite(self):
        columns = [("model_year", "text")]
        self.assertEqual(MASTER["missing_only_updates"]({"model_year": "2026"}, {"model_year": "2025"}, columns), [])
        for missing in (None, "", "null", "[null]"):
            self.assertEqual(MASTER["missing_only_updates"]({"model_year": "2025"}, {"model_year": missing}, columns),
                             [])
        self.assertEqual(MASTER["missing_only_updates"]({"model_year": "unknown"}, {"model_year": None}, columns), [])

    def test_hydration_scopes_item_account_and_active_flag(self):
        cur = Cursor(master_rows=[("tv-item", "2025")])
        rows = [
            {"item": "tv-item", "account_name": "Bestbuy", "model_year": None, "final_sku_price": "$123"},
            {"item": "tv-item", "account_name": "Walmart", "model_year": None},
            {"item": "other-item", "account_name": "Bestbuy", "model_year": None},
            {"item": "", "account_name": "Bestbuy", "model_year": None},
        ]
        result = MASTER["hydrate_tv_model_years"](cur, rows)
        self.assertEqual(result["filled_rows"], 1)
        self.assertEqual(rows[0]["model_year"], "2025")
        self.assertEqual(rows[0]["final_sku_price"], "$123")
        self.assertTrue(all(row["model_year"] is None for row in rows[1:]))
        self.assertEqual(cur.calls[1][1], ("Bestbuy", ["other-item", "tv-item"]))
        self.assertIn("is_product = TRUE", cur.calls[1][0])
        self.assertIn("ORDER BY id DESC", cur.calls[1][0])
        self.assertNotIn("FOR SHARE", cur.calls[1][0])

    def test_collected_year_wins_without_master_query(self):
        rows = [{"item": "tv-item", "account_name": "Bestbuy", "model_year": "2024"}]
        cur = Cursor(master_rows=[("tv-item", "2025")])
        with contextlib.redirect_stdout(io.StringIO()):
            result = MASTER["hydrate_tv_model_years"](cur, rows)
        self.assertEqual(rows[0]["model_year"], "2024")
        self.assertEqual(result["filled_rows"], 0)
        self.assertEqual(cur.calls, [])

    def test_conflicting_duplicates_use_latest_valid_row(self):
        rows = [{"item": "tv-item", "account_name": "Bestbuy", "model_year": None}]
        cur = Cursor(master_rows=[("tv-item", "2024"), ("tv-item", "2025")])
        MASTER["hydrate_tv_model_years"](cur, rows)
        self.assertEqual(rows[0]["model_year"], "2024")

    def test_missing_master_schema_reports_skip_without_ddl(self):
        cur = Cursor(columns=["item", "account_name"])
        result = MASTER["hydrate_tv_model_years"](cur, [{"item": "tv-item", "account_name": "Bestbuy", "model_year": None}])
        self.assertIn("skipped", result)
        self.assertEqual(len(cur.calls), 1)

    def test_master_load_ignores_year_even_in_old_input_rows(self):
        columns = [("item", "text"), ("account_name", "text"), ("sku", "text"), ("model_year", "text")]
        row = {"item": "tv-item", "account_name": "Bestbuy", "sku": "KEEP-SKU", "model_year": "2025"}
        cur = Cursor()
        self.assertEqual(MASTER["load_rows"](cur, "tv_item_mst", columns, [row])["inserted"], 1)
        self.assertNotIn("model_year", cur.calls[-1][0])
        self.assertNotIn("2025", cur.calls[-1][1])
        cur = Cursor(existing=("KEEP-SKU",))
        self.assertEqual(MASTER["load_rows"](cur, "tv_item_mst", columns, [row])["updated"], 0)
        self.assertFalse(any("UPDATE" in sql for sql, _ in cur.calls))

    def test_load_one_hydrates_before_delete_insert_and_scopes_recovery(self):
        for upsert in (False, True):
            with self.subTest(upsert=upsert):
                rows = [{"item": "tv-item", "account_name": "Bestbuy", "model_year": None}]
                events = []
                def hydrate(cur, selected):
                    events.append("hydrate")
                    selected[0]["model_year"] = "2025"
                    return {"filled_rows": 1}
                def insert(cur, table, columns, selected, *args):
                    events.append("write")
                    self.assertEqual(selected[0]["model_year"], "2025")
                    return {"inserted": 1}
                with patch.dict(LOAD, {"read_csv": lambda _: copy.deepcopy(rows),
                        "table_columns": lambda *args: [("model_year", "text")],
                        "hydrate_tv_model_years": hydrate, "insert_rows": insert,
                        "row_upsert_rows": insert, "ROW_UPSERT_ONLY": upsert}):
                    result = LOAD["load_one"](Cursor(), "unused.csv", "tv_retail_com")
                self.assertEqual(events, ["hydrate", "write"])
                self.assertEqual(result["model_year_master"]["filled_rows"], 1)

    def test_dry_run_product_list_and_other_categories_do_not_hydrate(self):
        for category, table, dry in [("TV", "tv_retail_com", True),
                                      ("TV", "bby_tv_product_list", False),
                                      ("REF", "ref_retail_com", False),
                                      ("LDY", "ldy_retail_com", False),
                                      ("HHP", "hhp_retail_com", False)]:
            with self.subTest(category=category, table=table, dry=dry):
                hydrate = Mock(side_effect=AssertionError("unexpected master hydration"))
                with patch.dict(LOAD, {"CATEGORY": category, "read_csv": lambda _: [{"model_year": "2025"}],
                        "table_columns": lambda *args: [("model_year", "text")],
                        "hydrate_tv_model_years": hydrate, "insert_rows": lambda *args: {"inserted": 1}}):
                    LOAD["load_one"](Cursor(), "unused.csv", table, dry)
                hydrate.assert_not_called()


class MemoryDB:
    """Execute data/order logic in SQLite; PostgreSQL locking is not emulated."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.create_function("btrim", 1, lambda value: str(value).strip() if value is not None else None)
        master_fields = [name for name, _ in MASTER["table_columns"]("TV")]
        master_defs = [f'"{name}" INTEGER PRIMARY KEY' if name == "id" else f'"{name}" TEXT'
                       for name in master_fields]
        # Boolean columns need actual boolean values for SQL is_product = TRUE.
        master_defs = [definition.replace('"is_product" TEXT', '"is_product" INTEGER')
                      .replace('"is_checked" TEXT', '"is_checked" INTEGER') for definition in master_defs]
        self.db.execute('CREATE TABLE tv_item_mst (' + ', '.join(master_defs) + ')')
        retail_names = set(WALMART.EXTRACTED_FIELDS + WALMART.PASSTHROUGH_FIELDS + list(WALMART.SAVE_META_FIELDS))
        self.db.execute('CREATE TABLE test_tv_retail_com (id INTEGER PRIMARY KEY, ' +
                        ', '.join(f'"{name}" TEXT' for name in sorted(retail_names)) + ')')
        self.db.execute('CREATE TABLE tv_retail_com (id INTEGER PRIMARY KEY, ' +
                        ', '.join(f'"{name}" TEXT' for name in sorted(retail_names)) + ')')
        self.cur = self.db.cursor()
        self.calls = []
        self.metadata = None
        self.closed = False

    def cursor(self):
        return self

    @property
    def rowcount(self):
        return self.cur.rowcount

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self.metadata = None
        if "information_schema.columns" in sql:
            self.metadata = [(row[1],) for row in self.db.execute('PRAGMA table_info(tv_item_mst)')]
            return
        sql = sql.replace('"public".', '')
        sql = re.sub(r'\bFOR (?:SHARE|UPDATE)\b', '', sql)
        sql = re.sub(r'("[^"]+"|\b\w+)::text', r'CAST(\1 AS TEXT)', sql)
        values, pieces = [], []
        iterator = iter(params or ())
        for part in re.split(r'(= ANY\(%s\)|%s)', sql):
            if part == '%s':
                value = next(iterator)
                values.append(value.isoformat(sep=' ') if isinstance(value, datetime) else value)
                pieces.append('?')
            elif part == '= ANY(%s)':
                group = next(iterator)
                pieces.append('IN (' + ','.join('?' for _ in group) + ')')
                values.extend(group)
            else:
                pieces.append(part)
        self.cur.execute(''.join(pieces), values)

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)

    def fetchall(self):
        return self.metadata if self.metadata is not None else self.cur.fetchall()

    def fetchone(self):
        return self.metadata[0] if self.metadata else self.cur.fetchone()

    def close(self):
        pass

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def seed(self, account, master_id, year, item="workflow-item", active=True,
             created="2026-01-01", updated=None):
        self.db.execute('INSERT INTO tv_item_mst (id,item,account_name,sku,screen_size,model_year,is_product,created_at,updated_at) '
                        'VALUES (?,?,?,?,?,?,?,?,?)',
                        (master_id,item,account,"KEEP-SKU","55 inches",year,active,created,updated))
        self.db.commit()


class ModelYearWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.db.close()

    def store(self):
        store = MemoryDB()
        self.stores.append(store)
        return store

    def cycle(self, store, account, incoming):
        raw = {"item": "workflow-item", "account_name": account, "model_year": incoming,
               "sku": "KEEP-SKU", "screen_size": "55 inches", "is_product": True}
        if account == "Walmart":
            obj = crawler(store)
            obj.db_conn = store
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(obj.upsert_item_mst(raw))
                self.assertTrue(obj.save_to_retail_com(raw))
            return store.db.execute('SELECT model_year FROM test_tv_retail_com ORDER BY id DESC LIMIT 1').fetchone()[0]
        final = {key: raw[key] for key in ("item", "account_name", "model_year")}
        MASTER["hydrate_tv_model_years"](store, [final])
        LOAD["insert_rows"](store, "tv_retail_com",
                            [("item", "text"), ("account_name", "text"), ("model_year", "text")], [final])
        store.commit()
        MASTER["load_rows"](store, "tv_item_mst", MASTER["table_columns"]("TV"), [raw])
        store.commit()
        return store.db.execute('SELECT model_year FROM tv_retail_com ORDER BY id DESC LIMIT 1').fetchone()[0]

    def test_collected_2026_preserves_manual_master_and_next_null_reuses_2025(self):
        for account in ("Walmart", "Bestbuy"):
            with self.subTest(account=account):
                store = self.store()
                store.seed(account, 1, "2025")
                store.seed(account, 2, None)
                self.assertEqual(self.cycle(store, account, "2026"), "2026")
                self.assertEqual(self.cycle(store, account, None), "2025")
                self.assertEqual(store.db.execute('SELECT id,model_year FROM tv_item_mst ORDER BY id').fetchall(),
                                 [(1, "2025"), (2, None)])

    def test_latest_inserted_valid_id_wins_over_year_value_and_update_time(self):
        for account in ("Walmart", "Bestbuy"):
            with self.subTest(account=account):
                store = self.store()
                store.seed(account, 10, "2025", updated="2026-09-12")
                store.seed(account, 20, "2024", created="2025-01-01")
                store.seed(account, 30, None)
                store.seed(account, 40, "2027", active=False)
                store.seed("Other-shop", 50, "2028")
                store.seed(account, 60, "2029", item="other-item")
                self.assertEqual(self.cycle(store, account, None), "2024")
                self.assertEqual(self.cycle(store, account, "2026"), "2026")
                self.assertEqual(self.cycle(store, account, None), "2024")

    def test_all_blank_masters_remain_blank_after_successful_collection(self):
        for account in ("Walmart", "Bestbuy"):
            with self.subTest(account=account):
                store = self.store()
                store.seed(account, 1, None)
                store.seed(account, 2, None)
                self.assertEqual(self.cycle(store, account, "2026"), "2026")
                self.assertIsNone(self.cycle(store, account, None))
                self.assertEqual(store.db.execute('SELECT id,model_year FROM tv_item_mst ORDER BY id').fetchall(),
                                 [(1, None), (2, None)])

    def test_new_product_year_requires_manual_master_entry(self):
        for account in ("Walmart", "Bestbuy"):
            with self.subTest(account=account):
                store = self.store()
                self.assertEqual(self.cycle(store, account, "2026"), "2026")
                self.assertIsNone(self.cycle(store, account, None))
                self.assertTrue(all(year is None for (year,) in store.db.execute('SELECT model_year FROM tv_item_mst')))
                store.db.execute("UPDATE tv_item_mst SET model_year='2025' WHERE item='workflow-item'")
                store.commit()
                self.assertEqual(self.cycle(store, account, None), "2025")

    def test_appended_master_replaces_fallback_but_editing_older_id_does_not(self):
        for account in ("Walmart", "Bestbuy"):
            with self.subTest(account=account):
                store = self.store()
                store.seed(account, 1, "2025")
                self.assertEqual(self.cycle(store, account, None), "2025")
                store.seed(account, 2, "2024", created="2020-01-01")
                store.db.execute("UPDATE tv_item_mst SET model_year='2027',updated_at='2026-09-12' WHERE id=1")
                store.commit()
                self.assertEqual(self.cycle(store, account, None), "2024")

    def test_valid_collection_never_overwrites_single_master(self):
        for account in ("Walmart", "Bestbuy"):
            with self.subTest(account=account):
                store = self.store()
                store.seed(account, 1, "2025")
                self.assertEqual(self.cycle(store, account, "2026"), "2026")
                self.assertEqual(store.db.execute('SELECT model_year FROM tv_item_mst WHERE id=1').fetchone()[0], "2025")
                self.assertEqual(self.cycle(store, account, None), "2025")
                self.assertEqual(self.cycle(store, account, "2024"), "2024")
                self.assertEqual(self.cycle(store, account, None), "2025")

    def test_missing_or_invalid_collection_never_erases_master(self):
        for account in ("Walmart", "Bestbuy"):
            with self.subTest(account=account):
                store = self.store()
                store.seed(account, 1, "2025")
                for missing in (None, "", "null", "unknown"):
                    self.assertEqual(self.cycle(store, account, missing), "2025")
                self.assertEqual(store.db.execute('SELECT model_year FROM tv_item_mst WHERE id=1').fetchone()[0], "2025")

    def test_same_collected_year_does_not_touch_master_timestamp(self):
        for account in ("Walmart", "Bestbuy"):
            with self.subTest(account=account):
                store = self.store()
                store.seed(account, 1, "2025", updated="2020-01-01")
                self.assertEqual(self.cycle(store, account, "2025"), "2025")
                self.assertEqual(store.db.execute('SELECT updated_at FROM tv_item_mst WHERE id=1').fetchone()[0], "2020-01-01")

    def test_walmart_normal_test_and_update_modes_preserve_manual_year(self):
        for test_mode in (False, True):
            for update in (False, True):
                with self.subTest(test_mode=test_mode, update=update):
                    store = self.store()
                    store.seed("Walmart", 1, "2025")
                    obj = crawler(store, update=update)
                    obj.db_conn, obj.test_mode = store, test_mode
                    table = 'test_tv_retail_com' if test_mode else 'tv_retail_com'
                    store.db.execute(f"INSERT INTO {table} (id,model_year) VALUES (99,'2024')")
                    store.commit()
                    for incoming, expected in (("2026", "2026"), (None, "2025")):
                        row = {"id": 99, "item": "workflow-item", "sku": "KEEP-SKU",
                               "screen_size": "55 inches", "model_year": incoming}
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertTrue(obj.upsert_item_mst(row))
                            self.assertTrue(obj.save_to_retail_com(row))
                        self.assertEqual(store.db.execute(f'SELECT model_year FROM {table} ORDER BY id DESC LIMIT 1').fetchone()[0], expected)
                        self.assertEqual(store.db.execute('SELECT model_year FROM tv_item_mst').fetchone()[0], "2025")

    def test_walmart_failed_retail_save_never_changes_master_year(self):
        store = self.store()
        store.seed("Walmart", 1, "2025")
        obj = crawler(store)
        obj.db_conn = store
        row = {"item": "workflow-item", "sku": "KEEP-SKU", "screen_size": "55 inches", "model_year": "2026"}
        execute = store.execute
        def fail_retail(sql, params=()):
            if "INSERT INTO test_tv_retail_com" in sql:
                raise RuntimeError("simulated retail save failure")
            return execute(sql, params)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(obj.upsert_item_mst(row))
            with patch.object(store, "execute", fail_retail):
                self.assertFalse(obj.save_to_retail_com(row))
        self.assertEqual(store.db.execute('SELECT model_year FROM tv_item_mst').fetchone()[0], "2025")
        self.assertEqual(store.db.execute('SELECT count(*) FROM test_tv_retail_com').fetchone()[0], 0)

    def test_bestbuy_partial_replay_does_not_change_selected_or_unselected_master_year(self):
        store = self.store()
        store.seed("Bestbuy", 1, "2026", item="selected-item")
        store.seed("Bestbuy", 2, "2026", item="unselected-item")
        raw = [{"item": item, "account_name": "Bestbuy", "model_year": "2025",
                "sku": "KEEP-SKU", "screen_size": "55 inches"}
               for item in ("selected-item", "unselected-item")]
        with patch.dict(LOAD, {"ROW_UPSERT_ITEMS": {"selected-item"}, "ROW_UPSERT_SKUS": set()}):
            self.assertEqual([row["item"] for row in LOAD["row_upsert_candidates"](raw)], ["selected-item"])
        MASTER["load_rows"](store, "tv_item_mst", MASTER["table_columns"]("TV"), raw)
        store.commit()
        self.assertEqual(store.db.execute('SELECT model_year FROM tv_item_mst ORDER BY id').fetchall(), [("2026",), ("2026",)])

    def test_bestbuy_fallback_does_not_depend_on_master_load_step(self):
        store = self.store()
        store.seed("Bestbuy", 1, "2025")
        for incoming, expected in (("2026", "2026"), (None, "2025")):
            row = {"item": "workflow-item", "account_name": "Bestbuy", "model_year": incoming}
            MASTER["hydrate_tv_model_years"](store, [row])
            LOAD["insert_rows"](store, "tv_retail_com", [("item", "text"), ("account_name", "text"), ("model_year", "text")], [row])
            store.commit()
            self.assertEqual(store.db.execute('SELECT model_year FROM tv_retail_com ORDER BY id DESC LIMIT 1').fetchone()[0], expected)
        self.assertEqual(store.db.execute('SELECT model_year FROM tv_item_mst').fetchone()[0], "2025")

    def test_other_master_fields_can_be_filled_without_writing_year(self):
        for account in ("Walmart", "Bestbuy"):
            for previous in (None, "2025"):
                with self.subTest(account=account, previous=previous):
                    store = self.store()
                    store.seed(account, 1, previous)
                    store.db.execute('UPDATE tv_item_mst SET screen_size=NULL')
                    store.commit()
                    self.assertEqual(self.cycle(store, account, "2026"), "2026")
                    self.assertEqual(store.db.execute('SELECT sku,screen_size,model_year FROM tv_item_mst').fetchone(),
                                     ("KEEP-SKU", "55 inches", previous))


if __name__ == "__main__":
    unittest.main()
