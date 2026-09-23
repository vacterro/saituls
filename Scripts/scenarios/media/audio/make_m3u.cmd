@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem T-166 launch contract: the launcher's positional folder is authoritative.
rem Delayed expansion stays OFF while the path is handled: with it ON, cmd
rem silently eats every '!' in the folder name, and '&' or ')' in the value
rem would break any later unquoted %TARGET% expansion.
set "TARGET=%~1"
if not defined TARGET (
    echo No folder supplied.
    exit /b 2
)
if not exist "%TARGET%\" (
    echo Target is not an existing folder: "%TARGET%"
    exit /b 2
)

pushd "%TARGET%"
if errorlevel 1 (
    echo Cannot enter folder: "%TARGET%"
    exit /b 2
)

set "M3U="
for %%F in (*.m3u) do if not defined M3U set "M3U=%%F"
if not defined M3U (
    echo .m3u file not found in "%TARGET%"
    popd
    exit /b 1
)

echo Found playlist: "%M3U%"
echo Creating list.txt...

rem Per-line work goes through a subroutine so %%A substitution happens while
rem delayed expansion is OFF (a '!' in a playlist line would otherwise be
rem eaten); the subroutine enables it only around the echo, where delayed
rem values cannot break the command.
for /f "usebackq delims=" %%A in ("%M3U%") do call :add "%%A"

echo list.txt written to the current folder.
popd
exit /b 0

:add
set "line=%~1"
if not defined line goto :eof
if /i "%line:~0,1%"=="#" goto :eof
setlocal EnableDelayedExpansion
set "safe=%line:'='\''%"
>>list.txt echo file '!safe!'
endlocal
goto :eof
