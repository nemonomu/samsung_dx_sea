@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0_bby_daily_task.bat" LDY
exit /b %ERRORLEVEL%
