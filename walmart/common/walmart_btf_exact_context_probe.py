from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


ITEM_BTF_NAME = "ItemByIdBtf"
ITEM_BTF_HASH = "416881a26c8b729024477c66f06bd8448535ed85833ea3beb1b10534c5949672"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def find_seed(seed_path: Path) -> Dict[str, Any]:
    data = load_json(seed_path)
    for seed in data.get("seeds") or []:
        if ITEM_BTF_NAME in str(seed.get("url") or ""):
            return seed
    return {}


def normalize_headers(headers: Dict[str, Any], referer: str) -> Dict[str, str]:
    drop = {"content-length", "host", ":method", ":scheme", ":authority", ":path", "accept-encoding"}
    out: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        if str(key).lower() in drop or value is None:
            continue
        out[str(key)] = str(value)
    out["accept"] = "application/json"
    out["accept-language"] = "en-US"
    out["Referer"] = referer
    out["WM_PAGE_URL"] = referer
    out["x-o-gql-query"] = f"query {ITEM_BTF_NAME}"
    out["X-APOLLO-OPERATION-NAME"] = ITEM_BTF_NAME
    out.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/148 Safari/537.36")
    out.setdefault("x-o-platform", "rweb")
    out.setdefault("x-o-mart", "B2C")
    out.setdefault("x-o-bu", "WALMART-US")
    out.setdefault("x-o-segment", "oaoh")
    out.setdefault("tenant-id", "elh9ie")
    return out


def request_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: int) -> tuple[Dict[str, Any], Any]:
    import urllib.error
    import urllib.request
    import gzip

    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST")
    raw = b""
    status: Any = None
    reason = ""
    encoding = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            reason = getattr(resp, "reason", "")
            encoding = resp.headers.get("content-encoding", "")
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        reason = exc.reason
        encoding = exc.headers.get("content-encoding", "") if exc.headers else ""
        raw = exc.read()
    if raw and (encoding == "gzip" or raw.startswith(b"\x1f\x8b")):
        raw = gzip.decompress(raw)
    elif raw and encoding == "br":
        try:
            import brotli  # type: ignore

            raw = brotli.decompress(raw)
        except Exception:
            pass
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = {"raw_text": text[:3000]}
    return {"status": status, "reason": reason, "bytes": len(raw), "url": url}, parsed


def content_layout(detail_next: Dict[str, Any]) -> Dict[str, Any]:
    return (((detail_next.get("props") or {}).get("pageProps") or {}).get("initialData") or {}).get("data", {}).get(
        "contentLayout", {}
    )


def product_data(detail_next: Dict[str, Any]) -> Dict[str, Any]:
    return (((detail_next.get("props") or {}).get("pageProps") or {}).get("initialData") or {}).get("data", {}).get(
        "product", {}
    )


def winner_details(product: Dict[str, Any]) -> Dict[str, Any]:
    store_id = (((product.get("location") or {}).get("mpPickupLocation") or {}).get("storeId")) or ""
    seller_id = product.get("sellerId") or ""
    options: List[str] = []
    for option in product.get("fulfillmentOptions") or []:
        if not isinstance(option, dict):
            continue
        if option.get("__typename") in {"PickupOptionV2", "DeliveryOptionV2", "ShippingOptionV2"}:
            if option.get("availabilityStatus") == "IN_STOCK" and option.get("type"):
                options.append(option.get("type"))
    return {"storeId": store_id, "sellerId": seller_id, "fulfillmentOptions": options}


def exact_p13n_cls(item: str, detail_next: Dict[str, Any], lazy_modules: List[Dict[str, Any]]) -> Dict[str, Any]:
    layout = content_layout(detail_next)
    metadata = layout.get("pageMetadata") or {}
    product = product_data(detail_next)
    return {
        "pageId": item,
        "skipPtcFetch": True,
        "availabilityStatus": product.get("availabilityStatus"),
        "winnerDetails": winner_details(product),
        "p13NCallType": "BTF",
        "p13nMetadata": metadata.get("p13nMetadata") or "",
        "lazyModules": lazy_modules,
        "userClientInfo": {"isZipLocated": True, "callType": "CLIENT"},
        "userReqInfo": {
            "refererContext": {
                "source": "itempage",
                "variantSwitch": False,
                "itemSwitchContext": {"refererItem": None, "sizeReferer": None, "sizeReferers": None},
            }
        },
    }


def similar_names(data: Any) -> List[str]:
    names: List[str] = []
    modules = ((data.get("data") or {}).get("contentLayout") or {}).get("modules") or []
    for module in modules:
        if not isinstance(module, dict):
            continue
        cfg = module.get("configs") or {}
        title = str(cfg.get("title") or "").strip().lower()
        if title != "similar items you might like":
            continue
        for product in cfg.get("products") or []:
            if not isinstance(product, dict):
                continue
            name = str(product.get("name") or product.get("productName") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe ItemByIdBtf with current PDP lazyModules/p13nMetadata.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--detail-next", type=Path, required=True)
    parser.add_argument("--item", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    sys.path.insert(0, str(args.project_root))
    seed = find_seed(args.project_root / "log" / "walmart_api_seeds_fullheaders.json")
    seed_vars = copy.deepcopy(((seed.get("body") or {}).get("variables") or {}))
    detail_next = load_json(args.detail_next)
    metadata = content_layout(detail_next).get("pageMetadata") or {}
    lazy_modules = [m for m in metadata.get("lazyModules") or [] if isinstance(m, dict)]
    similar_modules = [m for m in lazy_modules if m.get("moduleId") == "d5046d65-dd59-411b-ad2f-3e27a1cc8331"]
    if not seed_vars or not lazy_modules:
        print(json.dumps({"error": "missing seed variables or current lazyModules"}, ensure_ascii=False))
        return 1

    endpoint = f"https://www.walmart.com/orchestra/pdp/graphql/{ITEM_BTF_NAME}/{ITEM_BTF_HASH}/ip/{args.item}"
    headers = normalize_headers(seed.get("headers") or {}, args.url)

    variants: list[tuple[str, Dict[str, Any]]] = []
    baseline = copy.deepcopy(seed_vars)
    baseline["iId"] = args.item
    if isinstance(baseline.get("p13nCls"), dict):
        baseline["p13nCls"]["pageId"] = args.item
    variants.append(("baseline_seed_lazy", baseline))

    all_lazy = copy.deepcopy(seed_vars)
    all_lazy["iId"] = args.item
    all_lazy["version"] = "v2" if str(metadata.get("contentLayoutVersion") or "").upper() == "V2" else "v1"
    all_lazy["p13nCls"] = exact_p13n_cls(args.item, detail_next, lazy_modules)
    variants.append(("current_all_lazy", all_lazy))

    similar_only = copy.deepcopy(all_lazy)
    similar_only["p13nCls"] = exact_p13n_cls(args.item, detail_next, similar_modules)
    variants.append(("current_similar_only", similar_only))

    rows = []
    for label, variables in variants:
        meta, parsed = request_json(endpoint, headers, {"variables": variables}, args.timeout)
        names = similar_names(parsed if isinstance(parsed, dict) else {})
        dump_json(args.out_dir / f"{label}_request.json", {"variables": variables})
        dump_json(args.out_dir / f"{label}_response.json", parsed)
        rows.append({**meta, "label": label, "similar_count": len(names), "similar_names": names})

    dump_json(args.out_dir / "summary.json", rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
