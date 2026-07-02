import json
import os
import time
from datetime import datetime
from pathlib import Path

from requests import RequestException
from zenrows import ZenRowsClient

from . import step01_main_list as listing


def now():
    return datetime.now().isoformat(timespec="seconds")


def load_dotenv_if_needed():
    if os.getenv("ZENROWS_API_KEY"):
        return
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def out_root():
    root = Path(os.getenv("BESTBUY_PROBE_OUT", "task_logs/bby_listing_graphql_probe"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def response_header(headers, name):
    target = name.lower()
    for key, value in dict(headers or {}).items():
        if key.lower() == target:
            return value
    return ""


def redact_headers(headers):
    return listing.redacted_response_headers(headers)


def request_evidence(params, headers):
    return listing.request_evidence(params, headers)


def bootstrap_profiles(session_id):
    return [
        {
            "name": "manual_render_wait5000",
            "params": {
                "custom_headers": "true",
                "premium_proxy": "true",
                "proxy_country": "us",
                "js_render": "true",
                "wait": "5000",
                "session_id": str(session_id),
            },
        },
        {
            "name": "auto_wait5000",
            "params": {
                "custom_headers": "true",
                "mode": "auto",
                "proxy_country": "us",
                "wait": "5000",
                "session_id": str(session_id),
            },
        },
    ]


def graph_params(session_id):
    mode_auto = os.getenv("BESTBUY_PROBE_GRAPH_MODE_AUTO", "1").lower() in {"1", "true", "yes", "y"}
    if mode_auto:
        return {
            "custom_headers": "true",
            "mode": "auto",
            "proxy_country": "us",
            "wait": os.getenv("BESTBUY_PROBE_GRAPH_WAIT_MS", "5000"),
            "session_id": str(session_id),
        }
    return {
        "custom_headers": "true",
        "premium_proxy": "true",
        "proxy_country": "us",
        "js_render": "true",
        "wait": os.getenv("BESTBUY_PROBE_GRAPH_WAIT_MS", "5000"),
        "session_id": str(session_id),
    }


def run_bootstrap(client, page, profile, session_state, root):
    url = os.getenv("BESTBUY_PROBE_BOOTSTRAP_URL") or listing.build_search_url(page)
    headers = listing.listing_headers(page, session_state, graphql=False)
    started = now()
    start = time.perf_counter()
    record = {
        "stage": "bootstrap",
        "profile": profile["name"],
        "url": url,
        "started_at": started,
        "request_params": dict(profile["params"]),
        **request_evidence(profile["params"], headers),
    }
    try:
        response = client.get(url, params=profile["params"], headers=headers, timeout=listing.REQUEST_TIMEOUT)
        text = response.text or ""
        received = listing.parse_zr_cookies(response.headers.get("Zr-Cookies", ""))
        record.update(
            {
                "finished_at": now(),
                "elapsed_seconds": round(time.perf_counter() - start, 3),
                "status_code": response.status_code,
                "x_request_cost": response.headers.get("x-request-cost", ""),
                "x_request_id": response.headers.get("x-request-id", ""),
                "zr_cookies_count": len(received),
                "zr_cookie_names": sorted(received),
                "concurrency_limit": response_header(response.headers, "Concurrency-Limit"),
                "concurrency_remaining": response_header(response.headers, "Concurrency-Remaining"),
                "headers": redact_headers(response.headers),
                "body_preview": text[:500],
                "bytes": len(text.encode("utf-8", errors="ignore")),
            }
        )
        write_json(root / f"bootstrap_{profile['name']}.json", record)
        return response, received, record
    except RequestException as exc:
        record.update(
            {
                "finished_at": now(),
                "elapsed_seconds": round(time.perf_counter() - start, 3),
                "status_code": "ERR",
                "error": str(exc),
            }
        )
        write_json(root / f"bootstrap_{profile['name']}.json", record)
        return None, {}, record


def run_graphql(client, page, payload, session_state, params, root):
    headers = listing.listing_headers(page, session_state, graphql=True)
    started = now()
    start = time.perf_counter()
    record = {
        "stage": "graphql",
        "url": listing.GRAPHQL_ENDPOINT,
        "started_at": started,
        "request_params": dict(params),
        **request_evidence(params, headers),
    }
    try:
        response = client.post(
            listing.GRAPHQL_ENDPOINT,
            params=params,
            headers=headers,
            data=json.dumps(payload),
            timeout=listing.REQUEST_TIMEOUT,
        )
        text = response.text or ""
        response_json = {}
        try:
            response_json = response.json()
        except ValueError:
            pass
        rows = listing.parse_page_rows(page, response_json) if response.status_code == 200 else []
        record.update(
            {
                "finished_at": now(),
                "elapsed_seconds": round(time.perf_counter() - start, 3),
                "status_code": response.status_code,
                "x_request_cost": response.headers.get("x-request-cost", ""),
                "x_request_id": response.headers.get("x-request-id", ""),
                "zenrows_error_code": listing.zenrows_error_code(response_json),
                "row_count": len(rows),
                "unique_skus": len({row.get("sku_id") for row in rows if row.get("sku_id")}),
                "concurrency_limit": response_header(response.headers, "Concurrency-Limit"),
                "concurrency_remaining": response_header(response.headers, "Concurrency-Remaining"),
                "headers": redact_headers(response.headers),
                "body_preview": text[:500],
                "bytes": len(text.encode("utf-8", errors="ignore")),
            }
        )
        write_json(root / "graphql_post.json", record)
        return record
    except RequestException as exc:
        record.update(
            {
                "finished_at": now(),
                "elapsed_seconds": round(time.perf_counter() - start, 3),
                "status_code": "ERR",
                "error": str(exc),
            }
        )
        write_json(root / "graphql_post.json", record)
        return record


def main():
    load_dotenv_if_needed()
    api_key = os.getenv("ZENROWS_API_KEY")
    if not api_key:
        raise RuntimeError("Set ZENROWS_API_KEY")
    page = int(os.getenv("BESTBUY_PROBE_PAGE", "1"))
    root = out_root() / datetime.now().strftime("%Y%m%d_%H%M%S")
    root.mkdir(parents=True, exist_ok=True)
    client = ZenRowsClient(api_key)
    operation = listing.load_product_list_operation()
    payload = listing.prepare_product_list_payload(operation, page)
    session_state = listing.ListingSessionState()
    summary = {"root": str(root), "page": page, "bootstrap": [], "graphql": None}

    winning_cookies = {}
    winning_profile = ""
    for profile in bootstrap_profiles(session_state.session_id):
        _, cookies, record = run_bootstrap(client, page, profile, session_state, root)
        summary["bootstrap"].append(
            {
                "profile": profile["name"],
                "status_code": record.get("status_code"),
                "elapsed_seconds": record.get("elapsed_seconds"),
                "x_request_cost": record.get("x_request_cost", ""),
                "zr_cookies_count": record.get("zr_cookies_count", 0),
                "zr_cookie_names": record.get("zr_cookie_names", []),
                "concurrency_remaining": record.get("concurrency_remaining", ""),
                "error": record.get("error", ""),
            }
        )
        print(
            f"bootstrap profile={profile['name']} status={record.get('status_code')} "
            f"cookies={record.get('zr_cookies_count', 0)} cost={record.get('x_request_cost', '')} "
            f"elapsed={record.get('elapsed_seconds')}s",
            flush=True,
        )
        if cookies:
            winning_cookies = cookies
            winning_profile = profile["name"]
            break

    if winning_cookies:
        session_state.cookies = winning_cookies
        gql_record = run_graphql(client, page, payload, session_state, graph_params(session_state.session_id), root)
        summary["graphql"] = {
            "bootstrap_profile": winning_profile,
            "status_code": gql_record.get("status_code"),
            "elapsed_seconds": gql_record.get("elapsed_seconds"),
            "x_request_cost": gql_record.get("x_request_cost", ""),
            "zenrows_error_code": gql_record.get("zenrows_error_code", ""),
            "row_count": gql_record.get("row_count", 0),
            "unique_skus": gql_record.get("unique_skus", 0),
            "request_cookie_present": gql_record.get("request_cookie_present"),
            "request_cookie_count": gql_record.get("request_cookie_count"),
            "request_cookie_names": gql_record.get("request_cookie_names", []),
            "concurrency_remaining": gql_record.get("concurrency_remaining", ""),
        }
        print(
            f"graphql status={gql_record.get('status_code')} code={gql_record.get('zenrows_error_code', '')} "
            f"rows={gql_record.get('row_count', 0)} cookie={gql_record.get('request_cookie_present')} "
            f"cookie_count={gql_record.get('request_cookie_count')} cost={gql_record.get('x_request_cost', '')}",
            flush=True,
        )
    else:
        print("graphql skipped: no Zr-Cookies from bootstrap profiles", flush=True)

    write_json(root / "summary.json", summary)
    print(f"summary={root / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
