"""Run Walmart full probe pipeline and generate a timing PDF report.

This runner intentionally orchestrates the existing probe scripts instead of
changing their collection logic.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_PROJECT_ROOT = Path(
    r"C:\Users\gomguard\Documents\퀵오일\삼성전자\samsung_dx_retail_com\samsung_dx_retail_com"
)


@dataclass
class StageResult:
    name: str
    command: List[str]
    started_at: str
    ended_at: str
    elapsed_sec: float
    returncode: int
    log_path: str


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fmt_duration(seconds: float) -> str:
    seconds = max(0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h:
        return f"{h}h {m:02d}m {s:04.1f}s"
    if m:
        return f"{m}m {s:04.1f}s"
    return f"{s:.1f}s"


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_read_error": str(exc)}


def csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception:
        return 0


def csv_header(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            return next(reader, [])
    except Exception:
        return []


def fill_stats(path: Path, fields: Iterable[str]) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return []
    total = len(rows)
    out = []
    for field in fields:
        if total == 0 or (rows and field not in rows[0]):
            out.append({"field": field, "filled": 0, "blank": total, "fill_rate": "N/A"})
            continue
        filled = sum(1 for row in rows if str(row.get(field) or "").strip())
        out.append(
            {
                "field": field,
                "filled": filled,
                "blank": total - filled,
                "fill_rate": f"{(filled / total * 100):.1f}%" if total else "N/A",
            }
        )
    return out


def run_stage(
    *,
    name: str,
    command: List[str],
    cwd: Path,
    log_dir: Path,
    env: Dict[str, str],
) -> StageResult:
    log_path = log_dir / f"{name}.log"
    started = iso_now()
    start = time.perf_counter()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"$ {' '.join(command)}\n")
        log.write(f"cwd={cwd}\nstarted_at={started}\n\n")
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
        returncode = proc.wait()
        ended = iso_now()
        elapsed = time.perf_counter() - start
        log.write(f"\nended_at={ended}\nreturncode={returncode}\nelapsed_sec={elapsed:.3f}\n")
    return StageResult(
        name=name,
        command=command,
        started_at=started,
        ended_at=ended,
        elapsed_sec=elapsed,
        returncode=returncode,
        log_path=str(log_path),
    )


def find_chrome() -> Optional[Path]:
    env_path = os.getenv("CHROME_PATH")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.getenv("LocalAppData", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Microsoft/Edge/Application/msedge.exe",
        ]
    )
    for path in candidates:
        if path and path.exists():
            return path
    return None


def render_html_report(
    *,
    report_path: Path,
    project_root: Path,
    run_id: str,
    zip_code: str,
    started_at: str,
    ended_at: str,
    total_elapsed: float,
    stages: List[StageResult],
    metrics: Dict[str, Any],
) -> None:
    def table(rows: List[List[Any]], header: List[str]) -> str:
        th = "".join(f"<th>{html.escape(str(x))}</th>" for x in header)
        trs = [f"<tr>{th}</tr>"]
        for row in rows:
            tds = "".join(f"<td>{html.escape(str(x))}</td>" for x in row)
            trs.append(f"<tr>{tds}</tr>")
        return "<table>" + "\n".join(trs) + "</table>"

    stage_rows = [
        [
            s.name,
            fmt_duration(s.elapsed_sec),
            s.returncode,
            s.started_at,
            s.ended_at,
            Path(s.log_path).name,
        ]
        for s in stages
    ]
    metric_rows = [[k, v] for k, v in metrics.get("counts", {}).items()]
    fill_rows = [
        [x["field"], x["filled"], x["blank"], x["fill_rate"]]
        for x in metrics.get("detail_fill_stats", [])
    ]
    files_rows = [[label, path] for label, path in metrics.get("files", {}).items()]

    body = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Walmart Full Run Report {html.escape(run_id)}</title>
  <style>
    body {{ font-family: Arial, 'Malgun Gothic', sans-serif; color: #1f2933; margin: 28px; }}
    h1 {{ font-size: 24px; margin: 0 0 8px; }}
    h2 {{ font-size: 17px; margin: 26px 0 8px; border-bottom: 1px solid #d8dee4; padding-bottom: 5px; }}
    .meta {{ color: #52606d; font-size: 12px; line-height: 1.45; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 11px; }}
    th {{ background: #f1f5f9; text-align: left; }}
    th, td {{ border: 1px solid #d8dee4; padding: 6px 7px; vertical-align: top; word-break: break-word; }}
    .ok {{ color: #087f5b; font-weight: 700; }}
    .bad {{ color: #b42318; font-weight: 700; }}
    code {{ font-family: Consolas, monospace; font-size: 11px; }}
  </style>
</head>
<body>
  <h1>Walmart Full Run Report</h1>
  <div class="meta">
    run_id: <code>{html.escape(run_id)}</code><br>
    project_root: <code>{html.escape(str(project_root))}</code><br>
    zip_code: <code>{html.escape(zip_code)}</code><br>
    started_at: {html.escape(started_at)}<br>
    ended_at: {html.escape(ended_at)}<br>
    total_elapsed: <strong>{html.escape(fmt_duration(total_elapsed))}</strong>
  </div>

  <h2>Stage Timings</h2>
  {table(stage_rows, ["stage", "elapsed", "returncode", "started_at", "ended_at", "log"])}

  <h2>Output Counts</h2>
  {table(metric_rows, ["metric", "value"])}

  <h2>Detail Field Fill Check</h2>
  {table(fill_rows, ["field", "filled", "blank", "fill_rate"])}

  <h2>Output Files</h2>
  {table(files_rows, ["label", "path"])}
</body>
</html>
"""
    report_path.write_text(body, encoding="utf-8")


