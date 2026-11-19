"""Offline behavioral tests for recommendation request reuse and fallback."""
import json
import unittest
from unittest.mock import Mock

from walmart.tv.wmart_tv_next_data import WalmartNextDataClient

URL = 'https://www.walmart.com/ip/100'
META = {'source': 'browser_render', 'content_valid': True, 'location_verified': False, 'location_zip': '95829'}

def page(item='100', name=None):
    data = {'props': {'pageProps': {'initialData': {'product': {'usItemId': item, 'name': 'Current TV',
        'priceInfo': {'currentPrice': {'price': 200}}}}}}}
    carousel = (f'<div id="ip-carousel-Similar items you might like"><span data-automation-id="product-title">{name}</span></div>' if name else '')
    return '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>' + carousel

def recommendation(name):
    data = {'data': {'contentLayout': {'modules': [{'type': 'ItemCarousel',
        'configs': {'title': 'Similar items you might like', 'products': [{'usItemId': '201', 'name': name}]}}]}}}
    return {'html': '', 'xhr': [{'url': 'https://www.walmart.com/orchestra/graphql/Search', 'body': json.dumps(data)}]}

class RecommendationRequestTests(unittest.TestCase):
    def client(self):
        renderer = Mock()
        client = WalmartNextDataClient(renderer=renderer)
        client.fetch_direct_html = Mock(return_value=({'source': 'direct', 'status': 403}, ''))
        client.fetch_zenrows_html = Mock(side_effect=AssertionError('unexpected ZenRows HTML'))
        client.fetch_zenrows_json_response = Mock(side_effect=AssertionError('unexpected ZenRows JSON'))
        return client, renderer

    def test_browser_detail_with_names_reuses_capture(self):
        client, renderer = self.client()
        renderer.fetch_html.return_value = (META, page(name='Relevant TV'))
        fetched = client.fetch_next_data(URL)
        self.assertEqual(fetched['source'], 'browser_render')
        result = client.fetch_similar_product_names(URL, current_item='100')
        self.assertEqual(result['names'], 'Relevant TV')
        renderer.fetch_html.assert_called_once_with(URL)
        client.fetch_zenrows_json_response.assert_not_called()

    def test_missing_names_in_cached_page_allows_render_to_complete(self):
        client, renderer = self.client()
        renderer.fetch_html.side_effect = [(META, page()), (META, page(name='Late TV'))]
        client.fetch_next_data(URL)
        result = client.fetch_similar_product_names(URL, current_item='100')
        self.assertEqual(result['names'], 'Late TV')
        self.assertEqual(renderer.fetch_html.call_count, 2)
        renderer.fetch_html.assert_called_with(URL, wait_for_similar=True)
        client.fetch_zenrows_json_response.assert_not_called()

    def test_no_cache_and_failed_render_uses_existing_zenrows(self):
        client, renderer = self.client()
        renderer.fetch_html.return_value = ({'content_valid': False}, '')
        client.fetch_zenrows_json_response = Mock(return_value=({'source': 'zenrows_json', 'status': 200}, recommendation('Fallback TV')))
        result = client.fetch_similar_product_names(URL, current_item='100')
        self.assertEqual(result['names'], 'Fallback TV')
        renderer.fetch_html.assert_called_once_with(URL, wait_for_similar=True)
        client.fetch_zenrows_json_response.assert_called_once()

    def test_empty_cache_and_empty_render_keep_existing_zenrows_fallback(self):
        client, renderer = self.client()
        renderer.fetch_html.side_effect = [(META, page()), (META, page())]
        client.fetch_zenrows_json_response = Mock(return_value=({'source': 'zenrows_json', 'status': 200}, recommendation('Fallback TV')))
        client.fetch_next_data(URL)
        result = client.fetch_similar_product_names(URL, current_item='100')
        self.assertEqual(result['names'], 'Fallback TV')
        self.assertEqual(renderer.fetch_html.call_count, 2)
        client.fetch_zenrows_json_response.assert_called_once()

    def test_unrelated_cached_and_rendered_products_are_not_used(self):
        client, renderer = self.client()
        client._rendered_pages[URL] = (META, page(item='999', name='Wrong TV'))
        renderer.fetch_html.return_value = (META, page(item='888', name='Wrong TV 2'))
        client.fetch_zenrows_json_response = Mock(return_value=({'source': 'zenrows_json', 'status': 200}, recommendation('Fallback TV')))
        result = client.fetch_similar_product_names(URL, current_item='100')
        self.assertEqual(result['names'], 'Fallback TV')
        renderer.fetch_html.assert_called_once_with(URL, wait_for_similar=True)
        client.fetch_zenrows_json_response.assert_called_once()

if __name__ == '__main__': unittest.main()
