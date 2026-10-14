"""Standalone dryer collection using the existing LDY merge/finalization rules.

Importing this module is offline. Runtime imports the existing settings loader;
payloads stay in memory and only public product/spec projections are saved.
"""
import argparse
from collections import Counter
import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlsplit

from .step00_dryer import (
    BASE_URL, BSR_URL, COLUMNS, Settings, bsr_url, detail_product, detail_url,
    enrich_row, finalize_dryer, listing_page, listing_url, next_offsets,
    parse_card, product_kind, reviews_url, select_bsr, select_main, validate_reviews,
    validate_rows,
)
from .step00_dryer_audit import AuditJournal, project_listing, source_record, value_record


OCCURRENCE_COLUMNS = (
    "omni_item_id", "model_id", "brand", "description", "product_url", "page",
    "rank_in_page", "original_rank", "original_bsr_rank", "main_rank", "bsr_rank",
    "selling_price", "was_price", "total_saving", "rating", "review_count", "sponsored", "kind",
)
AUDIT_COLUMNS = (
    "item", "sku", "main_rank", "bsr_rank", "kind", "column", "raw_key", "raw_value",
    "normalized", "source", "reason", "source_spec_reason", "productdetail_status",
    "reviews_status", "serving_store", "loading_type", "capacity",
)

# No response text/error message is returned on HTTP/network/JSON failures.
XHR = r"""
const done = arguments[arguments.length - 1];
const path = arguments[0], timeout = arguments[1];
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), timeout);
fetch(path, {credentials: 'same-origin', headers: {accept: 'application/json'},
             signal: controller.signal})
  .then(async r => {
    if (!r.ok) return done({status: r.status, reason: 'http_error'});
    try { done({status: r.status, payload: await r.json()}); }
    catch (_) { done({status: r.status, reason: 'non_json_response'}); }
  })
  .catch(e => done({status: 'network', reason:
    e.name === 'AbortError' ? 'request_timeout' : 'network_error'}))
  .finally(() => clearTimeout(timer));
"""


class CollectionError(RuntimeError):
    """Reasons are controlled code labels, never raw browser/DB exception text."""


@dataclass
class APIResult:
    payload: object = None
    status: object = "not_requested"
    attempts: int = 0
    reason: str = ""
    store: str = ""

    @property
    def ok(self):
        return self.payload is not None and not self.reason

    def summary(self):
        return {"status": self.status, "attempts": self.attempts,
                "retries": max(0, self.attempts - 1), "reason": self.reason, "store": self.store}


def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def create_logger(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("lowes.dryer")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(path, encoding="utf-8")):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


