from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List


def run(cmd: List[str], *, allow_nonzero: bool = False) -> subprocess.CompletedProcess[str]:
    print("\n[RUN] " + " ".join(f'"{part}"' if " " in part else part for part in cmd), flush=True)
    completed = subprocess.run(cmd, text=True)
    if completed.returncode and not allow_nonzero:
        raise SystemExit(completed.returncode)
    return completed


def log_stage(step: str, message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[STAGE {step}] {stamp} | {message}", flush=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def require_file(path: Path, message: str) -> None:
    if not path.exists():
        raise SystemExit(f"{message}: {path}")


def assert_listing_minimums(summary_path: Path, min_main: int = 300, min_bsr: int = 100) -> dict:
    summary = read_json(summary_path)
    listing = summary.get("listing") or {}
    by_type = listing.get("by_type") or {}
    combined = listing.get("combined") or {}
    main_count = int((by_type.get("main") or {}).get("accepted_items") or 0)
    bsr_count = int((by_type.get("bsr") or {}).get("accepted_items") or 0)
    unique_items = int(combined.get("unique_items") or 0)
    if main_count < min_main or bsr_count < min_bsr:
        raise SystemExit(
            f"Listing minimum not met: main accepted={main_count}/{min_main}, "
            f"bsr accepted={bsr_count}/{min_bsr}. Increase listing pages or review listing source URLs."
        )
    return {"main_count": main_count, "bsr_count": bsr_count, "unique_items": unique_items}


def count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def main() -> int:
    parser = argparse.ArgumentParser(description="Walmart full collection -> normalize -> validate -> DB insert")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--main-pages", type=int, default=12)
    parser.add_argument("--bsr-pages", type=int, default=5)
    parser.add_argument("--target-per-type", type=int, default=330)
    parser.add_argument("--max-reviews", type=int, default=20)
    parser.add_argument("--max-review-pages", type=int, default=2)
    parser.add_argument("--recovery-passes", type=int, default=3)
    parser.add_argument("--table", default="tv_retail_com")
    parser.add_argument("--commit-db", action="store_true")
    parser.add_argument("--skip-db", action="store_true")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    common_dir = project_root / "walmart" / "common"
    log_dir = project_root / "log"
    sample_csv = log_dir / "tv_retail_com_202606051111.csv"
    run_started_at = datetime.now()
    run_id = args.run_id or run_started_at.strftime("%Y%m%d_%H%M%S")
    crawl_datetime = run_started_at.strftime("%Y-%m-%d %H:%M:%S")
    batch_id = f"t_w_{run_id}"
    base_out = log_dir / f"walmart_full_run_{run_id}"
    merged_out = log_dir / f"walmart_full_run_{run_id}_merged"
    python = sys.executable

    collector = common_dir / "walmart_raw_http_collection.py"
    audit = common_dir / "walmart_chunk_recovery_audit.py"
    merge = common_dir / "walmart_merge_detail_review_chunks.py"
    transform = common_dir / "walmart_db_shape_transform.py"
    validator = common_dir / "walmart_db_shape_validator.py"
    inserter = common_dir / "walmart_db_insert_csv.py"
    btf_seed = log_dir / "walmart_api_seeds_fullheaders.json"
    require_file(project_root / "config.py", "Missing DB config.py")
    require_file(btf_seed, "Missing Walmart BTF seed JSON. Copy it before running; retailer_sku_name_similar depends on this file")

    log_stage("1/8", f"listing collection start: main_pages={args.main_pages}, bsr_pages={args.bsr_pages}, target_per_type={args.target_per_type}")
    run([
        python, str(collector),
        "--project-root", str(project_root),
        "--out-dir", str(base_out),
        "--stage", "listing",
        "--main-pages", str(args.main_pages),
        "--bsr-pages", str(args.bsr_pages),
        "--target-per-type", str(args.target_per_type),
        "--timeout", "35",
        "--retries", "1",
        "--retry-sleep", "2",
        "--between-pages", "0.8",
    ])
    log_stage("2/8", "listing minimum check: require main>=300 and bsr>=100")
    listing_counts = assert_listing_minimums(base_out / "summary.json", min_main=300, min_bsr=100)
    seed_count = count_csv_rows(base_out / "all_unique_items.csv")
    print(
        f"[CHECK] listing accepted main={listing_counts['main_count']} bsr={listing_counts['bsr_count']} "
        f"unique_items={listing_counts['unique_items']} detail_review_seed_rows={seed_count}",
        flush=True,
    )
    print(
        "[CHECK] detail/review expected workload: "
        f"detail~{seed_count}, review~{seed_count} to {seed_count * max(1, args.max_review_pages)}, "
        f"btf up to {seed_count}; max_review_pages={args.max_review_pages}. "
        "runtime depends on Walmart response time, review p2 count, BTF fallback count, retries, and sleeps.",
        flush=True,
    )

    log_stage("3/8", f"detail/review collection start: seed_rows={seed_count}, max_reviews={args.max_reviews}, max_review_pages={args.max_review_pages}, with_btf=true")
    run([
        python, str(collector),
        "--project-root", str(project_root),
        "--out-dir", str(base_out),
        "--stage", "detail-review",
        "--seed", str(base_out / "all_unique_items.csv"),
        "--limit", "0",
        "--max-reviews", str(args.max_reviews),
        "--max-review-pages", str(args.max_review_pages),
        "--timeout", "35",
        "--retries", "2",
        "--retry-sleep", "5",
        "--between-pages", "1.2",
        "--between-items", "1.2",
        "--progress-every", "1",
        "--flush-every", "10",
        "--with-btf",
    ])

    chunk_dirs: List[Path] = []
    for pass_no in range(1, args.recovery_passes + 1):
        log_stage("4/8", f"recovery audit pass {pass_no}/{args.recovery_passes}")
        audit_cmd = [python, str(audit), "--base-dir", str(base_out)]
        for chunk_dir in chunk_dirs:
            audit_cmd.extend(["--chunk-dir", str(chunk_dir)])
        run(audit_cmd, allow_nonzero=True)
        audit_summary = read_json(base_out / "chunk_recovery_audit.json")
        missing_count = int(audit_summary.get("missing_any_count", 0))
        print(f"[CHECK] recovery pass {pass_no}: missing_any_count={missing_count}", flush=True)
        if missing_count == 0:
            break
        missing_seed = base_out / "missing_seed.csv"
        chunk_out = log_dir / f"walmart_full_run_{run_id}_missing{pass_no}"
        log_stage("4/8", f"recovery collection pass {pass_no}/{args.recovery_passes}: retry missing detail/review rows")
        run([
            python, str(collector),
            "--project-root", str(project_root),
            "--out-dir", str(chunk_out),
            "--stage", "detail-review",
            "--seed", str(missing_seed),
            "--limit", "0",
            "--max-reviews", str(args.max_reviews),
            "--max-review-pages", str(args.max_review_pages),
            "--timeout", "35",
            "--retries", "2",
            "--retry-sleep", "5",
            "--between-pages", "1.2",
            "--between-items", "1.2",
            "--with-btf",
        ])
        chunk_dirs.append(chunk_out)
    else:
        audit_summary = read_json(base_out / "chunk_recovery_audit.json")
        if int(audit_summary.get("missing_any_count", 0)) > 0:
            raise SystemExit("Recovery passes exhausted with missing rows remaining")

    merge_cmd = [python, str(merge), "--base-dir", str(base_out), "--out-dir", str(merged_out)]
    for chunk_dir in chunk_dirs:
        merge_cmd.extend(["--chunk-dir", str(chunk_dir)])
    log_stage("5/8", "merge base and recovery chunks")
    run(merge_cmd)

    log_stage("6/8", f"normalize to DB insert shape: batch_id={batch_id}, account_name=Walmart, country=SEA")
    run([
        python, str(transform),
        "--out-dir", str(merged_out),
        "--crawl-datetime", crawl_datetime,
        "--account-name", "Walmart",
        "--batch-id", batch_id,
        "--country", "SEA",
    ])

    target_csv = merged_out / "db_insert_review_items_wmart_dt_shape.csv"
    for idx in range(1, 4):
        log_stage("7/8", f"validator pass {idx}/3 against sample CSV")
        run([
            python, str(validator),
            "--target-csv", str(target_csv),
            "--sample-csv", str(sample_csv),
            "--out-dir", str(merged_out),
            "--label", f"bat_verify{idx}",
        ])

    if not args.skip_db:
        log_stage("8/8", f"DB insert start: table={args.table}, mode={'commit' if args.commit_db else 'dry_run'}")
        insert_cmd = [
            python, str(inserter),
            "--project-root", str(project_root),
            "--csv", str(target_csv),
            "--table", args.table,
        ]
        if args.commit_db:
            insert_cmd.append("--commit")
        run(insert_cmd)

    print(json.dumps({
        "status": "complete",
        "base_out": str(base_out),
        "merged_out": str(merged_out),
        "final_insert_csv": str(target_csv),
        "db_mode": "skipped" if args.skip_db else ("committed" if args.commit_db else "rolled_back_dry_run"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
