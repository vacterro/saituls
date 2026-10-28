param(
    [string]$ExporterSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Scripts\host\Export-UsnDiagnosticBundle.ps1'),
    [string]$GuardSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Scripts\host\UsnJournalGuard.ps1'),
    [string]$SuiteSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'tests\test_usn_journal_guard.ps1'),
    [string]$Sandbox = (Join-Path $env:TEMP ('saituls_usnbundle_' + [Guid]::NewGuid().ToString('N'))),
    [string]$RunnerHost = '',
    [switch]$KeepSandbox
)
$ErrorActionPreference = 'Stop'
# Integration tests over the completed output directory, not merely the
# scrubber function. Adversarial fixtures carry Windows profile paths,
# non-system drive paths, UNC paths, emails, SIDs, account identities and
# command-line errors containing absolute paths -- in JSON values AND in JSON
# property names, nested objects and arrays. Every fixture identity is
# synthetic (fx_user, FX-SRV, fxsynthuser, FXSYNTHHOST, *.invalid); no real
# local user identifier is used as fixture data. ALL files in each final
# output directory are inspected.
$fails = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}
if (-not (Test-Path -LiteralPath $ExporterSource)) { Write-Host "FAIL  exporter source missing: $ExporterSource"; exit 1 }
$Exe = $RunnerHost
if (-not $Exe) {
    if ($PSVersionTable.PSVersion.Major -ge 6) { $Exe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source } else { $Exe = (Get-Command powershell -ErrorAction SilentlyContinue).Source }
}
if (-not $Exe) { Write-Host 'FAIL  no powershell host found'; exit 1 }
New-Item -ItemType Directory -Path $Sandbox -Force | Out-Null

# ---- 0. the child runtime is the one this runner claims -------------------
$childMajor = (& $Exe -NoProfile -Command '$PSVersionTable.PSVersion.Major' | Select-Object -Last 1)
$expectSeven = [bool]($Exe -match 'pwsh(\.exe)?$')
$runtimeOk = if ($expectSeven) { [int]$childMajor -ge 7 } else { [int]$childMajor -eq 5 }
Check 'child_runtime_matches' $runtimeOk "exe=$(Split-Path -Leaf $Exe) child=PS$childMajor"

