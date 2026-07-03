import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin
from datetime import datetime

from bs4 import BeautifulSoup
from requests import RequestException
from zenrows import ZenRowsClient

from .step00_config import DEFAULT_LOWES_RUN_ROOT, load_env, redact_sensitive
from .step00_uc import launch_chrome
from .step00_parse_search import (
    extract_preloaded_state,
    find_item_list,
    parse_item,
    product_card_prices,
)

SCRIPT_DIR = Path(__file__).resolve().parent
LOWES_ROOT = SCRIPT_DIR
PROJECT_ROOT = LOWES_ROOT.parent

load_env(PROJECT_ROOT / ".env")

REF_BSR_URL = "https://www.lowes.com/best-sellers/appliances/refrigerators/4294857973"
LDY_BSR_URL = "https://www.lowes.com/best-sellers/appliances/washers-dryers/washing-machines/4294857977"

PRODUCT_GROUP = os.getenv("LOWES_BSR_PRODUCT_GROUP", "REF").upper()
BSR_URL = os.getenv("LOWES_BSR_URL", REF_BSR_URL if PRODUCT_GROUP == "REF" else LDY_BSR_URL)
BSR_OFFSET = int(os.getenv("LOWES_BSR_OFFSET", "0"))
RUN_ID = os.getenv("LOWES_BSR_RUN_ID", "bsr")
RUN_ROOT = Path(os.getenv("LOWES_RUN_ROOT", str(DEFAULT_LOWES_RUN_ROOT))) / RUN_ID
OUT_DIR = Path(os.getenv("LOWES_BSR_OUT_DIR", str(RUN_ROOT / "raw" / "main_pages")))
CSV_PATH = Path(os.getenv("LOWES_BSR_CSV", str(RUN_ROOT / "parsed" / "main_occurrences.csv")))
TIMEOUT = int(os.getenv("ZENROWS_TIMEOUT", "180"))
BSR_TRANSPORT = os.getenv("LOWES_BSR_TRANSPORT", "uc").strip().lower()
BSR_FALLBACK_ZENROWS = os.getenv("LOWES_BSR_FALLBACK_ZENROWS", "0").strip().lower() not in {"0", "false", "no"}
UC_HEADLESS = os.getenv("LOWES_UC_HEADLESS", "0").strip().lower() in {"1", "true", "yes"}
UC_WAIT_SECONDS = float(os.getenv("LOWES_BSR_UC_WAIT_SECONDS", "5"))
UC_PAGE_LOAD_TIMEOUT = int(os.getenv("LOWES_BSR_UC_PAGE_LOAD_TIMEOUT", "60"))
UC_READY_TIMEOUT_SECONDS = float(os.getenv("LOWES_BSR_UC_READY_TIMEOUT_SECONDS", "60"))
UC_READY_POLL_SECONDS = float(os.getenv("LOWES_BSR_UC_READY_POLL_SECONDS", "5"))
BSR_RETRIES = max(0, int(os.getenv("LOWES_BSR_RETRIES", "2")))
BSR_RETRY_SLEEP_SECONDS = max(0.0, float(os.getenv("LOWES_BSR_RETRY_SLEEP_SECONDS", "2")))


def now():
    return datetime.now().isoformat(timespec="seconds")


def compact_text(value):
    return " ".join((value or "").split())


