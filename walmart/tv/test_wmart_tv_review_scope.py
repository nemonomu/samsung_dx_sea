import unittest

from walmart.tv.wmart_tv_dt import WalmartTVDetailCrawler
from walmart.tv.wmart_tv_next_data import (
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
