from __future__ import annotations

import argparse
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

from probe import decode_body, extract_data_keys, extract_ops_and_vars, normalize_headers, safe_getattr  # noqa: E402
from walmart_detail_review_batch_probe import item_from_url, make_page  # noqa: E402


DEFAULT_URL = "https://www.walmart.com/ip/LG-65-4K-UHD-UA75-AI-Smart-TV-65UA7500/14365163951"
CAPTURE_PATTERNS = (
    "graphql",
    "orchestra",
    "cegateway",
    "p13n",
    "recomm",
    "carousel",
    "similar",
    "athena",
)
NAME_NEEDLES = (
    "Hisense 65",
    "Hisense 58",
    "onn 32",
    "TCL 65",
    "LG 75",
    "Samsung 43",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def packet_to_dict(packet: Any) -> dict[str, Any]:
    request = safe_getattr(packet, ["request"], None)
    response = safe_getattr(packet, ["response"], None)
    url = safe_getattr(packet, ["url"], "") or safe_getattr(request, ["url"], "")
    method = safe_getattr(request, ["method"], "")
    status = safe_getattr(response, ["status", "status_code"], None)
    req_headers = normalize_headers(safe_getattr(request, ["headers"], {}))
    resp_headers = normalize_headers(safe_getattr(response, ["headers"], {}))
    req_body = decode_body(safe_getattr(request, ["body", "postData", "data"], None), req_headers)
    resp_body = decode_body(safe_getattr(response, ["body", "raw_body", "data"], None), resp_headers)
    operations, variable_keys = extract_ops_and_vars(req_body)
    data_keys = extract_data_keys(resp_body)
    body_text = json.dumps(resp_body, ensure_ascii=False, default=str) if resp_body is not None else ""
    return {
        "url": str(url or ""),
        "method": str(method or ""),
        "status": int(status) if str(status).isdigit() else status,
        "request_headers": req_headers,
        "response_headers": resp_headers,
        "request_body": req_body,
        "response_body": resp_body,
        "operation_names": operations,
        "variable_keys": variable_keys,
        "data_keys": data_keys,
        "matched_name_needles": [needle for needle in NAME_NEEDLES if needle.lower() in body_text.lower()],
        "response_size": len(body_text),
    }


def summarize_capture(capture: dict[str, Any]) -> dict[str, Any]:
    body = capture.get("request_body")
    resp = capture.get("response_body")
    body_preview = re.sub(r"\s+", " ", json.dumps(body, ensure_ascii=False, default=str))[:700] if body is not None else ""
    resp_preview = re.sub(r"\s+", " ", json.dumps(resp, ensure_ascii=False, default=str))[:700] if resp is not None else ""
    return {
        "status": capture.get("status"),
        "method": capture.get("method"),
        "url": capture.get("url"),
        "operation_names": " | ".join(capture.get("operation_names") or []),
        "variable_keys": " | ".join(capture.get("variable_keys") or []),
        "data_keys": " | ".join(capture.get("data_keys") or []),
        "matched_name_needles": " | ".join(capture.get("matched_name_needles") or []),
        "response_size": capture.get("response_size"),
        "request_preview": body_preview,
        "response_preview": resp_preview,
    }


def drain_packets(page: Any, seconds: float) -> list[dict[str, Any]]:
    captures: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    deadline = time.time() + seconds
    while time.time() < deadline:
        packet = page.listen.wait(timeout=1)
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
            json.dumps(capture.get("request_body"), ensure_ascii=False, default=str)[:900],
        )
        if capture.get("url") and key not in seen:
            seen.add(key)
            captures.append(capture)
    return captures


def page_state(page: Any) -> dict[str, Any]:
    script = r"""
    const n = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const text = n(document.body.innerText || '');
    return {
      url: location.href,
      title: document.title,
      hasRobot: /robot or human|press & hold|verify you are human/i.test(text),
      hasSimilarHeading: /Similar items you might like/i.test(text),
      hasPopularHeading: /Popular items in this category/i.test(text),
      textPreview: text.slice(0, 1000)
    };
    """
    try:
        return page.run_js(script, timeout=10) or {}
    except Exception as exc:
        return {"error": str(exc)}


def scroll_to_similar(page: Any, rounds: int, pause: float) -> dict[str, Any]:
    states: list[dict[str, Any]] = []
    script = r"""
    const n = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const visible = (el) => {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const headings = [...document.querySelectorAll('h1,h2,h3,h4,div,span')]
      .filter(visible)
      .filter(el => /^Similar items you might like$/i.test(n(el.innerText || el.textContent || '')));
    if (headings[0]) {
      headings[0].scrollIntoView({block: 'center'});
      return {found: true, headingText: n(headings[0].innerText || headings[0].textContent || ''), y: window.scrollY};
    }
    window.scrollBy(0, Math.max(600, Math.floor(window.innerHeight * 0.85)));
    return {found: false, y: window.scrollY, height: document.body.scrollHeight};
    """
    for _ in range(rounds):
        try:
            state = page.run_js(script, timeout=10) or {}
        except Exception as exc:
            state = {"error": str(exc)}
        states.append(state)
        time.sleep(pause)
        if state.get("found"):
            break
    return {"states": states, "final": page_state(page)}


