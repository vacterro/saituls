@echo off
rem T-167: thin launcher. All Merge Audio logic lives in MERGE_AUD.PS1.
rem The old all-CMD implementation fought for/f + usebackq + codepage
rem re-parsing; the PowerShell worker owns the contract now.
setlocal
set "MERGE_IN=%~1"
set "MERGE_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0MERGE_AUD.PS1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if defined T167_NO_PAUSE goto done
if not "%RC%"=="0" pause
:done
exit /b %RC%