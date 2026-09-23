param([string]$InstallRoot, [string]$Version, [string]$BackupRoot, [string]$ExpectedB64)
# Compatibility entry point. The installed generation in state.json owns ALL
# restore targets; command-line hashes and the current manifest never do.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$lib = Join-Path (Split-Path -Parent (Split-Path -Parent $here)) 'lib'
. (Join-Path $lib 'state.ps1')
. (Join-Path $lib 'contract.ps1')
. (Join-Path $lib 'transaction.ps1')
$install = [pscustomobject]@{ found = $true; root = $InstallRoot; version = $Version; exe = Join-Path $InstallRoot 'bin/opencode.exe' }
$report = [pscustomobject]@{ state = ''; reason = $null; backup = $false }
$result = Invoke-PatchRestore $install $here $report
Write-Output "$($result.state): $($result.reason)"
exit $(if ($result.state -in @('AVAILABLE', 'NOT_INSTALLED')) { 0 } else { 4 })
