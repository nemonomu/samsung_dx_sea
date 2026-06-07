from __future__ import annotations

import argparse
import copy
import csv
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1] / "samsung_dx_retail_com"


DROP_HEADERS = {
    "content-length",
    "host",
    ":method",
    ":scheme",
    ":authority",
    ":path",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def normalize_headers(headers: dict[str, Any], operation_header: str) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in (headers or {}).items():
        lower = str(key).lower()
        if lower in DROP_HEADERS or value is None:
            continue
        clean[str(key)] = str(value)
    clean["accept"] = "application/json"
    clean["content-type"] = "application/json"
    clean["x-o-gql-query"] = operation_header
    clean.setdefault("x-o-platform", "rweb")
    clean.setdefault("x-o-mart", "B2C")
    clean.setdefault("x-o-bu", "WALMART-US")
    clean.setdefault("x-o-segment", "oaoh")
    clean.setdefault("tenant-id", "elh9ie")
    return clean


def location_context(zip_code: str, store_id: str, state: str) -> dict[str, Any]:
    store_front = {
        "anchorStore": True,
        "deliveryStore": True,
        "deliveryWICAgencies": [],
        "pickupStore": True,
        "pickupWICAgencies": [],
        "stateOrProvinceCode": state,
        "storeId": store_id,
    }
    return {
        "storeId": store_id,
        "stateCode": state,
        "zipCode": zip_code,
        "storeFrontIds": [store_front],
        "intentStrength": "IMPLICIT",
    }


def merge_location(existing: Any, loc: dict[str, Any], zip_code: str, store_id: str, state: str) -> dict[str, Any]:
    out = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    out.update(loc)
    if "deliveryStore" in out:
        out["deliveryStore"] = store_id
    if "pickupStore" in out:
        out["pickupStore"] = store_id
    if "stateCode" in out:
        out["stateCode"] = state
    if "zipCode" in out:
        out["zipCode"] = zip_code
    if "storeId" in out:
        out["storeId"] = store_id
    if "storeFrontIds" in out:
        out["storeFrontIds"] = loc["storeFrontIds"]
    return out


def replace_all_location_contexts(value: Any, loc: dict[str, Any], zip_code: str, store_id: str, state: str) -> int:
    replaced = 0
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if key == "locationContext":
                value[key] = merge_location(child, loc, zip_code, store_id, state)
                replaced += 1
            else:
                replaced += replace_all_location_contexts(child, loc, zip_code, store_id, state)
    elif isinstance(value, list):
        for child in value:
            replaced += replace_all_location_contexts(child, loc, zip_code, store_id, state)
    return replaced


def replace_location(body: dict[str, Any], loc: dict[str, Any], inject_if_missing: bool = False) -> dict[str, Any]:
    out = copy.deepcopy(body)
    variables = out.get("variables")
    if not isinstance(variables, dict):
        raise ValueError("seed body has no variables")
    replaced = replace_all_location_contexts(
        variables,
        loc,
        str(loc["zipCode"]),
        str(loc["storeId"]),
        str(loc["stateCode"]),
    )
    if not replaced:
        if inject_if_missing:
            variables["locationContext"] = loc
        else:
            raise ValueError("seed body has no locationContext")
    return out


def request_json(label: str, url: str, headers: dict[str, str], body: dict[str, Any], timeout: int) -> tuple[dict[str, Any], Any]:
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    started = time.time()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    raw = b""
    status = None
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
    except Exception as exc:
        return {
            "label": label,
            "status": None,
            "reason": type(exc).__name__,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": str(exc),
        }, None
    decoded = raw
    if raw.startswith(b"\x1f\x8b"):
        try:
            decoded = gzip.decompress(raw)
        except Exception:
            decoded = raw
    text = decoded.decode("utf-8", errors="replace")
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        pass
    return {
        "label": label,
        "status": status,
        "reason": reason,
        "elapsed_ms": int((time.time() - started) * 1000),
        "json": parsed is not None,
        "top_keys": " | ".join(parsed.keys()) if isinstance(parsed, dict) else "",
        "data_keys": " | ".join((parsed.get("data") or {}).keys()) if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else "",
        "error_preview": preview_errors(parsed) if parsed is not None else re.sub(r"\s+", " ", text)[:500],
    }, parsed if parsed is not None else text


def preview_errors(parsed: Any) -> str:
    if not isinstance(parsed, dict) or not parsed.get("errors"):
        return ""
    return re.sub(r"\s+", " ", json.dumps(parsed.get("errors"), ensure_ascii=False))[:500]


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def response_signals(parsed: Any) -> dict[str, Any]:
    text = json.dumps(parsed, ensure_ascii=False, default=str) if parsed is not None else ""
    products = []
    for obj in walk(parsed):
        if not isinstance(obj, dict):
            continue
        if obj.get("usItemId") or obj.get("canonicalUrl"):
            products.append(
                {
                    "usItemId": obj.get("usItemId"),
                    "name": obj.get("name"),
                    "availabilityStatus": obj.get("availabilityStatus"),
                    "fulfillmentType": obj.get("fulfillmentType"),
                    "fulfillmentTitle": obj.get("fulfillmentTitle"),
                }
            )
        if len(products) >= 5:
            break
    return {
        "has_11581": "11581" in text,
        "has_5293": "5293" in text,
        "has_95829": "95829" in text,
        "has_3081": "3081" in text,
        "product_count_sampled": len(products),
        "products_preview": products,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Walmart GraphQL seed response with replaced locationContext")
    parser.add_argument("--seed", type=Path, default=ROOT / "log" / "walmart_api_seeds_fullheaders.json")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "log" / "walmart_location_context_apply_probe")
    parser.add_argument("--zip-code", default="11581")
    parser.add_argument("--store-id", default="5293")
    parser.add_argument("--state", default="NY")
    parser.add_argument("--seed-index", type=int, default=2, help="1-based seed index. Default 2 is first AdV3 seed.")
    parser.add_argument("--inject-if-missing", action="store_true")
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()

    seeds = load_json(args.seed).get("seeds") or []
    seed = seeds[args.seed_index - 1]
    url = seed["url"]
    headers = normalize_headers(seed.get("headers") or {}, seed.get("headers", {}).get("x-o-gql-query") or "query AdV3")
    original_body = seed["body"]
    target_body = replace_location(
        original_body,
        location_context(args.zip_code, args.store_id, args.state),
        inject_if_missing=args.inject_if_missing,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for label, body in (("original", original_body), ("target_11581", target_body)):
        print(f"[location-context] POST {label} seed_index={args.seed_index}")
        row, parsed = request_json(label, url, headers, body, args.timeout)
        signals = response_signals(parsed)
        row.update({k: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v for k, v in signals.items()})
        rows.append(row)
        dump_json(args.out_dir / f"{label}.json", parsed)

    dump_json(args.out_dir / "target_location_context.json", location_context(args.zip_code, args.store_id, args.state))
    with (args.out_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"[location-context] wrote {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
