"""Dryer run summaries; reuse the existing Lowe's SMTP adapter only at runtime."""
from datetime import datetime
import json
import os
from pathlib import Path


def enabled(value, default=False):
    return str(value if value is not None else int(default)).strip().lower() in {"1", "true", "yes", "y"}


def build_notification(manifest):
    collection = manifest.get("collection") or {}
    counts = collection.get("counts") or {}
    db = manifest.get("db") or {}
    status = manifest.get("status", "failed")
    table = db.get("table") or manifest.get("table")
    collected = counts.get("rows")
    loaded = bool(manifest.get("db_loaded"))
    no_targets = manifest.get("db_skip_reason") == "no_eligible_products"
    inserted = db.get("inserted") if loaded else 0 if no_targets else None
    issues = []
    if status != "complete":
        issues.append(status)
    if status == "complete" and not loaded and not no_targets and not manifest.get("collect_only"):
        issues.append("db_load_unconfirmed")
    if loaded and (inserted != collected or db.get("verified_count") != inserted):
        issues.append("db_count_mismatch")
    if loaded and table != manifest.get("table"):
        issues.append("db_table_mismatch")
    label = {"complete": "정상 완료", "partial_failure": "일부 실패", "failed": "실패",
             "interrupted": "중단"}.get(status, "실패")
    if status == "complete" and issues:
        label = "확인 필요"
    if manifest.get("collect_only"):
        label += " / 수집만 실행·DB 미적재"
    if "db_load_unconfirmed" in issues:
        label += " / DB 적재 미확인"
    try:
        seconds = round((datetime.fromisoformat(manifest["finished_at"]) -
                         datetime.fromisoformat(manifest["started_at"])).total_seconds(), 1)
    except (KeyError, TypeError, ValueError):
        seconds = None
    show = lambda value: "미확인" if value is None else str(value)
    subject = "[SEA] " + ("[Warning] " if issues else "[Collect only] " if manifest.get("collect_only") else "")
    subject += "Lowes DRYER crawled"
    lines = ["Lowe's 단독 건조기 수집 결과", f"결과: {label}",
             f"배치: {manifest.get('batch_id') or '미확인'}", f"적재 대상: {table or '미확인'}",
             f"수집 상품: {show(collected)}개", f"메인: {show(counts.get('main'))}개",
             f"BSR: {show(counts.get('bsr'))}개", f"공통: {show(counts.get('shared'))}개",
             f"확인된 DB 적재: {show(inserted)}개", f"수집·적재 소요 시간: {show(seconds)}초",
             f"API 실패 상품: {show(collection.get('api_failure_products'))}개",
             f"품질 경고: {show(collection.get('quality_warnings'))}개"]
    if no_targets:
        lines.append("적재 생략: 수집 대상 없음")
    if manifest.get("failed_stage"):
        lines.append("실패 단계: " + manifest["failed_stage"])
    if issues:
        lines.append("확인 사항: " + ", ".join(issues))
    return {"subject": subject, "body": "\n".join(lines), "issues": issues,
            "metrics": {"status": status, "table": table, "collected": collected, "inserted": inserted,
                        "main": counts.get("main"), "bsr": counts.get("bsr"), "shared": counts.get("shared"),
                        "elapsed_seconds": seconds}}


def _send_existing_lowes(subject, body):
    # Existing runtime loader already loaded env. Do not copy settings or recipients.
    from .step15_email_notify import email_config, send_email
    sent, _ = send_email(subject, body, email_config())
    return bool(sent)


def notify_run(root, manifest, logger, sender=None, env=None):
    """One notification per batch/table, regardless of standalone/chain entry.

Persist 'sending' before SMTP. An uncertain delivery is never retried
automatically, since SMTP cannot guarantee exactly-once delivery after a crash.
No SMTP settings, recipient addresses or raw exception messages are persisted.
"""
    env = os.environ if env is None else env
    root = Path(root)
    path = root / "output" / "email_notify_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError:
        logger.warning("[메일] notification_in_progress_or_interrupted batch=%s", manifest.get("batch_id"))
        return {"status": "skipped", "sent": False, "reason": "notification_locked", "path": str(path)}
    handle.close()
    try:
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if (previous.get("batch_id"), previous.get("table")) == (manifest.get("batch_id"), manifest.get("table")) and previous.get("status") in {"sent", "sending", "delivery_unknown"}:
                logger.info("[메일] duplicate_suppressed batch=%s previous_status=%s",
                            manifest.get("batch_id"), previous["status"])
                return {"status": "skipped", "sent": previous.get("status") == "sent",
                        "reason": "already_sent_or_delivery_unconfirmed", "path": str(path)}
        notification = build_notification(manifest)
        payload = {"batch_id": manifest.get("batch_id"), "table": manifest.get("table"),
                   "collection_status": manifest.get("status"), **notification,
                   "status": "sending", "sent": False, "reason": ""}

        def save():
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)

        if not enabled(env.get("LOWES_EMAIL_NOTIFY"), default=True):
            payload.update(status="skipped", reason="LOWES_EMAIL_NOTIFY=0")
        elif enabled(env.get("LOWES_EMAIL_DRY_RUN")):
            payload.update(status="skipped", reason="LOWES_EMAIL_DRY_RUN=1")
        else:
            save()
            try:
                sent = (sender or _send_existing_lowes)(notification["subject"], notification["body"])
                payload.update(status="sent" if sent else "not_sent", sent=bool(sent),
                               reason="" if sent else "email_configuration_missing_or_not_sent")
            except Exception as exc:
                payload.update(status="delivery_unknown", reason=type(exc).__name__)
        save()
        log = logger.info if payload["status"] in {"sent", "skipped"} else logger.warning
        log("[메일] batch=%s status=%s sent=%s reason=%s", payload["batch_id"], payload["status"],
            payload["sent"], payload["reason"] or "none")
        return {key: payload[key] for key in ("status", "sent", "reason")} | {"path": str(path)}
    finally:
        lock.unlink()
