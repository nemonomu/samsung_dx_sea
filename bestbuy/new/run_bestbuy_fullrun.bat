@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "CATEGORY=%~1"
if "%CATEGORY%"=="" set "CATEGORY=TV"
set "CATEGORY=%CATEGORY:"=%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "RUN_DATE=%%i"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "RUN_TS=%%i"
for /f %%i in ('powershell -NoProfile -Command "'%CATEGORY%'.ToLowerInvariant()"') do set "CATEGORY_DIR=%%i"
for /f %%i in ('powershell -NoProfile -Command "$categoryRoot = Join-Path '%~dp0bestbuy\data' '%CATEGORY_DIR%'; $baseName = '%RUN_DATE%'; $candidate = Join-Path $categoryRoot $baseName; if (-not (Test-Path -LiteralPath $candidate)) { $baseName } else { $n = 2; do { $name = '{0}_{1}' -f $baseName, $n; $candidate = Join-Path $categoryRoot $name; $n++ } while (Test-Path -LiteralPath $candidate); $name }"') do set "RUN_FOLDER=%%i"

set "BESTBUY_CATEGORY=%CATEGORY%"
set "BESTBUY_RUN_DATE=%RUN_FOLDER%"
set "BESTBUY_RUN_ROOT=%~dp0bestbuy\data\%CATEGORY_DIR%\%RUN_FOLDER%"
set "BESTBUY_OUTPUT_ROOT=%BESTBUY_RUN_ROOT%\output"
set "BESTBUY_DETAIL_RUN_ROOT=%BESTBUY_RUN_ROOT%\detail"
set "BESTBUY_DETAIL_TARGET_CSV=%BESTBUY_OUTPUT_ROOT%\bestbuy_final_targets.csv"
set "BESTBUY_FINAL_OUTPUT_CSV=%BESTBUY_OUTPUT_ROOT%\final_output.csv"
set "BESTBUY_PRODUCT_LIST_OUTPUT=%BESTBUY_OUTPUT_ROOT%\bestbuy_product_list.csv"
set "BESTBUY_ITEM_MST_OUTPUT_CSV=%BESTBUY_OUTPUT_ROOT%\item_mst.csv"
set "BESTBUY_BATCH_ID=b_%RUN_TS%"
set "BESTBUY_FETCH_MODE=zenrows"
set "BESTBUY_GRAPHQL_FETCH_MODE=zenrows"
set "BESTBUY_DETAIL_FETCH_MODE=zenrows"
set "BESTBUY_FETCH_SPONSORED_ENRICHMENT=0"
set "BESTBUY_DETAIL_FETCH_COMPARE=1"
if not defined BESTBUY_DETAIL_LIMIT set "BESTBUY_DETAIL_LIMIT=0"
if not defined BESTBUY_DETAIL_SKUS set "BESTBUY_DETAIL_SKUS="
set "BESTBUY_DETAIL_RETRY_ONLY=0"
set "BESTBUY_DETAIL_REBUILD_ONLY=0"
set "BESTBUY_DETAIL_RETRY_MISSING_SIMILAR=0"
set "BESTBUY_DETAIL_FETCH_GET_IT_FAST=0"
set "BESTBUY_DETAIL_FETCH_FULFILLMENT_DYNAMIC=0"
set "BESTBUY_DETAIL_SKU_BATCH_SIZE=5"
set "BESTBUY_DETAIL_WORKERS=3"
set "BESTBUY_AVAILABILITY_BACKFILL_BATCH_ID=%BESTBUY_BATCH_ID%"
set "BESTBUY_AVAILABILITY_BACKFILL_FINAL_CSV=%BESTBUY_FINAL_OUTPUT_CSV%"
set "BESTBUY_AVAILABILITY_BACKFILL_DETAIL_ROWS_CSV=%BESTBUY_DETAIL_RUN_ROOT%\parsed\detail_enriched_rows.csv"
set "BESTBUY_AVAILABILITY_BACKFILL_ROOT=%BESTBUY_RUN_ROOT%\availability_backfill"
set "BESTBUY_AVAILABILITY_BACKFILL_CHUNK_SIZE=1"
set "BESTBUY_AVAILABILITY_BACKFILL_ALLOW_MULTI_SKU=0"
set "BESTBUY_AVAILABILITY_BACKFILL_CANDIDATE_MODE=all_rows"
set "BESTBUY_AVAILABILITY_BACKFILL_OVERWRITE=1"
set "BESTBUY_AVAILABILITY_BACKFILL_CLEAR_EXISTING_FIELDS=1"
set "BESTBUY_AVAILABILITY_BACKFILL_SKIP=0"
if not defined BESTBUY_AVAILABILITY_BACKFILL_LIMIT set "BESTBUY_AVAILABILITY_BACKFILL_LIMIT=0"
set "BESTBUY_DB_UPDATE_SIMILAR_ONLY=0"
set "BESTBUY_DB_UPDATE_AVAILABILITY_ONLY=0"
if not defined BESTBUY_DB_LOAD_DRY_RUN set "BESTBUY_DB_LOAD_DRY_RUN=0"
if not defined BESTBUY_FORCE_STEP_ENV set "BESTBUY_FORCE_STEP_ENV=1"
set "PYTHONUNBUFFERED=1"

