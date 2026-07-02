import asyncio
import json
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from zenrows import ZenRowsClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SENSITIVE_HEADERS = {"cookie", "authorization", "x-api-key", "apikey"}


def load_dotenv(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_candidate_dotenvs():
    for path in (ROOT / ".env", ROOT.parent / ".env", ROOT.parent.parent / ".env"):
        load_dotenv(path)


def json_dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def safe_json(text):
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def redacted_headers(headers):
    out = {}
    for key, value in (headers or {}).items():
        if key.lower() in SENSITIVE_HEADERS:
            out[key] = "[REDACTED]"
        else:
            out[key] = value
    return out


def response_rows(listing, response_json):
    try:
        return listing.response_products(response_json)
    except Exception:
        return []


def status_summary(response, elapsed, response_json):
    body = response.text or ""
    return {
        "status_code": response.status_code,
        "elapsed_seconds": elapsed,
        "cost": response.headers.get("x-request-cost", ""),
        "request_id": response.headers.get("x-request-id", ""),
        "concurrency_limit": response.headers.get("Concurrency-Limit", ""),
        "concurrency_remaining": response.headers.get("Concurrency-Remaining", ""),
        "bytes": len(response.content or b""),
        "body_head": body[:300],
        "zenrows_code": response_json.get("code") if isinstance(response_json, dict) else "",
    }


def direct_headers(referer):
    return {
        "accept": "application/graphql-response+json, application/json",
        "content-type": "application/json",
        "origin": "https://www.bestbuy.com",
        "referer": referer,
    }


def direct_params(session_id):
    return {
        "custom_headers": "true",
        "mode": "auto",
        "proxy_country": "us",
        "wait": os.getenv("BESTBUY_RESCUE_DIRECT_WAIT_MS", "5000"),
        "session_id": str(session_id),
    }


def build_payloads(listing, pages):
    operation = listing.load_product_list_operation()
    return {page: listing.prepare_product_list_payload(operation, page) for page in pages}


def direct_post_test(listing, client, payloads, out_dir):
    results = []
    session_id = int(os.getenv("BESTBUY_RESCUE_SESSION_ID", str(1000 + secrets.randbelow(9000))))
    sleep_seconds = float(os.getenv("BESTBUY_RESCUE_DIRECT_SLEEP_SECONDS", "8"))
    for page, payload in payloads.items():
        referer = listing.build_search_url(page)
        params = direct_params(session_id)
        headers = direct_headers(referer)
        started = time.perf_counter()
        response = client.post(
            "https://www.bestbuy.com/gateway/graphql",
            params=params,
            headers=headers,
            data=json.dumps(payload),
            timeout=int(os.getenv("ZENROWS_TIMEOUT", "240")),
        )
        elapsed = round(time.perf_counter() - started, 3)
        response_json = safe_json(response.text)
        rows = response_rows(listing, response_json)
        item = {
            "page": page,
            "referer": referer,
            "request_params": params,
            "request_headers": redacted_headers(headers),
            **status_summary(response, elapsed, response_json),
            "row_count": len(rows),
            "unique_sku_count": len({str(row.get("skuId") or row.get("sku_id") or "") for row in rows if row}),
        }
        results.append(item)
        json_dump(out_dir / f"direct_page_{page:03d}_request.json", payload)
        json_dump(out_dir / f"direct_page_{page:03d}_response.json", response_json)
        print(
            f"[direct] page={page:03d} status={item['status_code']} rows={item['row_count']} "
            f"cost={item['cost']} elapsed={elapsed}s code={item['zenrows_code']}",
            flush=True,
        )
        if page != list(payloads)[-1] and sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return results


async def browser_context_fetch_test(listing, api_key, payloads, out_dir):
    results = []
    wait_ms = int(os.getenv("BESTBUY_RESCUE_BROWSER_WAIT_MS", "5000"))
    endpoint = f"wss://browser.zenrows.com?apikey={api_key}&proxy_country=us"
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(endpoint, timeout=120000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        for page, payload in payloads.items():
            page_obj = await context.new_page()
            referer = listing.build_search_url(page)
            started = time.perf_counter()
            try:
                await page_obj.goto(referer, wait_until="domcontentloaded", timeout=120000)
                await page_obj.wait_for_timeout(wait_ms)
                fetch_result = await page_obj.evaluate(
                    """async ({ payload }) => {
                        try {
                            const response = await fetch('/gateway/graphql', {
                                method: 'POST',
                                credentials: 'include',
                                headers: {
                                    'accept': 'application/graphql-response+json, application/json',
                                    'content-type': 'application/json',
                                    'x-client-id': 'plp-web',
                                    'x-requested-for-operation-name': payload.operationName || ''
                                },
                                body: JSON.stringify(payload)
                            });
                            const text = await response.text();
                            return {
                                status: response.status,
                                ok: response.ok,
                                contentType: response.headers.get('content-type') || '',
                                body: text,
                                error: ''
                            };
                        } catch (error) {
                            return {
                                status: 0,
                                ok: false,
                                contentType: '',
                                body: '',
                                error: String(error && error.message ? error.message : error)
                            };
                        }
                    }""",
                    {"payload": payload},
                )
            except Exception as exc:
                fetch_result = {
                    "status": 0,
                    "ok": False,
                    "contentType": "",
                    "body": "",
                    "error": str(exc),
                }
            elapsed = round(time.perf_counter() - started, 3)
            response_json = safe_json(fetch_result.get("body") or "")
            rows = response_rows(listing, response_json)
            item = {
                "page": page,
                "referer": referer,
                "status_code": fetch_result.get("status"),
                "ok": fetch_result.get("ok"),
                "content_type": fetch_result.get("contentType"),
                "elapsed_seconds": elapsed,
                "bytes": len((fetch_result.get("body") or "").encode("utf-8")),
                "body_head": (fetch_result.get("body") or "")[:300],
                "error": fetch_result.get("error") or "",
                "row_count": len(rows),
                "unique_sku_count": len({str(row.get("skuId") or row.get("sku_id") or "") for row in rows if row}),
            }
            results.append(item)
            json_dump(out_dir / f"browser_page_{page:03d}_request.json", payload)
            json_dump(out_dir / f"browser_page_{page:03d}_response.json", response_json)
            print(
                f"[browser_fetch] page={page:03d} status={item['status_code']} rows={item['row_count']} "
                f"elapsed={elapsed}s",
                flush=True,
            )
            await page_obj.close()
        await browser.close()
    return results


async def main():
    load_candidate_dotenvs()
    os.environ.setdefault("BESTBUY_CATEGORY", "TV")
    os.environ.setdefault("BESTBUY_SEARCH_TERM", "tv")
    os.environ.setdefault("BESTBUY_MAIN_SOURCE_PAYLOAD", "references/page_001_request.json")
    os.environ.setdefault("BESTBUY_GRAPHQL_MODE_AUTO", "1")

    from bestbuy import step01_main_list as listing

    api_key = os.getenv("ZENROWS_API_KEY")
    if not api_key:
        raise RuntimeError("ZENROWS_API_KEY is missing")

    pages = [int(value) for value in os.getenv("BESTBUY_RESCUE_PAGES", "1,2,3").split(",") if value.strip()]
    out_dir = ROOT / "task_logs" / "bby_listing_graphql_rescue" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    payloads = build_payloads(listing, pages)

    client = ZenRowsClient(api_key)
    direct_results = direct_post_test(listing, client, payloads, out_dir)
    direct_failures = sum(1 for item in direct_results if item.get("status_code") != 200 or item.get("row_count", 0) <= 0)
    direct_failure_rate = direct_failures / max(1, len(direct_results))
    run_browser = direct_failure_rate >= float(os.getenv("BESTBUY_RESCUE_BROWSER_FALLBACK_THRESHOLD", "0.34"))

    browser_results = []
    if run_browser:
        print(
            f"[circuit_breaker] direct_failure_rate={direct_failure_rate:.2f}; "
            "running browser-context GraphQL fallback",
            flush=True,
        )
        browser_results = await browser_context_fetch_test(listing, api_key, payloads, out_dir)

    summary = {
        "category": os.getenv("BESTBUY_CATEGORY", ""),
        "search_term": os.getenv("BESTBUY_SEARCH_TERM", ""),
        "pages": pages,
        "direct_failure_rate": direct_failure_rate,
        "browser_fallback_run": run_browser,
        "direct": direct_results,
        "browser_context_fetch": browser_results,
    }
    json_dump(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"out_dir={out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
