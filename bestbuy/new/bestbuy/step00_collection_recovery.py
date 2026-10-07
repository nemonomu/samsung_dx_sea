"""Bounded recovery and append-only evidence; no browser or configuration imports."""

import hashlib
import csv
import json
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DELAYS = (30, 120, 300, 600)
RECOVERY_SECONDS = 1800
MAX_LISTING_PASSES = 3
MAX_ITEM_ATTEMPTS = 2  # Initial detail request plus one retry, including transport failures.
SUCCESS_STATES = {"success", "empty", "not_required"}
DETAIL_READY_STATES = {"complete", "complete_with_warnings"}
RECOVERY_EVENTS = {"waiting", "browser_restart", "recovered", "pass_start", "pass_abandoned",
                   "complete", "complete_with_warnings", "incomplete", "partial", "previous_invocation"}


class CollectionIncomplete(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def atomic_csv(writer, path, rows, fields=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        if fields is None:
            writer(temp, rows)
        else:
            writer(temp, rows, fields)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def sanitized(value):
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if re.search(
            r"authorization|cookie|token|password|secret|api.?key|email|phone", str(k), re.I
        ) else sanitized(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitized(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)((?:token|api[_-]?key|password|cookie)=)[^&\s]+", r"\1[REDACTED]", value)
        return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", value)
    return value


class Evidence:
    def __init__(self, root, stage, settings=None):
        self.stage = stage
        self.invocation = uuid.uuid4().hex
        self.root = Path(root) / "recovery" / self.invocation
        self.root.mkdir(parents=True, exist_ok=True)
        self.sequence = 0
        self.last_request = None
        self.events = []
        try:
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
                stderr=subprocess.DEVNULL, timeout=3, text=True,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            revision = "unknown"
        sources = {}
        for name in ("step00_collection_recovery.py", "step01_listing_recovery.py", "step08_collection_recovery.py"):
            path = Path(__file__).with_name(name)
            sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.emit("start", revision=revision, source_hashes=sources, settings=settings or {})

    def emit(self, event, **fields):
        self.sequence += 1
        entry = sanitized(dict(time=utc_now(), invocation=self.invocation,
                               sequence=self.sequence, stage=self.stage, event=event, **fields))
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.events.append(entry)
        print("[collection] " + json.dumps(entry, ensure_ascii=False), flush=True)
        return entry

    def request(self, payload, response, **fields):
        event_id = uuid.uuid4().hex
        artifact = self.root / (event_id + ".json")
        safe_payload = sanitized(payload)
        atomic_json(artifact, dict(request=safe_payload, response=sanitized(response), **sanitized(fields)))
        current = time.monotonic()
        gap = None if self.last_request is None else round(current - self.last_request, 3)
        self.last_request = current
        return self.emit("request", evidence_path=str(artifact),
                         request_hash=fingerprint(safe_payload), gap_seconds=gap, **fields)


class RecoveryBudget:
    """One budget per stage; recovery success never resets its delay index."""
    def __init__(self, evidence, *, delays=DELAYS, seconds=RECOVERY_SECONDS,
                 clock=time.monotonic, sleep=time.sleep):
        self.evidence, self.delays, self.seconds = evidence, delays, seconds
        self.clock, self.sleep = clock, sleep
        self.started = None
        self.index = 0
        self.reason = ""

    def expired(self):
        if self.started is not None and self.clock() - self.started >= self.seconds:
            self.reason = "recovery_time_limit"
            return True
        return False

    def wait(self, **context):
        if self.started is None:
            self.started = self.clock()
        if self.expired():
            return False
        if self.index >= len(self.delays):
            self.reason = "recovery_attempt_limit"
            return False
        delay = self.delays[self.index]
        if self.clock() - self.started + delay >= self.seconds:
            self.reason = "recovery_time_limit"
            return False
        self.index += 1
        self.evidence.emit("waiting", wait_seconds=delay, recovery_attempt=self.index, **context)
        remaining = delay
        while remaining > 0:
            chunk = min(remaining, 30)
            self.sleep(chunk)
            remaining -= chunk
        return not self.expired()


def permanent_errors(errors):
    return any(str((e.get("extensions") or {}).get("code", "")).upper() in {
        "GRAPHQL_VALIDATION_FAILED", "GRAPHQL_PARSE_FAILED", "BAD_USER_INPUT"
    } for e in errors if isinstance(e, dict))


def assert_ready(run_root, *, listing=None):
    """Block downstream writes even if a stale final CSV still exists."""
    root = Path(run_root)
    names = (listing,) if listing else ("main", "bsr")
    paths = [root / name / "collection_status.json" for name in names]
    if listing is None:
        paths.append(root / "output" / "collection_status.json")
        if any(read_json(p) for p in paths[:-1]) and not read_json(paths[-1]):
            raise CollectionIncomplete("Finalization blocked: detail_not_collected")
    for path in paths:
        report = read_json(path)
        allowed = DETAIL_READY_STATES if path == root / "output" / "collection_status.json" else {"complete"}
        if report and report.get("status") not in allowed:
            raise CollectionIncomplete(f"Finalization blocked: {path}: {report.get('status')}")
        if report.get("status") == "complete_with_warnings":
            if not report.get("finalization_ready") or any(
                read_json(root / name / "collection_status.json").get("status") != "complete"
                for name in ("main", "bsr")
            ):
                raise CollectionIncomplete("Finalization blocked: unverified_warning_output")
        if report.get("occurrences_csv"):
            csv_path = Path(report["occurrences_csv"])
            if not csv_path.exists() or hashlib.sha256(csv_path.read_bytes()).hexdigest() != report.get("occurrences_sha256"):
                raise CollectionIncomplete("Finalization blocked: listing_artifact_changed_or_missing")
        if report.get("target_csv"):
            target_path = Path(report["target_csv"])
            if not target_path.exists():
                raise CollectionIncomplete("Finalization blocked: target_csv_missing")
            with target_path.open(encoding="utf-8-sig", newline="") as stream:
                target_rows = list(csv.DictReader(stream))
            if fingerprint(target_rows) != report.get("target_hash"):
                raise CollectionIncomplete("Finalization blocked: target_list_changed")
            if not Path(report["final_output_csv"]).exists():
                raise CollectionIncomplete("Finalization blocked: final_output_missing")
        for name, source in report.get("listing_sources", {}).items():
            current = read_json(root / name / "collection_status.json")
            if current.get("evidence_path") != source:
                raise CollectionIncomplete("Finalization blocked: listing_source_changed")


def recovery_notification(category, root, status="success"):
    root = Path(root)
    listings = {name: read_json(root / name / "collection_status.json") for name in ("main", "bsr")}
    detail = read_json(root / "output" / "collection_status.json")
    reports = [r for r in [*listings.values(), detail] if r]
    if not reports:
        return None
    incomplete = (not detail or detail.get("status") not in DETAIL_READY_STATES
                  or any(r.get("status") != "complete" for r in listings.values()))
    duplicates = [(name, item) for name, report in listings.items() for item in report.get("duplicates", [])]
    review_needed = detail.get("status") == "complete_with_warnings" or bool(duplicates)
    label = "부분 완료" if incomplete and detail.get("completed_skus", 0) else "미완료" if incomplete else "수집 완료"
    if not incomplete and review_needed:
        label = "검수 필요"
    if not incomplete and status != "success":
        label = "후속 단계 실패"
    lines = [f"수집 상태: {label}", "", "목록·순위"]
    for name, report in listings.items():
        state = "검증 완료" if report.get("status") == "complete" else "미완료" if report else "미실행/기존 방식"
        lines.append(f"- {name}: {state}, 확보 {report.get('organic_count', 0)}개, 채택 시도 {report.get('accepted_pass', '-')}")
        if report.get("failed_page"):
            lines.append(f"  실패 페이지 {report['failed_page']}, 사유 {report.get('reason', '')}")
    lines += ["", "상세 수집",
              f"- 전체 항목 완료 {detail.get('completed_skus', 0)}/{detail.get('target_count', 0)}개"]
    for stage, counts in detail.get("stage_counts", {}).items():
        lines.append(f"- {stage}: 성공 {counts.get('success', 0)}, 정상 빈 결과 {counts.get('empty', 0)}, "
                     f"미완료 {sum(n for s, n in counts.items() if s not in SUCCESS_STATES)}")
    lines += ["", "미완료 상품 (SKU | main 순위 | BSR 순위 | 항목 | 상태 | 실제 요청 횟수 | 응답 판정 횟수 | 원인)"]
    lines += ["detail=상세정보, review=리뷰, compare=비교상품 / failed=실패, pending=미실행"]
    failures = detail.get("failures", [])
    for item in failures[:50]:
        shown = dict(item, request_attempt=item.get("request_attempt", item.get("attempt", 0)),
                     attempt=item.get("attempt", "미기록"))
        lines.append(" | ".join(str(shown.get(k, "")) for k in
                              ("sku_id", "main_rank", "bsr_rank", "stage", "status", "request_attempt", "attempt", "reason")))
        if not incomplete:
            columns = ", ".join(item.get("null_columns", [])) or "없음 — 확보한 값 유지"
            lines.append(f"  재수집 후 미완료 / NULL 컬럼: {columns}")
        if item.get("error_paths"):
            lines.append("  응답 오류 위치: " + ", ".join(item["error_paths"]))
    if len(failures) > 50:
        lines.append(f"- 전체 {len(failures)}개 미완료 항목 중 50개 표시. 전체 내역: {root / 'detail/parsed/detail_failures.csv'}")
    if not failures:
        lines.append("- 없음" if detail else "- 상세 미실행")
    if duplicates:
        lines += ["", "목록 중복 — 첫 등장 채택"]
        for name, item in duplicates:
            lines.append(f"- {name} SKU {item['sku_id']}, {item['first_page']}페이지 {item['first_position']}번째 / "
                         f"{item['page']}페이지 {item['position']}번째 중복, 첫 등장 채택")
    lines += ["", "복구 이력"]
    for report in reports:
        for event in report.get("recovery_history", []):
            lines.append(f"- {event.get('time')} {event.get('stage')} {event.get('event')}: "
                         f"page={event.get('page', '-')} wait={event.get('wait_seconds', 0)}s "
                         f"reason={event.get('reason', '')}")
    if detail.get("reason"):
        lines.append("- 상세 종료 사유: " + detail["reason"])
    lines += ["", "최종 반영: " + ("보류 — 목록 미완료 또는 상세 처리 중단" if incomplete else
                                  "적재 허용 — 상세 누락은 NULL 처리, 실제 DB 반영 결과는 아래 확인" if review_needed else
                                  "수집 검증 통과 — DB 반영 결과는 아래 실행 결과 확인"),
              "실패로 인한 NULL은 실제 정보 없음과 구분하여 위 미완료 내역에 기록합니다.",
              "후속 조치: " + ("미완료 목록/항목을 재수집하세요." if incomplete else
                               "NULL 컬럼과 목록 중복 내역을 검수하세요." if review_needed else "후속 단계 결과를 확인하세요."),
              "", "진단 자료"]
    lines.extend(f"- {r.get('evidence_path', '')}" for r in reports)
    return {"subject": f"[SEA] [{label}] BBY {category} — {detail.get('completed_skus', 0)}/{detail.get('target_count', 0)}개",
            "body": "\n".join(lines), "incomplete": incomplete,
            "collected_count": detail.get("completed_skus", 0), "reports": reports}
