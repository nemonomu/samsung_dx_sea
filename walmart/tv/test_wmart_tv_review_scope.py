import unittest
from unittest.mock import patch

from walmart.tv.wmart_tv_dt import WalmartTVDetailCrawler
from walmart.tv.wmart_tv_dt_update import WalmartTVDetailUpdateCrawler
from walmart.tv.wmart_tv_crawl import build_walmart_tv_email_report
from walmart.tv.wmart_tv_next_data import (
    WalmartNextDataClient,
    build_item_url,
    build_review_url,
    parse_review_page,
    product_scope_query_params,
    review_response_scope_error,
)


ITEM = '17309421750'
SCOPED_PRODUCT_URL = (
    'https://www.walmart.com/ip/50-HISENSE-4K-GOOGLE-TV/'
    '17309421750?conditionGroupCode=2&classType=REGULAR&athbdg=L1100'
)


def review_next_data(
    item,
    reviews,
    text_count,
    rating_count,
    star_rating,
    **query_scope,
):
    return {
        'query': {'id': item, **query_scope},
        'payload': {
            'customerReviews': [
                {'reviewId': str(index), 'reviewText': text}
                for index, text in enumerate(reviews, 1)
            ],
            'reviewsWithTextCount': text_count,
            'totalReviewCount': rating_count,
            'averageOverallRating': star_rating,
            'roundedAverageOverallRating': star_rating,
        },
    }


class FakeNextDataClient:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def fetch_next_data(self, url, **kwargs):
        self.urls.append(url)
        return {
            'next_data': self.responses.get(url),
            'source': 'test',
            'attempts': [],
        }


def bare_crawler():
    crawler = WalmartTVDetailCrawler.__new__(WalmartTVDetailCrawler)
    crawler._env_int = lambda name, default, minimum=1: default
    crawler._record_run_error = lambda *args, **kwargs: None
    crawler.detail_next_data_workers = 1
    crawler.detail_next_data_chunk_size = 40
    crawler.zenrows_recovery_workers = 1
    crawler.zenrows_recovery_attempts = 1
    crawler.parallel_miss_reasons = {}
    return crawler


def valid_detail_row():
    return {
        'item': ITEM,
        'final_sku_price': '$225.00',
        'original_sku_price': None,
        'count_of_reviews': '1',
        'star_rating': '1.0',
        'count_of_star_ratings': '1',
        'detailed_review_content': 'review1 - scoped review',
    }


class ReviewScopeUrlTests(unittest.TestCase):
    def test_scope_params_keep_semantic_keys_only(self):
        self.assertEqual(
            product_scope_query_params(SCOPED_PRODUCT_URL),
            {'conditionGroupCode': '2', 'classType': 'REGULAR'},
        )

    def test_item_and_review_urls_preserve_scope(self):
        self.assertEqual(
            build_item_url(ITEM, SCOPED_PRODUCT_URL),
            'https://www.walmart.com/ip/17309421750'
            '?conditionGroupCode=2&classType=REGULAR',
        )
        self.assertEqual(
            build_review_url(ITEM, 1, SCOPED_PRODUCT_URL),
            'https://www.walmart.com/reviews/product/17309421750'
            '?conditionGroupCode=2&classType=REGULAR',
        )
        self.assertEqual(
            build_review_url(ITEM, 2, SCOPED_PRODUCT_URL),
            'https://www.walmart.com/reviews/product/17309421750'
            '?conditionGroupCode=2&classType=REGULAR&page=2',
        )

    def test_review_response_scope_validation(self):
        valid = review_next_data(
            ITEM,
            ['one'],
            1,
            1,
            1,
            conditionGroupCode='2',
            classType='REGULAR',
        )
        missing_scope = review_next_data(ITEM, ['one'], 1, 1, 1)
        self.assertIsNone(
            review_response_scope_error(valid, ITEM, SCOPED_PRODUCT_URL)
        )
        self.assertIn(
            'conditionGroupCode',
            review_response_scope_error(missing_scope, ITEM, SCOPED_PRODUCT_URL),
        )

    def test_review_parser_returns_one_coherent_summary(self):
        parsed = parse_review_page(
            review_next_data(
                ITEM,
                ['one'],
                1,
                1,
                1,
                conditionGroupCode='2',
                classType='REGULAR',
            )
        )
        self.assertEqual(parsed['total_review_count'], '1')
        self.assertEqual(parsed['star_rating'], '1.0')
        self.assertEqual(parsed['count_of_star_ratings'], '1')


