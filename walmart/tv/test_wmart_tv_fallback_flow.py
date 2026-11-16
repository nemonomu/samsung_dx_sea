import json
import sys
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from walmart.tv.wmart_tv_dt import WalmartTVDetailCrawler
from walmart.tv.wmart_tv_dt_update import WalmartTVDetailUpdateCrawler
from walmart.tv.wmart_tv_next_data import WalmartNextDataClient


class RendererLifecycleTests(unittest.TestCase):
    def test_concurrent_workers_share_one_renderer_and_recovery_has_none(self):
        crawler = WalmartTVDetailCrawler.__new__(WalmartTVDetailCrawler)
        renderer = Mock()
        factory = Mock(return_value=renderer)
        module = types.ModuleType('walmart.tv.wmart_tv_render')
        module.WalmartTVBrowserRenderer = factory
        clients = []
        def make_client(**kwargs):
            clients.append(kwargs)
            return object()
        crawler.crawl_detail_next_data = Mock(return_value={'item': '100'})
        with patch.dict(sys.modules, {'walmart.tv.wmart_tv_render': module}), \
             patch('walmart.tv.wmart_tv_dt.WalmartNextDataClient', side_effect=make_client):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda i: crawler._crawl_detail_next_data_worker(i, {}, {}), range(4)))
            crawler._crawl_detail_next_data_worker(5, {}, {}, zenrows_only=True)
        self.assertTrue(all(result[1] for result in results))
        factory.assert_called_once_with()
        self.assertTrue(all(client['renderer'] is renderer and client['direct_enabled'] for client in clients[:4]))
        self.assertEqual(clients[4], {'direct_enabled': False, 'renderer': None})

    def test_close_error_does_not_prevent_main_or_update_cleanup(self):
        for crawler_type in (WalmartTVDetailCrawler, WalmartTVDetailUpdateCrawler):
            with self.subTest(crawler_type=crawler_type):
                crawler = crawler_type.__new__(crawler_type)
                renderer = Mock()
                renderer.close.side_effect = RuntimeError('close failed')
                crawler._browser_renderer = renderer
                crawler.db_conn = Mock()
                crawler.initialize = Mock(return_value=False)
                crawler.start_logging = Mock(return_value=None)
                crawler.stop_logging = Mock()
                crawler.batch_id = 'test'
                self.assertFalse(crawler.run())
                renderer.close.assert_called_once_with()
                crawler.db_conn.close.assert_called_once_with()
                self.assertIsNone(crawler._browser_renderer)
                if crawler_type is WalmartTVDetailUpdateCrawler:
                    crawler.stop_logging.assert_called_once_with()

    def test_no_renderer_created_for_zenrows_only_worker(self):
        crawler = WalmartTVDetailCrawler.__new__(WalmartTVDetailCrawler)
        crawler.crawl_detail_next_data = Mock(return_value={'item': '100'})
        with patch.object(crawler, '_get_browser_renderer') as get_renderer, \
             patch('walmart.tv.wmart_tv_dt.WalmartNextDataClient'):
            crawler._crawl_detail_next_data_worker(1, {}, {}, zenrows_only=True)
        get_renderer.assert_not_called()

    def test_renderer_initialization_error_keeps_other_routes_available(self):
        crawler = WalmartTVDetailCrawler.__new__(WalmartTVDetailCrawler)
        crawler.crawl_detail_next_data = Mock(return_value={'item': '100'})
        module = types.ModuleType('walmart.tv.wmart_tv_render')
        module.WalmartTVBrowserRenderer = Mock(side_effect=RuntimeError('init failed'))
        with patch.dict(sys.modules, {'walmart.tv.wmart_tv_render': module}), \
             patch('walmart.tv.wmart_tv_dt.WalmartNextDataClient') as client:
            result = crawler._crawl_detail_next_data_worker(1, {}, {})
        self.assertEqual(result[1], {'item': '100'})
        client.assert_called_once_with(direct_enabled=True, renderer=None)


class DetailIdentityFallbackTests(unittest.TestCase):
    @staticmethod
    def html(item, name):
        data = {'props': {'pageProps': {'initialData': {
            'product': {'usItemId': item, 'name': name,
                        'priceInfo': {'currentPrice': {'price': 199}}},
            'reviews': {'reviewsWithTextCount': 0, 'totalReviewCount': 0},
        }}}}
        return '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'

    def setUp(self):
        self.crawler = WalmartTVDetailCrawler.__new__(WalmartTVDetailCrawler)
        self.crawler.extract_sku = Mock(return_value=(None, None, None, None, None))
        self.crawler.extract_screen_size = Mock(return_value=(None, None, None, None))
        self.crawler._fill_similar_from_json_response = Mock()
        self.product = {'product_url': 'https://www.walmart.com/ip/100',
                        'retailer_sku_name': 'Example Television 55 inch'}
        self.renderer = Mock()
        self.client = WalmartNextDataClient(renderer=self.renderer)
        self.client.fetch_zenrows_html = Mock(side_effect=AssertionError('unexpected ZenRows'))

    def crawl(self):
        return self.crawler.crawl_detail_next_data(
            self.product, next_data_client=self.client, mst_specs={},
            record_errors=False, collect_spec_diff=False, log=False,
        )

    def test_unrelated_direct_redirect_uses_rendered_page(self):
        self.client.fetch_direct_html = Mock(return_value=(
            {'source': 'direct', 'status': 200}, self.html('999', 'Unrelated television 22 inch')))
        self.renderer.fetch_html.return_value = (
            {'source': 'browser_render', 'content_valid': True,
             'location_verified': True, 'location_zip': '11581'},
            self.html('100', self.product['retailer_sku_name']))
        row = self.crawl()
        self.assertEqual(row['item'], '100')
        self.assertEqual(row['_detail_source'], 'browser_render')
        self.renderer.fetch_html.assert_called_once()
        self.client.fetch_zenrows_html.assert_not_called()

    def test_matching_name_redirect_keeps_direct_response(self):
        self.client.fetch_direct_html = Mock(return_value=(
            {'source': 'direct', 'status': 200}, self.html('999', self.product['retailer_sku_name'])))
        row = self.crawl()
        self.assertEqual(row['item'], '999')
        self.assertEqual(row['_detail_source'], 'direct')
        self.renderer.fetch_html.assert_not_called()
        self.client.fetch_zenrows_html.assert_not_called()


if __name__ == '__main__':
    unittest.main()
