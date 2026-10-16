@echo off
setlocal EnableExtensions
pushd "%~dp0.."
if errorlevel 1 exit /b 1
set "PYTHONUNBUFFERED=1"
set "PYTHONUTF8=1"
if not defined LOWES_DRYER_MAIN_LIMIT set "LOWES_DRYER_MAIN_LIMIT=300"
if not defined LOWES_DRYER_BSR_LIMIT set "LOWES_DRYER_BSR_LIMIT=100"
if not defined LOWES_DRYER_DETAIL_CONCURRENCY set "LOWES_DRYER_DETAIL_CONCURRENCY=2"
echo Lowe's dryer: ZIP=10010. Table, limits and run path appear in the runtime log.
python -u -m lowes.lowes_dryer %*
set "DRYER_EXIT_CODE=%ERRORLEVEL%"
echo Dryer finished: exit=%DRYER_EXIT_CODE% [0=complete 2=partial failure 1=failed 130=interrupted]
popd
exit /b %DRYER_EXIT_CODE%
