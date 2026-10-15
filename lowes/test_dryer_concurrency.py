"""Offline concurrent-transport, isolation, checkpoint and output regression tests."""
import asyncio
from collections import Counter
import copy
import json
import logging
from pathlib import Path
import re
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from .lowes_dryer import BrowserAPI, run_collection
from .step00_dryer import Settings, SettingsError, detail_product
from .step00_dryer_audit import read_records
from .step00_dryer_concurrency import START_FETCH, POLL_FETCH, STOP_FETCH, browser_fetch, run_jobs
from .test_dryer import card, detail, operations, scratch
from .lowes_dryer_check import check_run


class OfflineDriver:
    """Simulate Selenium's async browser mailbox, with deliberately reversed I/O."""
    def __init__(self, main, bsr, plan=None, delay=True, interrupt=None):
        self.main, self.bsr = main, bsr
        self.plan = copy.deepcopy(plan or {})
        self.delay, self.interrupt = delay, interrupt
        self.pending, self.calls, self.events = {}, Counter(), []
        self.owner_thread = threading.get_ident()
        self.region = None
        self.max_requests = 0

    def response(self, path):
        parsed = urlsplit(path)
        offset = int(parse_qs(parsed.query).get("offset", [0])[0])
        if parsed.path == "/search/products":
            return {"status": 200, "payload": {"itemList": self.main[offset:offset+24],
                "productCount": len(self.main), "adjustedNextOffset": offset+24}}
        if parsed.path == "/best-sellers/node/bspl":
            return {"status": 200, "payload": {"itemList": self.bsr[offset:offset+24]}}
        parts = parsed.path.split("/")
        label, item, store = ("detail", parts[2], parts[4]) if parts[1] == "wpd" else ("reviews", parts[4], "")
        self.calls[label, item, store] += 1
        status = self.plan.get((label, item, store), [200])
        code = status.pop(0) if len(status) > 1 else status[0]
        if code == "timeout":
            return {"status": "network", "reason": "request_timeout"}
        if code == "bad_json":
            return {"status": 200, "reason": "non_json_response"}
        if code != 200:
            return {"status": code, "reason": "http_error"}
        number = int(item) - 1000
        if label == "detail":
            payload = detail(item, specs=[
                {"key": "Dryer Load Type", "value": "Top load" if number % 2 else "Front load"},
                {"key": "Dryer Capacity (Cu. Feet)", "value": str(3 + number / 10)}])
            product = payload["productDetails"][item]["product"]
            product["omniItemId"] = item
            payload["productDetails"][item]["mfePrice"]["price"].update(
                additionalData={"sellingPrice": 500+number, "wasPrice": 700+number},
                savings={"totalSaving": 200})
        else:
            payload = {"reviewStatistics": {"averageOverallRating": round(number / 10, 1),
                                              "totalReviewCount": number * 10}}
        return {"status": 200, "payload": payload}

    def execute_async_script(self, script, path, timeout):
        assert threading.get_ident() == self.owner_thread
        assert not self.pending, "sync browser request overlapped a concurrent pool"
        return self.response(path)

    def execute_script(self, script, *args):
        assert threading.get_ident() == self.owner_thread, "Selenium used from another thread"
        request_id = args[0]
        if script == START_FETCH:
            _, path, _ = args
            parts = urlsplit(path).path.split("/")
            label, item = ("detail", parts[2]) if parts[1] == "wpd" else ("reviews", parts[4])
            if label == "detail":
                assert parts[4] == self.region, "shared region changed during a request"
            if (label, item) == self.interrupt:
                raise KeyboardInterrupt()
            wait = 0.12 if self.delay and item == "1000" else 0
            response = self.response(path)
            self.pending[request_id] = (time.monotonic()+wait, response, label, item)
            self.events.append(("start", label, item))
            self.max_requests = max(self.max_requests, len(self.pending))
            return True
        if script == POLL_FETCH:
            ready, response, label, item = self.pending[request_id]
            if time.monotonic() < ready:
                return None
            del self.pending[request_id]
            self.events.append(("done", label, item))
            return response
        if script == STOP_FETCH:
            self.pending.pop(request_id, None)
            return None
        raise AssertionError("unexpected browser script")


class OfflineBrowser(BrowserAPI):
    def __init__(self, settings, driver):
        super().__init__(settings, logging.getLogger("lowes.dryer.concurrent-test"))
        self.driver = driver

    def open(self, url):
        assert not self.driver.pending

    def set_region(self, store, alternate=False):
        assert not self.driver.pending, "store override did not drain other requests"
        self.active_store = self.driver.region = str(store).zfill(4)
        self.driver.events.append(("region", self.active_store))


