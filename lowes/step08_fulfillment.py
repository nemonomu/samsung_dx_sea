"""Lowe's display rules and bounded, same-region checks for ambiguous inventory.

The productdetail methods are shipping options, not necessarily separate cards.
Keep simple cases on XHR; read rendered cards when selecting a card would require
guessing UI eligibility, pricing, or the customer's selected delivery option.
This module has no crawler/configuration imports and can be tested offline.
"""
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit


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


def api_display(node, flags=None, now=None):
    """Return (fields, reason_for_screen_check). Never join underlying methods."""
    result = empty_display()
    flags = flags or {}
    analytics = (node.get('itemInventory') or {}).get('analyticsData') or {}
    slots = [fulfillment_slot(node, analytics, key, kind) for key, kind in (
        ('truck', 'Delivery'), ('parcel', 'Parcel'),
        ('expeditedDelivery', 'ExpeditedDelivery'), ('fastTruck', 'FAST_TRUCK'),
    )]
    regular = [slot for slot in slots[:2] if available(slot)]
    if any(available(slot) for slot in slots[2:]):
        return result, 'fast_delivery_display'
    if len(regular) > 1:
        return result, 'multiple_delivery_methods'
    if not regular:
        return result, ''
    if any(name not in flags for name in FLAG_NAMES):
        return result, 'unknown_display_flags'
    if flags['enableNetworkStock'] is not True or flags['isApplianceSwimLaneEnabled']:
        return result, 'different_display_configuration'
    slot = regular[0]
    product = node.get('product') or {}
    major = (product.get('majorAppliance') is True
             or str(product.get('productMerchClass') or '').lower() == 'major_appliance'
             or str((product.get('merchandisingHierarchy') or {}).get('productGroup')) in APPLIANCE_GROUPS)
    parcel = slot.get('fulfillmentType') == 'Parcel'
    label = 'Shipping' if parcel and flags['enableThreeTileDesign'] and not major else 'Delivery'
    value = promise_date(slot, parcel=parcel)
    date = date_label(value, now=now, relative=parcel)
    # Parcel's display utility abbreviates tomorrow, but prints today's date.
    if parcel and date == 'Today':
        date = date_label(value)
    if not date:
        # itmLdTmDays=0 describes processing/lead time, not a missing parcel promise.
        return result, 'missing_delivery_date'
    if parcel and not slot.get('isDynamicLeadTime'):
        return result, 'estimated_delivery_date'
    qty = slot.get('totalQty')
    if qty is None:
        return result, 'missing_display_quantity'
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return result, 'invalid_display_quantity'
    if qty > 5000:
        return result, 'capped_display_quantity'
    result['delivery_availability'] = f'{label} {date}'
    result['available_quantity_for_purchase_delivery'] = qty if qty > 0 else ''
    return result, ''


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


# Scope all values to the fulfillment cards, excluding carousel/recommendation text.
# Productdetail resource URLs establish the actual delivery region; location.zipcode
# in the response can instead be the store ZIP and must not be used for this check.
CARD_SCRIPT = r"""
const visible = e => !!(e && e.getClientRects().length &&
    getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none');
const text = e => visible(e) ? (e.innerText || '').replace(/\s+/g, ' ').trim() : '';
const contexts = performance.getEntriesByType('resource').flatMap(e => {
    try {
        const u = new URL(e.name), m = u.pathname.match(/^\/wpd\/(\d+)\/productdetail\/(\d+)\/Guest\/(\d+)$/);
        return m && u.hostname === 'www.lowes.com' ? [{sku:m[1], store:m[2], zip:m[3],
            state:u.searchParams.get('zipState') || '', nearby_store:u.searchParams.get('nearByStore') || ''}] : [];
    } catch (_) { return []; }
});
const cards = [...document.querySelectorAll('.radio-tile')].filter(visible).map(e => ({
    title: text(e.querySelector('.tile-title, .fulfilment-title')),
    date: text(e.querySelector('[data-testid="tile-time"], .fulfilment-messages')),
    stock: text(e.querySelector('[data-testid="tile-stock"], [data-testid="delivery-stock-msg"], .fulfilment-stock-messages')),
    disabled: !!e.querySelector('input:disabled') || e.getAttribute('aria-disabled') === 'true'
}));
const legacyFast = !document.querySelector('.three-tile') &&
    [...document.querySelectorAll('.zipcode-link')].some(e => /fast\s*delivery/i.test(text(e)));
const fastOption = legacyFast ? [...document.querySelectorAll('.delivery-option')]
    .filter(visible).find(e => e.querySelector('input:checked')) : null;
const fastMatch = text(fastOption).match(/Get it(?: by)?\s+(?:Today|Tomorrow|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2})/i);
return {path:location.pathname, contexts, cards,
    loading: [...document.querySelectorAll('.loader-tile, .three-tile [aria-busy="true"]')].some(visible),
    blocked: /access denied|verify you are human/i.test(document.title || ''),
    legacyFast, fast_message: fastMatch ? fastMatch[0] : ''};
"""


