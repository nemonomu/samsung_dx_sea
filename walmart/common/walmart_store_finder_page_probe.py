from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1] / "samsung_dx_retail_com"
sys.path.insert(0, str(ROOT))

from walmart_detail_review_batch_probe import make_page  # noqa: E402
from walmart_zip_ui_capture_probe import packet_to_dict, summarize_capture  # noqa: E402


CAPTURE_PATTERNS = (
    "store",
    "finder",
    "graphql",
    "location",
    "postal",
)


def visible_texts(page: Any) -> dict[str, Any]:
    script = r"""
    const visible = (el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' &&
        rect.width > 0 && rect.height > 0;
    };
    const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const texts = [...document.querySelectorAll('h1,h2,h3,a,button,span,strong,b,div,p')]
      .filter(visible)
      .map(el => normalize(el.innerText || el.textContent || ''))
      .filter(text => /11581|Valley Stream|NY|store|Supercenter|Green Acres|pickup|delivery/i.test(text))
      .slice(0, 160);
    return {url: location.href, title: document.title, texts};
    """
    try:
        return page.run_js(script, timeout=20) or {}
    except Exception as exc:
        return {"error": str(exc)}


def next_data(page: Any) -> Any:
    script = r"""
    const el = document.querySelector('script#__NEXT_DATA__');
    if (!el) return null;
    try { return JSON.parse(el.textContent || 'null'); } catch (e) { return {parseError: String(e), raw: (el.textContent || '').slice(0, 1000)}; }
    """
    try:
        return page.run_js(script, timeout=20)
    except Exception as exc:
        return {"error": str(exc)}


def walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from walk(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            yield from walk(child, f"{path}[{idx}]")


def store_like_objects(value: Any, zip_code: str) -> list[dict[str, Any]]:
    rows = []
    for path, obj in walk(value):
        text = json.dumps(obj, ensure_ascii=False, default=str)
        key_text = " ".join(obj.keys()).lower()
        if not ("store" in key_text or "storeId" in text or "displayName" in text):
            continue
        if not (zip_code in text or "Valley Stream" in text or "Green Acres" in text or "NY" in text):
            continue
        slim = {}
        for key, child in obj.items():
            if re.search(r"store|id|name|address|city|state|postal|zip|display|distance", str(key), re.I):
                slim[key] = child
        rows.append({"path": path, "fields": slim})
        if len(rows) >= 40:
            break
    return rows


def collect_packets(page: Any, seconds: int) -> list[dict[str, Any]]:
    captures = []
    seen: set[tuple[str, str, str]] = set()
    deadline = time.time() + seconds
    while time.time() < deadline:
        packet = page.listen.wait(timeout=1.5)
        if not packet:
            continue
        try:
            capture = packet_to_dict(packet)
        except Exception as exc:
            captures.append({"parse_error": str(exc)})
            continue
        key = (
            str(capture.get("method") or ""),
            str(capture.get("url") or ""),
            json.dumps(capture.get("request_body"), ensure_ascii=False, default=str)[:500],
        )
        if capture.get("url") and key not in seen:
            seen.add(key)
            captures.append(capture)
    return captures


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Walmart store-finder page data for a zip code")
    parser.add_argument("--zip-code", default="11581")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "log" / "walmart_store_finder_page_probe")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--capture-seconds", type=int, default=25)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://www.walmart.com/store-finder?singleLineAddr={args.zip_code}"

    page = make_page(headless=args.headless)
    captures: list[dict[str, Any]] = []
    try:
        try:
            page.listen.start(CAPTURE_PATTERNS)
        except Exception:
            page.listen.start("graphql")
        print(f"[store-finder] GET {url}", flush=True)
        page.get(url)
        time.sleep(8)
        for _ in range(3):
            try:
                page.run_js("window.scrollBy(0, Math.floor(window.innerHeight * 0.8));", timeout=5)
            except Exception:
                pass
            time.sleep(2)
        captures = collect_packets(page, args.capture_seconds)
        state = visible_texts(page)
        data = next_data(page)
    finally:
        try:
            page.listen.stop()
        except Exception:
            pass

    candidates = store_like_objects(data, args.zip_code)
    packet_candidates = []
    for capture in captures:
        for payload_key in ("request_body", "response_body"):
            payload = capture.get(payload_key)
            packet_candidates.extend(
                {
                    "url": capture.get("url"),
                    "payload": payload_key,
                    **row,
                }
                for row in store_like_objects(payload, args.zip_code)
            )
            if len(packet_candidates) >= 80:
                break
        if len(packet_candidates) >= 80:
            break

    (args.out_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (args.out_dir / "next_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (args.out_dir / "captures.json").write_text(json.dumps(captures, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (args.out_dir / "store_candidates.json").write_text(
        json.dumps({"next_data": candidates, "packets": packet_candidates}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    summary_rows = [summarize_capture(capture) for capture in captures]
    with (args.out_dir / "requests.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        fields = list(summary_rows[0].keys()) if summary_rows else [
            "status",
            "method",
            "url",
            "operation_names",
            "variable_keys",
            "data_keys",
            "request_preview",
            "response_preview",
        ]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"[store-finder] visible_texts={len(state.get('texts') or [])}", flush=True)
    print(f"[store-finder] next_candidates={len(candidates)} packet_candidates={len(packet_candidates)}", flush=True)
    print(f"[store-finder] wrote {args.out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
