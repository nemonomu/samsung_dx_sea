import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


os.environ.setdefault("BESTBUY_CATEGORY", "TV")
os.environ.setdefault("BESTBUY_URL_SOURCE", "default")

if "zenrows" not in sys.modules:
    try:
        import zenrows  # noqa: F401
    except ModuleNotFoundError:
        zenrows_stub = types.ModuleType("zenrows")
        zenrows_stub.ZenRowsClient = object
        sys.modules["zenrows"] = zenrows_stub

import bestbuy.step08_detail_enrichment as detail
import bestbuy.step00_browser_session as browser_session


SKU = "6673625"
PDP_URL = f"https://www.bestbuy.com/product/sample-product/sample-id/sku/{SKU}"
STRIPPED_PDP_URL = "https://www.bestbuy.com/product/sample-product/sample-id"


def browser_state(href, *, origin="https://www.bestbuy.com", body_text="", ready_state="complete"):
    return {
        "href": href,
        "origin": origin,
        "readyState": ready_state,
        "title": "Best Buy",
        "bodyText": body_text,
    }


class ScriptedPage:
    def __init__(self, states, *, click_result=True):
        self.states = list(states)
        self.click_result = click_result
        self.get_calls = []
        self.get_timeouts = []
        self.script_calls = []

    def get(self, url, timeout=None):
        self.get_calls.append(url)
        self.get_timeouts.append(timeout)
        return True

    def run_js(self, script, timeout=None):
        self.script_calls.append(script)
        if "document.readyState" in script:
            if not self.states:
                raise AssertionError("No scripted browser state remains")
            return json.dumps(self.states.pop(0))
        if "const selectors" in script:
            return json.dumps({"submitted": True, "selector": "#gh-search-input"})
        if "const anchors" in script:
            return json.dumps(
                {"clicked": self.click_result, "href": PDP_URL if self.click_result else "", "reason": "sku_link_missing"}
            )
        if "location.assign(targetUrl)" in script:
            return json.dumps({"assigned": True, "href": PDP_URL})
        raise AssertionError(f"Unexpected JavaScript: {script[:80]}")


class MemoryPath:
    def __init__(self):
        self.writes = []

    def write_text(self, value, **kwargs):
        self.writes.append((value, kwargs))
        return len(value)


