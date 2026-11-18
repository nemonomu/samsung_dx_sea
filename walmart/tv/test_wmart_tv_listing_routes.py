"""Offline checks for listing route order and page-level rank safety."""
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
                crawler.page = Mock(html='')
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


if __name__ == '__main__':
    unittest.main()
