"""Offline checks for listing route order and page-level rank safety."""
import json
import unittest
import sys
from types import ModuleType
from unittest.mock import Mock, patch

config = ModuleType("config")
config.DB_CONFIG = {}
with patch.dict(sys.modules, {"config": config}):
    from walmart.tv import wmart_tv_main, wmart_tv_bsr


class ListingRouteTests(unittest.TestCase):
    def setUp(self):
        self.cases = ((wmart_tv_main, wmart_tv_main.WalmartTVMainCrawler),
                      (wmart_tv_bsr, wmart_tv_bsr.WalmartTVBSRCrawler))

    def test_opt_in_order_and_zenrows_fallback(self):
        for module, cls in self.cases:
            with self.subTest(cls=cls.__name__):
                crawler = cls.__new__(cls)
                crawler.listing_browser_first = True
                calls = []
                def direct(page, client=None, use_zenrows=True):
                    calls.append('zenrows' if client else 'http')
                    return [{'id': 'ok'}] if client else None
                crawler.crawl_page_direct = direct
                def browser(page):
                    calls.append('browser')
                    raise RuntimeError('failed')
                crawler.crawl_page_browser = browser
                with patch.object(module, 'WalmartNextDataClient') as client:
                    self.assertEqual(crawler.crawl_page(1), [{'id': 'ok'}])
                    client.assert_called_once_with(direct_enabled=False)
                self.assertEqual(calls, ['http', 'browser', 'zenrows'])

    def test_preferred_success_stops_before_later_routes(self):
        for module, cls in self.cases:
            for http in (True, False):
                with self.subTest(cls=cls.__name__, http=http):
                    crawler = cls.__new__(cls)
                    crawler.listing_browser_first = True
                    crawler.crawl_page_direct = Mock(return_value=[{'id': 'ok'}] if http else None)
                    crawler.crawl_page_browser = Mock(return_value=[{'id': 'ok'}])
                    with patch.object(module, 'WalmartNextDataClient') as client:
                        self.assertEqual(crawler.crawl_page(1), [{'id': 'ok'}])
                        client.assert_not_called()
                    crawler.crawl_page_direct.assert_called_once_with(1, use_zenrows=False)
                    self.assertEqual(crawler.crawl_page_browser.call_count, 0 if http else 1)

    def test_http_200_without_products_uses_backup_for_same_listing_page(self):
        item = {'name': 'Test TV', 'canonicalUrl': '/ip/test/123', 'usItemId': '123'}
        data = {'props': {'pageProps': {'initialData': {'searchResult': {
            'itemStacks': [{'title': 'Results for tv', 'items': [item]}],
        }}}}}
        html = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'

        for module, cls in self.cases:
            with self.subTest(cls=cls.__name__):
                crawler = cls.__new__(cls)
                crawler.url_template = 'https://www.walmart.com/search?q=tv&page={page}'
                crawler.account_name = 'Walmart'
                crawler.page_type = 'bsr' if module is wmart_tv_bsr else 'main'
                crawler.calendar_week = 'week'
                crawler.batch_id = 'batch'
                client = module.WalmartNextDataClient()
                client.fetch_direct_html = Mock(return_value=(
                    {'source': 'direct', 'status': 200},
                    '<script id="__NEXT_DATA__">{"props":{}}</script>',
                ))
                client.fetch_zenrows_html = Mock(return_value=(
                    {'source': 'zenrows_static', 'status': 200}, html,
                ))

                rows = crawler.crawl_page_direct(2, client=client)

                self.assertEqual([row['product_url'] for row in rows],
                                 ['https://www.walmart.com/ip/test/123'])
                self.assertEqual(rows[0]['page_number'], 2)
                client.fetch_zenrows_html.assert_called_once_with(
                    client.fetch_direct_html.call_args.args[0], js_render=False,
                )

    def test_integrated_uses_valid_browser_listing_without_trying_other_sources(self):
        item = {'name': 'Test TV', 'canonicalUrl': '/ip/test/123', 'usItemId': '123'}
        data = {'props': {'pageProps': {'initialData': {'searchResult': {
            'itemStacks': [{'title': 'Results for tv', 'items': [item]}],
        }}}}}
        html = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'

        for module, cls in self.cases:
            with self.subTest(cls=cls.__name__):
                crawler = cls.__new__(cls)
                crawler.url_template = 'https://www.walmart.com/search?q=tv&page={page}'
                crawler.account_name = 'Walmart'
                crawler.page_type = 'bsr' if module is wmart_tv_bsr else 'main'
                crawler.calendar_week = 'week'
                crawler.batch_id = 'batch'
                renderer = Mock()
                renderer.fetch_html.return_value = ({'content_valid': True}, html)
                crawler.next_data_client = module.WalmartNextDataClient(
                    direct_enabled=False, renderer=renderer,
                )
                crawler.next_data_client.fetch_direct_html = Mock()
                crawler.next_data_client.fetch_zenrows_html = Mock()
                crawler.crawl_page_browser = Mock()

                rows = crawler.crawl_page(1)

                self.assertEqual(len(rows), 1)
                renderer.fetch_html.assert_called_once()
                crawler.next_data_client.fetch_direct_html.assert_not_called()
                crawler.next_data_client.fetch_zenrows_html.assert_not_called()
                crawler.crawl_page_browser.assert_not_called()

    def test_mismatched_listing_is_rejected_before_parsing(self):
        item = {'name': 'Test TV', 'canonicalUrl': '/ip/test/123', 'usItemId': '123'}
        data = {'query': {'page': 1}, 'props': {'pageProps': {'initialData': {'searchResult': {
            'itemStacks': [{'title': 'Results for tv', 'items': [item]}],
        }}}}}
        html = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'

        for module, cls in self.cases:
            with self.subTest(cls=cls.__name__):
                crawler = cls.__new__(cls)
                crawler.url_template = 'https://www.walmart.com/search?q=tv&page={page}'
                crawler.page_type = 'main'
                client = module.WalmartNextDataClient(direct_enabled=False)
                client.fetch_zenrows_html = Mock(return_value=(
                    {'source': 'zenrows_static', 'status': 200}, html,
                ))
                self.assertIsNone(crawler.crawl_page_direct(2, client=client))

    def test_exhausted_integrated_sources_use_existing_dom_fallback(self):
        for module, cls in self.cases:
            for blocked in (False, True):
                with self.subTest(cls=cls.__name__, blocked=blocked):
                    crawler = cls.__new__(cls)
                    crawler.next_data_client = module.WalmartNextDataClient(renderer=Mock())
                    crawler._listing_browser_blocked = blocked
                    crawler.crawl_page_direct = Mock(return_value=None)
                    crawler.crawl_page_browser = Mock(return_value=[{'id': 'ok'}])
                    self.assertEqual(crawler.crawl_page(2), None if blocked else [{'id': 'ok'}])
                    self.assertEqual(crawler.crawl_page_browser.call_count, int(not blocked))

    def test_later_listing_miss_keeps_saved_rows_available_for_detail_stage(self):
        for module, cls in self.cases:
            with self.subTest(cls=cls.__name__):
                crawler = cls(test_mode=False, batch_id='test')
                crawler.next_data_client = module.WalmartNextDataClient(renderer=Mock())
                crawler.initialize = Mock(return_value=True)
                crawler.crawl_page = Mock(side_effect=[[{'id': 'ok'}], None])
                crawler.save_products = Mock(
                    return_value={'insert': 1, 'update': 0} if module is wmart_tv_bsr else 1,
                )
                with patch.object(module.time, 'sleep'):
                    self.assertTrue(crawler.run())
                self.assertFalse(crawler.stats['quota_met'])
                crawler.save_products.assert_called_once_with([{'id': 'ok'}])

    def test_standalone_listing_uses_and_closes_its_own_renderer(self):
        for module, cls in self.cases:
            with self.subTest(cls=cls.__name__):
                crawler = cls(test_mode=True, batch_id='test')
                crawler.connect_db = Mock(return_value=True)
                crawler.load_xpaths = Mock(return_value=True)
                crawler.load_page_urls = Mock(return_value='https://example.test/search?page={page}')
                crawler.cleanup_old_logs = Mock()
                crawler.build_db_item_cache = Mock(return_value={})
                crawler.save_products = Mock(
                    return_value={'insert': 1, 'update': 0} if module is wmart_tv_bsr else 1,
                )
                crawler.crawl_page_direct = Mock(return_value=[{'id': 'ok'}])
                with patch.object(module, 'WalmartTVBrowserRenderer') as renderer:
                    self.assertTrue(crawler.run())
                self.assertIs(crawler.next_data_client.renderer, renderer.return_value)
                self.assertFalse(crawler.next_data_client.direct_enabled)
                crawler.crawl_page_direct.assert_called_once_with(1)
                renderer.return_value.close.assert_called_once_with()

    def test_default_order_is_unchanged_and_all_routes_failed_raises(self):
        for module, cls in self.cases:
            with self.subTest(cls=cls.__name__):
                crawler = cls.__new__(cls)
                crawler.listing_browser_first = False
                crawler.crawl_page_direct = Mock(return_value=[{'id': 'ok'}])
                crawler.crawl_page_browser = Mock()
                self.assertEqual(crawler.crawl_page(1), [{'id': 'ok'}])
                crawler.crawl_page_browser.assert_not_called()
                crawler.listing_browser_first = True
                crawler.crawl_page_direct = Mock(return_value=None)
                crawler.crawl_page_browser = Mock(return_value=[])
                with patch.object(module, 'WalmartNextDataClient'):
                    with self.assertRaises(RuntimeError):
                        crawler.crawl_page(1)

    def test_bad_card_does_not_hide_failure_or_skip_next_card(self):
        for module, cls in self.cases:
            with self.subTest(cls=cls.__name__):
                crawler = cls.__new__(cls)
                crawler.page = Mock(html='', url='https://example.test/?page=1')
                crawler.xpaths = {'base_container': {'xpath': 'card'}}
                crawler.url_template = 'https://example.test/?page={page}'
                crawler.account_name = 'Walmart'
                crawler.page_type = 'main'
                crawler.calendar_week = 'week'
                crawler.batch_id = 'batch'
                crawler.add_random_mouse_movements = Mock()
                crawler.handle_captcha = Mock()
                crawler.find_product_containers = Mock(return_value=['bad', 'good'])
                with patch.object(module.time, 'sleep'), \
                     patch.object(module, 'parse_listing_card_element', side_effect=[ValueError('bad'), {'product_url': 'ok'}]) as parse:
                    with self.assertRaisesRegex(RuntimeError, 'positions \\[1\\]'):
                        crawler.crawl_page_browser(1)
                    self.assertEqual(parse.call_count, 2)
                    with self.assertRaisesRegex(RuntimeError, 'different listing page'):
                        crawler.crawl_page_browser(2)
                    self.assertEqual(parse.call_count, 2)


if __name__ == '__main__':
    unittest.main()
