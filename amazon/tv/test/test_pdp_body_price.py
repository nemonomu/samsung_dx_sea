import unittest
from unittest.mock import Mock, patch

from amazon.tv import amazon_tv_dt
from amazon.tv.amazon_tv_cart_price import CartPriceParseError


class PdpBodyPriceTests(unittest.TestCase):
    def crawl(self, xpath_price, body_prices, cart_price):
        asin = 'B0FV57TPWT'
        url = f'https://www.amazon.com/dp/{asin}'
        crawler = Mock(xpaths={})
        crawler.page.url = url
        crawler.page.html = '<main><input id="ASIN" name="ASIN" value="B0FV57TPWT"></main>'
        crawler.page.run_js.side_effect = lambda script, *args: (
            crawler.page.html if 'outerHTML' in script else body_prices
        )
        crawler.should_take_capture.return_value = False
        crawler.extract_final_sku_price.return_value = xpath_price
        crawler.get_tv_specs_from_mst.return_value = (None, None, None)
        crawler.extract_sku.return_value = (None, None, None)
        crawler.extract_screen_size.return_value = (None, None, None)
        crawler.extract_reviews_with_retry.return_value = (None, 0, False)
        crawler.resolve_hidden_price_from_cart.side_effect = (
            cart_price if isinstance(cart_price, Exception)
            else lambda tree, *_: (cart_price, False, None, tree, None)
        )
        with patch.object(amazon_tv_dt.time, 'sleep'), patch('builtins.print'):
            result = amazon_tv_dt.AmazonTVDetailCrawler.crawl_detail(
                crawler, {'item': asin, 'product_url': url}
            )
        return result['final_sku_price'], crawler.resolve_hidden_price_from_cart

    def test_visible_body_price_does_not_need_a_buying_option_or_cart(self):
        price, cart = self.crawl(None, ['$ 149.99', '$149.99'], CartPriceParseError('ambiguous offer'))
        self.assertEqual(price, '$149.99')
        cart.assert_not_called()

    def test_keeps_existing_xpath_price_and_explicit_unavailable_status(self):
        price, cart = self.crawl('$119.99', ['$149.99'], None)
        self.assertEqual(price, '$119.99')
        cart.assert_not_called()
        unavailable = 'Currently unavailable.'
        price, cart = self.crawl(unavailable, ['$149.99'], unavailable)
        self.assertEqual(price, unavailable)
        self.assertEqual(cart.call_args.args[-1], unavailable)

    def test_no_visible_price_keeps_exact_cart_resolution(self):
        price, cart = self.crawl(None, [], '$89.99')
        self.assertEqual(price, '$89.99')
        self.assertIsNone(cart.call_args.args[-1])

    def test_ambiguous_or_malformed_body_price_is_never_saved(self):
        for prices in (['$149.99', '$142.49'], ['Currently unavailable.']):
            with self.subTest(prices=prices):
                price, _ = self.crawl(None, prices, CartPriceParseError('ambiguous offer'))
                self.assertIsNone(price)
