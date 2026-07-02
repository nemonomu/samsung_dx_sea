@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "BESTBUY_PRESERVE_RUN_ENV="
call "%~dp0_bby_daily_task.bat" TV
exit /b %ERRORLEVEL%
