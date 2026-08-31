#!/usr/bin/env pwsh
<#
.SYNOPSIS
    One-click SAITULS setup. Elevates, installs context menus, compiles
    SAITULS.exe, and provisions the heavy payload (ffmpeg, yt-dlp etc.)
    from official sources — no pre-built release asset required.

    Run from the SAITULS root:
        powershell -ExecutionPolicy Bypass -File setup.ps1
#>

$ErrorActionPreference = "Stop"

# ── Self-elevate ──────────────────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $myArgs = @('-ExecutionPolicy','Bypass','-NoProfile','-File',"`"$($MyInvocation.MyCommand.Definition)`"")
    Start-Process powershell.exe -ArgumentList $myArgs -Verb RunAs
    exit
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

Write-Host "=== SAITULS Setup ===" -ForegroundColor Cyan
Write-Host "Root: $root" -ForegroundColor Gray

# ── Prerequisites ─────────────────────────────────────────────────────────
$hasPython = $false
$pythonInstalled = $false
try {
    $v = & python --version 2>&1
    if ($v -match 'Python 3\.\d+') { $hasPython = $true; Write-Host "Python: $v" -ForegroundColor Green }
    else { Write-Host "Python: $v (may work, but 3.x recommended)" -ForegroundColor Yellow }
} catch {
    $pythonUrl = "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe"
    $pythonExe = Join-Path $env:TEMP "python-3.12.9-amd64.exe"
    Write-Host "Python: not found — attempting auto-install..." -ForegroundColor Yellow
    # Try winget first (Windows 10/11). If fails, download from python.org.
    $wingetOk = $false
    try {
        $wv = & winget --version 2>&1
        if ($LASTEXITCODE -eq 0) { $wingetOk = $true }
    } catch { }
    if ($wingetOk) {
        try {
            & winget install -e --id Python.Python.3.12 --accept-package-agreements --silent 2>&1 | Out-Null
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
            $v = & python --version 2>&1
            if ($v -match 'Python 3\.\d+') { $hasPython = $true; $pythonInstalled = $true; Write-Host "Python installed via winget: $v" -ForegroundColor Green }
        } catch { Write-Host "winget install failed, trying direct download..." -ForegroundColor Yellow }
    }
    if (-not $hasPython) {
        try {
            Write-Host "Downloading Python from python.org (~30 MB)..." -ForegroundColor Yellow
            Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonExe -UseBasicParsing
            Write-Host "Running Python installer (silent)..." -ForegroundColor Yellow
            $p = Start-Process -FilePath $pythonExe -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1" -Wait -PassThru
            if ($p.ExitCode -eq 0) {
                $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
                $v = & python --version 2>&1
                if ($v -match 'Python 3\.\d+') { $hasPython = $true; $pythonInstalled = $true; Write-Host "Python installed: $v" -ForegroundColor Green }
                else { Write-Host "Python installer ran but python not on PATH" -ForegroundColor Red }
            } else { Write-Host "Python installer exit code: $($p.ExitCode)" -ForegroundColor Red }
            Remove-Item $pythonExe -Force -ErrorAction SilentlyContinue
        } catch {
            Write-Host "Python auto-install failed — .PYW menu items will NOT work" -ForegroundColor Red
            Write-Host "  Install Python manually: https://www.python.org/downloads/" -ForegroundColor Gray
        }
    }
}

$hasDotNet = $false
try {
    $csc = Get-Command csc.exe -ErrorAction Stop
    $hasDotNet = $true; Write-Host ".NET: $($csc.Source)" -ForegroundColor Green
} catch { Write-Host ".NET: csc.exe not found — SAITULS.exe needs pre-built binary or .NET SDK" -ForegroundColor Yellow }

