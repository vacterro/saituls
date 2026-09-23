#!/usr/bin/env pwsh
<#
.SYNOPSIS
    One-click SAITULS install and repair.

.DESCRIPTION
    Elevates itself, installs only missing runtime tools, rebuilds the two
    small WinForms apps when the built-in .NET Framework compiler is present,
    and refreshes every Explorer context menu. Re-running it is safe.
#>

param(
    [switch]$SkipPayload,
    [switch]$NoLaunch,
    [switch]$WaitAtEnd,
    [switch]$EnableAutostart
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$failures = New-Object System.Collections.Generic.List[string]

# Every temporary file this run creates carries this token, so two setups can
# never delete or half-read each other's downloads. Previously the staging paths
# were four fixed names ($Destination.download and three saituls-*.zip), and each
# was deleted in its own finally -- so a second run wiped the first run's archive
# mid-extract and both reported a mysterious failure.
$runToken = [guid]::NewGuid().ToString('N').Substring(0, 8)

function Add-Failure([string]$Message) {
    $script:failures.Add($Message)
    Write-Host "[FAILED] $Message" -ForegroundColor Red
}

function Refresh-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Has-Command([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function New-StagingPath([string]$Name) {
    return Join-Path $env:TEMP "saituls-$script:runToken-$Name"
}

function Download-File([string]$Uri, [string]$Destination, [long]$MinimumBytes) {
    # Owned by this run, not derived from the destination: two runs repairing the
    # same missing file used to share "$Destination.download".
    $temp = New-StagingPath ((Split-Path -Leaf $Destination) + '.download')
    Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    try {
        Invoke-WebRequest -Uri $Uri -OutFile $temp -UseBasicParsing
        $item = Get-Item -LiteralPath $temp
        if ($item.Length -lt $MinimumBytes) { throw "download is only $($item.Length) bytes" }
        Move-Item -LiteralPath $temp -Destination $Destination -Force
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
}

# Self-elevate once. Every user-facing switch must survive the UAC hop.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $forward = @('-ExecutionPolicy', 'Bypass', '-NoProfile', '-File', "`"$($MyInvocation.MyCommand.Definition)`"")
    if ($SkipPayload) { $forward += '-SkipPayload' }
    if ($NoLaunch) { $forward += '-NoLaunch' }
    if ($WaitAtEnd) { $forward += '-WaitAtEnd' }
    if ($EnableAutostart) { $forward += '-EnableAutostart' }
    try {
        $elevated = Start-Process powershell.exe -ArgumentList $forward -Verb RunAs -Wait -PassThru
        exit $elevated.ExitCode
    }
    catch { Write-Host "Setup was cancelled. Nothing was changed." -ForegroundColor Yellow }
    exit 1
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$binDir = Join-Path $root 'Bin'
$saitulsExe = Join-Path $root 'SAITULS.exe'

# Single-flight. SAITULS' Home tab starts this script on every click of
# Install / repair and keeps no in-progress state, so a double-click or a manual
# INSTALL.cmd alongside it used to run two elevated repairs against the same
# downloads and the same registry. The mutex is held for the whole mutating run;
# a second instance says so and exits 0 rather than racing.
$setupMutex = $null
$mutexOwned = $false
try {
    $setupMutex = New-Object System.Threading.Mutex($true, 'Global\SAITULS_SETUP', [ref]$mutexOwned)
} catch {
    Write-Host "Could not check for another running setup: $($_.Exception.Message)" -ForegroundColor Yellow
    exit 1
}
if (-not $mutexOwned) {
    Write-Host 'Another SAITULS install / repair is already running.' -ForegroundColor Yellow
    Write-Host 'Nothing was changed. Wait for it to finish, then run this again if needed.' -ForegroundColor Gray
    if ($WaitAtEnd) { [void](Read-Host 'Press Enter to close') }
    exit 0
}

try {

New-Item -ItemType Directory -Path $binDir -Force | Out-Null
Set-Location $root

Write-Host '=== SAITULS install / repair ===' -ForegroundColor Cyan
Write-Host "Folder: $root" -ForegroundColor Gray
Write-Host 'Existing files are kept; only missing or rebuildable parts are touched.' -ForegroundColor Gray

# Python is required by five small Explorer tools and the SAISPIN watchdog.
# The Explorer menus invoke `pyw.exe`, the launcher that ships in the Windows
# directory, because a shell verb resolves a bare executable name only from the
# Windows directory and System32 -- never from PATH, where a per-user
# pythonw.exe lives. So the launcher's presence is part of "Python is ready",
# not an extra: without it the menus die with "Application not found".
$pythonReady = (Has-Command 'python.exe') -and (Has-Command 'pythonw.exe') -and (Has-Command 'pyw.exe')
if (-not $pythonReady) {
    Write-Host "`n[1/4] Installing Python silently..." -ForegroundColor Yellow
    if (Has-Command 'winget.exe') {
        try {
            & winget.exe install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements --silent
            if ($LASTEXITCODE -ne 0) { throw "winget returned $LASTEXITCODE" }
            Refresh-ProcessPath
            $pythonReady = (Has-Command 'python.exe') -and (Has-Command 'pythonw.exe') -and (Has-Command 'pyw.exe')
        } catch { Write-Host "winget was unavailable; using python.org instead." -ForegroundColor Yellow }
    }
    if (-not $pythonReady) {
        $pythonVersion = '3.13.14'
        $pythonInstaller = New-StagingPath "python-$pythonVersion-amd64.exe"
        try {
            Download-File "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-amd64.exe" $pythonInstaller 20000000
            # Include_launcher=1 puts py.exe/pyw.exe in the Windows directory,
            # which is what makes the Explorer menu commands resolvable at all.
            $process = Start-Process -FilePath $pythonInstaller -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_test=0' -Wait -PassThru
            if ($process.ExitCode -ne 0) { throw "Python installer returned $($process.ExitCode)" }
            Refresh-ProcessPath
            $pythonReady = (Has-Command 'python.exe') -and (Has-Command 'pythonw.exe') -and (Has-Command 'pyw.exe')
            if (-not $pythonReady) { throw 'Python installed but python.exe/pythonw.exe/pyw.exe are not available' }
        } catch { Add-Failure "Python: $($_.Exception.Message)" }
        finally { Remove-Item -LiteralPath $pythonInstaller -Force -ErrorAction SilentlyContinue }
    }
} else {
    Write-Host "`n[1/4] Python: ready" -ForegroundColor Green
}

# Clipboard+ needs real importable modules, not a Python interpreter alone.
# The Tools tab reports DEPENDENCY MISSING for exactly this pair, so the
# repair path that message points at must actually provide it. Import truth
# (not 'pip says installed') decides whether a wheel install is attempted.
if ($pythonReady) {
    Write-Host "`n[2/5] Checking Clipboard+ Python packages..." -ForegroundColor Yellow
    $probe = & python.exe -c "import win32clipboard, PIL; print('ok')" 2>$null
    if ($probe -ne 'ok') {
        try {
            & python.exe -m pip install --disable-pip-version-check --no-input pywin32 Pillow
            if ($LASTEXITCODE -ne 0) { throw "pip returned $LASTEXITCODE" }
            $probe = & python.exe -c "import win32clipboard, PIL; print('ok')" 2>$null
            if ($probe -ne 'ok') { throw 'packages installed but import still fails' }
            Write-Host '[2/5] Clipboard+ packages: installed' -ForegroundColor Green
        } catch { Add-Failure "Clipboard+ Python packages: $($_.Exception.Message)" }
    } else {
        Write-Host '[2/5] Clipboard+ packages: ready' -ForegroundColor Green
    }
}

# Rebuild from source when Windows has its normal .NET Framework compiler.
Write-Host "`n[3/5] Checking SAITULS apps..." -ForegroundColor Yellow
$cscCandidates = @(
    'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe',
    'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe'
)
$csc = $cscCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

function Compile-App([string]$Source, [string]$Output, [string[]]$References) {
    if (-not $script:csc) { return }
    $arguments = @('-nologo', '-target:winexe', "-out:$Output", '-optimize+')
    # CONTRIBUTING documents /win32icon for this app, and a repair that produced
    # an icon-less binary would not converge on the documented build.
    $icon = [System.IO.Path]::ChangeExtension($Source, '.ico')
    if (Test-Path -LiteralPath $icon) { $arguments += "-win32icon:$icon" }
    $arguments += $References | ForEach-Object { "-r:$_" }
    $arguments += $Source
    & $script:csc @arguments
    if ($LASTEXITCODE -ne 0) { throw "compiler returned $LASTEXITCODE for $(Split-Path -Leaf $Output)" }
}

# Why an app needs rebuilding, or '' when it does not. Existence was the old
# test, and it is the weakest possible one: a zero-byte or truncated SAITULS.exe
# satisfies Test-Path, suppresses the rebuild, and gets reported as ready -- which
# is the opposite of what README promises for a repair run.
function Get-RebuildReason([string]$Exe, [string]$Source) {
    if (-not (Test-Path -LiteralPath $Exe)) { return 'missing' }
    $item = Get-Item -LiteralPath $Exe
    if ($item.Length -lt 4096) { return "only $($item.Length) bytes" }
    # A PE starts 'MZ' and its header points at a 'PE\0\0' signature. Cheap, and
    # it catches the truncated/garbage cases a length check alone lets through.
    try {
        $head = [byte[]]::new(64)
        $stream = [System.IO.File]::OpenRead($Exe)
        try { $read = $stream.Read($head, 0, 64) } finally { $stream.Dispose() }
        if ($read -lt 64 -or $head[0] -ne 0x4D -or $head[1] -ne 0x5A) { return 'not a Windows executable' }
        $peOffset = [BitConverter]::ToInt32($head, 60)
        if ($peOffset -le 0 -or ($peOffset + 4) -gt $item.Length) { return 'corrupt PE header' }
        $sig = [byte[]]::new(4)
        $stream = [System.IO.File]::OpenRead($Exe)
        try {
            [void]$stream.Seek($peOffset, 'Begin')
            [void]$stream.Read($sig, 0, 4)
        } finally { $stream.Dispose() }
        if ($sig[0] -ne 0x50 -or $sig[1] -ne 0x45) { return 'corrupt PE signature' }
    } catch {
        return "unreadable ($($_.Exception.Message))"
    }
    if ((Test-Path -LiteralPath $Source) -and
        (Get-Item -LiteralPath $Source).LastWriteTimeUtc -gt $item.LastWriteTimeUtc) {
        return 'older than its source'
    }
    return ''
}

# Compile to a staging path first, then swap. Two reasons: a compiler failure
# must not leave a half-written exe where a working one was, and SAITULS starts
# this script from its own running window -- so when the target IS the live
# image, the swap is impossible and the staged build is kept with instructions
# instead of being written over a running process.
function Update-App([string]$Exe, [string]$Source, [string[]]$References, [string]$Reason) {
    $staged = New-StagingPath ((Split-Path -Leaf $Exe) + '.new')
    try {
        Compile-App $Source $staged $References
        if (-not (Test-Path -LiteralPath $staged)) { throw 'compiler produced no output' }
        $verdict = Get-RebuildReason $staged $Source
        # 'older than its source' is expected here: the staged file was just
        # built FROM that source, and a same-second timestamp can read as older.
        if ($verdict -and $verdict -ne 'older than its source') {
            throw "the rebuilt file is $verdict"
        }
        $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -and ($_.Path -ieq $Exe) } catch { $false }
        })
        if ($running.Count -gt 0) {
            $keep = "$Exe.new"
            Move-Item -LiteralPath $staged -Destination $keep -Force
            Add-Failure ("$(Split-Path -Leaf $Exe) is $Reason but is running (PID " +
                (($running | ForEach-Object { $_.Id }) -join ', ') + "). The new build is at " +
                "$(Split-Path -Leaf $keep) -- close the app and run this again, " +
                'or rename that file over the old one yourself.')
            return $false
        }
        Move-Item -LiteralPath $staged -Destination $Exe -Force
        # A previous blocked run may have parked its build next to the exe; the
        # swap just superseded it.
        Remove-Item -LiteralPath "$Exe.new" -Force -ErrorAction SilentlyContinue
        # Stamp the result strictly after the source. Without this, a source file
        # dated in the future -- clock skew, an archive extracted with preserved
        # timestamps, a file copied from another machine -- keeps reading as
        # "newer than the exe" and setup rebuilds on every single run, which is
        # the opposite of converging on a good build.
        try {
            $srcTime = (Get-Item -LiteralPath $Source).LastWriteTimeUtc
            $now = (Get-Date).ToUniversalTime()
            $stamp = if ($srcTime -ge $now) { $srcTime.AddSeconds(1) } else { $now }
            (Get-Item -LiteralPath $Exe).LastWriteTimeUtc = $stamp
        } catch { }
        Write-Host "$(Split-Path -Leaf $Exe): rebuilt ($Reason)" -ForegroundColor Green
        return $true
    } finally {
        Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue
    }
}

try {
    $saitulsSource = Join-Path $root 'SAITULS.cs'
    $reason = Get-RebuildReason $saitulsExe $saitulsSource
    if (-not $reason) {
        Write-Host 'SAITULS.exe: ready' -ForegroundColor Green
    } elseif (-not $csc) {
        Add-Failure "SAITULS.exe is $reason and no .NET Framework compiler was found to rebuild it"
    } else {
        [void](Update-App $saitulsExe $saitulsSource `
            @('System.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll') $reason)
    }
} catch { Add-Failure "App build: $($_.Exception.Message)" }

# Media payload. Missing pieces are fetched automatically; there is no menu
# that lets a user accidentally choose a half-working installation.
$ffmpeg = Join-Path $binDir 'FFMPEG.EXE'
$ffprobe = Join-Path $binDir 'FFPROBE.EXE'
$ytdlp = Join-Path $binDir 'yt-dlp.exe'
$aria2 = Join-Path $binDir 'ARIA2C.EXE'
$deno = Join-Path $binDir 'deno.exe'

# T-134: presence is NOT validity. Payload validation and atomic publication
# live in Scripts/media_payload.ps1 so they can be harness-driven; setup only
# orchestrates stage -> validate -> publish.
$mediaLib = Join-Path $root 'Scripts\media_payload.ps1'
if (-not $SkipPayload -and -not (Test-Path -LiteralPath $mediaLib -PathType Leaf)) {
    Add-Failure "Media payload: library missing: $mediaLib"
    $SkipPayload = $true
}
if (-not $SkipPayload) { . $mediaLib }

if ($SkipPayload) {
    Write-Host "`n[4/5] Media payload: skipped by -SkipPayload" -ForegroundColor Yellow
} else {
    Write-Host "`n[4/5] Installing missing media tools..." -ForegroundColor Yellow

    $ffmpegValid = (Test-ExecutableValid -Path $ffmpeg -Kind 'ffmpeg' -NoVersionProbe).Valid
    $ffprobeValid = (Test-ExecutableValid -Path $ffprobe -Kind 'ffprobe' -NoVersionProbe).Valid
    if (-not ($ffmpegValid -and $ffprobeValid)) {
        $invalid = @()
        if (-not $ffmpegValid) { $invalid += 'ffmpeg.exe' }
        if (-not $ffprobeValid) { $invalid += 'ffprobe.exe' }
        Write-Host ("FFmpeg pair: invalid/missing ({0}); rebuilding as one generation." -f ($invalid -join ', ')) -ForegroundColor Yellow
        $staging = New-StagingPath 'ffmpeg_pair'
        New-Item -ItemType Directory -Path $staging -Force | Out-Null
        try {
            $archive = Join-Path $staging 'ffmpeg.zip'
            Download-File 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' $archive 50000000
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [IO.Compression.ZipFile]::OpenRead($archive)
            try {
                foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
                    $entry = $zip.Entries | Where-Object { $_.Name -ieq $name } | Select-Object -First 1
                    if (-not $entry) { throw "$name is missing from the archive" }
                    [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $staging $name), $true)
                }
            } finally { $zip.Dispose() }
            # BOTH members are validated BEFORE either destination is touched.
            $stagedFfmpeg = Test-ExecutableValid -Path (Join-Path $staging 'ffmpeg.exe') -Kind 'ffmpeg' -NoVersionProbe
            $stagedFfprobe = Test-ExecutableValid -Path (Join-Path $staging 'ffprobe.exe') -Kind 'ffprobe' -NoVersionProbe
            if (-not ($stagedFfmpeg.Valid -and $stagedFfprobe.Valid)) {
                throw ("staged ffmpeg/ffprobe failed validation: {0}" -f (($stagedFfmpeg, $stagedFfprobe | ForEach-Object Reason) -join '; '))
            }
            # One generation: pair publish with rollback of the previous pair.
            Publish-Pair -StagingDir $staging -DestDir $binDir -Members @('ffmpeg.exe', 'ffprobe.exe')
            Rename-Item -LiteralPath (Join-Path $binDir 'ffmpeg.exe') 'FFMPEG.EXE' -ErrorAction SilentlyContinue
            Rename-Item -LiteralPath (Join-Path $binDir 'ffprobe.exe') 'FFPROBE.EXE' -ErrorAction SilentlyContinue
            Write-Host 'FFmpeg: ready' -ForegroundColor Green
        } catch { Add-Failure "FFmpeg: $($_.Exception.Message)" }
        finally { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
    } else { Write-Host 'FFmpeg: already present' -ForegroundColor Green }

    if (-not (Test-ExecutableValid -Path $ytdlp -Kind 'yt-dlp').Valid) {
        $staging = Join-Path $binDir ("yt-dlp.exe.stage_$runToken")
        try {
            Download-File 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' $staging 10000000
            $v = Test-ExecutableValid -Path $staging -Kind 'yt-dlp'
            if (-not $v.Valid) { throw "staged yt-dlp failed validation: $($v.Reason)" }
            Move-Item -LiteralPath $staging -Destination $ytdlp -Force
            Write-Host 'yt-dlp: ready' -ForegroundColor Green
        } catch { Add-Failure "yt-dlp: $($_.Exception.Message)" }
        finally { Remove-Item -LiteralPath $staging -Force -ErrorAction SilentlyContinue }
    } else { Write-Host 'yt-dlp: already present' -ForegroundColor Green }

    if (-not (Test-ExecutableValid -Path $aria2 -Kind 'aria2c').Valid) {
        $staging = New-StagingPath 'aria2_stage'
        New-Item -ItemType Directory -Path $staging -Force | Out-Null
        try {
            $archive = Join-Path $staging 'aria2.zip'
            Download-File 'https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip' $archive 1000000
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [IO.Compression.ZipFile]::OpenRead($archive)
            try {
                $entry = $zip.Entries | Where-Object { $_.Name -ieq 'aria2c.exe' } | Select-Object -First 1
                if (-not $entry) { throw 'aria2c.exe is missing from the archive' }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $staging 'aria2c.exe'), $true)
            } finally { $zip.Dispose() }
            $v = Test-ExecutableValid -Path (Join-Path $staging 'aria2c.exe') -Kind 'aria2c'
            if (-not $v.Valid) { throw "staged aria2c failed validation: $($v.Reason)" }
            Publish-Pair -StagingDir $staging -DestDir $binDir -Members @('aria2c.exe')
            Rename-Item -LiteralPath (Join-Path $binDir 'aria2c.exe') 'ARIA2C.EXE' -ErrorAction SilentlyContinue
            Write-Host 'aria2: ready' -ForegroundColor Green
        } catch { Add-Failure "aria2: $($_.Exception.Message)" }
        finally { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
    } else { Write-Host 'aria2: already present' -ForegroundColor Green }

    if (-not (Test-ExecutableValid -Path $deno -Kind 'deno').Valid) {
        $staging = New-StagingPath 'deno_stage'
        New-Item -ItemType Directory -Path $staging -Force | Out-Null
        try {
            $archive = Join-Path $staging 'deno.zip'
            Download-File 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' $archive 10000000
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [IO.Compression.ZipFile]::OpenRead($archive)
            try {
                $entry = $zip.Entries | Where-Object { $_.Name -ieq 'deno.exe' } | Select-Object -First 1
                if (-not $entry) { throw 'deno.exe is missing from the archive' }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $staging 'deno.exe'), $true)
            } finally { $zip.Dispose() }
            $v = Test-ExecutableValid -Path (Join-Path $staging 'deno.exe') -Kind 'deno'
            if (-not $v.Valid) { throw "staged deno failed validation: $($v.Reason)" }
            Publish-Pair -StagingDir $staging -DestDir $binDir -Members @('deno.exe')
            Write-Host 'Deno JavaScript runtime: ready' -ForegroundColor Green
        } catch { Add-Failure "Deno: $($_.Exception.Message)" }
        finally { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
    } else { Write-Host 'Deno JavaScript runtime: already present' -ForegroundColor Green }
}

Write-Host "`n[5/5] Refreshing Explorer menus..." -ForegroundColor Yellow
$installer = Join-Path $root 'Installers\INSTALL_ALL.PS1'
try {
    if (-not (Test-Path $installer)) { throw "installer not found: $installer" }
    & $installer
    # $? only reports whether PowerShell itself faulted. The installer signals a
    # failed registry import through its exit code, so that is what decides here.
    if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) {
        throw "context-menu installer exited with code $LASTEXITCODE"
    }
    Write-Host 'Explorer menus: installed' -ForegroundColor Green
} catch { Add-Failure "Explorer menus: $($_.Exception.Message)" }

