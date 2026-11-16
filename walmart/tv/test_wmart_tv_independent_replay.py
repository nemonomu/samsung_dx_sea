"""Independent offline contract checks using retained Walmart page captures."""
import copy
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock

from walmart.tv.wmart_tv_dt import WalmartTVDetailCrawler
from walmart.tv.wmart_tv_dt_update import WalmartTVDetailUpdateCrawler
from walmart.tv.wmart_tv_next_data import WalmartNextDataClient, extract_next_data, get_initial_data

CAPTURES = Path('C:/Users/User/Desktop/크롤러/검증/walmart_zip_11581_20261116')
IDS = ('17309421750', '20008614541', '20272208349')

def encoded(data):
    return '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'

def direct(body):
    return {'source': 'direct', 'status': 200, 'blocked': False}, body

def browser(body):
    return {'source': 'browser_render', 'content_valid': True, 'location_verified': False,
            'location_zip': '95829'}, body

@unittest.skipUnless(all((CAPTURES / f'{item}_{kind}.html').is_file() for item in IDS for kind in ('browser', 'selected')), 'retained captures unavailable')
class SavedPageReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bodies = {(item, kind): (CAPTURES / f'{item}_{kind}.html').read_text(encoding='utf-8')
                      for item in IDS for kind in ('browser', 'selected')}
        cls.data = {item: extract_next_data(cls.bodies[item, 'browser']) for item in IDS}

    def crawler_and_client(self, direct_response, rendered_response):
        crawler = WalmartTVDetailCrawler.__new__(WalmartTVDetailCrawler)
        crawler.extract_sku = Mock(return_value=(None, None, None, None, None))
        crawler.extract_screen_size = Mock(return_value=(None, None, None, None))
        crawler.extract_model_year = Mock(return_value=None)
        crawler._fill_similar_from_json_response = Mock()
        def reviews(item, count, inline, product, star_rating=None, count_of_star_ratings=None, **kwargs):
            return None, {'count_of_reviews': count, 'star_rating': star_rating,
                          'count_of_star_ratings': count_of_star_ratings}, False
        crawler.collect_reviews_next_data = reviews
        renderer = Mock()
        renderer.fetch_html.return_value = rendered_response
        client = WalmartNextDataClient(renderer=renderer)
        client.fetch_direct_html = Mock(return_value=direct_response)
        client.fetch_zenrows_html = Mock(side_effect=AssertionError('unexpected ZenRows'))
        return crawler, client, renderer

    def crawl(self, item, direct_response, rendered_response):
        crawler, client, renderer = self.crawler_and_client(direct_response, rendered_response)
        name = get_initial_data(self.data[item])['product']['name']
        row = crawler.crawl_detail_next_data(
            {'product_url': f'https://www.walmart.com/ip/{item}', 'retailer_sku_name': name},
            next_data_client=client, mst_specs={}, record_errors=False,
            collect_spec_diff=False, log=False,
        )
        return row, client, renderer

    def test_all_original_captures_same_item_with_unverified_zip(self):
        for item in IDS:
            for kind in ('browser', 'selected'):
                with self.subTest(item=item, capture=kind):
                    body = self.bodies[item, kind]
                    row, client, renderer = self.crawl(item, direct(body), browser(body))
                    self.assertEqual(row['item'], item)
                    self.assertEqual(row['_detail_source'], 'direct')
                    self.assertIsNotNone(row['final_sku_price'])
                    renderer.fetch_html.assert_not_called()
                    client.fetch_zenrows_html.assert_not_called()

    def test_retained_page_with_price_removed_is_preserved(self):
        item = IDS[0]
        changed = copy.deepcopy(self.data[item])
        get_initial_data(changed)['product']['priceInfo'] = {}
        row, client, renderer = self.crawl(item, direct(encoded(changed)), browser(self.bodies[item, 'browser']))
        self.assertEqual(row['item'], item)
        self.assertIsNone(row['final_sku_price'])
        self.assertEqual(row['_detail_source'], 'direct')
        self.assertIsNotNone(row['count_of_reviews'])
        renderer.fetch_html.assert_not_called()
        client.fetch_zenrows_html.assert_not_called()

    def test_unverified_zip_browser_is_used_without_zenrows(self):
        item = IDS[1]; body = self.bodies[item, 'browser']
        row, client, renderer = self.crawl(item, ({'source': 'direct', 'status': 403}, ''), browser(body))
        self.assertEqual(row['item'], item)
        self.assertEqual(row['_detail_source'], 'browser_render')
        renderer.fetch_html.assert_called_once()
        client.fetch_zenrows_html.assert_not_called()

    def test_wrong_item_with_different_name_uses_browser(self):
        item = IDS[0]; changed = copy.deepcopy(self.data[item])
        product = get_initial_data(changed)['product']
        product['usItemId'] = '99999999999'
        product['name'] = 'Completely unrelated television'
        row, client, renderer = self.crawl(item, direct(encoded(changed)), browser(self.bodies[item, 'browser']))
        self.assertEqual(row['item'], item)
        self.assertEqual(row['_detail_source'], 'browser_render')
        renderer.fetch_html.assert_called_once()
        client.fetch_zenrows_html.assert_not_called()

    def test_matching_name_redirect_keeps_existing_contract(self):
        item = IDS[0]; changed = copy.deepcopy(self.data[item])
        get_initial_data(changed)['product']['usItemId'] = '99999999999'
        row, client, renderer = self.crawl(item, direct(encoded(changed)), browser(self.bodies[item, 'browser']))
        self.assertEqual(row['item'], '99999999999')
        self.assertEqual(row['_detail_source'], 'direct')
        renderer.fetch_html.assert_not_called()
        client.fetch_zenrows_html.assert_not_called()

    def test_partial_browser_data_is_kept_without_zenrows(self):
        item = IDS[0]; changed = copy.deepcopy(self.data[item])
        get_initial_data(changed)['product']['priceInfo'] = {}
        row, client, renderer = self.crawl(item, ({'source': 'direct', 'status': 403}, ''), browser(encoded(changed)))
        self.assertEqual(row['item'], item)
        self.assertIsNone(row['final_sku_price'])
        self.assertEqual(row['_detail_source'], 'browser_render')
        renderer.fetch_html.assert_called_once()
        client.fetch_zenrows_html.assert_not_called()

    def test_zenrows_is_used_after_direct_and_browser_fail(self):
        item = IDS[0]; body = self.bodies[item, 'browser']
        crawler, client, renderer = self.crawler_and_client(
            ({'source': 'direct', 'status': 403}, ''),
            ({'source': 'browser_render', 'content_valid': False}, ''),
        )
        client.fetch_zenrows_html = Mock(return_value=({'source': 'zenrows_static', 'status': 200}, body))
        name = get_initial_data(self.data[item])['product']['name']
        row = crawler.crawl_detail_next_data(
            {'product_url': f'https://www.walmart.com/ip/{item}', 'retailer_sku_name': name},
            next_data_client=client, mst_specs={}, record_errors=False,
            collect_spec_diff=False, log=False,
        )
        self.assertEqual(row['item'], item)
        self.assertEqual(row['_detail_source'], 'zenrows_static')
        renderer.fetch_html.assert_called_once()
        client.fetch_zenrows_html.assert_called_once()

    def test_empty_product_uses_browser(self):
        item = IDS[0]; changed = copy.deepcopy(self.data[item])
        get_initial_data(changed)['product'] = {}
        row, client, renderer = self.crawl(item, direct(encoded(changed)), browser(self.bodies[item, 'browser']))
        self.assertEqual(row['item'], item)
        self.assertEqual(row['_detail_source'], 'browser_render')
        renderer.fetch_html.assert_called_once()
        client.fetch_zenrows_html.assert_not_called()