# ── Compile SAITULS.exe + LIMISAW.exe if csc available ────────────────
$hasWebExt = ($null -ne ([System.Reflection.Assembly]::LoadWithPartialName("System.Web.Extensions")) -or `
              (Test-Path "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Web.Extensions.dll"))
function Compile-Exe {
    param([string]$Source, [string]$Out, [string[]]$Refs = @("System.dll","System.Drawing.dll","System.Windows.Forms.dll"))
    Write-Host "Compiling $Out..." -ForegroundColor Yellow
    $argList = @("-nologo","-target:winexe","-out:$Out","-optimize+") + ($Refs | ForEach-Object { "-r:$_" }) + @($Source)
    & csc.exe @argList | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "  $Out compiled" -ForegroundColor Green }
    else { Write-Host "  $Out FAILED" -ForegroundColor Red }
}

$saitulsExe = Join-Path $root "SAITULS.exe"
$limisawExe = Join-Path $root "LIMISAW.exe"
if ($hasDotNet) {
    if (-not (Test-Path $saitulsExe)) {
        Compile-Exe (Join-Path $root "SAITULS.cs") $saitulsExe
    }
    if (-not (Test-Path $limisawExe) -and $hasWebExt) {
        Compile-Exe (Join-Path $root "LIMISAW.cs") $limisawExe @("System.dll","System.Drawing.dll","System.Windows.Forms.dll","System.Web.Extensions.dll")
    } elseif (-not (Test-Path $limisawExe)) {
        Write-Host "LIMISAW.exe: System.Web.Extensions not found, skipping compile" -ForegroundColor Yellow
    }
}

# ── Payload download (ffmpeg, yt-dlp, aria2c) ─────────────────────────────
$binDir = Join-Path $root "Bin"
$needFfmpeg = -not (Test-Path (Join-Path $binDir "FFMPEG.EXE"))
$needYtdlp  = -not (Test-Path (Join-Path $binDir "yt-dlp.exe"))
$needAria2  = -not (Test-Path (Join-Path $binDir "ARIA2C.EXE"))

if ($needFfmpeg -or $needYtdlp -or $needAria2) {
    Write-Host "`nSome payload binaries are missing. Options:" -ForegroundColor Yellow
    Write-Host "  1. Download from official sources (recommended)"
    Write-Host "  2. Skip — menus will work, but media tools (FFmpeg, yt-dlp) will fail"
    Write-Host "  3. Download from GitHub Releases (SAITULS-payload-<ver>.zip)"
    $choice = Read-Host "Choice (1/2/3)"
} else {
    $choice = "0"
    Write-Host "Payload: all binaries present" -ForegroundColor Green
}

if ($choice -eq "1") {
    if ($needFfmpeg) {
        Write-Host "Downloading ffmpeg (gyan.dev, ~160 MB)..." -ForegroundColor Yellow
        $ffmpegZip = Join-Path $env:TEMP "ffmpeg-release.zip"
        try {
            Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $ffmpegZip -UseBasicParsing
            # Extract just ffmpeg.exe + ffprobe.exe
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [System.IO.Compression.ZipFile]::OpenRead($ffmpegZip)
            $entries = $zip.Entries | Where-Object { $_.Name -match '^(ffmpeg|ffprobe)\.exe$' }
            foreach ($e in $entries) {
                $target = Join-Path $binDir $e.Name
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
                Write-Host "  Extracted: $($e.Name)" -ForegroundColor Green
            }
            $zip.Dispose()
            Remove-Item $ffmpegZip -Force -ErrorAction SilentlyContinue
        } catch { Write-Host "  ffmpeg download failed: $_" -ForegroundColor Red }
    }
    if ($needYtdlp) {
        Write-Host "Downloading yt-dlp (~17 MB)..." -ForegroundColor Yellow
        try {
            $ytdlp = Join-Path $binDir "yt-dlp.exe"
            Invoke-WebRequest -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile $ytdlp -UseBasicParsing
            Write-Host "  yt-dlp.exe downloaded" -ForegroundColor Green
        } catch { Write-Host "  yt-dlp download failed: $_" -ForegroundColor Red }
    }
    if ($needAria2) {
        Write-Host "Downloading aria2c (~5 MB)..." -ForegroundColor Yellow
        try {
            $aria2Zip = Join-Path $env:TEMP "aria2.zip"
            Invoke-WebRequest -Uri "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip" -OutFile $aria2Zip -UseBasicParsing
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [System.IO.Compression.ZipFile]::OpenRead($aria2Zip)
            $entry = $zip.Entries | Where-Object { $_.Name -eq 'aria2c.exe' } | Select-Object -First 1
            if ($entry) {
                $target = Join-Path $binDir "aria2c.exe"
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
                Write-Host "  aria2c.exe extracted" -ForegroundColor Green
            }
            $zip.Dispose()
            Remove-Item $aria2Zip -Force -ErrorAction SilentlyContinue
        } catch { Write-Host "  aria2c download failed: $_" -ForegroundColor Red }
    }
} elseif ($choice -eq "3") {
    Write-Host "Download payload from GitHub Releases..."
    Write-Host "  URL: https://github.com/<owner>/SAITULS/releases/latest/download/SAITULS-payload-<ver>.zip"
    $url = Read-Host "Enter the full download URL"
    if ($url) {
        try {
            $zip = Join-Path $env:TEMP "saituls-payload.zip"
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
            Expand-Archive -Path $zip -DestinationPath $root -Force
            Write-Host "Payload extracted" -ForegroundColor Green
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
        } catch { Write-Host "Download failed: $_" -ForegroundColor Red }
    }
}

