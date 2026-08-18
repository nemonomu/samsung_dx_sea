@echo off
setlocal EnableExtensions

REM Per-category Lowes daily task. Invokes the orchestrator with --all.
REM Args: %1 = CATEGORY (REF or LDY)
REM       %2 = parent task log path (optional, will append run results)

cd /d "%~dp0.."
set "REPO_ROOT=%CD%"

set "CATEGORY=%~1"
if "%CATEGORY%"=="" set "CATEGORY=REF"
set "CATEGORY=%CATEGORY:"=%"

set "PARENT_LOG=%~2"
set "PARENT_LOG=%PARENT_LOG:"=%"

set "PYTHONUNBUFFERED=1"

rem Local disk retention: prune run folders older than 3 days; delete by age
rem regardless of S3 (s3_sync is not relied on here) to avoid filling the disk.
if not defined LOCAL_RETENTION_DAYS set "LOCAL_RETENTION_DAYS=3"
set "LOCAL_CLEANUP_REQUIRE_S3_SUCCESS=0"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "RUN_TS=%%i"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "RUN_DATE=%%i"
for /f %%i in ('powershell -NoProfile -Command "'%CATEGORY%'.ToLowerInvariant()"') do set "CATEGORY_DIR=%%i"
for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "TASK_START_EPOCH=%%i"

set "TASK_LOG_DIR=%~dp0data\%CATEGORY_DIR%\%RUN_DATE%\logs"
if not exist "%TASK_LOG_DIR%" mkdir "%TASK_LOG_DIR%"
set "TASK_LOG=%TASK_LOG_DIR%\fullrun_%RUN_TS%.log"

echo ================================================== >> "%TASK_LOG%"
echo Lowes %CATEGORY% full run started >> "%TASK_LOG%"
echo run_date=%RUN_DATE% >> "%TASK_LOG%"
echo cwd=%CD% >> "%TASK_LOG%"
echo ================================================== >> "%TASK_LOG%"

echo ==================================================
echo Lowes %CATEGORY% full run started
echo run_date=%RUN_DATE%
echo task_log=%TASK_LOG%
echo ==================================================

rem Release stale idle-in-transaction locks left by a force-killed run (non-fatal).
echo [db_unlock] releasing stale DB locks
echo [db_unlock] releasing stale DB locks >> "%TASK_LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m lowes.db_unlock 2>&1 | Tee-Object -FilePath '%TASK_LOG%' -Append"

rem Prune old run folders up front so this run has disk headroom (non-fatal).
echo [cleanup] pruning %CATEGORY% run folders older than %LOCAL_RETENTION_DAYS% days
echo [cleanup] pruning %CATEGORY% run folders older than %LOCAL_RETENTION_DAYS% days >> "%TASK_LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m lowes.lowes_orchestrator --product-type '%CATEGORY%' 12 2>&1 | Tee-Object -FilePath '%TASK_LOG%' -Append"

powershell -NoProfile -ExecutionPolicy Bypass -Command "& python -m lowes.lowes_orchestrator --product-type '%CATEGORY%' --all 2>&1 | Tee-Object -FilePath '%TASK_LOG%' -Append; exit $LASTEXITCODE"
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="-1073741510" set "EXIT_CODE=130"
if "%EXIT_CODE%"=="3221225786" set "EXIT_CODE=130"

for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "TASK_END_EPOCH=%%i"
set /a TASK_ELAPSED_SEC=TASK_END_EPOCH-TASK_START_EPOCH

echo ================================================== >> "%TASK_LOG%"
echo Lowes %CATEGORY% full run finished exit_code=%EXIT_CODE% elapsed_sec=%TASK_ELAPSED_SEC% >> "%TASK_LOG%"
echo ================================================== >> "%TASK_LOG%"

echo ==================================================
echo Lowes %CATEGORY% full run finished exit_code=%EXIT_CODE%
echo elapsed_sec=%TASK_ELAPSED_SEC%
echo task_log=%TASK_LOG%
echo ==================================================

if not "%PARENT_LOG%"=="" (
  echo Lowes %CATEGORY% finished exit_code=%EXIT_CODE% elapsed_sec=%TASK_ELAPSED_SEC% task_log=%TASK_LOG% >> "%PARENT_LOG%"
)

exit /b %EXIT_CODE%