set "LOG_DIR=%BESTBUY_RUN_ROOT%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\fullrun_%RUN_TS%.log"
set "BESTBUY_NOTIFY_LOG=%LOG_FILE%"

echo ==================================================
echo BestBuy %CATEGORY% full run started
echo batch_id=%BESTBUY_BATCH_ID%
echo run_folder=%BESTBUY_RUN_DATE%
echo run_root=%BESTBUY_RUN_ROOT%
echo log=%LOG_FILE%
echo ==================================================
echo BestBuy %CATEGORY% full run started > "%LOG_FILE%"
echo batch_id=%BESTBUY_BATCH_ID% >> "%LOG_FILE%"
echo run_folder=%BESTBUY_RUN_DATE% >> "%LOG_FILE%"
echo run_root=%BESTBUY_RUN_ROOT% >> "%LOG_FILE%"

call :run_step 01 14 "main_list" 01
if errorlevel 1 goto :fail
call :run_step 02 14 "main_targets" 02
if errorlevel 1 goto :fail
call :run_step 03 14 "bsr_list" 03
if errorlevel 1 goto :fail
call :run_step 04 14 "bsr_rank" 04
if errorlevel 1 goto :fail
call :run_step 05 14 "promotion_deals" 05
if errorlevel 1 goto :fail
call :run_step 06 14 "trending_deals" 06
if errorlevel 1 goto :fail
call :run_step 07 14 "final_targets" 07
if errorlevel 1 goto :fail
call :run_step 08 14 "detail_html" 08
if errorlevel 1 goto :fail
call :run_step 09 14 "review20" 09
if errorlevel 1 goto :fail
call :run_step 10 14 "availability_backfill" 10
if errorlevel 1 goto :fail
call :run_step 11 14 "status_check" 11
if errorlevel 1 goto :fail

call :run_step 12 14 "db_prepare" 14
if errorlevel 1 goto :fail
call :run_step 13 14 "db_load" 15
if errorlevel 1 goto :fail
call :run_step 14 14 "item_mst_load" 16
if errorlevel 1 goto :fail

echo ==================================================
echo BestBuy %CATEGORY% full run completed
echo log=%LOG_FILE%
echo ==================================================
echo BestBuy %CATEGORY% full run completed >> "%LOG_FILE%"
call :notify "success" "" ""
exit /b 0

:run_step
set "CUR=%~1"
set "TOTAL=%~2"
set "NAME=%~3"
set "STEP=%~4"
echo.
echo [%CUR%/%TOTAL%] %NAME% started
echo [%CUR%/%TOTAL%] %NAME% started >> "%LOG_FILE%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m bestbuy.bestbuy_orchestrator --category '%CATEGORY%' '%STEP%' 2>&1 | Tee-Object -FilePath '%LOG_FILE%' -Append"
if errorlevel 1 (
  set "FAILED_STEP_NAME=%NAME%"
  set "FAILED_STEP=%STEP%"
  echo [%CUR%/%TOTAL%] %NAME% failed
  echo [%CUR%/%TOTAL%] %NAME% failed >> "%LOG_FILE%"
  exit /b 1
)
echo [%CUR%/%TOTAL%] %NAME% completed
echo [%CUR%/%TOTAL%] %NAME% completed >> "%LOG_FILE%"
exit /b 0

:fail
echo.
echo BestBuy %CATEGORY% full run failed. See log: %LOG_FILE%
echo BestBuy %CATEGORY% full run failed >> "%LOG_FILE%"
call :notify "failed" "%FAILED_STEP_NAME%" "%FAILED_STEP%"
exit /b 1

:notify
set "BESTBUY_NOTIFY_STATUS=%~1"
set "BESTBUY_NOTIFY_FAILED_STEP_NAME=%~2"
set "BESTBUY_NOTIFY_FAILED_STEP=%~3"
set "BESTBUY_NOTIFY_LOG=%LOG_FILE%"
echo.
echo [notify] email_notify started status=%BESTBUY_NOTIFY_STATUS%
echo [notify] email_notify started status=%BESTBUY_NOTIFY_STATUS% >> "%LOG_FILE%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m bestbuy.step16_email_notify 2>&1 | Tee-Object -FilePath '%LOG_FILE%' -Append; exit 0"
echo [notify] email_notify completed
echo [notify] email_notify completed >> "%LOG_FILE%"
exit /b 0
