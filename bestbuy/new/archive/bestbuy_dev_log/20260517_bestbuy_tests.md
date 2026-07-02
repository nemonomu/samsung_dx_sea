# BestBuy Development Log - 2026-05-17

Updated at: 2026-05-17 15:50:09 +09:00

## 2026-05-21 정정

이 문서는 과거 실험 기록으로만 보관한다.

- Best Buy 운영 수집은 어떤 경우에도 직접 접속/direct/VPN-first 방식을 쓰지 않는다.
- guide.md 기준으로 ZenRows GraphQL 호출과 저장된 GraphQL 응답 파싱만 사용한다.
- `auto`, `direct`, `direct_first`, `fallback` 전송 모드는 폐기한다.
- `/gateway/graphql/fulfillment` 별도 fallback도 당분간 사용하지 않는다.

## Scope

Retailer: `BestBuy`

Goal: reduce crawler cost by trying direct/VPN-friendly collection before
ZenRows, and make benchmark CSVs append in realtime during long runs.

## Test Timeline

### 15:50:09 - Cost-first transport and realtime benchmarks

Context:

- User provided an external Best Buy VPN/browser crawler pattern that collects
  PDP data without ZenRows.
- User requested future crawls to keep at least two approaches and avoid
  ZenRows unless cheaper paths fail.
- User additionally requested main/detail benchmark files to append while the
  crawl runs instead of being written only at the end.

Implemented changes:

- `bestbuy/step01_main_list.py`
  - Added `BESTBUY_GRAPHQL_FETCH_MODE` / `BESTBUY_FETCH_MODE`.
  - Supported modes:
    - `direct`: post GraphQL directly with browser-like headers.
    - `auto`, `direct_first`, `fallback`: try direct first, then ZenRows.
    - `zenrows`: force ZenRows.
  - Added `transport` and `fetch_mode` metadata.
  - Appends `benchmarks/page_benchmarks.csv` page by page during the run.
- `bestbuy/step08_detail_enrichment.py`
  - Added `BESTBUY_DETAIL_FETCH_MODE` / `BESTBUY_FETCH_MODE`.
  - Detail HTML and review GraphQL now support direct first with ZenRows
    fallback.
  - Added transport/fetch mode metadata to detail and review meta files.
  - Appends `detail/benchmarks/detail_benchmarks.csv` per SKU during the run.
  - Uses a lock around benchmark append when workers run in parallel.
- `bestbuy/step00_detail_benchmarks.py`
  - Added append helper for realtime detail benchmark rows.
  - Added `detail_transport` and `review_transport` fields.

Validation:

```powershell
python -m py_compile amazon\step01_main_list.py amazon\step03_bsr_list.py amazon\step08_detail_enrichment.py bestbuy\step00_detail_benchmarks.py bestbuy\step01_main_list.py bestbuy\step08_detail_enrichment.py
```

Result: passed.

Recommended limited test:

```powershell
$env:BESTBUY_CATEGORY='TV'
$env:BESTBUY_GRAPHQL_FETCH_MODE='direct'
$env:BESTBUY_MAIN_PAGES='1'
python -m bestbuy.step01_main_list

$env:BESTBUY_DETAIL_FETCH_MODE='direct'
$env:BESTBUY_DETAIL_LIMIT='2'
$env:BESTBUY_DETAIL_WORKERS='1'
python -m bestbuy.step08_detail_enrichment
```

Fallback test:

```powershell
$env:BESTBUY_FETCH_MODE='auto'
```

This tries direct first and only uses ZenRows when direct collection does not
produce usable rows.

## 2026-05-21 - Best Buy fulfillment 검증

guide.md 기준으로 HTML/DOM은 스키마 위치 확인용으로만 보고, 운영 수집은 GraphQL 중심으로 검토했다.

검증 내용:

- 기존 5개 SKU `productBySkuId` alias batch는 정상 성공했다.
- 해당 기존 detail batch에는 `fulfillmentOptions`를 넣지 않았기 때문에 `pick_up_availability`, `fastest_delivery`, `delivery_availability`가 비어 있었다.
- detail alias batch에 `fulfillmentOptions`를 input 없이 추가한 1차 테스트는 ZenRows `RESP001`, HTTP 422로 실패했다. `X-Request-Cost=0`이라 과금은 없었다.
- PDP 샘플의 실제 구조를 반영해 `fulfillmentOptions(input:$fulfillmentInput)` 형태로 다시 5개 SKU 1회 batch 호출을 수행했다.
- 두 번째 호출은 HTTP 200, 5개 SKU 모두 `productBySkuId`는 반환됐지만, 각 SKU의 `fulfillmentOptions` path가 모두 GraphQL `401`로 내려왔다.
- 두 번째 호출 cost는 `0.0027996`이었다.

정정:

- `productBySkuId` detail 통합 batch 안에 간소화한 `fulfillmentOptions(input:$fulfillmentInput)`를 넣은 요청은 `fulfillmentOptions`만 `401`로 막혔다.
- 그러나 `references/bestbuy_detail_page_sample.html` 안에는 실제로 `fulfillmentOptions`가 내려온 성공 Apollo 응답이 있다.
- 기존 `step00_parse_pdp.py`는 `shippingDetails`, `deliveryDetails`, `ispuDetails`가 list 구조인데 dict처럼 읽고 있어 샘플 안의 availability를 제대로 파싱하지 못했다.
- 따라서 "detail 통합 불가"가 아니라, "샘플 성공 operation 원형 재현 및 parser 수정 필요"가 정확한 결론이다.
- 운영 우선순위는 `샘플 원형 기반 detail GraphQL에서 fulfillmentOptions까지 통합 수집 -> 실패 시 /gateway/graphql/fulfillment batch 보강`으로 둔다.

추가 수정:

- `step00_parse_pdp.py`에 list-aware `first_nested()`를 추가했다.
- 샘플 PDP 파싱 결과 `shipping_eligible=True`, `shipping_max_date=2026-05-16`, `delivery_eligible=True`, `pickup_eligible=True`, `pickup_max_date=2026-05-16`, `pickup_quantity=9999`를 확인했다.

관련 산출물:

- `GraphQL/bestbuy_dev_log/detail_alias_batch_5sku_probe/detail_with_fulfillment_probe/request.json`
- `GraphQL/bestbuy_dev_log/detail_alias_batch_5sku_probe/detail_with_fulfillment_probe/response.json`
- `GraphQL/bestbuy_dev_log/detail_alias_batch_5sku_probe/detail_with_fulfillment_probe/parsed_availability.csv`
- `GraphQL/bestbuy_dev_log/detail_alias_batch_5sku_probe/detail_with_fulfillment_probe/summary.json`
