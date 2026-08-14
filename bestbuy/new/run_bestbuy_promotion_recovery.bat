@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "RUN_ROOT=%~1"
set "BATCH_ID=%~2"

if "%RUN_ROOT%"=="" (
  echo Usage: run_bestbuy_promotion_recovery.bat RUN_ROOT [BATCH_ID]
  echo Example: run_bestbuy_promotion_recovery.bat "C:\samsung_dx_sea\bestbuy\new\bestbuy\data\tv\20260813"
  exit /b 2
)

set "RUN_ROOT=%RUN_ROOT:"=%"
set "BATCH_ID=%BATCH_ID:"=%"

if not exist "%RUN_ROOT%\output\final_output.csv" (
  echo [error] existing final_output.csv not found: %RUN_ROOT%\output\final_output.csv
  exit /b 2
)

set "BESTBUY_CATEGORY=TV"
set "BESTBUY_RUN_ROOT=%RUN_ROOT%"
if "%BATCH_ID%"=="" (
  set "BESTBUY_BATCH_ID="
) else (
  set "BESTBUY_BATCH_ID=%BATCH_ID%"
)
set "BESTBUY_DB_UPDATE_SIMILAR_ONLY=0"
set "BESTBUY_DB_UPDATE_AVAILABILITY_ONLY=0"
set "BESTBUY_DB_UPDATE_PROMOTION_ONLY=0"
set "PYTHONUNBUFFERED=1"

if "%BATCH_ID%"=="" (
  python -m bestbuy.sos_refill --category TV --run-root "%RUN_ROOT%" --promotion-only
) else (
  python -m bestbuy.sos_refill --category TV --run-root "%RUN_ROOT%" --batch-id "%BATCH_ID%" --promotion-only
)
exit /b %ERRORLEVEL%
