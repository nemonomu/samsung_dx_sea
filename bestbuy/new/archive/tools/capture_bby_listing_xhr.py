import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from zenrows import ZenRowsClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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


def safe_json(text):
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def summarize_xhr(item):
    body = item.get("body") or ""
    request_body = item.get("request_body") or item.get("requestBody") or ""
    request_headers = item.get("request_headers") or {}
    blob = "\n".join(
        [
            item.get("url") or "",
            body[:5000],
            request_body[:5000],
            json.dumps(request_headers, ensure_ascii=False)[:5000],
        ]
    )
    operation = ""
    for candidate in ("SearchProducts", "GetProducts", "ProductListing", "GetSearchResults"):
        if candidate in blob:
            operation = candidate
            break
    return {
        "url": item.get("url"),
        "method": item.get("method"),
        "status_code": item.get("status_code"),
        "operation_hint": operation,
        "body_chars": len(body),
        "request_body_chars": len(request_body),
        "has_gateway_graphql": "/gateway/graphql" in (item.get("url") or ""),
        "has_product_connection": "ProductConnection" in blob or "products" in blob,
        "request_headers_keys": sorted(request_headers.keys()) if isinstance(request_headers, dict) else [],
    }


def main():
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
    page = int(os.getenv("BESTBUY_CAPTURE_PAGE", "1"))
    wait_ms = int(os.getenv("BESTBUY_CAPTURE_WAIT_MS", "15000"))

    # Keep the same URL builder used by listing. Search term is controlled by env.
    url = listing.build_search_url(page)
    out_dir = ROOT / "task_logs" / "bby_listing_xhr_capture" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    client = ZenRowsClient(api_key)
    params = {
        "js_render": "true",
        "json_response": "true",
        "premium_proxy": "true",
        "proxy_country": "us",
        "wait": str(wait_ms),
    }
    headers = listing.listing_headers(page, listing.ListingSessionState(), graphql=False)

    started = time.perf_counter()
    response = client.get(url, params=params, headers=headers, timeout=240)
    elapsed = round(time.perf_counter() - started, 3)
    text = response.text or ""
    data = safe_json(text)
    xhr = data.get("xhr") or []
    if not isinstance(xhr, list):
        xhr = []

    gateway = [item for item in xhr if "/gateway/graphql" in (item.get("url") or "")]
    summary = {
        "category": category,
        "search_term": search_term,
        "page": page,
        "url": url,
        "params": params,
        "status_code": response.status_code,
        "elapsed_seconds": elapsed,
        "cost": response.headers.get("x-request-cost", ""),
        "request_id": response.headers.get("x-request-id", ""),
        "content_type": response.headers.get("content-type", ""),
        "bytes": len(response.content or b""),
        "xhr_count": len(xhr),
        "gateway_graphql_count": len(gateway),
        "gateway_graphql": [summarize_xhr(item) for item in gateway],
    }

    json_dump(out_dir / "summary.json", summary)
    json_dump(out_dir / "response_headers.json", dict(response.headers))
    json_dump(out_dir / "xhr_gateway_graphql.json", gateway)
    (out_dir / "response.json").write_text(text, encoding="utf-8", errors="ignore")

    reference_path = ROOT / "references" / "page_001_request.json"
    if reference_path.exists() and gateway:
        ref = safe_json(reference_path.read_text(encoding="utf-8", errors="ignore"))
        latest = gateway[-1]
        captured_request_body = latest.get("request_body") or latest.get("requestBody") or ""
        captured = safe_json(captured_request_body)
        compare = {
            "reference_keys": sorted(ref.keys()) if isinstance(ref, dict) else [],
            "captured_keys": sorted(captured.keys()) if isinstance(captured, dict) else [],
            "reference_operationName": ref.get("operationName") if isinstance(ref, dict) else None,
            "captured_operationName": captured.get("operationName") if isinstance(captured, dict) else None,
            "reference_query_chars": len(ref.get("query") or "") if isinstance(ref, dict) else 0,
            "captured_query_chars": len(captured.get("query") or "") if isinstance(captured, dict) else 0,
            "reference_variables_keys": sorted((ref.get("variables") or {}).keys()) if isinstance(ref, dict) else [],
            "captured_variables_keys": sorted((captured.get("variables") or {}).keys()) if isinstance(captured, dict) else [],
        }
        json_dump(out_dir / "compare_reference.json", compare)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"out_dir={out_dir}")


if __name__ == "__main__":
    main()
