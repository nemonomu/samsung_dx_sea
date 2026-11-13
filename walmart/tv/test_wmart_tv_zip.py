"""Offline checks for Walmart TV location enforcement."""

import json
import io
import threading
import unittest
from contextlib import redirect_stdout
from html import escape
from unittest.mock import Mock, patch

from walmart.tv import wmart_tv_next_data as client_module


def next_data(initial):
    return {'props': {'pageProps': {'initialData': initial}}}


def search_data(zip_code):
    location = {} if zip_code is None else {'postalCode': zip_code}
    return next_data({
        'pageMetadata': {'location': location},
        'searchResult': {'itemStacks': [{'items': [{'usItemId': '100'}]}]},
    })


def detail_data(zip_code, item='100'):
    return next_data({'data': {'product': {'usItemId': item, 'location': {'postalCode': zip_code}}}})


def page_html(payload):
    return '<script id="__NEXT_DATA__">' + json.dumps(payload) + '</script>'


def response(source, payload, *, blocked=False, status=200):
    return ({'source': source, 'status': status, 'blocked': blocked}, page_html(payload))


def pickup_body(store='5293', postal='11581'):
    return {'data': {'fulfillmentMutations': {'setPickup': {'fulfillment': {
        'pickupStore': {'storeId': store, 'postalCode': postal},
        'deliveryAddress': {'postalCode': '11581'},
    }}}}}


class ResponseZipTests(unittest.TestCase):
    def test_reads_listing_and_detail_location_shapes(self):
        examples = [
            search_data('11581'),
            next_data({'data': {'product': {'location': {'postalCode': '11581'}}}}),
            next_data({'contentLayout': {'pageMetadata': {'location': {'postalCode': '11581'}}}}),
        ]
        for payload in examples:
            with self.subTest(payload=payload):
                self.assertEqual(client_module.response_zip(payload), '11581')

    def test_missing_and_conflicting_locations_are_unknown(self):
        self.assertIsNone(client_module.response_zip(next_data({})))
        self.assertIsNone(client_module.response_zip(next_data({
            'pageMetadata': {'location': {'postalCode': '11581'}},
            'data': {'product': {'location': {'postalCode': '95829'}}},
        })))
        for payload in ({'props': []}, {'props': {'pageProps': []}}, next_data({'pageMetadata': 'unexpected'})):
            self.assertIsNone(client_module.response_zip(payload))
        self.assertIsNone(client_module.response_zip(next_data({
            'contentLayout': {'pageMetadata': {'location': {'postalCode': '11581'}}},
            'data': {'product': {'location': {'postalCode': '95829'}}},
        })))


