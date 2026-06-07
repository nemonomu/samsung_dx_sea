"""Parse __NEXT_DATA__ from raw HTTP Walmart HTML files using existing parser."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_PROJECT_ROOT = Path(
    r"C:\Users\gomguard\Documents\퀵오일\삼성전자\samsung_dx_retail_com\samsung_dx_retail_com"
)
NEXT_RE = re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)


def load_next_data(path: Path) -> Optional[Dict[str, Any]]:
    html = path.read_text(encoding="utf-8", errors="replace")
    match = NEXT_RE.search(html)
    if not match:
        return None
    return json.loads(match.group(1))


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: List[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse raw HTTP Walmart HTML files")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--max-reviews", type=int, default=20)
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    sys.path.insert(0, str(project_root))
    from walmart_json_parser import detail_row, review_collection_row, search_items  # type: ignore

    raw_dir = args.raw_dir or project_root / "log" / "walmart_raw_http_probe" / "raw"
    out_dir = args.out_dir or project_root / "log" / "walmart_raw_http_probe"
    out_dir.mkdir(parents=True, exist_ok=True)

    search_rows: List[Dict[str, Any]] = []
    detail_rows: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for path in sorted(raw_dir.glob("*.html")):
        kind = path.name.split("_", 1)[0]
        try:
            next_data = load_next_data(path)
            if not next_data:
                errors.append({"path": str(path), "error": "missing_next_data"})
                continue
            if kind == "search":
                for idx, row in enumerate(search_items(next_data), 1):
                    row = dict(row)
                    row["source_raw_html"] = str(path)
                    row["raw_rank"] = idx
                    search_rows.append(row)
            elif kind == "detail":
                row = detail_row(next_data, max_reviews=args.max_reviews)
                row["source_raw_html"] = str(path)
                detail_rows.append(row)
            elif kind == "review":
                row = review_collection_row([next_data], max_reviews=args.max_reviews)
                row["source_raw_html"] = str(path)
                review_rows.append(row)
        except Exception as exc:
            errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})

    write_csv(out_dir / "parsed_search_items.csv", search_rows)
    write_csv(out_dir / "parsed_detail_items.csv", detail_rows)
    write_csv(out_dir / "parsed_review_items.csv", review_rows)
    (out_dir / "parse_summary.json").write_text(
        json.dumps(
            {
                "raw_dir": str(raw_dir),
                "search_rows": len(search_rows),
                "detail_rows": len(detail_rows),
                "review_rows": len(review_rows),
                "errors": errors[:50],
                "error_count": len(errors),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "search_rows": len(search_rows),
                "detail_rows": len(detail_rows),
                "review_rows": len(review_rows),
                "error_count": len(errors),
                "out_dir": str(out_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
