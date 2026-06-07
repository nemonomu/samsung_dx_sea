from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ITEM_BTF_NAME = "ItemByIdBtf"
ITEM_BTF_HASH = "416881a26c8b729024477c66f06bd8448535ed85833ea3beb1b10534c5949672"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def walk(value: Any, path: str = "$") -> Iterable[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            yield from walk(child, f"{path}[{idx}]")


def find_first_metadata(data: Any) -> Optional[tuple[str, str, Dict[str, Any]]]:
    for path, value in walk(data):
        if isinstance(value, dict) and isinstance(value.get("p13nMetadata"), str):
            return path, value["p13nMetadata"], value
    return None


def find_seed(seed_path: Path, contains: str) -> Dict[str, Any]:
    data = load_json(seed_path)
    for seed in data.get("seeds") or []:
        if contains in str(seed.get("url") or ""):
            return seed
    return {}


def normalize_headers(headers: Dict[str, Any], referer: str, operation: str) -> Dict[str, str]:
    drop = {"content-length", "host", ":method", ":scheme", ":authority", ":path"}
    out: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        if str(key).lower() in drop or value is None:
            continue
        out[str(key)] = str(value)
    out["accept"] = "application/json"
    out["accept-language"] = "en-US"
    out["Referer"] = referer
    out["WM_PAGE_URL"] = referer
    out["x-o-gql-query"] = f"query {operation}"
    out["X-APOLLO-OPERATION-NAME"] = operation
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

    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST")
    raw = b""
    status: Any = None
    reason = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            reason = getattr(resp, "reason", "")
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        reason = exc.reason
        raw = exc.read()
    text = raw.decode("utf-8", errors="replace")
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = {"raw_text": text[:2000]}
    return {"status": status, "reason": reason, "bytes": len(raw), "url": url}, parsed


def similar_names(data: Any) -> List[str]:
    names: List[str] = []
    modules = ((data.get("data") or {}).get("contentLayout") or {}).get("modules") or []
    for module in modules:
        if not isinstance(module, dict):
            continue
        cfg = module.get("configs") or {}
        title = str(cfg.get("title") or "").strip()
        if title.lower() != "similar items you might like":
            continue
        for product in cfg.get("products") or cfg.get("similarItems") or []:
            if not isinstance(product, dict):
                continue
            name = str(product.get("name") or product.get("productName") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--detail-next", type=Path, required=True)
    parser.add_argument("--item", default="14365163951")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("log/walmart_btf_metadata_probe"))
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    sys.path.insert(0, str(args.project_root))
    seed_path = args.project_root / "log" / "walmart_api_seeds_fullheaders.json"
    seed = find_seed(seed_path, ITEM_BTF_NAME)
    seed_vars = copy.deepcopy(((seed.get("body") or {}).get("variables") or {}))
    meta_found = find_first_metadata(load_json(args.detail_next))
    if not seed_vars or not meta_found:
        print(json.dumps({"error": "missing seed vars or current metadata", "has_seed": bool(seed_vars), "has_meta": bool(meta_found)}, ensure_ascii=False))
        return 1

    meta_path, current_meta, current_meta_node = meta_found
    variants: List[tuple[str, Dict[str, Any]]] = []

    v1 = copy.deepcopy(seed_vars)
    v1["iId"] = args.item
    if isinstance(v1.get("p13nCls"), dict):
        v1["p13nCls"]["pageId"] = args.item
    variants.append(("item_only", v1))

    v2 = copy.deepcopy(v1)
    if isinstance(v2.get("p13nCls"), dict):
        v2["p13nCls"]["p13nMetadata"] = current_meta
    variants.append(("current_p13n_metadata", v2))

    v3 = copy.deepcopy(v1)
    if isinstance(v3.get("p13nCls"), dict):
        for key, value in current_meta_node.items():
            if key in {"p13nMetadata", "contentLayoutVersion", "previousRefreshCount", "totalPages"}:
                v3["p13nCls"][key] = value
    variants.append(("current_p13n_known_keys", v3))

    endpoint = f"https://www.walmart.com/orchestra/pdp/graphql/{ITEM_BTF_NAME}/{ITEM_BTF_HASH}/ip/{args.item}"
    headers = normalize_headers(seed.get("headers") or {}, args.url, ITEM_BTF_NAME)
    rows = []
    for label, variables in variants:
        meta, parsed = request_json(endpoint, headers, {"variables": variables}, args.timeout)
        names = similar_names(parsed if isinstance(parsed, dict) else {})
        dump_json(args.out_dir / f"{label}_response.json", parsed)
        rows.append(
            {
                "label": label,
                **meta,
                "metadata_path": meta_path,
                "similar_count": len(names),
                "similar_names": names,
            }
        )
    dump_json(args.out_dir / "summary.json", rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
