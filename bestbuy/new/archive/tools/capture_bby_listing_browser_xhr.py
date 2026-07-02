import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright


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


def json_dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def redacted_headers(headers):
    out = {}
    for key, value in (headers or {}).items():
        if key.lower() in SENSITIVE_HEADERS:
            out[key] = "[REDACTED]"
        else:
            out[key] = value
    return out


def safe_json(text):
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def summarize_request(item):
    body = item.get("post_data") or ""
    parsed = safe_json(body)
    variables = parsed.get("variables") if isinstance(parsed, dict) else {}
    return {
        "url": item.get("url"),
        "method": item.get("method"),
        "resource_type": item.get("resource_type"),
        "status": item.get("status"),
        "failure": item.get("failure"),
        "post_data_chars": len(body),
        "operationName": parsed.get("operationName") if isinstance(parsed, dict) else None,
        "query_chars": len(parsed.get("query") or "") if isinstance(parsed, dict) else 0,
        "variables_keys": sorted(variables.keys()) if isinstance(variables, dict) else [],
        "pagination": variables.get("pagination") if isinstance(variables, dict) else None,
        "paginationForDetailedProductSearch": (
            variables.get("paginationForDetailedProductSearch") if isinstance(variables, dict) else None
        ),
    }


async def main():
    load_dotenv(ROOT / ".env")
    os.environ.setdefault("BESTBUY_CATEGORY", "REF")
    os.environ.setdefault("BESTBUY_SEARCH_TERM", "refrigerator")
    os.environ.setdefault("BESTBUY_MAIN_SOURCE_PAYLOAD", "references/page_001_request.json")

    from bestbuy import step01_main_list as listing

    api_key = os.getenv("ZENROWS_API_KEY")
    if not api_key:
        raise RuntimeError("ZENROWS_API_KEY is missing")

    category = os.getenv("BESTBUY_CATEGORY", "REF").upper()
    search_term = os.getenv("BESTBUY_SEARCH_TERM", "refrigerator")
    page_no = int(os.getenv("BESTBUY_CAPTURE_PAGE", "1"))
    wait_ms = int(os.getenv("BESTBUY_CAPTURE_WAIT_MS", "15000"))
    scroll_steps = int(os.getenv("BESTBUY_CAPTURE_SCROLL_STEPS", "8"))
    scroll_y = int(os.getenv("BESTBUY_CAPTURE_SCROLL_Y", "1200"))
    scroll_wait_ms = int(os.getenv("BESTBUY_CAPTURE_SCROLL_WAIT_MS", "1200"))
    url = listing.build_search_url(page_no)
    out_dir = ROOT / "task_logs" / "bby_listing_browser_xhr_capture" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    captured = []
    started = time.perf_counter()
    endpoint = f"wss://browser.zenrows.com?apikey={api_key}"

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(endpoint, timeout=120000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()

        async def on_request(request):
            request_url = request.url
            if "graphql" not in request_url and request.resource_type not in {"xhr", "fetch"}:
                return
            try:
                post_data = request.post_data or ""
            except Exception:
                post_data = ""
            captured.append(
                {
                    "event": "request",
                    "url": request_url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "headers": redacted_headers(await request.all_headers()),
                    "post_data": post_data,
                }
            )

        async def on_response(response):
            request = response.request
            request_url = response.url
            if "graphql" not in request_url and request.resource_type not in {"xhr", "fetch"}:
                return
            body_text = ""
            try:
                body_text = await response.text()
            except Exception as exc:
                body_text = f"[response_text_error] {exc}"
            captured.append(
                {
                    "event": "response",
                    "url": request_url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "status": response.status,
                    "headers": redacted_headers(response.headers),
                    "body": body_text[:800000],
                    "body_chars": len(body_text),
                }
            )

        async def on_request_failed(request):
            request_url = request.url
            if "graphql" not in request_url and request.resource_type not in {"xhr", "fetch"}:
                return
            captured.append(
                {
                    "event": "requestfailed",
                    "url": request_url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "failure": request.failure,
                }
            )

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

        await page.goto(url, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(wait_ms)
        for _ in range(max(0, scroll_steps)):
            await page.mouse.wheel(0, scroll_y)
            await page.wait_for_timeout(scroll_wait_ms)
        await page.wait_for_timeout(wait_ms)
        html = await page.content()
        screenshot_path = out_dir / "page.png"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True, timeout=60000)
        except Exception:
            screenshot_path = None
        await browser.close()

    elapsed = round(time.perf_counter() - started, 3)
    graphql_requests = [
        item
        for item in captured
        if item.get("event") == "request" and "/gateway/graphql" in (item.get("url") or "")
    ]
    graphql_responses = [
        item
        for item in captured
        if item.get("event") == "response" and "/gateway/graphql" in (item.get("url") or "")
    ]

    summary = {
        "category": category,
        "search_term": search_term,
        "page": page_no,
        "url": url,
        "elapsed_seconds": elapsed,
        "wait_ms": wait_ms,
        "scroll_steps": scroll_steps,
        "scroll_y": scroll_y,
        "scroll_wait_ms": scroll_wait_ms,
        "captured_count": len(captured),
        "graphql_request_count": len(graphql_requests),
        "graphql_response_count": len(graphql_responses),
        "graphql_requests": [summarize_request(item) for item in graphql_requests],
        "html_chars": len(html),
        "html_skuId_count": html.count("skuId"),
        "screenshot": str(screenshot_path) if screenshot_path else "",
    }

    json_dump(out_dir / "summary.json", summary)
    json_dump(out_dir / "captured_network.json", captured)
    (out_dir / "page.html").write_text(html, encoding="utf-8", errors="ignore")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"out_dir={out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
