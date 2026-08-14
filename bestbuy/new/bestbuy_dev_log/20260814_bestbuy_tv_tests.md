# BestBuy TV Promotion Recovery Development Log - 2026-08-14

Updated at: 2026-08-14 13:24:36 +09:00 (Asia/Seoul)

## Scope

- Retailer/category: BestBuy / TV
- Branch/base: `fix/bestbuy-promotion-recovery` / `760bd5d`
- Affected stage: step05 promotion browser DOM collection and promotion-only DB recovery
- Source incident run: 2026-08-13 AWS RDP collection (`promotion listing sku 0/18`)
- Goal: correct the promotion source page, preserve the UI label exactly, and recover only promotion fields on existing main rows.

## Incident evidence and cause confirmation

- Failed run URL: `pcmcat1720647543741` (`TV Deals - Best Buy`).
- Failed artifacts: target headings absent, `container_found=false`, `row_count=0`; downstream `promotion_type` and `promotion_position` were blank for all rows.
- AWS RDP canary at 2026-08-13 22:21:51 +09:00 injected only the verified parent URL `pcmcat1690836748285`.
- Canary mode: `browser_dom`, headful Chrome, one external step05 invocation, no local code modification.
- Canary result: elapsed 30.026 seconds, `container_found=true`, 32 raw links/items, 16 unique rows, no collection error.
- Interpretation: the primary cause was the configured child TV Deals URL, not a changed promotion UI or downstream merge. The old `/18` threshold was also stale; the verified carousel contains 16 unique SKUs.
- Canary artifact root supplied from AWS RDP: `%TEMP%\bestbuy_promotion_url_test_20260813_222105`.

## Implemented changes

- Changed the default and CSV promotion URL to `pcmcat1690836748285`.
- Changed the expected minimum from 18 to 16 and made email reporting use the summary/config value.
- Changed the stored promotion label to exact `Don’t-miss deals on TVs` (U+2019); no uppercasing is applied downstream.
- Added run-root scoping for promotion/trending artifacts.
- Added `sos_refill --promotion-only` and `run_bestbuy_promotion_recovery.bat`.
  - Collects step05 only into timestamped staging.
  - Requires the verified URL, target container, exact label, and at least 16 unique SKUs.
  - Inner-joins the staged promotion SKUs with existing `page_type=main` rows in the original batch.
  - Does not add promotion-only, BSR-only, trend, or new product rows.
  - Builds a narrow overlay and invokes the existing promotion-only DB mode.
  - DB SQL updates only `promotion_type` and `promotion_position`: final table by batch/item/main page type and product-list table by batch/SKU/main page type.
  - Patches only those two fields in existing local artifacts after DB success; original artifacts are backed up under the recovery folder.
  - The BAT supports double-click recovery: enter only the `YYYYMMDD` run folder or press Enter to select the newest valid dated run, confirm with `Y`, and infer `batch_id` from `final_output.csv`.
  - Added a repository `.gitattributes` rule that checks out BAT files with CRLF so Windows `cmd.exe` parses the recovery launcher reliably.
- Reset `BESTBUY_DB_UPDATE_PROMOTION_ONLY=0` in normal full-run/recovery entry points to prevent inherited update-only mode.

## Local validation

Working directory: `bestbuy/new`

```powershell
python -m unittest discover -s tests -p 'test_*.py' -v
```

Result: 14 tests passed. Covered canonical URL/count/label, DOM SKU dedupe, existing-main-only overlay, no new SKU insertion, SKU/item collision rejection, final/product-list batch/main DB scope, exact update-count enforcement, multi-position normalization, two-column SQL, wrong-page/empty recovery rejection, artifact rollback, manifest-write transaction rollback, BAT argument handling, recovered-count email behavior, and custom run-root scoping.

```powershell
python -m py_compile bestbuy/step00_config.py bestbuy/step05_promotion_deals.py bestbuy/bestbuy_orchestrator.py bestbuy/step14_db_load.py bestbuy/step16_email_notify.py bestbuy/sos_refill.py tests/test_promotion_recovery.py
git diff --check
```

Result: passed; no syntax or whitespace errors. No live collection or DB mutation was performed from the local development machine.

Windows `cmd.exe` smoke checks:

- No-argument interactive launch with a temporary dated fixture selected the newest run after Enter, displayed the target scope, and exited `0` on `N` without invoking Python or DB work.
- A nonexistent date argument exited `2`, printed the resolved missing `final_output.csv`, and restored the caller's original code page.

## AWS RDP verification/recovery command

While the RDP session is connected, double-click `run_bestbuy_promotion_recovery.bat`. Enter the failed run folder name such as `20260815`, or press Enter to choose the newest valid dated run automatically. Verify the displayed full path and press `Y`; batch ID is inferred from `final_output.csv`.

The no-argument command-line form opens the same interactive prompt:

```powershell
.\run_bestbuy_promotion_recovery.bat
```

To start immediately without the confirmation prompt, pass a date folder name:

```powershell
.\run_bestbuy_promotion_recovery.bat 20260815
```

The exact-path form remains available for automation:

```powershell
.\run_bestbuy_promotion_recovery.bat "C:\samsung_dx_sea\bestbuy\new\bestbuy\data\tv\20260813"
```

Expected recovery manifest: `<run_root>\promotion_recovery\<timestamp>\manifest.json` with `status=completed`, `validation.unique_skus>=16`, `overlay.matched_main_skus>0`, and both DB results' `updated` counts equal to the overlay row count.

If AWS sets `BESTBUY_URL_SOURCE=db` explicitly, update the TV promotion row in `public.dx_target_page_url` to the verified URL before the next normal full run. The promotion-only recovery command itself forces the verified URL and is not affected by a stale DB URL.

## Files changed

- `.gitattributes`
- `bestbuy/step00_config.py`
- `bestbuy/config/bestbuy_initial_urls.csv`
- `bestbuy/step05_promotion_deals.py`
- `bestbuy/bestbuy_orchestrator.py`
- `bestbuy/sos_refill.py`
- `bestbuy/step14_db_load.py`
- `bestbuy/step16_email_notify.py`
- `_bby_daily_task.bat`
- `run_bestbuy_fullrun.bat`
- `run_bestbuy_full_recovery.bat`
- `run_bestbuy_promotion_recovery.bat`
- `README.md`
- `tests/test_promotion_recovery.py`
