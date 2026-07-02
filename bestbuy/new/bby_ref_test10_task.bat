@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "BESTBUY_FORCE_STEP_ENV=0"
set "BESTBUY_FINAL_TARGET_SIZE=7"
set "BESTBUY_MAIN_RANK_LIMIT=7"
set "BESTBUY_BSR_RANK_LIMIT=20"
set "BESTBUY_FINAL_ROW_LIMIT=10"
set "BESTBUY_DB_LOAD_DRY_RUN=1"
set "BESTBUY_PRESERVE_RUN_ENV=1"

call "%~dp0_bby_daily_task.bat" REF
exit /b %ERRORLEVEL%