class BrowserAPI:
    def __init__(self, settings, logger):
        self.settings, self.logger, self.driver = settings, logger, None
        self.active_store = None

    def launch(self):
        import undetected_chromedriver as uc
        from .step00_uc import launch_chrome
        options = uc.ChromeOptions()
        options.page_load_strategy = "eager"
        options.add_argument("--lang=en-US")
        options.add_argument("--disable-blink-features=AutomationControlled")
        self.driver = launch_chrome(uc, options, headless=self.settings.headless)
        self.driver.set_page_load_timeout(75)
        self.driver.set_script_timeout(self.settings.timeout + 15)
        self.open(BASE_URL + "/")
        self.set_region(self.settings.listing_store)
        self.open(BASE_URL + "/search?searchTerm=dryer")
        self.set_region(self.settings.listing_store)
        self.logger.info("[지역] ZIP=%s listing_store=%s detail_store=%s nearby=%s",
                         self.settings.zip_code, self.settings.listing_store,
                         self.settings.detail_store, self.settings.nearby_stores)

    def open(self, url):
        self.driver.get(url)
        time.sleep(self.settings.seed_wait)
        if urlsplit(self.driver.current_url).hostname not in {"www.lowes.com", "lowes.com"}:
            raise CollectionError("browser_origin_mismatch")
        # Inspect only to detect a challenge; never persist or log page text.
        denied = self.driver.execute_script(
            "return /access denied|verify you are human/i.test(document.title + ' ' + "
            "(document.body ? document.body.innerText.slice(0,1500) : ''));"
        )
        if denied:
            raise CollectionError("browser_access_denied")

    def set_region(self, store, alternate=False):
        s = self.settings
        store = str(store).zfill(4)
        name = s.alt_store_name if alternate else s.store_name
        city = s.alt_store_city if alternate else s.store_city
        region = s.alt_store_region if alternate else s.store_region
        data = {"id": store, "zip": s.zip_code, "state": s.state,
                "name": name, "city": city, "region": region}
        personalization = {"zipCode": s.zip_code, "storeId": store,
                           "state": s.state, "audienceList": []}
        values = {
            "sn": store, "sd": quote(json.dumps(data, separators=(",", ":")), safe=""),
            "zipcode": s.zip_code, "zipstate": s.state, "nearbyid": s.nearby_store,
            "regionNumber": region,
            "p13n": quote(json.dumps(personalization, separators=(",", ":")), safe=""),
        }
        for name, value in values.items():
            self.driver.add_cookie({"name": name, "value": value, "domain": ".lowes.com",
                                    "path": "/", "secure": True})
        self.active_store = store
        # These values are created from public location settings; no cookies are read.

    def get(self, path, label, item="", sku="", validator=None, store=""):
        result = APIResult(store=store)
        for attempt in range(1, self.settings.attempts + 1):
            try:
                response = self.driver.execute_async_script(XHR, path, self.settings.timeout * 1000)
                if not isinstance(response, dict):
                    response = {"status": "browser", "reason": "invalid_browser_result"}
            except Exception as exc:
                response = {"status": "browser", "reason": type(exc).__name__}
            status, reason = response.get("status"), response.get("reason", "")
            payload = response.get("payload")
            if status == 200 and not reason:
                try:
                    if validator:
                        validator(payload)
                except ValueError as exc:
                    # Validators in step00_dryer raise controlled schema labels.
                    reason = str(exc)
                except Exception as exc:
                    reason = "invalid_payload_" + type(exc).__name__
            if status != 200 and not reason:
                reason = "http_error"
            if payload is None and not reason:
                reason = "missing_payload"
            result = APIResult(payload if not reason else None, status, attempt, reason, store)
            if result.ok:
                return result
            self.logger.warning("[API 재시도] api=%s item=%s sku=%s status=%s attempt=%s/%s reason=%s",
                                label, item or "-", sku or "-", status, attempt,
                                self.settings.attempts, reason)
            if attempt < self.settings.attempts:
                time.sleep(self.settings.retry_wait)
        return result

    def close(self):
        if self.driver is not None:
            try:
                self.driver.quit()
                self.driver = None
            except Exception:
                self.logger.warning("[브라우저] close_failed")


def fetch_detail(api, settings, source, logger):
    item, sku = source["omni_item_id"], source.get("model_id", "")
    previous_store = getattr(api, "active_store", None) or settings.detail_store
    try:
        if str(previous_store).lstrip("0") != settings.detail_store.lstrip("0"):
            api.set_region(settings.detail_store)
        primary = api.get(detail_url(item, settings), "productdetail", item, sku,
                          lambda payload: detail_product(payload, item), settings.detail_store)
        if primary.ok or not settings.alt_store_enabled or settings.alt_store.lstrip("0") == settings.detail_store:
            return primary, []
        logger.warning("[상세 대체 매장] item=%s primary_status=%s alt_store=%s ZIP=%s",
                       item, primary.status, settings.alt_store, settings.zip_code)
        api.set_region(settings.alt_store, alternate=True)
        alternate = api.get(detail_url(item, settings, settings.alt_store), "productdetail_alt", item, sku,
                            lambda payload: detail_product(payload, item), settings.alt_store)
        alternate.attempts += primary.attempts
        return alternate, [{"item": item, "api": "productdetail_primary", **primary.summary(),
                            "recovered": alternate.ok}]
    finally:
        if str(getattr(api, "active_store", previous_store)).lstrip("0") != str(previous_store).lstrip("0"):
            api.set_region(previous_store)


