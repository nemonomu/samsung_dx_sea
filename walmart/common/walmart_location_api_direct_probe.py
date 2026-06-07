from __future__ import annotations

import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1] / "samsung_dx_retail_com"
sys.path.insert(0, str(ROOT))

from walmart_detail_review_batch_probe import DEFAULT_SEED, item_from_url, make_page  # noqa: E402


UPDATE_POSTAL_CODE = {
    "type": "mutation",
    "name": "UpdatePostalCode",
    "hash": "0175f7072637b82757e89f50ed35676e89499341cd199d4032350ee9483cfc0e",
}
GET_CART = {
    "type": "query",
    "name": "getCart",
    "hash": "bc5d096f4fde6deb6f0d0a340c51fffde365925f012884fd9494b3333008875c",
}
MERGE_AND_GET_CART = {
    "type": "mutation",
    "name": "MergeAndGetCart",
    "hash": "17467ce8dab5ac42e209f62a47f87a9329d22c10b9038d423968fe0397f97d4a",
}
DELIVERY_ADDRESSES = {
    "type": "query",
    "name": "DeliveryAddresses",
    "hash": "aee49b90e801d9d70eb132fc20bba8eb0b364bb6d87f87274778e80f455aa2ee",
}
DELIVERY_ADDRESSES_SELECTOR_V2 = {
    "type": "query",
    "name": "DeliveryAddressesAddressSelectorV2",
    "hash": "6c5e0caa9d2840d6e2bcdd4a9b27f268eb9dd60cd6c505baad255ed564c9f80c",
}


def first_product_url() -> str:
    with Path(DEFAULT_SEED).open("r", encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))
    return row["product_url"]


def fetch_json_candidate(page: Any, url: str, body: dict[str, Any], operation: dict[str, str]) -> dict[str, Any]:
    script = r"""
    const url = arguments[0];
    const body = arguments[1];
    const appVersion = arguments[2];
    const rand = Math.random().toString(36).slice(2) + Date.now().toString(36);
    const trace = '00-' + Array.from(crypto.getRandomValues(new Uint8Array(16))).map(b => b.toString(16).padStart(2, '0')).join('') + '-' + Array.from(crypto.getRandomValues(new Uint8Array(8))).map(b => b.toString(16).padStart(2, '0')).join('') + '-00';
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url, false);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.setRequestHeader('x-o-ccm', 'server');
    xhr.setRequestHeader('x-o-gql-query', arguments[3].type + ' ' + arguments[3].name);
    xhr.setRequestHeader('X-APOLLO-OPERATION-NAME', arguments[3].name);
    xhr.setRequestHeader('x-o-platform', 'rweb');
    xhr.setRequestHeader('x-o-platform-version', appVersion);
    xhr.setRequestHeader('x-o-mart', 'B2C');
    xhr.setRequestHeader('x-o-bu', 'WALMART-US');
    xhr.setRequestHeader('x-o-segment', 'oaoh');
    xhr.setRequestHeader('x-o-correlation-id', rand);
    xhr.setRequestHeader('wm_qos.correlation_id', rand);
    xhr.setRequestHeader('WM_MP', 'true');
    xhr.setRequestHeader('WM_PAGE_URL', window.location.href);
    xhr.setRequestHeader('traceparent', trace);
    xhr.setRequestHeader('tenant-id', 'elh9ie');
    xhr.send(JSON.stringify(body));
    let parsed = null;
    try { parsed = JSON.parse(xhr.responseText || 'null'); } catch (e) {}
    return {
      status: xhr.status,
      statusText: xhr.statusText,
      finalUrl: xhr.responseURL || url,
      text: (xhr.responseText || '').slice(0, 1000),
      json: parsed
    };
    """
    return page.run_js(
        script,
        url,
        body,
        "usweb-1.268.0-966f60c9de57fae982545ada5ada32cd89c042ed-6041711r",
        operation,
        timeout=120,
    ) or {}