def matching_snapshot(snapshot, sku, context):
    if not str(snapshot.get('path') or '').rstrip('/').endswith('/' + str(sku)):
        return False
    requests = [r for r in snapshot.get('contexts', []) if r.get('sku') == str(sku)]
    if not requests:
        return False
    latest = requests[-1]
    return (str(latest.get('store') or '').lstrip('0') == str(context['store']).lstrip('0')
            and latest.get('zip') == context['zip']
            and latest.get('state') == context['state']
            and (not context.get('nearby_store') or latest.get('nearby_store') == context['nearby_store']))


def read_display(driver, sku, path, context, page_timeout=12, wait_timeout=8):
    """One navigation, no retry loop; require stable cards and matching request ZIP."""
    result = {'status': 'unresolved', 'source': 'screen', 'context': context, 'values': empty_display()}
    parts = urlsplit(path or '')
    if parts.scheme or parts.netloc or not parts.path.startswith('/pd/') or not parts.path.endswith('/' + str(sku)):
        result['reason'] = 'invalid_product_path'
        return result
    started = time.monotonic()
    original_timeout = None
    try:
        original_timeout = driver.timeouts.page_load
        driver.set_page_load_timeout(page_timeout)
        try:
            driver.get('https://www.lowes.com' + parts.path)
        except Exception as exc:
            # A load timeout may be caused by ads while the cards are already ready.
            result['navigation_error'] = type(exc).__name__
        deadline = time.monotonic() + wait_timeout
        previous, stable_since = None, None
        while time.monotonic() < deadline:
            snapshot = driver.execute_script(CARD_SCRIPT) or {}
            if snapshot.get('blocked'):
                result['reason'] = 'page_blocked'
                break
            delivery_card_ready = any(str(card.get('title') or '').lower() in (
                'shipping', 'delivery', 'appliance delivery', 'fast delivery',
            ) for card in snapshot.get('cards', []))
            if matching_snapshot(snapshot, sku, context) and delivery_card_ready and not snapshot.get('loading'):
                cards = snapshot['cards']
                signature = json.dumps([cards, snapshot.get('fast_message', '')], sort_keys=True)
                if signature != previous:
                    previous, stable_since = signature, time.monotonic()
                elif time.monotonic() - stable_since >= 1:
                    values = displayed_fields(cards, snapshot.get('fast_message', ''))
                    if values is not None:
                        result.update(status='ok', values=values, cards=cards,
                                      captured_at=datetime.now(timezone.utc).isoformat())
                        if snapshot.get('legacyFast') and not snapshot.get('fast_message'):
                            result.update(status='partial', reason='legacy_fast_message_unresolved')
                        break
            else:
                previous, stable_since = None, None
            time.sleep(0.25)
        else:
            result['reason'] = 'cards_not_ready_or_region_mismatch'
    except Exception as exc:
        result['reason'] = 'screen_check_' + type(exc).__name__
    finally:
        if original_timeout is not None:
            try:
                driver.set_page_load_timeout(original_timeout)
            except Exception:
                result.update(status='unresolved', values=empty_display(), reason='restore_page_timeout_failed')
        result['elapsed_seconds'] = round(time.monotonic() - started, 2)
    return result
