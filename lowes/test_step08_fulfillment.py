import ast
import copy
import json
import unittest
from datetime import datetime
from pathlib import Path


def parser_functions(category='REF'):
    # Load only pure parsing functions, without crawler/configuration imports.
    path = Path(__file__).with_name('step08_uc_xhr.py')
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    names = {
        '_format_lead_date', '_fulfillment_slot', '_slot_text', '_slot_qty',
        'parse_productdetail',
    }
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        'datetime': datetime,
        'json': json,
        'lowes_product_type': lambda: category,
        'purchased_units_phrase': lambda value: value,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def delivery_product():
    # Minimal synthetic response reproducing the observed disagreement between
    # the analytics lead days and the appointment displayed on the product page.
    return {
        'product': {
            'majorAppliance': True,
            'specs': [
                {'key': 'Appliance Type', 'value': 'test appliance'},
                {'key': 'Overall Capacity', 'value': '18'},
            ],
        },
        'itemInventory': {'analyticsData': {
            'truck': {
                'fulfillmentType': 'Delivery', 'isAvlSts': True,
                'itmLdTmDays': 1, 'itmLdTm': '09-14-2026-04:00 UTC',
                'availableQuantity': 10,
            },
            'parcel': {'fulfillmentType': 'Parcel', 'isAvlSts': False},
            'pickup': {'fulfillmentType': 'Pickup', 'isAvlSts': True, 'itmLdTmDays': 0},
            'expeditedDelivery': {'fulfillmentType': 'ExpeditedDelivery', 'isAvlSts': False},
        }},
        'location': {'itemInventory': {'itemAvailList': [
            {'fulfillmentType': 'Parcel', 'isAvlSts': False, 'displayStatus': False},
            {
                'fulfillmentType': 'Delivery', 'isAvlSts': True, 'displayStatus': True,
                'itmConsolidationApptDate': '2026-09-16T07:00:00-04:00',
                'itmConsolidationDate': '09-15-2026-04:00 UTC',
            },
        ]}},
    }


def parse_node(node, category='REF'):
    body = json.dumps({'productDetails': {'sample': node}})
    return parser_functions(category)['parse_productdetail']('sample', body)


def add_shipping(node):
    node['itemInventory']['analyticsData']['parcel'] = {
        'fulfillmentType': 'Parcel', 'deliveryMethodName': 'Parcel Shipping',
        'isAvlSts': True, 'availableQuantity': 5,
        'itmLdTmDays': 4, 'itmLdTm': '09-18-2026-04:00 UTC',
    }
    node['location']['itemInventory']['itemAvailList'][0].update({
        'isAvlSts': True, 'displayStatus': True,
    })


