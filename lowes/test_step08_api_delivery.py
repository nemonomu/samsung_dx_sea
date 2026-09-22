"""Offline regressions for the 2026-09-22 screen/API comparison.

Synthetic minimal inputs, preserving only the observed display disagreements.
No production response, browser, retailer request or database is used here.
"""
import ast
import copy
import json
import unittest
from datetime import datetime
from pathlib import Path

from lowes.test_step08_fulfillment import display, parser_functions


NOW = datetime.fromisoformat('2026-09-22T02:04:39+00:00')
FLAGS = display.VERIFIED_FLAGS
STORE = {'timeZone': 'America/Anchorage', 'storeHours': [
    {'day': {'day': 'Tuesday', 'open': '06.00.00', 'close': '22.00.00'}},
]}
NO_SERVICES = {'rtf': False, 'eligible_methods': None}
RTF = {'rtf': True, 'eligible_methods': ['delivery', 'fasttruck']}


def item(kind, qty, priority, **values):
    return {'fulfillmentType': kind,
            'fullMtdMsg': 'Delivery' if kind == 'FAST_TRUCK' else kind,
            'totalQty': qty, 'isAvlSts': True, 'priority': priority, **values}


def mini():
    return {'additionalServices': False, 'product': {'majorAppliance': False},
            'location': {'itemInventory': {'itemAvailList': [
                item('Parcel', 1225, 3, isDynamicLeadTime=True,
                     parcelDates=[{'carrierType': 'STANDARD', 'promiseDate': '2026-09-23T17:00:00-04:00'}]),
                item('Pickup', 544, 1, onhandQty=9, itmLdTm='09-22-2026-08:00 UTC',
                     itmLdDateTm='2026-09-22T08:05:00-08:00', isDynamicLeadTime=True),
                item('Delivery', 866, 4, itmLdTm='09-22-2026-04:00 UTC',
                     itmConsolidationApptDate='2026-09-24T08:00:00-04:00'),
                item('ExpeditedDelivery', 7, 2,
                     fullPath=[{'promiseDate': '2026-09-22T23:59:00-04:00'}]),
            ]}}}


def washer():
    return {'additionalServices': True, 'product': {'majorAppliance': True},
            'location': {'itemInventory': {'itemAvailList': [
                item('Pickup', 91, 1, onhandQty=0, itmLdTm='09-28-2026-08:00 UTC', isDynamicLeadTime=False),
                item('Delivery', 14, 4, itmLdTm='09-22-2026-04:00 UTC',
                     itmConsolidationApptDate='2026-09-24T07:00:00-04:00'),
                item('FAST_TRUCK', 14, 3, itmLdTm='09-22-2026-04:00 UTC',
                     itmConsolidationApptDate='2026-09-23T07:00:00-04:00'),
            ]}}}


def rdp_service_response():
    # Minimal display fields supplied by the user from the successful RDP probe.
    # selected/default are null in the API; the captured ServiceDiscovery code
    # selects CUSTOM_RTF on initial mount for the verified guest profile.
    return {'status': 200, 'body': json.dumps({'additionalServices': {'CUSTOM_RTF': [{
        'description': 'with required $43.48 install kit', 'avlFulfillTypes': ['SD'],
        'selected': None, 'isDefaultSelected': None,
        'isPremiumInstallation': None, 'alerts': None,
    }]}})}


