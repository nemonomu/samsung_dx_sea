from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def cookie_header_from_cookies(cookies: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "")
        if name and (not domain or "walmart.com" in domain):
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Walmart browser session JSON for raw HTTP collection")
    parser.add_argument("--project-root", type=Path, default=Path(r"C:\samsung_dx_sea"))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--url", default="https://www.walmart.com/search?q=TV")
    parser.add_argument("--wait", type=float, default=15.0)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--user-data-dir", type=Path, default=None)
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise SystemExit(
            "Playwright is required for session bootstrap. Install on the RDP once with: "
            "pip install playwright && python -m playwright install chromium. "
            f"Import error: {type(exc).__name__}: {exc}"
        )

    project_root = args.project_root.resolve()
    out = (args.out or project_root / "log" / "walmart_browser_session.json").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    user_data_dir = (args.user_data_dir or project_root / "log" / "walmart_browser_profile").resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=args.headless,
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1440, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(int(args.wait * 1000))
        final_url = page.url
        title = page.title()
        user_agent = page.evaluate("() => navigator.userAgent")
        cookies = context.cookies("https://www.walmart.com")
        storage_state = context.storage_state()
        blocked = "/blocked" in final_url.lower() or "robot" in title.lower() or "verify" in title.lower()
        session = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_url": args.url,
            "final_url": final_url,
            "title": title,
            "blocked_detected": blocked,
            "user_agent": user_agent,
            "headers": {
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            },
            "cookies": cookies,
            "cookie_header": cookie_header_from_cookies(cookies),
            "storage_state": storage_state,
        }
        out.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "out": str(out),
            "final_url": final_url,
            "title": title,
            "blocked_detected": blocked,
            "cookie_count": len(cookies),
            "cookie_header_length": len(session["cookie_header"]),
        }, ensure_ascii=False, indent=2))
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