def extract_similar_names(page: Any) -> dict[str, Any]:
    script = r"""
    const n = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const visible = (el) => {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const bad = /^(Add|Options|Save with|Delivery available|Shipping,|Free shipping|Pickup as soon as|Rollback|Sponsored|In \d+|Now \$|You save|\$|Based on|Similar items you might like|Popular items in this category|Free 30-day returns|Price when purchased online)$/i;
    const headings = [...document.querySelectorAll('h1,h2,h3,h4,div,span')]
      .filter(visible)
      .filter(el => /^Similar items you might like$/i.test(n(el.innerText || el.textContent || '')));
    const heading = headings[0];
    if (!heading) return {found: false, names: [], candidates: []};
    let root = heading.parentElement;
    for (let i = 0; root && i < 8; i++) {
      const text = n(root.innerText || root.textContent || '');
      const imgCount = root.querySelectorAll('img').length;
      const addCount = [...root.querySelectorAll('button')].filter(b => /^(\+?\s*Add|Options)$/i.test(n(b.innerText || b.textContent || ''))).length;
      if (imgCount >= 4 && addCount >= 3 && !/Popular items in this category/i.test(text)) break;
      root = root.parentElement;
    }
    root = root || heading.parentElement;
    const raw = [...root.querySelectorAll('a,span,div')]
      .filter(visible)
      .map(el => n(el.innerText || el.textContent || ''))
      .filter(text => text.length >= 15 && text.length <= 260)
      .filter(text => !bad.test(text))
      .filter(text => !/^\d+(\.\d+)?$/.test(text))
      .filter(text => !/^\d+\s+ratings?$/i.test(text))
      .filter(text => !/^\d+\s+reviews?$/i.test(text));
    const names = [];
    for (const text of raw) {
      if (/(\bClass\b|Smart TV|Television|UHD|QLED|OLED|Roku|VIZIO|Samsung|Hisense|TCL|LG|onn)/i.test(text) && !names.includes(text)) {
        names.push(text);
      }
    }
    return {found: true, names: names.slice(0, 30), candidates: raw.slice(0, 80), rootTextPreview: n(root.innerText || root.textContent || '').slice(0, 2500)};
    """
    try:
        return page.run_js(script, timeout=10) or {}
    except Exception as exc:
        return {"error": str(exc), "names": []}


def write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "status",
        "method",
        "url",
        "operation_names",
        "variable_keys",
        "data_keys",
        "matched_name_needles",
        "response_size",
        "request_preview",
        "response_preview",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Walmart PDP network calls around the Similar carousel.")
    parser.add_argument("--product-url", default=DEFAULT_URL)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "log" / "walmart_similar_graphql_capture")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--initial-wait", type=float, default=8)
    parser.add_argument("--initial-capture-seconds", type=float, default=8)
    parser.add_argument("--post-scroll-capture-seconds", type=float, default=25)
    parser.add_argument("--scroll-rounds", type=int, default=14)
    parser.add_argument("--pause", type=float, default=1.2)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    item = item_from_url(args.product_url)
    page = make_page(headless=args.headless)
    started = time.perf_counter()
    captures: list[dict[str, Any]] = []
    before_scroll: dict[str, Any] = {}
    scroll_state: dict[str, Any] = {}
    similar: dict[str, Any] = {}
    after: dict[str, Any] = {}
    error = ""
    try:
        print("[stage] start listener", flush=True)
        try:
            page.listen.start(CAPTURE_PATTERNS)
        except Exception:
            page.listen.start("graphql")
        print("[stage] open page", flush=True)
        page.get(args.product_url)
        time.sleep(args.initial_wait)
        print("[stage] drain initial packets", flush=True)
        captures.extend(drain_packets(page, args.initial_capture_seconds))
        before_scroll = page_state(page)
        write_json(args.out_dir / "partial_before_scroll.json", before_scroll)
        print("[stage] scroll to similar", flush=True)
        scroll_state = scroll_to_similar(page, args.scroll_rounds, args.pause)
        write_json(args.out_dir / "partial_scroll_state.json", scroll_state)
        print("[stage] drain post-scroll packets", flush=True)
        captures.extend(drain_packets(page, args.post_scroll_capture_seconds))
        write_json(args.out_dir / "partial_requests.json", captures)
        print("[stage] extract rendered similar names", flush=True)
        similar = extract_similar_names(page)
        after = page_state(page)
        try:
            page.listen.stop()
        except Exception:
            pass
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"[error] {error}", flush=True)
    finally:
        result = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "product_url": args.product_url,
            "item": item,
            "error": error,
            "before_scroll": before_scroll,
            "scroll_state": scroll_state,
            "after": after,
            "similar": similar,
            "request_count": len(captures),
            "requests": captures,
        }
        write_json(args.out_dir / "result.json", result)
        write_summary_csv(args.out_dir / "requests.csv", [summarize_capture(capture) for capture in captures])
        try:
            page.quit()
        except Exception:
            pass

    summary_rows = [summarize_capture(capture) for capture in captures]
    result = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "product_url": args.product_url,
        "item": item,
        "error": error,
        "before_scroll": before_scroll,
        "scroll_state": scroll_state,
        "after": after,
        "similar": similar,
        "request_count": len(captures),
        "matched_request_count": sum(1 for row in summary_rows if row.get("matched_name_needles")),
        "requests": captures,
    }
    write_json(args.out_dir / "result.json", result)
    write_summary_csv(args.out_dir / "requests.csv", summary_rows)
    print(json.dumps({**result, "requests": summary_rows}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
