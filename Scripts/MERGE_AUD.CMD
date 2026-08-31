@echo off
@chcp 65001 > NUL
setlocal
set "ffmpeg=%~dp0..\Bin\FFMPEG.EXE"
set "in=%~1"
set "out=%~dpn1_merged%~x1"

"%ffmpeg%" -i "%in%" -filter_complex "[0:a]amerge=inputs=2[aout]" -map 0:v -map "[aout]" -c:v copy -c:a aac -b:a 320k "%out%" -y
if %ERRORLEVEL% equ 0 (
    move /y "%out%" "%in%"
) else (
    echo Произошла ошибка при слиянии аудио.
    if exist "%out%" del "%out%"
    pause
)
endlocal
