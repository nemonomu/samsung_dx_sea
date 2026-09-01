import copy
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ["BESTBUY_CATEGORY"] = "TV"
os.environ["BESTBUY_URL_SOURCE"] = "default"

from bestbuy.bestbuy_orchestrator import apply_run_path_env
from bestbuy.sos_refill import (
    apply_prepared_promotion_updates,
    backup_promotion_recovery_inputs,
    build_promotion_overlay_rows,
    preserve_existing_artifacts_and_append_new_rows,
    prepare_promotion_artifact_updates,
    rows_for_skus,
    step_by_name,
    step_env,
    validate_new_promotion_rows,
    validate_promotion_recovery,
)
from bestbuy.step00_config import (
    BESTBUY_URLS,
    PROMOTION_LABELS,
    PROMOTION_TV_EXPECTED_MIN_ROWS,
    PROMOTION_TV_HOME_THEATER_URL,
    PROMOTION_TV_PLACEMENT_ID,
)
from bestbuy.step05_promotion_deals import (
    PROMOTION_DOM_TYPE,
    parse_dom_items,
    validate_browser_dom_payload,
)
from bestbuy.step14_db_load import (
    normalize_value,
    promotion_new_rows,
    product_list_promotion_update_candidates,
    promotion_update_candidates,
    upsert_promotion_new_rows,
    update_product_list_promotion_only,
    update_promotion_only,
)
from bestbuy.step16_email_notify import listing_count_issues
import bestbuy.step14_db_load as db_load_module


DYNAMIC_PROMOTION_TYPE = "Go all in on big-screen action"


