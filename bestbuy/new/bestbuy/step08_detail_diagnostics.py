"""Read-only REF/LDY diagnostics, separate from collection state and output rows."""
import re
import time
from contextlib import contextmanager
from functools import wraps
from urllib.parse import urlsplit

from .step00_browser_diagnostics import BrowserDiagnostics


def enabled(d):
    return getattr(d, "CATEGORY", "") in {"REF", "LDY"}


def emit(d, evidence, name, **fields):
    try:
        if enabled(d):
            evidence.emit(name, category=d.CATEGORY, **fields)
    except Exception:
        pass  # A diagnostic failure must not abort or retry collection.


def context(d):
    result = {}
    try:
        page = getattr(d, "BROWSER_GRAPHQL_PAGE", None)
        parts = urlsplit(str(getattr(page, "url", "") or ""))
        if parts.scheme in {"http", "https"} and parts.hostname == "www.bestbuy.com":
            result["context_url"] = f"{parts.scheme}://{parts.hostname}{parts.path}"
        for name, key in (("BROWSER_GRAPHQL_PROCESS_GENERATION", "process_generation"),
                          ("BROWSER_GRAPHQL_RECOVERY_PROFILE_GENERATION", "profile_generation")):
            value = getattr(d, name, None)
            if type(value) is int:
                result[key] = value
        kind = getattr(d, "BROWSER_GRAPHQL_SESSION_KIND", "")
        if kind in {"home_origin", "pdp"}:
            result["session_kind"] = kind
    except Exception:
        pass
    return result


def finish(observer, failed):
    """Keep network enums/status only, excluding messages, headers and bodies."""
    try:
        raw = observer.finish(failed) if observer is not None else {}
        info = {"listener_status": raw.get("listener_status", "unavailable"), "network_events": []}
        for packet in raw.get("network_events", [])[:20]:
            item = {}
            code = re.search(r"\bnet::ERR_[A-Z0-9_]+\b", str(packet.get("errorText", "")))
            if code:
                item["error_code"] = code.group()
            for key in ("is_failed", "canceled"):
                if type(packet.get(key)) is bool:
                    item[key] = packet[key]
            status = packet.get("status_code")
            if type(status) is int and 100 <= status <= 599:
                item["http_status"] = status
            cors = packet.get("corsErrorStatus") or {}
            cors = cors.get("corsError", "") if isinstance(cors, dict) else cors
            for key, value in (("blocked_reason", packet.get("blockedReason", "")), ("cors_error", cors)):
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", value):
                    item[key] = value
            if item:
                info["network_events"].append(item)
        if raw.get("ready_state") in {"loading", "interactive", "complete"}:
            info["ready_state"] = raw["ready_state"]
        if type(raw.get("online")) is bool:
            info["online"] = raw["online"]
        return info
    except Exception:
        return {"diagnostic_status": "unavailable"}


@contextmanager
def observe_request(d):
    """Observe each existing fetch after bootstrap; never initiate a fetch/retry."""
    report = {"attempts": []}
    original = getattr(d, "fetch_detail_browser_graphql_envelope", None)
    if not enabled(d) or not callable(original):
        yield report
        return

    @wraps(original)
    def observed(*args, **kwargs):
        observer = None
        try:
            observer = BrowserDiagnostics(getattr(d, "BROWSER_GRAPHQL_PAGE", None))
        except Exception:
            pass
        started = time.monotonic()
        failed, status = True, None
        record = context(d)
        try:
            result = original(*args, **kwargs)
            failed = False
            if isinstance(result, dict) and type(result.get("status")) is int:
                status = result["status"]
            return result
        finally:
            try:
                record.update(finish(observer, failed))
                record.update(attempt=len(report["attempts"]) + 1, failed=failed, http_status=status,
                              elapsed_seconds=round(time.monotonic() - started, 3))
                report["attempts"].append(record)
            except Exception:
                pass

    d.fetch_detail_browser_graphql_envelope = observed
    try:
        yield report
    finally:
        d.fetch_detail_browser_graphql_envelope = original


def request_result(d, evidence, requests, report, code, response, transport_ok, evidence_path):
    try:
        names = {"ProductSchema_init", "ProductSchemaGetItFastProbe", "GetCompareProduct",
                 "FulfillmentOptionHook_FulfillmentDynamicQuery", "ProductCarousel_Recommendations", "GetTradeInData"}
        skus = {str((p.get("variables") or {}).get("skuId")) for p in requests
                if (p.get("variables") or {}).get("skuId") is not None}
        observed_statuses = set()
        if str(code).isdigit() and 100 <= int(code) <= 599:
            observed_statuses.add(int(code))
        for attempt in report.get("attempts", []):
            for source in [attempt, *attempt.get("network_events", [])]:
                status = source.get("http_status")
                if type(status) is int and 100 <= status <= 599:
                    observed_statuses.add(status)
        emit(d, evidence, "request_diagnostic", request_evidence_path=evidence_path,
             operation_count=len(requests), sku_count=len(skus),
             operation_names=sorted({p.get("operationName") if p.get("operationName") in names else "other"
                                     for p in requests}),
             http_status=code if str(code).isdigit() else "ERR",
             http_response_received=bool(observed_statuses), observed_http_statuses=sorted(observed_statuses),
             batch_shape_valid=transport_ok,
             response_shape="array" if isinstance(response, list) else "object" if isinstance(response, dict) else "other",
             response_count=len(response) if isinstance(response, list) else None,
             transport=report)
    except Exception:
        pass


def operation_result(d, evidence, sku, stage, item, state, response_received):
    try:
        paths = ("data.productBySkuId.skuId", "data.productBySkuId.manufacturer.modelNumber",
                 "data.productBySkuId.specificationGroups", "data.productBySkuId.price") if stage == "detail" else (
                 "data.productBySkuId.reviews",) if stage == "review" else ("data.recommendations.subPlacements",)
        fields = {}
        if response_received:
            for path in paths:
                value = item
                for key in path.split("."):
                    if not isinstance(value, dict) or key not in value:
                        fields[path] = "missing"
                        break
                    value = value[key]
                else:
                    fields[path] = "null" if value is None else "empty" if value in ("", [], {}) else "present"
        emit(d, evidence, "operation_diagnostic", sku_id=sku, operation=stage,
             request_attempt=state.get("request_attempt"), response_received=response_received,
             status=state.get("status"),
             reason=state.get("reason") if response_received else "transport_or_batch_failure",
             error_paths=state.get("error_paths", []) if response_received else [], response_fields=fields,
             request_evidence_path=state.get("evidence_path"))
    except Exception:
        pass


def output_fields(d, evidence, targets, rows, states):
    try:
        if not enabled(d):
            return
        fields = {"sku", "final_sku_price", "original_sku_price", "offer", "ref_capacity", "ref_refrigerator_type",
                  "ldy_capacity", "ldy_loading_type", "detailed_review_content", "recommendation_intent",
                  "retailer_sku_name_similar", "pick_up_availability", "delivery_availability"}
        for target, row in zip(targets, rows):
            sku = str(target["sku_id"])
            emit(d, evidence, "output_field_diagnostic", sku_id=sku,
                 scope="detail_output_before_availability_backfill",
                 blank_columns=sorted(k for k in fields & row.keys() if row[k] is None or row[k] == ""),
                 operations={stage: {k: state.get(k) for k in ("status", "response_received", "reused", "null_columns")}
                             for stage, state in states[sku].items()})
    except Exception:
        pass