def fetch_html(page: Any, url: str) -> dict[str, Any]:
    script = r"""
    const url = arguments[0];
    const xhr = new XMLHttpRequest();
    xhr.open('GET', url, false);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8');
    xhr.setRequestHeader('Cache-Control', 'no-cache');
    xhr.setRequestHeader('Pragma', 'no-cache');
    xhr.send(null);
    return {
      status: xhr.status,
      statusText: xhr.statusText,
      finalUrl: xhr.responseURL || url,
      text: xhr.responseText || ''
    };
    """
    return page.run_js(script, url, timeout=120) or {}


def inspect_state(page: Any) -> dict[str, Any]:
    script = r"""
    const ls = {};
    const ss = {};
    for (const key of ['glassCartIdMap', 'hasCID', 'CID', 'hasACID', 'ACID', 'cartId']) {
      try { ls[key] = window.localStorage.getItem(key); } catch (e) {}
      try { ss[key] = window.sessionStorage.getItem(key); } catch (e) {}
    }
    return {
      cookies: document.cookie,
      localStorage: ls,
      sessionStorage: ss,
      url: window.location.href
    };
    """
    state = page.run_js(script, timeout=60) or {}
    cookies = str(state.get("cookies") or "")
    state["cookie_names"] = [part.split("=", 1)[0].strip() for part in cookies.split(";") if part.strip()]
    state["cookies_preview"] = cookies[:1000]
    state.pop("cookies", None)
    return state


def gql_url(endpoint: str, operation: dict[str, str]) -> str:
    return f"https://www.walmart.com{endpoint}/{operation['name']}/{operation['hash']}"


def post_gql(page: Any, endpoint: str, operation: dict[str, str], variables: dict[str, Any]) -> dict[str, Any]:
    return fetch_json_candidate(page, gql_url(endpoint, operation), {"variables": variables}, operation)


def summarize_gql(result: dict[str, Any]) -> dict[str, Any]:
    parsed = result.get("json")
    out: dict[str, Any] = {
        "status": result.get("status"),
        "statusText": result.get("statusText"),
        "finalUrl": result.get("finalUrl"),
        "data_keys": "",
        "cart_id": "",
        "customer_id": "",
        "customer_is_guest": "",
        "error_preview": "",
        "text_preview": str(result.get("text", ""))[:300],
    }
    if not isinstance(parsed, dict):
        return out
    data = parsed.get("data")
    if isinstance(data, dict):
        out["data_keys"] = " | ".join(data.keys())
        cart = None
        for value in data.values():
            if isinstance(value, dict):
                cart = value.get("cart") if isinstance(value.get("cart"), dict) else value
                break
        if isinstance(cart, dict):
            out["cart_id"] = cart.get("id") or ""
            customer = cart.get("customer") if isinstance(cart.get("customer"), dict) else {}
            out["customer_id"] = customer.get("id") or ""
            out["customer_is_guest"] = customer.get("isGuest")
    if parsed.get("errors"):
        out["error_preview"] = json.dumps(parsed.get("errors"), ensure_ascii=False)[:500]
    return out


def summarize_html(html: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "html_length": len(html),
        "has_11581": "11581" in html,
        "has_valley_stream": "Valley Stream" in html,
        "has_10118": "10118" in html,
        "has_sacramento": "Sacramento" in html,
        "robot_detected": any(
            token in html.lower()
            for token in ("robot or human", "press & hold", "press and hold", "verify you are human")
        ),
    }
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S | re.I)
    out["has_next_data"] = bool(match)
    if not match:
        return out
    try:
        data = json.loads(match.group(1))
    except Exception as exc:
        out["next_data_error"] = str(exc)
        return out
    product = (
        data.get("props", {})
        .get("pageProps", {})
        .get("initialData", {})
        .get("data", {})
        .get("product", {})
    )
    out["item"] = product.get("usItemId") or product.get("primaryProductId")
    out["fulfillment_options"] = [
        {
            "type": opt.get("type"),
            "locationText": opt.get("locationText"),
            "availabilityStatus": opt.get("availabilityStatus"),
        }
        for opt in (product.get("fulfillmentOptions") or [])
        if isinstance(opt, dict)
    ]
    out["fulfillment_has_target_zip"] = any(
        "11581" in str(opt.get("locationText") or "") or "Valley Stream" in str(opt.get("locationText") or "")
        for opt in out["fulfillment_options"]
    )
    return out


