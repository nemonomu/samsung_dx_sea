@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "BESTBUY_CATEGORY=DRYER"
set "BESTBUY_URL_SOURCE=default"
set "BESTBUY_LISTING_COLLECTION_MODE=browser_graphql"
set "BESTBUY_DETAIL_FETCH_MODE=browser_graphql"
set "BESTBUY_DETAIL_PDP_FALLBACK=0"
set "BESTBUY_BROWSER_GRAPHQL_HEADLESS=0"
set "BESTBUY_DETAIL_BROWSER_GRAPHQL_HEADLESS=0"
set "PYTHONUNBUFFERED=1"
set "DRYER_PYTHON=python"
if defined BESTBUY_PYTHON set "DRYER_PYTHON=%BESTBUY_PYTHON%"
if "%~1"=="" (
    "%DRYER_PYTHON%" -B -m bestbuy.step17_dryer --main-limit 20 --bsr-limit 10
) else (
    "%DRYER_PYTHON%" -B -m bestbuy.step17_dryer %*
)
set "DRYER_EXIT=%ERRORLEVEL%"
endlocal & exit /b %DRYER_EXIT%
