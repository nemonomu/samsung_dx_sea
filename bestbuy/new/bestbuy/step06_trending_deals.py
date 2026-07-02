import csv
import html
import json
import os
import re
import time
from urllib.parse import unquote
from datetime import datetime
from pathlib import Path

from .step00_apollo import iter_apollo_push_payloads
from .step00_browser_session import (
    add_intl_nosplash,
    browser_fetch_graphql,
    browser_outer_html,
    close_browser_page,
    create_browser_page,
    env_bool,
    env_int,
)
from .step00_config import DEFAULT_BESTBUY_RUN_ROOT, bestbuy_category, has_target_url, load_initial_urls, rel_path


RUN_DATE = os.getenv("BESTBUY_RUN_DATE", datetime.now().strftime("%Y%m%d"))
CATEGORY = bestbuy_category()
INPUT_HTML = Path(os.getenv("BESTBUY_TRENDING_HTML", "references/bestbuy_tv_trending_page_sample.html"))
RUN_ROOT = Path(os.getenv("BESTBUY_TRENDING_RUN_ROOT", DEFAULT_BESTBUY_RUN_ROOT / "trending"))
FETCH_MODE = os.getenv("BESTBUY_TRENDING_FETCH_MODE", "browser_graphql").strip().lower()
SOURCE_PAYLOAD_ENV = os.getenv("BESTBUY_TRENDING_SOURCE_PAYLOAD", "").strip()
TRENDING_URL_ENV = os.getenv("BESTBUY_TRENDING_URL", "").strip()
SOURCE_PAYLOAD_PATH = Path(
    SOURCE_PAYLOAD_ENV or f"references/bestbuy_trending_{CATEGORY.lower()}_request.json"
)
SOURCE_PAYLOAD_FALLBACK_PATH = Path("references/bestbuy_trending_request.json")
OUTPUT_CSV = Path(
    os.getenv(
        "BESTBUY_TRENDING_OUTPUT",
        DEFAULT_BESTBUY_RUN_ROOT / "trending" / "parsed" / "trending_products.csv",
    )
)
TRENDING_URL = TRENDING_URL_ENV or load_initial_urls().get("trending_tvs_projectors", "")
LIMIT = int(os.getenv("BESTBUY_TRENDING_LIMIT", "10"))
SKIP_IF_NO_SOURCE = os.getenv("BESTBUY_TRENDING_SKIP_IF_NO_SOURCE", "1").lower() in {"1", "true", "yes", "y"}
BROWSER_WAIT_SECONDS = max(0, int(os.getenv("BESTBUY_TRENDING_BROWSER_WAIT_SECONDS", "8")))
BROWSER_JS_TIMEOUT = max(1, int(os.getenv("BESTBUY_TRENDING_BROWSER_JS_TIMEOUT", "120")))
BROWSER_HEADLESS = env_bool("BESTBUY_TRENDING_BROWSER_HEADLESS", "0")
BROWSER_LOCAL_PORT = env_int("BESTBUY_TRENDING_BROWSER_LOCAL_PORT", "0")
REQUIRE_ROWS = os.getenv(
    "BESTBUY_TRENDING_REQUIRE_ROWS",
    "1",
).lower() in {"1", "true", "yes", "y"}
ALLOW_NETWORK_SKU_FALLBACK = os.getenv(
    "BESTBUY_TRENDING_ALLOW_NETWORK_SKUS",
    "1",
).lower() in {"1", "true", "yes", "y"}
DEFAULT_TREND_SECTION = (
    "Trending Deals in Cell Phones & Accessories"
    if CATEGORY == "HHP"
    else "Trending Deals in TVs & Projectors"
)
TREND_SECTION = os.getenv("BESTBUY_TRENDING_SECTION", DEFAULT_TREND_SECTION)
SKU_WINDOW = os.getenv("BESTBUY_TRENDING_SKU_WINDOW", "tail").strip().lower()
BESTBUY_BASE_URL = "https://www.bestbuy.com"


def now():
    return datetime.now().isoformat(timespec="seconds")


def decode_capture_text(text):
    decoded = unquote(str(text or "").replace("^%^", "%"))
    decoded = decoded.replace("^\\^\"", '"').replace("^\"", '"').replace("^", "")
    decoded = decoded.replace('\\"', '"')
    return html.unescape(decoded)


def clean_text(value):
    return " ".join(str(value or "").split())


def walk_nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_nodes(item)


def absolute_url(path):
    if not path:
        return ""
    if path.startswith("http"):
        return path
    if path.startswith("/"):
        return f"{BESTBUY_BASE_URL}{path}"
    return path


