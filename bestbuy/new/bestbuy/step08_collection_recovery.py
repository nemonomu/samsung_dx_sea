"""Browser detail collection with per-operation evidence and explicit partial results."""

import json
import time
from collections import Counter
from datetime import date
from pathlib import Path

from .step00_collection_recovery import (
    CollectionIncomplete, Evidence, MAX_ITEM_ATTEMPTS, RecoveryBudget, SUCCESS_STATES, RECOVERY_EVENTS,
    assert_ready, atomic_csv, atomic_json, fingerprint, permanent_errors, read_json, sanitized, utc_now,
)


def is_compare_feature_warning(stage, error):
    """Only the observed NOT_FOUND review-summary leaves are nonessential."""
    if stage != "compare" or not isinstance(error, dict):
        return False
    extensions = error.get("extensions")
    path = error.get("path")
    if not isinstance(extensions, dict) or extensions.get("code") != "NOT_FOUND" or not isinstance(path, list):
        return False
    if len(path) == 3:
        return path[:2] == ["productBySkuId", "reviewInfo"] and path[2] in ("proFeatures", "conFeatures")
    return (len(path) == 8 and path[:2] == ["recommendations", "subPlacements"]
            and type(path[2]) is int and path[2] >= 0 and path[3] == "recommendations"
            and type(path[4]) is int and path[4] >= 0 and path[5:7] == ["item", "reviewInfo"]
            and path[7] in ("proFeatures", "conFeatures"))


def validate_operation(d, stage, sku, item, target):
    if not isinstance(item, dict):
        return "failed", "missing_data"
    errors = item.get("errors") or []
    if not isinstance(errors, list):
        return "failed", "graphql_errors"
    blocking = [error for error in errors if not is_compare_feature_warning(stage, error)]
    if blocking:
        return "failed", "permanent_graphql_error" if permanent_errors(
            [error for error in blocking if isinstance(error, dict)]) else "graphql_errors"
    if not isinstance(item.get("data"), dict):
        return "failed", "missing_data"
    data = item["data"]
    product = data.get("productBySkuId") or {}
    if stage in {"detail", "review", "compare"}:
        if not isinstance(product, dict) or str(product.get("skuId") or "") != sku:
            return "failed", "product_identity_mismatch_or_missing"
    if stage == "detail":
        if not d.product_short_name(product):
            return "failed", "product_name_missing"
        if d.CATEGORY in {"REF", "LDY"} and not d.product_model_number([product]):
            return "failed", "model_number_missing"
        price = d.best_price([product])
        if not any(price.get(k) not in (None, "") for k in ("customerPrice", "displayableCustomerPrice")):
            if not d.is_detail_no_longer_available_product(product):
                return "failed", "price_missing"
    if stage == "review":
        count = d.review_result_count_from_json(item)
        expected = d.expected_review_text_count(target, sku)
        if (product.get("reviewInfo") or {}).get("reviewCount") == 0:
            expected = 0
        actual = d.review_text_count_from_json(item)
        if count is None:
            return "failed", "review_list_missing"
        if not d.review_text_count_is_sufficient(actual, expected):
            return "failed", f"review_partial_{actual}_of_{expected}"
        return ("empty", "verified_empty") if count == 0 else ("success", "verified")
    if stage in {"compare", "compare_v2"}:
        recs = data.get("recommendationsV2" if stage == "compare_v2" else "recommendations")
        placements = recs.get("subPlacements") if isinstance(recs, dict) else None
        if not isinstance(placements, list):
            return "failed", "recommendations_shape_missing"
        if any(not isinstance(p, dict) or not isinstance(p.get("recommendations"), list) for p in placements):
            return "failed", "recommendations_list_missing"
        for placement in placements:
            for recommendation in placement["recommendations"]:
                candidate = recommendation.get("item") if isinstance(recommendation, dict) else None
                if not isinstance(candidate, dict) or not str(candidate.get("skuId") or "").strip():
                    return "failed", "recommendation_identity_missing"
                name = d.product_short_name(candidate)
                if not isinstance(name, str) or not name.strip():
                    return "failed", "recommendation_name_missing"
        empty = not any(p["recommendations"] for p in placements)
        reason = "verified_empty" if empty else "verified"
        return ("empty" if empty else "success"), reason + ("_with_warnings" if errors else "")
    return "success", "verified"


