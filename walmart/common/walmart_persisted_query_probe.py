"""Replay Walmart persisted GraphQL candidates discovered from JS chunks."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REVIEW_NAME = "ReviewsById"
REVIEW_HASH = "b26c3733f6f3677bce6628bc3bc8e90df6c8bf7c016ee98d1625de0918c6e1ae"
DYNAMIC_ITEM_NAME = "DynamicItemById"
DYNAMIC_ITEM_HASH = "213fbfcb77702bae4d3cb31a8f08640fbc974a47791bd7a5902f752c26af8384"
ITEM_BTF_NAME = "ItemByIdBtf"
ITEM_BTF_HASH = "416881a26c8b729024477c66f06bd8448535ed85833ea3beb1b10534c5949672"


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


def seed_entries(path: Path) -> Iterable[Dict[str, Any]]:
    data = load_json(path)
    raw = data.get("seeds") if isinstance(data, dict) else data
    for row in raw or []:
        if isinstance(row, dict):
            yield row


def normalize_headers(headers: Dict[str, Any], referer: str, operation: str) -> Dict[str, str]:
    clean: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        lower = str(key).lower()
        if lower in DROP_HEADERS:
            continue
        if value is None:
            continue
        clean[str(key)] = str(value)
    clean.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    )
    clean["accept"] = "application/json"
    clean["accept-language"] = "en-US"
    clean["Referer"] = referer
    clean["WM_PAGE_URL"] = referer
    clean["x-o-gql-query"] = f"query {operation}"
    clean["X-APOLLO-OPERATION-NAME"] = operation
    clean.setdefault("x-o-platform", "rweb")
    clean.setdefault("x-o-platform-version", "usweb-1.267.0-153ae89d7141df6b58359e210cffa1b0a852a3b5-6021401r")
    clean.setdefault("x-o-mart", "B2C")
    clean.setdefault("x-o-bu", "WALMART-US")
    clean.setdefault("x-o-segment", "oaoh")
    clean.setdefault("tenant-id", "elh9ie")
    return clean


def find_seed(seed_path: Path, contains: str) -> Optional[Dict[str, Any]]:
    for seed in seed_entries(seed_path):
        if contains in str(seed.get("url") or ""):
            return seed
    return None


def ssl_context() -> Optional[ssl.SSLContext]:
    if os.environ.get("WALMART_SSL_NO_VERIFY") == "1":
        return ssl._create_unverified_context()
    return None


def request_json(
    label: str,
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[Any],
    timeout: int,
) -> Tuple[Dict[str, Any], Optional[Any], str]:
    data = None
    req_headers = dict(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    started = time.time()
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    raw = b""
    status = None
    reason = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            status = resp.status
            reason = getattr(resp, "reason", "")
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        reason = exc.reason
        raw = exc.read()
    except Exception as exc:
        return (
            {
                "label": label,
                "method": method,
                "url": url,
                "status": None,
                "reason": type(exc).__name__,
                "elapsed_ms": int((time.time() - started) * 1000),
                "error": str(exc),
            },
            None,
            "",
        )
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
    row = {
        "label": label,
        "method": method,
        "url": url,
        "status": status,
        "reason": reason,
        "elapsed_ms": int((time.time() - started) * 1000),
        "bytes": len(raw),
        "decoded_bytes": len(decoded),
        "json": parsed is not None,
        "top_keys": " | ".join(parsed.keys()) if isinstance(parsed, dict) else "",
        "data_keys": " | ".join((parsed.get("data") or {}).keys()) if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else "",
        "error_preview": preview_errors(parsed) if parsed is not None else text[:300],
    }
    return row, parsed, text


def preview_errors(parsed: Any) -> str:
    if not isinstance(parsed, dict):
        return ""
    errors = parsed.get("errors")
    if not errors:
        return ""
    return re.sub(r"\s+", " ", json.dumps(errors, ensure_ascii=False))[:500]


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: List[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def review_vars(item_id: str, page: int, limit: int) -> Dict[str, Any]:
    return {
        "itemId": item_id,
        "page": page,
        "sort": "relevancy",
        "filter": "",
        "filters": [],
        "limit": limit,
        "lookup": None,
        "aspect": None,
        "conditionGroupCode": None,
        "hasSortFilterParams": False,
        "filterCriteria": None,
        "revampedSelectedRatings": [],
        "enableReviewsAddToCart": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default=r"log\walmart_api_seeds_fullheaders.json")
    parser.add_argument("--out-dir", default=r"log\walmart_persisted_query_probe")
    parser.add_argument("--detail-item", default="18051805520")
    parser.add_argument("--review-item", default="17942205635")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    seed_path = Path(args.seed)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    btf_seed = find_seed(seed_path, ITEM_BTF_NAME) or {}
    search_seed = find_seed(seed_path, "orchestra/home/graphql") or btf_seed
    btf_vars = ((btf_seed.get("body") or {}).get("variables") or {}) if isinstance(btf_seed.get("body"), dict) else {}

    detail_referer = f"https://www.walmart.com/ip/Roku-Select-Series-32-FHD-TV/{args.detail_item}"
    review_referer = f"https://www.walmart.com/reviews/product/{args.review_item}"
    detail_headers = normalize_headers(btf_seed.get("headers") or {}, detail_referer, DYNAMIC_ITEM_NAME)
    review_headers = normalize_headers(search_seed.get("headers") or {}, review_referer, REVIEW_NAME)

    candidates = []
    for page in (1, 2):
        variables = review_vars(args.review_item, page, 10)
        qs = urllib.parse.urlencode({"variables": json.dumps(variables, ensure_ascii=False, separators=(",", ":"))})
        persisted_url = f"https://www.walmart.com/orchestra/home/graphql/{REVIEW_NAME}/{REVIEW_HASH}?{qs}"
        candidates.append((f"reviews_get_p{page}", "GET", persisted_url, review_headers, None))
        candidates.append((f"reviews_post_p{page}", "POST", f"https://www.walmart.com/orchestra/home/graphql/{REVIEW_NAME}/{REVIEW_HASH}", review_headers, {"variables": variables}))
        candidates.append(
            (
                f"reviews_post_extensions_p{page}",
                "POST",
                "https://www.walmart.com/orchestra/home/graphql",
                review_headers,
                {
                    "operationName": REVIEW_NAME,
                    "variables": variables,
                    "extensions": {"persistedQuery": {"version": 1, "sha256Hash": REVIEW_HASH}},
                },
            )
        )

    dynamic_vars = {"iId": args.detail_item, "bbe": True, "fSId": True}
    dynamic_url = f"https://www.walmart.com/orchestra/pdp/graphql/{DYNAMIC_ITEM_NAME}/{DYNAMIC_ITEM_HASH}/ip/{args.detail_item}"
    candidates.append(("dynamic_item_post_min", "POST", dynamic_url, detail_headers, {"variables": dynamic_vars}))
    candidates.append(("dynamic_item_post_body_flat", "POST", dynamic_url, detail_headers, dynamic_vars))
    candidates.append(("dynamic_item_get_min", "GET", dynamic_url + "?" + urllib.parse.urlencode({"variables": json.dumps(dynamic_vars, separators=(",", ":"))}), detail_headers, None))

    if btf_vars:
        btf_url = f"https://www.walmart.com/orchestra/pdp/graphql/{ITEM_BTF_NAME}/{ITEM_BTF_HASH}/ip/{args.detail_item}"
        btf_headers = normalize_headers(btf_seed.get("headers") or {}, detail_referer, ITEM_BTF_NAME)
        candidates.append(("control_btf_seed_body", "POST", btf_url, btf_headers, {"variables": btf_vars}))

    rows: List[Dict[str, Any]] = []
    for label, method, url, headers, body in candidates:
        row, parsed, text = request_json(label, method, url, headers, body, args.timeout)
        rows.append(row)
        response_path = out_dir / f"{label}.json"
        if parsed is not None:
            dump_json(response_path, parsed)
        else:
            response_path.with_suffix(".txt").write_text(text, encoding="utf-8", errors="replace")

    write_csv(out_dir / "summary.csv", rows)
    dump_json(out_dir / "summary.json", rows)
    ok_rows = [{"label": r.get("label"), "status": r.get("status"), "data_keys": r.get("data_keys")} for r in rows if r.get("status") == 200]
    print(json.dumps({"out_dir": str(out_dir), "attempts": len(rows), "ok": ok_rows}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
