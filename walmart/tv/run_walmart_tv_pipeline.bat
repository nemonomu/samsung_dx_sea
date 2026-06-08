@echo off
setlocal
chcp 65001 > nul

set "PROJECT_ROOT=C:\samsung_dx_sea"
set "PYTHON=python"
set "RUNNER=%PROJECT_ROOT%\walmart\common\walmart_full_pipeline_runner.py"
set "WALMART_SSL_NO_VERIFY=1"
if "%MAX_REVIEW_PAGES%"=="" set "MAX_REVIEW_PAGES=2"
if "%WALMART_WITH_BTF%"=="" set "WALMART_WITH_BTF=0"
if "%WALMART_SESSION_JSON%"=="" set "WALMART_SESSION_JSON="

set "BTF_ARGS="
if "%WALMART_WITH_BTF%"=="1" set "BTF_ARGS=--with-btf"

echo [START] Walmart full pipeline
echo [PROJECT_ROOT] %PROJECT_ROOT%
echo [SSL] WALMART_SSL_NO_VERIFY=%WALMART_SSL_NO_VERIFY%
echo [MAX_REVIEW_PAGES] %MAX_REVIEW_PAGES%
echo [WALMART_WITH_BTF] %WALMART_WITH_BTF%
echo [SESSION_JSON] %WALMART_SESSION_JSON%
echo.

if "%WALMART_SESSION_JSON%"=="" goto RUN_NO_SESSION
if exist "%WALMART_SESSION_JSON%" goto RUN_WITH_SESSION
echo [SESSION] session JSON not found; running without browser session.
goto RUN_NO_SESSION

:RUN_WITH_SESSION
echo [SESSION] using browser session.
"%PYTHON%" "%RUNNER%" --project-root "%PROJECT_ROOT%" --table public.test_tv_retail_com --max-review-pages %MAX_REVIEW_PAGES% %BTF_ARGS% --session-json "%WALMART_SESSION_JSON%" --commit-db
goto AFTER_RUN

:RUN_NO_SESSION
echo [SESSION] running without browser session.
"%PYTHON%" "%RUNNER%" --project-root "%PROJECT_ROOT%" --table public.test_tv_retail_com --max-review-pages %MAX_REVIEW_PAGES% %BTF_ARGS% --commit-db

:AFTER_RUN
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
