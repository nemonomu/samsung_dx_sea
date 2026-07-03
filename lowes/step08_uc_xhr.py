"""
Detail/Review enrichment via UC + 4 XHR per SKU (Plan D).

One UC browser session navigates to lowes.com (seed Akamai cookies),
then iterates final_targets SKUs doing same-origin XHRs:
  - GET  /wpd/{sku}/productdetail/{store}/Guest    (covers fields 41,42,46-50)
  - GET  /rnr/r/get-by-product/{sku}?sortBy=newestFirst&offset=N (covers 51,52, 53 reviews)
  - POST /pythia-recs-svc/v2/compare                            (covers 54)

Zero ZenRows cost. Spec 41~54 fully covered (43/44/45 saved as raw labels).
"""
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import undetected_chromedriver as uc

from .step00_config import DEFAULT_LOWES_RUN_ROOT, load_env, redact_sensitive, lowes_product_type
from .step00_erd_schema import retailer_sku_name_text
from .step00_uc import launch_chrome


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
load_env(PROJECT_ROOT / '.env')


RUN_DATE = os.getenv('LOWES_RUN_DATE', datetime.now().strftime('%Y%m%d'))
RUN_ROOT = Path(os.getenv('LOWES_RUN_ROOT', str(DEFAULT_LOWES_RUN_ROOT)))
DETAIL_ROOT = Path(os.getenv('LOWES_DETAIL_RUN_ROOT', str(RUN_ROOT / 'detail')))
OUTPUT_ROOT = Path(os.getenv('LOWES_OUTPUT_ROOT', str(RUN_ROOT / 'output')))

DEFAULT_INPUT_CSV = OUTPUT_ROOT / 'lowes_final_targets.csv'
INPUT_CSV = Path(os.getenv('LOWES_DETAIL_TARGET_CSV', str(DEFAULT_INPUT_CSV)))
RAW_DIR = Path(os.getenv('LOWES_DETAIL_DIR', str(DETAIL_ROOT / 'raw' / 'detail_xhr')))
DETAIL_CSV = Path(os.getenv('LOWES_DETAIL_CSV', str(DETAIL_ROOT / 'parsed' / 'detail_enriched_rows.csv')))
FAILURES_CSV = Path(os.getenv('LOWES_DETAIL_FAILURES_CSV', str(DETAIL_ROOT / 'parsed' / 'detail_failures.csv')))
FINAL_CSV = Path(os.getenv('LOWES_FINAL_OUTPUT_CSV', str(OUTPUT_ROOT / 'final_output.csv')))
MANIFEST_PATH = Path(os.getenv('LOWES_DETAIL_MANIFEST', str(DETAIL_ROOT / 'manifest_uc_xhr.json')))

LIMIT = int(os.getenv('LOWES_DETAIL_LIMIT', '0'))
HEADLESS = os.getenv('LOWES_UC_HEADLESS', '0').strip().lower() in {'1', 'true', 'yes'}
SEED_WAIT = float(os.getenv('LOWES_UC_SEED_WAIT_SECONDS', '3'))
PAGE_LOAD_TIMEOUT = int(os.getenv('LOWES_DETAIL_UC_PAGE_LOAD_TIMEOUT', '75'))
SCRIPT_TIMEOUT = int(os.getenv('LOWES_DETAIL_UC_SCRIPT_TIMEOUT', '30'))
SLEEP_BETWEEN = float(os.getenv('LOWES_DETAIL_UC_INTER_SLEEP', '0.2'))
REVIEW_TARGET = max(0, int(os.getenv('LOWES_REVIEW_TARGET', os.getenv('LOWES_REVIEW_TEXT_TARGET', '20'))))
REVIEW_PAGE_SIZE = max(1, int(os.getenv('LOWES_REVIEW_PAGE_SIZE', '10')))
REVIEW_MAX_OFFSET = max(0, int(os.getenv('LOWES_REVIEW_MAX_OFFSET', '100')))
REVIEW_EMPTY_TEXT = os.getenv('LOWES_REVIEW_EMPTY_TEXT', 'No review text provided')
PURCHASED_UNITS_RE = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*[kKmM]?\+?\s+(?:bought|purchased|sold)\b(?:\s+last\s+week)?",
    re.I,
)

