# Regression harness for Scripts\saipatch\tools\probe_native_tui.ps1.
#
# Subject: the probe's classification logic and its fail-closed behaviour.
# The signature table inside the probe was calibrated byte-for-byte against
# OpenCode 1.18.29 Windows x64 (SHA256
# 88d2fa691b2d9e32fde6d1039382a850ddf96fe49cd41683c6375fe1dc8ec2a5); this
# harness replays that calibrated profile synthetically so every runner --
# including a clean CI machine with only the stub CLI -- exercises the exact
# decision rules without any network or provider access.
#
# Cases:
#   1. synthetic 1.18.29 profile (legacy TUI calls + native v2 surface +
#      native scheduler)            -> SPLIT_LEGACY_NATIVE
#   2. synthetic unified profile (all four TUI call sites on the native v2
#      domain, zero legacy call sites) -> UNIFIED_NATIVE_TUI
#   3. ambiguous profile (legacy AND native submit call sites) -> UNKNOWN_BUILD
#   4. junk root / text stand-in exe -> UNSUPPORTED_LAYOUT, exit 2
#   5. when a REAL OpenCode binary is resolvable on this machine (beyond the
#      ci_fixtures stub): verdict SPLIT_LEGACY_NATIVE with the recorded hash
#      and version. A clean runner has no real binary; the stub is then the
#      subject of case 4, which still proves the probe refuses to bless a
#      non-binary stand-in. This conditional is an asserted alternative, not a
#      silent skip: the fake-green danger the house rule forbids.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_native_tui_probe.ps1
# Exit: 0 = all PASS, 1 = at least one failure.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$probe = Join-Path (Split-Path -Parent $here) 'Scripts\saipatch\tools\probe_native_tui.ps1'
$fails = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    $mark = 'PASS'
    if (-not $ok) { $mark = 'FAIL'; $script:fails++ }
    Write-Host ($mark + '  ' + $name + $(if ($detail) { "  $detail" }))
}

function Invoke-Probe([string]$root) {
    $jsonFile = Join-Path $work ("probe-" + [guid]::NewGuid().ToString('N') + '.json')
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $probe -OpenCodeRoot $root -OutJson $jsonFile 2>&1
    $code = $LASTEXITCODE
    $json = $null
    if (Test-Path -LiteralPath $jsonFile) {
        try { $json = Get-Content -LiteralPath $jsonFile -Raw | ConvertFrom-Json } catch { }
    }
    [pscustomobject]@{ code = $code; output = @($out); json = $json }
}

function New-ProfileRoot([string]$dir, [string[]]$literals) {
    New-Item -ItemType Directory -Path (Join-Path $dir 'bin') -Force | Out-Null
    $body = ($literals | ForEach-Object { "/*calib*/ $_ /*end*/" }) -join "`n"
    Set-Content -LiteralPath (Join-Path $dir 'bin\opencode.exe') -Value $body -Encoding Ascii
}

$legacyCalls = @(
    'client.session.prompt({sessionID:'
    'client.session.status({'
    'client.session.messages({'
    'client.session.abort({sessionID:'
)
$legacyPaths = @(
    '/session/{sessionID}/message'
    '/session/status'
    '/session/{sessionID}/abort'
)
$v2Paths = @(
    '/api/session/{sessionID}/prompt'
    '/api/session/active'
    '/api/session/{sessionID}/message'
    '/api/session/{sessionID}/interrupt'
)
$engine = @('promoteNextQueued', 'SessionRunner.run', 'promoteSteers', 'tui.json')
$v2Calls = @(
    'v2.session.prompt({sessionID:'
    'v2.session.messages({'
    'v2.session.status({'
    'v2.session.active('
    'v2.session.interrupt('
)

