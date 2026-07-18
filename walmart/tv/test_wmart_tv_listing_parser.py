import unittest

from lxml import html

from walmart.tv.wmart_tv_next_data import (
    normalize_availability_value,
    parse_listing_card_element,
    parse_listing_products,
    parse_similar_product_names,
    repair_similar_text_encoding,
)


def fulfillment_member(mem_id, *values):
    return {
        'memId': mem_id,
        'content': [
            {'type': 'TEXT', 'value': value}
            for value in values
        ],
    }


def listing_next_data(members, price_info=None):
    return {
        'props': {
            'pageProps': {
                'initialData': {
                    'searchResult': {
                        'itemStacks': [{
                            'title': 'Results for "tv"',
                            'items': [{
                                'usItemId': '100',
                                'name': 'Example TV',
                                'canonicalUrl': '/ip/Example-TV/100',
                                'priceInfo': price_info or {},
                                'badges': {
                                    'groupsV2': [{
                                        'name': 'ProdTileBadgeModule5',
                                        'members': members,
                                    }],
                                },
                            }],
                        }],
                    },
                },
            },
        },
    }


def parse_one(next_data):
    rows = parse_listing_products(
        next_data,
        account_name='Walmart',
        page_type='main',
        page_number=1,
        calendar_week='w29',
        batch_id='w_test',
    )
    return rows[0]


class ListingNextDataParserTests(unittest.TestCase):
    def test_verified_members_return_only_values(self):
        row = parse_one(listing_next_data([
            fulfillment_member('L1052', 'Delivery as soon as', '12 mins'),
            fulfillment_member('L1053', 'Free shipping, arrives', 'tomorrow'),
            fulfillment_member('L1051', 'Free pickup as soon as', '11am'),
        ]))

        self.assertEqual(row['pick_up_availability'], '11am')
        self.assertEqual(row['fastest_delivery'], 'tomorrow')
        self.assertEqual(row['delivery_availability'], '12 mins')

    def test_labels_and_neighboring_members_never_cross_contaminate(self):
        row = parse_one(listing_next_data([
            fulfillment_member('L1051', 'Pickup at a', 'nearby store'),
            fulfillment_member('L1053', 'Free shipping, arrives'),
            fulfillment_member('L1052', 'Delivery as soon as', '21 mins'),
        ]))

        self.assertIsNone(row['pick_up_availability'])
        self.assertIsNone(row['fastest_delivery'])
        self.assertEqual(row['delivery_availability'], '21 mins')

    def test_line_price_is_current_and_was_price_is_original(self):
        row = parse_one(listing_next_data([], {
            'itemPrice': '$78.00',
            'linePrice': '$68.00',
            'linePriceDisplay': 'Now $68.00',
            'wasPrice': '$78.00',
        }))

        self.assertEqual(row['final_sku_price'], '$68.00')
        self.assertEqual(row['original_sku_price'], '$78.00')

    def test_field_normalizer_rejects_labels_and_other_fields(self):
        self.assertEqual(
            normalize_availability_value(
                'Free pickup as soon as tomorrow',
                'pick_up_availability',
            ),
            'tomorrow',
        )
        self.assertIsNone(normalize_availability_value(
            'Free pickup as soon as',
            'pick_up_availability',
        ))
        self.assertIsNone(normalize_availability_value(
            'Free shipping, arrives',
            'delivery_availability',
        ))


class ListingHtmlParserTests(unittest.TestCase):
    def test_card_uses_same_fulfillment_container_and_accessible_price(self):
        card = html.fromstring(
            """
            <div data-item-id="100">
              <a href="/ip/Example-TV/100">
                <h3 data-automation-id="product-title">Example TV</h3>
              </a>
              <div data-automation-id="product-price">
                Now$6800current price Now $68.00, Was $78.00$78.00
              </div>
              <span>
                <span>Delivery as soon as</span><span>12 mins</span>
              </span>
              <span>
                <span>Free shipping, arrives</span><span>tomorrow</span>
              </span>
              <span>
                <span>Free pickup as soon as</span><span>11am</span>
              </span>
            </div>
            """
        )

        row = parse_listing_card_element(card)
        self.assertEqual(row['final_sku_price'], '$68.00')
        self.assertEqual(row['original_sku_price'], '$78.00')
        self.assertEqual(row['pick_up_availability'], '11am')
        self.assertEqual(row['fastest_delivery'], 'tomorrow')
        self.assertEqual(row['delivery_availability'], '12 mins')

    def test_prefix_only_card_values_are_null(self):
        card = html.fromstring(
            """
            <div data-item-id="100">
              <a href="/ip/Example-TV/100">
                <h3 data-automation-id="product-title">Example TV</h3>
              </a>
              <span><span>Free shipping, arrives</span></span>
              <span><span>Free pickup as soon as</span></span>
            </div>
            """
        )

        row = parse_listing_card_element(card)
        self.assertIsNone(row['pick_up_availability'])
        self.assertIsNone(row['fastest_delivery'])
        self.assertIsNone(row['delivery_availability'])


class SimilarEncodingRepairTests(unittest.TestCase):
    def test_repairs_confirmed_right_quote_mojibake(self):
        polluted = 'Sony 55\u00e2\u20ac\u009d class BRAVIA 7'
        self.assertEqual(
            repair_similar_text_encoding(polluted),
            'Sony 55\u201d class BRAVIA 7',
        )

    def test_preserves_unrelated_unicode_exactly(self):
        clean = 'Bang & Olufsen Beovision \u2013 CanvasTV\u2122 65\u201d'
        self.assertEqual(repair_similar_text_encoding(clean), clean)

    def test_repairs_before_similar_name_deduplication(self):
        next_data = {
            'props': {
                'pageProps': {
                    'initialData': {
                        'contentLayout': {
                            'modules': [{
                                'type': 'ItemCarousel',
                                'configs': {
                                    'title': 'Similar items you might like',
                                    'products': [
                                        {
                                            'usItemId': '200',
                                            'name': 'Sony 55\u00e2\u20ac\u009d class BRAVIA 7',
                                        },
                                        {
                                            'usItemId': '200',
                                            'name': 'Sony 55\u201d class BRAVIA 7',
                                        },
                                    ],
                                },
                            }],
                        },
                    },
                },
            },
        }

        self.assertEqual(
            parse_similar_product_names(next_data, current_item='100'),
            'Sony 55\u201d class BRAVIA 7',
        )


if __name__ == '__main__':
    unittest.main()
