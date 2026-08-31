@echo off
setlocal
title SAITULS Payload Build

rem ============================================================
rem  Builds SAITULS-payload-<ver>.zip from the local Bin\ tree.
rem  The zip goes to ..\dist\ ready for a GitHub Release upload.
rem
rem  Heavy binaries are intentionally NOT in the git repo; this
rem  script is how a release owner regenerates the payload zip.
rem ============================================================

set "VER=0.0.1"
set "ROOT=%~dp0"
rem Strip the trailing backslash: "%ROOT%" would end in \" which escapes
rem the closing quote and mangles the argument for tar / 7z.
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "DIST=%ROOT%\dist"
set "ZIP=%DIST%\SAITULS-payload-%VER%.zip"

if not exist "%ROOT%\Bin\FFMPEG.EXE" (
    echo [ERROR] Bin\FFMPEG.EXE not found. Payload sources missing.
    echo Run this from the full local tree, not a source-only checkout.
    exit /b 1
)

if not exist "%DIST%" mkdir "%DIST%"

if exist "%ZIP%" del "%ZIP%"

rem 7-Zip if present, else Windows tar (Win10+). Both write the
rem Bin\ root into the zip so extracting at the toolkit root works.
if exist "C:\Program Files\7-Zip\7z.exe" (
    "C:\Program Files\7-Zip\7z.exe" a -tzip "%ZIP%" "%ROOT%\Bin" -xr!__pycache__ >nul
) else (
    tar -a -c -f "%ZIP%" -C "%ROOT%" Bin
)

if not exist "%ZIP%" (
    echo [ERROR] payload zip not created.
    exit /b 1
)

echo.
echo Payload:  %ZIP%
for %%F in ("%ZIP%") do echo Size:     %%~zF bytes
echo.
echo Upload this file to the GitHub Release for SAITULS %VER%.
endlocal