# ── Install context menus ─────────────────────────────────────────────────
$installer = Join-Path $root "Installers\INSTALL_ALL.PS1"
if (Test-Path $installer) {
    Write-Host "`nInstalling context menus..." -ForegroundColor Yellow
    & $installer
    Write-Host "Context menus installed" -ForegroundColor Green
} else {
    Write-Host "Installer not found: $installer" -ForegroundColor Red
}

# ── Autostart ─────────────────────────────────────────────────────────────
$autostart = $false
if (Test-Path $saitulsExe) {
    $resp = Read-Host "`nAdd SAITULS to Windows autostart? (y/N)"
    if ($resp -eq 'y' -or $resp -eq 'Y') {
        try {
            $runKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Software\Microsoft\Windows\CurrentVersion\Run", $true)
            $runKey.SetValue("SaitulsApp", "`"$saitulsExe`"")
            $runKey.Close()
            Write-Host "SAITULS added to autostart" -ForegroundColor Green
            $autostart = $true
        } catch { Write-Host "Autostart failed: $_" -ForegroundColor Red }
    }
}

# ── Done ──────────────────────────────────────────────────────────────────
Write-Host "`n=== SAITULS Setup complete ===" -ForegroundColor Cyan
Write-Host "Context menus: installed" -ForegroundColor Green
if ($hasPython) { Write-Host "Python:         OK" -ForegroundColor Green } else { Write-Host "Python:         MISSING — .PYW scripts won't work" -ForegroundColor Red }
if (Test-Path $saitulsExe) { Write-Host "SAITULS.exe:    compiled" -ForegroundColor Green } else { Write-Host "SAITULS.exe:    pre-built binary needed" -ForegroundColor Yellow }
if (Test-Path (Join-Path $root "Bin\FFMPEG.EXE")) { Write-Host "FFmpeg:         present" -ForegroundColor Green } else { Write-Host "FFmpeg:         missing — media tools disabled" -ForegroundColor Yellow }
if (Test-Path (Join-Path $root "Bin\yt-dlp.exe")) { Write-Host "yt-dlp:         present" -ForegroundColor Green } else { Write-Host "yt-dlp:         missing — YouTube tools disabled" -ForegroundColor Yellow }
if ($hasDotNet) { Write-Host ".NET:            OK" -ForegroundColor Green } else { Write-Host ".NET:            csc.exe not found — SAITULS.exe needs pre-built binary" -ForegroundColor Yellow }

Write-Host "`nLaunch with: SAITULS_LAUNCHER.cmd" -ForegroundColor White
Write-Host "Run:         python tests\test_regs.py (from repo root)" -ForegroundColor Gray