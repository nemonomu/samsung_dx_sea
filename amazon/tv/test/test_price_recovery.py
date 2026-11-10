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
