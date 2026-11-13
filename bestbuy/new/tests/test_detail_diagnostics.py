"""Offline checks for diagnostics that cannot alter collection decisions."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bestbuy import step08_detail_diagnostics as diag


class DetailDiagnosticsTests(unittest.TestCase):
    def helpers(self, fetch, category="LDY"):
        listener = Mock(listening=False)
        listener.wait.return_value = []
        page = SimpleNamespace(url="https://user:secret@www.bestbuy.com/?secret=1#secret", listen=listener,
                               run_js=Mock(return_value={"ready_state": "complete", "online": True}))
        return SimpleNamespace(CATEGORY=category, fetch_detail_browser_graphql_envelope=fetch,
            BROWSER_GRAPHQL_PAGE=page, BROWSER_GRAPHQL_SESSION_KIND="home_origin",
            BROWSER_GRAPHQL_PROCESS_GENERATION=0, BROWSER_GRAPHQL_RECOVERY_PROFILE_GENERATION=0)

    def test_success_has_same_payload_result_and_call_count(self):
        for category in ("REF", "LDY"):
            with self.subTest(category=category):
                result = {"status": 200, "body": "secret"}
                fetch = Mock(return_value=result)
                d = self.helpers(fetch, category)
                payload = [{"query": "secret", "variables": {"skuId": "1"}}]
                before = copy.deepcopy(payload)
                with diag.observe_request(d) as report:
                    self.assertIs(d.fetch_detail_browser_graphql_envelope(payload), result)
                fetch.assert_called_once_with(payload)
                self.assertIs(fetch.call_args.args[0], payload)
                self.assertEqual(payload, before)
                self.assertIs(d.fetch_detail_browser_graphql_envelope, fetch)
                self.assertEqual(report["attempts"][0]["http_status"], 200)
                self.assertNotIn("secret", json.dumps(report))
                d.BROWSER_GRAPHQL_PAGE.listen.wait.assert_not_called()
                d.BROWSER_GRAPHQL_PAGE.run_js.assert_not_called()

    def test_failed_fetch_retains_exception_and_captures_network_code(self):
        error = RuntimeError("Failed to fetch secret")
        fetch = Mock(side_effect=error)
        d = self.helpers(fetch)
        d.BROWSER_GRAPHQL_PAGE.listen.wait.return_value = [SimpleNamespace(is_failed=True,
            fail_info=SimpleNamespace(errorText="net::ERR_HTTP2_PROTOCOL_ERROR secret", canceled=False,
                blockedReason="csp", corsErrorStatus={"corsError": "DisallowedByMode", "failedParameter": "secret"}))]
        with diag.observe_request(d) as report:
            with self.assertRaises(RuntimeError) as caught:
                d.fetch_detail_browser_graphql_envelope([])
        self.assertIs(caught.exception, error)
        self.assertEqual(fetch.call_count, 1)
        packet = report["attempts"][0]["network_events"][0]
        self.assertEqual(packet["error_code"], "net::ERR_HTTP2_PROTOCOL_ERROR")
        self.assertEqual(packet["cors_error"], "DisallowedByMode")
        self.assertNotIn("secret", json.dumps(report))
        self.assertIs(d.fetch_detail_browser_graphql_envelope, fetch)

    def test_http_failure_and_already_active_listener(self):
        for active in (False, True):
            d = self.helpers(Mock(side_effect=RuntimeError("systemic HTTP status: 403")))
            listener = d.BROWSER_GRAPHQL_PAGE.listen
            listener.listening = active
            listener.wait.return_value = [SimpleNamespace(is_failed=False, response=SimpleNamespace(status=403))]
            with diag.observe_request(d) as report:
                with self.assertRaises(RuntimeError):
                    d.fetch_detail_browser_graphql_envelope([])
            if active:
                listener.start.assert_not_called()
                listener.stop.assert_not_called()
                listener.wait.assert_not_called()
            else:
                self.assertEqual(report["attempts"][0]["network_events"][0]["http_status"], 403)

    def test_dryer_and_tv_not_instrumented(self):
        for category in ("DRYER", "TV"):
            d = self.helpers(Mock(return_value={"status": 200}), category)
            original = d.fetch_detail_browser_graphql_envelope
            with patch.object(diag, "BrowserDiagnostics") as observer, diag.observe_request(d) as report:
                self.assertIs(d.fetch_detail_browser_graphql_envelope, original)
                d.fetch_detail_browser_graphql_envelope([])
            observer.assert_not_called()
            self.assertEqual(report["attempts"], [])

    def test_reports_do_not_leak_between_requests_and_no_automatic_retry(self):
        fetch = Mock(side_effect=[RuntimeError("offline"), {"status": 200}])
        d = self.helpers(fetch)
        with diag.observe_request(d) as first:
            with self.assertRaises(RuntimeError):
                d.fetch_detail_browser_graphql_envelope([])
        self.assertEqual(fetch.call_count, 1)
        with diag.observe_request(d) as second:
            d.fetch_detail_browser_graphql_envelope([])
        self.assertTrue(first["attempts"][0]["failed"])
        self.assertFalse(second["attempts"][0]["failed"])
        self.assertEqual(len(second["attempts"]), 1)
        self.assertEqual(second["attempts"][0]["attempt"], 1)

    def test_diagnostic_start_and_finish_failures_preserve_original_exception(self):
        for phase in ("start", "finish"):
            error = RuntimeError("original")
            d = self.helpers(Mock(side_effect=error))
            observer = Mock()
            observer.finish.side_effect = ValueError("secret")
            with patch.object(diag, "BrowserDiagnostics", return_value=observer,
                              side_effect=ValueError("secret") if phase == "start" else None):
                with diag.observe_request(d):
                    with self.assertRaises(RuntimeError) as caught:
                        d.fetch_detail_browser_graphql_envelope([])
            self.assertIs(caught.exception, error)

    def test_response_fields_and_output_blanks_do_not_modify_states_or_rows(self):
        d, evidence = self.helpers(Mock()), Mock()
        state = {"status": "success", "request_attempt": 1, "response_received": True, "reason": "verified"}
        item = {"data": {"productBySkuId": {"skuId": "1", "manufacturer": {"modelNumber": None},
                                          "specificationGroups": []}}}
        rows = [{"sku": None, "ldy_capacity": "", "final_sku_price": 0}]
        states = {"1": {"detail": state}}
        before = copy.deepcopy((item, rows, states))
        diag.operation_result(d, evidence, "1", "detail", item, state, True)
        fields = evidence.emit.call_args.kwargs["response_fields"]
        self.assertEqual(fields["data.productBySkuId.manufacturer.modelNumber"], "null")
        self.assertEqual(fields["data.productBySkuId.specificationGroups"], "empty")
        self.assertEqual(fields["data.productBySkuId.price"], "missing")
        diag.output_fields(d, evidence, [{"sku_id": "1"}], rows, states)
        self.assertEqual(evidence.emit.call_args.kwargs["blank_columns"], ["ldy_capacity", "sku"])
        self.assertEqual((item, rows, states), before)
        # A failed retry must not claim a response just because a prior attempt had one.
        diag.operation_result(d, evidence, "1", "detail", {}, state, False)
        self.assertFalse(evidence.emit.call_args.kwargs["response_received"])
        self.assertEqual(evidence.emit.call_args.kwargs["response_fields"], {})

    def test_request_summary_and_emit_failure(self):
        d, evidence = self.helpers(Mock()), Mock()
        requests = [{"operationName": name, "variables": {"skuId": "1", "private": "secret"}, "query": "secret"}
                    for name in ("ProductSchema_init", "GetCompareProduct")]
        diag.request_result(d, evidence, requests, {"attempts": []}, 200, [], False, "request.json")
        record = evidence.emit.call_args.kwargs
        self.assertEqual(record["sku_count"], 1)
        self.assertEqual(record["operation_count"], 2)
        self.assertTrue(record["http_response_received"])
        self.assertFalse(record["batch_shape_valid"])
        self.assertNotIn("secret", json.dumps(record))
        evidence.emit.side_effect = OSError("log unavailable")
        diag.request_result(d, evidence, requests, {}, "ERR", {}, False, "request.json")

    def test_http_error_response_is_observed_even_when_transport_raises(self):
        d, evidence = self.helpers(Mock()), Mock()
        report = {"attempts": [{"http_status": None, "network_events": [{"http_status": 403}]}]}
        diag.request_result(d, evidence, [], report, "ERR", {}, False, "request.json")
        record = evidence.emit.call_args.kwargs
        self.assertTrue(record["http_response_received"])
        self.assertEqual(record["observed_http_statuses"], [403])
        self.assertEqual(record["http_status"], "ERR")
        self.assertFalse(record["batch_shape_valid"])

    def test_ref_type_uses_real_output_column(self):
        d, evidence = self.helpers(Mock(), "REF"), Mock()
        diag.output_fields(d, evidence, [{"sku_id": "1"}], [{"ref_refrigerator_type": None}], {"1": {}})
        self.assertEqual(evidence.emit.call_args.kwargs["blank_columns"], ["ref_refrigerator_type"])


if __name__ == "__main__":
    unittest.main()
