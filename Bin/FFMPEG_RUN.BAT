@echo off
set "input=%~1"
set "args=%~2"
set "suffix_ext=%~3"
set "output=%~dpn1%suffix_ext%"

echo ========================================================
echo Input  : "%input%"
echo Output : "%output%"
echo ========================================================

"%~dp0FFMPEG.EXE" -i "%input%" %args% "%output%"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ENCODING ERROR! Check the log above.
    pause
)
