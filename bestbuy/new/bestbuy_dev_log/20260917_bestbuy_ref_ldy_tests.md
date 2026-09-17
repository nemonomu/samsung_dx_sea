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

## 11:28 KST (Asia/Seoul): readable RDP acceptance-test runner

- User switched the RDP checkout to the clean branch and requested live testing
  with human-readable output, explicitly limited to the test presentation.
- Added `diagnose_graphql_offers.py`; production crawler files are unchanged.
  Run from repository root:
  `python bestbuy/new/diagnose_graphql_offers.py --category REF --pages 2 --open-report`.
  LDY uses the same command with `--category LDY` after REF results are reviewed.
- Uses orchestrator step-01 environment settings and the real listing payload,
  session initialization, page collector/retry and GraphQL offer collector.
  Initial listing navigation prepares the session; no DOM offer reads or added
  per-page navigation. No new collection formula is embedded in the runner.
- Real CSV files pass through step-02 normalization, step-07 enrichment, and
  step-08 `output_row` (only its offer output is retained). Detail fetching is
  not run. DB selectors, DB writes, S3 and notification stages are not invoked.
- Isolation: unique `bestbuy/new/offer_diagnostics/<category>_<timestamp>_<id>`
  output/profile root, explicit detail/output subdirectories, zero fixed browser
  port, CSV URL source, orchestrator category search term. ZIP/store and saved
  request settings otherwise come from runner configuration. These differences
  from a full production run are recorded in summary metadata. No paid proxy
  path is used; production transport is reused unchanged.
- Each completed page saves `collected_rows.csv`, `final_targets.csv`,
  `final_offer_values.csv`, `pages.json`, `offer_results.csv`, `summary.json`,
  `summary.txt`, and standalone `report.html`. Raw requests/responses/evidence
  remain under `main/raw/browser_graphql`; detailed runtime output is in
  `run.log`. Reports remain available on interruption/error. No ZIP is created.
  `.gitignore` excludes this local results/profile directory.
- Report distinguishes verified absence (blank CSV; displayed as zero),
  unverified data, CSV/final-output mismatch and missing 1/2/3 samples.
  A passing API/storage check explicitly does not certify agreement with the
  live screen. SKU links are optional manual inspection links only.
- Local checks: `python -m unittest discover -s tests -p
  'test_graphql_offer_diagnostic.py'` from `bestbuy/new`: 7 tests passed in
  0.082s. Windows temporary-directory tests used sandbox escalation. An earlier
  six-test run passed in 0.120s before adding the final-CSV zero-evidence-loss
  check. `--help` was checked from repo root; UTF-8 console setup fixes garbled
  Korean output under redirected PowerShell output. `git diff --check` passed.
- Local fixtures exercise real CSV and final-output functions for REF/LDY,
  corrupted/missing values, unknown versus zero, insufficient samples, HTML
  escaping and error-report generation. No live network/HTTP status or real
  collected product count is claimed from these checks. AWS/RDP live execution
  is the next step; collection totals below 300 have not been investigated.
- Changed files: the diagnostic runner, its seven offline tests, this log, and
  the test-output ignore rule. Production source diff is empty.

## 12:38 KST (Asia/Seoul): RDP failure; reuse successful listing data

- User-supplied live run: `REF_20260916_233200_880ec2`, REF main pages 1–2,
  24 rows each / 48 occurrences / 45 unique SKUs. Listing HTTP was 200 on both
  pages, but all 48 offer values were unverified. No offer sample passed.
  Runtime elapsed time and the runner's timezone were not supplied.
- Saved `page_001_offers.json` errors supplied by the user show both attempts of
  `OfferCountProducts` returning `Error - Internal Server Error`, path
  `productsBySkuIds`, extension code `INTERNAL_SERVER_ERROR`. This confirms the
  failing resolver; its internal server-side cause is unknown. It does not prove
  a schema error or anti-bot block. Do not call this live test successful.
- User also supplied the saved listing product for SKU 6467055. It includes
  Product/ItemPrice types, showPlusOffers, nullable mobileContracts, whatIfPrice,
  tieredOffersTracking, spendAndGetCabosAvailable, customerSelectedDiscountsAvailable,
  priceWithCart, giftSkus, isEcoRebateEligible and offers. The extra bulk product
  re-query duplicated data already present in the successful listing response.
- Production fix: remove `OfferCountProducts` / `productsBySkuIds` entirely.
  Read SKU-matched `raw_product_json` produced by the normal listing parser.
  Keep config and required content/rebate requests. Verify the listing offer
  input matches live config; reject missing/conflicting products or listing
  GraphQL partial errors rather than treating failed null fields as zero.
  Report product source and number of reused products. Formula, selection,
  navigation and TV/HHP behavior are unchanged.
- Test presentation fix: prior forced UTF-8 console output was garbled on the
  actual RDP PowerShell. Fixed console messages now use ASCII English; full
  Korean/product text remains in UTF-8 HTML/CSV/text reports. The diagnostic
  includes server message, operation, attempt, path and code in both the console
  and report. Raw response files remain unchanged. No archive is generated.
- Changed production files: `step00_offer_graphql.py`, `step01_main_list.py`.
  Changed test files: `diagnose_graphql_offers.py`, `test_offer_graphql.py`,
  `test_offer_pipeline.py`, `test_graphql_offer_diagnostic.py`; plus this log.
- Offline command: `python -m unittest discover -s tests -p 'test_*.py'` from
  `bestbuy/new`. Windows temp-directory execution required sandbox escalation.
  Result: **94 passed**, 1.892 seconds, exit 0. Coverage includes reused listing
  products, real nullable listing field shapes, SKU/context mismatch, partial
  GraphQL errors, unchanged 1/2/3 propagation, ASCII console and server-error
  visibility. Product fixtures remain offline fixtures; supporting responses
  are not newly captured live responses. `git diff --check` passed.
- Local work made no live requests and wrote no DB/S3 data. Production artifacts
  remain in the user's RDP run directory; only pasted excerpts were available
  locally. Next: run REF one page on RDP to validate config/content/rebate
  requests after removing the failed bulk lookup, then examine actual values.
  No claim of live accuracy yet; the under-300 investigation remains deferred.
