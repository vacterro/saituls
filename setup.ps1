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

function Download-File([string]$Uri, [string]$Destination, [long]$MinimumBytes) {
    $temp = "$Destination.download"
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
$limisawExe = Join-Path $root 'LIMISAW.exe'
New-Item -ItemType Directory -Path $binDir -Force | Out-Null
Set-Location $root

Write-Host '=== SAITULS install / repair ===' -ForegroundColor Cyan
Write-Host "Folder: $root" -ForegroundColor Gray
Write-Host 'Existing files are kept; only missing or rebuildable parts are touched.' -ForegroundColor Gray

# Python is required by five small Explorer tools and the LIMISAW probe.
$pythonReady = (Has-Command 'python.exe') -and (Has-Command 'pythonw.exe')
if (-not $pythonReady) {
    Write-Host "`n[1/4] Installing Python silently..." -ForegroundColor Yellow
    if (Has-Command 'winget.exe') {
        try {
            & winget.exe install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements --silent
            if ($LASTEXITCODE -ne 0) { throw "winget returned $LASTEXITCODE" }
            Refresh-ProcessPath
            $pythonReady = (Has-Command 'python.exe') -and (Has-Command 'pythonw.exe')
        } catch { Write-Host "winget was unavailable; using python.org instead." -ForegroundColor Yellow }
    }
    if (-not $pythonReady) {
        $pythonVersion = '3.13.14'
        $pythonInstaller = Join-Path $env:TEMP "python-$pythonVersion-amd64.exe"
        try {
            Download-File "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-amd64.exe" $pythonInstaller 20000000
            $process = Start-Process -FilePath $pythonInstaller -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -Wait -PassThru
            if ($process.ExitCode -ne 0) { throw "Python installer returned $($process.ExitCode)" }
            Refresh-ProcessPath
            $pythonReady = (Has-Command 'python.exe') -and (Has-Command 'pythonw.exe')
            if (-not $pythonReady) { throw 'Python installed but python.exe/pythonw.exe are not on PATH' }
        } catch { Add-Failure "Python: $($_.Exception.Message)" }
        finally { Remove-Item -LiteralPath $pythonInstaller -Force -ErrorAction SilentlyContinue }
    }
} else {
    Write-Host "`n[1/4] Python: ready" -ForegroundColor Green
}

# Rebuild from source when Windows has its normal .NET Framework compiler.
Write-Host "`n[2/4] Checking SAITULS apps..." -ForegroundColor Yellow
$cscCandidates = @(
    'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe',
    'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe'
)
$csc = $cscCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

function Compile-App([string]$Source, [string]$Output, [string[]]$References) {
    if (-not $script:csc) { return }
    $arguments = @('-nologo', '-target:winexe', "-out:$Output", '-optimize+')
    $arguments += $References | ForEach-Object { "-r:$_" }
    $arguments += $Source
    & $script:csc @arguments
    if ($LASTEXITCODE -ne 0) { throw "compiler returned $LASTEXITCODE for $(Split-Path -Leaf $Output)" }
}

try {
    if (-not (Test-Path $saitulsExe) -and $csc) {
        Compile-App (Join-Path $root 'SAITULS.cs') $saitulsExe @('System.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll')
    }
    if (-not (Test-Path $limisawExe) -and $csc) {
        Compile-App (Join-Path $root 'LIMISAW.cs') $limisawExe @('System.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll', 'System.Web.Extensions.dll')
    }
    if ((Test-Path $saitulsExe) -and (Test-Path $limisawExe)) {
        Write-Host 'Prebuilt apps: ready' -ForegroundColor Green
    } else {
        Add-Failure 'A prebuilt app is missing and could not be rebuilt'
    }
} catch { Add-Failure "App build: $($_.Exception.Message)" }

# Media payload. Missing pieces are fetched automatically; there is no menu
# that lets a user accidentally choose a half-working installation.
$ffmpeg = Join-Path $binDir 'FFMPEG.EXE'
$ffprobe = Join-Path $binDir 'FFPROBE.EXE'
$ytdlp = Join-Path $binDir 'yt-dlp.exe'
$aria2 = Join-Path $binDir 'ARIA2C.EXE'
$deno = Join-Path $binDir 'deno.exe'

if ($SkipPayload) {
    Write-Host "`n[3/4] Media payload: skipped by -SkipPayload" -ForegroundColor Yellow
} else {
    Write-Host "`n[3/4] Installing missing media tools..." -ForegroundColor Yellow
    if (-not (Test-Path $ffmpeg) -or -not (Test-Path $ffprobe)) {
        $archive = Join-Path $env:TEMP 'saituls-ffmpeg.zip'
        try {
            Download-File 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' $archive 50000000
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [IO.Compression.ZipFile]::OpenRead($archive)
            try {
                foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
                    $entry = $zip.Entries | Where-Object { $_.Name -ieq $name } | Select-Object -First 1
                    if (-not $entry) { throw "$name is missing from the archive" }
                    [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $binDir $name), $true)
                }
            } finally { $zip.Dispose() }
            Write-Host 'FFmpeg: ready' -ForegroundColor Green
        } catch { Add-Failure "FFmpeg: $($_.Exception.Message)" }
        finally { Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue }
    } else { Write-Host 'FFmpeg: already present' -ForegroundColor Green }

    if (-not (Test-Path $ytdlp)) {
        try {
            Download-File 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' $ytdlp 10000000
            Write-Host 'yt-dlp: ready' -ForegroundColor Green
        } catch { Add-Failure "yt-dlp: $($_.Exception.Message)" }
    } else { Write-Host 'yt-dlp: already present' -ForegroundColor Green }

    if (-not (Test-Path $aria2)) {
        $archive = Join-Path $env:TEMP 'saituls-aria2.zip'
        try {
            Download-File 'https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip' $archive 1000000
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [IO.Compression.ZipFile]::OpenRead($archive)
            try {
                $entry = $zip.Entries | Where-Object { $_.Name -ieq 'aria2c.exe' } | Select-Object -First 1
                if (-not $entry) { throw 'aria2c.exe is missing from the archive' }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $aria2, $true)
            } finally { $zip.Dispose() }
            Write-Host 'aria2: ready' -ForegroundColor Green
        } catch { Add-Failure "aria2: $($_.Exception.Message)" }
        finally { Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue }
    } else { Write-Host 'aria2: already present' -ForegroundColor Green }

    if (-not (Test-Path $deno)) {
        $archive = Join-Path $env:TEMP 'saituls-deno.zip'
        try {
            Download-File 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' $archive 10000000
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [IO.Compression.ZipFile]::OpenRead($archive)
            try {
                $entry = $zip.Entries | Where-Object { $_.Name -ieq 'deno.exe' } | Select-Object -First 1
                if (-not $entry) { throw 'deno.exe is missing from the archive' }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $deno, $true)
            } finally { $zip.Dispose() }
            Write-Host 'Deno JavaScript runtime: ready' -ForegroundColor Green
        } catch { Add-Failure "Deno: $($_.Exception.Message)" }
        finally { Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue }
    } else { Write-Host 'Deno JavaScript runtime: already present' -ForegroundColor Green }
}

Write-Host "`n[4/4] Refreshing Explorer menus..." -ForegroundColor Yellow
$installer = Join-Path $root 'Installers\INSTALL_ALL.PS1'
try {
    if (-not (Test-Path $installer)) { throw "installer not found: $installer" }
    & $installer
    if (-not $?) { throw 'context-menu installer returned an error' }
    Write-Host 'Explorer menus: installed' -ForegroundColor Green
} catch { Add-Failure "Explorer menus: $($_.Exception.Message)" }

if ($EnableAutostart -and (Test-Path $saitulsExe)) {
    try {
        $runKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Run', $true)
        if (-not $runKey) { throw 'Run registry key is unavailable' }
        try { $runKey.SetValue('SaitulsApp', "`"$saitulsExe`" --minimized") } finally { $runKey.Close() }
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
