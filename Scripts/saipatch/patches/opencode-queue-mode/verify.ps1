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
    Write-Output "SAIPATCH native queue: $($report.patch_version)"
    Write-Output ("Runtime restart required: {0}" -f $(if ($report.runtime_restart_required) { 'YES' } else { 'NO' }))
    # Restart-required is a truthful actionable answer (disk proof held, memory
    # predates it), not a verification failure.
    $ok = @('INSTALLED', 'INSTALLED_RESTART_REQUIRED', 'RESTORED', 'RESTORED_RESTART_REQUIRED')
    exit $(if ($report.state -in $ok) { 0 } else { 3 })
} finally { $lock.ReleaseMutex(); $lock.Dispose() }
