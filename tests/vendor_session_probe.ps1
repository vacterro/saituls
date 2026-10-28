# T-301 item 5: real vendor (Antigravity) session acceptance probe.
#
# Purpose: decide whether a REAL, test-owned, operator-equivalent vendor
# session can be driven for clipboard acceptance. It never fabricates a
# verdict: it either observes an authorized interactive session, or it
# reports VENDOR_ACCEPTANCE=BLOCKED with the observed reason.
#
# Hard rules this probe implements:
#   * It launches its OWN instance in a disposable isolated profile. It never
#     attaches to, reuses or terminates an operator session (AG1/AG2/AG3).
#   * It never reads, decrypts or copies the account vault, never broadens an
#     ACL, never disables sandbox isolation, never enables the legacy global
#     input bridge.
#   * If the isolated launch still authenticates with a stored credential,
#     that session is NOT test-owned. Using it for acceptance would reuse a
#     private provider credential without authorization, so the probe reports
#     BLOCKED and never touches that session.
#   * The only process it ever stops is the PID it started itself.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\vendor_session_probe.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\vendor_session_probe.ps1 `
#       -AgyPath "C:\ProgramData\SAI\Accounts\bin\agy.exe" -TimeoutSeconds 90
param(
    [string]$AgyPath = "$env:ProgramData\SAI\Accounts\bin\agy.exe",
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'

function Write-Verdict($key, $value) { Write-Host ("$key=$value") }

if (-not (Test-Path $AgyPath)) {
    Write-Verdict 'VENDOR_ACCEPTANCE' 'BLOCKED'
    Write-Verdict 'VENDOR_REASON' "vendor executable not found at $AgyPath"
    exit 2
}

# Disposable, test-owned profile: the vendor must not find an operator
# credential here. Everything is under a unique temp directory.
$nonce = [guid]::NewGuid().ToString('N').Substring(0, 8)
$profileRoot = Join-Path $env:TEMP ("t301-vendor-" + $nonce)
$homeDir = Join-Path $profileRoot 'home'
$appdata = Join-Path $profileRoot 'appdata'
$localappdata = Join-Path $profileRoot 'local'
foreach ($d in @($profileRoot, $homeDir, $appdata, $localappdata)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
$stdout = Join-Path $profileRoot 'vendor.out.txt'
$stderr = Join-Path $profileRoot 'vendor.err.txt'

$saved = @{}
foreach ($name in @('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH')) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name)
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}
[Environment]::SetEnvironmentVariable('HOME', $homeDir, 'Process')
[Environment]::SetEnvironmentVariable('USERPROFILE', $homeDir, 'Process')
[Environment]::SetEnvironmentVariable('APPDATA', $appdata, 'Process')
[Environment]::SetEnvironmentVariable('LOCALAPPDATA', $localappdata, 'Process')
[Environment]::SetEnvironmentVariable('HOMEDRIVE', 'C:', 'Process')
[Environment]::SetEnvironmentVariable('HOMEPATH', '\Users\probe', 'Process')

$proc = $null
$outcome = 'UNKNOWN'
$detail = ''
try {
    Write-Host "Launching isolated vendor session: $AgyPath"
    Write-Host "Isolated profile root: $profileRoot"
    $proc = Start-Process -FilePath $AgyPath -WorkingDirectory $profileRoot `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while (-not $proc.HasExited -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    $exited = $proc.HasExited
    $exitCode = if ($exited) { $proc.ExitCode } else { $null }
    $alive = -not $exited

    $out = ''
    foreach ($f in @($stdout, $stderr)) {
        if (Test-Path $f) { $out += (Get-Content -Raw -Path $f -ErrorAction SilentlyContinue) }
    }

    $quota = $out -match 'RESOURCE_EXHAUSTED' -or $out -match 'quota' -or $out -match '(?i)429'
    $authPrompt = $out -match '(?i)sign in' -or $out -match '(?i)log ?in' -or $out -match '(?i)authenticate' -or $out -match '(?i)signin'

    if ($quota) {
        $outcome = 'BLOCKED'
        $detail = 'provider refused the session: quota exhausted (RESOURCE_EXHAUSTED / 429). Independent of the product.'
    }
    elseif ($alive) {
        # A live session that needed no operator interaction authenticated from
        # a credential store the isolated profile does not own. Driving it would
        # reuse a private provider credential without authorization.
        $outcome = 'BLOCKED'
        $detail = 'isolated launch authenticated from a stored credential (session is not test-owned); using it needs explicit operator authorization'
    }
    elseif ($authPrompt) {
        $outcome = 'BLOCKED'
        $detail = 'vendor requires an interactive login; no authorized test-owned credential is available'
    }
    else {
        $outcome = 'BLOCKED'
        $detail = "vendor exited with code $exitCode without an interactive session; no authorized test-owned session available"
    }

    Write-Verdict 'VENDOR_EXITED' ([string]$exited)
    Write-Verdict 'VENDOR_EXIT_CODE' ([string]$exitCode)
    Write-Verdict 'VENDOR_OUTPUT_TAIL' (($out -replace '\s+', ' ').Substring(0, [Math]::Min(400, ($out -replace '\s+', ' ').Length)))
}
catch {
    $outcome = 'BLOCKED'
    $detail = 'probe error: ' + $_.Exception.Message
}
finally {
    # Only the process this probe started is stopped. Operator sessions are
    # never enumerated, attached or terminated.
    if ($proc -and -not $proc.HasExited) {
        try { $proc.Kill() } catch { }
    }
    foreach ($name in $saved.Keys) {
        [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
    }
}

Write-Verdict 'VENDOR_ACCEPTANCE' $outcome
Write-Verdict 'VENDOR_REASON' $detail
Write-Verdict 'VENDOR_PROFILE_ROOT' $profileRoot
exit $(if ($outcome -eq 'BLOCKED') { 2 } else { 0 })
