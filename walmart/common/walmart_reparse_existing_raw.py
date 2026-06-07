from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List


DEFAULT_PROJECT_ROOT = Path(r"C:\Users\gomguard\Documents\퀵오일\삼성전자\samsung_dx_retail_com\samsung_dx_retail_com")
DEFAULT_LISTING_RAW = DEFAULT_PROJECT_ROOT / "log" / "walmart_neo_listing_full_20260606_step5_300check" / "raw" / "listing"
DEFAULT_DETAIL_LOG_GLOB = "walmart_neo_detail_review_*"
EXPECTED_DETAIL_FILES = ("detail_next_data.json",)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: List[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        if not fields:
            fh.write("")
            return
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def walk(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def item_objects(next_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    search = (((next_data.get("props") or {}).get("pageProps") or {}).get("initialData") or {}).get("searchResult") or {}
    out: Dict[str, Dict[str, Any]] = {}
    for stack in search.get("itemStacks") or []:
        if not isinstance(stack, dict):
            continue
        for item in stack.get("items") or []:
            if isinstance(item, dict) and item.get("usItemId"):
                out[str(item["usItemId"])] = item
    return out


def text_fragments(value: Any) -> List[str]:
    out: List[str] = []
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        for key in ("text", "label", "value", "slaText", "flag"):
            raw = node.get(key)
            if isinstance(raw, str) and raw.strip():
                out.append(re.sub(r"\s+", " ", raw).strip())
    return out


def only_left_quantity(item: Dict[str, Any]) -> str:
    for value in text_fragments(item):
        match = re.search(r"\bOnly\s+([\d,]+)\s+left\b", value, re.I)
        if match:
            return match.group(1).replace(",", "")
    return ""


def normalize_screen_size(value: Any) -> Any:
    value = str(value or "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:inches|inch|in\b|\")", value, re.I)
    return f"{match.group(1)} inches" if match else value


def infer_screen_size_from_text(*values: Any) -> str:
    for value in values:
        text = str(value or "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:inches|inch|in\b|\")", text, re.I)
        if match:
            return f"{match.group(1)} inches"
    return ""


def clean_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    if str(row.get("discount_type") or "").upper() == "UNKNOWN":
        row["discount_type"] = ""
    if row.get("screen_size"):
        row["screen_size"] = normalize_screen_size(row["screen_size"])
    return row


def item_from_url(value: Any) -> str:
    match = re.search(r"/(?:ip|reviews/product)/(?:[^/?#]+/)?(\d+)(?:[/?#]|$)", str(value or ""))
    return match.group(1) if match else ""


def add_meta(row: Dict[str, Any], crawl_datetime: str, batch_id: str) -> Dict[str, Any]:
    row = dict(row)
    row.setdefault("calendar_week", "w23")
    row["crawl_datetime"] = crawl_datetime
    row["account_name"] = "Walmart"
    row["batch_id"] = batch_id
    row["country"] = "SEA"
    if not row.get("screen_size"):
        row["screen_size"] = infer_screen_size_from_text(row.get("retailer_sku_name"), row.get("product_url"))
    return row


def apply_listing_context(row: Dict[str, Any], seed_row: Dict[str, Any]) -> Dict[str, Any]:
    for field in [
        "rank", "page_type", "product_id", "product_url", "review_url", "sku_popularity",
        "inventory_status", "sku_status", "pick_up_availability", "fastest_delivery",
        "delivery_availability", "available_quantity_for_purchase",
        "number_of_ppl_purchased_yesterday", "number_of_ppl_added_to_carts", "offer",
        "discount_type", "page_number", "page_url", "page_rank", "main_rank",
        "main_page_number", "unique_rank", "bsr_rank", "bsr_page_number",
    ]:
        value = seed_row.get(field)
        if value in (None, ""):
            continue
        if field in {"page_type", "inventory_status", "sku_status", "pick_up_availability",
                     "fastest_delivery", "delivery_availability", "available_quantity_for_purchase",
                     "number_of_ppl_purchased_yesterday", "number_of_ppl_added_to_carts"}:
            row[field] = value
        elif row.get(field) in (None, ""):
            row[field] = value
    return row


def apply_detail_context(row: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    for field in [
        "star_rating", "count_of_star_ratings", "count_of_reviews", "final_sku_price",
        "original_sku_price", "savings", "discount_type", "sku_popularity",
        "model_year", "screen_size", "retailer_sku_name_similar", "offer",
    ]:
        if row.get(field) in (None, "") and detail.get(field) not in (None, ""):
            row[field] = detail[field]
    return row


def similar_names_from_btf(data: Dict[str, Any], limit: int) -> List[str]:
    names: List[str] = []
    modules = (data.get("data") or {}).get("contentLayout", {}).get("modules") or []
    for module in modules:
        if not isinstance(module, dict) or module.get("type") != "ItemCarousel":
            continue
        cfg = module.get("configs") or {}
        if str(cfg.get("title") or "").strip().lower() != "similar items you might like":
            continue
        for product in cfg.get("products") or []:
            if not isinstance(product, dict):
                continue
            name = str(product.get("name") or product.get("productName") or "").strip()
            if name and name not in names:
                names.append(name)
            if len(names) >= limit:
                return names
    return names


def parse_listing(project_root: Path, listing_raw_dir: Path, out_dir: Path) -> List[Dict[str, Any]]:
    from walmart_json_parser import search_items

    all_rows: List[Dict[str, Any]] = []
    for page_type in ("main", "bsr"):
        rows: List[Dict[str, Any]] = []
        for page_path in sorted((listing_raw_dir / page_type).glob("page_*_next_data.json")):
            page_match = re.search(r"page_(\d+)", page_path.name)
            page_number = int(page_match.group(1)) if page_match else 0
            next_data = read_json(page_path)
            by_item = item_objects(next_data)
            parsed = search_items(next_data)
            for local_rank, row in enumerate(parsed, 1):
                row = clean_row(row)
                item = str(row.get("item") or item_from_url(row.get("product_url")))
                row["item"] = item
                row["page_type"] = page_type
                row["page_number"] = page_number
                row["page_rank"] = local_rank
                row["rank"] = len(rows) + 1
                if page_type == "main":
                    row["main_rank"] = row["rank"]
                    row["main_page_number"] = page_number
                else:
                    row["bsr_rank"] = row["rank"]
                    row["bsr_page_number"] = page_number
                source = by_item.get(item)
                if source:
                    qty = only_left_quantity(source)
                    if qty:
                        row["available_quantity_for_purchase"] = qty
                rows.append(row)
        seen: set[str] = set()
        unique: List[Dict[str, Any]] = []
        for row in rows:
            item = str(row.get("item") or "")
            if item and item not in seen:
                seen.add(item)
                unique.append(row)
        write_csv(out_dir / f"{page_type}_items.csv", unique)
        all_rows.extend(unique)
    write_csv(out_dir / "all_items.csv", all_rows)
    seen_all: set[str] = set()
    all_unique: List[Dict[str, Any]] = []
    for row in all_rows:
        item = str(row.get("item") or "")
        if item and item not in seen_all:
            seen_all.add(item)
            row = dict(row)
            row["unique_rank"] = len(all_unique) + 1
            all_unique.append(row)
    write_csv(out_dir / "all_unique_items.csv", all_unique)
    return all_unique


def discover_detail_dirs(project_root: Path) -> Dict[str, Path]:
    by_item: Dict[str, Path] = {}
    for base in sorted((project_root / "log").glob(DEFAULT_DETAIL_LOG_GLOB)):
        raw = base / "raw" / "detail_review"
        if not raw.exists():
            continue
        for detail_path in raw.glob("*/detail_next_data.json"):
            item = detail_path.parent.name
            current = by_item.get(item)
            if current is None or detail_path.stat().st_mtime > current.stat().st_mtime:
                by_item[item] = detail_path.parent
    return by_item


def parse_detail_review(project_root: Path, seeds: List[Dict[str, Any]], out_dir: Path, max_reviews: int, similar_limit: int) -> Dict[str, Any]:
    from walmart_json_parser import detail_row, review_collection_row

    raw_by_item = discover_detail_dirs(project_root)
    detail_rows: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    detail_by_item: Dict[str, Dict[str, Any]] = {}
    now = datetime.now()
    crawl_datetime = now.strftime("%Y-%m-%d %H:%M:%S")
    batch_id = now.strftime("w_%Y%m%d_%H%M%S")

    for offset, seed in enumerate(seeds, 1):
        item = str(seed.get("item") or item_from_url(seed.get("product_url")))
        raw_dir = raw_by_item.get(item)
        if not raw_dir:
            failures.append({"item": item, "stage": "raw_discovery", "error": "detail raw missing"})
            continue
        try:
            drow = clean_row(detail_row(read_json(raw_dir / "detail_next_data.json"), max_reviews))
            apply_listing_context(drow, seed)
            btf_path = raw_dir / "btf_response.json"
            if btf_path.exists():
                names = similar_names_from_btf(read_json(btf_path), similar_limit)
                if names:
                    drow["retailer_sku_name_similar"] = " ||| ".join(names)
            drow["seed_index"] = offset
            drow["seed_product_url"] = seed.get("product_url", "")
            drow = add_meta(drow, crawl_datetime, batch_id)
            detail_rows.append(drow)
            detail_by_item[item] = drow
        except Exception as exc:
            failures.append({"item": item, "stage": "detail_parse", "error": str(exc)})
            continue

        review_paths = sorted(raw_dir.glob("review_p*_next_data.json"))
        if review_paths:
            try:
                rrow = clean_row(review_collection_row([read_json(path) for path in review_paths], max_reviews))
                apply_listing_context(rrow, seed)
                apply_detail_context(rrow, detail_by_item.get(item, {}))
                rrow["seed_index"] = offset
                rrow["seed_product_url"] = seed.get("product_url", "")
                rrow = add_meta(rrow, crawl_datetime, batch_id)
                review_rows.append(rrow)
            except Exception as exc:
                failures.append({"item": item, "stage": "review_parse", "error": str(exc)})

    write_csv(out_dir / "detail_items.csv", detail_rows)
    write_csv(out_dir / "review_items.csv", review_rows)
    write_csv(out_dir / "db_insert_detail_items.csv", detail_rows)
    write_csv(out_dir / "db_insert_review_items.csv", review_rows)
    write_json(out_dir / "detail_review_summary.json", {"detail_rows": len(detail_rows), "review_rows": len(review_rows), "failures": failures})
    return {"detail_rows": len(detail_rows), "review_rows": len(review_rows), "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--listing-raw-dir", type=Path, default=DEFAULT_LISTING_RAW)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-reviews", type=int, default=20)
    parser.add_argument("--similar-limit", type=int, default=20)
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    sys.path.insert(0, str(project_root))
    sys.path.insert(0, str(project_root / "walmart_neo"))
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now()
    listing_rows = parse_listing(project_root, args.listing_raw_dir, out_dir)
    detail_summary = parse_detail_review(project_root, listing_rows, out_dir, args.max_reviews, args.similar_limit)
    summary = {
        "created_at": started.isoformat(timespec="seconds"),
        "out_dir": str(out_dir),
        "listing_rows": len(listing_rows),
        "detail_review": detail_summary,
    }
    write_json(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not detail_summary["failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