class FetchLocationTests(unittest.TestCase):
    url = 'https://www.walmart.com/search?q=TV&page=2'

    def client(self, direct, zenrows):
        client = client_module.WalmartNextDataClient()
        client.fetch_direct_html = Mock(return_value=direct)
        client.fetch_zenrows_html = Mock(side_effect=zenrows)
        return client

    def test_correct_direct_location_is_accepted_without_zenrows(self):
        good = search_data('11581')
        client = self.client(response('direct', good), [])
        result = client.fetch_next_data(self.url)
        self.assertEqual(result['next_data'], good)
        self.assertEqual(result['source'], 'direct')
        client.fetch_zenrows_html.assert_not_called()

    def test_disabled_location_flow_returns_first_valid_direct_without_zenrows(self):
        for zipcode in ('11581', '95829', None):
            with self.subTest(zipcode=zipcode):
                payload = search_data(zipcode)
                client = self.client(response('direct', payload), [])
                output = io.StringIO()
                with redirect_stdout(output):
                    result = client.fetch_next_data(self.url)
                self.assertEqual(result['next_data'], payload)
                client.fetch_zenrows_html.assert_not_called()
                self.assertIn('automatic 11581 setup disabled', output.getvalue())
                if zipcode != '11581':
                    self.assertFalse(result['meta']['location_verified'])
                    self.assertIn('[LOCATION WARNING]', output.getvalue())

    def test_disabled_location_flow_returns_first_valid_static_without_js(self):
        for zipcode in ('11581', '95829', None):
            with self.subTest(zipcode=zipcode):
                payload = search_data(zipcode)
                client = self.client(response('direct', payload, blocked=True), [
                    response('zenrows_static', payload),
                ])
                output = io.StringIO()
                with redirect_stdout(output):
                    result = client.fetch_next_data(self.url)
                self.assertEqual(result['next_data'], payload)
                self.assertEqual(client.fetch_zenrows_html.call_count, 1)
                self.assertFalse(client.fetch_zenrows_html.call_args.kwargs['js_render'])
                self.assertIn('automatic 11581 setup disabled', output.getvalue())
                if zipcode != '11581':
                    self.assertFalse(result['meta']['location_verified'])
                    self.assertIn('[LOCATION WARNING]', output.getvalue())

    def test_disabled_location_flow_still_tries_js_after_blocked_or_failed_routes(self):
        payload = search_data('95829')
        client = self.client(response('direct', payload, blocked=True), [
            response('zenrows_static', payload, status=503),
            response('zenrows_js', payload),
        ])
        with redirect_stdout(io.StringIO()):
            result = client.fetch_next_data(self.url)
        self.assertEqual(result['next_data'], payload)
        self.assertEqual(result['source'], 'zenrows_js')
        self.assertFalse(result['meta']['location_verified'])
        self.assertEqual([call.kwargs.get('js_render') for call in client.fetch_zenrows_html.call_args_list], [False, True])

    @patch.object(client_module, '_WALMART_LOCATION_FLOW_VERIFIED', True)
    def test_wrong_then_missing_location_advance_to_matching_response(self):
        good = search_data('11581')
        client = self.client(response('direct', search_data('95829')), [
            response('zenrows_static', search_data(None)),
            response('zenrows_js', good),
        ])
        result = client.fetch_next_data(self.url)
        self.assertEqual(result['next_data'], good)
        self.assertEqual(result['source'], 'zenrows_js')
        self.assertEqual([call.kwargs.get('js_render') for call in client.fetch_zenrows_html.call_args_list], [False, True])

    def test_no_matching_response_falls_back_and_records_location(self):
        client = self.client(response('direct', search_data('95829')), [
            response('zenrows_static', search_data(None)),
            response('zenrows_js', search_data('33178')),
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            result = client.fetch_next_data(self.url)
        self.assertEqual(result['source'], 'direct')
        self.assertEqual(result['meta']['location_zip'], '95829')
        self.assertFalse(result['meta']['location_verified'])
        self.assertIn('[LOCATION WARNING]', output.getvalue())
        self.assertIn('95829', output.getvalue())

    def test_blocked_matching_response_is_not_accepted(self):
        client = self.client(response('direct', search_data('11581'), blocked=True), [
            response('zenrows_static', search_data('95829')),
            response('zenrows_js', search_data('11581'), blocked=True),
        ])
        result = client.fetch_next_data(self.url)
        self.assertEqual(result['source'], 'zenrows_static')
        self.assertFalse(result['meta']['location_verified'])

    def test_http_error_with_matching_data_is_not_accepted(self):
        client = self.client(response('direct', search_data('11581'), status=403), [
            response('zenrows_static', search_data('95829')),
            response('zenrows_js', search_data('11581'), status=503),
        ])
        result = client.fetch_next_data(self.url)
        self.assertEqual(result['source'], 'zenrows_static')
        self.assertFalse(result['meta']['location_verified'])

    def test_review_without_location_preserves_existing_behavior(self):
        review = {'query': {'id': '100'}, 'payload': {'customerReviews': []}}
        client = self.client(response('direct', review), [])
        result = client.fetch_next_data('https://www.walmart.com/reviews/product/100')
        self.assertEqual(result['next_data'], review)
        client.fetch_zenrows_html.assert_not_called()


class ZenRowsLocationTests(unittest.TestCase):
    url = 'https://www.walmart.com/ip/100'

    def test_unverified_location_setup_does_not_make_automatic_request(self):
        client = client_module.WalmartNextDataClient()
        client.fetch_zenrows_json_response = Mock(return_value=({'error': 'failed'}, None))
        meta, payload = client.fetch_with_location(self.url)
        self.assertIsNone(payload)
        self.assertEqual(meta['error'], 'Walmart 11581 location flow unverified')
        client.fetch_zenrows_json_response.assert_not_called()

    def test_disabled_location_attempt_runs_only_existing_js_route(self):
        client = client_module.WalmartNextDataClient()
        client.zenrows_api_key = 'fake-test-key'
        fallback = Mock(status_code=200, text=page_html(detail_data('95829')),
                        url='https://api.zenrows.com/v1/')
        output = io.StringIO()
        with patch.object(client_module.requests, 'get', return_value=fallback) as get:
            with redirect_stdout(output):
                meta, html = client.fetch_zenrows_html(self.url, js_render=True)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.kwargs['params']['url'], self.url)
        self.assertEqual(get.call_args.kwargs['params']['js_render'], 'true')
        self.assertEqual(html, fallback.text)
        self.assertEqual(meta['error'], 'Walmart 11581 location flow unverified')
        self.assertIn('Walmart 11581 location flow unverified', output.getvalue())

    def test_initial_search_is_reloaded_after_store_selection(self):
        instructions = client_module.walmart_location_instructions(client_module.WALMART_LOCATION_START_URL)
        self.assertIn({'evaluate': 'location.reload()'}, instructions)

    def test_unrelated_target_store_in_response_does_not_confirm_pickup(self):
        payload = self.valid_payload()
        body = pickup_body(3081, '95829')
        body['data']['otherAvailableStores'] = [{'storeId': 5293, 'postalCode': '11581'}]
        payload['xhr'][0]['body'] = json.dumps(body)
        fake_response = Mock(status_code=200, text='{}', url='https://api.zenrows.com/v1/')
        fake_response.json.return_value = payload
        client = client_module.WalmartNextDataClient()
        client.zenrows_api_key = 'fake-test-key'
        with patch.object(client_module.requests, 'get', return_value=fake_response):
            meta, actual = client.fetch_zenrows_json_response(self.url, set_location=True)
        self.assertIsNone(actual)
        self.assertEqual(meta['error'], 'Walmart location not confirmed')

    def test_pickup_requires_both_expected_values_at_observed_path(self):
        bodies = [pickup_body('5293', '95829'), pickup_body('3081', '11581'),
                  {'data': {'fulfillmentMutations': {'setPickup': {'fulfillment': {'pickupStore': None}}}}}]
        for body in bodies:
            with self.subTest(body=body):
                payload = self.valid_payload()
                payload['xhr'][0]['body'] = json.dumps(body)
                fake_response = Mock(status_code=200, text='{}', url='https://api.zenrows.com/v1/')
                fake_response.json.return_value = payload
                client = client_module.WalmartNextDataClient()
                client.zenrows_api_key = 'fake-test-key'
                with patch.object(client_module.requests, 'get', return_value=fake_response):
                    meta, actual = client.fetch_zenrows_json_response(self.url, set_location=True)
                self.assertIsNone(actual)
                self.assertEqual(meta['error'], 'Walmart location not confirmed')

    def test_later_failed_pickup_does_not_reuse_prior_success(self):
        payload = self.valid_payload()
        payload['xhr'].append({
            'url': 'https://www.walmart.com/orchestra/cartxo/graphql/setPickup/hash',
            'status_code': 200,
            'body': json.dumps(pickup_body('3081', '95829')),
        })
        fake_response = Mock(status_code=200, text='{}', url='https://api.zenrows.com/v1/')
        fake_response.json.return_value = payload
        client = client_module.WalmartNextDataClient()
        client.zenrows_api_key = 'fake-test-key'
        with patch.object(client_module.requests, 'get', return_value=fake_response):
            meta, actual = client.fetch_zenrows_json_response(self.url, set_location=True)
        self.assertIsNone(actual)
        self.assertEqual(meta['error'], 'Walmart location not confirmed')

    @patch.object(client_module, '_WALMART_LOCATION_FLOW_VERIFIED', True)
    def test_unverified_pickup_uses_legacy_js_and_logs_reason(self):
        payload = self.valid_payload()
        body = pickup_body(3081, '95829')
        body['data']['otherAvailableStores'] = [{'storeId': 5293, 'postalCode': '11581'}]
        payload['xhr'][0]['body'] = json.dumps(body)
        location_response = Mock(status_code=200, text='{}', url='https://api.zenrows.com/v1/')
        location_response.json.return_value = payload
        fallback_response = Mock(status_code=200, text=page_html(detail_data('95829')),
                                 url='https://api.zenrows.com/v1/')
        client = client_module.WalmartNextDataClient()
        client.zenrows_api_key = 'fake-test-key'
        output = io.StringIO()
        with patch.object(client_module.requests, 'get', side_effect=[location_response, fallback_response]) as get:
            with redirect_stdout(output):
                meta, html = client.fetch_zenrows_html(self.url, js_render=True)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(html, fallback_response.text)
        self.assertEqual(meta['error'], 'Walmart location not confirmed')
        self.assertIn('Walmart location not confirmed', output.getvalue())

    def setUp(self):
        client_module.WalmartNextDataClient._location_failures = 0
        client_module.WalmartNextDataClient._location_retry_after = 0

    def tearDown(self):
        client_module.WalmartNextDataClient._location_failures = 0
        client_module.WalmartNextDataClient._location_retry_after = 0

    def target_html(self, data, url=None):
        return '<html data-walmart-final-url="' + escape(url or self.url, quote=True) + '"><body>' + page_html(data) + '</body></html>'

    def valid_payload(self):
        return {
            'html': self.target_html(detail_data('11581')),
            'xhr': [{
                'url': 'https://www.walmart.com/orchestra/cartxo/graphql/setPickup/hash',
                'status_code': 200,
                'body': json.dumps(pickup_body()),
            }],
            'js_instructions_report': {
                'instructions_failed': 0,
                'instructions_executed': len(client_module.walmart_location_instructions(self.url)),
            },
        }

    def test_verified_location_setup_reloads_url_and_keeps_only_new_xhrs(self):
        old = {'url': 'https://www.walmart.com/orchestra/pdp/graphql/ItemByIdBtf', 'body': '{}'}
        pickup = {
            'url': 'https://www.walmart.com/orchestra/cartxo/graphql/setPickup/hash',
            'status_code': 200,
            'body': json.dumps(pickup_body()),
        }
        fresh = {'url': 'https://www.walmart.com/orchestra/pdp/graphql/ItemByIdBtf?fresh=1', 'body': '{}'}
        count = len(client_module.walmart_location_instructions(self.url))
        payload = {
            'html': self.target_html(detail_data('11581')),
            'xhr': [old, pickup, fresh],
            'js_instructions_report': {'instructions_failed': 0, 'instructions_executed': count},
        }
        fake_response = Mock(status_code=200, text='{}', url='https://api.zenrows.com/v1/')
        fake_response.json.return_value = payload
        client = client_module.WalmartNextDataClient()
        client.zenrows_api_key = 'fake-test-key'
        with patch.object(client_module.requests, 'get', return_value=fake_response) as get:
            meta, actual = client.fetch_zenrows_json_response(self.url, set_location=True)
        self.assertIsNone(meta['error'])
        self.assertEqual(actual['xhr'], [fresh])
        params = get.call_args.kwargs['params']
        self.assertEqual(params['url'], client_module.WALMART_LOCATION_START_URL)
        self.assertIn(self.url, params['js_instructions'])

    def test_location_setup_rejects_failed_steps_missing_pickup_and_wrong_reload(self):
        variants = []
        failed_steps = self.valid_payload()
        failed_steps['js_instructions_report']['instructions_failed'] = 1
        variants.append(failed_steps)
        missing_pickup = self.valid_payload()
        missing_pickup['xhr'] = []
        variants.append(missing_pickup)
        wrong_location = self.valid_payload()
        wrong_location['html'] = self.target_html(detail_data('95829'))
        variants.append(wrong_location)
        wrong_page = self.valid_payload()
        wrong_page['html'] = self.target_html(detail_data('11581'), 'https://www.walmart.com/ip/999')
        variants.append(wrong_page)
        wrong_item = self.valid_payload()
        wrong_item['html'] = self.target_html(detail_data('11581', '101'))
        variants.append(wrong_item)
        for payload in variants:
            with self.subTest(payload=payload):
                fake_response = Mock(status_code=200, text='{}', url='https://api.zenrows.com/v1/')
                fake_response.json.return_value = payload
                client = client_module.WalmartNextDataClient()
                client.zenrows_api_key = 'fake-test-key'
                with patch.object(client_module.requests, 'get', return_value=fake_response):
                    meta, actual = client.fetch_zenrows_json_response(self.url, set_location=True)
                self.assertIsNone(actual)
                self.assertIsNotNone(meta['error'])

    def test_location_setup_accepts_same_item_with_canonical_slug(self):
        payload = self.valid_payload()
        payload['html'] = self.target_html(detail_data('11581'), 'https://www.walmart.com/ip/example-tv/100')
        fake_response = Mock(status_code=200, text='{}', url='https://api.zenrows.com/v1/')
        fake_response.json.return_value = payload
        client = client_module.WalmartNextDataClient()
        client.zenrows_api_key = 'fake-test-key'
        with patch.object(client_module.requests, 'get', return_value=fake_response):
            meta, actual = client.fetch_zenrows_json_response(self.url, set_location=True)
        self.assertIsNotNone(actual)
        self.assertIsNone(meta['error'])

    def test_guest_session_error_is_recorded_without_logging_response(self):
        payload = self.valid_payload()
        payload['xhr'][0]['body'] = json.dumps({'errors': [{'message': 'Missing Data: guestAccount'}]})
        fake_response = Mock(status_code=200, text='{}', url='https://api.zenrows.com/v1/')
        fake_response.json.return_value = payload
        client = client_module.WalmartNextDataClient()
        client.zenrows_api_key = 'fake-test-key'
        with patch.object(client_module.requests, 'get', return_value=fake_response):
            meta, actual = client.fetch_zenrows_json_response(self.url, set_location=True)
        self.assertIsNone(actual)
        self.assertEqual(meta['error'], 'Walmart guest session unavailable')

    @patch.object(client_module, '_WALMART_LOCATION_FLOW_VERIFIED', True)
    def test_repeated_location_failures_pause_requests_across_clients(self):
        first = client_module.WalmartNextDataClient()
        second = client_module.WalmartNextDataClient()
        first.fetch_zenrows_json_response = Mock(return_value=({'error': 'failed'}, None))
        second.fetch_zenrows_json_response = Mock(return_value=({'error': 'failed'}, None))
        with patch.object(client_module.time, 'monotonic', return_value=100):
            for _ in range(3):
                first.fetch_with_location(self.url)
            meta, payload = second.fetch_with_location(self.url)
        self.assertIsNone(payload)
        self.assertIn('temporarily paused', meta['error'])
        second.fetch_zenrows_json_response.assert_not_called()

    def test_location_failure_preserves_original_js_route(self):
        client = client_module.WalmartNextDataClient()
        client.zenrows_api_key = 'fake-test-key'
        client.fetch_with_location = Mock(return_value=({'error': 'failed'}, None))
        fake_response = Mock(status_code=200, text=page_html(detail_data('95829')),
                             url='https://api.zenrows.com/v1/')
        with patch.object(client_module.requests, 'get', return_value=fake_response) as get:
            meta, html = client.fetch_zenrows_html(self.url, js_render=True)
        self.assertIn('__NEXT_DATA__', html)
        self.assertEqual(meta['error'], 'failed')
        self.assertEqual(get.call_args.kwargs['params']['js_render'], 'true')

    @patch.object(client_module, '_WALMART_LOCATION_FLOW_VERIFIED', True)
    def test_inflight_success_does_not_close_open_circuit(self):
        client = client_module.WalmartNextDataClient()
        started, release = threading.Event(), threading.Event()
        def slow_success(*args, **kwargs):
            started.set()
            release.wait(2)
            return {}, {'html': 'ok'}
        client.fetch_zenrows_json_response = Mock(side_effect=slow_success)
        with patch.object(client_module.time, 'monotonic', return_value=100):
            thread = threading.Thread(target=client.fetch_with_location, args=(self.url,))
            thread.start()
            self.assertTrue(started.wait(1))
            client_module.WalmartNextDataClient._location_retry_after = 700
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(client_module.WalmartNextDataClient._location_retry_after, 700)


if __name__ == '__main__':
    unittest.main()
