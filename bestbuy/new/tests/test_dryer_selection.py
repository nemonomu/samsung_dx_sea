"""Offline exclusions, refill, ranking, reuse and interruption checks for DRYER."""
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import test_dryer as fixtures
from test_dryer import api_graph, api_product, list_row, listing, ldy, rules, runner


class SelectionRulesTests(unittest.TestCase):
    def test_clear_non_targets_are_excluded_without_brand_or_sku_rules(self):
        cases = {
            "Brand - Front Load Washer": "washer_only",
            "Washing Machine with Quick Wash": "washer_only",
            "Hand Dryer, 2 Pack": "non_laundry_dryer",
            "Brand - Professional Hair Dryer": "non_laundry_dryer",
            "27-in Wide Laundry Pedestal for Washers and Dryers": "accessory",
            "Washer/Dryer Laundry Pedestal with Storage Drawer": "accessory",
            "Washer & Dryer Laundry Pedestal with Storage Drawer": "accessory",
            "Stacking Kit for Most Washers and Dryers": "accessory",
            "Stack Bracket Kit for Dryers": "accessory",
            "Recess Kit for Electric Hand Dryer": "accessory",
            "Wall Guard for Electric Hand Dryer": "accessory",
            "Dryer Lint Filter Replacement": "accessory",
            "Dryer Drum Belt Replacement": "accessory",
        }
        for name, reason in cases.items():
            with self.subTest(name=name):
                result = rules.classify_product_name(name)
                self.assertEqual((result["action"], result["reason"]), ("exclude", reason))

    def test_laundry_combinations_and_included_accessories_are_kept(self):
        names = [
            "Brand - 7.4 Cu. Ft. Electric Dryer with Washer Link",
            "5.3 Cu. Ft. All-In-One Washer/Electric Dryer Combo",
            "Front Load Washer and Gas Dryer WashTower",
            "Laundry Tower Washer & Electric Dryer",
            "Top Load Washer and Electric Dryer Laundry Center",
            "24 inch Drying Cabinet",
            "Compact Drying Machine",
            "Open Box Electric Dryer with Sensor Dry",
            "Commercial Gas Dryer",
            "Electric Dryer with Included Stacking Kit",
            "WashTower with Included Stacking Kit",
            "Gas Dryer with Drying Rack",
            "Electric Dryer and Laundry Pedestal",
            "Washer with Drying Function",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertNotEqual(rules.classify_product_name(name)["action"], "exclude")
        for name in ("", "Unknown Appliance", "Professional Blow Dryer", "Washer / Dryer"):
            self.assertNotEqual(rules.classify_product_name(name)["action"], "exclude")

    def test_own_detail_identity_is_distinct_from_matching_products(self):
        p = api_product(1)
        p.update(name={"short": "Unknown Appliance"}, description={"short": None, "long": None},
                 specificationGroups=[{"specifications": [{"displayName": "Matching Washer Type", "value": "Front load"}]}],
                 features=[{"skuId": "different", "description": "This hand dryer is fast."}])
        self.assertEqual(rules.product_selection({}, p)["action"], "uncertain")
        p["features"] = [{"description": "The matching top load washer has a large capacity."}]
        self.assertEqual(rules.product_selection({}, p)["action"], "uncertain")
        p["description"]["long"] = "This matching washer is compatible with your dryer."
        self.assertEqual(rules.product_selection({}, p)["action"], "uncertain")
        p["name"]["short"] = "Brand - Hand Dryer"
        self.assertEqual(rules.product_selection({}, p)["action"], "exclude")

    def test_unclear_names_can_use_own_product_type_or_description(self):
        p = api_product(1)
        p.update(name={"short": "Unknown Appliance"}, specificationGroups=[], features=[],
                 description={"short": None, "long": "This front load washing machine cleans laundry."})
        self.assertEqual(rules.product_selection({}, p)["reason"], "washer_only")
        p["description"]["long"] = "Pair with this front load washing machine."
        self.assertEqual(rules.product_selection({}, p)["action"], "uncertain")
        p["specificationGroups"] = [{"specifications": [{"displayName": "Product Type", "value": "Laundry Pedestal"}]}]
        self.assertEqual(rules.product_selection({}, p)["reason"], "accessory")
        p["specificationGroups"][0]["specifications"].append({"displayName": "Dryer Heating Source", "value": "Electric"})
        self.assertEqual(rules.product_selection({}, p)["action"], "uncertain")
        p["name"]["short"] = "Washer with Drying Function"
        p["specificationGroups"] = [{"specifications": [{"displayName": "Product Type", "value": "Washer"}]}]
        self.assertEqual(rules.product_selection({}, p)["action"], "uncertain")


class SelectionFlowTests(unittest.TestCase):
    def setUp(self):
        fixtures.ApiFlowTests.setUp(self)

    def graph(self, ids, names=None):
        body = api_graph(ids)
        for doc in body["data"]["detailedProductSearch"]["documents"]:
            sku = int(doc["product"]["skuId"])
            if names and sku in names:
                doc["product"]["name"]["short"] = names[sku]
        return body

    def test_blank_name_exclusion_refills_and_uncertain_reuse_preserves_order(self):
        pages = {1: self.graph([1, 2, 3, 4, 5, 6], {
            1: "Front Load Washer", 2: "Electric Dryer", 3: None,
            4: "Washer/Electric Dryer Combo", 5: "Stacking Kit", 6: None}),
            2: self.graph([7, 8])}
        products = {3: api_product(3), 6: api_product(6)}
        products[3]["name"]["short"] = "Top Load Washer"
        products[6].update(name={"short": "Unknown Appliance"}, specificationGroups=[], features=[],
                           description={"short": None, "long": None})
        requests = []

        def post(helpers, payload):
            if isinstance(payload, dict):
                requests.append(("list", payload["variables"]["pagination"]["pageNumber"]))
                return 200, pages[payload["variables"]["pagination"]["pageNumber"]]
            ids = [int(op["variables"]["skuId"]) for op in payload]
            requests.append(("detail", ids))
            return 200, [{"data": {"productBySkuId": products.get(sid, api_product(sid))}} for sid in ids]

        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", side_effect=post), redirect_stdout(io.StringIO()):
            root = Path(directory)
            state = {}
            main = runner.collect_listing(self.runtime, root, "main", 5, 4, "b_test", 5, state)
            self.assertEqual([r["sku_id"] for r in main], ["2", "4", "6", "7"])
            bsr = runner.collect_listing(self.runtime, root, "bsr", 5, 3, "b_test", 5, state)
            merged = rules.merge_targets(main, bsr, 4, 3)
            self.assertEqual([(r["main_rank"], r["bsr_rank"]) for r in merged], [(1, 1), (2, 2), (3, 3), (4, "")])
            rows, _, errors = runner.collect_details(self.runtime, root, merged, "b_test", 5, state)
            self.assertFalse(errors)
            self.assertEqual([r["main_rank"] for r in rows], [1, 2, 3, 4])
            audit = json.loads((root / "main_selection.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["summary"]["excluded_count"], 3)
            self.assertEqual(audit["summary"]["uncertain_retained_count"], 1)
        detail_ids = [sid for kind, ids in requests if kind == "detail" for sid in ids]
        self.assertEqual(detail_ids.count(3), 1)
        self.assertEqual(detail_ids.count(6), 1)
        self.assertFalse({1, 5}.intersection(detail_ids))
        self.assertIn(("list", 2), requests)

    def test_real_end_is_shortfall_but_http_error_is_not_end(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", side_effect=[(200, self.graph([1, 2], {1: "Washer"})), (200, api_graph([]))]), redirect_stdout(io.StringIO()) as logs:
            root = Path(directory)
            selected = runner.collect_listing(self.runtime, root, "main", 5, 3)
            self.assertEqual([r["sku_id"] for r in selected], ["2"])
            saved = json.loads((root / "main_listing.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["complete"])
            self.assertEqual(saved["selection_summary"]["shortfall"], 2)
            self.assertIn('reason="products_exhausted"', logs.getvalue())
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", side_effect=[(200, self.graph([1])), (429, {})]), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(runner.DryerError, "http_429"):
                runner.collect_listing(self.runtime, Path(directory), "main", 5, 3)

    def test_selection_http429_stops_before_bsr_and_db(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "load_runtime", return_value=self.runtime), patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", side_effect=[(200, self.graph([1], {1: None})), (429, {})]) as post, patch.object(runner, "load_table") as load, redirect_stdout(io.StringIO()):
            root = Path(directory)
            with runner.run_logging(root) as logger:
                self.assertEqual(runner.run(runner.parse_args(["--no-load"]), root, logger), 1)
            manifest = json.loads((root / "dryer_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual((manifest["status"], manifest["error"], manifest["db_loaded"]), ("failed", "http_429", False))
            self.assertEqual(post.call_count, 2)
            load.assert_not_called()

    def test_retry_exhausted_uncertain_is_retained_without_repeated_detail_attempts(self):
        state = {}
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", side_effect=[(200, self.graph([1], {1: None})), (200, [{"errors": [{}]}]), (200, [{"errors": [{}]}])]) as post, patch.object(ldy, "detail_retry_sleep_seconds", return_value=0), redirect_stdout(io.StringIO()):
            root = Path(directory)
            selected = runner.collect_listing(self.runtime, root, "main", 5, 1, "b_test", 5, state)
            final = rules.merge_targets(selected, [], 1, 0)
            rows, evidence, errors = runner.collect_details(self.runtime, root, final, "b_test", 5, state)
            self.assertEqual(post.call_count, 3)
            self.assertEqual((len(rows), len(errors), rows[0]["main_rank"]), (1, 1, 1))
            self.assertEqual(evidence[0]["detail_status"], "failed")
            self.assertTrue(errors[0]["retry_exhausted"])

    def test_old_unfiltered_cache_is_not_reused(self):
        for version in (4, 5):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", return_value=(200, self.graph([2]))) as post, redirect_stdout(io.StringIO()):
                root = Path(directory)
                runner.write_json(root / "main_listing.json", {"collector_version": version, "limit": 1,
                    "limit_reached": True, "rows": [list_row(1, 1)]})
                self.assertEqual([r["sku_id"] for r in runner.collect_listing(self.runtime, root, "main", 5, 1)], ["2"])
                post.assert_called_once()

    def test_uncertain_memo_does_not_hide_a_later_clear_non_target_name(self):
        state = {"selection_decisions": {("1", "item1"): {
            "action": "uncertain", "reason": "detail_unavailable", "source": "detail_api"}}}
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", return_value=(200, self.graph([1, 2], {1: "Washer"}))) as post, redirect_stdout(io.StringIO()):
            rows = runner.collect_listing(self.runtime, Path(directory), "bsr", 5, 1, "b_test", 5, state)
            self.assertEqual([row["sku_id"] for row in rows], ["2"])
            post.assert_called_once()

    def test_300_100_refill_contiguous_ranks_and_overlap_with_one_detail_per_product(self):
        main_ids = list(range(1, 451))
        bsr_ids = list(range(201, 301)) + list(range(1001, 1101))
        detail_requests = []
        list_requests = []

        def post(helpers, payload):
            if isinstance(payload, dict):
                variables = payload["variables"]
                page = variables["pagination"]["pageNumber"]
                bsr = variables["sort"]["sort"] == "Best-Selling"
                ids = (bsr_ids if bsr else main_ids)[(page - 1) * 18:page * 18]
                list_requests.append((bsr, page))
                return 200, self.graph(ids, {sid: "Top Load Washer" if sid % 4 == 0 else "Electric Dryer" for sid in ids})
            ids = [int(op["variables"]["skuId"]) for op in payload]
            detail_requests.extend(ids)
            return 200, [{"data": {"productBySkuId": api_product(sid)}} for sid in ids]

        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "load_runtime", return_value=self.runtime), patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", side_effect=post), redirect_stdout(io.StringIO()):
            root = Path(directory)
            with runner.run_logging(root) as logger:
                self.assertEqual(runner.run(runner.parse_args(["--no-load"]), root, logger), 0)
            manifest = json.loads((root / "dryer_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual((manifest["main_target_count"], manifest["bsr_target_count"], manifest["overlap_count"], manifest["output_count"]), (300, 100, 75, 325))
            with (root / "output/final_output.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows[0]), 20)
            self.assertEqual(sorted(int(r["main_rank"]) for r in rows if r["main_rank"]), list(range(1, 301)))
            self.assertEqual(sorted(int(r["bsr_rank"]) for r in rows if r["bsr_rank"]), list(range(1, 101)))
            self.assertEqual(len({r["item"] for r in rows}), 325)
            self.assertEqual(len(detail_requests), len(set(detail_requests)))
            self.assertEqual(len(detail_requests), 325)
            self.assertTrue(all(sid % 4 for sid in detail_requests))
            self.assertGreater(max(page for bsr, page in list_requests if not bsr), 17)
            self.assertIn('"elapsed_s"', (root / "logs/dryer_events.jsonl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
