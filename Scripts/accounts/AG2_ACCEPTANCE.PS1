# Live acceptance for managed Antigravity (AG2). Not a hermetic harness: this
# drives the INSTALLED control plane and the real managed Windows account, and
# two of its observations are named as operator-only rather than faked here.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\accounts\AG2_ACCEPTANCE.PS1
#   powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\accounts\AG2_ACCEPTANCE.PS1 -RestoreDisconnected
#
# Run build.ps1 -Deploy first. This script refuses to report anything green
# against binaries that are not the ones in this repository.
param([switch]$RestoreDisconnected)

$ErrorActionPreference = 'Stop'
$engine = "$env:ProgramData\SAI\Accounts\bin\sai-accounts.exe"
$helper = "$env:ProgramData\SAI\Accounts\bin\sai-accounts-helper.exe"
$repoBin = Join-Path $PSScriptRoot 'bin'
$failures = 0

function Say($ok, $label, $detail = '') {
    if ($ok) { Write-Host "PASS  $label" -ForegroundColor Green }
    else { Write-Host "FAIL  $label :: $detail" -ForegroundColor Red; $script:failures++ }
}
function Engine { & $engine @args 2>&1 | ConvertFrom-Json }
function RegistryAccounts {
    $f = Get-ChildItem "$env:ProgramData\SAI\Accounts\scopes\*\accounts.json" | Select-Object -First 1
    $raw = [IO.File]::ReadAllBytes($f.FullName)
    if ($raw[0] -eq 0xEF) { $raw = $raw[3..($raw.Length - 1)] }
    ($raw | ForEach-Object { [char]$_ }) -join '' | ConvertFrom-Json
}
function ManagedSid($accounts, $label) {
    ($accounts.accounts | Where-Object { $_.compact_label -eq $label -or
            $_.account_id -eq $label }).provider_metadata.windows_sid
}
function BrokerPid($sid) {
    Get-CimInstance Win32_Process -Filter "Name='sai-accounts.exe'" |
        Where-Object { $_.CommandLine -like "*--broker*" -and $_.CommandLine -like "*$sid*" } |
        Select-Object -First 1
}
function PipeName($scope, $sid) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $fp = ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sid.ToLower())) |
        ForEach-Object { $_.ToString('x2') }) -join ''
    "SAI.Accounts.$scope.$($fp.Substring(0,16))"
}
function PipeStatus($scope, $sid, [int]$waitMs = 10000) {
    # Bounded wait, because "Connect returned" and "the broker bound its pipe"
    # are different moments and the difference is not the product's business.
    $deadline = (Get-Date).AddMilliseconds($waitMs)
    while ((Get-Date) -lt $deadline) {
        try {
            $c = New-Object IO.Pipes.NamedPipeClientStream(
                '.', (PipeName $scope $sid), [IO.Pipes.PipeDirection]::InOut,
                [IO.Pipes.PipeOptions]::None, [Security.Principal.TokenImpersonationLevel]::Identification)
            $c.Connect(1000)
            $w = New-Object IO.StreamWriter($c); $w.WriteLine('{"cmd":"status"}'); $w.Flush()
            $r = New-Object IO.StreamReader($c); $line = $r.ReadLine(); $r.Dispose(); $c.Dispose()
            if ($line) { return ($line | ConvertFrom-Json) }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    return $null
}
function HelperRequest($req) {
    # Impersonation level, not the .NET default. The helper resolves the client
    # SID by impersonating the pipe client, and an identification-level
    # connection hands it a token it cannot open -- which reads as ACCESS_DENIED
    # for a perfectly legitimate operator. Same call the engine makes.
    $name = if ($env:SAI_HELPER_PIPE) { $env:SAI_HELPER_PIPE } else { 'sai-accounts-helper' }
    try {
        $c = New-Object IO.Pipes.NamedPipeClientStream(
            '.', $name, [IO.Pipes.PipeDirection]::InOut,
            [IO.Pipes.PipeOptions]::None, [Security.Principal.TokenImpersonationLevel]::Impersonation)
        $c.Connect(5000)
        $w = New-Object IO.StreamWriter($c); $w.WriteLine(($req | ConvertTo-Json -Compress)); $w.Flush()
        $r = New-Object IO.StreamReader($c); $line = $r.ReadLine(); $r.Dispose(); $c.Dispose()
        if ($line) { $line | ConvertFrom-Json } else { $null }
    } catch { @{ status = 'HELPER_UNREACHABLE'; error = $_.Exception.Message } }
}

# ------------------------------------------------------------------ preflight
foreach ($pair in @(@($engine, 'sai-accounts.exe'), @($helper, 'sai-accounts-helper.exe'))) {
    $installed, $built = $pair[0], (Join-Path $repoBin $pair[1])
    if (-not (Test-Path $built)) { Say $false "preflight $pair[1] built" "missing $built"; return }
    $a = (Get-FileHash $installed -Algorithm SHA256).Hash
    $b = (Get-FileHash $built -Algorithm SHA256).Hash
    Say ($a -eq $b) "preflight $pair[1] is the deployed build" "$installed / $built"
}
if ($failures) { Write-Host 'NOT DEPLOYED - run Scripts\accounts\build.ps1 -Deploy'; exit 1 }

$reg = RegistryAccounts
$scope = (Split-Path (Get-ChildItem "$env:ProgramData\SAI\Accounts\scopes\*\accounts.json" |
        Select-Object -First 1).FullName -Parent | Split-Path -Leaf)
$ag2 = $reg.accounts | Where-Object { $_.provider_metadata.windows_user -eq 'antigravity_b' } | Select-Object -First 1
if (-not $ag2) { Say $false 'locate AG2' 'no managed antigravity account in the registry'; exit 1 }
$sid = $ag2.provider_metadata.windows_sid
Write-Host "AG2 account=$($ag2.account_id) sid=$sid scope=$scope"
$pipe = PipeName $scope $sid

# ------------------------------------------------- A: Connect once -> persists
$before = $ag2.auto_connect
$c = Engine 'context' 'connect' 'AG2'
$now = (RegistryAccounts).accounts | Where-Object { $_.account_id -eq $ag2.account_id }
Say ($now.auto_connect -eq $true) 'A Connect persists auto_connect=true' "before=$before after=$($now.auto_connect)"
$live = PipeStatus $scope $sid
Say ($null -ne $live) 'A broker answers after Connect' "$live"

# ------------------------------- B: SAITULS restarts, no Disconnect, still up
$b = Engine 'autoconnect' '--budget-ms' '60000'
$row = $b.results | Where-Object { $_.account_id -eq $ag2.account_id }
Say ($row.outcome -in @('already_online', 'started')) 'B reopen reconciles without a Connect press' ($row | ConvertTo-Json -Compress)
Say ($null -ne (PipeStatus $scope $sid)) 'B broker reachable after reconciliation' ''

# ------------------------- C: kill only AG2's broker; reconciliation revives it
$victim = BrokerPid $sid
Say ($null -ne $victim) 'C AG2 broker located' ''
if ($victim) {
    Stop-Process -Id $victim.ProcessId -Force
    Start-Sleep -Milliseconds 800
    $c = Engine 'autoconnect' '--budget-ms' '60000'
    $row = $c.results | Where-Object { $_.account_id -eq $ag2.account_id }
    Say ($row.outcome -eq 'started') 'C dead AG2 broker restarted by reconciliation' ($row | ConvertTo-Json -Compress)
    # AG1 is the operator's own account and has no broker. Nothing of its was touched.
    $others = @(Get-CimInstance Win32_Process -Filter "Name='sai-accounts.exe'" |
        Where-Object { $_.CommandLine -like '*--broker*' -and $_.CommandLine -notlike "*$sid*" })
    Say ($others.Count -eq 0) 'C no other broker was disturbed (AG1 independent)' ($others.ProcessId -join ',')
}

# ------------- D: auth proves who, and it is a person other than the operator
# Hermetically this is Test18-Test22 in tests/saituls_accounts_flow_test.cs.
# Live it is only observable as: AG2 is authenticated, it proves an identity
# from a token claim rather than a login name, and that proof belongs to a
# DIFFERENT Google account than AG1's. Without the last check this script
# passes a machine where AG2 is simply a second login to the same account --
# which is not managed identity isolation, however healthy the broker looks.
$ag2st = Engine 'status' 'AG2'
$id2 = ($ag2st.credential).provider_identity
$id1 = ((Engine 'status' 'AG1').credential).provider_identity
Say ($ag2st.auth_state -eq 'AUTHENTICATED') 'D AG2 is authenticated' "auth_state=$($ag2st.auth_state)"
# The operator's own account answers from this process rather than over the
# pipe, and once reported a different field set from the managed one -- its row
# rendered as "no auth state" beside an AUTHENTICATED AG2. Same schema, both.
$ag1st = Engine 'status' 'AG1'
Say ($ag1st.auth_state) 'D AG1 reports auth_state through the same schema' "auth_state=$($ag1st.auth_state)"
Say ($id2.verified -eq $true -and $id2.fingerprint) `
    'D AG2 proves an identity from a token claim, not a login name' ($id2 | ConvertTo-Json -Compress)
# Two reads on purpose: a fingerprint that varied per read would make every
# downstream dedup miss silently -- the class of bug this ticket opened. The
# gap matters: Windows clock granularity is ~15.6ms, so two reads close
# together can share one tick and a per-read nonce slip through unnoticed.
Start-Sleep -Milliseconds 400
$id2b = ((Engine 'status' 'AG2').credential).provider_identity
Say ($id2.fingerprint -and $id2b.fingerprint -eq $id2.fingerprint) `
    'D the same human reads as the same fingerprint on every read' "$($id2.fingerprint) then $($id2b.fingerprint)"
# Distinctness is the entire point of a second slot. When AG1 proves nothing
# there is no comparison to make, so say that rather than score a vacuous pass.
if ($id1.fingerprint) {
    Say ($id2.fingerprint -and $id2.fingerprint -ne $id1.fingerprint) `
        'D AG2 is a different Google account from AG1' "AG1=$($id1.fingerprint) AG2=$($id2.fingerprint)"
} else {
    Say $false 'D AG2 is a different Google account from AG1' `
        'AG1 proves no identity, so distinctness cannot be established'
}

# ------------------------- E: Disconnect persists false and is not resurrected
$c = Engine 'context' 'disconnect' 'AG2'
$now = (RegistryAccounts).accounts | Where-Object { $_.account_id -eq $ag2.account_id }
Say ($now.auto_connect -eq $false) 'E Disconnect persists auto_connect=false' "$($now.auto_connect)"
Start-Sleep -Milliseconds 800
$e = Engine 'autoconnect' '--budget-ms' '20000'
$row = $e.results | Where-Object { $_.account_id -eq $ag2.account_id }
Say ($row.outcome -eq 'skipped' -and $row.reason -eq 'auto_connect_disabled') `
    'E a disconnected account is not resurrected' ($row | ConvertTo-Json -Compress)
Say ($null -eq (PipeStatus $scope $sid)) 'E broker stays offline' ''

# ------------------------------ F: Connect again restores it, without an ask
$c = Engine 'context' 'connect' 'AG2'
$now = (RegistryAccounts).accounts | Where-Object { $_.account_id -eq $ag2.account_id }
Say ($now.auto_connect -eq $true) 'F reconnect restores auto_connect=true' "$($now.auto_connect)"
Say ($null -ne (PipeStatus $scope $sid)) 'F broker back online' ''

# ------------- Explorer route: silent start, exact workdir, YOLO, no AG1 borrow
$stale = BrokerPid $sid
if ($stale) { Stop-Process -Id $stale.ProcessId -Force; Start-Sleep -Milliseconds 800 }
Say ($null -eq (PipeStatus $scope $sid)) 'explorer precondition: AG2 offline but wanted' ''
$project = (Get-Location).Path
$before = @(Get-CimInstance Win32_Process -Filter "Name='agy.exe'" | Select-Object -ExpandProperty ProcessId)
$l = Engine 'launch' 'AG2' '--workdir' $project
Say ($l.status -eq 'launched') 'explorer launch started AG2 with SAITULS closed' ($l | ConvertTo-Json -Compress)
Start-Sleep -Seconds 3
$new = @(Get-CimInstance Win32_Process -Filter "Name='agy.exe'" |
    Where-Object { $before -notcontains $_.ProcessId })
Say ($new.Count -ge 1) 'a real agy.exe child appeared' "new=$($new.ProcessId -join ',')"
if ($new.Count -ge 1) {
    $child = $new | Select-Object -First 1
    # The launch-contract oracle is the child's own command line, not the
    # engine's report of what it meant to pass. No credential can appear here:
    # the fixed argument set is one flag long.
    Say ($child.CommandLine -like '*--dangerously-skip-permissions*') `
        'YOLO flag present on the real child command line' $child.CommandLine
    Say ($child.CommandLine -like "*$project*" -or $child.ExecutablePath -eq "$env:ProgramData\SAI\Accounts\bin\agy.exe") `
        'launch used the engine-owned binary and the exact workdir' "$($child.ExecutablePath) :: $($child.CommandLine)"
    $owner = Invoke-CimMethod -InputObject $child -MethodName GetOwner
    Say ($owner.ReturnValue -eq 0 -and $owner.User -eq $ag2.provider_metadata.windows_user) `
        'the child runs as AG2, not as the operator' "$($owner.User)\$($owner.Domain) rc=$($owner.ReturnValue)"
}

# --------------------- Console profile: the helper's own structured report
$cp = HelperRequest @{ op = 'managed_console_profile'; target_sid = $sid }
Say ($cp.status -in @('applied', 'already_correct')) `
    'managed console profile applied for AG2' ($cp | ConvertTo-Json -Compress -Depth 4)
Say (@($cp.keys).Count -ge 2) 'default Console profile and the per-exe subkey both written' ($cp.keys -join ' | ')
Say (-not $cp.error) 'no console profile error' "$($cp.error)"

# ------------------------------------------------------------- operator-only
Write-Host ''
Write-Host 'NOT PROVABLE FROM THIS SESSION (operator observation, not a fake pass):'
Write-Host '  D. Windows-logon persistence across a physical reboot of the machine.'
Write-Host '  Ctrl+V / Unicode / multiline / Shift+Insert / right-click paste, and the'
Write-Host '  absence of a per-tool "Requesting permission for..." menu, both observed'
Write-Host '  by hand inside the AG2 console that is now open on this desktop.'

if ($RestoreDisconnected -and -not $now.auto_connect) { Engine 'context' 'connect' 'AG2' | Out-Null }
Write-Host ''
Write-Host "AG2_ACCEPTANCE_FAILURES=$failures"
exit ([Math]::Min($failures, 1))