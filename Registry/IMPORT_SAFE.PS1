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
    # The token only ever appears inside quoted VALUE data, where a backslash is
    # an escape character: the root must go in doubled, exactly as
    # INSTALL_ALL.PS1 does it. Substituting a raw path produced value lines with
    # lone backslashes, which reg.exe SKIPS while still exiting 0 -- so every
    # import through this wrapper reported success and wrote no command value at
    # all, leaving whatever an earlier install had put there.
    $content = $content.Replace('%%ROOT%%', $Root.Replace('\', '\\'))
}

$tmp = Join-Path $env:TEMP ("reg_import_{0}.reg" -f [guid]::NewGuid().ToString("N"))
if ($utf16) {
    [System.IO.File]::WriteAllText($tmp, $content, [System.Text.Encoding]::Unicode)
} else {
    [System.IO.File]::WriteAllText($tmp, $content, [System.Text.Encoding]::Default)
}

# reg.exe's exit code is per FILE, not per line: a value line it cannot parse is
# SKIPPED while the import still reports success. So the exit code alone is not
# evidence -- read the intended values back and compare.
function Get-IntendedValues([string]$regText) {
    $pairs = @()
    $key = $null
    foreach ($line in ($regText -split "`r?`n")) {
        $t = $line.Trim()
        if ($t -match '^\[(-?)(.+)\]$') {
            # A [-HKEY...] line DELETES the key; nothing to read back for it.
            $key = if ($Matches[1] -eq '-') { $null } else { $Matches[2] }
            continue
        }
        if (-not $key) { continue }
        if ($t -match '^(@|"([^"]+)")=(".*")$') {
            $name = if ($Matches[1] -eq '@') { '(default)' } else { $Matches[2] }
            $raw  = $Matches[3]
            # .reg string data: strip the outer quotes, then unescape \\ and \"
            $data = $raw.Substring(1, $raw.Length - 2).Replace('\\', "`0").Replace('\"', '"').Replace("`0", '\')
            $pairs += [pscustomobject]@{ Key = $key; Name = $name; Data = $data }
        }
    }
    return $pairs
}

# T-133: this importer is the shared mutation sink for SAITULS.cs and the menu
# scripts, so it takes the one cross-process registry lock for the import itself.
$registryMutationMutex = New-Object Threading.Mutex($false, 'Global\SAITULS_REGISTRY_MUTATION')
$registryMutationOwned = $false
try {
    try { $registryMutationOwned = $registryMutationMutex.WaitOne(5000) }
    catch [Threading.AbandonedMutexException] { $registryMutationOwned = $true }
    if (-not $registryMutationOwned) { throw 'BUSY: SAITULS registry mutation is already in progress' }

    try {
        reg import $tmp
        if ($LASTEXITCODE -ne 0) {
            throw "reg import failed with exit code $LASTEXITCODE"
        }

        $missing = @()
        foreach ($p in (Get-IntendedValues $content)) {
            $actual = $null
            try {
                $item = Get-ItemProperty -LiteralPath ("Registry::" + $p.Key) -ErrorAction Stop
                $actual = $item.($p.Name)
            } catch { }
            if ($actual -ne $p.Data) {
                $missing += "$($p.Key) :: $($p.Name)"
            }
        }
        if ($missing.Count -gt 0) {
            throw ("reg import exited 0 but $($missing.Count) value(s) did not land: " +
                   (($missing | Select-Object -First 4) -join '; '))
        }

        Write-Host "Imported OK (root = $Root): $File" -ForegroundColor Green
    }
    finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
} finally {
    if ($registryMutationOwned) { $registryMutationMutex.ReleaseMutex() }
    $registryMutationMutex.Dispose()
}
