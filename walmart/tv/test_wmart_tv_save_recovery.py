"""Offline tests: real crawler methods, simulated DB/network failures, no config.

python -B -m unittest walmart.tv.test_wmart_tv_save_recovery -v
"""
import ast
import contextlib
import copy
import io
import json
import random
import re
import tempfile
import time
import traceback
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unittest.mock import patch

import psycopg2

from walmart.tv import wmart_tv_next_data as next_data
from walmart.tv import wmart_tv_save_recovery as recovery
from walmart.tv.wmart_tv_replay_saves import replay_batch


def load_crawler_namespace(filename, extra=None):
    """Load production classes without importing config or starting a browser."""
    path = Path(__file__).with_name(filename)
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    namespace = dict(vars(next_data))
    namespace.update(globals())
    namespace.update({'WalmartBaseCrawler': object, '__file__': str(path)})
    namespace.update(extra or {})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


DETAIL_NAMESPACE = load_crawler_namespace('wmart_tv_dt.py')
Crawler = DETAIL_NAMESPACE['WalmartTVDetailCrawler']
UpdateCrawler = load_crawler_namespace(
    'wmart_tv_dt_update.py', {'WalmartTVDetailCrawler': Crawler},
)['WalmartTVDetailUpdateCrawler']


def product(number):
    return {
        'item': str(number), 'product_url': f'https://www.walmart.com/ip/TV/{number}',
        'retailer_sku_name': f'Test TV {number}', 'final_sku_price': '$208.00',
        'count_of_reviews': '1', 'count_of_star_ratings': '2', 'star_rating': '4.5',
        'detailed_review_content': 'review1 - Original review', 'sku': 'KEEP-SKU',
        'screen_size': '43 inches', 'model_year': '2026',
        'page_type': 'main', 'main_rank': number, 'bsr_rank': number,
        '_save_crawl_datetime': '2026-09-22 00:10:00',
    }


class Database:
    def __init__(self):
        self.rows = []
        self.masters = {}
        self.failures = []
        self.events = []
        self.connects = 0
        self.reject_connections = 0

    def fail(self, stage, item, count=1, error=None):
        self.failures.append([stage, str(item), count, error or psycopg2.OperationalError('simulated disconnect')])

    def check(self, stage, item):
        self.events.append((stage, str(item)))
        for failure in self.failures:
            if failure[0:2] == [stage, str(item)] and failure[2] > 0:
                failure[2] -= 1
                raise failure[3]

    def connection(self):
        self.connects += 1
        return Connection(self)


class Connection:
    def __init__(self, db):
        self.db = db
        self.closed = False
        self.changes = []
        self.stage = ''
        self.item = ''

    def cursor(self):
        if self.closed:
            raise psycopg2.InterfaceError('closed')
        return Cursor(self)

    def commit(self):
        self.db.check(f'{self.stage}_commit_before', self.item)
        for kind, value in self.changes:
            if kind == 'master':
                self.db.masters[value['item']] = value
            elif kind == 'retail':
                self.db.rows.append(value)
            elif kind == 'update':
                self.db.rows[value['id'] - 1].update(value)
        self.changes.clear()
        self.db.check(f'{self.stage}_commit_after', self.item)

    def rollback(self):
        self.changes.clear()

    def close(self):
        self.closed = True


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = []
        self.rowcount = 1

    def execute(self, query, params=None):
        conn, db = self.connection, self.connection.db
        sql = ' '.join(query.split())
        self.result = []
        if sql.startswith('SELECT sku, screen_size'):
            conn.item = str(params[0]); conn.stage = 'master'
            db.check('master', conn.item)
            master = db.masters.get(conn.item)
            if master:
                self.result = [(master['sku'], master['screen_size'])]
        elif sql.startswith('INSERT INTO tv_item_mst'):
            conn.changes.append(('master', dict(zip(
                ['item', 'account_name', 'sku', 'product_url', 'screen_size'], params,
            ))))
        elif ' AS matches FROM ' in sql:
            conn.stage = 'retail'
            columns = re.findall(r'(\w+) IS NOT DISTINCT FROM %s', sql)
            expected = dict(zip(columns, params[:len(columns)]))
            account, batch, identity = params[-3:]
            field = 'product_url' if 'product_url = %s' in sql else 'item'
            conn.item = str(identity).rsplit('/', 1)[-1]
            db.check('retail_check', conn.item)
            self.result = [
                (all(row.get(key) == value for key, value in expected.items()),)
                for row in db.rows
                if row['account_name'] == account and row['batch_id'] == batch
                and row.get(field) == identity
            ][:2]
        elif sql.startswith('INSERT INTO tv_retail_com') or sql.startswith('INSERT INTO test_tv_retail_com'):
            columns = query.split('(', 1)[1].split(')', 1)[0].split(', ')
            row = dict(zip(columns, params))
            conn.item = row['item']; conn.stage = 'retail'
            db.check('retail', conn.item)
            conn.changes.append(('retail', row))
        elif sql.startswith('UPDATE test_tv_retail_com') or sql.startswith('UPDATE tv_retail_com'):
            fields = Crawler.EXTRACTED_FIELDS
            row = dict(zip(fields, params[:len(fields)]))
            row['id'] = params[len(fields)]
            conn.item = row['item']; conn.stage = 'update'
            db.check('update', conn.item)
            self.rowcount = 1 if 0 < row['id'] <= len(db.rows) else 0
            if self.rowcount:
                conn.changes.append(('update', row))
        elif sql.startswith('SELECT model_year'):
            self.result = [('2026',)]
        elif sql.startswith('SET LOCAL') or sql.startswith('SELECT pg_advisory_xact_lock'):
            pass
        else:
            raise AssertionError(f'Unexpected test query: {sql}')

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result

    def close(self):
        pass


class SaveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(patch.object(time, 'sleep'))
        self.db = Database()

    def crawler(self, count=3, update=False):
        cls = UpdateCrawler if update else Crawler
        obj = cls.__new__(cls)
        obj.account_name = 'Walmart'
        obj.batch_id = 'w_20260922_000013'
        obj.test_mode = False
        obj.save_recovery_dir = Path(self.folder.name)
        obj.db_conn = self.db.connection()
        obj.spec_diffs = []
        obj.detail_next_data_chunk_size = 40
        obj.detail_report = dict(
            target_records=0, main_records=0, bsr_records=0, saved_records=0,
            detail_records=0, review_mismatches=[], run_errors=[], redirects=[],
        )
        def connect(**kwargs):
            if self.db.reject_connections:
                self.db.reject_connections -= 1
                return False
            obj.db_conn = self.db.connection()
            return True
        obj.connect_db = connect
        obj.initialize = lambda: True
        obj.load_product_list = lambda: [product(i) for i in range(1, count + 1)]
        def collect(chunk):
            self.db.events.append(('chunk', [i for i, _ in chunk]))
            return {i: copy.deepcopy(row) for i, row in chunk}, {}, {}
        obj._collect_detail_initial_parallel = collect
        return obj

    def test_reconnect_at_each_observed_chunk_boundary_processes_all_later_products(self):
        for failed_item in (1, 41, 81):
            with self.subTest(failed_item=failed_item):
                self.db = Database()
                self.db.fail('master', failed_item)
                crawler = self.crawler(85)
                self.assertTrue(crawler.run())
                self.assertEqual([row['item'] for row in self.db.rows], [str(i) for i in range(1, 86)])
                self.assertEqual(crawler.detail_report['saved_records'], 85)
                self.assertEqual(crawler.detail_report['detail_records'], 85)
                self.assertEqual(crawler.detail_report['run_errors'], [])
                self.assertEqual(self.db.connects, 2)

    def test_exhausted_row_is_recovered_only_after_remaining_products(self):
        self.db.fail('master', 1, 3)
        crawler = self.crawler(45)
        self.assertTrue(crawler.run())
        self.assertEqual([row['item'] for row in self.db.rows], [str(i) for i in range(2, 46)] + ['1'])
        self.assertEqual(crawler.detail_report['saved_records'], 45)
        self.assertEqual(crawler.detail_report['run_errors'], [])
        self.assertEqual(len(list(Path(self.folder.name).glob('*.saved.json'))), 1)
        self.assertEqual(list(Path(self.folder.name).glob('*.pending.json')), [])

    def test_persistent_failure_does_not_stop_remaining_products_and_reports_incomplete(self):
        self.db.fail('master', 1, 6)
        crawler = self.crawler(45)
        self.assertFalse(crawler.run())
        self.assertEqual([row['item'] for row in self.db.rows], [str(i) for i in range(2, 46)])
        self.assertEqual(crawler.detail_report['unsaved_records'], 1)
        snapshots = list(Path(self.folder.name).glob('*.pending.json'))
        self.assertEqual(len(snapshots), 1)
        snapshot = json.loads(snapshots[0].read_text(encoding='utf-8'))
        self.assertEqual(snapshot['product']['final_sku_price'], '$208.00')
        self.assertEqual(snapshot['product']['_save_crawl_datetime'], '2026-09-22 00:10:00')

    def test_retail_commit_response_lost_does_not_duplicate_and_keeps_processing(self):
        self.db.fail('retail_commit_after', 1)
        crawler = self.crawler()
        self.assertTrue(crawler.run())
        self.assertEqual([row['item'] for row in self.db.rows], ['1', '2', '3'])
        self.assertEqual(self.db.rows[0]['crawl_datetime'], '2026-09-22 00:10:00')

    def test_failed_commit_before_write_is_retried_with_same_values(self):
        self.db.fail('retail_commit_before', 1)
        crawler = self.crawler()
        self.assertTrue(crawler.run())
        self.assertEqual([row['item'] for row in self.db.rows], ['1', '2', '3'])
        self.assertEqual(self.db.rows[0]['final_sku_price'], '$208.00')

    def test_master_commit_response_lost_does_not_duplicate_master(self):
        self.db.fail('master_commit_after', 1)
        crawler = self.crawler()
        self.assertTrue(crawler.run())
        self.assertEqual(len(self.db.masters), 3)
        self.assertEqual(len(self.db.rows), 3)

    def test_non_connection_error_is_not_retried_but_next_products_are_saved(self):
        self.db.fail('master', 1, 10, psycopg2.DataError('invalid data'))
        crawler = self.crawler()
        self.assertFalse(crawler.run())
        self.assertEqual(self.db.events.count(('master', '1')), 1)
        self.assertEqual([row['item'] for row in self.db.rows], ['2', '3'])

    def test_reconnect_failure_is_bounded_and_next_product_still_runs(self):
        self.db.fail('master', 1)
        self.db.reject_connections = 2
        crawler = self.crawler()
        self.assertTrue(crawler.run())
        self.assertEqual([row['item'] for row in self.db.rows], ['2', '3', '1'])

    def test_snapshot_disk_failure_does_not_abort_remaining_products(self):
        self.db.fail('master', 1, 6)
        crawler = self.crawler()
        with patch.object(recovery, 'atomic_json', side_effect=OSError('disk full')):
            self.assertFalse(crawler.run())
        self.assertEqual([row['item'] for row in self.db.rows], ['2', '3'])
        self.assertTrue(any(error['stage'] == 'save_recovery_file' for error in crawler.detail_report['run_errors']))

    def test_replay_uses_snapshot_without_refetch_and_is_idempotent(self):
        self.db.fail('retail', 1, 6)
        crawler = self.crawler()
        self.assertFalse(crawler.run())
        factory = lambda kind: self.crawler()
        self.assertTrue(replay_batch(crawler.batch_id, directory=Path(self.folder.name), factory=factory))
        self.assertEqual([row['item'] for row in self.db.rows], ['2', '3', '1'])
        self.assertEqual(self.db.rows[-1]['crawl_datetime'], '2026-09-22 00:10:00')
        self.assertTrue(replay_batch(crawler.batch_id, directory=Path(self.folder.name), factory=factory))
        self.assertEqual(len(self.db.rows), 3)

    def test_replay_does_not_cross_batch_or_test_mode(self):
        self.db.fail('master', 1, 6)
        crawler = self.crawler(1)
        self.assertFalse(crawler.run())
        def forbidden(kind):
            self.fail('Unrelated batch must not connect')
        self.assertTrue(replay_batch('another-batch', directory=Path(self.folder.name), factory=forbidden))
        self.assertTrue(replay_batch(crawler.batch_id, True, Path(self.folder.name), forbidden))
        self.assertEqual(len(list(Path(self.folder.name).glob('*.pending.json'))), 1)

    def test_listing_fallback_failure_continues_and_does_not_write_master(self):
        crawler = self.crawler()
        def collect(chunk):
            return {i: copy.deepcopy(row) for i, row in chunk if i != 1}, {
                1: {'product': product(1), 'reason': 'no_next_data'},
            }, {}
        crawler._collect_detail_initial_parallel = collect
        crawler.collect_detail_zenrows_recovery_parallel = lambda pending, specs: ({}, pending)
        self.db.fail('retail', 1)
        self.assertTrue(crawler.run())
        self.assertEqual([row['item'] for row in self.db.rows], ['2', '3', '1'])
        self.assertNotIn('1', self.db.masters)
        self.assertEqual(crawler.detail_report['detail_records'], 2)

    def test_update_retry_preserves_row_id_and_continues_all_rows(self):
        crawler = self.crawler(update=True)
        self.db.rows = [dict(product(i), id=i) for i in range(1, 4)]
        crawler.load_product_list = lambda: copy.deepcopy(self.db.rows)
        crawler.crawl_detail = lambda row: dict(row)
        crawler.start_logging = lambda *args: None
        crawler.stop_logging = lambda: None
        self.db.fail('update', 1)
        self.assertTrue(crawler.run())
        self.assertEqual(len(self.db.rows), 3)
        self.assertIn(('update', '3'), self.db.events)

    def test_snapshot_only_contains_allowed_product_fields(self):
        crawler = self.crawler()
        row = product(1)
        row['unexpected_metadata'] = 'not a product field'
        recovery.queue_failed_save(crawler, row, 'insert_detail')
        path = next(Path(self.folder.name).glob('*.pending.json'))
        payload = json.loads(path.read_text(encoding='utf-8'))
        self.assertNotIn('unexpected_metadata', payload['product'])

    def test_deferred_detail_save_retry_does_not_skip_other_recovered_products(self):
        crawler = self.crawler()
        pending = {i: {'product': product(i), 'reason': 'no_next_data'} for i in (1, 2)}
        crawler._collect_detail_initial_parallel = lambda chunk: ({3: product(3)}, pending, {})
        crawler.collect_detail_zenrows_recovery_parallel = lambda *args: ({i: product(i) for i in (1, 2)}, {})
        self.db.fail('master', 1)
        self.assertTrue(crawler.run())
        self.assertEqual([row['item'] for row in self.db.rows], ['3', '1', '2'])

    def test_review_mismatch_warning_survives_final_db_recovery(self):
        crawler = self.crawler(1)
        row = product(1)
        row['_review_mismatch'] = {'item': '1', 'expected_review_bodies': 2, 'collected_review_bodies': 1}
        crawler._collect_detail_initial_parallel = lambda chunk: ({1: row}, {}, {})
        self.db.fail('master', 1, 3)
        self.assertTrue(crawler.run())
        self.assertEqual(crawler.detail_report['review_mismatches'], [row['_review_mismatch']])

    def test_corrupt_snapshot_does_not_prevent_other_snapshot_replay(self):
        crawler = self.crawler(1)
        recovery.queue_failed_save(crawler, product(1), 'insert_detail')
        (Path(self.folder.name) / '000.pending.json').write_text('invalid json', encoding='utf-8')
        self.assertFalse(replay_batch(
            crawler.batch_id, directory=Path(self.folder.name), factory=lambda kind: self.crawler(),
        ))
        self.assertEqual([row['item'] for row in self.db.rows], ['1'])

    def test_failed_replay_retains_pending_file_and_saves_later_snapshot(self):
        crawler = self.crawler()
        for i in (1, 2):
            recovery.queue_failed_save(crawler, product(i), 'insert_detail')
        self.db.fail('master', 1, 3)
        self.assertFalse(replay_batch(
            crawler.batch_id, directory=Path(self.folder.name), factory=lambda kind: self.crawler(),
        ))
        self.assertEqual([row['item'] for row in self.db.rows], ['2'])
        self.assertEqual(len(list(Path(self.folder.name).glob('*.pending.json'))), 1)

    def test_missing_update_row_is_failure(self):
        crawler = self.crawler(update=True)
        row = dict(product(1), id=999)
        self.assertFalse(crawler.save_detail_result(row))
        self.assertEqual(self.db.rows, [])

    def test_first_snapshot_survives_later_collection_of_same_batch(self):
        crawler = self.crawler()
        first = product(1)
        recovery.queue_failed_save(crawler, first, 'insert_detail')
        path = next(Path(self.folder.name).glob('*.pending.json'))
        original_bytes = path.read_bytes()
        later = dict(first, final_sku_price='$999.00', _save_crawl_datetime='2026-09-23 12:00:00')
        recovery.queue_failed_save(self.crawler(), later, 'insert_detail')
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['product']['final_sku_price'], '$208.00')

    def test_pending_original_blocks_different_values_before_any_db_write(self):
        crawler = self.crawler()
        recovery.queue_failed_save(crawler, product(1), 'insert_detail')
        later_crawler = self.crawler()
        self.assertFalse(later_crawler.save_detail_result(dict(product(1), final_sku_price='$999.00')))
        self.assertEqual(self.db.events, [])
        self.assertEqual(self.db.rows, [])
        self.assertTrue(later_crawler._last_save_conflict)

    def test_conflicting_existing_row_stays_unchanged_and_pending(self):
        crawler = self.crawler()
        self.assertTrue(crawler.save_detail_result(product(1)))
        self.db.rows[0]['final_sku_price'] = '$999.00'
        self.db.rows[0]['detailed_review_content'] = None
        recovery.queue_failed_save(crawler, product(1), 'insert_detail')
        self.assertFalse(replay_batch(
            crawler.batch_id, directory=Path(self.folder.name), factory=lambda kind: self.crawler(),
        ))
        self.assertEqual(self.db.rows[0]['final_sku_price'], '$999.00')
        self.assertIsNone(self.db.rows[0]['detailed_review_content'])
        self.assertEqual(len(list(Path(self.folder.name).glob('*.pending.json'))), 1)
        self.assertEqual(list(Path(self.folder.name).glob('*.saved.json')), [])

    def test_db_conflict_does_not_skip_following_products(self):
        initial = self.crawler()
        self.assertTrue(initial.save_detail_result(product(1)))
        self.db.rows[0]['star_rating'] = '1.0'
        crawler = self.crawler()
        self.assertFalse(crawler.run())
        self.assertEqual([row['item'] for row in self.db.rows], ['1', '2', '3'])
        self.assertEqual(self.db.rows[0]['star_rating'], '1.0')
        self.assertEqual(crawler.detail_report['saved_records'], 2)

    def test_equal_existing_row_is_verified_without_duplicate(self):
        crawler = self.crawler()
        self.assertTrue(crawler.save_detail_result(product(1)))
        self.assertTrue(crawler.save_detail_result(product(1)))
        self.assertEqual(len(self.db.rows), 1)
        self.assertEqual(self.db.events.count(('retail', '1')), 1)

    def test_duplicate_identity_rows_are_a_conflict_even_when_values_match(self):
        crawler = self.crawler()
        self.assertTrue(crawler.save_detail_result(product(1)))
        self.db.rows.append(copy.deepcopy(self.db.rows[0]))
        self.assertFalse(crawler.save_detail_result(product(1)))
        self.assertEqual(len(self.db.rows), 2)

    def test_archive_failure_is_file_pending_not_db_failure(self):
        crawler = self.crawler()
        recovery.queue_failed_save(crawler, product(1), 'insert_detail')
        with patch.object(recovery.os, 'replace', side_effect=PermissionError('simulated archive failure')):
            self.assertFalse(replay_batch(
                crawler.batch_id, directory=Path(self.folder.name), factory=lambda kind: self.crawler(),
            ))
        self.assertEqual(len(self.db.rows), 1)
        self.assertEqual(len(list(Path(self.folder.name).glob('*.pending.json'))), 1)
        self.assertIn('DB saved: 1, DB failed: 0, File cleanup pending: 1', self.output.getvalue())
        self.assertTrue(replay_batch(
            crawler.batch_id, directory=Path(self.folder.name), factory=lambda kind: self.crawler(),
        ))
        self.assertEqual(len(self.db.rows), 1)
        self.assertEqual(list(Path(self.folder.name).glob('*.pending.json')), [])

    def test_file_cleanup_failure_does_not_stop_remaining_products(self):
        crawler = self.crawler()
        recovery.queue_failed_save(crawler, product(1), 'insert_detail')
        with patch.object(recovery.os, 'replace', side_effect=PermissionError('simulated archive failure')):
            self.assertTrue(crawler.run())
        self.assertEqual([row['item'] for row in self.db.rows], ['1', '2', '3'])
        self.assertEqual(crawler.detail_report['unsaved_records'], 0)
        self.assertEqual(crawler.detail_report['recovery_file_pending'], 1)

    def test_model_year_enrichment_does_not_block_archiving_original_snapshot(self):
        crawler = self.crawler()
        row = dict(product(1), model_year=None)
        recovery.queue_failed_save(crawler, row, 'insert_detail')
        self.assertTrue(crawler.save_detail_result(row))
        self.assertEqual(self.db.rows[0]['model_year'], '2026')
        self.assertTrue(crawler._last_file_cleanup_ok)

    def test_cleanup_success_clears_stale_file_warning(self):
        crawler = self.crawler()
        row = product(1)
        recovery.queue_failed_save(crawler, row, 'insert_detail')
        with patch.object(recovery.os, 'replace', side_effect=PermissionError('simulated archive failure')):
            self.assertTrue(crawler.save_detail_result(row))
        self.assertEqual(crawler.detail_report['recovery_file_pending'], 1)
        self.assertTrue(crawler.save_detail_result(row))
        self.assertEqual(crawler.detail_report['recovery_file_pending'], 0)
        self.assertFalse(any(e['stage'] == 'recovery_file_cleanup' for e in crawler.detail_report['run_errors']))

    def test_atomic_snapshot_publish_never_replaces_existing_bytes(self):
        path = Path(self.folder.name) / 'immutable.json'
        recovery.atomic_json(path, {'first': 1})
        with self.assertRaises(FileExistsError):
            recovery.atomic_json(path, {'later': 2})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'first': 1})
        self.assertEqual(list(Path(self.folder.name).glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
