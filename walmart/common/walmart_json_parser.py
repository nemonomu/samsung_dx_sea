"""Parse Walmart structured JSON captured by probe.py.

Inputs supported:
  - search __NEXT_DATA__ JSON
  - detail __NEXT_DATA__ JSON
  - ItemByIdBtf GraphQL response JSON

This is a probe-stage parser: it favors visible field coverage and CSV review
over DB writes. Once fields are confirmed, the same helpers can be moved into
the production Walmart crawler.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = re.sub(r"\s+", " ", value).strip()
        return value or None
    return str(value)


def walk_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_values(child)


def text_fragments(value: Any) -> List[str]:
    out: List[str] = []
    for node in walk_values(value):
        if not isinstance(node, dict):
            continue
        for key in ("text", "label", "value", "slaText", "title"):
            raw = node.get(key)
            if isinstance(raw, str) and raw.strip():
                out.append(re.sub(r"\s+", " ", raw).strip())
    return out


def full_url(value: Any) -> Optional[str]:
    value = text(value)
    if not value:
        return None
    value = value.split("?", 1)[0]
    if value.startswith("/"):
        return "https://www.walmart.com" + value
    return value


def money_string(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return "${:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return None


def normalize_price_display(value: Any) -> Optional[str]:
    value = text(value)
    if not value:
        return None
    match = re.search(r"\$[\d,]+(?:\.\d{2})?", value)
    if match:
        return match.group(0)
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return money_string(value)
    return value


def display_rating(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        rounded = round(float(value), 1)
    except (TypeError, ValueError):
        return text(value)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.1f}"


def review_display_rating(reviews: Dict[str, Any], fallback: Any = None) -> Optional[str]:
    for key in ("roundedAverageOverallRating", "averageRating", "averageOverallRating"):
        rating = display_rating(reviews.get(key))
        if rating:
            return rating
    rating = display_rating(fallback)
    if rating:
        return rating
    count = review_rating_count(reviews) if isinstance(reviews, dict) else None
    if str(count or "").strip() in {"0", "0.0"}:
        return "No ratings yet"
    return None


def date_label(value: Any) -> Optional[str]:
    value = text(value)
    if not value:
        return None
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", value)
    if not match:
        return None
    try:
        from datetime import datetime, timedelta

        target = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        today = datetime.now().date()
    except ValueError:
        return None
    if target == today:
        return "today"
    if target == today + timedelta(days=1):
        return "tomorrow"
    return target.strftime("%a, %b ") + str(target.day)


def review_url(item_id: Any) -> Optional[str]:
    item_id = text(item_id)
    if not item_id:
        return None
    return f"https://www.walmart.com/reviews/product/{item_id}"


def item_from_url(value: Any) -> Optional[str]:
    value = text(value)
    if not value:
        return None
    match = re.search(r"/(?:ip|reviews/product)/(?:[^/?]+/)?(\d+)(?:[/?#]|$)", value)
    return match.group(1) if match else None


def nested(data: Dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def price_fields(price_info: Any) -> Dict[str, Any]:
    if not isinstance(price_info, dict):
        return {
            "final_sku_price": None,
            "original_sku_price": None,
            "savings": None,
            "discount_type": None,
        }

    def hidden_cart_price() -> Optional[str]:
        estimated = nested(price_info, "additionalFees", "estimatedTotalPrice", "priceString")
        if estimated and re.fullmatch(r"\$[\d,]+(?:\.\d{2})?", str(estimated)):
            return str(estimated)
        current_price = price_info.get("currentPrice")
        if isinstance(current_price, dict):
            return money_string(current_price.get("price"))
        return None

    def price_string(value: Any) -> Any:
        if isinstance(value, dict):
            display = value.get("priceString") or value.get("priceDisplay") or value.get("variantPriceString")
            if str(display or "").strip() == "See price in cart":
                return hidden_cart_price()
            return display or money_string(value.get("price")) or value.get("price")
        if str(value or "").strip() == "See price in cart":
            return hidden_cart_price()
        return value

    current = price_info.get("currentPrice")
    final_price = price_string(current)
    final_price = (
        final_price
        or price_string(price_info.get("linePriceDisplay"))
        or price_string(price_info.get("linePrice"))
        or price_string(price_info.get("itemPrice"))
        or price_string(price_info.get("priceDisplay"))
    )

    original_price = price_string(price_info.get("wasPrice") or price_info.get("listPrice"))
    savings = price_string(price_info.get("savings") or price_info.get("savingsAmt") or price_info.get("savingsAmount"))
    final_price = normalize_price_display(final_price)
    original_price = normalize_price_display(original_price)
    savings = normalize_price_display(savings)

    discount_type = "Price when purchased online" if final_price else None
    for key in ("priceDisplayCodes", "promoDiscount"):
        value = price_info.get(key)
        if isinstance(value, dict):
            code = value.get("priceDisplayType") or value.get("submapType")
            if code and str(code).upper() not in {"UNKNOWN", "CART"}:
                discount_type = text(code)

    return {
        "final_sku_price": text(final_price),
        "original_sku_price": text(original_price),
        "savings": text(savings),
        "discount_type": discount_type,
    }


def offer_count(item: Dict[str, Any]) -> Optional[str]:
    services = item.get("addOnServices")
    if not isinstance(services, list):
        return None
    for service in services:
        if not isinstance(service, dict):
            continue
        title = text(service.get("serviceTitle"))
        if not title or "free offer" not in title.lower():
            continue
        match = re.match(r"(\d+)\s+free offers?\b", title, re.I)
        if match:
            return match.group(1)
    return None


def badge_texts(item: Dict[str, Any]) -> List[str]:
    out: List[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            txt = value.get("text") or value.get("label") or value.get("value") or value.get("flag")
            if txt:
                out.append(str(txt))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for key in ("badges", "badge", "fulfillmentBadges", "fulfillmentBadgeGroups", "socialProofBadges"):
        visit(item.get(key))
    seen: List[str] = []
    for value in out:
        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def parse_count_text(value: Optional[str]) -> Optional[str]:
    value = text(value)
    if not value:
        return None
    match = re.search(r"([\d,.]+)\s*K\+", value, re.I)
    if match:
        return str(int(float(match.group(1).replace(",", "")) * 1000))
    match = re.search(r"([\d,]+)\+", value)
    if match:
        return match.group(1).replace(",", "")
    match = re.search(r"([\d,]+)", value)
    return match.group(1).replace(",", "") if match else None


def badge_signals(item: Dict[str, Any]) -> Dict[str, Any]:
    badges = badge_texts(item)
    sku_status = []
    sku_popularity = []
    inventory_status = None
    purchased = None
    carts = None

    if item.get("sponsoredProduct"):
        sku_status.append("Sponsored")

    for value in badges:
        lower = value.lower()
        if value in {"Rollback", "Reduced price", "Sponsored"} and value not in sku_status:
            sku_status.append(value)
        if any(token in lower for token in ("overall pick", "best seller", "popular pick")):
            sku_popularity.append(value)
        if "low stock" in lower:
            inventory_status = "Low stock"
        if "bought since yesterday" in lower:
            purchased = parse_count_text(value)
        if "people" in lower and "cart" in lower:
            carts = parse_count_text(value)

    return {
        "sku_status": ", ".join(sku_status) if sku_status else None,
        "sku_popularity": ", ".join(sku_popularity) if sku_popularity else None,
        "inventory_status": inventory_status,
        "number_of_ppl_purchased_yesterday": purchased,
        "number_of_ppl_added_to_carts": carts,
        "all_badges": badges,
    }


def fulfillment_options_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "pick_up_availability": None,
        "fastest_delivery": None,
        "delivery_availability": None,
    }
    options = item.get("fulfillmentOptions") or []
    if not isinstance(options, list):
        return out
    for option in options:
        if not isinstance(option, dict) or option.get("availabilityStatus") != "IN_STOCK":
            continue
        typ = str(option.get("type") or option.get("__typename") or "").upper()
        speed = option.get("speedDetails") or {}
        if not isinstance(speed, dict):
            speed = {}
        if "SHIPPING" in typ and out["fastest_delivery"] is None:
            if str(speed.get("fulfillmentBadge") or "").lower() == "today":
                out["fastest_delivery"] = "today"
            else:
                out["fastest_delivery"] = date_label(speed.get("deliveryDate") or speed.get("maxDeliveryDate"))
        elif "PICKUP" in typ and out["pick_up_availability"] is None:
            out["pick_up_availability"] = text(speed.get("slaText"))
        elif "DELIVERY" in typ and out["delivery_availability"] is None:
            out["delivery_availability"] = text(speed.get("slaText"))
    return out


def shipping_arrival_text(item: Dict[str, Any]) -> Optional[str]:
    for value in text_fragments(item):
        lower = value.lower()
        if "arrives" not in lower:
            continue
        if "tomorrow" in lower:
            return "tomorrow"
        if "today" in lower:
            return "today"
        match = re.search(r"arrives\s+(in\s+3\+\s+days)", value, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
        match = re.search(r"arrives\s+((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+[A-Z][a-z]{2}\s+\d{1,2})", value)
        if match:
            return match.group(1).strip()
    return None


def fulfillment_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    out = fulfillment_options_fields(item)
    arrival = shipping_arrival_text(item)
    if arrival and (out["fastest_delivery"] is None or str(out["fastest_delivery"]).lower() == "available"):
        out["fastest_delivery"] = arrival
    groups = item.get("fulfillmentBadgeGroups") or []
    if not isinstance(groups, list):
        return out
    for group in groups:
        if not isinstance(group, dict):
            continue
        key = str(group.get("key") or "").upper()
        value = text(group.get("slaText"))
        if not value:
            continue
        if "DELIVERY" in key and out["delivery_availability"] is None:
            out["delivery_availability"] = value
        elif "SHIPPING" in key and out["fastest_delivery"] is None:
            out["fastest_delivery"] = value
        elif "PICKUP" in key and out["pick_up_availability"] is None:
            out["pick_up_availability"] = value
    return out


def status_value(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return text(value.get("display") or value.get("value"))
    return text(value)


def item_row(item: Dict[str, Any], rank: Optional[int] = None, page_type: Optional[str] = None) -> Dict[str, Any]:
    price = price_fields(item.get("priceInfo"))
    signals = badge_signals(item)
    fulfillment = fulfillment_fields(item)
    name = text(item.get("name") or item.get("productName"))
    screen_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:"|in\b|inch|inches)', name or "", re.I)
    row = {
        "rank": rank,
        "page_type": page_type,
        "item": text(item.get("usItemId") or item.get("id")),
        "product_id": text(item.get("id")),
        "retailer_sku_name": name,
        "brand": text(item.get("brand") or item.get("manufacturerName")),
        "product_url": full_url(item.get("canonicalUrl")),
        "review_url": review_url(item.get("usItemId") or item.get("id")),
        "star_rating": item.get("averageRating"),
        "count_of_star_ratings": item.get("numberOfReviews"),
        "count_of_reviews": None,
        "screen_size": f"{screen_match.group(1)} inches" if screen_match else None,
        "sku_popularity": signals["sku_popularity"],
        "inventory_status": signals["inventory_status"],
        "sku_status": signals["sku_status"],
        "seller_id": text(item.get("sellerId")),
        "seller_name": text(item.get("sellerName")),
        "fulfillment_type": text(item.get("fulfillmentType")),
        "fulfillment_title": text(item.get("fulfillmentTitle")),
        "pick_up_availability": fulfillment["pick_up_availability"],
        "fastest_delivery": fulfillment["fastest_delivery"],
        "delivery_availability": fulfillment["delivery_availability"],
        "available_quantity_for_purchase": None,
        "number_of_ppl_purchased_yesterday": signals["number_of_ppl_purchased_yesterday"],
        "number_of_ppl_added_to_carts": signals["number_of_ppl_added_to_carts"],
        "offer": offer_count(item),
        "model_year": (re.search(r"\b(20\d{2})\b", name or "").group(1) if re.search(r"\b(20\d{2})\b", name or "") else None),
    }
    row.update(price)
    return row


def search_items(next_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    search = nested(next_data, "props", "pageProps", "initialData", "searchResult") or {}
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    rank = 0
    for stack in search.get("itemStacks") or []:
        for item in stack.get("items") or []:
            if not isinstance(item, dict) or not item.get("usItemId"):
                continue
            key = str(item.get("usItemId"))
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            rows.append(item_row(item, rank=rank, page_type="main"))
    return rows


def specs_map(idml: Dict[str, Any]) -> Dict[str, str]:
    specs: Dict[str, str] = {}
    for item in idml.get("specifications") or []:
        if not isinstance(item, dict):
            continue
        name = text(item.get("name") or item.get("key"))
        value = text(item.get("value"))
        if name and value:
            specs[name.lower()] = value
    return specs


def first_spec(specs: Dict[str, str], *needles: str) -> Optional[str]:
    for needle in needles:
        needle_l = needle.lower()
        for key, value in specs.items():
            if needle_l in key:
                return value
    return None


def format_review_list(review_items: Iterable[Dict[str, Any]], max_reviews: int) -> Optional[str]:
    rows = []
    seen: set[str] = set()
    for review in review_items:
        if len(rows) >= max_reviews:
            break
        if not isinstance(review, dict):
            continue
        review_id = text(review.get("reviewId") or review.get("reviewReferenceId"))
        if review_id and review_id in seen:
            continue
        if review_id:
            seen.add(review_id)
        body = text(review.get("reviewText"))
        title = text(review.get("reviewTitle"))
        review_text = body or title
        if review_text:
            rows.append(f"review{len(rows) + 1} - {review_text}")
    return " ||| ".join(rows) if rows else None


def format_reviews(reviews: Dict[str, Any], max_reviews: int) -> Optional[str]:
    return format_review_list(reviews.get("customerReviews") or [], max_reviews)


def review_aspects(reviews: Dict[str, Any]) -> Optional[str]:
    aspects = []
    for item in reviews.get("aspects") or []:
        if isinstance(item, dict) and item.get("name"):
            aspects.append(f"{item.get('name')} ({item.get('snippetCount')})")
    return ", ".join(aspects[:20]) if aspects else None


def review_rating_count(reviews: Dict[str, Any]) -> Any:
    value = reviews.get("reviewAndRatingCountAsString")
    if isinstance(value, dict):
        return value.get("totalReviewsCountAsString") or value.get("reviewsWithTextCountAsString")
    return value or reviews.get("totalReviewCount")


def review_text_count(reviews: Dict[str, Any]) -> Any:
    value = reviews.get("reviewAndRatingCountAsString")
    if isinstance(value, dict):
        return value.get("reviewsWithTextCountAsString") or value.get("filteredReviewsCountAsString")
    return reviews.get("reviewsWithTextCount")


def detail_row(next_data: Dict[str, Any], max_reviews: int = 20) -> Dict[str, Any]:
    data = nested(next_data, "props", "pageProps", "initialData", "data") or {}
    product = data.get("product") or {}
    reviews = data.get("reviews") or {}
    idml = data.get("idml") or {}
    seo = data.get("seoItemMetaData") or {}
    specs = specs_map(idml)

    row = item_row(product, page_type="detail")
    row.update(
        {
            "item": text(product.get("usItemId") or product.get("primaryProductId") or row.get("item")),
            "retailer_sku_name": text(product.get("name") or row.get("retailer_sku_name")),
            "brand": text(product.get("brand") or seo.get("brand") or row.get("brand")),
            "model": text(product.get("model") or product.get("manufacturerProductId")),
            "screen_size": first_spec(specs, "screen size", "display size"),
            "resolution": first_spec(specs, "resolution"),
            "display_type": first_spec(specs, "display"),
            "refresh_rate": first_spec(specs, "refresh rate"),
            "model_year": first_spec(specs, "model year") or row.get("model_year"),
            "count_of_star_ratings": review_rating_count(reviews),
            "count_of_reviews": review_text_count(reviews) or row.get("count_of_reviews"),
            "star_rating": review_display_rating(reviews, row.get("star_rating")),
            "reviews_with_text_count": reviews.get("reviewsWithTextCount"),
            "recommended_percentage": reviews.get("recommendedPercentage"),
            "top_mentions": review_aspects(reviews),
            "summarized_review_content": text(
                reviews.get("reviewSummary")
                or reviews.get("bulletReviewSummary")
                or nested(idml, "genAiDetails", "genAiDescription")
            ),
            "detailed_review_content": format_reviews(reviews, max_reviews),
            "short_description": text(product.get("shortDescription") or idml.get("shortDescription")),
            "long_description": text(idml.get("longDescription")),
            "sku_status": row.get("sku_status"),
        }
    )
    return row


def review_page_data(next_data: Dict[str, Any]) -> Dict[str, Any]:
    return nested(next_data, "props", "pageProps", "initialData", "data") or {}


def review_collection_row(next_datas: List[Dict[str, Any]], max_reviews: int = 20) -> Dict[str, Any]:
    first_data = review_page_data(next_datas[0]) if next_datas else {}
    product = first_data.get("product") or {}
    first_reviews = first_data.get("reviews") or {}
    total_reviews = review_text_count(first_reviews) or first_reviews.get("totalReviewCount")
    try:
        target_reviews = min(int(total_reviews), max_reviews) if total_reviews is not None else max_reviews
    except (TypeError, ValueError):
        target_reviews = max_reviews

    combined_reviews: List[Dict[str, Any]] = []
    pages_loaded = 0
    for next_data in next_datas:
        data = review_page_data(next_data)
        reviews = data.get("reviews") or {}
        page_reviews = reviews.get("customerReviews") or []
        if page_reviews:
            pages_loaded += 1
        for review in page_reviews:
            if isinstance(review, dict):
                combined_reviews.append(review)
        if len(combined_reviews) >= target_reviews:
            break

    item_id = text(product.get("usItemId") or product.get("primaryProductId")) or item_from_url(product.get("canonicalUrl"))
    row = item_row(product, page_type="review")
    row.update(
        {
            "item": item_id or row.get("item"),
            "review_url": review_url(item_id or row.get("item")),
            "retailer_sku_name": text(product.get("name") or row.get("retailer_sku_name")),
            "count_of_reviews": total_reviews,
            "count_of_star_ratings": review_rating_count(first_reviews),
            "star_rating": review_display_rating(first_reviews, row.get("star_rating")),
            "reviews_with_text_count": first_reviews.get("reviewsWithTextCount"),
            "detailed_review_content": format_review_list(combined_reviews, target_reviews),
            "review_pages_loaded": pages_loaded,
            "review_target_count": target_reviews,
            "review_extracted_count": min(len(combined_reviews), target_reviews),
        }
    )
    return row


def btf_rows(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = nested(response, "data", "contentLayout") or {}
    rows: List[Dict[str, Any]] = []
    for module_index, module in enumerate(content.get("modules") or []):
        if not isinstance(module, dict):
            continue
        cfg = module.get("configs") or {}
        module_type = module.get("type") or cfg.get("__typename")
        module_name = module.get("name")
        candidates = []
        candidates.extend(cfg.get("compChartItems") or [])
        candidates.extend(cfg.get("products") or [])
        for item in candidates:
            if not isinstance(item, dict) or not (item.get("usItemId") or item.get("id")):
                continue
            row = item_row(item, rank=len(rows) + 1, page_type="btf")
            row["module_index"] = module_index
            row["module_type"] = module_type
            row["module_name"] = module_name
            rows.append(row)
    return rows


def csv_fieldnames(rows: Iterable[Dict[str, Any]]) -> List[str]:
    fields: List[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    return fields


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fieldnames(rows))
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def print_preview(name: str, rows: List[Dict[str, Any]], limit: int = 5) -> None:
    def safe_print(value: str) -> None:
        encoding = sys.stdout.encoding or "utf-8"
        print(value.encode(encoding, errors="replace").decode(encoding, errors="replace"))

    safe_print(f"[{name}] rows={len(rows)}")
    for row in rows[:limit]:
        safe_print(
            "{item}\t{final_sku_price}\t{star_rating}\t{count_of_reviews}\t{retailer_sku_name}".format(
                item=row.get("item"),
                final_sku_price=row.get("final_sku_price"),
                star_rating=row.get("star_rating"),
                count_of_reviews=row.get("count_of_reviews"),
                retailer_sku_name=(row.get("retailer_sku_name") or "")[:90],
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse Walmart probe JSON outputs")
    parser.add_argument("--search-next", type=Path)
    parser.add_argument("--detail-next", type=Path)
    parser.add_argument("--review-next", type=Path, action="append", default=[], help="Review page __NEXT_DATA__ JSON; pass page1 and page2 to merge up to max reviews")
    parser.add_argument("--btf-response", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("log/walmart_parsed"))
    parser.add_argument("--max-reviews", type=int, default=20)
    args = parser.parse_args()

    summary: Dict[str, Any] = {}

    if args.search_next:
        rows = search_items(load_json(args.search_next))
        write_csv(args.out_dir / "search_items.csv", rows)
        write_json(args.out_dir / "search_items.json", rows)
        print_preview("search", rows)
        summary["search_rows"] = len(rows)

    if args.detail_next:
        row = detail_row(load_json(args.detail_next), args.max_reviews)
        write_csv(args.out_dir / "detail_item.csv", [row])
        write_json(args.out_dir / "detail_item.json", row)
        print_preview("detail", [row])
        summary["detail_item"] = row.get("item")
        summary["detail_reviews"] = row.get("count_of_reviews")

    if args.review_next:
        row = review_collection_row([load_json(path) for path in args.review_next], args.max_reviews)
        write_csv(args.out_dir / "review_item.csv", [row])
        write_json(args.out_dir / "review_item.json", row)
        print_preview("review", [row])
        summary["review_item"] = row.get("item")
        summary["review_pages_loaded"] = row.get("review_pages_loaded")
        summary["review_extracted_count"] = row.get("review_extracted_count")

    if args.btf_response:
        rows = btf_rows(load_json(args.btf_response))
        write_csv(args.out_dir / "btf_items.csv", rows)
        write_json(args.out_dir / "btf_items.json", rows)
        print_preview("btf", rows)
        summary["btf_rows"] = len(rows)

    write_json(args.out_dir / "summary.json", summary)
    print(f"[saved] {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