# T-133 6.2: the direct Run-key write below is project-owned registry mutation
# and must also serialize through the one cross-process contract. INSTALL_ALL
# already acquires that lock itself; this second acquisition is the bounded
# autostart transaction and follows the same lock.
if ($EnableAutostart -and (Test-Path $saitulsExe)) {
    try {
        # The INI is the authoritative setting and the Run key is its projection:
        # SAITULS reapplies AutoStart from the INI on every launch, so writing
        # only the Run key here enabled autostart until the very next start, at
        # which point the app read AutoStart=0 and deleted the entry again.
        $ini = Join-Path $root 'SAITULS.ini'
        $lines = if (Test-Path -LiteralPath $ini) { @(Get-Content -LiteralPath $ini) } else { @('[saituls]') }
        if ($lines -notcontains '[saituls]') { $lines = @('[saituls]') + $lines }
        if ($lines -match '^\s*AutoStart\s*=') {
            $lines = $lines | ForEach-Object { if ($_ -match '^\s*AutoStart\s*=') { 'AutoStart=1' } else { $_ } }
        } else {
            $lines = @($lines[0]) + 'AutoStart=1' + $lines[1..($lines.Count - 1)]
        }
        Set-Content -LiteralPath $ini -Value $lines -Encoding UTF8

        $autostartMutex = New-Object Threading.Mutex($false, 'Global\SAITULS_REGISTRY_MUTATION')
        $autostartOwned = $false
        try {
            try { $autostartOwned = $autostartMutex.WaitOne(5000) }
            catch [Threading.AbandonedMutexException] { $autostartOwned = $true }
            if (-not $autostartOwned) { throw 'BUSY: SAITULS registry mutation is already in progress' }
            $runKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Run', $true)
            if (-not $runKey) { throw 'Run registry key is unavailable' }
            try { $runKey.SetValue('SaitulsApp', "`"$saitulsExe`" --minimized") } finally { $runKey.Close() }
        } finally {
            if ($autostartOwned) { $autostartMutex.ReleaseMutex() }
            $autostartMutex.Dispose()
        }
        Write-Host 'Start with Windows: enabled' -ForegroundColor Green
    } catch { Add-Failure "Autostart: $($_.Exception.Message)" }
}

Write-Host ''
if ($failures.Count -eq 0) {
    Write-Host '=== SAITULS is ready ===' -ForegroundColor Cyan
    Write-Host 'Open SAITULS.exe or SAITULS_LAUNCHER.cmd. Setup can be run again to repair the installation.' -ForegroundColor Green
    if (-not $NoLaunch -and (Test-Path $saitulsExe)) {
        # Explorer is the medium-integrity shell, so the everyday app does not
        # inherit this setup process's administrator token.
        Start-Process explorer.exe -ArgumentList "`"$saitulsExe`""
    }
} else {
    Write-Host "=== Setup finished with $($failures.Count) problem(s) ===" -ForegroundColor Red
    foreach ($failure in $failures) { Write-Host " - $failure" -ForegroundColor Red }
    Write-Host 'Run INSTALL.cmd again after checking the internet connection.' -ForegroundColor Yellow
}

if ($WaitAtEnd) { [void](Read-Host 'Press Enter to close') }
if ($failures.Count -gt 0) { exit 1 }
exit 0

} finally {
    # Anything this run staged and did not consume, and only this run's files:
    # the token in the name is what makes that safe to do unconditionally.
    Get-ChildItem -Path $env:TEMP -Filter "saituls-$runToken-*" -File -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    if ($setupMutex) {
        if ($mutexOwned) { try { $setupMutex.ReleaseMutex() } catch { } }
        try { $setupMutex.Dispose() } catch { }
    }
}
