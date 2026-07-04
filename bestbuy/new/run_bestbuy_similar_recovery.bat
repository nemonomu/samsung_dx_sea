@echo off
setlocal EnableExtensions

rem Recover retailer_sku_name_similar for an existing run folder:
rem   step08 detail retry (missing-similar rows only) -> DB update (similar column only).
rem Usage: run_bestbuy_similar_recovery.bat [CATEGORY] [RUN_FOLDER]
rem   CATEGORY   default TV
rem   RUN_FOLDER default = newest folder under bestbuy\data\<category> (e.g. 20260703)

cd /d "%~dp0"

set "CATEGORY=%~1"
if "%CATEGORY%"=="" set "CATEGORY=TV"
set "CATEGORY=%CATEGORY:"=%"

for /f %%i in ('powershell -NoProfile -Command "'%CATEGORY%'.ToLowerInvariant()"') do set "CATEGORY_DIR=%%i"

set "RUN_FOLDER=%~2"
if "%RUN_FOLDER%"=="" (
  for /f %%i in ('powershell -NoProfile -Command "(Get-ChildItem -LiteralPath '%~dp0bestbuy\data\%CATEGORY_DIR%' -Directory | Sort-Object Name | Select-Object -Last 1).Name"') do set "RUN_FOLDER=%%i"
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

rem Keep the ORIGINAL batch_id: step08 rewrites final_output.csv stamping BESTBUY_BATCH_ID
rem into every row, and the similar-only DB update matches on batch_id + item.
for /f %%i in ('powershell -NoProfile -Command "(Import-Csv -LiteralPath '%FINAL_OUTPUT%' | Select-Object -First 1).batch_id"') do set "BESTBUY_BATCH_ID=%%i"
if "%BESTBUY_BATCH_ID%"=="" (
  echo [error] could not read batch_id from %FINAL_OUTPUT%
  exit /b 1
)

rem Let our overrides win over orchestrator per-step env (step env fills the rest as defaults).
set "BESTBUY_FORCE_STEP_ENV=0"

rem step08 scope: only SKUs whose final_output row is missing retailer_sku_name_similar,
rem and only those without a successful detail fetch (RETRY_ONLY also bypasses the attempt cap).
set "BESTBUY_DETAIL_STAGE=detail"
set "BESTBUY_DETAIL_RETRY_ONLY=1"
set "BESTBUY_DETAIL_RETRY_MISSING_SIMILAR=1"
set "BESTBUY_DETAIL_RETRY_MISSING_SIMILAR_ALLOW_ALL=1"
set "BESTBUY_DETAIL_AUTO_RETRY=1"
set "BESTBUY_DETAIL_MAX_ATTEMPTS=3"
set "BESTBUY_DETAIL_RETRY_SLEEP_SECONDS=5"

rem DB: update only retailer_sku_name_similar (batch_id + item match), skip full replace.
set "BESTBUY_DB_UPDATE_SIMILAR_ONLY=1"
if not defined BESTBUY_DB_LOAD_DRY_RUN set "BESTBUY_DB_LOAD_DRY_RUN=0"

set "PYTHONUNBUFFERED=1"

set "LOG_DIR=%BESTBUY_RUN_ROOT%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\similar_recovery_%RUN_TS%.log"

echo ==================================================
echo BestBuy %CATEGORY% similar recovery started
echo run_folder=%RUN_FOLDER%
echo batch_id=%BESTBUY_BATCH_ID%
echo run_root=%BESTBUY_RUN_ROOT%
echo log=%LOG_FILE%
echo ==================================================
echo BestBuy %CATEGORY% similar recovery started > "%LOG_FILE%"
echo run_folder=%RUN_FOLDER% >> "%LOG_FILE%"
echo batch_id=%BESTBUY_BATCH_ID% >> "%LOG_FILE%"

echo.
echo [1/2] detail retry (missing similar) started
echo [1/2] detail retry started >> "%LOG_FILE%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m bestbuy.bestbuy_orchestrator --category '%CATEGORY%' 08 2>&1 | Tee-Object -FilePath '%LOG_FILE%' -Append"
if errorlevel 1 (
  echo [1/2] detail retry failed. See log: %LOG_FILE%
  echo [1/2] detail retry failed >> "%LOG_FILE%"
  exit /b 1
)
echo [1/2] detail retry completed
echo [1/2] detail retry completed >> "%LOG_FILE%"

for /f %%i in ('powershell -NoProfile -Command "@(Import-Csv -LiteralPath '%FINAL_OUTPUT%' | Where-Object { [string]::IsNullOrWhiteSpace($_.retailer_sku_name_similar) }).Count"') do set "STILL_MISSING=%%i"
echo [check] rows still missing retailer_sku_name_similar: %STILL_MISSING%
echo [check] rows still missing retailer_sku_name_similar: %STILL_MISSING% >> "%LOG_FILE%"

echo.
echo [2/2] db update (similar only) started
echo [2/2] db update started >> "%LOG_FILE%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m bestbuy.bestbuy_orchestrator --category '%CATEGORY%' 15 2>&1 | Tee-Object -FilePath '%LOG_FILE%' -Append"
if errorlevel 1 (
  echo [2/2] db update failed. See log: %LOG_FILE%
  echo [2/2] db update failed >> "%LOG_FILE%"
  exit /b 1
)
echo [2/2] db update completed
echo [2/2] db update completed >> "%LOG_FILE%"

echo ==================================================
echo BestBuy %CATEGORY% similar recovery completed
echo still_missing=%STILL_MISSING%
echo log=%LOG_FILE%
echo ==================================================
echo BestBuy %CATEGORY% similar recovery completed >> "%LOG_FILE%"
exit /b 0
