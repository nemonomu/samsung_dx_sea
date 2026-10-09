"""Safe DRYER progress logs: never serialize exception messages or network data."""
import io
import json
import re
import sys
import threading
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from functools import wraps
from pathlib import Path

_LOGGER = None


def exception_chain(exc):
    result, seen = [], set()
    while exc is not None and id(exc) not in seen and len(result) < 8:
        seen.add(id(exc))
        result.append(exc)
        exc = exc.__cause__ or exc.__context__
    return result


def error_diagnostic(exc):
    chain = exception_chain(exc)
    category = "unclassified_error"
    # Inspect messages internally only to return a fixed category. Never return text.
    markers = (
        ("err_http2_protocol_error", "browser_http2_error"),
        ("err_name_not_resolved", "browser_dns_error"),
        ("dns_probe_", "browser_dns_error"),
        ("access denied", "site_access_denied"),
        ("verify you are human", "site_challenge"),
        ("captcha", "site_challenge"),
        ("err_connection_", "browser_connection_error"),
        ("unexpected_home_url:", "home_url_not_verified"),
        ("unexpected_origin:", "site_origin_not_verified"),
        ("could not open chrome session", "browser_start_failed"),
        ("could not open browser_graphql chrome session", "browser_start_failed"),
        ("navigation returned false", "browser_navigation_failed"),
        ("systemic http status: 403", "http_403"),
        ("systemic http status: 429", "http_429"),
        ("failed to fetch", "graphql_fetch_failed"),
    )
    for cause in reversed(chain):
        text = str(cause).lower()
        matched = next((label for marker, label in markers if marker in text), "")
        if matched:
            category = matched
            break
        name = type(cause).__name__
        http_error = re.fullmatch(r"http_[1-5][0-9]{2}", text)
        if name == "DryerError" and http_error:
            category = text
            break
        if name in {"TimeoutError", "TimeoutException", "WaitTimeoutError"}:
            category = "timeout"
        elif name == "BrowserConnectError":
            category = "browser_start_failed"
        elif name == "PageDisconnectedError":
            category = "browser_disconnected"
    frames = []
    for cause in reversed(chain):
        tb = cause.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            filename = Path(code.co_filename).name
            # Only code metadata; no full paths, source lines, arguments or locals.
            if re.fullmatch(r"[A-Za-z0-9_]+\.py", filename) and re.fullmatch(r"[A-Za-z0-9_<>]+", code.co_name):
                value = f"{filename}:{code.co_name}:{tb.tb_lineno}"
                if value not in frames:
                    frames.append(value)
            tb = tb.tb_next
    return {"error_type": type(exc).__name__, "error_category": category,
            "cause_types": list(dict.fromkeys(type(c).__name__ for c in chain)),
            "trace": frames[-12:]}


def graphql_diagnostic(errors):
    errors = errors if isinstance(errors, list) else []
    categories, fields, types = set(), set(), set()
    allowed = {"skuId", "bsin", "name", "short", "long", "description", "features", "title", "manufacturer", "modelNumber",
               "url", "pdp", "reviewInfo", "averageRating", "reviewCount", "specificationGroups", "specifications",
               "displayName", "value", "price", "customerPrice", "regularPrice", "totalSavings",
               "salesChannel", "locationId", "customerId", "customerAttributes", "planPaidMemberType", "ct", "isStoreAgent"}
    allowed_types = {"ProductPriceInput", "ProductItemPriceInput", "String", "Boolean", "Int", "Float"}
    for error in errors:
        if not isinstance(error, dict):
            continue
        message = str(error.get("message") or "")
        if "Unknown type" in message:
            categories.add("unknown_type")
        elif "Cannot query field" in message:
            categories.add("unsupported_field")
        elif "Unknown argument" in message:
            categories.add("unsupported_argument")
        elif "used in position expecting type" in message or "Expected value of type" in message:
            categories.add("type_mismatch")
        elif "Variable" in message and ("invalid value" in message or "was not provided" in message):
            categories.add("invalid_variables")
        elif "Syntax Error" in message:
            categories.add("syntax_error")
        else:
            extensions = error.get("extensions")
            code = extensions.get("code") if isinstance(extensions, dict) else None
            category = {"GRAPHQL_VALIDATION_FAILED": "graphql_validation_failed", "BAD_USER_INPUT": "bad_user_input"}.get(
                code if isinstance(code, str) else "", "unclassified_graphql_error")
            categories.add(category)
        quoted = set(re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]*)!?[\"']", message))
        fields.update(allowed.intersection(quoted))
        types.update(allowed_types.intersection(quoted))
    return {"graphql_error_count": len(errors), "graphql_error_categories": sorted(categories),
            "graphql_fields": sorted(fields), "graphql_types": sorted(types)}


def graphql_response_diagnostic(body):
    """Summarize object/batch errors without exposing any response or message text."""
    responses = body if isinstance(body, list) else [body]
    errors = []
    for response in responses:
        if isinstance(response, dict) and isinstance(response.get("errors"), list):
            errors.extend(response["errors"])
    shape = "array" if isinstance(body, list) else "object" if isinstance(body, dict) else "other"
    return {"response_shape": shape, **graphql_diagnostic(errors)}