STORE = os.getenv('LOWES_API_STORE_ID', '289').lstrip('0') or '289'
STORE_FMT = STORE.zfill(4) if len(STORE) <= 4 else STORE
ZIP = os.getenv('LOWES_API_STORE_ZIP', '10010')
STATE = os.getenv('LOWES_API_STORE_STATE', 'NY')
NEARBY_STORE = os.getenv('LOWES_API_NEARBY_STORE', '1674')

# Alt-store fallback: used only when primary returns productdetail != 200.
# Verified 2026-06-01: store=1854 (Zephyrhills FL) services SKUs that 0289 (Brooklyn NY) refuses.
# URL kept the same ZIP/state/nearby as primary (verified working combination).
ALT_STORE = os.getenv('LOWES_API_ALT_STORE_ID', '1854').lstrip('0') or '1854'
ALT_STORE_FMT = ALT_STORE.zfill(4) if len(ALT_STORE) <= 4 else ALT_STORE
ALT_STORE_NAME = os.getenv('LOWES_API_ALT_STORE_NAME', "Zephyrhills Lowe's")
ALT_STORE_CITY = os.getenv('LOWES_API_ALT_STORE_CITY', 'Zephyrhills')
ALT_STORE_REGION = os.getenv('LOWES_API_ALT_STORE_REGION', '27')
ALT_STORE_ENABLED = (
    bool(ALT_STORE_FMT)
    and ALT_STORE_FMT != STORE_FMT
    and os.getenv('LOWES_API_ALT_STORE_ENABLED', '1').strip().lower() in {'1', 'true', 'yes'}
)


CATEGORY_BY_PRODUCT = {
    'REF': ('refrigerators', 'appliances'),
    'LDY': ('washing_machine', 'appliances'),
}


XHR_GET = r"""
    const done = arguments[arguments.length - 1];
    fetch(arguments[0], {credentials:'include', headers:{'accept':'application/json'}})
      .then(async r => done({status: r.status, body: await r.text()}))
      .catch(e => done({status:'err', error: String(e && e.message || e)}));
"""

XHR_POST = r"""
    const done = arguments[arguments.length - 1];
    fetch(arguments[0], {
      method:'POST', credentials:'include',
      headers:{'content-type':'application/json','accept':'application/json'},
      body: arguments[1]
    }).then(async r => done({status: r.status, body: await r.text()}))
      .catch(e => done({status:'err', error: String(e && e.message || e)}));
"""


def now_iso():
    return datetime.now().isoformat(timespec='seconds')


def truthy(v):
    return v not in ('', None, '0', 'False', False, 0)


def purchased_units_phrase(value):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    match = PURCHASED_UNITS_RE.search(text)
    return match.group(0) if match else ''


