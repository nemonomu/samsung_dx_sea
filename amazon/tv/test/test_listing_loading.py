import contextlib
import io
import json
import re
import unittest
from unittest.mock import Mock, patch

from lxml import html

from amazon.tv.amazon_tv_listing import AmazonTVListingMixin


def cards(start, stop):
    return '<html><body>' + ''.join(
        f'<div class="card" data-asin="B{i:09d}"><a class="product" href="/dp/B{i:09d}">'
        f'TV {i}</a><span class="zg-bdg-text">#{i}</span></div>'
        for i in range(start, stop)
    ) + '</body></html>'


class Clock:
    now = 0

    def sleep(self, seconds):
        self.now += seconds


class Page:
    url = 'https://www.amazon.com/test-bsr'

    def __init__(self, document):
        self.document = document
        self.scrolls = []
        self.refreshes = 0

    @property
    def html(self):
        return self.document(self)

    def refresh(self):
        self.refreshes += 1

    def run_js(self, script, *args):
        if 'window.scrollTo' in script:
            self.scrolls.append(args[0])
            return True
        return {'scroll_y': 900, 'document_height': 2000, 'viewport_height': 500}


class Listing(AmazonTVListingMixin):
    page_type = 'bsr'
    batch_id = 'test'
    xpaths = {'base_container': {'xpath': '//div[@class="card"]'}}

    def __init__(self, document):
        self.page = Page(document)
        self._listing_snapshot = Mock()
        self.recover_amazon_pages = Mock(return_value=True)

    def safe_extract_chain(self, node, field):
        values = node.xpath('.//a[@class="product"]/@href' if field == 'product_url' else './/a/text()')
        return values[0] if values else None

    def get_chain_xpaths(self, field):
        return [(field, './/a[@class="product"]/@href')]

    def extract_item(self, url):
        match = re.search(r'/dp/(\w{10})', url or '')
        return match[1] if match else None