class FakeCursor:
    def __init__(self, columns=None, rowcounts=None):
        self.rowcount = 0
        self.updates = []
        self._columns = columns or [
            ("batch_id", "text"),
            ("item", "text"),
            ("page_type", "text"),
            ("promotion_type", "text"),
            ("promotion_position", "integer"),
        ]
        self._rowcounts = iter(rowcounts or [])

    def execute(self, sql, params):
        if "information_schema.columns" in sql:
            self.rowcount = 0
            return
        self.updates.append((sql, params))
        self.rowcount = next(self._rowcounts, 1)

    def fetchall(self):
        return self._columns

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self):
        self.exit_exception = None
        self.closed = False
        self._cursor = FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exit_exception = exc_type
        return False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class PromotionRecoveryTests(unittest.TestCase):
    def test_verified_url_placement_and_dynamic_count(self):
        self.assertEqual(
            PROMOTION_TV_HOME_THEATER_URL,
            "https://www.bestbuy.com/site/all-electronics-on-sale/all-tv-home-theater-on-sale/"
            "pcmcat1690836748285.c?id=pcmcat1690836748285",
        )
        self.assertEqual(BESTBUY_URLS["promotion_tv_home_theater"], PROMOTION_TV_HOME_THEATER_URL)
        self.assertEqual(PROMOTION_TV_PLACEMENT_ID, "pcmcat1690836748285")
        self.assertEqual(PROMOTION_TV_EXPECTED_MIN_ROWS, 0)
        self.assertEqual(PROMOTION_LABELS["pcmcat1690836748285-1"], "TV promotion")
        self.assertEqual(PROMOTION_DOM_TYPE, "")
        config_source = Path(__file__).parents[1] / "bestbuy" / "step00_config.py"
        self.assertNotIn("pcmcat1720647543741", config_source.read_text(encoding="utf-8"))

    def test_parse_dom_items_dedupes_and_preserves_label(self):
        url = "https://www.bestbuy.com/product/sample-tv/sample-tv/sku/6614066"
        rows = parse_dom_items(
            [
                {"href": url, "linkText": "Sample TV with a sufficiently long name"},
                {"href": url, "imageAlt": "Sample TV duplicate product link"},
            ],
            promotion_type=DYNAMIC_PROMOTION_TYPE,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["promotion_position"], 1)
        self.assertEqual(rows[0]["promotion_type"], DYNAMIC_PROMOTION_TYPE)

    def test_overlay_updates_all_existing_overlaps_and_preserves_page_type(self):
        final_rows = [
            {
                "batch_id": "b_test",
                "page_type": "main",
                "sku_id": "100",
                "item": "main-a",
                "retailer_sku_name": "Main product A",
                "final_sku_price": "999.99",
                "promotion_type": "",
                "promotion_position": "",
            },
            {
                "batch_id": "b_test",
                "page_type": "main",
                "sku_id": "200",
                "item": "main-b",
                "retailer_sku_name": "Main product B",
                "final_sku_price": "499.99",
                "promotion_type": "",
                "promotion_position": "",
            },
            {
                "batch_id": "b_test",
                "page_type": "bsr",
                "sku_id": "300",
                "item": "bsr-c",
                "retailer_sku_name": "BSR product C",
                "promotion_type": "",
                "promotion_position": "",
            },
        ]
        target_rows = [
            {"target_source": "main", "sku_id": "100", "bsin": "main-a"},
            {"target_source": "main", "sku_id": "200", "bsin": "main-b"},
            {"target_source": "bsr_only_backfill", "sku_id": "300", "bsin": "bsr-c"},
        ]
        promotion_rows = [
            {
                "sku_id": "100",
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
                "promotion_position": "7",
                "retailer_sku_name": "Wrong promotion-side name",
                "product_url": "https://www.bestbuy.com/product/wrong/wrong-item/sku/100",
            },
            {
                "sku_id": "300",
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
                "promotion_position": "8",
            },
            {
                "sku_id": "400",
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
                "promotion_position": "9",
            },
        ]

        overlay, stats = build_promotion_overlay_rows(
            promotion_rows,
            final_rows,
            target_rows,
            "b_test",
        )
        self.assertEqual(len(overlay), 2)
        self.assertEqual([row["sku_id"] for row in overlay], ["100", "300"])
        self.assertEqual(overlay[0]["item"], "main-a")
        self.assertEqual(overlay[0]["page_type"], "main")
        self.assertEqual(overlay[1]["item"], "bsr-c")
        self.assertEqual(overlay[1]["page_type"], "bsr")
        self.assertEqual(overlay[0]["promotion_type"], DYNAMIC_PROMOTION_TYPE)
        self.assertEqual(overlay[0]["promotion_position"], "7")
        self.assertEqual(stats["matched_existing_skus"], 2)
        self.assertEqual(stats["unmatched_promotion_skus"], ["400"])

        run_root = Path("C:/bestbuy/test-promotion-recovery")
        before = copy.deepcopy(final_rows)

        def fake_read_csv_table(path):
            if Path(path).name == "final_output.csv":
                return copy.deepcopy(final_rows), list(final_rows[0])
            return [], []

        with patch("bestbuy.sos_refill.read_csv_table", side_effect=fake_read_csv_table):
            plans = prepare_promotion_artifact_updates(run_root, overlay, "b_test")
        final_plan = next(plan for plan in plans if plan["label"] == "final_output")
        after = final_plan["rows"]
        self.assertEqual(len(after), len(before))
        for index, (old, new) in enumerate(zip(before, after)):
            if index in {0, 2}:
                self.assertEqual(new["promotion_type"], DYNAMIC_PROMOTION_TYPE)
                self.assertEqual(new["promotion_position"], "7" if index == 0 else "8")
                for field in set(old) - {"promotion_type", "promotion_position"}:
                    self.assertEqual(new[field], old[field])
            else:
                self.assertEqual(new, old)

    def test_new_pipeline_output_keeps_existing_rows_exact_and_appends_only_new_skus(self):
        plan = {
            "label": "final_output",
            "path": Path("C:/bestbuy/final_output.csv"),
            "original_rows": [
                {
                    "sku_id": "100",
                    "batch_id": "b_test",
                    "page_type": "main",
                    "retailer_sku_name": "Original exact value",
                    "promotion_type": "",
                }
            ],
            "original_fieldnames": [
                "sku_id",
                "batch_id",
                "page_type",
                "retailer_sku_name",
                "promotion_type",
            ],
        }
        rebuilt_by_pipeline = [
            {
                "sku_id": "100",
                "batch_id": "b_test",
                "page_type": "main",
                "retailer_sku_name": "Unexpected rebuilt value",
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
            },
            {
                "sku_id": "400",
                "batch_id": "b_test",
                "page_type": "promotion",
                "retailer_sku_name": "Promotion-only product",
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
            },
        ]
        writes = []
        with patch(
            "bestbuy.sos_refill.read_csv_table",
            return_value=(rebuilt_by_pipeline, list(rebuilt_by_pipeline[0])),
        ), patch(
            "bestbuy.sos_refill.write_csv_rows_atomic",
            side_effect=lambda path, rows, fields: writes.append((path, copy.deepcopy(rows), list(fields))),
        ):
            result = preserve_existing_artifacts_and_append_new_rows([plan], ["400"])

        self.assertEqual(result[0]["existing_rows_preserved"], 1)
        self.assertEqual(result[0]["new_rows_appended"], 1)
        self.assertEqual(writes[0][1][0], plan["original_rows"][0])
        self.assertEqual(writes[0][1][1], rebuilt_by_pipeline[1])

    def test_final_output_without_sku_id_is_scoped_by_product_url(self):
        rows = [
            {
                "item": "promotion-only",
                "product_url": "https://www.bestbuy.com/product/promotion-only/ABC123/sku/6673119?intl=nosplash",
                "batch_id": "b_test",
                "page_type": "promotion",
            },
            {
                "item": "existing",
                "product_url": "https://www.bestbuy.com/product/existing/XYZ789/sku/6000000",
                "batch_id": "b_test",
                "page_type": "main",
            },
        ]

        selected = rows_for_skus(rows, ["6673119"])

        self.assertEqual(selected, [rows[0]])
        validate_new_promotion_rows(selected, ["6673119"], "b_test", "final_output")

    def test_review20_keeps_new_promotion_sku_scope(self):
        env = step_env(
            step_by_name("review20"),
            "TV",
            {
                "BESTBUY_BATCH_ID": "b_test",
                "BESTBUY_DETAIL_SKUS": "6673119,6673143",
            },
        )

        self.assertEqual(env["BESTBUY_DETAIL_SKUS"], "6673119,6673143")

    def test_promotion_backup_only_copies_published_artifacts_not_browser_profile(self):
        run_root = Path("C:/bestbuy/run")
        recovery_root = run_root / "promotion_recovery" / "test"
        promotion_root = run_root / "promotion"
        summary_path = promotion_root / "summary.json"
        parsed_path = promotion_root / "parsed"
        browser_dom_path = promotion_root / "raw" / "browser_dom"
        target_manifest_path = run_root / "output" / "bestbuy_final_targets.manifest.json"
        existing = {
            str(summary_path),
            str(parsed_path),
            str(browser_dom_path),
            str(target_manifest_path),
        }
        with patch.object(Path, "mkdir"), patch.object(
            Path,
            "exists",
            autospec=True,
            side_effect=lambda path: str(path) in existing,
        ), patch.object(
            Path,
            "is_dir",
            autospec=True,
            side_effect=lambda path: str(path) in {str(parsed_path), str(browser_dom_path)},
        ), patch.object(Path, "write_text"), patch(
            "bestbuy.sos_refill.shutil.copytree"
        ) as copytree, patch(
            "bestbuy.sos_refill.shutil.copy2"
        ) as copy2:
            backup_promotion_recovery_inputs(run_root, recovery_root, [])

        copied_tree_sources = {str(item.args[0]) for item in copytree.call_args_list}
        copied_file_sources = {str(item.args[0]) for item in copy2.call_args_list}
        self.assertEqual(copied_tree_sources, {str(parsed_path), str(browser_dom_path)})
        self.assertEqual(copied_file_sources, {str(summary_path), str(target_manifest_path)})
        self.assertFalse(any("promotion_dom_profile" in value for value in copied_tree_sources))

    def test_overlay_rejects_duplicate_db_item_identity(self):
        final_rows = [
            {
                "batch_id": "b_test",
                "page_type": "main",
                "sku_id": "100",
                "item": "same-item",
            },
            {
                "batch_id": "b_test",
                "page_type": "main",
                "sku_id": "200",
                "item": "same-item",
            },
        ]
        promotion_rows = [
            {
                "sku_id": "100",
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
                "promotion_position": "1",
            }
        ]
        with self.assertRaises(RuntimeError):
            build_promotion_overlay_rows(promotion_rows, final_rows, [], "b_test")

    def test_artifact_patch_does_not_item_fallback_when_sku_disagrees(self):
        overlay = [
            {
                "batch_id": "b_test",
                "page_type": "main",
                "sku_id": "100",
                "item": "same-item",
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
                "promotion_position": "1",
            }
        ]
        final_rows = [
            {
                "batch_id": "b_test",
                "page_type": "main",
                "sku_id": "999",
                "item": "same-item",
                "promotion_type": "",
                "promotion_position": "",
            }
        ]

        def fake_read_csv_table(path):
            if Path(path).name == "final_output.csv":
                return copy.deepcopy(final_rows), list(final_rows[0])
            return [], []

        with patch("bestbuy.sos_refill.read_csv_table", side_effect=fake_read_csv_table), self.assertRaises(
            RuntimeError
        ):
            prepare_promotion_artifact_updates(
                Path("C:/bestbuy/test-promotion-recovery"),
                overlay,
                "b_test",
            )

    def test_artifact_patch_rolls_back_previous_file_on_later_failure(self):
        plans = [
            {
                "label": "final_output",
                "path": Path("C:/bestbuy/final_output.csv"),
                "rows": [{"promotion_type": DYNAMIC_PROMOTION_TYPE}],
                "fieldnames": ["promotion_type"],
                "original_rows": [{"promotion_type": ""}],
                "original_fieldnames": ["promotion_type"],
                "matched_rows": 1,
                "changed_rows": 1,
            },
            {
                "label": "final_targets",
                "path": Path("C:/bestbuy/final_targets.csv"),
                "rows": [{"promotion_type": DYNAMIC_PROMOTION_TYPE}],
                "fieldnames": ["promotion_type"],
                "original_rows": [{"promotion_type": ""}],
                "original_fieldnames": ["promotion_type"],
                "matched_rows": 1,
                "changed_rows": 1,
            },
        ]
        calls = []

        def fake_atomic(path, rows, fieldnames):
            calls.append((path, copy.deepcopy(rows), list(fieldnames)))
            if path.name == "final_targets.csv":
                raise PermissionError("locked")

        with patch("bestbuy.sos_refill.write_csv_rows_atomic", side_effect=fake_atomic), self.assertRaises(
            PermissionError
        ):
            apply_prepared_promotion_updates(plans)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1][0].name, "final_output.csv")
        self.assertEqual(calls[-1][1], plans[0]["original_rows"])

    def test_db_update_is_existing_page_type_and_batch_scoped_and_two_columns_only(self):
        rows = [
            {
                "batch_id": "b_test",
                "item": "main-a",
                "page_type": "main",
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
                "promotion_position": "7",
            },
            {
                "batch_id": "b_test",
                "item": "promotion-only",
                "page_type": "promotion",
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
                "promotion_position": "8",
            },
            {
                "batch_id": "b_other",
                "item": "other-batch",
                "page_type": "main",
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
                "promotion_position": "9",
            },
        ]
        candidates = promotion_update_candidates(rows, "b_test")
        self.assertEqual([row["item"] for row in candidates], ["main-a", "promotion-only"])

        csv_path = Path("C:/bestbuy/test-promotion-overlay.csv")
        with patch("bestbuy.step14_db_load.read_csv", return_value=copy.deepcopy(rows)), patch(
            "bestbuy.step14_db_load.rel_path", return_value="test-promotion-overlay.csv"
        ):
            cursor = FakeCursor()
            result = update_promotion_only(cursor, csv_path, "tv_table", batch_id="b_test")
        self.assertEqual(result["candidate_rows"], 2)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(len(cursor.updates), 2)
        sql, params = cursor.updates[0]
        self.assertIn("SET promotion_type = %s, promotion_position = %s", sql)
        self.assertIn("WHERE batch_id = %s AND item = %s AND page_type = %s", sql)
        self.assertNotIn("retailer_sku_name", sql)
        self.assertEqual(params, (DYNAMIC_PROMOTION_TYPE, 7, "b_test", "main-a", "main"))
        self.assertEqual(
            cursor.updates[1][1],
            (DYNAMIC_PROMOTION_TYPE, 8, "b_test", "promotion-only", "promotion"),
        )
        self.assertEqual(normalize_value("2 ||| 5", "integer", "promotion_position"), 2)

        missing_page_type = FakeCursor(
            columns=[
                ("batch_id", "text"),
                ("item", "text"),
                ("promotion_type", "text"),
                ("promotion_position", "integer"),
            ]
        )
        with patch("bestbuy.step14_db_load.read_csv", return_value=[rows[0]]), self.assertRaises(RuntimeError):
            update_promotion_only(missing_page_type, csv_path, "tv_table", batch_id="b_test")

    def test_product_list_db_update_uses_sku_and_two_columns_only(self):
        row = {
            "batch_id": "b_test",
            "item": "main-a",
            "sku_id": "100",
            "page_type": "main",
            "promotion_type": DYNAMIC_PROMOTION_TYPE,
            "promotion_position": "2 ||| 5",
        }
        self.assertEqual(product_list_promotion_update_candidates([row], "b_test"), [row])
        cursor = FakeCursor(
            columns=[
                ("batch_id", "text"),
                ("sku_id", "text"),
                ("page_type", "text"),
                ("promotion_type", "text"),
                ("promotion_position", "integer"),
            ]
        )
        with patch("bestbuy.step14_db_load.read_csv", return_value=[row]), patch(
            "bestbuy.step14_db_load.rel_path", return_value="overlay.csv"
        ):
            result = update_product_list_promotion_only(
                cursor,
                Path("C:/bestbuy/overlay.csv"),
                "product_list_table",
                batch_id="b_test",
            )
        self.assertEqual(result["updated"], 1)
        sql, params = cursor.updates[0]
        self.assertIn("SET promotion_type = %s, promotion_position = %s", sql)
        self.assertIn("WHERE batch_id = %s AND sku_id = %s AND page_type = %s", sql)
        self.assertEqual(params, (DYNAMIC_PROMOTION_TYPE, 2, "b_test", "100", "main"))

    def test_db_update_count_mismatch_fails_inside_update(self):
        row = {
            "batch_id": "b_test",
            "item": "main-a",
            "page_type": "main",
            "promotion_type": DYNAMIC_PROMOTION_TYPE,
            "promotion_position": "7",
        }
        cursor = FakeCursor(rowcounts=[0])
        with patch("bestbuy.step14_db_load.read_csv", return_value=[row]), self.assertRaises(RuntimeError):
            update_promotion_only(
                cursor,
                Path("C:/bestbuy/overlay.csv"),
                "tv_table",
                batch_id="b_test",
            )

    def test_manifest_write_failure_occurs_before_db_commit(self):
        connection = FakeConnection()
        psycopg2 = types.ModuleType("psycopg2")
        psycopg2.connect = lambda **kwargs: connection
        with patch.dict(sys.modules, {"psycopg2": psycopg2}), patch.object(
            db_load_module, "db_config", return_value={"host": "x", "database": "x"}
        ), patch.object(db_load_module, "DRY_RUN", False), patch.object(
            db_load_module, "UPDATE_AVAILABILITY_ONLY", False
        ), patch.object(db_load_module, "UPDATE_PROMOTION_ONLY", True), patch.object(
            db_load_module, "UPDATE_SIMILAR_ONLY", False
        ), patch.object(
            db_load_module,
            "update_promotion_only",
            return_value={"candidate_rows": 1, "updated": 1},
        ), patch.object(
            db_load_module,
            "update_product_list_promotion_only",
            return_value={"candidate_rows": 1, "updated": 1},
        ), patch.object(
            db_load_module,
            "write_db_load_manifest",
            side_effect=OSError("manifest write failed"),
        ), self.assertRaises(OSError):
            db_load_module.main()
        self.assertIs(connection.exit_exception, OSError)
        self.assertTrue(connection.closed)

    def test_recovery_validation_rejects_empty_or_wrong_page(self):
        valid_summary = {
            "row_count": 16,
            "summaries": [
                {
                    "container_found": True,
                    "url": PROMOTION_TV_HOME_THEATER_URL,
                    "promotion_type": DYNAMIC_PROMOTION_TYPE,
                    "stable": True,
                    "validation_errors": [],
                    "card_count": 16,
                }
            ],
        }
        rows = [
            {
                "sku_id": str(index),
                "promotion_type": DYNAMIC_PROMOTION_TYPE,
                "promotion_position": str(index),
            }
            for index in range(1, 17)
        ]
        self.assertEqual(validate_promotion_recovery(valid_summary, rows, 16)["unique_skus"], 16)
        with self.assertRaises(RuntimeError):
            validate_promotion_recovery({"summaries": []}, [], 16)
        wrong_page = copy.deepcopy(valid_summary)
        wrong_page["summaries"][0]["url"] = "https://www.bestbuy.com/pcmcat1720647543741"
        with self.assertRaises(RuntimeError):
            validate_promotion_recovery(wrong_page, rows, 16)

    def test_dom_validation_accepts_dynamic_10_or_16_cards_and_rejects_bad_order(self):
        for count in (10, 16):
            items = [
                {
                    "href": f"https://www.bestbuy.com/product/tv-{index}/item-{index}/sku/{6600000 + index}",
                    "position": index,
                    "dataOrder": str(index - 1),
                }
                for index in range(1, count + 1)
            ]
            payload = {
                "containerFound": True,
                "promotionType": DYNAMIC_PROMOTION_TYPE,
                "stable": True,
                "cardCount": count,
                "items": items,
            }
            rows = parse_dom_items(items, promotion_type=DYNAMIC_PROMOTION_TYPE)
            self.assertEqual(validate_browser_dom_payload(payload, rows), [])
            self.assertEqual(len(rows), count)

        bad_order = copy.deepcopy(payload)
        bad_order["items"][-1]["dataOrder"] = str(count)
        self.assertIn(
            "promotion_card_data_order_not_contiguous",
            validate_browser_dom_payload(
                bad_order,
                parse_dom_items(bad_order["items"], promotion_type=DYNAMIC_PROMOTION_TYPE),
            ),
        )
        missing_order = copy.deepcopy(payload)
        missing_order["items"][0].pop("dataOrder")
        self.assertIn(
            "promotion_card_data_order_missing",
            validate_browser_dom_payload(
                missing_order,
                parse_dom_items(missing_order["items"], promotion_type=DYNAMIC_PROMOTION_TYPE),
            ),
        )

    def test_promotion_new_rows_require_same_batch_and_promotion_page_type(self):
        valid = [{"sku_id": "400", "batch_id": "b_test", "page_type": "promotion"}]
        with patch("bestbuy.step14_db_load.read_csv", return_value=valid):
            self.assertEqual(promotion_new_rows("new.csv", batch_id="b_test"), valid)
        with patch(
            "bestbuy.step14_db_load.read_csv",
            return_value=[{"sku_id": "400", "batch_id": "b_other", "page_type": "promotion"}],
        ), self.assertRaises(RuntimeError):
            promotion_new_rows("new.csv", batch_id="b_test")
        with patch(
            "bestbuy.step14_db_load.read_csv",
            return_value=[{"sku_id": "400", "batch_id": "b_test", "page_type": "main"}],
        ), self.assertRaises(RuntimeError):
            promotion_new_rows("new.csv", batch_id="b_test")

    def test_promotion_new_upsert_matches_page_type_and_inserts_exactly_one_row(self):
        row = {
            "batch_id": "b_test",
            "item": "promotion-only",
            "page_type": "promotion",
            "promotion_type": DYNAMIC_PROMOTION_TYPE,
            "promotion_position": "4",
        }
        cursor = FakeCursor(rowcounts=[0, 1])
        with patch("bestbuy.step14_db_load.read_csv", return_value=[row]), patch(
            "bestbuy.step14_db_load.rel_path", return_value="new.csv"
        ):
            result = upsert_promotion_new_rows(
                cursor,
                "new.csv",
                "tv_table",
                batch_id="b_test",
            )
        self.assertEqual(result["candidate_rows"], 1)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["updated"], 0)
        update_sql, update_params = cursor.updates[0]
        self.assertIn(
            'WHERE "batch_id" = %s AND "item" = %s AND "page_type" = %s',
            update_sql,
        )
        self.assertEqual(update_params[-3:], ("b_test", "promotion-only", "promotion"))

    def test_apply_run_path_env_scopes_promotion_to_existing_run(self):
        run_root = Path("C:/bestbuy/test-run")
        env = {"BESTBUY_RUN_ROOT": str(run_root), "BESTBUY_FORCE_RUN_PATH_ENV": "1"}
        apply_run_path_env(env)
        self.assertEqual(Path(env["BESTBUY_PROMOTION_RUN_ROOT"]), run_root / "promotion")
        self.assertEqual(Path(env["BESTBUY_TRENDING_RUN_ROOT"]), run_root / "trending")

    def test_recovery_bat_supports_interactive_latest_run_without_batch_argument(self):
        bat_path = Path(__file__).parents[1] / "run_bestbuy_promotion_recovery.bat"
        raw_source = bat_path.read_bytes()
        self.assertIn(b"\r\n", raw_source)
        self.assertNotIn(b"\n", raw_source.replace(b"\r\n", b""))
        source = bat_path.read_text(encoding="utf-8")
        self.assertIn('set "INTERACTIVE=1"', source)
        self.assertIn('set "ORIGINAL_CODEPAGE="', source)
        self.assertIn('if defined ORIGINAL_CODEPAGE chcp %ORIGINAL_CODEPAGE% >nul', source)
        self.assertLess(source.index('set "RUN_ROOT="'), source.index(':classify_input'))
        self.assertIn('set /p "RUN_INPUT=', source)
        self.assertIn('Enter=가장 최근', source)
        self.assertIn('dir /b /ad /o-n "%~dp0bestbuy\\data\\tv\\20??????*"', source)
        self.assertIn('if not defined RUN_ROOT if exist ', source)
        self.assertIn('if /I "%RUN_INPUT:~0,2%"=="b_"', source)
        self.assertIn('goto :find_by_batch', source)
        self.assertIn('findstr /L /C:"%BATCH_ID%"', source)
        self.assertIn('batch_id=%BATCH_ID%가 있는 final_output.csv', source)
        self.assertIn('choice /C YN', source)
        self.assertIn('if exist "%RUN_INPUT%\\output\\final_output.csv" (', source)
        self.assertIn('set "RUN_ROOT=%RUN_INPUT%"', source)
        self.assertIn('set "RUN_ROOT=%~dp0bestbuy\\data\\tv\\%RUN_INPUT%"', source)
        self.assertIn('if not exist "%RUN_ROOT%\\output\\final_output.csv" (', source)
        self.assertIn('if not defined RUN_ROOT (', source)
        self.assertIn('if errorlevel 2 (', source)
        self.assertIn('if "%BATCH_ID%"=="" (', source)
        self.assertIn('set "BESTBUY_BATCH_ID="', source)
        self.assertEqual(source.count('python -m bestbuy.sos_refill'), 2)
        self.assertEqual(source.count('--promotion-only'), 2)
        self.assertIn('--run-root "%RUN_ROOT%" --promotion-only', source)
        self.assertIn('--batch-id "%BATCH_ID%" --promotion-only', source)
        self.assertNotIn('bestbuy.bestbuy_orchestrator', source)
        self.assertNotIn('--refresh-join-sources', source)
        self.assertIn('set "BESTBUY_DB_UPDATE_SIMILAR_ONLY=0"', source)
        self.assertIn('set "BESTBUY_DB_UPDATE_AVAILABILITY_ONLY=0"', source)
        self.assertIn('set "BESTBUY_DB_UPDATE_PROMOTION_ONLY=0"', source)
        self.assertNotIn('set "BESTBUY_DB_UPDATE_PROMOTION_ONLY=1"', source)
        self.assertIn("page_type=promotion", source)
        self.assertIn("상세/리뷰/재고 수집 후 추가", source)
        validate = source.index('if not exist "%RUN_ROOT%\\output\\final_output.csv"')
        confirm = source.index('choice /C YN')
        execute = source.index('python -m bestbuy.sos_refill')
        self.assertLess(validate, confirm)
        self.assertLess(confirm, execute)
        self.assertLess(execute, source.index('set "EXIT_CODE=%ERRORLEVEL%"'))
        self.assertIn('exit /b %EXIT_CODE%', source)
        self.assertNotIn("--batch-id '%BATCH_ID%'", source)

    def test_email_uses_recovered_csv_count_and_includes_recovery_command(self):
        run_root = Path("C:/bestbuy/run/20260813")
        with patch(
            "bestbuy.step16_email_notify.read_json",
            return_value={"expected_min_rows": 16},
        ), patch("bestbuy.step16_email_notify.unique_csv_count", return_value=16):
            issues = listing_count_issues(
                "TV",
                run_root,
                [],
                {"trending_unique_count": 10, "promotion_unique_count": 5},
            )
        self.assertFalse(any("promotion listing" in issue for issue in issues))

        with patch(
            "bestbuy.step16_email_notify.read_json",
            return_value={"expected_min_rows": 16},
        ), patch("bestbuy.step16_email_notify.unique_csv_count", return_value=0):
            issues = listing_count_issues(
                "TV",
                run_root,
                [],
                {"trending_unique_count": 10, "promotion_unique_count": 0},
            )
        promotion_issue = next(issue for issue in issues if "promotion listing" in issue)
        self.assertIn("promotion listing sku 0/16", promotion_issue)
        self.assertIn("run_bestbuy_promotion_recovery.bat", promotion_issue)


if __name__ == "__main__":
    unittest.main()
