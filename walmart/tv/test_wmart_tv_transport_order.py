import json
import unittest
from unittest.mock import Mock

from walmart.tv.wmart_tv_next_data import WalmartNextDataClient


URL = 'https://www.walmart.com/ip/example/100'


def page(item='100', similar=False, price=199):
    data = {'props': {'pageProps': {'initialData': {
        'product': {'usItemId': item, 'priceInfo': {'currentPrice': {'price': price}}},
    }}}}
    html = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'
    if similar:
        html += '<div id="ip-carousel-Similar items you might like"><h3 data-automation-id="product-title">Related TV</h3></div>'
    return html


def direct(body='', status=403):
    return {'source': 'direct', 'status': status}, body


def rendered(body=''):
    return {'source': 'browser_render', 'status': None, 'content_valid': bool(body),
            'location_verified': True, 'location_zip': '11581'}, body


class TransportOrderTests(unittest.TestCase):
    def make_client(self, direct_result, render_result, provider_result=None):
        order = []
        renderer = Mock()
        client = WalmartNextDataClient(renderer=renderer)
        def http(*args, **kwargs):
            order.append('html')
            return direct_result
        def render(*args, **kwargs):
            order.append('render')
            if isinstance(render_result, Exception):
                raise render_result
            return render_result
        def provider(*args, **kwargs):
            order.append('zenrows_js' if kwargs.get('js_render') else 'zenrows_static')
            return provider_result or ({'source': 'zenrows_static', 'status': 403}, '')
        client.fetch_direct_html = Mock(side_effect=http)
        renderer.fetch_html.side_effect = render
        client.fetch_zenrows_html = Mock(side_effect=provider)
        return client, renderer, order

    def test_valid_html_uses_neither_renderer_nor_provider(self):
        client, renderer, order = self.make_client(direct(page(), 200), rendered())
        self.assertEqual(client.fetch_next_data(URL)['source'], 'direct')
        self.assertEqual(order, ['html'])
        renderer.fetch_html.assert_not_called()
        client.fetch_zenrows_html.assert_not_called()

    def test_html_failure_then_render_success_avoids_provider(self):
        client, renderer, order = self.make_client(direct(), rendered(page()))
        result = client.fetch_next_data(URL)
        self.assertEqual(result['source'], 'browser_render')
        self.assertIsNone(result['meta']['status'])
        self.assertEqual(order, ['html', 'render'])
        client.fetch_zenrows_html.assert_not_called()

    def test_invalid_product_html_advances_to_render(self):
        for body in ('<script id="__NEXT_DATA__">{"props":{}}</script>', page(price=None)):
            with self.subTest(body=body):
                client, _, order = self.make_client(direct(body, 200), rendered(page()))
                self.assertEqual(client.fetch_next_data(URL)['source'], 'browser_render')
                self.assertEqual(order, ['html', 'render'])

    def test_renderer_failure_or_exception_advances_to_provider(self):
        for render_result in (rendered(), RuntimeError('browser closed')):
            with self.subTest(render_result=render_result):
                client, _, order = self.make_client(direct(), render_result,
                    ({'source': 'zenrows_static', 'status': 200}, page()))
                self.assertEqual(client.fetch_next_data(URL)['source'], 'zenrows_static')
                self.assertEqual(order, ['html', 'render', 'zenrows_static'])

    def test_unverified_browser_zip_is_kept_without_provider(self):
        for location in ({'location_verified': True, 'location_zip': '95829'},
                         {'location_verified': False, 'location_zip': '11581'},
                         {'location_verified': True, 'location_zip': None}, {}):
            with self.subTest(location=location):
                meta = {'source': 'browser_render', 'content_valid': True, **location}
                client, _, order = self.make_client(direct(), (meta, page()),
                    ({'source': 'zenrows_static', 'status': 200}, page()))
                result = client.fetch_next_data(URL)
                self.assertEqual(result['source'], 'browser_render')
                self.assertIs(result['meta']['location_verified'], False)
                self.assertEqual(order, ['html', 'render'])
                self.assertIn(URL, client._rendered_pages)

    def test_malformed_browser_metadata_advances_to_provider(self):
        client, _, order = self.make_client(direct(), ('unexpected', page()),
            ({'source': 'zenrows_static', 'status': 200}, page()))
        self.assertEqual(client.fetch_next_data(URL)['source'], 'zenrows_static')
        self.assertEqual(order, ['html', 'render', 'zenrows_static'])

    def test_unverified_browser_data_does_not_request_provider(self):
        for use_zenrows in (True, False):
            with self.subTest(use_zenrows=use_zenrows):
                meta = {'source': 'browser_render', 'content_valid': True,
                        'location_verified': False, 'location_zip': '95829'}
                client, _, order = self.make_client(direct(), (meta, page()))
                result = client.fetch_next_data(URL, use_zenrows=use_zenrows)
                self.assertEqual(result['source'], 'browser_render')
                self.assertEqual(result['meta']['location_zip'], '95829')
                self.assertIs(result['meta']['location_verified'], False)
                self.assertEqual(order, ['html', 'render'])

    def test_unverified_invalid_product_or_rejected_identity_is_not_kept(self):
        meta = {'source': 'browser_render', 'content_valid': True,
                'location_verified': False, 'location_zip': '95829'}
        for body, accept in ((page(price=None), None), (page(), lambda _: False)):
            with self.subTest(body=body, accept=accept):
                client, _, _ = self.make_client(direct(), (meta, body))
                self.assertIsNone(client.fetch_next_data(URL, accept_response=accept)['next_data'])

    def test_unverified_browser_review_is_kept_without_provider(self):
        url = 'https://www.walmart.com/reviews/product/100'
        body = '<script id="__NEXT_DATA__">' + json.dumps({
            'query': {'id': '100'}, 'payload': {'customerReviews': []},
        }) + '</script>'
        client, _, order = self.make_client(direct(),
            ({'source': 'browser_render', 'content_valid': True}, body),
            ({'source': 'zenrows_static', 'status': 200}, body))
        result = client.fetch_next_data(url)
        self.assertEqual(result['source'], 'browser_render')
        self.assertIs(result['meta']['location_verified'], False)
        self.assertEqual(order, ['html', 'render'])

    def test_all_sources_fail_and_next_product_still_runs(self):
        client, _, order = self.make_client(direct(), rendered())
        self.assertIsNone(client.fetch_next_data(URL)['next_data'])
        self.assertEqual(order, ['html', 'render', 'zenrows_static', 'zenrows_js'])
        client.fetch_direct_html.return_value = direct(page(item='200'), 200)
        client.fetch_direct_html.side_effect = None
        self.assertEqual(client.fetch_next_data('https://www.walmart.com/ip/200')['source'], 'direct')

    def test_mismatched_review_response_advances_to_render(self):
        url = 'https://www.walmart.com/reviews/product/100?classType=REGULAR'
        def reviews(item):
            return '<script id="__NEXT_DATA__">' + json.dumps({
                'query': {'id': item, 'classType': 'REGULAR'},
                'payload': {'customerReviews': []},
            }) + '</script>'
        client, _, order = self.make_client(direct(reviews('999'), 200), rendered(reviews('100')))
        self.assertEqual(client.fetch_next_data(url)['source'], 'browser_render')
        self.assertEqual(order, ['html', 'render'])

    def test_review_page_two_does_not_reuse_page_one(self):
        url = 'https://www.walmart.com/reviews/product/100?page=2'
        def reviews(page_number):
            return '<script id="__NEXT_DATA__">' + json.dumps({
                'query': {'id': '100', 'page': str(page_number)},
                'payload': {'customerReviews': []},
            }) + '</script>'
        client, _, order = self.make_client(direct(reviews(1), 200), rendered(reviews(2)))
        self.assertEqual(client.fetch_next_data(url)['source'], 'browser_render')
        self.assertEqual(order, ['html', 'render'])
        client, _, order = self.make_client(direct(), rendered(),
            ({'source': 'zenrows_static', 'status': 200}, reviews(1)))
        self.assertIsNone(client.fetch_next_data(url)['next_data'])
        self.assertEqual(order, ['html', 'render', 'zenrows_static', 'zenrows_js'])

    def test_unexpected_price_structure_advances_to_render(self):
        malformed = '<script id="__NEXT_DATA__">' + json.dumps({
            'props': {'pageProps': {'initialData': {'product': {
                'usItemId': '100', 'priceInfo': {'additionalFees': []},
            }}}},
        }) + '</script>'
        client, _, order = self.make_client(direct(malformed, 200), rendered(page()))
        self.assertEqual(client.fetch_next_data(URL)['source'], 'browser_render')
        self.assertEqual(order, ['html', 'render'])

    def test_cached_rendered_similar_items_need_no_new_request(self):
        client, renderer, _ = self.make_client(direct(), rendered(page(similar=True)))
        client.fetch_next_data(URL)
        renderer.fetch_html.reset_mock()
        client.fetch_zenrows_json_response = Mock()
        result = client.fetch_similar_product_names(URL, current_item='100')
        self.assertEqual(result['names'], 'Related TV')
        renderer.fetch_html.assert_not_called()
        client.fetch_zenrows_json_response.assert_not_called()

    def test_similar_items_wait_for_render_before_using_provider(self):
        client, renderer, _ = self.make_client(direct(), rendered())
        client.fetch_zenrows_json_response = Mock(return_value=(
            {'source': 'zenrows_json', 'status': 200}, {'html': page(similar=True)},
        ))
        result = client.fetch_similar_product_names(URL, current_item='100')
        self.assertEqual(result['names'], 'Related TV')
        renderer.fetch_html.assert_called_once_with(URL, wait_for_similar=True)
        client.fetch_zenrows_json_response.assert_called_once()

    def test_unverified_similar_cache_and_render_are_kept(self):
        for invalid_meta in ({'content_valid': True, 'location_verified': True, 'location_zip': '95829'},
                             {'content_valid': True}, 'bad metadata'):
            with self.subTest(invalid_meta=invalid_meta):
                client, _, _ = self.make_client(direct(), (invalid_meta, page(similar=True)))
                client._rendered_pages[URL] = (invalid_meta, page(similar=True))
                client.fetch_zenrows_json_response = Mock(return_value=(
                    {'source': 'zenrows_json', 'status': 200}, {'html': page(similar=True)},
                ))
                result = client.fetch_similar_product_names(URL, current_item='100')
                self.assertEqual(result['source'], 'zenrows_json' if isinstance(invalid_meta, str) else 'browser_render')
                if isinstance(invalid_meta, str):
                    client.fetch_zenrows_json_response.assert_called_once()
                else:
                    self.assertIs(result['meta']['location_verified'], False)
                    client.fetch_zenrows_json_response.assert_not_called()

    def test_unverified_similar_results_do_not_request_provider(self):
        meta = {'source': 'browser_render', 'content_valid': True,
                'location_verified': False, 'location_zip': '95829'}
        for cached in (False, True):
            with self.subTest(cached=cached):
                client, _, _ = self.make_client(direct(), (meta, page(similar=True)))
                if cached:
                    client._rendered_pages[URL] = (meta, page(similar=True))
                client.fetch_zenrows_json_response = Mock(side_effect=RuntimeError('unavailable'))
                result = client.fetch_similar_product_names(URL, current_item='100')
                self.assertEqual(result['names'], 'Related TV')
                self.assertEqual(result['source'], 'browser_render')
                self.assertIs(result['meta']['location_verified'], False)
                client.fetch_zenrows_json_response.assert_not_called()

    def test_similar_failure_does_not_raise_or_stop_collection(self):
        client, _, _ = self.make_client(direct(), RuntimeError('browser closed'))
        client.fetch_zenrows_json_response = Mock(side_effect=RuntimeError('provider unavailable'))
        self.assertIsNone(client.fetch_similar_product_names(URL, current_item='100')['names'])


if __name__ == '__main__':
    unittest.main()