def read_input_rows():
    if not INPUT_CSV.exists():
        raise SystemExit(f'INPUT_CSV not found: {INPUT_CSV}')
    with INPUT_CSV.open('r', encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    if LIMIT > 0:
        rows = rows[:LIMIT]
    return rows


def category_codes():
    product = (lowes_product_type() or 'ref').upper()
    return CATEGORY_BY_PRODUCT.get(product, ('refrigerators', 'appliances'))


def launch_driver():
    options = uc.ChromeOptions()
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--lang=en-US')
    print(f'[uc] launch headless={HEADLESS}')
    driver = launch_chrome(uc, options=options, headless=HEADLESS)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(SCRIPT_TIMEOUT)
    return driver


def _add_cookie(driver, name, value):
    try:
        driver.add_cookie({'name': name, 'value': str(value), 'domain': '.lowes.com', 'path': '/', 'secure': True})
    except Exception as exc:
        print(f'[seed] cookie {name} failed: {exc}')


def _seed_store_cookies(driver, store_id=None, name=None, city=None, region=None):
    """Match the cookies a real browser sets after selecting store / entering ZIP.
    Defaults to primary (module-level) constants. ZIP/state/nearby always primary
    (verified working combination); only store identity varies for alt-store fallback.
    """
    store_id = store_id or STORE_FMT
    name = name if name is not None else os.getenv('LOWES_API_STORE_NAME', "Brooklyn Lowe's")
    city = city if city is not None else os.getenv('LOWES_API_STORE_CITY', 'Brooklyn')
    region = region if region is not None else os.getenv('LOWES_API_STORE_REGION', '4')
    store_data = {
        "id": store_id, "zip": ZIP, "state": STATE,
        "name": name, "city": city, "region": region,
    }
    personalization = {"zipCode": ZIP, "storeId": store_id, "state": STATE, "audienceList": []}
    _add_cookie(driver, 'sn', store_id)
    _add_cookie(driver, 'sd', json.dumps(store_data, separators=(',', ':')))
    _add_cookie(driver, 'zipcode', ZIP)
    _add_cookie(driver, 'zipstate', STATE)
    _add_cookie(driver, 'nearbyid', NEARBY_STORE)
    _add_cookie(driver, 'regionNumber', region)
    _add_cookie(driver, 'p13n', json.dumps(personalization, separators=(',', ':')))


def seed_session(driver):
    started = time.time()
    driver.get('https://www.lowes.com/')
    time.sleep(SEED_WAIT)
    if 'Access Denied' in (driver.page_source or '')[:1000]:
        raise RuntimeError('Access Denied on lowes.com home (UC blocked)')
    _seed_store_cookies(driver)
    # reload home so cookies take effect
    driver.get('https://www.lowes.com/')
    time.sleep(SEED_WAIT)
    elapsed = time.time() - started
    title = driver.title
    print(f'[seed] title={title!r} elapsed={elapsed:.1f}s  sn={STORE_FMT} zip={ZIP} state={STATE} nearby={NEARBY_STORE}')
    return elapsed


def reseed_for_alt(driver):
    """Switch session cookies to alt store and reload home so they take effect.
    Used before the alt-store retry pass when primary productdetail returned non-200.
    """
    started = time.time()
    _seed_store_cookies(driver, store_id=ALT_STORE_FMT, name=ALT_STORE_NAME, city=ALT_STORE_CITY, region=ALT_STORE_REGION)
    driver.get('https://www.lowes.com/')
    time.sleep(SEED_WAIT)
    elapsed = time.time() - started
    print(f'[reseed-alt] sn={ALT_STORE_FMT} zip={ZIP} state={STATE} nearby={NEARBY_STORE}  elapsed={elapsed:.1f}s')
    return elapsed


def run_xhr_get(driver, path):
    try:
        return driver.execute_async_script(XHR_GET, path)
    except Exception as exc:
        return {'status': 'err', 'error': f'{type(exc).__name__}: {exc}'}


def run_xhr_post(driver, path, body):
    try:
        return driver.execute_async_script(XHR_POST, path, json.dumps(body))
    except Exception as exc:
        return {'status': 'err', 'error': f'{type(exc).__name__}: {exc}'}


def review_path(sku, offset):
    suffix = '' if offset == 0 else f'&offset={offset}'
    return f'/rnr/r/get-by-product/{sku}?sortBy=newestFirst{suffix}'


def review_result_count_from_body(body):
    try:
        obj = json.loads(body) if body else {}
    except Exception:
        return 0
    return len(obj.get('results') or [])


def review_has_media(review):
    if not isinstance(review, dict):
        return False
    has_media = review.get('hasMediaContent')
    if isinstance(has_media, str):
        if has_media.strip().lower() in {'1', 'true', 'yes', 'y'}:
            return True
    elif has_media is True:
        return True
    for key in ('photos', 'photoUrls', 'videos'):
        if review.get(key):
            return True
    return False


def review_output_text(review):
    if not isinstance(review, dict):
        return ''
    text = (review.get('reviewText') or '').strip()
    if text:
        return text
    if review_has_media(review):
        return REVIEW_EMPTY_TEXT
    return ''


def review_output_count_from_body(body):
    try:
        obj = json.loads(body) if body else {}
    except Exception:
        return 0
    return sum(1 for review in (obj.get('results') or []) if review_output_text(review))


def review_total_results_from_body(body):
    try:
        obj = json.loads(body) if body else {}
    except Exception:
        return None
    value = obj.get('totalResults')
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_reviews_until_target(driver, sku):
    responses = {}
    collected_reviews = 0
    total_results = None
    offset = 0
    page_index = 1

    while True:
        label = f'reviews_p{page_index}'
        response = run_xhr_get(driver, review_path(sku, offset))
        responses[label] = response
        if response.get('status') != 200:
            break

        body = response.get('body', '') or ''
        collected_reviews += review_output_count_from_body(body)
        if total_results is None:
            total_results = review_total_results_from_body(body)

        next_offset = offset + REVIEW_PAGE_SIZE
        if REVIEW_TARGET and collected_reviews >= REVIEW_TARGET:
            break
        if total_results is not None and next_offset >= total_results:
            break
        if next_offset > REVIEW_MAX_OFFSET:
            break

        offset = next_offset
        page_index += 1

    return responses


def fetch_sku(driver, sku, category_id, parent_category, store=None):
    """Run 4 XHRs for a SKU. `store` overrides STORE_FMT for productdetail URL + compare body."""
    store = store or STORE_FMT
    out = {}
    out['productdetail'] = run_xhr_get(driver, f'/wpd/{sku}/productdetail/{store}/Guest/{ZIP}?nearByStore={NEARBY_STORE}&zipState={STATE}')
    out.update(fetch_reviews_until_target(driver, sku))
    body = {
        "anchors": [{
            "omniItemId": sku, "attrId": sku,
            "categoryId": category_id,
            "parentCategory": parent_category,
            "pageType": "product-display",
        }],
        "storeIds": [store],
        "audiences": [], "customerType": [], "audienceList": [],
        "zipCode": ZIP, "stateCode": STATE,
        "categoryId": category_id,
        "productAvailable": True,
        "channel": "desktop",
        "version": "default_fabrik",
    }
    out['compare'] = run_xhr_post(driver, '/pythia-recs-svc/v2/compare?version=default_fabrik&source=product-display', body)
    return out


CATEGORY_TO_REF_TYPE = {
    'TOP_FREEZER_REFRIGERATORS': 'Top-freezer refrigerator',
    'BOTTOM_FREEZER_REFRIGERATORS': 'Bottom-freezer refrigerator',
    'FRENCH_DOOR_REFRIGERATORS': 'French door refrigerator',
    'SIDE_BY_SIDE_REFRIGERATORS': 'Side-by-side refrigerator',
    'MINI_FRIDGES': 'Mini fridge',
    'COMPACT_REFRIGERATORS': 'Compact refrigerator',
    'BUILT_IN_REFRIGERATORS': 'Built-in refrigerator',
    'COUNTER_DEPTH_REFRIGERATORS': 'Counter-depth refrigerator',
    'FREEZERLESS_REFRIGERATORS': 'Freezerless refrigerator',
    'DRAWER_REFRIGERATORS': 'Drawer refrigerator',
}


def _category_fallback_ref_type(categories):
    """Map categories dict to human-readable type (fallback when Appliance Type spec is absent)."""
    if not isinstance(categories, dict):
        return ''
    for code in categories.values():
        if not isinstance(code, str):
            continue
        if code in CATEGORY_TO_REF_TYPE:
            return CATEGORY_TO_REF_TYPE[code]
    for code in categories.values():
        if isinstance(code, str) and 'REFRIGERATOR' in code:
            words = code.replace('_', ' ').split()
            words = [w for w in words if w.upper() != 'REFRIGERATORS']
            base = ' '.join(w.title() for w in words).strip()
            return f'{base} refrigerator' if base else 'Refrigerator'
    return ''


def _format_lead_date(itm_ld_tm):
    """Parse '06-04-2026-05:00 UTC' → 'Thu, Jun 4'. Returns '' on failure."""
    if not itm_ld_tm:
        return ''
    text = str(itm_ld_tm).split(' ')[0]  # 06-04-2026-05:00
    try:
        dt = datetime.strptime(text, '%m-%d-%Y-%H:%M')
    except Exception:
        return ''
    return dt.strftime('%a, %b ') + str(dt.day)


def _slot_text(slot, when_pickup='Pickup Ready by', when_delivery='Shipping'):
    """Build human-readable availability text or '' when unavailable/missing."""
    if not isinstance(slot, dict) or not slot:
        return ''
    if not slot.get('isAvlSts'):
        return ''
    ftype = (slot.get('fulfillmentType') or '').upper().replace('_', '').replace(' ', '')
    days = slot.get('itmLdTmDays')
    date = _format_lead_date(slot.get('itmLdTm'))
    is_fast = ftype in {'EXPEDITEDDELIVERY', 'FASTTRUCK', 'FAST'}

    if is_fast:
        if isinstance(days, (int, float)):
            if days <= 0:
                return 'Get it Today'
            if days == 1:
                return 'Get it Tomorrow'
            if date:
                return f'Get it by {date}'
        if slot.get('leadtimeToday') is True:
            return 'Get it Today'
        if date:
            return f'Get it by {date}'
        return 'Get it Today'

    if ftype == 'PICKUP':
        if isinstance(days, (int, float)):
            if days <= 0:
                return 'Pickup Ready Today'
            if days == 1:
                return 'Pickup Ready Tomorrow'
        if date:
            return f'{when_pickup} {date}'
        return ''
    if ftype == 'DELIVERY':
        if isinstance(days, (int, float)):
            if days <= 0:
                return 'Shipping Today'
            if days == 1:
                return 'Shipping Tomorrow'
        if date:
            return f'{when_delivery} {date}'
        return ''
    if date:
        return date
    return ''


def _slot_qty(slot):
    """Return availableQuantity if > 0 (page would render it), else '' -> NULL in DB."""
    if not isinstance(slot, dict) or not slot:
        return ''
    if not slot.get('isAvlSts'):
        return ''
    qty = slot.get('availableQuantity')
    if qty is None:
        return ''
    try:
        if int(qty) <= 0:
            return ''
    except (TypeError, ValueError):
        return ''
    return qty


def _fastest_slot(pickup, truck, fast):
    """Pick the soonest available slot among the 3."""
    candidates = []
    for s in (fast, pickup, truck):
        if isinstance(s, dict) and s.get('isAvlSts') and s.get('itmLdTmDays') is not None:
            candidates.append(s)
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.get('itmLdTmDays') or 999)


