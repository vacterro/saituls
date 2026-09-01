@echo off
setlocal
title SAITULS Install or Repair
cd /d "%~dp0"

echo SAITULS will install or repair everything it needs.
echo Windows may ask for administrator permission once.
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -NoLaunch
set "RESULT=%ERRORLEVEL%"

echo.
if not "%RESULT%"=="0" (
    echo Setup did not finish cleanly. The error is shown above.
    pause
    exit /b %RESULT%
)

echo Setup complete. Starting SAITULS...
start "" "%~dp0SAITULS.exe"
timeout /t 2 /nobreak >nul
endlocal