def extract_analytics_sku_sequences(text):
    sequences = []
    seen = set()
    for match in re.finditer(r"\bskus\b[^\n\r]{0,8000}", text, flags=re.IGNORECASE):
        snippet = decode_capture_text(match.group(0))
        skus = re.findall(r"\b\d{7}\b", snippet)
        if len(skus) < 3:
            continue
        key = tuple(skus)
        if key in seen:
            continue
        seen.add(key)
        sequences.append(skus)
    return sequences


def choose_trending_skus(text, limit=10):
    sequences = extract_analytics_sku_sequences(text)
    if not sequences:
        return []
    sequence = max(sequences, key=len)
    if not limit:
        return sequence
    if SKU_WINDOW in {"head", "first"}:
        return sequence[:limit]
    return sequence[-limit:]


def extract_structured_product_metadata(text):
    decoded = decode_capture_text(text)
    metadata = {}

    patterns = [
        re.compile(
            r'"skuId"\s*:\s*"(?P<sku>\d{7})".{0,2500?}'
            r'"name"\s*:\s*\{[^{}]*"short"\s*:\s*"(?P<name>[^"]+)"[^{}]*\}.{0,2500?}'
            r'"url"\s*:\s*\{[^{}]*(?:"pdp"|"relativePdp"|"skuSpecificUrl")\s*:\s*"(?P<url>[^"]+)"',
            re.DOTALL,
        ),
        re.compile(
            r'"skuId"\s*:\s*"(?P<sku>\d{7})".{0,2500?}'
            r'"url"\s*:\s*\{[^{}]*(?:"pdp"|"relativePdp"|"skuSpecificUrl")\s*:\s*"(?P<url>[^"]+)"[^{}]*\}.{0,2500?}'
            r'"name"\s*:\s*\{[^{}]*"short"\s*:\s*"(?P<name>[^"]+)"',
            re.DOTALL,
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(decoded):
            sku = match.group("sku")
            metadata.setdefault(sku, {})
            metadata[sku].update(
                {
                    "retailer_sku_name": clean_text(match.group("name")),
                    "product_url": absolute_url(match.group("url")),
                }
            )

    return metadata


def clean_graphql_value(value):
    raw = str(value or "").replace('\\\\"', '\\"')
    try:
        decoded = json.loads(f'"{raw}"')
    except ValueError:
        decoded = raw
    return clean_text(html.unescape(str(decoded).replace("\\u0026", "&").replace("\\/", "/").replace('\\"', '"')))


def extract_spotlight_product_rows(text, limit=10):
    decoded = decode_capture_text(text).replace('\\\\"', '\\"')
    connection_pos = decoded.find('"__typename":"SpotlightProductConnection"')
    if connection_pos < 0:
        return []

    block = decoded[connection_pos : connection_pos + 250000]
    header_match = re.search(r'"storyHeader":"(?P<header>(?:\\.|[^"])*)"', block)
    trend_section = clean_graphql_value(header_match.group("header")) if header_match else TREND_SECTION
    pattern = re.compile(
        r'"__typename":"SpotlightProduct","sku":"(?P<sku>\d{7})"'
        r'(?P<body>.*?)'
        r'"bsin":"(?P<bsin>[A-Z0-9]+)","originalSkuId":"(?P<original_sku>\d{7})"',
        re.DOTALL,
    )
    rows = []
    seen = set()
    for match in pattern.finditer(block):
        sku = match.group("sku")
        if sku in seen:
            continue
        seen.add(sku)
        body = match.group("body")
        name_match = re.search(r'"short":"(?P<name>(?:\\.|[^"])*)"', body)
        url_match = re.search(r'"pdp":"(?P<url>(?:\\.|[^"])*)"', body)
        if not url_match:
            url_match = re.search(r'"relativePdp":"(?P<url>(?:\\.|[^"])*)"', body)
        rows.append(
            {
                "trend_section": trend_section,
                "trend_rank": len(rows) + 1,
                "sku_id": sku,
                "bsin": match.group("bsin"),
                "retailer_sku_name": clean_graphql_value(name_match.group("name")) if name_match else "",
                "product_url": absolute_url(clean_graphql_value(url_match.group("url"))) if url_match else "",
                "source_card_id": "",
                "source": "spotlight_product_connection",
            }
        )
        if limit and len(rows) >= limit:
            break
    return rows


def parse_trending_products(html_text, limit=10):
    spotlight_rows = extract_spotlight_product_rows(html_text, limit=limit)
    if spotlight_rows:
        return spotlight_rows
    if not ALLOW_NETWORK_SKU_FALLBACK:
        return []

    trend_skus = choose_trending_skus(html_text, limit=limit)
    metadata = extract_structured_product_metadata(html_text)
    rows = []
    for rank, sku in enumerate(trend_skus, 1):
        product = metadata.get(sku, {})
        rows.append(
            {
                "trend_section": TREND_SECTION,
                "trend_rank": rank,
                "sku_id": sku,
                "retailer_sku_name": product.get("retailer_sku_name", ""),
                "product_url": product.get("product_url", ""),
                "source_card_id": "",
                "source": "network_skus_with_structured_product_metadata" if product else "network_skus",
            }
        )
    return rows


def parse_json_value(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {}


def json_response_xhr_items(json_data):
    if not isinstance(json_data, dict):
        return []
    xhr = json_data.get("xhr") or []
    return xhr if isinstance(xhr, list) else []


def json_response_capture_texts(json_data):
    if not isinstance(json_data, (dict, list)):
        return []

    texts = []
    seen = set()

    def add_text(value):
        if value is None:
            return
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        value = str(value or "")
        if not value:
            return
        key = (len(value), value[:500])
        if key in seen:
            return
        seen.add(key)
        texts.append(value)

    if isinstance(json_data, dict):
        for key in ("html", "content", "body", "response", "responseText", "text"):
            add_text(json_data.get(key))
        for request in json_response_xhr_items(json_data):
            if isinstance(request, dict):
                for key in ("body", "response", "responseText", "content", "text", "html"):
                    add_text(request.get(key))
                add_text(request)
            else:
                add_text(request)
    add_text(json_data)
    return texts


def parse_trending_products_from_capture(html_text, json_data=None, limit=10):
    rows = parse_trending_products(html_text or "", limit=limit)
    if rows:
        return rows
    for capture_text in json_response_capture_texts(json_data):
        rows = parse_trending_products(capture_text, limit=limit)
        if rows:
            for row in rows:
                row["source"] = f"json_response_{row.get('source') or 'capture'}"
            return rows
    return []


def product_name(product):
    name = (product or {}).get("name") if isinstance(product, dict) else {}
    if isinstance(name, dict):
        return clean_text(name.get("short") or name.get("title") or name.get("display") or name.get("rawShort") or "")
    return clean_text(name)


def product_url(product):
    url = (product or {}).get("url") if isinstance(product, dict) else {}
    if isinstance(url, dict):
        return absolute_url(
            clean_text(url.get("pdp") or url.get("relativePdp") or url.get("skuSpecificUrl") or "")
        )
    return absolute_url(clean_text(url))


def spotlight_product_items(connection):
    items = []
    for key in ("items", "products", "nodes"):
        value = connection.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    edges = connection.get("edges")
    if isinstance(edges, list):
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict):
                items.append(node)
    if items:
        return items
    return [
        node
        for node in walk_nodes(connection)
        if node is not connection and node.get("__typename") == "SpotlightProduct"
    ]


def parse_trending_products_from_graphql(response_json, limit=10):
    rows = []
    seen = set()
    for node in walk_nodes(response_json):
        if node.get("__typename") != "SpotlightProductConnection":
            continue
        trend_section = clean_text(node.get("storyHeader") or TREND_SECTION)
        for item in spotlight_product_items(node):
            if not isinstance(item, dict):
                continue
            sku = clean_text(item.get("sku") or item.get("skuId") or item.get("originalSkuId") or "")
            if not sku or sku in seen:
                continue
            product = item.get("product") if isinstance(item.get("product"), dict) else item
            seen.add(sku)
            rows.append(
                {
                    "trend_section": trend_section,
                    "trend_rank": len(rows) + 1,
                    "sku_id": sku,
                    "bsin": clean_text(item.get("bsin") or product.get("bsin") or ""),
                    "retailer_sku_name": product_name(product),
                    "product_url": product_url(product),
                    "source_card_id": "",
                    "source": "browser_graphql_spotlight_product_connection",
                }
            )
            if limit and len(rows) >= limit:
                return rows
    return rows


def source_payload_candidates(path=SOURCE_PAYLOAD_PATH):
    candidates = [Path(path)]
    if not SOURCE_PAYLOAD_ENV and SOURCE_PAYLOAD_FALLBACK_PATH not in candidates:
        candidates.append(SOURCE_PAYLOAD_FALLBACK_PATH)
    return candidates


def existing_source_payload_path(path=SOURCE_PAYLOAD_PATH):
    for candidate in source_payload_candidates(path):
        if candidate.exists():
            return candidate
    return None


def no_exposed_trending_source():
    if not SKIP_IF_NO_SOURCE:
        return False
    if CATEGORY != "TV":
        return False
    if TRENDING_URL:
        return False
    if existing_source_payload_path() is not None:
        return False
    return True


def load_graphql_payload(path=SOURCE_PAYLOAD_PATH):
    path = existing_source_payload_path(path)
    if not path:
        searched = ", ".join(str(candidate) for candidate in source_payload_candidates())
        raise FileNotFoundError(
            f"Trending direct GraphQL source payload not found. searched={searched}. "
            "Set BESTBUY_TRENDING_SOURCE_PAYLOAD to a saved /gateway/graphql request body."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        for key in ("payload", "body", "request"):
            nested = payload.get(key)
            if isinstance(nested, (dict, list)):
                payload = nested
                break
    if not isinstance(payload, (dict, list)):
        raise ValueError(f"Trending direct GraphQL payload must be a JSON object or list: {path}")
    return payload


def operation_name_from_query(query):
    query = str(query or "")
    return query.split("{", 1)[0].replace("query", "", 1).strip().split("(", 1)[0]


def find_trending_started_operation(html_text):
    for payload in iter_apollo_push_payloads(html_text or ""):
        for event in payload.get("events", []):
            if event.get("type") != "started":
                continue
            options = event.get("options") or {}
            query = options.get("query") or ""
            if "SpotlightProductConnection" not in query and "SpotlightProduct" not in query:
                continue
            result = {
                "operationName": options.get("operationName") or operation_name_from_query(query),
                "variables": options.get("variables") or {},
                "query": query,
            }
            extensions = options.get("extensions")
            if isinstance(extensions, dict):
                result["extensions"] = extensions
            return result
    return None


def browser_graphql():
    if not TRENDING_URL:
        raise RuntimeError("Set BESTBUY_TRENDING_URL or target_urls.trend before browser trending collection")

    raw_dir = RUN_ROOT / "raw" / "browser_graphql"
    raw_dir.mkdir(parents=True, exist_ok=True)
    page = None
    browser_meta = {}
    try:
        page, browser_meta = create_browser_page(
            run_root=RUN_ROOT,
            name="trending_browser",
            headless=BROWSER_HEADLESS,
            local_port=BROWSER_LOCAL_PORT,
        )
        browser_url = add_intl_nosplash(TRENDING_URL)
        page.get(browser_url)
        if BROWSER_WAIT_SECONDS:
            time.sleep(BROWSER_WAIT_SECONDS)
        html_text = browser_outer_html(page, timeout=BROWSER_JS_TIMEOUT)
        html_path = raw_dir / "trending_browser_page.html"
        html_path.write_text(html_text, encoding="utf-8", errors="replace")

        html_rows = parse_trending_products_from_capture(html_text, {}, LIMIT)
        if html_rows:
            for row in html_rows:
                row["source"] = f"browser_html_{row.get('source') or 'payload'}"
            summary = {
                "started_at": now(),
                "live": True,
                "fetch_mode": "browser_page_payload",
                "url": TRENDING_URL,
                "browser_url": browser_url,
                "endpoint": "",
                "status_code": 200,
                "elapsed_seconds": 0,
                "x_request_cost": "0",
                "total_x_request_cost": 0,
                "call_count": 1,
                "bytes": len(html_text or ""),
                "payload_source": "browser_apollo_rehydrate_html",
                "html": rel_path(html_path),
                "browser": browser_meta,
                "row_count": len(html_rows),
                "success": True,
            }
            (RUN_ROOT / "summary_browser_graphql.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return {"__trending_rows": html_rows}

        payload = find_trending_started_operation(html_text)
        payload_source = "browser_apollo_started"
        if payload is None:
            payload_path = existing_source_payload_path()
            if payload_path:
                payload = load_graphql_payload(payload_path)
                payload_source = rel_path(payload_path)
        if payload is None:
            raise RuntimeError(
                "Trending browser page did not expose a SpotlightProduct GraphQL payload. "
                "Save a captured request JSON and set BESTBUY_TRENDING_SOURCE_PAYLOAD."
            )

        start = time.perf_counter()
        envelope = browser_fetch_graphql(page, payload, timeout=BROWSER_JS_TIMEOUT)
        elapsed = round(time.perf_counter() - start, 3)
    finally:
        close_browser_page(page)

    status_code = int(envelope.get("status") or 0)
    text = str(envelope.get("body") or "")
    request_path = raw_dir / "trending_request.json"
    response_path = raw_dir / "trending_response.txt"
    json_path = raw_dir / "trending_response.json"
    envelope_path = raw_dir / "trending_envelope.json"
    request_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    response_path.write_text(text, encoding="utf-8", errors="replace")
    envelope_path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")

    response_json = parse_json_value(text)
    if response_json:
        json_path.write_text(json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {
        "started_at": now(),
        "live": True,
        "fetch_mode": "browser_graphql",
        "url": TRENDING_URL,
        "browser_url": browser_url,
        "endpoint": "/gateway/graphql",
        "status_code": status_code,
        "elapsed_seconds": elapsed,
        "x_request_cost": "0",
        "total_x_request_cost": 0,
        "call_count": 1,
        "bytes": len(text or ""),
        "payload_source": payload_source,
        "request": rel_path(request_path),
        "response": rel_path(json_path if response_json else response_path),
        "envelope": rel_path(envelope_path),
        "browser": browser_meta,
        "success": status_code == 200 and bool(response_json),
    }
    (RUN_ROOT / "summary_browser_graphql.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if status_code != 200:
        raise RuntimeError(f"Trending browser GraphQL fetch failed: status={status_code}")
    if not response_json:
        raise RuntimeError("Trending browser GraphQL fetch returned non-JSON response")
    return response_json


def write_skip_summary(reason):
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    summary = {
        "started_at": now(),
        "live": True,
        "fetch_mode": FETCH_MODE,
        "skipped": True,
        "reason": reason,
        "source_payload": rel_path(SOURCE_PAYLOAD_PATH),
        "source_payload_searched": [rel_path(path) for path in source_payload_candidates()],
        "row_count": 0,
        "total_x_request_cost": 0,
    }
    (RUN_ROOT / "summary_skip.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def update_browser_summary(row_count):
    path = RUN_ROOT / "summary_browser_graphql.json"
    if not path.exists():
        return
    summary = parse_json_value(path.read_text(encoding="utf-8", errors="ignore"))
    if not isinstance(summary, dict):
        return
    summary["row_count"] = row_count
    summary["success"] = bool(summary.get("success")) and row_count > 0
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "trend_section",
                "trend_rank",
                "sku_id",
                "bsin",
                "retailer_sku_name",
                "product_url",
                "source_card_id",
                "source",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def write_failure_skip_summary(exc):
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    write_rows(OUTPUT_CSV, [])
    summary = {
        "started_at": now(),
        "live": True,
        "fetch_mode": FETCH_MODE,
        "skipped": True,
        "collection_failed": True,
        "reason": "trending collection failed; continuing pipeline with empty trending rows",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "source_payload": rel_path(SOURCE_PAYLOAD_PATH),
        "source_payload_searched": [rel_path(path) for path in source_payload_candidates()],
        "row_count": 0,
        "total_x_request_cost": 0,
        "call_count": 0,
        "csv": rel_path(OUTPUT_CSV),
    }
    (RUN_ROOT / "summary_skip.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main():
    if not has_target_url("trend"):
        write_rows(OUTPUT_CSV, [])
        print(f"skipped trending: no trend URL for category -> {OUTPUT_CSV}")
        return
    if no_exposed_trending_source():
        write_rows(OUTPUT_CSV, [])
        summary = write_skip_summary(
            "TV trending source is not exposed/configured; skipping step06 without collection"
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    rows = []
    if FETCH_MODE in {"auto", "browser", "browser_graphql"}:
        response_json = browser_graphql()
        if isinstance(response_json, dict) and isinstance(response_json.get("__trending_rows"), list):
            rows = response_json["__trending_rows"]
        else:
            rows = parse_trending_products_from_graphql(response_json, LIMIT)
        if REQUIRE_ROWS and not rows:
            raise RuntimeError(
                "Trending browser GraphQL returned 0 SpotlightProductConnection rows; "
                "verify the captured browser payload contains product data"
            )
    else:
        raise ValueError(
            "BESTBUY_TRENDING_FETCH_MODE must be browser_graphql. "
            "ZenRows page/direct GraphQL fallback is disabled for trending collection."
        )
    write_rows(OUTPUT_CSV, rows)
    update_browser_summary(len(rows))
    print(f"wrote {len(rows)} rows -> {OUTPUT_CSV}")
    for row in rows:
        print(f"{row['trend_rank']}. {row['sku_id']} {row['retailer_sku_name']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        write_failure_skip_summary(exc)