class CapturedDisplayTests(unittest.TestCase):
    def test_mini_all_six_fields_match_observed_cards(self):
        node = mini()
        values, reason = display.api_display(node, FLAGS, NOW, NO_SERVICES)
        pickup, pickup_reason = display.pickup_display(node, STORE, FLAGS, NOW)
        self.assertEqual((reason, pickup_reason), ('', ''))
        self.assertEqual(values | pickup, {
            'pick_up_availability': 'Pickup 9am Tomorrow',
            'delivery_availability': 'Shipping Wed, Sep 23',
            'fastest_delivery': 'Get it Tomorrow',
            'available_quantity_for_purchase_pickup': 9,
            'available_quantity_for_purchase_delivery': 1225,
            'available_quantity_for_purchase_fastdelivery': 866,
        })

    def test_washer_installation_message_and_selected_option_are_separate(self):
        node = washer()
        values, reason = display.api_display(node, FLAGS, NOW, RTF)
        pickup, pickup_reason = display.pickup_display(node, STORE, FLAGS, NOW)
        self.assertEqual((reason, pickup_reason), ('', ''))
        self.assertEqual(values | pickup, {
            'pick_up_availability': 'Pickup Ready by Mon, Sep 28 (Est.)',
            'delivery_availability': 'Delivery w/FREE Installation',
            'fastest_delivery': 'Get it by Wed, Sep 23',
            'available_quantity_for_purchase_pickup': 91,
            'available_quantity_for_purchase_delivery': 14,
            'available_quantity_for_purchase_fastdelivery': '',
        })

    def test_neither_installed_flag_nor_lead_days_invent_installation_or_date(self):
        node = washer()
        node['product']['installAvailInd'] = True
        for slot in node['location']['itemInventory']['itemAvailList']:
            slot['itmLdTmDays'] = 0
        values, reason = display.api_display(node, FLAGS, NOW)
        self.assertIn('unknown_service_selection', reason)
        self.assertEqual(values['delivery_availability'], '')
        self.assertEqual(values['fastest_delivery'], '')
        self.assertEqual(values['available_quantity_for_purchase_delivery'], 14)

    def test_missing_fast_promise_does_not_use_zero_lead_days(self):
        node = mini()
        slot = node['location']['itemInventory']['itemAvailList'][-1]
        slot.update(fullPath=[], itmLdTmDays=0, itmLdTm='09-22-2026-04:00 UTC')
        values, reason = display.api_display(node, FLAGS, NOW, NO_SERVICES)
        self.assertEqual(values['fastest_delivery'], '')
        self.assertIn('missing_expediteddelivery_promise', reason)
        self.assertEqual(values['delivery_availability'], 'Shipping Wed, Sep 23')

    def test_network_quantity_cap_is_not_an_exact_count(self):
        node = mini()
        node['location']['itemInventory']['itemAvailList'][2]['totalQty'] = 9000
        values, reason = display.api_display(node, FLAGS, NOW, NO_SERVICES)
        self.assertEqual(reason, '')
        self.assertEqual(values['available_quantity_for_purchase_fastdelivery'], '5000+')

    def test_api_inventory_takes_precedence_over_stale_analytics(self):
        node = mini()
        node['itemInventory'] = {'analyticsData': {'expeditedDelivery': {'totalQty': 9999}}}
        original = copy.deepcopy(node)
        values, _ = display.api_display(node, FLAGS, NOW, NO_SERVICES)
        self.assertEqual(values['available_quantity_for_purchase_fastdelivery'], 866)
        self.assertEqual(node, original)

    def test_conflicting_or_missing_priority_does_not_choose_fastest_date(self):
        for priority in (None, 4):
            node = washer()
            node['location']['itemInventory']['itemAvailList'][-1]['priority'] = priority
            values, reason = display.api_display(node, FLAGS, NOW, RTF)
            self.assertEqual(values['fastest_delivery'], '')
            self.assertIn('priority', reason)

    def test_calendar_relative_to_destination_and_store_not_korean_clock(self):
        node = mini()
        now = datetime.fromisoformat('2026-09-22T04:01:00+00:00')
        values, _ = display.api_display(node, FLAGS, now, NO_SERVICES)
        self.assertEqual(values['fastest_delivery'], 'Get it Today')
        pickup, _ = display.pickup_display(node, STORE, FLAGS, now)
        self.assertEqual(pickup['pick_up_availability'], 'Pickup 9am Tomorrow')

    def test_missing_store_hours_does_not_invent_9am(self):
        values, reason = display.pickup_display(mini(), {'timeZone': 'America/Anchorage'}, FLAGS, NOW)
        self.assertEqual(values['pick_up_availability'], '')
        self.assertEqual(values['available_quantity_for_purchase_pickup'], 9)
        self.assertEqual(reason, 'missing_pickup_store_hours')

    def test_delivery_zip_does_not_supply_pickup_timezone(self):
        values, reason = display.pickup_display(mini(), {'zipCode': '10010'}, FLAGS, NOW)
        self.assertEqual(values['pick_up_availability'], '')
        self.assertEqual(reason, 'missing_pickup_store_timezone')

    def test_product_group_also_selects_major_appliance_layout(self):
        node = washer()
        node['product'] = {'merchandisingHierarchy': {'productGroup': '517402'}}
        values, reason = display.api_display(node, FLAGS, NOW, RTF)
        self.assertEqual(reason, '')
        self.assertEqual(values['delivery_availability'], 'Delivery w/FREE Installation')

    def test_display_status_does_not_replace_card_eligibility(self):
        node = mini()
        node['location']['itemInventory']['itemAvailList'][0]['displayStatus'] = False
        values, reason = display.api_display(node, FLAGS, NOW, NO_SERVICES)
        self.assertEqual(reason, '')
        self.assertEqual(values['delivery_availability'], 'Shipping Wed, Sep 23')