class FulfillmentTests(unittest.TestCase):
    def test_appointment_wins_over_analytics_days_and_consolidation_date(self):
        for category in ('REF', 'LDY'):
            with self.subTest(category=category):
                result = parse_node(delivery_product(), category)
                self.assertEqual(result['delivery_availability'], 'Delivery Wed, Sep 16')
                self.assertEqual(result['available_quantity_for_purchase_delivery'], 10)
                self.assertEqual(result['pick_up_availability'], 'Pickup Ready Today')
                self.assertEqual(result['fastest_delivery'], '')

    def test_appointment_uses_local_calendar_date(self):
        slot = {
            'fulfillmentType': 'Delivery', 'isAvlSts': True,
            'itmConsolidationApptDate': '2026-09-16T23:30:00-07:00',
        }
        self.assertEqual(parser_functions()['_slot_text'](slot), 'Delivery Wed, Sep 16')

    def test_without_appointment_preserves_lead_time_fallback(self):
        formatter = parser_functions()['_slot_text']
        for days, date, expected in (
            (0, None, 'Delivery Today'),
            (1, None, 'Delivery Tomorrow'),
            (2, '09-16-2026-04:00 UTC', 'Delivery Wed, Sep 16'),
        ):
            with self.subTest(days=days):
                slot = {
                    'fulfillmentType': 'Delivery', 'isAvlSts': True,
                    'itmLdTmDays': days, 'itmLdTm': date,
                    'itmConsolidationApptDate': 'invalid',
                }
                self.assertEqual(formatter(slot), expected)

    def test_missing_page_inventory_keeps_analytics_fallback(self):
        node = delivery_product()
        for location in (None, {}, {'itemInventory': None}, {'itemInventory': {'itemAvailList': 'invalid'}}):
            with self.subTest(location=location):
                node['location'] = location
                self.assertEqual(parse_node(node)['delivery_availability'], 'Delivery Tomorrow')

    def test_hidden_delivery_is_not_restored_by_installation_fallback(self):
        node = delivery_product()
        node['location']['itemInventory']['itemAvailList'][1]['displayStatus'] = False
        result = parse_node(node)
        self.assertEqual(result['delivery_availability'], '')
        self.assertEqual(result['available_quantity_for_purchase_delivery'], '')

    def test_page_unavailability_overrides_stale_analytics_availability(self):
        node = delivery_product()
        node['location']['itemInventory']['itemAvailList'][1]['isAvlSts'] = False
        self.assertEqual(parse_node(node)['delivery_availability'], '')

    def test_analytics_fields_are_not_mutated_by_page_overlay(self):
        node = delivery_product()
        original = copy.deepcopy(node)
        analytics = node['itemInventory']['analyticsData']
        result = parser_functions()['_fulfillment_slot'](node, analytics, 'truck', 'Delivery')
        self.assertEqual(result['itmConsolidationApptDate'], '2026-09-16T07:00:00-04:00')
        self.assertEqual(node, original)

    def test_pickup_and_fast_delivery_text_are_preserved(self):
        formatter = parser_functions()['_slot_text']
        for kind, days, expected in (
            ('Pickup', 0, 'Pickup Ready Today'),
            ('Pickup', 1, 'Pickup Ready Tomorrow'),
            ('ExpeditedDelivery', 0, 'Get it Today'),
            ('FastTruck', 1, 'Get it Tomorrow'),
        ):
            with self.subTest(kind=kind, days=days):
                self.assertEqual(formatter({
                    'fulfillmentType': kind, 'isAvlSts': True, 'itmLdTmDays': days,
                }), expected)

    def test_parcel_only_uses_shipping_label_date_and_quantity(self):
        for category in ('REF', 'LDY'):
            with self.subTest(category=category):
                node = delivery_product()
                add_shipping(node)
                node['location']['itemInventory']['itemAvailList'][1]['isAvlSts'] = False
                result = parse_node(node, category)
                self.assertEqual(result['delivery_availability'], 'Shipping Fri, Sep 18')
                self.assertEqual(result['available_quantity_for_purchase_delivery'], 5)

    def test_hidden_parcel_is_not_included_even_if_analytics_says_available(self):
        node = delivery_product()
        add_shipping(node)
        node['location']['itemInventory']['itemAvailList'][0]['displayStatus'] = False
        self.assertEqual(parse_node(node)['delivery_availability'], 'Delivery Wed, Sep 16')

    def test_both_displayed_methods_keep_their_own_labels_and_dates(self):
        node = delivery_product()
        add_shipping(node)
        self.assertEqual(
            parse_node(node)['delivery_availability'],
            'Delivery Wed, Sep 16 / Shipping Fri, Sep 18',
        )

    def test_parcel_relative_dates_do_not_use_delivery_label(self):
        formatter = parser_functions()['_slot_text']
        for days, expected in ((0, 'Shipping Today'), (1, 'Shipping Tomorrow')):
            with self.subTest(days=days):
                self.assertEqual(formatter({
                    'fulfillmentType': 'Parcel', 'isAvlSts': True, 'itmLdTmDays': days,
                }), expected)

    def test_parcel_does_not_use_a_truck_appointment(self):
        slot = {
            'fulfillmentType': 'Parcel', 'isAvlSts': True, 'itmLdTmDays': 4,
            'itmLdTm': '09-18-2026-04:00 UTC',
            'itmConsolidationApptDate': '2026-09-16T07:00:00-04:00',
        }
        self.assertEqual(parser_functions()['_slot_text'](slot), 'Shipping Fri, Sep 18')

    def test_shipping_can_fall_back_to_analytics_when_page_inventory_is_missing(self):
        node = delivery_product()
        add_shipping(node)
        node['itemInventory']['analyticsData']['truck']['isAvlSts'] = False
        node['location'] = {}
        self.assertEqual(parse_node(node)['delivery_availability'], 'Shipping Fri, Sep 18')


if __name__ == '__main__':
    unittest.main()
