<#
.SYNOPSIS
    PowerShell wrapper for Claude profile SAIPEN hook reconciliation.
#>
[CmdletBinding()]
param(
    [string]$ProfileDir,
    [switch]$CheckOnly,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$PyScript = Join-Path $ScriptDir 'claude_hooks.py'

function Invoke-ClaudeHooksReconcile([string]$Dir, [switch]$Check, [switch]$Drop) {
    if (-not $Dir) { return $null }
    $py = Get-Command 'python.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $py) {
        Write-Warning "python.exe not found on PATH; skipping Claude hooks reconciliation for $Dir"
        return $null
    }
    $argsList = @($PyScript, '--profile-dir', $Dir)
    if ($Check) { $argsList += '--check' }
    if ($Drop) { $argsList += '--remove' }
    
    $proc = Start-Process -FilePath $py.Source -ArgumentList $argsList -NoNewWindow -PassThru -RedirectStandardOutput ($outFile = [System.IO.Path]::GetTempFileName()) -RedirectStandardError ($errFile = [System.IO.Path]::GetTempFileName())
    try {
        $proc.WaitForExit(15000) | Out-Null
        $rawOut = [System.IO.File]::ReadAllText($outFile, [System.Text.Encoding]::UTF8)
        if ($rawOut) {
            try { return ($rawOut | ConvertFrom-Json) } catch { }
        }
        return $null
    } finally {
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

if ($ProfileDir) {
    return Invoke-ClaudeHooksReconcile -Dir $ProfileDir -Check:$CheckOnly -Drop:$Remove
}
