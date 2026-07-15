"""Compare pre-login and post-login Amazon PDP extraction in one browser.

The pre-login pass skips detailed_review_content but extracts every other
production field. The post-login pass extracts all fields, including detailed
reviews. The test disables every DB write and never opens a review-page URL.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse


_current_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(os.path.dirname(_current_dir))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from common.setup import setup_environment

setup_environment(__file__)

from amazon.tv.amazon_login import (
    ensure_amazon_login_dp,
    ensure_amazon_logout_dp,
    is_amazon_login_verified_dp,
    load_amazon_login_credentials,
)
from amazon.tv.amazon_tv_dt import AmazonTVDetailCrawler
from amazon.tv.amazon_tv_login_pdp_test_urls import DEFAULT_TEST_URLS


DEFAULT_OUTPUT_DIR = os.path.join(_current_dir, "data", "login_pdp_test")
REVIEW_FIELDS = {"detailed_review_content"}
MONITORED_FIELDS = tuple(dict.fromkeys([
    "sku",
    *AmazonTVDetailCrawler.EXTRACTED_FIELDS,
    *AmazonTVDetailCrawler.PASSTHROUGH_FIELDS,
]))
COMPARISON_FIELDS = tuple(
    field for field in MONITORED_FIELDS
    if field != "detailed_review_content"
)


class FieldComparisonCrawler(AmazonTVDetailCrawler):
    """Production extractor with all persistence and MST fallback disabled."""

    skip_detailed_review = False

    def get_tv_specs_from_mst(self, item):
        return None, None, None

    def save_debug_html(self, tag, max_files=3):
        return None

    def upsert_item_mst(self, product):
        raise AssertionError("DB write is forbidden in the login PDP test")

    def save_to_retail_com(self, product):
        raise AssertionError("DB write is forbidden in the login PDP test")

    def extract_reviews_with_retry(self, tree, max_reviews=20, page_html=None):
        if self.skip_detailed_review:
            print(
                "  [REVIEW] Pre-login field comparison skips "
                "detailed_review_content"
            )
            return None, 0, False
        return super().extract_reviews_with_retry(
            tree,
            max_reviews=max_reviews,
            page_html=page_html,
        )


def load_test_urls(path=None, limit=0):
    """Load unique Amazon product-detail URLs only."""
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            raw_urls = [
                line.strip() for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            ]
    else:
        raw_urls = list(DEFAULT_TEST_URLS)

    urls = []
    seen = set()
    for url in raw_urls:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme not in ("http", "https") or not host.endswith("amazon.com"):
            raise ValueError(f"Not an amazon.com URL: {url}")
        if "/product-reviews/" in parsed.path:
            raise ValueError(f"Review-page URL is forbidden: {url}")
        if not re.search(r"/(?:dp|gp/product)/[A-Z0-9]{10}(?:[/?]|$)", parsed.path):
            raise ValueError(f"Not an Amazon product detail URL: {url}")
        if url not in seen:
            urls.append(url)
            seen.add(url)

    if limit and limit > 0:
        urls = urls[:limit]
    if not urls:
        raise ValueError(f"No test URLs found in {path or 'built-in fixture'}")
    return urls


def extract_asin(url):
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", url)
    return match.group(1) if match else None


def is_empty(value):
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def json_safe(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)


def observe_field(field, value):
    present = not is_empty(value)
    observation = {"present": present}
    if not present:
        return observation

    if field in REVIEW_FIELDS:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        observation.update({
            "length": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
        if field == "detailed_review_content":
            observation["review_count"] = len(re.findall(
                r"(?:^|\s*\|\|\|\s*)review\d+\s+-",
                str(value),
            ))
    else:
        observation["value"] = json_safe(value)
    return observation


def snapshot_fields(data):
    return {
        field: observe_field(field, data.get(field))
        for field in MONITORED_FIELDS
    }


def crawl_one(crawler, url, mode):
    started = time.time()
    asin = extract_asin(url)
    product = {
        "product_url": url,
        "item": asin,
        "retailer_sku_name": "",
        "page_type": "bsr",
        "redirect": False,
    }
    entry = {
        "requested_url": url,
        "asin": asin,
        "mode": mode,
    }
    gate_count_before = crawler.detail_report.get("review_gated_count", 0)

    try:
        data = crawler.crawl_detail(product)
        gate_count_after = crawler.detail_report.get("review_gated_count", 0)
        gated = gate_count_after > gate_count_before
        entry["landing_url"] = crawler.page.url
        entry["gated"] = gated

        if data is None:
            entry["status"] = "error"
            entry["error"] = "crawl_detail returned None"
            data = product
        elif data.get("_detail_skip"):
            entry["status"] = "redirect_skip"
            entry["skip_reason"] = data.get("_detail_skip")
        elif data is product:
            entry["status"] = "error"
            entry["error"] = "crawl_detail returned the unchanged input"
        elif gated:
            entry["status"] = "gated"
        else:
            entry["status"] = "detail_ok"

        if mode == "logged_in" and not is_amazon_login_verified_dp(crawler.page):
            entry["status"] = "session_lost"
        elif mode == "logged_out" and is_amazon_login_verified_dp(crawler.page):
            entry["status"] = "unexpected_login"

        entry["fields"] = snapshot_fields(data)
        entry["filled_fields"] = [
            field for field, observation in entry["fields"].items()
            if observation["present"]
        ]
        return entry
    except Exception as exc:
        entry.update({
            "status": "error",
            "gated": False,
            "error": str(exc)[:500],
            "fields": snapshot_fields(product),
            "filled_fields": [],
        })
        return entry
    finally:
        entry["elapsed_seconds"] = round(time.time() - started, 2)


def run_mode(crawler, urls, mode, sleep_seconds):
    rows = []
    for index, url in enumerate(urls, 1):
        if mode == "logged_in" and not is_amazon_login_verified_dp(crawler.page):
            rows.append({
                "sequence": index,
                "requested_url": url,
                "asin": extract_asin(url),
                "mode": mode,
                "status": "session_lost",
                "gated": False,
                "fields": {},
                "filled_fields": [],
                "elapsed_seconds": 0,
            })
            break
        if mode == "logged_out" and is_amazon_login_verified_dp(crawler.page):
            rows.append({
                "sequence": index,
                "requested_url": url,
                "asin": extract_asin(url),
                "mode": mode,
                "status": "unexpected_login",
                "gated": False,
                "fields": {},
                "filled_fields": [],
                "elapsed_seconds": 0,
            })
            break

        row = crawl_one(crawler, url, mode)
        row["sequence"] = index
        rows.append(row)
        print(
            f"[{mode} {index}/{len(urls)}] asin={row.get('asin')} "
            f"status={row['status']} filled={len(row['filled_fields'])}"
        )
        if row["status"] in ("session_lost", "unexpected_login"):
            break
        time.sleep(max(sleep_seconds, 0))
    return rows


def observation_token(observation):
    return json.dumps(
        observation,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def compare_modes(urls, logged_out_rows, logged_in_rows):
    logged_out = {row["requested_url"]: row for row in logged_out_rows}
    logged_in = {row["requested_url"]: row for row in logged_in_rows}
    comparisons = []

    for url in urls:
        out_row = logged_out.get(url)
        in_row = logged_in.get(url)
        comparison = {
            "requested_url": url,
            "asin": extract_asin(url),
            "logged_out_status": out_row.get("status") if out_row else "not_run",
            "logged_in_status": in_row.get("status") if in_row else "not_run",
            "lost_after_login": [],
            "gained_after_login": [],
            "changed_after_login": [],
            "missing_in_both": [],
            "fields": {},
            "detailed_review_content": {
                "compared": False,
                "post_login_present": False,
                "post_login_review_count": 0,
                "post_login_length": 0,
                "login_gate": bool(in_row and in_row.get("gated")),
            },
        }

        if out_row and in_row:
            out_fields = out_row.get("fields", {})
            in_fields = in_row.get("fields", {})
            for field in COMPARISON_FIELDS:
                out_observation = out_fields.get(field, {"present": False})
                in_observation = in_fields.get(field, {"present": False})
                out_present = out_observation.get("present", False)
                in_present = in_observation.get("present", False)
                if out_present and not in_present:
                    state = "LOST"
                    comparison["lost_after_login"].append(field)
                elif not out_present and in_present:
                    state = "GAINED"
                    comparison["gained_after_login"].append(field)
                elif not out_present and not in_present:
                    state = "MISSING_BOTH"
                    comparison["missing_in_both"].append(field)
                elif observation_token(out_observation) != observation_token(in_observation):
                    state = "CHANGED"
                    comparison["changed_after_login"].append(field)
                else:
                    state = "SAME"
                comparison["fields"][field] = {
                    "state": state,
                    "before_login": out_observation.get("value"),
                    "after_login": in_observation.get("value"),
                }

            review_observation = in_fields.get(
                "detailed_review_content",
                {"present": False},
            )
            comparison["detailed_review_content"].update({
                "post_login_present": bool(review_observation.get("present")),
                "post_login_review_count": review_observation.get(
                    "review_count",
                    0,
                ),
                "post_login_length": review_observation.get("length", 0),
                "post_login_sha256": review_observation.get("sha256"),
            })
        comparisons.append(comparison)
    return comparisons


def _log_value(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def print_final_comparison_log(comparisons):
    print("\n" + "=" * 96)
    print("[FINAL FIELD COMPARISON] excludes detailed_review_content")
    print("=" * 96)
    for comparison in comparisons:
        print(
            f"\n[PRODUCT] asin={comparison['asin']} "
            f"before_status={comparison['logged_out_status']} "
            f"after_status={comparison['logged_in_status']}"
        )
        print(f"  URL: {comparison['requested_url']}")
        for field in COMPARISON_FIELDS:
            values = comparison["fields"].get(field, {
                "state": "NOT_RUN",
                "before_login": None,
                "after_login": None,
            })
            print(f"  [FIELD] {field} | {values['state']}")
            print(
                "    BEFORE_LOGIN: "
                f"{_log_value(values['before_login'])}"
            )
            print(
                "    AFTER_LOGIN : "
                f"{_log_value(values['after_login'])}"
            )

        review = comparison["detailed_review_content"]
        print("  [FIELD] detailed_review_content | COMPARE_EXCLUDED")
        print(
            "    AFTER_LOGIN : "
            f"present={review['post_login_present']} "
            f"review_count={review['post_login_review_count']} "
            f"length={review['post_login_length']} "
            f"login_gate={review['login_gate']} "
            f"sha256={review.get('post_login_sha256')}"
        )
    print("\n" + "=" * 96)


def enforce_read_only_db(crawler):
    """Make the XPath-loading connection reject any accidental write."""
    if not crawler.db_conn:
        return False
    try:
        crawler.db_conn.rollback()
        crawler.db_conn.set_session(readonly=True, autocommit=False)
        return True
    except Exception as exc:
        print(f"[ERROR] Could not enforce read-only DB session: {exc}")
        return False


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Compare Amazon TV detail fields except detailed_review_content "
            "before and after one LOGIN_2 login; collect detailed reviews "
            "after login in the same browser session"
        ),
    )
    parser.add_argument(
        "--urls-file",
        help="Optional newline-delimited URL file (default: built-in 15 URLs)",
    )
    parser.add_argument("--limit", type=int, default=0, help="0 means all URLs")
    parser.add_argument("--sleep", type=float, default=2.5)
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=int(os.environ.get("AMAZON_LOGIN_TIMEOUT", "180")),
    )
    parser.add_argument("--no-profile", action="store_true")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main():
    args = parse_args()
    urls = load_test_urls(args.urls_file, args.limit)
    try:
        load_amazon_login_credentials()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        return 3
    os.makedirs(args.output_dir, exist_ok=True)

    started_at = datetime.now()
    crawler = FieldComparisonCrawler(
        batch_id=f"login_pdp_test_{started_at.strftime('%Y%m%d_%H%M%S')}",
        test_mode=True,
        require_amazon_login=False,
    )
    crawler.use_trusted_profile = not args.no_profile
    crawler.capture_enabled = False
    crawler._first_detail_html_saved = True

    result = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "credentials_source": "AMAZON_LOGIN_2",
        "same_browser_session": True,
        "comparison_fields": list(COMPARISON_FIELDS),
        "comparison_excluded_fields": ["detailed_review_content"],
        "pre_login_detailed_review_skipped": True,
        "login_attempts": 0,
        "login_ok": False,
        "logged_out_baseline_ok": False,
        "review_page_navigation": False,
        "db_write": False,
        "db_session_read_only": False,
        "mst_fallback": False,
        "extraction_page_type": "bsr",
        "urls_source": (
            os.path.abspath(args.urls_file)
            if args.urls_file
            else "built-in fixture"
        ),
        "requested_count": len(urls),
        "modes": {
            "logged_out": {"products": []},
            "logged_in": {"products": []},
        },
        "comparisons": [],
    }
    exit_code = 0

    try:
        if not crawler.initialize():
            result["fatal_error"] = "Crawler initialization failed"
            exit_code = 1
        elif not enforce_read_only_db(crawler):
            result["fatal_error"] = "Could not enforce a read-only DB session"
            exit_code = 1
        elif not ensure_amazon_logout_dp(crawler.page):
            result["fatal_error"] = "Could not establish a logged-out baseline"
            exit_code = 3
        elif not crawler.set_amazon_zip_code(crawler.amazon_zip_code):
            result["fatal_error"] = "ZIP reset failed after logout"
            exit_code = 3
        else:
            result["db_session_read_only"] = True
            result["logged_out_baseline_ok"] = True
            crawler.skip_detailed_review = True
            print(
                f"[TEST PHASE 1/2] Pre-login field extraction: "
                f"{len(urls)} PDP URLs "
                "(detailed_review_content skipped)"
            )
            logged_out_rows = run_mode(crawler, urls, "logged_out", args.sleep)
            result["modes"]["logged_out"]["products"] = logged_out_rows

            crawler.skip_detailed_review = False
            result["login_attempts"] = 1
            print("[TEST PHASE 2/2] Starting AMAZON_LOGIN_2 login")
            result["login_ok"] = ensure_amazon_login_dp(
                crawler.page,
                timeout_seconds=args.login_timeout,
            )
            if not result["login_ok"]:
                result["fatal_error"] = "Amazon LOGIN_2 authentication failed"
                exit_code = 3
            elif not crawler.set_amazon_zip_code(crawler.amazon_zip_code):
                result["fatal_error"] = "ZIP reset failed after login"
                exit_code = 3
            else:
                print(
                    f"[TEST PHASE 2/2] Post-login full-field extraction: "
                    f"{len(urls)} PDP URLs"
                )
                logged_in_rows = run_mode(crawler, urls, "logged_in", args.sleep)
                result["modes"]["logged_in"]["products"] = logged_in_rows
                result["comparisons"] = compare_modes(
                    urls,
                    logged_out_rows,
                    logged_in_rows,
                )
    except Exception as exc:
        result["fatal_error"] = str(exc)[:500]
        exit_code = 3 if result["login_attempts"] else 1
    finally:
        if crawler.page:
            try:
                crawler.page.quit()
            except Exception:
                pass
        if crawler.db_conn:
            try:
                crawler.db_conn.close()
            except Exception:
                pass

    out_rows = result["modes"]["logged_out"]["products"]
    in_rows = result["modes"]["logged_in"]["products"]
    out_statuses = Counter(row["status"] for row in out_rows)
    in_statuses = Counter(row["status"] for row in in_rows)
    lost_field_count = sum(
        len(row["lost_after_login"]) for row in result["comparisons"]
    )
    changed_field_count = sum(
        len(row["changed_after_login"]) for row in result["comparisons"]
    )
    detailed_review_collected = sum(
        bool(row["detailed_review_content"]["post_login_present"])
        for row in result["comparisons"]
    )
    review_login_gate_count = sum(
        bool(row["detailed_review_content"]["login_gate"])
        for row in result["comparisons"]
    )
    result["summary"] = {
        "logged_out_statuses": dict(out_statuses),
        "logged_in_statuses": dict(in_statuses),
        "lost_after_login_field_count": lost_field_count,
        "changed_after_login_field_count": changed_field_count,
        "detailed_review_content_collected": detailed_review_collected,
        "review_login_gate_count": review_login_gate_count,
    }
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    result["elapsed_seconds"] = round(
        (datetime.now() - started_at).total_seconds(),
        2,
    )

    if exit_code == 0:
        logged_out_blocking_statuses = {
            "session_lost",
            "unexpected_login",
            "error",
            "redirect_skip",
        }
        logged_in_blocking_statuses = {
            "gated",
            "session_lost",
            "unexpected_login",
            "error",
            "redirect_skip",
        }
        if any(
            out_statuses.get(status)
            for status in logged_out_blocking_statuses
        ) or any(
            in_statuses.get(status)
            for status in logged_in_blocking_statuses
        ):
            exit_code = 2
        elif lost_field_count:
            exit_code = 2

    if result["comparisons"]:
        print_final_comparison_log(result["comparisons"])

    stamp = started_at.strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(args.output_dir, f"login_pdp_test_{stamp}.json")
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)

    print(
        "[SUMMARY] "
        f"login_attempts={result['login_attempts']} "
        f"login_ok={result['login_ok']} "
        f"logged_out={dict(out_statuses)} "
        f"logged_in={dict(in_statuses)} "
        f"lost_fields={lost_field_count} "
        f"changed_fields={changed_field_count} "
        f"detailed_review_content="
        f"{detailed_review_collected}/{len(in_rows)} "
        f"review_login_gate={review_login_gate_count}"
    )
    print(f"[RESULT] {output_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