def parse_productdetail(sku, body):
    out = {}
    try:
        obj = json.loads(body)
    except Exception:
        return out
    pd_root = obj.get('productDetails', {}) or {}
    node = pd_root.get(sku) or pd_root.get(str(sku)) or {}
    if not isinstance(node, dict):
        return out
    product = node.get('product', {}) or {}
    out['sku'] = product.get('modelId', '')
    out['detail_product_brand'] = product.get('brand', '')
    out['detail_product_description'] = product.get('description', '')

    spm = node.get('socialProofingMessages', {}) or {}
    out['number_of_units_purchased_past_week'] = purchased_units_phrase(spm.get('socialProofingMessage', ''))

    inv = (node.get('itemInventory', {}) or {}).get('analyticsData', {}) or {}
    pickup = inv.get('pickup', {}) or {}
    truck = inv.get('truck', {}) or {}
    expedited = inv.get('expeditedDelivery', {}) or {}
    fast_truck = inv.get('fastTruck', {}) or {}
    if expedited.get('isAvlSts'):
        fast = expedited
    elif fast_truck.get('isAvlSts'):
        fast = fast_truck
    else:
        fast = expedited or fast_truck or {}

    out['available_quantity_for_purchase_pickup'] = _slot_qty(pickup)
    out['available_quantity_for_purchase_delivery'] = _slot_qty(truck)
    # fastdelivery quantity is NOT shown on the page -> always null
    out['available_quantity_for_purchase_fastdelivery'] = ''

    major_appliance = bool(product.get('majorAppliance'))

    out['pick_up_availability'] = _slot_text(pickup)
    # Delivery: if no shipping date, fall back to "w/FREE Installation" for major appliances
    delivery_text = _slot_text(truck)
    if not delivery_text and truck.get('isAvlSts') and major_appliance:
        delivery_text = 'w/FREE Installation'
    out['delivery_availability'] = delivery_text
    out['fastest_delivery'] = _slot_text(fast)

    specs = product.get('specs', []) or []
    appliance_type = ''
    capacity_overall = ''
    capacity_refrigerator = ''
    capacity_freezer = ''
    load_type = ''
    washer_capacity = ''
    washer_dryer_capacity = ''
    for s in specs:
        key = (s.get('key') or '').strip()
        val = s.get('value', '')
        key_norm = ' '.join(
            key.lower()
            .replace('/', ' ')
            .replace('&', ' and ')
            .replace('-', ' ')
            .split()
        )
        if key == 'Appliance Type':
            appliance_type = val
        elif key.startswith('Overall Capacity'):
            capacity_overall = val
        elif key.startswith('Refrigerator Capacity'):
            capacity_refrigerator = val
        elif key.startswith('Freezer Capacity'):
            capacity_freezer = val
        elif key == 'Washer Load Type':
            load_type = val
        elif key.startswith('Washer Capacity'):
            washer_capacity = val
        elif (
            key_norm.startswith('washer dryer capacity')
            or key_norm.startswith('washer and dryer capacity')
        ):
            washer_dryer_capacity = val

    product_kind = (lowes_product_type() or 'ref').upper()
    if product_kind == 'LDY':
        ldy_capacity = washer_capacity or washer_dryer_capacity
        out['ldy_loading_type'] = load_type or appliance_type
        out['ldy_capacity'] = f'{ldy_capacity} Cu.Feet' if ldy_capacity else ''
    else:
        ref_type = appliance_type or _category_fallback_ref_type(product.get('categories', {}))
        out['ref_refrigerator_type'] = ref_type
        ref_cap = capacity_overall or capacity_refrigerator or capacity_freezer
        out['ref_capacity'] = f'{ref_cap} Cu.Feet' if ref_cap else ''

    ratings = (obj.get('ratings', {}) or {}).get(sku) or {}
    if ratings:
        out['_pdp_rating'] = ratings.get('rating', '')
        out['_pdp_reviewCount'] = ratings.get('reviewCount', '')

    mfe = (node.get('mfePrice', {}) or {}).get('price', {}) or {}
    additional = (mfe.get('additionalData') or {}) if isinstance(mfe, dict) else {}
    savings_obj = (mfe.get('savings') or {}) if isinstance(mfe, dict) else {}
    out['_detail_selling_price'] = additional.get('sellingPrice', '')
    out['_detail_was_price'] = additional.get('wasPrice', '')
    out['_detail_total_saving'] = savings_obj.get('totalSaving', '')
    out['_detail_total_percentage'] = savings_obj.get('totalPercentage', '')
    out['_detail_display_type'] = additional.get('displayType', '')

    return out


