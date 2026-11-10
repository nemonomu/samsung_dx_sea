import unittest
from unittest.mock import Mock, patch

from lxml import html

from amazon.tv.amazon_tv_cart_price import CartPriceParseError, extract_selected_offer_price


def page(active_price='$99.99', asin='B0BZTB81QV', extra=''):
    return html.fromstring(f'''<div id="buyBoxAccordion">
      <div class="a-box a-accordion-active">
        <div id="apex_offerDisplay_desktop"><div id="corePrice_feature_div"><div class="apex-core-price-identifier">
          <span class="a-offscreen">{active_price}</span>
        </div></div></div>
        <form id="addToCart" action="/gp/product/handle-buy-box">
          <input name="items[0.base][asin]" value="{asin}">
        </form>{extra}
      </div>
      <div class="a-box"><div id="corePrice_feature_div">
        <span class="a-offscreen">$89.99</span>
      </div><form id="addToCart" action="/gp/product/handle-buy-box">
        <input name="items[0.base][asin]" value="B0BZTB81QV">
      </form></div>
    </div>''')


class SelectedOfferPriceTests(unittest.TestCase):
    def test_selects_active_offer_not_other_offer(self):
        self.assertEqual(extract_selected_offer_price(page(), 'B0BZTB81QV'), '$99.99')

    def test_ignores_empty_price_before_selected_offer(self):
        tree = page()
        tree.insert(0, html.fromstring('''<div id="apex_offerDisplay_desktop">
          <div id="corePrice_feature_div"><div class="apex-core-price-identifier">
            <span class="a-offscreen"> </span></div></div></div>'''))
        matches = tree.xpath("//div[@id='apex_offerDisplay_desktop']//div[@id='corePrice_feature_div']//div[contains(@class, 'apex-core-price-identifier')]//span[contains(@class, 'a-offscreen')]/text()")
        self.assertEqual([value.strip() for value in matches], ['', '$99.99'])
        self.assertEqual(extract_selected_offer_price(tree, 'B0BZTB81QV'), '$99.99')

    def test_rejects_other_asin_or_missing_form(self):
        self.assertIsNone(extract_selected_offer_price(page(asin='B000000000'), 'B0BZTB81QV'))
        tree = page()
        tree.xpath('//form')[0].drop_tree()
        self.assertIsNone(extract_selected_offer_price(tree, 'B0BZTB81QV'))

    def test_rejects_multiple_active_rows_or_prices(self):
        tree = page(extra='<div id="corePrice_feature_div"><div class="apex-core-price-identifier"><span class="a-offscreen">$1.00</span></div></div>')
        self.assertIsNone(extract_selected_offer_price(tree, 'B0BZTB81QV'))
        tree = page()
        tree.xpath('//*[@class="a-box"]')[0].set('class', 'a-box a-accordion-active')
        self.assertIsNone(extract_selected_offer_price(tree, 'B0BZTB81QV'))

    def test_rejects_duplicate_forms_in_active_row(self):
        tree = page(extra='''<form id="addToCart" action="/gp/product/handle-buy-box">
          <input name="items[0.base][asin]" value="B0BZTB81QV"></form>''')
        self.assertIsNone(extract_selected_offer_price(tree, 'B0BZTB81QV'))

    def test_rejects_invalid_or_missing_price(self):
        self.assertIsNone(extract_selected_offer_price(page(active_price='Currently unavailable.'), 'B0BZTB81QV'))
        self.assertIsNone(extract_selected_offer_price(page(active_price=''), 'B0BZTB81QV'))

    def test_reads_new_price_when_price_and_form_are_outside_the_active_row(self):
        # Minimized from the Roku PDP snapshot captured during mode 6 recovery.
        tree = html.fromstring('''<main>
          <div id="buyBoxAccordion">
            <div id="newAccordionRow_0" class="buying-option a-accordion-active"></div>
            <div id="usedAccordionRow" class="buying-option"></div>
          </div>
          <div id="apex_desktop">
            <div id="apex_desktop_newAccordionRow">
              <div id="corePriceDisplay_desktop_feature_div">
                <span class="a-price priceToPay"><span aria-hidden="true">$149.99</span></span>
              </div>
            </div>
            <div id="apex_desktop_usedAccordionRow" style="display:none">
              <div id="corePriceDisplay_desktop_feature_div">
                <span class="a-price priceToPay"><span aria-hidden="true">$142.49</span></span>
              </div>
            </div>
          </div>
          <div id="offerActions_desktop_newAccordionRow">
            <form id="addToCart" action="/gp/product/handle-buy-box">
              <input name="items[0.base][asin]" value="B0FV57TPWT">
              <input name="items[0.base][customerVisiblePrice][displayString]" value="$149.99">
            </form>
          </div>
          <div id="offerActions_desktop_usedAccordionRow">
            <form id="addToCart" action="/gp/product/handle-buy-box">
              <input name="items[0.base][asin]" value="B0FV57TPWT">
              <input name="items[0.base][customerVisiblePrice][displayString]" value="$142.49">
            </form>
          </div>
        </main>''')
        asin = 'B0FV57TPWT'
        self.assertEqual(extract_selected_offer_price(tree, asin), '$149.99')
        self.assertIsNone(extract_selected_offer_price(tree, 'B0BZTB81QV'))
        new_area = tree.xpath("//*[@id='apex_desktop_newAccordionRow']")[0]
        new_area.set('style', 'display: none;')
        self.assertIsNone(extract_selected_offer_price(tree, asin))
        new_area.attrib.pop('style')
        new_area.xpath(".//span[@aria-hidden='true']")[0].text = '$142.49'
        self.assertIsNone(extract_selected_offer_price(tree, asin))

    def test_detail_uses_selected_offer_only_after_existing_price_paths(self):
        from amazon.tv import amazon_tv_dt

        cases = (
            ('$119.99', '$119.99', False, '$119.99'),
            (None, '$89.99', False, '$89.99'),
            ('Currently unavailable.', 'Currently unavailable.', False, 'Currently unavailable.'),
            (None, None, False, '$99.99'),
            (None, None, True, '$99.99'),
        )
        asin = 'B0BZTB81QV'
        for xpath_price, cart_price, cart_rejected, expected in cases:
            with self.subTest(xpath_price=xpath_price, cart_price=cart_price, cart_rejected=cart_rejected):
                crawler = Mock(xpaths={})
                crawler.page.html = html.tostring(page(), encoding='unicode')
                crawler.page.run_js.return_value = crawler.page.html
                crawler.should_take_capture.return_value = False
                crawler.extract_final_sku_price.return_value = xpath_price
                crawler.get_tv_specs_from_mst.return_value = (None, None, None)
                crawler.extract_sku.return_value = (None, None, None)
                crawler.extract_screen_size.return_value = (None, None, None)
                crawler.extract_reviews_with_retry.return_value = (None, 0, False)
                if cart_rejected:
                    crawler.resolve_hidden_price_from_cart.side_effect = CartPriceParseError('ambiguous offer')
                else:
                    crawler.resolve_hidden_price_from_cart.side_effect = (
                        lambda tree, *_: (cart_price, False, None, tree, None)
                    )
                product = {'item': asin, 'product_url': f'https://www.amazon.com/dp/{asin}'}
                with patch.object(amazon_tv_dt.time, 'sleep'), patch('builtins.print'):
                    result = amazon_tv_dt.AmazonTVDetailCrawler.crawl_detail(crawler, product)
                self.assertEqual(crawler.resolve_hidden_price_from_cart.call_args.args[-1], xpath_price)
                self.assertEqual(result['final_sku_price'], expected)


if __name__ == '__main__':
    unittest.main()
