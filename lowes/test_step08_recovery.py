"""Offline endpoint recovery, row retention, SQL NULL and email regressions.

Load function definitions only: no environment files, retailer, DB or mail access.
"""
import ast
import copy
import csv
import json
import re
import shutil
import tempfile
import unittest
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from lowes.test_step08_fulfillment import parser_functions


def functions(filename, namespace):
    path = Path(__file__).with_name(filename)
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def response(obj, status=200):
    return {'status': status, 'body': json.dumps(obj)}


def product(sku):
    return response({'productDetails': {sku: {'product': {'modelId': 'MODEL', 'specs': []}}}})


def reviews(total=1, count=1):
    return response({'results': [{'reviewText': f'text {i}'} for i in range(count)],
                     'totalResults': total, 'reviewSummary': 'Summary',
                     'reviewStatistics': {'averageOverallRating': 4.5, 'totalReviewCount': total,
                                          'recommendationPercentage': 90}})


def collector(category='LDY'):
    ns = functions('step08_uc_xhr.py', parser_functions(category))
    ns.update(re=re, csv=csv, REVIEW_TARGET=20, REVIEW_PAGE_SIZE=10, REVIEW_MAX_OFFSET=100,
              REVIEW_EMPTY_TEXT='No review text provided', OK_STATUSES={200, 204, 206},
              CATEGORY_TO_REF_TYPE={},
              PURCHASED_UNITS_RE=re.compile(r'\d+ bought'),
              REF_CAPACITY_FROM_DESCRIPTION_RE=re.compile(r'(\d+) cu ft'),
              retailer_sku_name_text=lambda row: row.get('description', ''),
              print=lambda *a, **k: None)
    ns['collect_fulfillment_display'] = lambda *a, **k: response({
        'status': 'ok', 'values': {'delivery_availability': 'Delivery Tomorrow'}})
    return ns


