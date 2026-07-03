import csv
import json
import os
from datetime import datetime
from pathlib import Path

from .step00_config import DEFAULT_LOWES_RUN_ROOT
from .step00_erd_schema import retailer_sku_name_text


RUN_DATE = os.getenv("LOWES_RUN_DATE", datetime.now().strftime("%Y%m%d"))
RUN_ID = os.getenv("LOWES_MAIN_RUN_ID", "main")
RUN_ROOT = Path(os.getenv("LOWES_RUN_ROOT", str(DEFAULT_LOWES_RUN_ROOT))) / RUN_ID
INPUT_CSV = Path(os.getenv("LOWES_MAIN_TARGET_INPUT", RUN_ROOT / "parsed" / "main_occurrences.csv"))
OUTPUT_CSV = Path(os.getenv("LOWES_MAIN_TARGET_OUTPUT", RUN_ROOT / "parsed" / "main_target_occurrences.csv"))
TARGET_LIMIT = int(os.getenv("LOWES_MAIN_TARGET_LIMIT", "300"))


def read_csv(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def numeric(value, fallback=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def truthy(v):
    return v not in ("", None, "0", "False", False, "[]", "{}")


def money(v):
    if not truthy(v):
        return ""
    try:
        f = float(v)
        return f"${f:,.2f}" if not f.is_integer() else f"${int(f):,}"
    except (TypeError, ValueError):
        return str(v)


def parse_badges(blob):
    if not truthy(blob):
        return ""
    try:
        arr = json.loads(blob) if isinstance(blob, str) else blob
        if not isinstance(arr, list):
            return ""
        names = [b.get("badgeName", "").replace("_", " ") for b in arr if isinstance(b, dict) and b.get("badgeName")]
        return " | ".join(names)
    except Exception:
        return ""


def is_collection_target(row):
    """Collection cards (e.g. GR_18384) are not individual SKUs — detail XHR returns 403.
    Identifier: product_url contains '/collections/'. Confirmed via LDY 2026-05-30 run.
    """
    url = str(row.get("product_url") or "")
    return "/collections/" in url


def select_targets(rows):
    """Dedup + re-rank main_rank 1~LIMIT + parse badges + format prices + map spec columns."""
    seen = set()
    output = []
    skipped_collection = 0
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            numeric(row.get("page"), 10**9),
            numeric(row.get("rank_in_page"), 10**9),
        ),
    )
    for row in sorted_rows:
        product_id = (row.get("omni_item_id") or row.get("item_number") or row.get("product_url") or "").strip()
        if not product_id or product_id in seen:
            continue
        if is_collection_target(row):
            seen.add(product_id)
            skipped_collection += 1
            continue
        seen.add(product_id)
        new_rank = len(output) + 1
        out = dict(row)
        out["main_rank"] = new_rank
        out["target_rank"] = new_rank
        out["page_type"] = "main"
        out["selection_source"] = "main"
        # spec columns mapping
        out["retailer_sku_name"] = retailer_sku_name_text(row)
        out["final_sku_price"] = money(row.get("selling_price", ""))
        out["original_sku_price"] = money(row.get("was_price", ""))
        out["savings"] = money(row.get("total_saving", ""))
        out["star_rating"] = row.get("rating", "")
        out["count_of_reviews"] = row.get("review_count", "")
        out["count_of_star_ratings"] = row.get("review_count", "")
        out["discount_type"] = row.get("promotion_labels", "")
        out["sku_popularity"] = parse_badges(row.get("location.badge.badges", ""))
        out["sku_status"] = "Sponsored" if str(row.get("sponsored", "")).lower() == "true" else ""
        output.append(out)
        if len(output) >= TARGET_LIMIT:
            break
    return output, skipped_collection


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = []
    seen = set()
    preferred = [
        "page_type",
        "main_rank",
        "target_rank",
        "selection_source",
        "page",
        "rank_in_page",
        "omni_item_id",
        "item_number",
        "brand",
        "model_id",
        "retailer_sku_name",
        "product_url",
        "final_sku_price",
        "original_sku_price",
        "savings",
        "star_rating",
        "count_of_reviews",
        "count_of_star_ratings",
        "discount_type",
        "sku_popularity",
        "sku_status",
        "description",
        "rating",
        "review_count",
        "selling_price",
        "sponsored",
    ]
    for key in preferred:
        if any(key in row for row in rows):
            fieldnames.append(key)
            seen.add(key)
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    started = datetime.now().isoformat(timespec="seconds")
    rows = read_csv(INPUT_CSV)
    targets, skipped_collection = select_targets(rows)
    write_csv(OUTPUT_CSV, targets)
    manifest = {
        "run_type": "step02_main_targets",
        "run_date": RUN_DATE,
        "run_root": str(RUN_ROOT),
        "input_csv": str(INPUT_CSV),
        "output_csv": str(OUTPUT_CSV),
        "input_rows": len(rows),
        "target_limit": TARGET_LIMIT,
        "output_rows": len(targets),
        "skipped_collection_urls": skipped_collection,
        "started_at": started,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    manifest_path = RUN_ROOT / "manifest_main_targets.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
