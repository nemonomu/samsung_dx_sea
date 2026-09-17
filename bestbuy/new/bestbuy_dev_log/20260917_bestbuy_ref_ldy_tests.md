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

## 12:52 KST (Asia/Seoul): inspect REF_20260916_234024_9cd0fb ZIP

- User requested analysis of the RDP result archive at
  `C:\Users\kensi\Desktop\project\log\REF_20260916_234024_9cd0fb.zip`.
  Read selected JSON/CSV entries directly with Python `zipfile`; no extraction,
  browser launch or new site request. Archive remains unchanged.
- Actual run: REF one page, started 2026-09-16 23:40:24 -04:00, finished
  23:40:51 -04:00, elapsed 27.24 seconds. Listing HTTP 200; 24 occurrences,
  22 unique SKUs; zero passed offer rows, all 24 blank/unverified with
  `listing_graphql_errors`. Offer request count was **zero**.
- All 84 listing GraphQL errors are unrelated to main-card offer inputs:
  68 `fulfillmentOptions` errors (extension code `401`), 16 `arModels` errors
  (`NOT_FOUND`). Paths include both main products and nested open-box options.
  The previous unconditional listing-error guard blocked all offers before
  config/support requests. This is a collector error-handling defect, not proof
  of an offer API or numeric counting failure in this run.
- The five production module hashes in summary match local files after
  normalizing Git Windows CRLF checkout endings. The expected revision ran.
- Saved products for 6472693 / 6486389 / 6506246 contain progressively gift,
  tiered and membership inputs, but no live config/content/rebate responses
  were requested in this run; final displayed counts remain unverified.
- Four sponsored-ingrid SKUs have no price object: 6468484, 6477390, 6634588,
  6470555. They remain unknown, not zero. This missing sponsored data requires
  separate follow-up; do not remove these rows or call the whole sample passed.
- Fix in `step00_offer_graphql.py`: inspect the subtree immediately below the
  listing `product`; only fulfillmentOptions, arModels and separate openBoxOptions
  subtrees are classified as unrelated. Price/offers, ancestor-level, absent or
  unknown error paths still block verification. Retain unrelated errors in raw
  proof. No formula, page selection, navigation or fulfillment change.
- Diagnostic reports label unrelated errors `affects_offer: false`. Console
  shows a warning count, avoiding 84 repeated error lines; HTML retains full
  error detail. Real offer/API errors remain visible and do not pass silently.
- Offline verification: full `python -m unittest discover -s tests -p 'test_*.py'`
  from `bestbuy/new`: **98 passed**, 1.702 seconds. Windows temp tests used
  sandbox escalation. Also ran the new classifier on the actual ZIP's complete
  84-error array: blocking 0, unrelated 84. This replay tests classification only,
  not live collection. `git diff --check` passed.
- Files changed: collector, diagnostic report, their two test files, this log.
  Existing collected rows/requests/reports are untouched. Next: REF one-page live
  rerun to inspect actual config/content/rebate behavior and remaining sponsored
  gaps. Under-300 investigation remains deferred.

## 13:02 KST (Asia/Seoul): explicit missing SiteControl content

- Inspected `REF_20260916_235429_c9cb89.zip` directly without extracting the
  browser profile. Actual REF run: 2026-09-16 23:54:29–23:54:57 -04:00,
  27.5 seconds, listing HTTP 200, 24 occurrences / 21 unique SKUs.
  Config succeeded; support requested twice. Both support responses contain
  `o0: {rows: null}` and `NOT_FOUND` at `[o0, rows]` for offer 664995.
  Rebate responses succeeded (including empty program lists). Original result:
  zero passed, 21 rows unknown due to missing content proof, 3 due to missing price.
- Rechecked previously downloaded official PLP chunks in the local audit cache:
  `plp-2577.js` module 82577 `M` uses module 82656 `iQ`; `iQ` uses
  `errorPolicy: ignore` then `getSiteControlOfferContent`. The offer query builder
  `er` in module 90435 (`plp-90ff1eb4-2fac64fe7b289606.js`) requests the same
  `page: <offer ID>, view: native` shape. The content mapper maps null rows to
  an empty array. Thus this exact absent-content response contributes no top
  offer to the displayed count. An initial cache-file lookup used an incorrect
  filename; locating the actual `plp-` files resolved it without network access.
- Narrow fix: accept only an existing alias object with explicit `rows: null`
  and relevant errors exclusively `NOT_FOUND` at exactly `[alias, rows]`.
  Authentication, generic service/transport errors, global errors, absent alias
  data and child-field failures remain unknown. Record `absent_offer_content`
  and keep the original errors; diagnostic marks the explicit absent-content
  response with `handling: no_displayed_offer_content`.
- Replayed **all actual saved responses**, including live config, products and
  rebates, through the revised collector. No new HTTP request or synthetic
  successful auxiliary response was used. Result: offer 1 on 7 rows, 2 on 12,
  3 on 2, unknown on 3. Examples: 6472693=1; 6486389=2; 6506246=3; 6511564=3.
  Missing-price sponsored SKUs remain 6477390, 6634588, 6470555.
