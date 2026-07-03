"""Email notification for Lowes pipeline runs.

Mirrors the BestBuy step16 email notification pattern but simplified for Lowes:
- Listing (main/bsr) uses ZenRows (paid)
- Detail uses UC + 4 XHR per SKU (free)
- Final cost = listing only

Email subject: [SEA] (Warning) Lowes {REF|LDY} crawled
Body: 수집 SKU, 비용 KRW, 호출 내역, 특이사항.
"""
import csv
import json
import os
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from .step00_config import (
    DEFAULT_LOWES_RUN_ROOT,
    lowes_product_type,
    lowes_run_date,
    rel_path,
)


KRW_PER_USD = int(os.getenv("LOWES_KRW_PER_USD", "1550"))

PRODUCT_TYPE = (lowes_product_type() or "REF").upper()
RUN_ROOT = Path(os.getenv("LOWES_RUN_ROOT", str(DEFAULT_LOWES_RUN_ROOT)))
OUTPUT_ROOT = Path(os.getenv("LOWES_OUTPUT_ROOT", str(RUN_ROOT / "output")))
FINAL_OUTPUT_CSV = Path(os.getenv("LOWES_FINAL_OUTPUT_CSV", str(OUTPUT_ROOT / "final_output.csv")))
MANIFEST_PATH = OUTPUT_ROOT / "email_notify_manifest.json"


# Columns where empty/null is normal (sparse spec fields)
EXCLUDED_NULL_COLUMNS = frozenset({
    "sku_popularity",
    "discount_type",
    "sku_status",
    "savings",
    "original_sku_price",
    "available_quantity_for_purchase_fastdelivery",  # page 미노출 디자인
    "fastest_delivery",
    "ldy_loading_type",  # REF run에서는 null 정상
    "ldy_capacity",
    "ref_refrigerator_type",  # LDY run에서는 null 정상
    "ref_capacity",
    "offer",  # 기존 retailer 테이블 컬럼, Lowes 미수집
})

CRITICAL_COLUMN_ALIASES = {
    "retailer_sku_name": ["retailer_sku_name"],
    "sku": ["sku"],
    "item": ["item", "omni_item_id"],
    "product_url": ["product_url"],
}

