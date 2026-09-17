"""Offline checks of the live runner/report; fixtures never enter live mode."""

import copy
import importlib.util
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from test_offer_graphql import Replay, collect, product, api
from test_offer_pipeline import listing, targets, final
from bestbuy import step08_detail_enrichment as detail

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offer_diagnostic", ROOT / "diagnose_graphql_offers.py")
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


def audit(rows, saved=None, outputs=None):
    saved = rows if saved is None else saved
    outputs = [{"sku_id": row["sku_id"], "offer": api.graphql_offer_count(row)} for row in saved] if outputs is None else outputs
    return diagnostic.audit_rows(rows, saved, outputs, api.offer_evidence)


def page(complete=True):
    return {"page": 1, "status_code": 200, "rows": 3, "listing_complete": True,
            "offer_complete": complete, "offer_reason": "fixture", "offer_requests": 3,
            "zip_code": "10010", "navigate_each_page": 0}


class LiveDiagnosticTests(unittest.TestCase):
    def test_manual_review_saves_opens_waits_then_closes_browser(self):
        events = []
        args = types.SimpleNamespace(category="REF", open_report=True, keep_browser=True)
        summary = {"category": "REF", "started_at": "fixture", "collection_status": "failed"}
        browser = object()
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            output = Path(folder)
            def opened(path):
                self.assertTrue(Path(path).exists())
                events.append("open_report")
            def wait(prompt):
                self.assertTrue((output / "summary.json").exists())
                self.assertEqual(events, ["open_report"])
                events.append("wait_for_enter")
                return ""
            def closed(actual):
                self.assertIs(actual, browser)
                events.append("close_browser")
            with patch.object(diagnostic.os, "startfile", side_effect=opened, create=True), patch("builtins.input", side_effect=wait):
                diagnostic.finish_review(output, summary, [], args, browser, closed, io.StringIO())
        self.assertEqual(events, ["open_report", "wait_for_enter", "close_browser"])

    def test_default_interrupted_and_cancelled_reviews_close_browser(self):
        for keep, state, interruption in ((False, "passed", None), (True, "interrupted", None),
                                           (True, "failed", KeyboardInterrupt), (True, "failed", EOFError)):
            with self.subTest(keep=keep, state=state, interruption=interruption), tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
                args = types.SimpleNamespace(category="REF", open_report=False, keep_browser=keep)
                summary = {"category": "REF", "started_at": "fixture", "collection_status": state}
                with patch("builtins.input", side_effect=interruption) as wait, patch.object(listing, "close_browser_graphql_page") as close:
                    browser = object()
                    diagnostic.finish_review(Path(folder), summary, [], args, browser, close, io.StringIO())
                    close.assert_called_once_with(browser)
                    self.assertEqual(wait.call_count, 1 if interruption else 0)

    def test_report_failure_still_closes_browser(self):
        args = types.SimpleNamespace(category="REF", open_report=False, keep_browser=True)
        with patch.object(diagnostic, "write_reports", side_effect=OSError("disk full")), patch.object(listing, "close_browser_graphql_page") as close:
            browser = object()
            with self.assertRaises(OSError):
                diagnostic.finish_review(Path("unused"), {}, [], args, browser, close, io.StringIO())
            close.assert_called_once_with(browser)

    def test_report_links_preserve_exact_page_and_zip(self):
        url = "https://www.bestbuy.com/site/searchpage.jsp?st=refrigerator&cp=2&intl=nosplash"
        summary = {"category": "REF", "started_at": "fixture", "collection_status": "passed", "keep_browser": True,
                   "pages": [{**page(), "page": 2, "url": url}]}
        with tempfile.TemporaryDirectory() as folder:
            diagnostic.write_reports(Path(folder), summary, [])
            report = (Path(folder) / "report.html").read_text(encoding="utf-8")
            self.assertIn(url.replace("&", "&amp;"), report)
            self.assertIn("목록 2페이지 열기", report)
            self.assertIn("ZIP 10010", report)
            self.assertIn("Enter", report)
        self.assertNotIn('href=', diagnostic.listing_link({"page": 1, "url": "javascript:alert(1)"}))

    def test_absent_content_response_is_explained_without_hiding_other_errors(self):
        error = {"message": "Error - Not Found", "path": ["o0", "rows"], "extensions": {"code": "NOT_FOUND"}}
        report = {"absent_offer_content": {"664995": {"alias": "o0", "count": 0}},
                  "requests": [{"operation": "OfferCountContent", "attempt": 1,
                                "response": {"data": {"o0": {"rows": None}}, "errors": [error]}}]}
        result = diagnostic.request_errors(report)[0]
        self.assertFalse(result["affects_offer"])
        self.assertEqual(result["handling"], "no_displayed_offer_content")
        error["extensions"]["code"] = "INTERNAL_SERVER_ERROR"
        self.assertTrue(diagnostic.request_errors(report)[0]["affects_offer"])

    def test_unrelated_errors_are_recorded_as_non_offer_warnings(self):
        errors = diagnostic.request_errors({"ignored_listing_errors": [
            {"message": "Error - Not Found", "path": ["product", "arModels"],
             "extensions": {"code": "NOT_FOUND"}}]})
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0]["affects_offer"])

    def test_rdp_server_error_message_and_path_are_visible(self):
        error = {"message": "Error - Internal Server Error", "path": ["productsBySkuIds"],
                 "extensions": {"code": "INTERNAL_SERVER_ERROR"}}
        report = {"requests": [{"operation": "OfferCountProducts", "attempt": 1,
                                 "response": {"data": {"productsBySkuIds": None}, "errors": [error]}}]}
        errors = diagnostic.request_errors(report)
        self.assertEqual(errors[0]["message"], error["message"])
        self.assertEqual(errors[0]["code"], "INTERNAL_SERVER_ERROR")
        summary = {"category": "REF", "started_at": "fixture", "collection_status": "failed",
                   "pages": [{**page(False), "api_errors": errors}]}
        with tempfile.TemporaryDirectory() as folder:
            diagnostic.write_reports(Path(folder), summary, [])
            self.assertIn("Error - Internal Server Error", (Path(folder) / "report.html").read_text(encoding="utf-8"))

    def test_console_output_is_ascii_independent_of_windows_code_page(self):
        capture = io.StringIO()
        with redirect_stdout(capture):
            diagnostic.console("SKU 6467055 | UNKNOWN | Insignia™ – Frigidaire")
        self.assertTrue(capture.getvalue().isascii())
        self.assertIn("UNKNOWN", capture.getvalue())

    def test_real_csv_normalization_and_output_function_preserve_one_two_three(self):
        rows, _ = collect(Replay([product(), product("6486389", tier=True), product("6506246", tier=True, member=True)]))
        for category in ("REF", "LDY"):
            with tempfile.TemporaryDirectory() as folder, patch.object(final, "CATEGORY", category), \
                    patch.multiple(detail, CATEGORY=category, RUN_ROOT=Path(folder), USE_DB_SELECTORS=False,
                                   RAW_DETAIL_DIR=Path(folder) / "raw/detail", RAW_REVIEW_DIR=Path(folder) / "raw/review",
                                   RAW_COMPARE_DIR=Path(folder) / "raw/compare"):
                results, issues = diagnostic.save_pipeline(rows, Path(folder), listing, targets, final, detail, api)
                self.assertEqual(issues, [])
                self.assertEqual([r["final_offer"] for r in results], ["1", "2", "3"])

    def test_corrupted_csv_sku_evidence_counts_and_missing_output_fail(self):
        rows, _ = collect(Replay())
        for key, value in (("sku_id", "6506246"), ("offer", "9"), ("offer_count", "9"), ("offer_graphql_json", "{}")):
            saved = copy.deepcopy(rows)
            saved[0][key] = value
            self.assertIn("unverified_or_changed_rows", audit(rows, saved)[1], key)
        self.assertIn("saved_row_count_mismatch", audit(rows, outputs=[])[1])
        self.assertIn("no_collected_rows", audit([])[1])

    def test_unknown_and_verified_zero_are_distinct_in_report(self):
        p = product()
        p["price"]["showPlusOffers"] = False
        zero = collect(Replay([p]))[0][0]
        bad = api.normalize_graphql_offer({"sku_id": "6506246", "category_key": "REF"})
        results, issues = audit([zero, bad])
        summary = {}
        diagnostic.summarize(summary, results, [page(False)], issues, {"1", "2", "3"})
        self.assertEqual(summary["collection_status"], "failed")
        self.assertEqual(summary["offer_value_counts"], {"0(표시 없음)": 1, "미확인": 1})
        self.assertEqual(results[0]["status"], "passed")
        self.assertEqual(results[1]["status"], "failed")

    def test_missing_two_three_is_insufficient_samples_not_success(self):
        results, issues = audit(collect(Replay())[0])
        summary = {}
        diagnostic.summarize(summary, results, [page()], issues, {"1", "2", "3"})
        self.assertEqual(summary["collection_status"], "incomplete_samples")
        self.assertEqual(summary["missing_count_samples"], ["2", "3"])

    def test_zero_proof_lost_at_final_csv_does_not_pass_as_empty(self):
        p = product()
        p["price"]["showPlusOffers"] = False
        rows, _ = collect(Replay([p]))
        lost = {"sku_id": rows[0]["sku_id"], "offer": ""}
        results, issues = diagnostic.audit_rows(rows, rows, [lost], api.offer_evidence, [lost])
        self.assertIn("unverified_or_changed_rows", issues)
        self.assertEqual(results[0]["status"], "failed")

    def test_report_escapes_product_text_and_writes_no_zip(self):
        rows, _ = collect(Replay())
        rows[0]["product_name"] = '<script>alert("x")</script>'
        results, issues = audit(rows)
        summary = {"category": "REF", "started_at": "2026-09-17"}
        diagnostic.summarize(summary, results, [page()], issues, {"1"})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            diagnostic.write_reports(root, summary, results)
            report = (root / "report.html").read_text(encoding="utf-8")
            self.assertNotIn('<script>alert', report)
            self.assertIn('&lt;script&gt;', report)
            self.assertIn('실제 화면', report)
            self.assertIn('CSV offer', report)
            self.assertFalse(list(root.glob('*.zip')))
            self.assertEqual(diagnostic.load_csv(root / "offer_results.csv")[0]["final_offer"], "1")

    def test_error_report_exists_even_without_collected_rows(self):
        summary = {"category": "LDY", "started_at": "2026-09-17", "collection_status": "error",
                   "error": "FileNotFoundError: saved payload missing"}
        with tempfile.TemporaryDirectory() as folder:
            diagnostic.write_reports(Path(folder), summary, [])
            self.assertIn("FileNotFoundError", (Path(folder) / "summary.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
