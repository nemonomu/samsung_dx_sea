import atexit
import csv
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from requests import RequestException
from zenrows import ZenRowsClient

from .step00_config import (
    DEFAULT_BESTBUY_RUN_ROOT,
    apply_bestbuy_location,
    bestbuy_category,
    load_initial_urls,
    old_pdp_url,
    rel_path,
    search_term_from_url,
    url_for_page,
)
from .step00_graphql_query import sanitize_product_list_query
from .step00_parse_pdp import absolute_bestbuy_url, extract_apollo_payloads, first_nested, nested_get
from .step00_parse_search import merge_dict, parse_product as parse_search_product

BESTBUY_BASE_URL = "https://www.bestbuy.com"
GRAPHQL_ENDPOINT = os.getenv("BESTBUY_GRAPHQL_ENDPOINT", "https://www.bestbuy.com/gateway/graphql")
SEARCH_SORT = os.getenv("BESTBUY_SEARCH_SORT", "")
SEARCH_PAGES = int(os.getenv("BESTBUY_MAIN_PAGES", "13"))
ORGANIC_OFFSET = int(os.getenv("BESTBUY_MAIN_ORGANIC_OFFSET", "18"))
INCLUDE_SPONSORED_CAROUSEL = os.getenv("BESTBUY_INCLUDE_SPONSORED_CAROUSEL", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
REQUEST_TIMEOUT = int(os.getenv("ZENROWS_TIMEOUT", "120"))
FETCH_MODE = os.getenv("BESTBUY_FETCH_MODE", os.getenv("BESTBUY_GRAPHQL_FETCH_MODE", "zenrows")).strip().lower()
LISTING_COLLECTION_MODE = os.getenv("BESTBUY_LISTING_COLLECTION_MODE", "dom").strip().lower()
LISTING_WAIT_MS = max(0, int(os.getenv("BESTBUY_LISTING_WAIT_MS", "15000")))
BROWSER_GRAPHQL_WAIT_SECONDS = max(0.0, float(os.getenv("BESTBUY_BROWSER_GRAPHQL_WAIT_SECONDS", "8")))
BROWSER_GRAPHQL_JS_TIMEOUT = max(1, int(os.getenv("BESTBUY_BROWSER_GRAPHQL_JS_TIMEOUT", "120")))
BROWSER_GRAPHQL_HEADLESS = os.getenv("BESTBUY_BROWSER_GRAPHQL_HEADLESS", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
BROWSER_GRAPHQL_LOCAL_PORT = int(os.getenv("BESTBUY_BROWSER_GRAPHQL_LOCAL_PORT", "0") or "0")
BROWSER_GRAPHQL_NAVIGATE_EACH_PAGE = os.getenv(
    "BESTBUY_BROWSER_GRAPHQL_NAVIGATE_EACH_PAGE",
    "0",
).lower() in {"1", "true", "yes", "y"}
LISTING_MAX_ATTEMPTS = max(1, int(os.getenv("BESTBUY_LISTING_MAX_ATTEMPTS", "5")))
LISTING_RETRY_SLEEP_SECONDS = float(os.getenv("BESTBUY_LISTING_RETRY_SLEEP_SECONDS", "2"))
LISTING_RETRY_MAX_SLEEP_SECONDS = max(
    LISTING_RETRY_SLEEP_SECONDS,
    float(os.getenv("BESTBUY_LISTING_RETRY_MAX_SLEEP_SECONDS", "8")),
)
LISTING_RETRY_SLEEP_SEQUENCE = [
    max(0.0, float(value))
    for value in re.split(r"[,\s]+", os.getenv("BESTBUY_LISTING_RETRY_SLEEP_SEQUENCE", ""))
    if value.strip()
]
LISTING_PAGE_SLEEP_SECONDS = max(0.0, float(os.getenv("BESTBUY_LISTING_PAGE_SLEEP_SECONDS", "0")))
LISTING_RETRY_STATUS_CODES = {
    int(value)
    for value in re.split(
        r"[,\s]+",
        os.getenv("BESTBUY_LISTING_RETRY_STATUS_CODES", "408,425,429,500,502,503,504"),
    )
    if value.strip().isdigit()
}
LISTING_RECOVERY_ENABLED = os.getenv("BESTBUY_LISTING_RECOVERY_ENABLED", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
LISTING_RECOVERY_PROFILE_NAMES = [
    value.strip().lower()
    for value in re.split(
        r"[,\s]+",
        os.getenv("BESTBUY_LISTING_RECOVERY_PROFILES", "wait,session_wait,auto"),
    )
    if value.strip()
]
LISTING_RECOVERY_ATTEMPTS_PER_PROFILE = max(
    1,
    int(os.getenv("BESTBUY_LISTING_RECOVERY_ATTEMPTS_PER_PROFILE", "2")),
)
LISTING_RECOVERY_WAIT_MS = max(0, int(os.getenv("BESTBUY_LISTING_RECOVERY_WAIT_MS", "5000")))
LISTING_COUNTRY_CLICK_ENABLED = os.getenv("BESTBUY_LISTING_COUNTRY_CLICK", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
LISTING_COUNTRY_CLICK_SELECTOR = os.getenv("BESTBUY_LISTING_COUNTRY_CLICK_SELECTOR", ".us-link").strip()
LISTING_FAILED_PAGE_RETRY_ROUNDS = max(0, int(os.getenv("BESTBUY_LISTING_FAILED_PAGE_RETRY_ROUNDS", "2")))
LISTING_FAILED_PAGE_RETRY_SLEEP_SECONDS = [
    max(0.0, float(value))
    for value in re.split(r"[,\s]+", os.getenv("BESTBUY_LISTING_FAILED_PAGE_RETRY_SLEEP_SECONDS", "30"))
    if value.strip()
]
LISTING_DEBUG_SCREENSHOT_MODE = os.getenv("BESTBUY_LISTING_DEBUG_SCREENSHOT", "0").strip().lower()
LISTING_DEBUG_SCREENSHOT_ENABLED = LISTING_DEBUG_SCREENSHOT_MODE in {
    "1",
    "true",
    "yes",
    "y",
    "failed",
    "all",
}
LISTING_DEBUG_SCREENSHOT_WAIT_MS = max(0, int(os.getenv("BESTBUY_LISTING_DEBUG_SCREENSHOT_WAIT_MS", "15000")))
LISTING_HTML_FALLBACK_ENABLED = os.getenv("BESTBUY_LISTING_HTML_FALLBACK_ENABLED", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
LISTING_HTML_FALLBACK_WAIT_MS = max(0, int(os.getenv("BESTBUY_LISTING_HTML_FALLBACK_WAIT_MS", "15000")))
LISTING_HTML_FALLBACK_MIN_ROWS = max(0, int(os.getenv("BESTBUY_LISTING_HTML_FALLBACK_MIN_ROWS", "24")))
LISTING_HTML_FALLBACK_SCROLL_ENABLED = os.getenv("BESTBUY_LISTING_HTML_FALLBACK_SCROLL", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
LISTING_HTML_FALLBACK_SCROLL_STEPS = max(0, int(os.getenv("BESTBUY_LISTING_HTML_FALLBACK_SCROLL_STEPS", "3")))
LISTING_HTML_FALLBACK_SCROLL_Y = max(1, int(os.getenv("BESTBUY_LISTING_HTML_FALLBACK_SCROLL_Y", "1400")))
LISTING_HTML_FALLBACK_SCROLL_WAIT_MS = max(0, int(os.getenv("BESTBUY_LISTING_HTML_FALLBACK_SCROLL_WAIT_MS", "1200")))
LISTING_HTML_FALLBACK_SCROLL_FINAL_WAIT_MS = max(
    0,
    int(os.getenv("BESTBUY_LISTING_HTML_FALLBACK_SCROLL_FINAL_WAIT_MS", "1600")),
)
LISTING_HTML_FALLBACK_SCROLL_RESET_TOP = os.getenv("BESTBUY_LISTING_HTML_FALLBACK_SCROLL_RESET_TOP", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
SANITIZE_PRODUCT_LIST_QUERY = os.getenv("BESTBUY_SANITIZE_PRODUCT_LIST_QUERY", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
STRIP_PRODUCT_LIST_FULFILLMENT = os.getenv("BESTBUY_STRIP_PRODUCT_LIST_FULFILLMENT", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
LISTING_SESSION_ENABLED = os.getenv("BESTBUY_LISTING_SESSION_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
LISTING_SESSION_MAX_AGE_SECONDS = max(
    60,
    int(os.getenv("BESTBUY_LISTING_SESSION_MAX_AGE_SECONDS", "480")),
)
LISTING_SESSION_BOOTSTRAP = os.getenv("BESTBUY_LISTING_SESSION_BOOTSTRAP", "0").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
RUN_DATE = os.getenv("BESTBUY_RUN_DATE", datetime.now().strftime("%Y%m%d"))
RUN_ID = os.getenv("BESTBUY_MAIN_RUN_ID", "main")
RUN_ROOT = Path(os.getenv("BESTBUY_RUN_ROOT", DEFAULT_BESTBUY_RUN_ROOT)) / RUN_ID
SOURCE_HTML_PATH = Path(os.getenv("BESTBUY_MAIN_SOURCE_HTML", "references/bestbuy_main_search_page_sample.html"))
SOURCE_PAYLOAD_PATH = Path(os.getenv("BESTBUY_MAIN_SOURCE_PAYLOAD", "references/page_001_request.json"))
ALLOW_HTML_TEMPLATE = os.getenv("BESTBUY_MAIN_ALLOW_HTML_TEMPLATE", "0").lower() in {"1", "true", "yes", "y"}
FORCE_REFRESH = os.getenv("BESTBUY_FORCE_REFRESH", "0").lower() in {"1", "true", "yes", "y"}
CATEGORY = bestbuy_category()
URLS = load_initial_urls()
SEARCH_URL_KEY = "bsr_search" if RUN_ID == "bsr" or SEARCH_SORT == "Best-Selling" else "main_search"
SEARCH_URL_TEMPLATE = os.getenv("BESTBUY_SEARCH_URL", URLS.get(SEARCH_URL_KEY, ""))
SEARCH_TERM = os.getenv("BESTBUY_SEARCH_TERM", search_term_from_url(SEARCH_URL_TEMPLATE) or "tv")


def now():
    return datetime.now().isoformat(timespec="seconds")


def build_search_url(page):
    if SEARCH_URL_TEMPLATE:
        return bestbuy_nosplash_url(url_for_page(SEARCH_URL_TEMPLATE, page))
    query = {"id": "pcat17071", "st": SEARCH_TERM, "intl": "nosplash"}
    if SEARCH_SORT:
        query["sp"] = SEARCH_SORT
    if page > 1:
        query["cp"] = page
    return f"{BESTBUY_BASE_URL}/site/searchpage.jsp?{urlencode(query)}"


def bestbuy_nosplash_url(url):
    parts = urlsplit(str(url or ""))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["intl"] = "nosplash"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def operation_name(query):
    if not isinstance(query, str):
        return ""
    match = re.search(r"\bquery\s+([A-Za-z0-9_]+)", query)
    return match.group(1) if match else ""


def find_started_operation(html_text, target_name):
    for payload in extract_apollo_payloads(html_text):
        for event in payload.get("events", []):
            if event.get("type") != "started":
                continue
            options = event.get("options", {})
            query = options.get("query", "")
            if operation_name(query) == target_name:
                return {
                    "operationName": target_name,
                    "query": query,
                    "variables": options.get("variables", {}),
                    "extensions": options.get("extensions", {}),
                    "event_id": event.get("id", ""),
                }
    raise RuntimeError(f"Could not find Apollo operation: {target_name}")


def load_product_list_operation(target_name="PlpView_ProductList_Init"):
    source_payload_candidates = [
        SOURCE_PAYLOAD_PATH,
        Path("../../references/rdp/page_001_request.json"),
    ]
    source_html_candidates = [
        SOURCE_HTML_PATH,
        Path("../../references/bestbuy_main_search_page_sample.html"),
    ]

    for path in source_payload_candidates:
        if not path.exists():
            continue
        payload = read_json(path)
        if payload.get("operationName") != target_name:
            continue
        operation = {
            "operationName": payload["operationName"],
            "query": payload.get("query", ""),
            "variables": payload.get("variables", {}),
            "extensions": payload.get("extensions", {}),
            "event_id": "",
            "source_path": rel_path(path),
            "source_type": "payload",
        }
        if not operation["query"]:
            continue
        return operation

    if ALLOW_HTML_TEMPLATE:
        for path in source_html_candidates:
            if path.exists():
                html_text = path.read_text(encoding="utf-8", errors="replace")
                operation = find_started_operation(html_text, target_name)
                operation["source_path"] = rel_path(path)
                operation["source_type"] = "html"
                return operation

    searched = [str(path) for path in source_payload_candidates + source_html_candidates]
    raise FileNotFoundError(
        f"Could not find {target_name} GraphQL request payload. searched={searched}. "
        "Set BESTBUY_MAIN_SOURCE_PAYLOAD to a saved /gateway/graphql request body. "
        "Set BESTBUY_MAIN_ALLOW_HTML_TEMPLATE=1 only for one-off local migration."
    )


def prepare_product_list_payload(operation, page):
    variables = json.loads(json.dumps(operation["variables"]))
    for key in ("input", "detailedSearchInput"):
        if isinstance(variables.get(key), dict):
            variables[key]["query"] = SEARCH_TERM
            variables[key]["queryType"] = "SEARCH"
            variables[key]["site"] = "WWW"

    variables["categoryId"] = SEARCH_TERM
    variables["isBrowse"] = False
    variables.setdefault("sort", {})
    variables["sort"]["sort"] = SEARCH_SORT
    variables.setdefault("pagination", {})
    variables["pagination"]["pageNumber"] = page
    variables["pagination"]["offset"] = ORGANIC_OFFSET
    variables.setdefault("paginationForDetailedProductSearch", {})
    variables["paginationForDetailedProductSearch"]["pageNumber"] = page
    variables["paginationForDetailedProductSearch"]["offset"] = ORGANIC_OFFSET
    apply_bestbuy_location(variables)

    query = operation["query"]
    if SANITIZE_PRODUCT_LIST_QUERY or STRIP_PRODUCT_LIST_FULFILLMENT:
        query = sanitize_product_list_query(
            query,
            strip_fulfillment=STRIP_PRODUCT_LIST_FULFILLMENT,
        )

    extensions = operation.get("extensions") or {
        "clientLibrary": {
            "name": "@apollo/client",
            "version": "4.1.6",
        }
    }
    return {
        "operationName": operation["operationName"],
        "variables": variables,
        "query": query,
        "extensions": extensions,
    }


def new_listing_session_id():
    return secrets.randbelow(99999) + 1


def parse_zr_cookies(value):
    cookies = {}
    for part in str(value or "").split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if separator and name:
            cookies[name] = cookie_value
    return cookies


def redacted_response_headers(headers):
    redacted = dict(headers or {})
    for name in list(redacted):
        if name.lower() in {"zr-cookies", "zr-set-cookie", "set-cookie"}:
            redacted[name] = "[REDACTED]"
    return redacted


def zenrows_error_code(response_json):
    if not isinstance(response_json, dict):
        return ""
    return str(response_json.get("code") or "").strip().upper()


class ListingSessionState:
    def __init__(self):
        self.generation = 0
        self.session_id = None
        self.cookies = {}
        self.started_monotonic = 0.0
        self.bootstrapped = False
        self.last_reset_reason = ""
        self.reset("initial")

    def reset(self, reason):
        self.generation += 1
        self.session_id = new_listing_session_id() if LISTING_SESSION_ENABLED else None
        self.cookies = {}
        self.started_monotonic = time.monotonic()
        self.bootstrapped = False
        self.last_reset_reason = reason

    def expired(self):
        if not LISTING_SESSION_ENABLED:
            return False
        return time.monotonic() - self.started_monotonic >= LISTING_SESSION_MAX_AGE_SECONDS

    def update_from_headers(self, headers):
        if not LISTING_SESSION_ENABLED:
            return 0
        received = parse_zr_cookies((headers or {}).get("Zr-Cookies", ""))
        for name, value in received.items():
            if value:
                self.cookies[name] = value
            else:
                self.cookies.pop(name, None)
        return len(received)

    def cookie_header(self):
        if not LISTING_SESSION_ENABLED:
            return ""
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items())

    def metadata(self):
        return {
            "listing_session_enabled": LISTING_SESSION_ENABLED,
            "listing_session_id": self.session_id or "",
            "listing_session_generation": self.generation,
            "listing_session_cookie_count": len(self.cookies),
            "listing_session_cookie_names": sorted(self.cookies),
            "listing_session_reset_reason": self.last_reset_reason,
        }


def zenrows_mode_auto():
    return os.getenv("BESTBUY_GRAPHQL_MODE_AUTO", "0").lower() in {"1", "true", "yes", "y"}


def listing_profile_name(request_profile=None):
    if not request_profile:
        return "default"
    return str(request_profile.get("name") or "default")


def listing_recovery_profiles():
    profiles = []
    for raw_name in LISTING_RECOVERY_PROFILE_NAMES:
        if raw_name in {"default", "manual"}:
            profiles.append({"name": "default_recovery"})
        elif raw_name == "wait":
            profiles.append({"name": "wait", "wait_ms": LISTING_RECOVERY_WAIT_MS})
        elif raw_name == "session":
            profiles.append({"name": "session", "session_id": new_listing_session_id()})
        elif raw_name == "session_wait":
            profiles.append(
                {
                    "name": "session_wait",
                    "session_id": new_listing_session_id(),
                    "wait_ms": LISTING_RECOVERY_WAIT_MS,
                }
            )
        elif raw_name == "auto":
            profiles.append({"name": "auto", "mode_auto": True})
        elif raw_name == "auto_wait":
            profiles.append({"name": "auto_wait", "mode_auto": True, "wait_ms": LISTING_RECOVERY_WAIT_MS})
        else:
            print(f"[listing_recovery] unknown profile skipped: {raw_name}", flush=True)
    return profiles


def zenrows_params(session_id=None, request_profile=None):
    request_profile = request_profile or {}
    params = {"custom_headers": "true"}
    mode_auto = bool(request_profile.get("mode_auto")) or zenrows_mode_auto()
    if mode_auto:
        params["mode"] = "auto"
        params["proxy_country"] = "us"
    else:
        if os.getenv("BESTBUY_GRAPHQL_PREMIUM_PROXY", "1").lower() in {"1", "true", "yes"}:
            params["premium_proxy"] = "true"
            params["proxy_country"] = "us"
        if os.getenv("BESTBUY_GRAPHQL_JS_RENDER", "1").lower() in {"1", "true", "yes"}:
            params["js_render"] = "true"
    wait_ms = int(request_profile.get("wait_ms") if "wait_ms" in request_profile else LISTING_WAIT_MS)
    if wait_ms > 0:
        if not mode_auto:
            params["js_render"] = "true"
        params["wait"] = str(wait_ms)
    profile_session_id = request_profile.get("session_id")
    if profile_session_id:
        params["session_id"] = str(profile_session_id)
    elif LISTING_SESSION_ENABLED and session_id:
        params["session_id"] = str(session_id)
    if not mode_auto and LISTING_COUNTRY_CLICK_ENABLED and LISTING_COUNTRY_CLICK_SELECTOR:
        params["js_render"] = "true"
        params["js_instructions"] = json.dumps([{"click": LISTING_COUNTRY_CLICK_SELECTOR}])
    return params


def fetch_transports():
    if FETCH_MODE in {"zenrows", "zr"}:
        return ["zenrows"]
    raise RuntimeError("Best Buy listing collection is ZenRows GraphQL only. Set BESTBUY_FETCH_MODE=zenrows.")


def manifest_fetch_transports():
    if LISTING_COLLECTION_MODE == "dom":
        return ["zenrows_html_dom"]
    if LISTING_COLLECTION_MODE == "browser_graphql":
        return ["browser_graphql"]
    return fetch_transports()


def listing_headers(page, session_state, graphql=True, request_profile=None):
    headers = {
        "referer": build_search_url(page) if graphql else f"{BESTBUY_BASE_URL}/",
    }
    if graphql:
        headers.update(
            {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": BESTBUY_BASE_URL,
            }
        )
    else:
        headers["accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    cookie_header = session_state.cookie_header()
    if cookie_header:
        headers["cookie"] = cookie_header
    return headers


def bootstrap_listing_session(client, session_state, page=1):
    start = time.perf_counter()
    started_at = now()
    try:
        response = client.get(
            build_search_url(page),
            params=zenrows_params(session_state.session_id),
            headers=listing_headers(page, session_state, graphql=False),
            timeout=REQUEST_TIMEOUT,
        )
        received_cookie_count = session_state.update_from_headers(response.headers)
        summary = {
            "started_at": started_at,
            "finished_at": now(),
            "elapsed_seconds": round(time.perf_counter() - start, 3),
            "status_code": response.status_code,
            "x_request_cost": response.headers.get("x-request-cost", ""),
            "x_request_id": response.headers.get("x-request-id", ""),
            "zr_gateway_status": response.headers.get("zr-gatewaystatus", ""),
            "received_cookie_count": received_cookie_count,
            **session_state.metadata(),
        }
    except RequestException as exc:
        summary = {
            "started_at": started_at,
            "finished_at": now(),
            "elapsed_seconds": round(time.perf_counter() - start, 3),
            "status_code": "ERR",
            "x_request_cost": "",
            "x_request_id": "",
            "zr_gateway_status": "",
            "received_cookie_count": 0,
            "error": str(exc),
            **session_state.metadata(),
        }
    session_state.bootstrapped = True
    return summary


def post_graphql(client, payload, page, transport, session_state, request_profile=None):
    start = time.perf_counter()
    started_at = now()
    response = client.post(
        GRAPHQL_ENDPOINT,
        params=zenrows_params(session_state.session_id, request_profile),
        headers=listing_headers(page, session_state, graphql=True, request_profile=request_profile),
        data=json.dumps(payload),
        timeout=REQUEST_TIMEOUT,
    )
    session_state.update_from_headers(response.headers)
    elapsed = time.perf_counter() - start
    return response, started_at, now(), round(elapsed, 3), transport


def make_dirs():
    for subdir in ("raw/main_graphql", "raw/html_dom_fallback", "parsed", "benchmarks"):
        (RUN_ROOT / subdir).mkdir(parents=True, exist_ok=True)
    if LISTING_DEBUG_SCREENSHOT_ENABLED:
        (RUN_ROOT / "raw/debug_screenshots").mkdir(parents=True, exist_ok=True)


def page_stem(page):
    return f"page_{page:03d}"


def page_folder(page, status=None):
    raw_dir = RUN_ROOT / "raw/main_graphql"
    stem = page_stem(page)
    if status:
        folder = raw_dir / f"{stem}_{status}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    for suffix in ("success", "fail"):
        folder = raw_dir / f"{stem}_{suffix}"
        if folder.exists():
            return folder
    return raw_dir


def page_artifact_paths(page, status=None):
    folder = page_folder(page, status)
    stem = page_stem(page)
    return {
        "folder": folder,
        "request": folder / f"{stem}_request.json",
        "response": folder / f"{stem}_response.txt",
        "headers": folder / f"{stem}_headers.json",
        "meta": folder / f"{stem}_meta.json",
        "json": folder / f"{stem}_response.json",
    }


def read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def should_capture_listing_debug_screenshot(summary):
    if not LISTING_DEBUG_SCREENSHOT_ENABLED:
        return False
    if LISTING_DEBUG_SCREENSHOT_MODE == "all":
        return True
    return is_failed_listing_summary(summary)


def listing_debug_screenshot_params():
    params = {
        "mode": "auto",
        "proxy_country": "us",
        "screenshot": "true",
        "screenshot_fullpage": "true",
    }
    if LISTING_DEBUG_SCREENSHOT_WAIT_MS > 0:
        params["wait"] = str(LISTING_DEBUG_SCREENSHOT_WAIT_MS)
    return params


def capture_listing_debug_screenshot(client, page, summary, label="initial"):
    if not client or not should_capture_listing_debug_screenshot(summary):
        return {}
    started_at = now()
    start = time.perf_counter()
    screenshot_dir = RUN_ROOT / "raw/debug_screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{page_stem(page)}_{label}"
    screenshot_path = screenshot_dir / f"{stem}.png"
    headers_path = screenshot_dir / f"{stem}_headers.json"
    meta_path = screenshot_dir / f"{stem}_meta.json"
    result = {
        "debug_screenshot_started_at": started_at,
        "debug_screenshot_url": build_search_url(page),
        "debug_screenshot_path": rel_path(screenshot_path),
        "debug_screenshot_headers_path": rel_path(headers_path),
        "debug_screenshot_meta_path": rel_path(meta_path),
    }
    try:
        response = client.get(
            build_search_url(page),
            params=listing_debug_screenshot_params(),
            headers=listing_headers(page, ListingSessionState(), graphql=False),
            timeout=REQUEST_TIMEOUT,
        )
        elapsed = round(time.perf_counter() - start, 3)
        headers_path.write_text(
            json.dumps(redacted_response_headers(response.headers), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        screenshot_path.write_bytes(response.content or b"")
        result.update(
            {
                "debug_screenshot_finished_at": now(),
                "debug_screenshot_elapsed_seconds": elapsed,
                "debug_screenshot_status_code": response.status_code,
                "debug_screenshot_bytes": len(response.content or b""),
                "debug_screenshot_cost": response.headers.get("x-request-cost", ""),
                "debug_screenshot_request_id": response.headers.get("x-request-id", ""),
                "debug_screenshot_final_url": response.headers.get("zr-final-url", ""),
            }
        )
    except RequestException as exc:
        result.update(
            {
                "debug_screenshot_finished_at": now(),
                "debug_screenshot_elapsed_seconds": round(time.perf_counter() - start, 3),
                "debug_screenshot_status_code": "ERR",
                "debug_screenshot_bytes": 0,
                "debug_screenshot_cost": "",
                "debug_screenshot_request_id": "",
                "debug_screenshot_final_url": "",
                "debug_screenshot_error": str(exc),
            }
        )
    meta_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"[listing_debug_screenshot] page={page:03d} label={label} "
        f"status={result.get('debug_screenshot_status_code')} "
        f"bytes={result.get('debug_screenshot_bytes')} "
        f"cost={result.get('debug_screenshot_cost')} "
        f"file={result.get('debug_screenshot_path')}",
        flush=True,
    )
    return result


def load_cached_page(page):
    if FORCE_REFRESH:
        return None
    paths = page_artifact_paths(page)
    meta = read_json(paths["meta"])
    response_json = read_json(paths["json"])
    if int(meta.get("status_code") or 0) != 200 or not response_json:
        return None
    rows = parse_page_rows(page, response_json)
    if not rows:
        return None
    return response_json, meta, rows


def save_page_artifacts(
    page,
    payload,
    response,
    started_at,
    finished_at,
    elapsed,
    transport,
    session_state=None,
    request_profile=None,
):
    status = "success" if response.status_code == 200 else "fail"
    paths = page_artifact_paths(page, status)
    request_path = paths["request"]
    response_path = paths["response"]
    headers_path = paths["headers"]
    meta_path = paths["meta"]
    json_path = paths["json"]

    request_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    response_path.write_text(response.text, encoding="utf-8", errors="replace")
    headers_path.write_text(
        json.dumps(redacted_response_headers(response.headers), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    response_json = {}
    parse_error = ""
    try:
        response_json = response.json()
        json_path.write_text(json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8")
    except ValueError as exc:
        parse_error = str(exc)

    meta = {
        "page": page,
        "artifact_folder": rel_path(paths["folder"]),
        "url": build_search_url(page),
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": elapsed,
        "transport": transport,
        "fetch_mode": FETCH_MODE,
        "listing_request_profile": listing_profile_name(request_profile),
        "status_code": response.status_code,
        "x_request_cost": response.headers.get("x-request-cost", ""),
        "bytes": len(response.text or ""),
        "parse_error": parse_error,
        "zenrows_error_code": zenrows_error_code(response_json),
        "x_request_id": response.headers.get("x-request-id", ""),
        "zr_gateway_status": response.headers.get("zr-gatewaystatus", ""),
        "request_path": rel_path(request_path),
        "response_path": rel_path(response_path),
        "response_json_path": rel_path(json_path) if response_json else "",
        "headers_path": rel_path(headers_path),
        "sanitize_product_list_query": int(SANITIZE_PRODUCT_LIST_QUERY),
        "strip_product_list_fulfillment": int(STRIP_PRODUCT_LIST_FULFILLMENT),
        "query_has_fulfillment_options": "fulfillmentOptions" in (payload.get("query") or ""),
    }
    if session_state is not None:
        meta.update(session_state.metadata())
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return response_json, meta


def is_sponsored_doc(document):
    if not isinstance(document, dict):
        return False
    if document.get("source"):
        return True
    for key in document:
        if key.startswith("on") and "Beacon" in key:
            return True
    return False


def parse_product_occurrence(product, occurrence, extra=None):
    row = parse_search_product(product, occurrence)
    if row.get("product_url"):
        row["product_url"] = absolute_bestbuy_url(row["product_url"])
    if extra:
        row.update(extra)
    return row


def first_money_from_text(text):
    match = re.search(r"\$\s*\d[\d,]*(?:\.\d+)?", str(text or ""))
    return match.group(0).replace(" ", "") if match else ""


def text_content(node):
    if not node:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def product_url_bsin(url):
    match = re.search(r"/product/[^/]+/([^/?#]+)(?:/sku/\d+)?", str(url or ""))
    return match.group(1) if match else ""


def product_url_sku(url):
    match = re.search(r"/sku/(\d+)", str(url or ""))
    return match.group(1) if match else ""


def product_card_href_for_sku(href, sku):
    href = absolute_bestbuy_url(href or "")
    if not href or "/product/" not in href:
        return ""
    split = urlsplit(href)
    query_sku = dict(parse_qsl(split.query)).get("skuId", "")
    path_sku = product_url_sku(href)
    if path_sku and path_sku != str(sku):
        return ""
    if query_sku and query_sku != str(sku):
        return ""
    if path_sku == str(sku) or query_sku == str(sku):
        return href
    if product_url_bsin(href):
        return urlunsplit((split.scheme, split.netloc, f"{split.path.rstrip('/')}/sku/{sku}", split.query, ""))
    return ""


def iter_nested_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_nested_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_nested_dicts(child)


def apollo_product_score(value):
    if not isinstance(value, dict) or not value.get("skuId"):
        return 0
    score = 0
    name = value.get("name") if isinstance(value.get("name"), dict) else {}
    url = value.get("url") if isinstance(value.get("url"), dict) else {}
    price = value.get("price") if isinstance(value.get("price"), dict) else {}
    if name.get("short") or name.get("title"):
        score += 2
    if url.get("skuSpecificUrl") or url.get("pdp") or url.get("relativePdp"):
        score += 2
    if price.get("displayableCustomerPrice") not in (None, "") or price.get("customerPrice") not in (None, ""):
        score += 2
    if value.get("primaryImage"):
        score += 1
    if value.get("reviewInfo"):
        score += 1
    if value.get("bsin"):
        score += 1
    return score


def apollo_products_by_sku(html_text):
    products = {}
    scores = {}
    try:
        payloads = extract_apollo_payloads(html_text or "")
    except Exception:
        return products
    for payload in payloads:
        for candidate in iter_nested_dicts(payload):
            sku = str(candidate.get("skuId") or "")
            score = apollo_product_score(candidate)
            if not sku or score <= 0:
                continue
            if score > scores.get(sku, 0):
                products[sku] = {}
                scores[sku] = score
            if score == scores.get(sku, 0):
                merge_dict(products.setdefault(sku, {}), candidate)
    return products


def parse_rating_text(text):
    rating = ""
    count = ""
    rating_match = re.search(r"Rating\s+([0-9.]+)\s+out of", str(text or ""), re.I)
    if rating_match:
        rating = rating_match.group(1)
    count_match = re.search(r"with\s+([\d,]+)\s+reviews?", str(text or ""), re.I)
    if count_match:
        count = count_match.group(1).replace(",", "")
    return rating, count


def card_price_text(card):
    node = card.select_one('[data-testid="price-block-customer-price"]')
    return first_money_from_text(text_content(node))


def card_regular_price_text(card):
    node = card.select_one('[data-testid="price-block-regular-price"]')
    return first_money_from_text(text_content(node))


def card_savings_text(card):
    node = card.select_one('[data-testid="price-block-total-savings-text"]')
    return first_money_from_text(text_content(node))


def ensure_product_sku_url(product, sku):
    url = product.get("url") if isinstance(product.get("url"), dict) else {}
    sku_url = absolute_bestbuy_url(url.get("skuSpecificUrl") or "")
    pdp_url = absolute_bestbuy_url(url.get("pdp") or url.get("relativePdp") or "")
    if not sku_url and pdp_url and "/product/" in pdp_url and product_url_bsin(pdp_url):
        split = urlsplit(pdp_url)
        sku_url = urlunsplit((split.scheme, split.netloc, f"{split.path.rstrip('/')}/sku/{sku}", split.query, ""))
    if not sku_url and sku:
        sku_url = old_pdp_url(sku)
    if sku_url:
        product.setdefault("url", {})
        product["url"]["skuSpecificUrl"] = sku_url
        product["url"].setdefault("pdp", pdp_url or sku_url)
    return product


def dom_card_product(card, product_lookup=None):
    sku = str(card.get("data-product-id") or card.get("data-testid") or "").strip()
    if not re.fullmatch(r"\d+", sku):
        return None
    link = card.select_one("a.product-list-item-link[href]")
    if not link:
        link = card.select_one('a[href*="/product/"][href*="/sku/"]')
    href = link.get("href", "") if link else ""
    href = product_card_href_for_sku(href, sku)

    title_node = card.select_one("h3.product-title")
    product_name = (title_node.get("title") if title_node else "") or text_content(title_node)
    if not href or not product_name:
        lookup_product = (product_lookup or {}).get(sku)
        if not isinstance(lookup_product, dict):
            return None
        product = dict(lookup_product)
        product["skuId"] = sku
        return ensure_product_sku_url(product, sku)

    brand = text_content(card.select_one("h3.product-title .first-title"))
    if not brand and " - " in product_name:
        brand = product_name.split(" - ", 1)[0].strip()

    rating = ""
    review_count = ""
    for rating_node in card.select("p.visually-hidden"):
        rating, review_count = parse_rating_text(text_content(rating_node))
        if rating or review_count:
            break

    image = card.select_one('img[data-testid="product-image"]') or card.select_one("img[src]")
    image_url = image.get("src", "") if image else ""
    customer_price = card_price_text(card)
    regular_price = card_regular_price_text(card)
    total_savings = card_savings_text(card)

    product = {
        "skuId": sku,
        "bsin": product_url_bsin(href),
        "brand": brand,
        "name": {"short": product_name},
        "url": {"skuSpecificUrl": href, "pdp": href},
        "primaryImage": {"piscesHref": image_url},
        "reviewInfo": {
            "averageRating": rating,
            "reviewCount": review_count,
            "isReviewable": "",
        },
        "price": {
            "displayableCustomerPrice": customer_price,
            "customerPrice": customer_price,
            "displayableRegularPrice": regular_price,
            "regularPrice": regular_price,
            "totalSavings": total_savings,
        },
    }
    lookup_product = (product_lookup or {}).get(sku)
    if isinstance(lookup_product, dict):
        merge_dict(product, lookup_product)
    return ensure_product_sku_url(product, sku)


def dom_product_cards(soup):
    scoped_cards = []
    selectors = [
        (
            "ul.product-grid-view-container > li.product-list-item[data-product-id], "
            "ul.product-grid-view-container > li.product-list-item[data-testid]",
            False,
        ),
        (
            "div.sponsored-content.product-list-sponsored-wrapper-grid-view > "
            ".product-list-item[data-product-id], "
            "div.sponsored-content.product-list-sponsored-wrapper-grid-view > "
            ".product-list-item[data-testid]",
            True,
        ),
    ]
    for selector, is_sponsored in selectors:
        for card in soup.select(selector):
            scoped_cards.append((card, is_sponsored))
    scoped_cards.sort(
        key=lambda item: (
            getattr(item[0], "sourceline", None) or 0,
            getattr(item[0], "sourcepos", None) or 0,
        )
    )
    return scoped_cards


def parse_html_dom_rows(page, html_text, source_html_path=""):
    soup = BeautifulSoup(html_text or "", "html.parser")
    product_lookup = apollo_products_by_sku(html_text)
    rows = []
    visual_rank = 0
    organic_rank = 0
    sponsored_rank = 0
    cards = dom_product_cards(soup)
    for card, is_sponsored in cards:
        product = dom_card_product(card, product_lookup)
        if not product:
            continue
        sku = str(product.get("skuId") or "")
        visual_rank += 1
        if is_sponsored:
            sponsored_rank += 1
            occurrence_organic_rank = ""
            global_organic_rank = ""
            container_type = "sponsored_ingrid"
            placement = "DOM_PRODUCT_LIST_SPONSORED"
        else:
            organic_rank += 1
            occurrence_organic_rank = organic_rank
            global_organic_rank = (page - 1) * ORGANIC_OFFSET + organic_rank
            container_type = "organic_product"
            placement = "DOM_PRODUCT_LIST"
        occurrence = {
            "page": page,
            "visual_rank": visual_rank,
            "organic_rank": occurrence_organic_rank,
            "container_type": container_type,
            "is_sponsored": is_sponsored,
            "placement": placement,
            "source_event_id": "html_dom_product_card",
            "sku_id": sku,
        }
        rows.append(
            parse_product_occurrence(
                product,
                occurrence,
                {
                    "placement_name": "SPONSORED" if is_sponsored else "ORGANIC",
                    "placement_index": "",
                    "sponsored_rank": sponsored_rank if is_sponsored else "",
                    "source_doc_index": "",
                    "global_organic_rank": global_organic_rank,
                    "category_key": CATEGORY,
                    "source_html_path": source_html_path,
                },
            )
        )

    for row in rows:
        row["category_key"] = CATEGORY
        row["global_visual_rank"] = (page - 1) * 1000 + int(row.get("visual_rank") or 0)
    return rows


def parse_page_rows(page, response_json):
    data = response_json.get("data", {}) if isinstance(response_json, dict) else {}
    rows = []
    products = {}
    visual_rank = 0

    documents = nested_get(data, ["detailedProductSearch", "documents"], [])
    if isinstance(documents, list):
        for organic_rank, document in enumerate(documents, 1):
            product = document.get("product") if isinstance(document, dict) else None
            if not isinstance(product, dict) or not product.get("skuId"):
                continue
            sku = str(product["skuId"])
            products.setdefault(sku, {})
            merge_dict(products[sku], product)
            visual_rank += 1
            occurrence = {
                "page": page,
                "visual_rank": visual_rank,
                "organic_rank": organic_rank,
                "container_type": "organic_product",
                "is_sponsored": False,
                "placement": "detailedProductSearch.documents",
                "source_event_id": "graphql_product_list",
                "sku_id": sku,
            }
            rows.append(
                parse_product_occurrence(
                    products[sku],
                    occurrence,
                    {
                        "placement_name": "ORGANIC",
                        "placement_index": "",
                        "sponsored_rank": "",
                        "source_doc_index": "",
                        "global_organic_rank": (page - 1) * ORGANIC_OFFSET + organic_rank,
                    },
                )
            )

    placements = nested_get(data, ["search", "withBestMedia", "placements"], [])
    if isinstance(placements, list):
        for placement_index, placement in enumerate(placements):
            if not isinstance(placement, dict):
                continue
            placement_name = placement.get("name", "")
            if placement_name == "SEARCH_SPONSORED_INGRID":
                sponsored_rank = 0
                sponsored_documents = nested_get(placement, ["documentsGridView", "sponsoredDocuments"], [])
                if not isinstance(sponsored_documents, list):
                    continue
                for source_index, document in enumerate(sponsored_documents, 1):
                    if not is_sponsored_doc(document):
                        continue
                    product = document.get("product") if isinstance(document, dict) else None
                    if not isinstance(product, dict) or not product.get("skuId"):
                        continue
                    sku = str(product["skuId"])
                    products.setdefault(sku, {})
                    merge_dict(products[sku], product)
                    sponsored_rank += 1
                    visual_rank += 1
                    occurrence = {
                        "page": page,
                        "visual_rank": visual_rank,
                        "organic_rank": "",
                        "container_type": "sponsored_ingrid",
                        "is_sponsored": True,
                        "placement": "SEARCH_SPONSORED_INGRID",
                        "source_event_id": "graphql_product_list",
                        "sku_id": sku,
                    }
                    rows.append(
                        parse_product_occurrence(
                            products[sku],
                            occurrence,
                            {
                                "placement_name": placement_name,
                                "placement_index": placement_index,
                                "sponsored_rank": sponsored_rank,
                                "source_doc_index": source_index,
                                "global_organic_rank": "",
                                "ad_source": document.get("source", ""),
                            },
                        )
                    )
            elif placement_name == "SEARCH_SPONSORED_CAROUSEL_DEFAULT" and INCLUDE_SPONSORED_CAROUSEL:
                documents = placement.get("documents", [])
                if not isinstance(documents, list):
                    continue
                for sponsored_rank, document in enumerate(documents, 1):
                    product = document.get("product") if isinstance(document, dict) else None
                    if not isinstance(product, dict) or not product.get("skuId"):
                        continue
                    sku = str(product["skuId"])
                    products.setdefault(sku, {})
                    merge_dict(products[sku], product)
                    visual_rank += 1
                    occurrence = {
                        "page": page,
                        "visual_rank": visual_rank,
                        "organic_rank": "",
                        "container_type": "sponsored_carousel",
                        "is_sponsored": True,
                        "placement": "SEARCH_SPONSORED_CAROUSEL_DEFAULT",
                        "source_event_id": "graphql_product_list",
                        "sku_id": sku,
                    }
                    rows.append(
                        parse_product_occurrence(
                            products[sku],
                            occurrence,
                            {
                                "placement_name": placement_name,
                                "placement_index": placement_index,
                                "sponsored_rank": sponsored_rank,
                                "source_doc_index": sponsored_rank,
                                "global_organic_rank": "",
                                "ad_source": document.get("source", ""),
                            },
                        )
                    )

    for row in rows:
        row["category_key"] = CATEGORY
        row["global_visual_rank"] = (page - 1) * 1000 + int(row.get("visual_rank") or 0)
    return rows


def response_products(response_json):
    data = response_json.get("data", {}) if isinstance(response_json, dict) else {}
    products = []
    documents = nested_get(data, ["detailedProductSearch", "documents"], [])
    if isinstance(documents, list):
        for document in documents:
            product = document.get("product") if isinstance(document, dict) else None
            if isinstance(product, dict):
                products.append(product)
    placements = nested_get(data, ["search", "withBestMedia", "placements"], [])
    if isinstance(placements, list):
        for placement in placements:
            if not isinstance(placement, dict):
                continue
            sponsored_documents = nested_get(placement, ["documentsGridView", "sponsoredDocuments"], [])
            if isinstance(sponsored_documents, list):
                for document in sponsored_documents:
                    product = document.get("product") if isinstance(document, dict) else None
                    if isinstance(product, dict):
                        products.append(product)
            documents = placement.get("documents", [])
            if isinstance(documents, list):
                for document in documents:
                    product = document.get("product") if isinstance(document, dict) else None
                    if isinstance(product, dict):
                        products.append(product)
    return products


def response_fulfillment_counts(response_json):
    products = response_products(response_json)
    with_fulfillment = 0
    shipping = 0
    delivery = 0
    pickup = 0
    for product in products:
        options = product.get("fulfillmentOptions")
        if isinstance(options, dict) and options:
            with_fulfillment += 1
            if first_nested(options, ["shippingDetails", "shippingAvailability"], {}) not in ("", None, [], {}):
                shipping += 1
            if first_nested(options, ["deliveryDetails", "deliveryAvailability"], {}) not in ("", None, [], {}):
                delivery += 1
            if first_nested(options, ["ispuDetails", "ispuAvailability"], {}) not in ("", None, [], {}):
                pickup += 1
    return {
        "response_product_count": len(products),
        "response_fulfillment_product_count": with_fulfillment,
        "response_shipping_availability_count": shipping,
        "response_delivery_availability_count": delivery,
        "response_pickup_availability_count": pickup,
    }


def write_csv(path, rows):
    keys = set()
    for row in rows:
        keys.update(row)
    preferred = [
        "category_key",
        "page",
        "visual_rank",
        "global_visual_rank",
        "organic_rank",
        "global_organic_rank",
        "container_type",
        "is_sponsored",
        "placement",
        "placement_name",
        "placement_index",
        "sponsored_rank",
        "source_doc_index",
        "ad_source",
        "sku_id",
        "bsin",
        "item",
        "brand",
        "product_name",
        "retailer_sku_name",
        "product_url",
        "image_url",
        "rating",
        "review_count",
        "star_rating",
        "count_of_star_ratings",
        "sku_status",
        "customer_price",
        "regular_price",
        "total_savings",
        "final_sku_price",
        "original_sku_price",
        "savings",
        "total_savings_percent",
        "fastest_delivery",
        "delivery_availability",
        "pick_up_availability",
        "offer",
        "shipping_eligible",
        "pickup_eligible",
        "offer_count",
    ]
    fieldnames = [key for key in preferred if key in keys]
    fieldnames.extend(sorted(keys - set(fieldnames)))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_csv(path, row, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def page_summary(page, rows, meta, response_json):
    errors = response_json.get("errors", []) if isinstance(response_json, dict) else []
    organic = [row for row in rows if row.get("container_type") == "organic_product"]
    ingrid = [row for row in rows if row.get("container_type") == "sponsored_ingrid"]
    carousel = [row for row in rows if row.get("container_type") == "sponsored_carousel"]
    fulfillment_counts = response_fulfillment_counts(response_json)
    summary = {
        "page": page,
        "started_at": meta["started_at"],
        "finished_at": meta["finished_at"],
        "elapsed_seconds": meta["elapsed_seconds"],
        "status_code": meta["status_code"],
        "transport": meta.get("transport", ""),
        "x_request_cost": meta["x_request_cost"],
        "bytes": meta["bytes"],
        "error_count": len(errors),
        "organic_count": len(organic),
        "sponsored_ingrid_count": len(ingrid),
        "sponsored_carousel_count": len(carousel),
        "total_occurrence_count": len(rows),
        "unique_sku_count": len({row.get("sku_id") for row in rows if row.get("sku_id")}),
        "organic_price_missing": sum(1 for row in organic if row.get("customer_price") in ("", None)),
        "sponsored_price_missing": sum(
            1 for row in ingrid + carousel if row.get("customer_price") in ("", None)
        ),
        "rows_with_pickup_availability": sum(1 for row in rows if row.get("pick_up_availability")),
        "rows_with_fastest_delivery": sum(1 for row in rows if row.get("fastest_delivery")),
        "rows_with_delivery_availability": sum(1 for row in rows if row.get("delivery_availability")),
        "rows_with_any_availability": sum(
            1
            for row in rows
            if row.get("pick_up_availability")
            or row.get("fastest_delivery")
            or row.get("delivery_availability")
        ),
        "graphql_error_preview": json.dumps(errors[:2], ensure_ascii=False)[:500] if errors else "",
        "response_path": meta["response_json_path"] or meta["response_path"],
        "attempt_count": meta.get("attempt_count", 1),
        "attempt_status_codes": meta.get("attempt_status_codes", str(meta.get("status_code", ""))),
        "attempt_costs": meta.get("attempt_costs", str(meta.get("x_request_cost", ""))),
        "attempt_retry_reasons": meta.get("attempt_retry_reasons", ""),
        "attempt_retry_delays": meta.get("attempt_retry_delays", ""),
        "attempt_errors": meta.get("attempt_errors", ""),
        "attempt_profiles": meta.get("attempt_profiles", ""),
        "recovery_attempt_count": meta.get("recovery_attempt_count", 0),
        "recovery_profiles": meta.get("recovery_profiles", ""),
        "recovery_success": meta.get("recovery_success", 0),
        "delayed_retry_round": meta.get("delayed_retry_round", 0),
        "delayed_retry_previous_status_code": meta.get("delayed_retry_previous_status_code", ""),
        "delayed_retry_previous_rows": meta.get("delayed_retry_previous_rows", ""),
    }
    summary.update(fulfillment_counts)
    return summary


def status_code_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def listing_retry_reason(rows, meta, response_json):
    if rows:
        return ""
    status_code = meta.get("status_code")
    if status_code == "ERR":
        return "request_exception"
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        return ""
    error_code = str(meta.get("zenrows_error_code") or zenrows_error_code(response_json)).upper()
    if status_code == 422 and error_code == "RESP001":
        return "resp001"
    if status_code in LISTING_RETRY_STATUS_CODES:
        return f"http_{status_code}"
    if status_code == 200 and not response_json:
        return "empty_response"
    if status_code == 200 and not rows:
        return "empty_rows"
    return ""


def listing_html_fallback_reason(rows, meta, response_json):
    retry_reason = listing_retry_reason(rows, meta, response_json)
    if retry_reason:
        return retry_reason
    partial_reason = listing_partial_rows_reason(rows)
    if partial_reason:
        return partial_reason
    return ""


def listing_partial_rows_reason(rows):
    if LISTING_HTML_FALLBACK_MIN_ROWS <= 0:
        return ""
    occurrence_count = len(rows)
    if 0 < occurrence_count < LISTING_HTML_FALLBACK_MIN_ROWS:
        return f"partial_rows_{occurrence_count}_lt_{LISTING_HTML_FALLBACK_MIN_ROWS}"
    return ""


def listing_rows_complete(rows):
    return listing_occurrence_count_complete(len(rows))


def listing_occurrence_count_complete(occurrence_count):
    if occurrence_count <= 0:
        return False
    if LISTING_HTML_FALLBACK_MIN_ROWS <= 0:
        return True
    return occurrence_count >= LISTING_HTML_FALLBACK_MIN_ROWS


def listing_retry_delay(attempt):
    if LISTING_RETRY_SLEEP_SEQUENCE:
        index = min(max(0, attempt - 1), len(LISTING_RETRY_SLEEP_SEQUENCE) - 1)
        return LISTING_RETRY_SLEEP_SEQUENCE[index]
    if LISTING_RETRY_SLEEP_SECONDS <= 0:
        return 0.0
    return min(
        LISTING_RETRY_SLEEP_SECONDS * (2 ** max(0, attempt - 1)),
        LISTING_RETRY_MAX_SLEEP_SECONDS,
    )


def listing_dom_retry_reason(rows, meta):
    status_code = meta.get("status_code")
    if status_code == "ERR":
        return "request_exception"
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        return "invalid_status"
    if status_code != 200:
        return f"http_{status_code}"
    partial_reason = listing_partial_rows_reason(rows)
    if partial_reason:
        return partial_reason
    if not rows:
        return "empty_rows"
    return ""


def ensure_listing_session(client, session_state, page, bootstrap_attempts):
    if not LISTING_SESSION_ENABLED:
        return
    if session_state.expired():
        session_state.reset("max_age")
    if LISTING_SESSION_BOOTSTRAP and not session_state.bootstrapped:
        bootstrap_attempts.append(bootstrap_listing_session(client, session_state, page))


def collect_network_page(page, payload, client, listing_session, bootstrap_attempts):
    response_json = {}
    rows = []
    meta = {}
    attempt_status_codes = []
    attempt_costs = []
    attempt_errors = []
    attempt_retry_reasons = []
    attempt_retry_delays = []
    attempt_profiles = []
    recovery_attempt_count = 0
    recovery_profiles_used = []
    recovery_success = 0

    def run_one_attempt(attempt_label, request_profile=None):
        nonlocal response_json, rows, meta
        profile_name = listing_profile_name(request_profile)
        ensure_listing_session(client, listing_session, page, bootstrap_attempts)
        for transport in fetch_transports():
            if transport == "zenrows" and not client:
                continue
            print(
                f"page={page:03d} {attempt_label} transport={transport} request_start "
                f"profile={profile_name} mode={'auto' if (request_profile or {}).get('mode_auto') or zenrows_mode_auto() else 'manual'} "
                f"session={'on' if LISTING_SESSION_ENABLED or (request_profile or {}).get('session_id') else 'off'}",
                flush=True,
            )
            try:
                response, started_at, finished_at, elapsed, transport = post_graphql(
                    client,
                    payload,
                    page,
                    transport,
                    listing_session,
                    request_profile=request_profile,
                )
                response_json, meta = save_page_artifacts(
                    page,
                    payload,
                    response,
                    started_at,
                    finished_at,
                    elapsed,
                    transport,
                    listing_session,
                    request_profile=request_profile,
                )
                rows = parse_page_rows(page, response_json) if response.status_code == 200 else []
            except RequestException as exc:
                response_json = {}
                rows = []
                meta = {
                    "page": page,
                    "url": build_search_url(page),
                    "started_at": now(),
                    "finished_at": now(),
                    "elapsed_seconds": 0,
                    "transport": transport,
                    "fetch_mode": FETCH_MODE,
                    "listing_request_profile": profile_name,
                    "status_code": "ERR",
                    "x_request_cost": "",
                    "bytes": 0,
                    "parse_error": "",
                    "error": str(exc),
                    "response_json_path": "",
                    "response_path": "",
                    **listing_session.metadata(),
                }
            print(
                f"page={page:03d} {attempt_label} profile={profile_name} "
                f"status={meta.get('status_code')} zenrows_code={meta.get('zenrows_error_code', '')} "
                f"elapsed={meta.get('elapsed_seconds')}s cost={meta.get('x_request_cost', '')}",
                flush=True,
            )
            attempt_status_codes.append(str(meta.get("status_code", "")))
            attempt_costs.append(str(meta.get("x_request_cost", "")))
            attempt_profiles.append(profile_name)
            if meta.get("error"):
                attempt_errors.append(str(meta.get("error")))
            retry_reason = listing_retry_reason(rows, meta, response_json)
            if rows or not retry_reason:
                break
        return listing_retry_reason(rows, meta, response_json)

    for attempt in range(1, LISTING_MAX_ATTEMPTS + 1):
        retry_reason = run_one_attempt(f"attempt={attempt}")
        if rows or not retry_reason:
            break
        if attempt >= LISTING_MAX_ATTEMPTS:
            break
        retry_delay = listing_retry_delay(attempt)
        attempt_retry_reasons.append(retry_reason)
        attempt_retry_delays.append(retry_delay)
        print(
            f"page={page:03d} attempt={attempt} retry_reason={retry_reason} "
            f"retry_delay={retry_delay:g}s",
            flush=True,
        )
        if retry_delay > 0:
            time.sleep(retry_delay)
        if retry_reason == "resp001" and LISTING_SESSION_ENABLED:
            listing_session.reset("resp001")

    final_retry_reason = listing_retry_reason(rows, meta, response_json)
    if LISTING_RECOVERY_ENABLED and not rows and final_retry_reason:
        for request_profile in listing_recovery_profiles():
            profile_name = listing_profile_name(request_profile)
            recovery_profiles_used.append(profile_name)
            for recovery_attempt in range(1, LISTING_RECOVERY_ATTEMPTS_PER_PROFILE + 1):
                recovery_attempt_count += 1
                retry_reason = run_one_attempt(
                    f"recovery_profile={profile_name} recovery_attempt={recovery_attempt}",
                    request_profile=request_profile,
                )
                if rows:
                    recovery_success = 1
                    break
                if not retry_reason:
                    break
                retry_delay = listing_retry_delay(LISTING_MAX_ATTEMPTS + recovery_attempt_count)
                attempt_retry_reasons.append(f"{profile_name}:{retry_reason}")
                attempt_retry_delays.append(retry_delay)
                print(
                    f"page={page:03d} recovery_profile={profile_name} "
                    f"recovery_attempt={recovery_attempt} retry_reason={retry_reason} "
                    f"retry_delay={retry_delay:g}s",
                    flush=True,
                )
                if retry_delay > 0:
                    time.sleep(retry_delay)
            if rows or not listing_retry_reason(rows, meta, response_json):
                break

    html_fallback_reason = listing_html_fallback_reason(rows, meta, response_json)
    if LISTING_HTML_FALLBACK_ENABLED and html_fallback_reason:
        previous_rows = rows
        previous_meta = dict(meta)
        html_meta, html_rows = collect_html_dom_page(page, client)
        attempt_status_codes.append(str(html_meta.get("status_code", "")))
        attempt_costs.append(str(html_meta.get("x_request_cost", "")))
        attempt_profiles.append("html_dom_fallback")
        if html_meta.get("error"):
            attempt_errors.append(str(html_meta.get("error")))
        html_unique_count = len({row.get("sku_id") for row in html_rows if row.get("sku_id")})
        previous_unique_count = len({row.get("sku_id") for row in previous_rows if row.get("sku_id")})
        html_occurrence_count = len(html_rows)
        previous_occurrence_count = len(previous_rows)
        html_meta["html_dom_fallback_reason"] = html_fallback_reason
        html_meta["html_dom_previous_status_code"] = previous_meta.get("status_code", "")
        html_meta["html_dom_previous_unique_sku_count"] = previous_unique_count
        html_meta["html_dom_previous_occurrence_count"] = previous_occurrence_count
        html_meta["html_dom_replaced_previous_rows"] = int(
            bool(html_rows) and html_occurrence_count > previous_occurrence_count
        )
        if html_rows and html_occurrence_count > previous_occurrence_count:
            response_json = {}
            rows = html_rows
            meta = html_meta
        else:
            meta["html_dom_fallback_reason"] = html_fallback_reason
            meta["html_dom_status_code"] = html_meta.get("status_code", "")
            meta["html_dom_unique_sku_count"] = html_unique_count
            meta["html_dom_occurrence_count"] = html_occurrence_count
            meta["html_dom_replaced_previous_rows"] = 0

    meta["attempt_count"] = len(attempt_status_codes)
    meta["attempt_status_codes"] = ",".join(attempt_status_codes)
    meta["attempt_costs"] = ",".join(attempt_costs)
    meta["attempt_profiles"] = ",".join(attempt_profiles)
    meta["attempt_retry_reasons"] = ",".join(attempt_retry_reasons)
    meta["attempt_retry_delays"] = ",".join(f"{delay:g}" for delay in attempt_retry_delays)
    meta["recovery_attempt_count"] = recovery_attempt_count
    meta["recovery_profiles"] = ",".join(recovery_profiles_used)
    meta["recovery_success"] = recovery_success
    if attempt_errors:
            meta["attempt_errors"] = " | ".join(attempt_errors[-3:])
    return response_json, meta, rows


def collect_dom_listing_page(page, client):
    rows = []
    meta = {}
    attempt_status_codes = []
    attempt_costs = []
    attempt_errors = []
    attempt_retry_reasons = []
    attempt_retry_delays = []
    for attempt in range(1, LISTING_MAX_ATTEMPTS + 1):
        print(f"page={page:03d} dom_attempt={attempt} request_start", flush=True)
        meta, rows = collect_html_dom_page(page, client)
        attempt_status_codes.append(str(meta.get("status_code", "")))
        attempt_costs.append(str(meta.get("x_request_cost", "")))
        if meta.get("error"):
            attempt_errors.append(str(meta.get("error")))
        retry_reason = listing_dom_retry_reason(rows, meta)
        print(
            f"page={page:03d} dom_attempt={attempt} status={meta.get('status_code')} "
            f"elapsed={meta.get('elapsed_seconds')}s cost={meta.get('x_request_cost', '')} "
            f"rows={len(rows)} retry_reason={retry_reason}",
            flush=True,
        )
        if not retry_reason:
            break
        if attempt >= LISTING_MAX_ATTEMPTS:
            break
        retry_delay = listing_retry_delay(attempt)
        attempt_retry_reasons.append(retry_reason)
        attempt_retry_delays.append(retry_delay)
        print(
            f"page={page:03d} dom_attempt={attempt} retry_reason={retry_reason} "
            f"retry_delay={retry_delay:g}s",
            flush=True,
        )
        if retry_delay > 0:
            time.sleep(retry_delay)

    meta["attempt_count"] = len(attempt_status_codes)
    meta["attempt_status_codes"] = ",".join(attempt_status_codes)
    meta["attempt_costs"] = ",".join(attempt_costs)
    meta["attempt_profiles"] = ",".join(["html_dom"] * len(attempt_status_codes))
    meta["attempt_retry_reasons"] = ",".join(attempt_retry_reasons)
    meta["attempt_retry_delays"] = ",".join(f"{delay:g}" for delay in attempt_retry_delays)
    meta["recovery_attempt_count"] = 0
    meta["recovery_profiles"] = ""
    meta["recovery_success"] = 0
    if attempt_errors:
        meta["attempt_errors"] = " | ".join(attempt_errors[-3:])
    return {}, meta, rows


def create_browser_graphql_page():
    try:
        from DrissionPage import ChromiumOptions, ChromiumPage
    except ImportError as exc:
        raise RuntimeError(
            "BESTBUY_LISTING_COLLECTION_MODE=browser_graphql requires DrissionPage. "
            "Install requirements.txt on the runner."
        ) from exc

    options = ChromiumOptions()
    profile_dir = browser_graphql_profile_dir()
    cache_dir = browser_graphql_cache_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    local_port = browser_graphql_local_port()
    options.set_paths(
        local_port=local_port,
        user_data_path=str(profile_dir),
        cache_path=str(cache_dir),
    )
    if BROWSER_GRAPHQL_HEADLESS:
        try:
            options.headless(True)
        except TypeError:
            options.headless()
    try:
        return ChromiumPage(options)
    except Exception as first_exc:
        # Chrome may be alive before DrissionPage resolves its websocket endpoint.
        time.sleep(2)
        reconnect_options = ChromiumOptions()
        reconnect_options.set_address(f"127.0.0.1:{local_port}")
        try:
            return ChromiumPage(reconnect_options)
        except Exception as second_exc:
            raise RuntimeError(
                f"Could not open browser_graphql Chrome session on port {local_port}: "
                f"initial={first_exc!r}; reconnect={second_exc!r}"
            ) from second_exc


def browser_graphql_local_port():
    if BROWSER_GRAPHQL_LOCAL_PORT > 0:
        return BROWSER_GRAPHQL_LOCAL_PORT
    seed = f"{CATEGORY}:{RUN_ID}:{RUN_ROOT}"
    return 19000 + (sum(ord(ch) for ch in seed) % 20000)


def browser_graphql_profile_dir():
    return RUN_ROOT / "raw" / "browser_graphql_profile"


def browser_graphql_cache_dir():
    return RUN_ROOT / "raw" / "browser_graphql_cache"


def close_browser_graphql_page(browser_page):
    if not browser_page:
        return
    for method_name in ("quit", "close"):
        method = getattr(browser_page, method_name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
            return


def initialize_browser_graphql_session(browser_page):
    if not browser_page:
        return
    session_url = build_search_url(1)
    browser_page.get(session_url)
    if BROWSER_GRAPHQL_WAIT_SECONDS > 0:
        time.sleep(BROWSER_GRAPHQL_WAIT_SECONDS)


def status_code_ok(value):
    try:
        return int(value or 0) == 200
    except (TypeError, ValueError):
        return False


def browser_graphql_fetch_once(page, payload, browser_page):
    started_at = now()
    start = time.perf_counter()
    page_url = build_search_url(page)
    raw_dir = RUN_ROOT / "raw/browser_graphql"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem = page_stem(page)
    request_path = raw_dir / f"{stem}_request.json"
    response_path = raw_dir / f"{stem}_response.txt"
    response_json_path = raw_dir / f"{stem}_response.json"
    meta_path = raw_dir / f"{stem}_meta.json"
    request_path.write_text(
        json.dumps({"url": page_url, "payload": payload}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    graph = {}
    envelope = {}
    status_code = "ERR"
    content_type = ""
    error = ""
    parse_error = ""
    raw = ""
    try:
        if BROWSER_GRAPHQL_NAVIGATE_EACH_PAGE:
            browser_page.get(page_url)
            time.sleep(BROWSER_GRAPHQL_WAIT_SECONDS)
        payload_json = json.dumps(payload, ensure_ascii=False)
        js = (
            "return fetch('/gateway/graphql', {"
            "method:'POST', credentials:'include', "
            "headers:{'accept':'application/json, text/plain, */*','content-type':'application/json'}, "
            f"body: JSON.stringify({payload_json})"
            "}).then(async r=>{const t=await r.text(); "
            "return JSON.stringify({status:r.status, contentType:r.headers.get('content-type'), body:t});"
            "}).catch(e=>JSON.stringify({error:String(e)}));"
        )
        raw = browser_page.run_js(js, timeout=BROWSER_GRAPHQL_JS_TIMEOUT)
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        envelope = json.loads(raw)
        if envelope.get("error"):
            error = str(envelope.get("error"))
        status_code = envelope.get("status", "ERR")
        content_type = envelope.get("contentType") or ""
        body = envelope.get("body") or ""
        if body:
            graph = json.loads(body)
            response_json_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        error = str(exc)
    if raw:
        response_path.write_text(str(raw), encoding="utf-8", errors="replace")
    elapsed = round(time.perf_counter() - start, 3)
    rows = []
    if status_code_ok(status_code) and graph:
        try:
            rows = parse_page_rows(page, graph)
        except Exception as exc:
            parse_error = repr(exc)
    meta = {
        "page": page,
        "url": page_url,
        "started_at": started_at,
        "finished_at": now(),
        "elapsed_seconds": elapsed,
        "transport": "browser_graphql",
        "fetch_mode": "browser",
        "listing_request_profile": "browser_graphql",
        "status_code": status_code,
        "x_request_cost": "0",
        "bytes": len(str(raw).encode("utf-8", errors="ignore")),
        "parse_error": parse_error,
        "error": error,
        "content_type": content_type,
        "zenrows_error_code": "",
        "request_path": rel_path(request_path),
        "response_path": rel_path(response_path) if response_path.exists() else "",
        "response_json_path": rel_path(response_json_path) if response_json_path.exists() else "",
        "headers_path": "",
        "browser_graphql_wait_seconds": BROWSER_GRAPHQL_WAIT_SECONDS,
        "browser_graphql_js_timeout": BROWSER_GRAPHQL_JS_TIMEOUT,
        "browser_graphql_headless": int(BROWSER_GRAPHQL_HEADLESS),
        "browser_graphql_local_port": browser_graphql_local_port(),
        "browser_graphql_profile_dir": rel_path(browser_graphql_profile_dir()),
        "browser_graphql_cache_dir": rel_path(browser_graphql_cache_dir()),
        "browser_graphql_navigate_each_page": int(BROWSER_GRAPHQL_NAVIGATE_EACH_PAGE),
        "browser_graphql_context_url": getattr(browser_page, "url", "") or "",
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return graph, meta, rows


def collect_browser_graphql_page(page, payload, browser_page):
    response_json = {}
    rows = []
    meta = {}
    attempt_status_codes = []
    attempt_costs = []
    attempt_errors = []
    attempt_retry_reasons = []
    attempt_retry_delays = []
    for attempt in range(1, LISTING_MAX_ATTEMPTS + 1):
        print(f"page={page:03d} browser_graphql_attempt={attempt} request_start", flush=True)
        response_json, meta, rows = browser_graphql_fetch_once(page, payload, browser_page)
        attempt_status_codes.append(str(meta.get("status_code", "")))
        attempt_costs.append(str(meta.get("x_request_cost", "")))
        if meta.get("error"):
            attempt_errors.append(str(meta.get("error")))
        retry_reason = listing_retry_reason(rows, meta, response_json)
        print(
            f"page={page:03d} browser_graphql_attempt={attempt} status={meta.get('status_code')} "
            f"elapsed={meta.get('elapsed_seconds')}s rows={len(rows)} retry_reason={retry_reason}",
            flush=True,
        )
        if rows or not retry_reason or attempt >= LISTING_MAX_ATTEMPTS:
            break
        retry_delay = listing_retry_delay(attempt)
        attempt_retry_reasons.append(retry_reason)
        attempt_retry_delays.append(retry_delay)
        if retry_delay > 0:
            time.sleep(retry_delay)

    meta["attempt_count"] = len(attempt_status_codes)
    meta["attempt_status_codes"] = ",".join(attempt_status_codes)
    meta["attempt_costs"] = ",".join(attempt_costs)
    meta["attempt_profiles"] = ",".join(["browser_graphql"] * len(attempt_status_codes))
    meta["attempt_retry_reasons"] = ",".join(attempt_retry_reasons)
    meta["attempt_retry_delays"] = ",".join(f"{delay:g}" for delay in attempt_retry_delays)
    meta["recovery_attempt_count"] = 0
    meta["recovery_profiles"] = ""
    meta["recovery_success"] = 0
    if attempt_errors:
        meta["attempt_errors"] = " | ".join(attempt_errors[-3:])
    return response_json, meta, rows


def collect_listing_page(page, operation, client, listing_session, bootstrap_attempts, browser_page=None):
    if LISTING_COLLECTION_MODE == "dom":
        return collect_dom_listing_page(page, client)
    if LISTING_COLLECTION_MODE == "graphql":
        payload = prepare_product_list_payload(operation, page)
        return collect_network_page(page, payload, client, listing_session, bootstrap_attempts)
    if LISTING_COLLECTION_MODE == "browser_graphql":
        payload = prepare_product_list_payload(operation, page)
        return collect_browser_graphql_page(page, payload, browser_page)
    raise RuntimeError(
        f"Unsupported BESTBUY_LISTING_COLLECTION_MODE={LISTING_COLLECTION_MODE!r}; use dom, graphql, or browser_graphql"
    )


def html_dom_fallback_js_instructions():
    if not LISTING_HTML_FALLBACK_SCROLL_ENABLED or LISTING_HTML_FALLBACK_SCROLL_STEPS <= 0:
        return []
    instructions = []
    if LISTING_HTML_FALLBACK_SCROLL_WAIT_MS > 0:
        instructions.append({"wait": LISTING_HTML_FALLBACK_SCROLL_WAIT_MS})
    for _ in range(LISTING_HTML_FALLBACK_SCROLL_STEPS):
        instructions.append({"scroll_y": LISTING_HTML_FALLBACK_SCROLL_Y})
        if LISTING_HTML_FALLBACK_SCROLL_WAIT_MS > 0:
            instructions.append({"wait": LISTING_HTML_FALLBACK_SCROLL_WAIT_MS})
    if LISTING_HTML_FALLBACK_SCROLL_FINAL_WAIT_MS > 0:
        instructions.append({"wait": LISTING_HTML_FALLBACK_SCROLL_FINAL_WAIT_MS})
    if LISTING_HTML_FALLBACK_SCROLL_RESET_TOP:
        instructions.append({"evaluate": "window.scrollTo(0, 0);"})
        if LISTING_HTML_FALLBACK_SCROLL_WAIT_MS > 0:
            instructions.append({"wait": min(LISTING_HTML_FALLBACK_SCROLL_WAIT_MS, 1000)})
    return instructions


def html_dom_fallback_params():
    params = {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "us",
    }
    if LISTING_HTML_FALLBACK_WAIT_MS > 0:
        params["wait"] = str(LISTING_HTML_FALLBACK_WAIT_MS)
    instructions = html_dom_fallback_js_instructions()
    if instructions:
        params["js_instructions"] = json.dumps(instructions)
    return params


def collect_html_dom_page(page, client):
    started_at = now()
    start = time.perf_counter()
    html_dir = RUN_ROOT / "raw/html_dom_fallback"
    html_dir.mkdir(parents=True, exist_ok=True)
    stem = page_stem(page)
    html_path = html_dir / f"{stem}.html"
    headers_path = html_dir / f"{stem}_headers.json"
    meta_path = html_dir / f"{stem}_meta.json"
    rows = []
    parse_error = ""
    status_code = "ERR"
    cost = ""
    request_id = ""
    bytes_count = 0
    error = ""
    if not client:
        error = "ZenRows client unavailable"
    else:
        try:
            response = client.get(
                build_search_url(page),
                params=html_dom_fallback_params(),
                headers=listing_headers(page, ListingSessionState(), graphql=False),
                timeout=REQUEST_TIMEOUT,
            )
            status_code = response.status_code
            cost = response.headers.get("x-request-cost", "")
            request_id = response.headers.get("x-request-id", "")
            headers_path.write_text(
                json.dumps(redacted_response_headers(response.headers), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            html_text = response.text or ""
            bytes_count = len(html_text.encode("utf-8", errors="ignore"))
            html_path.write_text(html_text, encoding="utf-8", errors="ignore")
            if response.status_code == 200:
                try:
                    rows = parse_html_dom_rows(page, html_text, rel_path(html_path))
                except Exception as exc:
                    parse_error = repr(exc)
        except RequestException as exc:
            error = str(exc)
    elapsed = round(time.perf_counter() - start, 3)
    meta = {
        "page": page,
        "url": build_search_url(page),
        "started_at": started_at,
        "finished_at": now(),
        "elapsed_seconds": elapsed,
        "transport": "zenrows_html_dom",
        "fetch_mode": FETCH_MODE,
        "listing_request_profile": "html_dom_fallback",
        "status_code": status_code,
        "x_request_cost": cost,
        "x_request_id": request_id,
        "bytes": bytes_count,
        "parse_error": parse_error,
        "error": error,
        "response_json_path": "",
        "response_path": rel_path(html_path),
        "request_path": "",
        "headers_path": rel_path(headers_path) if headers_path.exists() else "",
        "html_dom_fallback_enabled": int(LISTING_HTML_FALLBACK_ENABLED),
        "html_dom_fallback_wait_ms": LISTING_HTML_FALLBACK_WAIT_MS,
        "html_dom_fallback_scroll_enabled": int(LISTING_HTML_FALLBACK_SCROLL_ENABLED),
        "html_dom_fallback_scroll_steps": LISTING_HTML_FALLBACK_SCROLL_STEPS,
        "html_dom_fallback_scroll_y": LISTING_HTML_FALLBACK_SCROLL_Y,
        "html_dom_fallback_scroll_wait_ms": LISTING_HTML_FALLBACK_SCROLL_WAIT_MS,
        "html_dom_fallback_scroll_final_wait_ms": LISTING_HTML_FALLBACK_SCROLL_FINAL_WAIT_MS,
        "html_dom_card_count": len(rows),
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"page={page:03d} html_dom_fallback status={status_code} elapsed={elapsed}s "
        f"cost={cost} rows={len(rows)} bytes={bytes_count} parse_error={parse_error}",
        flush=True,
    )
    return meta, rows


def is_failed_listing_summary(summary):
    occurrence_count = int(summary.get("total_occurrence_count") or 0)
    return status_code_int(summary.get("status_code")) != 200 or not listing_occurrence_count_complete(occurrence_count)


def failed_page_retry_sleep(round_number):
    if not LISTING_FAILED_PAGE_RETRY_SLEEP_SECONDS:
        return 0.0
    index = min(max(0, round_number - 1), len(LISTING_FAILED_PAGE_RETRY_SLEEP_SECONDS) - 1)
    return LISTING_FAILED_PAGE_RETRY_SLEEP_SECONDS[index]


def replace_raw_search(raw_search, page, response_json, meta, summary):
    record = {
        "page": page,
        "url": build_search_url(page),
        "meta": meta,
        "summary": summary,
    }
    for index, existing in enumerate(raw_search):
        if int(existing.get("page") or 0) == page:
            raw_search[index] = record
            return
    raw_search.append(record)


def append_csv_values(*values):
    parts = []
    for value in values:
        for part in str(value or "").split(","):
            part = part.strip()
            if part:
                parts.append(part)
    return ",".join(parts)


def append_pipe_values(*values, limit=12):
    parts = []
    for value in values:
        for part in str(value or "").split("|"):
            part = part.strip()
            if part:
                parts.append(part)
    if limit and len(parts) > limit:
        parts = parts[-limit:]
    return " | ".join(parts)


def int_value(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def merge_listing_attempt_history(previous_summary, current_summary):
    """Keep final page result from current_summary while preserving prior retry costs."""
    if not previous_summary:
        return current_summary
    merged = dict(current_summary)
    prior_attempts = int_value(previous_summary.get("attempt_count"), 0)
    current_attempts = int_value(current_summary.get("attempt_count"), 0)
    merged["attempt_count"] = prior_attempts + current_attempts
    for field in (
        "attempt_status_codes",
        "attempt_costs",
        "attempt_retry_reasons",
        "attempt_retry_delays",
        "attempt_profiles",
    ):
        merged[field] = append_csv_values(previous_summary.get(field), current_summary.get(field))
    merged["attempt_errors"] = append_pipe_values(
        previous_summary.get("attempt_errors"),
        current_summary.get("attempt_errors"),
    )
    merged["recovery_attempt_count"] = int_value(previous_summary.get("recovery_attempt_count"), 0) + int_value(
        current_summary.get("recovery_attempt_count"),
        0,
    )
    merged["delayed_retry_prior_attempt_count"] = prior_attempts
    merged["delayed_retry_total_attempt_count"] = merged["attempt_count"]
    return merged


def retry_failed_pages_with_delay(
    operation,
    client,
    listing_session,
    bootstrap_attempts,
    browser_page,
    rows_by_page,
    page_benchmarks,
    raw_search,
    realtime_benchmarks_path,
):
    if LISTING_FAILED_PAGE_RETRY_ROUNDS <= 0:
        return
    for round_number in range(1, LISTING_FAILED_PAGE_RETRY_ROUNDS + 1):
        failed_summaries = [summary for summary in page_benchmarks if is_failed_listing_summary(summary)]
        if not failed_summaries:
            return
        failed_pages = [int(summary.get("page")) for summary in failed_summaries]
        sleep_seconds = failed_page_retry_sleep(round_number)
        print(
            f"[listing_failed_page_retry] round={round_number} pages={failed_pages} "
            f"sleep={sleep_seconds:g}s",
            flush=True,
        )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        for failed_summary in list(failed_summaries):
            page = int(failed_summary.get("page"))
            previous_status_code = failed_summary.get("status_code", "")
            previous_rows = failed_summary.get("total_occurrence_count", "")
            response_json, meta, rows = collect_listing_page(
                page,
                operation,
                client,
                listing_session,
                bootstrap_attempts,
                browser_page,
            )
            meta["delayed_retry_round"] = round_number
            meta["delayed_retry_previous_status_code"] = previous_status_code
            meta["delayed_retry_previous_rows"] = previous_rows
            summary = page_summary(page, rows, meta, response_json)
            summary = merge_listing_attempt_history(failed_summary, summary)
            summary["source"] = f"network_delayed_retry_{round_number}"
            summary.update(capture_listing_debug_screenshot(client, page, summary, f"retry{round_number}"))
            rows_by_page[page] = rows
            for index, existing in enumerate(page_benchmarks):
                if int(existing.get("page") or 0) == page:
                    page_benchmarks[index] = summary
                    break
            else:
                page_benchmarks.append(summary)
            replace_raw_search(raw_search, page, response_json, meta, summary)
            print(
                f"[listing_failed_page_retry] round={round_number} page={page:03d} "
                f"status={summary.get('status_code')} rows={summary.get('total_occurrence_count')} "
                f"attempts={summary.get('attempt_count')}",
                flush=True,
            )
        write_csv(realtime_benchmarks_path, page_benchmarks)


def main():
    api_key = os.getenv("ZENROWS_API_KEY")
    needs_zenrows = LISTING_COLLECTION_MODE == "dom" or (
        LISTING_COLLECTION_MODE == "graphql" and any(item == "zenrows" for item in fetch_transports())
    )
    if needs_zenrows and not api_key:
        raise RuntimeError("Set ZENROWS_API_KEY in .env")
    make_dirs()
    run_started_at = now()
    run_start = time.perf_counter()

    operation = (
        load_product_list_operation("PlpView_ProductList_Init")
        if LISTING_COLLECTION_MODE in {"graphql", "browser_graphql"}
        else {"source_path": "", "source_type": "dom_html"}
    )
    client = ZenRowsClient(api_key) if api_key else None
    listing_session = ListingSessionState()
    browser_page = create_browser_graphql_page() if LISTING_COLLECTION_MODE == "browser_graphql" else None
    if browser_page is not None:
        atexit.register(close_browser_graphql_page, browser_page)
        initialize_browser_graphql_session(browser_page)

    rows_by_page = {}
    page_benchmarks = []
    raw_search = []
    bootstrap_attempts = []
    realtime_benchmarks_path = RUN_ROOT / "benchmarks" / "page_benchmarks.csv"
    if realtime_benchmarks_path.exists():
        realtime_benchmarks_path.unlink()

    print(f"RUN_ROOT={RUN_ROOT}")
    print(
        f"SEARCH_TERM={SEARCH_TERM} pages={SEARCH_PAGES} mode={LISTING_COLLECTION_MODE} "
        f"endpoint={GRAPHQL_ENDPOINT if LISTING_COLLECTION_MODE in {'graphql', 'browser_graphql'} else build_search_url(1)} "
        f"template={operation.get('source_path', '')}"
    )
    print(f"benchmark_start={run_started_at}")

    for page in range(1, SEARCH_PAGES + 1):
        if page > 1 and LISTING_PAGE_SLEEP_SECONDS > 0:
            print(f"[listing_page_sleep] before_page={page:03d} sleep={LISTING_PAGE_SLEEP_SECONDS:g}s", flush=True)
            time.sleep(LISTING_PAGE_SLEEP_SECONDS)
        cached = load_cached_page(page) if LISTING_COLLECTION_MODE == "graphql" else None
        if cached:
            response_json, meta, rows = cached
            source = "cache"
        else:
            response_json, meta, rows = collect_listing_page(
                page,
                operation,
                client,
                listing_session,
                bootstrap_attempts,
                browser_page,
            )
            source = meta.get("transport") or "network"
        rows_by_page[page] = rows
        summary = page_summary(page, rows, meta, response_json)
        summary["source"] = source
        summary.update(capture_listing_debug_screenshot(client, page, summary, source))
        page_benchmarks.append(summary)
        append_csv(realtime_benchmarks_path, summary, list(summary.keys()))
        raw_search.append(
            {
                "page": page,
                "url": build_search_url(page),
                "meta": meta,
                "summary": summary,
            }
        )
        print(
            f"page={page:03d} source={source} status={meta['status_code']} elapsed={meta['elapsed_seconds']}s "
            f"cost={meta['x_request_cost']} organic={summary['organic_count']} "
            f"ingrid={summary['sponsored_ingrid_count']} carousel={summary['sponsored_carousel_count']} "
            f"rows={summary['total_occurrence_count']} "
            f"response_fulfillment={summary['response_fulfillment_product_count']}/{summary['response_product_count']} "
            f"rows_any_availability={summary['rows_with_any_availability']}",
            flush=True,
        )

    retry_failed_pages_with_delay(
        operation,
        client,
        listing_session,
        bootstrap_attempts,
        browser_page,
        rows_by_page,
        page_benchmarks,
        raw_search,
        realtime_benchmarks_path,
    )
    page_benchmarks.sort(key=lambda summary: int(summary.get("page") or 0))
    raw_search.sort(key=lambda item: int(item.get("page") or 0))
    all_rows = [
        row
        for page in sorted(rows_by_page)
        for row in rows_by_page.get(page, [])
    ]

    parsed_dir = RUN_ROOT / "parsed"
    benchmarks_dir = RUN_ROOT / "benchmarks"
    write_csv(parsed_dir / "main_occurrences.csv", all_rows)
    write_csv(benchmarks_dir / "page_benchmarks.csv", page_benchmarks)
    (parsed_dir / "main_page_summary.json").write_text(
        json.dumps(page_benchmarks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (RUN_ROOT / "raw_search_summary.json").write_text(
        json.dumps(raw_search, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    run_elapsed = round(time.perf_counter() - run_start, 3)
    total_cost = 0.0
    for bootstrap in bootstrap_attempts:
        try:
            total_cost += float(bootstrap.get("x_request_cost") or 0)
        except ValueError:
            pass
    for summary in page_benchmarks:
        costs = str(summary.get("attempt_costs") or summary.get("x_request_cost") or "").split(",")
        for cost in costs:
            try:
                total_cost += float(cost or 0)
            except ValueError:
                pass
    listing_request_calls = sum(int(summary.get("attempt_count") or 1) for summary in page_benchmarks)
    graphql_post_calls = listing_request_calls if LISTING_COLLECTION_MODE in {"graphql", "browser_graphql"} else 0
    bootstrap_call_count = len(bootstrap_attempts)
    total_request_calls = listing_request_calls + bootstrap_call_count
    manifest = {
        "run_type": "step01_main_list",
        "run_root": rel_path(RUN_ROOT),
        "run_started_at": run_started_at,
        "run_finished_at": now(),
        "elapsed_seconds": run_elapsed,
        "search_term": SEARCH_TERM,
        "search_sort": SEARCH_SORT,
        "search_pages": SEARCH_PAGES,
        "organic_offset": ORGANIC_OFFSET,
        "graphql_endpoint": GRAPHQL_ENDPOINT,
        "fetch_mode": FETCH_MODE,
        "listing_collection_mode": LISTING_COLLECTION_MODE,
        "fetch_transports": manifest_fetch_transports(),
        "source_html": rel_path(SOURCE_HTML_PATH),
        "source_payload": rel_path(SOURCE_PAYLOAD_PATH),
        "allow_html_template": ALLOW_HTML_TEMPLATE,
        "source_template": operation.get("source_path", ""),
        "source_template_type": operation.get("source_type", ""),
        "expected_post_calls": SEARCH_PAGES if LISTING_COLLECTION_MODE in {"graphql", "browser_graphql"} else 0,
        "actual_post_calls": graphql_post_calls,
        "total_request_calls": total_request_calls,
        "listing_request_calls": listing_request_calls,
        "graphql_post_calls": graphql_post_calls,
        "bootstrap_call_count": bootstrap_call_count,
        "bootstrap_attempts": bootstrap_attempts,
        "page_count": len(page_benchmarks),
        "retry_attempt_count": sum(max(0, int(summary.get("attempt_count") or 1) - 1) for summary in page_benchmarks),
        "failed_pages": [
            int(summary.get("page"))
            for summary in page_benchmarks
            if status_code_int(summary.get("status_code")) != 200
            or not listing_occurrence_count_complete(int(summary.get("total_occurrence_count") or 0))
        ],
        "listing_max_attempts": LISTING_MAX_ATTEMPTS,
        "listing_wait_ms": LISTING_WAIT_MS,
        "listing_retry_status_codes": sorted(LISTING_RETRY_STATUS_CODES),
        "listing_retry_sleep_seconds": LISTING_RETRY_SLEEP_SECONDS,
        "listing_retry_max_sleep_seconds": LISTING_RETRY_MAX_SLEEP_SECONDS,
        "listing_retry_sleep_sequence": LISTING_RETRY_SLEEP_SEQUENCE,
        "listing_page_sleep_seconds": LISTING_PAGE_SLEEP_SECONDS,
        "listing_recovery_enabled": LISTING_RECOVERY_ENABLED,
        "listing_recovery_profiles": LISTING_RECOVERY_PROFILE_NAMES,
        "listing_recovery_attempts_per_profile": LISTING_RECOVERY_ATTEMPTS_PER_PROFILE,
        "listing_recovery_wait_ms": LISTING_RECOVERY_WAIT_MS,
        "listing_recovery_attempt_count": sum(
            int(summary.get("recovery_attempt_count") or 0) for summary in page_benchmarks
        ),
        "listing_recovered_pages": [
            int(summary.get("page"))
            for summary in page_benchmarks
            if int(summary.get("recovery_success") or 0) == 1
        ],
        "listing_recovery_still_failed_pages": [
            int(summary.get("page"))
            for summary in page_benchmarks
            if int(summary.get("recovery_attempt_count") or 0) > 0
            and (
                status_code_int(summary.get("status_code")) != 200
                or not listing_occurrence_count_complete(int(summary.get("total_occurrence_count") or 0))
            )
        ],
        "listing_failed_page_retry_rounds": LISTING_FAILED_PAGE_RETRY_ROUNDS,
        "listing_failed_page_retry_sleep_seconds": LISTING_FAILED_PAGE_RETRY_SLEEP_SECONDS,
        "listing_debug_screenshot_enabled": LISTING_DEBUG_SCREENSHOT_ENABLED,
        "listing_debug_screenshot_mode": LISTING_DEBUG_SCREENSHOT_MODE,
        "listing_debug_screenshot_wait_ms": LISTING_DEBUG_SCREENSHOT_WAIT_MS,
        "listing_html_fallback_enabled": LISTING_HTML_FALLBACK_ENABLED,
        "listing_html_fallback_wait_ms": LISTING_HTML_FALLBACK_WAIT_MS,
        "listing_html_fallback_min_rows": LISTING_HTML_FALLBACK_MIN_ROWS,
        "listing_html_fallback_scroll_enabled": LISTING_HTML_FALLBACK_SCROLL_ENABLED,
        "listing_html_fallback_scroll_steps": LISTING_HTML_FALLBACK_SCROLL_STEPS,
        "listing_html_fallback_scroll_y": LISTING_HTML_FALLBACK_SCROLL_Y,
        "listing_html_fallback_scroll_wait_ms": LISTING_HTML_FALLBACK_SCROLL_WAIT_MS,
        "listing_html_fallback_scroll_final_wait_ms": LISTING_HTML_FALLBACK_SCROLL_FINAL_WAIT_MS,
        "listing_html_fallback_scroll_reset_top": LISTING_HTML_FALLBACK_SCROLL_RESET_TOP,
        "listing_html_fallback_pages": [
            int(summary.get("page"))
            for summary in page_benchmarks
            if "html_dom_fallback" in str(summary.get("attempt_profiles") or "")
        ],
        "listing_dom_pages": [
            int(summary.get("page"))
            for summary in page_benchmarks
            if "html_dom" in str(summary.get("attempt_profiles") or "")
            or summary.get("source") == "zenrows_html_dom"
        ],
        "listing_debug_screenshots": [
            {
                "page": int(summary.get("page")),
                "source": summary.get("source", ""),
                "status_code": summary.get("debug_screenshot_status_code", ""),
                "bytes": summary.get("debug_screenshot_bytes", ""),
                "cost": summary.get("debug_screenshot_cost", ""),
                "path": summary.get("debug_screenshot_path", ""),
                "final_url": summary.get("debug_screenshot_final_url", ""),
            }
            for summary in page_benchmarks
            if summary.get("debug_screenshot_path")
        ],
        "listing_delayed_retry_pages": [
            int(summary.get("page"))
            for summary in page_benchmarks
            if int(summary.get("delayed_retry_round") or 0) > 0
        ],
        "listing_delayed_retry_success_pages": [
            int(summary.get("page"))
            for summary in page_benchmarks
            if int(summary.get("delayed_retry_round") or 0) > 0
            and status_code_int(summary.get("status_code")) == 200
            and listing_occurrence_count_complete(int(summary.get("total_occurrence_count") or 0))
        ],
        "listing_zenrows_mode": "auto" if zenrows_mode_auto() else "manual",
        "listing_session_enabled": LISTING_SESSION_ENABLED,
        "listing_session_bootstrap": LISTING_SESSION_BOOTSTRAP,
        "listing_session_max_age_seconds": LISTING_SESSION_MAX_AGE_SECONDS,
        "listing_session_count": (
            listing_session.generation
            if LISTING_SESSION_ENABLED and total_request_calls
            else 0
        ),
        "total_x_request_cost": round(total_cost, 7),
        "main_occurrences": len(all_rows),
        "unique_skus": len({row.get("sku_id") for row in all_rows if row.get("sku_id")}),
        "organic_occurrences": sum(1 for row in all_rows if row.get("container_type") == "organic_product"),
        "sponsored_ingrid_occurrences": sum(1 for row in all_rows if row.get("container_type") == "sponsored_ingrid"),
        "sponsored_carousel_occurrences": sum(1 for row in all_rows if row.get("container_type") == "sponsored_carousel"),
        "outputs": {
            "main_occurrences": rel_path(parsed_dir / "main_occurrences.csv"),
            "page_benchmarks": rel_path(benchmarks_dir / "page_benchmarks.csv"),
            "main_page_summary": rel_path(parsed_dir / "main_page_summary.json"),
        },
    }
    (RUN_ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=" * 80)
    print(f"benchmark_end={manifest['run_finished_at']}")
    print(
        f"elapsed={run_elapsed}s calls={manifest['total_request_calls']} "
        f"cost={manifest['total_x_request_cost']} rows={manifest['main_occurrences']} "
        f"unique_skus={manifest['unique_skus']}"
    )
    print(f"main_csv={parsed_dir / 'main_occurrences.csv'}")
    print(f"benchmarks_csv={benchmarks_dir / 'page_benchmarks.csv'}")
    print(f"manifest={RUN_ROOT / 'manifest.json'}")


if __name__ == "__main__":
    main()
