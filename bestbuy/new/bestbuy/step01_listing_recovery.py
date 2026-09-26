"""Sequential browser listings: never splice pages from different collection passes."""

from .step00_collection_recovery import (
    CollectionIncomplete, Evidence, MAX_LISTING_PASSES, RecoveryBudget,
    atomic_json, fingerprint, permanent_errors,
)
from .step01_stage_retry import listing_failure


def validate_page(graph, meta, rows):
    if str(meta.get("status_code")) != "200" or meta.get("error") or meta.get("parse_error"):
        return False, meta.get("error") or meta.get("parse_error") or f"http_{meta.get('status_code')}", False
    if not isinstance(graph, dict):
        return False, "invalid_response", False
    errors = graph.get("errors") or []
    # Field failures such as fulfillmentOptions do not erase verified list positions.
    # Errors at the search/document/product identity level do invalidate the list.
    for error in errors:
        path = error.get("path") or []
        if not path or (path[0] == "detailedProductSearch" and (len(path) <= 4 or path[-1] == "skuId")):
            return False, "graphql_listing_error", False
        if path[0] == "search" and (len(path) <= 6 or path[-1] == "skuId"):
            return False, "graphql_sponsored_listing_error", False
    search = (graph.get("data") or {}).get("detailedProductSearch")
    docs = search.get("documents") if isinstance(search, dict) else None
    if not isinstance(docs, list):
        return False, "missing_documents", False
    expected = []
    for position, doc in enumerate(docs, 1):
        if not isinstance(doc, dict):
            return False, "missing_document", False
        product = doc.get("product") or {}
        if isinstance(product, dict) and product.get("skuId"):
            expected.append((str(product["skuId"]), position))
        elif not (isinstance(doc.get("combo"), dict) and doc["combo"].get("id")):
            return False, "missing_product_identity", False
    actual = [(str(r.get("sku_id")), int(r.get("organic_rank") or 0))
              for r in rows if r.get("container_type") == "organic_product"]
    if actual != expected:
        return False, "parsed_positions_mismatch", False
    if len({sku for sku, _ in actual}) != len(actual):
        return False, "duplicate_organic_sku", False
    expected_ads = []
    placements = (((graph.get("data") or {}).get("search") or {}).get("withBestMedia") or {}).get("placements", [])
    for placement in placements or []:
        if placement.get("name") != "SEARCH_SPONSORED_INGRID":
            continue
        documents = (placement.get("documentsGridView") or {}).get("sponsoredDocuments")
        if not isinstance(documents, list):
            return False, "missing_sponsored_documents", False
        for doc in documents:
            if not isinstance(doc, dict):
                return False, "missing_sponsored_document", False
            if not doc.get("source") and not any(k.startswith("on") and "Beacon" in k for k in doc):
                continue
            sku = (doc.get("product") or {}).get("skuId")
            if not sku:
                return False, "missing_sponsored_identity", False
            expected_ads.append(str(sku))
    if expected_ads != [str(r.get("sku_id")) for r in rows if r.get("container_type") == "sponsored_ingrid"]:
        return False, "parsed_sponsored_positions_mismatch", False
    return True, "verified_empty" if not docs else "verified", not docs