def collect_main(api, settings, logger, run_dir, detail_cache, on_detail=None, on_page=None):
    logger.info("[메인 시작] target_limit=%s", settings.main_limit)
    rows, seen_ids, signatures, pages = [], set(), set(), []
    offset, adjusted, finished = 0, None, False
    eligible_count = 0
    for page in range(1, settings.max_main_pages + 1):
        result = api.get(listing_url(offset, adjusted, settings), "main", validator=listing_page)
        if not result.ok:
            write_json(run_dir / "audit" / "main_failure.json", result.summary())
            raise CollectionError("main_listing_failed")
        cards, metadata = listing_page(result.payload)
        signature = tuple(str(c["product"].get("omniItemId", "")) for c in cards)
        if cards and signature in signatures:
            raise CollectionError("main_repeated_page")
        signatures.add(signature)
        for position, card in enumerate(cards, 1):
            row = parse_card(card, page, position, offset + position)
            row.update(_audit_listing=project_listing(card), _listing_api=result.summary())
            item = row["omni_item_id"]
            if "/collections/" in row["product_url"]:
                rows.append(row)
                if item:
                    seen_ids.add(item)
                logger.info("[메인 제외] page=%s position=%s reason=collection_card", page, position)
                continue
            if not item:
                if on_page:
                    on_page(select_main(rows, settings.main_limit))
                logger.error("[목록 오류] stage=main page=%s position=%s sku=%s reason=missing_product_id",
                             page, position, row["model_id"] or "-")
                raise CollectionError("main_missing_product_id")
            if row["kind"] in {"unknown", "other"} and item not in seen_ids and eligible_count < settings.main_limit:
                logger.info("[종류 확인] item=%s sku=%s", item, row["model_id"] or "-")
                response, recovered = fetch_detail(api, settings, row, logger)
                detail_cache[item] = (response, recovered)
                if response.ok:
                    product, _ = detail_product(response.payload, item)
                    row["kind"] = product_kind(product)
                else:
                    row["kind"] = "unknown"
                if on_detail:
                    on_detail(row, response)
            if item not in seen_ids and row["kind"] not in {"washer", "other"}:
                eligible_count += 1
            rows.append(row)
            seen_ids.add(item)
        targets = select_main(rows, settings.main_limit)
        if on_page:
            on_page(targets)
        page_info = {"page": page, "offset": offset, "rows": len(cards), "total": metadata["total"],
                     "targets": len(targets), "status": result.status, "attempts": result.attempts}
        pages.append(page_info)
        write_csv(run_dir / "main" / "main_occurrences.csv", rows, OCCURRENCE_COLUMNS)
        write_json(run_dir / "main" / "pages.json", pages)
        logger.info("[메인] page=%s cards=%s accumulated=%s target=%s/%s",
                    page, len(cards), len(rows), len(targets), settings.main_limit)
        total = metadata["total"]
        try:
            exhausted = total is not None and offset + settings.page_size >= int(total)
        except (TypeError, ValueError):
            exhausted = False
        pagination = metadata.get("pagination") or {}
        if pagination.get("pageCount") not in (None, ""):
            try:
                exhausted = exhausted or page >= int(pagination["pageCount"])
            except (TypeError, ValueError):
                pass
        if not cards and total not in (None, 0, "0"):
            try:
                unexpected_empty = offset < int(total)
            except (TypeError, ValueError):
                unexpected_empty = True
            if unexpected_empty:
                raise CollectionError("main_unexpected_empty_page")
        if len(targets) >= settings.main_limit or exhausted or not cards:
            finished = True
            break
        offset, adjusted = next_offsets(offset, metadata, settings.page_size, adjusted)
        time.sleep(settings.page_wait)
    if not finished:
        raise CollectionError("main_page_limit_reached_before_target_or_end")
    return rows, pages


