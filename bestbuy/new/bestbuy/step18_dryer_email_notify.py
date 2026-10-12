"""DRYER result summary using the existing BBY mail settings and transport."""
import os
from pathlib import Path

from .step00_dryer_log import error_diagnostic, event


def notification_status(manifest):
    status = manifest.get("status")
    if status == "failed":
        return "partial" if manifest.get("collection_status") == "partial" and manifest.get("output_count", 0) else "failed"
    if status not in {"success", "success_with_warnings"} or not manifest.get("db_loaded"):
        return "failed"
    shortfall = any(manifest.get(kind + "_limit", 0) > manifest.get(kind + "_target_count", 0)
                   for kind in ("main", "bsr"))
    if shortfall or manifest.get("failure_count", 0) or manifest.get("collection_status") != "complete":
        return "partial"
    return "success"


def build_notification(manifest, run_dir):
    from .step16_email_notify import build_subject
    status = notification_status(manifest)
    labels = {"success": "성공", "partial": "부분수집", "failed": "실패"}
    log_path = str(Path(run_dir) / "logs" / "dryer.log")
    lines = [
        "Best Buy DRYER 수집 결과",
        f"결과: {labels[status]}",
        f"수집 상태: {manifest.get('collection_status', '미완료')}",
        f"배치: {manifest.get('batch_id', '미생성')}",
        f"적재 테이블: {manifest.get('table', '')}",
        f"메인: {manifest.get('main_target_count', 0)} / {manifest.get('main_limit', 0)}",
        f"BSR: {manifest.get('bsr_target_count', 0)} / {manifest.get('bsr_limit', 0)}",
        f"중복: {manifest.get('overlap_count', 0)}",
        f"최종 대상: {manifest.get('target_count', 0)}",
        f"상세 수집: {manifest.get('collected_count', 0)}",
        f"최종 출력: {manifest.get('output_count', 0)}",
        f"DB 적재: {manifest.get('inserted_count', 0)} (완료: {bool(manifest.get('db_loaded'))})",
        f"상세 실패: {manifest.get('failure_count', 0)} / 목록값 보완: {manifest.get('fallback_count', 0)}",
        f"소요 시간: {manifest.get('elapsed_s', 0)}초 (메일 발송 전까지)",
        f"실패 단계: {manifest.get('failure_stage') or '없음'}",
        f"오류: {manifest.get('error') or '없음'}",
        f"로그: {log_path}",
    ]
    return {"status": status, "subject": build_subject("DRYER", [] if status == "success" else [status]),
            "body": "\n".join(lines)}


def notify_run(manifest, run_dir):
    from . import step16_email_notify as mail
    enabled = mail.truthy(os.getenv("BESTBUY_EMAIL_NOTIFY", "1"), default=True)
    dry_run = mail.truthy(os.getenv("BESTBUY_EMAIL_DRY_RUN", "0"))
    result = {"enabled": enabled, "dry_run": dry_run, "sent": False,
              "run_status": notification_status(manifest)}
    try:
        notification = build_notification(manifest, run_dir)
        result["subject"] = notification["subject"]
        if not enabled:
            result.update(status="skipped", reason="email_disabled")
        elif dry_run:
            result.update(status="skipped", reason="email_dry_run")
        else:
            sent, reason = mail.send_email(notification["subject"], notification["body"], mail.email_config())
            result.update(sent=bool(sent), status="sent" if sent else "skipped")
            if not sent:
                result["reason"] = "email_config_missing" if reason.startswith("email config missing:") else "email_not_sent"
    except Exception as exc:
        result.update(status="failed", **error_diagnostic(exc))
    # Persist only outcome and safe diagnostics, never mail addresses or settings.
    event("email_notify_complete", **result)
    return result
