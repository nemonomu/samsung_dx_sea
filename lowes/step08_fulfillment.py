"""Lowe's API display rules for the verified guest layout.

The productdetail methods are shipping options, not necessarily separate cards.
Use API inputs and the verified guest rendering profile. Unsupported cases stay
explicitly unresolved; the collector never navigates to a product page.
This module has no crawler/configuration imports and can be tested offline.
"""
import json
import math
import re
from datetime import datetime, timezone


FIELDS = (
    'delivery_availability', 'fastest_delivery',
    'available_quantity_for_purchase_delivery',
    'available_quantity_for_purchase_fastdelivery',
)
FLAG_NAMES = ('enableThreeTileDesign', 'enableNetworkStock', 'isApplianceSwimLaneEnabled')
# select_inventory_display 0_302_0, fulfillment constants / isMajorAppliance.
APPLIANCE_GROUPS = {
    '511830', '510059', '511811', '510069', '510403', '518620', '517402',
    '510065', '518670', '510056', '510070', '510060', '510034', '500285',
    '500283', '500281', '500274', '500273', '500272',
}


def empty_display():
    return dict.fromkeys(FIELDS, '')


def display_flags(html):
    """Conflicting/missing defaults remain unknown instead of guessing an A/B flag."""
    flags = {}
    for name in FLAG_NAMES:
        values = set(re.findall(r'"' + name + r'"\s*:\s*(true|false)\b', html or ''))
        if len(values) == 1:
            flags[name] = values.pop() == 'true'
    return flags


def fulfillment_slot(node, analytics, key, kind):
    slot = dict(analytics.get(key) or {})
    location = node.get('location') or {}
    inventories = location.get('itemInventoryList') or []
    inventory = inventories[0] if isinstance(inventories, list) and inventories else location.get('itemInventory')
    items = inventory.get('itemAvailList') if isinstance(inventory, dict) else None
    normalize = lambda value: re.sub(r'[_\s]', '', str(value or '')).lower()
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and normalize(item.get('fulfillmentType')) == normalize(kind):
                slot.update(item)
                break
        else:
            # A complete page inventory must not resurrect an analytics-only method.
            slot = {}
    if slot:
        slot.setdefault('fulfillmentType', kind)
    return slot


def available(slot):
    return bool(slot.get('isAvlSts')) and slot.get('displayStatus') is not False


