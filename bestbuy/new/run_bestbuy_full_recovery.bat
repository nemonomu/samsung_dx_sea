@echo off
setlocal EnableExtensions

rem Full recovery for an existing run folder that lost detail data (the
rem "Failed to fetch" dead-page): re-fetch only the failed/missing-similar rows,
rem rebuild the FULL final_output.csv (recovers sku, ref_refrigerator_type,
rem ref_capacity, retailer_sku_name_similar), then FULL DB batch-replace by
rem batch_id (delete the batch + re-insert all rows) -- NOT similar-only.
rem
rem Use this instead of run_bestbuy_similar_recovery.bat when sku / ref_type /
rem capacity are also missing, not just retailer_sku_name_similar.
rem
rem Usage: run_bestbuy_full_recovery.bat [CATEGORY] [RUN_FOLDER]
rem   CATEGORY   default TV
rem   RUN_FOLDER default = newest yyyyMMdd folder under bestbuy\data\<category>

cd /d "%~dp0"

set "CATEGORY=%~1"
if "%CATEGORY%"=="" set "CATEGORY=TV"
set "CATEGORY=%CATEGORY:"=%"

for /f %%i in ('powershell -NoProfile -Command "'%CATEGORY%'.ToLowerInvariant()"') do set "CATEGORY_DIR=%%i"

