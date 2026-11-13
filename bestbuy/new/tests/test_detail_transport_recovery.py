"""Offline REF/LDY outage, fallback, and same-run resume regression scenarios."""

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_collection_recovery as fixtures
from bestbuy import step00_collection_recovery as common
from bestbuy import step08_collection_recovery as recovery
from bestbuy import step08_detail_enrichment as detail
from bestbuy.step08_detail_transport_recovery import RecoverySettings


class DetailTransportRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixtures.RecoveryTests.setUp(self)
        fixtures.RecoveryTests.install_detail(self)
        patcher = patch.multiple(detail, CATEGORY="LDY", DETAIL_DIRECT_GRAPHQL=True,
                                 recover_detail_browser_session=Mock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = fixtures.Clock()
        self.requests = []
        self.request_times = []

    @staticmethod
    def targets(count=1):
        return [dict(sku_id=str(i), main_rank=str(i), product_name="Listing " + str(i),
                     customer_price="12", regular_price="15", total_savings="3", review_count="0")
                for i in range(1, count + 1)]

    @staticmethod
    def response(payload):
        item = fixtures.RecoveryTests.response(payload)
        item["data"]["productBySkuId"]["manufacturer"] = {"modelNumber": "MODEL-1"}
        return item

    def post(self, payloads, *args):
        self.requests.append(payloads)
        self.request_times.append(self.clock.now)
        return 200, "", [self.response(p) for p in payloads], {}, 0

    def run_collection(self, post=None, targets=None):
        targets = targets if targets is not None else self.targets()
        with patch.object(detail, "browser_graphql_post", post or self.post):
            return recovery.run(detail, targets, targets, budget_factory=self.clock.budget)

    def report(self):
        return common.read_json(self.root / "output/collection_status.json")

    def test_normal_ref_and_ldy_keep_five_sku_batches_without_delay(self):
        for category in ("REF", "LDY"):
            with self.subTest(category=category), patch.object(detail, "CATEGORY", category), patch.object(detail, "FORCE_REFRESH", True):
                self.requests.clear()
                report = self.run_collection(targets=self.targets(7))
                self.assertEqual([len(p) for p in self.requests], [15, 6])
                self.assertEqual(report["status"], "complete")
                self.assertEqual(self.clock.delays, [])
                detail.recover_detail_browser_session.assert_not_called()

    def test_failed_batch_recovers_single_operations_then_returns_to_batches(self):
        def post(payloads, *args):
            result = self.post(payloads)
            if len(self.requests) == 1:
                raise RuntimeError("net::ERR_HTTP2_PROTOCOL_ERROR")
            return result
        report = self.run_collection(post, self.targets(6))
        self.assertEqual([len(p) for p in self.requests], [15] + [1] * 15 + [3])
        self.assertEqual(report["status"], "complete")
        state = report["items"]["1"]["detail"]
        self.assertEqual((state["request_attempt"], state["attempt"], state["transport_attempts"]), (2, 1, 1))
        self.assertTrue(all(t2 - t1 >= 3 for t1, t2 in zip(self.request_times[:15], self.request_times[1:16])))
        self.assertLess(report["transport_recovery"]["recovery_seconds_used"], self.clock.now)

    def test_only_failed_review_retried_and_verified_empty_is_kept(self):
        def post(payloads, *args):
            result = self.post(payloads)
            if len(self.requests) == 1:
                result[2][1] = {"errors": [{"message": "temporarily unavailable"}]}
            return result
        report = self.run_collection(post)
        self.assertEqual([len(p) for p in self.requests], [3, 1])
        self.assertEqual(self.requests[1][0], self.requests[0][1])
        self.assertEqual(report["items"]["1"]["review"]["status"], "empty")
        self.assertEqual(report["items"]["1"]["compare"]["request_attempt"], 1)

    def test_outage_stops_before_next_chunk_and_blocks_stale_final_publication(self):
        detail.FINAL_OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        detail.FINAL_OUTPUT_CSV.write_text("old final", encoding="utf-8")
        def post(payloads, *args):
            self.post(payloads)
            raise RuntimeError("TypeError: Failed to fetch")
        with self.assertRaisesRegex(common.CollectionIncomplete, "recovery_attempt_limit"):
            self.run_collection(post, self.targets(30))
        report = self.report()
        self.assertEqual([len(p) for p in self.requests], [15, 1, 1, 1, 1, 1])
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["finalization_ready"])
        self.assertEqual(report["items"]["30"]["detail"]["request_attempt"], 0)
        self.assertEqual(report["items"]["1"]["detail"]["attempt"], 0)
        self.assertEqual(detail.FINAL_OUTPUT_CSV.read_text(encoding="utf-8"), "old final")
        detail.update_product_list_from_detail_rows.assert_not_called()
        detail.recover_detail_browser_session.assert_called_once()
        self.assertEqual([e["wait_seconds"] for e in report["recovery_history"] if e["event"] == "waiting"], [15, 30, 60])
        with self.assertRaises(common.CollectionIncomplete):
            common.assert_ready(self.root)
        row = common.read_json(detail.OUTPUT_ROOT / "partial_output.json")[0]
        self.assertEqual(row["final_sku_price"], "12")

    def test_probe_success_is_saved_without_duplicate_request(self):
        def post(payloads, *args):
            result = self.post(payloads)
            if len(self.requests) <= 3:
                raise RuntimeError("offline")
            return result
        report = self.run_collection(post)
        self.assertEqual([len(p) for p in self.requests], [3, 1, 1, 1, 1, 1])
        self.assertEqual(self.requests[3][0], self.requests[0][0])
        # Detail and review may share the same ProductSchema query; check logical operations.
        events = [json.loads(line) for line in (Path(report["evidence_path"]) / "events.jsonl")
                  .read_text(encoding="utf-8").splitlines()]
        calls = [e for e in events if e["event"] == "request"]
        self.assertEqual(calls[3]["request_mode"], "probe")
        self.assertEqual([e["targets"][0]["operation"] for e in calls[3:]], ["detail", "review", "compare"])
        self.assertEqual(report["items"]["1"]["detail"]["attempt"], 1)
        self.assertEqual(report["status"], "complete")
        detail.recover_detail_browser_session.assert_not_called()

    def test_recurrent_batch_failure_latches_single_mode(self):
        def post(payloads, *args):
            result = self.post(payloads)
            if len(payloads) > 1:
                raise RuntimeError("batch failed")
            return result
        report = self.run_collection(post, self.targets(11))
        self.assertEqual([len(p) for p in self.requests].count(15), 2)
        self.assertEqual([len(p) for p in self.requests][-3:], [1, 1, 1])
        self.assertTrue(report["transport_recovery"]["single_mode"])
        self.assertEqual(report["status"], "complete")

    def test_recurrent_circuit_retains_wait_index_and_one_failed_restart(self):
        results = iter([False, False, False, True, True, True,
                        False, False, False, True, True, True])
        def post(payloads, *args):
            result = self.post(payloads)
            if not next(results):
                raise RuntimeError("offline")
            return result
        detail.recover_detail_browser_session.side_effect = OSError("browser disconnected")
        with patch.object(detail, "DETAIL_SKU_BATCH_SIZE", 1):
            report = self.run_collection(post, self.targets(2))
        self.assertEqual(report["status"], "complete")
        self.assertEqual([e["wait_seconds"] for e in report["recovery_history"] if e["event"] == "waiting"], [15, 30, 60])
        detail.recover_detail_browser_session.assert_called_once()
        self.assertEqual(report["transport_recovery"]["recovery_attempts"], 3)
        self.assertLessEqual(report["transport_recovery"]["recovery_seconds_used"], 180)

    def test_wait_cannot_overrun_remaining_recovery_budget(self):
        def post(payloads, *args):
            self.post(payloads)
            self.clock.now += 55
            raise RuntimeError("offline")
        with self.assertRaisesRegex(common.CollectionIncomplete, "recovery_time_limit"):
            self.run_collection(post)
        self.assertLessEqual(self.clock.now, 180)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.report()["status"], "partial")

    def test_healthy_slow_collection_does_not_use_recovery_budget(self):
        def post(payloads, *args):
            result = self.post(payloads)
            if len(self.requests) == 1:
                raise RuntimeError("batch failed")
            self.clock.now += 20
            return result
        report = self.run_collection(post, self.targets(6))
        self.assertGreater(self.clock.now, 180)
        self.assertLess(report["transport_recovery"]["recovery_seconds_used"], 30)
        self.assertEqual(report["status"], "complete")

    def test_recovery_time_limit_and_success_received_at_deadline_are_checkpointed(self):
        def post(payloads, *args):
            result = self.post(payloads)
            if len(self.requests) == 1:
                raise RuntimeError("offline")
            self.clock.now += 180
            return result
        with self.assertRaisesRegex(common.CollectionIncomplete, "recovery_time_limit"):
            self.run_collection(post)
        report = self.report()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(report["items"]["1"]["detail"]["status"], "success")
        self.assertEqual(report["status"], "partial")

    def test_validation_attempts_survive_transport_failure_and_errors_only_dict(self):
        def post(payloads, *args):
            self.post(payloads)
            if len(self.requests) in (1, 2):
                raise RuntimeError("offline")
            errors = {"errors": [{"extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]}
            return 200, "", errors, {}, 0
        report = self.run_collection(post)
        self.assertEqual(report["status"], "complete_with_warnings")
        first = report["items"]["1"]["detail"]
        self.assertEqual((first["request_attempt"], first["attempt"]), (4, 2))
        self.assertEqual(first["failure_kind"], "validation")
        self.assertEqual(report["transport_recovery"]["recovery_attempts"], 0)
        common.assert_ready(self.root)

    def test_resume_after_interrupt_only_requests_unfinished_operations(self):
        def post(payloads, *args):
            result = self.post(payloads)
            if len(self.requests) == 1:
                result[2][1] = {"errors": [{"message": "review unavailable"}]}
            else:
                raise KeyboardInterrupt()
            return result
        with self.assertRaises(KeyboardInterrupt):
            self.run_collection(post)
        self.assertEqual(self.report()["status"], "partial")
        review_payload = self.requests[0][1]
        paths = detail.detail_paths("1")
        saved = paths["apollo"].read_bytes()
        self.requests.clear()
        report = self.run_collection()
        self.assertEqual([len(p) for p in self.requests], [1])
        self.assertEqual(self.requests[0][0], review_payload)
        self.assertEqual(report["items"]["1"]["review"]["request_attempt"], 1)
        self.assertEqual(paths["apollo"].read_bytes(), saved)
        self.assertTrue(report["items"]["1"]["detail"]["reused"])
        self.assertEqual(report["status"], "complete")

    def test_transport_partial_resume_preserves_successful_skus(self):
        def post(payloads, *args):
            result = self.post(payloads)
            if len(self.requests) > 1:
                raise RuntimeError("offline")
            return result
        with self.assertRaises(common.CollectionIncomplete):
            self.run_collection(post, self.targets(6))
        self.requests.clear()
        report = self.run_collection(targets=self.targets(6))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual({p["variables"]["skuId"] for p in self.requests[0]}, {"6"})
        self.assertEqual(report["status"], "complete")

    def test_resume_retains_single_mode_but_gets_new_bounded_recovery_allowance(self):
        def post(payloads, *args):
            result = self.post(payloads)
            if len(payloads) > 1:
                raise RuntimeError("batch failed")
            if payloads[0]["variables"]["skuId"] == "3":
                raise KeyboardInterrupt()
            return result
        with patch.object(detail, "DETAIL_SKU_BATCH_SIZE", 1):
            with self.assertRaises(KeyboardInterrupt):
                self.run_collection(post, self.targets(3))
            self.assertTrue(self.report()["transport_recovery"]["single_mode"])
            self.requests.clear()
            report = self.run_collection(targets=self.targets(3))
        self.assertEqual([len(p) for p in self.requests], [1, 1, 1])
        self.assertEqual({p["variables"]["skuId"] for batch in self.requests for p in batch}, {"3"})
        self.assertTrue(report["transport_recovery"]["single_mode"])
        self.assertEqual(report["transport_recovery"]["recovery_seconds_used"], 0)
        self.assertEqual(report["status"], "complete")

    def test_http_error_and_invalid_batch_shape_do_not_spend_validation_attempts(self):
        for code, body in ((503, {}), (200, [])):
            with self.subTest(code=code), patch.object(detail, "FORCE_REFRESH", True):
                self.requests.clear()
                def post(payloads, *args):
                    result = self.post(payloads)
                    return (code, "", body, {}, 0) if len(self.requests) == 1 else result
                report = self.run_collection(post)
                self.assertEqual(report["status"], "complete")
                self.assertEqual(report["items"]["1"]["detail"]["attempt"], 1)
                self.assertEqual(report["items"]["1"]["detail"]["transport_attempts"], 1)

    def test_changed_targets_cannot_reuse_success_and_stale_date_is_rejected(self):
        self.run_collection()
        self.requests.clear()
        target = self.targets()[0]
        target["main_rank"] = "2"
        self.run_collection(targets=[target])
        self.assertEqual([len(p) for p in self.requests], [3])
        report = self.report()
        report["collected_date"] = "2000-01-01"
        common.atomic_json(detail.OUTPUT_ROOT / "collection_status.json", report)
        self.requests.clear()
        with self.assertRaisesRegex(common.CollectionIncomplete, "stale_run"):
            self.run_collection(targets=[target])
        self.assertEqual(self.requests, [])

    def test_restore_browser_owner_even_after_fatal_error(self):
        original = detail.BROWSER_GRAPHQL_MAX_RECOVERIES
        with self.assertRaises(ValueError):
            self.run_collection(Mock(side_effect=ValueError("unexpected bug")))
        self.assertFalse(detail.BROWSER_GRAPHQL_RECOVERY_MANAGED)
        self.assertIsNone(detail.BROWSER_GRAPHQL_RECOVERY_REMAINING)
        self.assertEqual(detail.BROWSER_GRAPHQL_MAX_RECOVERIES, original)

    def test_invalid_settings_are_rejected(self):
        for key, value in (("SECONDS", "nan"), ("MAX_RESTARTS", "2"), ("DELAYS", "15,-1")):
            with self.subTest(key=key), patch.dict("os.environ", {"BESTBUY_DETAIL_RECOVERY_" + key: value}):
                with self.assertRaises(ValueError):
                    RecoverySettings.from_env()


if __name__ == "__main__":
    unittest.main()
