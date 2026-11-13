"""Offline transport logging checks; no Chrome, network, DB or filesystem writes."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bestbuy import step00_dryer_log as log


class DryerTransportLogTests(unittest.TestCase):
    def helpers(self, fetch):
        listener = Mock(listening=False)
        listener.wait.return_value = []
        page = SimpleNamespace(url="https://user:secret@www.bestbuy.com/site/searchpage.jsp?secret=value#secret",
                               listen=listener, run_js=Mock(return_value={"ready_state": "complete", "online": True}))
        return SimpleNamespace(fetch_detail_browser_graphql_envelope=fetch, BROWSER_GRAPHQL_PAGE=page,
                               BROWSER_GRAPHQL_PROCESS_GENERATION=1,
                               BROWSER_GRAPHQL_RECOVERY_PROFILE_GENERATION=0,
                               BROWSER_GRAPHQL_SESSION_KIND="home_origin")

    def payload(self):
        return {"operationName": "PlpView_ProductList_Init", "query": "secret",
                "variables": {"pagination": {"pageNumber": 1}, "cookie": "secret"}}

    def test_success_preserves_payload_result_and_request_count(self):
        result = {"status": 200, "body": "secret"}
        fetch = Mock(return_value=result)
        helpers = self.helpers(fetch)
        payload = self.payload()
        with patch.object(log, "event") as emit, log.trace_browser_calls(helpers):
            self.assertIs(helpers.fetch_detail_browser_graphql_envelope(payload), result)
        fetch.assert_called_once_with(payload)
        self.assertIs(fetch.call_args.args[0], payload)
        self.assertIs(helpers.fetch_detail_browser_graphql_envelope, fetch)
        transport = next(c.kwargs for c in emit.call_args_list if c.args[0] == "graphql_transport")
        self.assertEqual(transport["http_status"], 200)
        self.assertEqual(transport["page_numbers"], [1])
        self.assertEqual(transport["context_url"], "https://www.bestbuy.com/site/searchpage.jsp")
        self.assertNotIn("secret", str(emit.call_args_list))
        helpers.BROWSER_GRAPHQL_PAGE.listen.wait.assert_not_called()
        helpers.BROWSER_GRAPHQL_PAGE.run_js.assert_not_called()

    def test_failure_records_network_code_without_changing_exception_or_retrying(self):
        error = RuntimeError("TypeError: Failed to fetch; secret")
        fetch = Mock(side_effect=error)
        helpers = self.helpers(fetch)
        helpers.BROWSER_GRAPHQL_PAGE.listen.wait.return_value = [SimpleNamespace(is_failed=True,
            fail_info=SimpleNamespace(errorText="net::ERR_HTTP2_PROTOCOL_ERROR secret",
                blockedReason="csp", canceled=False,
                corsErrorStatus={"corsError": "DisallowedByMode", "failedParameter": "secret"}))]
        with patch.object(log, "event") as emit, log.trace_browser_calls(helpers):
            with self.assertRaises(RuntimeError) as caught:
                helpers.fetch_detail_browser_graphql_envelope(self.payload())
        self.assertIs(caught.exception, error)
        self.assertEqual(fetch.call_count, 1)
        record = next(c.kwargs for c in emit.call_args_list if c.args[0] == "graphql_transport")
        self.assertEqual(record["network_events"][0]["error_code"], "net::ERR_HTTP2_PROTOCOL_ERROR")
        self.assertEqual(record["network_events"][0]["cors_error"], "DisallowedByMode")
        self.assertNotIn("secret", str(emit.call_args_list))
        helpers.BROWSER_GRAPHQL_PAGE.listen.stop.assert_called_once()

    def test_existing_listener_ownership_preserved(self):
        helpers = self.helpers(Mock(side_effect=RuntimeError("Failed to fetch")))
        listener = helpers.BROWSER_GRAPHQL_PAGE.listen
        listener.listening = True
        with patch.object(log, "event"), log.trace_browser_calls(helpers):
            with self.assertRaises(RuntimeError):
                helpers.fetch_detail_browser_graphql_envelope(self.payload())
        listener.start.assert_not_called()
        listener.stop.assert_not_called()
        listener.wait.assert_not_called()

    def test_diagnostic_failures_do_not_change_original_result_or_exception(self):
        error = RuntimeError("original")
        for stage in ("start", "finish", "emit"):
            for failing in (False, True):
                with self.subTest(stage=stage, failing=failing):
                    result = {"status": 200}
                    fetch = Mock(side_effect=error) if failing else Mock(return_value=result)
                    helpers = self.helpers(fetch)
                    diagnostic = Mock()
                    diagnostic.finish.return_value = {}
                    if stage == "finish":
                        diagnostic.finish.side_effect = ValueError("secret")
                    def emit(name, **fields):
                        if stage == "emit" and name in {"graphql_transport", "graphql_request_context"}:
                            raise OSError("secret")
                    with patch.object(log, "BrowserDiagnostics", return_value=diagnostic,
                                      side_effect=ValueError("secret") if stage == "start" else None), \
                         patch.object(log, "event", side_effect=emit), log.trace_browser_calls(helpers):
                        if failing:
                            with self.assertRaises(RuntimeError) as caught:
                                helpers.fetch_detail_browser_graphql_envelope(self.payload())
                            self.assertIs(caught.exception, error)
                        else:
                            self.assertIs(helpers.fetch_detail_browser_graphql_envelope(self.payload()), result)
                    self.assertEqual(fetch.call_count, 1)

    def test_observed_retry_is_logged_but_not_initiated_by_logger(self):
        fetch = Mock(side_effect=[RuntimeError("Failed to fetch"), {"status": 200}])
        helpers = self.helpers(fetch)
        with patch.object(log, "event") as emit, log.trace_browser_calls(helpers):
            with self.assertRaises(RuntimeError):
                helpers.fetch_detail_browser_graphql_envelope(self.payload())
            self.assertEqual(fetch.call_count, 1)
            helpers.fetch_detail_browser_graphql_envelope(self.payload())
        retries = [c.kwargs for c in emit.call_args_list if c.args[0] == "graphql_retry"]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["attempt"], 2)
        self.assertEqual(retries[0]["reason"], "graphql_fetch_failed")
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
