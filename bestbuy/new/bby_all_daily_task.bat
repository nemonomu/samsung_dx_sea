@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

echo ==================================================
echo BestBuy all-category daily task started
echo order=TV HHP LDY REF
echo cwd=%CD%
echo ==================================================

for %%C in (TV HHP LDY REF) do (
  echo.
  echo ==================================================
  echo BestBuy %%C daily task queued
  echo ==================================================
  call "%~dp0_bby_daily_task.bat" %%C
  set "EXIT_CODE=!ERRORLEVEL!"
  if not "!EXIT_CODE!"=="0" (
    echo.
    echo BestBuy %%C daily task failed exit_code=!EXIT_CODE!. Stopping all-category run.
    exit /b !EXIT_CODE!
  )
)

echo.
echo ==================================================
echo BestBuy all-category daily task completed
echo ==================================================
exit /b 0