function Run-Exporter([string]$fixtureDir, [string]$outDir, [string]$guard = '', [string]$suite = '', [hashtable]$envOverride = $null) {
    $o = Join-Path $Sandbox "out_$([Guid]::NewGuid().ToString('N')).txt"
    $e = Join-Path $Sandbox "err_$([Guid]::NewGuid().ToString('N')).txt"
    $g = if ($guard) { $guard } else { $GuardSource }
    $s = if ($suite) { $suite } else { $SuiteSource }
    $saved = @{}
    if ($envOverride) { foreach ($k in $envOverride.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process'); [Environment]::SetEnvironmentVariable($k, $envOverride[$k], 'Process') } }
    try {
        & $Exe -NoProfile -ExecutionPolicy Bypass -File $ExporterSource -FixtureDir $fixtureDir -OutDir $outDir -GuardSource $g -SuiteSource $s 1> $o 2> $e
        $code = $LASTEXITCODE
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    }
    $ot = ''; if (Test-Path -LiteralPath $o) { $ot = Get-Content -Raw -LiteralPath $o }
    return [pscustomobject]@{ Code = $code; Out = $ot }
}
function Run-Verify([string]$bundleDir) {
    & $Exe -NoProfile -ExecutionPolicy Bypass -File $ExporterSource -VerifyBundle $bundleDir *> $null
    return $LASTEXITCODE
}
function Read-Manifest([string]$dir) {
    try { return (Get-Content -Raw -LiteralPath (Join-Path $dir 'bundle_manifest.json') | ConvertFrom-Json) } catch { return $null }
}
function Rehash-Member([string]$dir, [string]$name) {
    # Recomputes one member's manifest hash so its bytes match the manifest; an
    # unsafe member must then be rejected on CONTENT, never on membership (a
    # SHA-256 match proves integrity, not safety). The manifest text is patched
    # in place so no other byte of it moves.
    $mp = Join-Path $dir 'bundle_manifest.json'
    $txt = Get-Content -Raw -LiteralPath $mp
    $old = [regex]::Match($txt, '"' + [regex]::Escape($name) + '"\s*:\s*"([0-9a-fA-F]{64})"').Groups[1].Value
    if (-not $old) { return $false }
    $new = (Get-FileHash -LiteralPath (Join-Path $dir $name) -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $mp -Value ($txt.Replace($old, $new)) -Encoding UTF8
    return $true
}
# Every synthetic sensitive token any fixture plants, plus the sandbox path
# itself (which lives under the real temp/profile directory).
$Needles = @('fx_user', 'FX-SRV', 'fx.user@example.invalid', '@example.invalid', 'S-1-5-21-777', 'SAIAG_abcdef12', 'FXSECRET', 'fxsynthuser', 'FXSYNTHHOST', 'V:\___VAC', 'D:\Projects\fx', 'D:\FxKeyA', 'D:\FxKeyB', $Sandbox)
function Find-Leaks([string]$dir) {
    # Inspects ALL files under the output directory (recursive, hidden too).
    $hits = New-Object System.Collections.Generic.List[string]
    if (-not (Test-Path -LiteralPath $dir)) { return , $hits }
    foreach ($f in @(Get-ChildItem -LiteralPath $dir -File -Recurse -Force)) {
        $t = Get-Content -Raw -LiteralPath $f.FullName
        if ($null -eq $t) { continue }
        foreach ($n in $Needles) { if ($t.IndexOf($n, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $hits.Add("$($f.Name)~$n") | Out-Null } }
    }
    return , $hits
}
function Test-WarningsShareSafe($manifest) {
    foreach ($w in @($manifest.warnings)) {
        $t = ([string]$w) -replace '<(USERPROFILE|PATH|UNC|EMAIL|SID|ACCOUNT|HOST|USER)>', ' '
        if ($t -match '(?<!HKL)(?<!HKC)(?<!HK)[A-Za-z]:\\' -or $t -match '\\\\[A-Za-z0-9._-]+\\') { return $false }
    }
    return $true
}
function New-AdversarialFixture([string]$name, [switch]$MalformedSnapshot, [switch]$NoTaskPlan, [string]$SnapshotJson = '', [switch]$NoLogTail, [switch]$NoStderr, [string]$TaskQueryJson = '', [string]$HostStateJson = '') {
    $d = Join-Path $Sandbox ("fx_" + $name)
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    if ($SnapshotJson) {
        Set-Content -LiteralPath (Join-Path $d 'guard_snapshot.raw.json') -Value $SnapshotJson -Encoding UTF8
    } elseif (-not $MalformedSnapshot) {
        $snap = [ordered]@{
            schema = 'saituls.usn-journal-guard.v1'
            mode = 'Diagnose'
            exitCode = 1
            detail = 'probe C:\Users\fx_user\Documents\secret.txt and D:\Projects\fx\plan.txt and \\FX-SRV\share\secret.txt mail fx.user@example.invalid sid S-1-5-21-777-888-999-1001 acct SAIAG_abcdef12 repo V:\___VAC\__K\__CODE\__SAITULS\Scripts\host\UsnJournalGuard.ps1'
        } | ConvertTo-Json -Depth 4 -Compress
        Set-Content -LiteralPath (Join-Path $d 'guard_snapshot.raw.json') -Value $snap -Encoding UTF8
    } else {
        Set-Content -LiteralPath (Join-Path $d 'guard_snapshot.raw.json') -Value '{ this is not json, path C:\Users\fx_user\x, "\\\\FX-SRV\\share\\k": "D:\\Projects\\fx\\y" }' -Encoding UTF8
    }
    if (-not $NoStderr) { Set-Content -LiteralPath (Join-Path $d 'guard_snapshot.raw.err') -Value 'fsutil failed on C:\Users\fx_user\tool.exe with code 5' -Encoding UTF8 }
    Set-Content -LiteralPath (Join-Path $d 'snapshotExit.txt') -Value '1' -Encoding UTF8
    if (-not $NoTaskPlan) {
        $planJson = [ordered]@{
            taskPath = '\SAITULS\'
            taskName = 'USN Journal Guard'
            action = [ordered]@{ execute = 'powershell.exe'; arguments = '-NoProfile -File "V:\___VAC\__K\__CODE\__SAITULS\Scripts\host\UsnJournalGuard.ps1" -CorrectRegistry' }
        } | ConvertTo-Json -Depth 5 -Compress
        Set-Content -LiteralPath (Join-Path $d 'task_plan.raw.txt') -Value ("task plan: " + $planJson) -Encoding UTF8
    }
    if (-not $NoStderr) { Set-Content -LiteralPath (Join-Path $d 'task_plan.raw.err') -Value 'task query failed at D:\Admin\fx\query.log' -Encoding UTF8 }
    if ($TaskQueryJson) {
        # Simulated scheduled-task query outcome: {"query":"OK|FAIL",
        # "queryError":"...","info":"OK|FAIL","infoError":"...","tasks":[...]}.
        Set-Content -LiteralPath (Join-Path $d 'installed_tasks.query.json') -Value $TaskQueryJson -Encoding UTF8
    } else {
        $tasks = ConvertTo-Json -InputObject @([ordered]@{ taskPath = '\SAITULS\'; taskName = 'USN Journal Guard'; state = 'Ready'; lastRun = ''; lastResult = 1 }) -Depth 4
        Set-Content -LiteralPath (Join-Path $d 'installed_tasks.raw.json') -Value $tasks -Encoding UTF8
    }
    if ($HostStateJson) {
        # Simulated host observation state: {"bootState","bootTime","clampState","clampValue"}.
        Set-Content -LiteralPath (Join-Path $d 'host_state.json') -Value $HostStateJson -Encoding UTF8
    }
    if (-not $NoLogTail) { Set-Content -LiteralPath (Join-Path $d 'guard_log_tail.raw.log') -Value "2026-10-10 PROBLEM: C:: journal at C:\Users\fx_user\x sid S-1-5-21-777-888-999-1001 mail fx.user@example.invalid unc \\FX-SRV\share\y" -Encoding UTF8 }
    return $d
}
$Allow = @('UsnJournalGuard.ps1', 'test_usn_journal_guard.ps1', 'guard_snapshot.scrubbed.json', 'guard_log_tail.scrubbed.log', 'task_plan.json', 'installed_tasks.scrubbed.json', 'bundle_manifest.json')
$MandatoryNames = @('UsnJournalGuard.ps1', 'test_usn_journal_guard.ps1', 'guard_snapshot.scrubbed.json', 'task_plan.json', 'installed_tasks.scrubbed.json', 'bundle_manifest.json')

# ---- 1. adversarial bundle builds clean (correct complete bundle) ----------
$fx = New-AdversarialFixture 'good'
$out = Join-Path $Sandbox 'bundle_good'
$r = Run-Exporter $fx $out
Check 'adversarial_build_exits_0' ($r.Code -eq 0) "exit=$($r.Code) out=$($r.Out)"
$files = @()
if (Test-Path -LiteralPath $out) { $files = @(Get-ChildItem -LiteralPath $out -File | ForEach-Object { $_.Name }) }
Check 'only_allowlisted_members' ((@($files | Where-Object { $Allow -notcontains $_ }).Count -eq 0) -and $files.Count -gt 0) "files=[$($files -join ' / ')]"
Check 'no_subdirectories' (@(Get-ChildItem -LiteralPath $out -Directory -Force).Count -eq 0) ''
Check 'no_raw_snapshot_remains' ((-not (Test-Path (Join-Path $out 'guard_snapshot.json'))) -and (-not (Test-Path (Join-Path $out 'guard_snapshot.err')))) ''
Check 'no_raw_task_remains' ((-not (Test-Path (Join-Path $out 'task_plan.raw.txt'))) -and (-not (Test-Path (Join-Path $out 'task_plan.err')))) ''
Check 'no_raw_tmp_dir_left' ((-not (Test-Path (Join-Path $out '.raw-tmp'))) -and (-not (Test-Path (Join-Path $out '.tmp-raw')))) ''
$joined = ''
foreach ($f in $files) { $joined += (Get-Content -Raw -LiteralPath (Join-Path $out $f)) + "`n" }
Check 'profile_paths_scrubbed' ($joined -notmatch 'C:\\Users\\fx_user') ''
Check 'nonsystem_drive_scrubbed' ($joined -notmatch 'D:\\Projects\\fx') ''
Check 'unc_scrubbed' ($joined -notmatch 'FX-SRV') ''
Check 'email_scrubbed' ($joined -notmatch 'fx\.user@example\.invalid' -and $joined -notmatch '@example\.invalid') ''
Check 'sid_scrubbed' ($joined -notmatch 'S-1-5-21-777') ''
Check 'account_scrubbed' ($joined -notmatch 'SAIAG_abcdef12') ''
Check 'repo_path_scrubbed_from_evidence' (((Get-Content -Raw (Join-Path $out 'guard_snapshot.scrubbed.json')) -notmatch 'V:\\___VAC') -and ((Get-Content -Raw (Join-Path $out 'task_plan.json')) -notmatch 'V:\\___VAC')) ''
Check 'volume_token_preserved' ((Get-Content -Raw (Join-Path $out 'guard_snapshot.scrubbed.json')) -match 'saituls\.usn-journal-guard') ''
$leaks = Find-Leaks $out
Check 'good_all_files_leak_free' ($leaks.Count -eq 0) "hits=[$($leaks -join ' / ')]"
$manifest = Read-Manifest $out
Check 'manifest_valid_json' ($null -ne $manifest) ''
Check 'manifest_complete_true' ($manifest -and $manifest.complete -eq $true) ''
$hashOk = $true
if ($manifest) {
    foreach ($prop in $manifest.hashes.PSObject.Properties) {
        $p = Join-Path $out $prop.Name
        if (-not (Test-Path -LiteralPath $p)) { $hashOk = $false; break }
        $actual = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne [string]$prop.Value) { $hashOk = $false; break }
    }
    $memberOk = $true
    foreach ($m in @($manifest.members)) { if (-not (Test-Path -LiteralPath (Join-Path $out $m))) { $memberOk = $false } }
    Check 'manifest_hashes_match' ($hashOk) ''
    Check 'manifest_members_exist' ($memberOk) "members=[$($manifest.members -join ' / ')]"
    Check 'manifest_lists_all_mandatory' (@($MandatoryNames | Where-Object { @($manifest.members) -notcontains $_ }).Count -eq 0) ''
    Check 'suite_copy_matches_source' ((Get-FileHash -LiteralPath (Join-Path $out 'test_usn_journal_guard.ps1') -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $SuiteSource -Algorithm SHA256).Hash) ''
    Check 'good_warnings_share_safe' (Test-WarningsShareSafe $manifest) "warnings=[$(@($manifest.warnings) -join ' / ')]"
} else {
    Check 'manifest_hashes_match' ($false) 'no manifest'
    Check 'manifest_members_exist' ($false) 'no manifest'
}
Check 'verify_complete_bundle_exits_0' ((Run-Verify $out) -eq 0) ''

# ---- 2. malformed snapshot is partial, never success ----------------------
$fx2 = New-AdversarialFixture 'badjson' -MalformedSnapshot
$out2 = Join-Path $Sandbox 'bundle_bad'
$r2 = Run-Exporter $fx2 $out2
Check 'malformed_snapshot_not_exit_0' ($r2.Code -ne 0) "exit=$($r2.Code)"
$m2 = Read-Manifest $out2
Check 'malformed_manifest_complete_false' ($m2 -and $m2.complete -eq $false) ''
$l2 = Find-Leaks $out2
Check 'malformed_all_files_leak_free' ($l2.Count -eq 0) "hits=[$($l2 -join ' / ')]"
Check 'verify_partial_bundle_not_0' ((Run-Verify $out2) -ne 0) ''

# ---- 3. missing mandatory evidence is partial -----------------------------
$fx3 = New-AdversarialFixture 'notask' -NoTaskPlan
$out3 = Join-Path $Sandbox 'bundle_notask'
$r3 = Run-Exporter $fx3 $out3
Check 'missing_task_plan_not_exit_0' ($r3.Code -ne 0) "exit=$($r3.Code)"
$m3 = Read-Manifest $out3
Check 'missing_task_plan_complete_false' ($m3 -and $m3.complete -eq $false -and (@($m3.warnings) -match 'task plan missing').Count -gt 0) ''

# ---- 4. unavailable guard source refuses ----------------------------------
$out4 = Join-Path $Sandbox 'bundle_noguard'
$r4 = Run-Exporter $fx $out4 -guard (Join-Path $Sandbox 'does-not-exist.ps1')
Check 'missing_guard_exits_2' ($r4.Code -eq 2) "exit=$($r4.Code)"
$m4 = Read-Manifest $out4
Check 'missing_guard_complete_false' ($m4 -and $m4.complete -eq $false -and (@($m4.warnings) -match 'mandatory member missing: UsnJournalGuard\.ps1').Count -gt 0) "warnings=[$(@($m4.warnings) -join ' / ')]"
Check 'missing_guard_warnings_share_safe' ($m4 -and (Test-WarningsShareSafe $m4) -and (Find-Leaks $out4).Count -eq 0) ''

# ---- 5. missing regression suite fails closed -----------------------------
$out5 = Join-Path $Sandbox 'bundle_nosuite'
$r5 = Run-Exporter $fx $out5 -suite (Join-Path $Sandbox 'FXSECRET missing dir\test_usn_journal_guard.ps1')
Check 'missing_suite_exits_1' ($r5.Code -eq 1) "exit=$($r5.Code)"
$m5 = Read-Manifest $out5
Check 'missing_suite_complete_false' ($m5 -and $m5.complete -eq $false) ''
Check 'missing_suite_reason_recorded' ($m5 -and (@($m5.warnings) -match 'mandatory member missing: test_usn_journal_guard\.ps1').Count -gt 0) "warnings=[$(@($m5.warnings) -join ' / ')]"
Check 'missing_suite_not_member' ($m5 -and @($m5.members) -notcontains 'test_usn_journal_guard.ps1' -and -not (Test-Path (Join-Path $out5 'test_usn_journal_guard.ps1'))) ''
Check 'missing_suite_leak_free' ($m5 -and (Test-WarningsShareSafe $m5) -and (Find-Leaks $out5).Count -eq 0) ''
Check 'verify_missing_suite_not_0' ((Run-Verify $out5) -ne 0) ''

# ---- 6. unreadable regression suite fails closed, message scrubbed --------
$lockDir = Join-Path $Sandbox 'FXSECRET locked dir'
New-Item -ItemType Directory -Path $lockDir -Force | Out-Null
$lockedSuite = Join-Path $lockDir 'test_usn_journal_guard.ps1'
Copy-Item -LiteralPath $SuiteSource -Destination $lockedSuite
$out6 = Join-Path $Sandbox 'bundle_lockedsuite'
$fs = [IO.File]::Open($lockedSuite, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
try { $r6 = Run-Exporter $fx $out6 -suite $lockedSuite } finally { $fs.Dispose() }
Check 'unreadable_suite_exits_1' ($r6.Code -eq 1) "exit=$($r6.Code)"
$m6 = Read-Manifest $out6
Check 'unreadable_suite_complete_false' ($m6 -and $m6.complete -eq $false) ''
Check 'unreadable_suite_reason_recorded' ($m6 -and (@($m6.warnings) -match 'mandatory member unreadable: test_usn_journal_guard\.ps1').Count -gt 0) "warnings=[$(@($m6.warnings) -join ' / ')]"
Check 'unreadable_suite_no_partial_copy' (-not (Test-Path (Join-Path $out6 'test_usn_journal_guard.ps1'))) ''
$l6 = Find-Leaks $out6
Check 'copy_failure_message_scrubbed' ($m6 -and (Test-WarningsShareSafe $m6) -and $l6.Count -eq 0) "hits=[$($l6 -join ' / ')]"
Check 'console_output_scrubbed' ($r6.Out -notmatch 'FXSECRET' -and $r6.Out.IndexOf($Sandbox, [StringComparison]::OrdinalIgnoreCase) -lt 0) ''

# ---- 7. manifest member/hash disagreement is detected ----------------------
function Copy-Bundle([string]$name) { $d = Join-Path $Sandbox $name; Copy-Item -LiteralPath $out -Destination $d -Recurse; return $d }
$t1 = Copy-Bundle 'tamper_bytes'
Add-Content -LiteralPath (Join-Path $t1 'task_plan.json') -Value ' '
Check 'verify_hash_disagreement_not_0' ((Run-Verify $t1) -ne 0) ''
$t2 = Copy-Bundle 'tamper_member'
Remove-Item -LiteralPath (Join-Path $t2 'test_usn_journal_guard.ps1')
Check 'verify_missing_member_not_0' ((Run-Verify $t2) -ne 0) ''
$t3 = Copy-Bundle 'tamper_manifest'
$mt = Get-Content -Raw -LiteralPath (Join-Path $t3 'bundle_manifest.json') | ConvertFrom-Json
$mt.members = @($mt.members | Where-Object { $_ -ne 'test_usn_journal_guard.ps1' })
Set-Content -LiteralPath (Join-Path $t3 'bundle_manifest.json') -Value ($mt | ConvertTo-Json -Depth 6) -Encoding UTF8
Check 'verify_undeclared_file_not_0' ((Run-Verify $t3) -ne 0) ''
$t4 = Copy-Bundle 'tamper_subdir'
New-Item -ItemType Directory -Path (Join-Path $t4 'extra') | Out-Null
Check 'verify_unexpected_subdir_not_0' ((Run-Verify $t4) -ne 0) ''
$t5 = Copy-Bundle 'tamper_warning'
$mw = Get-Content -Raw -LiteralPath (Join-Path $t5 'bundle_manifest.json') | ConvertFrom-Json
$mw.warnings = @('copy failed for D:\Projects\fx\raw.txt')
Set-Content -LiteralPath (Join-Path $t5 'bundle_manifest.json') -Value ($mw | ConvertTo-Json -Depth 6) -Encoding UTF8
Check 'verify_unsafe_warning_not_0' ((Run-Verify $t5) -ne 0) ''

# ---- 8. a non-empty output directory is refused, nothing written ----------
$out8 = Join-Path $Sandbox 'bundle_stale'
Copy-Item -LiteralPath $out -Destination $out8 -Recurse
$before = @(Get-ChildItem -LiteralPath $out8 -File | ForEach-Object { $_.Name + ':' + $_.Length + ':' + $_.LastWriteTimeUtc.Ticks }) -join '|'
$r8 = Run-Exporter $fx $out8 -suite (Join-Path $Sandbox 'nope.ps1')
$after = @(Get-ChildItem -LiteralPath $out8 -File | ForEach-Object { $_.Name + ':' + $_.Length + ':' + $_.LastWriteTimeUtc.Ticks }) -join '|'
Check 'stale_outdir_refused_exit_2' ($r8.Code -eq 2) "exit=$($r8.Code)"
Check 'stale_outdir_untouched' ($before -eq $after) ''

# ---- 9. sensitive JSON property names, nested keys, arrays, collisions -----
$keySnap = @'
{
  "schema": "saituls.usn-journal-guard.v1",
  "mode": "Diagnose",
  "exitCode": 0,
  "C:\\Users\\fx_user\\secret.txt": "profile-key",
  "fx.user@example.invalid": { "S-1-5-21-777-888-999-1001": [ { "\\\\FX-SRV\\share\\k": "unc-key" }, ["SAIAG_abcdef12", []] ] },
  "D:\\FxKeyA\\one": 1,
  "D:\\FxKeyB\\two": 2,
  "<PATH>": "literal",
  "fxsynthuser-notes": "host FXSYNTHHOST",
  "volumes": [ { "volume": "C", "state": "Active", "maxBytes": 33554432, "detail": "" } ],
  "problems": [],
  "actions": ["one"]
}
'@
$fx9 = New-AdversarialFixture 'keys' -SnapshotJson $keySnap
$out9 = Join-Path $Sandbox 'bundle_keys'
$r9 = Run-Exporter $fx9 $out9 -envOverride @{ USERNAME = 'fxsynthuser'; COMPUTERNAME = 'FXSYNTHHOST' }
Check 'sensitive_keys_build_exits_0' ($r9.Code -eq 0) "exit=$($r9.Code) out=$($r9.Out)"
$l9 = Find-Leaks $out9
Check 'sensitive_keys_all_files_leak_free' ($l9.Count -eq 0) "hits=[$($l9 -join ' / ')]"
$s9 = $null
try { $s9 = Get-Content -Raw -LiteralPath (Join-Path $out9 'guard_snapshot.scrubbed.json') | ConvertFrom-Json } catch { }
Check 'sensitive_keys_output_valid_json' ($null -ne $s9) ''
if ($s9) {
    $names = @($s9.PSObject.Properties | ForEach-Object { $_.Name })
    $pathKeys = @($s9.PSObject.Properties | Where-Object { $_.Name -like '<PATH>*' })
    $pathVals = @($pathKeys | ForEach-Object { [string]$_.Value } | Sort-Object)
    Check 'colliding_keys_not_merged' ($pathKeys.Count -eq 3 -and (($pathVals -join ',') -eq '1,2,literal')) "keys=[$($pathKeys.Name -join ' / ')] vals=[$($pathVals -join ',')]"
    Check 'colliding_keys_distinct' (@($names | Sort-Object -Unique).Count -eq $names.Count) ''
    Check 'canonical_keys_preserved' (@('schema', 'mode', 'exitCode', 'volumes', 'problems', 'actions' | Where-Object { $names -cnotcontains $_ }).Count -eq 0) "keys=[$($names -join ' / ')]"
    Check 'env_identity_key_scrubbed' (@($names | Where-Object { $_ -like '<USER>-notes*' }).Count -eq 1) ''
    $nested = $s9.'<EMAIL>'
    Check 'nested_key_scrubbed' ($null -ne $nested -and $null -ne $nested.'<SID>') ''
    if ($null -ne $nested -and $null -ne $nested.'<SID>') {
        $arr = $nested.'<SID>'
        Check 'nested_array_shape_stable' ($arr -is [array] -and $arr.Count -eq 2 -and $arr[1] -is [array] -and $arr[1].Count -eq 2 -and $arr[1][1] -is [array] -and $arr[1][1].Count -eq 0) ''
        Check 'nested_array_key_scrubbed' ($null -ne $arr[0].'<UNC>' -and $arr[1][0] -eq '<ACCOUNT>') ''
    }
    Check 'single_element_arrays_stay_arrays' ($s9.volumes -is [array] -and @($s9.volumes).Count -eq 1 -and $s9.actions -is [array] -and @($s9.actions).Count -eq 1) ''
    Check 'empty_array_stays_array' ($s9.problems -is [array] -and @($s9.problems).Count -eq 0) ''
}
$m9 = Read-Manifest $out9
Check 'key_redaction_reported' ($m9 -and (@($m9.warnings) -match 'json property names redacted in guard_snapshot\.scrubbed\.json').Count -gt 0) ''
Check 'sensitive_keys_verify_exits_0' ((Run-Verify $out9) -eq 0) ''

# ---- 10. keys colliding only by case: partial OR both values preserved -----
$fx10 = New-AdversarialFixture 'casekeys' -SnapshotJson '{"schema":"saituls.usn-journal-guard.v1","FxKey":"upper","fxkey":"lower"}'
$out10 = Join-Path $Sandbox 'bundle_casekeys'
$r10 = Run-Exporter $fx10 $out10
$m10 = Read-Manifest $out10
$caseOk = $false
if ($r10.Code -ne 0) { $caseOk = ($m10 -and $m10.complete -eq $false) }
else {
    $raw10 = Get-Content -Raw -LiteralPath (Join-Path $out10 'guard_snapshot.scrubbed.json')
    $caseOk = ($raw10 -match '"upper"' -and $raw10 -match '"lower"')
}
Check 'case_collision_never_silently_merged' $caseOk "exit=$($r10.Code)"

# ---- 11. completely clean expected schema passes unchanged -----------------
$cleanSnap = '{"schema":"saituls.usn-journal-guard.v1","mode":"Diagnose","exitCode":0,"elevated":false,"psVersion":"5.1","fsutil":"fsutil.exe","rebootRequired":false,"targetUnmet":false,"registry":{"path":"HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem","name":"NtfsAllowUsnMinSize1Mb","present":false,"value":null,"readFailed":false},"volumes":[{"volume":"C","state":"Active","maxBytes":33554432,"maxMB":32,"healthy":true,"detail":"","targetMB":32,"targetMet":true}],"problems":[],"actions":[]}'
$fx11 = New-AdversarialFixture 'clean' -SnapshotJson $cleanSnap -NoLogTail -NoStderr
$out11 = Join-Path $Sandbox 'bundle_clean'
$r11 = Run-Exporter $fx11 $out11
Check 'clean_schema_exits_0' ($r11.Code -eq 0) "exit=$($r11.Code) out=$($r11.Out)"
$inNorm = ConvertTo-Json -InputObject ($cleanSnap | ConvertFrom-Json) -Depth 10 -Compress
$outNorm = ''
try { $outNorm = ConvertTo-Json -InputObject (Get-Content -Raw -LiteralPath (Join-Path $out11 'guard_snapshot.scrubbed.json') | ConvertFrom-Json) -Depth 10 -Compress } catch { }
Check 'clean_schema_round_trips_identically' ($inNorm -ceq $outNorm) ''
$m11 = Read-Manifest $out11
Check 'clean_schema_no_key_redaction' ($m11 -and (@($m11.warnings) -match 'redacted').Count -eq 0) ''
Check 'optional_absence_reported_not_fatal' ($m11 -and $m11.complete -eq $true -and (@($m11.warnings) -match 'optional member absent: guard_log_tail\.scrubbed\.log').Count -gt 0) ''

# ---- 12. scheduled-task observation: absence is never UNKNOWN --------------
# A successful empty query is a truthfully empty task set; a failed one is
# UNKNOWN presence and must never be serialized as an empty task list.
$fxEmpty = New-AdversarialFixture 'tskempty' -TaskQueryJson '{"query":"OK","tasks":[]}'
$outEmpty = Join-Path $Sandbox 'bundle_tskempty'
$rEmpty = Run-Exporter $fxEmpty $outEmpty
Check 'task_query_empty_exits_0' ($rEmpty.Code -eq 0) "exit=$($rEmpty.Code) out=$($rEmpty.Out)"
$mEmpty = Read-Manifest $outEmpty
Check 'task_query_empty_complete_true' ($mEmpty -and $mEmpty.complete -eq $true) ''
$eEmpty = $null
try { $eEmpty = Get-Content -Raw -LiteralPath (Join-Path $outEmpty 'installed_tasks.scrubbed.json') | ConvertFrom-Json } catch { }
Check 'task_query_empty_presence_none' ($eEmpty -and $eEmpty.queryState -ceq 'QUERY_OK_EMPTY' -and $eEmpty.taskPresence -ceq 'NONE' -and $eEmpty.taskCount -eq 0) ''
Check 'verify_task_query_empty_exits_0' ((Run-Verify $outEmpty) -eq 0) ''

$fxFail = New-AdversarialFixture 'tskfail' -TaskQueryJson '{"query":"FAIL","queryError":"The Task Scheduler service is not available (0x800706BA)"}'
$outFail = Join-Path $Sandbox 'bundle_tskfail'
$rFail = Run-Exporter $fxFail $outFail
Check 'task_query_failed_not_exit_0' ($rFail.Code -ne 0) "exit=$($rFail.Code)"
$mFail = Read-Manifest $outFail
Check 'task_query_failed_complete_false' ($mFail -and $mFail.complete -eq $false) ''
Check 'task_query_failed_reason_recorded' ($mFail -and (@($mFail.warnings) -match 'task presence UNKNOWN').Count -gt 0) "warnings=[$(@($mFail.warnings) -join ' / ')]"
$failRaw = Get-Content -Raw -LiteralPath (Join-Path $outFail 'installed_tasks.scrubbed.json')
$eFail = $null
try { $eFail = $failRaw | ConvertFrom-Json } catch { }
Check 'task_query_failed_presence_unknown' ($eFail -and $eFail.queryState -ceq 'QUERY_FAILED' -and $eFail.taskPresence -ceq 'UNKNOWN' -and $null -eq $eFail.taskCount) ''
Check 'task_query_failed_no_fabricated_absence' ($eFail -and $eFail.taskPresence -cne 'NONE' -and $eFail.taskCount -ne 0 -and @($eFail.tasks).Count -eq 0) ''
Check 'task_query_failed_not_bare_array' ($failRaw.TrimStart().StartsWith('{')) ''
Check 'task_query_failed_warning_share_safe' ($mFail -and (Test-WarningsShareSafe $mFail) -and (Find-Leaks $outFail).Count -eq 0) "hits=[$((Find-Leaks $outFail) -join ' / ')]"

$fxDeny = New-AdversarialFixture 'tskdeny' -TaskQueryJson '{"query":"FAIL","queryError":"Access is denied."}'
$outDeny = Join-Path $Sandbox 'bundle_tskdeny'
$rDeny = Run-Exporter $fxDeny $outDeny
$mDeny = Read-Manifest $outDeny
Check 'task_query_access_denied_visible' ($rDeny.Code -ne 0 -and $mDeny -and (@($mDeny.warnings) -match 'Access is denied').Count -gt 0) "exit=$($rDeny.Code) warnings=[$(@($mDeny.warnings) -join ' / ')]"

$fxPartial = New-AdversarialFixture 'tskpartial' -TaskQueryJson '{"query":"OK","tasks":[{"taskPath":"\\SAITULS\\","taskName":"USN Journal Guard","state":"Ready"},{"taskPath":"\\SAITULS\\","taskName":"SAISPIN Watchdog","state":"Running"}],"info":"FAIL","infoError":"Access is denied."}'
$outPartial = Join-Path $Sandbox 'bundle_tskpartial'
$rPartial = Run-Exporter $fxPartial $outPartial
Check 'task_info_partial_exits_0' ($rPartial.Code -eq 0) "exit=$($rPartial.Code) out=$($rPartial.Out)"
$mPartial = Read-Manifest $outPartial
$ePartial = $null
try { $ePartial = Get-Content -Raw -LiteralPath (Join-Path $outPartial 'installed_tasks.scrubbed.json') | ConvertFrom-Json } catch { }
Check 'task_info_partial_marks_state' ($ePartial -and $ePartial.infoState -ceq 'TASK_INFO_PARTIAL' -and $ePartial.queryState -ceq 'QUERY_OK_WITH_TASKS') ''
Check 'task_info_partial_keeps_two_identities' ($ePartial -and @($ePartial.tasks).Count -eq 2 -and @($ePartial.tasks | Where-Object { $_.infoState -ceq 'TASK_INFO_PARTIAL' }).Count -eq 2) ''
Check 'task_info_partial_reported_not_fatal' ($mPartial -and $mPartial.complete -eq $true -and (@($mPartial.warnings) -match 'metadata partially unavailable').Count -gt 0) "warnings=[$(@($mPartial.warnings) -join ' / ')]"
Check 'verify_task_info_partial_exits_0' ((Run-Verify $outPartial) -eq 0) ''

# ---- 13. host observation is separate from bundle completeness -------------
$fxHostUnread = New-AdversarialFixture 'hostunread' -HostStateJson '{"bootState":"UNREADABLE","bootTime":null,"clampState":"UNREADABLE","clampValue":null}'
$outHostUnread = Join-Path $Sandbox 'bundle_hostunread'
$rHostUnread = Run-Exporter $fxHostUnread $outHostUnread
$mHostUnread = Read-Manifest $outHostUnread
Check 'host_unreadable_bundle_still_complete' ($rHostUnread.Code -eq 0 -and $mHostUnread -and $mHostUnread.complete -eq $true) "exit=$($rHostUnread.Code) out=$($rHostUnread.Out)"
Check 'host_unreadable_observation_incomplete' ($mHostUnread -and $mHostUnread.hostObservationComplete -eq $false -and $mHostUnread.clampState -ceq 'UNREADABLE' -and $mHostUnread.bootTimeState -ceq 'UNREADABLE') ''
Check 'host_unreadable_not_absent_not_healthy' ($mHostUnread -and $mHostUnread.clampState -cne 'ABSENT' -and $null -eq $mHostUnread.clampValue -and $null -eq $mHostUnread.bootTime) ''
Check 'host_unreadable_warned' ($mHostUnread -and (@($mHostUnread.warnings) -match 'UNKNOWN, not absent').Count -eq 2) "warnings=[$(@($mHostUnread.warnings) -join ' / ')]"
Check 'verify_host_unreadable_still_exits_0' ((Run-Verify $outHostUnread) -eq 0) ''

$fxHostPresent = New-AdversarialFixture 'hostpresent' -HostStateJson '{"bootState":"OK","bootTime":"2026-10-10T00:00:00.0000000+03:00","clampState":"PRESENT","clampValue":"1"}'
$outHostPresent = Join-Path $Sandbox 'bundle_hostpresent'
$rHostPresent = Run-Exporter $fxHostPresent $outHostPresent
$mHostPresent = Read-Manifest $outHostPresent
# The boot time is compared as present + state OK, not as a date string: PS 7
# ConvertFrom-Json coerces an ISO-8601 string to DateTime, PS 5.1 does not.
Check 'host_present_confirmed' ($rHostPresent.Code -eq 0 -and $mHostPresent -and $mHostPresent.clampState -ceq 'PRESENT' -and [string]$mHostPresent.clampValue -ceq '1' -and $mHostPresent.bootTimeState -ceq 'OK' -and $null -ne $mHostPresent.bootTime -and $mHostPresent.hostObservationComplete -eq $true) "exit=$($rHostPresent.Code) out=$($rHostPresent.Out)"

$fxHostAbsent = New-AdversarialFixture 'hostabsent' -HostStateJson '{"bootState":"OK","bootTime":"synthetic","clampState":"ABSENT","clampValue":null}'
$outHostAbsent = Join-Path $Sandbox 'bundle_hostabsent'
$rHostAbsent = Run-Exporter $fxHostAbsent $outHostAbsent
$mHostAbsent = Read-Manifest $outHostAbsent
Check 'host_absent_confirmed' ($rHostAbsent.Code -eq 0 -and $mHostAbsent -and $mHostAbsent.clampState -ceq 'ABSENT' -and $null -eq $mHostAbsent.clampValue -and $mHostAbsent.hostObservationComplete -eq $true) "exit=$($rHostAbsent.Code) out=$($rHostAbsent.Out)"

# ---- 14. -VerifyBundle rejects unsafe content despite a matching hash ------
$tv = Copy-Bundle 'verify_privacy'
Set-Content -LiteralPath (Join-Path $tv 'task_plan.json') -Value '{"taskPath":"\\SAITULS\\","taskName":"USN Journal Guard"}' -Encoding UTF8
$reh = Rehash-Member $tv 'task_plan.json'
Check 'verify_safe_rehash_still_0' ($reh -and ((Run-Verify $tv) -eq 0)) 'safe content with a recomputed hash must still verify'

$tv2 = Copy-Bundle 'verify_privacy_value'
Set-Content -LiteralPath (Join-Path $tv2 'guard_snapshot.scrubbed.json') -Value '{"schema":"saituls.usn-journal-guard.v1","mode":"Diagnose","detail":"probe C:\\Users\\fx_real\\secret.txt"}' -Encoding UTF8
$reh2 = Rehash-Member $tv2 'guard_snapshot.scrubbed.json'
Check 'verify_unsafe_value_rehash_not_0' ($reh2 -and ((Run-Verify $tv2) -ne 0)) ''

$tv3 = Copy-Bundle 'verify_privacy_key'
Set-Content -LiteralPath (Join-Path $tv3 'guard_snapshot.scrubbed.json') -Value '{"schema":"saituls.usn-journal-guard.v1","mode":"Diagnose","D:\\FxKeyA\\secret":1}' -Encoding UTF8
$reh3 = Rehash-Member $tv3 'guard_snapshot.scrubbed.json'
Check 'verify_unsafe_key_rehash_not_0' ($reh3 -and ((Run-Verify $tv3) -ne 0)) ''

$tv6 = Copy-Bundle 'verify_privacy_nested'
Set-Content -LiteralPath (Join-Path $tv6 'guard_snapshot.scrubbed.json') -Value '{"schema":"saituls.usn-journal-guard.v1","mode":"Diagnose","volumes":[{"volume":"C","state":"Active","detail":["nested C:\\Users\\fx_real\\secret.txt"]}]}' -Encoding UTF8
$reh6 = Rehash-Member $tv6 'guard_snapshot.scrubbed.json'
Check 'verify_unsafe_nested_rehash_not_0' ($reh6 -and ((Run-Verify $tv6) -ne 0)) ''

$tv7 = Copy-Bundle 'verify_privacy_badjson'
Set-Content -LiteralPath (Join-Path $tv7 'guard_snapshot.scrubbed.json') -Value '{"schema": broken' -Encoding UTF8
$reh7 = Rehash-Member $tv7 'guard_snapshot.scrubbed.json'
Check 'verify_invalid_json_rehash_not_0' ($reh7 -and ((Run-Verify $tv7) -ne 0)) ''

$tv4 = Copy-Bundle 'verify_privacy_log'
Set-Content -LiteralPath (Join-Path $tv4 'guard_log_tail.scrubbed.log') -Value 'audit C:\Users\fx_real\secret.txt' -Encoding UTF8
$reh4 = Rehash-Member $tv4 'guard_log_tail.scrubbed.log'
Check 'verify_unsafe_log_rehash_not_0' ($reh4 -and ((Run-Verify $tv4) -ne 0)) ''

$tv5 = Copy-Bundle 'verify_privacy_warning'
$mw5 = Get-Content -Raw -LiteralPath (Join-Path $tv5 'bundle_manifest.json') | ConvertFrom-Json
$mw5.warnings = @($mw5.warnings) + 'host C:\Users\fx_real\secret.txt'
Set-Content -LiteralPath (Join-Path $tv5 'bundle_manifest.json') -Value ($mw5 | ConvertTo-Json -Depth 6) -Encoding UTF8
Check 'verify_unsafe_warning_rehash_not_0' ((Run-Verify $tv5) -ne 0) ''

# ---- 15. source-member privacy boundary (T-280) ----------------------------
# The two shipped source members are mandatory troubleshooting evidence and are
# never scrubbed; their DELIVERED bytes get an independent read-only privacy
# verdict instead. Every poisoned fixture is a synthetic copy of the real member
# with one sensitive line appended as a comment: the repository's own source
# files are never modified, and the pristine members stay clean.
function New-PoisonedSource([string]$orig, [string]$tag, [string]$line) {
    $p = Join-Path $Sandbox ("poison_$tag")
    New-Item -ItemType Directory -Path $p -Force | Out-Null
    $leaf = Split-Path -Leaf $orig
    $t = Get-Content -Raw -LiteralPath $orig
    Set-Content -LiteralPath (Join-Path $p $leaf) -Value ($t + "`r`n" + $line + "`r`n") -Encoding UTF8
    return (Join-Path $p $leaf)
}
$poisonCases = @(
    @{ tag = 'profile'; line = '# probe C:\Users\fx_real\private\notes.txt' },
    @{ tag = 'repo'; line = '# probe V:\___VAC\__K\__CODE\__SAITULS\Scripts\host\UsnJournalGuard.ps1' },
    @{ tag = 'ids'; line = '# probe fx.real@example.invalid S-1-5-21-777-888-999-1001 SAIAG_abcdef12' },
    @{ tag = 'unc'; line = '# probe \\FX-SRV\share\secret.txt' }
)
foreach ($c in $poisonCases) {
    $poison = New-PoisonedSource $GuardSource ("guard_" + $c.tag) $c.line
    $outP = Join-Path $Sandbox ("bundle_poison_" + $c.tag)
    $rP = Run-Exporter $fx $outP -guard $poison
    Check ("source_poison_$($c.tag)_refused") ($rP.Code -ne 0) "exit=$($rP.Code)"
    $mP = Read-Manifest $outP
    Check ("source_poison_$($c.tag)_complete_false") ($mP -and $mP.complete -eq $false) ''
    Check ("source_poison_$($c.tag)_member_not_shipped") (-not (Test-Path (Join-Path $outP 'UsnJournalGuard.ps1'))) ''
    Check ("source_poison_$($c.tag)_reason_recorded") ($mP -and (@($mP.warnings) -match 'not share-safe: UsnJournalGuard\.ps1').Count -gt 0) "warnings=[$(@($mP.warnings) -join ' / ')]"
    Check ("source_poison_$($c.tag)_warnings_scrubbed") ($mP -and (Test-WarningsShareSafe $mP) -and (Find-Leaks $outP).Count -eq 0) "hits=[$((Find-Leaks $outP) -join ' / ')]"
    Check ("source_poison_$($c.tag)_verify_refused") ((Run-Verify $outP) -ne 0) ''
}
# Ordinary technical constants in a source member are accepted, and the member
# is shipped byte-identically (privacy validation never rewrites the file).
$techDir = Join-Path $Sandbox 'techsrc'
New-Item -ItemType Directory -Path $techDir -Force | Out-Null
$techGuard = Join-Path $techDir 'UsnJournalGuard.ps1'
Set-Content -LiteralPath $techGuard -Value "param() # HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem 0x0000000000100000 0x7fffffffffff0000 volume C: D: '\SAITULS\' Depth 10 'Maximum Size' 4096 2026-10-10T00:00:00Z" -Encoding UTF8
$outTech = Join-Path $Sandbox 'bundle_techsrc'
$rTech = Run-Exporter $fx $outTech -guard $techGuard
Check 'source_technical_constants_accepted' ($rTech.Code -eq 0) "exit=$($rTech.Code) out=$($rTech.Out)"
Check 'source_technical_constants_member_byte_identical' ((Test-Path (Join-Path $outTech 'UsnJournalGuard.ps1')) -and ((Get-FileHash -LiteralPath (Join-Path $outTech 'UsnJournalGuard.ps1') -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $techGuard -Algorithm SHA256).Hash)) ''
Check 'guard_copy_matches_source' ((Get-FileHash -LiteralPath (Join-Path $out 'UsnJournalGuard.ps1') -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $GuardSource -Algorithm SHA256).Hash) ''
Check 'clean_source_members_verify_0' ((Run-Verify $out) -eq 0) ''
# A recomputed manifest hash over unsafe source bytes is rejected on content.
$ts = Copy-Bundle 'verify_source_privacy'
Set-Content -LiteralPath (Join-Path $ts 'test_usn_journal_guard.ps1') -Value ((Get-Content -Raw -LiteralPath (Join-Path $ts 'test_usn_journal_guard.ps1')) + "`r`n# probe C:\Users\fx_real\private\notes.txt`r`n") -Encoding UTF8
$rehs = Rehash-Member $ts 'test_usn_journal_guard.ps1'
Check 'source_member_rehash_cannot_bypass_privacy' ($rehs -and ((Run-Verify $ts) -ne 0)) ''

# ---- 16. deep JSON cannot silently bypass the privacy verdict (T-280/Phase3) -
# A sensitive token nested past the supported verification depth used to be
# silently skipped (the walk stopped at depth > 12 and the member read as
# scrub-stable). It is now REFUSED: no silent traversal truncation may produce
# a successful privacy verdict, and a recomputed hash does not change that.
$deep = Copy-Bundle 'verify_deep_json'
$inner = '{"leak":"C:\\fx_deep\\secret.txt"}'
$node = $inner
for ($i = 0; $i -lt 13; $i++) { $node = '[' + $node + ']' }
$deepJson = '{"schema":"saituls.usn-journal-guard.v1","mode":"Diagnose","exitCode":0,"payload":' + $node + '}'
Set-Content -LiteralPath (Join-Path $deep 'guard_snapshot.scrubbed.json') -Value $deepJson -Encoding UTF8
$rehd = Rehash-Member $deep 'guard_snapshot.scrubbed.json'
Check 'deep_json_rehash_refused' ($rehd -and ((Run-Verify $deep) -ne 0)) ''
# Within the supported depth the same token is still caught on content.
$shallow = Copy-Bundle 'verify_shallow_json'
$shallowJson = '{"schema":"saituls.usn-journal-guard.v1","mode":"Diagnose","exitCode":0,"payload":{"leak":"C:\\fx_deep\\secret.txt"}}'
Set-Content -LiteralPath (Join-Path $shallow 'guard_snapshot.scrubbed.json') -Value $shallowJson -Encoding UTF8
$rehsh = Rehash-Member $shallow 'guard_snapshot.scrubbed.json'
Check 'shallow_json_rehash_refused' ($rehsh -and ((Run-Verify $shallow) -ne 0)) ''
# Existing canonical JSON shapes (depth <= 10) still verify.
Check 'canonical_json_still_verifies' ((Run-Verify $out) -eq 0) ''

Write-Host ''
if ($fails -eq 0) {
    if (-not $KeepSandbox) { Remove-Item -LiteralPath $Sandbox -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Host "USN diagnostic bundle: all checks PASS (sandbox removed: $(-not (Test-Path -LiteralPath $Sandbox)))"
    exit 0
}
Write-Host "USN diagnostic bundle: $fails check(s) FAILED (sandbox kept for diagnosis: $Sandbox)"
exit 1
