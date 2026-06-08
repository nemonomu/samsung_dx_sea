@echo off
setlocal
chcp 65001 > nul

set "PROJECT_ROOT=C:\samsung_dx_sea"
set "PYTHON=python"
set "BOOTSTRAP=%PROJECT_ROOT%\walmart\common\walmart_session_bootstrap.py"
set "SESSION_JSON=%PROJECT_ROOT%\log\walmart_browser_session.json"

echo [START] Walmart browser session bootstrap
echo [PROJECT_ROOT] %PROJECT_ROOT%
echo [SESSION_JSON] %SESSION_JSON%
echo.

"%PYTHON%" "%BOOTSTRAP%" --project-root "%PROJECT_ROOT%" --out "%SESSION_JSON%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
  echo [FAILED] Walmart session bootstrap failed. Exit code: %EXIT_CODE%
  pause
  exit /b %EXIT_CODE%
)

echo [DONE] Walmart browser session saved.
pause
exit /b 0
