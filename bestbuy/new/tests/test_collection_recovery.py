"""Offline regression scenarios for the September 22 intermittent failures."""

import io
import copy
import csv
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("BESTBUY_CATEGORY", "TV")
os.environ.setdefault("BESTBUY_URL_SOURCE", "default")
try:
    import zenrows
except ModuleNotFoundError:
    sys.modules.setdefault("zenrows", types.SimpleNamespace(ZenRowsClient=object))

from bestbuy import step00_collection_recovery as common
from bestbuy import step01_listing_recovery as listing
from bestbuy import step08_collection_recovery as recovery
from bestbuy import step08_detail_enrichment as detail


class Clock:
    def __init__(self):
        self.now = 0
        self.delays = []

    def sleep(self, value):
        self.now += value
        self.delays.append(value)

    def budget(self, evidence):
        return common.RecoveryBudget(evidence, clock=lambda: self.now, sleep=self.sleep)


def page_data(sku):
    docs = [] if sku is None else [{"product": {"skuId": sku}}]
    graph = {"data": {"detailedProductSearch": {"documents": docs}}}
    rows = [] if sku is None else [dict(sku_id=sku, organic_rank=1, container_type="organic_product")]
    return graph, {"status_code": 200}, rows


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def fake_listing(self, responses):
        calls = []
        def fetch(page, payload, browser):
            calls.append(page)
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result
        api = types.SimpleNamespace(RUN_ROOT=self.root / "bsr", SEARCH_TERM="refrigerator",
            SEARCH_SORT="Best-Selling", SEARCH_PAGES=3, LISTING_ORGANIC_TARGET=3,
            LISTING_MAX_PAGES=8, ORGANIC_OFFSET=1,
            prepare_product_list_payload=lambda operation, page: {"page": page},
            browser_graphql_fetch_once=fetch, browser_graphql_local_port=lambda: 1234,
            page_summary=lambda page, rows, meta, graph: {"page": page},
            close_browser_graphql_page=Mock(), create_browser_graphql_page=Mock(return_value=object()),
            initialize_browser_graphql_session=Mock())
        return api, calls

    def test_failed_page_is_probed_then_new_pass_no_splicing(self):
        api, calls = self.fake_listing(iter([page_data("old"), RuntimeError("Failed to fetch"),
            page_data("probe"), page_data("new1"), page_data("new2"), page_data("new3")]))
        rows, _, _, report = listing.collect(api, {}, object(), budget_factory=Clock().budget)
        self.assertEqual(calls, [1, 2, 2, 1, 2, 3])
        self.assertEqual([r[0]["sku_id"] for r in rows.values()], ["new1", "new2", "new3"])
        self.assertEqual(report["accepted_pass"], 2)
        self.assertEqual(report["status"], "validated")  # CSV publication is a separate commit point.
        evidence = list((self.root / "bsr/recovery").rglob("events.jsonl"))[0].read_text(encoding="utf-8")
        self.assertIn('"old"', evidence)
        self.assertIn("Failed to fetch", evidence)

    def test_repeated_failure_caps_passes_and_does_not_reset_waits(self):
        api, calls = self.fake_listing(iter([RuntimeError("offline"), page_data("probe"),
            RuntimeError("offline"), page_data("probe"), RuntimeError("offline")]))
        clock = Clock()
        with self.assertRaisesRegex(common.CollectionIncomplete, "listing_pass_limit"):
            listing.collect(api, {}, object(), budget_factory=clock.budget)
        report = common.read_json(self.root / "bsr/collection_status.json")
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual([e["wait_seconds"] for e in report["recovery_history"] if e["event"] == "waiting"], [30, 120])
        self.assertEqual(calls, [1, 1, 1, 1, 1])
        with self.assertRaises(common.CollectionIncomplete):
            common.assert_ready(self.root)

    def test_transport_failure_never_becomes_verified_empty(self):
        graph, meta, rows = page_data(None)
        self.assertTrue(listing.validate_page(graph, meta, rows)[2])
        meta["status_code"] = "ERR"
        self.assertFalse(listing.validate_page(graph, meta, rows)[0])
        self.assertFalse(listing.validate_page({"data": {}}, {"status_code": 200}, [])[0])

    def test_combo_positions_allowed_but_missing_product_rejected(self):
        graph, meta, rows = page_data("a")
        graph["data"]["detailedProductSearch"]["documents"].append({"combo": {"id": "bundle"}})
        self.assertTrue(listing.validate_page(graph, meta, rows)[0])
        graph["data"]["detailedProductSearch"]["documents"][1] = {"product": None}
        self.assertFalse(listing.validate_page(graph, meta, rows)[0])

    def test_budget_checks_elapsed_time_and_never_sleeps_past_limit(self):
        clock = Clock()
        budget = common.RecoveryBudget(Mock(), seconds=40, clock=lambda: clock.now, sleep=clock.sleep)
        self.assertTrue(budget.wait())
        self.assertFalse(budget.wait())
        self.assertEqual(clock.now, 30)
        self.assertEqual(budget.reason, "recovery_time_limit")

    def test_journal_redacts_sensitive_values_without_losing_error_paths(self):
        evidence = common.Evidence(self.root, "test")
        event = evidence.request({"token": "fake-key", "sku": "A"},
            {"errors": [{"path": ["product", "price"], "message": "Bearer fake-key"}], "email": "fake@example.com"})
        text = Path(event["evidence_path"]).read_text(encoding="utf-8")
        self.assertNotIn("fake-key", text)
        self.assertNotIn("fake@example.com", text)
        self.assertIn('"price"', text)

    def install_detail(self):
        for name in ("main", "bsr"):
            common.atomic_json(self.root / name / "collection_status.json", dict(status="complete"))
        base = self.root / "detail"
        changes = dict(OUTPUT_ROOT=self.root / "output", DETAIL_ROOT=base,
            RAW_DETAIL_DIR=base / "raw/detail_html", RAW_REVIEW_DIR=base / "raw/review20",
            RAW_COMPARE_DIR=base / "raw/compare", PARSED_DIR=base / "parsed", BENCHMARKS_DIR=base / "benchmarks",
            DETAIL_ROWS_CSV=base / "parsed/detail_enriched_rows.csv", FAILURES_CSV=base / "parsed/detail_failures.csv",
            MANIFEST_PATH=base / "manifest_detail_enrichment.json", FINAL_OUTPUT_CSV=self.root / "output/final_output.csv",
            PRODUCT_LIST_CSV=self.root / "output/bestbuy_product_list.csv", TARGET_CSV=self.root / "output/bestbuy_final_targets.csv", FORCE_REFRESH=False,
            FETCH_COMPARE=True, FETCH_FULFILLMENT_DYNAMIC=False, FETCH_GET_IT_FAST=False,
            CATEGORY="TV", DETAIL_SKU_BATCH_SIZE=5, STAGE="detail")
        patcher = patch.multiple(detail, **changes)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, replacement in {
            "detail_selector_values": lambda text: {},
            "sample_fields": lambda: ["sku_id", "main_rank", "bsr_rank", "retailer_sku_name", "final_sku_price", "original_sku_price", "savings", "detailed_review_content", "count_of_reviews", "retailer_sku_name_similar"],
            "update_product_list_from_detail_rows": Mock(),
            "preserve_existing_availability": Mock(),
            "close_detail_browser_page": Mock(),
        }.items():
            patcher = patch.object(detail, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def response(payload, compare_failure=False):
        sku = str(payload["variables"]["skuId"])
        if payload["operationName"] == "GetCompareProduct":
            if compare_failure:
                return {"errors": [{"message": "unavailable", "path": ["recommendations"], "extensions": {"code": "500"}}]}
            return {"data": {"productBySkuId": {"skuId": sku}, "recommendations": {"subPlacements": []}}}
        return {"data": {"productBySkuId": {"skuId": sku, "name": {"short": "Product " + sku},
            "price": {"customerPrice": 999, "displayableCustomerPrice": "$999.00"},
            "reviewInfo": {"reviewCount": 0}, "reviews": {"results": []}}}}

    def test_compare_error_publishes_null_with_warning_then_resume_only_compare(self):
        self.install_detail()
        target = dict(sku_id="12345", main_rank="15", bsr_rank="7", product_name="Example", review_count="0")
        requests = []
        def post(payload, *args):
            requests.append(payload)
            return 200, "", [self.response(p, compare_failure=True) for p in payload], {}, 0
        with patch.object(detail, "browser_graphql_post", post):
            recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        self.assertEqual([len(p) for p in requests], [3, 1])
        report = common.read_json(self.root / "output/collection_status.json")
        self.assertEqual(report["items"]["12345"]["detail"]["status"], "success")
        self.assertEqual(report["items"]["12345"]["review"]["status"], "empty")
        row = common.read_json(self.root / "output/partial_output.json")[0]
        self.assertEqual(row["main_rank"], "15")
        self.assertIsNone(row["retailer_sku_name_similar"])
        self.assertTrue(row["final_sku_price"])
        self.assertTrue(detail.FINAL_OUTPUT_CSV.exists())
        self.assertEqual(report["status"], "complete_with_warnings")
        common.assert_ready(self.root)
        note = common.recovery_notification("TV", self.root)
        self.assertIn("12345 | 15 | 7 | compare", note["body"])
        self.assertIn("NULL 컬럼: retailer_sku_name_similar", note["body"])
        self.assertIn("적재 허용", note["body"])
        requests.clear()
        def recovered(payload, *args):
            requests.append(payload)
            return 200, "", [self.response(p) for p in payload], {}, 0
        with patch.object(detail, "browser_graphql_post", recovered):
            result = recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0]["operationName"], "GetCompareProduct")
        self.assertEqual(result["items"]["12345"]["compare"]["status"], "empty")
        with detail.FINAL_OUTPUT_CSV.open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(next(csv.reader(stream)), detail.sample_fields())
        common.assert_ready(self.root)

    def test_transport_outage_retries_each_chunk_once_and_publishes_listing_values(self):
        self.install_detail()
        targets = [dict(sku_id=str(i), main_rank=str(i), product_name="Listing " + str(i),
                        customer_price="12", regular_price="15", total_savings="3", review_count="0")
                   for i in range(1, 8)]
        detail.FINAL_OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        detail.FINAL_OUTPUT_CSV.write_text("old final", encoding="utf-8")
        post = Mock(side_effect=RuntimeError("TypeError: Failed to fetch"))
        with patch.object(detail, "browser_graphql_post", post):
            recovery.run(detail, targets, targets, budget_factory=Clock().budget)
        self.assertEqual(post.call_count, 4)
        report = common.read_json(self.root / "output/collection_status.json")
        self.assertEqual(report["status"], "complete_with_warnings")
        for item in report["items"].values():
            for state in item.values():
                self.assertEqual(state["request_attempt"], 2)
                self.assertEqual(state["attempt"], 0)
        with detail.FINAL_OUTPUT_CSV.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0]["final_sku_price"], "12")
        self.assertEqual(rows[0]["original_sku_price"], "15")
        self.assertEqual(rows[0]["savings"], "3")
        self.assertEqual(rows[0]["detailed_review_content"], "")
        common.assert_ready(self.root)

    def test_graphql_error_and_missing_compare_shape_not_empty(self):
        t = {"review_count": "0"}
        self.assertEqual(recovery.validate_operation(detail, "compare", "A", {"data": {}}, t)[0], "failed")
        item = {"data": {"recommendations": {"subPlacements": []}}, "errors": [{"message": "broken"}]}
        self.assertEqual(recovery.validate_operation(detail, "compare", "A", item, t)[0], "failed")

    @staticmethod
    def compare_with_warning(sku="12345"):
        return {"data": {"productBySkuId": {"skuId": sku, "name": {"short": "Current"}},
                         "recommendations": {"subPlacements": [{"recommendations": [
                             {"item": {"skuId": "99999", "name": {"short": "Similar"}}}]}]}},
                "errors": [{"message": "Error - Not Found", "extensions": {"code": "NOT_FOUND"},
                            "path": ["productBySkuId", "reviewInfo", "proFeatures"]},
                           {"message": "Error - Not Found", "extensions": {"code": "NOT_FOUND"},
                            "path": ["recommendations", "subPlacements", 0, "recommendations", 0,
                                     "item", "reviewInfo", "conFeatures"]}]}

    def test_compare_optional_warning_requires_valid_core_and_exact_error_paths(self):
        good = self.compare_with_warning()
        self.assertEqual(recovery.validate_operation(detail, "compare", "12345", good, {}),
                         ("success", "verified_with_warnings"))
        for mutation in ("missing_product", "wrong_sku", "null_placements", "null_list", "null_item",
                         "missing_sku", "missing_name", "critical_error", "unknown_code", "unknown_path",
                         "wrong_index", "malformed_error"):
            with self.subTest(mutation=mutation):
                item = copy.deepcopy(good)
                product = item["data"]["productBySkuId"]
                recs = item["data"]["recommendations"]
                candidate = recs["subPlacements"][0]["recommendations"][0]["item"]
                if mutation == "missing_product": item["data"]["productBySkuId"] = None
                elif mutation == "wrong_sku": product["skuId"] = "wrong"
                elif mutation == "null_placements": recs["subPlacements"] = None
                elif mutation == "null_list": recs["subPlacements"][0]["recommendations"] = None
                elif mutation == "null_item": recs["subPlacements"][0]["recommendations"][0]["item"] = None
                elif mutation == "missing_sku": candidate.pop("skuId")
                elif mutation == "missing_name": candidate["name"] = {"short": " "}
                elif mutation == "critical_error": item["errors"].append({"path": ["recommendations"], "extensions": {"code": "NOT_FOUND"}})
                elif mutation == "unknown_code": item["errors"][0]["extensions"]["code"] = "INTERNAL_SERVER_ERROR"
                elif mutation == "unknown_path": item["errors"][0]["path"][-1] = "reviewCount"
                elif mutation == "wrong_index": item["errors"][1]["path"][2] = -1
                elif mutation == "malformed_error": item["errors"].append("unexpected")
                self.assertEqual(recovery.validate_operation(detail, "compare", "12345", item, {})[0], "failed")
        for stage in ("detail", "review", "compare_v2"):
            self.assertEqual(recovery.validate_operation(detail, stage, "12345", good, {})[0], "failed")

    def test_compare_explicit_empty_differs_from_missing_even_with_warning(self):
        item = self.compare_with_warning()
        item["errors"] = item["errors"][:1]
        item["data"]["recommendations"]["subPlacements"] = []
        self.assertEqual(recovery.validate_operation(detail, "compare", "12345", item, {}),
                         ("empty", "verified_empty_with_warnings"))
        item["data"]["recommendations"]["subPlacements"] = None
        self.assertEqual(recovery.validate_operation(detail, "compare", "12345", item, {})[0], "failed")

    def test_warning_success_is_saved_not_retried_and_survives_resume(self):
        self.install_detail()
        targets = [dict(sku_id=sku, main_rank=rank, bsr_rank="7", review_count="0")
                   for sku, rank in (("12345", "15"), ("67890", "16"))]
        requests = []
        def post(payloads, *args):
            requests.append(payloads)
            responses = []
            for payload in payloads:
                sku = payload["variables"]["skuId"]
                if payload["operationName"] == "GetCompareProduct" and sku == "12345":
                    responses.append(self.compare_with_warning(sku))
                else:
                    responses.append(self.response(payload, compare_failure=True))
            return 200, "", responses, {}, 0
        with patch.object(detail, "browser_graphql_post", post):
            recovery.run(detail, targets, targets, budget_factory=Clock().budget)
        self.assertEqual([len(p) for p in requests], [6, 1])
        self.assertTrue(all(p["variables"]["skuId"] == "67890" for batch in requests[1:] for p in batch))
        report = common.read_json(self.root / "output/collection_status.json")
        self.assertEqual(report["completed_skus"], 1)
        self.assertEqual(len(report["warnings"]), 1)
        self.assertEqual(report["warnings"][0]["main_rank"], "15")
        rows = common.read_json(self.root / "output/partial_output.json")
        self.assertEqual(rows[0]["retailer_sku_name_similar"], "Current ||| Similar")
        self.assertIsNone(rows[1]["retailer_sku_name_similar"])
        journal = next((self.root / "detail/recovery").rglob("events.jsonl")).read_text(encoding="utf-8")
        self.assertIn('"warnings":', journal)
        self.assertIn("proFeatures", journal)
        def recovered(payloads, *args):
            self.assertTrue(all(p["variables"]["skuId"] == "67890" for p in payloads))
            return 200, "", [self.response(p) for p in payloads], {}, 0
        with patch.object(detail, "browser_graphql_post", recovered):
            report = recovery.run(detail, targets, targets, budget_factory=Clock().budget)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(report["warnings"]), 1)
        note = common.recovery_notification("TV", self.root)
        self.assertFalse(note["incomplete"])
        self.assertIn("수집 완료", note["subject"])
        self.assertNotIn("부가 필드 경고", note["body"])
        self.assertNotIn("proFeatures", note["body"])
        self.assertNotIn("보류", note["body"])
        with detail.FINAL_OUTPUT_CSV.open(encoding="utf-8-sig", newline="") as stream:
            final = list(csv.DictReader(stream))
        self.assertEqual(final[0]["retailer_sku_name_similar"], "Current ||| Similar")
        self.assertEqual((final[0]["main_rank"], final[0]["bsr_rank"]), ("15", "7"))

    def test_entire_repeated_page_still_restarts_without_accepting_shifted_ranks(self):
        api, calls = self.fake_listing(iter([page_data("same"), page_data("same"), page_data("probe"),
                                           page_data("a"), page_data("b"), page_data("c")]))
        rows, _, _, report = listing.collect(api, {}, object(), budget_factory=Clock().budget)
        self.assertEqual(report["accepted_pass"], 2)
        self.assertEqual(calls, [1, 2, 2, 1, 2, 3])
        self.assertNotIn("same", [r[0]["sku_id"] for r in rows.values()])

    def test_browser_bootstrap_failure_enters_same_recovery_budget(self):
        api, calls = self.fake_listing(iter([page_data("probe"), page_data("a"), page_data("b"), page_data("c")]))
        api.initialize_browser_graphql_session.side_effect = [RuntimeError("bootstrap failed"), None]
        _, _, _, report = listing.collect(api, {}, None, budget_factory=Clock().budget)
        self.assertEqual(report["accepted_pass"], 2)
        self.assertEqual(calls, [1, 1, 2, 3])

    def test_successful_response_after_deadline_is_not_accepted(self):
        api, calls = self.fake_listing(iter([RuntimeError("offline"), page_data("a")]))
        clock = Clock()
        original = api.browser_graphql_fetch_once
        def fetch(*args):
            result = original(*args)
            clock.now += 1800
            return result
        api.browser_graphql_fetch_once = fetch
        with self.assertRaisesRegex(common.CollectionIncomplete, "recovery_time_limit"):
            listing.collect(api, {}, object(), budget_factory=clock.budget)
        self.assertEqual(calls, [1, 1])

    def test_query_error_retries_once_then_publishes_nulls(self):
        self.install_detail()
        target = dict(sku_id="12345", main_rank="15", review_count="0")
        def post(payload, *args):
            return 200, "", [{"errors": [{"extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]} for _ in payload], {}, 0
        with patch.object(detail, "browser_graphql_post", side_effect=post) as call:
            result = recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["status"], "complete_with_warnings")
        common.assert_ready(self.root)

    def test_wrong_product_identity_is_never_stored_as_success(self):
        self.install_detail()
        target = dict(sku_id="12345", review_count="0")
        payload = detail.fallback_review20_payload("different")
        outcome, reason = recovery.validate_operation(detail, "detail", "12345", self.response(payload), target)
        self.assertEqual((outcome, reason), ("failed", "product_identity_mismatch_or_missing"))

    def test_fresh_transport_failure_does_not_relabel_old_cached_price_as_current(self):
        self.install_detail()
        target = dict(sku_id="12345", main_rank="15", product_name="New listing", review_count="0")
        payload = detail.fallback_review20_payload("12345")
        recovery.store_operation(detail, target, "detail", payload, self.response(payload),
                                 dict(status="success", reason="verified", attempt=1))
        with patch.object(detail, "browser_graphql_post", side_effect=RuntimeError("offline")):
            recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        row = common.read_json(self.root / "output/partial_output.json")[0]
        self.assertIsNone(row["final_sku_price"])
        self.assertEqual(row["main_rank"], "15")
        self.assertEqual(row["retailer_sku_name"], "New listing")

    def test_email_build_notification_uses_partial_summary_and_real_request_counts(self):
        from bestbuy import step16_email_notify as notify
        report = dict(status="partial", target_count=300, completed_skus=298,
            failures=[dict(sku_id="A", main_rank=15, bsr_rank=7, stage="detail", status="failed",
                           request_attempt=4, attempt=3, reason="timeout")], reason="item_attempt_limit")
        common.atomic_json(self.root / "output/collection_status.json", report)
        common.atomic_json(self.root / "main/collection_status.json", dict(status="complete", listing_request_calls=20))
        common.atomic_json(self.root / "detail/manifest_detail_enrichment.json",
                           dict(runs_by_stage={"one": {"detail_calls": 8}}))
        result = notify.build_notification("REF", self.root, status="failed", failed_step_name="detail_html")
        self.assertIn("298/300", result["subject"])
        self.assertIn("부분 완료", result["subject"])
        self.assertIn("A | 15 | 7 | detail | failed | 4 | 3 | timeout", result["body"])
        self.assertIn("실제 요청 횟수 | 응답 판정 횟수", result["body"])
        self.assertIn("실행 결과: failed", result["body"])
        self.assertNotIn("총 수집 0", result["body"])
        self.assertEqual(result["metrics"]["call_counts"]["total"], 28)

    def test_list_targets_main_blocks_before_loading_stale_rows(self):
        from bestbuy import step02_main_targets as targets
        common.atomic_json(self.root / "main/collection_status.json", dict(status="incomplete"))
        with patch.object(targets, "RUN_ROOT", self.root / "main"), patch.object(targets, "load_rows") as read:
            with self.assertRaises(common.CollectionIncomplete):
                targets.main()
        read.assert_not_called()

    def test_publication_rejects_changed_target_list_even_when_status_says_complete(self):
        path = self.root / "targets.csv"
        rows = [{"sku_id": "A", "main_rank": "1"}]
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        final = self.root / "final.csv"
        final.write_text("existing result", encoding="utf-8")
        common.atomic_json(self.root / "output/collection_status.json", dict(status="complete",
            target_csv=str(path), target_hash=common.fingerprint(rows), final_output_csv=str(final)))
        common.assert_ready(self.root)
        path.write_text("sku_id,main_rank\nB,1\n", encoding="utf-8")
        with self.assertRaisesRegex(common.CollectionIncomplete, "target_list_changed"):
            common.assert_ready(self.root)

    def test_listing_only_success_does_not_allow_stale_final_publication(self):
        common.atomic_json(self.root / "main/collection_status.json", dict(status="complete"))
        with self.assertRaisesRegex(common.CollectionIncomplete, "detail_not_collected"):
            common.assert_ready(self.root)
        common.assert_ready(self.root, listing="main")

    def test_output_write_failure_does_not_mark_collection_complete(self):
        self.install_detail()
        target = dict(sku_id="12345", main_rank="15", review_count="0")
        def post(payload, *args):
            return 200, "", [self.response(p) for p in payload], {}, 0
        with patch.object(detail, "browser_graphql_post", post), patch.object(detail, "write_csv", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        report = common.read_json(self.root / "output/collection_status.json")
        self.assertNotEqual(report["status"], "complete")
        with self.assertRaises(common.CollectionIncomplete):
            common.assert_ready(self.root)

    def test_observed_null_compare_response_is_retried_then_inserted_as_db_null(self):
        from bestbuy import step14_db_load as db
        from bestbuy import bestbuy_orchestrator as orchestrator
        self.install_detail()
        target = dict(sku_id="6672730", main_rank="251", review_count="0")
        calls = []
        def post(payloads, *args):
            calls.append(payloads)
            responses = [self.response(p) for p in payloads]
            for p, response in zip(payloads, responses):
                if p["operationName"] == "GetCompareProduct":
                    response["data"]["recommendations"]["subPlacements"] = None
            return 200, "", responses, {}, 0
        with patch.object(detail, "browser_graphql_post", post):
            report = recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        self.assertEqual([len(batch) for batch in calls], [3, 1])
        self.assertEqual(report["failures"][0]["null_columns"], ["retailer_sku_name_similar"])
        with patch.object(orchestrator, "run_root", return_value=self.root):
            self.assertTrue(orchestrator.detail_html_complete()[0])
            self.assertTrue(orchestrator.review20_complete()[0])
        with detail.FINAL_OUTPUT_CSV.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        cursor = Mock()
        with patch.object(db, "validate_insert_columns", return_value=[]), patch.object(db, "delete_existing_batch", return_value=0):
            db.insert_rows(cursor, "test_output", [("sku_id", "text"), ("final_sku_price", "text"),
                           ("retailer_sku_name_similar", "text")], rows)
        self.assertEqual(cursor.executemany.call_args.args[1], [("6672730", "$999", None)])
        for name in ("main", "bsr"):
            common.atomic_json(self.root / name / "collection_status.json", dict(status="incomplete", failed_page=2))
            with self.assertRaises(common.CollectionIncomplete):
                common.assert_ready(self.root)
            common.atomic_json(self.root / name / "collection_status.json", dict(status="complete"))

    def test_observed_short_review_is_null_but_review_total_and_prices_survive(self):
        self.install_detail()
        target = dict(sku_id="6668726", main_rank="16", bsr_rank="8", review_count="15")
        def post(payloads, *args):
            responses = [self.response(p) for p in payloads]
            for p, response in zip(payloads, responses):
                if p["operationName"] != "GetCompareProduct":
                    product = response["data"]["productBySkuId"]
                    product["reviewInfo"] = {"reviewCount": 15, "averageRating": 4.8}
                    product["reviews"]["results"] = [{"text": "Review " + str(i)} for i in range(14)]
            return 200, "", responses, {}, 0
        with patch.object(detail, "browser_graphql_post", post):
            report = recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        self.assertEqual(report["failures"][0]["reason"], "review_partial_14_of_15")
        self.assertEqual(report["failures"][0]["request_attempt"], 2)
        row = common.read_json(self.root / "output/partial_output.json")[0]
        self.assertIsNone(row["detailed_review_content"])
        self.assertEqual(row["count_of_reviews"], "15")
        self.assertEqual(row["final_sku_price"], "$999")
        self.assertIn("NULL 컬럼: detailed_review_content", common.recovery_notification("LDY", self.root)["body"])

    def test_observed_buying_options_error_preserves_listing_prices_and_nulls_review(self):
        self.install_detail()
        target = dict(sku_id="6563821", main_rank="114", review_count="514", customer_price="404.99",
                      regular_price="674.99", total_savings="270", product_name="Whirlpool washer")
        def post(payloads, *args):
            responses = [self.response(p) for p in payloads]
            for p, response in zip(payloads, responses):
                if p["operationName"] != "GetCompareProduct":
                    product = response["data"]["productBySkuId"]
                    product.pop("price")
                    product["buyingOptions"] = None
                    product["manufacturer"] = {"modelNumber": "WTW4957PW"}
                    product["reviewInfo"] = {"reviewCount": 514, "averageRating": 4.3}
                    product["reviews"]["results"] = [{"text": "Review " + str(i)} for i in range(20)]
                    response["errors"] = [{"path": ["productBySkuId", "buyingOptions"],
                                           "extensions": {"code": "NOT_FOUND"}}]
            return 200, "", responses, {}, 0
        with patch.object(detail, "CATEGORY", "LDY"), patch.object(detail, "browser_graphql_post", post):
            report = recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        self.assertEqual(report["status"], "complete_with_warnings")
        self.assertEqual({f["stage"] for f in report["failures"]}, {"detail", "review"})
        row = common.read_json(self.root / "output/partial_output.json")[0]
        self.assertEqual([row[k] for k in ("final_sku_price", "original_sku_price", "savings")],
                         ["$404.99", "$674.99", "$270"])
        self.assertIsNone(row["detailed_review_content"])
        note = common.recovery_notification("LDY", self.root)
        self.assertIn("productBySkuId.buyingOptions", note["body"])
        self.assertIn("detailed_review_content", note["body"])
        common.assert_ready(self.root)

    def test_failed_operation_recovers_on_second_request_without_retrying_successes(self):
        self.install_detail()
        target = dict(sku_id="12345", review_count="0")
        calls = []
        def post(payloads, *args):
            calls.append(payloads)
            return 200, "", [self.response(p, compare_failure=len(calls) == 1) for p in payloads], {}, 0
        with patch.object(detail, "browser_graphql_post", post):
            report = recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        self.assertEqual([len(batch) for batch in calls], [3, 1])
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["failures"], [])

    def test_malformed_and_wrong_sku_data_cannot_leak_into_final_output(self):
        self.install_detail()
        target = dict(sku_id="12345", product_name="Verified listing", main_rank="1", customer_price="12")
        def post(payloads, *args):
            responses = [self.response(p) for p in payloads]
            for item in responses:
                item["data"]["productBySkuId"]["skuId"] = "another-sku"
            return 200, "", responses, {}, 0
        with patch.object(detail, "browser_graphql_post", post):
            recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        row = common.read_json(self.root / "output/partial_output.json")[0]
        self.assertEqual(row["retailer_sku_name"], "Verified listing")
        self.assertEqual(row["final_sku_price"], "$12")
        self.assertIsNone(row["detailed_review_content"])
        self.assertIsNone(row["retailer_sku_name_similar"])
        with patch.object(detail, "browser_graphql_post", return_value=(200, "", [None, [], None], {}, 0)):
            result = recovery.run(detail, [target], [target], budget_factory=Clock().budget)
        self.assertEqual(result["status"], "complete_with_warnings")

    def test_missing_listing_blocks_detail_requests_and_warning_publication(self):
        self.install_detail()
        for name in ("main", "bsr"):
            common.atomic_json(self.root / name / "collection_status.json", dict(status="incomplete", failed_page=2))
            with patch.object(detail, "browser_graphql_post") as post:
                with self.assertRaises(common.CollectionIncomplete):
                    recovery.run(detail, [dict(sku_id="1")], [dict(sku_id="1")])
                post.assert_not_called()
            common.atomic_json(self.root / name / "collection_status.json", dict(status="complete"))
        common.atomic_json(self.root / "output/collection_status.json", dict(status="complete_with_warnings"))
        with self.assertRaisesRegex(common.CollectionIncomplete, "unverified_warning_output"):
            common.assert_ready(self.root)

    def test_boundary_duplicate_keeps_first_position_and_collects_rest_of_page(self):
        responses = []
        for page in range(1, 17):
            skus = [str((page - 1) * 18 + i) for i in range(1, 19)]
            if page == 15:
                skus[-1] = "12372812"
            elif page == 16:
                skus[0] = "12372812"
            graph = {"data": {"detailedProductSearch": {"documents": [{"product": {"skuId": sku}} for sku in skus]}}}
            rows = [dict(sku_id=sku, organic_rank=i, page=page, container_type="organic_product") for i, sku in enumerate(skus, 1)]
            responses.append((graph, {"status_code": 200}, rows))
        api, calls = self.fake_listing(iter(responses))
        api.SEARCH_PAGES, api.LISTING_ORGANIC_TARGET, api.ORGANIC_OFFSET = 16, 0, 18
        rows, _, _, report = listing.collect(api, {}, object(), budget_factory=Clock().budget)
        self.assertEqual(calls, list(range(1, 17)))
        self.assertEqual(report["accepted_pass"], 1)
        self.assertEqual(report["organic_count"], 287)
        self.assertEqual(rows[15][-1]["sku_id"], "12372812")
        self.assertEqual(rows[16][0]["organic_rank"], 2)
        self.assertEqual(len(rows[16]), 17)
        self.assertEqual(report["duplicates"], [dict(sku_id="12372812", first_page=15, first_position=18, page=16, position=1)])
        note = common.recovery_notification("REF", self.root)
        self.assertIn("15페이지 18번째 / 16페이지 1번째 중복, 첫 등장 채택", note["body"])

    def test_email_reports_warning_columns_and_actual_db_counts(self):
        from bestbuy import step16_email_notify as notify
        self.install_detail()
        common.atomic_json(self.root / "output/collection_status.json", dict(status="complete_with_warnings",
            finalization_ready=True, target_count=313, completed_skus=312, failures=[dict(sku_id="6672730",
                main_rank="251", stage="compare", status="failed", request_attempt=2, attempt=2,
                reason="recommendations_shape_missing", null_columns=["retailer_sku_name_similar"])]))
        common.atomic_json(self.root / "output/db_load_manifest.json", dict(dry_run=False,
            final_output=dict(inserted=313, csv_rows=313)))
        result = notify.build_notification("TV", self.root, status="success")
        self.assertIn("검수 필요", result["subject"])
        self.assertIn("DB 적재 기록: 신규 313개 / 갱신 0개", result["body"])
        self.assertIn("NULL 컬럼: retailer_sku_name_similar", result["body"])
        self.assertNotIn("collection_incomplete", result["issues"])
        common.atomic_json(self.root / "output/db_load_manifest.json", dict(dry_run=True,
            final_output=dict(inserted=313)))
        self.assertIn("모의 실행 — 실제 반영 아님", notify.build_notification("TV", self.root)["body"])

    def test_fullrun_review_step_does_not_start_a_second_retry_cycle(self):
        from bestbuy import bestbuy_orchestrator as orchestrator
        self.install_detail()
        common.atomic_json(self.root / "output/collection_status.json",
                           dict(status="complete_with_warnings", finalization_ready=True))
        with patch.object(orchestrator, "run_root", return_value=self.root), \
             patch.object(orchestrator, "apply_run_path_env"), patch.object(orchestrator.subprocess, "run") as execute:
            orchestrator.run_step(orchestrator.step_by_key("review20"))
        execute.assert_not_called()

    def test_listing_main_publishes_verified_combo_page_without_false_failed_page(self):
        from bestbuy import step01_main_list as production
        root = self.root / "main"
        rows = [dict(sku_id=str(i), container_type="organic_product" if i < 17 else "sponsored_ingrid") for i in range(23)]
        summary = dict(page=1, status_code=200, total_occurrence_count=23, attempt_count=1, x_request_cost=0)
        report = dict(status="validated", accepted_pass=1, listing_request_calls=1, offer_request_calls=0,
                      evidence_path="synthetic", organic_count=17)
        def make_dirs():
            (root / "parsed").mkdir(parents=True)
            (root / "benchmarks").mkdir(parents=True)
        with patch.multiple(production, RUN_ROOT=root, LISTING_COLLECTION_MODE="browser_graphql", SEARCH_PAGES=1,
                            LISTING_ORGANIC_TARGET=0, LISTING_PAGE_COMPLETE_MIN_ROWS=24), \
             patch.object(production, "make_dirs", make_dirs), \
             patch.object(production, "load_product_list_operation", return_value={"source_path": "fixture", "source_type": "payload"}), \
             patch.object(listing, "collect", return_value=({1: rows}, [summary], [{"page": 1, "meta": {}}], report)), \
             patch.dict(os.environ, {"ZENROWS_API_KEY": ""}):
            production.main()
        status = common.read_json(root / "collection_status.json")
        manifest = common.read_json(root / "manifest.json")
        self.assertEqual(status["status"], "complete")
        self.assertEqual(manifest["failed_pages"], [])
        common.assert_ready(self.root, listing="main")

    def test_listing_fetch_persists_chrome_diagnostics_with_original_error(self):
        from bestbuy import step01_main_list as production
        browser = types.SimpleNamespace(url="https://www.bestbuy.com/site/searchpage.jsp",
            run_js=Mock(side_effect=[json.dumps({"error": "TypeError: Failed to fetch"}),
                                    {"ready_state": "complete", "online": True}]),
            listen=Mock(listening=False))
        browser.listen.wait.return_value = [types.SimpleNamespace(is_failed=True,
            fail_info=types.SimpleNamespace(errorText="net::ERR_CONNECTION_RESET", canceled=False,
                                           blockedReason="", corsErrorStatus=None))]
        with patch.multiple(production, RUN_ROOT=self.root, BROWSER_GRAPHQL_NAVIGATE_EACH_PAGE=False):
            _, meta, rows = production.browser_graphql_fetch_once(1, {"query": "fixture"}, browser)
        self.assertEqual(meta["error"], "TypeError: Failed to fetch")
        self.assertEqual(meta["status_code"], "ERR")
        self.assertEqual(rows, [])
        disk_meta = json.loads(next(self.root.rglob("*_meta.json")).read_text(encoding="utf-8"))
        self.assertEqual(disk_meta["browser_diagnostics"]["network_events"][0]["errorText"],
                         "net::ERR_CONNECTION_RESET")
        self.assertTrue(disk_meta["browser_diagnostics"]["online"])

    def test_navigation_failure_preserves_exception_and_diagnostics(self):
        from bestbuy import step01_main_list as production
        original = TimeoutError("navigation timed out")
        browser = types.SimpleNamespace(url="https://www.bestbuy.com/", get=Mock(side_effect=original),
            run_js=Mock(return_value={"ready_state": "loading", "online": False}), listen=Mock(listening=False))
        browser.listen.wait.return_value = False
        with self.assertRaises(TimeoutError) as caught:
            production.initialize_browser_graphql_session(browser)
        self.assertIs(caught.exception, original)
        self.assertFalse(original.bestbuy_browser_diagnostics["online"])
        browser.listen.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