COLLECTED_COUNT_WARNING_MIN = {
    "REF": 300,
    "LDY": 250,
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def truthy(value, default=False):
    text = str(value if value is not None else ("1" if default else "0")).strip().lower()
    return text in {"1", "true", "yes", "y"}


def blank(value):
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text.lower() in {"null", "none", "nan"}


def as_float(value):
    if value in ("", None):
        return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return 0.0


def as_int(value):
    if value in ("", None):
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except ValueError:
        return 0


def money_krw(value):
    return f"{int(round(float(value or 0))):,}원"


def read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def read_csv_rows(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def first_env(names):
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def recipient_list(value):
    return [item.strip() for item in re.split(r"[;,]", str(value or "")) if item.strip()]


def listing_costs(run_root):
    """Sum ZenRows costs from main + bsr listing manifests (per-page x_request_cost)."""
    total = 0.0
    breakdown = []

    # main: page_summary.csv aggregates per-page x_request_cost
    for main_id in ("main_p1_13", "main"):
        summary_csv = run_root / main_id / "parsed" / "main_page_summary.csv"
        if not summary_csv.exists():
            continue
        cost = 0.0
        calls = 0
        for row in read_csv_rows(summary_csv):
            c = as_float(row.get("x_request_cost"))
            if c:
                cost += c
                calls += 1
        if cost or calls:
            total += cost
            breakdown.append({"source": f"main ({main_id})", "calls": calls, "cost_usd": round(cost, 6)})
            break

    # bsr: per-offset meta.json files
    for bsr_id in ("bsr_p1_5", "bsr"):
        bsr_dir = run_root / bsr_id / "raw" / "main_pages"
        if not bsr_dir.exists():
            continue
        cost = 0.0
        calls = 0
        for meta_path in bsr_dir.glob("*/meta.json"):
            data = read_json(meta_path)
            c = as_float(data.get("x_request_cost"))
            if c:
                cost += c
                calls += 1
        if cost or calls:
            total += cost
            breakdown.append({"source": f"bsr ({bsr_id})", "calls": calls, "cost_usd": round(cost, 6)})
            break

    return round(total, 7), breakdown


def detail_summary(run_root):
    """Read step08 manifest for UC detail run stats."""
    manifest = read_json(run_root / "detail" / "manifest_uc_xhr.json")
    return {
        "targets": as_int(manifest.get("targets")),
        "success": as_int(manifest.get("success")),
        "failure": as_int(manifest.get("failure")),
        "elapsed_seconds": as_float(manifest.get("overall_elapsed_seconds")),
    }


def db_summary(run_root):
    manifest = read_json(run_root / "output" / "db_load_manifest.json")
    return {
        "table": manifest.get("table", ""),
        "csv_rows": as_int(manifest.get("csv_rows")),
        "inserted": as_int(manifest.get("inserted")),
        "deleted_existing": as_int(manifest.get("deleted_existing")),
        "success": bool(manifest.get("success")),
    }


def insert_columns(rows):
    if rows:
        return [c for c in rows[0].keys() if c != "id"]
    return []


def critical_null_issues(rows):
    issues = []
    if not rows:
        return issues
    available = set(rows[0].keys())
    for logical, candidates in CRITICAL_COLUMN_ALIASES.items():
        column = next((c for c in candidates if c in available), "")
        if not column:
            issues.append(f"{logical} 컬럼이 final_output에 없음")
            continue
        bad = [r for r in rows if blank(r.get(column))]
        if not bad:
            continue
        examples = []
        for r in bad:
            url = str(r.get("product_url") or "").strip()
            item = str(r.get("item") or r.get("omni_item_id") or "").strip()
            if url:
                examples.append(f"{item} {url}".strip())
            elif item:
                examples.append(item)
        suffix = f", product_url: {' | '.join(examples)}" if examples else ""
        issues.append(f"{logical} {len(bad)} rows null{suffix}")
    return issues


def all_null_column_issues(rows):
    if not rows:
        return []
    cols = insert_columns(rows)
    all_null = [
        c
        for c in cols
        if c not in EXCLUDED_NULL_COLUMNS
        and all(blank(r.get(c)) for r in rows)
    ]
    if not all_null:
        return []
    return [f"전체 null 컬럼: {', '.join(all_null)}"]


def rank_values(rows, column):
    return sorted({as_int(r.get(column)) for r in rows if as_int(r.get(column)) > 0})


def compact_ranges(values):
    values = list(values or [])
    if not values:
        return ""
    ranges = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(f"{start}" if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(f"{start}" if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)


def page_number_values(value):
    if isinstance(value, list):
        return [as_int(item) for item in value if as_int(item) > 0]
    if isinstance(value, int):
        return list(range(1, value + 1)) if value > 0 else []
    if isinstance(value, str):
        return [as_int(item) for item in re.findall(r"\d+", value) if as_int(item) > 0]
    return []


def offset_values(value):
    if isinstance(value, list):
        return [as_int(item) for item in value if as_int(item) >= 0]
    if isinstance(value, int):
        return [value] if value >= 0 else []
    if isinstance(value, str):
        return [as_int(item) for item in re.findall(r"\d+", value)]
    return []


def listing_effective_expected_pages(manifest):
    requested = as_int(
        manifest.get("pages_requested")
        or manifest.get("search_pages")
        or len(page_number_values(manifest.get("page_numbers")))
    )
    page_counts = []
    for result in manifest.get("page_results") or []:
        if isinstance(result, dict):
            page_count = as_int(result.get("pagination_page_count"))
            if page_count:
                page_counts.append(page_count)
    actual_available = max(page_counts) if page_counts else 0
    if requested and actual_available:
        return min(requested, actual_available)
    return requested or actual_available


def listing_fetch_issues_for_manifest(run_root, listing_id, label):
    manifest = read_json(run_root / listing_id / "manifest.json")
    if not manifest:
        return []
    issues = []
    failed_offsets = offset_values(manifest.get("failed_offsets"))
    if failed_offsets:
        issues.append(f"{label} listing failed offsets {compact_ranges(failed_offsets)}")

    if listing_id == "bsr":
        per_page = manifest.get("per_page") if isinstance(manifest.get("per_page"), list) else []
        failed_from_page = []
        for entry in per_page:
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            parsed = as_int(entry.get("parsed"))
            if status != 200 or parsed <= 0:
                failed_from_page.append(as_int(entry.get("offset")))
        failed_from_page = sorted({offset for offset in failed_from_page if offset >= 0})
        if failed_from_page and failed_from_page != failed_offsets:
            issues.append(f"{label} listing failed offsets {compact_ranges(failed_from_page)}")
        return issues

    expected_pages = listing_effective_expected_pages(manifest)
    failed_pages_raw = manifest.get("failed_pages")
    failed_pages = []
    if isinstance(failed_pages_raw, list):
        failed_pages = page_number_values(failed_pages_raw)
    elif as_int(failed_pages_raw) > 0:
        for result in manifest.get("page_results") or []:
            if not isinstance(result, dict):
                continue
            page = as_int(result.get("page"))
            status_code = as_int(result.get("status_code"))
            if page and status_code != 200:
                failed_pages.append(page)
        if not failed_pages:
            failed_pages = list(range(1, as_int(failed_pages_raw) + 1))
    if expected_pages:
        failed_pages = [page for page in failed_pages if page <= expected_pages]
    failed_pages = sorted(set(failed_pages))
    if failed_pages:
        issues.append(f"{label} listing failed pages {compact_ranges(failed_pages)}")

    successful_pages = as_int(manifest.get("successful_http_pages"))
    valid_pages = as_int(manifest.get("valid_item_pages"))
    if expected_pages and successful_pages and successful_pages < expected_pages:
        issues.append(f"{label} listing http_ok {successful_pages}/{expected_pages}")
    if expected_pages and valid_pages and valid_pages < expected_pages:
        issues.append(f"{label} listing valid_pages {valid_pages}/{expected_pages}")

    challenge_pages = []
    for result in manifest.get("page_results") or []:
        if not isinstance(result, dict):
            continue
        page = as_int(result.get("page"))
        if expected_pages and page > expected_pages:
            continue
        attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
        challenge = result.get("challenge_detected") is True or any(
            isinstance(attempt, dict) and attempt.get("challenge_detected") is True
            for attempt in attempts
        )
        if challenge and page:
            challenge_pages.append(page)
    if challenge_pages:
        issues.append(f"{label} listing challenge pages {compact_ranges(sorted(set(challenge_pages)))}")
    return issues


def listing_fetch_issues(run_root):
    issues = []
    issues.extend(listing_fetch_issues_for_manifest(run_root, "main", "main"))
    issues.extend(listing_fetch_issues_for_manifest(run_root, "bsr", "bsr"))
    return issues


def listing_count_issues(run_root, rows):
    issues = []
    bsr_ranks = rank_values(rows, "bsr_rank")
    if bsr_ranks and len(bsr_ranks) < 100:
        missing = [rank for rank in range(1, 101) if rank not in set(bsr_ranks)]
        missing_text = compact_ranges(missing)
        suffix = f" missing {missing_text}" if missing_text else ""
        issues.append(f"bsr_rank {len(bsr_ranks)}/100{suffix}")
    return issues


def rank_collection_counts(rows):
    return {
        "main_rank": len(rank_values(rows, "main_rank")),
        "bsr_rank": len(rank_values(rows, "bsr_rank")),
    }


def detail_failure_issue(detail):
    failures = detail.get("failure", 0)
    targets = detail.get("targets", 0)
    if failures and targets:
        return [f"detail UC 실패 {failures}/{targets} SKU"]
    return []


def db_count_issue(db, row_count):
    inserted = db.get("inserted", 0)
    csv_rows = db.get("csv_rows", row_count)
    if csv_rows and inserted < csv_rows:
        return [f"DB insert 미달 {inserted}/{csv_rows}"]
    return []


def step_failure_issues(status, failed_step, failed_step_name):
    if str(status or "").strip().lower() not in {"failed", "fail", "error"}:
        return []
    label = " ".join(part for part in [f"step{failed_step}" if failed_step else "", failed_step_name] if part) or "step failure"
    return [f"{label} failed"]

def collected_count_issues(product_type, collected_count):
    minimum = COLLECTED_COUNT_WARNING_MIN.get(str(product_type or "").strip().upper())
    if minimum and as_int(collected_count) < minimum:
        return [f"collected_count {as_int(collected_count)}/{minimum}"]
    return []


def build_subject(product_type, issues):
    if issues:
        return f"[SEA] [Warning] Lowes {product_type} crawled"
    return f"[SEA] Lowes {product_type} crawled"


def build_body(collected_count, cost_krw, listing_breakdown, detail, db, issues, rank_counts=None):
    listing_calls = sum(b.get("calls", 0) for b in listing_breakdown)
    detail_calls = 0  # UC = no ZenRows calls
    total_calls = listing_calls + detail_calls
    per_call_krw = int(round(cost_krw / total_calls)) if total_calls else 0

    lines = [
        f"총 수집 {collected_count} sku",
        "",
        f"총 호출 비용 {money_krw(cost_krw)}(환율 {KRW_PER_USD:,}원 기준)",
        f"총 ZenRows 호출 {total_calls:,}회" + (f" / 1회당 {per_call_krw:,}원" if total_calls else ""),
        "",
        "호출 내역",
    ]
    for b in listing_breakdown:
        lines.append(f"  {b['source']} - {b['calls']:,}회 (${b['cost_usd']:.4f})")
    lines.append(f"  detail/review/compare - UC ($0)")
    lines.append("")
    if rank_counts is not None:
        lines.append("랭크 수집 현황")
        lines.append(f"  main_rank - {as_int(rank_counts.get('main_rank')):,}/300")
        lines.append(f"  bsr_rank - {as_int(rank_counts.get('bsr_rank')):,}/100")
        lines.append("")
    lines.append(f"detail UC: success {detail.get('success')}/{detail.get('targets')}  ({detail.get('elapsed_seconds'):.0f}s)")
    lines.append(f"DB: {db.get('table')} inserted={db.get('inserted')}")
    lines.append("")
    if issues:
        lines.append("특이사항")
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("특이사항 없음")
    return "\n".join(lines)


def build_notification(product_type, run_root, status="success", failed_step="", failed_step_name=""):
    rows = read_csv_rows(FINAL_OUTPUT_CSV)
    detail = detail_summary(run_root)
    db = db_summary(run_root)

    collected_count = db.get("inserted", 0) or len(rows)

    issues = []
    issues.extend(step_failure_issues(status, failed_step, failed_step_name))
    if not rows:
        issues.append("final_output.csv rows 0 또는 파일 없음")
    issues.extend(listing_fetch_issues(run_root))
    issues.extend(critical_null_issues(rows))
    issues.extend(all_null_column_issues(rows))
    issues.extend(listing_count_issues(run_root, rows))
    issues.extend(detail_failure_issue(detail))
    issues.extend(db_count_issue(db, len(rows)))
    issues.extend(collected_count_issues(product_type, collected_count))

    cost_usd, listing_breakdown = listing_costs(run_root)
    cost_krw = round(cost_usd * KRW_PER_USD)
    rank_counts = rank_collection_counts(rows)

    subject = build_subject(product_type, issues)
    body = build_body(collected_count, cost_krw, listing_breakdown, detail, db, issues, rank_counts=rank_counts)
    return {
        "subject": subject,
        "body": body,
        "issues": issues,
        "metrics": {
            "collected_count": collected_count,
            "cost_usd": cost_usd,
            "cost_krw": cost_krw,
            "krw_per_usd": KRW_PER_USD,
            "final_output_rows": len(rows),
            "listing_breakdown": listing_breakdown,
            "detail": detail,
            "db": db,
            "rank_counts": rank_counts,
        },
    }


def email_config():
    config = {
        "smtp_server": first_env(["LOWES_SMTP_SERVER", "SMTP_SERVER"]),
        "smtp_port": first_env(["LOWES_SMTP_PORT", "SMTP_PORT"]) or 587,
        "sender_email": first_env(["LOWES_EMAIL_FROM", "SMTP_EMAIL", "SMTP_SENDER_EMAIL"]),
        "sender_password": first_env(["LOWES_EMAIL_PASSWORD", "SMTP_PASSWORD", "SMTP_SENDER_PASSWORD"]),
        "receiver_email": first_env(["LOWES_EMAIL_TO", "ALERT_EMAIL", "SMTP_RECEIVER_EMAIL"]),
        "source": "env",
    }
    try:
        config["smtp_port"] = int(config["smtp_port"])
    except (TypeError, ValueError):
        config["smtp_port"] = 587
    return config


def send_email(subject, body, config):
    missing = [
        name
        for name in ("smtp_server", "smtp_port", "sender_email", "sender_password", "receiver_email")
        if not config.get(name)
    ]
    if missing:
        return False, f"email config missing: {', '.join(missing)}"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["sender_email"]
    message["To"] = ", ".join(recipient_list(config["receiver_email"]))
    message.set_content(body, charset="utf-8")
    with smtplib.SMTP(config["smtp_server"], config["smtp_port"], timeout=30) as server:
        server.starttls()
        server.login(config["sender_email"], config["sender_password"])
        server.send_message(message)
    return True, ""


def write_manifest(payload):
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    started_at = now()
    status = os.getenv("LOWES_NOTIFY_STATUS", "success").strip().lower() or "success"
    notification = build_notification(
        PRODUCT_TYPE,
        RUN_ROOT,
        status=status,
        failed_step=os.getenv("LOWES_NOTIFY_FAILED_STEP", ""),
        failed_step_name=os.getenv("LOWES_NOTIFY_FAILED_STEP_NAME", ""),
    )
    config = email_config()
    enabled = truthy(os.getenv("LOWES_EMAIL_NOTIFY", "1"), default=True)
    dry_run = truthy(os.getenv("LOWES_EMAIL_DRY_RUN", "0"))

    sent = False
    skipped_reason = ""
    error = ""

    if not enabled:
        skipped_reason = "LOWES_EMAIL_NOTIFY=0"
    elif dry_run:
        skipped_reason = "LOWES_EMAIL_DRY_RUN=1"
    else:
        try:
            sent, skipped_reason = send_email(notification["subject"], notification["body"], config)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    manifest = {
        "run_type": "step15_email_notify",
        "started_at": started_at,
        "finished_at": now(),
        "product_type": PRODUCT_TYPE,
        "run_root": rel_path(RUN_ROOT),
        "status": status,
        "subject": notification["subject"],
        "body": notification["body"],
        "issues": notification["issues"],
        "metrics": notification["metrics"],
        "email": {
            "enabled": enabled,
            "dry_run": dry_run,
            "sent": sent,
            "skipped_reason": skipped_reason,
            "error": error,
            "from": config.get("sender_email", ""),
            "to": config.get("receiver_email", ""),
        },
    }
    write_manifest(manifest)
    print(json.dumps(manifest, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