def collect(listing, operation, browser_page, *, budget_factory=RecoveryBudget):
    root = listing.RUN_ROOT
    evidence = Evidence(root, root.name, {"search_term": listing.SEARCH_TERM,
        "sort": listing.SEARCH_SORT, "pages": listing.SEARCH_PAGES,
        "organic_target": listing.LISTING_ORGANIC_TARGET,
        "max_passes": MAX_LISTING_PASSES, "organic_offset": listing.ORGANIC_OFFSET})
    budget = budget_factory(evidence)
    report = dict(status="running", accepted_pass=None, organic_count=0,
                  evidence_path=str(evidence.root), reason="", failed_page=None,
                  listing_request_calls=0, offer_request_calls=0)

    def checkpoint():
        report["recovery_history"] = [e for e in evidence.events if e["event"] != "request"]
        atomic_json(root / "collection_status.json", report)

    checkpoint()
    session_ready = browser_page is not None
    def fetch(page, pass_number, probe=False):
        nonlocal browser_page, session_ready
        payload = listing.prepare_product_list_payload(operation, page)
        try:
            if browser_page is None:
                browser_page = listing.create_browser_graphql_page()
            if not session_ready:
                listing.initialize_browser_graphql_session(browser_page)
                session_ready = True
            graph, meta, rows = listing.browser_graphql_fetch_once(page, payload, browser_page)
        except Exception as exc:
            graph, rows = {}, []
            meta = {"status_code": "ERR", "error": str(exc), "exception_type": type(exc).__name__,
                    "browser_diagnostics": getattr(exc, "bestbuy_browser_diagnostics", {})}
        ok, reason, empty = validate_page(graph, meta, rows)
        if not ok:
            report["last_failure"] = listing_failure(graph, meta, reason)
        event = evidence.request(payload, graph, page=page, pass_number=pass_number, probe=probe,
            status="success" if ok else "failed", reason=reason,
            status_code=meta.get("status_code"), elapsed_seconds=meta.get("elapsed_seconds"),
            started_at=meta.get("started_at"), finished_at=meta.get("finished_at"),
            session=listing.browser_graphql_local_port(),
            raw_response_path=meta.get("response_path", ""),
            sku_order=[r.get("sku_id") for r in rows],
            order_hash=fingerprint([(r.get("sku_id"), r.get("organic_rank"), r.get("container_type")) for r in rows]),
            graphql_errors=graph.get("errors", []) if isinstance(graph, dict) else [],
            browser_diagnostics=meta.get("browser_diagnostics", {}))
        meta["recovery_evidence"] = event["evidence_path"]
        report["listing_request_calls"] += 1
        report["offer_request_calls"] += int(meta.get("offer_graphql_request_count") or 0)
        return graph, meta, rows, ok, reason, empty

    restart_done = False
    try:
        for pass_number in range(1, MAX_LISTING_PASSES + 1):
            evidence.emit("pass_start", pass_number=pass_number)
            rows_by_page, summaries, raw = {}, [], []
            seen = {}
            report["duplicates"] = []
            organic = 0
            complete = False
            page_limit = listing.LISTING_MAX_PAGES if listing.LISTING_ORGANIC_TARGET else listing.SEARCH_PAGES
            for page in range(1, page_limit + 1):
                if budget.expired():
                    raise CollectionIncomplete(budget.reason)
                graph, meta, rows, ok, reason, empty = fetch(page, pass_number)
                if budget.expired():
                    raise CollectionIncomplete(budget.reason)
                skus = [str(r.get("sku_id")) for r in rows if r.get("container_type") == "organic_product"]
                if ok and skus and all(sku in seen for sku in skus):
                    # A whole repeated page is not evidence of forward pagination.
                    ok, reason = False, "repeated_organic_page"
                if not ok:
                    report["last_failure"] = listing_failure(graph, meta, reason)
                    report.update(failed_page=page, reason=reason, organic_count=organic)
                    evidence.emit("pass_abandoned", pass_number=pass_number, page=page, reason=reason)
                    checkpoint()
                    if pass_number == MAX_LISTING_PASSES:
                        raise CollectionIncomplete("listing_pass_limit")
                    if permanent_errors(graph.get("errors", []) if isinstance(graph, dict) else []):
                        raise CollectionIncomplete("permanent_graphql_error")
                    recovered = False
                    while budget.wait(page=page, reason=reason):
                        if budget.index >= 2 and not restart_done:
                            listing.close_browser_graphql_page(browser_page)
                            browser_page = None
                            session_ready = False
                            restart_done = True
                            evidence.emit("browser_restart", page=page)
                        _, _, _, probe_ok, _, _ = fetch(page, pass_number, probe=True)
                        if budget.expired():
                            break
                        if probe_ok:
                            evidence.emit("recovered", page=page, reason="start_new_pass")
                            recovered = True
                            break
                    if not recovered:
                        raise CollectionIncomplete(budget.reason or "recovery_exhausted")
                    break
                accepted_rows = []
                for row in rows:
                    if row.get("container_type") == "organic_product":
                        sku = str(row["sku_id"])
                        position = int(row["organic_rank"])
                        if sku in seen:
                            first_page, first_position = seen[sku]
                            report["duplicates"].append(dict(sku_id=sku, first_page=first_page,
                                first_position=first_position, page=page, position=position))
                            continue
                        seen[sku] = (page, position)
                        organic += 1
                    accepted_rows.append(row)
                # The raw response/evidence retains every original position.
                rows = accepted_rows
                rows_by_page[page] = rows
                summary = listing.page_summary(page, rows, meta, graph)
                summary["source"] = "browser_graphql_verified"
                summaries.append(summary)
                raw.append({"page": page, "meta": meta, "summary": summary})
                report.update(organic_count=organic)
                # Empty documents must be a verified response, never a transport failure.
                if empty or (listing.LISTING_ORGANIC_TARGET and organic >= listing.LISTING_ORGANIC_TARGET):
                    complete = True
                    break
            else:
                complete = not listing.LISTING_ORGANIC_TARGET or organic >= listing.LISTING_ORGANIC_TARGET
                if not complete:
                    raise CollectionIncomplete("organic_target_not_reached_at_page_limit")
            if complete:
                evidence.emit("complete", pass_number=pass_number, organic_count=organic)
                report.update(status="validated", accepted_pass=pass_number, failed_page=None,
                              reason="verified_end" if empty else "target_reached")
                checkpoint()
                return rows_by_page, summaries, raw, report
        raise CollectionIncomplete("listing_pass_limit")
    except BaseException as exc:
        report.update(status="incomplete", reason=str(exc) or type(exc).__name__)
        evidence.emit("incomplete", page=report.get("failed_page"), reason=report["reason"])
        checkpoint()
        raise
    finally:
        listing.close_browser_graphql_page(browser_page)
