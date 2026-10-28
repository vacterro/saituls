@echo off
setlocal
title SAITULS Payload Build

rem The PowerShell worker owns preflight, unique staging, ZIP validation and
rem publication. This wrapper preserves the documented one-click entry point.
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\Scripts\build_payload.ps1" -Root "%ROOT%"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
