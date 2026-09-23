param([string]$InstallRoot, [string]$Version)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$lib = Join-Path (Split-Path -Parent (Split-Path -Parent $here)) 'lib'
. (Join-Path $lib 'state.ps1')
. (Join-Path $lib 'contract.ps1')
$lock = Enter-PatchLock
try {
    Repair-PatchPublication
    $install = [pscustomobject]@{ found = $true; root = $InstallRoot; version = $Version; exe = Join-Path $InstallRoot 'bin/opencode.exe' }
    $report = Get-PatchStatus $install $here
    Write-Output "$($report.state): $($report.reason)"
    exit $(switch ($report.state) { 'AVAILABLE' { 0 } 'INSTALLED' { 3 } 'NEEDS_REAPPLY' { 4 } default { 5 } })
} finally { $lock.ReleaseMutex(); $lock.Dispose() }
