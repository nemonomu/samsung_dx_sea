from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List


DETAIL_FILE = "detail_items_probe_raw.csv"
REVIEW_FILE = "review_items_probe_raw.csv"


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: Iterable[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(fieldnames or [])
    if not fields:
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


def item_of(row: Dict[str, str]) -> str:
    return (row.get("item") or "").strip()


def load_rows_by_item(folder: Path, filename: str) -> Dict[str, Dict[str, str]]:
    rows = read_csv(folder / filename)
    by_item: Dict[str, Dict[str, str]] = {}
    for row in rows:
        item = item_of(row)
        if item and item not in by_item:
            by_item[item] = row
    return by_item


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--chunk-dir", type=Path, action="append", default=[])
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    base_dir = args.base_dir.resolve()
    folders = [base_dir] + [chunk.resolve() for chunk in args.chunk_dir]
    out_dir = (args.out_dir or base_dir).resolve()

    seed_rows = read_csv(base_dir / "all_unique_items.csv")
    seed_by_item = {item_of(row): row for row in seed_rows if item_of(row)}
    seed_items = list(seed_by_item.keys())

    detail_by_item: Dict[str, Dict[str, str]] = {}
    review_by_item: Dict[str, Dict[str, str]] = {}
    folder_stats: List[Dict[str, Any]] = []
    for folder in folders:
        detail_rows = load_rows_by_item(folder, DETAIL_FILE)
        review_rows = load_rows_by_item(folder, REVIEW_FILE)
        folder_stats.append({
            "folder": str(folder),
            "detail_rows": len(read_csv(folder / DETAIL_FILE)),
            "detail_unique_items": len(detail_rows),
            "review_rows": len(read_csv(folder / REVIEW_FILE)),
            "review_unique_items": len(review_rows),
        })
        for item, row in detail_rows.items():
            detail_by_item.setdefault(item, row)
        for item, row in review_rows.items():
            review_by_item.setdefault(item, row)

    missing_detail = [item for item in seed_items if item not in detail_by_item]
    missing_review = [item for item in seed_items if item not in review_by_item]
    missing_any = [item for item in seed_items if item not in detail_by_item or item not in review_by_item]

    missing_seed_rows = [seed_by_item[item] for item in missing_any if item in seed_by_item]
    write_csv(out_dir / "missing_seed.csv", missing_seed_rows, seed_rows[0].keys() if seed_rows else [])
    write_csv(out_dir / "missing_items.csv", [
        {
            "item": item,
            "missing_detail": "Y" if item in missing_detail else "",
            "missing_review": "Y" if item in missing_review else "",
            "product_url": seed_by_item.get(item, {}).get("product_url", ""),
            "main_rank": seed_by_item.get(item, {}).get("main_rank", ""),
            "bsr_rank": seed_by_item.get(item, {}).get("bsr_rank", ""),
        }
        for item in missing_any
    ], ["item", "missing_detail", "missing_review", "product_url", "main_rank", "bsr_rank"])

    detail_dupes = Counter()
    review_dupes = Counter()
    for folder in folders:
        detail_dupes.update(item_of(row) for row in read_csv(folder / DETAIL_FILE) if item_of(row))
        review_dupes.update(item_of(row) for row in read_csv(folder / REVIEW_FILE) if item_of(row))

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "base_dir": str(base_dir),
        "folders": [str(folder) for folder in folders],
        "seed_count": len(seed_items),
        "detail_unique_collected": len(detail_by_item),
        "review_unique_collected": len(review_by_item),
        "missing_detail_count": len(missing_detail),
        "missing_review_count": len(missing_review),
        "missing_any_count": len(missing_any),
        "missing_seed_csv": str(out_dir / "missing_seed.csv"),
        "missing_items_csv": str(out_dir / "missing_items.csv"),
        "folder_stats": folder_stats,
        "duplicate_detail_items": {k: v for k, v in detail_dupes.items() if v > 1},
        "duplicate_review_items": {k: v for k, v in review_dupes.items() if v > 1},
    }
    (out_dir / "chunk_recovery_audit.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not missing_any else 2


if __name__ == "__main__":
    raise SystemExit(main())
