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
    build_promotion_overlay_rows,
    prepare_promotion_artifact_updates,
    validate_promotion_recovery,
)
from bestbuy.step00_config import (
    BESTBUY_URLS,
    PROMOTION_LABELS,
    PROMOTION_TV_EXPECTED_MIN_ROWS,
    PROMOTION_TV_HEADLINE,
    PROMOTION_TV_HOME_THEATER_URL,
)
from bestbuy.step05_promotion_deals import PROMOTION_DOM_TYPE, parse_dom_items
from bestbuy.step14_db_load import (
    normalize_value,
    product_list_promotion_update_candidates,
    promotion_update_candidates,
    update_product_list_promotion_only,
    update_promotion_only,
)
from bestbuy.step16_email_notify import listing_count_issues
import bestbuy.step14_db_load as db_load_module


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
    def test_verified_url_label_and_expected_count(self):
        self.assertEqual(
            PROMOTION_TV_HOME_THEATER_URL,
            "https://www.bestbuy.com/site/all-electronics-on-sale/all-tv-home-theater-on-sale/"
            "pcmcat1690836748285.c?id=pcmcat1690836748285",
        )
        self.assertEqual(BESTBUY_URLS["promotion_tv_home_theater"], PROMOTION_TV_HOME_THEATER_URL)
        self.assertEqual(PROMOTION_TV_EXPECTED_MIN_ROWS, 16)
        self.assertEqual(PROMOTION_TV_HEADLINE, "Don’t-miss deals on TVs")
        self.assertEqual(PROMOTION_LABELS["pcmcat1690836748285-1"], PROMOTION_TV_HEADLINE)
        self.assertEqual(PROMOTION_DOM_TYPE, PROMOTION_TV_HEADLINE)
        config_source = Path(__file__).parents[1] / "bestbuy" / "step00_config.py"
        self.assertNotIn("pcmcat1720647543741", config_source.read_text(encoding="utf-8"))

    def test_parse_dom_items_dedupes_and_preserves_label(self):
        url = "https://www.bestbuy.com/product/sample-tv/sample-tv/sku/6614066"
        rows = parse_dom_items(
            [
                {"href": url, "linkText": "Sample TV with a sufficiently long name"},
                {"href": url, "imageAlt": "Sample TV duplicate product link"},
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["promotion_position"], 1)
        self.assertEqual(rows[0]["promotion_type"], PROMOTION_TV_HEADLINE)

    def test_overlay_updates_only_existing_main_overlap(self):
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
                "promotion_type": PROMOTION_TV_HEADLINE,
                "promotion_position": "7",
                "retailer_sku_name": "Wrong promotion-side name",
                "product_url": "https://www.bestbuy.com/product/wrong/wrong-item/sku/100",
            },
            {
                "sku_id": "300",
                "promotion_type": PROMOTION_TV_HEADLINE,
                "promotion_position": "8",
            },
            {
                "sku_id": "400",
                "promotion_type": PROMOTION_TV_HEADLINE,
                "promotion_position": "9",
            },
        ]

        overlay, stats = build_promotion_overlay_rows(
            promotion_rows,
            final_rows,
            target_rows,
            "b_test",
        )
        self.assertEqual(len(overlay), 1)
        self.assertEqual(overlay[0]["sku_id"], "100")
        self.assertEqual(overlay[0]["item"], "main-a")
        self.assertEqual(overlay[0]["promotion_type"], PROMOTION_TV_HEADLINE)
        self.assertEqual(overlay[0]["promotion_position"], "7")
        self.assertEqual(stats["matched_main_skus"], 1)
        self.assertEqual(stats["unmatched_promotion_skus"], ["300", "400"])

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
            if index == 0:
                self.assertEqual(new["promotion_type"], PROMOTION_TV_HEADLINE)
                self.assertEqual(new["promotion_position"], "7")
                for field in set(old) - {"promotion_type", "promotion_position"}:
                    self.assertEqual(new[field], old[field])
            else:
                self.assertEqual(new, old)

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
                "promotion_type": PROMOTION_TV_HEADLINE,
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
                "promotion_type": PROMOTION_TV_HEADLINE,
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
                "rows": [{"promotion_type": PROMOTION_TV_HEADLINE}],
                "fieldnames": ["promotion_type"],
                "original_rows": [{"promotion_type": ""}],
                "original_fieldnames": ["promotion_type"],
                "matched_rows": 1,
                "changed_rows": 1,
            },
            {
                "label": "final_targets",
                "path": Path("C:/bestbuy/final_targets.csv"),
                "rows": [{"promotion_type": PROMOTION_TV_HEADLINE}],
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

    def test_db_update_is_main_batch_scoped_and_two_columns_only(self):
        rows = [
            {
                "batch_id": "b_test",
                "item": "main-a",
                "page_type": "main",
                "promotion_type": PROMOTION_TV_HEADLINE,
                "promotion_position": "7",
            },
            {
                "batch_id": "b_test",
                "item": "promotion-only",
                "page_type": "promotion",
                "promotion_type": PROMOTION_TV_HEADLINE,
                "promotion_position": "8",
            },
            {
                "batch_id": "b_other",
                "item": "other-batch",
                "page_type": "main",
                "promotion_type": PROMOTION_TV_HEADLINE,
                "promotion_position": "9",
            },
        ]
        candidates = promotion_update_candidates(rows, "b_test")
        self.assertEqual([row["item"] for row in candidates], ["main-a"])

        csv_path = Path("C:/bestbuy/test-promotion-overlay.csv")
        with patch("bestbuy.step14_db_load.read_csv", return_value=copy.deepcopy(rows)), patch(
            "bestbuy.step14_db_load.rel_path", return_value="test-promotion-overlay.csv"
        ):
            cursor = FakeCursor()
            result = update_promotion_only(cursor, csv_path, "tv_table", batch_id="b_test")
        self.assertEqual(result["candidate_rows"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(cursor.updates), 1)
        sql, params = cursor.updates[0]
        self.assertIn("SET promotion_type = %s, promotion_position = %s", sql)
        self.assertIn("WHERE batch_id = %s AND item = %s AND page_type = %s", sql)
        self.assertNotIn("retailer_sku_name", sql)
        self.assertEqual(params, (PROMOTION_TV_HEADLINE, 7, "b_test", "main-a", "main"))
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
            "promotion_type": PROMOTION_TV_HEADLINE,
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
        self.assertEqual(params, (PROMOTION_TV_HEADLINE, 2, "b_test", "100", "main"))

    def test_db_update_count_mismatch_fails_inside_update(self):
        row = {
            "batch_id": "b_test",
            "item": "main-a",
            "page_type": "main",
            "promotion_type": PROMOTION_TV_HEADLINE,
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
                }
            ],
        }
        rows = [
            {
                "sku_id": str(index),
                "promotion_type": PROMOTION_TV_HEADLINE,
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

    def test_apply_run_path_env_scopes_promotion_to_existing_run(self):
        run_root = Path("C:/bestbuy/test-run")
        env = {"BESTBUY_RUN_ROOT": str(run_root), "BESTBUY_FORCE_RUN_PATH_ENV": "1"}
        apply_run_path_env(env)
        self.assertEqual(Path(env["BESTBUY_PROMOTION_RUN_ROOT"]), run_root / "promotion")
        self.assertEqual(Path(env["BESTBUY_TRENDING_RUN_ROOT"]), run_root / "trending")

    def test_recovery_bat_omits_empty_batch_argument(self):
        bat_path = Path(__file__).parents[1] / "run_bestbuy_promotion_recovery.bat"
        source = bat_path.read_text(encoding="utf-8")
        self.assertIn('if "%BATCH_ID%"=="" (', source)
        self.assertIn('set "BESTBUY_BATCH_ID="', source)
        self.assertIn('--run-root "%RUN_ROOT%" --promotion-only', source)
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
