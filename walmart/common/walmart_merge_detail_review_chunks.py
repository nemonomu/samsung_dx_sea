from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List


DETAIL_FILE = "detail_items_probe_raw.csv"
REVIEW_FILE = "review_items_probe_raw.csv"
LISTING_FILES = [
    "all_items.csv",
    "all_unique_items.csv",
    "all_rejected.csv",
    "main_items.csv",
    "main_rejected.csv",
    "bsr_items.csv",
    "bsr_rejected.csv",
    "main_items.json",
    "bsr_items.json",
]


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


def collect_by_item(folders: List[Path], filename: str) -> Dict[str, Dict[str, str]]:
    by_item: Dict[str, Dict[str, str]] = {}
    for folder in folders:
        for row in read_csv(folder / filename):
            item = item_of(row)
            if item:
                by_item[item] = row
    return by_item


def ordered_rows(seed_rows: List[Dict[str, str]], by_item: Dict[str, Dict[str, str]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    seen: set[str] = set()
    for seed in seed_rows:
        item = item_of(seed)
        if item and item in by_item and item not in seen:
            rows.append(by_item[item])
            seen.add(item)
    return rows


def copy_listing_files(base_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename in LISTING_FILES:
        source = base_dir / filename
        if source.exists():
            shutil.copy2(source, out_dir / filename)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--chunk-dir", type=Path, action="append", default=[])
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    base_dir = args.base_dir.resolve()
    folders = [base_dir] + [chunk.resolve() for chunk in args.chunk_dir]
    out_dir = args.out_dir.resolve()
    seed_rows = read_csv(base_dir / "all_unique_items.csv")
    seed_items = [item_of(row) for row in seed_rows if item_of(row)]

    copy_listing_files(base_dir, out_dir)
    detail_by_item = collect_by_item(folders, DETAIL_FILE)
    review_by_item = collect_by_item(folders, REVIEW_FILE)
    detail_rows = ordered_rows(seed_rows, detail_by_item)
    review_rows = ordered_rows(seed_rows, review_by_item)

    write_csv(out_dir / DETAIL_FILE, detail_rows)
    write_csv(out_dir / REVIEW_FILE, review_rows)
    write_csv(out_dir / "db_insert_detail_items.csv", detail_rows)
    write_csv(out_dir / "db_insert_review_items.csv", review_rows)

    missing_detail = [item for item in seed_items if item not in detail_by_item]
    missing_review = [item for item in seed_items if item not in review_by_item]
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "base_dir": str(base_dir),
        "folders": [str(folder) for folder in folders],
        "out_dir": str(out_dir),
        "seed_count": len(seed_items),
        "detail_rows": len(detail_rows),
        "review_rows": len(review_rows),
        "missing_detail_count": len(missing_detail),
        "missing_review_count": len(missing_review),
        "missing_detail": missing_detail,
        "missing_review": missing_review,
        "detail_file": str(out_dir / DETAIL_FILE),
        "review_file": str(out_dir / REVIEW_FILE),
        "db_insert_detail_file": str(out_dir / "db_insert_detail_items.csv"),
        "db_insert_review_file": str(out_dir / "db_insert_review_items.csv"),
    }
    (out_dir / "merge_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not missing_detail and not missing_review else 2


if __name__ == "__main__":
    raise SystemExit(main())
