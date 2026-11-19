import unittest
from unittest.mock import Mock, patch

from amazon.tv.amazon_tv_dt_update import AmazonTVDetailUpdateCrawler


class AmazonTVPriceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.crawler = object.__new__(AmazonTVDetailUpdateCrawler)
        self.crawler.mode = self.crawler.MODE_PRICE_NULL
        self.crawler.account_name = 'Amazon'
        self.crawler.batch_id = 'a_20261109_170010'
        self.crawler.asins = ['B082349G1C', 'B09QRM1LVN']
        self.crawler.start_id = 42
        self.crawler.test_mode = True
        self.crawler.db_conn = Mock()
        self.cursor = self.crawler.db_conn.cursor.return_value
        self.product = {
            'id': 42,
            'item': 'B082349G1C',
            'product_url': 'https://www.amazon.com/dp/B082349G1C',
            'final_sku_price': '$299.00',
            'star_rating': '4.7',
        }

    def test_recovery_selects_only_allowlisted_rows_with_missing_prices(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.crawler.load_product_list(), [])
        query, params = self.cursor.execute.call_args.args
        self.assertIn('FROM test_tv_retail_com', query)
        self.assertIn('account_name = %s AND batch_id = %s', query)
        self.assertIn("NULLIF(BTRIM(final_sku_price), '') IS NULL AND redirect IS NOT TRUE", query)
        self.assertIn('item = ANY(%s)', query)
        self.assertIn('id >= %s', query)
        self.assertEqual(params, ['Amazon', self.crawler.batch_id, self.crawler.asins, 42])

    def test_full_update_checks_that_same_row_still_needs_recovery(self):
        self.cursor.rowcount = 0
        self.assertFalse(self.crawler.save_to_retail_com(self.product))
        query, params = self.cursor.execute.call_args.args
        self.assertIn('final_sku_price = %s', query)
        self.assertIn('star_rating = %s', query)
        self.assertIn('WHERE id = %s AND account_name = %s AND batch_id = %s AND item = %s AND product_url = %s', query)
        self.assertIn("NULLIF(BTRIM(final_sku_price), '') IS NULL AND redirect IS NOT TRUE", query)
        self.assertEqual(params[-5:], [42, 'Amazon', self.crawler.batch_id, self.product['item'], self.product['product_url']])
        self.cursor.rowcount = 1
        self.assertTrue(self.crawler.save_to_retail_com(self.product))

    @patch('builtins.print')
    def test_no_update_for_unrecovered_wrong_or_redirected_product(self, _print):
        for invalid in (
            {'final_sku_price': None},
            {'final_sku_price': 'See price in cart'},
            {'item': 'B0F1DGM1TM'},
            {'redirect': True},
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(self.crawler.save_to_retail_com({**self.product, **invalid}))
        self.crawler.db_conn.cursor.assert_not_called()

    def run_recovery(self, results):
        crawler = self.crawler
        crawler.page = Mock()
        crawler.start_logging = Mock(return_value=None)
        crawler.stop_logging = Mock()
        crawler.initialize = Mock(return_value=True)
        crawler.print_spec_diff_summary = Mock()
        crawler.upsert_item_mst = Mock()
        crawler.load_product_list = Mock(return_value=[
            {'id': i + 1, 'product_url': self.product['product_url']} for i in range(len(results))
        ])
        crawler.crawl_detail = Mock(side_effect=results)
        with patch('amazon.tv.amazon_tv_dt_update.time.sleep'), patch('builtins.print'):
            success = crawler.run()
        crawler.upsert_item_mst.assert_not_called()
        return success

    def test_zero_or_partial_recovery_is_not_success(self):
        self.cursor.rowcount = 1
        missing = {**self.product, 'final_sku_price': None}
        self.assertFalse(self.run_recovery([missing]))
        self.assertEqual(self.crawler.update_summary, {'updated': 0, 'failed': 1, 'skipped': 0})
        self.assertFalse(self.run_recovery([dict(self.product), missing]))
        self.assertEqual(self.crawler.update_summary, {'updated': 1, 'failed': 1, 'skipped': 0})

    def test_complete_recovery_and_no_targets_are_success(self):
        self.cursor.rowcount = 1
        self.assertTrue(self.run_recovery([dict(self.product)]))
        self.assertEqual(self.crawler.update_summary, {'updated': 1, 'failed': 0, 'skipped': 0})
        self.assertTrue(self.run_recovery([]))

    def test_redirect_and_concurrent_row_change_are_skips(self):
        self.cursor.rowcount = 0
        self.assertFalse(self.run_recovery([
            {**self.product, '_detail_skip': 'asin_mismatch'}, dict(self.product),
        ]))
        self.assertEqual(self.crawler.update_summary, {'updated': 0, 'failed': 0, 'skipped': 2})

    def test_successful_retry_is_counted_only_once(self):
        crawler = self.crawler
        self.cursor.rowcount = 1
        crawler.page = Mock()
        crawler.start_logging = Mock(return_value=None)
        crawler.stop_logging = Mock()
        crawler.initialize = Mock(return_value=True)
        crawler.print_spec_diff_summary = Mock()
        crawler.load_product_list = Mock(return_value=[{'id': 42, 'product_url': self.product['product_url']}])
        crawler.restart_browser = Mock(return_value=True)
        crawler.crawl_detail = Mock(side_effect=[RuntimeError('transient error'), dict(self.product)])
        with patch('amazon.tv.amazon_tv_dt_update.time.sleep'), patch('builtins.print'):
            self.assertTrue(crawler.run())
        self.assertEqual(crawler.update_summary, {'updated': 1, 'failed': 0, 'skipped': 0})

    def test_price_recovery_keeps_full_detail_overwrite(self):
        self.cursor.rowcount = 1
        self.assertTrue(self.crawler.save_to_retail_com(self.product))
        query, params = self.cursor.execute.call_args.args
        fields = query.split(' SET ', 1)[1].split(' WHERE ', 1)[0].split(', ')
        data = dict(zip((field.split(' = ')[0] for field in fields), params))
        self.assertEqual(data['star_rating'], '4.7')
        self.assertIn('detailed_review_content', data)
        self.assertIsNone(data['detailed_review_content'])
