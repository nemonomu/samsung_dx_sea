
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CATEGORIES = {
    "REF": "refrigerator",
    "TV": "tv",
    "LDY": "washing machine",
}

STAGES = [
    {
        "name": "main_probe",
        "sort": "",
        "organic_offset": "18",
        "pages": "16",
    },
    {
        "name": "bsr_probe",
        "sort": "Best-Selling",
        "organic_offset": "72",
        "pages": "2",
    },
]


def repo_root():
    return Path(__file__).resolve().parents[1]


def run_stage(category, search_term, run_root, stage):
    env = os.environ.copy()
    env.update(
        {
            "BESTBUY_CATEGORY": category,
            "BESTBUY_RUN_ROOT": str(run_root),
            "BESTBUY_SEARCH_TERM": search_term,
            "BESTBUY_MAIN_RUN_ID": stage["name"],
            "BESTBUY_MAIN_PAGES": stage["pages"],
            "BESTBUY_MAIN_ORGANIC_OFFSET": stage["organic_offset"],
            "BESTBUY_SEARCH_SORT": stage["sort"],
            "BESTBUY_FORCE_REFRESH": "1",
            "BESTBUY_MAIN_ALLOW_HTML_TEMPLATE": "0",
            "BESTBUY_GRAPHQL_PREMIUM_PROXY": "1",
            "BESTBUY_GRAPHQL_JS_RENDER": "1",
            "BESTBUY_SANITIZE_PRODUCT_LIST_QUERY": "0",
            "BESTBUY_STRIP_PRODUCT_LIST_FULFILLMENT": "0",
            "BESTBUY_LISTING_RECOVERY_ENABLED": "1",
            "BESTBUY_LISTING_RECOVERY_PROFILES": "wait,session_wait,auto",
            "BESTBUY_LISTING_RECOVERY_ATTEMPTS_PER_PROFILE": "2",
            "BESTBUY_LISTING_RECOVERY_WAIT_MS": "5000",
            "ZENROWS_TIMEOUT": "180",
        }
    )
    if not stage["sort"]:
        env.pop("BESTBUY_SEARCH_SORT", None)
    command = [sys.executable, "-m", "bestbuy.step01_main_list"]
    print(
        f"[probe] {category} {stage['name']} start search_term={search_term} pages={stage['pages']}",
        flush=True,
    )
    subprocess.run(command, cwd=repo_root(), env=env, check=True)
    manifest_path = run_root / stage["name"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = {
        "category": category,
        "stage": stage["name"],
        "search_term": manifest.get("search_term"),
        "search_sort": manifest.get("search_sort"),
        "rows": manifest.get("main_occurrences"),
        "unique_skus": manifest.get("unique_skus"),
        "failed_pages": manifest.get("failed_pages"),
        "recovered_pages": manifest.get("listing_recovered_pages"),
        "recovery_still_failed_pages": manifest.get("listing_recovery_still_failed_pages"),
        "recovery_attempt_count": manifest.get("listing_recovery_attempt_count"),
        "calls": manifest.get("total_request_calls"),
        "cost_usd": manifest.get("total_x_request_cost"),
        "manifest": str(manifest_path),
    }
    failed_pages = result["failed_pages"] or []
    result["ok"] = bool(result["rows"] and not failed_pages)
    print(
        "[probe] {category} {stage} ok={ok} rows={rows} unique={unique} "
        "failed={failed} recovered={recovered} recovery_attempts={recovery_attempts} "
        "calls={calls} cost={cost}".format(
            category=category,
            stage=stage["name"],
            ok=result["ok"],
            rows=result["rows"],
            unique=result["unique_skus"],
            failed=failed_pages,
            recovered=result["recovered_pages"],
            recovery_attempts=result["recovery_attempt_count"],
            calls=result["calls"],
            cost=result["cost_usd"],
        ),
        flush=True,
    )
    return result


def main():
    base = repo_root() / "bestbuy" / "data" / "_listing_recovery_probe" / datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    for category, search_term in CATEGORIES.items():
        category_root = base / category.lower()
        for stage in STAGES:
            results.append(run_stage(category, search_term, category_root, stage))
    summary_path = base / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    failed = [item for item in results if not item["ok"]]
    print(f"[probe] summary={summary_path}")
    print(f"[probe] failed_count={len(failed)}")
    if failed:
        print(json.dumps(failed, indent=2, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