def numeric_or_none(value):
    if value in (None, ''):
        return None
    try:
        return float(str(value).replace(',', '').strip())
    except Exception:
        return None


def format_recommendation_intent(stats):
    if not isinstance(stats, dict):
        return ''

    explicit_pct = numeric_or_none(stats.get('recommendationPercentage'))
    if explicit_pct is not None:
        return f'{round(explicit_pct)}% Recommend this product'

    rec = numeric_or_none(stats.get('totalRecommendedCount'))
    if rec is None:
        rec = numeric_or_none(stats.get('recommendedCount'))
    nrec = numeric_or_none(stats.get('notRecommendedCount')) or 0
    total_reviews = numeric_or_none(stats.get('totalReviewCount')) or 0

    if rec is None:
        return ''

    denominator = rec + nrec
    if denominator > 0:
        pct = round(rec / denominator * 100)
        return f'{pct}% Recommend this product'
    if rec == 0 and total_reviews > 0:
        return '0% Recommend this product'
    return ''


def parse_reviews(sku, *review_bodies):
    out = {}
    review_texts = []
    review_summary = ''
    rec_intent = ''
    p1_obj = None
    try:
        p1_obj = json.loads(review_bodies[0]) if review_bodies and review_bodies[0] else None
    except Exception:
        p1_obj = None
    if isinstance(p1_obj, dict):
        review_summary = p1_obj.get('reviewSummary', '') or ''
        stats = p1_obj.get('reviewStatistics', {}) or {}
        rec_intent = format_recommendation_intent(stats)
        # detail-side fallback for star_rating / review counts (NULL vs 0 분간용)
        out['_pdp_average_rating'] = stats.get('averageOverallRating', '')
        out['_pdp_total_reviews'] = stats.get('totalReviewCount', '')
        for r in (p1_obj.get('results') or []):
            t = review_output_text(r)
            if t:
                review_texts.append(t)
    for body in review_bodies[1:]:
        try:
            obj = json.loads(body) if body else None
        except Exception:
            obj = None
        if not isinstance(obj, dict):
            continue
        for r in (obj.get('results') or []):
            t = review_output_text(r)
            if t:
                review_texts.append(t)
                if REVIEW_TARGET and len(review_texts) >= REVIEW_TARGET:
                    break
        if REVIEW_TARGET and len(review_texts) >= REVIEW_TARGET:
            break

    out['recommendation_intent'] = rec_intent
    out['summarized_review_content'] = review_summary
    formatted = [f'review{i + 1} - {t}' for i, t in enumerate(review_texts[:20])]
    out['detailed_review_content'] = ' ||| '.join(formatted)
    return out