class DetailBrowserRecoveryTests(unittest.TestCase):
    GLOBAL_NAMES = (
        "BROWSER_GRAPHQL_PAGE",
        "BROWSER_GRAPHQL_META",
        "BROWSER_GRAPHQL_CURRENT_URL",
        "BROWSER_GRAPHQL_SESSION_READY",
        "BROWSER_GRAPHQL_SESSION_KIND",
        "BROWSER_GRAPHQL_PROCESS_GENERATION",
        "BROWSER_GRAPHQL_PROFILE_KIND",
        "BROWSER_GRAPHQL_RECOVERY_PROFILE_GENERATION",
        "BROWSER_GRAPHQL_WAIT_SECONDS",
    )

    def setUp(self):
        self.saved_globals = {name: getattr(detail, name) for name in self.GLOBAL_NAMES}
        self.addCleanup(self.restore_globals)
        detail.BROWSER_GRAPHQL_WAIT_SECONDS = 0

    def restore_globals(self):
        for name, value in self.saved_globals.items():
            setattr(detail, name, value)

    def install_page(self, page, *, ready=False, current_url=""):
        detail.BROWSER_GRAPHQL_PAGE = page
        detail.BROWSER_GRAPHQL_META = {}
        detail.BROWSER_GRAPHQL_CURRENT_URL = current_url
        detail.BROWSER_GRAPHQL_SESSION_READY = ready
        detail.BROWSER_GRAPHQL_SESSION_KIND = "home_origin" if ready and current_url else ""
        detail.BROWSER_GRAPHQL_PROCESS_GENERATION = 0
        detail.BROWSER_GRAPHQL_PROFILE_KIND = "primary"
        detail.BROWSER_GRAPHQL_RECOVERY_PROFILE_GENERATION = 0

    def test_cold_bootstrap_uses_home_search_and_result_click_before_committing_url(self):
        home_url = "https://www.bestbuy.com/?intl=nosplash"
        search_url = f"https://www.bestbuy.com/site/searchpage.jsp?st={SKU}"
        page = ScriptedPage(
            [
                browser_state(home_url),
                browser_state(search_url),
                browser_state(PDP_URL),
            ]
        )
        self.install_page(page)

        with patch.object(detail, "BROWSER_BOOTSTRAP_POLL_SECONDS", 0.001), patch.object(
            detail, "BROWSER_BOOTSTRAP_TIMEOUT_SECONDS", 1
        ):
            state = detail.bootstrap_detail_browser_session(STRIPPED_PDP_URL, SKU)

        self.assertEqual(page.get_calls, [detail.BROWSER_BOOTSTRAP_HOME_URL])
        self.assertEqual(state["href"], PDP_URL)
        self.assertEqual(detail.BROWSER_GRAPHQL_CURRENT_URL, PDP_URL)
        self.assertTrue(detail.BROWSER_GRAPHQL_SESSION_READY)
        script_text = "\n".join(page.script_calls)
        self.assertLess(script_text.index("const selectors"), script_text.index("const anchors"))

    def test_direct_graphql_bootstrap_commits_verified_home_without_search_or_pdp(self):
        home_url = "https://www.bestbuy.com/?intl=nosplash"
        page = ScriptedPage([browser_state(home_url), browser_state(home_url)])
        self.install_page(page)

        state = detail.bootstrap_detail_browser_session(
            STRIPPED_PDP_URL,
            SKU,
            require_pdp=False,
        )

        self.assertEqual(page.get_calls, [detail.BROWSER_BOOTSTRAP_HOME_URL])
        self.assertEqual(state["href"], home_url)
        self.assertEqual(detail.BROWSER_GRAPHQL_CURRENT_URL, home_url)
        self.assertTrue(detail.BROWSER_GRAPHQL_SESSION_READY)
        self.assertEqual(detail.BROWSER_GRAPHQL_SESSION_KIND, "home_origin")
        script_text = "\n".join(page.script_calls)
        self.assertNotIn("const selectors", script_text)
        self.assertNotIn("const anchors", script_text)
        self.assertNotIn("location.assign(targetUrl)", script_text)

    def test_home_bootstrap_rejects_same_origin_non_home_redirect(self):
        page = ScriptedPage([browser_state(PDP_URL)])
        self.install_page(page)

        with patch.object(detail, "BROWSER_BOOTSTRAP_POLL_SECONDS", 0.001), patch.object(
            detail, "BROWSER_BOOTSTRAP_TIMEOUT_SECONDS", 0.003
        ):
            with self.assertRaises(detail.DetailBrowserBootstrapError):
                detail.bootstrap_detail_browser_session(
                    STRIPPED_PDP_URL,
                    SKU,
                    require_pdp=False,
                )

        self.assertEqual(detail.BROWSER_GRAPHQL_CURRENT_URL, "")
        self.assertFalse(detail.BROWSER_GRAPHQL_SESSION_READY)
        self.assertEqual(detail.BROWSER_GRAPHQL_SESSION_KIND, "")

    def test_bootstrap_rejects_chrome_error_without_committing_expected_url(self):
        page = ScriptedPage(
            [
                browser_state(
                    "chrome-error://chromewebdata/",
                    origin="null",
                    body_text="This site can't be reached ERR_HTTP2_PROTOCOL_ERROR",
                )
            ]
        )
        self.install_page(page)

        with patch.object(detail, "BROWSER_BOOTSTRAP_POLL_SECONDS", 0.001), patch.object(
            detail, "BROWSER_BOOTSTRAP_TIMEOUT_SECONDS", 0.1
        ):
            with self.assertRaises(detail.DetailBrowserBootstrapError):
                detail.bootstrap_detail_browser_session(PDP_URL)

        self.assertEqual(detail.BROWSER_GRAPHQL_CURRENT_URL, "")
        self.assertFalse(detail.BROWSER_GRAPHQL_SESSION_READY)

    def test_navigation_false_rejects_http2_error_before_search(self):
        class FailedNavigationPage(ScriptedPage):
            def get(self, url, timeout=None):
                self.get_calls.append(url)
                self.get_timeouts.append(timeout)
                return False

        page = FailedNavigationPage(
            [
                browser_state(
                    "chrome-error://chromewebdata/",
                    origin="null",
                    body_text="This site can't be reached ERR_HTTP2_PROTOCOL_ERROR",
                )
            ]
        )
        self.install_page(page)

        with self.assertRaises(detail.DetailBrowserBootstrapError):
            detail.bootstrap_detail_browser_session(STRIPPED_PDP_URL, SKU)

        self.assertEqual(page.get_calls, [detail.BROWSER_BOOTSTRAP_HOME_URL])
        self.assertFalse(any("const selectors" in script for script in page.script_calls))
        self.assertEqual(detail.BROWSER_GRAPHQL_CURRENT_URL, "")
        self.assertFalse(detail.BROWSER_GRAPHQL_SESSION_READY)

    def test_missing_result_link_uses_same_origin_assign_not_direct_pdp_get(self):
        home_url = "https://www.bestbuy.com/?intl=nosplash"
        search_url = f"https://www.bestbuy.com/site/searchpage.jsp?st={SKU}"
        page = ScriptedPage(
            [
                browser_state(home_url),
                browser_state(search_url),
                browser_state(PDP_URL),
                browser_state(PDP_URL),
            ],
            click_result=False,
        )
        self.install_page(page)

        with patch.object(detail, "BROWSER_BOOTSTRAP_POLL_SECONDS", 0.001), patch.object(
            detail, "BROWSER_BOOTSTRAP_TIMEOUT_SECONDS", 1
        ), patch.object(detail, "BROWSER_BOOTSTRAP_TRANSITION_TIMEOUT_SECONDS", 0.003):
            state = detail.bootstrap_detail_browser_session(STRIPPED_PDP_URL, SKU)

        self.assertEqual(state["href"], PDP_URL)
        self.assertEqual(page.get_calls, [detail.BROWSER_BOOTSTRAP_HOME_URL])
        self.assertTrue(any("location.assign(targetUrl)" in script for script in page.script_calls))

    def test_verified_pdp_uses_configured_settle_wait(self):
        home_url = "https://www.bestbuy.com/?intl=nosplash"
        search_url = f"https://www.bestbuy.com/site/searchpage.jsp?st={SKU}"
        page = ScriptedPage(
            [
                browser_state(home_url),
                browser_state(search_url),
                browser_state(PDP_URL),
                browser_state(PDP_URL),
            ]
        )
        self.install_page(page)

        with patch.object(detail, "BROWSER_GRAPHQL_WAIT_SECONDS", 3), patch.object(
            detail.time, "sleep"
        ) as sleep_mock:
            detail.bootstrap_detail_browser_session(STRIPPED_PDP_URL, SKU)

        sleep_mock.assert_called_once_with(3)

    def test_verified_home_uses_configured_settle_wait(self):
        home_url = "https://www.bestbuy.com/?intl=nosplash"
        page = ScriptedPage([browser_state(home_url), browser_state(home_url)])
        self.install_page(page)

        with patch.object(detail, "BROWSER_GRAPHQL_WAIT_SECONDS", 3), patch.object(
            detail.time, "sleep"
        ) as sleep_mock:
            detail.bootstrap_detail_browser_session(
                STRIPPED_PDP_URL,
                SKU,
                require_pdp=False,
            )

        sleep_mock.assert_called_once_with(3)

    def test_state_validation_requires_real_bestbuy_origin_and_matching_sku(self):
        self.assertIn(
            "unexpected_origin",
            detail.detail_browser_state_error(
                {
                    "href": PDP_URL,
                    "origin": "null",
                    "body_text": "",
                },
                SKU,
                True,
            ),
        )
        self.assertIn(
            "unexpected_pdp_sku",
            detail.detail_browser_state_error(
                {
                    "href": PDP_URL.replace(SKU, "1234567"),
                    "origin": "https://www.bestbuy.com",
                    "body_text": "",
                },
                SKU,
                True,
            ),
        )
        self.assertEqual(
            detail.detail_browser_state_error(
                {
                    "href": PDP_URL,
                    "origin": "https://www.bestbuy.com",
                    "body_text": "",
                },
                SKU,
                True,
            ),
            "",
        )
        self.assertIn(
            "browser_error_document",
            detail.detail_browser_state_error(
                {
                    "href": PDP_URL,
                    "origin": "https://www.bestbuy.com",
                    "body_text": "Access Denied - verify you are human",
                },
                SKU,
                True,
            ),
        )

    def test_failed_fetch_retries_same_home_session_before_restart(self):
        self.install_page(object(), ready=True, current_url=PDP_URL)
        success = {"status": 200, "contentType": "application/json", "body": "{}"}

        with patch.object(
            detail,
            "browser_fetch_graphql",
            side_effect=[RuntimeError("TypeError: Failed to fetch"), success],
        ) as fetch_mock, patch.object(detail, "recover_detail_browser_session") as recover_mock, patch.object(
            detail, "BROWSER_GRAPHQL_MAX_RECOVERIES", 1
        ), patch.object(detail, "BROWSER_GRAPHQL_RECOVERY_BACKOFF_SECONDS", 0):
            status, _, _, headers, _ = detail.browser_graphql_post([{"query": "q"}], STRIPPED_PDP_URL, SKU)

        self.assertEqual(status, 200)
        self.assertEqual(fetch_mock.call_count, 2)
        recover_mock.assert_not_called()
        self.assertEqual(headers["browser_profile_kind"], "primary")

    def test_second_failed_fetch_restarts_home_session_then_succeeds(self):
        self.install_page(object(), ready=True, current_url=PDP_URL)
        success = {"status": 200, "contentType": "application/json", "body": "{}"}

        with patch.object(
            detail,
            "browser_fetch_graphql",
            side_effect=[
                RuntimeError("TypeError: Failed to fetch"),
                RuntimeError("TypeError: Failed to fetch"),
                success,
            ],
        ) as fetch_mock, patch.object(detail, "recover_detail_browser_session") as recover_mock, patch.object(
            detail, "BROWSER_GRAPHQL_MAX_RECOVERIES", 2
        ), patch.object(detail, "BROWSER_GRAPHQL_RECOVERY_BACKOFF_SECONDS", 0):
            status, _, _, _, _ = detail.browser_graphql_post([{"query": "q"}], STRIPPED_PDP_URL, SKU)

        self.assertEqual(status, 200)
        self.assertEqual(fetch_mock.call_count, 3)
        recover_mock.assert_called_once_with(
            STRIPPED_PDP_URL + "?intl=nosplash",
            SKU,
            fresh_profile=False,
            require_pdp=False,
        )

    def test_real_recovery_path_restarts_into_home_without_pdp_navigation(self):
        home_url = "https://www.bestbuy.com/?intl=nosplash"
        restarted_page = ScriptedPage([browser_state(home_url)])
        restart_calls = []
        self.install_page(object(), ready=True, current_url=home_url)
        success = {"status": 200, "contentType": "application/json", "body": "{}"}

        def recreate(*, fresh_profile=False):
            restart_calls.append(fresh_profile)
            detail.BROWSER_GRAPHQL_PAGE = restarted_page
            detail.BROWSER_GRAPHQL_CURRENT_URL = ""
            detail.BROWSER_GRAPHQL_SESSION_READY = False
            detail.BROWSER_GRAPHQL_SESSION_KIND = ""
            return restarted_page

        with patch.object(
            detail,
            "browser_fetch_graphql",
            side_effect=[
                RuntimeError("TypeError: Failed to fetch"),
                RuntimeError("TypeError: Failed to fetch"),
                success,
            ],
        ), patch.object(detail, "recreate_detail_browser_page", side_effect=recreate), patch.object(
            detail, "BROWSER_GRAPHQL_MAX_RECOVERIES", 2
        ), patch.object(detail, "BROWSER_GRAPHQL_RECOVERY_BACKOFF_SECONDS", 0):
            status, _, _, headers, _ = detail.browser_graphql_post(
                [{"query": "q"}],
                STRIPPED_PDP_URL,
                SKU,
            )

        self.assertEqual(status, 200)
        self.assertEqual(restart_calls, [False])
        self.assertEqual(restarted_page.get_calls, [detail.BROWSER_BOOTSTRAP_HOME_URL])
        scripts = "\n".join(restarted_page.script_calls)
        self.assertNotIn("const selectors", scripts)
        self.assertNotIn("const anchors", scripts)
        self.assertEqual(headers["browser_url"], home_url)
        self.assertEqual(headers["browser_session_kind"], "home_origin")

    def test_recovery_recreates_process_before_full_bootstrap(self):
        events = []

        def recreate(*, fresh_profile=False):
            events.append(("recreate", fresh_profile))

        def bootstrap(browser_url, expected_sku="", *, require_pdp=True):
            events.append(("bootstrap", browser_url, expected_sku, require_pdp))
            return {"href": PDP_URL}

        with patch.object(detail, "recreate_detail_browser_page", side_effect=recreate), patch.object(
            detail, "bootstrap_detail_browser_session", side_effect=bootstrap
        ):
            state = detail.recover_detail_browser_session(
                STRIPPED_PDP_URL,
                SKU,
                fresh_profile=False,
            )

        self.assertEqual(
            events,
            [
                ("recreate", False),
                ("bootstrap", STRIPPED_PDP_URL, SKU, True),
            ],
        )
        self.assertEqual(state["href"], PDP_URL)

    def test_ready_home_session_is_reused_and_reported_as_actual_referer(self):
        home_url = "https://www.bestbuy.com/?intl=nosplash"
        self.install_page(object(), ready=True, current_url=home_url)
        response = {"status": 200, "contentType": "application/json", "body": "{}"}

        with patch.object(detail, "browser_fetch_graphql", return_value=response) as fetch_mock:
            first = detail.browser_graphql_post([{"query": "first"}], STRIPPED_PDP_URL, SKU)
            second = detail.browser_graphql_post(
                [{"query": "second"}],
                STRIPPED_PDP_URL.replace(SKU, "1234567"),
                "1234567",
            )

        self.assertEqual(fetch_mock.call_count, 2)
        for result in (first, second):
            headers = result[3]
            self.assertEqual(headers["browser_url"], home_url)
            self.assertEqual(headers["browser_referer_url"], home_url)
            self.assertEqual(headers["browser_session_kind"], "home_origin")
        self.assertEqual(detail.BROWSER_GRAPHQL_CURRENT_URL, home_url)

    def test_canary_accepts_zero_reviews_and_zero_recommendations(self):
        target = {"sku_id": SKU, "product_url": PDP_URL}
        with patch.object(detail, "FETCH_COMPARE", True):
            _, entries = detail.detail_browser_canary_request_entries([target], stage="detail")
        indices = entries[0]["indices"]
        response_json = [{} for _ in range(max(indices.values()) + 1)]
        response_json[indices["detail"]] = {
            "data": {"productBySkuId": {"skuId": SKU, "reviews": {"results": []}}}
        }
        response_json[indices["review"]] = {
            "data": {"productBySkuId": {"skuId": SKU, "reviews": {"results": []}}}
        }
        response_json[indices["compare"]] = {
            "data": {
                "productBySkuId": {"skuId": SKU},
                "recommendations": {"subPlacements": []},
            }
        }

        report = detail.validate_detail_browser_canary_response(response_json, entries, 200)

        self.assertEqual(report["sku_count"], 1)
        self.assertEqual(report["operation_count"], 3)

    def test_canary_rejects_wrong_sku_and_graphql_errors(self):
        entries = [
            {
                "sku": SKU,
                "indices": {"detail": 0, "review": 1},
            }
        ]
        wrong_sku = [
            {"data": {"productBySkuId": {"skuId": "1234567"}}},
            {"data": {"productBySkuId": {"skuId": SKU, "reviews": {"results": []}}}},
        ]
        graphql_error = [
            {"errors": [{"message": "blocked"}]},
            {"data": {"productBySkuId": {"skuId": SKU, "reviews": {"results": []}}}},
        ]

        for response_json in (wrong_sku, graphql_error):
            with self.subTest(response_json=response_json):
                with self.assertRaises(detail.DetailBrowserCanaryError):
                    detail.validate_detail_browser_canary_response(response_json, entries, 200)

    def test_canary_requires_compare_shape_but_allows_empty_recommendations(self):
        entries = [{"sku": SKU, "indices": {"compare": 0}}]
        missing_shape = [{"data": {"productBySkuId": {"skuId": SKU}}}]
        empty_shape = [
            {
                "data": {
                    "productBySkuId": {"skuId": SKU},
                    "recommendations": {"subPlacements": []},
                }
            }
        ]

        with self.assertRaises(detail.DetailBrowserCanaryError):
            detail.validate_detail_browser_canary_response(missing_shape, entries, 200)
        report = detail.validate_detail_browser_canary_response(empty_shape, entries, 200)
        self.assertEqual(report["warning_count"], 0)

    def test_non_strict_preflight_treats_missing_product_as_item_warning(self):
        entries = [{"sku": SKU, "indices": {"detail": 0}}]
        response_json = [{"data": {"productBySkuId": None}}]

        report = detail.validate_detail_browser_canary_response(
            response_json,
            entries,
            200,
            strict=False,
        )

        self.assertEqual(report["warning_count"], 1)
        self.assertIn("product_missing", report["warnings"][0])

    def test_review_canary_does_not_include_detail_or_compare_operations(self):
        target = {"sku_id": SKU, "product_url": PDP_URL}
        with patch.object(detail, "FETCH_COMPARE", True):
            payloads, entries = detail.detail_browser_canary_request_entries([target], stage="review")

        self.assertEqual(len(payloads), 1)
        self.assertEqual(set(entries[0]["indices"]), {"review"})

        singleton_response = {
            "data": {"productBySkuId": {"skuId": SKU, "reviews": {"results": []}}}
        }
        report = detail.validate_detail_browser_canary_response(
            singleton_response,
            entries,
            200,
        )
        self.assertEqual(report["operation_count"], 1)

    def test_preflight_skips_cache_complete_stage(self):
        target = {"sku_id": SKU, "product_url": PDP_URL}
        with patch.object(detail, "FORCE_REFRESH", False), patch.object(
            detail, "STAGE", "detail"
        ), patch.object(detail, "detail_success", return_value=True):
            self.assertEqual(detail.detail_browser_preflight_candidates([target]), [])
        with patch.object(detail, "FORCE_REFRESH", False), patch.object(
            detail, "STAGE", "review"
        ), patch.object(detail, "review_needs_retry", return_value=False):
            self.assertEqual(detail.detail_browser_preflight_candidates([target]), [])

    def test_canary_supports_five_sku_batch_without_writing_outputs(self):
        skus = [str(6673600 + index) for index in range(5)]
        targets = [
            {
                "sku_id": sku,
                "product_url": PDP_URL.replace(SKU, sku),
            }
            for sku in skus
        ]
        response_json = []
        for sku in skus:
            item = {"data": {"productBySkuId": {"skuId": sku, "reviews": {"results": []}}}}
            response_json.extend([item, item])
        post_result = (
            200,
            json.dumps(response_json),
            response_json,
            {
                "browser_url": "https://www.bestbuy.com/?intl=nosplash",
                "browser_session_kind": "home_origin",
            },
            0.25,
        )

        with patch.object(detail, "FETCH_COMPARE", False), patch.object(
            detail, "browser_graphql_post", return_value=post_result
        ) as post_mock:
            report = detail.run_detail_browser_graphql_canary(targets, 5, canary_only=True)

        self.assertEqual(report["sku_count"], 5)
        self.assertEqual(report["operation_count"], 10)
        self.assertEqual(report["mode"], "canary_only")
        self.assertEqual(len(post_mock.call_args.args[0]), 10)

    def test_semantic_canary_failure_retries_once_in_same_session(self):
        target = {"sku_id": SKU, "product_url": PDP_URL}
        wrong = [
            {"data": {"productBySkuId": {"skuId": "1234567"}}},
            {"data": {"productBySkuId": {"skuId": SKU, "reviews": {"results": []}}}},
        ]
        good_item = {"data": {"productBySkuId": {"skuId": SKU, "reviews": {"results": []}}}}
        good = [good_item, good_item]

        def post_result(response_json):
            return (
                200,
                json.dumps(response_json),
                response_json,
                {
                    "browser_url": "https://www.bestbuy.com/?intl=nosplash",
                    "browser_session_kind": "home_origin",
                },
                0.1,
            )

        with patch.object(detail, "FETCH_COMPARE", False), patch.object(
            detail,
            "browser_graphql_post",
            side_effect=[post_result(wrong), post_result(good)],
        ) as post_mock, patch.object(detail, "BROWSER_GRAPHQL_CANARY_MAX_ATTEMPTS", 2), patch.object(
            detail, "BROWSER_GRAPHQL_RECOVERY_BACKOFF_SECONDS", 0
        ):
            report = detail.run_detail_browser_graphql_canary([target], 1, canary_only=True)

        self.assertEqual(post_mock.call_count, 2)
        self.assertEqual(report["attempts"], 2)

    def test_browser_fetch_keeps_relative_gateway_and_includes_credentials(self):
        class FetchPage:
            def __init__(self):
                self.script = ""

            def run_js(self, script, timeout=None):
                self.script = script
                return json.dumps({"status": 200, "contentType": "application/json", "body": "{}"})

        page = FetchPage()
        browser_session.browser_fetch_graphql(page, [{"query": "q"}])

        self.assertIn("fetch('/gateway/graphql'", page.script)
        self.assertIn("credentials:'include'", page.script)
        self.assertNotIn("referer", page.script.lower())

    def test_exhausted_transport_recovery_raises_fatal_and_clears_session(self):
        self.install_page(object(), ready=True, current_url=PDP_URL)

        with patch.object(
            detail,
            "browser_fetch_graphql",
            side_effect=RuntimeError("browser fetch returned empty result"),
        ), patch.object(detail, "recover_detail_browser_session"), patch.object(
            detail, "BROWSER_GRAPHQL_MAX_RECOVERIES", 2
        ), patch.object(detail, "BROWSER_GRAPHQL_RECOVERY_BACKOFF_SECONDS", 0):
            with self.assertRaises(detail.DetailBrowserUnavailable):
                detail.browser_graphql_post([{"query": "q"}], STRIPPED_PDP_URL, SKU)

        self.assertEqual(detail.BROWSER_GRAPHQL_CURRENT_URL, "")
        self.assertFalse(detail.BROWSER_GRAPHQL_SESSION_READY)

    def test_systemic_http_statuses_raise_fatal_instead_of_becoming_item_failures(self):
        for status_code in (0, 403, 429, 500):
            with self.subTest(status_code=status_code):
                self.install_page(object(), ready=True, current_url=PDP_URL)
                envelope = {
                    "status": status_code,
                    "contentType": "text/plain",
                    "body": "blocked",
                }
                with patch.object(detail, "browser_fetch_graphql", return_value=envelope), patch.object(
                    detail, "BROWSER_GRAPHQL_MAX_RECOVERIES", 0
                ):
                    with self.assertRaises(detail.DetailBrowserUnavailable):
                        detail.browser_graphql_post([{"query": "q"}], STRIPPED_PDP_URL, SKU)

    def test_process_restart_reuses_primary_profile_and_fresh_fallback_does_not(self):
        calls = []

        def fake_create_browser_page(**kwargs):
            calls.append(kwargs)
            return object(), {}

        self.install_page(None)
        detail.BROWSER_GRAPHQL_PROCESS_GENERATION = 1
        with patch.object(detail, "create_browser_page", side_effect=fake_create_browser_page):
            detail.BROWSER_GRAPHQL_PROFILE_KIND = "primary"
            detail.open_detail_browser_page()
            primary_call = calls[-1]
            detail.BROWSER_GRAPHQL_PAGE = None
            detail.BROWSER_GRAPHQL_PROCESS_GENERATION = 2
            detail.BROWSER_GRAPHQL_PROFILE_KIND = "recovery"
            detail.BROWSER_GRAPHQL_RECOVERY_PROFILE_GENERATION = 1
            detail.open_detail_browser_page()
            recovery_call = calls[-1]
            detail.BROWSER_GRAPHQL_PAGE = None
            detail.BROWSER_GRAPHQL_PROCESS_GENERATION = 3
            detail.open_detail_browser_page()
            restarted_recovery_call = calls[-1]
            detail.BROWSER_GRAPHQL_PAGE = None
            detail.BROWSER_GRAPHQL_PROCESS_GENERATION = 4
            detail.BROWSER_GRAPHQL_RECOVERY_PROFILE_GENERATION = 2
            detail.open_detail_browser_page()
            fresh_recovery_call = calls[-1]

        base_name = detail.detail_browser_base_name()
        self.assertEqual(primary_call["profile_name"], base_name)
        self.assertIn("process_1", primary_call["name"])
        self.assertEqual(primary_call["local_port"], 0)
        self.assertNotEqual(recovery_call["profile_name"], base_name)
        self.assertIn(detail.RUN_BATCH_ID, recovery_call["profile_name"])
        self.assertEqual(recovery_call["profile_name"], restarted_recovery_call["profile_name"])
        self.assertNotEqual(recovery_call["profile_name"], fresh_recovery_call["profile_name"])

    def test_fatal_browser_error_escapes_batches_before_single_fallback(self):
        fatal = detail.DetailBrowserUnavailable("transport unavailable")
        target = {"sku_id": SKU, "product_url": PDP_URL}
        detail_paths = {"meta": MemoryPath()}
        review_paths = {"meta": MemoryPath(), "request": MemoryPath()}
        common = (
            patch.object(detail, "fetch_transports", return_value=["browser_graphql"]),
            patch.object(detail, "browser_graphql_post", side_effect=fatal),
            patch.object(detail, "next_attempt", return_value=1),
            patch.object(detail, "attempt_cap_blocks_retry", return_value=False),
        )
        with common[0], common[1], common[2], common[3], patch.object(
            detail, "detail_success", return_value=False
        ), patch.object(detail, "detail_paths", return_value=detail_paths), patch.object(
            detail,
            "detail_batch_request_entries",
            return_value=([{"query": "q"}], [{"pdp_url": PDP_URL, "sku": SKU}]),
        ):
            with self.assertRaises(detail.DetailBrowserUnavailable):
                detail.fetch_detail_sku_batch(None, [target])

        single_fallback = Mock()
        with patch.object(detail, "fetch_transports", return_value=["browser_graphql"]), patch.object(
            detail, "browser_graphql_post", side_effect=fatal
        ), patch.object(detail, "next_attempt", return_value=1), patch.object(
            detail, "review_needs_retry", return_value=True
        ), patch.object(detail, "review_paths", return_value=review_paths), patch.object(
            detail, "review_paths_for_status", return_value=review_paths
        ), patch.object(detail, "review20_payload_for_sku", return_value={"query": "q"}), patch.object(
            detail, "fetch_review20", single_fallback
        ):
            with self.assertRaises(detail.DetailBrowserUnavailable):
                detail.fetch_review20_batch(None, [target])
        single_fallback.assert_not_called()

    def test_fullrun_preserves_python_exit_code_through_tee(self):
        bat_path = Path(__file__).parents[1] / "run_bestbuy_fullrun.bat"
        source = bat_path.read_text(encoding="utf-8")
        self.assertIn("Tee-Object -FilePath '%LOG_FILE%' -Append; exit $LASTEXITCODE", source)


if __name__ == "__main__":
    unittest.main()