class ZenRowsRecoveryTests(unittest.TestCase):
    def test_zenrows_only_client_never_calls_direct(self):
        client = WalmartNextDataClient(direct_enabled=False)
        zenrows_calls = []

        def fail_direct(*args, **kwargs):
            raise AssertionError('direct HTTP must not run in recovery mode')

        def fake_zenrows(url, js_render=False):
            zenrows_calls.append(js_render)
            return (
                {
                    'source': 'zenrows_static',
                    'status': 200,
                    'blocked': False,
                },
                '<script id="__NEXT_DATA__">{"query":{"id":"100"}}</script>',
            )

        client.fetch_direct_html = fail_direct
        client.fetch_zenrows_html = fake_zenrows
        result = client.fetch_next_data('https://www.walmart.com/ip/100')

        self.assertEqual(result['source'], 'zenrows_static')
        self.assertEqual(result['next_data']['query']['id'], '100')
        self.assertEqual(zenrows_calls, [False])
        self.assertEqual(
            [attempt['source'] for attempt in result['attempts']],
            ['zenrows_static'],
        )

    def test_zenrows_only_client_falls_back_from_static_to_js(self):
        client = WalmartNextDataClient(direct_enabled=False)
        zenrows_calls = []

        def fail_direct(*args, **kwargs):
            raise AssertionError('direct HTTP must not run in recovery mode')

        def fake_zenrows(url, js_render=False):
            zenrows_calls.append(js_render)
            source = 'zenrows_js' if js_render else 'zenrows_static'
            html_text = ''
            if js_render:
                html_text = (
                    '<script id="__NEXT_DATA__">'
                    '{"query":{"id":"100"}}'
                    '</script>'
                )
            return (
                {
                    'source': source,
                    'status': 200,
                    'blocked': False,
                },
                html_text,
            )

        client.fetch_direct_html = fail_direct
        client.fetch_zenrows_html = fake_zenrows
        result = client.fetch_next_data('https://www.walmart.com/ip/100')

        self.assertEqual(result['source'], 'zenrows_js')
        self.assertEqual(zenrows_calls, [False, True])
        self.assertEqual(
            [attempt['source'] for attempt in result['attempts']],
            ['zenrows_static', 'zenrows_js'],
        )

    @patch('walmart.tv.wmart_tv_dt.time.sleep', return_value=None)
    def test_recovery_rounds_use_zenrows_only_until_success(self, _sleep):
        crawler = bare_crawler()
        crawler.zenrows_recovery_attempts = 2
        calls = []

        def fake_worker(index, product, mst_specs, zenrows_only=False):
            calls.append(zenrows_only)
            if len(calls) == 1:
                return (
                    index,
                    None,
                    None,
                    'review_incomplete',
                    [{
                        'stage': 'review_next_data_incomplete',
                        'message': 'collected 19',
                    }],
                )
            return (
                index,
                {
                    'item': '100',
                    '_detail_source': 'zenrows_static',
                },
                None,
                None,
                [],
            )

        crawler._crawl_detail_next_data_worker = fake_worker
        recovered, unresolved = crawler.collect_detail_zenrows_recovery_parallel(
            {
                1: {
                    'product': {'product_url': 'https://www.walmart.com/ip/100'},
                    'reason': 'review_incomplete',
                    'diagnostics': [],
                    'error': None,
                },
            },
            {},
        )

        self.assertEqual(calls, [True, True])
        self.assertEqual(recovered[1]['item'], '100')
        self.assertEqual(unresolved, {})

    @patch('walmart.tv.wmart_tv_dt.time.sleep', return_value=None)
    def test_recovery_rounds_stop_after_ten_failed_attempts(self, _sleep):
        crawler = bare_crawler()
        crawler.zenrows_recovery_attempts = 10
        calls = []

        def fake_worker(index, product, mst_specs, zenrows_only=False):
            calls.append(zenrows_only)
            return (
                index,
                None,
                None,
                'review_incomplete',
                [{
                    'stage': 'review_next_data_incomplete',
                    'message': 'collected 19',
                }],
            )

        crawler._crawl_detail_next_data_worker = fake_worker
        recovered, unresolved = crawler.collect_detail_zenrows_recovery_parallel(
            {
                1: {
                    'product': {'product_url': 'https://www.walmart.com/ip/100'},
                    'reason': 'review_incomplete',
                    'diagnostics': [],
                    'error': None,
                },
            },
            {},
        )

        self.assertEqual(len(calls), 10)
        self.assertTrue(all(calls))
        self.assertEqual(recovered, {})
        self.assertIn(1, unresolved)
        self.assertEqual(
            unresolved[1]['diagnostics'][-1]['recovery_attempt'],
            10,
        )

    @patch('walmart.tv.wmart_tv_dt.time.sleep', return_value=None)
    def test_recovery_rounds_remove_successes_from_later_rounds(self, _sleep):
        crawler = bare_crawler()
        crawler.zenrows_recovery_attempts = 3
        call_counts = {1: 0, 2: 0}

        def fake_worker(index, product, mst_specs, zenrows_only=False):
            self.assertTrue(zenrows_only)
            call_counts[index] += 1
            succeeds = index == 1 or call_counts[index] == 2
            if succeeds:
                return (
                    index,
                    {
                        'item': str(index * 100),
                        '_detail_source': 'zenrows_static',
                    },
                    None,
                    None,
                    [],
                )
            return (
                index,
                None,
                None,
                'no_next_data',
                [{'stage': 'detail_next_data_no_next_data'}],
            )

        misses = {
            index: {
                'product': {
                    'product_url': f'https://www.walmart.com/ip/{index * 100}',
                },
                'reason': 'no_next_data',
                'diagnostics': [],
                'error': None,
            }
            for index in (1, 2)
        }
        crawler._crawl_detail_next_data_worker = fake_worker

        recovered, unresolved = crawler.collect_detail_zenrows_recovery_parallel(
            misses,
            {},
        )

        self.assertEqual(call_counts, {1: 1, 2: 2})
        self.assertEqual(set(recovered), {1, 2})
        self.assertEqual(unresolved, {})

    def test_initial_pass_retries_each_miss_once_before_defer(self):
        crawler = bare_crawler()
        crawler.load_mst_specs_cache = lambda products: {}
        calls = []

        def fake_worker(index, product, mst_specs, zenrows_only=False):
            calls.append(zenrows_only)
            if len(calls) == 1:
                return (
                    index,
                    None,
                    None,
                    'no_next_data',
                    [{'stage': 'detail_next_data_no_next_data'}],
                )
            return (
                index,
                {
                    'item': '100',
                    '_detail_source': 'direct',
                },
                None,
                None,
                [],
            )

        crawler._crawl_detail_next_data_worker = fake_worker
        results, misses, _ = crawler._collect_detail_initial_parallel([
            (1, {'product_url': 'https://www.walmart.com/ip/100'}),
        ])

        self.assertEqual(calls, [False, False])
        self.assertEqual(results[1]['item'], '100')
        self.assertEqual(misses, {})

    def test_parallel_miss_is_recovered_without_run_error(self):
        crawler = bare_crawler()
        crawler.detail_next_data_workers = 1
        crawler.zenrows_recovery_workers = 1
        crawler.zenrows_recovery_attempts = 1
        crawler.parallel_miss_reasons = {}
        crawler.load_mst_specs_cache = lambda products: {}
        run_errors = []
        crawler._record_run_error = lambda *args: run_errors.append(args)

        def fake_worker(index, product, mst_specs, zenrows_only=False):
            if zenrows_only:
                return (
                    index,
                    {
                        'item': '100',
                        '_detail_source': 'zenrows_js',
                    },
                    None,
                    None,
                    [],
                )
            return (
                index,
                None,
                None,
                'review_incomplete',
                [{
                    'stage': 'review_next_data_incomplete',
                    'message': 'collected 19',
                }],
            )

        crawler._crawl_detail_next_data_worker = fake_worker
        result = crawler.collect_detail_next_data_parallel([
            (1, {
                'product_url': 'https://www.walmart.com/ip/100',
                'retailer_sku_name': 'Example TV',
            }),
        ])

        self.assertEqual(result[1]['_detail_source'], 'zenrows_js')
        self.assertEqual(crawler.parallel_miss_reasons, {})
        self.assertEqual(run_errors, [])

    def test_unresolved_recovery_records_one_final_error(self):
        crawler = bare_crawler()
        crawler.detail_next_data_workers = 1
        crawler.zenrows_recovery_workers = 1
        crawler.zenrows_recovery_attempts = 1
        crawler.parallel_miss_reasons = {}
        crawler.load_mst_specs_cache = lambda products: {}
        run_errors = []
        crawler._record_run_error = lambda *args: run_errors.append(args)
        crawler._crawl_detail_next_data_worker = (
            lambda index, product, mst_specs, zenrows_only=False: (
                index,
                None,
                None,
                'price_missing',
                [{
                    'stage': 'detail_next_data_price_missing',
                    'message': 'item=100',
                }],
            )
        )

        result = crawler.collect_detail_next_data_parallel([
            (1, {
                'product_url': 'https://www.walmart.com/ip/100',
                'retailer_sku_name': 'Example TV',
            }),
        ])

        self.assertEqual(result, {})
        self.assertEqual(crawler.parallel_miss_reasons, {1: 'price_missing'})
        self.assertEqual(len(run_errors), 1)
        self.assertEqual(run_errors[0][0], 'detail_zenrows_recovery_exhausted')


