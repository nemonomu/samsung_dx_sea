"""Runner-side live offer check. Production collectors are imported unchanged.

From the repository root:
  python bestbuy/new/diagnose_graphql_offers.py --category REF --pages 2
"""

import argparse
import csv
import hashlib
import html
import json
import os
import sys
import time
import traceback
import unicodedata
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from uuid import uuid4


LABELS = {"membership": "멤버십", "rebates": "리베이트", "spend_and_get": "묶음/금액 혜택",
          "top_offers": "프로모션", "cabo_suco": "선택 할인", "gifts": "사은품"}
STATUS = {"running": "진행 중", "passed": "API·저장 검사 통과", "failed": "확인 필요",
          "incomplete_samples": "1·2·3 표본 부족", "error": "실행 오류", "interrupted": "중단됨"}
ISSUES = {"no_collected_rows": "수집된 상품이 없습니다", "saved_row_count_mismatch": "저장 전후 행 수가 다릅니다",
          "unverified_or_changed_rows": "미확인 응답 또는 저장값 불일치가 있습니다",
          "incomplete_page": "목록/offer 응답이 불완전한 페이지가 있습니다",
          "unexpected_per_page_navigation": "페이지별 이동 설정을 확인해야 합니다",
          "missing_required_count_samples": "요청한 offer 숫자의 표본이 부족합니다"}


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def console(text):
    # ASCII survives legacy RDP/PowerShell code-page mismatches. The HTML/CSV
    # report retains full Korean and product text in UTF-8.
    print(unicodedata.normalize("NFKD", str(text)).encode("ascii", "replace").decode("ascii"), flush=True)


def request_errors(report):
    errors = []
    absent_aliases = {item["alias"] for item in report.get("absent_offer_content", {}).values()}
    requests = list(report.get("requests", []))
    if report.get("listing_errors"):
        requests.append({"operation": "Listing", "response": {"errors": report["listing_errors"]}})
    if report.get("ignored_listing_errors"):
        requests.append({"operation": "Listing", "affects_offer": False,
                         "response": {"errors": report["ignored_listing_errors"]}})
    for request in requests:
        response = request.get("response") or {}
        for error in response.get("errors", []) if isinstance(response, dict) else []:
            path = error.get("path") or []
            absent = (request.get("operation") == "OfferCountContent" and len(path) == 2
                      and path[0] in absent_aliases and path[1] == "rows"
                      and (error.get("extensions") or {}).get("code") == "NOT_FOUND"
                      and (response.get("data") or {}).get(path[0]) == {"rows": None})
            errors.append({"operation": request.get("operation"), "attempt": request.get("attempt"),
                           "message": error.get("message"), "path": error.get("path"),
                           "code": (error.get("extensions") or {}).get("code"),
                           "affects_offer": False if absent else request.get("affects_offer", True),
                           "handling": "no_displayed_offer_content" if absent else ""})
        if request.get("error"):
            errors.append({"operation": request.get("operation"), "attempt": request.get("attempt"),
                           "message": request["error"]})
    return errors


def audit_rows(collected, saved, final_rows, proof_reader, final_targets=None):
    issues = []
    results = []
    if not collected:
        issues.append("no_collected_rows")
    if len(collected) != len(saved) or len(collected) != len(final_rows):
        issues.append("saved_row_count_mismatch")
    for i, row in enumerate(collected):
        stored = saved[i] if i < len(saved) else {}
        final = final_rows[i] if i < len(final_rows) else {}
        proof = proof_reader(stored)
        reasons = []
        if not row.get("sku_id") or not row.get("sku_id") == stored.get("sku_id") == final.get("sku_id"):
            reasons.append("저장 SKU 불일치 / saved_sku_mismatch")
        if proof.get("status") != "verified":
            reasons.append(proof.get("reason") or "GraphQL 근거 미확인")
        if not row.get("offer", "") == stored.get("offer", "") == stored.get("offer_count", "") == final.get("offer", ""):
            reasons.append("수집·CSV·최종 offer 값 불일치")
        if row.get("offer_graphql_json") != stored.get("offer_graphql_json"):
            reasons.append("CSV 근거 데이터 불일치")
        if final_targets is not None:
            target = final_targets[i] if i < len(final_targets) else {}
            if proof_reader(target) != proof or target.get("sku_id") != stored.get("sku_id"):
                reasons.append("최종 대상 CSV 근거 데이터 불일치")
        components = proof.get("components", {})
        results.append({"page": row.get("page", ""), "sku_id": row.get("sku_id", ""),
                        "product_name": row.get("product_name", ""),
                        "collected_offer": row.get("offer", ""), "csv_offer": stored.get("offer", ""),
                        "final_offer": final.get("offer", ""), "status": "failed" if reasons else "passed",
                        "label_absent": proof.get("status") == "verified" and proof.get("count") == "",
                        "reason": " | ".join(reasons), "components": components})
    if any(r["status"] == "failed" for r in results):
        issues.append("unverified_or_changed_rows")
    return results, issues


