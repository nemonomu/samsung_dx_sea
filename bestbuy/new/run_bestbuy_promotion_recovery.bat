@echo off
setlocal EnableExtensions
set "ORIGINAL_CODEPAGE="
for /f "tokens=2 delims=:" %%i in ('chcp') do set "ORIGINAL_CODEPAGE=%%i"
chcp 65001 >nul

cd /d "%~dp0"

set "RUN_INPUT=%~1"
set "RUN_ROOT="
set "BATCH_ID=%~2"
set "INTERACTIVE=0"

if not "%RUN_INPUT%"=="" goto :resolve_input

set "INTERACTIVE=1"
echo.
echo ==================================================
echo BestBuy TV promotion 전용 복구
echo ==================================================
set /p "RUN_INPUT=복구할 수집 폴더명 입력 (예: 20260815, Enter=가장 최근): "

if not "%RUN_INPUT%"=="" goto :resolve_input

for /f "delims=" %%i in ('dir /b /ad /o-n "%~dp0bestbuy\data\tv\20??????*" 2^>nul') do (
  if not defined RUN_ROOT if exist "%~dp0bestbuy\data\tv\%%i\output\final_output.csv" set "RUN_ROOT=%~dp0bestbuy\data\tv\%%i"
)
goto :validate_run

:resolve_input
set "RUN_INPUT=%RUN_INPUT:"=%"
if exist "%RUN_INPUT%\output\final_output.csv" (
  set "RUN_ROOT=%RUN_INPUT%"
) else (
  set "RUN_ROOT=%~dp0bestbuy\data\tv\%RUN_INPUT%"
)

:validate_run
if defined RUN_ROOT set "RUN_ROOT=%RUN_ROOT:"=%"
if defined BATCH_ID set "BATCH_ID=%BATCH_ID:"=%"

if not defined RUN_ROOT (
  echo.
  echo [오류] output\final_output.csv가 있는 최신 수집 폴더를 찾지 못했습니다.
  if "%INTERACTIVE%"=="1" pause
  set "EXIT_CODE=2"
  goto :finish
)

if not exist "%RUN_ROOT%\output\final_output.csv" (
  echo.
  echo [오류] final_output.csv를 찾을 수 없습니다.
  echo 확인 경로: "%RUN_ROOT%\output\final_output.csv"
  if "%INTERACTIVE%"=="1" pause
  set "EXIT_CODE=2"
  goto :finish
)

echo.
echo 선택된 수집 폴더: "%RUN_ROOT%"
echo batch_id: final_output.csv에서 자동 확인
echo 갱신 컬럼: promotion_type, promotion_position
echo 신규 상품 추가: 안 함

if "%INTERACTIVE%"=="1" (
  choice /C YN /N /M "이 폴더의 promotion만 재수집할까요? [Y/N] "
  if errorlevel 2 (
    echo 취소했습니다.
    pause
    set "EXIT_CODE=0"
    goto :finish
  )
)

set "BESTBUY_CATEGORY=TV"
set "BESTBUY_RUN_ROOT=%RUN_ROOT%"
if "%BATCH_ID%"=="" (
  set "BESTBUY_BATCH_ID="
) else (
  set "BESTBUY_BATCH_ID=%BATCH_ID%"
)
set "BESTBUY_DB_UPDATE_SIMILAR_ONLY=0"
set "BESTBUY_DB_UPDATE_AVAILABILITY_ONLY=0"
set "BESTBUY_DB_UPDATE_PROMOTION_ONLY=0"
set "PYTHONUNBUFFERED=1"

if "%BATCH_ID%"=="" (
  python -m bestbuy.sos_refill --category TV --run-root "%RUN_ROOT%" --promotion-only
) else (
  python -m bestbuy.sos_refill --category TV --run-root "%RUN_ROOT%" --batch-id "%BATCH_ID%" --promotion-only
)
set "EXIT_CODE=%ERRORLEVEL%"

if "%INTERACTIVE%"=="1" (
  echo.
  if "%EXIT_CODE%"=="0" (
    echo promotion 전용 복구가 완료되었습니다.
  ) else (
    echo promotion 전용 복구가 실패했습니다. 위 오류와 recovery manifest를 확인하세요.
  )
  pause
)

:finish
if defined ORIGINAL_CODEPAGE chcp %ORIGINAL_CODEPAGE% >nul
exit /b %EXIT_CODE%
