@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "CATEGORY=%~1"
if "%CATEGORY%"=="" set "CATEGORY=TV"
set "CATEGORY=%CATEGORY:"=%"

if not "%BESTBUY_PRESERVE_RUN_ENV%"=="1" (
  set "BESTBUY_FORCE_STEP_ENV=1"
  set "BESTBUY_FINAL_TARGET_SIZE="
  set "BESTBUY_MAIN_RANK_LIMIT="
  set "BESTBUY_BSR_RANK_LIMIT="
  set "BESTBUY_FINAL_ROW_LIMIT="
  set "BESTBUY_DETAIL_LIMIT="
  set "BESTBUY_DETAIL_SKUS="
  set "BESTBUY_AVAILABILITY_BACKFILL_LIMIT="
  set "BESTBUY_DB_PREPARE_ADD_MISSING_COLUMNS=1"
  set "BESTBUY_DB_LOAD_DRY_RUN=0"
  set "BESTBUY_DB_UPDATE_SIMILAR_ONLY=0"
  set "BESTBUY_DB_UPDATE_AVAILABILITY_ONLY=0"
  set "BESTBUY_FORCE_RUN_PATH_ENV="
  set "BESTBUY_RUN_ROOT="
  set "BESTBUY_OUTPUT_ROOT="
  set "BESTBUY_DETAIL_RUN_ROOT="
  set "BESTBUY_DETAIL_TARGET_CSV="
  set "BESTBUY_FINAL_OUTPUT_CSV="
  set "BESTBUY_PRODUCT_LIST_OUTPUT="
  set "BESTBUY_ITEM_MST_OUTPUT_CSV="
)

set "LOCK_FILE=%~dp0bestbuy_daily_%CATEGORY%.lock"
set "TASK_LOG_DIR=%~dp0bestbuy\data\%CATEGORY%\task_logs"
if not exist "%TASK_LOG_DIR%" mkdir "%TASK_LOG_DIR%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TASK_TS=%%i"
set "TASK_LOG=%TASK_LOG_DIR%\daily_task_%TASK_TS%.log"
for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "TASK_START_EPOCH=%%i"

python -m bestbuy.step00_daily_lock acquire --lock "%LOCK_FILE%" --category "%CATEGORY%" --log "%TASK_LOG%" --root "%~dp0"
set "LOCK_STATUS=%ERRORLEVEL%"
if "%LOCK_STATUS%"=="2" (
  call :notify_preflight "daily_lock failed" "%LOCK_STATUS%"
  exit /b 2
)
if not "%LOCK_STATUS%"=="0" (
  call :notify_preflight "daily_lock failed" "%LOCK_STATUS%"
  exit /b %LOCK_STATUS%
)

echo ================================================== >> "%TASK_LOG%"
echo BestBuy %CATEGORY% daily task started >> "%TASK_LOG%"
echo cwd=%CD% >> "%TASK_LOG%"
echo task_log=%TASK_LOG% >> "%TASK_LOG%"
echo ================================================== >> "%TASK_LOG%"

echo ==================================================
echo BestBuy %CATEGORY% daily task started
echo cwd=%CD%
echo task_log=%TASK_LOG%
echo ==================================================

powershell -NoProfile -ExecutionPolicy Bypass -Command "& '%~dp0run_bestbuy_fullrun.bat' '%CATEGORY%' 2>&1 | Tee-Object -FilePath '%TASK_LOG%' -Append; exit $LASTEXITCODE"
set "EXIT_CODE=%ERRORLEVEL%"

python -m bestbuy.step00_daily_lock release --lock "%LOCK_FILE%" --category "%CATEGORY%" --log "%TASK_LOG%" --root "%~dp0" >nul 2>nul
if exist "%LOCK_FILE%" del "%LOCK_FILE%"

for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "TASK_END_EPOCH=%%i"
set /a TASK_ELAPSED_SEC=TASK_END_EPOCH-TASK_START_EPOCH

echo ================================================== >> "%TASK_LOG%"
echo BestBuy %CATEGORY% daily task finished exit_code=%EXIT_CODE% >> "%TASK_LOG%"
echo elapsed_sec=%TASK_ELAPSED_SEC% >> "%TASK_LOG%"
echo ================================================== >> "%TASK_LOG%"

echo ==================================================
echo BestBuy %CATEGORY% daily task finished exit_code=%EXIT_CODE%
echo elapsed_sec=%TASK_ELAPSED_SEC%
echo task_log=%TASK_LOG%
echo ==================================================

exit /b %EXIT_CODE%

:notify_preflight
set "BESTBUY_CATEGORY=%CATEGORY%"
set "BESTBUY_NOTIFY_STATUS=failed"
set "BESTBUY_NOTIFY_PREFLIGHT_ISSUE=%~1 exit_code=%~2"
set "BESTBUY_NOTIFY_LOG=%TASK_LOG%"
set "BESTBUY_RUN_ROOT=%~dp0bestbuy\data\%CATEGORY%\preflight_%TASK_TS%"
set "BESTBUY_OUTPUT_ROOT=%BESTBUY_RUN_ROOT%\output"
set "BESTBUY_FINAL_OUTPUT_CSV=%BESTBUY_OUTPUT_ROOT%\final_output.csv"
echo [notify] preflight email_notify started status=%BESTBUY_NOTIFY_STATUS% issue=%BESTBUY_NOTIFY_PREFLIGHT_ISSUE%
echo [notify] preflight email_notify started status=%BESTBUY_NOTIFY_STATUS% issue=%BESTBUY_NOTIFY_PREFLIGHT_ISSUE% >> "%TASK_LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m bestbuy.step16_email_notify 2>&1 | Tee-Object -FilePath '%TASK_LOG%' -Append; exit 0"
echo [notify] preflight email_notify completed
echo [notify] preflight email_notify completed >> "%TASK_LOG%"
exit /b 0