set "RUN_FOLDER=%~2"
if "%RUN_FOLDER%"=="" (
  for /f %%i in ('powershell -NoProfile -Command "(Get-ChildItem -LiteralPath '%~dp0bestbuy\data\%CATEGORY_DIR%' -Directory | Where-Object { $_.Name -match '^\d{8}' } | Sort-Object Name | Select-Object -Last 1).Name"') do set "RUN_FOLDER=%%i"
)
if "%RUN_FOLDER%"=="" (
  echo [error] no run folder found under bestbuy\data\%CATEGORY_DIR%
  exit /b 1
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "RUN_TS=%%i"

set "BESTBUY_CATEGORY=%CATEGORY%"
set "BESTBUY_RUN_DATE=%RUN_FOLDER%"
set "BESTBUY_RUN_ROOT=%~dp0bestbuy\data\%CATEGORY_DIR%\%RUN_FOLDER%"
set "BESTBUY_OUTPUT_ROOT=%BESTBUY_RUN_ROOT%\output"
set "FINAL_OUTPUT=%BESTBUY_OUTPUT_ROOT%\final_output.csv"

if not exist "%FINAL_OUTPUT%" (
  echo [error] %FINAL_OUTPUT% not found; recovery needs an existing full-run output
  exit /b 1
)

rem Keep the ORIGINAL batch_id: step08 rewrites final_output.csv stamping
rem BESTBUY_BATCH_ID into every row, and the full DB replace deletes/re-inserts
rem exactly that batch_id, so the row set stays consistent.
for /f %%i in ('powershell -NoProfile -Command "(Import-Csv -LiteralPath '%FINAL_OUTPUT%' | Select-Object -First 1).batch_id"') do set "BESTBUY_BATCH_ID=%%i"
if "%BESTBUY_BATCH_ID%"=="" (
  echo [error] could not read batch_id from %FINAL_OUTPUT%
  exit /b 1
)

rem Let our overrides win over orchestrator per-step env (step env fills the rest as defaults).
set "BESTBUY_FORCE_STEP_ENV=0"

rem step08 scope: only SKUs whose final_output row is missing retailer_sku_name_similar
rem (the dead-page also blanks sku/ref_type on those rows) and whose detail cache
rem still shows failure (RETRY_ONLY). AUTO_RETRY + the browser session-recreate
rem hardening give the re-fetch a real chance this time.
set "BESTBUY_DETAIL_STAGE=detail"
set "BESTBUY_DETAIL_RETRY_ONLY=1"
set "BESTBUY_DETAIL_RETRY_MISSING_SIMILAR=1"
set "BESTBUY_DETAIL_RETRY_MISSING_SIMILAR_ALLOW_ALL=1"
set "BESTBUY_DETAIL_AUTO_RETRY=1"
set "BESTBUY_DETAIL_MAX_ATTEMPTS=3"
set "BESTBUY_DETAIL_RETRY_SLEEP_SECONDS=5"

rem DB: FULL batch-replace (delete batch_id + re-insert all rows), NOT similar-only.
set "BESTBUY_DB_UPDATE_SIMILAR_ONLY=0"
set "BESTBUY_DB_UPDATE_AVAILABILITY_ONLY=0"
if not defined BESTBUY_DB_LOAD_DRY_RUN set "BESTBUY_DB_LOAD_DRY_RUN=0"

set "PYTHONUNBUFFERED=1"

set "LOG_DIR=%BESTBUY_RUN_ROOT%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\full_recovery_%RUN_TS%.log"

echo ==================================================
echo BestBuy %CATEGORY% FULL recovery started
echo run_folder=%RUN_FOLDER%
echo batch_id=%BESTBUY_BATCH_ID%
echo run_root=%BESTBUY_RUN_ROOT%
echo log=%LOG_FILE%
echo ==================================================
echo BestBuy %CATEGORY% FULL recovery started > "%LOG_FILE%"
echo run_folder=%RUN_FOLDER% >> "%LOG_FILE%"
echo batch_id=%BESTBUY_BATCH_ID% >> "%LOG_FILE%"

echo.
echo [1/2] detail re-fetch (missing similar/sku) started
echo [1/2] detail re-fetch started >> "%LOG_FILE%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m bestbuy.bestbuy_orchestrator --category '%CATEGORY%' 08 2>&1 | Tee-Object -FilePath '%LOG_FILE%' -Append"
if errorlevel 1 (
  echo [1/2] detail re-fetch failed. See log: %LOG_FILE%
  echo [1/2] detail re-fetch failed >> "%LOG_FILE%"
  exit /b 1
)
echo [1/2] detail re-fetch completed
echo [1/2] detail re-fetch completed >> "%LOG_FILE%"

for /f %%i in ('powershell -NoProfile -Command "@(Import-Csv -LiteralPath '%FINAL_OUTPUT%' | Where-Object { [string]::IsNullOrWhiteSpace($_.sku) }).Count"') do set "STILL_MISSING_SKU=%%i"
for /f %%i in ('powershell -NoProfile -Command "@(Import-Csv -LiteralPath '%FINAL_OUTPUT%' | Where-Object { [string]::IsNullOrWhiteSpace($_.retailer_sku_name_similar) }).Count"') do set "STILL_MISSING_SIMILAR=%%i"
echo [check] rows still missing sku: %STILL_MISSING_SKU%
echo [check] rows still missing similar: %STILL_MISSING_SIMILAR%
echo [check] rows still missing sku: %STILL_MISSING_SKU% >> "%LOG_FILE%"
echo [check] rows still missing similar: %STILL_MISSING_SIMILAR% >> "%LOG_FILE%"

echo.
echo [2/2] db FULL batch-replace started
echo [2/2] db FULL batch-replace started >> "%LOG_FILE%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m bestbuy.bestbuy_orchestrator --category '%CATEGORY%' 15 2>&1 | Tee-Object -FilePath '%LOG_FILE%' -Append"
if errorlevel 1 (
  echo [2/2] db full load failed. See log: %LOG_FILE%
  echo [2/2] db full load failed >> "%LOG_FILE%"
  exit /b 1
)
echo [2/2] db full load completed
echo [2/2] db full load completed >> "%LOG_FILE%"

echo ==================================================
echo BestBuy %CATEGORY% FULL recovery completed
echo still_missing_sku=%STILL_MISSING_SKU% still_missing_similar=%STILL_MISSING_SIMILAR%
echo log=%LOG_FILE%
echo ==================================================
echo BestBuy %CATEGORY% FULL recovery completed >> "%LOG_FILE%"
exit /b 0