def summarize(summary, results, page_results, issues, required):
    values = Counter(r["collected_offer"] or ("0(표시 없음)" if r["label_absent"] else "미확인") for r in results)
    missing = sorted(required - set(values), key=int)
    issues = list(issues)
    if any(not p["listing_complete"] or str(p["status_code"]) != "200" or p["offer_complete"] is not True
           for p in page_results):
        issues.append("incomplete_page")
    if any(p["navigate_each_page"] != 0 for p in page_results):
        issues.append("unexpected_per_page_navigation")
    state = "failed" if issues else "passed"
    if missing:
        issues.append("missing_required_count_samples")
        if state == "passed":
            state = "incomplete_samples"
    summary.update(row_count=len(results), unique_sku_count=len({r["sku_id"] for r in results}),
                   passed_rows=sum(r["status"] == "passed" for r in results),
                   failed_rows=sum(r["status"] != "passed" for r in results),
                   offer_value_counts=dict(values), missing_count_samples=missing,
                   issues=list(dict.fromkeys(issues)), collection_status=state, pages=page_results)


def report_text(summary):
    return "\n".join([
        f"[{summary['category']}] {STATUS[summary['collection_status']]}",
        f"상품 {summary.get('unique_sku_count', 0)}개 / 수집 행 {summary.get('row_count', 0)}개",
        f"API·저장 검사: 통과 {summary.get('passed_rows', 0)} / 확인 필요 {summary.get('failed_rows', 0)}",
        "offer 분포: " + (", ".join(f"{k}: {v}행" for k, v in summary.get("offer_value_counts", {}).items()) or "없음"),
        "부족한 숫자 표본: " + (", ".join(summary.get("missing_count_samples", [])) or "없음"),
        *["- " + ISSUES.get(x, x) for x in summary.get("issues", [])],
        *( ["실행 오류: " + summary["error"]] if summary.get("error") else []),
        "화면 숫자와의 일치 여부: 미대조 (API·저장 검사 통과와 별개)",
    ])


def write_reports(output, summary, results):
    """Standalone readable report; escape all remote text and use no external assets."""
    esc = lambda value: html.escape(str(value), quote=True)
    rows = []
    for r in sorted(results, key=lambda row: (row["status"] == "passed", int(row["page"] or 0))):
        empty = "0 · 표시 없음" if r["label_absent"] else "미확인"
        counts = "".join(f'<td class="number">{esc(r[k] or empty)}</td>' for k in
                         ("collected_offer", "csv_offer", "final_offer"))
        parts = " · ".join(f"{LABELS.get(k, k)} {v}" for k, v in r["components"].items() if v) or ("혜택 표시 없음" if r["label_absent"] else "근거 부족")
        sku = r["sku_id"]
        link = f'<a href="https://www.bestbuy.com/site/{sku}.p?skuId={sku}" target="_blank" rel="noopener">{esc(sku)}</a>' if sku.isascii() and sku.isdigit() else esc(sku)
        rows.append(f'<tr class="{r["status"]}"><td>{esc(r["page"])}</td><td>{link}<small>{esc(r["product_name"])}</small></td>'
                    f'{counts}<td>{esc(parts)}</td><td>{"확인 필요" if r["status"] != "passed" else "API·저장 일치"}'
                    f'<small>{esc(r["reason"])}</small></td></tr>')
    body = "".join(rows) or '<tr><td colspan="7">아직 수집 결과가 없습니다. 아래 실행 기록을 확인하세요.</td></tr>'
    page_rows = "".join(f'<tr><td>{p["page"]}</td><td>{esc(p["status_code"])}</td><td>{p["rows"]}</td>'
                        f'<td>{p["offer_requests"]}</td><td>{esc(p["offer_reason"])}</td><td>{esc(p["zip_code"])}</td></tr>'
                        for p in summary.get("pages", []))
    document = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Best Buy offer 테스트</title><style>
