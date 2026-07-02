@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "LOWES_PRODUCT_ARG=%~1"
if "%LOWES_PRODUCT_ARG%"=="" set "LOWES_PRODUCT_ARG=REF"
set "LOWES_PRODUCT_ARG=%LOWES_PRODUCT_ARG:"=%"

if "%LOWES_DAILY_DRY_RUN%"=="1" (
  python -m lowes.lowes_orchestrator --product-type "%LOWES_PRODUCT_ARG%" --dry-run --all
) else (
  python -m lowes.lowes_orchestrator --product-type "%LOWES_PRODUCT_ARG%" --all
)

exit /b %ERRORLEVEL%
