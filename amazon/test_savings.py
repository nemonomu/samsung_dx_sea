"""Offline tests: python -B -m unittest amazon.test_savings -v

Tests TV savings and HHP exclusion using real Amazon save methods with fake DB cursors. The unchanged
cross-retailer base class is stubbed to isolate browser/driver dependencies.
Config is replaced before imports; no credentials, sessions or live DB are used.
"""

from contextlib import ExitStack, redirect_stdout
import importlib
import io
import types
import unittest
from unittest.mock import Mock, patch

from lxml import html

from amazon.savings import (
    build_amazon_extracted_data, extract_page_savings, normalize_savings,
)


# Minimal markup matching the supplied iFFALCON main price badge. No raw page data.
PAGE_BADGE = """
<div id="corePriceDisplay_desktop_feature_div">
  <div class="a-section apex-core-price-identifier">
    <span class="apex-savings-container">
      <span aria-hidden="true" class="a-size-large a-color-price savingPriceOverride aok-align-center reinventPriceSavingsPercentageMargin savingsPercentage apex-savings-percentage">-21%</span>
    </span>
    <span class="a-price"><span class="a-offscreen">$699.99</span></span>
    <span>List Price: $885.99</span>
  </div>
</div>
"""


class SavingsPercentageTests(unittest.TestCase):
    def test_normalization_preserves_percentage_and_removes_minus(self):
        for raw, expected in [('-21%', '21%'), ('21%', '21%'),
                              (' \n-21%\t', '21%'), ('\u221221%', '21%'),
                              ('- 21 %', '21%'), ('-12.5%', '12.5%'),
                              ('0%', '0%'), ('-100%', '100%')]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_savings(raw), expected)

    def test_invalid_or_old_amount_savings_are_null(self):
        for value in (None, '', ' ', '$186.00', '$652.00', '-21', 'Save 21%',
                      '+21%', '--21%', '-101%', 'NaN%', '21% off',
                      '21% - 30%', True, 21, 21.0, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(normalize_savings(value))

    def test_supplied_badge_keeps_raw_minus_until_db_mapping(self):
        raw = extract_page_savings(html.fromstring(PAGE_BADGE))
        self.assertEqual(raw, '-21%')
        product = {'original_sku_price': '$885.99', 'final_sku_price': '$699.99',
                   'savings': raw, 'item': 'test-item'}
        before = dict(product)
        data = build_amazon_extracted_data(product, list(product))
        self.assertEqual(data['savings'], '21%')
        self.assertEqual(product, before)
        self.assertEqual(data['original_sku_price'], '$885.99')
        self.assertEqual(data['final_sku_price'], '$699.99')

    def test_missing_badge_does_not_use_prices_or_other_discounts(self):
        markup = PAGE_BADGE.replace('savingsPercentage ', '') + (
            '<div id="recommendations"><span class="savingsPercentage">-70%</span></div>'
            '<span>Get $50 off instantly</span>')
        raw = extract_page_savings(html.fromstring('<html>' + markup + '</html>'))
        self.assertIsNone(raw)
        product = {'savings': raw, 'original_sku_price': '$885.99',
                   'final_sku_price': '$699.99'}
        self.assertIsNone(build_amazon_extracted_data(product, product)['savings'])
        self.assertIsNone(extract_page_savings(None))

    def test_hidden_badges_are_skipped(self):
        for attribute in ('class="aok-hidden"', 'hidden', 'style="display: none"',
                          'style="visibility: hidden"', 'class="a-offscreen"'):
            with self.subTest(attribute=attribute):
                hidden = '<div ' + attribute + '>' + PAGE_BADGE.replace('-21%', '-90%') + '</div>'
                tree = html.fromstring('<html>' + hidden + PAGE_BADGE + '</html>')
                self.assertEqual(extract_page_savings(tree), '-21%')
                self.assertIsNone(extract_page_savings(html.fromstring(hidden)))

    def test_duplicate_and_conflicting_badges(self):
        self.assertEqual(extract_page_savings(html.fromstring(
            '<html>' + PAGE_BADGE * 2 + '</html>')), '-21%')
        self.assertIsNone(extract_page_savings(html.fromstring(
            '<html>' + PAGE_BADGE + PAGE_BADGE.replace('-21%', '-30%') + '</html>')))

    def test_legacy_core_price_container(self):
        tree = html.fromstring(PAGE_BADGE.replace(
            'corePriceDisplay_desktop_feature_div', 'corePrice_feature_div'))
        self.assertEqual(extract_page_savings(tree), '-21%')

    def test_fields_without_savings_do_not_add_it(self):
        self.assertEqual(build_amazon_extracted_data({'item': 'test'}, ['item']),
                         {'item': 'test'})


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
                    savings='-21%', star_rating='4.5',
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

    def test_insert_and_update_use_page_percentage_and_preserve_other_fields(self):
        for cls in self.crawlers:
            for test_mode in (False, True):
                with self.subTest(crawler=cls.__name__, test_mode=test_mode):
                    crawler = self.make_crawler(cls, test_mode)
                    product = self.product()
                    before = dict(product)
                    self.assertTrue(crawler.save_to_retail_com(product))
                    query, data = self.saved_data(crawler)
                    self.assertEqual(product, before)
                    self.assertEqual(data['savings'], '21%')
                    for field in ('original_sku_price', 'final_sku_price', 'item',
                                  'star_rating', 'detailed_review_content'):
                        self.assertEqual(data[field], product[field])
                    table = 'tv_retail_com'
                    table = ('test_' if test_mode else '') + table
                    self.assertIn(' ' + table + ' ', query)
                    if query.startswith('INSERT'):
                        self.assertEqual(data['account_name'], 'Amazon')

    def test_detail_collection_replaces_stale_savings_before_insert_and_update(self):
        for cls in self.crawlers:
            for markup, raw, stored in (
                (PAGE_BADGE, '-21%', '21%'),
                ('<html><span>List Price: $885.99; Price: $699.99</span></html>', None, None),
            ):
                with self.subTest(crawler=cls.__name__, raw=raw):
                    crawler = self.make_crawler(cls)
                    crawler.page = Mock(html=markup, url='https://example.test/product')
                    crawler.page.run_js.return_value = markup
                    crawler.xpaths = {}
                    crawler.product_type = 'tv'
                    crawler._first_detail_html_saved = True
                    crawler.spec_diffs = []
                    for name, value in {
                        'recover_amazon_pages': True,
                        'resolve_loaded_product_url_for_tv': None,
                        'should_take_capture': False,
                        'extract_final_sku_price': '$699.99',
                        'extract_original_sku_price': '$885.99',
                        'safe_extract_chain': None,
                        'normalize_sku_popularity': None,
                        'extract_delivery_field': None,
                        'convert_first_number': None,
                        'normalize_available_quantity_for_purchase': None,
                        'scroll_to_section': None,
                        'get_tv_specs_from_mst': (None, None, None),
                        'extract_sku': (None, None, None),
                        'extract_model_year': None,
                        'extract_screen_size': (None, None, None),
                        'move_to_review_section': None,
                        'extract_star_rating': '4.5',
                        'extract_count_of_star_rating': '37',
                        'extract_reviews_with_retry': (None, 0, False),
                    }.items():
                        setattr(crawler, name, Mock(return_value=value))
                    crawler.resolve_hidden_price_from_cart = Mock(
                        side_effect=lambda tree, *args: ('$699.99', False, None, tree, None))
                    product = self.product()
                    product.update(product_url='https://example.test/product', savings='-99%')
                    # crawl_detail is inherited from the detail module by UPDATE.
                    detail_module = importlib.import_module(self.crawlers[0].__module__)
                    with redirect_stdout(io.StringIO()), patch.object(detail_module.time, 'sleep'):
                        result = crawler.crawl_detail(product)
                    self.assertEqual(result['savings'], raw)
                    self.assertEqual(result['original_sku_price'], '$885.99')
                    self.assertEqual(result['final_sku_price'], '$699.99')
                    self.assertTrue(crawler.save_to_retail_com(result))
                    self.assertEqual(self.saved_data(crawler)[1]['savings'], stored)

    def test_failed_detail_collection_clears_previous_percentage(self):
        for cls in self.crawlers:
            crawler = self.make_crawler(cls)
            crawler.page = Mock(url='https://example.test/product')
            crawler.page.get.side_effect = RuntimeError('test page failure')
            product = self.product()
            product['product_url'] = 'https://example.test/product'
            with redirect_stdout(io.StringIO()):
                result = crawler.crawl_detail(product)
            self.assertIsNone(result['savings'])
            self.assertTrue(crawler.save_to_retail_com(result))
            self.assertIsNone(self.saved_data(crawler)[1]['savings'])

    def test_missing_badge_and_old_amounts_save_null_despite_valid_prices(self):
        for cls in self.crawlers:
            for value in (None, '', '$652.00'):
                with self.subTest(crawler=cls.__name__, savings=value):
                    crawler = self.make_crawler(cls)
                    product = self.product()
                    product['savings'] = value
                    self.assertTrue(crawler.save_to_retail_com(product))
                    self.assertIsNone(self.saved_data(crawler)[1]['savings'])

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
                        with patch('amazon.savings.normalize_savings',
                                   side_effect=AssertionError('HHP must not process savings')):
                            self.assertTrue(crawler.save_to_retail_com(product))
                        query, data = self.saved_data(crawler)
                        self.assertNotIn('savings', data)
                        self.assertEqual(product, before)
                        self.assertEqual(data['final_sku_price'], final)
                        self.assertEqual(data['original_sku_price'], before['original_sku_price'])
                        table = ('test_' if test_mode else '') + 'hhp_retail_com'
                        self.assertIn(' ' + table + ' ', query)

    def test_page_percentage_does_not_depend_on_valid_prices(self):
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
                    self.assertEqual(data['savings'], '21%')
                    self.assertEqual(data['original_sku_price'], original)
                    self.assertEqual(data['final_sku_price'], final)

    def test_equal_prices_without_badge_save_null(self):
        for cls in self.crawlers:
            crawler = self.make_crawler(cls)
            product = self.product()
            product['final_sku_price'] = product['original_sku_price']
            product['savings'] = None
            self.assertTrue(crawler.save_to_retail_com(product))
            self.assertIsNone(self.saved_data(crawler)[1]['savings'])

    def test_listing_only_insert_without_original_price_is_null(self):
        for cls in (self.crawlers[0],):
            crawler = self.make_crawler(cls)
            product = {'item': 'test-item', 'final_sku_price': '$847.99'}
            self.assertTrue(crawler.save_to_retail_com(product))
            self.assertIsNone(self.saved_data(crawler)[1]['savings'])

    def test_tv_listing_fallback_clears_uncollected_savings(self):
        crawler = self.make_crawler(self.crawlers[0])
        crawler.detail_report = {'run_errors': [], 'saved_records': 0,
                                 'listing_only_records': 0}
        crawler._product_counts_loaded = True
        crawler.load_product_list = Mock(return_value=[self.product()])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(crawler.save_listing_only_fallback('test'), 1)
        _, data = self.saved_data(crawler)
        self.assertIsNone(data['savings'])
        self.assertIsNone(data['redirect'])
        self.assertEqual(crawler.detail_report['listing_only_records'], 1)

    def test_all_price_update_modes_save_page_percentage(self):
        for cls, modes in [(self.crawlers[1], ('1', '2', '3', '5'))]:
            for mode in modes:
                with self.subTest(crawler=cls.__name__, mode=mode):
                    crawler = self.make_crawler(cls, mode=mode)
                    self.assertTrue(crawler.save_to_retail_com(self.product()))
                    self.assertEqual(self.saved_data(crawler)[1]['savings'], '21%')

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
