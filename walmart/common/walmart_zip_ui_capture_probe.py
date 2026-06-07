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
from walmart_detail_review_batch_probe import DEFAULT_SEED, item_from_url, make_page  # noqa: E402


CAPTURE_PATTERNS = (
    "graphql",
    "orchestra",
    "account",
    "cegateway",
    "location",
    "postal",
    "address",
    "cartxo",
)


def first_product_url(seed: Path) -> str:
    with seed.open("r", encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))
    return row["product_url"]


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
        "is_graphql": "graphql" in str(url).lower() or bool(operations),
    }


def visible_location_text(page: Any) -> dict[str, Any]:
    script = r"""
    const visible = (el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' &&
        rect.width > 0 && rect.height > 0;
    };
    const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const texts = [...document.querySelectorAll('a,button,span,strong,b,div')]
      .filter(visible)
      .map(el => normalize(el.innerText || el.textContent || ''))
      .filter(text => /Ships to|New York|Sacramento|Valley Stream|11581|10118|95829|Zip code|Update your location/i.test(text))
      .slice(0, 80);
    return {url: location.href, title: document.title, texts};
    """
    try:
        return page.run_js(script, timeout=10) or {}
    except Exception as exc:
        return {"error": str(exc)}


def click_location_js(page: Any) -> bool:
    script = r"""
    const visible = (el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' &&
        rect.width > 0 && rect.height > 0;
    };
    const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const candidates = [];
    for (const el of document.querySelectorAll('a,button,span,strong,b,div')) {
      if (!visible(el)) continue;
      const text = normalize(el.innerText || el.textContent || '');
      if (!/^(Sacramento,\s*95829|New York,\s*10118|Valley Stream,\s*11581)$/.test(text)) continue;
      const rect = el.getBoundingClientRect();
      candidates.push({el, top: rect.top, left: rect.left});
    }
    candidates.sort((a, b) => b.top - a.top || b.left - a.left);
    const target = candidates[0] && candidates[0].el;
    if (!target) return false;
    const clickable = target.closest('a,button,[role="button"]') || target;
    clickable.scrollIntoView({block: 'center', inline: 'center'});
    clickable.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
    clickable.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
    clickable.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
    clickable.click();
    return true;
    """
    try:
        return bool(page.run_js(script, timeout=10))
    except Exception:
        return False


def submit_zip_js(page: Any, zip_code: str) -> bool:
    script = r"""
    const zip = arguments[0];
    const visible = (el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' &&
        rect.width > 0 && rect.height > 0;
    };
    const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const scopes = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], aside, section, div')]
      .filter(el => visible(el) && /Update your location|Zip code|Enter zip code/i.test(normalize(el.innerText || el.textContent || '')));
    const scope = scopes[0] || document;
    const inputs = [...scope.querySelectorAll('input')]
      .filter(el => visible(el) && !['search', 'checkbox', 'radio', 'hidden'].includes((el.type || '').toLowerCase()));
    const input = inputs[0];
    if (!input) return false;
    input.focus();
    input.value = '';
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.value = zip;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
    input.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
    input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
    return true;
    """
    try:
        return bool(page.run_js(script, zip_code, timeout=10))
    except Exception:
        return False


def submit_zip(page: Any, zip_code: str) -> dict[str, Any]:
    opened = click_location_js(page)
    time.sleep(2)
    submitted = submit_zip_js(page, zip_code)
    return {"clicked_location": opened, "submitted": submitted}


def collect_packets(page: Any, seconds: int) -> list[dict[str, Any]]:
    captures: list[dict[str, Any]] = []
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
            json.dumps(capture.get("request_body"), ensure_ascii=False, default=str)[:800],
        )
        if capture.get("url") and key not in seen:
            seen.add(key)
            captures.append(capture)
    return captures


def summarize_capture(capture: dict[str, Any]) -> dict[str, Any]:
    body = capture.get("request_body")
    resp = capture.get("response_body")
    body_preview = re.sub(r"\s+", " ", json.dumps(body, ensure_ascii=False, default=str))[:500] if body is not None else ""
    resp_preview = re.sub(r"\s+", " ", json.dumps(resp, ensure_ascii=False, default=str))[:500] if resp is not None else ""
    return {
        "status": capture.get("status"),
        "method": capture.get("method"),
        "url": capture.get("url"),
        "operation_names": " | ".join(capture.get("operation_names") or []),
        "variable_keys": " | ".join(capture.get("variable_keys") or []),
        "data_keys": " | ".join(capture.get("data_keys") or []),
        "request_preview": body_preview,
        "response_preview": resp_preview,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Walmart zip dialog network calls")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--product-url", default="")
    parser.add_argument("--zip-code", default="11581")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "log" / "walmart_zip_ui_capture_probe")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--manual-wait", type=int, default=35)
    parser.add_argument("--capture-seconds", type=int, default=35)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    product_url = args.product_url or first_product_url(args.seed)
    item = item_from_url(product_url)

    page = make_page(headless=args.headless)
    try:
        page.get(product_url)
        time.sleep(6)
        before = visible_location_text(page)
        (args.out_dir / "before_state.json").write_text(
            json.dumps(before, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        try:
            page.listen.start(CAPTURE_PATTERNS)
        except Exception:
            page.listen.start("graphql")

        action = submit_zip(page, args.zip_code)
        (args.out_dir / "action_state.json").write_text(
            json.dumps({"action": action, "state": visible_location_text(page)}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(5)
        captures = collect_packets(page, args.capture_seconds)

        if not captures and args.manual_wait > 0:
            print(
                f"[manual] Open the detail-page ship-to zip dialog, enter {args.zip_code}, press Enter. "
                f"Capturing for {args.manual_wait}s..."
            )
            captures = collect_packets(page, args.manual_wait)

        after = visible_location_text(page)
        try:
            page.listen.stop()
        except Exception:
            pass

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

        result = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "product_url": product_url,
            "item": item,
            "zip_code": args.zip_code,
            "action": action,
            "before": before,
            "after": after,
            "request_count": len(captures),
            "requests": captures,
        }
        (args.out_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(json.dumps({**result, "requests": summary_rows}, ensure_ascii=False, indent=2, default=str))
    finally:
        try:
            page.quit()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