def parse_compare(sku, body):
    out = {'retailer_sku_name_similar': ''}
    try:
        obj = json.loads(body) if body else None
    except Exception:
        return out
    if not isinstance(obj, dict):
        return out
    rr = obj.get('recommendationResponse') or []
    if not rr:
        return out
    products = rr[0].get('products') if isinstance(rr[0], dict) else []
    descs = []
    for p in products or []:
        if str(p.get('omniItemId')) == str(sku):
            continue
        d = retailer_sku_name_text(p)
        if d:
            descs.append(d)
    out['retailer_sku_name_similar'] = ' ||| '.join(descs)
    return out


def save_raw_artifacts(rank, sku, responses, success):
    name = f'{rank:03d}_{sku}_{"success" if success else "fail"}'
    folder = RAW_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    for label, r in responses.items():
        body = (r or {}).get('body', '') or ''
        body = redact_sensitive(body)
        (folder / f'{label}.json').write_text(body[:500000], encoding='utf-8', errors='replace')
    meta = {label: {k: v for k, v in (r or {}).items() if k != 'body'} for label, r in responses.items()}
    (folder / 'meta.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')


OK_STATUSES = {200, 204, 206}


def status_ok(r):
    if not isinstance(r, dict):
        return False
    s = r.get('status')
    return s in OK_STATUSES


