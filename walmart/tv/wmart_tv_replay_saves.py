"""Replay preserved DB-save failures without fetching current retailer data.

python -m walmart.tv.wmart_tv_replay_saves --batch-id w_YYYYMMDD_HHMMSS [--test]
Run after the original crawler has finished. Successful files are archived.
"""
import argparse
import json

from walmart.tv.wmart_tv_save_recovery import RECOVERY_ROOT


def replay_batch(batch_id, test_mode=False, directory=None, factory=None):
    directory = RECOVERY_ROOT if directory is None else directory
    if factory is None:
        # Import only on execution; no production config is read during test discovery.
        from walmart.tv.wmart_tv_dt import WalmartTVDetailCrawler
        from walmart.tv.wmart_tv_dt_update import WalmartTVDetailUpdateCrawler

        def factory(kind):
            cls = WalmartTVDetailUpdateCrawler if kind == 'update_detail' else WalmartTVDetailCrawler
            return cls(batch_id=batch_id, test_mode=test_mode)

    crawlers = {}
    matched = saved = failed = file_pending = 0
    try:
        for path in sorted(directory.glob('*.pending.json')):
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
                if payload.get('batch_id') != batch_id or payload.get('test_mode') is not test_mode:
                    continue
                matched += 1
                if payload.get('version') != 1 or payload.get('account_name') != 'Walmart':
                    raise ValueError('Unsupported snapshot')
                kind = payload.get('kind')
                if kind not in {'insert_detail', 'insert_listing', 'update_detail'}:
                    raise ValueError('Unsupported operation')
                row = payload['product']
                if not isinstance(row, dict) or not row.get('_save_crawl_datetime'):
                    raise ValueError('Missing original collection time')
                if kind == 'update_detail' and not row.get('id'):
                    raise ValueError('Missing update row id')
                if kind != 'update_detail' and row.get('id') is not None:
                    raise ValueError('Unexpected update row id')
                if kind not in crawlers:
                    crawlers[kind] = factory(kind)
                    crawlers[kind].save_recovery_dir = directory
                crawler = crawlers[kind]
                # A renamed/tampered snapshot must not leave the wrong file pending.
                from walmart.tv.wmart_tv_save_recovery import recovery_key
                if path.name != f'{recovery_key(crawler, row, kind)}.pending.json':
                    raise ValueError('Snapshot identity mismatch')
                ok = (
                    crawler.save_listing_fallback(row, 'db_save_replay')
                    if kind == 'insert_listing' else crawler.save_detail_result(row)
                )
                if ok:
                    saved += 1
                    if not getattr(crawler, '_last_file_cleanup_ok', True):
                        file_pending += 1
                else:
                    failed += 1
            except Exception as error:
                failed += 1
                print(f'[REPLAY ERROR] error={type(error).__name__}; continuing other snapshots')
    finally:
        for crawler in crawlers.values():
            if crawler.db_conn is not None:
                try:
                    crawler.db_conn.close()
                except Exception:
                    pass
    print(
        f'[REPLAY DONE] Matched: {matched}, DB saved: {saved}, '
        f'DB failed: {failed}, File cleanup pending: {file_pending}'
    )
    return failed == 0 and file_pending == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-id', required=True)
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    raise SystemExit(0 if replay_batch(args.batch_id, args.test) else 1)


if __name__ == '__main__':
    main()
