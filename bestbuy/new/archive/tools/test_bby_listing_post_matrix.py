import json
import os
import secrets
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


def load_candidate_dotenvs():
    for path in (ROOT / ".env", ROOT.parent / ".env", ROOT.parent.parent / ".env"):
        load_dotenv(path)


def safe_json(text):
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def json_dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def base_payload(page=1):
    from bestbuy import step01_main_list as listing

    operation = listing.load_product_list_operation()
    return listing.prepare_product_list_payload(operation, page)


def page_url(page=1):
    from bestbuy import step01_main_list as listing

    return listing.build_search_url(page)


def row_count(response_json, page=1):
    from bestbuy import step01_main_list as listing

    try:
        return len(listing.parse_page_rows(page, response_json))
    except Exception:
        return 0


def profiles(session_id):
    return [
        {
            "name": "custom_only_browser_accept",
            "params": {"custom_headers": "true"},
            "accept": "application/json, text/plain, */*",
        },
        {
            "name": "custom_only_graphql_accept",
            "params": {"custom_headers": "true"},
            "accept": "application/graphql-response+json, application/json",
        },
        {
            "name": "premium_only",
            "params": {"custom_headers": "true", "premium_proxy": "true", "proxy_country": "us"},
            "accept": "application/json, text/plain, */*",
        },
        {
            "name": "js_only_wait",
            "params": {"custom_headers": "true", "js_render": "true", "wait": "5000"},
            "accept": "application/json, text/plain, */*",
        },
        {
            "name": "premium_js_no_wait",
            "params": {"custom_headers": "true", "premium_proxy": "true", "proxy_country": "us", "js_render": "true"},
            "accept": "application/json, text/plain, */*",
        },
        {
            "name": "premium_js_wait",
            "params": {
                "custom_headers": "true",
                "premium_proxy": "true",
                "proxy_country": "us",
                "js_render": "true",
                "wait": "5000",
            },
            "accept": "application/json, text/plain, */*",
        },
        {
            "name": "premium_js_wait_original_status",
            "params": {
                "custom_headers": "true",
                "premium_proxy": "true",
                "proxy_country": "us",
                "js_render": "true",
                "wait": "5000",
                "original_status": "true",
            },
            "accept": "application/json, text/plain, */*",
        },
        {
            "name": "mode_auto_no_wait",
            "params": {"custom_headers": "true", "mode": "auto", "proxy_country": "us"},
            "accept": "application/json, text/plain, */*",
        },
        {
            "name": "mode_auto_wait",
            "params": {"custom_headers": "true", "mode": "auto", "proxy_country": "us", "wait": "5000"},
            "accept": "application/json, text/plain, */*",
        },
        {
            "name": "mode_auto_session",
            "params": {
                "custom_headers": "true",
                "mode": "auto",
                "proxy_country": "us",
                "session_id": str(session_id),
            },
            "accept": "application/json, text/plain, */*",
        },
        {
            "name": "premium_js_session",
            "params": {
                "custom_headers": "true",
                "premium_proxy": "true",
                "proxy_country": "us",
                "js_render": "true",
                "session_id": str(session_id),
            },
            "accept": "application/json, text/plain, */*",
        },
    ]


def headers(referer, accept):
    return {
        "accept": accept,
        "content-type": "application/json",
        "origin": "https://www.bestbuy.com",
        "referer": referer,
    }


def main():
    load_candidate_dotenvs()
    os.environ.setdefault("BESTBUY_CATEGORY", "TV")
    os.environ.setdefault("BESTBUY_SEARCH_TERM", "tv")
    os.environ.setdefault("BESTBUY_MAIN_SOURCE_PAYLOAD", "references/page_001_request.json")

    api_key = os.getenv("ZENROWS_API_KEY")
    if not api_key:
        raise RuntimeError("ZENROWS_API_KEY is missing")

    page = int(os.getenv("BESTBUY_MATRIX_PAGE", "1"))
    payload = base_payload(page)
    referer = page_url(page)
    client = ZenRowsClient(api_key)
    session_id = int(os.getenv("BESTBUY_MATRIX_SESSION_ID", str(1000 + secrets.randbelow(9000))))
    out_dir = ROOT / "task_logs" / "bby_listing_post_matrix" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []

    for profile in profiles(session_id):
        name = profile["name"]
        params = profile["params"]
        request_headers = headers(referer, profile["accept"])
        started = time.perf_counter()
        response = client.post(
            "https://www.bestbuy.com/gateway/graphql",
            params=params,
            headers=request_headers,
            data=json.dumps(payload),
            timeout=int(os.getenv("ZENROWS_TIMEOUT", "240")),
        )
        elapsed = round(time.perf_counter() - started, 3)
        response_json = safe_json(response.text)
        rows = row_count(response_json, page) if response.status_code == 200 else 0
        item = {
            "name": name,
            "page": page,
            "params": params,
            "accept": profile["accept"],
            "status_code": response.status_code,
            "zenrows_code": response_json.get("code") if isinstance(response_json, dict) else "",
            "cost": response.headers.get("x-request-cost", ""),
            "elapsed_seconds": elapsed,
            "bytes": len(response.content or b""),
            "row_count": rows,
            "body_head": (response.text or "")[:300],
        }
        summary.append(item)
        json_dump(out_dir / f"{name}_response.json", response_json)
        print(
            f"[matrix] {name} status={item['status_code']} rows={rows} "
            f"cost={item['cost']} elapsed={elapsed}s code={item['zenrows_code']}",
            flush=True,
        )
        sleep_seconds = float(os.getenv("BESTBUY_MATRIX_SLEEP_SECONDS", "5"))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    json_dump(out_dir / "request.json", payload)
    json_dump(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"out_dir={out_dir}")


if __name__ == "__main__":
    main()