def has_body(r):
    return isinstance(r, dict) and r.get('status') == 200 and bool(r.get('body'))


def review_response_labels(responses):
    return sorted(
        [key for key in responses if key.startswith('reviews_p')],
        key=lambda key: int(key.replace('reviews_p', '') or '0'),
    )


def reviews_success(responses):
    labels = review_response_labels(responses)
    if not labels:
        return False
    # Keep the original baseline strictness for the first two review pages.
    # Supplemental pages are best-effort so a late pagination hiccup does not drop the SKU.
    required_labels = labels[:2]
    return all((responses.get(label) or {}).get('status') == 200 for label in required_labels)


def build_row(src, sku, responses, serving_store=None):
    row = dict(src)
    row['omni_item_id'] = sku
    if has_body(responses.get('productdetail')):
        row.update(parse_productdetail(sku, responses['productdetail']['body']))
    review_bodies = [
        responses.get(label, {}).get('body', '') if has_body(responses.get(label)) else ''
        for label in review_response_labels(responses)
    ]
    row.update(parse_reviews(sku, *review_bodies))
    if has_body(responses.get('compare')):
        row.update(parse_compare(sku, responses['compare']['body']))
    else:
        row['retailer_sku_name_similar'] = ''
    row['detail_fetch_at'] = now_iso()
    statuses = {k: v.get('status') for k, v in responses.items()}
    row['detail_xhr_status'] = json.dumps(statuses)
    if serving_store is not None:
        row['serving_store'] = serving_store
    return row


