@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "CHAIN_NAME=REF_LDY_TV_HHP"
set "CHAIN_ORDER=REF LDY TV HHP"
set "CHAIN_LOG_DIR=%~dp0bestbuy\data\task_logs"
set "CHAIN_FAILURE_COUNT=0"
set "CHAIN_FAILURES="
if not exist "%CHAIN_LOG_DIR%" mkdir "%CHAIN_LOG_DIR%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "CHAIN_TS=%%i"
for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "CHAIN_START_EPOCH=%%i"
set "CHAIN_LOG=%CHAIN_LOG_DIR%\daily_task_ref_ldy_tv_hhp_%CHAIN_TS%.log"

echo ==================================================
echo BestBuy REF+LDY+TV+HHP daily task started
echo order=%CHAIN_ORDER%
echo cwd=%CD%
echo chain_log=%CHAIN_LOG%
echo ==================================================
echo ================================================== > "%CHAIN_LOG%"
echo BestBuy REF+LDY+TV+HHP daily task started >> "%CHAIN_LOG%"
echo order=%CHAIN_ORDER% >> "%CHAIN_LOG%"
echo cwd=%CD% >> "%CHAIN_LOG%"
echo chain_log=%CHAIN_LOG% >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"

call :run_category REF
call :record_category REF

call :run_category LDY
call :record_category LDY

call :run_category TV
call :record_category TV

call :run_category HHP
call :record_category HHP

for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "CHAIN_END_EPOCH=%%i"
set /a CHAIN_ELAPSED_SEC=CHAIN_END_EPOCH-CHAIN_START_EPOCH

if not "%CHAIN_FAILURE_COUNT%"=="0" goto :done_with_failures

echo ==================================================
echo BestBuy REF+LDY+TV+HHP daily task completed
echo elapsed_sec=%CHAIN_ELAPSED_SEC%
echo chain_log=%CHAIN_LOG%
echo ==================================================
echo ================================================== >> "%CHAIN_LOG%"
echo BestBuy REF+LDY+TV+HHP daily task completed >> "%CHAIN_LOG%"
echo elapsed_sec=%CHAIN_ELAPSED_SEC% >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"
exit /b 0

:run_category
set "CATEGORY=%~1"
set "CATEGORY_EXIT=0"
echo.
echo ==================================================
echo BestBuy %CATEGORY% daily task queued
echo ==================================================
echo. >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"
echo BestBuy %CATEGORY% daily task queued >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"

call :clear_operational_env

if "%BESTBUY_REF_LDY_TV_HHP_DRY_RUN%"=="1" (
  echo [dry-run] call "%~dp0_bby_daily_task.bat" %CATEGORY%
  echo [dry-run] call "%~dp0_bby_daily_task.bat" %CATEGORY% >> "%CHAIN_LOG%"
  exit /b 0
)

call "%~dp0_bby_daily_task.bat" %CATEGORY%
set "CATEGORY_EXIT=!ERRORLEVEL!"
echo BestBuy %CATEGORY% daily task exit_code=!CATEGORY_EXIT!
echo BestBuy %CATEGORY% daily task exit_code=!CATEGORY_EXIT! >> "%CHAIN_LOG%"
exit /b !CATEGORY_EXIT!

:record_category
set "REC_CATEGORY=%~1"
if not "!CATEGORY_EXIT!"=="0" (
  set /a CHAIN_FAILURE_COUNT+=1
  if "!CHAIN_FAILURES!"=="" (
    set "CHAIN_FAILURES=%REC_CATEGORY%(!CATEGORY_EXIT!)"
  ) else (
    set "CHAIN_FAILURES=!CHAIN_FAILURES!, %REC_CATEGORY%(!CATEGORY_EXIT!)"
  )
)
exit /b 0

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

:done_with_failures
echo.
echo ==================================================
echo BestBuy REF+LDY+TV+HHP daily task completed with failures
echo failed_count=%CHAIN_FAILURE_COUNT%
echo failures=%CHAIN_FAILURES%
echo elapsed_sec=%CHAIN_ELAPSED_SEC%
echo chain_log=%CHAIN_LOG%
echo ==================================================
echo. >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"
echo BestBuy REF+LDY+TV+HHP daily task completed with failures >> "%CHAIN_LOG%"
echo failed_count=%CHAIN_FAILURE_COUNT% >> "%CHAIN_LOG%"
echo failures=%CHAIN_FAILURES% >> "%CHAIN_LOG%"
echo elapsed_sec=%CHAIN_ELAPSED_SEC% >> "%CHAIN_LOG%"
echo ================================================== >> "%CHAIN_LOG%"
exit /b 1
