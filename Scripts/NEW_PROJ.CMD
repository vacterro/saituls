@echo off
setlocal
set "target=%~1"
if not defined target (
    echo [NEW_PROJ] No target folder passed.
    pause
    exit /b 1
)
:: Remove trailing backslash if exists
if "%target:~-1%"=="\" set "target=%target:~0,-1%"
if not exist "%target%\" (
    echo [NEW_PROJ] Target folder not found: %target%
    pause
    exit /b 1
)
set "directory=%target%\_new_project"

call :ensure "%directory%" root || goto :fail
for %%n in (ae c4d _output _input) do call :ensure "%directory%\%%n" %%n || goto :fail

for %%n in (ae c4d _output _input) do if not exist "%directory%\%%n\" (
    echo [NEW_PROJ] Required folder missing after creation: %directory%\%%n
    goto :fail
)

exit /b 0

:ensure
if exist "%~1\" exit /b 0
if exist "%~1" (
    echo [NEW_PROJ] Not a folder, refusing to replace: %~1
    exit /b 1
)
md "%~1"
if errorlevel 1 (
    echo [NEW_PROJ] Failed to create: %~1
    echo Check permissions or whether the path is on a writable drive.
    exit /b 1
)
set "own_%~2=1"
exit /b 0

:: Removes only the folders this run created, and only while they stay empty.
:rollback
for %%n in (_input _output c4d ae) do if defined own_%%n rd "%directory%\%%n" 2>nul
if defined own_root rd "%directory%" 2>nul
exit /b 0

:fail
call :rollback
pause
exit /b 1
