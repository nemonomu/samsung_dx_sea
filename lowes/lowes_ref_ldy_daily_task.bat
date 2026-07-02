@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "OVERALL_EXIT_CODE=0"
set "FAILURE_COUNT=0"
set "FAILURES="

call :run_lowes REF
call :record_failure LOWES_REF !CATEGORY_EXIT!

call :run_lowes LDY
call :record_failure LOWES_LDY !CATEGORY_EXIT!

if not "%FAILURE_COUNT%"=="0" (
  echo Lowes REF+LDY daily task completed with failures
  echo failed_count=%FAILURE_COUNT%
  echo failures=%FAILURES%
) else (
  echo Lowes REF+LDY daily task completed successfully
)

exit /b %OVERALL_EXIT_CODE%

:run_lowes
set "CATEGORY=%~1"
set "CATEGORY_EXIT=0"
echo.
echo ==================================================
echo Lowes %CATEGORY% daily task started
echo ==================================================
call "%~dp0_lowes_daily_task.bat" %CATEGORY%
set "CATEGORY_EXIT=!ERRORLEVEL!"
echo Lowes %CATEGORY% daily task exit_code=!CATEGORY_EXIT!
exit /b !CATEGORY_EXIT!

:record_failure
set "FAIL_GROUP=%~1"
set "FAIL_CODE=%~2"
if "%FAIL_CODE%"=="" set "FAIL_CODE=0"
if not "%FAIL_CODE%"=="0" (
  set /a FAILURE_COUNT+=1
  set "OVERALL_EXIT_CODE=%FAIL_CODE%"
  if "!FAILURES!"=="" (
    set "FAILURES=%FAIL_GROUP%(%FAIL_CODE%)"
  ) else (
    set "FAILURES=!FAILURES!, %FAIL_GROUP%(%FAIL_CODE%)"
  )
  echo [warn] %FAIL_GROUP% failed exit_code=%FAIL_CODE%; continuing.
)
exit /b 0
