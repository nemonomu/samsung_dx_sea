"""Zero-cost audit for Walmart collection candidates from existing local files.

The script reads already captured JSON/CSV artifacts and writes:
  - walmart_internal_call_inventory.csv
  - walmart_url_string_inventory.csv
  - walmart_field_source_matrix.csv
  - summary.json

It does not read ZenRows API key files and does not make network calls.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_PROJECT_ROOT = Path(
    r"C:\Users\gomguard\Documents\퀵오일\삼성전자\samsung_dx_retail_com\samsung_dx_retail_com"
)
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+|/(?:orchestra|graphql|ip|reviews|search|browse|fulfillment|account)[^\s\"'<>\\]*", re.I)
HASH_RE = re.compile(r"\b[a-f0-9]{64}\b", re.I)
WALMART_HINT_RE = re.compile(r"walmart|graphql|operationName|sha256Hash|reviews|product|search|item", re.I)


EXCLUDED_FIELDS = {
    "shippin_info",
    "retailer_membership_discounts",
    "summarized_review_content",
    "top_mentions",
    "recommendation_intent",
    "rank_1",
    "rank_2",
    "promotion_position",
    "trend_rank",
    "promotion_type",
    "number_of_units_purchased_past_month",
}


FIELD_SOURCE_RULES: Dict[str, Tuple[str, str, str, str]] = {
    "batch_id": ("runtime", "server collection timestamp", "format from sample csv", "confirmed"),
    "account_name": ("runtime/config", "crawler account/retailer account name", "old code/config", "needs final confirmation"),
    "retailer": ("constant", "Walmart", "constant", "confirmed"),
    "category": ("constant/config", "TV category", "old crawler config", "confirmed"),
    "item": ("listing/detail url", "Walmart numeric item id", "URL /ip/.../{item}", "confirmed"),
    "product_url": ("listing", "canonical PDP URL", "listing item.productUrl/url", "confirmed"),
    "retailer_sku_name": ("listing/detail", "display product title", "__NEXT_DATA__ item/product name", "confirmed"),
    "retailer_sku_name_similar": ("detail", "similar item names only", "PDP recommendations/similar items", "needs source validation"),
    "brand": ("detail", "brand text", "PDP product metadata", "confirmed"),
    "model": ("detail", "model number if present", "PDP specs/product highlights", "needs validation"),
    "model_year": ("detail/spec", "model year if explicit", "specifications/title fallback only if explicit", "unresolved"),
    "screen_size": ("detail/parser", "screen size display value", "title/spec parser", "confirmed"),
    "final_sku_price": ("listing/detail", "current displayed price string", "priceInfo/currentPrice", "confirmed"),
    "original_sku_price": ("listing/detail", "was/list/comparable price string", "priceInfo/wasPrice/comparablePrice", "confirmed"),
    "discount": ("detail/listing", "price difference or displayed savings", "derived/display text", "needs exact rule"),
    "discount_type": ("detail", "text near price e.g. Price when purchased online", "PDP price support text", "needs source validation"),
    "offer": ("listing/detail", "leading number from free offers text", "addOnServices/serviceTitle or card text", "confirmed"),
    "seller": ("detail", "seller display name", "PDP seller info", "confirmed"),
    "shipping_fee": ("listing/detail", "shipping fee/free shipping text", "fulfillment badges", "confirmed"),
    "fastest_delivery": ("listing", "fastest shipping arrival text", "listing fulfillment text", "confirmed"),
    "delivery_availability": ("listing", "delivery availability text", "listing card text", "confirmed"),
    "pick_up_availability": ("listing", "pickup availability text", "listing card text", "confirmed"),
    "availability_quantity_for_purchase": ("listing only if explicit", "quantity if Walmart exposes it", "do not infer", "unresolved"),
    "inventory_status": ("listing only if explicit", "stock status badge", "listing card text", "needs validation"),
    "sku_status": ("listing", "listing badge such as Sponsored/Rollback", "listing badge only, not PDP condition", "confirmed"),
    "number_of_ppl_added_to_carts": ("listing/exact search", "cart social proof count", "badge text e.g. In 50+ people's carts", "confirmed"),
    "number_of_ppl_purchased_yesterday": ("listing", "bought since yesterday count", "badge text", "confirmed"),
    "star_rating": ("listing/review", "display-rounded rating", "averageRating/display rating", "confirmed"),
    "count_of_star_ratings": ("review/detail", "rating count, not review count", "rating count text/API", "confirmed"),
    "count_of_reviews": ("review/detail", "written review count", "review count text/API", "confirmed"),
    "detailed_review_content": ("review", "up to 20 review texts with delimiter", "review page p1/p2, delimiter ' ||| '", "confirmed"),
    "review_extracted_count": ("review/runtime", "number of reviews extracted", "parser count", "confirmed"),
    "review_pages_loaded": ("review/runtime", "review page count loaded", "parser/runtime", "confirmed"),
}


def safe_read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return None
    except Exception:
        return None


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: List[str] = []
        seen = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        fieldnames = keys
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def iter_json_files(project_root: Path) -> Iterable[Path]:
    log_dir = project_root / "log"
    for path in log_dir.rglob("*.json"):
        lower = str(path).lower()
        if "zenrows_doc" in lower or "siel_" in lower:
            continue
        if path.stat().st_size > 25 * 1024 * 1024:
            continue
        yield path


def join_path(parent: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    if not parent:
        return str(key)
    return f"{parent}.{key}"


def value_preview(value: Any, limit: int = 240) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def find_operation_dicts(obj: Any, source_file: Path, root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            operation = value.get("operationName") or value.get("operation")
            extensions = value.get("extensions") if isinstance(value.get("extensions"), dict) else {}
            persisted = extensions.get("persistedQuery") if isinstance(extensions.get("persistedQuery"), dict) else {}
            sha = persisted.get("sha256Hash") or value.get("sha256Hash")
            if operation or sha:
                variables = value.get("variables")
                rows.append(
                    {
                        "source_file": str(source_file.relative_to(root)),
                        "json_path": path or "$",
                        "operation_name": operation or "",
                        "sha256_hash": sha or "",
                        "variables_keys": ",".join(sorted(variables.keys())) if isinstance(variables, dict) else "",
                        "variables_preview": value_preview(variables),
                        "method": value.get("method") or "",
                        "status": value.get("status") or value.get("status_code") or "",
                        "url": value_preview(value.get("url") or value.get("requestUrl") or value.get("endpoint") or ""),
                    }
                )
            for k, v in value.items():
                walk(v, join_path(path, k))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                walk(item, join_path(path, idx))

    walk(obj, "")
    return rows


def find_url_strings(obj: Any, source_file: Path, root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, join_path(path, k))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                walk(item, join_path(path, idx))
        elif isinstance(value, str) and WALMART_HINT_RE.search(value):
            for match in URL_RE.finditer(value):
                url = match.group(0)
                if "amazon.com" in url.lower():
                    continue
                rows.append(
                    {
                        "source_file": str(source_file.relative_to(root)),
                        "json_path": path or "$",
                        "url_or_path": url[:500],
                        "has_hash": ",".join(sorted(set(HASH_RE.findall(value))))[:300],
                    }
                )

    walk(obj, "")
    return rows


def csv_stats(path: Path) -> Tuple[List[str], int, Dict[str, int], Dict[str, str]]:
    if not path.exists():
        return [], 0, {}, {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return [], 0, {}, {}
    fields = rows[0].keys() if rows else []
    filled = {field: 0 for field in fields}
    sample = {field: "" for field in fields}
    for row in rows:
        for field in fields:
            val = str(row.get(field) or "").strip()
            if val:
                filled[field] += 1
                if not sample[field]:
                    sample[field] = val[:240]
    return list(fields), len(rows), filled, sample


def build_field_matrix(project_root: Path) -> List[Dict[str, Any]]:
    detail_csv = project_root / "log" / "walmart_detail_review_batch_probe" / "detail_items.csv"
    listing_csv = project_root / "log" / "walmart_listing_300_probe" / "all_unique_items.csv"
    sample_csv = project_root / "log" / "tv_retail_com_202606051111.csv"
    detail_fields, detail_rows, detail_filled, detail_sample = csv_stats(detail_csv)
    listing_fields, listing_rows, listing_filled, listing_sample = csv_stats(listing_csv)
    sample_fields, sample_rows, _, _ = csv_stats(sample_csv)

    target_fields: List[str] = []
    for field in sample_fields or detail_fields:
        if field in EXCLUDED_FIELDS:
            continue
        if field not in target_fields:
            target_fields.append(field)
    for field in detail_fields:
        if field not in EXCLUDED_FIELDS and field not in target_fields:
            target_fields.append(field)

    rows: List[Dict[str, Any]] = []
    for field in target_fields:
        primary, expected, rule, status = FIELD_SOURCE_RULES.get(
            field,
            ("unmapped", "not reviewed yet", "needs manual mapping from sample/origin code", "unresolved"),
        )
        rows.append(
            {
                "field": field,
                "insert_target": "N" if field in EXCLUDED_FIELDS else "Y",
                "primary_source": primary,
                "expected_value": expected,
                "transform_rule": rule,
                "status": status,
                "present_in_sample_csv": "Y" if field in sample_fields else "N",
                "present_in_detail_output": "Y" if field in detail_fields else "N",
                "detail_rows": detail_rows if field in detail_fields else "",
                "detail_filled": detail_filled.get(field, ""),
                "detail_fill_rate": f"{detail_filled.get(field, 0) / detail_rows * 100:.1f}%" if detail_rows and field in detail_fields else "",
                "detail_sample_value": detail_sample.get(field, ""),
                "present_in_listing_output": "Y" if field in listing_fields else "N",
                "listing_rows": listing_rows if field in listing_fields else "",
                "listing_filled": listing_filled.get(field, ""),
                "listing_fill_rate": f"{listing_filled.get(field, 0) / listing_rows * 100:.1f}%" if listing_rows and field in listing_fields else "",
                "listing_sample_value": listing_sample.get(field, ""),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit local Walmart artifacts without network calls")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    out_dir = args.out_dir or project_root / "log" / "walmart_zero_cost_audit"
    out_dir.mkdir(parents=True, exist_ok=True)

    op_rows: List[Dict[str, Any]] = []
    url_rows: List[Dict[str, Any]] = []
    file_counter = Counter()
    parse_errors: List[Dict[str, str]] = []
    for path in iter_json_files(project_root):
        file_counter["json_files_seen"] += 1
        obj = safe_read_json(path)
        if obj is None:
            parse_errors.append({"source_file": str(path.relative_to(project_root)), "error": "json_parse_failed"})
            continue
        file_counter["json_files_parsed"] += 1
        ops = find_operation_dicts(obj, path, project_root)
        urls = find_url_strings(obj, path, project_root)
        op_rows.extend(ops)
        url_rows.extend(urls)

    # Deduplicate noisy repeated URL strings while preserving first source.
    seen_urls = set()
    dedup_url_rows = []
    for row in url_rows:
        key = (row["url_or_path"], row.get("has_hash", ""))
        if key in seen_urls:
            continue
        seen_urls.add(key)
        dedup_url_rows.append(row)

    op_fieldnames = [
        "source_file",
        "json_path",
        "operation_name",
        "sha256_hash",
        "variables_keys",
        "variables_preview",
        "method",
        "status",
        "url",
    ]
    write_csv(out_dir / "walmart_internal_call_inventory.csv", op_rows, op_fieldnames)
    write_csv(out_dir / "walmart_url_string_inventory.csv", dedup_url_rows, ["source_file", "json_path", "url_or_path", "has_hash"])
    field_matrix = build_field_matrix(project_root)
    write_csv(out_dir / "walmart_field_source_matrix.csv", field_matrix)

    by_operation = Counter(row["operation_name"] or "(missing)" for row in op_rows)
    by_hash = Counter(row["sha256_hash"] or "(missing)" for row in op_rows)
    endpoint_keywords = defaultdict(int)
    for row in dedup_url_rows:
        url = row["url_or_path"].lower()
        for key in ["graphql", "orchestra", "reviews", "search", "ip/", "fulfillment", "account"]:
            if key in url:
                endpoint_keywords[key] += 1

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": str(project_root),
        "out_dir": str(out_dir),
        "json_files_seen": file_counter["json_files_seen"],
        "json_files_parsed": file_counter["json_files_parsed"],
        "json_parse_errors": len(parse_errors),
        "operation_candidate_rows": len(op_rows),
        "unique_url_candidate_rows": len(dedup_url_rows),
        "top_operations": by_operation.most_common(20),
        "top_hashes": by_hash.most_common(20),
        "endpoint_keyword_counts": dict(sorted(endpoint_keywords.items())),
        "parse_errors_sample": parse_errors[:20],
        "outputs": {
            "internal_call_inventory": str(out_dir / "walmart_internal_call_inventory.csv"),
            "url_string_inventory": str(out_dir / "walmart_url_string_inventory.csv"),
            "field_source_matrix": str(out_dir / "walmart_field_source_matrix.csv"),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    strategy_rows = [
        {
            "stage": "listing_main_bsr",
            "candidate": "raw search page __NEXT_DATA__",
            "cost": "0",
            "current_result": "works when page HTML is reachable; local Chromium hits captcha at session start",
            "blocking_issue": "need non-rendered HTTP fetch success or search GraphQL variables",
            "next_action": "extract search/listing GraphQL variables from JS and saved seed captures; avoid browser full run",
        },
        {
            "stage": "listing_exact_search_enrichment",
            "candidate": "raw exact /search?q={item} __NEXT_DATA__",
            "cost": "0",
            "current_result": "works in existing captured raw; gives cart badge/social proof for some SKUs",
            "blocking_issue": "local browser access can trigger captcha",
            "next_action": "test simple HTTP HTML fetch and direct search GraphQL candidate before ZenRows",
        },
        {
            "stage": "detail_btf_enrichment",
            "candidate": "ItemByIdBtf persisted GraphQL",
            "cost": "0",
            "current_result": "confirmed 200 OK by direct urllib replay; data keys idml/contentLayout",
            "blocking_issue": "BTF is not full PDP; mainly similar/content modules/idml supplement",
            "next_action": "reuse as stable direct module for retailer_sku_name_similar and detail supplement fields",
        },
        {
            "stage": "detail_atf_core",
            "candidate": "ItemById or DynamicItemById persisted GraphQL",
            "cost": "0",
            "current_result": "hash known; DynamicItemById current minimal replay gets GraphQL fragment validation error",
            "blocking_issue": "full variables/query context not yet reproduced",
            "next_action": "recover exact variables from __NEXT_DATA__/SSR retry gate and test ItemById first",
        },
        {
            "stage": "review",
            "candidate": "ReviewsById persisted GraphQL",
            "cost": "0",
            "current_result": "hash/variables known; direct urllib replay currently 456 Forbidden",
            "blocking_issue": "requires browser/session headers or ce-gateway route context",
            "next_action": "test getReviewHistogramById and ReviewsById with reduced headers/session cookies; ZenRows only if still blocked",
        },
        {
            "stage": "review_counts",
            "candidate": "detail __NEXT_DATA__ reviews object or getReviewHistogramById",
            "cost": "0",
            "current_result": "counts available from captured __NEXT_DATA__; hash known for histogram query",
            "blocking_issue": "direct histogram variables not yet verified",
            "next_action": "probe histogram direct before full ReviewsById",
        },
        {
            "stage": "zip_location",
            "candidate": "cookie/location context + store finder GraphQL",
            "cost": "0",
            "current_result": "store finder for 11581 known; UI mutation unreliable",
            "blocking_issue": "Walmart PDP fulfillment may ignore injected locationContext",
            "next_action": "treat availability as listing-stage field; do not block detail on zip UI",
        },
        {
            "stage": "zenrows_fallback",
            "candidate": "Universal API json_response only for missing operation variables",
            "cost": "paid minimal",
            "current_result": "docs downloaded; API key not read by audit",
            "blocking_issue": "cost; should not be full-run engine",
            "next_action": "use only after zero-cost direct probes cannot recover search/review variables",
        },
    ]
    write_csv(out_dir / "walmart_zero_cost_strategy_matrix.csv", strategy_rows)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
