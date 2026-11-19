"""Independent regression for one ZIP setup attempt per owned browser session."""
import unittest
from unittest.mock import Mock, patch
from walmart.tv.test_wmart_tv_render import Browser, Clock, Page, html
from walmart.tv.wmart_tv_render import WalmartTVBrowserRenderer

A = 'https://www.walmart.com/ip/100'
B = 'https://www.walmart.com/ip/200'

class RoutedPage(Page):
    def __init__(self): super().__init__([''])
    def get(self, url, **kwargs):
        self.urls.append(url)
        item = url.rsplit('/', 1)[-1]
        self.contents = [html(item=item, zip_code='95829')]

class ZipAttemptSessionTests(unittest.TestCase):
    def make(self, side_effect):
        page = RoutedPage(); clock = Clock(); renderer = WalmartTVBrowserRenderer()
        renderer._open_browser = lambda: Browser(page)
        renderer._set_zip = Mock(side_effect=side_effect)
        return page, clock, renderer

    def test_one_attempt_across_different_products_and_similar(self):
        for scenario, outcome in [('failure', False), ('exception', RuntimeError('zip error')), ('apparent_success_wrong_zip', True)]:
            with self.subTest(scenario=scenario):
                page, clock, renderer = self.make(outcome if isinstance(outcome, Exception) else lambda: outcome)
                with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
                    meta_a, body_a = renderer.fetch_html(A)
                    meta_b, body_b = renderer.fetch_html(B)
                    meta_similar, body_similar = renderer.fetch_html(B, wait_for_similar=True)
                expected = [A, A, B] if outcome is True else [A, B]
                self.assertEqual(page.urls, expected)
                self.assertEqual(renderer._set_zip.call_count, 1)
                for meta in (meta_a, meta_b, meta_similar):
                    self.assertTrue(meta['content_valid']); self.assertFalse(meta['location_verified'])
                    self.assertEqual(meta['location_zip'], '95829')
                self.assertIn('"usItemId": "100"', body_a)
                self.assertIn('"usItemId": "200"', body_b)
                self.assertIn('"usItemId": "200"', body_similar)

    def test_new_owned_session_may_attempt_zip_again(self):
        page, clock, renderer = self.make(lambda: False)
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
            first, _ = renderer.fetch_html(A)
            renderer.close()
            second, _ = renderer.fetch_html(B)
        self.assertEqual(page.urls, [A, B])
        self.assertEqual(renderer._set_zip.call_count, 2)
        self.assertTrue(first['content_valid']); self.assertTrue(second['content_valid'])

if __name__ == '__main__': unittest.main()
