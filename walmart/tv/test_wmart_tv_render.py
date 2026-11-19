import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from walmart.tv.wmart_tv_render import WalmartTVBrowserRenderer

URL = 'https://www.walmart.com/ip/example/123?classType=REGULAR'

def html(item='123', scope='REGULAR', zip_code='11581'):
    data = {'query': {'classType': scope}, 'props': {'pageProps': {'initialData': {
        'product': {'usItemId': item, 'priceInfo': {'currentPrice': {'price': 100}},
                    'location': {'postalCode': zip_code, 'intent': 'SHIPPING'}},
        'contentLayout': {'pageMetadata': {'location': {'postalCode': zip_code,
                            'intent': 'SHIPPING', 'storeId': '5293'}}},
    }}}}
    return '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'

class Clock:
    def __init__(self): self.now = 0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds

class Page:
    def __init__(self, contents): self.contents, self.urls = contents, []
    def get(self, url, **kwargs): self.urls.append(url)
    @property
    def html(self):
        if len(self.contents) > 1: return self.contents.pop(0)
        return self.contents[0]
    @property
    def url(self): return self.urls[-1]

class Browser:
    def __init__(self, page): self.latest_tab, self.quits = page, 0
    def quit(self, **kwargs): self.quits += 1

class RenderTests(unittest.TestCase):
    def test_zip_ui_waits_for_hydration_and_sets_store_then_shipping(self):
        renderer = WalmartTVBrowserRenderer()
        banner = Mock(text='Sacramento, 95829')
        banner.attr.return_value = 'false'
        selected_banner = Mock(text='Valley Stream, 11581')
        selected_banner.attr.return_value = 'false'
        store, field, result, save, shipping = [Mock() for _ in range(5)]
        renderer._page = Mock()
        renderer._page.ele.side_effect = [banner, store, field, result, save, selected_banner, shipping]
        with patch('walmart.tv.wmart_tv_render.time.sleep'):
            self.assertTrue(renderer._set_zip())
        self.assertEqual(renderer._page.ele.call_args_list[0].kwargs['timeout'], 60)
        self.assertIn('5293-', renderer._page.ele.call_args_list[3].args[0])
        field.input.assert_called_once_with('11581', clear=True)
        result.click.assert_called_once(); save.click.assert_called_once()
        shipping.click.assert_called_once()

    def test_zip_ui_does_not_click_hidden_store_result(self):
        renderer = WalmartTVBrowserRenderer()
        banner, store, field, result = [Mock() for _ in range(4)]
        banner.attr.return_value = 'false'
        result.wait.clickable.return_value = False
        renderer._page = Mock()
        renderer._page.ele.side_effect = [banner, store, field, result]
        self.assertFalse(renderer._set_zip())
        result.wait.clickable.assert_called_once_with(timeout=15)
        result.click.assert_not_called()
        self.assertEqual(renderer._page.ele.call_count, 4)

    def test_zip_ui_does_not_click_hidden_shipping(self):
        renderer = WalmartTVBrowserRenderer()
        banner = Mock(text='Sacramento, 95829'); banner.attr.return_value = 'false'
        selected = Mock(text='Valley Stream, 11581'); selected.attr.return_value = 'true'
        store, field, result, save, shipping = [Mock() for _ in range(5)]
        shipping.wait.clickable.return_value = False
        renderer._page = Mock()
        renderer._page.ele.side_effect = [banner, store, field, result, save, selected, shipping]
        with patch('walmart.tv.wmart_tv_render.time.sleep'):
            self.assertFalse(renderer._set_zip())
        shipping.wait.clickable.assert_called_once_with(timeout=10)
        shipping.click.assert_not_called()

    def test_zip_ui_does_not_report_success_when_save_keeps_old_zip(self):
        clock = Clock(); renderer = WalmartTVBrowserRenderer()
        banner = Mock(text='Sacramento, 95829')
        banner.attr.return_value = 'true'
        renderer._page = Mock()
        renderer._page.ele.return_value = banner
        with (patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic),
              patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep),
              patch('walmart.tv.wmart_tv_render.random.uniform', return_value=2)):
            self.assertFalse(renderer._set_zip())
        self.assertEqual(clock.now, 22)

    def make(self, page, clock):
        renderer = WalmartTVBrowserRenderer()
        browser = Browser(page)
        renderer._open_browser = lambda: browser
        renderer._set_zip = lambda: False
        return renderer, browser

    def test_explicit_block_skips_the_wait_without_reopening_the_page(self):
        clock = Clock()
        page = Page(['<html><head><title>Robot or human?</title></head></html>'])
        renderer, _ = self.make(page, clock)
        with (patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic),
              patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep)):
            meta, body = renderer.fetch_html(URL)
        self.assertTrue(meta['blocked'])
        self.assertFalse(body)
        self.assertEqual(clock.now, 0)
        self.assertEqual(page.urls, [URL])

    def test_first_challenge_waits_same_page_for_300_seconds(self):
        clock = Clock(); page = Page(['Robot or human']); renderer, _ = self.make(page, clock)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            meta, body = renderer.fetch_html(URL)
        self.assertEqual(clock.now, 300)
        self.assertEqual(page.urls, [URL])
        self.assertTrue(meta['blocked'])
        self.assertFalse(meta['content_valid'])
        self.assertEqual(body, '')

    def test_ready_after_challenge_and_location_are_recorded(self):
        clock = Clock(); page = Page(['Robot or human', html()]); renderer, _ = self.make(page, clock)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            meta, body = renderer.fetch_html(URL)
        self.assertEqual(page.urls, [URL])
        self.assertTrue(meta['content_valid']); self.assertTrue(meta['location_verified'])
        self.assertEqual(meta['location_zip'], '11581'); self.assertIsNone(meta['status'])
        self.assertIn('__NEXT_DATA__', body)

    def test_navigation_context_change_waits_without_reload(self):
        from DrissionPage.errors import ContextLostError
        clock = Clock()
        class NavigatingPage(Page):
            reads = 0
            @property
            def html(self):
                self.reads += 1
                if self.reads == 1:
                    raise ContextLostError()
                return html()
        page = NavigatingPage([])
        renderer, _ = self.make(page, clock)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            meta, body = renderer.fetch_html(URL)
        self.assertTrue(meta['content_valid'])
        self.assertEqual(page.urls, [URL])
        self.assertEqual(clock.now, 1)

    def test_wrong_item_scope_and_unknown_location(self):
        for content in (html(item='999'), html(scope='VARIANT'), html().replace('REGULAR', '')):
            clock = Clock(); page = Page([content]); renderer, _ = self.make(page, clock)
            with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
                meta, _ = renderer.fetch_html(URL)
            self.assertFalse(meta['content_valid'])
        clock = Clock(); page = Page([html(zip_code='95829')]); renderer, _ = self.make(page, clock)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            meta, _ = renderer.fetch_html(URL)
        self.assertTrue(meta['content_valid']); self.assertFalse(meta['location_verified'])
        self.assertEqual(meta['location_zip'], '95829')

    def test_spacing_subsequent_timeout_and_close(self):
        clock = Clock(); page = Page([html()]); renderer, browser = self.make(page, clock)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
            renderer.fetch_html(URL)
            page.contents = ['Robot or human']
            meta, _ = renderer.fetch_html(URL)
            self.assertEqual(clock.now, 100)
        self.assertEqual(page.urls, [URL, URL]); self.assertTrue(meta['blocked'])
        renderer.close(); renderer.close(); self.assertEqual(browser.quits, 1)

    def test_startup_failure_preserves_first_browser_wait(self):
        clock = Clock(); page = Page(['Robot or human']); renderer, browser = self.make(page, clock)
        def fail(): raise RuntimeError('startup error')
        renderer._open_browser = fail
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            meta, _ = renderer.fetch_html(URL)
            renderer._open_browser = lambda: browser
            renderer.fetch_html(URL)
        self.assertFalse(meta['content_valid']); self.assertEqual(clock.now, 300)

    def test_zip_setup_failure_preserves_unverified_content(self):
        clock = Clock(); page = Page([html(zip_code='95829')]); renderer, _ = self.make(page, clock)
        def fail(): raise RuntimeError('zip failed')
        renderer._set_zip = fail
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            meta, body = renderer.fetch_html(URL)
        self.assertTrue(meta['content_valid']); self.assertFalse(meta['location_verified'])
        self.assertIn('__NEXT_DATA__', body)

    def test_broken_browser_is_disposed_and_next_call_restarts(self):
        clock = Clock(); page = Page([html()]); renderer, browser = self.make(page, clock)
        original_get = page.get
        def fail(*args, **kwargs): raise RuntimeError('closed')
        page.get = fail
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
            meta, _ = renderer.fetch_html(URL)
            page.get = original_get
            result, _ = renderer.fetch_html(URL)
        self.assertFalse(meta['content_valid']); self.assertTrue(result['content_valid'])
        self.assertEqual(browser.quits, 1)

    def test_review_requires_same_id_and_scope(self):
        url = 'https://www.walmart.com/reviews/product/123?classType=REGULAR'
        for item, scope, valid in [('123', 'REGULAR', True), ('999', 'REGULAR', False), ('123', 'VARIANT', False)]:
            data = {'query': {'id': item, 'classType': scope}, 'payload': {'customerReviews': []}}
            review_html = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'
            page = Page([html()]); clock = Clock(); renderer, _ = self.make(page, clock)
            with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
                detail, _ = renderer.fetch_html(URL)
                self.assertTrue(detail['location_verified'])
                page.contents = [review_html]
                meta, _ = renderer.fetch_html(url)
            self.assertEqual(meta['content_valid'], valid)
            if valid:
                self.assertTrue(meta['location_verified'])
                self.assertEqual(meta['location_zip'], '11581')

    def test_review_page_two_rejects_stale_page_one(self):
        url = 'https://www.walmart.com/reviews/product/123?classType=REGULAR&page=2'
        for page_number, valid in [(None, False), ('1', False), ('2', True)]:
            query = {'id': '123', 'classType': 'REGULAR'}
            if page_number is not None:
                query['page'] = page_number
            data = {'query': query, 'payload': {'customerReviews': []}}
            review_html = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'
            page = Page([html()]); clock = Clock(); renderer, _ = self.make(page, clock)
            with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
                renderer.fetch_html(URL)
                page.contents = [review_html]
                meta, _ = renderer.fetch_html(url)
            self.assertEqual(meta['content_valid'], valid)

    def test_review_without_verified_session_preserves_content(self):
        url = 'https://www.walmart.com/reviews/product/123?classType=REGULAR'
        data = {'query': {'id': '123', 'classType': 'REGULAR'}, 'payload': {'customerReviews': []}}
        page = Page(['<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'])
        clock = Clock(); renderer, _ = self.make(page, clock)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            meta, body = renderer.fetch_html(url)
        self.assertTrue(meta['content_valid']); self.assertFalse(meta['location_verified'])
        self.assertIn('__NEXT_DATA__', body)

    def test_changed_zip_invalidates_previously_verified_review_session(self):
        review_url = 'https://www.walmart.com/reviews/product/123?classType=REGULAR'
        data = {'query': {'id': '123', 'classType': 'REGULAR'}, 'payload': {'customerReviews': []}}
        page = Page([html()]); clock = Clock(); renderer, _ = self.make(page, clock)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            first, _ = renderer.fetch_html(URL)
            page.contents = [html(zip_code='95829')]
            changed, _ = renderer.fetch_html(URL)
            page.contents = ['<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>']
            review, body = renderer.fetch_html(review_url)
        self.assertTrue(first['location_verified'])
        self.assertTrue(changed['content_valid']); self.assertFalse(changed['location_verified'])
        self.assertTrue(review['content_valid']); self.assertFalse(review['location_verified'])
        self.assertIn('__NEXT_DATA__', body)

    def test_browser_restart_discards_verified_review_session(self):
        review_url = 'https://www.walmart.com/reviews/product/123?classType=REGULAR'
        data = {'query': {'id': '123', 'classType': 'REGULAR'}, 'payload': {'customerReviews': []}}
        page = Page([html()]); clock = Clock(); renderer, _ = self.make(page, clock)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            first, _ = renderer.fetch_html(URL)
            renderer.close()
            page.contents = ['<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>']
            review, body = renderer.fetch_html(review_url)
        self.assertTrue(first['location_verified'])
        self.assertTrue(review['content_valid']); self.assertFalse(review['location_verified'])
        self.assertIn('__NEXT_DATA__', body)

    def test_wrong_shipping_or_store_preserves_unverified_content(self):
        for content in (html().replace('SHIPPING', 'PICKUP'), html().replace('5293', '9999')):
            clock = Clock(); page = Page([content]); renderer, _ = self.make(page, clock)
            with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
                meta, body = renderer.fetch_html(URL)
            self.assertTrue(meta['content_valid']); self.assertFalse(meta['location_verified'])
            self.assertIn('__NEXT_DATA__', body)

    def test_similar_reuses_current_valid_page_without_new_get(self):
        clock = Clock(); page = Page([html()]); renderer, _ = self.make(page, clock)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            renderer.fetch_html(URL)
            meta, _ = renderer.fetch_html(URL, wait_for_similar=True)
        self.assertTrue(meta['content_valid'])
        self.assertEqual(page.urls, [URL])
        self.assertEqual(clock.now, 5)

    def test_similar_does_not_return_stale_page_after_page_changes(self):
        clock = Clock(); page = Page([html(), html(), html(item='999')]); renderer, _ = self.make(page, clock)
        renderer._zip_attempted = True
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            meta, body = renderer.fetch_html(URL, wait_for_similar=True)
        self.assertFalse(meta['content_valid'])
        self.assertEqual(body, '')

    def test_zip_success_revisits_and_checks_new_response(self):
        clock = Clock(); page = Page([html(zip_code='95829')]); renderer, _ = self.make(page, clock)
        def set_zip():
            page.contents = [html()]
            return True
        renderer._set_zip = set_zip
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
            meta, _ = renderer.fetch_html(URL)
        self.assertEqual(page.urls, [URL, URL])
        self.assertEqual(meta['location_zip'], '11581')
        self.assertTrue(meta['location_verified'])

    def test_zip_revisit_failure_preserves_first_valid_response(self):
        clock = Clock(); page = Page([html(zip_code='95829')]); renderer, _ = self.make(page, clock)
        def set_zip():
            page.contents = ['Robot or human']
            return True
        renderer._set_zip = set_zip
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
            meta, body = renderer.fetch_html(URL)
        self.assertTrue(meta['content_valid']); self.assertFalse(meta['location_verified'])
        self.assertEqual(meta['location_zip'], '95829')
        self.assertIn('95829', body)
        self.assertTrue(meta.get('location_warning'))

    def test_zip_revisit_connection_failure_preserves_first_valid_response(self):
        clock = Clock(); page = Page([html(zip_code='95829')]); renderer, browser = self.make(page, clock)
        renderer._set_zip = lambda: True
        original_get = page.get
        def get(url, **kwargs):
            if page.urls:
                raise RuntimeError('connection closed')
            original_get(url, **kwargs)
        page.get = get
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
            meta, body = renderer.fetch_html(URL)
        self.assertTrue(meta['content_valid']); self.assertFalse(meta['location_verified'])
        self.assertEqual(meta['location_zip'], '95829')
        self.assertIn('95829', body)
        self.assertEqual(browser.quits, 1)

    def test_similar_navigates_when_other_worker_moved_page(self):
        clock = Clock(); page = Page([html()]); renderer, _ = self.make(page, clock)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
            renderer.fetch_html(URL)
            page.urls.append('https://www.walmart.com/ip/other/456')
            renderer.fetch_html(URL, wait_for_similar=True)
        self.assertEqual(page.urls[-1], URL)
        self.assertEqual(page.urls.count(URL), 2)

    def test_open_browser_uses_independent_options(self):
        renderer = WalmartTVBrowserRenderer()
        with patch('DrissionPage.ChromiumOptions') as options_cls, patch('DrissionPage.Chromium') as chromium:
            renderer._open_drission_browser()
        options_cls.assert_called_once_with(read_file=False)
        options_cls.return_value.auto_port.assert_called_once_with()
        options_cls.return_value.set_load_mode.assert_called_once_with('none')
        chromium.assert_called_once_with(options_cls.return_value)

    def test_chrome_startup_failure_tries_installed_edge_once(self):
        from DrissionPage.errors import BrowserConnectError

        renderer = WalmartTVBrowserRenderer()
        with TemporaryDirectory() as program_files:
            edge_path = Path(program_files) / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe'
            edge_path.parent.mkdir(parents=True)
            edge_path.touch()
            chrome_options, edge_options, edge = Mock(), Mock(), Mock()
            with (
                patch.dict(os.environ, {'ProgramFiles(x86)': program_files}, clear=True),
                patch('DrissionPage.ChromiumOptions', side_effect=[chrome_options, edge_options]),
                patch('DrissionPage.Chromium', side_effect=[BrowserConnectError('startup failed'), edge]) as launch,
            ):
                self.assertIs(renderer._open_drission_browser(), edge)
            edge_options.set_browser_path.assert_called_once_with(str(edge_path))
            edge_options.auto_port.assert_called_once_with()
            edge_options.set_load_mode.assert_called_once_with('none')
            self.assertEqual([call.args[0] for call in launch.call_args_list], [chrome_options, edge_options])

    def test_chrome_timeout_uses_edge_but_explicit_block_does_not(self):
        for chrome_body, switched in (
            ('', True), ('<title>Robot or human?</title>', False), ('<title>403 Forbidden</title>', False),
            ('<title>429 Too Many Requests</title>', False),
        ):
            with self.subTest(switched=switched):
                clock = Clock()
                chrome = Browser(Page([chrome_body]))
                edge = Browser(Page([html()]))
                renderer = WalmartTVBrowserRenderer(retry_once=True)
                renderer._open_browser = Mock(return_value=chrome)
                renderer._browser_backend = 'chrome'
                with (
                    patch.object(renderer, '_edge_path', return_value=Path('msedge.exe')),
                    patch.object(renderer, '_launch_browser', return_value=edge) as launch,
                    patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic),
                    patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep),
                ):
                    meta, _ = renderer.fetch_html(URL)
                self.assertEqual(meta['content_valid'], switched)
                self.assertEqual(launch.call_count, int(switched))
                self.assertEqual(chrome.quits, int(switched))

    def test_integrated_retries_each_browser_once_before_provider(self):
        clock = Clock()
        chrome = Browser(Page(['']))
        edge = Browser(Page(['']))
        renderer = WalmartTVBrowserRenderer(setup_zip=False, retry_once=True)
        renderer._open_browser = Mock(return_value=chrome)
        renderer._browser_backend = 'chrome'
        with (
            patch.object(renderer, '_edge_path', return_value=Path('msedge.exe')),
            patch.object(renderer, '_launch_browser', return_value=edge) as launch,
            patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic),
            patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep),
        ):
            meta, body = renderer.fetch_html(URL)
        self.assertEqual(chrome.latest_tab.urls, [URL, URL])
        self.assertEqual(edge.latest_tab.urls, [URL, URL])
        launch.assert_called_once_with(Path('msedge.exe'))
        self.assertEqual(meta['error'], 'page readiness timeout')
        self.assertFalse(meta['blocked'])
        self.assertEqual(body, '')

    def test_rendered_search_requires_products_and_rejects_wrong_page(self):
        item = {'name': 'Test TV', 'canonicalUrl': '/ip/test/123', 'usItemId': '123'}
        search = {'itemStacks': [{'title': 'Results for tv', 'items': [item]}]}
        data = {'query': {'page': 2}, 'props': {'pageProps': {'initialData': {'searchResult': search}}}}
        renderer = WalmartTVBrowserRenderer()
        self.assertTrue(renderer._valid('https://www.walmart.com/search?q=tv&page=2', data))
        self.assertFalse(renderer._valid('https://www.walmart.com/search?q=tv&page=1', data))
        search['itemStacks'][0]['items'] = []
        self.assertFalse(renderer._valid('https://www.walmart.com/search?q=tv&page=2', data))

    def test_chrome_missing_without_installed_edge_leaves_transport_fallback(self):
        renderer = WalmartTVBrowserRenderer()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch('DrissionPage.ChromiumOptions'),
            patch('DrissionPage.Chromium', side_effect=FileNotFoundError('startup failed')) as launch,
        ):
            with self.assertRaises(FileNotFoundError):
                renderer._open_drission_browser()
            launch.assert_called_once()

    def test_uc_success_uses_same_browser_for_later_pages(self):
        clock = Clock()
        renderer = WalmartTVBrowserRenderer()
        with (
            patch('amazon.tv.uc_page.make_uc_driver') as make,
            patch.object(renderer, '_open_drission_browser') as fallback,
            patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic),
            patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep),
        ):
            driver = make.return_value
            driver.current_url = URL
            driver.page_source = html()
            for _ in range(2):
                meta, _ = renderer.fetch_html(URL)
                self.assertTrue(meta['content_valid'])
                self.assertEqual(meta['browser_backend'], 'uc')
            renderer.close()
        make.assert_called_once_with(page_load_strategy='eager')
        self.assertEqual(driver.get.call_count, 2)
        driver.quit.assert_called_once_with()
        fallback.assert_not_called()

    def test_uc_startup_navigation_and_readiness_failures_use_drission_for_same_url(self):
        from selenium.common.exceptions import WebDriverException

        for failure in ('startup', 'navigation', 'readiness', 'product_id_only', 'challenge_timeout'):
            with self.subTest(failure=failure):
                clock = Clock()
                renderer = WalmartTVBrowserRenderer(retry_once=True)
                drission = Browser(Page([html()]))
                driver = Mock(current_url=URL, page_source='')
                if failure == 'navigation':
                    driver.get.side_effect = WebDriverException('disconnected')
                if failure == 'product_id_only':
                    driver.page_source = html().replace('"price": 100', '"price": null')
                if failure == 'challenge_timeout':
                    driver.page_source = 'Robot or human'

                def start_drission():
                    self.assertEqual(driver.quit.call_count, int(failure != 'startup'))
                    renderer._browser_backend = 'chrome'
                    return drission

                with (
                    patch('amazon.tv.uc_page.make_uc_driver', return_value=driver,
                          side_effect=OSError('driver unavailable') if failure == 'startup' else None),
                    patch.object(renderer, '_open_drission_browser', side_effect=start_drission) as fallback,
                    patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic),
                    patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep),
                ):
                    meta, _ = renderer.fetch_html(URL)
                self.assertTrue(meta['content_valid'])
                self.assertEqual(meta['browser_backend'], 'chrome')
                self.assertEqual(drission.latest_tab.urls, [URL])
                fallback.assert_called_once_with()

    def test_uc_explicit_block_preserves_block_result(self):
        renderer = WalmartTVBrowserRenderer(retry_once=True)
        with (
            patch('amazon.tv.uc_page.make_uc_driver') as make,
            patch.object(renderer, '_open_drission_browser') as fallback,
        ):
            make.return_value.current_url = URL
            make.return_value.page_source = '<title>403 Forbidden</title>'
            meta, body = renderer.fetch_html(URL)
        self.assertTrue(meta['blocked'])
        self.assertEqual(meta['error'], 'explicit_block')
        self.assertEqual(body, '')
        fallback.assert_not_called()

    def test_concurrent_navigation_does_not_overlap(self):
        page = Page([html()]); renderer, _ = self.make(page, Clock())
        active = 0; maximum = 0; guard = threading.Lock()
        def get(url, **kwargs):
            nonlocal active, maximum
            with guard: active += 1; maximum = max(maximum, active)
            time.sleep(0.03)
            page.urls.append(url)
            with guard: active -= 1
        page.get = get
        with patch('walmart.tv.wmart_tv_render.random.uniform', return_value=0):
            threads = [threading.Thread(target=renderer.fetch_html, args=(URL,)) for _ in range(2)]
            for t in threads: t.start()
            for t in threads: t.join()
        self.assertEqual(maximum, 1)

if __name__ == '__main__': unittest.main()
