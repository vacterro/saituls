#Requires -RunAsAdministrator
#  Safe per-file .REG importer for this toolkit.
#
#  The Registry\*.REG files are %%ROOT%%-tokenized (T-034): paths are stored
#  as %%ROOT%%\Bin\... etc. A DIRECT `reg import` writes those literals into
#  the registry and produces BROKEN menu commands (T-037).
#
#  This wrapper substitutes %%ROOT%% with the toolkit root, writes a temp
#  copy and imports that. Usage:
#
#    powershell -ExecutionPolicy Bypass -File IMPORT_SAFE.PS1 -File .\FFMPEG_MENU.REG
#    powershell -ExecutionPolicy Bypass -File IMPORT_SAFE.PS1 -File .\DL_YT.REG -Root "D:\somewhere\__SAITULS"
#
#  Removal files (*_REM.REG) contain no %%ROOT%% tokens, but running them
#  through this wrapper is harmless and keeps the habit uniform.

param(
    [Parameter(Mandatory = $true)][string]$File,
    [string]$Root
)

$ErrorActionPreference = "Stop"

if (-not $Root) {
    $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
$Root = (Resolve-Path -LiteralPath $Root).ProviderPath.TrimEnd('\')

if (-not (Test-Path -LiteralPath $File)) {
    throw "File not found: $File"
}
$File = (Resolve-Path -LiteralPath $File).ProviderPath

# Detect encoding by BOM: UTF-16 LE (+BOM) regs with Cyrillic, ANSI/ASCII otherwise.
$bytes = [System.IO.File]::ReadAllBytes($File)
if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
    $content = [System.Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2)
    $utf16 = $true
} elseif ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    # UTF-8 with BOM - rare but possible
    $content = [System.Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3)
    $utf16 = $false
} else {
    # No BOM: try UTF-8 first (most common in this repo), fall back to ANSI.
    $try = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($try.Contains('Windows Registry Editor')) {
        $content = $try
    } else {
        $content = [System.Text.Encoding]::Default.GetString($bytes)
    }
    $utf16 = $false
}

if ($content -notmatch '%%ROOT%%') {
    Write-Host "No %%ROOT%% tokens found - importing as-is." -ForegroundColor DarkGray
} else {
    $content = $content.Replace('%%ROOT%%', $Root)
}

$tmp = Join-Path $env:TEMP ("reg_import_{0}.reg" -f [guid]::NewGuid().ToString("N"))
if ($utf16) {
    [System.IO.File]::WriteAllText($tmp, $content, [System.Text.Encoding]::Unicode)
} else {
    [System.IO.File]::WriteAllText($tmp, $content, [System.Text.Encoding]::Default)
}

try {
    reg import $tmp
    if ($LASTEXITCODE -ne 0) {
        throw "reg import failed with exit code $LASTEXITCODE"
    }
    Write-Host "Imported OK (root = $Root): $File" -ForegroundColor Green
}
finally {
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}
