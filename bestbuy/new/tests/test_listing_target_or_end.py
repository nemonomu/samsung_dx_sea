"""Offline target/end regressions; config, browser, DB and network are not loaded."""

import copy
import csv
import hashlib
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from bestbuy import step00_collection_recovery as common
from bestbuy import step01_listing_recovery as listing
from bestbuy import step01_stage_retry as retry


def page_data(page, count=18, total=None, combos=0, ads=0):
    docs, rows = [], []
    for position in range(1, count + 1):
        sku = f"{page}-{position}"
        if position <= combos:
            docs.append({"combo": {"id": sku}})
        else:
            docs.append({"product": {"skuId": sku}})
            rows.append(dict(sku_id=sku, organic_rank=position, page=page,
                             global_organic_rank=(page - 1) * 18 + position,
                             global_visual_rank=(page - 1) * 25 + position,
                             container_type="organic_product"))
    sponsored = [{"source": "fixture", "product": {"skuId": f"ad-{i}"}} for i in range(ads)]
    rows.extend(dict(sku_id=f"ad-{i}", container_type="sponsored_ingrid") for i in range(ads))
    graph = {"data": {"detailedProductSearch": {"documents": docs}, "search": {
        "withBestMedia": {"placements": [{"name": "SEARCH_SPONSORED_INGRID",
            "documentsGridView": {"sponsoredDocuments": sponsored}}]}}}}
    if total is not None:
        graph["data"]["search"]["numFound"] = total
    return graph, {"status_code": 200}, rows


def not_found():
    return {"data": {"detailedProductSearch": {"documents": None}, "search": None},
            "errors": [{"path": ["detailedProductSearch", "documents"],
                        "extensions": {"code": "NOT_FOUND"}}]}, {"status_code": 200}, []


class TargetOrEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 0
        self.output = io.StringIO()
        redirect = redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        # Never import operational configuration or consult the real environment.
        self.env = {"BESTBUY_CATEGORY": "TV", "BESTBUY_RUN_ROOT": str(self.root)}
        patcher = patch.object(os, "environ", self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sleep(self, seconds):
        self.now += seconds

    def budget(self, evidence):
        return common.RecoveryBudget(evidence, clock=lambda: self.now, sleep=self.sleep)

    def api(self, responses, category="TV", stage="main", target=300, cap=100):
        return types.SimpleNamespace(
            CATEGORY=category, RUN_ROOT=self.root / stage, SEARCH_TERM="fixture", SEARCH_SORT="",
            SEARCH_PAGES=16 if stage == "main" else 6, ORGANIC_OFFSET=18,
            LISTING_ORGANIC_TARGET=target, LISTING_MAX_PAGES=cap,
            prepare_product_list_payload=lambda op, page: {"page": page},
            browser_graphql_fetch_once=Mock(side_effect=responses),
            browser_graphql_local_port=lambda: 1234,
            page_summary=lambda page, rows, meta, graph: {"page": page},
            close_browser_graphql_page=Mock(), create_browser_graphql_page=Mock(return_value=object()),
            initialize_browser_graphql_session=Mock())

    def collect(self, api):
        return listing.collect(api, {}, object(), budget_factory=self.budget)

    def load_production(self, stem):
        config = types.ModuleType("bestbuy.step00_config")
        config.DEFAULT_BESTBUY_RUN_ROOT = self.root
        config.PROMOTION_TV_EXPECTED_MIN_ROWS = 1
        config.KRW_PER_USD = 1
        config.bestbuy_dated_run_root = lambda *args, **kwargs: self.root
        config.bestbuy_category = lambda: self.env["BESTBUY_CATEGORY"]
        config.has_target_url = lambda *args, **kwargs: False
        config.target_url = lambda *args, **kwargs: ""
        config.old_pdp_url = lambda sku: f"https://example.invalid/{sku}"
        config.rel_path = str
        name = "bestbuy._test_target_or_end_" + stem
        path = Path(listing.__file__).with_name(stem + ".py")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"bestbuy.step00_config": config, name: module}):
            spec.loader.exec_module(module)
        return module

    def publish_listing(self, stage, rows, report):
        path = self.root / stage / "parsed" / "main_occurrences.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = sorted({key for row in rows for key in row}) or ["sku_id"]
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        report = dict(report, status="complete", occurrences_csv=str(path),
                      occurrences_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        common.atomic_json(path.parent.parent / "collection_status.json", report)
        return path

    def test_observed_ldy_262_documents_end_at_15_for_all_three_categories(self):
        for category in ("TV", "REF", "LDY"):
            with self.subTest(category=category):
                pages = [page_data(p, 18 if p < 15 else 10, 262,
                                   combos=11 if p == 14 else 7 if p == 15 else 0, ads=7)
                         for p in range(1, 16)]
                api = self.api([*pages, not_found()], category=category)
                rows, _, _, report = self.collect(api)
                self.assertEqual(list(rows), list(range(1, 16)))
                self.assertEqual(api.browser_graphql_fetch_once.call_count, 15)
                self.assertEqual(report["reason"], "verified_end")
                self.assertEqual(report["organic_count"], 244)
                self.assertEqual(report["pagination"]["document_count"], 10)
                self.assertEqual(report["pagination"]["last_page"], 15)
                self.assertEqual(report["status"], "validated")
                self.assertFalse(listing.listing_exhausted(report, category))
                self.assertTrue(listing.listing_exhausted(dict(report, status="complete"), category))

    def test_main_grows_to_20_pages_and_stops_when_300_reached(self):
        api = self.api([page_data(p, total=360) for p in range(1, 21)])
        rows, _, _, report = self.collect(api)
        self.assertEqual(max(rows), 17)  # Whole-page validation; final main selection caps at 300.
        self.assertEqual(report["organic_count"], 306)
        self.assertEqual(report["reason"], "target_reached")
        final = self.load_production("step07_final_targets")
        selected, _ = final.choose_final_rows(final.unique_main_rows(
            [row for page in rows.values() for row in page]), [], 300)
        self.assertEqual(len(selected), 300)

    def test_main_reaches_page_20_but_has_fewer_than_300_products(self):
        api = self.api([page_data(p, total=360, combos=4) for p in range(1, 21)])
        rows, _, _, report = self.collect(api)
        self.assertEqual(max(rows), 20)
        self.assertEqual(report["organic_count"], 280)
        self.assertEqual(report["reason"], "verified_end")

    def test_main_shrinks_to_14_full_pages(self):
        api = self.api([page_data(p, total="252") for p in range(1, 15)])
        rows, _, _, report = self.collect(api)
        self.assertEqual(max(rows), 14)
        self.assertEqual(report["organic_count"], 252)
        self.assertEqual(report["reason"], "verified_end")

    def test_bsr_keeps_100_target_and_caps_rank_output(self):
        api = self.api([page_data(p, total=360) for p in range(1, 21)], stage="bsr", target=100)
        rows, _, _, report = self.collect(api)
        self.assertEqual(max(rows), 6)
        self.assertEqual(report["reason"], "target_reached")
        self.publish_listing("bsr", [r for page in rows.values() for r in page], report)
        rank = self.load_production("step04_bsr_rank")
        rank.main()
        self.assertEqual(len(rank.load_rows(rank.OUTPUT_CSV)), 100)

    def test_bsr_continues_past_old_20_page_cap_when_combos_dominate(self):
        api = self.api([page_data(p, total=450, combos=14) for p in range(1, 26)],
                       stage="bsr", target=100)
        rows, _, _, report = self.collect(api)
        self.assertEqual(max(rows), 25)
        self.assertEqual(report["organic_count"], 100)

    def test_below_target_outputs_and_resume_checks_accept_verified_end(self):
        for category in ("TV", "REF", "LDY"):
            with self.subTest(category=category):
                self.env["BESTBUY_CATEGORY"] = category
                pages = [page_data(p, 18 if p < 5 else 8, 80) for p in range(1, 6)]
                main = self.collect(self.api(copy.deepcopy(pages), category=category))
                bsr = self.collect(self.api(copy.deepcopy(pages), category=category, stage="bsr", target=100))
                main_rows = [r for page in main[0].values() for r in page]
                main_path = self.publish_listing("main", main_rows, main[3])
                self.publish_listing("bsr", [r for page in bsr[0].values() for r in page], bsr[3])
                rank = self.load_production("step04_bsr_rank")
                rank.main()
                final = self.load_production("step07_final_targets")
                final.MAIN_INPUT = main_path
                final.main()
                manifest = common.read_json(final.OUTPUT_CSV.with_suffix(".manifest.json"))
                self.assertEqual(manifest["final_unique_sku_count"], 80)
                self.assertTrue(manifest["main_exhausted"])
                self.assertFalse(manifest["needs_more_main_candidates"])
                orchestrator = self.load_production("bestbuy_orchestrator")
                self.assertTrue(orchestrator.bsr_rank_complete()[0])
                self.assertTrue(orchestrator.final_targets_complete()[0])
                # An incomplete run cannot reuse the small published output as success.
                common.atomic_json(self.root / "bsr/collection_status.json", dict(bsr[3], status="incomplete"))
                self.assertFalse(orchestrator.bsr_rank_complete()[0])
                self.assertFalse(orchestrator.final_targets_complete()[0])

    def test_orchestrator_scopes_policy_to_tv_ref_ldy(self):
        orchestrator = self.load_production("bestbuy_orchestrator")
        for category in ("TV", "REF", "LDY", "HHP", "DRYER"):
            self.env["BESTBUY_CATEGORY"] = category
            with patch.object(retry, "run_listing_step") as run:
                for key in ("01", "03"):
                    orchestrator.run_step(orchestrator.step_by_key(key))
                main_env, bsr_env = [call.args[1] for call in run.call_args_list]
                if category in {"TV", "REF", "LDY"}:
                    self.assertEqual(main_env["BESTBUY_LISTING_ORGANIC_TARGET"], "300")
                    self.assertEqual(main_env["BESTBUY_LISTING_MAX_PAGES"], "100")
                    self.assertEqual(bsr_env["BESTBUY_LISTING_MAX_PAGES"], "100")
                else:
                    self.assertNotIn("BESTBUY_LISTING_ORGANIC_TARGET", main_env)
                    self.assertEqual(bsr_env["BESTBUY_LISTING_MAX_PAGES"], "20")
                self.assertEqual(bsr_env["BESTBUY_LISTING_ORGANIC_TARGET"], "100")

    def test_unscoped_categories_do_not_get_metadata_end_behavior(self):
        for category in ("HHP", "DRYER"):
            api = self.api([page_data(1, 1, 1), page_data(2, 0)], category=category)
            rows, _, _, report = self.collect(api)
            self.assertEqual(list(rows), [1, 2])
            self.assertFalse(report["target_or_end"])

    def test_missing_invalid_or_inconsistent_total_does_not_truncate(self):
        for total in (None, True, -1, "unknown", 1.5, {"value": 1}, 1):
            with self.subTest(total=total):
                api = self.api([page_data(1, 2, total), page_data(2, 1, 19)])
                rows, _, _, report = self.collect(api)
                self.assertEqual(list(rows), [1, 2])
                self.assertEqual(report["reason"], "verified_end")

    def test_unproven_not_found_is_not_an_end_even_after_short_page(self):
        for total in (None, 262):
            api = self.api([page_data(1, 1, total), *[not_found() for _ in range(5)]])
            with self.assertRaisesRegex(common.CollectionIncomplete, "recovery_attempt_limit"):
                self.collect(api)
            report = common.read_json(self.root / "main/collection_status.json")
            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(report["failed_page"], 2)

    def test_metadata_does_not_override_transport_identity_or_graphql_failure(self):
        for kind in ("transport", "identity", "graphql", "empty_contradiction"):
            graph, meta, rows = page_data(1, 1, 1)
            if kind == "transport":
                meta.update(status_code="ERR", error="TypeError: Failed to fetch")
            elif kind == "identity":
                rows[0]["sku_id"] = "wrong"
            elif kind == "graphql":
                graph["errors"] = [{"path": ["search", "numFound"], "extensions": {"code": "NOT_FOUND"}}]
            else:
                graph, meta, rows = page_data(1, 0, 262)
            with self.subTest(kind=kind), self.assertRaises(common.CollectionIncomplete):
                self.collect(self.api([(graph, meta, rows) for _ in range(5)]))

    def test_recovery_restarts_a_pass_and_does_not_mix_pagination_metadata(self):
        failed = ({}, {"status_code": "ERR", "error": "TypeError: Failed to fetch"}, [])
        responses = [page_data(1, total=360), failed, page_data(2, total=360),
                     page_data(1, total=20), page_data(2, 2, 20)]
        api = self.api(responses)
        rows, _, _, report = self.collect(api)
        self.assertEqual([c.args[0] for c in api.browser_graphql_fetch_once.call_args_list], [1, 2, 2, 1, 2])
        self.assertEqual(report["accepted_pass"], 2)
        self.assertEqual(report["organic_count"], 20)
        self.assertEqual(report["pagination"]["num_found"], 20)
        self.assertEqual(len(rows[2]), 2)

    def test_safety_cap_before_target_or_end_is_incomplete(self):
        api = self.api([page_data(p, total=360) for p in range(1, 3)], cap=2)
        with self.assertRaisesRegex(common.CollectionIncomplete, "organic_target_not_reached_at_page_limit"):
            self.collect(api)
        self.assertEqual(common.read_json(self.root / "main/collection_status.json")["status"], "incomplete")
        with self.assertRaises(common.CollectionIncomplete):
            common.assert_ready(self.root, listing="main")

    def test_repeated_skus_do_not_count_towards_target(self):
        graph, meta, rows = page_data(2, 2, 20)
        graph["data"]["detailedProductSearch"]["documents"][0]["product"]["skuId"] = "1-1"
        rows[0]["sku_id"] = "1-1"
        api = self.api([page_data(1, total=20), (graph, meta, rows)])
        result, _, _, report = self.collect(api)
        self.assertEqual(report["organic_count"], 19)
        self.assertEqual(len(result[2]), 1)
        self.assertEqual(result[2][0]["organic_rank"], 2)
        self.assertEqual(report["reason"], "verified_end")


if __name__ == "__main__":
    unittest.main()