def date_value(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        try:
            return datetime.strptime(str(value).split(' ')[0], '%m-%d-%Y-%H:%M')
        except ValueError:
            return None


def date_label(value, now=None, relative=False):
    dt = date_value(value)
    if dt is None:
        return ''
    if relative and dt.tzinfo is not None:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is not None:
            days = (dt.date() - current.astimezone(dt.tzinfo).date()).days
            if days in (0, 1):
                return ('Today', 'Tomorrow')[days]
    return dt.strftime('%a, %b ') + str(dt.day)


def promise_date(slot, parcel=False):
    paths = slot.get('fullPath') or slot.get('fullpath') or []
    if parcel:
        dates = slot.get('parcelDates')
        if dates is None:
            dates = paths[0].get('parcelDates') if paths else []
        for item in dates or []:
            if item.get('carrierType') in ('STANDARD', 'BASIC') and item.get('promiseDate'):
                return item['promiseDate']
        return slot.get('itmLdTm')
    return (slot.get('itmConsolidationApptDateLocal')
            or slot.get('itmConsolidationApptDate')
            or next((path.get('promiseDate') for path in paths if path.get('promiseDate')), None))


# The page's guest layout captured on 2026-09-22. This is an explicit,
# versioned rendering profile, not a claim that productdetail contains UI flags.
DISPLAY_PROFILE = 'select_inventory_display/0_304_0:guest:20260922'
VERIFIED_FLAGS = dict(zip(FLAG_NAMES, (True, True, False)))


def inventory_items(node):
    location = node.get('location') or {}
    if not isinstance(location, dict):
        return None
    inventories = location.get('itemInventoryList')
    inventory = inventories[0] if isinstance(inventories, list) and inventories else location.get('itemInventory')
    items = inventory.get('itemAvailList') if isinstance(inventory, dict) else None
    return items if isinstance(items, list) and all(isinstance(x, dict) for x in items) else None


def method(slot):
    return re.sub(r'[_\s]', '', str(slot.get('fulfillmentType') or slot.get('fullMtdMsg') or '')).lower()


def eligible(slot):
    if not slot or slot.get('isAvlSts') is not True:
        return False
    minimum = slot.get('orderItemMinQty')
    return not minimum or (isinstance(minimum, (int, float))
                           and isinstance(slot.get('totalQty'), (int, float))
                           and slot['totalQty'] >= minimum)


def needs_service_selection(node):
    return node.get('additionalServices') is True and any(
        method(item) != 'pickup' and eligible(item) for item in inventory_items(node) or []
    )


def display_quantity(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or int(value) != value):
        return ''
    return '5000+' if value > 5000 else int(value)


def is_major(product):
    return (product.get('majorAppliance') is True
            or str(product.get('productMerchClass') or '').lower() == 'major_appliance'
            or str((product.get('merchandisingHierarchy') or {}).get('productGroup')) in APPLIANCE_GROUPS)


def service_selection(response):
    """Project only display inputs from the read-only additionalServices API.

    ServiceDiscovery 0_124_0 defaults CUSTOM_RTF to its first service after
    sorting descriptions descending. Other service selections need independent
    evidence; do not treat installAvailInd as a selected installation.
    """
    if not isinstance(response, dict) or response.get('status') != 200:
        return None, 'services_http_' + str((response or {}).get('status', 'missing'))
    try:
        payload = json.loads(response.get('body') or '{}')
    except (ValueError, TypeError):
        return None, 'invalid_services_json'
    services = payload.get('additionalServices') if isinstance(payload, dict) else None
    if not isinstance(services, dict) or services.get('alerts'):
        return None, 'missing_or_restricted_services'
    if any(not isinstance(services.get(key, []), list) for key in ('CUSTOM_RTF', 'ILB', 'LAB')):
        return None, 'invalid_services_shape'
    # Premium/default installation or assembly can change the selected method.
    for key in ('ILB', 'LAB'):
        for item in services.get(key) or []:
            if not isinstance(item, dict) or any(item.get(k) for k in (
                'selected', 'isDefaultSelected', 'isPremiumInstallation', 'mandatory',
            )):
                return None, 'unsupported_default_service'
    rtf = services.get('CUSTOM_RTF') or []
    if not rtf:
        return {'rtf': False, 'eligible_methods': None}, ''
    if any(not isinstance(x, dict) or not isinstance(x.get('description'), str) for x in rtf):
        return None, 'invalid_rtf_services'
    item = sorted(rtf, key=lambda x: x['description'], reverse=True)[0]
    types = item.get('avlFulfillTypes')
    if item.get('alerts') or not isinstance(types, list) or not all(isinstance(x, str) for x in types):
        return None, 'missing_rtf_fulfillments'
    mapping = {'SD': ('delivery', 'fasttruck'), 'ED': ('expediteddelivery',),
               'SH': ('parcel',), 'DDC': ('parcel',), 'SP': ('pickup',)}
    allowed = sorted({kind for code in types for kind in mapping.get(code, ())})
    if not allowed or 'ID' in types:
        return None, 'unsupported_installer_delivery'
    return {'rtf': True, 'eligible_methods': allowed}, ''


def pickup_display(node, store_details=None, flags=None, now=None):
    result = {'pick_up_availability': '', 'available_quantity_for_purchase_pickup': ''}
    items = inventory_items(node)
    if items is None:
        return result, 'missing_pickup_inventory'
    if sum(method(x) == 'pickup' for x in items) > 1:
        return result, 'duplicate_pickup_inventory'
    slot = next((x for x in items if method(x) == 'pickup'), {})
    if not eligible(slot):
        if any(x.get('isAvlSts') for x in slot.get('nearestStores') or []):
            return result, 'nearby_pickup_not_verified'
        return result, ''
    flags = flags or {}
    if any(k not in flags for k in FLAG_NAMES):
        return result, 'unknown_pickup_display_flags'
    onhand, total = slot.get('onhandQty'), slot.get('totalQty')
    quantity_reason = ''
    if flags['enableNetworkStock']:
        if display_quantity(onhand) == '' or display_quantity(total) == '':
            quantity_reason = 'missing_pickup_quantity'
        elif onhand > 0 or total > 0:
            result['available_quantity_for_purchase_pickup'] = display_quantity(onhand if onhand >= 1 else total)
    store_details = store_details or {}
    # Never use the delivery ZIP timezone for a pickup store.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        zone = ZoneInfo(store_details.get('timeZone') or store_details.get('storeTZ') or '')
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return result, 'missing_pickup_store_timezone'
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return result, 'missing_current_timezone'
    current = current.astimezone(zone)
    promised = date_value(slot.get('itmLdDateTm'))
    if promised is not None and promised.tzinfo is not None:
        local = promised.astimezone(zone)
        hours = (local - current).total_seconds() / 3600
    else:
        local = date_value(slot.get('itmLdTm'))
        if local is None:
            return result, 'missing_pickup_date'
        hours = None
    days = (local.date() - current.date()).days
    major = is_major(node.get('product') or {})
    three = flags.get('enableThreeTileDesign') and not major
    if hours is not None and hours < 0:
        return result, 'expired_pickup_promise'
    if hours is not None and hours < 3:
        label = 'within 3 hrs' if three else 'Ready within 3 hrs'
    elif days == 0:
        label = 'Today' if three else 'Ready Today'
    elif days == 1:
        prefix = ''
        if promised is not None and promised.tzinfo is not None:
            day = local.strftime('%A')
            entries = store_details.get('storeHours') or []
            hours_entry = next((x.get('day', {}) for x in entries
                                if isinstance(x, dict) and isinstance(x.get('day'), dict)
                                and x['day'].get('day') == day), {})
            opening = hours_entry.get('open')
            try:
                hour, minute, second = map(int, opening.split('.'))
                processing = (hour + 3) % 24
            except (AttributeError, ValueError):
                return result, 'missing_pickup_store_hours'
            # The UI compares the promise's own clock to opening + 3 hours.
            if (promised.hour, promised.minute, promised.second) <= (processing, minute, second):
                prefix = str(processing % 12 or 12) + (f':{minute:02}' if minute else '')
                prefix += ('pm' if processing >= 12 else 'am') + ' '
        label = ('' if three else ('Ready by ' if prefix else 'Ready ')) + prefix + 'Tomorrow'
    else:
        label = date_label(local.isoformat())
        if not slot.get('itmLdDateTm') and not slot.get('isDynamicLeadTime'):
            label += ' (Est.)'
        if not three:
            label = 'Ready by ' + label
    result['pick_up_availability'] = 'Pickup ' + label
    return result, quantity_reason


def api_display(node, flags=None, now=None, services=None):
    """Render verified guest cases from API data; return explicit partial reasons.

    No HTML request or browser navigation is a fallback. Inventory availability,
    card quantity, selected option and date are separate decisions.
    """
    result = empty_display()
    items = inventory_items(node)
    if items is None:
        return result, 'missing_page_inventory'
    kinds = [method(x) for x in items]
    if len(kinds) != len(set(kinds)):
        return result, 'duplicate_fulfillment_methods'
    active = [x for x in items if method(x) != 'pickup' and eligible(x)]
    if not active:
        return result, ''
    flags = flags or {}
    if any(name not in flags for name in FLAG_NAMES):
        return result, 'unknown_display_flags'
    if flags['isApplianceSwimLaneEnabled']:
        return result, 'different_display_configuration'
    product = node.get('product') or {}
    if product.get('groupType') or product.get('marketplaceSeller'):
        return result, 'unsupported_product_layout'
    if any(method(x) not in ('parcel', 'delivery', 'fasttruck', 'expediteddelivery') for x in active):
        return result, 'unknown_delivery_method'
    major = is_major(product)
    three = bool(flags['enableThreeTileDesign'] and not major)
    parcel = next((x for x in active if method(x) == 'parcel'), None)
    # The additionalServices API may fail independently; preserve a verified
    # shipping card while leaving service-dependent delivery values unresolved.
    issues = []
    if services is None:
        if node.get('additionalServices') is False:
            services = {'rtf': False, 'eligible_methods': None}
        else:
            issues.append('unknown_service_selection')
    allowed = services.get('eligible_methods') if services is not None else None
    delivery = [x for x in active if method(x) != 'parcel'
                and (allowed is None or method(x) in allowed)]

    def quantity(slots):
        if not flags['enableNetworkStock']:
            return ''
        values = [x.get('totalQty') for x in slots]
        if not values or any(display_quantity(x) == '' for x in values):
            issues.append('missing_display_quantity')
            return ''
        return display_quantity(max(values))

    def calendar(slot, kind):
        if kind == 'parcel':
            value = promise_date(slot, parcel=True)
        elif kind in ('delivery', 'fasttruck'):
            value = slot.get('itmConsolidationApptDate') or slot.get('itmConsolidationDate')
        else:
            value = promise_date(slot)
        label = date_label(value, now=now, relative=True)
        if not label:
            issues.append('missing_' + kind + '_promise')
            return ''
        if kind != 'expediteddelivery' and label == 'Today':
            label = date_label(value)
        if kind == 'parcel' and label != 'Tomorrow' and not slot.get('isDynamicLeadTime'):
            label += ' (Est.)'
        return label

    def selected(slots):
        if len(slots) == 1:
            return slots[0]
        priorities = [x.get('priority') for x in slots]
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or x <= 0 for x in priorities):
            issues.append('missing_delivery_priority')
            return None
        first = min(priorities)
        if priorities.count(first) != 1:
            issues.append('ambiguous_delivery_priority')
            return None
        return slots[priorities.index(first)]

    if three and parcel and services is not None and (allowed is None or 'parcel' in allowed):
        label = calendar(parcel, 'parcel')
        if label:
            result['delivery_availability'] = 'Shipping ' + label
        result['available_quantity_for_purchase_delivery'] = quantity([parcel])
    candidates = delivery if three else [x for x in active if allowed is None or method(x) in allowed]
    if not candidates:
        return result, ';'.join(dict.fromkeys(issues))
    chosen = selected(candidates)
    if chosen is None:
        return result, ';'.join(dict.fromkeys(issues))
    kind = method(chosen)
    if three:
        # Separate Shipping and Fast Delivery cards have been verified. A second
        # ordinary Delivery card cannot be silently squeezed into the same column.
        label = calendar(chosen, kind)
        if kind != 'expediteddelivery' or label not in ('Today', 'Tomorrow') or services is None or services.get('rtf'):
            issues.append('unverified_three_tile_delivery_title')
        else:
            # Free scheduled-delivery promos can change the title to Delivery.
            promotion = (node.get('location') or {}).get('promotion') or {}
            if promotion.get('productLevelPromotions'):
                issues.append('delivery_promotion_title_not_verified')
            else:
                result['fastest_delivery'] = 'Get it ' + label
                grouped = [x for x in items if x.get('fullMtdMsg') in ('ExpeditedDelivery', 'Delivery')
                           and (x.get('isAvlSts') or x.get('totalQty'))]
                result['available_quantity_for_purchase_fastdelivery'] = quantity(grouped)
    else:
        grouped = [x for x in items if method(x) != 'pickup' and (x.get('isAvlSts') or x.get('totalQty'))]
        result['available_quantity_for_purchase_delivery'] = quantity(grouped)
        if services is not None and not services.get('rtf'):
            pickup = next((x for x in items if method(x) == 'pickup' and eligible(x)), None)
            if pickup and (not pickup.get('priority') or not chosen.get('priority')
                           or pickup['priority'] < chosen['priority']):
                # With pickup selected, the legacy tile can show the earliest
                # alternative instead of the first delivery option. Do not
                # confuse that state with a selected delivery date.
                issues.append('pickup_selected_delivery_message_not_verified')
                return result, ';'.join(dict.fromkeys(issues))
        if services is not None:
            if services.get('rtf'):
                result['delivery_availability'] = 'Delivery w/FREE Installation'
            else:
                label = calendar(chosen, kind)
                if label:
                    result['delivery_availability'] = 'Delivery ' + label
            # A legacy option has no separate stock card. Never copy truck stock
            # into the fast quantity column merely because an option is selected.
            if kind in ('fasttruck', 'expediteddelivery'):
                label = calendar(chosen, kind)
                if label:
                    result['fastest_delivery'] = ('Get it ' if label in ('Today', 'Tomorrow') else 'Get it by ') + label
    return result, ';'.join(dict.fromkeys(issues))


