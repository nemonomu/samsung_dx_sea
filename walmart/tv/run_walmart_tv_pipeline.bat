@echo off
setlocal
chcp 65001 > nul

set "PROJECT_ROOT=C:\samsung_dx_sea"
set "PYTHON=python"
set "RUNNER=%PROJECT_ROOT%\walmart\common\walmart_full_pipeline_runner.py"
set "WALMART_SSL_NO_VERIFY=1"
if "%MAX_REVIEW_PAGES%"=="" set "MAX_REVIEW_PAGES=1"
if "%WALMART_SESSION_JSON%"=="" set "WALMART_SESSION_JSON=%PROJECT_ROOT%\log\walmart_browser_session.json"

echo [START] Walmart full pipeline
echo [PROJECT_ROOT] %PROJECT_ROOT%
echo [SSL] WALMART_SSL_NO_VERIFY=%WALMART_SSL_NO_VERIFY%
echo [MAX_REVIEW_PAGES] %MAX_REVIEW_PAGES%
echo [SESSION_JSON] %WALMART_SESSION_JSON%
echo.

if exist "%WALMART_SESSION_JSON%" (
  "%PYTHON%" "%RUNNER%" --project-root "%PROJECT_ROOT%" --table public.test_tv_retail_com --max-review-pages %MAX_REVIEW_PAGES% --session-json "%WALMART_SESSION_JSON%" --commit-db
) else (
  echo [SESSION] session JSON not found; running without browser session.
  "%PYTHON%" "%RUNNER%" --project-root "%PROJECT_ROOT%" --table public.test_tv_retail_com --max-review-pages %MAX_REVIEW_PAGES% --commit-db
)
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
  echo [FAILED] Walmart full pipeline failed. Exit code: %EXIT_CODE%
  pause
  exit /b %EXIT_CODE%
)

echo [DONE] Walmart full pipeline completed and DB insert committed.
pause
exit /b 0
