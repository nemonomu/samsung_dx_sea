# 2026-09-17 Best Buy REF / LDY clean rebuild

## 11:07 KST (Asia/Seoul): offline implementation checks

- Scope: rebuild from `38474f3`, before the September 14 offer change; keep
  August 18 detail GraphQL (`4f110e0`) and September 1 promotion (`bfcea85`).
  Apply only Lowe's `52e3751` and `bcd1aaf` to main as `0f9f202` and `498addf`.
  Main was pushed at `498addf`; its Best Buy tree equals `38474f3`.
- Branch: `fix/bestbuy-ref-ldy-graphql-clean`, based on main `498addf`.
  Previous work is preserved in local stash
  `684f3aa3998982afbe8a94a6d56dceffa7e4c6f3` before retiring the old branch.
- Product types and steps: REF/LDY listing step 01, normalization step 02,
  BSR CSV step 04, final targets step 07, detail output step 08.
  Run root: temporary local directories in the offline tests, removed afterward.
- Implementation: `step00_offer_graphql.py` obtains price-experience config,
  SKU price/offer inputs, and required promotional-content/rebate responses via
  the existing session's `/gateway/graphql` endpoint. Batched requests retry
  once on transport/GraphQL failure; timeout is capped at 30 seconds per request.
  No added navigation, scrolling, page-label parsing, or paid proxy request.
  Anonymous price context and ZIP are inherited from the listing payload;
  personalized contexts are marked unsupported. Cookies remain in the browser.
- Count evidence: membership, rebate, spend-and-get, displayed top-offer content,
  customer-selected discounts, and gifts follow the previously inspected PLP
  rules. Missing/malformed inputs produce a blank offer and `unverified` reason,
  never a fallback gift/hot-offer number. A verified absent label also has a
  blank output, but its evidence records zero components and `verified` status.
  `offer_graphql_json` carries SKU-matched evidence through intermediate CSVs.
- Prior analysis reused: PLP chunks `9555-03e4c0abcd69bdaa` and
  `2577.bd77f6fa706e7d58` were inspected earlier on this date. The product
  fixtures project earlier SSR observations for SKUs 6472693 / 6486389 /
  6506246 (labels 1 / 2 / 3). Content/rebate fixtures are synthetic. Earlier
  direct GraphQL transport attempts timed out; this rebuild performs no new
  live requests and does not establish that the live queries are accepted.
- Command: from `bestbuy/new`,
  `python -m unittest discover -s tests -p 'test_*.py'`.
  Local execution needed sandbox escalation for Windows temporary directories.
  Tests set `BESTBUY_CATEGORY=TV` and `BESTBUY_URL_SOURCE=default` by default,
  then patch REF/LDY per case. Transport is simulated; no network, browser
  process, credentials, DB, S3, or paid proxy is used. Printed browser/run logs
  come from mocks.
- Result: **82 tests passed**, 1.894 seconds, exit 0. Simulated 1/2/3 counts
  survive actual listing/normalization/BSR/final-CSV functions and the production
  detail offer expression. Checks cover verified zero, missing responses,
  API errors, SKU mismatch, duplicate row/order preservation, and unchanged
  TV/HHP behavior. No live HTTP status, error body, or real collected row count
  is available from this check. Lowe's separate suite previously passed 29 tests.
- Raw artifacts: simulated page request/response/meta/offer reports and CSVs
  were created only in temporary test directories. Actual runs will retain
  `raw/browser_graphql/*_offers.json` with request/response evidence and status,
  alongside page metadata and aggregate offer-request counts.
- Changed production files: `step00_offer_graphql.py` (new),
  `step01_main_list.py`, `step02_main_targets.py`, `step04_bsr_rank.py`,
  `step07_final_targets.py`, `step08_detail_enrichment.py`.
  New tests: `test_offer_graphql.py`, `test_offer_pipeline.py`.
- Scope audit: `step00_parse_search.py`, `step00_graphql_query.py`,
  `step00_config.py`, and `bestbuy_orchestrator.py` match main exactly.
  Detail changes are limited to the output offer expression. Selection/ranking
  functions and existing page-navigation settings are unchanged. Ancestor checks
  include `4f110e0`/`bfcea85` and exclude `8f2d963`/`08f9324`/`0650bb3`.
  `git diff --check` passed; only Windows line-ending notices were printed.
- Interpretation: offline implementation checks pass; live offer accuracy and
  live GraphQL compatibility remain unverified. Per the user's explicit order,
  report the completed branch rebuild first, then wait before running AWS live
  offer acceptance tests or investigating collection totals below 300.
