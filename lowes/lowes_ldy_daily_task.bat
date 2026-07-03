@echo off
setlocal EnableExtensions

REM Lowes LDY-only daily task. Use this for immediate LDY recovery on RDP.

cd /d "%~dp0"
call "%~dp0_lowes_daily_task.bat" LDY
exit /b %ERRORLEVEL%
