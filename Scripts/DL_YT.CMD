@echo off
rem T-167: thin compatibility launcher. All download orchestration (input
rem validation, per-run URL isolation, modes, exit codes) lives in DL_YT.PS1.
rem Explorer contract is unchanged: %1 = mode, %2 = destination folder.
setlocal
set "DL_MODE=%~1"
if not defined DL_MODE set "DL_MODE=video"
set "DL_OUT=%~2"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0DL_YT.PS1" -Mode "%DL_MODE%" -OutPath "%DL_OUT%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if not defined T167_NO_PAUSE pause
exit /b %RC%
