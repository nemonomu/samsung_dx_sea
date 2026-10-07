import json
import types
import unittest
from unittest.mock import Mock

from bestbuy.step00_browser_diagnostics import BrowserDiagnostics


class BrowserDiagnosticsTests(unittest.TestCase):
    def page(self):
        listener = Mock(listening=False)
        page = types.SimpleNamespace(listen=listener, url="https://www.bestbuy.com/site/searchpage.jsp?sensitive=value",
                                     run_js=Mock(return_value={"ready_state": "complete", "online": True}))
        return page

    def test_loading_failed_fields_preserved_without_headers_or_cors_parameter(self):
        page = self.page()
        packet = types.SimpleNamespace(is_failed=True, fail_info=types.SimpleNamespace(
            errorText="net::ERR_FAILED", blockedReason="csp", canceled=False,
            corsErrorStatus={"corsError": "DisallowedByMode", "failedParameter": "sensitive-value"}))
        page.listen.wait.return_value = [packet]
        result = BrowserDiagnostics(page).finish(True)
        self.assertEqual(result["network_events"][0], {
            "is_failed": True, "source": "Network.loadingFailed",
            "errorText": "net::ERR_FAILED", "blockedReason": "csp", "canceled": False,
            "corsErrorStatus": {"corsError": "DisallowedByMode"}})
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertEqual(result["ready_state"], "complete")
        self.assertTrue(result["online"])
        page.listen.stop.assert_called_once()
        self.assertEqual(page.listen.wait.call_args.kwargs["timeout"], 0.25)

    def test_http_failure_keeps_status_only(self):
        page = self.page()
        page.listen.wait.return_value = [types.SimpleNamespace(is_failed=False,
                                          response=types.SimpleNamespace(status=503))]
        result = BrowserDiagnostics(page).finish(True)
        self.assertEqual(result["network_events"], [{"is_failed": False, "status_code": 503}])

    def test_success_has_no_extra_wait_or_javascript(self):
        page = self.page()
        BrowserDiagnostics(page).finish(False)
        page.listen.wait.assert_not_called()
        page.run_js.assert_not_called()
        page.listen.stop.assert_called_once()

    def test_listener_errors_do_not_replace_collection_error(self):
        for operation in ("start", "wait", "stop"):
            with self.subTest(operation=operation):
                page = self.page()
                page.listen.wait.return_value = False
                getattr(page.listen, operation).side_effect = RuntimeError("diagnostic unavailable")
                result = BrowserDiagnostics(page).finish(True)
                self.assertIn("RuntimeError", json.dumps(result))
        result = BrowserDiagnostics(object()).finish(True)
        self.assertEqual(result["listener_status"], "unavailable")

    def test_existing_listener_is_not_reconfigured_or_stopped(self):
        page = self.page()
        page.listen.listening = True
        result = BrowserDiagnostics(page).finish(True)
        self.assertEqual(result["listener_status"], "already_in_use")
        page.listen.start.assert_not_called()
        page.listen.wait.assert_not_called()
        page.listen.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
