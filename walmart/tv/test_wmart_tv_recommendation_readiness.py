"""Offline regression for already-available recommendation JSON readiness."""
import json
import unittest
from unittest.mock import patch
from walmart.tv.test_wmart_tv_render import Browser, Clock, Page, html
from walmart.tv.wmart_tv_next_data import extract_next_data, get_initial_data, parse_similar_product_names
from walmart.tv.wmart_tv_render import WalmartTVBrowserRenderer

URL = 'https://www.walmart.com/ip/123'

def body_with_json(item='123', has_similar=True):
    data = extract_next_data(html(item=item))
    if has_similar:
        get_initial_data(data).setdefault('contentLayout', {})['modules'] = [{
            'type': 'ItemCarousel', 'configs': {'title': 'Similar items you might like',
                'products': [{'usItemId': '234', 'name': 'Nearby model'}]}}]
    return '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'

class InitialPage(Page):
    @property
    def url(self):
        return self.urls[-1] if self.urls else 'about:blank'

class RecommendationReadinessTests(unittest.TestCase):
    def run_render(self, body):
        page = InitialPage([body]); clock = Clock(); renderer = WalmartTVBrowserRenderer()
        renderer._open_browser = lambda: Browser(page)
        renderer._set_zip = lambda: False
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
            meta, actual = renderer.fetch_html(URL, wait_for_similar=True)
        return clock, page, meta, actual

    def test_same_product_json_recommendations_need_no_extra_wait_or_navigation(self):
        body = body_with_json()
        self.assertEqual(parse_similar_product_names(extract_next_data(body), current_item='123'), 'Nearby model')
        clock, page, meta, actual = self.run_render(body)
        self.assertTrue(meta['content_valid'])
        self.assertEqual(page.urls, [URL])
        self.assertEqual(clock.now, 0)
        self.assertEqual(parse_similar_product_names(extract_next_data(actual), current_item='123'), 'Nearby model')

    def test_no_recommendations_still_waits_for_dynamic_content(self):
        clock, page, meta, actual = self.run_render(body_with_json(has_similar=False))
        self.assertTrue(meta['content_valid'])
        self.assertEqual(page.urls, [URL])
        self.assertEqual(clock.now, 5)
        self.assertIsNone(parse_similar_product_names(extract_next_data(actual), current_item='123'))

    def test_recommendations_for_another_product_cannot_be_accepted(self):
        clock, page, meta, actual = self.run_render(body_with_json(item='999'))
        self.assertFalse(meta['content_valid'])
        self.assertEqual(actual, '')
        self.assertEqual(page.urls, [URL])

if __name__ == '__main__': unittest.main()