def collect_bsr(api, settings, logger, run_dir, on_page=None):
    logger.info("[BSR 시작] displayed_rank_limit=%s", settings.bsr_limit)
    api.open(BSR_URL)
    api.set_region(settings.listing_store)
    rows, pages, signatures = [], [], set()
    offset, adjusted = 0, 0
    while len(rows) < settings.bsr_limit:
        page = len(pages) + 1
        result = api.get(bsr_url(offset, adjusted), "bsr", validator=listing_page)
        if not result.ok:
            write_json(run_dir / "audit" / "bsr_failure.json", result.summary())
            raise CollectionError("bsr_listing_failed")
        cards, metadata = listing_page(result.payload)
        signature = tuple(str(c["product"].get("omniItemId", "")) for c in cards)
        if cards and signature in signatures:
            raise CollectionError("bsr_repeated_page")
        signatures.add(signature)
        for position, card in enumerate(cards[:settings.bsr_limit - len(rows)], 1):
            row = parse_card(card, page, position, len(rows) + 1)
            row.update(_audit_listing=project_listing(card), _listing_api=result.summary())
            if not row["omni_item_id"]:
                if on_page:
                    on_page(rows)
                logger.error("[목록 오류] stage=bsr page=%s position=%s sku=%s reason=missing_product_id",
                             page, position, row["model_id"] or "-")
                raise CollectionError("bsr_missing_product_id")
            rows.append(row)
        if on_page:
            on_page(rows)
        pages.append({"page": page, "offset": offset, "rows": len(cards), "collected": len(rows),
                      "status": result.status, "attempts": result.attempts})
        write_csv(run_dir / "bsr" / "bsr_occurrences.csv", rows, OCCURRENCE_COLUMNS)
        write_json(run_dir / "bsr" / "pages.json", pages)
        logger.info("[BSR] page=%s displayed=%s/%s", page, len(rows), settings.bsr_limit)
        if len(rows) >= settings.bsr_limit:
            break
        if not cards:
            raise CollectionError("bsr_ended_before_requested_displayed_rank")
        if len(cards) < settings.page_size:
            raise CollectionError("bsr_short_page_before_requested_displayed_rank")
        offset, adjusted = next_offsets(offset, metadata, settings.page_size, adjusted)
        time.sleep(settings.page_wait)
    return rows, pages


def legacy_operations(settings):
    # These modules are imported only during an authorized real collection.
    from . import step02_main_targets, step04_bsr_rank, step07_final_targets, step09_finalize
    step02_main_targets.TARGET_LIMIT = settings.main_limit
    return {
        "main": step02_main_targets.select_targets,
        "bsr": step04_bsr_rank.build_rank_map,
        "merge": step07_final_targets.build_final_targets,
        "finalize": step09_finalize.finalize_row,
    }


def selected_targets(main_rows, bsr_rows, settings, operations):
    eligible = select_main(main_rows, settings.main_limit)
    main, _ = operations["main"](eligible)
    bsr_cutoff = [dict(r, bsr_rank=r["original_rank"]) for r in bsr_rows
                  if r["original_rank"] <= settings.bsr_limit]
    first_by_id = {}
    for row in bsr_cutoff:
        first_by_id.setdefault(row["omni_item_id"], row)
    bsr = [dict(first_by_id[r["omni_item_id"]], **r) for r in operations["bsr"](bsr_cutoff)]
    if [r["omni_item_id"] for r in bsr] != [r["omni_item_id"] for r in select_bsr(bsr_rows, settings.bsr_limit)]:
        raise CollectionError("legacy_bsr_selection_mismatch")
    return main, bsr


