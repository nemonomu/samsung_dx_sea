import ast
import copy
import importlib.util
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('fulfillment', Path(__file__).with_name('step08_fulfillment.py'))
display = importlib.util.module_from_spec(spec)
spec.loader.exec_module(display)
FLAGS = {'enableThreeTileDesign': True, 'enableNetworkStock': True, 'isApplianceSwimLaneEnabled': False}


def parser_functions(category='REF'):
    # Load only pure parsing functions, without crawler/configuration imports.
    path = Path(__file__).with_name('step08_uc_xhr.py')
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    names = {
        '_format_lead_date', '_fulfillment_slot', '_slot_text', '_slot_qty',
        'parse_productdetail',
        'build_row', 'has_body', 'collect_fulfillment_display', 'fetch_sku',
    }
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        'datetime': datetime,
        'json': json,
        'lowes_product_type': lambda: category,
        'purchased_units_phrase': lambda value: value,
        '_category_fallback_ref_type': lambda value: '',
        'ref_capacity_from_description': lambda value: '',
        'api_display': display.api_display,
        'pickup_display': display.pickup_display,
        'service_selection': display.service_selection,
        'needs_service_selection': display.needs_service_selection,
        'VERIFIED_FLAGS': display.VERIFIED_FLAGS,
        'DISPLAY_PROFILE': display.DISPLAY_PROFILE,
        'date_label': display.date_label,
        'promise_date': display.promise_date,
        'fulfillment_slot': display.fulfillment_slot,
        'empty_display': display.empty_display,
        'display_flags': display.display_flags,
        'review_response_labels': lambda responses: [],
        'parse_reviews': lambda *args: {},
        'now_iso': lambda: '2026-09-15T04:25:00',
        'ZIP': '10010', 'STATE': 'NY', 'NEARBY_STORE': '1674', 'STORE_FMT': '0289',
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def delivery_product():
    # Minimal synthetic response reproducing the observed disagreement between
    # the analytics lead days and the appointment displayed on the product page.
    return {
        'additionalServices': False,
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
                'totalQty': 10,
                'itmConsolidationApptDate': '2026-09-16T07:00:00-04:00',
                'itmConsolidationDate': '09-15-2026-04:00 UTC',
            },
        ]}},
    }


def parse_node(node, category='REF'):
    body = json.dumps({'productDetails': {'sample': node}})
    return parser_functions(category)['parse_productdetail']('sample', body, FLAGS)


def add_shipping(node):
    node['itemInventory']['analyticsData']['parcel'] = {
        'fulfillmentType': 'Parcel', 'deliveryMethodName': 'Parcel Shipping',
        'isAvlSts': True, 'availableQuantity': 5,
        'totalQty': 5,
        'isDynamicLeadTime': True,
        'itmLdTmDays': 4, 'itmLdTm': '09-18-2026-04:00 UTC',
    }
    node['location']['itemInventory']['itemAvailList'][0].update({
        **node['itemInventory']['analyticsData']['parcel'],
        'isAvlSts': True, 'displayStatus': True,
    })


