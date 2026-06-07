"""Probe Walmart raw HTML access without browser rendering or paid APIs."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_PROJECT_ROOT = Path(
    r"C:\Users\gomguard\Documents\퀵오일\삼성전자\samsung_dx_retail_com\samsung_dx_retail_com"
)
NEXT_RE = re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)
ROBOT_TOKENS = ("robot or human", "press & hold", "press and hold", "verify you are human", "waitingroom")


def item_from_url(url: str) -> str:
    match = re.search(r"/(?:ip|reviews/product)/(?:[^/?#]+/)?(\d+)", url)
    return match.group(1) if match else ""


def read_seed(path: Path, limit: int) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[:limit] if limit > 0 else rows


def fetch(url: str, timeout: int = 30) -> Dict[str, Any]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    started = time.perf_counter()
    raw = b""
    status: Optional[int] = None
    final_url = url
    reason = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            final_url = resp.url
            reason = getattr(resp, "reason", "")
            raw = resp.read()
            encoding = resp.headers.get("content-encoding", "")
    except urllib.error.HTTPError as exc:
        status = exc.code
        final_url = exc.url or url
        reason = exc.reason
        raw = exc.read()
        encoding = exc.headers.get("content-encoding", "") if exc.headers else ""
    except Exception as exc:
        return {
            "status": "",
            "reason": type(exc).__name__,
            "final_url": final_url,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "html": "",
            "error": str(exc),
        }
    body = raw
    if encoding == "gzip" or raw.startswith(b"\x1f\x8b"):
        try:
            body = gzip.decompress(raw)
        except Exception:
            body = raw
    return {
        "status": status,
        "reason": reason,
        "final_url": final_url,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "raw_bytes": len(raw),
        "decoded_bytes": len(body),
        "html": body.decode("utf-8", errors="replace"),
        "error": "",
    }


def summarize_html(url: str, html: str) -> Dict[str, Any]:
    lower = html.lower()
    match = NEXT_RE.search(html)
    out = {
        "url": url,
        "html_length": len(html),
        "has_next_data": bool(match),
        "robot_detected": any(token in lower for token in ROBOT_TOKENS),
        "has_reviews_text": "customer ratings" in lower or "reviews" in lower,
        "has_search_results_text": "search results" in lower or "results for" in lower,
        "next_data_top_keys": "",
        "next_data_has_initial_data": "",
        "next_data_item": "",
        "next_data_name": "",
    }
    if match:
        try:
            data = json.loads(match.group(1))
            out["next_data_top_keys"] = ",".join(data.keys()) if isinstance(data, dict) else ""
            initial = (
                data.get("props", {})
                .get("pageProps", {})
                .get("initialData", {})
                .get("data", {})
            )
            out["next_data_has_initial_data"] = bool(initial)
            product = initial.get("product") if isinstance(initial, dict) else {}
            if isinstance(product, dict):
                out["next_data_item"] = product.get("usItemId") or product.get("primaryProductId") or ""
                out["next_data_name"] = product.get("name") or ""
        except Exception as exc:
            out["next_data_error"] = str(exc)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Raw HTTP Walmart probe")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--seed", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    out_dir = args.out_dir or project_root / "log" / "walmart_raw_http_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = args.seed or project_root / "log" / "walmart_listing_300_probe" / "all_unique_items.csv"

    urls: List[str] = list(args.url)
    if not urls:
        rows = read_seed(seed, args.limit)
        for row in rows:
            product_url = row.get("product_url") or ""
            item = row.get("item") or item_from_url(product_url)
            if product_url:
                urls.append(product_url)
            if item:
                urls.append(f"https://www.walmart.com/reviews/product/{item}")
        urls.append("https://www.walmart.com/search?q=TV&sort=best_seller&affinityOverride=default")

    rows_out: List[Dict[str, Any]] = []
    for idx, url in enumerate(urls, 1):
        print(f"[{idx}/{len(urls)}] GET {url}")
        result = fetch(url, timeout=args.timeout)
        html = result.pop("html", "")
        summary = summarize_html(url, html)
        item = item_from_url(url) or f"url_{idx:02d}"
        safe_kind = "review" if "/reviews/product/" in url else "search" if "/search" in url else "detail"
        raw_path = out_dir / "raw" / f"{safe_kind}_{item}.html"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(html, encoding="utf-8", errors="replace")
        rows_out.append({**result, **summary, "raw_path": str(raw_path)})

    fieldnames: List[str] = []
    for row in rows_out:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with (out_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)
    (out_dir / "summary.json").write_text(
        json.dumps({"created_at": datetime.now().isoformat(timespec="seconds"), "rows": rows_out}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(rows_out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
