@echo off
@chcp 65001 > NUL
title Download From Youtube

:: Bin first on PATH so yt-dlp finds aria2c (it rejects absolute --downloader paths)
set "PATH=%~dp0..\Bin;%PATH%"
:: yt-dlp needs a JS runtime for YouTube extraction. Use node if available
:: (yt-dlp-ejs package or regular node install), otherwise skip the flag.
set "params=--force-overwrites --quiet --progress --ffmpeg-location "%~dp0..\Bin\FFMPEG.EXE""
where node >nul 2>nul && set "params=--js-runtimes node %params%"

:: Set default outpath
set "outpath=%cd%"
if /i "%outpath%"=="C:\Windows\system32" set "outpath=%USERPROFILE%\Downloads\_YT_VIDEO"
if /i "%outpath%\"=="%~dp0" set "outpath=%USERPROFILE%\Downloads\_YT_VIDEO"
if not exist "%outpath%" mkdir "%outpath%"

setlocal

:: Get clipboard and save to temporary file in UTF-8
powershell -NoProfile -Command "Get-Clipboard | Out-File -FilePath $env:TEMP\yt-dlp-urls.txt -Encoding utf8"

if "%1"=="audio" goto audio
if "%1"=="video" goto video
if "%1"=="audiodated" goto audiodated
if "%1"=="videodated" goto videodated
if "%1"=="audioplaylist" goto audioplaylist
if "%1"=="videoplaylist" goto videoplaylist
goto end

:video
title Processing...
"%~dp0..\Bin\yt-dlp.exe" -a "%TEMP%\yt-dlp-urls.txt" --no-playlist -o "%outpath%\%%(title)s.%%(ext)s" -f "bestvideo[vcodec^=av01][height<=1080]+bestaudio/bestvideo[height<=1080]+bestaudio/best" --merge-output-format mkv %params%
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Ошибка при скачивании! Смотрите вывод выше.
    pause
)
goto end

:videodated
title Processing...
"%~dp0..\Bin\yt-dlp.exe" -a "%TEMP%\yt-dlp-urls.txt" --no-playlist -o "%outpath%\%%(upload_date>%%Y-%%m-%%d)s - %%(title)s.%%(ext)s" -f "bestvideo[vcodec^=av01][height<=1080]+bestaudio/bestvideo[height<=1080]+bestaudio/best" --merge-output-format mkv %params%
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Ошибка при скачивании! Смотрите вывод выше.
    pause
)
goto end

:videoplaylist
title Processing...
"%~dp0..\Bin\yt-dlp.exe" -a "%TEMP%\yt-dlp-urls.txt" --yes-playlist -o "%outpath%\%%(playlist_title)s\%%(playlist_index)s - %%(title)s.%%(ext)s" -f "bestvideo[vcodec^=av01][height<=1080]+bestaudio/bestvideo[height<=1080]+bestaudio/best" --merge-output-format mkv %params%
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Ошибка при скачивании! Смотрите вывод выше.
    pause
)
goto end

:audio
title Processing...
"%~dp0..\Bin\yt-dlp.exe" -a "%TEMP%\yt-dlp-urls.txt" --no-playlist -o "%outpath%\%%(title)s.%%(ext)s" -f bestaudio --extract-audio --audio-format mp3 --audio-quality 0 %params%
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Ошибка при скачивании! Смотрите вывод выше.
    pause
)
goto end

:audiodated
title Processing...
"%~dp0..\Bin\yt-dlp.exe" -a "%TEMP%\yt-dlp-urls.txt" --no-playlist -o "%outpath%\%%(upload_date>%%Y-%%m-%%d)s - %%(title)s.%%(ext)s" -f bestaudio --extract-audio --audio-format mp3 --audio-quality 0 %params%
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Ошибка при скачивании! Смотрите вывод выше.
    pause
)
goto end

:audioplaylist
title Processing...
"%~dp0..\Bin\yt-dlp.exe" -a "%TEMP%\yt-dlp-urls.txt" --yes-playlist -o "%outpath%\%%(playlist_title)s\%%(playlist_index)s - %%(title)s.%%(ext)s" -f bestaudio --extract-audio --audio-format mp3 --audio-quality 0 %params%
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Ошибка при скачивании! Смотрите вывод выше.
    pause
)
goto end

:end
