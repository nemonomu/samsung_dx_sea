"""Offline dryer/legacy/DB contract tests. No env files or services are accessed."""
import ast
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone, timedelta
from decimal import Decimal
from contextlib import redirect_stdout
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit
import uuid

from lowes import step00_dryer as rules
from lowes.lowes_dryer import APIResult, BrowserAPI, CollectionError, fetch_detail, run_collection, main as collector_main
from lowes.lowes_dryer_check import check_run
from lowes.lowes_dryer_check import main as check_main
from lowes import step00_dryer_audit as value_audit
from lowes import step14_dryer_db_load as loader


@contextmanager
def scratch():
    base = Path(tempfile.gettempdir()).resolve()
    path = base / ("lowes-dryer-test-" + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        assert path.resolve().parent == base and path.name.startswith("lowes-dryer-test-")
        shutil.rmtree(path)


def legacy_definitions(filename, namespace):
    path = Path(__file__).with_name(filename)
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def operations(limit=100):
    schema = legacy_definitions("step00_erd_schema.py", {"re": re})
    main = legacy_definitions("step02_main_targets.py", {
        "json": json, "TARGET_LIMIT": limit, "retailer_sku_name_text": schema["retailer_sku_name_text"],
    })
    bsr = legacy_definitions("step04_bsr_rank.py", {})
    merge = legacy_definitions("step07_final_targets.py", {"datetime": datetime, "os": os})
    finalize = legacy_definitions("step09_finalize.py", {
        "datetime": datetime, "FIELDNAMES": list(rules.COLUMNS), "PRODUCT_TYPE": "LDY",
        "CATEGORY_COLUMNS": ["ldy_capacity", "ldy_loading_type"],
        "retailer_sku_name_text": schema["retailer_sku_name_text"], "output_page_type": lambda r: "main",
    })
    return {"main": main["select_targets"], "bsr": bsr["build_rank_map"],
            "merge": merge["build_final_targets"], "finalize": finalize["finalize_row"]}


def card(item, name=None, sponsored=False, price=True):
    return {"product": {"omniItemId": str(item), "modelId": "MODEL" + str(item),
                        "brand": "Brand", "description": name or f"7-cu ft Electric Dryer {item}",
                        "pdURL": f"/pd/dryer/{item}", "rating": 4.1, "reviewCount": 7,
                        "sponsored": sponsored},
            "location": {"price": {"sellingPrice": 499, "wasPrice": 719, "totalSaving": 220}} if price else {}}


def detail(item, specs=None, model=None, name=None):
    return {"productDetails": {str(item): {"product": {
        "modelId": model or "MODEL" + str(item), "brand": "Brand",
        "description": name or "Electric Dryer",
        "specs": specs if specs is not None else [
            {"key": "Dryer Load Type", "value": "Front load"},
            {"key": "Dryer Capacity (Cu. Feet)", "value": "7.4"}],
    }, "mfePrice": {"price": {"additionalData": {"sellingPrice": 599, "wasPrice": 799},
                             "savings": {"totalSaving": 200}}}}}}


def final_row(item="1000", main=1, bsr=None):
    row = dict.fromkeys(rules.COLUMNS)
    row.update(country="SEA", account_name="Lowes", item=item, sku="MODEL" + item,
               retailer_sku_name="Brand Dryer", product_url=rules.BASE_URL + "/pd/dryer/" + item,
               count_of_reviews=7, count_of_star_ratings=7, star_rating="4.1",
               final_sku_price="$499", original_sku_price="$719", savings="$220",
               main_rank=main, bsr_rank=bsr, loading_type="Front load", capacity="7.4 Cu.Feet",
               calendar_week="w42", crawl_datetime="2026-10-14 10:00:00", batch_id="l_test")
    return row


def settings(**kwargs):
    return rules.Settings(page_wait=0, seed_wait=0, retry_wait=0, alt_store_enabled=False, **kwargs)


class RuleTests(unittest.TestCase):
    def test_settings_force_10010_and_keep_ldy_store_overrides(self):
        s = rules.Settings.from_env({"LOWES_API_STORE_ZIP": "10001", "LOWES_API_STORE_ID": "0289"})
        self.assertEqual(s.zip_code, "10010")
        self.assertEqual(s.detail_store, "289")
        self.assertEqual((s.main_limit, s.bsr_limit), (100, 100))
        self.assertIn("/0289/Guest/10010?", rules.detail_url("1000", s))
        with self.assertRaises(ValueError):
            rules.Settings(bsr_limit=101)

    def test_front_top_load_and_no_power_or_appliance_fallback(self):
        for value, expected in (("Front-load", "Front load"), ("Top load", "Top load")):
            result, _ = rules.dryer_specs({"specs": [{"key": "Dryer Load Type:", "value": value}]})
            self.assertEqual(result["loading_type"], expected)
        for specs in ([{"key": "Power Source", "value": "Electric"}],
                      [{"key": "Appliance Type", "value": "Electric Dryer"}],
                      [{"key": "Washer Load Type", "value": "Front load"}],
                      [{"key": "Dryer Load Type", "value": "Gas"}],
                      [{"key": "Dryer Load Type", "value": "Electric Dryer"}]):
            result, _ = rules.dryer_specs({"specs": specs})
            self.assertIsNone(result["loading_type"])

    def test_shared_capacity_is_null_and_raw_is_auditable(self):
        specs = [{"key": "Washer Dryer Capacity (Cu. Feet)", "value": "4.8"}]
        result, audit = rules.dryer_specs({"specs": specs})
        self.assertIsNone(result["capacity"])
        self.assertEqual(result["capacity_reason"], "shared_capacity_only")
        self.assertEqual(audit[0]["raw_value"], "4.8")
        result, _ = rules.dryer_specs({"specs": specs + [{"key": "Dryer Capacity (Cu. Feet)", "value": "7.0"}]})
        self.assertEqual(result["capacity"], "7 Cu.Feet")

    def test_conflicting_capacity_and_unknown_units_not_guessed(self):
        result, _ = rules.dryer_specs({"specs": [
            {"key": "Dryer Capacity (Cu. Feet)", "value": "7"},
            {"key": "Dryer Capacity (Cu. Feet)", "value": "8"}]})
        self.assertIsNone(result["capacity"])
        self.assertEqual(result["capacity_reason"], "conflicting_values")
        self.assertIsNone(rules.normalize_capacity("120 litres", "Dryer Capacity"))
        self.assertEqual(rules.normalize_capacity("7.0", "Dryer Capacity"), "7")

    def test_product_kind_by_evidence(self):
        cases = {"Electric Dryer": "dryer", "Top-Load Washer": "washer",
                 "All-In-One Washer Dryer Combo": "combo", "Dryer stacking kit": "other",
                 "Unclear appliance": "unknown"}
        for name, expected in cases.items():
            self.assertEqual(rules.product_kind({"description": name}), expected)
        self.assertEqual(rules.product_kind({"description": "Appliance", "categories": {
            "4294857980": {"label": "Dryers"}}}), "dryer")
        self.assertEqual(rules.product_kind({"description": "Top-Load Washer", "categories": {
            "parent": {"label": "Washers & Dryers"}}}), "washer")
        self.assertEqual(rules.product_kind({"description": "Appliance", "categories": {
            "parent": {"label": "Washers & Dryers"}}}), "unknown")

    def test_fractional_rank_not_silently_truncated(self):
        row = final_row()
        row["main_rank"] = 1.5
        with self.assertRaises(ValueError):
            rules.validate_rows([row])

    def test_main_dedup_filter_sponsored_and_contiguous_limit(self):
        raw = [card(1000, "Washer", True), card(1001, sponsored=True), card(1001),
               card(1002, "All-In-One Washer Dryer Combo", True)] + [card(i) for i in range(1003, 1110)]
        rows = [rules.parse_card(c, 1, i, i) for i, c in enumerate(raw, 1)]
        selected = rules.select_main(rows, 100)
        self.assertEqual(len(selected), 100)
        self.assertEqual(selected[0]["omni_item_id"], "1001")
        self.assertTrue(selected[0]["sponsored"])
        self.assertEqual(selected[1]["kind"], "combo")
        self.assertEqual([r["main_rank"] for r in selected], list(range(1, 101)))
        legacy, _ = operations()["main"](selected)
        self.assertEqual([r["omni_item_id"] for r in legacy], [r["omni_item_id"] for r in selected])

    def test_bsr_cutoff_before_dedup_never_backfills_rank101(self):
        raw = [card(1000), card(1000)] + [card(i) for i in range(1001, 1100)]
        rows = [rules.parse_card(c, 1, i, i) for i, c in enumerate(raw, 1)]
        selected = rules.select_bsr(rows, 100)
        self.assertEqual(len(selected), 99)
        self.assertNotIn("1099", {r["omni_item_id"] for r in selected})
        self.assertEqual([r["bsr_rank"] for r in selected], list(range(1, 100)))

    def test_merge_keeps_two_ranks_and_bsr_only(self):
        main = [dict(rules.parse_card(card(1000 + i), 1, i + 1, i + 1), main_rank=i + 1)
                for i in range(10)]
        bsr = [dict(rules.parse_card(card(item), 1, i + 1, i + 1), bsr_rank=i + 1)
               for i, item in enumerate([1100, 1101, 1102, 1103, 1009])]
        merged = operations()["merge"](main, bsr)
        self.assertEqual(len(merged), 14)
        self.assertEqual((merged[9]["main_rank"], merged[9]["bsr_rank"]), (10, 5))
        self.assertEqual(merged[10]["main_rank"], "")

    def test_detail_reviews_zero_priority_and_legacy_listing_prices(self):
        source = rules.parse_card(card(1000), 1, 1, 1)
        source.update(main_rank=1, batch_id="l_test")
        row, _ = rules.enrich_row(source, detail(1000), {
            "reviewStatistics": {"averageOverallRating": 0, "totalReviewCount": 0}})
        output = rules.finalize_dryer(row, "l_test", "2026-10-14 10:00:00", operations()["finalize"])
        self.assertEqual(tuple(output), rules.COLUMNS)
        self.assertEqual(output["final_sku_price"], "$499")
        self.assertEqual(output["star_rating"], "0")
        self.assertEqual(output["count_of_reviews"], 0)
        self.assertEqual(output["count_of_star_ratings"], 0)
        self.assertEqual(output["crawl_datetime"], "2026-10-14 10:00:00")

    def test_detail_failure_keeps_listing_values_without_inventing_specs(self):
        source = rules.parse_card(card(1000), 1, 1, 1)
        source.update(main_rank=1)
        row, _ = rules.enrich_row(source, None, None)
        output = rules.finalize_dryer(row, "l_test", "2026-10-14 10:00:00", operations()["finalize"])
        self.assertEqual(output["sku"], "MODEL1000")
        self.assertEqual(output["final_sku_price"], "$499")
        self.assertEqual(output["count_of_reviews"], 7)
        self.assertIsNone(output["capacity"])
        self.assertIsNone(output["loading_type"])

    def test_model_conflict_preserves_valid_listing_model(self):
        row, _ = rules.enrich_row(rules.parse_card(card(1000), 1, 1, 1), detail(1000, model="OTHER"), None)
        self.assertEqual(row["sku"], "MODEL1000")
        self.assertTrue(row["_sku_conflict"])

    def test_malformed_price_keeps_other_detail_fields_and_no_object_dump(self):
        source = rules.parse_card(card(1000), 1, 1, 1)
        payload = detail(1000)
        payload["productDetails"]["1000"]["mfePrice"]["price"] = ["UNEXPECTED_PAYLOAD"]
        row, _ = rules.enrich_row(source, payload, None)
        self.assertTrue(row["_detail_price_invalid"])
        self.assertEqual(row["capacity"], "7.4 Cu.Feet")
        self.assertIsNone(row["_detail_selling_price"])
        self.assertEqual(rules.public_text({"arbitrary": "UNEXPECTED_PAYLOAD"}), "")
        self.assertIsNone(rules.numeric_source({"arbitrary": "UNEXPECTED_PAYLOAD"}, currency=True))

    def test_unknown_review_response_not_assumed_zero(self):
        with self.assertRaises(ValueError):
            rules.validate_reviews({"results": []})
        self.assertEqual(rules.validate_reviews({"totalResults": 0})["totalReviewCount"], 0)
        for stats in ({"averageOverallRating": "nan"}, {"averageOverallRating": 6},
                      {"totalReviewCount": -1}, {"totalReviewCount": 1.5}):
            with self.assertRaises(ValueError):
                rules.validate_reviews({"reviewStatistics": stats})

    def test_invalid_listing_numbers_never_become_fallback_values(self):
        for rating, count, price in ((6, -1, -499), (-1, 1.5, "nan")):
            listing = card(1000)
            listing["product"].update(rating=rating, reviewCount=count)
            listing["location"]["price"].update(sellingPrice=price, wasPrice=-719, totalSaving=-220)
            source = rules.parse_card(listing, 1, 1, 1)
            source.update(main_rank=1)
            row, _ = rules.enrich_row(source, None, None)
            output = rules.finalize_dryer(row, "l_test", "2026-10-14 10:00:00", operations()["finalize"])
            for column in ("star_rating", "count_of_reviews", "count_of_star_ratings",
                           "final_sku_price", "original_sku_price", "savings"):
                self.assertIsNone(output[column], column)

        listing = card(1000)
        listing["product"].update(rating=0, reviewCount=0)
        listing["location"]["price"].update(sellingPrice=0, wasPrice=None, totalSaving=0)
        source = rules.parse_card(listing, 1, 1, 1)
        source.update(main_rank=1)
        row, _ = rules.enrich_row(source, None, None)
        output = rules.finalize_dryer(row, "l_test", "2026-10-14 10:00:00", operations()["finalize"])
        self.assertEqual(output["star_rating"], "0")
        self.assertEqual(output["count_of_reviews"], "0")
        self.assertEqual(output["final_sku_price"], "$0")

        source = rules.parse_card(card(1000, price=False), 1, 1, 1)
        source.update(bsr_rank=1)
        payload = detail(1000)
        payload["productDetails"]["1000"]["mfePrice"]["price"].update(
            additionalData={"sellingPrice": -599, "wasPrice": -799}, savings={"totalSaving": -200})
        row, _ = rules.enrich_row(source, payload, None)
        output = rules.finalize_dryer(row, "l_test", "2026-10-14 10:00:00", operations()["finalize"])
        self.assertTrue(row["_detail_price_invalid"])
        self.assertTrue(all(output[column] is None for column in (
            "final_sku_price", "original_sku_price", "savings")))

    def test_api_containers_and_pagination(self):
        for payload in ({"itemList": [card(1000)]}, {
            "productListCommonNormalizedPageSpecificProducts": {"productList": {"products": [card(1000)]}}}, {
            "itemList": [], "productListCommonNormalizedPageSpecificProducts": {
                "productList": {"products": [card(1000)]}}}):
            self.assertEqual(len(rules.listing_page(payload)[0]), 1)
        with self.assertRaises(ValueError):
            rules.listing_page({"recommendations": [card(1000)]})
        self.assertEqual(rules.next_offsets(24, {"adjusted_next": 52}), (48, 52))
        self.assertEqual(rules.next_offsets(120, {"adjusted_next": 120}, previous_adjusted=100), (144, 120))
        with self.assertRaises(ValueError):
            rules.next_offsets(24, {"adjusted_next": 24})


class DBTests(unittest.TestCase):
    def test_connection_defaults_missing_database_without_mutating_settings(self):
        from types import SimpleNamespace
        import sys
        for database in (None, ""):
            config = {"host": "offline.invalid", "user": "offline"}
            if database is not None:
                config["database"] = database
            original = dict(config)
            expected_connection = object()
            connect = MagicMock(return_value=expected_connection)
            with patch.dict(sys.modules, {
                "lowes.step00_config": SimpleNamespace(db_config=lambda: config),
                "psycopg2": SimpleNamespace(connect=connect),
            }):
                self.assertIs(loader.connect_db(), expected_connection)
            self.assertEqual(connect.call_args.kwargs["dbname"], "postgres")
            self.assertEqual(config, original)

    def test_connection_preserves_explicit_database(self):
        from types import SimpleNamespace
        import sys
        connect = MagicMock()
        with patch.dict(sys.modules, {
            "lowes.step00_config": SimpleNamespace(db_config=lambda: {
                "host": "offline.invalid", "user": "offline", "database": "offline_dryer",
            }),
            "psycopg2": SimpleNamespace(connect=connect),
        }):
            loader.connect_db()
        self.assertEqual(connect.call_args.kwargs["dbname"], "offline_dryer")

    def test_connection_rejects_missing_host_or_user_before_connecting(self):
        from types import SimpleNamespace
        import sys
        for config in ({}, {"host": "offline.invalid"}, {"user": "offline"}):
            connect = MagicMock()
            with patch.dict(sys.modules, {
                "lowes.step00_config": SimpleNamespace(db_config=lambda: config),
                "psycopg2": SimpleNamespace(connect=connect),
            }):
                with self.assertRaises(loader.DryerDBError) as failure:
                    loader.connect_db()
            self.assertEqual(failure.exception.public_reason, "DB settings not configured")
            connect.assert_not_called()

    def metadata(self):
        result = {c: {"type": "text", "identity": "NO", "default": None, "nullable": "YES"}
                  for c in rules.COLUMNS}
        result["id"].update(type="bigint", default="nextval('dryer_test_seq')", nullable="NO")
        for c in ("main_rank", "bsr_rank", "count_of_reviews", "count_of_star_ratings"):
            result[c]["type"] = "integer"
        for c in ("final_sku_price", "original_sku_price", "savings", "star_rating", "capacity"):
            result[c]["type"] = "numeric"
        result["crawl_datetime"]["type"] = "timestamp without time zone"
        return result

    def connection(self, count=1, insert_error=None):
        metadata = self.metadata()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchall.return_value = [(c, m["type"], m["identity"], m["default"], m["nullable"])
                                    for c, m in metadata.items()]
        cur.fetchone.return_value = (count,)
        cur.rowcount = 2
        cur.executemany.side_effect = insert_error
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        return conn, cur

    def test_types_id_autogeneration_and_null(self):
        row = final_row()
        columns, values = loader.prepare_insert([row], self.metadata())
        self.assertEqual(len(columns), 19)
        self.assertNotIn("id", columns)
        mapped = dict(zip(columns, values[0]))
        self.assertEqual(mapped["final_sku_price"], Decimal(499))
        self.assertEqual(mapped["capacity"], Decimal("7.4"))
        self.assertIsNone(mapped["bsr_rank"])
        self.assertIsInstance(mapped["crawl_datetime"], datetime)

    def test_capacity_float_storage_tolerance_does_not_hide_real_mismatch(self):
        self.assertTrue(loader.db_value_matches("capacity", 7.400000095367432, Decimal("7.4"), "real"))
        self.assertFalse(loader.db_value_matches("capacity", 7.41, Decimal("7.4"), "real"))
        self.assertFalse(loader.db_value_matches("item", 1001.0, Decimal(1000), "real"))

    def test_identifier_types_do_not_remove_model_zeros_or_round_product_ids(self):
        self.assertEqual(rules.coerce_db_value("sku", "001234", "text"), "001234")
        with self.assertRaises(ValueError):
            rules.coerce_db_value("sku", "001234", "integer")
        with self.assertRaises(ValueError):
            rules.coerce_db_value("item", "5015296155", "real")

    def test_same_batch_delete_scoped_to_lowes_and_insert_transaction(self):
        conn, cur = self.connection()
        result = loader.load_batch([final_row()], connection=conn)
        deletes = [(c.args[0], c.args[1]) for c in cur.execute.call_args_list if c.args[0].startswith("DELETE")]
        self.assertEqual(len(deletes), 1)
        self.assertIn('"public"."ldy_dryer_retail_test"', deletes[0][0])
        self.assertEqual(deletes[0][1], ("l_test", "Lowes"))
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(conn.__exit__.call_args.args, (None, None, None))

    def test_bad_values_fail_before_delete(self):
        conn, cur = self.connection()
        row = final_row()
        row["final_sku_price"] = "unavailable"
        with self.assertRaises(loader.DryerDBError):
            loader.load_batch([row], connection=conn)
        self.assertFalse(any(c.args[0].startswith("DELETE") for c in cur.execute.call_args_list))

    def test_insert_error_and_count_mismatch_trigger_transaction_rollback(self):
        for count, error in ((1, RuntimeError("simulated_insert_failure")), (0, None)):
            conn, _ = self.connection(count, error)
            with self.assertRaises(Exception):
                loader.load_batch([final_row()], connection=conn)
            self.assertIsNotNone(conn.__exit__.call_args.args[0])

    def test_production_and_foreign_table_not_implicitly_loaded(self):
        with patch.object(loader, "connect_db") as connect:
            for table in (rules.PRODUCTION_TABLE, "public.ldy_retail_com"):
                with self.assertRaises(ValueError):
                    loader.load_batch([final_row()], table=table)
            connect.assert_not_called()

    def test_missing_column_or_required_manual_id_not_silently_omitted(self):
        meta = self.metadata()
        meta["id"]["default"] = None
        with self.assertRaises(loader.DryerDBError):
            loader.prepare_insert([final_row()], meta)
        conn, cur = self.connection()
        cur.fetchall.return_value = [r for r in cur.fetchall.return_value if r[0] != "capacity"]
        with self.assertRaises(loader.DryerDBError):
            loader.load_batch([final_row()], connection=conn)
        self.assertFalse(any(c.args[0].startswith("DELETE") for c in cur.execute.call_args_list))

    def test_verify_only_checks_all19_values_and_generated_id_without_mutation(self):
        conn, cur = self.connection()
        row = final_row()
        expected = tuple(123 if c == "id" else rules.coerce_db_value(c, row[c], self.metadata()[c]["type"])
                         for c in rules.COLUMNS)
        schema_rows = cur.fetchall.return_value
        cur.fetchall.side_effect = [schema_rows, [expected]]
        result = loader.verify_batch([row], connection=conn)
        self.assertTrue(result["verified"])
        self.assertEqual(result["compared_columns"], [c for c in rules.COLUMNS if c != "id"])
        self.assertTrue(result["id_generation_verified"])
        self.assertEqual(result["id_values_checked"], 1)
        self.assertTrue(all(c.args[0].startswith("SELECT") for c in cur.execute.call_args_list))
        cur.executemany.assert_not_called()

    def test_db_mismatches_identify_price_reviews_metadata_and_product(self):
        row = final_row()
        for column, bad in (("final_sku_price", Decimal(500)), ("star_rating", Decimal(4.9)),
                            ("count_of_reviews", 8), ("country", "OTHER"),
                            ("crawl_datetime", datetime(2026, 10, 15, 10))):
            conn, cur = self.connection()
            values = {c: 123 if c == "id" else rules.coerce_db_value(c, row[c], self.metadata()[c]["type"])
                      for c in rules.COLUMNS}
            values[column] = bad
            cur.fetchall.side_effect = [cur.fetchall.return_value, [tuple(values[c] for c in rules.COLUMNS)]]
            with self.assertRaises(loader.DryerDBError) as failure:
                loader.verify_batch([row], connection=conn)
            self.assertEqual(failure.exception.differences[0]["item"], "1000")
            self.assertEqual(failure.exception.differences[0]["column"], column)
            self.assertTrue(all(call.args[0].startswith("SELECT") for call in cur.execute.call_args_list))

    def test_generated_ids_must_be_nonnull_and_unique(self):
        rows = [final_row("1000", main=1), final_row("1001", main=2)]
        for identifiers in ((None, 123), (123, 123)):
            conn, cur = self.connection(count=2)
            saved = [tuple(identifier if c == "id" else rules.coerce_db_value(c, row[c], self.metadata()[c]["type"])
                           for c in rules.COLUMNS) for row, identifier in zip(rows, identifiers)]
            cur.fetchall.side_effect = [cur.fetchall.return_value, saved]
            with self.assertRaises(loader.DryerDBError) as failure:
                loader.verify_batch(rows, connection=conn)
            self.assertTrue(any(d["column"] == "id" for d in failure.exception.differences))

    def test_float_money_and_rating_noise_never_hides_changed_cent_or_count(self):
        self.assertTrue(loader.db_value_matches("final_sku_price", 1499.989990234375, Decimal("1499.99"), "real"))
        self.assertFalse(loader.db_value_matches("final_sku_price", 1499.98, Decimal("1499.99"), "real"))
        self.assertTrue(loader.db_value_matches("star_rating", 4.349999904632568, Decimal("4.35"), "real"))
        self.assertFalse(loader.db_value_matches("count_of_reviews", 10000000.0, Decimal("10000001"), "real"))
        local = datetime(2026, 10, 14, 10)
        self.assertTrue(loader.db_value_matches("crawl_datetime", local.replace(tzinfo=timezone(timedelta(hours=9))),
                                                local, "timestamp with time zone"))


class FakeAPI:
    def __init__(self):
        self.main_cards = [card(i) for i in range(1000, 1120)]
        self.bsr_cards = [card(i, price=False) for i in range(1050, 1150)]
        self.calls = Counter()
        self.detail_failures = set()
        self.review_failures = set()
        self.repeated_main = False

    def open(self, url):
        pass

    def set_region(self, store):
        pass

    def get(self, path, label, item="", sku="", validator=None, store=""):
        self.calls[(label, item)] += 1
        offset = int(parse_qs(urlsplit(path).query).get("offset", [0])[0])
        if label == "main":
            actual = 0 if self.repeated_main else offset
            payload = {"itemList": self.main_cards[actual:actual + 24], "productCount": len(self.main_cards),
                       "adjustedNextOffset": offset + 24}
        elif label == "bsr":
            payload = {"productListCommonNormalizedPageSpecificProducts": {
                "productList": {"products": self.bsr_cards[offset:offset + 24]}}}
        elif label == "productdetail":
            if item in self.detail_failures:
                return APIResult(status=403, attempts=3, reason="http_error", store=store)
            if item == "1060":
                payload = detail(item, [{"key": "Dryer Load Type", "value": "Front load"},
                                        {"key": "Washer Dryer Capacity (Cu. Feet)", "value": "4.8"}],
                                 name="All-In-One Washer Dryer Combo")
            else:
                payload = detail(item)
        elif label == "reviews":
            if item in self.review_failures:
                return APIResult(status="network", attempts=3, reason="network_error")
            payload = {"reviewStatistics": {"averageOverallRating": 4.35, "totalReviewCount": 10}}
        else:
            raise AssertionError("unexpected API kind")
        if validator:
            validator(payload)
        return APIResult(payload, 200, 1, "", store)


class CollectionTests(unittest.TestCase):
    def logger(self):
        logger = logging.getLogger("lowes.dryer.offline-test")
        logger.addHandler(logging.NullHandler())
        return logger

    def collect(self, api, root):
        return run_collection(api, settings(), self.logger(), root, "l_test",
                              "2026-10-14 10:00:00", operations())

    def test_full_100_100_union_one_detail_target_and_audit_checker(self):
        api = FakeAPI()
        with scratch() as root:
            rows, summary = self.collect(api, root)
            self.assertEqual(summary["counts"]["main"], 100)
            self.assertEqual(summary["counts"]["bsr"], 100)
            self.assertEqual(summary["counts"]["shared"], 50)
            self.assertEqual(len(rows), 150)
            self.assertEqual(api.calls[("productdetail", "1050")], 1)
            self.assertEqual(summary["collection_status"], "complete")
            by_id = {r["item"]: r for r in rows}
            self.assertIsNone(by_id["1100"]["main_rank"])
            self.assertEqual(by_id["1100"]["bsr_rank"], 51)
            self.assertEqual(by_id["1100"]["final_sku_price"], "$599")
            self.assertIsNone(by_id["1060"]["capacity"])
            self.assertEqual(check_run(root)["counts"]["rows"], 150)

    def test_only_required_api_paths_and_exact20_output_columns(self):
        class RestrictedAPI(FakeAPI):
            def get(self, path, label, item="", sku="", validator=None, store=""):
                parsed = urlsplit(path)
                params = parse_qs(parsed.query)
                if label == "main":
                    if parsed.path != "/search/products" or params.get("searchTerm") != ["dryer"]:
                        raise AssertionError("unexpected main request")
                    if params.get("sortMethod") != ["sortBy_featured"]:
                        raise AssertionError("unexpected main sort")
                elif label == "bsr":
                    if parsed.path != "/best-sellers/node/bspl" or params.get("nValue") != ["4294857980"]:
                        raise AssertionError("unexpected BSR request")
                elif label == "productdetail":
                    if parsed.path != f"/wpd/{item}/productdetail/0289/Guest/10010":
                        raise AssertionError("unexpected detail request")
                elif label == "reviews":
                    if parsed.path != f"/rnr/r/get-by-product/{item}":
                        raise AssertionError("unexpected reviews request")
                else:
                    raise AssertionError("extra API request")
                return super().get(path, label, item, sku, validator, store)

        api = RestrictedAPI()
        with scratch() as root:
            rows, _ = self.collect(api, root)
            self.assertEqual({label for label, _ in api.calls}, {"main", "bsr", "productdetail", "reviews"})
            self.assertEqual(sum(count for (label, _), count in api.calls.items() if label == "productdetail"), len(rows))
            self.assertEqual(sum(count for (label, _), count in api.calls.items() if label == "reviews"), len(rows))
            self.assertTrue(all(tuple(row) == rules.COLUMNS for row in rows))

    def test_failure_report_distinguishes_affected_columns_from_actual_nulls(self):
        api = FakeAPI()
        api.detail_failures.add("1120")
        api.review_failures.add("1120")
        with scratch() as root:
            rows, _ = self.collect(api, root)
            output = next(row for row in rows if row["item"] == "1120")
            failures = json.loads((root / "audit" / "failures.json").read_text(encoding="utf-8"))
            detail_failure = next(f for f in failures if f["item"] == "1120" and f["api"] == "productdetail")
            review_failure = next(f for f in failures if f["item"] == "1120" and f["api"] == "reviews")
            self.assertIn("sku", detail_failure["affected_columns"])
            self.assertNotIn("sku", detail_failure["missing_columns"])
            self.assertEqual(set(detail_failure["missing_columns"]), {
                "loading_type", "capacity", "final_sku_price", "original_sku_price", "savings"})
            self.assertEqual(review_failure["missing_columns"], [])
            self.assertEqual(output["sku"], "MODEL1120")
            self.assertEqual(output["count_of_reviews"], 7)

    def test_detail_failure_retained_null_specs_and_marked_partial(self):
        api = FakeAPI()
        api.detail_failures.add("1120")
        api.review_failures.add("1120")
        with scratch() as root:
            rows, summary = self.collect(api, root)
            failed = next(r for r in rows if r["item"] == "1120")
            self.assertEqual(len(rows), 150)
            self.assertEqual(failed["sku"], "MODEL1120")
            self.assertIsNone(failed["capacity"])
            self.assertEqual(failed["count_of_reviews"], 7)
            self.assertEqual(summary["collection_status"], "partial_failure")
            self.assertEqual(summary["api_failure_products"], 1)
            report = check_run(root)
            self.assertGreater(report["spec_reasons"]["capacity"]["collection_failure"], 0)

    def test_ambiguous_main_detail_cached_and_sponsored_washer_excluded(self):
        api = FakeAPI()
        api.main_cards[0] = card(1000, "Unknown appliance")
        api.main_cards[1] = card(1001, "Top-Load Washer", sponsored=True)
        api.main_cards[2] = card(1002, sponsored=True)
        with scratch() as root:
            rows, summary = self.collect(api, root)
            self.assertEqual(summary["counts"]["main"], 100)
            self.assertNotIn("1001", {r["item"] for r in rows})
            self.assertIn("1002", {r["item"] for r in rows})
            self.assertEqual(api.calls[("productdetail", "1000")], 1)

    def test_late_non_target_skips_reviews_unless_it_is_a_bsr_product(self):
        class LateWasherAPI(FakeAPI):
            washer_id = "1000"
            def get(self, path, label, item="", sku="", validator=None, store=""):
                result = super().get(path, label, item, sku, validator, store)
                if label == "productdetail" and item == self.washer_id and result.ok:
                    result.payload = detail(item, specs=[], name="Top-Load Washer")
                return result

        for item, is_bsr in (("1000", False), ("1050", True)):
            api = LateWasherAPI()
            api.washer_id = item
            with scratch() as root:
                rows, summary = self.collect(api, root)
                self.assertEqual(summary["counts"]["main"], 99)
                self.assertEqual(summary["counts"]["bsr"], 100)
                self.assertEqual(api.calls[("productdetail", item)], 1)
                self.assertEqual(api.calls[("reviews", item)], 1 if is_bsr else 0)
                selected = next((row for row in rows if row["item"] == item), None)
                if is_bsr:
                    self.assertIsNotNone(selected)
                    self.assertIsNone(selected["main_rank"])
                    self.assertEqual(selected["bsr_rank"], 1)
                else:
                    self.assertIsNone(selected)

    def test_real_dryer_with_pedestal_not_excluded_as_accessory(self):
        api = FakeAPI()
        api.main_cards[0] = card(1000, "Electric Dryer with included pedestal")
        with scratch() as root:
            rows, summary = self.collect(api, root)
            self.assertEqual(summary["counts"]["main"], 100)
            self.assertIn("1000", {r["item"] for r in rows})
            self.assertEqual(api.calls[("productdetail", "1000")], 1)

    def test_collection_group_skipped_before_numeric_product_id_check(self):
        api = FakeAPI()
        api.main_cards[0]["product"].update(omniItemId="GR_COLLECTION", pdURL="/collections/dryer-set")
        with scratch() as root:
            rows, summary = self.collect(api, root)
            self.assertEqual(summary["counts"]["main"], 100)
            self.assertEqual(summary["main_collection_cards"], 1)
            self.assertNotIn("1000", {r["item"] for r in rows})

    def test_unselected_main_tail_does_not_trigger_detail_request(self):
        api = FakeAPI()
        api.main_cards[110] = card(1599, "Unknown appliance")
        with scratch() as root:
            rows, _ = self.collect(api, root)
            self.assertNotIn("1599", {r["item"] for r in rows})
            self.assertEqual(api.calls[("productdetail", "1599")], 0)

    def test_repeated_page_is_failure_not_complete(self):
        api = FakeAPI()
        api.repeated_main = True
        with scratch() as root:
            with self.assertRaisesRegex(CollectionError, "main_repeated_page"):
                self.collect(api, root)
            self.assertFalse((root / "output" / "final_output.csv").exists())

    def test_browser_retry_and_safe_error_projection(self):
        logger = self.logger()
        api = BrowserAPI(settings(), logger)
        api.driver = MagicMock()
        api.driver.execute_async_script.side_effect = [
            {"status": 429, "reason": "http_error"}, {"status": 200, "payload": {"itemList": []}}]
        result = api.get("/search/products", "main", validator=rules.listing_page)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.summary()["retries"], 1)
        self.assertNotIn("payload", result.summary())

    def test_calendar_week_uses_crawl_datetime_across_year_boundary(self):
        with scratch() as root:
            rows, _ = run_collection(FakeAPI(), settings(), self.logger(), root, "l_test",
                                     "2025-12-31 23:59:00", operations())
            self.assertEqual({r["calendar_week"] for r in rows}, {"w01"})

    def test_browser_exception_message_not_exposed(self):
        api = BrowserAPI(settings(), self.logger())
        api.driver = MagicMock()
        api.driver.execute_async_script.side_effect = RuntimeError("PRIVATE_ERROR_PAYLOAD")
        result = api.get("/search/products", "main", validator=rules.listing_page)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "RuntimeError")
        self.assertNotIn("PRIVATE_ERROR_PAYLOAD", json.dumps(result.summary()))

    def test_alternate_store_recovery_restores_primary_context(self):
        api = MagicMock()
        api.active_store = "1674"
        def set_region(store, alternate=False):
            api.active_store = str(store)
        api.set_region.side_effect = set_region
        api.get.side_effect = [APIResult(status=403, attempts=3, reason="http_error", store="289"),
                               APIResult(detail(1000), 200, 1, store="1854")]
        s = rules.Settings(page_wait=0, seed_wait=0, retry_wait=0)
        result, recovered = fetch_detail(api, s, rules.parse_card(card(1000), 1, 1, 1), self.logger())
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 4)
        self.assertEqual(result.store, "1854")
        self.assertTrue(recovered[0]["recovered"])
        self.assertEqual(api.active_store, "1674")
        self.assertIn("/1854/Guest/10010", api.get.call_args.args[0])

    def test_duplicate_specs_do_not_inflate_product_summary_counts(self):
        class DuplicateSpecAPI(FakeAPI):
            def get(self, *args, **kwargs):
                result = super().get(*args, **kwargs)
                if args[1] == "productdetail" and result.ok:
                    product = next(iter(result.payload["productDetails"].values()))["product"]
                    product["specs"] = product["specs"] * 2
                return result
        with scratch() as root:
            _, summary = self.collect(DuplicateSpecAPI(), root)
            self.assertEqual(sum(summary["kind_counts"].values()), 150)
            self.assertEqual(sum(summary["spec_reasons"]["capacity"].values()), 150)
            self.assertEqual(check_run(root)["spec_reasons"], summary["spec_reasons"])

    def test_bsr_extra_page_rows_are_cut_off_at_displayed100(self):
        class ExtraRowsAPI(FakeAPI):
            def get(self, path, label, *args, **kwargs):
                if label == "bsr":
                    self.calls[(label, "")] += 1
                    return APIResult({"itemList": [card(i) for i in range(1050, 1170)]}, 200, 1)
                return super().get(path, label, *args, **kwargs)
        with scratch() as root:
            api = ExtraRowsAPI()
            rows, summary = self.collect(api, root)
            self.assertEqual(summary["bsr_displayed"], 100)
            self.assertEqual(api.calls[("bsr", "")], 1)
            self.assertNotIn("1150", {r["item"] for r in rows})

    def test_setup_failure_is_logged_without_raw_exception(self):
        from types import SimpleNamespace
        import sys
        def fail(*args):
            raise RuntimeError("PRIVATE_SETUP_PAYLOAD")
        with scratch() as root:
            try:
                with patch.dict(sys.modules, {"lowes.step00_config": SimpleNamespace(load_env=fail)}):
                    self.assertEqual(collector_main(["--run-root", str(root)]), 1)
                text = (root / "logs" / "dryer.log").read_text(encoding="utf-8")
                self.assertIn("RuntimeError", text)
                self.assertNotIn("PRIVATE_SETUP_PAYLOAD", text)
                manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["failed_stage"], "settings")
            finally:
                logger = logging.getLogger("lowes.dryer")
                for handler in logger.handlers[:]:
                    handler.close()
                    logger.removeHandler(handler)


