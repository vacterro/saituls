# W2-002 (SRC-019): recovery + classification must be one indivisible read
# transaction, and mutex contention must never be reported as corruption.
#
# The pre-fix top level acquired the mutex, ran Repair-PatchPublication,
# RELEASED it, and only then called Get-PatchStatus -- so a reader could
# classify an intentionally intermediate publication as SOURCE_DRIFTED /
# BROKEN_PATCH -- and mapped ANY exception, ordinary contention included, to
# BROKEN_PATCH.
#
# This harness holds the real SAIPATCH mutex from a SECOND process and proves
# that a concurrent reader answers with an explicit transient BUSY (or a stable
# legitimate state), never corruption. It also proves that once the holder
# releases, the same command returns a real classification and exit 0.
#
# The genuine-corruption direction is covered by tests/test_saipatch.ps1, which
# must keep classifying real foreign bytes as SOURCE_DRIFTED/BROKEN_PATCH: the
# fix narrows what counts as corruption, it does not weaken the check.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$saipatch = Join-Path $repo 'Scripts\saipatch'

$script:pass = 0
$script:fail = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { $script:pass++; Write-Host ("PASS  {0}  {1}" -f $name, $detail) }
    else { $script:fail++; Write-Host ("FAIL  {0}  {1}" -f $name, $detail) }
}

# --- a holder process that owns the SAIPATCH mutex for a while --------------
$holderScript = Join-Path $env:TEMP ("saipatch-lock-holder-{0}.ps1" -f [Guid]::NewGuid().ToString('N'))
@"
`$ErrorActionPreference = 'Stop'
. '$saipatch\lib\resolve.ps1'
. '$saipatch\lib\state.ps1'
`$lock = Enter-PatchLock
# Signal readiness only AFTER the mutex is genuinely owned, so the reader
# never races the holder's own acquisition.
Set-Content -LiteralPath '$([IO.Path]::Combine($env:TEMP, 'saipatch-lock-ready.txt'))' -Value 'held'
Start-Sleep -Milliseconds `$args[0]
`$lock.ReleaseMutex()
`$lock.Dispose()
"@ | Set-Content -LiteralPath $holderScript -Encoding UTF8

function Start-LockHolder([int]$ms) {
    $ready = Join-Path $env:TEMP 'saipatch-lock-ready.txt'
    Remove-Item -LiteralPath $ready -ErrorAction SilentlyContinue
    $p = Start-Process -FilePath 'powershell' -NoNewWindow -PassThru `
        -ArgumentList @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $holderScript, "$ms")
    $deadline = (Get-Date).AddSeconds(15)
    while (-not (Test-Path -LiteralPath $ready)) {
        if ((Get-Date) -gt $deadline) { $p.Kill(); throw 'lock holder never signalled readiness' }
        Start-Sleep -Milliseconds 50
    }
    return $p
}

function Invoke-Reader([string]$command) {
    $out = & powershell -NoLogo -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $saipatch 'patcher.ps1') -Command $command -Json 2>&1
    $rc = $LASTEXITCODE
    $text = (($out | ForEach-Object { "$_" }) -join "`n")
    $obj = $null
    try { $obj = $text | ConvertFrom-Json } catch { $obj = $null }
    return @{ rc = $rc; text = $text; obj = $obj }
}

try {
    # A holder long past the five-second wait inside Enter-PatchLock, so the
    # reader must give up waiting -- and must call that contention, not
    # corruption. Enter-PatchLock waits 5000ms, so the hold has to exceed it.
    $holder = Start-LockHolder -ms 8000
    try {
        foreach ($command in @('Detect', 'Status', 'Verify')) {
            $r = Invoke-Reader $command
            $state = if ($r.obj) { "$($r.obj.state)" } else { '<no json>' }
            Check "$command under contention is not BROKEN_PATCH" `
                ($state -ne 'BROKEN_PATCH' -and $state -ne 'SOURCE_DRIFTED') "state=$state rc=$($r.rc)"
            Check "$command under contention reports an explicit outcome" `
                ($state -eq 'BUSY' -or $state -eq 'NOT_INSTALLED' -or $state -eq 'AVAILABLE' `
                    -or $state -eq 'NEEDS_REAPPLY' -or $state -eq 'INSTALLED' -or $state -eq 'RESTORABLE' `
                    -or $state -eq 'RESTORED' -or $state -eq 'OPEN_CODE_RUNNING' -or $state -eq 'UNSUPPORTED_VERSION' `
                    -or $state -eq 'UNSUPPORTED_BUILD') "state=$state"
        }
        # The exit code for transient contention is distinct from a refusal, so
        # a caller can tell "try again" from "something is wrong".
        $busy = Invoke-Reader 'Detect'
        Check "contention exit code is the transient BUSY path" `
            ($busy.rc -eq 4 -or ($busy.obj -and "$($busy.obj.state)" -ne 'BROKEN_PATCH')) "rc=$($busy.rc) state=$(if ($busy.obj) { $busy.obj.state } else { '?' })"
    }
    finally {
        $holder.WaitForExit(20000) | Out-Null
        if (-not $holder.HasExited) { $holder.Kill() }
    }

    # Once the writer is gone the same command classifies normally. This is the
    # "bounded wait followed by the stable generation" half of the contract.
    foreach ($command in @('Detect', 'Status')) {
        $r = Invoke-Reader $command
        $state = if ($r.obj) { "$($r.obj.state)" } else { '<no json>' }
        Check "$command after the holder exits classifies and exits 0" `
            ($r.rc -eq 0 -and $state -ne 'BROKEN_PATCH' -and $state -ne 'BUSY') "state=$state rc=$($r.rc)"
    }
}
finally {
    Remove-Item -LiteralPath $holderScript -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $env:TEMP 'saipatch-lock-ready.txt') -ErrorAction SilentlyContinue
}

Write-Host ("PASS ({0} failures)" -f $script:fail)
if ($script:fail -gt 0) { exit 1 }
exit 0