class FulfillmentTests(unittest.TestCase):
    def test_appointment_wins_over_analytics_days_and_consolidation_date(self):
        for category in ('REF', 'LDY'):
            with self.subTest(category=category):
                result = parse_node(delivery_product(), category)
                self.assertEqual(result['delivery_availability'], 'Delivery Wed, Sep 16')
                self.assertEqual(result['available_quantity_for_purchase_delivery'], 10)
                self.assertEqual(result['pick_up_availability'], '')  # analytics-only pickup is not a display promise
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

    def test_missing_page_inventory_is_unresolved_without_analytics_guess(self):
        node = delivery_product()
        for location in (None, {}, {'itemInventory': None}, {'itemInventory': {'itemAvailList': 'invalid'}}):
            with self.subTest(location=location):
                node['location'] = location
                self.assertEqual(parse_node(node)['delivery_availability'], '')
                self.assertEqual(display.api_display(node, FLAGS)[1], 'missing_page_inventory')

    def test_unavailable_delivery_is_not_restored_by_installation_fallback(self):
        node = delivery_product()
        node['location']['itemInventory']['itemAvailList'][1]['isAvlSts'] = False
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
                node['product']['majorAppliance'] = False
                add_shipping(node)
                node['location']['itemInventory']['itemAvailList'][1]['isAvlSts'] = False
                result = parse_node(node, category)
                self.assertEqual(result['delivery_availability'], 'Shipping Fri, Sep 18')
                self.assertEqual(result['available_quantity_for_purchase_delivery'], 5)

    def test_unavailable_parcel_is_not_included_even_if_analytics_says_available(self):
        node = delivery_product()
        add_shipping(node)
        node['location']['itemInventory']['itemAvailList'][0]['isAvlSts'] = False
        self.assertEqual(parse_node(node)['delivery_availability'], 'Delivery Wed, Sep 16')

    def test_both_api_methods_require_screen_instead_of_inventing_two_cards(self):
        node = delivery_product()
        add_shipping(node)
        self.assertEqual(
            parse_node(node)['delivery_availability'],
            '',
        )
        self.assertEqual(display.api_display(node, FLAGS)[1], 'missing_delivery_priority')

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

    def test_missing_inventory_does_not_use_unverified_analytics_shipping(self):
        node = delivery_product()
        node['product']['majorAppliance'] = False
        add_shipping(node)
        node['itemInventory']['analyticsData']['truck']['isAvlSts'] = False
        node['location'] = {}
        self.assertEqual(parse_node(node)['delivery_availability'], '')

    def test_parcel_in_major_appliance_layout_is_delivery(self):
        node = delivery_product()
        add_shipping(node)
        node['location']['itemInventory']['itemAvailList'][1]['isAvlSts'] = False
        self.assertEqual(parse_node(node)['delivery_availability'], 'Delivery Fri, Sep 18')

    def test_parcel_promise_wins_over_zero_lead_days_and_ship_date(self):
        node = delivery_product()
        node['product']['majorAppliance'] = False
        add_shipping(node)
        node['location']['itemInventory']['itemAvailList'][1]['isAvlSts'] = False
        node['location']['itemInventory']['itemAvailList'][0].update({
            'itmLdTmDays': 0, 'totalQty': 1236,
            'parcelDates': [{'carrierType': 'STANDARD', 'shipDate': '2026-09-15T17:00:00-04:00',
                             'promiseDate': '2026-09-16T17:00:00-04:00'}],
        })
        result, reason = display.api_display(node, FLAGS, datetime.fromisoformat('2026-09-14T16:00:00+00:00'))
        self.assertEqual(reason, '')
        self.assertEqual(result['delivery_availability'], 'Shipping Wed, Sep 16')
        self.assertEqual(result['available_quantity_for_purchase_delivery'], 1236)

    def test_inventory_list_precedes_older_inventory_and_fast_analytics(self):
        node = delivery_product()
        node['location']['itemInventoryList'] = [{'itemAvailList': [
            {'fulfillmentType': 'Delivery', 'isAvlSts': False},
        ]}]
        node['itemInventory']['analyticsData']['fastTruck'] = {'fulfillmentType': 'FAST_TRUCK', 'isAvlSts': True}
        self.assertEqual(display.api_display(node, FLAGS), (display.empty_display(), ''))

    def test_unknown_flags_are_not_assumed_from_freight_type(self):
        node = delivery_product()
        node['product']['sosFreightType'] = 'Collect'
        self.assertEqual(display.api_display(node)[1], 'unknown_display_flags')
        self.assertEqual(display.display_flags('"enableThreeTileDesign":true,"enableThreeTileDesign":false'), {})

    def test_fast_promise_uses_destination_calendar_across_midnight(self):
        slot = {'fullPath': [{'promiseDate': '2026-09-15T23:59:00-04:00'}]}
        value = display.promise_date(slot)
        before = datetime.fromisoformat('2026-09-15T03:59:00+00:00')
        after = datetime.fromisoformat('2026-09-15T04:24:50+00:00')
        self.assertEqual(display.date_label(value, before, relative=True), 'Tomorrow')
        self.assertEqual(display.date_label(value, after, relative=True), 'Today')
        self.assertEqual(parser_functions()['_slot_text']({'fulfillmentType': 'ExpeditedDelivery', 'isAvlSts': True}), '')

    def test_parcel_today_uses_calendar_date_and_nested_promise(self):
        node = delivery_product()
        add_shipping(node)
        node['product']['majorAppliance'] = False
        slots = node['location']['itemInventory']['itemAvailList']
        slots[1]['isAvlSts'] = False
        slots[0]['fullPath'] = [{'parcelDates': [{'carrierType': 'BASIC', 'promiseDate': '2026-09-15T23:59:00-04:00'}]}]
        result, reason = display.api_display(node, FLAGS, datetime.fromisoformat('2026-09-15T04:24:50+00:00'))
        self.assertEqual(reason, '')
        self.assertEqual(result['delivery_availability'], 'Shipping Tue, Sep 15')


def hisense_cards():
    # Minimal visible fields from the supplied screenshot, not a production HAR.
    return [
        {'title': 'Pickup', 'date': '9am Tomorrow', 'stock': '1 available'},
        {'title': 'Shipping', 'date': 'Wed, Sep 16', 'stock': '1,236 available'},
        {'title': 'Fast Delivery', 'date': 'Tomorrow', 'stock': '878 available'},
    ]