class DetailRunFlowTests(unittest.TestCase):
    def test_initialize_does_not_require_xpath_or_browser(self):
        crawler = bare_crawler()
        crawler.batch_id = 'w_test'
        crawler.account_name = 'Walmart'
        crawler.page_type = 'detail'
        crawler.connect_db = lambda: True
        crawler.cleanup_old_logs = lambda: None

        def fail_xpath(*args):
            raise AssertionError('detail initialization must not load XPath')

        def fail_browser(*args):
            raise AssertionError('detail initialization must not open Chrome')

        crawler.load_xpaths = fail_xpath
        crawler.setup_browser = fail_browser

        self.assertTrue(crawler.initialize())
        self.assertIsInstance(crawler.next_data_client, WalmartNextDataClient)

    @patch('walmart.tv.wmart_tv_dt.time.sleep', return_value=None)
    def test_run_saves_unresolved_as_listing_only_without_browser(self, _sleep):
        crawler = bare_crawler()
        crawler.batch_id = 'w_test'
        crawler.test_mode = False
        crawler.db_conn = None
        crawler.spec_diffs = []
        crawler.detail_next_data_chunk_size = 40
        crawler.parallel_miss_reasons = {}
        crawler.detail_report = {
            'product': 'TV',
            'main_records': 0,
            'bsr_records': 0,
            'target_records': 0,
            'detail_records': 0,
            'saved_records': 0,
            'redirects': [],
            'run_errors': [],
        }
        products = [
            {
                'product_url': 'https://www.walmart.com/ip/100',
                'retailer_sku_name': 'Recovered TV',
                'page_type': 'main',
                'bsr_rank': None,
            },
            {
                'product_url': 'https://www.walmart.com/ip/200',
                'retailer_sku_name': 'Unresolved TV',
                'page_type': 'bsr',
                'bsr_rank': 1,
                'final_sku_price': '$200.00',
                'fastest_delivery': 'Free shipping, arrives',
            },
        ]
        recovered = products[0].copy()
        recovered.update({
            'item': '100',
            '_detail_source': 'zenrows_static',
            'detailed_review_content': 'review',
        })
        crawler.initialize = lambda: True
        crawler.load_product_list = lambda: products

        def fake_initial_collect(indexed_products):
            misses = {
                2: {
                    'product': products[1],
                    'reason': 'review_incomplete',
                    'diagnostics': [{
                        'stage': 'review_next_data_incomplete',
                        'message': 'collected 19',
                    }],
                    'error': None,
                },
            }
            return {1: recovered}, misses, {}

        def fake_final_recovery(misses, mst_specs):
            return {}, misses

        def fail_browser_fallback(*args, **kwargs):
            raise AssertionError('run must not call browser fallback')

        crawler._collect_detail_initial_parallel = fake_initial_collect
        crawler.collect_detail_zenrows_recovery_parallel = fake_final_recovery
        crawler.crawl_detail = fail_browser_fallback
        detail_saved = []
        listing_saved = []
        crawler.save_detail_result = lambda row: detail_saved.append(row) or True
        crawler.save_to_retail_com = lambda row: listing_saved.append(row) or True

        self.assertTrue(crawler.run())
        self.assertEqual(detail_saved, [recovered])
        self.assertEqual(len(listing_saved), 1)
        self.assertEqual(listing_saved[0]['item'], '200')
        self.assertEqual(listing_saved[0]['final_sku_price'], '$200.00')
        self.assertIsNone(listing_saved[0]['fastest_delivery'])
        self.assertIsNone(listing_saved[0]['count_of_reviews'])
        self.assertEqual(listing_saved[0]['_detail_source'], 'listing_fallback')
        self.assertEqual(crawler.detail_report['target_records'], 2)
        self.assertEqual(crawler.detail_report['detail_records'], 1)
        self.assertEqual(crawler.detail_report['saved_records'], 2)

    @patch('walmart.tv.wmart_tv_dt.time.sleep', return_value=None)
    def test_run_defers_all_chunk_misses_to_one_final_queue(self, _sleep):
        crawler = bare_crawler()
        crawler.batch_id = 'w_test'
        crawler.test_mode = False
        crawler.db_conn = None
        crawler.spec_diffs = []
        crawler.detail_next_data_chunk_size = 1
        crawler.detail_report = {
            'product': 'TV',
            'main_records': 0,
            'bsr_records': 0,
            'target_records': 0,
            'detail_records': 0,
            'saved_records': 0,
            'redirects': [],
            'run_errors': [],
        }
        products = [
            {
                'product_url': 'https://www.walmart.com/ip/100',
                'retailer_sku_name': 'First TV',
                'page_type': 'main',
                'final_sku_price': '$100.00',
            },
            {
                'product_url': 'https://www.walmart.com/ip/200',
                'retailer_sku_name': 'Second TV',
                'page_type': 'main',
                'final_sku_price': '$200.00',
            },
        ]
        events = []
        crawler.initialize = lambda: True
        crawler.load_product_list = lambda: products

        def fake_initial_collect(indexed_products):
            index, product = indexed_products[0]
            events.append(f'initial-{index}')
            return {}, {
                index: {
                    'product': product,
                    'reason': 'no_next_data',
                    'diagnostics': [],
                    'error': None,
                },
            }, {}

        def fake_final_recovery(misses, mst_specs):
            events.append('recovery-' + ','.join(str(index) for index in sorted(misses)))
            recovered = products[0].copy()
            recovered.update({
                'item': '100',
                '_detail_source': 'zenrows_static',
                'detailed_review_content': 'review1 - recovered review',
            })
            return {1: recovered}, {2: misses[2]}

        crawler._collect_detail_initial_parallel = fake_initial_collect
        crawler.collect_detail_zenrows_recovery_parallel = fake_final_recovery
        detail_saved = []
        listing_saved = []
        crawler.save_detail_result = lambda row: detail_saved.append(row) or True
        crawler.save_to_retail_com = lambda row: listing_saved.append(row) or True

        self.assertTrue(crawler.run())
        self.assertEqual(events, ['initial-1', 'initial-2', 'recovery-1,2'])
        self.assertEqual(len(detail_saved), 1)
        self.assertEqual(detail_saved[0]['item'], '100')
        self.assertEqual(len(listing_saved), 1)
        self.assertEqual(listing_saved[0]['item'], '200')
        self.assertEqual(crawler.detail_report['detail_records'], 1)
        self.assertEqual(crawler.detail_report['saved_records'], 2)

    def test_product_list_loader_preserves_prices_and_sanitizes_availability(self):
        row = (
            'Example TV',
            '$200.00',
            '$250.00',
            '3',
            'Free pickup as soon as tomorrow',
            'Free shipping, arrives',
            '12 mins',
            'Rollback',
            '2',
            'Low stock',
            1,
            7,
            'https://www.walmart.com/ip/Example-TV/200',
            'w29',
            '2026-07-17 12:00:00',
            'main',
        )

        class FakeCursor:
            def execute(self, query, params):
                self.query = query
                self.params = params

            def fetchall(self):
                return [row]

            def close(self):
                pass

        class FakeConnection:
            def cursor(self):
                return FakeCursor()

        crawler = bare_crawler()
        crawler.db_conn = FakeConnection()
        crawler.account_name = 'Walmart'
        crawler.batch_id = 'w_test'

        products = crawler.load_product_list()
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]['final_sku_price'], '$200.00')
        self.assertEqual(products[0]['original_sku_price'], '$250.00')
        self.assertEqual(products[0]['pick_up_availability'], 'tomorrow')
        self.assertIsNone(products[0]['fastest_delivery'])
        self.assertEqual(products[0]['delivery_availability'], '12 mins')
        self.assertEqual(products[0]['product_url'], row[12])


