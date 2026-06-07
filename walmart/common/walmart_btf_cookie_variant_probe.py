from __future__ import annotations

import copy
import gzip
import http.cookiejar
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any


PROJECT = Path(r"C:\Users\gomguard\Documents\퀵오일\삼성전자\samsung_dx_retail_com\samsung_dx_retail_com")
ITEM = "14365163951"
URL = "https://www.walmart.com/ip/LG-65-4K-UHD-UA75-AI-Smart-TV-65UA7500/14365163951"
DETAIL_NEXT = PROJECT / "log" / "walmart_raw_http_sample_14365163951_v3" / "raw" / "detail_review" / ITEM / "detail_next_data.json"
SEED = PROJECT / "log" / "walmart_api_seeds_fullheaders.json"
OUT = Path(r"C:\tmp\walmart_btf_cookie_variant_14365163951")
OP = "ItemByIdBtf"
HASH = "416881a26c8b729024477c66f06bd8448535ed85833ea3beb1b10534c5949672"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def find_seed() -> dict[str, Any]:
    for seed in load(SEED).get("seeds") or []:
        if OP in str(seed.get("url") or ""):
            return seed
    return {}


def initial_data(next_data: dict[str, Any]) -> dict[str, Any]:
    return (((next_data.get("props") or {}).get("pageProps") or {}).get("initialData") or {}).get("data", {})


def winner_details(product: dict[str, Any]) -> dict[str, Any]:
    options: list[str] = []
    for option in product.get("fulfillmentOptions") or []:
        if isinstance(option, dict) and option.get("availabilityStatus") == "IN_STOCK" and option.get("type"):
            options.append(str(option.get("type")))
    return {
        "storeId": (((product.get("location") or {}).get("mpPickupLocation") or {}).get("storeId")) or "",
        "sellerId": product.get("sellerId") or "",
        "fulfillmentOptions": options,
    }


def build_vars(seed_vars: dict[str, Any], next_data: dict[str, Any]) -> dict[str, Any]:
    data = initial_data(next_data)
    layout = data.get("contentLayout") or {}
    metadata = layout.get("pageMetadata") or {}
    product = data.get("product") or {}
    lazy = [m for m in metadata.get("lazyModules") or [] if isinstance(m, dict)]
    out = copy.deepcopy(seed_vars)
    out["iId"] = ITEM
    out["version"] = "v2"
    out["p13nCls"] = {
        "pageId": ITEM,
        "skipPtcFetch": True,
        "availabilityStatus": product.get("availabilityStatus"),
        "winnerDetails": winner_details(product),
        "p13NCallType": "BTF",
        "p13nMetadata": metadata.get("p13nMetadata") or "",
        "lazyModules": lazy,
        "userClientInfo": {"isZipLocated": True, "callType": "CLIENT"},
        "userReqInfo": {
            "refererContext": {
                "source": "itempage",
                "variantSwitch": False,
                "itemSwitchContext": {"refererItem": None, "sizeReferer": None, "sizeReferers": None},
            }
        },
    }
    return out


def headers(seed_headers: dict[str, Any], variant: str) -> dict[str, str]:
    drop = {"content-length", "host", ":method", ":scheme", ":authority", ":path", "accept-encoding"}
    if variant == "drop_cookie":
        drop.add("cookie")
    out = {str(k): str(v) for k, v in seed_headers.items() if str(k).lower() not in drop and v is not None}
    out["accept"] = "application/json"
    out["accept-language"] = "en-US,en;q=0.9"
    out["Referer"] = URL
    out["WM_PAGE_URL"] = URL
    out["x-o-gql-query"] = f"query {OP}"
    out["X-APOLLO-OPERATION-NAME"] = OP
    out.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/148 Safari/537.36")
    out.setdefault("x-o-platform", "rweb")
    out.setdefault("x-o-mart", "B2C")
    out.setdefault("x-o-bu", "WALMART-US")
    out.setdefault("x-o-segment", "oaoh")
    out.setdefault("tenant-id", "elh9ie")
    return out


def read_response(resp: Any) -> bytes:
    raw = resp.read()
    enc = resp.headers.get("content-encoding", "")
    if raw.startswith(b"\x1f\x8b") or enc == "gzip":
        raw = gzip.decompress(raw)
    return raw


def post_json(body: dict[str, Any], hdrs: dict[str, str], opener: Any | None = None) -> dict[str, Any]:
    endpoint = f"https://www.walmart.com/orchestra/pdp/graphql/{OP}/{HASH}/ip/{ITEM}"
    raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(endpoint, data=raw_body, headers={**hdrs, "Content-Type": "application/json"}, method="POST")
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    with open_fn(req, timeout=35) as resp:
        raw = read_response(resp)
    return json.loads(raw.decode("utf-8", errors="replace"))


def session_from_detail(hdrs: dict[str, str]) -> Any:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req_headers = {
        "User-Agent": hdrs.get("User-Agent", "Mozilla/5.0"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(URL, headers=req_headers, method="GET")
    with opener.open(req, timeout=35) as resp:
        _ = read_response(resp)
    return opener


def similar(data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for module in (((data.get("data") or {}).get("contentLayout") or {}).get("modules") or []):
        if not isinstance(module, dict):
            continue
        cfg = module.get("configs") or {}
        if str(cfg.get("title") or "").strip().lower() != "similar items you might like":
            continue
        for product in cfg.get("products") or []:
            if isinstance(product, dict):
                name = str(product.get("name") or product.get("productName") or "").strip()
                if name and name not in names:
                    names.append(name)
    return names


def main() -> int:
    seed = find_seed()
    seed_vars = ((seed.get("body") or {}).get("variables") or {})
    vars_ = build_vars(seed_vars, load(DETAIL_NEXT))
    rows = []
    for variant in ("with_cookie", "drop_cookie", "detail_session_cookie"):
        hdrs = headers(seed.get("headers") or {}, "drop_cookie" if variant == "detail_session_cookie" else variant)
        opener = session_from_detail(hdrs) if variant == "detail_session_cookie" else None
        data = post_json({"variables": vars_}, hdrs, opener)
        names = similar(data)
        dump(OUT / f"{variant}_response.json", data)
        rows.append({"variant": variant, "count": len(names), "names": names})
    dump(OUT / "summary.json", rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
