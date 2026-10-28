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
    # -Wait -PassThru -ErrorAction Stop and the child's own exit code: the
    # destructive registry work happens only in the elevated child, so without
    # them the parent reported success the moment that child STARTED and a
    # dismissed UAC prompt looked exactly like a completed removal.
    try {
        $elevated = Start-Process powershell.exe -ArgumentList $psArgs -Verb RunAs -Wait -PassThru -ErrorAction Stop
    } catch {
        Write-Host "Elevation was cancelled. Nothing was changed." -ForegroundColor Yellow
        exit 1
    }
    # A process object with no ExitCode is counted with a missing one: `exit
    # $null` is `exit 0`, which is the very "success" this guard exists to stop.
    if (-not $elevated -or $null -eq $elevated.ExitCode) {
        Write-Host "Elevation did not complete. Nothing was changed." -ForegroundColor Yellow
        exit 1
    }
    exit $elevated.ExitCode
}

$ErrorActionPreference = "Stop"
$classesRoot = "HKLM:\SOFTWARE\Classes"

$Keys = @("CodexHere")
if ($IncludeLegacy) {
    $Keys += @("OpenCodex", "CodexMain", "CodexAccount2", "CodexAccount3Free")
}

# T-133: this writer participates in the one cross-process registry mutation lock.
$registryMutationMutex = New-Object Threading.Mutex($false, 'Global\SAITULS_REGISTRY_MUTATION')
$registryMutationOwned = $false
try {
    try { $registryMutationOwned = $registryMutationMutex.WaitOne(5000) }
    catch [Threading.AbandonedMutexException] { $registryMutationOwned = $true }
    if (-not $registryMutationOwned) { Write-Host 'BUSY: SAITULS registry mutation is already in progress' -ForegroundColor Yellow; exit 3 }

    foreach ($root in @("Directory", "Directory\Background")) {
        foreach ($k in $Keys) {
            $p = "$classesRoot\$root\shell\$k"
            if (Test-Path -LiteralPath $p) {
                Remove-Item -Path $p -Recurse -Force
                Write-Host "REMOVED: HKLM\...\$root\shell\$k"
            }
        }
    }
} finally {
    if ($registryMutationOwned) { $registryMutationMutex.ReleaseMutex() }
    $registryMutationMutex.Dispose()
}

Write-Host ""
Write-Host "Done."
# Explicit success code from the elevated child: the parent above exits with
# whatever lands here, so the terminal verdict must be stated, not inferred.
exit 0
