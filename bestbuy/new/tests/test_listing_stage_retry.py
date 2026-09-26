import io
import json
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from bestbuy import step00_collection_recovery as common
from bestbuy import step01_listing_recovery as listing
from bestbuy import step01_stage_retry as retry


class ListingStageRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "main"
        self.command = ["python", "-m", "bestbuy.step01_main_list"]
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def write_failure(self, invocation="new", reason="recovery_attempt_limit", retryable=True):
        report = dict(status="incomplete", evidence_path=invocation, reason=reason,
                      last_failure=dict(reason="TypeError: Failed to fetch", retryable=retryable))
        common.atomic_json(self.root / "collection_status.json", report)

    def events(self):
        return [json.loads(line) for line in (self.root / "stage_retry_events.jsonl").read_text(
            encoding="utf-8").splitlines()]

    def test_exact_delays_and_four_executions_then_failure(self):
        def fail(*args, **kwargs):
            self.write_failure(str(execute.call_count))
            raise subprocess.CalledProcessError(1, self.command)
        execute = Mock(side_effect=fail)
        with patch.object(retry.time, "sleep") as sleep, self.assertRaises(subprocess.CalledProcessError):
            retry.run_listing_step(self.command, {}, self.root, execute=execute)
        self.assertEqual(execute.call_count, 4)
        self.assertEqual([e["wait_seconds"] for e in self.events() if e["event"] == "waiting"], [600, 900, 1200])
        self.assertEqual(sum(call.args[0] for call in sleep.call_args_list), 2700)
        self.assertEqual(self.events()[-1]["event"], "exhausted")
        self.assertTrue(all(call.args[0] == self.command for call in execute.call_args_list))

    def test_success_after_one_failure_continues_without_further_retries(self):
        def execute(*args, **kwargs):
            if not (self.root / "collection_status.json").exists():
                self.write_failure()
                raise subprocess.CalledProcessError(1, self.command)
            return "completed"
        execute = Mock(side_effect=execute)
        with patch.object(retry.time, "sleep"):
            self.assertEqual(retry.run_listing_step(self.command, {}, self.root, execute=execute), "completed")
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(self.events()[-1]["event"], "recovered")

    def test_stale_missing_and_nonretryable_reports_do_not_restart(self):
        cases = ("missing", "stale", "invalid_request", "disk_full")
        for case in cases:
            with self.subTest(case=case):
                self.root = Path(self.temp.name) / case
                if case == "stale":
                    self.write_failure("old")
                def fail(*args, **kwargs):
                    if case == "invalid_request":
                        self.write_failure(reason="permanent_graphql_error")
                    elif case == "disk_full":
                        self.write_failure(reason="disk full")
                    raise subprocess.CalledProcessError(1, self.command)
                execute = Mock(side_effect=fail)
                with patch.object(retry.time, "sleep") as sleep, self.assertRaises(subprocess.CalledProcessError):
                    retry.run_listing_step(self.command, {}, self.root, execute=execute)
                self.assertEqual(execute.call_count, 1)
                sleep.assert_not_called()

    def test_ctrl_c_during_wait_stops_immediately(self):
        def fail(*args, **kwargs):
            self.write_failure()
            raise subprocess.CalledProcessError(1, self.command)
        execute = Mock(side_effect=fail)
        with patch.object(retry.time, "sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            retry.run_listing_step(self.command, {}, self.root, execute=execute)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(self.events()[-1]["event"], "interrupted")

    def test_native_interrupt_exit_never_retries(self):
        for code in retry.INTERRUPT_CODES:
            with self.subTest(code=code):
                execute = Mock(side_effect=subprocess.CalledProcessError(code, self.command))
                with patch.object(retry.time, "sleep") as sleep, self.assertRaises(subprocess.CalledProcessError):
                    retry.run_listing_step(self.command, {}, self.root, execute=execute)
                execute.assert_called_once()
                sleep.assert_not_called()

    def test_only_transient_failures_are_classified_for_delayed_restart(self):
        cases = [
            ({"status_code": "ERR"}, "TypeError: Failed to fetch", True),
            ({"status_code": "ERR", "exception_type": "TimeoutError"}, "deadline", True),
            ({"status_code": "ERR"}, "NameError: unknown variable", False),
            ({"status_code": 503}, "http_503", True),
            ({"status_code": 429}, "http_429", True),
            ({"status_code": 400}, "http_400", False),
            ({"status_code": 403}, "http_403", False),
            ({"status_code": 200}, "parsed_positions_mismatch", False),
            ({"status_code": 200, "parse_error": "bad JSON"}, "bad JSON", False),
        ]
        for meta, reason, expected in cases:
            with self.subTest(reason=reason):
                self.assertIs(retry.listing_failure({}, meta, reason)["retryable"], expected)
        for code, expected in [("INTERNAL_SERVER_ERROR", True), ("BAD_USER_INPUT", False)]:
            graph = {"errors": [{"extensions": {"code": code}}]}
            self.assertIs(retry.listing_failure(graph, {"status_code": 200}, "graphql_listing_error")["retryable"], expected)

    def test_real_inner_recovery_exhausts_then_outer_retry_recovers(self):
        # Replay September 25: five opaque fetch failures, followed by a successful new process.
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        def budget(evidence):
            return common.RecoveryBudget(evidence, clock=lambda: clock[0], sleep=sleep)
        api = types.SimpleNamespace(
            RUN_ROOT=self.root, SEARCH_TERM="tv", SEARCH_SORT="", SEARCH_PAGES=1,
            LISTING_ORGANIC_TARGET=0, LISTING_MAX_PAGES=1, ORGANIC_OFFSET=18,
            prepare_product_list_payload=lambda op, page: {"page": page},
            browser_graphql_local_port=lambda: 1234, page_summary=lambda *args: {},
            create_browser_graphql_page=Mock(return_value=object()),
            initialize_browser_graphql_session=Mock(), close_browser_graphql_page=Mock())
        failed = ({}, {"status_code": "ERR", "error": "TypeError: Failed to fetch"}, [])
        success = ({"data": {"detailedProductSearch": {"documents": [{"product": {"skuId": "1"}}]}}},
                   {"status_code": 200}, [{"sku_id": "1", "organic_rank": 1, "container_type": "organic_product"}])
        api.browser_graphql_fetch_once = Mock(side_effect=[failed] * 5 + [success])
        def execute(*args, **kwargs):
            try:
                return listing.collect(api, {}, None, budget_factory=budget)
            except common.CollectionIncomplete:
                raise subprocess.CalledProcessError(1, self.command) from None
        with patch.object(retry.time, "sleep"):
            _, _, _, report = retry.run_listing_step(self.command, {}, self.root, execute=execute)
        self.assertEqual(report["status"], "validated")
        self.assertEqual(api.browser_graphql_fetch_once.call_count, 6)
        failures = [e for e in self.events() if e["event"] == "attempt_failed"]
        self.assertEqual(failures[0]["reason"], "recovery_attempt_limit")
        self.assertEqual(failures[0]["last_failure"]["reason"], "TypeError: Failed to fetch")
        self.assertTrue(failures[0]["retryable"])
        self.assertEqual(clock[0], 1050)

    def test_orchestrator_routes_only_listing_steps_through_retry(self):
        from bestbuy import bestbuy_orchestrator as orchestrator
        with patch.object(orchestrator, "run_root", return_value=Path(self.temp.name)), \
             patch.object(retry, "run_listing_step") as run_listing, \
             patch.object(orchestrator.subprocess, "run") as execute, \
             patch.object(common, "assert_ready"):
            for key in ("01", "03"):
                orchestrator.run_step(orchestrator.step_by_key(key))
            self.assertEqual([c.args[2].name for c in run_listing.call_args_list], ["main", "bsr"])
            execute.assert_not_called()
            orchestrator.run_step(orchestrator.step_by_key("15"))
            self.assertEqual(run_listing.call_count, 2)
            execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
