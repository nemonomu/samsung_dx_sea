"""Collect Walmart listing/detail/review from raw HTML __NEXT_DATA__.

This is the zero-cost collector candidate. It avoids browser rendering and paid
APIs: regular HTTP GET -> extract __NEXT_DATA__ -> reuse walmart_json_parser.
"""

from __future__ import annotations

import argparse
import csv
import copy
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_PROJECT_ROOT = Path(
    r"C:\Users\gomguard\Documents\퀵오일\삼성전자\samsung_dx_retail_com\samsung_dx_retail_com"
)
NEXT_RE = re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)
ROBOT_TOKENS = ("robot or human", "press & hold", "press and hold", "verify you are human", "waitingroom")
EXCLUDED_NAME_RE = re.compile(r"\b(pre[- ]?owned|used|refurb(?:ished)?|renewed|open[- ]?box)\b", re.I)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


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


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def setup_project_imports(project_root: Path) -> None:
    sys.path.insert(0, str(project_root))
    sys.path.insert(0, str(project_root / "walmart" / "common"))


def fetch_html(url: str, timeout: int, retries: int, sleep: float) -> Dict[str, Any]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    last: Dict[str, Any] = {}
    for attempt in range(1, retries + 2):
        started = time.perf_counter()
        raw = b""
        status: Any = ""
        reason = ""
        final_url = url
        encoding = ""
        error = ""
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                reason = getattr(resp, "reason", "")
                final_url = resp.url
                encoding = resp.headers.get("content-encoding", "")
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            reason = exc.reason
            final_url = exc.url or url
            encoding = exc.headers.get("content-encoding", "") if exc.headers else ""
            raw = exc.read()
            error = f"HTTPError: {exc}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        body = raw
        if raw and (encoding == "gzip" or raw.startswith(b"\x1f\x8b")):
            try:
                body = gzip.decompress(raw)
            except Exception:
                body = raw
        html = body.decode("utf-8", errors="replace") if body else ""
        last = {
            "url": url,
            "status": status,
            "reason": reason,
            "final_url": final_url,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "raw_bytes": len(raw),
            "html_length": len(html),
            "robot_detected": any(token in html.lower() for token in ROBOT_TOKENS),
            "has_next_data": bool(NEXT_RE.search(html)),
            "attempt": attempt,
            "error": error,
            "html": html,
        }
        if last["status"] == 200 and last["has_next_data"] and not last["robot_detected"]:
            return last
        if attempt <= retries:
            time.sleep(sleep)
    return last


def extract_next_data(html: str) -> Optional[Dict[str, Any]]:
    match = NEXT_RE.search(html)
    if not match:
        return None
    return json.loads(match.group(1))


def walk_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_values(child)


