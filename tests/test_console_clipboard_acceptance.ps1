# T-301: real Windows console clipboard acceptance harness.
# Compiles the observable acceptance suite (real SendInput/mouse delivery to
# owned disposable consoles), runs the positive pass, then proves the oracle
# DETECTS every seeded fault. A missing observable is a FAIL, never a SKIP.
#
# The positive verdict and the negative-control verdict are two separate
# facts. A green positive pass never hides a blocked mandatory negative
# control, an incomplete fault matrix, or a clipboard that could not be
# restored: the aggregate exit code says so.
param(
    [string]$EnginePath = $env:SAI_ACCOUNTS_ENGINE,
    [string]$InstalledEnginePath,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$engine = if ($EnginePath) { $EnginePath } else { Join-Path $root 'Scripts\accounts\bin\sai-accounts.exe' }

# Every seeded fault the oracle must detect. The matrix is complete or the
# acceptance is not a product-level verdict.
$requiredFaults = @(
    'COPY_DISPATCH_SUPPRESSED',
    'PASTE_DISPATCH_SUPPRESSED',
    'CLIPBOARD_READ_RETURNS_STALE_DATA',
    'PASTE_PAYLOAD_TRUNCATED',
    'PASTE_PAYLOAD_DUPLICATED',
    'WRONG_FOREGROUND_TARGET',
    'CONTROLLER_DISABLED',
    'SENDINPUT_DELIVERY_REFUSED',
    'CLIPBOARD_RESTORATION_FAILURE'
)

# Runner exit code -> fault verdict. Anything else is a harness error.
function Get-FaultVerdict([int]$code) {
    switch ($code) {
        0 { return 'DETECTED' }
        1 { return 'NOT_DETECTED' }
        2 { return 'BLOCKED' }
        3 { return 'HARNESS_ERROR' }
        4 { return 'RESTORATION_FAILURE' }
        default { return 'HARNESS_ERROR' }
    }
}

# The aggregate verdict. One rule set, used by the run and by the self test.
function Get-AggregateVerdict {
    param(
        [string]$PositiveVerdict,
        [string[]]$RequiredFaults,
        [hashtable]$FaultResults
    )
    $reasons = New-Object System.Collections.Generic.List[string]
    $exit = 0
    $status = 'PASS'

    $missing = @($RequiredFaults | Where-Object { -not $FaultResults.ContainsKey($_) })
    $restorationFailures = @($RequiredFaults | Where-Object {
        $FaultResults.ContainsKey($_) -and $FaultResults[$_] -eq 'RESTORATION_FAILURE' })
    $harnessErrors = @($RequiredFaults | Where-Object {
        $FaultResults.ContainsKey($_) -and $FaultResults[$_] -eq 'HARNESS_ERROR' })
    $notDetected = @($RequiredFaults | Where-Object {
        $FaultResults.ContainsKey($_) -and $FaultResults[$_] -eq 'NOT_DETECTED' })
    $blocked = @($RequiredFaults | Where-Object {
        $FaultResults.ContainsKey($_) -and $FaultResults[$_] -eq 'BLOCKED' })

    if ($PositiveVerdict -eq 'HARNESS_ERROR') {
        $exit = 3; $status = 'HARNESS_ERROR'
        $reasons.Add('positive pass returned a harness error')
    }
    if ($missing.Count -gt 0) {
        $exit = 3; $status = 'HARNESS_ERROR'
        $reasons.Add("fault matrix incomplete: $($missing -join ', ')")
    }
    if ($restorationFailures.Count -gt 0) {
        $exit = 3; $status = 'INCOMPLETE'
        $reasons.Add("clipboard restoration failed: $($restorationFailures -join ', ')")
    }
    if ($harnessErrors.Count -gt 0) {
        $exit = 3; $status = 'HARNESS_ERROR'
        $reasons.Add("negative control harness errors: $($harnessErrors -join ', ')")
    }
    if ($notDetected.Count -gt 0) {
        $exit = 1; $status = 'FAIL'
        $reasons.Add("seeded faults not detected: $($notDetected -join ', ')")
    }
    if ($PositiveVerdict -eq 'FAIL') {
        $exit = 1; $status = 'FAIL'
        $reasons.Add('positive pass failed')
    }
    if ($blocked.Count -gt 0) {
        if ($exit -eq 0) { $exit = 2; $status = 'INCOMPLETE' }
        $reasons.Add("mandatory negative control blocked: $($blocked -join ', ')")
    }
    if ($PositiveVerdict -eq 'BLOCKED') {
        if ($exit -eq 0) { $exit = 2; $status = 'INCOMPLETE' }
        $reasons.Add('positive pass blocked')
    }
    if ($PositiveVerdict -eq 'INCOMPLETE') {
        if ($exit -eq 0) { $exit = 2; $status = 'INCOMPLETE' }
        $reasons.Add('positive pass incomplete')
    }
    return [pscustomobject]@{
        ExitCode = $exit
        Status   = $status
        Reasons  = $reasons.ToArray()
    }
}

if ($SelfTest) {
    # The aggregate must never turn a blocked mandatory negative control, an
    # incomplete matrix, or a failed clipboard restoration into exit 0.
    $all = @{ 'F1' = 'DETECTED'; 'F2' = 'DETECTED' }
    $cases = @(
        @{ name = 'all detected';            positive = 'PASS';          faults = $all.Clone();                                 want = 0 },
        @{ name = 'one blocked';             positive = 'PASS';          faults = @{ 'F1' = 'DETECTED'; 'F2' = 'BLOCKED' };      want = 2 },
        @{ name = 'one not detected';        positive = 'PASS';          faults = @{ 'F1' = 'DETECTED'; 'F2' = 'NOT_DETECTED' };  want = 1 },
        @{ name = 'one harness error';       positive = 'PASS';          faults = @{ 'F1' = 'DETECTED'; 'F2' = 'HARNESS_ERROR' }; want = 3 },
        @{ name = 'restoration failure';     positive = 'PASS';          faults = @{ 'F1' = 'DETECTED'; 'F2' = 'RESTORATION_FAILURE' }; want = 3 },
        @{ name = 'matrix incomplete';       positive = 'PASS';          faults = @{ 'F1' = 'DETECTED' };                        want = 3 },
        @{ name = 'positive fail';           positive = 'FAIL';          faults = $all.Clone();                                 want = 1 },
        @{ name = 'positive blocked';        positive = 'BLOCKED';       faults = $all.Clone();                                 want = 2 },
        @{ name = 'positive incomplete';     positive = 'INCOMPLETE';    faults = $all.Clone();                                 want = 2 },
        @{ name = 'positive harness error';  positive = 'HARNESS_ERROR'; faults = $all.Clone();                                 want = 3 }
    )
    $failed = @()
    foreach ($case in $cases) {
        $verdict = Get-AggregateVerdict -PositiveVerdict $case.positive -RequiredFaults @('F1', 'F2') -FaultResults $case.faults
        if ($verdict.ExitCode -ne $case.want) {
            $failed += "$($case.name): want exit $($case.want), got $($verdict.ExitCode) ($($verdict.Status))"
        }
    }
    if ($failed.Count -gt 0) {
        $failed | ForEach-Object { Write-Host "SELFTEST FAIL: $_" }
        Write-Host 'SELFTEST=FAIL'
        exit 1
    }
    Write-Host 'SELFTEST=PASS: 10 aggregate rules verified (blocked negative control can never yield exit 0)'
    exit 0
}

if (-not (Test-Path $engine)) {
    Write-Host "Building sai-accounts.exe..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'Scripts\accounts\build.ps1') | Out-Null
    if (-not (Test-Path $engine)) { Write-Host 'FAIL: engine build produced no binary'; exit 1 }
}

$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe" }
if (-not (Test-Path $csc)) { Write-Host 'FAIL: csc.exe not found: compiler missing means FAIL, never SKIP'; exit 1 }

$binDir = Join-Path $root 'tests\bin'
if (-not (Test-Path $binDir)) { New-Item -ItemType Directory -Force -Path $binDir | Out-Null }
$runnerExe = Join-Path $binDir 'console-clipboard-acceptance.exe'
$runnerSrc = Join-Path $root 'tests\console_clipboard_acceptance_test.cs'

Write-Host 'Compiling console-clipboard-acceptance.exe...'
& $csc -nologo -out:$runnerExe -r:System.dll -r:System.Windows.Forms.dll -r:System.Drawing.dll $runnerSrc
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL: acceptance suite compilation failed'; exit 3 }

Write-Host 'Running acceptance suite (positive pass)...'
$positiveOutput = & $runnerExe $engine
$positive = $LASTEXITCODE
$positiveOutput | ForEach-Object { Write-Host "  $_" }
Write-Host "positive pass exit=$positive"
switch ($positive) {
    0 { $positiveVerdict = 'PASS' }
    1 { $positiveVerdict = 'FAIL' }
    2 { $positiveVerdict = 'BLOCKED' }
    default { $positiveVerdict = 'HARNESS_ERROR' }
}
if ($positiveVerdict -eq 'HARNESS_ERROR') {
    Write-Host "HARNESS ERROR: positive pass returned unexpected exit code $positive"
}

# Negative controls: each runs the same oracle three times -- a passing
# baseline, a deterministic injected fault the oracle must reject, then the
# baseline again. Normal behavior (empty clipboard, Ctrl+C without selection)
# is not a fault and is not counted here.
$faultResults = @{}
$faultDetail = @{}
foreach ($fault in $requiredFaults) {
    Write-Host "Running negative control: $fault..."
    $out = & $runnerExe --negative $fault $engine
    $code = $LASTEXITCODE
    $out | ForEach-Object { Write-Host "  $_" }
    $verdict = Get-FaultVerdict $code
    if ($code -notin 0, 1, 2, 3, 4) {
        Write-Host "HARNESS ERROR: $fault returned unexpected exit code $code"
    }
    $faultResults[$fault] = $verdict
    $restoration = ($out | Where-Object { $_ -match 'CLIPBOARD_RESTORATION=' }) -join ''
    $safety = ($out | Where-Object { $_ -match 'CLIPBOARD_SAFETY=' }) -join ''
    $faultDetail[$fault] = "verdict=$verdict exit=$code $restoration $safety"
    Write-Host "  NEGATIVE-FAULT $fault : $verdict (exit=$code) $restoration $safety"

    # A clipboard that could not be restored ends the destructive run: the
    # operator's clipboard state is no longer provable, so no further
    # clipboard-touching control may run.
    if ($verdict -eq 'RESTORATION_FAILURE') {
        Write-Host "FAIL: clipboard restoration failed on $fault; stopping the destructive negative controls"
        break
    }
}

$matrix = @()
foreach ($fault in $requiredFaults) {
    if ($faultResults.ContainsKey($fault)) { $matrix += "$fault=$($faultResults[$fault])" }
    else { $matrix += "$fault=MISSING" }
}
Write-Host ("NEGATIVE_MATRIX=" + ($matrix -join ','))

$verdict = Get-AggregateVerdict -PositiveVerdict $positiveVerdict -RequiredFaults $requiredFaults -FaultResults $faultResults
foreach ($reason in $verdict.Reasons) { Write-Host "  REASON: $reason" }
Write-Host "POSITIVE_VERDICT=$positiveVerdict"
Write-Host "AGGREGATE=$($verdict.Status)"
Write-Host "AGGREGATE_EXIT=$($verdict.ExitCode)"

# Behavioral parity against the installed executable: the same acceptance, run
# against the image the operator actually launches. The identity check inside
# the suite reports the PE verdict; this pass reports the behavior.
if ($InstalledEnginePath) {
    if (-not (Test-Path $InstalledEnginePath)) {
        Write-Host "installed image absent at $InstalledEnginePath"
        Write-Host 'INSTALLED_BEHAVIOR=BLOCKED'
        if ($verdict.ExitCode -eq 0) { $verdict.ExitCode = 2; $verdict.Status = 'INCOMPLETE' }
    }
    else {
        Write-Host "Running acceptance suite against the installed image: $InstalledEnginePath"
        $installedOutput = & $runnerExe $InstalledEnginePath
        $installedCode = $LASTEXITCODE
        $installedOutput | ForEach-Object { Write-Host "  $_" }
        Write-Host "installed pass exit=$installedCode"
        switch ($installedCode) {
            0 { $installedVerdict = 'PASS' }
            1 { $installedVerdict = 'FAIL' }
            2 { $installedVerdict = 'BLOCKED' }
            default { $installedVerdict = 'HARNESS_ERROR' }
        }
        Write-Host "INSTALLED_BEHAVIOR=$installedVerdict"
        if ($installedVerdict -ne 'PASS' -and $verdict.ExitCode -eq 0) {
            $verdict.ExitCode = if ($installedVerdict -eq 'FAIL') { 1 } else { 2 }
            $verdict.Status = if ($installedVerdict -eq 'FAIL') { 'FAIL' } else { 'INCOMPLETE' }
            $verdict.Reasons = $verdict.Reasons + "installed executable behavior: $installedVerdict"
        }
    }
}
Write-Host "AGGREGATE=$($verdict.Status)"
Write-Host "AGGREGATE_EXIT=$($verdict.ExitCode)"
exit $verdict.ExitCode
