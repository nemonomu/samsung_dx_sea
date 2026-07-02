import copy
import json
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

from zenrows import ZenRowsClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


ULTRA_MIN_QUERY = """
query PlpView_ProductList_Init(
  $input: SearchInput!
  $pagination: SearchPagination!
  $filter: SearchFilter
  $sort: SearchSort
  $testing: SearchTesting!
  $detailedSearchInput: SearchInput!
  $paginationForDetailedProductSearch: SearchPagination!
  $searchWithBestMediaInput: SearchWithBestMediaInput
  $productPriceInput: ProductItemPriceInput!
  $skuOffersInput: ProductSkuOffersInput!
) {
  detailedProductSearch: search(
    input: $detailedSearchInput
    pagination: $paginationForDetailedProductSearch
    filter: $filter
    sort: $sort
    testing: $testing
  ) {
    documents {
      ... on SearchProduct {
        product { ...UltraListingProduct }
      }
    }
  }
  search(input: $input pagination: $pagination filter: $filter sort: $sort testing: $testing) {
    withBestMedia(searchWithBestMedia: $searchWithBestMediaInput) {
      placements {
        id
        name
        documentsGridView {
          sponsoredDocuments {
            ... on SearchProduct {
              product { ...UltraListingProduct }
            }
            ... on SearchMediaProduct {
              source
              onLoadBeaconForSku
              onViewBeaconForSku
              onClickBeaconForSku
              product { ...UltraListingProduct }
            }
          }
        }
      }
    }
  }
}

fragment UltraListingProduct on Product {
  skuId
  bsin
  brand
  name { short title }
  url { pdp relativePdp skuSpecificUrl }
  primaryImage { piscesHref href }
  reviewInfo { averageRating reviewCount isReviewable }
  price(input: $productPriceInput) {
    customerPrice
    regularPrice
    totalSavings
    totalSavingsPercent
    displayableCustomerPrice
    displayableRegularPrice
  }
  offers(input: $skuOffersInput) {
    offers { hotOffer offerId offerType }
  }
}
""".strip()


