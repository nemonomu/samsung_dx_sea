@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Lowes all-category daily orchestrator runner.
REM Order: LDY -> REF (stops on first failure).
REM Invokes lowes/lowes_orchestrator with --all per category.

cd /d "%~dp0.."
set "REPO_ROOT=%CD%"

set "PYTHONUNBUFFERED=1"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "RUN_TS=%%i"
set "TASK_LOG_DIR=%~dp0task_logs"
if not exist "%TASK_LOG_DIR%" mkdir "%TASK_LOG_DIR%"
set "TASK_LOG=%TASK_LOG_DIR%\all_daily_%RUN_TS%.log"

echo ==================================================
echo Lowes all-category daily task started
echo order=LDY REF
echo cwd=%CD%
echo task_log=%TASK_LOG%
echo ==================================================

(
  echo ==================================================
  echo Lowes all-category daily task started
  echo order=LDY REF
  echo cwd=%CD%
  echo ts=%RUN_TS%
  echo ==================================================
) > "%TASK_LOG%"

for %%C in (LDY REF) do (
  echo.
  echo ================================================== >> "%TASK_LOG%"
  echo Lowes %%C task queued at %RUN_TS% >> "%TASK_LOG%"
  echo ================================================== >> "%TASK_LOG%"
  echo.
  echo ==================================================
  echo Lowes %%C task started
  echo ==================================================
  call "%~dp0_lowes_daily_task.bat" %%C "%TASK_LOG%"
  set "EXIT_CODE=!ERRORLEVEL!"
  if not "!EXIT_CODE!"=="0" (
    echo.
    echo Lowes %%C task failed exit_code=!EXIT_CODE!. Stopping all-category run.
    echo Lowes %%C task failed exit_code=!EXIT_CODE!. Stopping all-category run. >> "%TASK_LOG%"
    exit /b !EXIT_CODE!
  )
)

echo.
echo ==================================================
echo Lowes all-category daily task completed
echo task_log=%TASK_LOG%
echo ==================================================
echo Lowes all-category daily task completed >> "%TASK_LOG%"
exit /b 0