def write_csv(path, rows, preferred=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            f.write('\n')
        return
    keys = set()
    for r in rows:
        keys.update(r.keys())
    preferred = preferred or []
    fieldnames = [k for k in preferred if k in keys] + sorted(keys - set(preferred))
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def main():
    targets = read_input_rows()
    print(f'detail targets: {len(targets)}  input={INPUT_CSV}')
    if not targets:
        print('!! no targets')
        return

    category_id, parent_category = category_codes()
    print(f'category_id={category_id} parent_category={parent_category} store={STORE_FMT}')

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    started_at = now_iso()
    overall_t0 = time.time()

    driver = launch_driver()
    seed_elapsed = 0
    success_rows = []
    failure_rows = []
    alt_pending = []  # productdetail-failed candidates for alt-store retry: (rank, src, sku)
    alt_recovered_count = 0
    alt_still_failed_count = 0
    try:
        seed_elapsed = seed_session(driver)
        for i, src in enumerate(targets, 1):
            sku = (src.get('omni_item_id') or src.get('item_number') or '').strip()
            if not sku:
                continue
            t0 = time.time()
            responses = fetch_sku(driver, sku, category_id, parent_category)
            elapsed = round(time.time() - t0, 2)
            statuses = {k: v.get('status') for k, v in responses.items()}
            success = (
                statuses.get('productdetail') == 200
                and reviews_success(responses)
                and statuses.get('compare') in OK_STATUSES
            )
            save_raw_artifacts(i, sku, responses, success)
            if success:
                success_rows.append(build_row(src, sku, responses, serving_store=STORE_FMT))
                print(f'[{i:>3}/{len(targets)}] sku={sku} OK  elapsed={elapsed}s')
            else:
                failure_rows.append({
                    'rank': i, 'omni_item_id': sku,
                    'statuses': json.dumps(statuses),
                    'elapsed_seconds': elapsed,
                })
                print(f'[{i:>3}/{len(targets)}] sku={sku} FAIL {statuses}  elapsed={elapsed}s')
                # Queue for alt-store retry only when productdetail itself failed
                # (other endpoint failures are unrelated to store routing).
                if ALT_STORE_ENABLED and statuses.get('productdetail') != 200:
                    alt_pending.append((i, src, sku))
            if SLEEP_BETWEEN > 0:
                time.sleep(SLEEP_BETWEEN)

        # ---- Alt-store fallback pass ----
        if ALT_STORE_ENABLED and alt_pending:
            print('-' * 80)
            print(f'[alt] productdetail-failed candidates for retry: {len(alt_pending)}  alt_store={ALT_STORE_FMT}')
            reseed_for_alt(driver)
            for ai, (orig_rank, src, sku) in enumerate(alt_pending, 1):
                t0 = time.time()
                responses = fetch_sku(driver, sku, category_id, parent_category, store=ALT_STORE_FMT)
                elapsed = round(time.time() - t0, 2)
                statuses = {k: v.get('status') for k, v in responses.items()}
                success = (
                    statuses.get('productdetail') == 200
                    and reviews_success(responses)
                    and statuses.get('compare') in OK_STATUSES
                )
                # save under alt_ prefix so primary fail marker is preserved
                alt_name = f'alt_{orig_rank:03d}_{sku}_{"success" if success else "fail"}'
                alt_folder = RAW_DIR / alt_name
                alt_folder.mkdir(parents=True, exist_ok=True)
                for label, r in responses.items():
                    body = (r or {}).get('body', '') or ''
                    body = redact_sensitive(body)
                    (alt_folder / f'{label}.json').write_text(body[:500000], encoding='utf-8', errors='replace')
                meta = {label: {k: v for k, v in (r or {}).items() if k != 'body'} for label, r in responses.items()}
                (alt_folder / 'meta.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')

                if success:
                    success_rows.append(build_row(src, sku, responses, serving_store=ALT_STORE_FMT))
                    # remove from failure_rows (matched by rank+sku)
                    failure_rows = [f for f in failure_rows if not (f.get('rank') == orig_rank and f.get('omni_item_id') == sku)]
                    alt_recovered_count += 1
                    print(f'[alt {ai:>2}/{len(alt_pending)}] sku={sku} OK (rank {orig_rank})  elapsed={elapsed}s')
                else:
                    alt_still_failed_count += 1
                    print(f'[alt {ai:>2}/{len(alt_pending)}] sku={sku} STILL FAIL {statuses}  elapsed={elapsed}s')
                if SLEEP_BETWEEN > 0:
                    time.sleep(SLEEP_BETWEEN)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    overall_elapsed = round(time.time() - overall_t0, 2)

    write_csv(
        DETAIL_CSV,
        success_rows,
        preferred=[
            'omni_item_id', 'sku', 'brand', 'detail_product_brand',
            'serving_store',
            'number_of_units_purchased_past_week',
            'pick_up_availability', 'delivery_availability', 'fastest_delivery',
            'available_quantity_for_purchase_pickup', 'available_quantity_for_purchase_delivery', 'available_quantity_for_purchase_fastdelivery',
            'ref_refrigerator_type', 'ref_capacity',
            'ldy_loading_type', 'ldy_capacity',
            'recommendation_intent', 'summarized_review_content', 'detailed_review_content',
            'retailer_sku_name_similar',
        ],
    )
    write_csv(FAILURES_CSV, failure_rows, preferred=['rank', 'omni_item_id', 'statuses', 'elapsed_seconds'])
    # NOTE: final_output.csv is produced by step09_finalize (ERD-aligned), NOT here.

    manifest = {
        'run_type': 'step08_uc_xhr',
        'started_at': started_at,
        'finished_at': now_iso(),
        'overall_elapsed_seconds': overall_elapsed,
        'seed_elapsed_seconds': round(seed_elapsed, 2),
        'targets': len(targets),
        'success': len(success_rows),
        'failure': len(failure_rows),
        'input_csv': str(INPUT_CSV),
        'detail_csv': str(DETAIL_CSV),
        'failures_csv': str(FAILURES_CSV),
        'final_csv': str(FINAL_CSV),
        'raw_dir': str(RAW_DIR),
        'limit': LIMIT,
        'headless': HEADLESS,
        'product_type': (lowes_product_type() or 'ref').upper(),
        'category_id': category_id,
        'store_id': STORE_FMT,
        'alt_store_enabled': ALT_STORE_ENABLED,
        'alt_store_id': ALT_STORE_FMT if ALT_STORE_ENABLED else '',
        'alt_pending_count': len(alt_pending),
        'alt_recovered_count': alt_recovered_count,
        'alt_still_failed_count': alt_still_failed_count,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    print('-' * 80)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