class DisplayIntegrationTests(unittest.TestCase):
    def test_hisense_uses_visible_fast_quantity_not_expedited_api_quantity(self):
        result = display.displayed_fields(hisense_cards())
        self.assertEqual(result, {
            'delivery_availability': 'Shipping Wed, Sep 16',
            'fastest_delivery': 'Get it Tomorrow',
            'available_quantity_for_purchase_delivery': 1236,
            'available_quantity_for_purchase_fastdelivery': 878,
        })

    def test_equator_uses_one_card_and_currently_selected_date(self):
        cards = [{'title': 'Pickup', 'date': 'Ready by Wed, Sep 23'},
                 {'title': 'Delivery', 'date': 'Mon, Sep 21', 'stock': '50 Available'}]
        self.assertEqual(display.displayed_fields(cards)['delivery_availability'], 'Delivery Mon, Sep 21')
        cards[1]['date'] = 'Wed, Sep 23'
        self.assertEqual(display.displayed_fields(cards)['delivery_availability'], 'Delivery Wed, Sep 23')
        self.assertEqual(display.displayed_fields(cards)['available_quantity_for_purchase_delivery'], 50)

    def test_unknown_and_two_normal_cards_do_not_choose_arbitrarily(self):
        self.assertIsNone(display.displayed_fields([{'title': 'Delivery', 'date': ''}]))
        self.assertIsNone(display.displayed_fields([
            {'title': 'Delivery', 'date': 'Tomorrow'}, {'title': 'Shipping', 'date': 'Today'},
        ]))
        self.assertEqual(display.displayed_fields([{'title': 'Delivery', 'date': 'Unavailable'}]), display.empty_display())

    def test_legacy_fast_message_does_not_invent_separate_quantity(self):
        result = display.displayed_fields(
            [{'title': 'Delivery', 'date': 'Tomorrow', 'stock': '50 Available'}], 'Get it Tomorrow',
        )
        self.assertEqual(result['fastest_delivery'], 'Get it Tomorrow')
        self.assertEqual(result['available_quantity_for_purchase_fastdelivery'], '')

    def test_unresolved_screen_overwrites_stale_listing_values(self):
        namespace = parser_functions()
        for status in ('ok', 'unresolved'):
            with self.subTest(status=status):
                evidence = {'status': status, 'source': 'screen', 'flags': FLAGS,
                            'values': display.displayed_fields(hisense_cards())}
                responses = {
                    'productdetail': {'status': 200, 'body': json.dumps({'productDetails': {'sample': delivery_product()}})},
                    'fulfillment_display': {'status': status, 'body': json.dumps(evidence)},
                }
                row = namespace['build_row']({'delivery_availability': 'Delivery old / Shipping old'}, 'sample', responses)
                expected = evidence['values'] if status == 'ok' else display.empty_display()
                self.assertEqual({key: row[key] for key in display.FIELDS}, expected)

    def test_collector_never_opens_or_fetches_product_page(self):
        namespace = parser_functions()
        calls = []
        namespace['run_xhr_get'] = lambda driver, path, **kwargs: calls.append(path) or {'status': 403}
        class NoBrowser:
            def __getattr__(self, name):
                raise AssertionError('Unexpected browser access: ' + name)
        node = delivery_product()
        node['product']['pdURL'] = '/pd/example/sample'
        response = {'status': 200, 'body': json.dumps({'productDetails': {'sample': node}})}
        cache = {}
        collect = namespace['collect_fulfillment_display']
        for store in ('0289', '0289', '1854'):
            result = json.loads(collect(NoBrowser(), 'sample', response, store, cache)['body'])
            self.assertEqual(result['source'], 'api')
            self.assertEqual(result['context']['store'], store)
        self.assertEqual(calls, [])
        self.assertEqual(cache['0289']['screen_checks'], 0)
        self.assertFalse(hasattr(display, 'read_display'))

    def test_service_failure_does_not_stop_remaining_products_or_navigate(self):
        namespace = parser_functions()
        calls = []
        namespace['run_xhr_get'] = lambda driver, path, **kwargs: calls.append(path) or {'status': 403}
        node = delivery_product()
        node['additionalServices'] = True
        response = {'status': 200, 'body': json.dumps({'productDetails': {'sample': node}})}
        result = json.loads(namespace['collect_fulfillment_display'](None, 'sample', response, '0289', {})['body'])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith('/purchase/api/items/sample/additionalServices?storeNumber=1674&'))
        self.assertNotIn('/pd/', calls[0])
        self.assertEqual(result['values']['delivery_availability'], '')
        self.assertIn('services_http_403', result['reason'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['values']['available_quantity_for_purchase_delivery'], 10)

    def test_malformed_product_does_not_inherit_listing_fields(self):
        namespace = parser_functions()
        response = {'status': 200, 'body': 'not json'}
        evidence = namespace['collect_fulfillment_display'](None, 'sample', response, '0289', {})
        responses = {'productdetail': response, 'fulfillment_display': evidence}
        src = dict.fromkeys((*display.FIELDS, 'pick_up_availability', 'available_quantity_for_purchase_pickup'), 'OLD')
        row = namespace['build_row'](src, 'sample', responses)
        self.assertTrue(all(row[k] == '' for k in src))


if __name__ == '__main__':
    unittest.main()
