"""Offline tests: python -B -m unittest amazon.test_savings -v

Tests TV savings and HHP exclusion using real Amazon save methods with fake DB cursors. The unchanged
cross-retailer base class is stubbed to isolate browser/driver dependencies.
Config is replaced before imports; no credentials, sessions or live DB are used.
"""

from contextlib import ExitStack, redirect_stdout
from decimal import Decimal, localcontext
import importlib
import io
import types
import unittest
from unittest.mock import Mock, patch

from amazon.savings import build_amazon_extracted_data, calculate_savings


class SavingsCalculationTests(unittest.TestCase):
    def test_valid_prices_and_formatting(self):
        cases = [
            ('$1,499.99', '$847.99', '$652.00'),
            ('$179.99', '$134.98', '$45.01'),
            ('$3,499.99', '$2,297.99', '$1,202.00'),
            ('$579.99', '$497.99', '$82.00'),
            ('$1,299.99', '$747.99', '$552.00'),
            ('$1.00', '$0.99', '$0.01'),
            ('$0.30', '$0.10', '$0.20'),
            ('$100', '$20.5', '$79.50'),
            ('1499.99', '847.99', '$652.00'),
            (' \t$\u00a01,499.99\n', ' $ 847.99 ', '$652.00'),
            ('$10.00', '$10.00', '$0.00'),
            ('$10.00', '$0.00', '$10.00'),
            ('$0', '$0', '$0.00'),
        ]
        for original, final, expected in cases:
            with self.subTest(original=original, final=final):
                self.assertEqual(calculate_savings(original, final), expected)

    def test_non_prices_are_null_in_either_column(self):
        invalid = [
            None, '', ' ', 'NULL', 'N/A', '품절', 'Out of stock',
            'Currently unavailable.', 'No featured offers available',
            'See price in cart', 'To see our price, add this item to your cart.',
            'Only 3 left in stock', 'No featured offers available from $99.99',
            'From $99.99', '$99.99 / month', '$99.99 - $199.99',
            '$99.99 ($10 savings)', '$1,49.99', '$1,,499.99', '$1, 499.99',
            '$1.499,99', '$99.999', '$', '$-10.00', '-10', '+10',
            'NaN', 'Infinity', '1e2', '€10.00', '£10.00', 'C$10.00',
            '$１２.００', '$10.00\nextra', True, 100, 99.99,
            Decimal('100'), [], {},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(calculate_savings('$1,499.99', value))
                self.assertIsNone(calculate_savings(value, '$847.99'))

    def test_inverted_prices_are_null(self):
        self.assertIsNone(calculate_savings('$99.99', '$100.00'))

    def test_precision_does_not_depend_on_global_decimal_context(self):
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(calculate_savings('$1,499.99', '$847.99'), '$652.00')
        self.assertEqual(
            calculate_savings('$123456789012345678901234567890.99', '$0.98'),
            '$123,456,789,012,345,678,901,234,567,890.01',
        )

    def test_derived_value_replaces_stale_savings_without_changing_input(self):
        product = {'original_sku_price': '$100', 'final_sku_price': '$80',
                   'savings': '$999.00', 'item': 'test-item'}
        original = dict(product)
        data = build_amazon_extracted_data(product, list(product))
        self.assertEqual(data['savings'], '$20.00')
        self.assertEqual(product, original)
        product['final_sku_price'] = 'No featured offers available'
        data = build_amazon_extracted_data(product, list(product))
        self.assertIsNone(data['savings'])
        self.assertEqual(data['final_sku_price'], product['final_sku_price'])


class AmazonSavingsSaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        fake_config = types.ModuleType('config')
        fake_config.DB_CONFIG = {}
        fake_config.EMAIL_CONFIG = {}
        fake_base = types.ModuleType('common.base_crawler')
        fake_base.BaseCrawler = type('BaseCrawler', (), {})
        stack.enter_context(patch.dict('sys.modules', {
            'config': fake_config, 'common.base_crawler': fake_base,
        }))
        stack.enter_context(patch('common.setup.setup_environment'))
        stack.enter_context(patch('socket.socket.connect',
                                  side_effect=AssertionError('Network forbidden')))
        stack.enter_context(patch('psycopg2.connect',
                                  side_effect=AssertionError('Live DB forbidden')))
        modules = [
            ('amazon.tv.amazon_tv_dt', 'AmazonTVDetailCrawler'),
            ('amazon.tv.amazon_tv_dt_update', 'AmazonTVDetailUpdateCrawler'),
            ('amazon.hhp.amazon_hhp_dt', 'AmazonDetailCrawler'),
            ('amazon.hhp.amazon_hhp_dt_update', 'AmazonDetailUpdateCrawler'),
        ]
        loaded = [getattr(importlib.import_module(module), name)
                  for module, name in modules]
        cls.crawlers = loaded[:2]  # TV only
        cls.excluded_crawlers = loaded[2:]  # HHP must stay unchanged

    def make_crawler(self, cls, test_mode=False, mode='1'):
        crawler = object.__new__(cls)
        crawler.db_conn = Mock()
        crawler.test_mode = test_mode
        crawler.mode = mode
        crawler.batch_id = 'test-batch'
        crawler.account_name = 'Amazon'
        crawler.ensure_listing_item = Mock()
        crawler.ensure_db_connection = Mock(return_value=True)
        crawler.safe_rollback = Mock()
        return crawler

    @staticmethod
    def product():
        return dict(id=123, item='test-item', redirect=False,
                    original_sku_price='$1,499.99', final_sku_price='$847.99',
                    savings='$999.00', star_rating='4.5',
                    detailed_review_content='test review')

    def saved_data(self, crawler):
        cursor = crawler.db_conn.cursor.return_value
        cursor.execute.assert_called_once()
        query, values = cursor.execute.call_args.args
        self.assertEqual(query.count('%s'), len(values))
        if query.startswith('INSERT'):
            columns = query.split('(', 1)[1].split(')', 1)[0].split(', ')
        else:
            columns = [entry.split(' = ', 1)[0] for entry in
                       query.split(' SET ', 1)[1].split(' WHERE ', 1)[0].split(', ')]
            self.assertTrue(query.endswith('WHERE id = %s'))
            self.assertEqual(values[-1], 123)
            values = values[:-1]
        self.assertEqual(len(columns), len(set(columns)))
        self.assertEqual(len(columns), len(values))
        crawler.db_conn.commit.assert_called_once()
        cursor.close.assert_called_once()
        return query, dict(zip(columns, values))

    def test_insert_and_update_use_final_prices_and_preserve_other_fields(self):
        for cls in self.crawlers:
            for test_mode in (False, True):
                with self.subTest(crawler=cls.__name__, test_mode=test_mode):
                    crawler = self.make_crawler(cls, test_mode)
                    product = self.product()
                    before = dict(product)
                    self.assertTrue(crawler.save_to_retail_com(product))
                    query, data = self.saved_data(crawler)
                    self.assertEqual(product, before)
                    self.assertEqual(data['savings'], '$652.00')
                    for field in ('original_sku_price', 'final_sku_price', 'item',
                                  'star_rating', 'detailed_review_content'):
                        self.assertEqual(data[field], product[field])
                    table = 'tv_retail_com'
                    table = ('test_' if test_mode else '') + table
                    self.assertIn(' ' + table + ' ', query)
                    if query.startswith('INSERT'):
                        self.assertEqual(data['account_name'], 'Amazon')

    def test_hhp_insert_and_update_do_not_calculate_or_write_savings(self):
        for cls in self.excluded_crawlers:
            for test_mode in (False, True):
                for final in ('$847.99', 'No featured offers available', None):
                    with self.subTest(crawler=cls.__name__, test_mode=test_mode, final=final):
                        crawler = self.make_crawler(cls, test_mode=test_mode)
                        product = self.product()
                        product['final_sku_price'] = final
                        before = dict(product)
                        self.assertNotIn('savings', crawler.EXTRACTED_FIELDS)
                        with patch('amazon.savings.calculate_savings',
                                   side_effect=AssertionError('HHP must not calculate savings')):
                            self.assertTrue(crawler.save_to_retail_com(product))
                        query, data = self.saved_data(crawler)
                        self.assertNotIn('savings', data)
                        self.assertEqual(product, before)
                        self.assertEqual(data['final_sku_price'], final)
                        self.assertEqual(data['original_sku_price'], before['original_sku_price'])
                        table = ('test_' if test_mode else '') + 'hhp_retail_com'
                        self.assertIn(' ' + table + ' ', query)

    def test_non_numeric_and_missing_prices_save_sql_null_and_keep_raw_values(self):
        for cls in self.crawlers:
            for original, final in [
                ('$1,499.99', 'No featured offers available'),
                ('$1,499.99', '품절'), ('$1,499.99', 'Only 3 left in stock'),
                (None, '$847.99'), ('$1,499.99', None), (None, None),
                ('$1,499.99', '$847.999'), ('$1.00', '$2.00'),
            ]:
                with self.subTest(crawler=cls.__name__, original=original, final=final):
                    crawler = self.make_crawler(cls)
                    product = self.product()
                    product.update(original_sku_price=original, final_sku_price=final)
                    self.assertTrue(crawler.save_to_retail_com(product))
                    _, data = self.saved_data(crawler)
                    self.assertIsNone(data['savings'])
                    self.assertEqual(data['original_sku_price'], original)
                    self.assertEqual(data['final_sku_price'], final)

    def test_equal_prices_save_zero(self):
        for cls in self.crawlers:
            crawler = self.make_crawler(cls)
            product = self.product()
            product['final_sku_price'] = product['original_sku_price']
            self.assertTrue(crawler.save_to_retail_com(product))
            self.assertEqual(self.saved_data(crawler)[1]['savings'], '$0.00')

    def test_listing_only_insert_without_original_price_is_null(self):
        for cls in (self.crawlers[0],):
            crawler = self.make_crawler(cls)
            product = {'item': 'test-item', 'final_sku_price': '$847.99'}
            self.assertTrue(crawler.save_to_retail_com(product))
            self.assertIsNone(self.saved_data(crawler)[1]['savings'])

    def test_tv_listing_fallback_uses_the_same_savings_calculation(self):
        crawler = self.make_crawler(self.crawlers[0])
        crawler.detail_report = {'run_errors': [], 'saved_records': 0,
                                 'listing_only_records': 0}
        crawler._product_counts_loaded = True
        crawler.load_product_list = Mock(return_value=[self.product()])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(crawler.save_listing_only_fallback('test'), 1)
        _, data = self.saved_data(crawler)
        self.assertEqual(data['savings'], '$652.00')
        self.assertIsNone(data['redirect'])
        self.assertEqual(crawler.detail_report['listing_only_records'], 1)

    def test_all_price_update_modes_recalculate_savings(self):
        for cls, modes in [(self.crawlers[1], ('1', '2', '3', '5'))]:
            for mode in modes:
                with self.subTest(crawler=cls.__name__, mode=mode):
                    crawler = self.make_crawler(cls, mode=mode)
                    self.assertTrue(crawler.save_to_retail_com(self.product()))
                    self.assertEqual(self.saved_data(crawler)[1]['savings'], '$652.00')

    def test_review_only_update_does_not_touch_prices_or_savings(self):
        cls = self.crawlers[1]
        crawler = self.make_crawler(cls, mode='4')
        module = importlib.import_module(cls.__module__)
        with patch.object(module, 'build_amazon_extracted_data') as calculate:
            self.assertTrue(crawler.save_to_retail_com(self.product()))
            calculate.assert_not_called()
        _, data = self.saved_data(crawler)
        self.assertEqual(set(data), {'detailed_review_content', 'crawl_datetime'})

    def test_failed_review_only_update_is_still_skipped(self):
        crawler = self.make_crawler(self.crawlers[1], mode='4')
        product = self.product()
        product['detailed_review_content'] = None
        with redirect_stdout(io.StringIO()):
            self.assertFalse(crawler.save_to_retail_com(product))
        crawler.db_conn.cursor.assert_not_called()

    def test_missing_update_id_and_empty_input_do_not_write(self):
        for cls in self.crawlers:
            crawler = self.make_crawler(cls)
            self.assertFalse(crawler.save_to_retail_com({}))
            crawler.db_conn.cursor.assert_not_called()
        for cls in (self.crawlers[1],):
            crawler = self.make_crawler(cls)
            product = self.product()
            del product['id']
            with redirect_stdout(io.StringIO()):
                self.assertFalse(crawler.save_to_retail_com(product))
            crawler.db_conn.cursor.assert_not_called()

    def test_db_failure_still_rolls_back_without_commit(self):
        for cls in self.crawlers:
            crawler = self.make_crawler(cls)
            crawler.db_conn.cursor.return_value.execute.side_effect = RuntimeError('test')
            with redirect_stdout(io.StringIO()), patch('traceback.print_exc'):
                self.assertFalse(crawler.save_to_retail_com(self.product()))
            crawler.db_conn.commit.assert_not_called()
            if cls is self.crawlers[0]:
                crawler.safe_rollback.assert_called_once()
            else:
                crawler.db_conn.rollback.assert_called_once()


if __name__ == '__main__':
    unittest.main()