class ValueAuditTests(unittest.TestCase):
    def collect(self, api, root):
        return CollectionTests().collect(api, root)

    def records(self, root):
        return value_audit.read_records(root / "audit" / "value_audit.jsonl")

    def test_source_priority_shared_capacity_and_full20_csv_match(self):
        with scratch() as root:
            rows, _ = self.collect(FakeAPI(), root)
            records = self.records(root)
            self.assertEqual(len(records), 150)
            self.assertTrue(all(record["status"] == "final" for record in records))
            by_id = {record["item"]: record for record in records}
            common = by_id["1050"]
            self.assertEqual(common["source_values"]["listing"]["price"]["sellingPrice"], 499)
            self.assertEqual(common["source_values"]["productdetail"]["price"]["sellingPrice"], 599)
            self.assertEqual(common["fields"]["final_sku_price"]["source"], "listing.price.sellingPrice")
            self.assertEqual(common["fields"]["count_of_reviews"]["raw_value"], 10)
            self.assertEqual(common["fields"]["count_of_reviews"]["source"], "reviews.totalReviewCount")
            self.assertEqual(common["fields"]["country"]["kind"], "fixed")
            self.assertEqual(common["fields"]["id"]["null_reason"], "generated_on_insert")
            only_bsr = by_id["1120"]
            self.assertEqual(only_bsr["fields"]["final_sku_price"]["raw_value"], 599)
            self.assertEqual(only_bsr["fields"]["final_sku_price"]["source"], "productdetail.price.sellingPrice")
            self.assertEqual(only_bsr["fields"]["main_rank"]["null_reason"], "not_in_main")
            self.assertEqual(by_id["1060"]["fields"]["capacity"]["null_reason"], "shared_capacity_only")
            self.assertEqual(by_id["1060"]["fields"]["capacity"]["raw_value"][0]["value"], "4.8")
            self.assertEqual(value_audit.compare_csv(records, rows)["compared_columns"], list(rules.COLUMNS))
            self.assertTrue(check_run(root)["value_audit"]["audit_csv_match"])

    def test_zero_invalid_absent_failure_and_model_conflict_are_distinct(self):
        class SpecialAPI(FakeAPI):
            def get(self, path, label, item="", *args, **kwargs):
                result = super().get(path, label, item, *args, **kwargs)
                if label == "reviews" and item == "1001":
                    result.payload = {"reviewStatistics": {"averageOverallRating": 0, "totalReviewCount": 0}}
                if label == "reviews" and item == "1002":
                    result.payload = {"totalResults": 0}
                if label == "productdetail" and item == "1003":
                    result.payload = detail(item, specs=[])
                if label == "productdetail" and item == "1004":
                    result.payload = detail(item, model="OTHER")
                return result
        api = SpecialAPI()
        api.main_cards[0]["product"].update(rating=6, reviewCount=1.5)
        api.main_cards[0]["location"]["price"].update(sellingPrice=-5, wasPrice=-8, totalSaving=-3)
        api.main_cards[5]["product"]["description"] = None
        api.detail_failures.add("1000")
        api.review_failures.add("1000")
        with scratch() as root:
            self.collect(api, root)
            by_id = {record["item"]: record for record in self.records(root)}
            bad = by_id["1000"]
            self.assertEqual(bad["source_values"]["listing"]["rating"], 6)
            self.assertEqual(bad["fields"]["star_rating"]["null_reason"], "invalid_value_and_collection_failure")
            self.assertEqual(bad["api"]["productdetail"]["status"], 403)
            self.assertEqual(bad["api"]["productdetail"]["retries"], 2)
            self.assertEqual(bad["fields"]["capacity"]["null_reason"], "collection_failure")
            zero = by_id["1001"]
            self.assertEqual(zero["final_values"]["count_of_reviews"], 0)
            self.assertIsNone(zero["fields"]["count_of_reviews"]["null_reason"])
            self.assertEqual(by_id["1002"]["fields"]["star_rating"]["source"], "reviews.totalResults")
            self.assertEqual(by_id["1003"]["fields"]["capacity"]["null_reason"], "absent")
            self.assertEqual(by_id["1004"]["final_values"]["sku"], "MODEL1004")
            self.assertEqual(by_id["1004"]["source_values"]["productdetail"]["modelId"], "OTHER")
            self.assertEqual(by_id["1005"]["fields"]["retailer_sku_name"]["source"]["description"], "derived_listing.retailer_sku_name")
            self.assertEqual(by_id["1005"]["fields"]["retailer_sku_name"]["raw_value"]["name"], "Brand")
            self.assertTrue(check_run(root)["value_audit"]["audit_csv_match"])

    def test_equal_original_price_has_policy_null_reason(self):
        api = FakeAPI()
        api.main_cards[0]["location"]["price"]["wasPrice"] = 499
        with scratch() as root:
            self.collect(api, root)
            first = next(record for record in self.records(root) if record["item"] == "1000")
            self.assertIsNone(first["final_values"]["original_sku_price"])
            self.assertEqual(first["fields"]["original_sku_price"]["raw_value"], 499)
            self.assertEqual(first["fields"]["savings"]["null_reason"], "original_equals_selling")

    def test_unknown_payload_fields_and_review_bodies_are_not_saved(self):
        class PrivateExtraAPI(FakeAPI):
            def get(self, *args, **kwargs):
                result = super().get(*args, **kwargs)
                if result.ok:
                    result.payload["private_extra"] = {"cookie": "PRIVATE_DO_NOT_COPY"}
                    if args[1] == "productdetail":
                        product = next(iter(result.payload["productDetails"].values()))["product"]
                        product["credentials"] = "PRIVATE_DO_NOT_COPY"
                        product["specs"].append({"key": "Cookie", "value": "PRIVATE_DO_NOT_COPY"})
                    if args[1] == "reviews":
                        result.payload["results"] = [{"reviewText": "PRIVATE_DO_NOT_COPY"}]
                return result
        api = PrivateExtraAPI()
        api.main_cards[0]["location"]["price"]["sellingPrice"] = {"auth": "PRIVATE_DO_NOT_COPY"}
        with scratch() as root:
            self.collect(api, root)
            text = (root / "audit" / "value_audit.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_DO_NOT_COPY", text)
            self.assertNotIn("private_extra", text)
            self.assertNotIn("reviewText", text)
            first = self.records(root)[0]
            self.assertEqual(first["source_values"]["listing"]["price"]["sellingPrice"], {"invalid_type": "object"})
            self.assertEqual(first["fields"]["final_sku_price"]["source"], "productdetail.price.sellingPrice")

    def test_each20_csv_column_mismatch_and_duplicate_is_identified(self):
        with scratch() as root:
            rows, _ = self.collect(FakeAPI(), root)
            records = self.records(root)
            for column in rules.COLUMNS:
                altered = [dict(row) for row in rows]
                altered[0][column] = "9999" if column == "item" else "DIFFERENT"
                with self.assertRaises(value_audit.DryerAuditError) as failure:
                    value_audit.compare_csv(records, altered)
                self.assertTrue(any(d["column"] == column for d in failure.exception.differences), column)
            with self.assertRaises(value_audit.DryerAuditError) as failure:
                value_audit.compare_csv(records, rows + [rows[0]])
            self.assertEqual(failure.exception.differences[0]["reason"], "duplicate_csv_product")

    def test_cli_reports_product_column_expected_and_actual(self):
        from lowes.lowes_dryer import write_csv
        with scratch() as root:
            rows, _ = self.collect(FakeAPI(), root)
            rows[0]["final_sku_price"] = "$500"
            write_csv(root / "output" / "final_output.csv", rows, rules.COLUMNS)
            output = io.StringIO()
            with redirect_stdout(output):
                code = check_main(["--run-root", str(root)])
            self.assertEqual(code, 1)
            difference = json.loads(output.getvalue())["differences"][0]
            self.assertEqual(difference, {"item": "1000", "column": "final_sku_price", "expected": "$499",
                                          "actual": "$500", "reason": "audit_vs_csv"})

    def test_interruption_keeps_completed_and_inflight_detail_checkpoints(self):
        class InterruptedAPI(FakeAPI):
            interrupt_label = "reviews"
            interrupt_item = "1000"
            def get(self, path, label, item="", *args, **kwargs):
                if label == self.interrupt_label and item == self.interrupt_item:
                    raise KeyboardInterrupt()
                return super().get(path, label, item, *args, **kwargs)
        for label, item, status in (("reviews", "1000", "detail_received"),
                                    ("productdetail", "1001", "provisional")):
            api = InterruptedAPI()
            api.interrupt_label, api.interrupt_item = label, item
            with scratch() as root:
                with self.assertRaises(KeyboardInterrupt):
                    self.collect(api, root)
                records = self.records(root)
                self.assertEqual(records[0]["status"], status)
                self.assertEqual(records[0]["source_values"]["productdetail"]["price"]["sellingPrice"], 599)
                self.assertFalse((root / "output" / "final_output.csv").exists())
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(check_main(["--run-root", str(root), "--audit-only"]), 0)
                self.assertFalse(json.loads(output.getvalue())["final_values_complete"])

    def test_interrupted_listing_phase_keeps_classification_response(self):
        class InterruptedBSR(FakeAPI):
            def get(self, path, label, *args, **kwargs):
                if label == "bsr":
                    raise KeyboardInterrupt()
                return super().get(path, label, *args, **kwargs)
        api = InterruptedBSR()
        api.main_cards[0]["product"]["description"] = "Unknown appliance"
        with scratch() as root:
            with self.assertRaises(KeyboardInterrupt):
                self.collect(api, root)
            records = self.records(root)
            self.assertEqual(records[0]["status"], "pending_selection")
            self.assertEqual(records[0]["api"]["productdetail"]["status"], 200)
            self.assertIsNone(records[0]["final_values"])
            self.assertEqual(len(records), 100)
            self.assertEqual(records[1]["source_values"]["listing"]["price"]["sellingPrice"], 499)

    def test_malformed_main_card_preserves_preceding_valid_listings(self):
        api = FakeAPI()
        api.main_cards[5]["product"]["omniItemId"] = None
        with scratch() as root:
            with self.assertRaises(CollectionError):
                self.collect(api, root)
            records = self.records(root)
            self.assertEqual(len(records), 5)
            self.assertEqual(records[0]["source_values"]["listing"]["price"]["sellingPrice"], 499)
            self.assertEqual(records[0]["api"]["listing"]["status"], 200)
            self.assertEqual(records[0]["api"]["productdetail"]["status"], "not_requested")

    def test_normalization_failure_preserves_already_received_review_statistics(self):
        with scratch() as root:
            with patch("lowes.lowes_dryer.enrich_row", side_effect=RuntimeError("synthetic_normalization_failure")):
                with self.assertRaises(RuntimeError):
                    self.collect(FakeAPI(), root)
            first = self.records(root)[0]
            self.assertEqual(first["status"], "detail_received")
            self.assertEqual(first["source_values"]["reviews"]["totalReviewCount"], 10)
            self.assertIsNone(first["final_values"])

    def test_atomic_snapshot_failure_keeps_previous_valid_file(self):
        with scratch() as root:
            path = root / "audit" / "value_audit.jsonl"
            journal = value_audit.AuditJournal(path)
            first = rules.parse_card(card(1000), 1, 1, 1)
            journal.upsert(value_audit.source_record(first, APIResult(detail(1000), 200, 1), None, "l_test"))
            previous = path.read_text(encoding="utf-8")
            second = rules.parse_card(card(1001), 1, 2, 2)
            with patch("lowes.step00_dryer_audit.os.replace", side_effect=OSError("synthetic_write_failure")):
                with self.assertRaises(OSError):
                    journal.upsert(value_audit.source_record(second, APIResult(detail(1001), 200, 1), None, "l_test"))
            self.assertEqual(path.read_text(encoding="utf-8"), previous)
            self.assertEqual(self.records(root)[0]["item"], "1000")
            self.assertEqual(list(path.parent.glob(".value_audit_*.tmp")), [])

    def test_late_exclusion_updates_final_ranks_without_duplicate_lines(self):
        class NonTargetAPI(FakeAPI):
            def get(self, path, label, item="", *args, **kwargs):
                result = super().get(path, label, item, *args, **kwargs)
                if label == "productdetail" and item == "1000":
                    result.payload = detail(item, specs=[], name="Top-Load Washer")
                return result
        with scratch() as root:
            rows, _ = self.collect(NonTargetAPI(), root)
            records = self.records(root)
            by_id = {record["item"]: record for record in records}
            self.assertEqual(len(records), 150)
            self.assertEqual(by_id["1000"]["status"], "excluded")
            self.assertEqual(by_id["1001"]["final_values"]["main_rank"], 1)
            self.assertEqual(by_id["1001"]["fields"]["main_rank"]["raw_value"], 2)
            self.assertTrue(value_audit.compare_csv(records, rows)["audit_csv_match"])

    def test_bad_json_duplicate_products_and_unfinished_records_are_rejected(self):
        with scratch() as root:
            self.collect(FakeAPI(), root)
            path = root / "audit" / "value_audit.jsonl"
            text = path.read_text(encoding="utf-8")
            path.write_text(text + text.splitlines()[0] + "\n", encoding="utf-8")
            with self.assertRaises(value_audit.DryerAuditError):
                self.records(root)
            path.write_text('{"schema_version":\n', encoding="utf-8")
            with self.assertRaises(value_audit.DryerAuditError):
                self.records(root)


if __name__ == "__main__":
    unittest.main()
