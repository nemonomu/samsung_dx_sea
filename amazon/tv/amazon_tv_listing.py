"""TV listing diagnostics and bounded BSR loading, without alternative URL collection."""

import hashlib
import json
import re
import time
from pathlib import Path

from lxml import html


class AmazonTVListingMixin:
    BSR_LOAD_SECONDS = 120
    BSR_POLL_SECONDS = 2
    LISTING_SNAPSHOT_LIMIT = 100

    def _listing_snapshot(self, tag, content):
        """Save the parsed HTML, rather than reading a potentially changed page."""
        try:
            count = getattr(self, '_listing_snapshot_count', 0)
            if count >= self.LISTING_SNAPSHOT_LIMIT:
                print('[LISTING DIAG] HTML snapshot limit reached; text diagnostics continue')
                return None
            directory = Path(__file__).resolve().parents[2] / 'logs'
            directory.mkdir(parents=True, exist_ok=True)
            batch = re.sub(r'[^A-Za-z0-9_-]', '_', str(self.batch_id or 'nobatch'))
            path = directory / f'{batch}_{self.page_type}_{tag}.html'
            path.write_text(content, encoding='utf-8')
            self._listing_snapshot_count = count + 1
            print(f'[LISTING DIAG] HTML saved: {path}')
            return str(path)
        except Exception as exc:
            print(f'[LISTING DIAG] HTML save failed: {exc}')
            return None

    def extract_listing_product_url(self, node, page_number, item_index):
        # Same URL rule as BaseCrawler.extract_product_url; candidates are diagnostic only.
        raw_url = self.safe_extract_chain(node, 'product_url')
        url = None if raw_url == '#' else raw_url
        if url:
            return f'https://www.amazon.com{url}' if url.startswith('/') else url
        try:
            fragment = html.tostring(node, encoding='unicode')
            digest = hashlib.sha256(fragment.encode('utf-8')).hexdigest()[:16]
            key = (page_number, digest)
            seen = getattr(self, '_url_diagnostic_seen', set())
            self._url_diagnostic_seen = seen
            if key not in seen:
                seen.add(key)
                hrefs = node.xpath('.//a/@href')
                asins = set(node.xpath('descendant-or-self::*/@data-asin'))
                asins.update(filter(None, (self.extract_item(href) for href in hrefs)))
                evidence = {
                    'page_type': self.page_type, 'page': page_number, 'item_index': item_index,
                    'listing_url': self.page.url,
                    'reason': 'href_is_hash' if raw_url == '#' else 'url_extraction_empty',
                    'name': self.safe_extract_chain(node, 'retailer_sku_name'),
                    'asin_candidates': sorted(asin for asin in asins if asin),
                    'href_candidates': hrefs, 'url_xpaths': self.get_chain_xpaths('product_url'),
                    'action': 'log_only_skip_collection',
                }
                print('[URL MISSING] ' + json.dumps(evidence, ensure_ascii=False))
                self._listing_snapshot(f'p{page_number}_item{item_index}_url_missing_{digest}', fragment)
        except Exception as exc:
            print(f'[LISTING DIAG] URL diagnostic failed: {exc}')
        return url

    def _bsr_dom_diagnostic(self, tree, matched, page_number, phase, attempt,
                            elapsed, expected, retained, target_ratio):
        try:
            try:
                viewport = self.page.run_js(
                    'return {scroll_y: window.scrollY, viewport_height: window.innerHeight,'
                    ' document_height: Math.max(document.body.scrollHeight,'
                    ' document.documentElement.scrollHeight), ready_state: document.readyState};'
                )
            except Exception as exc:
                viewport = {'error': str(exc)}
            data = {
                'page': page_number, 'url': self.page.url, 'refresh_count': phase,
                'attempt': attempt, 'elapsed_seconds': round(elapsed, 1),
                'expected': expected, 'matched': len(matched), 'retained_with_url': retained,
                'xpath': self.xpaths.get('base_container', {}).get('xpath'),
                'target_scroll_ratio': target_ratio, 'viewport': viewport,
                'dom_candidate_asins': sorted(set(tree.xpath('//*[@data-asin!=""]/@data-asin'))),
                'dom_rank_badges': tree.xpath(
                    '//*[contains(concat(" ",normalize-space(@class)," ")," zg-bdg-text ")]/text()'
                ),
                'note': 'Whole-page ASINs and badges can include unrelated widgets.',
            }
            print('[BSR LOAD] ' + json.dumps(data, ensure_ascii=False))
        except Exception as exc:
            print(f'[LISTING DIAG] BSR diagnostic failed: {exc}')

    def find_bsr_product_containers(self, xpath, page_number, expected=50):
        """Poll for 120 seconds, refresh once, then poll for another 120 seconds.

        Only URL-qualified cards are retained across reads/refresh. URL failures
        are diagnosed, and remain skipped by the normal save_products path.
        """
        retained = {}
        missing = {}
        latest_html = ''
        for phase in range(2):
            if phase:
                print(f'[BSR LOAD] Page {page_number}: refreshing once after {self.BSR_LOAD_SECONDS}s budget')
                try:
                    self.page.refresh()
                    if not self.recover_amazon_pages():
                        raise RuntimeError('Amazon recovery unresolved after BSR refresh')
                except Exception as exc:
                    if not retained:
                        raise
                    print(f'[WARNING] BSR refresh failed; retaining {len(retained)} URL-qualified cards: {exc}')
                    break
            deadline = time.monotonic() + self.BSR_LOAD_SECONDS
            started = time.monotonic()
            attempt = 0
            target_ratio = 0.90
            while True:
                attempt += 1
                latest_html = self.page.html
                tree = html.fromstring(latest_html)
                containers = tree.xpath(xpath)
                for index, node in enumerate(containers, 1):
                    url = self.extract_listing_product_url(node, page_number, index)
                    if url:
                        key = self.extract_item(url) or url
                        # Detach the card so old full-page DOMs are not kept in memory.
                        retained[key] = html.fromstring(html.tostring(node))
                    else:
                        # Keep failures only for the existing URL-missing counter, never as URLs.
                        key = hashlib.sha256(html.tostring(node)).hexdigest()
                        if key not in missing:
                            missing[key] = html.fromstring(html.tostring(node))
                self._bsr_dom_diagnostic(
                    tree, containers, page_number, phase, attempt,
                    time.monotonic() - started, expected, len(retained), target_ratio,
                )
                if len(containers) >= expected or len(retained) >= expected:
                    print(f'[BSR LOAD] Page {page_number}: target reached, retained={len(retained)}, refresh_count={phase}')
                    return list(retained.values()) + list(missing.values())
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._listing_snapshot(f'p{page_number}_phase{phase}_short', latest_html)
                    break
                # Small steps allow count checks during scrolling and obey the deadline.
                # Recalculate the target each time because lazy loading changes page height.
                reached = self.page.run_js(
                    'const maxY = Math.max(0, Math.max(document.body.scrollHeight,'
                    ' document.documentElement.scrollHeight) - window.innerHeight);'
                    ' const target = maxY * arguments[0];'
                    ' const next = Math.min(window.scrollY + 600, target);'
                    ' window.scrollTo(0, next); return next >= target - 2;', target_ratio,
                )
                if reached:
                    target_ratio = min(0.98, round(target_ratio + 0.05, 2))
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(self.BSR_POLL_SECONDS, remaining))
        print(f'[WARNING] BSR Page {page_number}: expected={expected}, retained={len(retained)}, '
              f'shortfall={max(0, expected - len(retained))}; collecting available cards only')
        return list(retained.values()) + list(missing.values())