def operation_paths(d, stage, sku):
    return {"detail": d.detail_paths, "review": d.review_paths, "compare": d.compare_paths}.get(stage, d.detail_paths)(sku)


def store_operation(d, target, stage, payload, item, state):
    sku = str(target["sku_id"])
    ok = state["status"] in SUCCESS_STATES
    metadata = dict(state, sku_id=sku, stage=stage, success=ok,
                    error="" if ok else state["reason"], url=d.target_url(target, sku),
                    finished_at=utc_now(), transport="browser_graphql", x_request_cost=0)
    body = json.dumps(sanitized(item), ensure_ascii=False)
    # Do not persist a mismatched product into the product parser's cache.
    product = ((item.get("data") or {}).get("productBySkuId") or {}) if isinstance(item, dict) else {}
    safe_item = item if not product or str(product.get("skuId") or "") == sku else {}
    if stage == "detail":
        paths = d.detail_paths_for_status(sku, target, ok)
        d.write_direct_detail_artifacts(paths, [payload], [sanitized(safe_item)], body, {})
        atomic_json(paths["json_response"], [sanitized(safe_item)])
    elif stage in {"review", "compare"}:
        factory = d.review_paths_for_status if stage == "review" else d.compare_paths_for_status
        paths = factory(sku, target, ok)
        writer = d.write_review_response_artifacts if stage == "review" else d.write_compare_response_artifacts
        writer(paths, payload, sanitized(safe_item), body, {})
        if stage == "review":
            metadata["review_count_returned"] = d.review_result_count_from_json(safe_item)
            metadata["review_text_count_returned"] = d.review_text_count_from_json(safe_item)
        else:
            metadata["recommendation_count"] = len(d.compare_recommendations_from_response(safe_item))
    else:
        # Auxiliary operations remain available to the established output parsers.
        paths = d.detail_paths(sku)
        existing = d.read_json(paths["apollo"])
        if ok and isinstance(existing, list):
            existing.extend(d.apollo_payload_from_graphql_response([payload], [safe_item]))
            atomic_json(paths["apollo"], sanitized(existing))
        return
    atomic_json(paths["meta"], metadata)


def make_report(targets, states, evidence, reason="", status="running"):
    failures, warnings, counts = [], [], {}
    complete = 0
    for target in targets:
        sku = str(target["sku_id"])
        stages = states[sku]
        complete += all(s["status"] in SUCCESS_STATES for s in stages.values() if s.get("required"))
        for stage, state in stages.items():
            counts.setdefault(stage, Counter())[state["status"]] += 1
            if state.get("required") and state["status"] not in SUCCESS_STATES:
                failures.append(dict(sku_id=sku, main_rank=target.get("main_rank", ""),
                    bsr_rank=target.get("bsr_rank", ""), stage=stage, **state))
            if state["status"] in SUCCESS_STATES and state.get("warnings"):
                warnings.append(dict(sku_id=sku, main_rank=target.get("main_rank", ""),
                    bsr_rank=target.get("bsr_rank", ""), stage=stage, **state))
    return dict(status=status, target_count=len(targets), completed_skus=complete,
                stage_counts=counts, failures=failures, warnings=warnings, items=states, reason=reason,
                evidence_path=str(evidence.root), collected_date=str(date.today()),
                recovery_history=[e for e in evidence.events if e["event"] in RECOVERY_EVENTS],
                updated_at=utc_now())