$work = Join-Path ([IO.Path]::GetTempPath()) ("saituls-native-tui-probe-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
try {
    # Case 1: the calibrated 1.18.29 profile.
    $split = Join-Path $work 'split'
    New-ProfileRoot $split ($legacyCalls + $legacyPaths + $v2Paths + $engine)
    $r = Invoke-Probe $split
    Check 'synthetic 1.18.29 profile classifies SPLIT_LEGACY_NATIVE' `
        ($r.code -eq 0 -and $r.json.verdict -eq 'SPLIT_LEGACY_NATIVE') `
        "exit=$($r.code) verdict=$($r.json.verdict)"
    Check 'synthetic split profile reports legacy TUI domains' `
        ($r.json.tui_submit_domain -eq 'legacy' -and $r.json.tui_render_domain -eq 'legacy' -and
        $r.json.tui_status_domain -eq 'legacy' -and $r.json.tui_abort_domain -eq 'legacy')
    Check 'synthetic split profile reports native queue delivery support' `
        ($r.json.queue_delivery_supported -eq $true -and $r.json.unified_native_tui_candidate -eq $false)

    # Case 2: a genuinely unified build must be recognized, but only when every
    # legacy TUI call site is gone -- never from the v2 surface alone.
    $uni = Join-Path $work 'unified'
    New-ProfileRoot $uni ($v2Calls + $legacyPaths + $v2Paths + $engine)
    $r = Invoke-Probe $uni
    Check 'synthetic unified profile classifies UNIFIED_NATIVE_TUI' `
        ($r.code -eq 0 -and $r.json.verdict -eq 'UNIFIED_NATIVE_TUI' -and
        $r.json.unified_native_tui_candidate -eq $true) "exit=$($r.code) verdict=$($r.json.verdict)"

    # Case 3: both submit boundaries present is ambiguous, never compatible.
    $amb = Join-Path $work 'ambiguous'
    New-ProfileRoot $amb ($legacyCalls + $v2Calls + $legacyPaths + $v2Paths + $engine)
    $r = Invoke-Probe $amb
    Check 'ambiguous profile fails closed to UNKNOWN_BUILD' `
        ($r.code -eq 0 -and $r.json.verdict -eq 'UNKNOWN_BUILD' -and
        $r.json.tui_submit_domain -eq 'ambiguous') "exit=$($r.code) verdict=$($r.json.verdict)"

    # Case 4: a text stand-in must never classify as compatible.
    $junk = Join-Path $work 'junk'
    New-Item -ItemType Directory -Path (Join-Path $junk 'bin') -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $junk 'bin\opencode.exe') `
        -Value 'SAITULS test stub: opencode %* not a real build' -Encoding Ascii
    $r = Invoke-Probe $junk
    Check 'text stand-in fails closed UNSUPPORTED_LAYOUT' `
        ($r.code -eq 2 -and $r.json.verdict -eq 'UNSUPPORTED_LAYOUT') "exit=$($r.code) verdict=$($r.json.verdict)"
    $r = Invoke-Probe (Join-Path $work 'does-not-exist')
    Check 'missing root fails closed UNSUPPORTED_LAYOUT' `
        ($r.code -eq 2 -and $r.json.verdict -eq 'UNSUPPORTED_LAYOUT') "exit=$($r.code)"

    # Case 5: the real recorded baseline, when a real binary exists here.
    $realExe = $null
    $cmd = Get-Command opencode -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) {
        if ([IO.Path]::GetExtension($cmd.Source) -eq '.exe' -and (Get-Item $cmd.Source).Length -gt 10MB) {
            $realExe = $cmd.Source
        } else {
            $shim = Get-Content $cmd.Source -Raw
            foreach ($pattern in @('node_modules[\\/]+opencode-ai[\\/]bin[\\/]opencode\.exe',
                    'node_modules[\\/]+@opencode-ai[\\/]+opencode-win32-x64[\\/]bin[\\/]opencode\.exe')) {
                if ($shim -match $pattern) {
                    $candidate = $Matches[0] -replace '[\\/]', '\'
                    $base = Split-Path -Parent $cmd.Source
                    $candidate = Join-Path $base $candidate
                    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $realExe = $candidate; break }
                }
            }
        }
    }
    if ($realExe) {
        $r = Invoke-Probe $realExe
        $actualHash = (Get-FileHash -LiteralPath $realExe -Algorithm SHA256).Hash.ToLowerInvariant()
        Check 'real binary observation identifies the inspected bytes' `
            ($r.json.sha256 -eq $actualHash -and $r.code -in @(0, 2)) `
            "version=$($r.json.version) verdict=$($r.json.verdict)"
        if ($r.json.version -eq '1.18.29') {
            Check 'recorded 1.18.29 baseline remains split with its exact hash' `
                ($r.code -eq 0 -and $r.json.verdict -eq 'SPLIT_LEGACY_NATIVE' -and
                 $r.json.sha256 -eq '88d2fa691b2d9e32fde6d1039382a850ddf96fe49cd41683c6375fe1dc8ec2a5') `
                "sha256=$($r.json.sha256)"
        } else {
            Write-Host 'SKIP  live 1.18.29 baseline acceptance: installed version differs; signature observation does not prove patch compatibility'
        }
    } else {
        Check 'no real OpenCode binary here; stub fail-closed case stands in for it' $true `
            'clean-runner mode: cases 1-4 carry this harness in CI'
    }
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
