# SAI Accounts background keeper: installer + toggle contract.
#
# Proves the durable reconciliation facility without touching the live plane:
#   1. Static definition contract on Install-SaiAccountsKeeper.ps1: one
#      canonical task identity, logon + ~2 min triggers, the INSTALLED engine
#      (never a checkout path), no credentials in the definition, IgnoreNew,
#      transactional replacement with rollback.
#   2. SAITULS toggle contract: persistent AccountKeeper setting, ON/OFF
#      wording, actual-task verification (never checkbox-only), Repair path,
#      elevated-retry path, OFF-means-no-heal semantics.
#   3. Engine contract: autoconnect serializes concurrent sweeps (named sweep
#      mutex, deduplicated loser) and writes a bounded keeper log.
#   4. Live cycle on a scratch task name (register/verify/disable/remove).
#      When task registration is administratively denied here, the installer
#      must fail with the categorized ELEVATION_REQUIRED diagnosis instead of
#      a raw access error -- that categorization IS the assertion.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_accounts_keeper.ps1
# Exit: 0 = all PASS, 1 = failures.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$installer = Join-Path $root 'Scripts\accounts\Install-SaiAccountsKeeper.ps1'
$saituls = Join-Path $root 'SAITULS.cs'
$engine = Join-Path $root 'Scripts\accounts\SaiAccounts.cs'
$fails = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

function Invoke-Installer([string[]]$extra) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $installer @extra 2>&1
        return [pscustomobject]@{
            Output = ($out | ForEach-Object { "$_" }) -join "`n"
            Exit   = $LASTEXITCODE
        }
    } finally { $ErrorActionPreference = $old }
}

Check 'keeper installer exists' (Test-Path -LiteralPath $installer) ''

$src = Get-Content -Raw -LiteralPath $installer

# --- 1. static definition contract -----------------------------------------

Check 'canonical task path \SAITULS\' ($src -match "\`$TaskPath = '\\SAITULS\\'") ''
Check "canonical task name 'SAI Accounts Keeper'" ($src -match "\`$TaskName = 'SAI Accounts Keeper'") ''
Check 'default cadence is 2 minutes' ($src -match '\[int\]\$IntervalMinutes = 2[^0-9]') ''
Check 'cadence bounded to 1..5 minutes' ($src -match 'IntervalMinutes -lt 1 -or .IntervalMinutes -gt 5') ''
Check 'default budget 90000ms' ($src -match '\[int\]\$BudgetMs = 90000') ''
Check 'budget bounded 30s..120s' ($src -match 'BudgetMs -lt 30000 -or .BudgetMs -gt 120000') ''
Check 'action is the installed engine, never a checkout' `
    ($src -match "Join-Path \`$env:ProgramData 'SAI\\Accounts\\bin\\sai-accounts\.exe'") ''
Check 'action carries only the fixed reconciliation verb' `
    ($src -match 'autoconnect --budget-ms \$Budget') ''
Check 'action runs the engine through a windowless host (no console popup, T-284)' `
    ($src -match 'New-ScheduledTaskAction -Execute \$KeeperHostExe' -and $src -match '//B //NoLogo' -and $src -match 'WScript\.Shell' -and $src -match ', 0, False') ''
Check 'windowless host is OS wscript, never a console host' `
    ($src -match '\$KeeperHostExe = Join-Path \$env:SystemRoot .*wscript\.exe' -and $src -notmatch '\$KeeperHostExe = .*powershell\.exe') ''
Check 'launcher is a fixed VBS beside the installed engine' `
    ($src -match '\$KeeperLauncher = Join-Path \(Split-Path -Parent \$EngineExe\) .sai-accounts-keeper\.vbs') ''
Check 'no repository-relative action path' `
    ($src -notmatch 'New-ScheduledTaskAction -Execute \$Watcher' -and $src -notmatch 'Scripts.*sai-accounts\.exe') ''
Check 'logon trigger present' ($src -match 'New-ScheduledTaskTrigger -AtLogOn') ''
Check 'periodic repetition carried by a -Once time trigger' `
    ($src -match 'New-ScheduledTaskTrigger -Once -At \(Get-Date\)\.AddMinutes\(-1\)' -and $src -match 'RepetitionInterval \$span -RepetitionDuration') ''
Check 'repetition is NEVER attached to the logon trigger' `
    ($src -notmatch '\$logonTrigger\.Repetition\s*=' -and $src -notmatch '\$trigger\.Repetition\s*=') ''
Check 'both triggers are registered together' `
    ($src -match '\$trigger = @\(\$logonTrigger, \$repeatTrigger\)') ''
Check 'verifier demands repetition on a non-logon trigger' `
    ($src -match "CimClass\.CimClassName -ne 'MSFT_TaskLogonTrigger'" -and $src -match 'no in-session repeating time trigger') ''