def collect(root, count=7, concurrency=2, main=None, bsr=None, **driver_options):
    main = main if main is not None else [card(i, price=False) for i in range(1000, 1000+count)]
    bsr = bsr if bsr is not None else list(reversed(copy.deepcopy(main)))
    settings = Settings(main_limit=count, bsr_limit=len(bsr), detail_concurrency=concurrency,
                        attempts=2, timeout=1, retry_wait=0, page_wait=0, seed_wait=0,
                        alt_store_enabled=driver_options.pop("alt_store_enabled", False))
    driver = OfflineDriver(main, bsr, **driver_options)
    api = OfflineBrowser(settings, driver)
    rows, summary = run_collection(api, settings, api.logger, root, "l_test",
                                   "2026-10-14 10:00:00", operations(count))
    return rows, summary, driver


class ConcurrentDryerTests(unittest.TestCase):
    def test_setting_default_override_and_reject_unsafe_limit(self):
        self.assertEqual(Settings().detail_concurrency, 2)
        self.assertEqual(Settings.from_env({"LOWES_DRYER_DETAIL_CONCURRENCY": "1"}).detail_concurrency, 1)
        for value in ("0", "3", "bad"):
            with self.assertRaises(SettingsError):
                Settings.from_env({"LOWES_DRYER_DETAIL_CONCURRENCY": value})
        bat = Path(__file__).with_name("lowes_dryer_daily_task.bat").read_text(encoding="utf-8")
        self.assertIn('if not defined LOWES_DRYER_DETAIL_CONCURRENCY set "LOWES_DRYER_DETAIL_CONCURRENCY=2"', bat)

    def test_1_2_3_and_7_products_complete_with_isolated_values_and_order(self):
        for count in (1, 2, 3, 7):
            with self.subTest(count=count), scratch() as root:
                rows, summary, driver = collect(root, count)
                self.assertEqual(len(rows), count)
                self.assertEqual([r["item"] for r in rows], [str(i) for i in range(1000, 1000+count)])
                self.assertEqual(summary["collection_status"], "complete")
                self.assertLessEqual(driver.max_requests, 2)
                records = {r["item"]: r for r in read_records(root/"audit/value_audit.jsonl")}
                for row in rows:
                    number = int(row["item"])-1000
                    self.assertEqual(row["sku"], "MODEL"+row["item"])
                    self.assertEqual(row["loading_type"], "Top load" if number % 2 else "Front load")
                    self.assertEqual(row["capacity"], f"{3+number/10:g} Cu.Feet")
                    self.assertEqual(row["count_of_reviews"], number*10)
                    self.assertEqual(row["final_sku_price"], f"${500+number}")
                    self.assertEqual(records[row["item"]]["final_values"], row)
                    self.assertEqual(records[row["item"]]["source_values"]["productdetail"]["modelId"], row["sku"])
                    self.assertEqual(driver.calls["detail", row["item"], "0289"], 1)
                self.assertTrue(check_run(root)["value_audit"]["audit_csv_match"])
                self.assertFalse(driver.pending)

    def test_fast_completion_refills_slot_before_slow_product_finishes(self):
        with scratch() as root:
            _, _, driver = collect(root, count=7)
            self.assertEqual(driver.max_requests, 2)
            self.assertLess(driver.events.index(("start", "detail", "1002")),
                            driver.events.index(("done", "detail", "1000")))

    def test_progress_counts_completed_products_once_despite_reverse_completion(self):
        with scratch() as root, self.assertLogs("lowes.dryer.concurrent-test", level="INFO") as captured:
            collect(root, count=7)
        progress = [line for line in captured.output if "[상세]" in line]
        self.assertEqual([int(re.search(r"\[상세\] (\d+)/7", line)[1]) for line in progress], list(range(1, 8)))
        self.assertEqual(len({re.search(r"item=(\d+)", line)[1] for line in progress}), 7)
        self.assertIn("100.0%", progress[-1])

    def test_sequential_and_concurrent_outputs_and_raw_values_match(self):
        runs = []
        for concurrency in (1, 2):
            with scratch() as root:
                main = [card(i, price=False) for i in range(1000, 1007)]
                bsr = [card(i, price=False) for i in (1006, 1005, 1007)]
                rows, _, _ = collect(root, main=main, bsr=bsr, concurrency=concurrency)
                records = {r["item"]: r for r in read_records(root/"audit/value_audit.jsonl")}
                runs.append((rows, records))
        self.assertEqual(*runs)
        by_id = {r["item"]: r for r in runs[0][0]}
        self.assertEqual((by_id["1006"]["main_rank"], by_id["1006"]["bsr_rank"]), (7, 1))
        self.assertIsNone(by_id["1007"]["main_rank"])

    def test_retry_timeout_and_one_failure_do_not_contaminate_other_products(self):
        plan = {("detail", "1000", "0289"): [429, 200],
                ("detail", "1001", "0289"): ["timeout"],
                ("reviews", "1001", ""): ["timeout"],
                ("reviews", "1002", ""): ["bad_json", 200]}
        with scratch() as root:
            rows, summary, driver = collect(root, count=7, plan=plan)
            self.assertEqual(len(rows), 7)
            self.assertEqual(summary["api_failure_products"], 1)
            self.assertEqual(summary["collection_status"], "partial_failure")
            records = {r["item"]: r for r in read_records(root/"audit/value_audit.jsonl")}
            self.assertEqual(records["1000"]["api"]["productdetail"]["attempts"], 2)
            self.assertEqual(records["1002"]["api"]["reviews"]["attempts"], 2)
            failed = next(r for r in rows if r["item"] == "1001")
            self.assertIsNone(failed["capacity"])
            self.assertEqual(failed["sku"], "MODEL1001")
            self.assertEqual(failed["count_of_reviews"], 7)
            for item in ("1003", "1004", "1005", "1006"):
                self.assertEqual(records[item]["api"]["productdetail"]["attempts"], 1)
            self.assertLessEqual(driver.max_requests, 2)
            self.assertTrue(check_run(root)["value_audit"]["audit_csv_match"])

    def test_alternate_region_waits_for_inflight_requests_and_restores(self):
        plan = {("detail", "1001", "0289"): [403],
                ("detail", "1002", "0289"): [403]}
        with scratch() as root:
            _, summary, driver = collect(root, count=7, plan=plan, alt_store_enabled=True)
            self.assertEqual(summary["api_failure_products"], 0)
            self.assertEqual(driver.region, "0289")
            self.assertEqual(driver.calls["detail", "1001", "1854"], 1)
            self.assertEqual(driver.calls["detail", "1002", "1854"], 1)
            self.assertFalse(driver.pending)
            self.assertEqual(len(json.loads((root/"audit/recovered_requests.json").read_text())), 2)

    def test_interruption_preserves_finished_product_and_cancels_other_request(self):
        main = [card(i, price=False) for i in range(1000, 1007)]
        driver = OfflineDriver(main, list(reversed(main)), interrupt=("reviews", "1006"))
        settings = Settings(main_limit=7, bsr_limit=7, detail_concurrency=2,
                            page_wait=0, seed_wait=0, retry_wait=0, alt_store_enabled=False)
        api = OfflineBrowser(settings, driver)
        with scratch() as root:
            with self.assertRaises(KeyboardInterrupt):
                run_collection(api, settings, api.logger, root, "l_test", "2026-10-14 10:00:00", operations(7))
            records = read_records(root/"audit/value_audit.jsonl")
            self.assertTrue(any(r["status"] == "provisional" for r in records))
            self.assertFalse((root/"output/final_output.csv").exists())
            self.assertFalse(driver.pending)
            self.assertEqual(len({r["item"] for r in records}), len(records))

    def test_explicit_wrong_product_id_is_rejected(self):
        payload = detail("1000")
        payload["productDetails"]["1000"]["product"]["omniItemId"] = "1001"
        with self.assertRaisesRegex(ValueError, "detail_product_id_mismatch"):
            detail_product(payload, "1000")

    def test_stalled_browser_poll_is_bounded_and_request_is_cancelled(self):
        driver = MagicMock()
        driver.execute_script.side_effect = [True, None, None]
        clock = SimpleNamespace(monotonic=MagicMock(side_effect=[0, 10]))
        with patch("lowes.step00_dryer_concurrency.time", clock):
            result = asyncio.run(browser_fetch(driver, "/offline", 1))
        self.assertEqual(result, {"status": "network", "reason": "request_timeout"})
        self.assertEqual(driver.execute_script.call_args.args[0], STOP_FETCH)

    def test_browser_exception_message_is_not_copied_to_logs_or_results(self):
        driver = MagicMock()
        driver.execute_script.side_effect = [RuntimeError("PRIVATE_ERROR_PAYLOAD"), None]
        result = asyncio.run(browser_fetch(driver, "/offline", 1))
        self.assertEqual(result["reason"], "RuntimeError")
        self.assertNotIn("PRIVATE_ERROR_PAYLOAD", json.dumps(result))

    def test_scheduler_empty_odd_and_callback_exception_cleanup(self):
        for count in (0, 1, 2, 3, 9):
            active, maximum, complete = 0, 0, []
            async def job(source):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                try:
                    await asyncio.sleep(0.001 if source % 2 else 0.002)
                    return source*10
                finally:
                    active -= 1
            run_jobs(range(count), 2, job, lambda source, result: complete.append((source, result)))
            self.assertEqual(sorted(complete), [(i, i*10) for i in range(count)])
            self.assertLessEqual(maximum, 2)
            self.assertEqual(active, 0)
        def fail(source, result):
            raise ValueError("test_writer_failure")
        with self.assertRaisesRegex(ValueError, "test_writer_failure"):
            run_jobs(range(3), 2, job, fail)
        self.assertEqual(active, 0)


if __name__ == "__main__":
    unittest.main()
