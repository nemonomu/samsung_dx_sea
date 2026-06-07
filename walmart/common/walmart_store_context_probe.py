from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1] / "samsung_dx_retail_com"
sys.path.insert(0, str(ROOT))

from walmart_detail_review_batch_probe import DEFAULT_SEED, make_page  # noqa: E402


def first_product_url(seed: Path) -> str:
    with seed.open("r", encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))
    return row["product_url"]


def browser_fetch(page: Any, url: str) -> dict[str, Any]:
    script = r"""
    const url = arguments[0];
    const operation = arguments[1];
    const xhr = new XMLHttpRequest();
    xhr.open('GET', url, false);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Accept', 'application/json,text/plain,*/*');
    xhr.setRequestHeader('Cache-Control', 'no-cache');
    xhr.setRequestHeader('Pragma', 'no-cache');
    if (operation) {
      const rand = Math.random().toString(36).slice(2) + Date.now().toString(36);
      xhr.setRequestHeader('x-o-gql-query', 'query ' + operation);
      xhr.setRequestHeader('X-APOLLO-OPERATION-NAME', operation);
      xhr.setRequestHeader('x-o-platform', 'rweb');
      xhr.setRequestHeader('x-o-platform-version', 'usweb-1.268.0-966f60c9de57fae982545ada5ada32cd89c042ed-6041711r');
      xhr.setRequestHeader('x-o-mart', 'B2C');
      xhr.setRequestHeader('x-o-bu', 'WALMART-US');
      xhr.setRequestHeader('x-o-segment', 'oaoh');
      xhr.setRequestHeader('x-o-correlation-id', rand);
      xhr.setRequestHeader('wm_qos.correlation_id', rand);
      xhr.setRequestHeader('WM_PAGE_URL', 'https://www.walmart.com/store-finder?location=11581');
      xhr.setRequestHeader('tenant-id', 'elh9ie');
    }
    xhr.send(null);
    let parsed = null;
    try { parsed = JSON.parse(xhr.responseText || 'null'); } catch (e) {}
    return {
      status: xhr.status,
      statusText: xhr.statusText,
      finalUrl: xhr.responseURL || url,
      text: (xhr.responseText || '').slice(0, 2000),
      json: parsed
    };
    """
    operation = "storeFinderNearbyNodesQuery" if "storeFinderNearbyNodesQuery" in url else ""
    return page.run_js(script, url, operation, timeout=120) or {}


def walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from walk(child, child_path)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            yield from walk(child, f"{path}[{idx}]")


def looks_like_store(obj: dict[str, Any], zip_code: str) -> bool:
    text = json.dumps(obj, ensure_ascii=False)
    has_id = any(key.lower() in {"id", "storeid", "store_id", "store"} for key in obj)
    has_store_word = any("store" in key.lower() for key in obj)
    has_zip_or_ny = zip_code in text or "NY" in text or "New York" in text or "Valley Stream" in text
    return (has_id or has_store_word) and has_zip_or_ny


def summarize_json(parsed: Any, zip_code: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "top_type": type(parsed).__name__,
        "top_keys": " | ".join(parsed.keys()) if isinstance(parsed, dict) else "",
        "store_candidates": [],
    }
    candidates = []
    for path, obj in walk(parsed):
        if looks_like_store(obj, zip_code):
            slim = {}
            for key, value in obj.items():
                if key.lower() in {
                    "id",
                    "storeid",
                    "store_id",
                    "store",
                    "displayname",
                    "name",
                    "city",
                    "state",
                    "stateorprovincecode",
                    "postalcode",
                    "zip",
                    "address",
                    "address1",
                    "addresslineone",
                }:
                    slim[key] = value
            if slim:
                candidates.append({"path": path, "fields": slim})
        if len(candidates) >= 20:
            break
    out["store_candidates"] = candidates
    return out


def candidate_urls(zip_code: str) -> list[str]:
    encoded = urllib.parse.quote(zip_code)
    city = urllib.parse.quote(f"Valley Stream, NY {zip_code}")
    store_finder_vars = {
        "input": {
            "postalCode": zip_code,
            "nodeTypes": ["STORE"],
            "accessTypes": [
                "PICKUP_INSTORE",
                "PICKUP_CURBSIDE",
                "DELIVERY_ADDRESS",
                "DELIVERY_IN_HOME",
                "DELIVERY_SPECIAL_EVENT",
                "PICKUP_SPOKE",
                "PICKUP_BAKERY",
                "ACC",
            ],
            "radius": 50,
        }
    }
    store_finder_qs = urllib.parse.urlencode(
        {"variables": json.dumps(store_finder_vars, ensure_ascii=False, separators=(",", ":"))}
    )
    return [
        "https://www.walmart.com/orchestra/home/graphql/storeFinderNearbyNodesQuery/"
        f"23594c3a307d6359f419a4247d6212be409ec7b74fb8963f5402147bed22e7fb?{store_finder_qs}",
        f"https://www.walmart.com/store/finder/electrode/api/stores?singleLineAddr={encoded}",
        f"https://www.walmart.com/store/finder/electrode/api/stores?singleLineAddr={encoded}&distance=50",
        f"https://www.walmart.com/store/finder/electrode/api/stores?singleLineAddr={encoded}&serviceTypes=pickup,delivery",
        f"https://www.walmart.com/store/finder/electrode/api/stores?singleLineAddr={city}",
        f"https://www.walmart.com/store/finder/electrode/api/stores?singleLineAddr={city}&distance=50",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Walmart store/location context for a zip code")
    parser.add_argument("--zip-code", default="11581")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--product-url", default="")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "log" / "walmart_store_context_probe")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--first-only", action="store_true")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    product_url = args.product_url or first_product_url(args.seed)

    page = make_page(headless=args.headless)
    rows = []
    responses: dict[str, Any] = {}
    try:
        page.get(product_url)
        time.sleep(5)
        urls = candidate_urls(args.zip_code)
        if args.first_only:
            urls = urls[:1]
        for index, url in enumerate(urls, 1):
            print(f"[store-context] GET candidate {index}: {url}")
            result = browser_fetch(page, url)
            parsed = result.get("json")
            summary = summarize_json(parsed, args.zip_code) if parsed is not None else {}
            key = f"candidate_{index:02d}"
            responses[key] = {
                "url": url,
                "result": result,
                "summary": summary,
            }
            rows.append(
                {
                    "candidate": key,
                    "status": result.get("status"),
                    "finalUrl": result.get("finalUrl"),
                    "json": parsed is not None,
                    "top_keys": summary.get("top_keys", ""),
                    "store_candidate_count": len(summary.get("store_candidates") or []),
                    "first_store_candidate": json.dumps(
                        (summary.get("store_candidates") or [{}])[0],
                        ensure_ascii=False,
                    )[:500],
                    "text_preview": re.sub(r"\s+", " ", str(result.get("text") or ""))[:500],
                }
            )
    finally:
        try:
            page.quit()
        except Exception:
            pass

    (args.out_dir / "responses.json").write_text(
        json.dumps(responses, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    with (args.out_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["candidate"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"[store-context] wrote {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