class ListingLoadingTests(unittest.TestCase):
    def collect(self, crawler):
        clock = Clock()
        output = io.StringIO()
        with patch('amazon.tv.amazon_tv_listing.time.monotonic', side_effect=lambda: clock.now), \
                patch('amazon.tv.amazon_tv_listing.time.sleep', side_effect=clock.sleep), \
                contextlib.redirect_stdout(output):
            result = crawler.find_bsr_product_containers('//div[@class="card"]', 1)
        return result, clock.now, output.getvalue()

    def test_fifty_cards_return_without_wait_or_refresh(self):
        crawler = Listing(lambda page: cards(1, 51))
        result, elapsed, _ = self.collect(crawler)
        self.assertEqual((len(result), elapsed, crawler.page.refreshes), (50, 0, 0))
        self.assertEqual(crawler.page.scrolls, [])

    def test_lazy_load_stops_as_soon_as_fifty_appear(self):
        crawler = Listing(lambda page: cards(1, 51 if len(page.scrolls) >= 3 else 31))
        result, elapsed, _ = self.collect(crawler)
        self.assertEqual((len(result), elapsed, crawler.page.refreshes), (50, 6, 0))
        self.assertEqual(crawler.page.scrolls, [.90, .95, .98])

    def test_two_full_budgets_then_partial_result(self):
        crawler = Listing(lambda page: cards(1, 31))
        result, elapsed, output = self.collect(crawler)
        self.assertEqual((len(result), elapsed, crawler.page.refreshes), (30, 240, 1))
        self.assertEqual(crawler._listing_snapshot.call_count, 2)
        self.assertIn('shortfall=20', output)
        self.assertTrue(all(.9 <= ratio <= .98 for ratio in crawler.page.scrolls))
        diagnostic = json.loads(next(line.split('] ', 1)[1] for line in output.splitlines() if line.startswith('[BSR LOAD] {')))
        self.assertEqual(len(diagnostic['dom_candidate_asins']), 30)
        self.assertEqual(len(diagnostic['dom_rank_badges']), 30)

    def test_refresh_keeps_previous_cards_and_deduplicates_by_asin(self):
        crawler = Listing(lambda page: cards(1, 41) if not page.refreshes else cards(21, 51))
        result, elapsed, _ = self.collect(crawler)
        asins = [node.get('data-asin') for node in result]
        self.assertEqual((len(set(asins)), elapsed, crawler.page.refreshes), (50, 120, 1))
        self.assertEqual(len(asins), 50)

    def test_smaller_refreshed_page_keeps_all_previous_cards(self):
        crawler = Listing(lambda page: cards(1, 41) if not page.refreshes else cards(1, 21))
        result, elapsed, _ = self.collect(crawler)
        self.assertEqual((len(result), elapsed, crawler.page.refreshes), (40, 240, 1))

    def test_failed_refresh_returns_already_captured_cards(self):
        crawler = Listing(lambda page: cards(1, 31))
        crawler.page.refresh = Mock(side_effect=RuntimeError('reload failed'))
        result, elapsed, output = self.collect(crawler)
        self.assertEqual((len(result), elapsed), (30, 120))
        crawler.page.refresh.assert_called_once()
        self.assertIn('refresh failed', output)

    def test_url_failure_logs_candidates_but_never_uses_them(self):
        crawler = Listing(lambda page: cards(1, 2))
        for link in ('<a class="product" href="#">TV</a>', '<a>TV</a>'):
            with self.subTest(link=link):
                node = html.fromstring('<div data-asin="B000000001">' + link +
                                       '<a href="/dp/B000000001">Image</a></div>')
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertIsNone(crawler.extract_listing_product_url(node, 16, 4))
                self.assertIn('B000000001', out.getvalue())
                self.assertIn('log_only_skip_collection', out.getvalue())
                saved_html = crawler._listing_snapshot.call_args.args[1]
                self.assertIn('/dp/B000000001', saved_html)

    def test_snapshot_failure_does_not_enable_url_collection(self):
        crawler = Listing(lambda page: cards(1, 2))
        crawler._listing_snapshot.side_effect = OSError('disk unavailable')
        with patch('builtins.print'):
            self.assertIsNone(crawler.extract_listing_product_url(html.fromstring('<div><a href="/dp/B000000001">TV</a></div>'), 1, 1))

    def test_missing_url_is_never_sent_to_database_in_main_or_bsr(self):
        from amazon.tv.amazon_tv_main import AmazonTVMainCrawler
        from amazon.tv.amazon_tv_bsr import AmazonTVBSRCrawler
        for cls, expected in ((AmazonTVMainCrawler, 0), (AmazonTVBSRCrawler, {'insert': 0, 'update': 0})):
            with self.subTest(crawler=cls.__name__):
                crawler = Mock()
                crawler.stats = dict.fromkeys(['collected', 'duplicates', 'url_missing', 'non_product'], 0)
                with patch('builtins.print'):
                    result = cls.save_products(crawler, [{'page_number': 1, 'product_url': None, 'retailer_sku_name': 'TV'}])
                self.assertEqual(result, expected)
                self.assertEqual(crawler.stats['url_missing'], 1)
                crawler.db_conn.cursor.assert_not_called()

    def test_empty_page_does_not_restart_four_minute_loading_again(self):
        from amazon.tv.amazon_tv_bsr import AmazonTVBSRCrawler
        crawler = Mock(test_mode=False, max_products=100, max_pages=2, batch_id='test')
        crawler.initialize.return_value = True
        crawler.crawl_page.side_effect = [[], [{'product_url': 'https://www.amazon.com/dp/B000000001'}]]
        crawler.save_products.return_value = {'insert': 1, 'update': 0}
        crawler.stats = dict.fromkeys(['collected', 'duplicates', 'url_missing', 'updated', 'inserted', 'skipped_by_target'], 0)
        with patch('amazon.tv.amazon_tv_bsr.time.sleep'), patch('builtins.print'):
            self.assertFalse(AmazonTVBSRCrawler.run(crawler))
        self.assertEqual([call.args[0] for call in crawler.crawl_page.call_args_list], [1, 2])
        crawler.restart_browser.assert_not_called()
        crawler.save_products.assert_called_once()