def displayed_fields(cards, fast_message=''):
    """Use only card titles, dates and quantities captured from visible DOM nodes."""
    result = empty_display()
    normal, fast = [], []
    for card in cards:
        title = ' '.join(str(card.get('title') or '').split())
        if title.lower() == 'pickup':
            continue
        if title.lower() not in ('shipping', 'delivery', 'appliance delivery', 'fast delivery'):
            return None
        if card.get('disabled') or re.search(r'\bunavailable\b|\bout of stock\b', str(card.get('date') or ''), re.I):
            continue
        date = ' '.join(str(card.get('date') or '').split())
        if not date:
            return None  # still loading / unknown layout
        qty_text = str(card.get('stock') or '')
        match = re.search(r'(?<!\d)([\d,]+)(\+?)\s*available\b', qty_text, re.I)
        qty = (match.group(1).replace(',', '') + match.group(2)) if match else ''
        if qty and not match.group(2):
            qty = int(qty)
        entry = (title, date, qty)
        (fast if title.lower() == 'fast delivery' else normal).append(entry)
    # Two visible normal cards cannot be squeezed into this single-value column
    # without defining a new business rule. Do not choose one arbitrarily.
    if len(normal) > 1 or len(fast) > 1:
        return None
    if normal:
        title, date, qty = normal[0]
        result['delivery_availability'] = f'{title} {date}'
        result['available_quantity_for_purchase_delivery'] = qty
    if fast:
        _, date, qty = fast[0]
        if date.lower().startswith('get it'):
            result['fastest_delivery'] = date
        elif date in ('Today', 'Tomorrow'):
            result['fastest_delivery'] = f'Get it {date}'
        else:
            result['fastest_delivery'] = f'Get it by {date}'
        result['available_quantity_for_purchase_fastdelivery'] = qty
    elif fast_message:
        # The legacy layout shows a fast badge with the selected delivery option,
        # but no separate fast stock card. Read its message, leave that qty empty.
        result['fastest_delivery'] = fast_message
    return result
