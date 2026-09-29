"""Bounded DB retries and failed-row snapshots for Walmart TV only.

Snapshots contain allowlisted product fields, never connection settings or HTTP
responses. They are independent of log retention and can be replayed offline
from the retailer (only a DB connection is needed).
"""
import copy
import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg2


SAVE_ATTEMPTS = 3
RETRY_DELAYS = (1, 2)
RECOVERY_ROOT = Path(__file__).resolve().parents[2] / 'recovery' / 'walmart_tv'


class SaveConflictError(Exception):
    """An existing DB row differs from the values we are trying to restore."""


def is_connection_error(error):
    code = getattr(error, 'pgcode', None)
    if code:
        return code.startswith('08') or code in {'57P01', '57P02', '57P03'}
    return isinstance(error, (psycopg2.OperationalError, psycopg2.InterfaceError))


def retry_db_write(crawler, product, operation):
    """Retry one write, never restart/return from the outer product loop."""
    crawler._last_save_retryable = False
    crawler._last_save_conflict = False
    for attempt in range(1, SAVE_ATTEMPTS + 1):
        try:
            connection = getattr(crawler, 'db_conn', None)
            if connection is None or getattr(connection, 'closed', False):
                if not crawler.connect_db(max_retries=1):
                    raise psycopg2.OperationalError('DB connection unavailable')
            return operation(product)
        except Exception as error:
            retryable = is_connection_error(error)
            crawler._last_save_retryable = retryable
            if isinstance(error, SaveConflictError):
                crawler._last_save_conflict = True
                crawler._record_run_error('db_save_conflict', product, 'existing row differs; no overwrite')
            # Exception messages can contain connection details; log types only.
            print(
                f"[DB SAVE ERROR] operation={operation.__name__} "
                f"item={product.get('item') or '-'} attempt={attempt}/{SAVE_ATTEMPTS} "
                f"error={type(error).__name__} sqlstate={getattr(error, 'pgcode', None) or '-'}"
            )
            connection = getattr(crawler, 'db_conn', None)
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
                if retryable:
                    try:
                        connection.close()
                    except Exception:
                        pass
                    crawler.db_conn = None
            if not retryable or attempt == SAVE_ATTEMPTS:
                return False
            time.sleep(RETRY_DELAYS[attempt - 1])
            print(f"[DB SAVE RETRY] item={product.get('item') or '-'}; reconnecting")
    return False


def recovery_key(crawler, product, kind):
    identity = [
        bool(crawler.test_mode), crawler.account_name, crawler.batch_id, kind,
        product.get('product_url'), product.get('item'), product.get('id'),
    ]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()


def recovery_directory(crawler):
    return Path(getattr(crawler, 'save_recovery_dir', RECOVERY_ROOT))


def snapshot_product(crawler, product):
    fields = set(crawler.EXTRACTED_FIELDS + crawler.PASSTHROUGH_FIELDS)
    fields.update({'sku', 'id', '_save_crawl_datetime', '_review_mismatch'})
    return copy.deepcopy({key: value for key, value in product.items() if key in fields})


def json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError('Unsupported product field type')