@contextmanager
def scratch_directory():
    # Use inherited Windows ACLs; Python 3.13's mode=0700 temp dirs can be
    # inaccessible to the sandbox identity.
    base = Path(tempfile.gettempdir()).resolve()
    path = base / ('lowes-recovery-test-' + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        assert path.resolve().parent == base and path.name.startswith('lowes-recovery-test-')
        shutil.rmtree(path)


def finalizer(category):
    schema = ast.parse(Path(__file__).with_name('step00_erd_schema.py').read_text(encoding='utf-8-sig'))
    lists = {n.targets[0].id: ast.literal_eval(n.value) for n in schema.body
             if isinstance(n, ast.Assign) and isinstance(n.value, ast.List)}
    fields = list(dict.fromkeys(lists['CORE_OUTPUT_COLUMNS'] + lists[category + '_ERD_COLUMNS']
                               + lists['COMMON_ERD_COLUMNS'] + lists['LEGACY_COMPAT_COLUMNS']))
    return functions('step09_finalize.py', {
        'datetime': datetime, 'FIELDNAMES': fields, 'PRODUCT_TYPE': category,
        'CATEGORY_COLUMNS': ['ldy_capacity', 'ldy_loading_type'] if category == 'LDY'
        else ['ref_capacity', 'ref_refrigerator_type'],
        'output_page_type': lambda row: 'main',
        'retailer_sku_name_text': lambda row: row.get('description', ''),
    })


class RecoveryTests(unittest.TestCase):
    def test_ge_reviews_and_compare_fail_twice_keep_row_prices_and_ranks(self):
        for category in ('LDY', 'REF'):
            with self.subTest(category=category):
                ns = collector(category)
                calls = Counter()
                def get(driver, path):
                    label = 'productdetail' if path.startswith('/wpd/') else 'reviews'
                    calls[label] += 1
                    return product('5014905209') if label == 'productdetail' else {'status': 'err', 'error': 'Failed to fetch'}
                def post(*args):
                    calls['compare'] += 1
                    return {'status': 'err', 'error': 'Failed to fetch'}
                ns.update(run_xhr_get=get, run_xhr_post=post)
                results = ns['fetch_sku'](None, '5014905209', 'category', 'parent')
                self.assertEqual(calls, {'productdetail': 1, 'reviews': 2, 'compare': 2})
                source = {'main_rank': '2', 'bsr_rank': '2', 'selling_price': '599',
                          'was_price': '849', 'total_saving': '250', 'rating': '4.4', 'review_count': '10410'}
                row = ns['build_row'](source, '5014905209', results)
                final = finalizer(category)['finalize_row'](row, 'batch', '2026-09-25')
                self.assertEqual((final['main_rank'], final['bsr_rank']), ('2', '2'))
                self.assertEqual((final['final_sku_price'], final['original_sku_price'], final['savings']),
                                 ('$599', '$849', '$250'))
                self.assertEqual((final['star_rating'], final['count_of_reviews']), ('4.4', '10410'))
                columns = ['detailed_review_content', 'recommendation_intent',
                           'summarized_review_content', 'retailer_sku_name_similar']
                self.assertTrue(all(final[c] == '' for c in columns))
                db = functions('step14_db_load.py', {
                    'datetime': datetime, 'PRODUCT_TYPE': category, 'INT_COLUMNS': {'main_rank', 'bsr_rank'},
                    'output_page_type': lambda row: 'main', 'retailer_sku_name_text': lambda row: '',
                })
                mapped = db['map_row'](final)
                self.assertTrue(all(db['empty_to_none'](mapped[c], c) is None for c in columns))
                warnings = ns['detail_warnings'](2, '5014905209', results, row)
                self.assertEqual({c for w in warnings for c in w['null_columns']}, set(columns))
                mail = functions('step15_email_notify.py', {})
                issues = mail['detail_warning_issues']({'warnings': warnings})
                self.assertEqual(len(issues), 2)
                self.assertTrue(all('5014905209' in x and '총 2회' in x and 'NULL 컬럼' in x for x in issues))

    def test_successful_retry_removes_warning_and_populates_compare(self):
        ns = collector()
        attempts = iter([{'status': 429}, response({'recommendationResponse': [
            {'products': [{'omniItemId': 'other', 'description': 'Other product'}]}]})])
        result = ns['request_with_retry'](lambda: next(attempts), 'compare', 'sample')
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['problem'], '')
        row = ns['build_row']({}, 'sample', {'compare': result})
        self.assertEqual(row['retailer_sku_name_similar'], 'Other product')
        self.assertEqual(ns['detail_warnings'](1, 'sample', {'compare': result}, row), [])

    def test_late_review_failure_retries_only_page_and_keeps_summary(self):
        ns = collector()
        paths = []
        def get(driver, path):
            paths.append(path)
            return {'status': 503} if 'offset=20' in path else reviews(total=25, count=10)
        ns.update(run_xhr_get=get, REVIEW_TARGET=25)
        results = ns['fetch_reviews_until_target'](None, 'sample')
        self.assertEqual(len(paths), 4)
        self.assertEqual(paths[-1], paths[-2])
        row = ns['build_row']({}, 'sample', results)
        self.assertEqual(row['detailed_review_content'], '')
        self.assertEqual(row['summarized_review_content'], 'Summary')
        self.assertEqual(row['recommendation_intent'], '90% Recommend this product')
        self.assertEqual(ns['detail_warnings'](1, 'sample', results, row)[0]['null_columns'],
                         ['detailed_review_content'])

    def test_recovered_review_page_continues_pagination(self):
        ns = collector()
        replies = iter([{'status': 'err'}, reviews(15, 10), reviews(15, 5)])
        paths = []
        def get(driver, path):
            paths.append(path)
            return next(replies)
        ns['run_xhr_get'] = get
        results = ns['fetch_reviews_until_target'](None, 'sample')
        self.assertEqual(len(paths), 3)
        self.assertEqual(paths[0], paths[1])
        self.assertIn('offset=10', paths[2])
        self.assertTrue(ns['reviews_success'](results))

    def test_short_review_page_retries_and_remains_null(self):
        ns = collector()
        replies = iter([reviews(15, 10), reviews(15, 4), reviews(15, 4)])
        ns['run_xhr_get'] = lambda *a: next(replies)
        results = ns['fetch_reviews_until_target'](None, 'sample')
        self.assertEqual(results['reviews_p2']['problem'], 'incomplete_review_page')
        self.assertEqual(results['reviews_p2']['attempts'], 2)
        self.assertEqual(ns['build_row']({}, 'sample', results)['detailed_review_content'], '')

    def test_short_first_review_page_preserves_successful_summary_and_statistics(self):
        ns = collector()
        calls = []
        ns['run_xhr_get'] = lambda *a: calls.append(1) or reviews(5, 4)
        results = ns['fetch_reviews_until_target'](None, 'sample')
        self.assertEqual(len(calls), 2)
        row = ns['build_row']({}, 'sample', results)
        self.assertEqual(row['detailed_review_content'], '')
        self.assertEqual(row['summarized_review_content'], 'Summary')
        self.assertEqual(row['_pdp_total_reviews'], 5)
        self.assertEqual(row['_pdp_average_rating'], 4.5)
        self.assertEqual(ns['detail_warnings'](1, 'sample', results, row)[0]['null_columns'],
                         ['detailed_review_content'])

    def test_empty_results_are_valid_but_null_and_malformed_payloads_retry(self):
        ns = collector()
        for label, valid in [('reviews_p1', reviews(0, 0)), ('compare', response({'recommendationResponse': []})),
                             ('compare', {'status': 204})]:
            calls = []
            result = ns['request_with_retry'](lambda: calls.append(1) or valid, label, 'sample')
            self.assertEqual(len(calls), 1)
            self.assertEqual(result['problem'], '')
        for label, invalid in [('reviews_p1', response({'results': None})),
                               ('compare', response({'recommendationResponse': None})),
                               ('compare', {'status': 200, 'body': '<html>blocked</html>'}),
                               ('productdetail', response({'productDetails': {}}))]:
            calls = []
            result = ns['request_with_retry'](lambda: calls.append(1) or invalid, label, 'sample')
            self.assertEqual(len(calls), 2)
            self.assertTrue(result['problem'])

    def test_productdetail_retry_preserves_successful_independent_requests(self):
        ns = collector()
        counts = Counter()
        def get(driver, path):
            if path.startswith('/wpd/'):
                counts['productdetail'] += 1
                return {'status': 403} if counts['productdetail'] == 1 else product('sample')
            counts['reviews'] += 1
            return reviews()
        def post(*args):
            counts['compare'] += 1
            return response({'recommendationResponse': []})
        ns.update(run_xhr_get=get, run_xhr_post=post)
        first = ns['fetch_sku'](None, 'sample', 'category', 'parent')
        snapshot = copy.deepcopy(first)
        second = ns['fetch_sku'](None, 'sample', 'category', 'parent', store='1854', previous=first)
        self.assertEqual(first, snapshot)
        self.assertEqual(counts, {'productdetail': 2, 'reviews': 1, 'compare': 1})
        self.assertEqual(second['productdetail']['attempts'], 2)
        self.assertEqual(second['productdetail']['problem'], '')
        self.assertEqual(second['productdetail']['request_context']['store'], '1854')

    def test_main_retains_all_targets_including_failed_detail_and_writes_warning_manifest(self):
        for alt_enabled in (True, False):
            with self.subTest(alt_enabled=alt_enabled), scratch_directory() as temp:
                root = Path(temp)
                ns = collector()
                rows = [{'omni_item_id': 'ok', 'main_rank': '1'},
                        {'omni_item_id': 'failed', 'main_rank': '2', 'bsr_rank': '2', 'selling_price': '599'}]
                counts = Counter()
                def get(driver, path):
                    counts[path] += 1
                    if path.startswith('/wpd/'):
                        sku = path.split('/')[2]
                        return {'status': 403} if sku == 'failed' else product(sku)
                    return reviews()
                ns.update(read_input_rows=lambda: rows, category_codes=lambda: ('category', 'parent'),
                          INPUT_CSV=root / 'targets.csv', RAW_DIR=root / 'raw', DETAIL_CSV=root / 'detail.csv',
                          FAILURES_CSV=root / 'failures.csv', FINAL_CSV=root / 'final.csv',
                          MANIFEST_PATH=root / 'manifest.json', LIMIT=0, HEADLESS=True,
                          ALT_STORE_ENABLED=alt_enabled, ALT_STORE_FMT='1854', SLEEP_BETWEEN=0,
                          time=SimpleNamespace(time=lambda: 0, sleep=lambda n: None),
                          launch_driver=lambda: SimpleNamespace(quit=lambda: None), seed_session=lambda d: 0,
                          reseed_for_alt=lambda d: None, redact_sensitive=lambda s: s,
                          run_xhr_get=get, run_xhr_post=lambda *a: response({'recommendationResponse': []}))
                ns['main']()
                manifest = json.loads(ns['MANIFEST_PATH'].read_text())
                with ns['DETAIL_CSV'].open(encoding='utf-8-sig', newline='') as f:
                    output = list(csv.DictReader(f))
                self.assertEqual(len(output), 2)
                self.assertEqual(manifest['output_rows'], 2)
                self.assertEqual(manifest['success'], 1)
                self.assertEqual(manifest['warning_sku_count'], 1)
                self.assertEqual(manifest['failure'], 0)
                self.assertEqual(sum(n for path, n in counts.items() if '/wpd/failed/' in path), 2)
                self.assertEqual(output[1]['selling_price'], '599')
                self.assertEqual(output[1]['main_rank'], '2')
                self.assertEqual(output[1]['detailed_review_content'], 'review1 - text 0')
                self.assertEqual(manifest['warnings'][0]['attempts'], 2)
                mail = functions('step15_email_notify.py', {'json': json, 'csv': csv, 'Path': Path, 'KRW_PER_USD': 1550})
                # detail_summary uses the run-root layout, so feed that same manifest.
                (root / 'detail').mkdir()
                (root / 'detail' / 'manifest_uc_xhr.json').write_text(json.dumps(manifest))
                detail = mail['detail_summary'](root)
                self.assertEqual(detail['warning_sku_count'], 1)
                issues = mail['detail_warning_issues'](detail)
                body = mail['build_body'](2, 0, [], detail, {'table': 'test', 'inserted': 2}, issues)
                self.assertIn('SKU failed', body)
                self.assertIn('NULL', body)


if __name__ == '__main__':
    unittest.main()
