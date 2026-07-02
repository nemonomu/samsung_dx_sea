@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "CHAIN_NAME=TV_HHP"
set "CHAIN_ORDER=TV HHP"
set "CHAIN_LOG_DIR=%~dp0bestbuy\data\task_logs"
if not exist "%CHAIN_LOG_DIR%" mkdir "%CHAIN_LOG_DIR%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "CHAIN_TS=%%i"
for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "CHAIN_START_EPOCH=%%i"
set "CHAIN_LOG=%CHAIN_LOG_DIR%\daily_task_tv_hhp_%CHAIN_TS%.log"

echo ==================================================
echo BestBuy TV+HHP daily task started
echo order=%CHAIN_ORDER%
echo cwd=%CD%
echo chain_log=%CHAIN_LOG%
echo ==================================================
echo ================================================== > "%CHAIN_LOG%"
echo BestBuy TV+HHP daily task started >> "%CHAIN_LOG%"
echo order=%CHAIN_ORDER% >> "%CHAIN_LOG%"
echo cwd=%CD% >> "%CHAIN_LOG%"
echo chain_log=%CHAIN_LOG% >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"

call :run_category TV
if errorlevel 1 goto :fail

call :run_category HHP
if errorlevel 1 goto :fail

for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "CHAIN_END_EPOCH=%%i"
set /a CHAIN_ELAPSED_SEC=CHAIN_END_EPOCH-CHAIN_START_EPOCH

echo ==================================================
echo BestBuy TV+HHP daily task completed
echo elapsed_sec=%CHAIN_ELAPSED_SEC%
echo chain_log=%CHAIN_LOG%
echo ==================================================
echo ================================================== >> "%CHAIN_LOG%"
echo BestBuy TV+HHP daily task completed >> "%CHAIN_LOG%"
echo elapsed_sec=%CHAIN_ELAPSED_SEC% >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"
exit /b 0

:run_category
set "CATEGORY=%~1"
echo.
echo ==================================================
echo BestBuy %CATEGORY% daily task queued
echo ==================================================
echo. >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"
echo BestBuy %CATEGORY% daily task queued >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"

call :clear_operational_env

if "%BESTBUY_TV_HHP_DRY_RUN%"=="1" (
  echo [dry-run] call "%~dp0_bby_daily_task.bat" %CATEGORY%
  echo [dry-run] call "%~dp0_bby_daily_task.bat" %CATEGORY% >> "%CHAIN_LOG%"
  exit /b 0
)

call "%~dp0_bby_daily_task.bat" %CATEGORY%
set "CATEGORY_EXIT=!ERRORLEVEL!"
echo BestBuy %CATEGORY% daily task exit_code=!CATEGORY_EXIT!
echo BestBuy %CATEGORY% daily task exit_code=!CATEGORY_EXIT! >> "%CHAIN_LOG%"
exit /b !CATEGORY_EXIT!

:clear_operational_env
set "BESTBUY_PRESERVE_RUN_ENV="
set "BESTBUY_FORCE_STEP_ENV="
set "BESTBUY_FORCE_RUN_PATH_ENV="
set "BESTBUY_FINAL_TARGET_SIZE="
set "BESTBUY_MAIN_RANK_LIMIT="
set "BESTBUY_BSR_RANK_LIMIT="
set "BESTBUY_FINAL_ROW_LIMIT="
set "BESTBUY_DETAIL_LIMIT="
set "BESTBUY_DETAIL_SKUS="
set "BESTBUY_AVAILABILITY_BACKFILL_LIMIT="
set "BESTBUY_DB_LOAD_DRY_RUN="
set "BESTBUY_DB_UPDATE_SIMILAR_ONLY="
set "BESTBUY_DB_UPDATE_AVAILABILITY_ONLY="
set "BESTBUY_RUN_ROOT="
set "BESTBUY_OUTPUT_ROOT="
set "BESTBUY_DETAIL_RUN_ROOT="
set "BESTBUY_DETAIL_TARGET_CSV="
set "BESTBUY_FINAL_OUTPUT_CSV="
set "BESTBUY_PRODUCT_LIST_OUTPUT="
set "BESTBUY_ITEM_MST_OUTPUT_CSV="
exit /b 0

:fail
for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "CHAIN_END_EPOCH=%%i"
set /a CHAIN_ELAPSED_SEC=CHAIN_END_EPOCH-CHAIN_START_EPOCH

echo.
echo ==================================================
echo BestBuy TV+HHP daily task failed
echo failed_category=%CATEGORY%
echo exit_code=%CATEGORY_EXIT%
echo elapsed_sec=%CHAIN_ELAPSED_SEC%
echo chain_log=%CHAIN_LOG%
echo ==================================================
echo. >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"
echo BestBuy TV+HHP daily task failed >> "%CHAIN_LOG%"
echo failed_category=%CATEGORY% >> "%CHAIN_LOG%"
echo exit_code=%CATEGORY_EXIT% >> "%CHAIN_LOG%"
echo elapsed_sec=%CHAIN_ELAPSED_SEC% >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"
exit /b %CATEGORY_EXIT%