def search_item_objects(next_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    data = next_data.get("props", {}).get("pageProps", {}).get("initialData", {})
    search = data.get("searchResult") if isinstance(data, dict) else {}
    by_item: Dict[str, Dict[str, Any]] = {}
    if not isinstance(search, dict):
        return by_item
    for stack in search.get("itemStacks") or []:
        if not isinstance(stack, dict):
            continue
        for item in stack.get("items") or []:
            if isinstance(item, dict) and item.get("usItemId"):
                by_item[str(item.get("usItemId"))] = item
    return by_item


def text_fragments(value: Any) -> List[str]:
    out: List[str] = []
    for node in walk_values(value):
        if not isinstance(node, dict):
            continue
        for key in ("text", "label", "value", "slaText"):
            raw = node.get(key)
            if isinstance(raw, str) and raw.strip():
                out.append(re.sub(r"\s+", " ", raw).strip())
    return out


def only_left_quantity(item: Dict[str, Any]) -> Optional[str]:
    for value in text_fragments(item):
        match = re.search(r"\bOnly\s+([\d,]+)\s+left\b", value, re.I)
        if match:
            return match.group(1).replace(",", "")
    return None


def sku_status_from_listing_item(item: Dict[str, Any]) -> Optional[str]:
    statuses: List[str] = []
    if item.get("sponsoredProduct"):
        statuses.append("Sponsored")
    for value in text_fragments(item):
        if value in {"Sponsored", "Rollback", "Reduced price"} and value not in statuses:
            statuses.append(value)
    return ", ".join(statuses) if statuses else None


def clean_row_values(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    if str(row.get("discount_type") or "").upper() == "UNKNOWN":
        row["discount_type"] = ""
    if row.get("screen_size"):
        row["screen_size"] = normalize_screen_size(row.get("screen_size"))
    return row


class ProductTitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.depth = 0
        self.parts: List[str] = []
        self.names: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        if attr.get("data-automation-id") == "product-title":
            self.in_title = True
            self.depth = 1
            self.parts = []
        elif self.in_title:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self.in_title:
            return
        self.depth -= 1
        if self.depth <= 0:
            name = re.sub(r"\s+", " ", " ".join(self.parts)).strip()
            if name and name not in self.names:
                self.names.append(name)
            self.in_title = False
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.in_title and data.strip():
            self.parts.append(data.strip())


def similar_names_from_rendered_html(html: str) -> List[str]:
    lower = html.lower()
    start = lower.find("similar items you might like")
    if start < 0:
        if 'data-testid="carousel-container"' not in lower and "data-testid='carousel-container'" not in lower:
            return []
        section = html
    else:
        end_candidates = [
            idx
            for idx in (
                lower.find("popular items in this category", start),
                lower.find("more items to consider", start),
            )
            if idx > start
        ]
        end = min(end_candidates) if end_candidates else len(html)
        section = html[start:end]
    parser = ProductTitleParser()
    parser.feed(section)
    return parser.names


def normalize_screen_size(value: Any) -> Any:
    value = str(value or "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:inches|inch|in\b|\")", value, re.I)
    if not match:
        return value
    return f"{match.group(1)} inches"


def enrich_listing_rows(rows: List[Dict[str, Any]], next_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    item_objects = search_item_objects(next_data)
    out: List[Dict[str, Any]] = []
    for row in rows:
        row = clean_row_values(row)
        item = str(row.get("item") or "")
        source = item_objects.get(item)
        if source:
            qty = only_left_quantity(source)
            if qty and not row.get("available_quantity_for_purchase"):
                row["available_quantity_for_purchase"] = qty
            status = sku_status_from_listing_item(source)
            if status and not row.get("sku_status"):
                row["sku_status"] = status
        out.append(row)
    return out


def item_from_url(value: Any) -> str:
    value = str(value or "")
    match = re.search(r"/(?:ip|reviews/product)/(?:[^/?#]+/)?(\d+)(?:[/?#]|$)", value)
    return match.group(1) if match else ""


def page2_review_url(item: str) -> str:
    return f"https://www.walmart.com/reviews/product/{item}?sort=relevancy&page=2"


def page2_review_fallback_urls(item: str) -> List[str]:
    return [
        f"https://www.walmart.com/reviews/product/{item}?page=2&sort=relevancy",
        f"https://www.walmart.com/reviews/product/{item}?page=2",
    ]


def safe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    value_text = str(value).strip()
    match = re.search(r"(\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*([kKmM])?", value_text)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
        suffix = (match.group(2) or "").lower()
        if suffix == "k":
            number *= 1_000
        elif suffix == "m":
            number *= 1_000_000
        return int(number)
    except ValueError:
        return None


def infer_screen_size_from_text(*values: Any) -> str:
    text_value = " ".join(str(value or "") for value in values)
    patterns = [
        r"(?<!\d)(\d{2,3}(?:\.\d+)?)\s*(?:-| )?\s*(?:inch|inches)\b",
        r"(?<!\d)(\d{2,3}(?:\.\d+)?)\s*(?:\"|”)",
        r"(?<!\d)(\d{2,3}(?:\.\d+)?)\s*Class\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text_value, flags=re.IGNORECASE):
            try:
                number = float(match.group(1))
            except ValueError:
                continue
            if 10 <= number <= 120:
                if number.is_integer():
                    return f"{int(number)} inches"
                return f"{number:g} inches"
    return ""


def fill_screen_size_fallback(row: Dict[str, Any]) -> None:
    if row.get("screen_size"):
        return
    inferred = infer_screen_size_from_text(
        row.get("retailer_sku_name"),
        row.get("product_url"),
        row.get("short_description"),
        row.get("long_description"),
    )
    if inferred:
        row["screen_size"] = inferred


def review_count_from_next(next_data: Optional[Dict[str, Any]]) -> int:
    if not isinstance(next_data, dict):
        return 0
    reviews = (
        (((next_data.get("props") or {}).get("pageProps") or {}).get("initialData") or {})
        .get("data", {})
        .get("reviews", {})
    )
    customer_reviews = reviews.get("customerReviews") if isinstance(reviews, dict) else []
    return len(customer_reviews or [])


def load_page_urls(project_root: Path, table: str, page_type: str, max_pages: int) -> List[Tuple[int, str]]:
    from config import DB_CONFIG  # type: ignore
    import psycopg2  # type: ignore

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT page_number, url
            FROM {table}
            WHERE page_type = %s AND is_active = TRUE
            ORDER BY page_number
            LIMIT %s
            """,
            (page_type, max_pages),
        )
        return [(int(row[0]), str(row[1])) for row in cur.fetchall()]
    finally:
        conn.close()


def default_listing_url(page_type: str, page_number: int) -> str:
    if page_type == "bsr":
        if page_number <= 1:
            return "https://www.walmart.com/search?q=TV&sort=best_seller"
        return f"https://www.walmart.com/search?q=TV&sort=best_seller&page={page_number}&affinityOverride=default"
    if page_number <= 1:
        return "https://www.walmart.com/search?q=TV"
    return f"https://www.walmart.com/search?q=TV&page={page_number}&affinityOverride=default"


def ensure_listing_page_urls(rows: List[Tuple[int, str]], page_type: str, max_pages: int) -> List[Tuple[int, str]]:
    """Use DB-configured URLs first, then generate missing page URLs up to max_pages."""
    by_page = {int(page_number): url for page_number, url in rows if page_number and url}
    for page_number in range(1, max_pages + 1):
        by_page.setdefault(page_number, default_listing_url(page_type, page_number))
    return sorted(by_page.items())[:max_pages]


def load_excluded_product_urls() -> set[str]:
    from config import DB_CONFIG  # type: ignore
    import psycopg2  # type: ignore

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT product_url
                FROM tv_item_mst
                WHERE is_product = FALSE AND product_url IS NOT NULL
                """
            )
            return {str(row[0]).rstrip("/") for row in cur.fetchall() if row[0]}
        finally:
            conn.close()
    except Exception:
        return set()


def normalize_listing_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    page_type: str,
    page_number: int,
    page_url: str,
    start_rank: int,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for local_rank, row in enumerate(rows, 1):
        row = dict(row)
        product_url = str(row.get("product_url") or "")
        item = row.get("item") or item_from_url(product_url)
        row["item"] = item
        row["page_type"] = page_type
        row["page_number"] = page_number
        row["page_url"] = page_url
        row["page_rank"] = local_rank
        row["rank"] = start_rank + len(out) + 1
        if page_type == "main":
            row["main_rank"] = row["rank"]
            row["main_page_number"] = page_number
        elif page_type == "bsr":
            row["bsr_rank"] = row["rank"]
            row["bsr_page_number"] = page_number
        out.append(row)
    return out


def dedupe_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = str(row.get("item") or "")
        product_url = str(row.get("product_url") or "").rstrip("/")
        key = item or product_url
        if not key or key in seen:
            continue
        seen.add(key)
        row = dict(row)
        row["unique_rank"] = len(out) + 1
        out.append(row)
    return out


def row_exclusion_reason(row: Dict[str, Any], excluded_urls: set[str]) -> Optional[str]:
    product_url = str(row.get("product_url") or "").rstrip("/")
    name = str(row.get("retailer_sku_name") or "")
    if product_url and product_url in excluded_urls:
        return "tv_item_mst.is_product=false"
    if EXCLUDED_NAME_RE.search(name):
        return "name_excluded_preowned_refurb_openbox"
    if not row.get("item") or not row.get("product_url"):
        return "missing_item_or_product_url"
    return None


def run_listing(args: argparse.Namespace, project_root: Path, out_dir: Path) -> Dict[str, Any]:
    from walmart_json_parser import search_items  # type: ignore

    raw_dir = out_dir / "raw" / "listing"
    excluded_urls = set() if args.no_mst_exclusion else load_excluded_product_urls()
    specs = {
        "main": ensure_listing_page_urls(
            load_page_urls(project_root, "wmart_tv_main_page_url", "main", args.main_pages),
            "main",
            args.main_pages,
        ),
        "bsr": ensure_listing_page_urls(
            load_page_urls(project_root, "wmart_tv_bsr_page_url", "bsr", args.bsr_pages),
            "bsr",
            args.bsr_pages,
        ),
    }
    page_types = ("main", "bsr") if args.only_type == "all" else (args.only_type,)
    all_by_type: Dict[str, List[Dict[str, Any]]] = {"main": [], "bsr": []}
    page_summaries: List[Dict[str, Any]] = []

    for page_type in page_types:
        for page_number, url in specs[page_type]:
            print(f"[listing {page_type} p{page_number}] GET {url}")
            result = fetch_html(url, args.timeout, args.retries, args.retry_sleep)
            html = result.pop("html", "")
            next_data = extract_next_data(html) if result["has_next_data"] else None
            raw_page_dir = raw_dir / page_type
            raw_page_dir.mkdir(parents=True, exist_ok=True)
            if args.save_html:
                (raw_page_dir / f"page_{page_number:02d}.html").write_text(html, encoding="utf-8", errors="replace")
            if next_data is not None:
                write_json(raw_page_dir / f"page_{page_number:02d}_next_data.json", next_data)
                parsed_rows = enrich_listing_rows(search_items(next_data), next_data)
                rows = normalize_listing_rows(
                    parsed_rows,
                    page_type=page_type,
                    page_number=page_number,
                    page_url=url,
                    start_rank=len(all_by_type[page_type]),
                )
            else:
                rows = []
            all_by_type[page_type].extend(rows)
            page_summary = {
                "stage": "listing",
                "page_type": page_type,
                "page_number": page_number,
                "url": url,
                "rows": len(rows),
                **{k: v for k, v in result.items() if k != "url"},
            }
            page_summaries.append(page_summary)
            print(
                f"[listing {page_type} p{page_number}] rows={len(rows)} "
                f"status={result['status']} next={result['has_next_data']} robot={result['robot_detected']}"
            )
            if len(dedupe_rows(all_by_type[page_type])) >= args.target_per_type:
                break
            time.sleep(args.between_pages)

    all_accepted: List[Dict[str, Any]] = []
    all_rejected: List[Dict[str, Any]] = []
    by_type: Dict[str, Any] = {}
    for page_type, rows in all_by_type.items():
        unique_rows = dedupe_rows(rows)
        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        for row in unique_rows:
            reason = row_exclusion_reason(row, excluded_urls)
            if reason:
                row = dict(row)
                row["exclude_reason"] = reason
                rejected.append(row)
            else:
                accepted.append(row)
        write_csv(out_dir / f"{page_type}_items.csv", accepted)
        write_json(out_dir / f"{page_type}_items.json", accepted)
        write_csv(out_dir / f"{page_type}_rejected.csv", rejected)
        all_accepted.extend(accepted)
        all_rejected.extend(rejected)
        by_type[page_type] = {
            "raw_rows": len(rows),
            "unique_rows": len(unique_rows),
            "accepted_items": len(accepted),
            "rejected_items": len(rejected),
            "target_met": len(accepted) >= args.target_per_type,
        }

    all_unique = dedupe_rows(all_accepted)
    write_csv(out_dir / "all_items.csv", all_accepted)
    write_csv(out_dir / "all_unique_items.csv", all_unique)
    write_csv(out_dir / "all_rejected.csv", all_rejected)
    return {
        "page_summaries": page_summaries,
        "by_type": by_type,
        "combined": {
            "accepted_rows": len(all_accepted),
            "unique_items": len(all_unique),
            "product_url_count": sum(1 for row in all_unique if row.get("product_url")),
        },
    }


def load_seed(path: Path, limit: int, start: int) -> List[Dict[str, str]]:
    rows = read_csv(path)
    rows = rows[start:]
    if limit > 0:
        rows = rows[:limit]
    return rows


LISTING_CONTEXT_FIELDS = [
    "rank",
    "page_type",
    "product_id",
    "product_url",
    "review_url",
    "sku_popularity",
    "inventory_status",
    "sku_status",
    "pick_up_availability",
    "fastest_delivery",
    "delivery_availability",
    "available_quantity_for_purchase",
    "number_of_ppl_purchased_yesterday",
    "number_of_ppl_added_to_carts",
    "offer",
    "discount_type",
    "page_number",
    "page_url",
    "page_rank",
    "main_rank",
    "main_page_number",
    "unique_rank",
    "bsr_rank",
    "bsr_page_number",
]


def apply_listing_context(row: Dict[str, Any], seed_row: Dict[str, str]) -> Dict[str, Any]:
    """Carry listing-only fields into detail/review rows without losing PDP values."""
    for field in LISTING_CONTEXT_FIELDS:
        seed_value = seed_row.get(field)
        if seed_value in (None, ""):
            continue
        if field == "page_type":
            row[field] = seed_value
        elif field in {
            "inventory_status",
            "sku_status",
            "pick_up_availability",
            "fastest_delivery",
            "delivery_availability",
            "available_quantity_for_purchase",
            "number_of_ppl_purchased_yesterday",
            "number_of_ppl_added_to_carts",
        }:
            row[field] = seed_value
        elif row.get(field) in (None, ""):
            row[field] = seed_value
    return row


def apply_detail_context(row: Dict[str, Any], detail_row_context: Dict[str, Any]) -> Dict[str, Any]:
    """Fill review rows with fields that are more reliable on the detail page."""
    if not detail_row_context:
        return row
    fallback_fields = [
        "star_rating",
        "count_of_star_ratings",
        "count_of_reviews",
        "final_sku_price",
        "original_sku_price",
        "savings",
        "discount_type",
        "sku_popularity",
        "model_year",
        "screen_size",
        "retailer_sku_name_similar",
        "offer",
    ]
    for field in fallback_fields:
        if row.get(field) in (None, "") and detail_row_context.get(field) not in (None, ""):
            row[field] = detail_row_context.get(field)
    return row


def run_detail_review(args: argparse.Namespace, project_root: Path, out_dir: Path) -> Dict[str, Any]:
    from walmart_json_parser import btf_rows, detail_row, review_collection_row, review_url  # type: ignore

    seed = args.seed or out_dir / "all_unique_items.csv"
    seeds = load_seed(seed, args.limit, args.start)
    raw_dir = out_dir / "raw" / "detail_review"
    detail_rows: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []
    item_summaries: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for offset, seed_row in enumerate(seeds, args.start + 1):
        product_url = seed_row.get("product_url") or ""
        item = seed_row.get("item") or item_from_url(product_url)
        if not item or not product_url:
            failures.append({"index": offset, "stage": "seed", "error": "missing item/product_url"})
            continue
        item_dir = raw_dir / str(item)
        item_summary: Dict[str, Any] = {"index": offset, "item": item, "product_url": product_url}

        print(f"[detail {item}] GET {product_url}")
        detail_result = fetch_html(product_url, args.timeout, args.retries, args.retry_sleep)
        detail_html = detail_result.pop("html", "")
        detail_next = extract_next_data(detail_html) if detail_result["has_next_data"] else None
        if args.save_html:
            item_dir.mkdir(parents=True, exist_ok=True)
            (item_dir / "detail.html").write_text(detail_html, encoding="utf-8", errors="replace")
        if detail_next is not None:
            write_json(item_dir / "detail_next_data.json", detail_next)
            try:
                drow = clean_row_values(detail_row(detail_next, args.max_reviews))
                apply_listing_context(drow, seed_row)
                fill_screen_size_fallback(drow)
                drow["seed_index"] = offset
                drow["seed_product_url"] = product_url
                similar_names = similar_names_from_rendered_html(detail_html)
                if similar_names:
                    drow["retailer_sku_name_similar"] = " ||| ".join(similar_names[: args.similar_limit])
                elif args.with_btf:
                    btf_result = fetch_btf(project_root, item, product_url, args.timeout, detail_next)
                    item_summary["btf_meta"] = btf_result["meta"]
                    if btf_result["data"] is not None:
                        write_json(item_dir / "btf_response.json", btf_result["data"])
                        similar_names = similar_names_from_btf_response(btf_result["data"])
                        item_summary["btf_similar_count"] = len(similar_names)
                        if similar_names:
                            drow["retailer_sku_name_similar"] = " ||| ".join(similar_names[: args.similar_limit])
                detail_rows.append(drow)
                item_summary["detail_ok"] = True
                item_summary["count_of_reviews"] = drow.get("count_of_reviews")
            except Exception as exc:
                failures.append({"index": offset, "item": item, "stage": "detail_parse", "error": str(exc)})
                item_summary["detail_ok"] = False
        else:
            failures.append({"index": offset, "item": item, "stage": "detail_fetch", "error": detail_result.get("error")})
            item_summary["detail_ok"] = False
        item_summary["detail_meta"] = {k: v for k, v in detail_result.items() if k != "url"}

        if not args.skip_reviews:
            review_nexts: List[Dict[str, Any]] = []
            review_count = safe_int(item_summary.get("count_of_reviews"))
            urls = [review_url(item) or f"https://www.walmart.com/reviews/product/{item}"]
            if review_count is None or review_count > 10:
                urls.append(page2_review_url(item))
            for page_index, review_page_url in enumerate(urls, 1):
                print(f"[review {item} p{page_index}] GET {review_page_url}")
                review_result = fetch_html(review_page_url, args.timeout, args.retries, args.retry_sleep)
                review_html = review_result.pop("html", "")
                review_next = extract_next_data(review_html) if review_result["has_next_data"] else None
                if (
                    review_next is not None
                    and page_index > 1
                    and safe_int(item_summary.get("count_of_reviews"))
                    and safe_int(item_summary.get("count_of_reviews")) > 10
                    and review_count_from_next(review_next) == 0
                ):
                    for fallback_url in page2_review_fallback_urls(item):
                        if fallback_url == review_page_url:
                            continue
                        fallback_result = fetch_html(fallback_url, args.timeout, args.retries, args.retry_sleep)
                        fallback_html = fallback_result.pop("html", "")
                        fallback_next = (
                            extract_next_data(fallback_html) if fallback_result["has_next_data"] else None
                        )
                        if review_count_from_next(fallback_next) > 0:
                            review_result = fallback_result
                            review_next = fallback_next
                            item_summary[f"review_p{page_index}_fallback_url"] = fallback_url
                            break
                if args.save_html:
                    item_dir.mkdir(parents=True, exist_ok=True)
                    (item_dir / f"review_p{page_index}.html").write_text(review_html, encoding="utf-8", errors="replace")
                if review_next is not None:
                    write_json(item_dir / f"review_p{page_index}_next_data.json", review_next)
                    review_nexts.append(review_next)
                else:
                    failures.append(
                        {
                            "index": offset,
                            "item": item,
                            "stage": f"review_p{page_index}_fetch",
                            "error": review_result.get("error"),
                        }
                    )
                item_summary[f"review_p{page_index}_meta"] = {k: v for k, v in review_result.items() if k != "url"}
                time.sleep(args.between_pages)
            if review_nexts:
                try:
                    rrow = clean_row_values(review_collection_row(review_nexts, args.max_reviews))
                    apply_listing_context(rrow, seed_row)
                    detail_context = (
                        detail_rows[-1]
                        if detail_rows and str(detail_rows[-1].get("item")) == str(item)
                        else {}
                    )
                    apply_detail_context(rrow, detail_context)
                    fill_screen_size_fallback(rrow)
                    rrow["seed_index"] = offset
                    rrow["seed_product_url"] = product_url
                    if detail_context:
                        similar = detail_context.get("retailer_sku_name_similar")
                        if similar:
                            rrow["retailer_sku_name_similar"] = similar
                    review_rows.append(rrow)
                    item_summary["review_ok"] = True
                    item_summary["review_pages_loaded"] = rrow.get("review_pages_loaded")
                    item_summary["review_extracted_count"] = rrow.get("review_extracted_count")
                except Exception as exc:
                    failures.append({"index": offset, "item": item, "stage": "review_parse", "error": str(exc)})
                    item_summary["review_ok"] = False
        item_summaries.append(item_summary)
        write_csv(out_dir / "detail_items_probe_raw.csv", detail_rows)
        write_csv(out_dir / "review_items_probe_raw.csv", review_rows)
        write_json(out_dir / "detail_review_summary.json", {"items": item_summaries, "failures": failures})
        time.sleep(args.between_items)

    write_csv(out_dir / "detail_items.csv", detail_rows)
    write_csv(out_dir / "review_items.csv", review_rows)
    return {
        "seed": str(seed),
        "detail_rows": len(detail_rows),
        "review_rows": len(review_rows),
        "failures": failures,
        "items": item_summaries,
    }


def content_layout_from_next(detail_next: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(detail_next, dict):
        return {}
    return (
        (((detail_next.get("props") or {}).get("pageProps") or {}).get("initialData") or {})
        .get("data", {})
        .get("contentLayout", {})
    )


def product_from_next(detail_next: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(detail_next, dict):
        return {}
    return (
        (((detail_next.get("props") or {}).get("pageProps") or {}).get("initialData") or {})
        .get("data", {})
        .get("product", {})
    )


def winner_details_from_product(product: Dict[str, Any]) -> Dict[str, Any]:
    store_id = (((product.get("location") or {}).get("mpPickupLocation") or {}).get("storeId")) or ""
    seller_id = product.get("sellerId") or ""
    options: List[str] = []
    for option in product.get("fulfillmentOptions") or []:
        if not isinstance(option, dict):
            continue
        if option.get("__typename") in {"PickupOptionV2", "DeliveryOptionV2", "ShippingOptionV2"}:
            if option.get("availabilityStatus") == "IN_STOCK" and option.get("type"):
                options.append(str(option.get("type")))
    return {"storeId": store_id, "sellerId": seller_id, "fulfillmentOptions": options}


def apply_current_btf_context(btf_vars: Dict[str, Any], item: str, detail_next: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = copy.deepcopy(btf_vars)
    out["iId"] = item
    layout = content_layout_from_next(detail_next)
    metadata = layout.get("pageMetadata") if isinstance(layout.get("pageMetadata"), dict) else {}
    lazy_modules = [m for m in metadata.get("lazyModules") or [] if isinstance(m, dict)]
    product = product_from_next(detail_next)
    if str(metadata.get("contentLayoutVersion") or "").upper() == "V2":
        out["version"] = "v2"
    if not lazy_modules:
        if isinstance(out.get("p13nCls"), dict):
            out["p13nCls"]["pageId"] = item
        return out
    out["p13nCls"] = {
        "pageId": item,
        "skipPtcFetch": True,
        "availabilityStatus": product.get("availabilityStatus"),
        "winnerDetails": winner_details_from_product(product),
        "p13NCallType": "BTF",
        "p13nMetadata": metadata.get("p13nMetadata") or "",
        "lazyModules": lazy_modules,
        "userClientInfo": {"isZipLocated": True, "callType": "CLIENT"},
        "userReqInfo": {
            "refererContext": {
                "source": "itempage",
                "variantSwitch": False,
                "itemSwitchContext": {"refererItem": None, "sizeReferer": None, "sizeReferers": None},
            }
        },
    }
    return out


def fetch_btf(
    project_root: Path,
    item: str,
    product_url: str,
    timeout: int,
    detail_next: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        from walmart_persisted_query_probe import (  # type: ignore
            ITEM_BTF_HASH,
            ITEM_BTF_NAME,
            find_seed,
            normalize_headers,
            request_json,
        )

        seed_path = project_root / "log" / "walmart_api_seeds_fullheaders.json"
        btf_seed = find_seed(seed_path, ITEM_BTF_NAME) or {}
        btf_vars = copy.deepcopy(
            ((btf_seed.get("body") or {}).get("variables") or {}) if isinstance(btf_seed.get("body"), dict) else {}
        )
        if not btf_vars:
            return {"meta": {"ok": False, "error": "missing_btf_seed_variables"}, "data": None}
        btf_vars = apply_current_btf_context(btf_vars, item, detail_next)
        btf_url = f"https://www.walmart.com/orchestra/pdp/graphql/{ITEM_BTF_NAME}/{ITEM_BTF_HASH}/ip/{item}"
        headers = normalize_headers(btf_seed.get("headers") or {}, product_url, ITEM_BTF_NAME)
        # Do not reuse stale browser cookies captured from an unrelated seed SKU.
        # P13N carousels are session-sensitive; old cookies can return a valid
        # but visibly different "Similar items" list.
        for key in list(headers):
            if key.lower() == "cookie":
                headers.pop(key, None)
        row, parsed, _text = request_json(
            "btf",
            "POST",
            btf_url,
            headers,
            {"variables": btf_vars},
            timeout,
        )
        return {"meta": row, "data": parsed if row.get("status") == 200 else None}
    except Exception as exc:
        return {"meta": {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "data": None}


def similar_names_from_btf_response(data: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    modules = (data.get("data") or {}).get("contentLayout", {}).get("modules") or []
    for module in modules:
        if not isinstance(module, dict):
            continue
        if module.get("type") != "ItemCarousel":
            continue
        cfg = module.get("configs") or {}
        title = str(cfg.get("title") or "").strip().lower()
        if title != "similar items you might like":
            continue
        for product in cfg.get("products") or []:
            if not isinstance(product, dict):
                continue
            name = str(product.get("name") or product.get("productName") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="Walmart raw HTTP collection")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--stage", choices=["listing", "detail-review", "all"], default="all")
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--main-pages", type=int, default=10)
    parser.add_argument("--bsr-pages", type=int, default=5)
    parser.add_argument("--only-type", choices=["all", "main", "bsr"], default="all")
    parser.add_argument("--target-per-type", type=int, default=300)
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--between-pages", type=float, default=0.8)
    parser.add_argument("--between-items", type=float, default=0.8)
    parser.add_argument("--max-reviews", type=int, default=20)
    parser.add_argument("--save-html", action="store_true")
    parser.add_argument("--no-mst-exclusion", action="store_true")
    parser.add_argument("--skip-reviews", action="store_true")
    parser.add_argument("--with-btf", action="store_true", help="Supplement detail rows with ItemByIdBtf GraphQL modules")
    parser.add_argument("--similar-limit", type=int, default=20)
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    setup_project_imports(project_root)
    out_dir = (args.out_dir or project_root / "log" / "walmart_raw_http_collection").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    summary: Dict[str, Any] = {
        "created_at": now_iso(),
        "project_root": str(project_root),
        "out_dir": str(out_dir),
        "stage": args.stage,
        "timings_sec": {},
    }
    if args.stage in ("listing", "all"):
        stage_start = time.perf_counter()
        summary["listing"] = run_listing(args, project_root, out_dir)
        summary["timings_sec"]["listing"] = round(time.perf_counter() - stage_start, 2)
    if args.stage in ("detail-review", "all"):
        stage_start = time.perf_counter()
        summary["detail_review"] = run_detail_review(args, project_root, out_dir)
        summary["timings_sec"]["detail_review"] = round(time.perf_counter() - stage_start, 2)
    summary["timings_sec"]["total"] = round(time.perf_counter() - started, 2)
    write_json(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