class ReviewCollectionTests(unittest.TestCase):
    def test_no_ratings_skips_review_request(self):
        client = FakeNextDataClient({})
        detailed, summary, complete = bare_crawler().collect_reviews_next_data(
            ITEM,
            '0',
            [],
            {'product_url': SCOPED_PRODUCT_URL},
            star_rating='No ratings yet',
            count_of_star_ratings='0',
            next_data_client=client,
            record_errors=False,
            error_collector=[],
            log=False,
        )
        self.assertTrue(complete)
        self.assertIsNone(detailed)
        self.assertEqual(client.urls, [])
        self.assertEqual(summary, {
            'count_of_reviews': '0',
            'star_rating': 'No ratings yet',
            'count_of_star_ratings': '0',
        })

    def test_missing_text_count_is_recovered_from_review_page(self):
        url = build_review_url(ITEM, 1, SCOPED_PRODUCT_URL)
        client = FakeNextDataClient({
            url: review_next_data(
                ITEM,
                ['one', 'two', 'three'],
                3,
                25,
                4.5,
                conditionGroupCode='2',
                classType='REGULAR',
            )
        })
        detailed, summary, complete = bare_crawler().collect_reviews_next_data(
            ITEM,
            None,
            [],
            {'product_url': SCOPED_PRODUCT_URL},
            star_rating='4.5',
            count_of_star_ratings='25',
            next_data_client=client,
            record_errors=False,
            error_collector=[],
            log=False,
        )
        self.assertTrue(complete)
        self.assertEqual(client.urls, [url])
        self.assertEqual(detailed.count(' ||| ') + 1, 3)
        self.assertEqual(summary['count_of_reviews'], '3')

    def test_photo_only_review_gap_preserves_pdp_count(self):
        url = build_review_url(ITEM, 1, SCOPED_PRODUCT_URL)
        client = FakeNextDataClient({
            url: review_next_data(
                ITEM,
                [f'text review {index}' for index in range(1, 10)],
                9,
                10,
                4.5,
                conditionGroupCode='2',
                classType='REGULAR',
            )
        })
        errors = []
        detailed, summary, complete = bare_crawler().collect_reviews_next_data(
            ITEM,
            '10',
            [],
            {'product_url': SCOPED_PRODUCT_URL},
            star_rating='4.5',
            count_of_star_ratings='10',
            next_data_client=client,
            record_errors=False,
            error_collector=errors,
            log=False,
        )

        self.assertFalse(complete)
        self.assertEqual(summary, {
            'count_of_reviews': '10',
            'star_rating': '4.5',
            'count_of_star_ratings': '10',
        })
        self.assertEqual(detailed.count(' ||| ') + 1, 9)
        self.assertEqual(errors, [])

    @patch('walmart.tv.wmart_tv_dt.parse_detail_product')
    def test_partial_reviews_still_produce_detail_row(self, parse_detail_mock):
        review_url = build_review_url(ITEM, 1, SCOPED_PRODUCT_URL)
        client = FakeNextDataClient({
            SCOPED_PRODUCT_URL: {'detail': True},
            review_url: review_next_data(
                ITEM,
                [f'text review {index}' for index in range(1, 10)],
                9,
                10,
                4.5,
                conditionGroupCode='2',
                classType='REGULAR',
            ),
        })
        parse_detail_mock.return_value = {
            'item': ITEM,
            'retailer_sku_name': 'Example TV',
            'count_of_reviews': '10',
            'star_rating': '4.5',
            'count_of_star_ratings': '10',
            'final_sku_price': '$225.00',
            'original_sku_price': None,
            'inline_reviews': [],
            'retailer_sku_name_similar': 'Similar TV',
        }
        crawler = bare_crawler()
        crawler.extract_item = lambda url: ITEM
        crawler.extract_sku = lambda *args, **kwargs: (
            None, None, None, None, None
        )
        crawler.extract_screen_size = lambda *args, **kwargs: (
            '55', 'PDP spec', None, '55'
        )
        crawler.extract_model_year = lambda name: None
        crawler._fill_similar_from_json_response = lambda *args, **kwargs: None

        row = crawler.crawl_detail_next_data(
            {
                'product_url': SCOPED_PRODUCT_URL,
                'retailer_sku_name': 'Example TV',
            },
            next_data_client=client,
            mst_specs={},
            record_errors=False,
            collect_spec_diff=False,
            log=False,
        )

        self.assertIsNotNone(row)
        self.assertEqual(row['count_of_reviews'], '10')
        self.assertEqual(row['screen_size'], '55')
        self.assertEqual(row['retailer_sku_name_similar'], 'Similar TV')
        self.assertEqual(row['detailed_review_content'].count(' ||| ') + 1, 9)
        self.assertEqual(row['_review_mismatch'], {
            'item': ITEM,
            'url': SCOPED_PRODUCT_URL,
            'pdp_review_count': 10,
            'expected_review_bodies': 10,
            'collected_review_bodies': 9,
        })
        crawler.detail_report = {'review_mismatches': [], 'run_errors': []}
        crawler.spec_diffs = []
        crawler.upsert_item_mst = lambda product: True
        crawler.save_to_retail_com = lambda product: True
        self.assertTrue(crawler.save_detail_result(row))
        self.assertEqual(len(crawler.detail_report['review_mismatches']), 1)

    def test_zero_text_reviews_with_ratings_is_validated(self):
        url = build_review_url(ITEM, 1, SCOPED_PRODUCT_URL)
        client = FakeNextDataClient({
            url: review_next_data(
                ITEM,
                [],
                0,
                4,
                3.5,
                conditionGroupCode='2',
                classType='REGULAR',
            )
        })
        detailed, summary, complete = bare_crawler().collect_reviews_next_data(
            ITEM,
            '0',
            [],
            {'product_url': SCOPED_PRODUCT_URL},
            star_rating='3.5',
            count_of_star_ratings='4',
            next_data_client=client,
            record_errors=False,
            error_collector=[],
            log=False,
        )
        self.assertTrue(complete)
        self.assertIsNone(detailed)
        self.assertEqual(client.urls, [url])
        self.assertEqual(summary['count_of_reviews'], '0')

    def test_scoped_single_review_stays_scoped(self):
        url = build_review_url(ITEM, 1, SCOPED_PRODUCT_URL)
        client = FakeNextDataClient({
            url: review_next_data(
                ITEM,
                ['scoped review'],
                1,
                1,
                1,
                conditionGroupCode='2',
                classType='REGULAR',
            )
        })
        errors = []
        detailed, summary, complete = bare_crawler().collect_reviews_next_data(
            ITEM,
            '1',
            ['scoped review'],
            {'product_url': SCOPED_PRODUCT_URL},
            star_rating='1.0',
            count_of_star_ratings='1',
            next_data_client=client,
            record_errors=False,
            error_collector=errors,
            log=False,
        )
        self.assertTrue(complete)
        self.assertEqual(client.urls, [url])
        self.assertEqual(summary, {
            'count_of_reviews': '1',
            'star_rating': '1.0',
            'count_of_star_ratings': '1',
        })
        self.assertEqual(detailed, 'review1 - scoped review')
        self.assertEqual(errors, [])

    def test_twenty_reviews_keep_scope_on_page_two(self):
        source_url = (
            'https://www.walmart.com/ip/example/100?classType=REGULAR'
        )
        page1 = build_review_url('100', 1, source_url)
        page2 = build_review_url('100', 2, source_url)
        client = FakeNextDataClient({
            page1: review_next_data(
                '100',
                [f'p1-{index}' for index in range(10)],
                20,
                25,
                4.5,
                classType='REGULAR',
            ),
            page2: review_next_data(
                '100',
                [f'p2-{index}' for index in range(10)],
                20,
                25,
                4.5,
                classType='REGULAR',
            ),
        })
        detailed, summary, complete = bare_crawler().collect_reviews_next_data(
            '100',
            '20',
            [],
            {'product_url': source_url},
            star_rating='4.5',
            count_of_star_ratings='25',
            next_data_client=client,
            record_errors=False,
            error_collector=[],
            log=False,
        )
        self.assertTrue(complete)
        self.assertEqual(client.urls, [page1, page2])
        self.assertEqual(detailed.count(' ||| ') + 1, 20)
        self.assertEqual(summary['count_of_reviews'], '20')

    def test_optional_extra_page_misses_are_log_notes_not_errors(self):
        source_url = (
            'https://www.walmart.com/ip/example/100?classType=REGULAR'
        )
        page1 = build_review_url('100', 1, source_url)
        page2 = build_review_url('100', 2, source_url)
        page3 = build_review_url('100', 3, source_url)
        page4 = build_review_url('100', 4, source_url)
        client = FakeNextDataClient({
            page1: review_next_data(
                '100',
                [f'p1-{index}' for index in range(10)],
                20,
                25,
                4.5,
                classType='REGULAR',
            ),
            page2: review_next_data(
                '100',
                [f'p2-{index}' for index in range(9)],
                20,
                25,
                4.5,
                classType='REGULAR',
            ),
            page3: review_next_data(
                '100', [], 20, 25, 4.5, classType='REGULAR'
            ),
            page4: review_next_data(
                '100', [], 20, 25, 4.5, classType='REGULAR'
            ),
        })
        errors = []
        notes = []
        detailed, summary, complete = bare_crawler().collect_reviews_next_data(
            '100',
            '20',
            [],
            {'product_url': source_url},
            star_rating='4.5',
            count_of_star_ratings='25',
            next_data_client=client,
            record_errors=False,
            error_collector=errors,
            note_collector=notes,
            log=False,
        )

        self.assertFalse(complete)
        self.assertEqual(detailed.count(' ||| ') + 1, 19)
        self.assertEqual(summary['count_of_reviews'], '20')
        self.assertEqual(errors, [])
        self.assertEqual(
            [note['stage'] for note in notes],
            ['review_page3_next_data', 'review_page4_next_data'],
        )
        self.assertTrue(all(note['message'] == 'no parsed reviews' for note in notes))
        self.assertEqual(
            client.urls,
            [page1, page2, page3, page3, page4, page4],
        )

    def test_required_page_miss_remains_an_error(self):
        source_url = (
            'https://www.walmart.com/ip/example/100?classType=REGULAR'
        )
        page1 = build_review_url('100', 1, source_url)
        page2 = build_review_url('100', 2, source_url)
        page3 = build_review_url('100', 3, source_url)
        page4 = build_review_url('100', 4, source_url)
        empty_page = review_next_data(
            '100', [], 20, 25, 4.5, classType='REGULAR'
        )
        client = FakeNextDataClient({
            page1: review_next_data(
                '100',
                [f'p1-{index}' for index in range(10)],
                20,
                25,
                4.5,
                classType='REGULAR',
            ),
            page2: empty_page,
            page3: empty_page,
            page4: empty_page,
        })
        errors = []
        notes = []

        detailed, _, complete = bare_crawler().collect_reviews_next_data(
            '100',
            '20',
            [],
            {'product_url': source_url},
            star_rating='4.5',
            count_of_star_ratings='25',
            next_data_client=client,
            record_errors=False,
            error_collector=errors,
            note_collector=notes,
            log=False,
        )

        self.assertFalse(complete)
        self.assertEqual(detailed.count(' ||| ') + 1, 10)
        self.assertEqual(
            [error['stage'] for error in errors],
            ['review_page2_next_data'],
        )
        self.assertEqual(
            [note['stage'] for note in notes],
            ['review_page3_next_data', 'review_page4_next_data'],
        )

    def test_optional_page_scope_mismatch_remains_an_error(self):
        source_url = (
            'https://www.walmart.com/ip/example/100?classType=REGULAR'
        )
        page1 = build_review_url('100', 1, source_url)
        page2 = build_review_url('100', 2, source_url)
        page3 = build_review_url('100', 3, source_url)
        client = FakeNextDataClient({
            page1: review_next_data(
                '100',
                [f'p1-{index}' for index in range(10)],
                20,
                25,
                4.5,
                classType='REGULAR',
            ),
            page2: review_next_data(
                '100',
                [f'p2-{index}' for index in range(9)],
                20,
                25,
                4.5,
                classType='REGULAR',
            ),
            page3: review_next_data(
                '999', ['wrong item'], 1, 1, 1, classType='REGULAR'
            ),
        })
        errors = []
        notes = []

        detailed, _, complete = bare_crawler().collect_reviews_next_data(
            '100',
            '20',
            [],
            {'product_url': source_url},
            star_rating='4.5',
            count_of_star_ratings='25',
            next_data_client=client,
            record_errors=False,
            error_collector=errors,
            note_collector=notes,
            log=False,
        )

        self.assertFalse(complete)
        self.assertIsNone(detailed)
        self.assertEqual(errors[0]['stage'], 'review_scope_mismatch')
        self.assertIn('expected=100, actual=999', errors[0]['message'])
        self.assertEqual(notes, [])

    def test_missing_scope_is_rejected_without_partial_merge(self):
        url = build_review_url(ITEM, 1, SCOPED_PRODUCT_URL)
        client = FakeNextDataClient({
            url: review_next_data(ITEM, ['wrong scope'], 734, 8560, 4.4)
        })
        errors = []
        detailed, summary, complete = bare_crawler().collect_reviews_next_data(
            ITEM,
            '1',
            ['scoped review'],
            {'product_url': SCOPED_PRODUCT_URL},
            star_rating='1.0',
            count_of_star_ratings='1',
            next_data_client=client,
            record_errors=False,
            error_collector=errors,
            log=False,
        )
        self.assertFalse(complete)
        self.assertIsNone(detailed)
        self.assertEqual(summary['count_of_reviews'], '1')
        self.assertEqual(errors[0]['stage'], 'review_scope_mismatch')

    def test_summary_mismatch_keeps_pdp_summary_and_accepts_reviews(self):
        url = build_review_url(ITEM, 1, SCOPED_PRODUCT_URL)
        client = FakeNextDataClient({
            url: review_next_data(
                ITEM,
                ['wrong summary'],
                734,
                8560,
                4.4,
                conditionGroupCode='2',
                classType='REGULAR',
            )
        })
        errors = []
        detailed, summary, complete = bare_crawler().collect_reviews_next_data(
            ITEM,
            '1',
            ['scoped review'],
            {'product_url': SCOPED_PRODUCT_URL},
            star_rating='1.0',
            count_of_star_ratings='1',
            next_data_client=client,
            record_errors=False,
            error_collector=errors,
            log=False,
        )
        self.assertTrue(complete)
        self.assertEqual(detailed, 'review1 - wrong summary')
        self.assertEqual(summary, {
            'count_of_reviews': '1',
            'star_rating': '1.0',
            'count_of_star_ratings': '1',
        })
        self.assertEqual(errors, [])

    def test_page_two_summary_mismatch_keeps_pdp_summary(self):
        source_url = 'https://www.walmart.com/ip/example/100?classType=REGULAR'
        page1 = build_review_url('100', 1, source_url)
        page2 = build_review_url('100', 2, source_url)
        client = FakeNextDataClient({
            page1: review_next_data(
                '100',
                [f'p1-{index}' for index in range(10)],
                20,
                25,
                4.5,
                classType='REGULAR',
            ),
            page2: review_next_data(
                '100',
                [f'p2-{index}' for index in range(10)],
                21,
                26,
                4.4,
                classType='REGULAR',
            ),
        })
        errors = []
        detailed, summary, complete = bare_crawler().collect_reviews_next_data(
            '100',
            '20',
            [],
            {'product_url': source_url},
            star_rating='4.5',
            count_of_star_ratings='25',
            next_data_client=client,
            record_errors=False,
            error_collector=errors,
            log=False,
        )
        self.assertTrue(complete)
        self.assertEqual(detailed.count(' ||| ') + 1, 20)
        self.assertEqual(summary, {
            'count_of_reviews': '20',
            'star_rating': '4.5',
            'count_of_star_ratings': '25',
        })
        self.assertEqual(errors, [])

    def test_scope_mismatch_has_priority_in_parallel_reason(self):
        diagnostics = [
            {'stage': 'review_scope_mismatch'},
            {'stage': 'detail_next_data_review_incomplete'},
        ]
        self.assertEqual(
            bare_crawler()._parallel_miss_reason(diagnostics),
            'review_scope_mismatch',
        )


