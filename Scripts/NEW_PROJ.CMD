@echo off
setlocal
set "target=%~1"
:: Remove trailing backslash if exists
if "%target:~-1%"=="\" set "target=%target:~0,-1%"
if not defined target (
    echo [NEW_PROJ] No target folder passed.
    pause
    exit /b 1
)
if not exist "%target%\" (
    echo [NEW_PROJ] Target folder not found: %target%
    pause
    exit /b 1
)
set "directory=%target%\_new_project"

md "%directory%\ae" 2>nul
md "%directory%\c4d" 2>nul
md "%directory%\_output" 2>nul
md "%directory%\_input" 2>nul

if not exist "%directory%\_output\" (
    echo [NEW_PROJ] Failed to create: %directory%
    echo Check permissions or whether the path is on a writable drive.
    pause
    exit /b 1
)
endlocal
exit /b 0
