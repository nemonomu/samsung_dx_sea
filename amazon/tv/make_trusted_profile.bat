@echo off
REM Trusted profile copy for Amazon TV crawler.
REM The review login gate passes for an aged, benign (normal-use) Chrome profile.
REM This copies the minimal files from the current user's Chrome profile so the
REM crawler browser can run with that session state. Re-run anytime to refresh.
REM Usage: make_trusted_profile.bat [dest_dir]   (default: C:\chrome_profile_amzn)

setlocal
set "SRC=%LOCALAPPDATA%\Google\Chrome\User Data"
set "DST=%~1"
if "%DST%"=="" set "DST=C:\chrome_profile_amzn"

if not exist "%SRC%\Local State" (
    echo [ERROR] Chrome profile not found: %SRC%
    exit /b 1
)

if not exist "%DST%\Default\Network" mkdir "%DST%\Default\Network"

copy /y "%SRC%\Local State" "%DST%\Local State" >nul && echo [OK] Local State
copy /y "%SRC%\Default\Preferences" "%DST%\Default\Preferences" >nul && echo [OK] Preferences

copy /y "%SRC%\Default\Network\Cookies" "%DST%\Default\Network\Cookies" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] Cookies
) else (
    echo [INFO] Cookies locked by running Chrome - trying VSS copy...
    esentutl /y "%SRC%\Default\Network\Cookies" /vss /d "%DST%\Default\Network\Cookies" >nul
    if %errorlevel%==0 (
        echo [OK] Cookies ^(VSS^)
    ) else (
        echo [ERROR] Cookies copy failed. Close Chrome and re-run.
        exit /b 1
    )
)

echo.
echo Done: %DST%
echo Test with:
echo   python amazon\tv\amazon_tv_review_dom_test.py --user-data-dir "%DST%"
endlocal