Check 'bounded repetition duration, not MaxValue' `
    ($src -match 'New-TimeSpan -Days 3650' -and ((($src -split "`n" | Where-Object { $_ -notmatch '^\s*#' }) -join "`n") -notmatch '\[TimeSpan\]::MaxValue')) ''
Check 'no second instance (IgnoreNew)' ($src -match 'MultipleInstances IgnoreNew') ''
Check 'hidden + start-when-available + battery-safe + no time limit' `
    ($src -match '-Hidden' -and $src -match '-StartWhenAvailable' -and $src -match '-DontStopIfGoingOnBatteries' -and $src -match 'ExecutionTimeLimit \(\[TimeSpan\]::Zero\)') ''
Check 'least-privilege principal (interactive, limited)' `
    ($src -match 'LogonType Interactive -RunLevel Limited') ''
Check 'verifier pins the exact action arguments' `
    ($src -match '\$action\.Arguments -ne \$expectedArgs') ''
Check 'verifier pins the windowless host, never the raw engine' `
    ($src -match '\$action\.Execute -ne \$KeeperHostExe' -and $src -match '\$expectedArgs = "//B //NoLogo' -and $src -match '\$launcherActual -ne \$launcherExpected') ''
Check 'verifier pins the launcher bytes to the canonical engine and verb' `
    ($src -match 'Launcher drift' -and $src -match 'Get-KeeperLauncherText \$EngineExe \$BudgetMs') ''
Check 'verifier rejects checkout/script references in the definition' `
    ($src -match "'\.ps1', '\.cmd', '\.bat', '\\Scripts\\'") ''
Check 'verifier rejects credential-shaped arguments' `
    ($src -match "'password', 'token', 'secret', 'credential'") ''
Check 'verifier pins exact cadence via XmlConvert' `
    ($src -match '\[System\.Xml\.XmlConvert\]::ToTimeSpan') ''
Check 'verifier pins least-privilege RunLevel' `
    ($src -match "expected Limited") ''
Check 'verifier refuses a disabled task (never checkbox-only)' `
    ($src -match "is registered but disabled") ''
Check 'transactional replacement with XML rollback' `
    ($src -match 'Export-ScheduledTask' -and $src -match '-Xml \$priorBackup\.Xml') ''
Check 'access-denied maps to categorized ELEVATION_REQUIRED' `
    (([regex]::Matches($src, 'ELEVATION_REQUIRED')).Count -ge 3) ''
Check 'enable runs one immediate reconciliation' `
    ($src -match '& \$EngineExe autoconnect --budget-ms \$BudgetMs') ''
Check 'disable leaves brokers and intent alone (says so)' `
    ($src -match 'Running brokers stay online') ''
$paramBlock = ([regex]::Match($src, '(?s)param\(.*?\)\s*\$ErrorActionPreference')).Value
$paramCode = (($paramBlock -split "`n" | Where-Object { $_ -notmatch '^\s*#' }) -join "`n")
Check 'installer takes no account state, no per-account parameters' `
    ($paramCode -notmatch '(?i)account_id|autoconnect|auto_?connect|compact|selector|windows_sid') ''

# --- 2. SAITULS toggle contract ---------------------------------------------

$gui = Get-Content -Raw -LiteralPath $saituls
Check 'keeper desire persisted in SAITULS.ini' `
    ($gui -match 'Read\("AccountKeeper", "0"\)' -and $gui -match 'Write\("AccountKeeper"') ''
Check 'global keeper toggle rendered in Accounts Advanced' `
    ($gui -match 'Background keeper' -and $gui -match 'KeeperSet\(!wantKeeper\)') ''
Check 'toggle states the global/per-account distinction' `
    ($gui -match 'NOT per-account Auto-connect') ''
Check 'keeper never claims to open IDE windows' `
    ($gui -match 'never opens IDE windows') ''
Check 'status inspects the real task definition (-VerifyOnly)' `
    ($gui -match '-VerifyOnly') ''
Check 'drift surfaces a Repair action' `
    ($gui -match 'needs repair' -and $gui -match '"Repair"') ''
Check 'elevation failure degrades to a warning, never a block' `
    ($gui -match 'elevation cancelled' -and $gui -match 'keeps working without it') ''
Check 'elevated retry uses the established RunAs pattern' `
    ($gui -match 'Verb = "runas"') ''
Check 'OFF wording promises no heal, no kill, no intent rewrite' `
    ($gui -match 'no background reconciliation; running contexts stay online') ''
Check 'keeper status reads scheduler last-run + engine keeper log' `
    ($gui -match 'Last Run Time:' -and $gui -match 'keeper-\*\.log') ''
Check 'keeper never polls usage on a cadence' `
    ($gui -match 'never polls /usage on a cadence') ''
Check 'no provider ordinal literals introduced by the keeper UI' `
    ([regex]::Matches($gui, '(?i)\b(AG|CL|CX)([1-9][0-9])\b').Count -eq 0) ''
Check 'no credential material in keeper paths' `
    ($gui -notmatch '(?i)Keeper.*(Password|Token|Secret)') ''

# --- 3. engine contract ------------------------------------------------------

$eng = Get-Content -Raw -LiteralPath $engine
Check 'engine serializes concurrent sweeps (named mutex)' `
    ($eng -match 'AutoconnectMutexName' -and $eng -match 'SAI\.Accounts\.Autoconnect') ''