def safe_output_row(d, target, states):
    row = d.output_row(target)
    detail_state = states.get("detail", {})
    if not detail_state.get("response_received") and not detail_state.get("reused"):
        metadata = {k: row.get(k) for k in ("id", "product", "account_name", "page_type", "batch_id",
                    "calendar_week", "crawl_datetime", "crawl_date", "retailer_sku_name", "product_url", "item")}
        observed_review = row.get("detailed_review_content")
        observed_compare = row.get("retailer_sku_name_similar")
        row = {k: target.get(k) if target.get(k) not in (None, "") else None for k in row}
        row.update(metadata)
        row["retailer_sku_name"] = target.get("retailer_sku_name") or target.get("product_name")
        row["item"] = target.get("item") or target.get("bsin")
        row["product_url"] = target.get("product_url")
        row["sku_id"] = str(target["sku_id"])
        row["main_rank"], row["bsr_rank"] = target.get("main_rank"), target.get("bsr_rank")
        row["final_sku_price"] = target.get("final_sku_price") or target.get("customer_price")
        row["detailed_review_content"] = observed_review
        row["retailer_sku_name_similar"] = observed_compare
    # Retain observed listing values; avoid synthetic zero/not-reviewed values after failed detail.
    if states.get("detail", {}).get("status") not in SUCCESS_STATES:
        product = d.first_value(d.products_from_detail(str(target["sku_id"])), "reviewInfo") or {}
        if not product and not target.get("review_count"):
            for field in ("star_rating", "count_of_reviews", "count_of_star_ratings", "recommendation_intent"):
                row[field] = None
        if not d.best_price(d.products_from_detail(str(target["sku_id"]))) and not target.get("customer_price") and not target.get("final_sku_price"):
            for field in ("final_sku_price", "original_sku_price", "savings"):
                row[field] = None
    if states.get("review", {}).get("status") not in SUCCESS_STATES:
        row["detailed_review_content"] = None
    if states.get("compare", {}).get("status") not in SUCCESS_STATES:
        row["retailer_sku_name_similar"] = None
    return row


