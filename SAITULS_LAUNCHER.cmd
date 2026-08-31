@echo off
title SAITULS Launcher
color 0B

echo ==========================================
echo  SAITULS - Unified Toolkit
echo ==========================================

cd /d "%~dp0"

if not exist "%~dp0SAITULS.exe" (
    echo [ERROR] SAITULS.exe not found.
    pause
    exit /b 1
)

set "PATH=%~dp0Bin;%~dp0Bin\App;%PATH%"

powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -Verb RunAs -FilePath '%~dp0SAITULS.exe'"

exit /b 0