def run_collection(api, settings, logger, run_dir, batch_id, crawl_dt, operations):
    started = time.monotonic()
    cache = {}
    journal = AuditJournal(run_dir / "audit" / "value_audit.jsonl")
    logger.info("[검수 파일] path=%s", journal.path)
    def preserve_classification(source, response):
        journal.upsert(source_record(source, response, None, batch_id, "pending_selection", main=source))
    main_rows, main_pages = collect_main(api, settings, logger, run_dir, cache, preserve_classification,
                                        lambda rows: journal.capture_listings(rows, batch_id, "main"))
    bsr_rows, bsr_pages = collect_bsr(api, settings, logger, run_dir,
                                     lambda rows: journal.capture_listings(rows, batch_id, "bsr"))
    main, bsr = selected_targets(main_rows, bsr_rows, settings, operations)
    main_by_id = {row["omni_item_id"]: row for row in main}
    bsr_by_id = {row["omni_item_id"]: row for row in bsr}
    targets = operations["merge"](main, bsr)
    calendar_week = f"w{datetime.fromisoformat(crawl_dt).isocalendar().week:02d}"
    for target in targets:
        target.update(batch_id=batch_id, crawl_datetime=crawl_dt, calendar_week=calendar_week)
    logger.info("[병합] main=%s bsr=%s shared=%s total=%s", len(main), len(bsr),
                len(main) + len(bsr) - len(targets), len(targets))
    write_csv(run_dir / "output" / "final_targets.csv", targets, OCCURRENCE_COLUMNS)
    logger.info("[상세 시작] total=%s", len(targets))
    api.set_region(settings.detail_store)
    failures, recovered_requests, quality, spec_audit, enriched = [], [], [], [], []
    rejected_main = set()
    for index, source in enumerate(targets, 1):
        previous_quality_count = len(quality)
        item, sku = source["omni_item_id"], source.get("model_id", "")
        response, recovered = cache.get(item) or fetch_detail(api, settings, source, logger)
        recovered_requests.extend(recovered)
        journal.upsert(source_record(source, response, None, batch_id,
                                     main=main_by_id.get(item), bsr=bsr_by_id.get(item)))
        actual_kind = product_kind(detail_product(response.payload, item)[0]) if response.ok else source.get("kind")
        if (actual_kind in {"washer", "other"} and source.get("main_rank") not in (None, "")
                and source.get("bsr_rank") in (None, "")):
            rejected_main.add(item)
            quality.append({"item": item, "reason": "main_excluded_after_detail", "kind": actual_kind})
            excluded = dict(journal.records[item], status="excluded", product_kind=actual_kind,
                            exclusion_reason="non_target_after_detail")
            journal.upsert(excluded)
            logger.warning("[메인 제외] item=%s sku=%s reason=non_target_after_detail kind=%s reviews=not_requested",
                           item, sku or "-", actual_kind)
            logger.info("[상세] %s/%s (%.1f%%) item=%s api_failure_products=%s elapsed=%.1fs",
                        index, len(targets), 100 * index / len(targets), item,
                        len({f["item"] for f in failures}), time.monotonic() - started)
            time.sleep(settings.page_wait)
            continue
        reviews = api.get(reviews_url(item), "reviews", item, sku, validate_reviews)
        try:
            row, audit = enrich_row(source, response.payload if response.ok else None,
                                     reviews.payload if reviews.ok else None)
            provisional = finalize_dryer(row, batch_id, crawl_dt, operations["finalize"])
            value_audit = value_record(source, row, audit, response, reviews, provisional, batch_id, crawl_dt,
                                      main=main_by_id.get(item), bsr=bsr_by_id.get(item))
        except BaseException:
            # Preserve already-received public review values even if normalization fails.
            journal.upsert(source_record(source, response, reviews, batch_id,
                                         main=main_by_id.get(item), bsr=bsr_by_id.get(item)))
            raise
        value_audit["product_kind"] = actual_kind
        journal.upsert(value_audit)
        if actual_kind in {"washer", "other"} and source.get("main_rank") not in (None, ""):
            rejected_main.add(item)
            quality.append({"item": item, "reason": "main_excluded_after_detail", "kind": actual_kind})
        elif actual_kind == "unknown":
            quality.append({"item": item, "reason": "product_kind_unconfirmed"})
        if row.get("_sku_conflict"):
            quality.append({"item": item, "reason": "listing_detail_model_conflict_original_kept",
                            "listing_model": source.get("model_id"), "detail_model": row.get("_detail_model_id")})
        if not row.get("sku"):
            quality.append({"item": item, "reason": "model_missing"})
        if row.get("_detail_price_invalid"):
            quality.append({"item": item, "reason": "invalid_detail_price"})
        for column in source.get("_listing_invalid_columns", []):
            quality.append({"item": item, "reason": "invalid_listing_value_rejected", "column": column})
        for label, result, columns in (
            ("productdetail", response, ["sku", "loading_type", "capacity", "final_sku_price", "original_sku_price", "savings"]),
            ("reviews", reviews, ["star_rating", "count_of_reviews", "count_of_star_ratings"]),
        ):
            if not result.ok:
                failure = {"item": item, "sku": sku, "stage": "detail", "api": label,
                           **result.summary(), "affected_columns": columns,
                           "action": "kept_with_available_values"}
                failures.append(failure)
                logger.warning("[수집 오류] item=%s sku=%s api=%s status=%s attempts=%s reason=%s affected_columns=%s",
                               item, sku or "-", label, result.status, result.attempts,
                               result.reason, ",".join(columns))
        for column in ("loading_type", "capacity"):
            reason = row[column + "_reason"]
            if reason in {"invalid_value", "conflicting_values"}:
                quality.append({"item": item, "reason": reason, "column": column})
            matching = [a for a in audit if a["column"] == column] or [{"column": column}]
            for entry in matching:
                spec_audit.append({**entry, "item": item, "sku": row.get("sku"),
                                   "main_rank": row.get("main_rank"), "bsr_rank": row.get("bsr_rank"),
                                   "kind": actual_kind, "source_spec_reason": reason,
                                   "reason": "collection_failure" if not response.ok and not row.get(column) else reason,
                                   "source": entry.get("source", "productdetail" if response.ok else "listing"),
                                   "productdetail_status": response.status, "reviews_status": reviews.status,
                                   "serving_store": response.store, "loading_type": row.get("loading_type"),
                                   "capacity": row.get("capacity")})
        enriched.append(row)
        for warning in quality[previous_quality_count:]:
            logger.warning("[검수 경고] item=%s reason=%s column=%s",
                           warning["item"], warning["reason"], warning.get("column", "-"))
        logger.info("[상세] %s/%s (%.1f%%) item=%s api_failure_products=%s elapsed=%.1fs",
                    index, len(targets), 100 * index / len(targets), item,
                    len({f["item"] for f in failures}), time.monotonic() - started)
        write_csv(run_dir / "audit" / "spec_audit.csv", spec_audit, AUDIT_COLUMNS)
        write_json(run_dir / "audit" / "failures.json", failures)
        time.sleep(settings.page_wait)
    # Confirmed non-targets found in detail must not retain a main rank.
    retained_main = [r for r in main if r["omni_item_id"] not in rejected_main]
    for rank, row in enumerate(retained_main, 1):
        row["main_rank"] = rank
    final_targets = operations["merge"](retained_main, bsr)
    by_id = {r["omni_item_id"]: r for r in enriched}
    final_rows = []
    for target in final_targets:
        row = dict(by_id[target["omni_item_id"]])
        row.update(main_rank=target.get("main_rank"), bsr_rank=target.get("bsr_rank"),
                   batch_id=batch_id, crawl_datetime=crawl_dt)
        final_rows.append(finalize_dryer(row, batch_id, crawl_dt, operations["finalize"]))
    counts = validate_rows(final_rows, settings.main_limit, settings.bsr_limit)
    journal.finish(final_rows)
    logger.info("[검수 파일 완료] final_products=%s recorded_products=%s path=%s",
                len(final_rows), len(journal.records), journal.path)
    final_ranks = {r["item"]: r for r in final_rows}
    for failure in failures:
        saved = final_ranks.get(failure["item"])
        failure["missing_columns"] = [
            column for column in failure["affected_columns"]
            if saved is not None and saved[column] in (None, "")
        ]
        if saved is None:
            failure["action"] = "excluded_after_detail"
        logger.warning("[오류 결과] item=%s sku=%s api=%s status=%s retries=%s missing_columns=%s action=%s",
                       failure["item"], (saved or {}).get("sku") or failure["sku"] or "-",
                       failure["api"], failure["status"], failure["retries"],
                       ",".join(failure["missing_columns"]) or "none", failure["action"])
    spec_audit = [dict(a, main_rank=final_ranks[a["item"]]["main_rank"],
                       bsr_rank=final_ranks[a["item"]]["bsr_rank"])
                  for a in spec_audit if a["item"] in final_ranks]
    write_csv(run_dir / "main" / "main_targets.csv", retained_main, OCCURRENCE_COLUMNS)
    write_csv(run_dir / "bsr" / "bsr_rank_map.csv", bsr, OCCURRENCE_COLUMNS)
    write_csv(run_dir / "output" / "final_targets.csv", final_targets, OCCURRENCE_COLUMNS)
    write_csv(run_dir / "output" / "final_output.csv", final_rows, COLUMNS)
    write_csv(run_dir / "audit" / "spec_audit.csv", spec_audit, AUDIT_COLUMNS)
    write_json(run_dir / "audit" / "failures.json", failures)
    write_json(run_dir / "audit" / "quality_warnings.json", quality)
    write_json(run_dir / "audit" / "recovered_requests.json", recovered_requests)
    one_audit_per_spec = {(a["item"], a["column"]): a for a in spec_audit}
    one_kind_per_item = {a["item"]: a["kind"] for a in spec_audit}
    one_excluded_per_item = {r["omni_item_id"]: r["kind"] for r in main_rows
                            if r["kind"] in {"washer", "other"}}
    summary = {
        "batch_id": batch_id, "crawl_datetime": crawl_dt, "settings": asdict(settings),
        "counts": counts, "main_pages": len(main_pages), "bsr_pages": len(bsr_pages),
        "main_raw": len(main_rows), "bsr_displayed": len(bsr_rows),
        "main_collection_cards": len({r["product_url"] for r in main_rows
                                      if "/collections/" in r["product_url"]}),
        "main_shortfall": max(0, settings.main_limit - counts["main"]),
        "main_excluded_by_kind": dict(Counter(one_excluded_per_item.values())),
        "excluded_after_detail": sorted(rejected_main),
        "kind_counts": dict(Counter(one_kind_per_item.values())),
        "spec_reasons": {c: dict(Counter(a["reason"] for a in one_audit_per_spec.values() if a["column"] == c))
                         for c in ("loading_type", "capacity")},
        "api_failures": len(failures), "api_failure_products": len({f["item"] for f in failures}),
        "quality_warnings": len(quality), "elapsed_seconds": round(time.monotonic() - started, 1),
        "collection_status": "partial_failure" if failures or quality else "complete",
    }
    if summary["main_shortfall"]:
        logger.warning("[메인 대상 부족] requested=%s selected=%s excluded_after_detail=%s",
                       settings.main_limit, counts["main"], len(rejected_main))
    logger.info("[사양 집계] loading_type_null=%s capacity_null=%s reasons=%s",
                counts["loading_type_null"], counts["capacity_null"],
                json.dumps(summary["spec_reasons"], ensure_ascii=False))
    write_json(run_dir / "collection_summary.json", summary)
    return final_rows, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lowe's dryer test collection")
    parser.add_argument("--collect-only", action="store_true", help="write CSV/audits without DB access")
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args(argv)
    now = datetime.now()
    try:
        # Existing loader keeps connection settings within the runtime process.
        from .step00_config import load_env
        load_env(Path(__file__).resolve().parent.parent / ".env")
        settings = Settings.from_env(os.environ)
    except Exception as exc:
        failed_root = args.run_root or Path(__file__).resolve().parent / "data" / "dryer" / now.strftime("%Y%m%d") / ("setup_failed_" + now.strftime("%H%M%S"))
        logger = create_logger(failed_root / "logs" / "dryer.log")
        reason = getattr(exc, "public_reason", "configuration_load_failed")
        logger.error("[설정 실패] type=%s stage=settings reason=%s", type(exc).__name__, reason)
        write_json(failed_root / "manifest.json", {
            "status": "failed", "failed_stage": "settings", "error_type": type(exc).__name__, "reason": reason,
            "started_at": now.isoformat(timespec="seconds"), "run_root": str(failed_root)})
        return 1
    batch_id = os.getenv("LOWES_DRYER_BATCH_ID") or "l_" + now.strftime("%y%m%d_%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", batch_id):
        raise ValueError("invalid dryer batch ID")
    root = args.run_root or Path(__file__).resolve().parent / "data" / "dryer" / now.strftime("%Y%m%d") / batch_id
    logger = create_logger(root / "logs" / "dryer.log")
    manifest = {"batch_id": batch_id, "run_root": str(root), "started_at": now.isoformat(timespec="seconds"),
                "status": "running", "db_loaded": False}
    write_json(root / "manifest.json", manifest)
    api = BrowserAPI(settings, logger)
    stage = "browser"
    logger.info("[시작] batch=%s ZIP=10010 main_limit=%s bsr_limit=%s run_root=%s",
                batch_id, settings.main_limit, settings.bsr_limit, root)
    try:
        api.launch()
        stage = "collection"
        operations = legacy_operations(settings)
        rows, summary = run_collection(api, settings, logger, root, batch_id,
                                       now.strftime("%Y-%m-%d %H:%M:%S"), operations)
        api.close()
        manifest["collection"] = summary
        if not args.collect_only:
            stage = "db_load"
            from .step14_dryer_db_load import load_batch
            loaded = load_batch(rows, logger=logger)
            manifest.update(db_loaded=True, db=loaded)
            write_json(root / "db_load_summary.json", loaded)
        manifest["status"] = summary["collection_status"]
        logger.info("[완료] status=%s products=%s main=%s bsr=%s api_failure_products=%s quality_warnings=%s db_loaded=%s",
                    manifest["status"], len(rows), summary["counts"]["main"], summary["counts"]["bsr"],
                    summary["api_failure_products"], summary["quality_warnings"], manifest["db_loaded"])
        return 2 if manifest["status"] == "partial_failure" else 0
    except KeyboardInterrupt:
        manifest.update(status="interrupted", failed_stage=stage)
        logger.error("[중단] stage=%s", stage)
        return 130
    except Exception as exc:
        reason = str(exc) if isinstance(exc, CollectionError) else getattr(exc, "public_reason", type(exc).__name__)
        frame = exc.__traceback__
        while frame and frame.tb_next:
            frame = frame.tb_next
        location = f"{Path(frame.tb_frame.f_code.co_filename).name}:{frame.tb_lineno}" if frame else "-"
        manifest.update(status="failed", failed_stage=stage, error_type=type(exc).__name__,
                        reason=reason, location=location)
        logger.error("[실패] stage=%s type=%s reason=%s location=%s", stage, type(exc).__name__, reason, location)
        return 1
    finally:
        api.close()
        manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        write_json(root / "manifest.json", manifest)


if __name__ == "__main__":
    raise SystemExit(main())
