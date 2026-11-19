"""Shared Walmart browser transport for listings, detail and reviews."""
import os
from pathlib import Path
import random
import threading
import time
from urllib.parse import parse_qs, urlparse

from walmart.tv.wmart_tv_next_data import (
    WalmartNextDataClient, _response_location_confirmed, extract_next_data, find_reviews_node,
    get_initial_data, has_listing_products, is_blocked_html, is_explicit_block_page, item_id_from_url,
    product_scope_query_params,
    response_zip, review_response_scope_error, parse_similar_product_names_from_html,
    parse_similar_product_names, _transport_log,
)


class WalmartTVBrowserRenderer:
    def __init__(self, *, setup_zip=True, retry_once=False):
        self._setup_zip = setup_zip
        self._retry_once = retry_once
        self._lock = threading.Lock()
        self._browser = None
        self._browser_backend = None
        self._page = None
        self._initial_attempted = False
        self._zip_attempted = False
        self._location_verified = False
        self._last_navigation = None

    @staticmethod
    def _edge_path():
        for variable in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
            install_dir = os.environ.get(variable)
            if not install_dir:
                continue
            edge = Path(install_dir) / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe'
            if edge.is_file():
                return edge
        return None

    def _launch_browser(self, edge_path=None):
        from DrissionPage import Chromium, ChromiumOptions

        options = ChromiumOptions(read_file=False)
        options.auto_port()
        if edge_path is not None:
            options.set_browser_path(str(edge_path))
        options.set_load_mode('none')
        options.set_timeouts(page_load=30)
        browser = Chromium(options)
        self._browser_backend = 'edge' if edge_path is not None else 'chrome'
        return browser

    def _open_browser(self):
        try:
            from amazon.tv.uc_page import UCPage, make_uc_driver

            browser = UCPage(make_uc_driver(page_load_strategy='eager'))
            self._browser_backend = 'uc'
            print('[BROWSER] Starting Walmart TV collection with UC')
            return browser
        except Exception as error:
            print(f'[BROWSER FALLBACK] UC startup error={type(error).__name__}; starting DrissionPage')
            return self._open_drission_browser()

    def _open_drission_browser(self):
        from DrissionPage.errors import BrowserConnectError

        try:
            return self._launch_browser()
        except (BrowserConnectError, OSError) as error:
            edge_path = self._edge_path()
            if edge_path is None:
                raise
            print(f'[BROWSER FALLBACK] Chrome startup error={type(error).__name__}; starting Edge')
            return self._launch_browser(edge_path)

    def _set_zip(self):
        # The banner hydrates after the embedded product data becomes available.
        banner = self._page.ele('css:button[data-automation-id="fulfillment-banner"]', timeout=60)
        if not banner or not banner.wait.clickable(timeout=15):
            return False
        if banner.attr('aria-expanded') != 'true':
            banner.click()
        store = self._page.ele(
            'xpath:(//*[@role="dialog"]//button[contains(., "Supercenter") or contains(., " Store")])[1]', timeout=15)
        if not store or not store.wait.clickable(timeout=15):
            return False
        store.click()
        field = self._page.ele('css:input[name="zipCodeOrCityState"]', timeout=15)
        if not field:
            return False
        field.input('11581', clear=True)
        store = self._page.ele(
            'xpath://label[@data-automation-id="pickup-store" and starts-with(@for,"5293-") and contains(.,"11581")]', timeout=20)
        if not store or not store.wait.clickable(timeout=15):
            return False
        store.click()
        time.sleep(random.uniform(1, 2))
        self._page.ele('css:button[data-automation-id="save-label"]', timeout=10).click()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            banner = self._page.ele('css:button[data-automation-id="fulfillment-banner"]', timeout=1)
            if banner and '11581' in (banner.text or ''):
                if banner.attr('aria-expanded') != 'true':
                    banner.click()
                shipping = self._page.ele('css:button[aria-label="Shipping"]', timeout=10)
                if shipping and shipping.wait.clickable(timeout=10):
                    shipping.click()
                    time.sleep(3)
                    return True
                return False
            time.sleep(1)
        return False

    def _navigate(self, url):
        if self._last_navigation is not None:
            remaining = random.uniform(10, 15) - (time.monotonic() - self._last_navigation)
            if remaining > 0:
                time.sleep(remaining)
        self._last_navigation = time.monotonic()
        if self._browser_backend == 'uc':
            self._page.get(url)
        else:
            self._page.get(url, retry=0, timeout=30)

    @staticmethod
    def _valid(url, data, final_url=None):
        if not isinstance(data, dict):
            return False
        item = item_id_from_url(url)
        query = data.get('query') or {}
        if not isinstance(query, dict):
            return False
        if any(str(query.get(k) or '') != v
               for k, v in product_scope_query_params(url).items()):
            return False
        path = urlparse(url).path
        if path.rstrip('/') == '/search':
            requested_page = parse_qs(urlparse(url).query).get('page', ['1'])[0]
            return has_listing_products(data, requested_page, final_url)
        if path.startswith('/reviews/'):
            requested_page = parse_qs(urlparse(url).query).get('page', ['1'])[0]
            response_page = str(query.get('page') or '1')
            return bool(item and requested_page == response_page
                        and not review_response_scope_error(data, item, url)
                        and find_reviews_node(data) is not None)
        product = get_initial_data(data).get('product') or {}
        if not isinstance(product, dict):
            return False
        actual = product.get('usItemId') or item_id_from_url(product.get('canonicalUrl'))
        return bool(item and actual and str(actual) == item
                    and WalmartNextDataClient._usable_detail_or_review(url, data))

    def _wait_ready(self, url, timeout, require_location=False):
        from DrissionPage.errors import ContextLostError
        started = time.monotonic()
        deadline = started + timeout
        _transport_log(url, 'browser_readiness', 0, None, 'waiting',
                       'browser_result', event='readiness_start', budget_s=timeout)
        blocked = False
        challenge_seen = False
        reason = 'next_data_missing'
        while True:
            try:
                body = self._page.html or ''
                current_url = self._page.url
                blocked = is_blocked_html(body, current_url)
                challenge_seen = challenge_seen or blocked
                if blocked and is_explicit_block_page(body, current_url):
                    _transport_log(url, 'browser_readiness', time.monotonic() - started, None,
                                   'explicit_block', 'browser_result', event='readiness')
                    return '', None, 'explicit_block'
            except ContextLostError:
                body = ''
                blocked = False
            data = extract_next_data(body) if not blocked else None
            if blocked:
                reason = 'blocked'
            elif data:
                reason = 'response_incomplete_or_mismatched'
            else:
                reason = 'next_data_missing'
            if data and self._valid(url, data, current_url):
                if not require_location or _response_location_confirmed(url, data):
                    _transport_log(url, 'browser_readiness', time.monotonic() - started, None,
                                   'ready', 'browser_processing', event='readiness')
                    return body, data, None
                reason = 'location_unverified'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _transport_log(url, 'browser_readiness', time.monotonic() - started, None, reason,
                               'browser_result', event='readiness')
                return '', None, 'challenge timeout' if challenge_seen else None
            time.sleep(min(1, remaining))

    def _read_ready(self, url, *, reuse=False, wait_for_similar=False):
        retry = self._retry_once and not wait_for_similar
        attempts = 2 if retry else 1
        for attempt in range(attempts):
            if attempt or not reuse:
                self._navigate(url)
            first = not self._initial_attempted
            self._initial_attempted = True
            timeout = 30 if retry else 300 if first else 90
            body, data, block_reason = self._wait_ready(url, timeout)
            if data or block_reason or attempt + 1 == attempts:
                return body, data, block_reason
            print(f"[BROWSER RETRY] backend={self._browser_backend} reason=page_readiness_timeout attempt=2/2")

    def _read_browser(self, url, *, reuse, wait_for_similar):
        from DrissionPage.errors import BrowserConnectError, PageDisconnectedError

        if self._browser_backend == 'uc':
            from selenium.common.exceptions import WebDriverException

            try:
                body, data, block_reason = self._read_ready(
                    url, reuse=reuse, wait_for_similar=wait_for_similar,
                )
                if data or block_reason == 'explicit_block':
                    return body, data, block_reason
                reason = block_reason or 'page readiness timeout'
            except WebDriverException as error:
                reason = type(error).__name__

            print(f'[BROWSER FALLBACK] UC {reason}; starting DrissionPage')
            self._close_browser()
            self._browser = self._open_drission_browser()
            self._page = self._browser.latest_tab
            reuse = False

        chrome = self._browser_backend == 'chrome'
        connection_error = None
        try:
            body, data, block_reason = self._read_ready(
                url, reuse=reuse, wait_for_similar=wait_for_similar,
            )
        except (BrowserConnectError, PageDisconnectedError) as error:
            if not self._retry_once or not chrome or wait_for_similar:
                raise
            body, data, block_reason = '', None, None
            connection_error = error

        if chrome and not data and block_reason is None and not wait_for_similar:
            edge_path = self._edge_path()
            if edge_path is not None:
                reason = type(connection_error).__name__ if connection_error else 'page readiness timeout'
                print(f'[BROWSER FALLBACK] Chrome {reason}; starting Edge')
                self._close_browser()
                self._browser = self._launch_browser(edge_path)
                self._page = self._browser.latest_tab
                return self._read_ready(url)
        if connection_error is not None:
            raise connection_error
        return body, data, block_reason

    def fetch_html(self, url, wait_for_similar=False):
        with self._lock:
            meta = {'source': 'browser_render', 'status': None, 'content_valid': False,
                    'blocked': False, 'error': None, 'location_verified': False,
                    'location_zip': None}
            try:
                if self._browser is None:
                    self._browser = self._open_browser()
                    self._page = (self._browser if self._browser_backend == 'uc'
                                  else self._browser.latest_tab)
                is_review = urlparse(url).path.startswith('/reviews/')
                reuse = False
                if wait_for_similar and self._page.url == url:
                    current = self._page.html or ''
                    reuse = (not is_blocked_html(current, self._page.url)
                             and self._valid(url, extract_next_data(current)))
                body, data, block_reason = self._read_browser(
                    url, reuse=reuse, wait_for_similar=wait_for_similar,
                )
                meta['browser_backend'] = self._browser_backend
                meta['blocked'] = bool(block_reason)
                if not data:
                    meta['error'] = block_reason or 'page readiness timeout'
                    return meta, ''

                meta['location_zip'] = response_zip(data)
                if (self._setup_zip and not is_review and not self._zip_attempted
                        and not _response_location_confirmed(url, data)):
                    self._zip_attempted = True
                    self._location_verified = False
                    try:
                        zip_set = self._set_zip()
                    except Exception as exc:
                        print(f'[LOCATION WARNING] Browser ZIP setup failed: {type(exc).__name__}')
                        zip_set = False
                    if zip_set:
                        try:
                            self._navigate(url)
                            updated_body, updated_data, _ = self._wait_ready(url, 90)
                        except Exception as exc:
                            print(f'[LOCATION WARNING] ZIP revisit failed: {type(exc).__name__}')
                            self._close_browser()
                            updated_data = None
                        if updated_data:
                            body, data = updated_body, updated_data
                        else:
                            print('[LOCATION WARNING] ZIP revisit failed; keeping previously collected product data')

                if not is_review:
                    self._location_verified = bool(_response_location_confirmed(url, data))
                elif response_zip(data) and response_zip(data) != '11581':
                    self._location_verified = False
                if not self._location_verified:
                    meta['location_warning'] = 'browser ZIP 11581 not verified; keeping collected data'
                    print('[LOCATION WARNING] Browser ZIP 11581 not verified; keeping collected data')

                if wait_for_similar:
                    deadline = time.monotonic() + 5
                    while (not (parse_similar_product_names_from_html(body)
                                or parse_similar_product_names(data, current_item=item_id_from_url(url)))
                           and time.monotonic() < deadline):
                        time.sleep(min(1, deadline - time.monotonic()))
                        candidate = self._page.html or ''
                        candidate_data = extract_next_data(candidate)
                        if is_blocked_html(candidate, self._page.url) or not self._valid(url, candidate_data):
                            self._location_verified = False
                            meta['blocked'] = is_blocked_html(candidate, self._page.url)
                            meta['error'] = 'page changed while waiting for similar items'
                            return meta, ''
                        body, data = candidate, candidate_data
                        self._location_verified = bool(_response_location_confirmed(url, data))
                meta['content_valid'] = True
                meta['location_zip'] = response_zip(data) or ('11581' if self._location_verified else None)
                meta['location_verified'] = self._location_verified
                if not self._location_verified:
                    meta['location_warning'] = 'browser ZIP 11581 not verified; keeping collected data'
                return meta, body
            except Exception as exc:
                meta['error'] = type(exc).__name__
                meta['error_class'] = type(exc).__name__
                self._close_browser()
                return meta, ''

    def _close_browser(self):
        browser, self._browser, self._page = self._browser, None, None
        self._browser_backend = None
        self._zip_attempted = False
        self._location_verified = False
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._close_browser()