def main() -> int:
    out_dir = ROOT / "log" / "walmart_location_api_direct_probe"
    out_dir.mkdir(parents=True, exist_ok=True)

    zip_code = "11581"
    product_url = first_product_url()
    item = item_from_url(product_url)
    variables = {
        "postalAddress": {
            "postalCode": zip_code,
            "zipLocated": False,
            "stateOrProvinceCode": "",
            "stateOrProvinceName": "",
            "countryCode": "",
            "addressType": "",
            "isPoBox": False,
        }
    }
    cart_input = {
        "cartInput": {
            "cartId": None,
            "forceRefresh": True,
            "enableLiquorBox": False,
            "enableCartSplitClarity": True,
            "features": "{}",
        }
    }
    merge_input = {
        "input": {
            "cartId": None,
            "strategy": "DEFAULT",
            "enableLiquorBox": False,
            "enableCartSplitClarity": True,
            "features": "{}",
        },
        "detailed": True,
        "enableSavingsBreakup": True,
        "fetchAddOnServices": True,
    }
    delivery_addresses_input = {
        "responseGroup": "storeDeliverable",
        "fetchMXFields": False,
        "fetchCAFields": False,
        "DeliveryAddressOptionInput": {"registryIds": []},
        "enableGEPKYC": False,
        "enablePayByInvoiceBetaFF": False,
    }
    delivery_addresses_selector_input = {
        "DeliveryAddressOptionInput": {"registryIds": []},
        "enableGEPKYC": False,
        "enableGEPAddress": False,
    }

    page = make_page(headless=False)
    rows: list[dict[str, Any]] = []
    try:
        page.get(product_url)
        time.sleep(5)
        before = summarize_html(page.html or "")
        (out_dir / "before_summary.json").write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "state_after_product_page.json").write_text(
            json.dumps(inspect_state(page), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        steps = [
            ("update_before_cart", "/orchestra/home/graphql", UPDATE_POSTAL_CODE, variables),
            ("delivery_addresses", "/orchestra/home/graphql", DELIVERY_ADDRESSES, delivery_addresses_input),
            (
                "delivery_addresses_selector_v2",
                "/orchestra/home/graphql",
                DELIVERY_ADDRESSES_SELECTOR_V2,
                delivery_addresses_selector_input,
            ),
            ("update_after_address_queries", "/orchestra/home/graphql", UPDATE_POSTAL_CODE, variables),
            ("get_cart_null", "/orchestra/cartxo/graphql", GET_CART, cart_input),
            ("update_after_get_cart", "/orchestra/home/graphql", UPDATE_POSTAL_CODE, variables),
            ("merge_and_get_cart", "/orchestra/cartxo/graphql", MERGE_AND_GET_CART, merge_input),
            ("update_after_merge", "/orchestra/home/graphql", UPDATE_POSTAL_CODE, variables),
        ]

        for label, endpoint, operation, body_vars in steps:
            result = post_gql(page, endpoint, operation, body_vars)
            row = {"label": label, "endpoint": endpoint, "operation": operation["name"], **summarize_gql(result)}
            rows.append(row)
            (out_dir / f"{label}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            (out_dir / f"{label}_state.json").write_text(
                json.dumps(inspect_state(page), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            detail = fetch_html(page, product_url)
            detail_html = str(detail.get("text") or "")
            summary = summarize_html(detail_html)
            (out_dir / f"{label}_detail_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if summary.get("fulfillment_has_target_zip"):
                (out_dir / f"{label}_detail.html").write_text(detail_html, encoding="utf-8", errors="replace")
                break

        with (out_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        summary = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "zip_code": zip_code,
            "item": item,
            "product_url": product_url,
            "rows": rows,
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        try:
            page.quit()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