def html_product_cards(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    cards = []
    seen = set()
    for link in soup.select('a[href*="/pd/"]'):
        href = urljoin("https://www.lowes.com", link.get("href", ""))
        match = re.search(r"/(\d{7,})(?:[/?#].*)?$", href)
        product_id = match.group(1) if match else ""
        card = link.find_parent(attrs={"data-webvision-id": True}) or link.find_parent(class_=re.compile("product|card", re.I))
        if card and not product_id:
            product_id = card.get("data-webvision-id", "")
        title = ""
        if card:
            title_node = card.select_one(".product-desc, [data-testid*='product-title'], [data-title]")
            if title_node:
                title = compact_text(title_node.get_text(" ", strip=True) or title_node.get("data-title", ""))
        if not title:
            title = compact_text(link.get_text(" ", strip=True))
        key = product_id or href
        if not key or key in seen or not title:
            continue
        seen.add(key)
        cards.append({"product_id": product_id, "title": title, "url": href})
    return cards


def bsr_state_products(state):
    page_products = state.get("productListCommonNormalizedPageSpecificProducts", {})
    product_list = page_products.get("productList", {}) if isinstance(page_products, dict) else {}
    products = product_list.get("products", {}) if isinstance(product_list, dict) else {}
    return products if isinstance(products, list) else []


def parse_bsr_state_product(item, rank, bsr_rank):
    product = item.get("product", {}) if isinstance(item, dict) else {}
    price = item.get("price", {}) if isinstance(item, dict) else {}
    pd_url = product.get("pdURL", "")
    match = re.search(r"/(\d{7,})(?:[/?#].*)?$", pd_url or "")
    omni_item_id = match.group(1) if match else ""
    alt = product.get("alt", "") or product.get("description", "")
    model_match = re.search(r"#([A-Za-z0-9._-]+)\s*$", alt)
    image_urls = product.get("imageUrls") or []
    image_url = ""
    if isinstance(image_urls, list):
        for image in image_urls:
            if isinstance(image, dict) and image.get("value"):
                image_url = urljoin("https://mobileimages.lowes.com", image["value"])
                break
    final_price = price.get("finalPriceCentForUi", "")
    return {
        "product_group": PRODUCT_GROUP,
        "bsr_rank": bsr_rank,
        "page": 1,
        "rank_in_page": rank,
        "main_rank": bsr_rank,
        "omni_item_id": omni_item_id,
        "item_number": omni_item_id,
        "brand": product.get("brand", ""),
        "model_id": model_match.group(1) if model_match else "",
        "description": product.get("description", ""),
        "product_url": urljoin("https://www.lowes.com", pd_url),
        "image_url": image_url,
        "rating": product.get("rating", ""),
        "review_count": product.get("count", ""),
        "selling_price": final_price,
        "price_source": "bsr_preloaded_state" if final_price != "" else "",
        "raw_item_json": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
    }


def fetch_bsr():
    if BSR_TRANSPORT in {"uc", "uc_first", "browser"}:
        try:
            return fetch_bsr_uc()
        except Exception as exc:
            if not BSR_FALLBACK_ZENROWS:
                raise
            print(f"[Lowes BSR] UC failed; falling back to ZenRows: {type(exc).__name__}: {exc}")
    return fetch_bsr_zenrows()


def fetch_bsr_uc():
    import undetected_chromedriver as uc

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[Lowes BSR] {PRODUCT_GROUP} UC GET {BSR_URL}")
    started_at = now()
    start = time.time()
    options = uc.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--start-maximized")
    options.add_argument("--lang=en-US")
    driver = launch_chrome(uc, options=options, headless=UC_HEADLESS)
    try:
        driver.set_page_load_timeout(UC_PAGE_LOAD_TIMEOUT)
        driver.get(BSR_URL)
        time.sleep(UC_WAIT_SECONDS)
        body = driver.page_source or ""
    finally:
        driver.quit()
    elapsed = time.time() - start
    success = bool(body and ("__PRELOADED_STATE__" in body or "/pd/" in body))
    status_name = "success" if success else "fail"
    body_path, headers_path, meta_path = write_raw_artifacts(
        status_name=status_name,
        body=body,
        headers={"transport": "uc"},
        meta={
            "status_code": 200 if success else "",
            "success": success,
            "attempt": 1,
            "transport": "uc",
            "elapsed_seconds": round(elapsed, 3),
            "x_request_cost": "0",
            "error": "" if success else "uc_empty_or_unparseable_page",
            "bytes": len(body),
            "started_at": started_at,
            "finished_at": now(),
        },
    )
    print(f"transport=uc status={'200' if success else 'ERR'} elapsed={elapsed:.1f}s bytes={len(body)}")
    print(f"html={body_path}")
    print(f"headers={headers_path}")
    print(f"meta={meta_path}")
    if not success:
        raise RuntimeError("UC BSR fetch did not return a parseable page")

    class LocalResponse:
        status_code = 200
        text = body
        headers = {"transport": "uc", "x-request-cost": "0"}

    return LocalResponse()


def fetch_bsr_zenrows():
    api_key = os.getenv("ZENROWS_API_KEY")
    if not api_key:
        raise RuntimeError("Set ZENROWS_API_KEY in .env")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = ZenRowsClient(api_key)
    params = {
        "mode": "auto",
        "proxy_country": "us",
    }
    print(f"[Lowes BSR] {PRODUCT_GROUP} GET {BSR_URL}")
    started_at = now()
    start = time.time()
    try:
        response = client.get(BSR_URL, params=params, timeout=TIMEOUT)
    except RequestException as exc:
        elapsed = time.time() - start
        write_raw_artifacts(
            status_name="fail",
            body=redact_sensitive(str(exc)),
            headers={},
            meta={
                "status_code": None,
                "success": False,
                "attempt": 1,
                "elapsed_seconds": round(elapsed, 3),
                "x_request_cost": "",
                "error": redact_sensitive(str(exc)),
                "bytes": 0,
                "started_at": started_at,
                "finished_at": now(),
            },
        )
        raise
    elapsed = time.time() - start
    print(f"status={response.status_code} elapsed={elapsed:.1f}s bytes={len(response.text)}")
    for header in ["x-request-cost", "x-request-id", "zr-final-url"]:
        if header in response.headers:
            print(f"{header}: {response.headers[header]}")

    status_name = "success" if response.status_code == 200 else "fail"
    body_path, headers_path, meta_path = write_raw_artifacts(
        status_name=status_name,
        body=response.text,
        headers=dict(response.headers),
        meta={
            "status_code": response.status_code,
            "success": response.status_code == 200,
            "attempt": 1,
            "elapsed_seconds": round(elapsed, 3),
            "x_request_cost": response.headers.get("x-request-cost", ""),
            "error": "" if response.status_code == 200 else response.text[:500],
            "bytes": len(response.text),
            "started_at": started_at,
            "finished_at": now(),
        },
    )
    print(f"html={body_path}")
    print(f"headers={headers_path}")
    print(f"meta={meta_path}")
    return response


def write_raw_artifacts(status_name, body, headers, meta):
    unit_name = f"bsr_{PRODUCT_GROUP.lower()}_{status_name}"
    unit_dir = OUT_DIR / unit_name
    unit_dir.mkdir(parents=True, exist_ok=True)
    body_path = unit_dir / f"bsr_{PRODUCT_GROUP.lower()}_response.html"
    headers_path = unit_dir / f"bsr_{PRODUCT_GROUP.lower()}_headers.json"
    meta_path = unit_dir / f"bsr_{PRODUCT_GROUP.lower()}_meta.json"
    body_path.write_text(redact_sensitive(body or ""), encoding="utf-8", errors="replace")
    headers_path.write_text(json.dumps(redact_sensitive(headers or {}), indent=2, ensure_ascii=False), encoding="utf-8")
    meta_path.write_text(json.dumps(redact_sensitive(meta or {}), indent=2, ensure_ascii=False), encoding="utf-8")
    return body_path, headers_path, meta_path


def write_csv(rows):
    if not rows:
        return
    preferred = [
        "product_group",
        "bsr_rank",
        "omni_item_id",
        "item_number",
        "brand",
        "model_id",
        "description",
        "product_url",
        "rating",
        "review_count",
        "selling_price",
    ]
    keys = set()
    for row in rows:
        keys.update(row)
    fieldnames = [key for key in preferred if key in keys]
    fieldnames.extend(sorted(keys - set(fieldnames)))
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_bsr(page_html):
    state = extract_preloaded_state(page_html)
    bsr_products = bsr_state_products(state)
    if bsr_products:
        rows = []
        for rank, item in enumerate(bsr_products, 1):
            bsr_rank = BSR_OFFSET + rank
            rows.append(parse_bsr_state_product(item, rank, bsr_rank))
        return rows, {"source": "bsr_preloaded_state", "item_count": len(bsr_products)}

    items = find_item_list(state) or []
    html_prices = product_card_prices(page_html)
    rows = []
    if items:
        for rank, item in enumerate(items, 1):
            bsr_rank = BSR_OFFSET + rank
            row = parse_item(item, 1, rank, bsr_rank, html_prices)
            row["product_group"] = PRODUCT_GROUP
            row["bsr_rank"] = bsr_rank
            rows.append(row)
        return rows, {"source": "preloaded_state", "item_count": len(items)}

    cards = html_product_cards(page_html)
    for rank, card in enumerate(cards, 1):
        bsr_rank = BSR_OFFSET + rank
        rows.append(
            {
                "product_group": PRODUCT_GROUP,
                "bsr_rank": bsr_rank,
                "omni_item_id": card["product_id"],
                "description": card["title"],
                "product_url": card["url"],
                "source": "html_card",
            }
        )
    return rows, {"source": "html_card", "item_count": len(cards)}


BSR_OFFSETS = [int(s.strip()) for s in os.getenv("LOWES_BSR_OFFSETS", "0,24,48,72,96").split(",") if s.strip()]
BSR_PAGE_SIZE = max(1, int(os.getenv("LOWES_BSR_PAGE_SIZE", "24")))
BSR_TARGET_UNIQUE = max(0, int(os.getenv("LOWES_BSR_TARGET_UNIQUE", "100")))
BSR_MAX_OFFSET = max(max(BSR_OFFSETS or [0]), int(os.getenv("LOWES_BSR_MAX_OFFSET", "240")))


def fetch_bsr_zenrows_url(url, offset, attempt=1):
    """ZenRows fetch at a specific paginated URL. Returns response or raises."""
    api_key = os.getenv("ZENROWS_API_KEY")
    if not api_key:
        raise RuntimeError("Set ZENROWS_API_KEY in .env")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = ZenRowsClient(api_key)
    params = {"mode": "auto", "proxy_country": "us"}
    print(f"[Lowes BSR] {PRODUCT_GROUP} offset={offset} GET {url}")
    started_at = now()
    start = time.time()
    try:
        response = client.get(url, params=params, timeout=TIMEOUT)
    except RequestException as exc:
        elapsed = time.time() - start
        unit_dir = OUT_DIR / f"offset_{offset:03d}_attempt_{attempt:02d}_fail"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "body.html").write_text(redact_sensitive(str(exc)), encoding="utf-8", errors="replace")
        (unit_dir / "meta.json").write_text(
            json.dumps(
                {
                    "url": url,
                    "offset": offset,
                    "attempt": attempt,
                    "status_code": None,
                    "success": False,
                    "elapsed_seconds": round(elapsed, 3),
                    "x_request_cost": "",
                    "bytes": 0,
                    "error": redact_sensitive(str(exc)),
                    "started_at": started_at,
                    "finished_at": now(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
    elapsed = time.time() - start
    print(f"  status={response.status_code} elapsed={elapsed:.1f}s bytes={len(response.text)}")
    status_name = f"offset_{offset:03d}_attempt_{attempt:02d}_{'success' if response.status_code == 200 else 'fail'}"
    unit_dir = OUT_DIR / status_name
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "body.html").write_text(redact_sensitive(response.text or ""), encoding="utf-8", errors="replace")
    (unit_dir / "meta.json").write_text(
        json.dumps(
            {
                "url": url, "offset": offset, "attempt": attempt,
                "status_code": response.status_code,
                "success": response.status_code == 200,
                "elapsed_seconds": round(elapsed, 3),
                "x_request_cost": response.headers.get("x-request-cost", ""),
                "bytes": len(response.text), "error": "" if response.status_code == 200 else response.text[:500],
                "started_at": started_at, "finished_at": now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return response


def launch_bsr_driver():
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--start-maximized")
    options.add_argument("--lang=en-US")
    driver = launch_chrome(uc, options=options, headless=UC_HEADLESS)
    driver.set_page_load_timeout(UC_PAGE_LOAD_TIMEOUT)
    return driver


def wait_for_bsr_ready(driver):
    deadline = time.time() + UC_READY_TIMEOUT_SECONDS
    last_state = {}
    while time.time() < deadline:
        try:
            title = driver.title or ""
            current_url = driver.current_url or ""
            ready_state = driver.execute_script("return document.readyState") or ""
        except Exception as exc:
            last_state = {"error": str(exc)}
            time.sleep(UC_READY_POLL_SECONDS)
            continue
        last_state = {"title": title, "url": current_url, "ready_state": ready_state}
        title_ok = bool(title.strip()) and "Access Denied" not in title
        ready_ok = ready_state in {"interactive", "complete"}
        if title_ok and ready_ok:
            return True, last_state
        time.sleep(UC_READY_POLL_SECONDS)
    return False, last_state


def fetch_bsr_uc_url(driver, url, offset, attempt=1):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[Lowes BSR] {PRODUCT_GROUP} offset={offset} UC GET {url}")
    started_at = now()
    start = time.time()
    error = ""
    try:
        driver.get(url)
        if UC_WAIT_SECONDS:
            time.sleep(UC_WAIT_SECONDS)
        ready, ready_state = wait_for_bsr_ready(driver)
        body = driver.page_source or ""
        if not ready:
            error = f"uc_ready_timeout last_state={ready_state}"
    except Exception as exc:
        body = ""
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - start
    rows, source = parse_bsr_at_offset(body, offset) if body else ([], "")
    success = bool(rows)
    status_code = 200 if success else ""
    status_name = f"offset_{offset:03d}_attempt_{attempt:02d}_{'success' if success else 'fail'}"
    unit_dir = OUT_DIR / status_name
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "body.html").write_text(redact_sensitive(body or error), encoding="utf-8", errors="replace")
    (unit_dir / "meta.json").write_text(
        json.dumps(
            {
                "url": url,
                "offset": offset,
                "attempt": attempt,
                "status_code": status_code,
                "success": success,
                "transport": "uc",
                "elapsed_seconds": round(elapsed, 3),
                "x_request_cost": "0",
                "bytes": len(body),
                "parsed": len(rows),
                "source": source,
                "error": "" if success else (error or "uc_empty_or_unparseable_page"),
                "started_at": started_at,
                "finished_at": now(),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"  status={status_code or 'ERR'} elapsed={elapsed:.1f}s bytes={len(body)} parsed={len(rows)}")
    return {
        "offset": offset,
        "attempt": attempt,
        "status": 200 if success else "ERR",
        "rows": rows,
        "parsed": len(rows),
        "source": source,
        "transport": "uc",
        "error": "" if success else (error or "uc_empty_or_unparseable_page"),
    }


def parse_bsr_at_offset(page_html, offset):
    """Parse BSR page and assign absolute bsr_rank = offset + rank_in_page."""
    state = extract_preloaded_state(page_html)
    bsr_products = bsr_state_products(state)
    if bsr_products:
        rows = []
        for rank_in_page, item in enumerate(bsr_products, 1):
            absolute_rank = offset + rank_in_page
            rows.append(parse_bsr_state_product(item, rank_in_page, absolute_rank))
        return rows, "bsr_preloaded_state"
    items = find_item_list(state) or []
    if items:
        html_prices = product_card_prices(page_html)
        rows = []
        for rank_in_page, item in enumerate(items, 1):
            absolute_rank = offset + rank_in_page
            row = parse_item(item, 1, rank_in_page, absolute_rank, html_prices)
            row["product_group"] = PRODUCT_GROUP
            row["bsr_rank"] = absolute_rank
            rows.append(row)
        return rows, "preloaded_state"
    cards = html_product_cards(page_html)
    rows = []
    for rank_in_page, card in enumerate(cards, 1):
        absolute_rank = offset + rank_in_page
        rows.append({
            "product_group": PRODUCT_GROUP, "bsr_rank": absolute_rank,
            "omni_item_id": card["product_id"], "description": card["title"],
            "product_url": card["url"], "source": "html_card",
        })
    return rows, "html_card"


def offset_error_result(offset, attempt, error, transport="uc"):
    return {
        "offset": offset,
        "attempt": attempt,
        "status": "ERR",
        "rows": [],
        "parsed": 0,
        "source": "",
        "transport": transport,
        "error": str(error),
    }


def _fetch_offset_zenrows(offset, attempt=1):
    url = BSR_URL + (f"?offset={offset}" if offset > 0 else "")
    try:
        response = fetch_bsr_zenrows_url(url, offset, attempt=attempt)
    except Exception as exc:
        return offset_error_result(offset, attempt, exc, transport="zenrows")
    if response.status_code != 200:
        return {
            "offset": offset,
            "attempt": attempt,
            "status": response.status_code,
            "rows": [],
            "parsed": 0,
            "source": "",
            "transport": "zenrows",
            "error": response.text[:500],
        }
    rows, source = parse_bsr_at_offset(response.text, offset)
    return {
        "offset": offset,
        "attempt": attempt,
        "status": 200,
        "rows": rows,
        "parsed": len(rows),
        "source": source,
        "transport": "zenrows",
        "error": "",
    }


def _fetch_offset(offset, attempt=1, driver=None):
    """Wrapper for parallel execution. Returns parsed rows for one offset attempt."""
    url = BSR_URL + (f"?offset={offset}" if offset > 0 else "")
    if BSR_TRANSPORT in {"uc", "browser", "uc_first"}:
        if driver is not None:
            result = fetch_bsr_uc_url(driver, url, offset, attempt=attempt)
            if not result_needs_retry(result) or not BSR_FALLBACK_ZENROWS:
                return result
            print(f"[Lowes BSR] offset={offset} UC failed; falling back to ZenRows: {result.get('error', '')}")
            return _fetch_offset_zenrows(offset, attempt=attempt)
        local_driver = None
        try:
            local_driver = launch_bsr_driver()
            return fetch_bsr_uc_url(local_driver, url, offset, attempt=attempt)
        except Exception as exc:
            if not BSR_FALLBACK_ZENROWS:
                return offset_error_result(offset, attempt, f"{type(exc).__name__}: {exc}", transport="uc")
            print(f"[Lowes BSR] offset={offset} UC failed; falling back to ZenRows: {type(exc).__name__}: {exc}")
        finally:
            try:
                if local_driver is not None:
                    local_driver.quit()
            except Exception:
                pass
    return _fetch_offset_zenrows(offset, attempt=attempt)


def fetch_offsets_zenrows(offsets, attempt, workers):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_offset_zenrows, offset, attempt): offset for offset in offsets}
        for fut in as_completed(futures):
            result = fut.result()
            results[result["offset"]] = result
    return results


def fetch_offsets(offsets, attempt, workers):
    if BSR_TRANSPORT in {"uc", "browser", "uc_first"}:
        results = {}
        try:
            driver = launch_bsr_driver()
        except Exception as exc:
            if BSR_FALLBACK_ZENROWS:
                print(f"[Lowes BSR] UC launch failed; falling back to ZenRows: {type(exc).__name__}: {exc}")
                return fetch_offsets_zenrows(offsets, attempt=attempt, workers=workers)
            error = f"UC launch failed: {type(exc).__name__}: {exc}"
            return {offset: offset_error_result(offset, attempt, error, transport="uc") for offset in offsets}
        try:
            for offset in offsets:
                results[offset] = _fetch_offset(offset, attempt=attempt, driver=driver)
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        return results

    return fetch_offsets_zenrows(offsets, attempt=attempt, workers=workers)


def result_needs_retry(result):
    if not result:
        return True
    return result.get("status") != 200 or int(result.get("parsed") or 0) <= 0


def summarize_attempt(result):
    return {
        "attempt": result.get("attempt", ""),
        "status": result.get("status", ""),
        "parsed": int(result.get("parsed") or 0),
        "source": result.get("source", ""),
        "error": str(result.get("error", ""))[:200],
    }


def keep_unique_rows(rows, seen):
    kept_rows = []
    duplicates = []
    for row in rows:
        sku = (row.get("omni_item_id") or "").strip()
        if not sku:
            continue
        if sku in seen:
            duplicates.append(
                {
                    "bsr_rank": row.get("bsr_rank", ""),
                    "omni_item_id": sku,
                    "model_id": row.get("model_id", ""),
                    "product_url": row.get("product_url", ""),
                }
            )
            continue
        seen.add(sku)
        kept_rows.append(row)
    return kept_rows, duplicates


def append_result_rows(result, seen, all_rows):
    kept_rows, duplicates = keep_unique_rows(result.get("rows") or [], seen)
    all_rows.extend(kept_rows)
    return kept_rows, duplicates


def reassign_unique_bsr_ranks(rows):
    ranked_rows = []
    for idx, row in enumerate(rows[:BSR_TARGET_UNIQUE or None], 1):
        out = dict(row)
        out.setdefault("original_bsr_rank", out.get("bsr_rank", ""))
        out["bsr_rank"] = idx
        ranked_rows.append(out)
    return ranked_rows


def main():
    workers = max(1, int(os.getenv("LOWES_BSR_WORKERS", "3")))

    all_rows = []
    seen = set()
    per_page = []
    attempt_history = {}
    results = fetch_offsets(BSR_OFFSETS, attempt=1, workers=workers)
    for offset, result in results.items():
        attempt_history[offset] = [summarize_attempt(result)]

    for attempt in range(2, BSR_RETRIES + 2):
        failed_offsets = [offset for offset in BSR_OFFSETS if result_needs_retry(results.get(offset))]
        if not failed_offsets:
            break
        print(f"[Lowes BSR] retry attempt={attempt} failed_offsets={failed_offsets}")
        if BSR_RETRY_SLEEP_SECONDS:
            time.sleep(BSR_RETRY_SLEEP_SECONDS)
        retry_results = fetch_offsets(failed_offsets, attempt=attempt, workers=min(workers, len(failed_offsets)))
        for offset, result in retry_results.items():
            attempt_history.setdefault(offset, []).append(summarize_attempt(result))
            results[offset] = result

    processed_offsets = set()

    # Process in offset order so bsr_rank ordering is stable before final re-ranking.
    for offset in BSR_OFFSETS:
        processed_offsets.add(offset)
        result = results.get(offset) or {"status": "ERR", "rows": [], "parsed": 0, "source": "", "error": "missing result"}
        status = result.get("status")
        rows = result.get("rows") or []
        attempts = attempt_history.get(offset, [])
        if status == "ERR":
            error = str(result.get("error", ""))
            print(f"  offset={offset} EXC: {error[:120]}")
            per_page.append({"offset": offset, "status": "ERR", "parsed": 0, "kept": 0, "error": error[:200], "attempts": attempts})
            continue
        if status != 200:
            per_page.append({"offset": offset, "status": status, "parsed": 0, "kept": 0, "error": str(result.get("error", ""))[:200], "attempts": attempts})
            continue
        kept_rows, duplicates = append_result_rows(result, seen, all_rows)
        per_page.append({
            "offset": offset,
            "status": 200,
            "parsed": len(rows),
            "kept": len(kept_rows),
            "duplicates": duplicates,
            "source": result.get("source", ""),
            "attempt": result.get("attempt", ""),
            "attempts": attempts,
        })

    supplemental_offsets = []
    next_offset = (max(processed_offsets) + BSR_PAGE_SIZE) if processed_offsets else 0
    while BSR_TARGET_UNIQUE and len(all_rows) < BSR_TARGET_UNIQUE and next_offset <= BSR_MAX_OFFSET:
        if next_offset in processed_offsets:
            next_offset += BSR_PAGE_SIZE
            continue
        processed_offsets.add(next_offset)
        supplemental_offsets.append(next_offset)
        result = _fetch_offset(next_offset, attempt=1)
        attempts = [summarize_attempt(result)]
        if result_needs_retry(result):
            for attempt in range(2, BSR_RETRIES + 2):
                print(f"[Lowes BSR] supplemental retry attempt={attempt} offset={next_offset}")
                if BSR_RETRY_SLEEP_SECONDS:
                    time.sleep(BSR_RETRY_SLEEP_SECONDS)
                result = _fetch_offset(next_offset, attempt=attempt)
                attempts.append(summarize_attempt(result))
                if not result_needs_retry(result):
                    break
        status = result.get("status")
        rows = result.get("rows") or []
        if status == "ERR":
            per_page.append({"offset": next_offset, "status": "ERR", "parsed": 0, "kept": 0, "error": str(result.get("error", ""))[:200], "attempts": attempts})
            break
        if status != 200:
            per_page.append({"offset": next_offset, "status": status, "parsed": 0, "kept": 0, "error": str(result.get("error", ""))[:200], "attempts": attempts})
            break
        kept_rows, duplicates = append_result_rows(result, seen, all_rows)
        per_page.append({
            "offset": next_offset,
            "status": 200,
            "parsed": len(rows),
            "kept": len(kept_rows),
            "duplicates": duplicates,
            "source": result.get("source", ""),
            "attempt": result.get("attempt", ""),
            "attempts": attempts,
            "supplemental": True,
        })
        if len(rows) <= 0:
            break
        next_offset += BSR_PAGE_SIZE

    all_rows = reassign_unique_bsr_ranks(all_rows)
    write_csv(all_rows)
    failed_offsets = [entry["offset"] for entry in per_page if entry.get("status") != 200 or int(entry.get("parsed") or 0) <= 0]
    manifest = {
        "run_type": "step03_bsr_list",
        "run_root": str(RUN_ROOT),
        "product_group": PRODUCT_GROUP,
        "bsr_url": BSR_URL,
        "offsets": sorted(processed_offsets),
        "initial_offsets": BSR_OFFSETS,
        "supplemental_offsets": supplemental_offsets,
        "target_unique_rows": BSR_TARGET_UNIQUE,
        "max_offset": BSR_MAX_OFFSET,
        "rank_mode": "dedupe_then_reassign_1_to_n",
        "transport": BSR_TRANSPORT,
        "fallback_zenrows": BSR_FALLBACK_ZENROWS,
        "retries": BSR_RETRIES,
        "failed_offsets": failed_offsets,
        "per_page": per_page,
        "total_rows_kept": len(all_rows),
        "output_csv": str(CSV_PATH),
        "raw_dir": str(OUT_DIR),
    }
    (RUN_ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nTotal unique BSR rows: {len(all_rows)} across {len(processed_offsets)} pages")
    print(f"csv={CSV_PATH}")


if __name__ == "__main__":
    main()
