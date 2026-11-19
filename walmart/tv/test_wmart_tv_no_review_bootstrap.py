"""Offline regression: review fetches must not repeat ZIP setup or detail navigation."""
import json
import unittest
from unittest.mock import Mock, patch

from walmart.tv.test_wmart_tv_render import Browser, Clock, Page, URL, html
from walmart.tv.wmart_tv_render import WalmartTVBrowserRenderer

REVIEW = 'https://www.walmart.com/reviews/product/123?classType=REGULAR'
REVIEW2 = REVIEW + '&page=2'

def review_body(page):
    query = {'id': '123', 'classType': 'REGULAR'}
    if page == 2:
        query['page'] = '2'
    data = {'query': query, 'payload': {'customerReviews': [{'reviewText': f'page {page}'}]}}
    return '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'

class RoutedPage(Page):
    def __init__(self):
        super().__init__([html(zip_code='95829')])
    def get(self, url, **kwargs):
        self.urls.append(url)
        if '/reviews/' in url:
            self.contents = [review_body(2 if 'page=2' in url else 1)]
        else:
            self.contents = [html(zip_code='95829')]

class ReviewNoBootstrapTests(unittest.TestCase):
    def make_renderer(self):
        clock = Clock(); page = RoutedPage(); renderer = WalmartTVBrowserRenderer()
        renderer._open_browser = lambda: Browser(page)
        renderer._set_zip = Mock(return_value=False)
        return clock, page, renderer

    def test_failed_zip_does_not_trigger_detail_or_zip_again_for_reviews(self):
        clock, page, renderer = self.make_renderer()
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
            detail, _ = renderer.fetch_html(URL)
            first, body1 = renderer.fetch_html(REVIEW)
            second, body2 = renderer.fetch_html(REVIEW2)
        self.assertEqual(page.urls, [URL, REVIEW, REVIEW2])
        renderer._set_zip.assert_called_once_with()
        self.assertTrue(detail['content_valid'])
        for meta in (first, second):
            self.assertTrue(meta['content_valid'])
            self.assertFalse(meta['location_verified'])
            self.assertTrue(meta.get('location_warning'))
        self.assertIn('page 1', body1)
        self.assertIn('page 2', body2)

    def test_review_first_never_opens_detail_or_sets_zip(self):
        clock, page, renderer = self.make_renderer()
        with patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep), patch('walmart.tv.wmart_tv_render.random.uniform', return_value=10):
            meta, body = renderer.fetch_html(REVIEW)
        self.assertEqual(page.urls, [REVIEW])
        renderer._set_zip.assert_not_called()
        self.assertTrue(meta['content_valid'])
        self.assertFalse(meta['location_verified'])
        self.assertTrue(meta.get('location_warning'))
        self.assertIn('page 1', body)

if __name__ == '__main__': unittest.main()