class DetailSaveGuardTests(unittest.TestCase):
    def save_crawler(self):
        crawler = bare_crawler()
        crawler.spec_diffs = []
        crawler.upsert_item_mst = lambda product: True
        crawler.save_to_retail_com = lambda product: True
        return crawler

    def test_valid_scoped_detail_row_reaches_db_writes(self):
        self.assertTrue(
            self.save_crawler().save_detail_result(valid_detail_row())
        )

    def test_equal_original_price_is_cleared_before_db_write(self):
        crawler = self.save_crawler()
        saved_rows = []
        crawler.save_to_retail_com = lambda product: saved_rows.append(product.copy()) or True
        row = valid_detail_row()
        row.update({
            'final_sku_price': '$89.99',
            'original_sku_price': '$89.99',
            'savings': '$0.00',
        })
        self.assertTrue(crawler.save_detail_result(row))
        self.assertIsNone(saved_rows[0]['original_sku_price'])
        self.assertIsNone(saved_rows[0]['savings'])

    def test_similar_normalizer_repairs_encoding_before_db_write(self):
        value = (
            'Sony 55\u00e2\u20ac\u009d class BRAVIA 7 ||| '
            'Bang & Olufsen Beovision \u2013 CanvasTV\u2122'
        )
        self.assertEqual(
            bare_crawler()._normalize_similar_value(value),
            'Sony 55\u201d class BRAVIA 7 ||| '
            'Bang & Olufsen Beovision \u2013 CanvasTV\u2122',
        )

    def test_incomplete_review_bodies_reach_db_writes(self):
        crawler = self.save_crawler()
        db_calls = []
        crawler.upsert_item_mst = lambda product: db_calls.append('mst') or True
        crawler.save_to_retail_com = lambda product: db_calls.append('retail') or True
        row = valid_detail_row()
        row.update({
            'count_of_reviews': '2',
            'count_of_star_ratings': '2',
        })

        self.assertTrue(crawler.save_detail_result(row))
        self.assertEqual(db_calls, ['mst', 'retail'])

    def test_optional_review_notes_are_logged_without_run_errors(self):
        crawler = self.save_crawler()
        crawler.detail_report = {
            'review_mismatches': [],
            'run_errors': [],
        }
        row = valid_detail_row()
        row['_review_notes'] = [{
            'stage': 'review_page3_next_data',
            'message': 'no parsed reviews',
            'logged': False,
        }]

        with patch('builtins.print') as print_mock:
            self.assertTrue(crawler.save_detail_result(row))

        self.assertEqual(crawler.detail_report['run_errors'], [])
        self.assertTrue(any(
            'review_page3_next_data' in str(call)
            for call in print_mock.call_args_list
        ))

    def test_listing_fallback_never_writes_item_mst(self):
        crawler = bare_crawler()
        saved_rows = []
        crawler.upsert_item_mst = lambda product: self.fail(
            'listing fallback must not write tv_item_mst'
        )
        crawler.save_to_retail_com = (
            lambda product: saved_rows.append(product.copy()) or True
        )
        product = {
            'product_url': 'https://www.walmart.com/ip/Example-TV/200',
            'retailer_sku_name': 'Example TV',
            'final_sku_price': '$200.00',
            'fastest_delivery': 'Free shipping, arrives tomorrow',
        }

        self.assertTrue(crawler.save_listing_fallback(
            product,
            'review_incomplete',
        ))
        self.assertEqual(saved_rows[0]['item'], '200')
        self.assertEqual(saved_rows[0]['fastest_delivery'], 'tomorrow')
        self.assertIsNone(saved_rows[0]['count_of_reviews'])

    def test_invalid_rows_are_rejected_before_db_writes(self):
        cases = {
            'item_missing': {'item': None},
            'price_missing': {'final_sku_price': None},
            'final_above_original': {
                'final_sku_price': '$225.00',
                'original_sku_price': '$200.00',
            },
            'reviews_above_ratings': {
                'count_of_reviews': '734',
                'count_of_star_ratings': '1',
            },
            'invalid_star': {'star_rating': '3.3333333333333335'},
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                crawler = self.save_crawler()
                db_calls = []
                crawler.upsert_item_mst = lambda product: db_calls.append('mst') or True
                crawler.save_to_retail_com = lambda product: db_calls.append('retail') or True
                row = valid_detail_row()
                row.update(changes)
                self.assertFalse(crawler.save_detail_result(row))
                self.assertEqual(db_calls, [])


class DetailReportTests(unittest.TestCase):
    def test_review_mismatch_is_warning_without_missing_detail(self):
        body, severity = build_walmart_tv_email_report(
            crawl_results={},
            detail_report={
                'target_records': 1,
                'detail_records': 1,
                'saved_records': 1,
                'review_mismatches': [{
                    'pdp_review_count': 20,
                    'expected_review_bodies': 20,
                    'collected_review_bodies': 19,
                    'url': 'https://www.walmart.com/ip/100',
                }],
            },
            log_file=None,
            elapsed=1,
            failed_stages=[],
        )

        self.assertEqual(severity, 'warning')
        self.assertIn('상품페이지 리뷰 수와 수집 리뷰 본문 수 불일치: 1건', body)
        self.assertIn('PDP reviews=20', body)
        self.assertNotIn('detail missing', body)
        self.assertNotIn('run errors', body)

    def test_required_review_page_error_remains_in_email(self):
        body, severity = build_walmart_tv_email_report(
            crawl_results={},
            detail_report={
                'target_records': 1,
                'detail_records': 1,
                'saved_records': 1,
                'review_mismatches': [],
                'run_errors': [{
                    'stage': 'review_page2_next_data',
                    'url': 'https://www.walmart.com/ip/100',
                    'message': 'no parsed reviews',
                }],
            },
            log_file=None,
            elapsed=1,
            failed_stages=[],
        )

        self.assertEqual(severity, 'warning')
        self.assertIn('run errors: 1', body)
        self.assertIn('필수 리뷰 페이지 실패', body)
        self.assertIn('stage=review_page2_next_data', body)
        self.assertIn('message=no parsed reviews', body)

    def test_run_error_email_uses_korean_labels_and_keeps_raw_details(self):
        run_errors = [
            {
                'stage': 'review_scope_mismatch',
                'url': 'https://www.walmart.com/ip/100',
                'message': 'review response item mismatch: expected=100, actual=999',
            },
            {
                'stage': 'review_scope_mismatch',
                'url': 'https://www.walmart.com/ip/100',
                'message': 'review response scope mismatch: classType expected=REGULAR, actual=VARIANT',
            },
            {
                'stage': 'detail_zenrows_recovery_exhausted',
                'url': 'https://www.walmart.com/ip/200',
                'message': 'reason=price_missing; item=200',
            },
            {
                'stage': 'detail_zenrows_recovery_exhausted',
                'url': 'https://www.walmart.com/ip/300',
                'message': 'reason=no_next_data; no __NEXT_DATA__',
            },
            {
                'stage': 'detail_update',
                'url': 'https://www.walmart.com/ip/400',
                'message': 'database update failed',
            },
            {
                'stage': 'new_unclassified_stage',
                'url': 'https://www.walmart.com/ip/500',
                'message': 'new error details',
            },
        ]

        body, severity = build_walmart_tv_email_report(
            crawl_results={},
            detail_report={
                'target_records': 6,
                'detail_records': 6,
                'saved_records': 6,
                'review_mismatches': [],
                'run_errors': run_errors,
            },
            log_file=None,
            elapsed=1,
            failed_stages=[],
        )

        self.assertEqual(severity, 'warning')
        self.assertIn('실행 오류 / run errors: 6', body)
        self.assertIn('다른 상품 리뷰 응답 감지', body)
        self.assertIn('상품 범위 불일치', body)
        self.assertIn('가격 누락으로 상세 복구 실패', body)
        self.assertIn('상세페이지 데이터 응답 없음으로 상세 복구 실패', body)
        self.assertIn('상세 데이터 업데이트 실패', body)
        self.assertIn('기타 수집 오류', body)
        for item in run_errors:
            self.assertIn(f"stage={item['stage']}", body)
            self.assertIn(f"message={item['message']}", body)


class DetailUpdateSaveTests(unittest.TestCase):
    def test_none_review_body_preserves_existing_value(self):
        class FakeCursor:
            def __init__(self):
                self.query = None
                self.params = None

            def execute(self, query, params):
                self.query = query
                self.params = params

            def close(self):
                pass

        class FakeConnection:
            def __init__(self):
                self.cursor_instance = FakeCursor()
                self.committed = False

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                self.committed = True

            def rollback(self):
                pass

        crawler = WalmartTVDetailUpdateCrawler.__new__(
            WalmartTVDetailUpdateCrawler
        )
        crawler.test_mode = True
        crawler.db_conn = FakeConnection()
        product = {
            'id': 7,
            'detailed_review_content': None,
        }

        self.assertTrue(crawler.save_to_retail_com(product))
        query = crawler.db_conn.cursor_instance.query
        self.assertIn(
            'detailed_review_content = '
            'COALESCE(%s, detailed_review_content)',
            query,
        )
        self.assertTrue(crawler.db_conn.committed)


if __name__ == '__main__':
    unittest.main()
