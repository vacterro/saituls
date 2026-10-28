# SAI Accounts autoconnect: desired-state gate, dedup, and runtime purity.
#
# Hermetic. Runs the REAL engine against a SCRATCH plane (SAI_ACCOUNTS_ROOT +
# SAI_ACCOUNTS_SCOPE), so no live account, no live registry and no live broker
# is touched. The eligible row points at a Windows SID that does not exist, so
# the helper refuses the start: that refusal is the assertion -- it proves the
# row passed the desired-state gate and reached the managed-start attempt,
# which a mis-ordered gate (or an auto_connect=false read) could not.
#
# What this covers that the Task Scheduler keeper test cannot:
#   1. desired-state ordering: lifecycle before managed-ness before intent;
#   2. concurrent autoconnect calls dedup (mutex, loser starts nothing);
#   3. already-online row reaches no start request;
#   4. runtime reconciliation never bumps the registry generation;
#   5. account_id and order are untouched by a sweep.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_accounts_autoconnect.ps1
# Exit: 0 = all PASS, 1 = failures.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$engineLocal = Join-Path $root 'Scripts\accounts\bin\sai-accounts.exe'
$engineInstalled = Join-Path $env:ProgramData 'SAI\Accounts\bin\sai-accounts.exe'
$engine = if (Test-Path -LiteralPath $engineLocal) { $engineLocal } else { $engineInstalled }
$fails = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

Check 'engine binary present' (Test-Path -LiteralPath $engine) $engine
if ($fails -gt 0) { exit 1 }

# ---------------------------------------------------------------- scratch plane

$scratch = Join-Path $env:TEMP ("sai-ac-autoconnect-" + $PID)
$scope = 'op-autoconnect-test'
$scopeDir = Join-Path (Join-Path $scratch 'scopes') $scope
New-Item -ItemType Directory -Force -Path $scopeDir | Out-Null

$me = "$env:USERDOMAIN\$env:USERNAME"

function New-Acc([string]$id, [string]$backend, [bool]$managed, [string]$state, $autoConnect, [int]$ordinal = 1) {
    # A syntactically valid SID for a user that does not exist here: the helper
    # cannot resolve it, so no context is ever spawned. Unique per row -- the
    # registry validator refuses duplicate execution contexts.
    $ghostSid = "S-1-5-21-999999999-888888888-777777777-$($4242 + $ordinal)"
    $acc = [ordered]@{
        account_id          = $id
        provider_id         = 'antigravity'
        display_name        = $id
        execution_backend   = $backend
        execution_context_id = "windows-user:ghost$ordinal"
        operational_state   = $state
        hidden              = $false
        created_at          = '2026-01-01T00:00:00.0000000Z'
        updated_at          = '2026-01-01T00:00:00.0000000Z'
        provider_metadata   = [ordered]@{
            windows_sid  = $ghostSid
            windows_user = "sai_autoconnect_ghost$ordinal"
            managed      = $managed
        }
        tags                = @()
        auto_connect        = $autoConnect
        auto_connect_reason = if ($autoConnect) { 'operator_connect' } else { 'operator_disconnect' }
        auto_connect_changed_at = $null
    }
    return $acc
}

