import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def env_bool(name, default="0"):
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y"}


def env_int(name, default="0"):
    try:
        return int(os.getenv(name, default) or default)
    except (TypeError, ValueError):
        return int(default)


def add_intl_nosplash(url):
    parts = urlsplit(str(url or ""))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("intl", "nosplash")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def stable_browser_port(seed, override=0):
    if override and int(override) > 0:
        return int(override)
    return 19000 + (sum(ord(ch) for ch in str(seed or "")) % 20000)


def create_browser_page(
    *,
    run_root,
    name,
    headless=False,
    local_port=0,
):
    try:
        from DrissionPage import ChromiumOptions, ChromiumPage
    except ImportError as exc:
        raise RuntimeError("DrissionPage is required for BestBuy browser session collection") from exc

    run_root = Path(run_root)
    port = stable_browser_port(f"{name}:{run_root}", local_port)
    profile_dir = run_root / "raw" / f"{name}_profile"
    cache_dir = run_root / "raw" / f"{name}_cache"
    profile_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    options = ChromiumOptions()
    options.set_paths(
        local_port=port,
        user_data_path=str(profile_dir),
        cache_path=str(cache_dir),
    )
    if headless:
        try:
            options.headless(True)
        except TypeError:
            options.headless()

    try:
        page = ChromiumPage(options)
    except Exception as first_exc:
        time.sleep(2)
        reconnect_options = ChromiumOptions()
        reconnect_options.set_address(f"127.0.0.1:{port}")
        try:
            page = ChromiumPage(reconnect_options)
        except Exception as second_exc:
            raise RuntimeError(
                f"Could not open Chrome session on port {port}: "
                f"initial={first_exc!r}; reconnect={second_exc!r}"
            ) from second_exc

    return page, {
        "browser_port": port,
        "browser_profile_dir": str(profile_dir),
        "browser_cache_dir": str(cache_dir),
        "browser_headless": bool(headless),
    }


def close_browser_page(page):
    if not page:
        return
    try:
        page.quit()
    except Exception:
        pass


def browser_fetch_graphql(page, payload, timeout=120):
    js = (
        "return fetch('/gateway/graphql', {"
        "method:'POST', credentials:'include', "
        "headers:{'accept':'application/json, text/plain, */*','content-type':'application/json'}, "
        "body: JSON.stringify("
        + json.dumps(payload, ensure_ascii=False)
        + ")"
        "}).then(async r=>{const t=await r.text(); "
        "return JSON.stringify({status:r.status, contentType:r.headers.get('content-type'), body:t});"
        "}).catch(e=>JSON.stringify({error:String(e)}));"
    )
    raw = page.run_js(js, timeout=timeout)
    if raw is None:
        raise RuntimeError("browser fetch returned empty result")
    envelope = json.loads(raw)
    if envelope.get("error"):
        raise RuntimeError(envelope["error"])
    return envelope


def browser_outer_html(page, timeout=120):
    raw = page.run_js("return document.documentElement.outerHTML;", timeout=timeout)
    return str(raw or "")
