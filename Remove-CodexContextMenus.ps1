# Removes the cascaded "Открыть Codex здесь" context menu entry.
# Use -IncludeLegacy to also delete the old flat entries:
#   - "OpenCodex" ("Открыть ChatGPT Codex OpenAI в этой папке")

param(
    [switch]$IncludeLegacy
)

# Self-elevate: HKLM writes silently fail without admin rights.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $psArgs = @('-ExecutionPolicy','Bypass','-NoProfile','-File',"`"$($MyInvocation.MyCommand.Definition)`"")
    if ($IncludeLegacy) { $psArgs += '-IncludeLegacy' }
    Start-Process powershell.exe -ArgumentList $psArgs -Verb RunAs
    exit
}

$ErrorActionPreference = "Stop"

$Keys = @("CodexHere", "ClineHere")
if ($IncludeLegacy) {
    $Keys += @("OpenCodex", "CodexMain", "CodexAccount2", "CodexAccount3Free")
}

foreach ($root in @("Directory", "Directory\Background")) {
    foreach ($k in $Keys) {
        $p = "HKLM:\SOFTWARE\Classes\$root\shell\$k"
        if (Test-Path -LiteralPath $p) {
            Remove-Item -Path $p -Recurse -Force
            Write-Host "REMOVED: HKLM\...\$root\shell\$k"
        }
    }
}

Write-Host ""
Write-Host "Done."