def load_dotenv(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_candidate_dotenvs():
    for path in (ROOT / ".env", ROOT.parent / ".env", ROOT.parent.parent / ".env"):
        load_dotenv(path)


def json_dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def safe_json(text):
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def set_path(value, keys, new_value):
    current = value
    for key in keys[:-1]:
        if not isinstance(current, dict):
            return
        current = current.setdefault(key, {})
    if isinstance(current, dict):
        current[keys[-1]] = new_value


def delete_path(value, keys):
    current = value
    for key in keys[:-1]:
        if not isinstance(current, dict):
            return
        current = current.get(key)
    if isinstance(current, dict):
        current.pop(keys[-1], None)


def lean_payload(base_payload, page, variant):
    payload = copy.deepcopy(base_payload)
    variables = payload.setdefault("variables", {})

    for key in ("input", "detailedSearchInput"):
        if isinstance(variables.get(key), dict):
            variables[key]["query"] = os.getenv("BESTBUY_SEARCH_TERM", "tv")
            variables[key]["queryType"] = "SEARCH"
            variables[key]["site"] = "WWW"

    variables["categoryId"] = os.getenv("BESTBUY_SEARCH_TERM", "tv")
    variables.setdefault("sort", {})["sort"] = os.getenv("BESTBUY_SEARCH_SORT", "")
    for key in ("pagination", "paginationForDetailedProductSearch"):
        variables.setdefault(key, {})
        variables[key]["pageNumber"] = page
        variables[key]["offset"] = int(os.getenv("BESTBUY_MAIN_ORGANIC_OFFSET", "18"))

    # Do not ask listing to resolve fulfillment/store fragments. Detail and
    # availability steps collect these later.
    variables["isSafeMode"] = True
    variables["includeFragment"] = False
    variables["hasPreferredStoreZipCode"] = False
    variables["useWithBestMedia"] = True
    variables["fetchGridDocs"] = True
    variables["fetchListDocs"] = False

    if variant in {"safe_no_location", "safe_min_options"}:
        delete_path(variables, ["filter", "availability"])
        variables["destinationZipCode"] = ""
        variables["locationId"] = ""
        variables["preferredStoreZipCode"] = ""
        set_path(variables, ["fulfillmentInput", "shipping", "destinationZipCode"], "")
        set_path(variables, ["fulfillmentInput", "delivery", "destinationZipCode"], "")
        set_path(variables, ["fulfillmentInput", "ispu", "locationId"], "")

    if variant == "safe_min_options":
        for flag in (
            "multiImageEnabled",
            "useCaboSucoFields",
            "useEcoRebatesFields",
            "useGiftWithPurchaseFields",
            "useOffersFields",
            "usePlusXOffersFields",
            "usePromotionalOptionListFields",
            "useMembershipUpsellFields",
            "useSpendAndGetFields",
            "includeAppleIntelligenceFragment",
            "narrowerTermEnabled",
        ):
            variables[flag] = False
        variables["imageLimit"] = 1

    if variant == "ultra_min_query":
        keep = {
            "input",
            "pagination",
            "filter",
            "sort",
            "testing",
            "detailedSearchInput",
            "paginationForDetailedProductSearch",
            "searchWithBestMediaInput",
            "productPriceInput",
            "skuOffersInput",
        }
        variables = {key: value for key, value in variables.items() if key in keep}
        delete_path(variables, ["filter", "availability"])
        payload["variables"] = variables
        payload["query"] = ULTRA_MIN_QUERY
        payload["operationName"] = "PlpView_ProductList_Init"

    return payload


def request_params(session_id):
    params = {
        "custom_headers": "true",
        "premium_proxy": "true",
        "proxy_country": "us",
        "js_render": "true",
        "wait": os.getenv("BESTBUY_LEAN_WAIT_MS", "5000"),
        "session_id": str(session_id),
    }
    return params


def request_headers(referer):
    return {
        "accept": "application/graphql-response+json, application/json",
        "content-type": "application/json",
        "origin": "https://www.bestbuy.com",
        "referer": referer,
    }


def main():
    load_candidate_dotenvs()
    os.environ.setdefault("BESTBUY_CATEGORY", "TV")
    os.environ.setdefault("BESTBUY_SEARCH_TERM", "tv")

    from bestbuy import step01_main_list as listing

    api_key = os.getenv("ZENROWS_API_KEY")
    if not api_key:
        raise RuntimeError("ZENROWS_API_KEY is missing")

    source_payload = Path(os.getenv("BESTBUY_MAIN_SOURCE_PAYLOAD", "references/page_001_request.json"))
    base_payload = json.loads(source_payload.read_text(encoding="utf-8"))
    client = ZenRowsClient(api_key)
    session_id = int(os.getenv("BESTBUY_LEAN_SESSION_ID", str(1000 + secrets.randbelow(9000))))
    pages = [int(value) for value in os.getenv("BESTBUY_LEAN_PAGES", "1,2,3").split(",") if value.strip()]
    variants = [value.strip() for value in os.getenv(
        "BESTBUY_LEAN_VARIANTS",
        "safe_mode,safe_no_location,safe_min_options",
    ).split(",") if value.strip()]
    out_dir = ROOT / "task_logs" / "bby_listing_lean_graphql" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []

    for variant in variants:
        for page in pages:
            payload = lean_payload(base_payload, page, variant)
            referer = listing.build_search_url(page)
            params = request_params(session_id)
            headers = request_headers(referer)
            started = time.perf_counter()
            response = client.post(
                "https://www.bestbuy.com/gateway/graphql",
                params=params,
                headers=headers,
                data=json.dumps(payload),
                timeout=int(os.getenv("ZENROWS_TIMEOUT", "240")),
            )
            elapsed = round(time.perf_counter() - started, 3)
            response_json = safe_json(response.text)
            rows = listing.parse_page_rows(page, response_json) if response.status_code == 200 else []
            item = {
                "variant": variant,
                "page": page,
                "status_code": response.status_code,
                "zenrows_code": response_json.get("code") if isinstance(response_json, dict) else "",
                "cost": response.headers.get("x-request-cost", ""),
                "elapsed_seconds": elapsed,
                "bytes": len(response.content or b""),
                "row_count": len(rows),
                "unique_sku_count": len({str(row.get("sku_id") or "") for row in rows if row.get("sku_id")}),
                "body_head": (response.text or "")[:300],
            }
            summary.append(item)
            stem = f"{variant}_page_{page:03d}"
            json_dump(out_dir / f"{stem}_request.json", payload)
            json_dump(out_dir / f"{stem}_response.json", response_json)
            print(
                f"[lean] variant={variant} page={page:03d} status={item['status_code']} "
                f"rows={item['row_count']} unique={item['unique_sku_count']} "
                f"cost={item['cost']} elapsed={elapsed}s code={item['zenrows_code']}",
                flush=True,
            )
            sleep_seconds = float(os.getenv("BESTBUY_LEAN_SLEEP_SECONDS", "8"))
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    json_dump(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"out_dir={out_dir}")


if __name__ == "__main__":
    main()