body{font:15px/1.65 "Segoe UI","Malgun Gothic",sans-serif;background:#f2f5fa;color:#172033;margin:0;padding:32px}
main{max-width:1450px;margin:auto}h1{margin:4px 0;font-size:30px}h2{font-size:19px;margin-top:28px}
.muted,small{color:#596579}small{display:block;max-width:430px;overflow-wrap:anywhere}.notice{background:#fff3cd;padding:14px 18px;border-radius:9px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;background:white;padding:20px;border-radius:10px}
.scroll{overflow:auto;background:white;border-radius:10px}table{border-collapse:collapse;width:100%;font-size:14px}
th{background:#213b64;color:white;text-align:left;white-space:nowrap}th,td{padding:13px;border-bottom:1px solid #e4e9f1;vertical-align:top}
td.number{text-align:center;font-weight:700;font-size:18px;white-space:nowrap}tr.failed{background:#fff0ef}a{color:#1959aa}
</style><main>'''
    document += f'<div class="muted">BEST BUY · {esc(summary["category"])} · {esc(summary["started_at"])}</div><h1>offer 실수집 테스트</h1>'
    if summary.get("mode") == "saved_response_replay":
        document += '<div class="notice">저장된 응답으로 수정 코드를 재검사한 결과입니다. 새로운 실수집 결과가 아닙니다.</div>'
    document += '<p>운영 GraphQL 수집 → 정규화 → 최종 대상 CSV → 운영 최종 출력 함수의 offer 값 비교</p>'
    document += '<div class="notice">이 보고서의 통과는 API 근거와 저장값의 일치를 뜻합니다. 실제 화면의 “N offers for you”와는 아직 대조하지 않았습니다. 숫자 2·3 표본이 없으면 표본 부족으로 표시합니다.</div>'
    document += f'<pre>{esc(report_text(summary))}</pre><h2>상품별 결과</h2><p class="muted">미확인 상품이 먼저 나옵니다. SKU 링크는 수동 확인용입니다. 0 · 표시 없음은 실제 CSV에서는 빈 칸으로 저장됩니다.</p>'
    document += '<div class="scroll"><table><thead><tr><th>페이지</th><th>SKU / 상품</th><th>수집 offer</th><th>CSV offer</th><th>최종 offer</th><th>혜택 구성</th><th>검사 결과 / 사유</th></tr></thead><tbody>' + body + '</tbody></table></div>'
    document += '<h2>페이지별 요청</h2><div class="scroll"><table><tr><th>페이지</th><th>목록 HTTP</th><th>행</th><th>offer API 요청</th><th>offer 응답 상태</th><th>ZIP</th></tr>' + page_rows + '</table></div>'
    document += '<h2>실행 기록</h2><p><a href="run.log">상세 로그</a> · <a href="summary.json">요약 JSON</a> · <a href="offer_results.csv">검사 결과 CSV</a></p>'
    errors = [{"page": p["page"], **error} for p in summary.get("pages", []) for error in p.get("api_errors", [])]
    if errors:
        document += '<h2>서버 응답 참고 사항</h2><p>affects_offer: false는 offer와 무관한 항목 오류 또는 사이트 규칙상 표시할 프로모션 콘텐츠가 없는 응답입니다. 원문과 처리 이유를 함께 기록합니다.</p><pre>' + esc(json.dumps(errors, ensure_ascii=False, indent=2)) + '</pre>'
    document += '<p class="muted">별도 테스트 폴더와 브라우저 프로필을 사용합니다. 상세 페이지 수집·DB 저장·S3 업로드는 실행하지 않습니다. 초기 목록 페이지는 세션 준비를 위해 한 번 열며, offer 숫자는 GraphQL 응답으로 계산합니다.</p></main></html>'
    (output / "report.html").write_text(document, encoding="utf-8")
    (output / "summary.txt").write_text(report_text(summary) + "\n", encoding="utf-8")
    save_json(output / "summary.json", summary)
    with (output / "offer_results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = ["page", "sku_id", "product_name", "collected_offer", "csv_offer", "final_offer", "status", "label_absent", "reason", "components"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({**r, "components": json.dumps(r["components"], ensure_ascii=False)} for r in results)


def configure(args, output, root):
    os.environ.update({
        "BESTBUY_CATEGORY": args.category, "BESTBUY_RUN_ROOT": str(output),
        "BESTBUY_DETAIL_RUN_ROOT": str(output / "detail"), "BESTBUY_OUTPUT_ROOT": str(output / "output"),
        "BESTBUY_MAIN_RUN_ID": "main", "BESTBUY_URL_SOURCE": "csv",
        "BESTBUY_SEARCH_TERM": "refrigerator" if args.category == "REF" else "washing machine",
        "BESTBUY_SEARCH_URL": "", "BESTBUY_SEARCH_SORT": "",
        "BESTBUY_LISTING_COLLECTION_MODE": "browser_graphql", "BESTBUY_BROWSER_GRAPHQL_NAVIGATE_EACH_PAGE": "0",
        "BESTBUY_BROWSER_GRAPHQL_HEADLESS": "1" if args.headless else "0", "BESTBUY_BROWSER_GRAPHQL_LOCAL_PORT": "0",
        "BESTBUY_DETAIL_USE_DB_SELECTORS": "0",
    })
    os.environ.setdefault("BESTBUY_MAIN_SOURCE_PAYLOAD", str(root / "references/page_001_request.json"))
    if args.zip_code:
        os.environ["BESTBUY_ZIP_CODE"] = args.zip_code


def save_pipeline(rows, output, listing, targets, final, detail, proof):
    """Use real functions and read back actual CSV bytes. No detail fetch."""
    listing.write_csv(output / "collected_rows.csv", rows)
    stored = load_csv(output / "collected_rows.csv")
    normalized = [targets.normalize_existing_listing_row(r) for r in stored]
    enriched = final.enrich_rows(normalized, {}, {}, {}, final.main_attribute_map(normalized))
    final.write_csv(output / "final_targets.csv", enriched)
    persisted_targets = load_csv(output / "final_targets.csv")
    final_values = [{"sku_id": row.get("sku_id", ""), "offer": detail.output_row(row)["offer"]}
                    for row in persisted_targets]
    listing.write_csv(output / "final_offer_values.csv", final_values)
    return audit_rows(rows, stored, load_csv(output / "final_offer_values.csv"), proof.offer_evidence, persisted_targets)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live production GraphQL offer check; Korean HTML report")
    parser.add_argument("--category", choices=("REF", "LDY"), default="REF")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--open-report", action="store_true", help="Open HTML report after completion (Windows)")
    parser.add_argument("--zip-code")
    parser.add_argument("--require-counts", default="1,2,3", help="Required sample counts (default: 1,2,3)")
    args = parser.parse_args(argv)
    if not 1 <= args.pages <= 16:
        parser.error("--pages must be between 1 and 16")
    if args.zip_code and (not args.zip_code.isascii() or not args.zip_code.isdigit() or len(args.zip_code) != 5):
        parser.error("--zip-code must contain 5 digits")
    required = {v.strip() for v in args.require_counts.split(",") if v.strip()}
    if any(not v.isascii() or not v.isdigit() or int(v) < 1 for v in required):
        parser.error("--require-counts must contain positive integers, e.g. 1,2,3")
    root = Path(__file__).resolve().parent
    output = root / "offer_diagnostics" / (args.category + "_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6])
    output.mkdir(parents=True)
    summary = {"category": args.category, "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "collection_status": "running", "issues": [], "display_accuracy": "not_compared",
               "pages_requested": args.pages, "scope": "main listing and offer output only; no detail fetch/DB/S3",
               "differences_from_daily_run": ["isolated profile/output", "selected pages", "CSV URL source; same category search term",
                                              "no BSR/promotion/trend/detail fetch or DB selectors"], "source_sha256": {}}
    results, page_results, browser = [], [], None
    started = time.perf_counter()
    exit_code = 1
    write_reports(output, summary, results)
    console(f"[{args.category}] Live offer test | pages={args.pages}")
    console("Production GraphQL path. Initial page opens once for session setup. Korean results: report.html")
    console("OUTPUT_DIR=" + str(output))
    with (output / "run.log").open("w", encoding="utf-8", buffering=1) as log:
        try:
            # Resolve saved-request fallback paths the same way as the normal runner.
            os.chdir(root)
            configure(args, output, root)
            from bestbuy.bestbuy_orchestrator import STEPS
            os.environ.update(next(step.env for step in STEPS if step.name == "main_list"))
            configure(args, output, root)
            from bestbuy import step01_main_list as listing, step00_offer_graphql as proof
            from bestbuy import step02_main_targets as targets, step07_final_targets as final
            from bestbuy import step08_detail_enrichment as detail
            for module in (listing, proof, targets, final, detail):
                path = Path(module.__file__)
                summary["source_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            if (listing.CATEGORY != args.category or final.CATEGORY != args.category or detail.CATEGORY != args.category
                    or listing.RUN_ROOT.resolve() != (output / "main").resolve()
                    or detail.DETAIL_ROOT.resolve() != (output / "detail").resolve()):
                raise RuntimeError("새 Python 프로세스에서 테스트를 실행해 주세요.")
            with redirect_stdout(log):
                listing.make_dirs()
                operation = listing.load_product_list_operation()
                browser = listing.create_browser_graphql_page()
                listing.initialize_browser_graphql_session(browser)
            rows = []
            for page in range(1, args.pages + 1):
                console(f"[{args.category}] page {page}/{args.pages} | Requesting GraphQL... (details: run.log)")
                with redirect_stdout(log):
                    payload = listing.prepare_product_list_payload(operation, page)
                    _, meta, page_rows = listing.collect_browser_graphql_page(page, payload, browser)
                rows.extend(page_rows)
                evidence_path = listing.RUN_ROOT / "raw/browser_graphql" / f"{listing.page_stem(page)}_offers.json"
                errors = request_errors(json.loads(evidence_path.read_text(encoding="utf-8"))) if evidence_path.exists() else []
                page_results.append({"page": page, "status_code": meta.get("status_code"), "rows": len(page_rows),
                    "api_errors": errors,
                    "listing_complete": listing.listing_rows_complete(page_rows),
                    "offer_complete": meta.get("offer_graphql_complete"), "offer_reason": meta.get("offer_graphql_reason"),
                    "offer_requests": meta.get("offer_graphql_request_count", 0),
                    "zip_code": payload.get("variables", {}).get("destinationZipCode"),
                    "navigate_each_page": meta.get("browser_graphql_navigate_each_page")})
                save_json(output / "pages.json", page_results)
                with redirect_stdout(log):
                    results, issues = save_pipeline(rows, output, listing, targets, final, detail, proof)
                summarize(summary, results, page_results, issues, required)
                write_reports(output, summary, results)
                for r in results[-len(page_rows):] if page_rows else []:
                    value = r["collected_offer"] or ("0(no label)" if r["label_absent"] else "UNKNOWN")
                    console(f"  SKU {r['sku_id']} | offer={value} | {r['status']} | {r['product_name'][:55]}")
                console(f"  rows={len(page_rows)} | listing HTTP={meta.get('status_code')} | unknown offers={meta.get('offer_graphql_unverified_rows', 0)}")
                for error in errors:
                    if error.get("affects_offer", True):
                        console("  API_ERROR " + json.dumps(error, ensure_ascii=True))
                unrelated_count = sum(e.get("affects_offer") is False for e in errors)
                if unrelated_count:
                    console(f"  NON_OFFER_WARNINGS={unrelated_count} (details in report.html; offer inputs checked separately)")
            exit_code = 0 if summary["collection_status"] == "passed" else 2
        except KeyboardInterrupt:
            summary["collection_status"] = "interrupted"
            exit_code = 130
        except Exception as exc:
            summary.update(collection_status="error", error=f"{type(exc).__name__}: {exc}")
            traceback.print_exc(file=log)
        finally:
            if browser is not None:
                with redirect_stdout(log):
                    listing.close_browser_graphql_page(browser)
            summary["elapsed_seconds"] = round(time.perf_counter() - started, 2)
            summary["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            write_reports(output, summary, results)
    console(f"\n[{args.category}] {summary['collection_status']} | unique SKUs={summary.get('unique_sku_count', 0)} | rows={summary.get('row_count', 0)}")
    console(f"PASS={summary.get('passed_rows', 0)} | CHECK={summary.get('failed_rows', 0)} | missing samples={','.join(summary.get('missing_count_samples', [])) or 'none'}")
    console("SCREEN_COMPARISON=not_checked | Issues=" + ",".join(summary.get("issues", [])))
    if summary.get("error"):
        console("ERROR=" + summary["error"])
    console("REPORT=" + str(output / "report.html"))
    console("Exit: 0=API/storage passed; 2=unknown/insufficient samples; 1=error; 130=interrupted")
    if args.open_report and hasattr(os, "startfile"):
        try:
            os.startfile(str(output / "report.html"))
        except OSError as exc:
            console("Cannot open report automatically; open report.html manually. " + str(exc))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