class RunLogger:
    def __init__(self, run_dir, heartbeat_seconds=10):
        folder = Path(run_dir) / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        self.text_path = folder / "dryer.log"
        self.json_path = folder / "dryer_events.jsonl"
        self.stream = sys.stdout
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.active = []
        self.failure_stage = ""
        self.stop = threading.Event()
        self.interval = heartbeat_seconds
        self.text = self.text_path.open("a", encoding="utf-8")
        self.structured = self.json_path.open("a", encoding="utf-8")
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def emit(self, event, **fields):
        with self.lock:
            record = {"time": datetime.now().isoformat(timespec="seconds"),
                      "elapsed_s": round(time.monotonic() - self.started, 2), "event": event, **fields}
            values = " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
            line = f"{record['time']} [{event}] elapsed_s={record['elapsed_s']} {values}".rstrip()
            print(line, file=self.stream, flush=True)
            self.text.write(line + "\n")
            self.text.flush()
            self.structured.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.structured.flush()

    def _heartbeat(self):
        while not self.stop.wait(self.interval):
            with self.lock:
                if self.active:
                    stage, started, fields = self.active[-1]
                    self.emit("waiting", stage=stage, wait_s=round(time.monotonic() - started, 2), **fields)

    def clear_failure(self):
        with self.lock:
            self.failure_stage = ""

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)
        with self.lock:
            self.text.close()
            self.structured.close()


def event(name, **fields):
    if _LOGGER is not None:
        _LOGGER.emit(name, **fields)
    else:
        values = " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
        print(f"[{name}] {values}", flush=True)


@contextmanager
def run_logging(run_dir):
    global _LOGGER
    previous = _LOGGER
    logger = RunLogger(run_dir)
    _LOGGER = logger
    try:
        event("logging_ready", text_log=str(logger.text_path), events_log=str(logger.json_path))
        yield logger
    finally:
        logger.close()
        _LOGGER = previous


@contextmanager
def phase(stage, **fields):
    logger = _LOGGER
    started = time.monotonic()
    entry = (stage, started, fields)
    if logger is not None:
        with logger.lock:
            logger.active.append(entry)
    event("stage_start", stage=stage, **fields)
    try:
        yield
    except (Exception, KeyboardInterrupt) as exc:
        known_stage = next((getattr(cause, "_dryer_failure_stage", "") for cause in exception_chain(exc)
                            if getattr(cause, "_dryer_failure_stage", "")), "")
        if not known_stage:
            try:
                exc._dryer_failure_stage = stage
            except Exception:
                pass
        if logger is not None:
            logger.failure_stage = known_stage or stage
        event("stage_failed", stage=stage, duration_s=round(time.monotonic() - started, 2),
              **error_diagnostic(exc), **fields)
        raise
    else:
        event("stage_complete", stage=stage, duration_s=round(time.monotonic() - started, 2), **fields)
    finally:
        if logger is not None:
            with logger.lock:
                logger.active.remove(entry)


class DiscardBinarySink(io.RawIOBase):
    def writable(self):
        return True

    def write(self, data):
        return len(data)


class LegacyLogSink(io.TextIOBase):
    """Discard legacy text completely; progress comes from instrumented functions."""
    def __init__(self):
        # step00_parse_pdp wraps sys.stdout.buffer during its first import.
        # Give it a private discard buffer so the real console is never closed.
        self.buffer = DiscardBinarySink()

    def write(self, text):
        return len(text)

    def flush(self):
        pass


@contextmanager
def safe_legacy_output():
    with redirect_stdout(LegacyLogSink()), redirect_stderr(LegacyLogSink()):
        yield


@contextmanager
def trace_browser_calls(helpers):
    # Instrument only this dedicated DRYER process and restore every original function.
    originals = {}
    names = {"create_browser_page": "chrome_start", "navigate_detail_browser": "site_navigation",
             "wait_for_detail_browser_state": "page_verification", "fetch_detail_browser_graphql_envelope": "graphql_fetch",
             "bootstrap_detail_browser_session": "session_bootstrap", "recreate_detail_browser_page": "chrome_restart",
             "recover_detail_browser_session": "session_recovery"}
    counts = {}
    try:
        for name, stage in names.items():
            original = getattr(helpers, name, None)
            if not callable(original):
                continue
            originals[name] = original
            def traced(*args, _original=original, _stage=stage, **kwargs):
                counts[_stage] = counts.get(_stage, 0) + 1
                fields = {"attempt": counts[_stage]}
                if _stage in {"chrome_restart", "session_recovery"}:
                    fields["fresh_profile"] = bool(kwargs.get("fresh_profile", False))
                with phase(_stage, **fields):
                    return _original(*args, **kwargs)
            setattr(helpers, name, wraps(original)(traced))
        yield
    finally:
        for name, original in originals.items():
            setattr(helpers, name, original)