def run(d, targets, output_targets, *, budget_factory=RecoveryBudget):
    assert_ready(d.OUTPUT_ROOT.parent, listing="main")
    assert_ready(d.OUTPUT_ROOT.parent, listing="bsr")
    evidence = Evidence(d.DETAIL_ROOT, "detail", dict(category=d.CATEGORY, stage=d.STAGE,
        batch_size=d.DETAIL_SKU_BATCH_SIZE, item_attempts=MAX_ITEM_ATTEMPTS))
    budget = budget_factory(evidence)
    status_path = d.OUTPUT_ROOT / "collection_status.json"
    previous = read_json(status_path)
    if previous:
        atomic_json(evidence.root / "previous_status.json", previous)
        evidence.emit("previous_invocation", evidence_path=previous.get("evidence_path"),
                      previous_status_path=str(evidence.root / "previous_status.json"))
    target_hash = fingerprint(output_targets)
    reusable = previous.get("target_hash") == target_hash and previous.get("collected_date") == str(date.today()) and not d.FORCE_REFRESH
    previous_states = previous.get("items", {}) if reusable else {}
    selected = {str(t["sku_id"]) for t in targets}
    states, operations = {}, {}
    for target in output_targets:
        sku = str(target["sku_id"])
        payloads, indices = d.detail_batch_payloads_for_sku(sku)
        operations[sku] = {stage: payloads[index] for stage, index in indices.items()}
        states[sku] = {}
        for stage in indices:
            state = dict(status="pending", attempt=0, request_attempt=0, reason="not_collected", required=stage in {"detail", "review", "compare"})
            prior = previous_states.get(sku, {}).get(stage, {})
            artifact = prior.get("evidence_path")
            if prior.get("status") in SUCCESS_STATES and artifact and Path(artifact).exists():
                paths = operation_paths(d, stage, sku)
                result_path = paths.get("apollo") if stage == "detail" else paths.get("response_json", paths.get("apollo"))
                if paths["meta"].exists() and result_path and result_path.exists():
                    state = dict(prior, reused=True)
            if stage == "review" and d.is_external_review_source(target):
                state.update(status="not_required", reason="external_review_source")
            states[sku][stage] = state

    calls = 0
    def checkpoint(reason="", status="running"):
        report = make_report(output_targets, states, evidence, reason, status)
        report["target_hash"] = target_hash
        report["request_calls"] = calls
        if d.TARGET_CSV.exists():
            report["target_csv"] = str(d.TARGET_CSV.resolve())
            report["final_output_csv"] = str(d.FINAL_OUTPUT_CSV.resolve())
        report["listing_sources"] = {
            name: source["evidence_path"] for name in ("main", "bsr")
            if (source := read_json(d.OUTPUT_ROOT.parent / name / "collection_status.json")) and source.get("evidence_path")
        }
        atomic_json(status_path, report)
        return report

    checkpoint()
    reason = ""
    restarted = False
    old_recoveries = d.BROWSER_GRAPHQL_MAX_RECOVERIES
    d.BROWSER_GRAPHQL_MAX_RECOVERIES = 0  # One shared recovery owner; no nested retry multiplication.
    fatal = None
    try:
        if previous.get("collected_date") and previous["collected_date"] != str(date.today()):
            raise CollectionIncomplete("stale_run_requires_new_listing_collection")
        for round_number in range(MAX_ITEM_ATTEMPTS):
            pending = [t for t in output_targets if str(t["sku_id"]) in selected and any(
                s["status"] not in SUCCESS_STATES and s.get("reason") != "permanent_graphql_error"
                and s["attempt"] < MAX_ITEM_ATTEMPTS for s in states[str(t["sku_id"])].values())]
            if not pending:
                break
            if round_number and not budget.wait(reason="retry_incomplete_operations"):
                reason = budget.reason
                break
            for offset in range(0, len(pending), max(1, d.DETAIL_SKU_BATCH_SIZE)):
                chunk = pending[offset:offset + max(1, d.DETAIL_SKU_BATCH_SIZE)]
                requests, entries = [], []
                for target in chunk:
                    sku = str(target["sku_id"])
                    for stage, state in states[sku].items():
                        if state["status"] in SUCCESS_STATES or state["reason"] == "permanent_graphql_error" or state["attempt"] >= MAX_ITEM_ATTEMPTS:
                            continue
                        entries.append((target, sku, stage, operations[sku][stage]))
                        requests.append(operations[sku][stage])
                if not entries:
                    continue
                while True:
                    if budget.expired():
                        raise CollectionIncomplete(budget.reason)
                    start = time.monotonic()
                    request_started_at = utc_now()
                    for _, sku, stage, _ in entries:
                        states[sku][stage]["request_attempt"] += 1
                    error, code, response, response_text = "", "ERR", {}, ""
                    try:
                        code, response_text, response, _, _ = d.browser_graphql_post(
                            requests, d.target_url(chunk[0], str(chunk[0]["sku_id"])), str(chunk[0]["sku_id"]))
                    except (RuntimeError, d.RequestException) as exc:
                        error = str(exc)
                    calls += 1
                    if isinstance(response, dict) and len(requests) == 1 and "data" in response:
                        response = [response]
                    transport_ok = str(code) == "200" and isinstance(response, list) and len(response) == len(requests)
                    event = evidence.request(requests, response, status_code=code, error=error,
                        started_at=request_started_at, finished_at=utc_now(),
                        unparsed_response=sanitized(response_text[:8000]) if not response else "",
                        elapsed_seconds=round(time.monotonic() - start, 3),
                        session_generation=d.BROWSER_GRAPHQL_PROCESS_GENERATION,
                        browser_version=d.BROWSER_GRAPHQL_META.get("browser_version", "unknown"),
                        targets=[dict(sku_id=sku, operation=stage) for _, sku, stage, _ in entries],
                        status="response_received" if transport_ok else "transport_failed")
                    if transport_ok and not budget.expired():
                        break
                    for target, sku, stage, payload in entries:
                        states[sku][stage].update(status="failed", reason=error or f"http_{code}_or_batch_shape",
                            status_code=code, evidence_path=event["evidence_path"])
                    checkpoint()
                    top_errors = response.get("errors", []) if isinstance(response, dict) else []
                    if str(code) in {"400", "404"} or permanent_errors(top_errors) or not budget.wait(reason=error or f"http_{code}"):
                        raise CollectionIncomplete(budget.reason or "non_retryable_transport_error")
                    if budget.index >= 2 and not restarted:
                        d.close_detail_browser_page()
                        restarted = True
                        evidence.emit("browser_restart", reason="repeated_transport_failure")
                for index, (target, sku, stage, payload) in enumerate(entries):
                    state = states[sku][stage]
                    state["attempt"] += 1
                    outcome, why = validate_operation(d, stage, sku, response[index], target)
                    state.update(status=outcome, reason=why, status_code=code,
                                 evidence_path=event["evidence_path"], collected_at=utc_now(), response_received=True)
                    state["warnings"] = sanitized(response[index].get("errors") or []) if (
                        outcome in SUCCESS_STATES and isinstance(response[index], dict)) else []
                    if outcome not in SUCCESS_STATES and budget.started is None:
                        budget.started = budget.clock()
                    store_operation(d, target, stage, payload, response[index], state)
                    evidence.emit("operation_result", sku_id=sku, operation=stage,
                        main_rank=target.get("main_rank"), bsr_rank=target.get("bsr_rank"), **state)
                checkpoint()
        reason = reason or "item_attempt_limit"
    except CollectionIncomplete as exc:
        reason = str(exc)
    except BaseException as exc:
        reason = type(exc).__name__ + ": " + str(exc)
        fatal = exc
    finally:
        d.BROWSER_GRAPHQL_MAX_RECOVERIES = old_recoveries
        d.close_detail_browser_page()

    try:
        report = checkpoint(reason)
        complete = not report["failures"] and bool(output_targets) and fatal is None
        rows = [safe_output_row(d, target, states[str(target["sku_id"])]) for target in output_targets]
        for folder in (d.PARSED_DIR, d.BENCHMARKS_DIR, d.OUTPUT_ROOT):
            folder.mkdir(parents=True, exist_ok=True)
        atomic_csv(d.write_csv, d.DETAIL_ROWS_CSV, rows)
        atomic_csv(d.write_csv, d.FAILURES_CSV, report["failures"], ["sku_id", "main_rank", "bsr_rank", "stage", "status", "attempt", "request_attempt", "reason", "evidence_path"])
        fields = d.sample_fields()
        for row in rows:
            for field in fields:
                row.setdefault(field, None)
        if complete:
            d.preserve_existing_availability(rows)
            final_rows = [{field: row.get(field) for field in fields} for row in rows]
            atomic_csv(d.write_csv, d.FINAL_OUTPUT_CSV, final_rows, fields)
            d.update_product_list_from_detail_rows(rows)
        else:
            atomic_csv(d.write_csv, d.OUTPUT_ROOT / "partial_output.csv", rows)
            atomic_json(d.OUTPUT_ROOT / "partial_output.json", rows)
        evidence.emit("complete" if complete else "partial", reason="verified" if complete else reason,
                      completed_skus=report["completed_skus"], target_count=len(output_targets))
        report = checkpoint("verified" if complete else reason, "complete" if complete else "partial")
        manifest = dict(run_type="step08_detail_enrichment", target_count=len(output_targets),
            success_count=report["completed_skus"], failure_count=len(report["failures"]),
            collection_status=report["status"], stage=d.STAGE, finished_at=utc_now(),
            total_calls_this_run=calls, detail_calls_this_run=calls, review_calls_this_run=0,
            compare_calls_this_run=0, total_cost_usd_this_run=0, evidence_path=str(evidence.root))
        history = dict(read_json(d.MANIFEST_PATH).get("runs_by_stage") or {})
        history[evidence.invocation] = dict(detail_calls=calls, review_calls=0, compare_calls=0,
                                          detail_cost_usd=0, review_cost_usd=0, compare_cost_usd=0)
        manifest["runs_by_stage"] = history
        atomic_json(d.MANIFEST_PATH, manifest)
        if fatal is not None:
            raise fatal
        if not complete:
            raise CollectionIncomplete(f"Partial detail collection: {report['completed_skus']}/{len(output_targets)}; {reason}")
        return report
    except Exception as exc:
        if not isinstance(exc, CollectionIncomplete):
            failure = "output_or_finalization_error: " + type(exc).__name__ + ": " + str(exc)
            evidence.emit("incomplete", reason=failure)
            checkpoint(failure, "partial")
        raise
