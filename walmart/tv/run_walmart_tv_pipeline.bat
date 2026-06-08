@echo off
setlocal
chcp 65001 > nul

set "PROJECT_ROOT=C:\samsung_dx_sea"
set "PYTHON=python"
set "RUNNER=%PROJECT_ROOT%\walmart\common\walmart_full_pipeline_runner.py"

echo [START] Walmart full pipeline
echo [PROJECT_ROOT] %PROJECT_ROOT%
echo.

"%PYTHON%" "%RUNNER%" --project-root "%PROJECT_ROOT%" --table public.test_tv_retail_com --commit-db
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