class ServiceAndCollectorTests(unittest.TestCase):
    def test_rdp_null_selection_uses_verified_page_default_not_api_boolean(self):
        selection, reason = display.service_selection(rdp_service_response())
        self.assertEqual((selection, reason), (RTF, ''))
        values, reason = display.api_display(washer(), FLAGS, NOW, selection)
        self.assertEqual(reason, '')
        self.assertEqual(values['delivery_availability'], 'Delivery w/FREE Installation')
        self.assertEqual(values['fastest_delivery'], 'Get it by Wed, Sep 23')
        self.assertNotIn('$43.48', json.dumps(values))

    def test_collector_with_rdp_service_shape_matches_whirlpool_capture(self):
        namespace = parser_functions()
        calls = []
        def get(driver, path, **kwargs):
            calls.append((path, kwargs.get('headers')))
            return rdp_service_response()
        namespace['run_xhr_get'] = get
        namespace['api_display'] = lambda node, flags, services=None: display.api_display(node, flags, NOW, services)
        namespace['pickup_display'] = lambda node, store, flags: display.pickup_display(node, store, flags, NOW)
        response = {'status': 200, 'body': json.dumps({'productDetails': {'sample': washer()}, 'storeDetails': STORE})}
        evidence = json.loads(namespace['collect_fulfillment_display'](None, 'sample', response, '0289', {})['body'])
        self.assertEqual(evidence['status'], 'ok')
        self.assertEqual(evidence['reason'], '')
        self.assertEqual(evidence['source'], 'api')
        self.assertEqual(evidence['values'], {
            'pick_up_availability': 'Pickup Ready by Mon, Sep 28 (Est.)',
            'delivery_availability': 'Delivery w/FREE Installation',
            'fastest_delivery': 'Get it by Wed, Sep 23',
            'available_quantity_for_purchase_pickup': 91,
            'available_quantity_for_purchase_delivery': 14,
            'available_quantity_for_purchase_fastdelivery': '',
        })
        self.assertEqual(len(calls), 1)
        self.assertIn('storeNumber=1674&quantity=1&zipCode=10010&stateCode=NY', calls[0][0])
        self.assertEqual(calls[0][1]['x-requested-with'], 'XMLHttpRequest')

    def test_finalize_preserves_six_columns_and_explicit_blanks_for_both_categories(self):
        # Execute only pure functions and literal column lists, never config,
        # environment loading or a pipeline main function.
        schema = ast.parse(Path(__file__).with_name('step00_erd_schema.py').read_text(encoding='utf-8-sig'))
        columns = {}
        for node in schema.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
                columns[node.targets[0].id] = ast.literal_eval(node.value)
        finalize_path = Path(__file__).with_name('step09_finalize.py')
        tree = ast.parse(finalize_path.read_text(encoding='utf-8-sig'))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name != 'main']
        for category in ('REF', 'LDY'):
            fields = list(dict.fromkeys(columns['CORE_OUTPUT_COLUMNS'] + columns[category + '_ERD_COLUMNS']
                                       + columns['COMMON_ERD_COLUMNS'] + columns['LEGACY_COMPAT_COLUMNS']))
            namespace = {'datetime': datetime, 'FIELDNAMES': fields, 'PRODUCT_TYPE': category,
                         'CATEGORY_COLUMNS': ['ldy_capacity', 'ldy_loading_type'] if category == 'LDY'
                         else ['ref_capacity', 'ref_refrigerator_type'],
                         'output_page_type': lambda row: 'main', 'retailer_sku_name_text': lambda row: ''}
            exec(compile(ast.Module(body=functions, type_ignores=[]), str(finalize_path), 'exec'), namespace)
            source = {'delivery_availability': '', 'fastest_delivery': '',
                      'available_quantity_for_purchase_delivery': '5000+', 'fulfillment_display_status': 'partial',
                      'delivery': 'STALE LISTING DATE'}
            result = namespace['finalize_row'](source, 'test', '2026-09-22 00:00:00')
            self.assertEqual(set(result), set(fields))
            self.assertEqual(result['delivery_availability'], '')
            self.assertEqual(result['fastest_delivery'], '')
            self.assertEqual(result['available_quantity_for_purchase_delivery'], '5000+')

    def test_eighty_products_continue_after_service_block_without_any_page_visit(self):
        namespace = parser_functions()
        paths = []
        class NoBrowser:
            def __getattr__(self, name):
                raise AssertionError('Unexpected browser operation: ' + name)
        def get(driver, path, **kwargs):
            paths.append(path)
            if path.startswith('/purchase/'):
                return {'status': 403, 'body': ''}
            sku = path.split('/')[2]
            return {'status': 200, 'body': json.dumps({'productDetails': {sku: washer()}, 'storeDetails': STORE})}
        namespace['run_xhr_get'] = get
        namespace['print'] = lambda *args, **kwargs: None
        namespace['run_xhr_post'] = lambda *args: {'status': 200, 'body': '{}'}
        namespace['fetch_reviews_until_target'] = lambda *args: {'reviews': {'status': 200, 'body': '{}'}}
        cache = {}
        for number in range(80):
            responses = namespace['fetch_sku'](NoBrowser(), str(number), 'category', 'parent', flag_cache=cache)
            self.assertEqual(responses['productdetail']['status'], 200)
            evidence = json.loads(responses['fulfillment_display']['body'])
            self.assertEqual(evidence['source'], 'api')
            self.assertEqual(evidence['values']['delivery_availability'], '')
            self.assertEqual(evidence['values']['available_quantity_for_purchase_delivery'], 14)
        self.assertEqual(sum(path.startswith('/wpd/') for path in paths), 80)
        self.assertEqual(sum(path.startswith('/purchase/') for path in paths), 1)
        self.assertFalse(any(path.startswith('/pd/') for path in paths))
        self.assertEqual(cache['0289']['screen_checks'], 0)

    def test_service_projection_requires_success_and_fulfillment_types(self):
        payload = {'additionalServices': {'CUSTOM_RTF': [
            {'description': 'Standard installation', 'avlFulfillTypes': ['SD']},
        ]}}
        result, reason = display.service_selection({'status': 200, 'body': json.dumps(payload)})
        self.assertEqual((result, reason), (RTF, ''))
        result, reason = display.service_selection({'status': 403, 'body': ''})
        self.assertIsNone(result)
        self.assertEqual(reason, 'services_http_403')
        del payload['additionalServices']['CUSTOM_RTF'][0]['avlFulfillTypes']
        self.assertIsNone(display.service_selection({'status': 200, 'body': json.dumps(payload)})[0])

    def test_empty_services_is_distinct_from_unavailable_services_api(self):
        self.assertEqual(display.service_selection({'status': 200, 'body': '{"additionalServices":{}}'}),
                         (NO_SERVICES, ''))
        self.assertIsNone(display.service_selection({'status': 200, 'body': '{}'})[0])

    def test_partial_evidence_clears_stale_values_in_all_six_columns(self):
        namespace = parser_functions()
        keys = (*display.FIELDS, 'pick_up_availability', 'available_quantity_for_purchase_pickup')
        responses = {'productdetail': {'status': 200, 'body': json.dumps({'productDetails': {'sample': washer()}})},
                     'fulfillment_display': {'body': json.dumps({'source': 'api', 'status': 'partial',
                         'flags': FLAGS, 'values': {'available_quantity_for_purchase_delivery': 14}})}}
        row = namespace['build_row'](dict.fromkeys(keys, 'STALE'), 'sample', responses)
        self.assertEqual(row['available_quantity_for_purchase_delivery'], 14)
        self.assertTrue(all(row[key] == '' for key in keys if key != 'available_quantity_for_purchase_delivery'))


if __name__ == '__main__':
    unittest.main()
