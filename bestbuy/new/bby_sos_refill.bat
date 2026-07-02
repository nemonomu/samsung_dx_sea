@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "CATEGORY=%~1"
set "RUN_ROOT=%~2"
set "BATCH_ID=%~3"

if "%CATEGORY%"=="" (
  echo Usage: bby_sos_refill.bat CATEGORY RUN_ROOT [BATCH_ID]
  echo Example: bby_sos_refill.bat TV C:\samsung_dx_retail_com\dx_bby_260521\GraphQL\bestbuy\data\tv\20260604 b_20260604_014300
  exit /b 2
)

if "%RUN_ROOT%"=="" (
  echo Usage: bby_sos_refill.bat CATEGORY RUN_ROOT [BATCH_ID]
  echo Example: bby_sos_refill.bat TV C:\samsung_dx_retail_com\dx_bby_260521\GraphQL\bestbuy\data\tv\20260604 b_20260604_014300
  exit /b 2
)

set "CATEGORY=%CATEGORY:"=%"
set "RUN_ROOT=%RUN_ROOT:"=%"
set "BATCH_ID=%BATCH_ID:"=%"

if not exist "%RUN_ROOT%" (
  echo [error] run root does not exist: %RUN_ROOT%
  exit /b 2
)

set "BESTBUY_CATEGORY=%CATEGORY%"
set "BESTBUY_RUN_ROOT=%RUN_ROOT%"
if not "%BATCH_ID%"=="" set "BESTBUY_BATCH_ID=%BATCH_ID%"

set "PYTHONUNBUFFERED=1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m bestbuy.sos_refill --category '%CATEGORY%' --run-root '%RUN_ROOT%' --batch-id '%BATCH_ID%'; exit $LASTEXITCODE"
exit /b %ERRORLEVEL%