- Also passed these 24 replayed rows through the real step-02 normalization,
  step-07 final CSV and step-08 output_row functions. Actual CSV readback gives
  21 passed / 3 unknown. Saved an explicitly labeled replay report in ignored
  `offer_diagnostics/replay_REF_20260916_235429_c9cb89/report.html`, with summary,
  result CSVs and run.log. The optional ZenRows import was stubbed with a client
  that raises on construction to prevent paid-client execution; no fake network
  data was introduced. DB URL lookup/selectors were disabled; no DB/S3 writes.
- Added a small fixture projected from the captured public fields for three
  SKUs, config and actual support errors/rebates. No visitor/customer identifiers,
  cookies, headers or private browser-profile data are included. Fixtures record
  the source run and observation time and do not claim a current screen comparison.
- Command: `python -m unittest discover -s tests -p 'test_*.py'` from
  `bestbuy/new`, with escalation for Windows temp directories. **101 tests pass**
  in 1.973 seconds. Tests cover actual-response 1/2/3 replay, narrow NOT_FOUND
  acceptance and rejection of unrelated failure shapes. `git diff --check` passes.
- Changed: collector, diagnostic, two test files, public-field JSON fixture and
  this log. Formula and non-offer collection unchanged. This is a saved-response
  replay result, not a fresh live crawl or a screen comparison. Overall collection
  is still incomplete because the three sponsored rows have no price inputs.
  Under-300 investigation remains deferred.

## 13:12 KST (Asia/Seoul): manual listing comparison, test runner only

- User requested list-page links or keeping the collected browser visible for
  manual comparison, explicitly for diagnostic runs only. Added optional
  `--keep-browser` to `diagnose_graphql_offers.py`; production code is unchanged.
- Result HTML and console now include the exact list URL from production page
  metadata and the request ZIP. The page link preserves page/query parameters.
  The observed REF page-1 URL is
  `https://www.bestbuy.com/site/searchpage.jsp?id=pcat17071&st=refrigerator&intl=nosplash`.
- With `--keep-browser --open-report`, save results and open the report first,
  then keep test Chrome alive until Enter is pressed in PowerShell. Close on
  Enter, Ctrl+C/EOF during inspection, or report-writing failure. An interrupted
  collection skips the wait. Default behavior still closes the test browser.
  Headless plus keep-browser is rejected because it cannot serve visual review.
- No automated offer DOM reads, extra navigation or product requests were added.
  Chrome remains on the initial page even when later pages were collected via
  API. Report explains matching visible ZIP and using the existing Chrome
  session; a link opened by the default browser may use another profile.
- Check: `python -m unittest discover -s tests -p
  'test_graphql_offer_diagnostic.py'`, from `bestbuy/new`: **15 passed**, 0.139s.
  Tests verify save/open/wait/close ordering, cancellation and failure cleanup,
  exact page links and ZIP. Browser and opener are mocked; no real site access.
  Sandbox escalation was needed for Windows temp directories. `--help` and
  `git diff --check` passed; production-file diff is empty.
- Files changed: diagnostic runner, diagnostic tests and this log. No actual
  crawl artifacts were created; test files used temporary folders. Next runner
  command adds `--keep-browser` to the existing REF one-page diagnostic command.

## 13:36 KST (Asia/Seoul): missing-price audit of runner REF archive

- Target: REF step01/offer diagnostic, runner root
  `C:\samsung_dx_sea\bestbuy\new\offer_diagnostics\REF_20260917_002403_eea544`.
  Read the user-provided archive of that run with Python zipfile/json/csv;
  compared saved request, response, collected rows and summary without extracting
  browser profiles or reading cookies. No live requests, proxy, DB or S3 access.
- Runner conditions: one page, browser GraphQL, ZIP 10010, visible Chrome with
  keep-browser; grid documents enabled, list documents disabled, organic offset
  18. Recorded result: HTTP 200, 24 rows / 21 unique SKUs, 46.16 seconds,
  21 passing rows and 3 `incomplete_offer_proof:missing_price` rows.
- All 18 detailedProductSearch products contain price. The three failed SKUs
  6634588, 6477390 and 6470555 occur only in the sponsored grid response and have
  no price, offers or name there. Their saved raw_product_json also lacks price;
  this is not a price field lost during CSV serialization or product parsing.
- Of six collected sponsored occurrences, three share SKUs with the organic
  detailed products (6468479, 6360748, 6642584), so parse_page_rows merges the
  detailed product data into those rows. The three sponsored-only SKUs have no
  such source to merge and remain unknown.
- Saved request inspection: sponsored product fragments request only
  PlpViewSearchProductInfoFragment (badges/basic identity), while
  DetailedSearchProductFragment adds ProductListItemFragment including price
  experience and offers. Compared these three fragment definitions with
  `git show 38474f3:bestbuy/new/references/page_001_request.json`: unchanged.
  Thus the request omits required fields for sponsored-only products; no evidence
  here that the server dropped requested price data for these SKUs.
