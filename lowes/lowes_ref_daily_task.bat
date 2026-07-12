@echo off
setlocal EnableExtensions

REM Lowes REF-only daily task. Use this for immediate REF recovery on RDP.

cd /d "%~dp0"
call "%~dp0_lowes_daily_task.bat" REF
exit /b %ERRORLEVEL%