Check 'loser sweep exits deduplicated without starting anything' `
    ($eng -match '"deduplicated", true') ''
Check 'engine writes a bounded keeper log (counts, no secrets)' `
    ($eng -match 'KeeperLogPath' -and $eng -match 'keeper-" \+ ScopeId\(\) \+ "\.log' -and $eng -match 'maxBytes = 256 \* 1024') ''
Check 'keeper log names outcomes, never SIDs or tokens' `
    ($eng -match 'No SIDs, no' -and $eng -notmatch 'KeeperLog.*target_sid') ''
Check 'desired-state rules untouched (lifecycle, managed, auto_connect order)' `
    ($eng -match 'lifecycle_" \+ state\.ToLowerInvariant' -and $eng -match 'no_managed_context' -and $eng -match 'auto_connect_disabled') ''

# --- 4. live cycle on a scratch name ------------------------------------------

$scratch = 'KeeperTest-' + $PID
$fakeExe = Join-Path $env:TEMP 'keeper-test\bin\sai-accounts.exe'
New-Item -ItemType Directory -Force -Path (Split-Path $fakeExe) | Out-Null
if (-not (Test-Path -LiteralPath $fakeExe)) {
    Copy-Item -LiteralPath (Join-Path $root 'Scripts\accounts\bin\sai-accounts.exe') -Destination $fakeExe -Force
}
try {
    $en = Invoke-Installer @('-Enable', '-TaskName', $scratch, '-EngineExe', $fakeExe, '-NoRun')
    if ($en.Exit -eq 0) {
        Check 'scratch task registers' $true ''
        $vf = Invoke-Installer @('-VerifyOnly', '-TaskName', $scratch, '-EngineExe', $fakeExe)
        Check 'scratch task verifies' ($vf.Exit -eq 0) ("exit=$($vf.Exit)")
        $t = Get-ScheduledTask -TaskPath '\SAITULS\' -TaskName $scratch -ErrorAction SilentlyContinue
        Check 'scratch task enabled' ($t -and $t.State -ne 'Disabled') ("state=$($t.State)")
        $action = @($t.Actions)[0]
        $launcher = Join-Path (Split-Path -Parent $fakeExe) 'sai-accounts-keeper.vbs'
        $expectedArgs = "//B //NoLogo `"$launcher`""
        Check 'scratch action runs the engine through the windowless host' `
            ($action.Execute -eq (Join-Path $env:SystemRoot 'System32\wscript.exe') -and $action.Arguments -eq $expectedArgs) ("execute=$($action.Execute)")
        Check 'scratch launcher pins the engine with SW_HIDE' `
            ((Test-Path -LiteralPath $launcher) -and ((Get-Content -Raw -LiteralPath $launcher) -match ', 0, False') -and ((Get-Content -Raw -LiteralPath $launcher) -match [regex]::Escape($fakeExe))) ''
        Check 'one action only (never one task per account)' (@($t.Actions).Count -eq 1) ''
        Check 'scratch task does not overlap itself' ("$($t.Settings.MultipleInstances)" -eq 'IgnoreNew') ''
        $logon = @($t.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
        $repeat = @($t.Triggers | Where-Object {
            $_.Repetition -and $_.Repetition.Interval -and
            $_.CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger' })
        Check 'scratch task triggers at logon' ($logon.Count -ge 1) ''
        Check 'scratch task repeats in-session (repetition off the logon trigger)' `
            ($repeat.Count -ge 1 -and $repeat[0].Repetition.Interval -eq 'PT2M') ("repeat=" + $repeat.Count)
        $dis = Invoke-Installer @('-Disable', '-TaskName', $scratch, '-EngineExe', $fakeExe)
        $t2 = Get-ScheduledTask -TaskPath '\SAITULS\' -TaskName $scratch -ErrorAction SilentlyContinue
        Check 'disable stops the periodic run' ($dis.Exit -eq 0 -and $t2 -and $t2.State -eq 'Disabled') ("exit=$($dis.Exit)")
        $vf2 = Invoke-Installer @('-VerifyOnly', '-TaskName', $scratch, '-EngineExe', $fakeExe)
        Check 'verify refuses a disabled task' ($vf2.Exit -ne 0) ("exit=$($vf2.Exit)")
    } else {
        Check 'registration refused here only with categorized ELEVATION_REQUIRED' `
            ($en.Output -match 'ELEVATION_REQUIRED') ($en.Output.Substring(0, [Math]::Min(200, $en.Output.Length)))
    }
} finally {
    Invoke-Installer @('-Remove', '-TaskName', $scratch, '-EngineExe', $fakeExe) | Out-Null
    $gone = Get-ScheduledTask -TaskPath '\SAITULS\' -TaskName $scratch -ErrorAction SilentlyContinue
    Check 'scratch task removed (no residue)' ($null -eq $gone) ''
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
