@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Lowes REF -> LDY daily orchestrator runner.
REM Runs both categories even if REF fails (for example during DB insert).
REM Final exit code is non-zero if any category failed, but no category blocks the next one.

cd /d "%~dp0.."
set "REPO_ROOT=%CD%"

set "PYTHONUNBUFFERED=1"
set "OVERALL_EXIT_CODE=0"
set "FAILED_CATEGORIES="

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "RUN_TS=%%i"
set "TASK_LOG_DIR=%~dp0task_logs"
if not exist "%TASK_LOG_DIR%" mkdir "%TASK_LOG_DIR%"
set "TASK_LOG=%TASK_LOG_DIR%\ref_ldy_daily_%RUN_TS%.log"

echo ==================================================
echo Lowes REF-LDY daily task started
echo order=REF LDY
echo continue_on_category_failure=true
echo cwd=%CD%
echo task_log=%TASK_LOG%
echo ==================================================

(
  echo ==================================================
  echo Lowes REF-LDY daily task started
  echo order=REF LDY
  echo continue_on_category_failure=true
  echo cwd=%CD%
  echo ts=%RUN_TS%
  echo ==================================================
) > "%TASK_LOG%"

for %%C in (REF LDY) do (
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
    echo Lowes %%C task failed exit_code=!EXIT_CODE!. Continuing with remaining categories.
    echo Lowes %%C task failed exit_code=!EXIT_CODE!. Continuing with remaining categories. >> "%TASK_LOG%"
    set "OVERALL_EXIT_CODE=!EXIT_CODE!"
    if "!FAILED_CATEGORIES!"=="" (
      set "FAILED_CATEGORIES=%%C"
    ) else (
      set "FAILED_CATEGORIES=!FAILED_CATEGORIES!,%%C"
    )
  ) else (
    echo Lowes %%C task completed successfully. >> "%TASK_LOG%"
  )
)

echo.
echo ==================================================
if "%FAILED_CATEGORIES%"=="" (
  echo Lowes REF-LDY daily task completed successfully
  echo Lowes REF-LDY daily task completed successfully >> "%TASK_LOG%"
) else (
  echo Lowes REF-LDY daily task completed with failures: %FAILED_CATEGORIES%
  echo Lowes REF-LDY daily task completed with failures: %FAILED_CATEGORIES% >> "%TASK_LOG%"
)
echo task_log=%TASK_LOG%
echo ==================================================

exit /b %OVERALL_EXIT_CODE%
