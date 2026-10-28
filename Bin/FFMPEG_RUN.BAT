@echo off
rem T-167: thin launcher. All safety lives in FFMPEG_RUN.PS1.
rem %1 input file  %2 ffmpeg args  %3 output suffix
rem The args string starts with "-c:a..." so PS would parse it as a parameter;
rem pass it through an env var instead of the command line.
set "SAIFMPEG_ARGS=%~2"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0FFMPEG_RUN.PS1" -InputPath "%~1" -Suffix "%~3"
set "RC=%ERRORLEVEL%"
set "SAIFMPEG_ARGS="
if not "%RC%"=="0" if not "%RC%"=="1" pause
exit /b %RC%