def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    chrome = find_chrome()
    if not chrome:
        return False
    cmd = [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        f"--print-to-pdf={pdf_path}",
        html_path.resolve().as_uri(),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return result.returncode == 0 and pdf_path.exists()


def collect_metrics(project_root: Path, report_dir: Path) -> Dict[str, Any]:
    listing_dir = project_root / "log" / "walmart_listing_300_probe"
    exact_dir = project_root / "log" / "walmart_exact_item_search_probe"
    detail_dir = project_root / "log" / "walmart_detail_review_batch_probe"

    files = {
        "listing_summary": listing_dir / "summary.json",
        "listing_all_unique_items": listing_dir / "all_unique_items.csv",
        "exact_items": exact_dir / "exact_items.csv",
        "exact_summary": exact_dir / "summary.json",
        "detail_items": detail_dir / "detail_items.csv",
        "review_items": detail_dir / "review_items.csv",
        "detail_summary": detail_dir / "summary.json",
        "final_output_columns": detail_dir / "final_output_columns.json",
        "final_detail_items": detail_dir / "detail_items.csv",
        "final_review_items": detail_dir / "review_items.csv",
        "report_dir": report_dir,
    }
    counts = {
        "listing_all_items_rows": csv_rows(listing_dir / "all_items.csv"),
        "listing_unique_item_rows": csv_rows(listing_dir / "all_unique_items.csv"),
        "listing_rejected_rows": csv_rows(listing_dir / "all_rejected.csv"),
        "exact_item_rows": csv_rows(exact_dir / "exact_items.csv"),
        "detail_item_rows": csv_rows(detail_dir / "detail_items.csv"),
        "review_item_rows": csv_rows(detail_dir / "review_items.csv"),
    }

    detail_fields = [
        "item",
        "product_url",
        "retailer_sku_name",
        "retailer_sku_name_similar",
        "final_sku_price",
        "original_sku_price",
        "discount_type",
        "discount",
        "offer",
        "seller",
        "shipping_fee",
        "fastest_delivery",
        "delivery_availability",
        "pick_up_availability",
        "number_of_ppl_added_to_carts",
        "number_of_ppl_purchased_yesterday",
        "star_rating",
        "count_of_star_ratings",
        "count_of_reviews",
        "detailed_review_content",
    ]
    return {
        "counts": counts,
        "detail_fill_stats": fill_stats(detail_dir / "detail_items.csv", detail_fields),
        "listing_summary": read_json(listing_dir / "summary.json"),
        "detail_summary": read_json(detail_dir / "summary.json"),
        "files": {k: str(v) for k, v in files.items()},
    }


def build_commands(args: argparse.Namespace) -> List[tuple[str, List[str]]]:
    py = sys.executable
    seed = Path("log") / "walmart_listing_300_probe" / "all_unique_items.csv"
    commands: List[tuple[str, List[str]]] = []

    listing = [
        py,
        "walmart_listing_300_probe.py",
        "--zip-code",
        args.zip_code,
        "--target-per-type",
        str(args.target_per_type),
        "--main-pages",
        str(args.main_pages),
        "--bsr-pages",
        str(args.bsr_pages),
        "--wait",
        str(args.listing_wait),
        "--between-pages",
        str(args.listing_between_pages),
        "--scroll-rounds",
        str(args.listing_scroll_rounds),
        "--captcha-wait",
        str(args.captcha_wait),
        "--nav-timeout",
        str(args.nav_timeout),
        "--no-mst-exclusion",
    ]
    exact = [
        py,
        "walmart_exact_item_search_probe.py",
        "--seed",
        str(seed),
        "--limit",
        str(args.exact_limit),
        "--wait",
        str(args.exact_wait),
        "--between-items",
        str(args.exact_between_items),
        "--nav-timeout",
        str(args.nav_timeout),
    ]
    detail = [
        py,
        "walmart_detail_review_batch_probe.py",
        "--seed",
        str(seed),
        "--limit",
        str(args.detail_limit),
        "--wait",
        str(args.detail_wait),
        "--between-items",
        str(args.detail_between_items),
        "--nav-timeout",
        str(args.nav_timeout),
        "--detail-scroll-rounds",
        str(args.detail_scroll_rounds),
        "--review-scroll-rounds",
        str(args.review_scroll_rounds),
        "--max-reviews",
        str(args.max_reviews),
        "--zip-code",
        args.zip_code,
    ]
    if args.headless:
        listing.append("--headless")
        exact.append("--headless")
        detail.append("--headless")
    if args.skip_reviews:
        detail.append("--skip-reviews")

    commands.append(("01_listing", listing))
    commands.append(("02_exact_listing_enrichment", exact))
    commands.append(("03_detail_review", detail))
    commands.append(
        (
            "04_reparse_raw",
            [
                py,
                "walmart_reparse_detail_review_raw.py",
                "--out-dir",
                str(Path("log") / "walmart_detail_review_batch_probe"),
                "--max-reviews",
                str(args.max_reviews),
            ],
        )
    )
    commands.append(("05_final_transform", [py, "walmart_final_output_transform.py"]))
    return commands


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Walmart full probe and write PDF report")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--zip-code", default=os.getenv("WALMART_ZIP_CODE", "11581"))
    parser.add_argument("--target-per-type", type=int, default=300)
    parser.add_argument("--main-pages", type=int, default=10)
    parser.add_argument("--bsr-pages", type=int, default=5)
    parser.add_argument("--listing-wait", type=float, default=8.0)
    parser.add_argument("--listing-between-pages", type=float, default=2.0)
    parser.add_argument("--listing-scroll-rounds", type=int, default=2)
    parser.add_argument("--captcha-wait", type=int, default=60)
    parser.add_argument("--exact-limit", type=int, default=0, help="0 means all listing seeds")
    parser.add_argument("--exact-wait", type=float, default=4.0)
    parser.add_argument("--exact-between-items", type=float, default=0.5)
    parser.add_argument("--detail-limit", type=int, default=0, help="0 means all listing seeds")
    parser.add_argument("--detail-wait", type=float, default=4.0)
    parser.add_argument("--detail-between-items", type=float, default=1.0)
    parser.add_argument("--detail-scroll-rounds", type=int, default=1)
    parser.add_argument("--review-scroll-rounds", type=int, default=1)
    parser.add_argument("--max-reviews", type=int, default=20)
    parser.add_argument("--nav-timeout", type=float, default=35.0)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--skip-reviews", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    run_id = now_stamp()
    report_dir = project_root / "log" / "walmart_full_run_report" / run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    log_dir = report_dir / "stage_logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["WALMART_ZIP_CODE"] = args.zip_code

    started_at = iso_now()
    total_start = time.perf_counter()
    stages: List[StageResult] = []
    print(f"[run] started {started_at} run_id={run_id}")
    print(f"[run] project_root={project_root}")
    print(f"[run] zip_code={args.zip_code}")

    for name, command in build_commands(args):
        print(f"[stage] start {name}")
        result = run_stage(name=name, command=command, cwd=project_root, log_dir=log_dir, env=env)
        stages.append(result)
        print(f"[stage] done {name} rc={result.returncode} elapsed={fmt_duration(result.elapsed_sec)}")
        if result.returncode != 0 and not args.continue_on_error:
            print(f"[stage] stop on failure: {name}")
            break

    ended_at = iso_now()
    total_elapsed = time.perf_counter() - total_start
    metrics = collect_metrics(project_root, report_dir)

    run_summary = {
        "run_id": run_id,
        "project_root": str(project_root),
        "zip_code": args.zip_code,
        "started_at": started_at,
        "ended_at": ended_at,
        "total_elapsed_sec": total_elapsed,
        "total_elapsed": fmt_duration(total_elapsed),
        "stages": [asdict(stage) for stage in stages],
        "metrics": metrics,
    }
    (report_dir / "run_summary.json").write_text(
        json.dumps(run_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    html_path = report_dir / "walmart_full_run_report.html"
    pdf_path = report_dir / "walmart_full_run_report.pdf"
    render_html_report(
        report_path=html_path,
        project_root=project_root,
        run_id=run_id,
        zip_code=args.zip_code,
        started_at=started_at,
        ended_at=ended_at,
        total_elapsed=total_elapsed,
        stages=stages,
        metrics=metrics,
    )
    pdf_ok = html_to_pdf(html_path, pdf_path)
    if pdf_ok:
        print(f"[report] pdf={pdf_path}")
    else:
        print(f"[report] html={html_path}")
        print("[report] PDF conversion failed or Chrome/Edge was not found.")
    return 0 if stages and all(stage.returncode == 0 for stage in stages) else 1


if __name__ == "__main__":
    raise SystemExit(main())
