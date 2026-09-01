@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0SAITULS.exe" (
    echo SAITULS.exe is missing. Run INSTALL.cmd first.
    pause
    exit /b 1
)

set "PATH=%~dp0Bin;%~dp0Bin\App;%PATH%"
start "" "%~dp0SAITULS.exe"
endlocal
