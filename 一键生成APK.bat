@echo off
REM Pocket Mortys enhanced APK builder -- Windows launcher.
REM This file must stay pure ASCII: cmd.exe parses .bat with the OEM
REM code page, so non-ASCII here garbles on other locales.
REM All Chinese output comes from Python (UTF-8 after chcp 65001).
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
echo.
echo   ==============================================================
echo    Pocket Mortys Enhanced - APK builder
echo   ==============================================================
echo.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (
    echo   [X] Python 3 not found.
    echo       Install it from https://www.python.org/downloads/
    echo       and tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)
%PY% "tools\make_apk.py" %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" (
    echo   [!] Something failed. Scroll up for the reason.
    echo.
    pause
)
endlocal
exit /b %RC%
