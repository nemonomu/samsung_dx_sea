import unittest
from unittest.mock import patch

from walmart.tv.wmart_tv_dt import WalmartTVDetailCrawler
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
    def test_recovery_worker_uses_zenrows_only_until_success(self, _sleep):
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
        result = crawler._crawl_detail_zenrows_recovery_worker(
            1,
            {'product_url': 'https://www.walmart.com/ip/100'},
            {},
            'review_incomplete',
        )

        self.assertEqual(calls, [True, True])
        self.assertEqual(result[1]['item'], '100')
        self.assertEqual(result[5], 2)

    @patch('walmart.tv.wmart_tv_dt.time.sleep', return_value=None)
    def test_recovery_worker_stops_after_ten_failed_attempts(self, _sleep):
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
        result = crawler._crawl_detail_zenrows_recovery_worker(
            1,
            {'product_url': 'https://www.walmart.com/ip/100'},
            {},
            'review_incomplete',
        )

        self.assertEqual(len(calls), 10)
        self.assertTrue(all(calls))
        self.assertIsNone(result[1])
        self.assertEqual(result[5], 10)

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

        def fake_collect(indexed_products):
            crawler.parallel_miss_reasons = {2: 'review_incomplete'}
            return {1: recovered}

        def fail_browser_fallback(*args, **kwargs):
            raise AssertionError('run must not call browser fallback')

        crawler.collect_detail_next_data_parallel = fake_collect
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

    def test_summary_mismatch_is_rejected_without_partial_merge(self):
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
        self.assertFalse(complete)
        self.assertIsNone(detailed)
        self.assertEqual(summary['count_of_reviews'], '1')
        self.assertEqual(errors[0]['stage'], 'review_scope_mismatch')

    def test_page_two_summary_mismatch_is_rejected(self):
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
        self.assertFalse(complete)
        self.assertIsNone(detailed)
        self.assertEqual(summary['count_of_reviews'], '20')
        self.assertEqual(errors[0]['stage'], 'review_scope_mismatch')

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
            'review_bodies_incomplete': {
                'count_of_reviews': '2',
                'count_of_star_ratings': '2',
            },
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


if __name__ == '__main__':
    unittest.main()
