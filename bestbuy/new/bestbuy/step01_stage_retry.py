"""Retry a failed listing subprocess after its own short recovery is exhausted."""

import json
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .step00_collection_recovery import permanent_errors, sanitized


RETRY_DELAYS = (600, 900, 1200)
EXHAUSTED_REASONS = {
    "recovery_attempt_limit", "recovery_time_limit", "recovery_exhausted", "listing_pass_limit",
}
INTERRUPT_CODES = {130, -1073741510, 3221225786}
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


def listing_failure(graph, meta, reason):
    """Keep the actual failure separate from the eventual recovery-limit reason."""
    errors = (graph.get("errors") or []) if isinstance(graph, dict) else []
    status = str(meta.get("status_code", "ERR"))
    message = str(reason or "")
    exception_type = str(meta.get("exception_type") or "")
    kind, retryable = "validation", False
    if permanent_errors(errors):
        kind = "permanent_graphql"
    elif meta.get("parse_error"):
        kind = "parse"
    elif status.isdigit() and int(status) != 200:
        kind, retryable = "http", int(status) in TRANSIENT_HTTP
    elif status == "ERR":
        kind = "transport"
        retryable = (
            "failed to fetch" in message.lower()
            or "networkerror" in message.lower()
            or "timed out" in message.lower()
            or any(code in message.upper() for code in (
                "NET::ERR_CONNECTION_", "NET::ERR_NETWORK_CHANGED", "NET::ERR_INTERNET_DISCONNECTED",
                "NET::ERR_NAME_NOT_RESOLVED", "NET::ERR_TIMED_OUT", "NET::ERR_HTTP2_PROTOCOL_ERROR",
            ))
            or exception_type in {
                "TimeoutError", "Timeout", "ReadTimeout", "ConnectTimeout",
                "ConnectionError", "ConnectionResetError", "PageDisconnectedError",
            }
        )
    elif reason in {"graphql_listing_error", "graphql_sponsored_listing_error"}:
        kind = "graphql"
        codes = [str((e.get("extensions") or {}).get("code", "")).upper()
                 for e in errors if isinstance(e, dict)]
        retryable = bool(codes) and all(code in {"INTERNAL_SERVER_ERROR", "SERVICE_UNAVAILABLE"} for code in codes)
    return sanitized(dict(kind=kind, retryable=retryable, reason=message,
                          status_code=status, exception_type=exception_type,
                          browser_diagnostics=meta.get("browser_diagnostics", {})))


def read_status(root):
    try:
        value = json.loads((Path(root) / "collection_status.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def retryable_result(before, after):
    # A crash before writing this invocation's report must not reuse an old failure.
    return (
        bool(after.get("evidence_path"))
        and after.get("evidence_path") != before.get("evidence_path")
        and after.get("status") == "incomplete"
        and after.get("reason") in EXHAUSTED_REASONS
        and isinstance(after.get("last_failure"), dict)
        and after["last_failure"].get("retryable") is True
    )


def run_listing_step(command, env, root, *, execute):
    """Run only this step, at most four times; never replay downstream writes."""
    root = Path(root)
    invocation = uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)

    def emit(event, attempt, **fields):
        entry = sanitized(dict(time=datetime.now(timezone.utc).isoformat(),
                               invocation=invocation, stage=root.name, event=event,
                               attempt=attempt, **fields))
        with (root / "stage_retry_events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print("[listing_step_retry] " + json.dumps(entry, ensure_ascii=False), flush=True)

    for attempt in range(1, len(RETRY_DELAYS) + 2):
        before = read_status(root)
        emit("attempt_start", attempt)
        try:
            result = execute(command, check=True, env=env)
        except KeyboardInterrupt:
            emit("interrupted", attempt)
            raise
        except subprocess.CalledProcessError as exc:
            if exc.returncode in INTERRUPT_CODES:
                emit("interrupted", attempt, exit_code=exc.returncode)
                raise
            after = read_status(root)
            eligible = exc.returncode == 1 and retryable_result(before, after)
            emit("attempt_failed", attempt, exit_code=exc.returncode,
                 retryable=eligible, reason=after.get("reason", "missing_report"),
                 last_failure=after.get("last_failure", {}), evidence_path=after.get("evidence_path", ""))
            if not eligible or attempt > len(RETRY_DELAYS):
                emit("exhausted" if eligible else "not_retryable", attempt)
                raise
            delay = RETRY_DELAYS[attempt - 1]
            emit("waiting", attempt, wait_seconds=delay, next_attempt=attempt + 1,
                 next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat())
            try:
                remaining = delay
                while remaining:
                    chunk = min(remaining, 30)
                    time.sleep(chunk)
                    remaining -= chunk
            except KeyboardInterrupt:
                emit("interrupted", attempt)
                raise
        else:
            emit("recovered" if attempt > 1 else "complete", attempt)
            return result