class SQLiteCursor:
    def __init__(self, cursor): self.cursor = cursor
    def execute(self, sql, params): return self.cursor.execute(sql.replace('%s', '?'), params)
    @property
    def rowcount(self): return self.cursor.rowcount
    def close(self): self.cursor.close()

class SQLiteConnection:
    def __init__(self, conn): self.conn = conn
    def cursor(self): return SQLiteCursor(self.conn.cursor())
    def commit(self): self.conn.commit()

class LocalUpdateTests(unittest.TestCase):
    def test_missing_price_preserves_existing_value_and_zero_is_written(self):
        crawler = WalmartTVDetailUpdateCrawler.__new__(WalmartTVDetailUpdateCrawler)
        crawler.test_mode = True; crawler.account_name = 'Walmart'; crawler.batch_id = 'batch'
        crawler.apply_master_model_year = lambda cursor, product: None
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        columns = ', '.join(f'{field} TEXT' for field in crawler.EXTRACTED_FIELDS)
        conn.execute(f'CREATE TABLE test_tv_retail_com (id INTEGER PRIMARY KEY, account_name TEXT, batch_id TEXT, {columns})')
        conn.execute("INSERT INTO test_tv_retail_com (id,account_name,batch_id,item,final_sku_price,count_of_reviews,count_of_star_ratings,star_rating) VALUES (1,'Walmart','batch','123','$100.00','9','9','4.0')")
        crawler.db_conn = SQLiteConnection(conn)
        product = {'id': 1, 'item': '123', 'final_sku_price': None,
                   'count_of_reviews': '0', 'count_of_star_ratings': '0', 'star_rating': 'No ratings yet'}
        self.assertTrue(crawler._save_to_retail_com_once(product))
        result = conn.execute('SELECT final_sku_price, count_of_reviews, count_of_star_ratings, star_rating FROM test_tv_retail_com WHERE id=1').fetchone()
        self.assertEqual(result, ('$100.00', '0', '0', 'No ratings yet'))

if __name__ == '__main__': unittest.main()