def atomic_json(path, payload):
    """Publish a complete new snapshot atomically; never replace an older one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, default=json_value)
            stream.flush()
            os.fsync(stream.fileno())
        # Both paths are on the same volume. Linking fails if path already exists,
        # unlike replace(), so a later writer cannot erase the first snapshot.
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def record_snapshot_error(crawler, product, error):
    print(f"[SAVE RECOVERY FILE ERROR] error={type(error).__name__}; continuing remaining products")
    crawler._record_run_error('save_recovery_file', product, type(error).__name__)


def same_snapshot(left, right):
    # JSON round trips convert Decimal/date values but do not change meaning.
    def canonical(row):
        return json.dumps(
            {key: value for key, value in row.items() if value is not None},
            ensure_ascii=True, sort_keys=True, default=json_value,
        )
    return canonical(left) == canonical(right)


def read_snapshot(crawler, path, kind):
    payload = json.loads(path.read_text(encoding='utf-8'))
    if (
        payload.get('version') != 1
        or payload.get('account_name') != crawler.account_name
        or payload.get('batch_id') != crawler.batch_id
        or payload.get('test_mode') is not bool(crawler.test_mode)
        or payload.get('kind') != kind
        or not isinstance(payload.get('product'), dict)
    ):
        raise ValueError('Snapshot scope mismatch')
    row = payload['product']
    if path.name != f'{recovery_key(crawler, row, kind)}.pending.json':
        raise ValueError('Snapshot identity mismatch')
    return row


def snapshot_conflict(crawler, product):
    crawler._last_save_retryable = False
    crawler._last_save_conflict = True
    print(f"[SAVE SNAPSHOT CONFLICT] item={product.get('item') or '-'}; first values retained")
    crawler._record_run_error('save_snapshot_conflict', product, 'first snapshot retained; no overwrite')


def prepare_save(crawler, product, kind):
    """Protect an older pending collection BEFORE any master/detail DB write."""
    crawler._last_file_cleanup_ok = True
    crawler._last_save_conflict = False
    crawler._last_save_retryable = False
    row = snapshot_product(crawler, product)
    key = recovery_key(crawler, row, kind)
    path = recovery_directory(crawler) / f'{key}.pending.json'
    try:
        original = None
        if path.exists():
            original = read_snapshot(crawler, path, kind)
        else:
            entry = getattr(crawler, '_pending_db_saves', {}).get(key)
            if entry:
                original = entry[0]
        if original is not None and not same_snapshot(original, row):
            snapshot_conflict(crawler, product)
            return False
        # DB enrichment may fill a missing model year; archive only the exact
        # input snapshot verified here, never a later collection with this key.
        product['_recovery_source_snapshot'] = row
        return True
    except Exception as error:
        record_snapshot_error(crawler, product, error)
        return False


def queue_failed_save(crawler, product, kind):
    """Keep the row in memory and on disk; disk failure must not stop the run."""
    key = recovery_key(crawler, product, kind)
    row = snapshot_product(crawler, product)
    pending = getattr(crawler, '_pending_db_saves', None)
    if pending is None:
        pending = crawler._pending_db_saves = {}
    entry = pending.setdefault(key, (row, kind, bool(getattr(crawler, '_last_save_retryable', False))))
    row = entry[0]
    payload = {
        'version': 1, 'account_name': crawler.account_name,
        'batch_id': crawler.batch_id, 'test_mode': bool(crawler.test_mode),
        'kind': kind, 'product': row,
    }
    try:
        atomic_json(recovery_directory(crawler) / f'{key}.pending.json', payload)
        print(f"[SAVE QUEUED] item={row.get('item') or '-'}; original values preserved")
    except FileExistsError:
        try:
            original = read_snapshot(crawler, recovery_directory(crawler) / f'{key}.pending.json', kind)
            pending[key] = (original, kind, entry[2])
            if not same_snapshot(original, snapshot_product(crawler, product)):
                snapshot_conflict(crawler, product)
        except Exception as error:
            record_snapshot_error(crawler, product, error)
    except Exception as error:
        record_snapshot_error(crawler, product, error)


def complete_save(crawler, product, kind):
    """Return file-cleanup status independently of confirmed DB success."""
    key = recovery_key(crawler, product, kind)
    getattr(crawler, '_pending_db_saves', {}).pop(key, None)
    path = recovery_directory(crawler) / f'{key}.pending.json'
    pending = getattr(crawler, '_file_cleanup_pending', None)
    if pending is None:
        pending = crawler._file_cleanup_pending = {}
    try:
        if path.exists():
            original = read_snapshot(crawler, path, kind)
            source = product.get('_recovery_source_snapshot', snapshot_product(crawler, product))
            if not same_snapshot(original, source):
                raise SaveConflictError('Pending snapshot changed; not archived')
            destination = path.with_name(f'{key}.saved.json')
            if destination.exists():
                destination = path.with_name(f'{key}.{uuid.uuid4().hex}.saved.json')
            os.replace(path, destination)
        pending.pop(key, None)
        report = getattr(crawler, 'detail_report', {})
        report['run_errors'] = [
            error for error in report.get('run_errors', [])
            if not (error.get('stage') == 'recovery_file_cleanup' and error.get('url') == product.get('product_url'))
        ]
        report['recovery_file_pending'] = len(pending)
        return True
    except Exception as error:
        pending[key] = product.get('item')
        crawler.detail_report['recovery_file_pending'] = len(pending)
        print(f"[DB SAVED / FILE PENDING] item={product.get('item') or '-'} error={type(error).__name__}")
        crawler._record_run_error('recovery_file_cleanup', product, 'DB saved; recovery file cleanup pending')
        return False


def report_file_cleanup(crawler):
    count = len(getattr(crawler, '_file_cleanup_pending', {}))
    crawler.detail_report['recovery_file_pending'] = count
    if count:
        print(f'[FILE CLEANUP PENDING] DB-saved products needing file cleanup: {count}')
    return count


def recover_pending_saves(crawler):
    """One final pass AFTER every product was visited, using in-memory values."""
    recovered = []
    for row, kind, retryable in list(getattr(crawler, '_pending_db_saves', {}).values()):
        if not retryable:
            continue
        try:
            saved = (
                crawler.save_listing_fallback(row, 'db_save_retry')
                if kind == 'insert_listing' else crawler.save_detail_result(row)
            )
            if saved:
                recovered.append((row, kind))
                print(f"[DB SAVE RECOVERED] item={row.get('item') or '-'}")
        except Exception as error:
            # A bad row must not hide other queued rows or terminate the run.
            crawler._record_run_error('db_save_recovery', row, type(error).__name__)
    return recovered
