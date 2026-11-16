import json
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
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            self.assertFalse(renderer._set_zip())
        self.assertGreaterEqual(clock.now, 21)
        self.assertLessEqual(clock.now, 22)

    def make(self, page, clock):
        renderer = WalmartTVBrowserRenderer()
        browser = Browser(page)
        renderer._open_browser = lambda: browser
        renderer._set_zip = lambda: False
        return renderer, browser

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
            renderer._open_browser()
        options_cls.assert_called_once_with(read_file=False)
        options_cls.return_value.auto_port.assert_called_once_with()
        options_cls.return_value.set_load_mode.assert_called_once_with('none')
        chromium.assert_called_once_with(options_cls.return_value)

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
