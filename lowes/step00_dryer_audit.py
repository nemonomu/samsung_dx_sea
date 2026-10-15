"""Public dryer value provenance and offline checks; no settings/service access."""
from collections import Counter
import json
import math
import os
from pathlib import Path
import uuid

from .step00_dryer import (COLUMNS, classification_evidence, detail_product, normalized_key,
                           positive_id, price_combination_warnings)


class DryerAuditError(ValueError):
    def __init__(self, public_reason, differences=None):
        self.public_reason = public_reason
        self.differences = differences or []
        super().__init__(public_reason)


def raw_scalar(value):
    """Copy only public scalar fields, never stringify an unexpected payload object."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"invalid_number": "non_finite"}
    return {"invalid_type": "object" if isinstance(value, dict) else
            "array" if isinstance(value, list) else "unsupported"}


def projected_specs(product):
    result = []
    for name in ("specs", "productSpecsData", "productSpecs"):
        entries = product.get(name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = normalized_key(entry.get("key"))
            if key.startswith(("dryer load type", "dryer capacity", "washer dryer capacity",
                               "washer and dryer capacity")):
                result.append({"key": raw_scalar(entry.get("key")),
                               "value": raw_scalar(entry.get("value"))})
    return result


def project_listing(card):
    product = card["product"]
    location = card.get("location") if isinstance(card.get("location"), dict) else {}
    price = location.get("price") if isinstance(location.get("price"), dict) else {}
    return {**{key: raw_scalar(product.get(key)) for key in (
        "omniItemId", "modelId", "brand", "description", "pdURL", "rating", "reviewCount")},
        "price": {key: raw_scalar(price.get(key)) for key in ("sellingPrice", "wasPrice", "totalSaving")},
        "specs": projected_specs(product), "classification_evidence": classification_evidence(product)}


def project_detail(payload, item):
    if payload is None:
        return None
    product, node = detail_product(payload, item)
    mfe = node.get("mfePrice") if isinstance(node.get("mfePrice"), dict) else {}
    price = mfe.get("price") if isinstance(mfe.get("price"), dict) else {}
    additional = price.get("additionalData") if isinstance(price.get("additionalData"), dict) else {}
    savings = price.get("savings") if isinstance(price.get("savings"), dict) else {}
    return {**{key: raw_scalar(product.get(key)) for key in ("modelId", "brand", "description")},
        "price": {"sellingPrice": raw_scalar(additional.get("sellingPrice")),
                  "wasPrice": raw_scalar(additional.get("wasPrice")),
                  "totalSaving": raw_scalar(savings.get("totalSaving"))},
        "price_schema": "present" if isinstance(mfe.get("price"), dict) else "absent_or_invalid",
        "specs": projected_specs(product), "classification_evidence": classification_evidence(product)}


def project_reviews(payload):
    if payload is None:
        return None
    stats = payload.get("reviewStatistics")
    stats = stats if isinstance(stats, dict) else {}
    return {"averageOverallRating": raw_scalar(stats.get("averageOverallRating")),
            "totalReviewCount": raw_scalar(stats.get("totalReviewCount")),
            "totalResults": raw_scalar(payload.get("totalResults"))}


def api_summary(result):
    if result is None:
        return {"status": "not_requested", "attempts": 0, "retries": 0,
                "reason": "", "store": "", "ok": False}
    return {**result.summary(), "ok": result.ok}


def fallback_listing(source):
    return {"omniItemId": source.get("omni_item_id"), "modelId": source.get("model_id"),
            "brand": source.get("brand"), "description": source.get("description"),
            "pdURL": source.get("product_url"), "rating": source.get("rating"),
            "reviewCount": source.get("review_count"), "price": {
                "sellingPrice": source.get("selling_price"), "wasPrice": source.get("was_price"),
                "totalSaving": source.get("total_saving")},
            "specs": projected_specs(source.get("_listing_product", {}))}


def source_record(source, response, reviews, batch_id, status="detail_received", main=None, bsr=None):
    item = source["omni_item_id"]
    selected = source.get("_audit_listing") or fallback_listing(source)
    listing_type = source.get("selection_source") or source.get("page_type") or source.get("_list_type") or "main"
    detail = project_detail(response.payload, item) if response and response.ok else None
    return {"schema_version": 1, "item": item, "sku": source.get("model_id") or None,
            "batch_id": batch_id, "status": status, "selection_source": listing_type,
            "product_kind": source.get("kind"), "classification": source.get("_classification"),
            "region": {"main": main.get("_listing_region") if main else source.get("_listing_region") if listing_type == "main" else None,
                       "bsr": bsr.get("_listing_region") if bsr else source.get("_listing_region") if listing_type == "bsr" else None,
                       "productdetail": getattr(response, "region", None)},
            "source_values": {"listing": selected,
                "main": main.get("_audit_listing") if main else selected if listing_type == "main" else None,
                "bsr": bsr.get("_audit_listing") if bsr else selected if listing_type == "bsr" else None,
                "productdetail": detail, "reviews": project_reviews(reviews.payload) if reviews and reviews.ok else None},
            "occurrences": {name: {key: row.get(key) for key in ("page", "rank_in_page", "original_rank")}
                            if row else None for name, row in (("main", main), ("bsr", bsr))},
            "api": {"listing": source.get("_listing_api") or {"status": "unknown", "attempts": 0},
                    "main": main.get("_listing_api") if main else None,
                    "bsr": bsr.get("_listing_api") if bsr else None,
                    "productdetail": api_summary(response), "reviews": api_summary(reviews)},
            "final_values": None, "fields": None}


def present(value):
    return value not in (None, "")


def missing_reason(value, raw_candidates, result):
    if value is not None:
        return None
    invalid = any(present(raw) for raw in raw_candidates)
    failed = result is not None and not result.ok and result.attempts > 0
    if invalid and failed:
        return "invalid_value_and_collection_failure"
    if invalid:
        return "invalid_value"
    return "collection_failure" if failed else "source_absent"


def field(value, raw, source, transformations=(), kind="source", null_reason=None):
    return {"raw_value": raw, "value": value, "source": source, "kind": kind,
            "transformations": list(transformations), "null_reason": null_reason}


def value_record(source, row, audit, response, reviews, output, batch_id, crawl_dt,
                 status="provisional", main=None, bsr=None):
    record = source_record(source, response, reviews, batch_id, status, main, bsr)
    record.update(sku=output["sku"], final_values=dict(output))
    listing = record["source_values"]["listing"]
    detail = record["source_values"]["productdetail"] or {}
    review = record["source_values"]["reviews"] or {}
    fields = {}
    fields["id"] = field(output["id"], None, "database.id", kind="db_generated",
                         null_reason="generated_on_insert" if output["id"] is None else None)
    for column in ("country", "account_name"):
        fields[column] = field(output[column], None, "configuration", kind="fixed")
    fields["item"] = field(output["item"], listing.get("omniItemId"), "listing.omniItemId", ("numeric_id_to_text",))
    model_source = "listing.modelId" if source.get("model_id") else "productdetail.modelId"
    fields["sku"] = field(output["sku"], listing.get("modelId") if source.get("model_id") else detail.get("modelId"),
                          model_source, ("public_text", "keep_existing_model_on_conflict"),
                          null_reason=missing_reason(output["sku"], [listing.get("modelId"), detail.get("modelId")], response))
    brand_source = "listing.brand" if source.get("brand") else "productdetail.brand"
    existing_name = source.get("retailer_sku_name")
    name_source = "derived_listing.retailer_sku_name" if existing_name else (
        "listing.description" if source.get("description") else "productdetail.description")
    fields["retailer_sku_name"] = field(output["retailer_sku_name"], {
        "brand": listing.get("brand") if source.get("brand") else detail.get("brand"),
        "name": existing_name or (listing.get("description") if source.get("description") else detail.get("description")),
        "listing_description": listing.get("description")},
        {"brand": brand_source, "description": name_source},
        ("legacy_retailer_sku_name_text", "reuse_existing_name_if_present", "avoid_duplicate_brand_prefix"), kind="derived",
        null_reason="source_absent" if output["retailer_sku_name"] is None else None)
    fields["product_url"] = field(output["product_url"], listing.get("pdURL"), "listing.pdURL",
                                  ("absolute_lowes_url",), null_reason="source_absent" if output["product_url"] is None else None)
    for column, raw_key, listing_key, detail_key in (
        ("final_sku_price", "sellingPrice", "selling_price", "_detail_selling_price"),
        ("original_sku_price", "wasPrice", "was_price", "_detail_was_price"),
        ("savings", "totalSaving", "total_saving", "_detail_total_saving"),
    ):
        listing_value = row.get(listing_key) or row.get(column)
        use_listing = present(listing_value)
        raw_listing = listing.get("price", {}).get(raw_key)
        raw_detail = detail.get("price", {}).get(raw_key)
        selected = listing_value if use_listing else row.get(detail_key)
        reason = missing_reason(output[column], [raw_listing, raw_detail], response)
        transforms = ["listing_then_detail", "nonnegative_finite_number", "legacy_money_format"]
        if column != "final_sku_price" and output[column] is None and present(selected):
            reason = "original_equals_selling"
            transforms.append("remove_original_and_savings_when_prices_equal")
        fields[column] = field(output[column], raw_listing if use_listing else raw_detail,
                               ("listing" if use_listing else "productdetail") + ".price." + raw_key,
                               transforms, null_reason=reason)
    for column, raw_key, listing_key, detail_key in (
        ("star_rating", "averageOverallRating", "rating", "_pdp_average_rating"),
        ("count_of_reviews", "totalReviewCount", "reviewCount", "_pdp_total_reviews"),
        ("count_of_star_ratings", "totalReviewCount", "reviewCount", "_pdp_total_reviews"),
    ):
        use_review = present(row.get(detail_key))
        raw = review.get(raw_key) if use_review else listing.get(listing_key)
        selected_source = "reviews." + raw_key if use_review else "listing." + listing_key
        transforms = ["review_statistics_then_listing", "validate_rating_0_to_5_or_nonnegative_integer_count"]
        if use_review and raw is None and review.get("totalResults") in (0, "0"):
            raw, selected_source = review["totalResults"], "reviews.totalResults"
            transforms.append("explicit_zero_results_to_zero_statistic")
        if column == "star_rating":
            transforms.append("legacy_rating_format")
        if column == "count_of_star_ratings":
            transforms.append("same_as_count_of_reviews")
        fields[column] = field(output[column], raw, selected_source, transforms,
                               null_reason=missing_reason(output[column], [listing.get(listing_key), review.get(raw_key)], reviews))
    for column, occurrence in (("main_rank", "main"), ("bsr_rank", "bsr")):
        original = (record["occurrences"][occurrence] or {}).get("original_rank")
        fields[column] = field(output[column], original, occurrence + ".original_rank",
                               ("deduplicate_filter_and_rerank_from_1",), kind="derived",
                               null_reason="not_in_" + occurrence if output[column] is None else None)
    for column in ("batch_id", "crawl_datetime"):
        raw = batch_id if column == "batch_id" else crawl_dt
        fields[column] = field(output[column], raw, "run." + column, kind="run_metadata")
    fields["calendar_week"] = field(output["calendar_week"], crawl_dt, "run.crawl_datetime",
                                    ("ISO_week_wNN",), kind="derived")
    for column in ("loading_type", "capacity"):
        entries = [dict(entry) for entry in audit if entry["column"] == column]
        reason = row[column + "_reason"]
        selected = next((entry for entry in entries if entry.get("normalized") == output[column]
                         and output[column] is not None), None)
        selected_source = selected.get("source", "productdetail") if selected else "productdetail" if response.ok else "listing"
        null_reason = None
        if output[column] is None:
            null_reason = "collection_failure" if not response.ok else reason
        fields[column] = field(output[column], [{"key": entry.get("raw_key"), "value": entry.get("raw_value"),
                                                "normalized": entry.get("normalized"), "source": entry.get("source")}
                                               for entry in entries], selected_source + ".specs",
                               ("explicit_dryer_spec_only", "normalize_loading_or_capacity", "reject_shared_capacity"),
                               null_reason=null_reason)
        fields[column]["source_spec_reason"] = reason
    record["fields"] = {column: fields[column] for column in COLUMNS}
    record["price_checks"] = price_combination_warnings(output)
    return record


class AuditJournal:
    """Atomic snapshots keep one line per product, including interrupted processing."""
    def __init__(self, path):
        self.path, self.records = Path(path), {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.flush()

    def upsert(self, record):
        previous = self.records.get(record["item"], {})
        for section in ("source_values", "occurrences", "api", "region"):
            for listing_type in ("main", "bsr"):
                if record.get(section, {}).get(listing_type) is None and previous.get(section, {}).get(listing_type) is not None:
                    record.setdefault(section, {})[listing_type] = previous[section][listing_type]
        self.records[record["item"]] = record
        self.flush()

    def capture_listings(self, rows, batch_id, listing_type):
        seen = set()
        for source in rows:
            item = source["omni_item_id"]
            if item in seen:
                continue
            seen.add(item)
            if item not in self.records:
                selected = dict(source, page_type=listing_type, selection_source=listing_type)
                self.records[item] = source_record(selected, None, None, batch_id, "pending_selection")
            record = self.records[item]
            record["source_values"][listing_type] = source.get("_audit_listing") or fallback_listing(source)
            record["occurrences"][listing_type] = {key: source.get(key) for key in ("page", "rank_in_page", "original_rank")}
            record["api"][listing_type] = source.get("_listing_api")
            record.setdefault("region", {})[listing_type] = source.get("_listing_region")
        self.flush()

    def finish(self, rows):
        final_by_id = {row["item"]: row for row in rows}
        for item, record in self.records.items():
            record["selection_complete"] = True
            if item not in final_by_id:
                record.update(status="excluded", final_values=None, fields=None)
                record.setdefault("exclusion_reason", "outside_final_targets")
                continue
            output = final_by_id[item]
            record.update(status="final", final_values=dict(output), sku=output["sku"])
            for column in COLUMNS:
                record["fields"][column]["value"] = output[column]
                if column in {"main_rank", "bsr_rank"}:
                    record["fields"][column]["null_reason"] = ("not_in_main" if column == "main_rank" else "not_in_bsr") if output[column] is None else None
        self.flush()

    def flush(self):
        temporary = None
        try:
            # Create an owned temporary file beside the snapshot before replacement.
            temporary = self.path.parent / (".value_audit_" + uuid.uuid4().hex + ".tmp")
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                for record in self.records.values():
                    handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def csv_value(value):
    return None if value in (None, "") else str(value)


def difference(item, column, expected, actual, reason):
    return {"item": positive_id(item) or "-", "column": column, "expected": expected,
            "actual": actual, "reason": reason}


def read_records(path):
    records, seen, batches = [], set(), set()
    with Path(path).open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                raise DryerAuditError("invalid_audit_json_line", [{"line": line_number}]) from None
            if not isinstance(record, dict) or record.get("schema_version") != 1:
                raise DryerAuditError("invalid_audit_schema", [{"line": line_number}])
            item = positive_id(record.get("item"))
            if not item or item in seen:
                raise DryerAuditError("missing_or_duplicate_audit_item", [{"line": line_number, "item": item or "-"}])
            if record.get("status") not in {"pending_selection", "detail_received", "provisional", "final", "excluded"}:
                raise DryerAuditError("invalid_audit_status", [{"item": item}])
            if not record.get("batch_id"):
                raise DryerAuditError("missing_audit_batch", [{"item": item}])
            seen.add(item)
            batches.add(record["batch_id"])
            if record["status"] in {"final", "provisional"}:
                output, fields = record.get("final_values"), record.get("fields")
                if not isinstance(output, dict) or not isinstance(fields, dict) or set(output) != set(COLUMNS) or set(fields) != set(COLUMNS):
                    raise DryerAuditError("expected20_audit_columns", [{"item": item}])
                for column in COLUMNS:
                    if not (output[column] is None or isinstance(output[column], (str, int, float))):
                        raise DryerAuditError("invalid_audit_final_scalar", [{"item": item, "column": column}])
                    if isinstance(output[column], float) and not math.isfinite(output[column]):
                        raise DryerAuditError("invalid_audit_final_scalar", [{"item": item, "column": column}])
                    if not isinstance(fields[column], dict) or csv_value(output[column]) != csv_value(fields[column].get("value")):
                        raise DryerAuditError("audit_field_final_mismatch", [difference(item, column, csv_value(output[column]), csv_value(fields[column].get("value")) if isinstance(fields[column], dict) else None, "field_vs_final")])
                    if output[column] is None and not fields[column].get("null_reason"):
                        raise DryerAuditError("missing_audit_null_reason", [{"item": item, "column": column}])
                if positive_id(output["item"]) != item or output["batch_id"] != record["batch_id"]:
                    raise DryerAuditError("audit_identity_mismatch", [{"item": item}])
            records.append(record)
    if len(batches) > 1:
        raise DryerAuditError("mixed_audit_batches")
    return records


def audit_summary(records):
    statuses = dict(Counter(record["status"] for record in records))
    null_reasons = {column: dict(Counter(record["fields"][column]["null_reason"] for record in records
                                      if record.get("fields") and record["fields"][column]["value"] is None))
                    for column in COLUMNS}
    return {"products": len(records), "statuses": statuses,
            "final_values_complete": bool(records) and (statuses.get("final", 0) > 0 or all(record.get("selection_complete") for record in records)) and all(record["status"] in {"final", "excluded"} for record in records),
            "null_reasons": null_reasons}


def compare_csv(records, rows):
    final = {record["item"]: record for record in records if record["status"] == "final"}
    csv_rows, differences = {}, []
    for row in rows:
        item = str(row["item"])
        if item in csv_rows:
            differences.append(difference(item, "item", "one_row", "duplicate_row", "duplicate_csv_product"))
        csv_rows.setdefault(item, row)
    for item in sorted(set(final) | set(csv_rows)):
        if item not in final or item not in csv_rows:
            differences.append(difference(item, "item", "present_in_both", "missing_in_audit" if item not in final else "missing_in_csv", "product_set_mismatch"))
            continue
        for column in COLUMNS:
            expected, actual = csv_value(final[item]["final_values"][column]), csv_value(csv_rows[item][column])
            if expected != actual:
                differences.append(difference(item, column, expected, actual, "audit_vs_csv"))
    if any(record["status"] not in {"final", "excluded"} for record in records):
        raise DryerAuditError("audit_has_unfinished_products", differences)
    if differences:
        raise DryerAuditError("audit_csv_mismatch", differences)
    return {"audit_csv_match": True, "compared_columns": list(COLUMNS), "products": len(rows)}



class ExclusionJournal(AuditJournal):
    """Separate, atomic exclusion evidence; also accepts collection cards without IDs."""
    def capture(self, source, batch_id, response=None):
        item = positive_id(source.get("omni_item_id")) or None
        key = item or source.get("product_url")
        classification = source.get("_classification") or {}
        previous = self.records.get(key, {})
        record = {"item": item, "sku": source.get("model_id") or None,
                  "product_url": source.get("product_url"), "batch_id": batch_id,
                  "reason": classification.get("reason", "product_kind_unconfirmed"),
                  "kind": source.get("kind"), "classification": classification,
                  "occurrences": dict(previous.get("occurrences", {})),
                  "api": dict(previous.get("api", {}))}
        listing_type = source.get("_list_type", "main")
        record["occurrences"][listing_type] = {k: source.get(k) for k in ("page", "rank_in_page", "original_rank")}
        record["api"][listing_type] = source.get("_listing_api")
        if response is not None:
            record["api"]["productdetail"] = api_summary(response)
        elif previous.get("api", {}).get("productdetail"):
            record["api"]["productdetail"] = previous["api"]["productdetail"]
        self.records[key] = record
        self.flush()


def region_summary(records):
    counts = Counter()
    results = []
    for record in records:
        if record["status"] != "final":
            continue
        region = record.get("region") or {}
        detail = region.get("productdetail")
        status = (detail or {}).get("status", "unverified")
        counts[status] += 1
        results.append({"item": record["item"], "sku": record.get("sku"), "status": status,
                        "reason": (detail or {}).get("reason", "no_detail_region_evidence")})
    return {"detail_delivery_zip_status": dict(counts), "price_region_verified": False,
            "results": results}