- Next: validate a narrowly scoped REF/LDY request addition for sponsored offer
  inputs, preserving selection/navigation and unknown-on-incomplete behavior.
  No collector/query changes made during this audit. No new raw artifacts or
  manifests, no live screen comparison. Under-300 investigation remains deferred.
  Only this development log changed.

## 13:44 KST (Asia/Seoul): whether the three sponsored SKUs are listing targets

- Read-only follow-up on the same REF runner archive, same one-page/ZIP 10010
  conditions. Inspected placement documents, rejected entries, displayDocuments,
  and local cached public Best Buy PLP JavaScript. No new network requests,
  browser access, cookies, proxy experiments, DB/S3 operations or crawl artifacts.
- `SEARCH_SPONSORED_INGRID.documentsGridView.sponsoredDocuments` contains an
  interleaved list of 24 documents: 18 organic and 6 source-A sponsored items.
  The missing-price SKUs occur at one-based document positions 4 (6634588),
  7 (6477390), and 14 (6470555). None appears in the placement's rejected list;
  that separate list has 18 rejected candidates with NOT_RELEVANT,
  NOT_AVAILABLE or NO_SPACE reasons. Placement.documents is empty.
- Previously downloaded public JS `plp-5076-6e75055dfe1a2f62.js` implements
  useDocumentList: falls back from placement.documents to grid sponsoredDocuments
  then list sponsoredDocuments. `plp-9368-ca1af624cb4b8dcb.js` consumes that hook
  and maps its items to DocumentGateway components. This supports including the
  three SKUs in this response's listing targets, rather than treating them as
  rejected candidates. Runtime overrides/experiments exist in the hooks, so this
  is not evidence that the user's separately loaded browser actually painted
  those cards or displayed offer labels.
- displayDocuments has rowNoFor3col/rowNoFor4col 3 and 5. Public JS also uses
  these positions for display-ad insertion; they are not a three-SKU allowlist.
  Do not use them alone to filter sponsored product cards.
- Existing parse_page_rows writes organic occurrences first and appends sponsored
  occurrences. Thus the same three items have stored visual_rank 20, 21 and 23,
  not source-array positions 4, 7 and 14. Report order is not a proven rendered
  screen order. Initial browser navigation and explicit diagnostic GraphQL fetch
  are separate requests, so their ad selections are not guaranteed identical.
- Result: retain these three as listing targets under the existing sponsored-
  inclusive policy; preserve UNKNOWN until price/offer proof is obtained. No
  production/test code changes, no rank/selection correction in this audit, no
  live screen comparison. Only this log changed; under-300 work remains deferred.

## 13:53 KST (Asia/Seoul): request sponsored offer inputs and prepare manual screen check

- User authorized the scoped REF/LDY offer request fix followed by one-page
  runner validation. Added PriceExperienceInit_Product to the existing
  PlpViewSearchMediaProductFragment product selection, in production payload
  preparation only for REF/LDY. This requests the same price/offer inputs used
  by organic cards for sponsored-only SKUs, without extra API operations or PDP
  navigation. Selection/rank/pagination/scroll code and offer arithmetic unchanged.
- The patch reuses existing declared fragments and variables; it is idempotent.
  An unfamiliar template raises a descriptive error before sending the request
  rather than silently treating incomplete inputs as complete. TV/HHP query
  contents remain unchanged. Missing price responses still produce UNKNOWN.
- Test runner only: --keep-browser now pauses after initial Chrome navigation,
  before the collection request. Operator sets the displayed delivery ZIP and
  must type that ZIP to continue; Q/Ctrl+C/EOF cancels. Confirmation is explicitly
  recorded as user_confirmed_zip, not automated DOM validation or offer accuracy.
  Normal noninteractive/headless runs do not acquire this manual setup step.
- HTML/results CSV label sponsored vs organic rows and explain why report order
  and separately fetched browser ad selections can differ. Screens without
  matching SKUs remain unverified; no screen-match status is automatically granted.
- Checks: `python -m unittest discover -s tests -p 'test_*.py'` from bestbuy/new:
  109 passed in 2.125 seconds, using escalation for Windows temporary directories.
  Added cases cover real-template scope/unchanged variables/idempotence, fail-fast
  template handling, sponsored-only parsing/counting and missing-price rejection,
  and explicit screen ZIP confirmation/cancellation. The sponsored response test
  uses controlled fixtures; it does not claim live resolution of the three SKUs.
- Applied amendment offline to the user's archived page_001_request.json:
  REF/LDY differ by exactly the 31-character fragment spread insertion; TV/HHP
  unchanged. --help and git diff --check pass. No new live requests, paid proxy,
  DB/S3 operations, raw response files or manifests. Existing saved responses
  cannot supply fields that were never requested; live acceptance remains pending.
- Changed: step00_offer_graphql.py, step01_main_list.py, diagnostic runner,
  test_offer_pipeline.py, test_graphql_offer_diagnostic.py, and this log. Next:
  push current feature branch, run REF --pages 1 --zip-code 10010 --open-report
  --keep-browser on RDP, inspect sponsored UNKNOWNs and compare same-SKU labels.
  Main merge and under-300 investigation are not part of this change.