$registry = [ordered]@{
    schema         = 'sai.accounts/registry/2'
    generation     = 7
    operator_scope = [ordered]@{ scope_id = $scope; operator_sid = 'S-1-5-21-999999999-888888888-777777777-1001' }
    accounts       = @(
        (New-Acc 'antigravity:windows-user:ghosteligible' 'managed_windows_user' $true  'ENABLED'  $true  1)
        (New-Acc 'antigravity:windows-user:ghostoff'      'managed_windows_user' $true  'ENABLED'  $false 2)
        (New-Acc 'antigravity:windows-user:ghostdisabled' 'managed_windows_user' $true  'DISABLED' $true  3)
        (New-Acc 'antigravity:windows-user:ghostfrozen'   'managed_windows_user' $true  'FROZEN'   $true  4)
        (New-Acc 'antigravity:windows-user:ghostunmanaged' 'windows_user'       $false 'ENABLED'  $true  5)
    )
}
$registryPath = Join-Path $scopeDir 'accounts.json'
($registry | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $registryPath -Encoding UTF8

$env:SAI_ACCOUNTS_ROOT = $scratch
$env:SAI_ACCOUNTS_SCOPE = $scope

function Invoke-Autoconnect {
    $out = & $engine autoconnect --budget-ms 30000 2>&1 | Out-String
    $json = ($out -split "`n" | Where-Object { $_.Trim().StartsWith('{') } | Select-Object -Last 1)
    $code = $LASTEXITCODE
    return [pscustomobject]@{ Raw = $out.Trim(); Exit = $code; Json = ($json | ConvertFrom-Json) }
}

function RowOf($rep, [string]$label) {
    return @($rep.Json.results | Where-Object { $_.account_id -like "*$label*" })[0]
}

try {
    # ---------------------------------------------------- 1. desired-state gate
    $before = (Get-Content -Raw -LiteralPath $registryPath | ConvertFrom-Json)
    $r1 = Invoke-Autoconnect
    Check 'single sweep returns the autoconnect envelope' ($null -ne $r1.Json -and $r1.Json.schema -eq 'sai.accounts/autoconnect/1') "exit=$($r1.Exit)"

    $eligible = RowOf $r1 'ghosteligible'
    Check 'eligible row passes the gate (reaches a managed-start attempt, not a skip)' `
        ($null -ne $eligible -and $eligible.outcome -ne 'skipped') ("outcome=$($eligible.outcome) reason=$($eligible.reason)")
    Check 'eligible row is never reported as an intent decision' `
        ($null -ne $eligible -and $eligible.reason -ne 'auto_connect_disabled') ''
    Check 'eligible row is never reported as lifecycle or unmanaged' `
        ($null -ne $eligible -and "$($eligible.reason)" -notmatch 'lifecycle_|no_managed_context') ("reason=$($eligible.reason)")
    Check 'a refused managed start is failed, never a fabricated start' `
        ($null -ne $eligible -and $eligible.outcome -ne 'started') ("outcome=$($eligible.outcome)")

    $off = RowOf $r1 'ghostoff'
    Check 'auto_connect=false -> skipped/auto_connect_disabled' `
        ($off.outcome -eq 'skipped' -and $off.reason -eq 'auto_connect_disabled') ("$($off.outcome)/$($off.reason)")

    $dis = RowOf $r1 'ghostdisabled'
    Check 'DISABLED -> lifecycle wins over intent (skipped/lifecycle_disabled)' `
        ($dis.outcome -eq 'skipped' -and $dis.reason -eq 'lifecycle_disabled') ("$($dis.outcome)/$($dis.reason)")

    $fro = RowOf $r1 'ghostfrozen'
    Check 'FROZEN -> skipped/lifecycle_frozen' `
        ($fro.outcome -eq 'skipped' -and $fro.reason -eq 'lifecycle_frozen') ("$($fro.outcome)/$($fro.reason)")

    $un = RowOf $r1 'ghostunmanaged'
    Check 'non-managed backend -> skipped/no_managed_context' `
        ($un.outcome -eq 'skipped' -and $un.reason -eq 'no_managed_context') ("$($un.outcome)/$($un.reason)")

    # ------------------------------------------------- 2. runtime purity
    $after = (Get-Content -Raw -LiteralPath $registryPath | ConvertFrom-Json)
    Check 'runtime reconciliation never bumps the registry generation' `
        ($after.generation -eq $before.generation) ("before=$($before.generation) after=$($after.generation)")
    $beforeIds = ($before.accounts | ForEach-Object { $_.account_id }) -join ','
    $afterIds = ($after.accounts | ForEach-Object { $_.account_id }) -join ','
    Check 'account_id values and ordering are stable across a sweep' ($beforeIds -eq $afterIds) ''
    Check 'auto_connect values are untouched by a sweep' `
        ((($after.accounts | ForEach-Object { "$($_.auto_connect)" }) -join ',') -eq
         (($before.accounts | ForEach-Object { "$($_.auto_connect)" }) -join ',')) ''

    # ------------------------------------------------- 3. concurrent dedup
    $jobs = 1..2 | ForEach-Object {
        Start-Job -ScriptBlock {
            param($exe, $r, $s)
            $env:SAI_ACCOUNTS_ROOT = $r; $env:SAI_ACCOUNTS_SCOPE = $s
            & $exe autoconnect --budget-ms 30000 2>&1 | Out-String
        } -ArgumentList $engine, $scratch, $scope
    }
    $outs = $jobs | Wait-Job | Receive-Job
    $jobs | Remove-Job

    $winners = 0; $deduped = 0
    foreach ($o in $outs) {
        $line = ($o -split "`n" | Where-Object { $_.Trim().StartsWith('{') } | Select-Object -Last 1)
        if (-not $line) { continue }
        $j = $line | ConvertFrom-Json
        if ($j.deduplicated -eq $true) { $deduped++ } else { $winners++ }
    }
    Check 'two concurrent sweeps: exactly one loses as deduplicated' `
        ($deduped -eq 1 -and $winners -eq 1) ("dedup=$deduped ran=$winners")
    $loser = $outs | Where-Object { $_ -match '"deduplicated"\s*:\s*true' }
    Check 'the deduplicated loser starts nothing (no results, zero elapsed)' `
        ($null -ne $loser -and $loser -match '"results"\s*:\s*\[\s*\]') ''

    # ------------------------------------------------- 4. keeper log is hermetic
    $logDir = Join-Path $scratch 'logs'
    $log = Join-Path $logDir ("keeper-" + $scope + ".log")
    Check 'keeper log is written inside the scratch plane, never the live one' (Test-Path -LiteralPath $log) $log
    if (Test-Path -LiteralPath $log) {
        $tail = (Get-Content -LiteralPath $log | Select-Object -Last 1)
        Check 'keeper log line carries counts only (no SIDs, no usernames)' `
            ($tail -match 'started=\d+ already=\d+ skipped=\d+ failed=\d+' -and $tail -notmatch 'S-1-5-21') $tail
    }

    # ------------------------------------------------- 5. no live-plane residue
    $liveScope = Get-ChildItem -LiteralPath (Join-Path $env:ProgramData 'SAI\Accounts\scopes') -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq $scope }
    Check 'no scratch scope leaked into the live plane' ($null -eq $liveScope) ''
} finally {
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item Env:\SAI_ACCOUNTS_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:\SAI_ACCOUNTS_SCOPE -ErrorAction SilentlyContinue
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
