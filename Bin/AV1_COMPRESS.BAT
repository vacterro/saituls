@echo off
set "input=%~1"
set "output=%~dpn1_AV1%~x1"

echo ========================================================
echo Input  : "%input%"
echo Output : "%output%"
echo ========================================================

"%~dp0FFMPEG.EXE" -i "%input%" -map 0 -c copy -c:v libsvtav1 -preset 6 -crf 24 -pix_fmt yuv420p10le -svtav1-params tune=0 "%output%"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ENCODING ERROR! Check the log above.
    pause
)
