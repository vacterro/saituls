param(
    [string]$GuardSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Scripts\host\UsnJournalGuard.ps1'),
    [string]$Sandbox = (Join-Path $env:TEMP ('saituls_usnguard_' + [Guid]::NewGuid().ToString('N'))),
    [string]$GuardHost = ''
)

$ErrorActionPreference = 'Stop'

# Regression suite for the USN journal guard (post-P0 stabilization).
#
# The defects this suite pins down:
#   * VerifyOnly was documented as the default but the default run MUTATED the
#     registry, and an unhealthy journal set still exited 0 -- a scheduled task
#     could therefore report success while everything was being rebuilt.
#   * Repair mode never checked an fsutil exit code before claiming success.
#   * Journal size parsing depended on the English label 'Maximum Size'.
#   * The post-reboot helper fell back to deleting the journal whenever a resize
#     was not visible immediately, which forces a full volume rescan for every
#     USN consumer.
#
# Nothing here touches the real registry policy, a real journal, or a real
# fsutil: every scenario runs the REAL guard under the REQUESTED host
# (-GuardHost; powershell for 5.1, pwsh for 7) with -FsutilPath pointed at a
# recording fake, -Volume pinned, a scratch HKCU key for the clamp value and a
# sandbox log. The fake records its own argv, so 'no destructive call happened'
# is proved by the absence of a deletejournal line, not by the guard's own
# report. The guard reports its child runtime (psVersion) and the suite
# asserts it matches the requested host, so CI cannot claim dual-version
# coverage while both suites drive the same runtime.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_usn_journal_guard.ps1
# Exit: 0 = all PASS, 1 = failures.

$fails = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

if (-not (Test-Path -LiteralPath $GuardSource)) { Write-Host "FAIL  guard source missing: $GuardSource"; exit 1 }
New-Item -ItemType Directory -Path $Sandbox -Force | Out-Null

# Controlled child-host selection: the guard must run under the REQUESTED
# runtime, not whatever `powershell` happens to resolve to. CI passes
# -GuardHost explicitly (powershell for 5.1, pwsh for 7); default is the
# runtime executing this suite. An unavailable host is a loud failure, never
# a silent same-runtime substitution.
$WantedHost = $GuardHost
if (-not $WantedHost) {
    if ($PSVersionTable.PSVersion.Major -ge 6) { $WantedHost = 'pwsh' } else { $WantedHost = 'powershell' }
}
$WantedMajor = if ($WantedHost -match 'pwsh') { 7 } else { 5 }
$HostCmd = Get-Command $WantedHost -ErrorAction SilentlyContinue
if (-not $HostCmd) { Write-Host "FAIL  requested guard host unavailable: $WantedHost (no fallback allowed)"; exit 1 }
$GuardExe = $HostCmd.Source
Write-Host "guard host: $GuardExe (requested major $WantedMajor; suite runs on $($PSVersionTable.PSVersion))"

$scratchKey = 'HKCU:\Software\_SAITULS_USN_GUARD_TEST_' + [Guid]::NewGuid().ToString('N')
New-Item -Path $scratchKey -Force | Out-Null
$clampName = 'NtfsAllowUsnMinSize1Mb'

# ---- the recording fake fsutil -------------------------------------------
$mockDir = Join-Path $Sandbox 'mock'
New-Item -ItemType Directory -Path $mockDir -Force | Out-Null

$mockCmd = @'
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0mock-fsutil.ps1" %*
exit /b %ERRORLEVEL%
'@
Set-Content -LiteralPath (Join-Path $mockDir 'fsutil.cmd') -Value $mockCmd -Encoding ASCII

$mockPs1 = @'
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $PSCommandPath
$cfg = Get-Content -Raw -LiteralPath (Join-Path $dir 'config.json') | ConvertFrom-Json
Add-Content -LiteralPath (Join-Path $dir 'calls.log') -Value ($args -join ' ')
$sub = ''
if ($args.Count -ge 2) { $sub = [string]$args[1] }

function Emit($text, $code) {
    if ($text) { foreach ($line in ($text -split "`n")) { Write-Output $line } }
    exit [int]$code
}

if ($sub -eq 'queryjournal') {
    $seq = @($cfg.querysequence)
    if ($seq.Count -eq 0) { $seq = @([pscustomobject]@{ exit = 0; output = '' }) }
    $countFile = Join-Path $dir 'qcount.txt'
    $n = 0
    if (Test-Path -LiteralPath $countFile) { $n = [int](Get-Content -Raw -LiteralPath $countFile) }
    $idx = [math]::Min($n, $seq.Count - 1)
    Set-Content -LiteralPath $countFile -Value ($n + 1)
    $item = $seq[$idx]
    Emit $item.output $item.exit
}
if ($sub -eq 'createjournal') { Emit $cfg.createjournal.output $cfg.createjournal.exit }
if ($sub -eq 'deletejournal') { Emit $cfg.deletejournal.output $cfg.deletejournal.exit }
Emit '' 0
'@
Set-Content -LiteralPath (Join-Path $mockDir 'mock-fsutil.ps1') -Value $mockPs1 -Encoding UTF8

# Canned fsutil output. Field order is the real one: Journal ID, First Usn,
# Next Usn, Lowest Valid Usn, Max Usn, Maximum Size, Allocation Delta.
$headerFull = 'Usn Journal ID   : 0x01dd584dbc8cc571|First Usn        : 0x00000000003c0000|Next Usn         : 0x00000000004e9898|Lowest Valid Usn : 0x0000000000000000|Max Usn          : 0x7fffffffffff0000'
$english1mb = ($headerFull + '|Maximum Size     : 0x0000000000100000 (  1,0 MB)|Allocation Delta : 0x0000000000040000 (256,0 KB)') -replace '\|', "`n"
$english128mb = ($headerFull + '|Maximum Size     : 0x0000000008000000 (128,0 MB)|Allocation Delta : 0x0000000000800000 (  8,0 MB)') -replace '\|', "`n"
# Same field order, translated labels, localized decimal comma and unit text:
# the old parser, which matched the English string 'Maximum Size', reads this
# as "query failed" and the guard would have been blind on such a host.
$localized1mb = ('Faili ID           : 0x01dd584dbc8cc571|Esimene USN        : 0x00000000003c0000|Jargmine USN       : 0x00000000004e9898|Vahim lubatud USN  : 0x0000000000000000|Maksimaalne USN    : 0x7fffffffffff0000|Suurim suurus      : 0x0000000000100000 (  1,0 MB)|Eralduse delta     : 0x0000000000040000 (256,0 KB)') -replace '\|', "`n"
$localized128mb = ($localized1mb -replace '0x0000000000100000', '0x0000000008000000')
$absent = ((
    'Usn Journal ID   : 0x0000000000000000|First Usn        : 0x0000000000000000|Next Usn         : 0x0000000000000000|Lowest Valid Usn : 0x0000000000000000|Max Usn          : 0x7fffffffffff0000|Maximum Size     : 0x0000000000000000 (  0,0 KB)|Allocation Delta : 0x0000000000000000 (  0,0 KB)') -replace '\|', "`n")

function New-Mock {
    param([string]$Name, [object[]]$QuerySequence, [object]$Create = $null, [object]$Delete = $null)
    if (-not $Create) { $Create = [pscustomobject]@{ exit = 0; output = '' } }
    if (-not $Delete) { $Delete = [pscustomobject]@{ exit = 0; output = '' } }
    $dir = Join-Path $mockDir $Name
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $mockDir 'fsutil.cmd') -Destination (Join-Path $dir 'fsutil.cmd') -Force
    Copy-Item -LiteralPath (Join-Path $mockDir 'mock-fsutil.ps1') -Destination (Join-Path $dir 'mock-fsutil.ps1') -Force
    $cfg = [ordered]@{ querysequence = @($QuerySequence); createjournal = $Create; deletejournal = $Delete }
    Set-Content -LiteralPath (Join-Path $dir 'config.json') -Value ($cfg | ConvertTo-Json -Depth 5) -Encoding UTF8
    foreach ($stale in 'calls.log', 'qcount.txt') {
        $p = Join-Path $dir $stale
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }
    }
    return (Join-Path $dir 'fsutil.cmd')
}

function Step([int]$exit, [string]$output) { return [pscustomobject]@{ exit = $exit; output = $output } }

function Invoke-Guard {
    param([string]$Mock, [string[]]$Extra = @(), [switch]$ExpectRaw, [string]$RegKey = '', [hashtable]$Replace = @{}, [string]$Volumes = 'C', [string]$Source = $GuardSource)
    $stamp = [Guid]::NewGuid().ToString('N')
    $stdout = Join-Path $Sandbox "out_$stamp.txt"
    $stderr = Join-Path $Sandbox "err_$stamp.txt"
    $log = Join-Path $Sandbox "guard_$stamp.log"
    $reg = if ($RegKey) { $RegKey } else { $scratchKey }
    $base = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Source,
        '-Volume', $Volumes, '-FsutilPath', $Mock, '-RegistryKey', $reg,
        '-LogPath', $log, '-Json', '-ResizeWaitSeconds', '10', '-ResizePollSeconds', '0')
    foreach ($k in $Replace.Keys) {
        $i = [Array]::IndexOf($base, $k)
        if ($i -ge 0) { $base[$i + 1] = [string]$Replace[$k] } else { $base += @($k, [string]$Replace[$k]) }
    }
    & $GuardExe @base @Extra 1> $stdout 2> $stderr
    $code = $LASTEXITCODE
    $text = if (Test-Path -LiteralPath $stdout) { (Get-Content -Raw -LiteralPath $stdout) } else { '' }
    $report = $null
    if (-not $ExpectRaw -and $text) {
        try { $report = $text | ConvertFrom-Json } catch { $report = $null }
    }
    $errText = ''
    if (Test-Path -LiteralPath $stderr) { $errText = (Get-Content -Raw -LiteralPath $stderr) }
    $logText = ''
    if (Test-Path -LiteralPath $log) { $logText = (Get-Content -Raw -LiteralPath $log) }
    return [pscustomobject]@{
        Code = $code
        Stdout = $text
        Stderr = $errText
        Log = $logText
        Report = $report
    }
}

function Get-Calls([string]$Mock) {
    $log = Join-Path (Split-Path -Parent $Mock) 'calls.log'
    if (-not (Test-Path -LiteralPath $log)) { return @() }
    return @(Get-Content -LiteralPath $log)
}

function Get-ProblemText($Report) {
    if (-not $Report) { return '' }
    return (@($Report.problems) -join ' ;; ')
}

# ---- 1. healthy journal: no action, exit 0 -------------------------------
$mock = New-Mock -Name 'healthy' -QuerySequence @((Step 0 $english128mb))
$r = Invoke-Guard -Mock $mock
$calls = Get-Calls $mock
Check 'healthy_journal_exits_0' ($r.Code -eq 0) "exit=$($r.Code)"
Check 'healthy_journal_reported_healthy' ($r.Report -and $r.Report.volumes[0].healthy -eq $true) "json=$($r.Stdout -replace "`r?`n", ' ')"
Check 'healthy_journal_not_mutated' ((@($calls | Where-Object { $_ -match 'createjournal|deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"
Check 'child_runtime_matches_requested_host' ($r.Report -and [string]$r.Report.psVersion -match "^$WantedMajor\." ) "child=$($r.Report.psVersion) wanted=$WantedMajor"

# ---- 2. the regression that mattered: unhealthy MUST NOT exit 0 ----------
$mock = New-Mock -Name 'clamped1mb' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock
$calls = Get-Calls $mock
Check 'unhealthy_default_exits_nonzero' ($r.Code -eq 1) "exit=$($r.Code)"
Check 'unhealthy_reports_the_volume' ((Get-ProblemText $r.Report) -match 'C:') "problems=$(Get-ProblemText $r.Report)"
Check 'diagnose_is_read_only' ((@($calls | Where-Object { $_ -match 'createjournal|deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"
$r2 = Invoke-Guard -Mock $mock -Extra @('-VerifyOnly')
Check 'legacy_verifyonly_is_read_only' ($r2.Code -eq 1 -and $r2.Report -and @($r2.Report.actions).Count -eq 0) "exit=$($r2.Code)"

# ---- 3. localized fsutil output -----------------------------------------
$mock = New-Mock -Name 'localized_healthy' -QuerySequence @((Step 0 $localized128mb))
$r = Invoke-Guard -Mock $mock
Check 'localized_healthy_parsed_as_128mb' ($r.Code -eq 0 -and $r.Report.volumes[0].maxBytes -eq 134217728) "exit=$($r.Code) max=$($r.Report.volumes[0].maxBytes)"
$mock = New-Mock -Name 'localized_clamped' -QuerySequence @((Step 0 $localized1mb))
$r = Invoke-Guard -Mock $mock
Check 'localized_clamped_parsed_as_1mb' ($r.Code -eq 1 -and $r.Report.volumes[0].maxBytes -eq 1048576) "exit=$($r.Code) max=$($r.Report.volumes[0].maxBytes)"

# ---- 4. output that cannot be read: refuse, never mutate ----------------
$mock = New-Mock -Name 'garbled' -QuerySequence @((Step 0 'fsutil is having a bad day'))
$r = Invoke-Guard -Mock $mock
$calls = Get-Calls $mock
Check 'garbled_output_exits_2' ($r.Code -eq 2) "exit=$($r.Code)"
Check 'garbled_output_not_mutated' ((@($calls | Where-Object { $_ -match 'createjournal|deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"
$mock = New-Mock -Name 'truncated' -QuerySequence @((Step 0 ($headerFull -replace '\|', "`n")))
$r = Invoke-Guard -Mock $mock
Check 'truncated_field_block_exits_2' ($r.Code -eq 2) "exit=$($r.Code) problems=$(Get-ProblemText $r.Report)"

# ---- 5. permission error: report the code, do not repair ----------------
$mock = New-Mock -Name 'denied' -QuerySequence @((Step 5 'Access is denied.'))
$r = Invoke-Guard -Mock $mock
$calls = Get-Calls $mock
Check 'permission_error_exits_nonzero' ($r.Code -eq 2 -or $r.Code -eq 3) "exit=$($r.Code)"
Check 'permission_error_names_the_exit_code' ((Get-ProblemText $r.Report) -match '5') "problems=$(Get-ProblemText $r.Report)"
Check 'permission_error_not_mutated' ((@($calls | Where-Object { $_ -match 'createjournal|deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"

# ---- 6. absent journal --------------------------------------------------
$mock = New-Mock -Name 'absent' -QuerySequence @((Step 0 $absent))
$r = Invoke-Guard -Mock $mock
Check 'absent_journal_reported_unhealthy' ($r.Code -eq 1 -and $r.Report.volumes[0].state -eq 'Absent') "exit=$($r.Code) state=$($r.Report.volumes[0].state)"
Check 'absent_journal_not_deleted' ((@(Get-Calls $mock | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$((Get-Calls $mock) -join ' / ')]"
$mock = New-Mock -Name 'absent_repair' -QuerySequence @((Step 0 $absent), (Step 0 $english128mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals')
$calls = Get-Calls $mock
Check 'absent_journal_repaired_nondestructively' ($r.Code -eq 0 -and (@($calls | Where-Object { $_ -match 'createjournal' }).Count) -eq 1) "exit=$($r.Code) calls=[$($calls -join ' / ')]"
Check 'absent_journal_repair_never_deletes' ((@($calls | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"

# ---- 7. delayed resize visibility --------------------------------------
$mock = New-Mock -Name 'delayed' -QuerySequence @((Step 0 $english1mb), (Step 0 $english1mb), (Step 0 $english128mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals')
$calls = Get-Calls $mock
Check 'delayed_resize_is_polled_to_success' ($r.Code -eq 0 -and $r.Report.volumes[0].healthy -eq $true) "exit=$($r.Code) log=$($r.Log -replace "`r?`n", ' / ')"
Check 'delayed_resize_queried_more_than_once' ((@($calls | Where-Object { $_ -match 'queryjournal' }).Count) -ge 3) "queries=$((@($calls | Where-Object { $_ -match 'queryjournal' }).Count))"
Check 'delayed_resize_never_deletes' ((@($calls | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"

# ---- 8. resize that never becomes visible ------------------------------
$mock = New-Mock -Name 'stuck' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals')
$calls = Get-Calls $mock
Check 'stuck_resize_exits_unhealthy' ($r.Code -eq 1) "exit=$($r.Code)"
Check 'stuck_resize_reports_not_visible' ((Get-ProblemText $r.Report) -match 'not visible') "problems=$(Get-ProblemText $r.Report)"
Check 'stuck_resize_does_not_delete_without_authorization' ((@($calls | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"
Check 'stuck_resize_says_delete_needs_authorization' ($r.Log -match 'AllowJournalRecreate') "log=$($r.Log -replace "`r?`n", ' / ')"

# ---- 9. destructive fallback only under explicit authorization ---------
$mock = New-Mock -Name 'authorized' -QuerySequence @((Step 0 $english1mb), (Step 0 $english1mb), (Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
$calls = Get-Calls $mock
Check 'authorized_recreate_deletes_once' ((@($calls | Where-Object { $_ -match 'deletejournal /n C:' }).Count) -eq 1) "calls=[$($calls -join ' / ')]"
Check 'authorized_recreate_warns_in_the_log' ($r.Log -match 'recreating the journal') "log=$($r.Log -replace "`r?`n", ' / ')"
Check 'authorized_recreate_still_reports_failure' ($r.Code -eq 1) "exit=$($r.Code)"

$mock = New-Mock -Name 'authorized_recover' -QuerySequence @((Step 0 $english1mb), (Step 0 $english1mb), (Step 0 $english1mb), (Step 0 $english128mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
Check 'authorized_recreate_can_succeed' ($r.Code -eq 0 -and $r.Report.volumes[0].healthy -eq $true) "exit=$($r.Code)"

# ---- 10. authorization cannot be given without a repair -----------------
$mock = New-Mock -Name 'authorization_alone' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Extra @('-AllowJournalRecreate') -ExpectRaw
Check 'authorization_alone_is_rejected' ($r.Code -eq 2) "exit=$($r.Code)"
Check 'authorization_alone_runs_no_fsutil' ((Get-Calls $mock).Count -eq 0) "calls=[$((Get-Calls $mock) -join ' / ')]"
$r = Invoke-Guard -Mock $mock -Extra @('-VerifyOnly', '-CorrectRegistry') -ExpectRaw
Check 'verifyonly_with_mutation_is_rejected' ($r.Code -eq 2) "exit=$($r.Code)"

# ---- 11. a healthy journal is never touched by a repair run -------------
$mock = New-Mock -Name 'repair_skips_healthy' -QuerySequence @((Step 0 $english128mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals')
$calls = Get-Calls $mock
Check 'repair_skips_healthy_journal' ($r.Code -eq 0 -and (@($calls | Where-Object { $_ -match 'createjournal|deletejournal' }).Count) -eq 0) "exit=$($r.Code) calls=[$($calls -join ' / ')]"

# ---- 12. failed operation is a failure, not success --------------------
$mock = New-Mock -Name 'create_fails' -QuerySequence @((Step 0 $english1mb)) -Create (Step 1 'Access is denied.')
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals')
$calls = Get-Calls $mock
Check 'failed_createjournal_exits_nonzero' ($r.Code -eq 2 -or $r.Code -eq 3) "exit=$($r.Code)"
Check 'failed_createjournal_is_reported' ((Get-ProblemText $r.Report) -match 'createjournal failed') "problems=$(Get-ProblemText $r.Report)"
Check 'failed_createjournal_does_not_delete' ((@($calls | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"

# The hosted runner can be elevated even when the local test process is not.
# Exercise both classification branches without elevation or real fsutil:
# only the admin fact in a sandbox copy changes; every operation still uses
# the recording fake and scratch registry key. Otherwise exit 3 on a local
# unelevated host can hide a missing ToolFailed flag (hosted exit 1).
$guardText = [System.IO.File]::ReadAllText($GuardSource)
$adminAssignment = '$script:IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)'
if ([regex]::Matches($guardText, [regex]::Escape($adminAssignment)).Count -ne 1) {
    throw 'admin classification fixture must replace exactly one native admin fact'
}
foreach ($adminFact in @($true, $false)) {
    $label = if ($adminFact) { 'elevated' } else { 'unelevated' }
    $expectedCode = if ($adminFact) { 2 } else { 3 }
    $factLiteral = if ($adminFact) { '$true' } else { '$false' }
    $fixtureSource = Join-Path $Sandbox ("guard_$label.ps1")
    [System.IO.File]::WriteAllText($fixtureSource,
        $guardText.Replace($adminAssignment, ('$script:IsAdmin = ' + $factLiteral)),
        (New-Object System.Text.UTF8Encoding($true)))

    $mock = New-Mock -Name ("create_fails_$label") -QuerySequence @((Step 0 $english1mb)) -Create (Step 1 'Access is denied.')
    $r = Invoke-Guard -Mock $mock -Source $fixtureSource -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
    $calls = Get-Calls $mock
    Check "failed_createjournal_${label}_exit_class" ($r.Code -eq $expectedCode -and $r.Report -and $r.Report.exitCode -eq $expectedCode -and $r.Report.elevated -eq $adminFact) "exit=$($r.Code) expected=$expectedCode"
    Check "failed_createjournal_${label}_no_delete" (@($calls | Where-Object { $_ -match 'deletejournal' }).Count -eq 0 -and @($calls | Where-Object { $_ -match 'createjournal' }).Count -eq 1) "calls=[$($calls -join ' / ')]"

    $mock = New-Mock -Name ("delete_fails_$label") -QuerySequence @((Step 0 $english1mb)) -Delete (Step 1 'Access is denied.')
    $r = Invoke-Guard -Mock $mock -Source $fixtureSource -Replace @{ '-ResizeWaitSeconds' = '0' } -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
    $calls = Get-Calls $mock
    Check "failed_deletejournal_${label}_exit_class" ($r.Code -eq $expectedCode -and $r.Report -and $r.Report.exitCode -eq $expectedCode -and $r.Report.elevated -eq $adminFact) "exit=$($r.Code) expected=$expectedCode"
    Check "failed_deletejournal_${label}_reported" ((Get-ProblemText $r.Report) -match 'deletejournal failed') "problems=$(Get-ProblemText $r.Report)"
    Check "failed_deletejournal_${label}_no_followup_create" (@($calls | Where-Object { $_ -match 'deletejournal' }).Count -eq 1 -and @($calls | Where-Object { $_ -match 'createjournal' }).Count -eq 1) "calls=[$($calls -join ' / ')]"
}

# ---- 13. registry clamp: read-only by default, removed only on request --
Set-ItemProperty -Path $scratchKey -Name $clampName -Value 1 -Type DWord
$mock = New-Mock -Name 'clamp_diagnose' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock
$stillThere = (Get-ItemProperty -Path $scratchKey -Name $clampName -ErrorAction SilentlyContinue)
Check 'diagnose_does_not_touch_the_registry' ($r.Code -eq 1 -and $null -ne $stillThere) "exit=$($r.Code) present=$($null -ne $stillThere)"
Check 'diagnose_reports_the_clamp' ((Get-ProblemText $r.Report) -match 'NtfsAllowUsnMinSize1Mb') "problems=$(Get-ProblemText $r.Report)"

$mock = New-Mock -Name 'clamp_repair' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Extra @('-CorrectRegistry')
$after = (Get-ItemProperty -Path $scratchKey -Name $clampName -ErrorAction SilentlyContinue)
$calls = Get-Calls $mock
Check 'correct_registry_removes_the_value' ($null -eq $after) "value after=$($after.$clampName)"
Check 'correct_registry_requires_a_reboot' ($r.Report.rebootRequired -eq $true -and $r.Log -match 'REBOOT REQUIRED') "exit=$($r.Code)"
Check 'correct_registry_skips_the_futile_resize' ((@($calls | Where-Object { $_ -match 'createjournal|deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"
Check 'correct_registry_still_exits_unhealthy' ($r.Code -eq 1) "exit=$($r.Code)"

Set-ItemProperty -Path $scratchKey -Name $clampName -Value 1 -Type DWord
$mock = New-Mock -Name 'clamp_repair_both' -QuerySequence @((Step 0 $english1mb), (Step 0 $english128mb))
$r = Invoke-Guard -Mock $mock -Extra @('-CorrectRegistry', '-RepairJournals')
$calls = Get-Calls $mock
Check 'clamped_run_removes_value_and_reports_reboot' ($null -eq (Get-ItemProperty -Path $scratchKey -Name $clampName -ErrorAction SilentlyContinue)) "exit=$($r.Code)"
Check 'clamped_run_does_not_resize_while_cached' ((@($calls | Where-Object { $_ -match 'createjournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"

# ---- 14. installed task definition --------------------------------------
$mock = New-Mock -Name 'unused' -QuerySequence @((Step 0 $english128mb))
$r = Invoke-Guard -Mock $mock -Extra @('-InstallPlan')
$plan = $r.Report
Check 'install_plan_is_read_only' ($r.Code -eq 0 -and $plan.taskPath -eq '\SAITULS\') "exit=$($r.Code)"
Check 'install_plan_corrects_the_registry' ($plan.action.arguments -match '-CorrectRegistry') "args=$($plan.action.arguments)"
Check 'install_plan_never_repairs_journals' (($plan.action.arguments -notmatch 'RepairJournals') -and ($plan.action.arguments -notmatch 'AllowJournalRecreate')) "args=$($plan.action.arguments)"
Check 'install_plan_triggers_boot_and_daily' ((@($plan.triggers | ForEach-Object { $_.type }) -join ',') -eq 'boot,daily') "triggers=$(@($plan.triggers | ForEach-Object { $_.type }) -join ',')"
Check 'install_plan_runs_as_system' ($plan.principal.userId -eq 'SYSTEM' -and $plan.settings.multipleInstances -eq 'IgnoreNew') "principal=$($plan.principal.userId)"

# ---- 15. full 64-bit field range (no signed-overflow assumption) ---------
$bigHealthy = ($english128mb -replace '0x7fffffffffff0000', '0xFFFFFFFFFFFF0000')
$mock = New-Mock -Name 'u64_healthy' -QuerySequence @((Step 0 $bigHealthy))
$r = Invoke-Guard -Mock $mock
Check 'u64_maxusn_healthy_parsed' ($r.Code -eq 0 -and $r.Report.volumes[0].maxBytes -eq 134217728) "exit=$($r.Code) max=$($r.Report.volumes[0].maxBytes)"
$bigId = ($english1mb -replace '0x01dd584dbc8cc571', '0xFFFFFFFFFFFFFFFF')
$mock = New-Mock -Name 'u64_id' -QuerySequence @((Step 0 $bigId))
$r = Invoke-Guard -Mock $mock
Check 'u64_journal_id_clamped_parsed' ($r.Code -eq 1 -and $r.Report.volumes[0].maxBytes -eq 1048576) "exit=$($r.Code) max=$($r.Report.volumes[0].maxBytes)"

# ---- 16. missing registry key is a read failure, not a healthy absence ---
$mock = New-Mock -Name 'nokey' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -RegKey ($scratchKey + '_MISSING_' + [Guid]::NewGuid().ToString('N'))
$calls = Get-Calls $mock
Check 'missing_registry_key_exits_2' ($r.Code -eq 2) "exit=$($r.Code)"
Check 'missing_registry_key_names_registry' ((Get-ProblemText $r.Report) -match 'registry') "problems=$(Get-ProblemText $r.Report)"
Check 'missing_registry_key_not_mutated' ((@($calls | Where-Object { $_ -match 'createjournal|deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"
$mock = New-Mock -Name 'nokey_repair' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -RegKey ($scratchKey + '_MISSING2') -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
Check 'missing_registry_key_blocks_authorized_repair' ($r.Code -eq 2 -and (@(Get-Calls $mock | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "exit=$($r.Code)"

# ---- 17. invalid numeric parameters fail before any mutation -------------
$mock = New-Mock -Name 'badparams' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-MinJournalMB', '0') -ExpectRaw
Check 'zero_min_rejected' ($r.Code -eq 2) "exit=$($r.Code)"
Check 'zero_min_runs_no_fsutil' ((Get-Calls $mock).Count -eq 0) "calls=[$((Get-Calls $mock) -join ' / ')]"
$mock = New-Mock -Name 'badparams2' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-TargetJournalMB', '8', '-MinJournalMB', '32') -ExpectRaw
Check 'target_below_min_rejected' ($r.Code -eq 2 -and (Get-Calls $mock).Count -eq 0) "exit=$($r.Code)"
$mock = New-Mock -Name 'badparams3' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals') -Replace @{ '-ResizeWaitSeconds' = '-1' } -ExpectRaw
Check 'negative_wait_rejected' ($r.Code -eq 2 -and (Get-Calls $mock).Count -eq 0) "exit=$($r.Code)"

# ---- 18. authorized recreate refused when the resize never issued --------
$mock = New-Mock -Name 'recreate_refused_createfail' -QuerySequence @((Step 0 $english1mb)) -Create (Step 1 'Access is denied.')
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
$calls = Get-Calls $mock
Check 'recreate_refused_after_permission_failure' ((@($calls | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"
Check 'recreate_refusal_says_why' ($r.Log -match 'recreate refused') "log=$($r.Log -replace "`r?`n", ' / ')"
Check 'recreate_refusal_nonzero' ($r.Code -ne 0) "exit=$($r.Code)"

# ---- 19. authorized recreate refused when follow-up is unreadable --------
$mock = New-Mock -Name 'recreate_refused_unreadable' -QuerySequence @((Step 0 $english1mb), (Step 5 'Access is denied.'))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
$calls = Get-Calls $mock
Check 'recreate_refused_after_unreadable_followup' ((@($calls | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"
Check 'unreadable_followup_nonzero' ($r.Code -ne 0) "exit=$($r.Code)"

# ---- 20. resize must reach the requested target, not just the minimum ----
# A landing at 32 MB with a 128 MB target is HEALTHY (minimum met) but the
# repair is incomplete: healthy=true, targetMet=false, targetUnmet=true,
# exit 1 with a separate target problem. Never a deletion reason.
$english32mb = ($english1mb -replace '0x0000000000100000', '0x0000000002000000')
$mock = New-Mock -Name 'below_target' -QuerySequence @((Step 0 $english1mb), (Step 0 $english32mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals')
$calls = Get-Calls $mock
Check 'resize_below_target_is_incomplete' ($r.Code -eq 1) "exit=$($r.Code)"
Check 'resize_below_target_truthful_health' ($r.Report.volumes[0].healthy -eq $true -and $r.Report.volumes[0].maxBytes -eq 33554432) "healthy=$($r.Report.volumes[0].healthy) max=$($r.Report.volumes[0].maxBytes)"
Check 'resize_below_target_reports_target' ($r.Report.volumes[0].targetMet -ne $true -and $r.Report.targetUnmet -eq $true -and ((Get-ProblemText $r.Report) -match 'below requested target')) "problems=$(Get-ProblemText $r.Report)"
Check 'resize_below_target_never_deletes' ((@($calls | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"

# ---- 21. healthy-after-partial-resize is never recreated (the P1 defect) --
# 1 MiB -> 32 MiB with a 128 MiB target, recreate authorized for C: the fresh
# re-read proves healthy (>= 32 MB minimum), so zero deletejournal calls.
$mock = New-Mock -Name 'partial_no_recreate' -QuerySequence @((Step 0 $english1mb), (Step 0 $english32mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
$calls = Get-Calls $mock
Check 'partial_resize_authorized_never_deletes_healthy' ((@($calls | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"
Check 'partial_resize_authorized_truthful' ($r.Report.volumes[0].healthy -eq $true -and $r.Report.volumes[0].targetMet -ne $true -and $r.Report.targetUnmet -eq $true) "healthy=$($r.Report.volumes[0].healthy) targetMet=$($r.Report.volumes[0].targetMet)"
Check 'partial_resize_authorized_refusal_logged' ($r.Log -match 'already healthy' -or (Get-ProblemText $r.Report) -match 'already healthy') "problems=$(Get-ProblemText $r.Report)"
Check 'partial_resize_authorized_incomplete' ($r.Code -eq 1) "exit=$($r.Code)"

# ---- 22. full resize to target succeeds with zero deletions --------------
$mock = New-Mock -Name 'full_success' -QuerySequence @((Step 0 $english1mb), (Step 0 $english128mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
$calls = Get-Calls $mock
Check 'full_resize_success' ($r.Code -eq 0 -and $r.Report.volumes[0].healthy -eq $true -and $r.Report.volumes[0].targetMet -eq $true) "exit=$($r.Code)"
Check 'full_resize_zero_deletions' ((@($calls | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"

# ---- 23. unreadable fresh verification blocks deletion -------------------
$mock = New-Mock -Name 'unreadable_fresh' -QuerySequence @((Step 0 $english1mb), (Step 5 'Access is denied.'))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
$calls = Get-Calls $mock
Check 'unreadable_fresh_zero_deletions' ((@($calls | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$($calls -join ' / ')]"
Check 'unreadable_fresh_nonzero' ($r.Code -ne 0) "exit=$($r.Code)"

# ---- 24. single-volume destructive auth never crosses volumes ------------
$mock = New-Mock -Name 'xvol' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Volumes 'C,E' -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
$calls = Get-Calls $mock
$delC = @($calls | Where-Object { $_ -match 'deletejournal /n C:' }).Count
$delE = @($calls | Where-Object { $_ -match 'deletejournal /n E:' }).Count
Check 'xvol_no_cross_volume_delete' ($delE -eq 0) "calls=[$($calls -join ' / ')]"
Check 'xvol_both_volumes_reported' ($r.Report.volumes.Count -eq 2) "volumes=$($r.Report.volumes.Count)"
Check 'xvol_scoped_refusal_visible' ($r.Log -match 'NOT authorized for this volume' -or (Get-ProblemText $r.Report) -match 'scoped to volume') "log/problems present"
$mock = New-Mock -Name 'xvol_mismatch' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Volumes 'E' -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'C')
Check 'xvol_mismatch_zero_deletions' ((@(Get-Calls $mock | Where-Object { $_ -match 'deletejournal' }).Count) -eq 0) "calls=[$((Get-Calls $mock) -join ' / ')]"

# ---- 25. invalid or absent destructive confirmation refuses pre-mutation -
$mock = New-Mock -Name 'noconfirm' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-AllowJournalRecreate') -ExpectRaw
Check 'absent_recreate_volume_rejected' ($r.Code -eq 2) "exit=$($r.Code)"
Check 'absent_recreate_volume_no_fsutil' ((Get-Calls $mock).Count -eq 0) "calls=[$((Get-Calls $mock) -join ' / ')]"
$mock = New-Mock -Name 'badconfirm' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals', '-AllowJournalRecreate', '-RecreateVolume', 'ZZ') -ExpectRaw
Check 'invalid_recreate_volume_rejected' ($r.Code -eq 2 -and (Get-Calls $mock).Count -eq 0) "exit=$($r.Code)"
$mock = New-Mock -Name 'confirm_alone' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RecreateVolume', 'C') -ExpectRaw
Check 'recreate_volume_alone_rejected' ($r.Code -eq 2 -and (Get-Calls $mock).Count -eq 0) "exit=$($r.Code)"

# ---- 26. JSON report describes the same snapshot as the exit code --------
$mock = New-Mock -Name 'json_ok' -QuerySequence @((Step 0 $english128mb))
$r = Invoke-Guard -Mock $mock
Check 'json_exit_matches_code' ($r.Report -and $r.Report.exitCode -eq $r.Code) "code=$($r.Code) json=$($r.Report.exitCode)"
Check 'json_registry_readfailed_explicit' ($r.Report -and $r.Report.registry.readFailed -eq $false) "readFailed=$($r.Report.registry.readFailed)"
$mock = New-Mock -Name 'json_nokey' -QuerySequence @((Step 0 $english1mb))
$r = Invoke-Guard -Mock $mock -RegKey ($scratchKey + '_MISSING_JSON_' + [Guid]::NewGuid().ToString('N'))
Check 'json_unknown_registry_matches_exit_2' ($r.Code -eq 2 -and $r.Report.exitCode -eq 2 -and $r.Report.registry.readFailed -eq $true) "code=$($r.Code) json=$($r.Report.exitCode) readFailed=$($r.Report.registry.readFailed)"
$mock = New-Mock -Name 'json_target' -QuerySequence @((Step 0 $english1mb), (Step 0 $english32mb))
$r = Invoke-Guard -Mock $mock -Extra @('-RepairJournals')
Check 'json_target_unmet_consistent' ($r.Code -eq 1 -and $r.Report.exitCode -eq 1 -and $r.Report.targetUnmet -eq $true -and $r.Report.volumes[0].healthy -eq $true -and $r.Report.volumes[0].targetMet -eq $false) "code=$($r.Code)"

# ---- 27. machine stdout contract: -Quiet keeps stdout parseable ----------
# Export-UsnDiagnosticBundle.ps1 parses the guard's stdout. Without -Quiet the
# human log echo (Write-Host 'task plan: {compact}') is written before the real
# -InstallPlan document, so stdout is two concatenated JSON documents and no
# single-document parse is possible -- the bundle then honestly reports a
# partial task plan. -Quiet makes the non-JSON machine modes parseable; -Json
# already implies it, which is why only -InstallPlan needed the switch.
$mock = New-Mock -Name 'quiet_plan' -QuerySequence @((Step 0 $english128mb))
$r = Invoke-Guard -Mock $mock -Extra @('-InstallPlan', '-Quiet') -ExpectRaw
$quietParsed = $null
try { $quietParsed = $r.Stdout | ConvertFrom-Json } catch { $quietParsed = $null }
Check 'install_plan_quiet_stdout_parses' ($r.Code -eq 0 -and $null -ne $quietParsed -and $quietParsed.taskPath -eq '\SAITULS\') "exit=$($r.Code)"
Check 'install_plan_quiet_has_no_echo' ($r.Stdout -notmatch 'task plan:') ''
Check 'install_plan_quiet_log_still_written' ($r.Log -match 'task plan') 'quiet suppresses the echo, never the log file'
$r2 = Invoke-Guard -Mock $mock -Extra @('-InstallPlan') -ExpectRaw
$r2Parsed = $null
try { $r2Parsed = $r2.Stdout | ConvertFrom-Json } catch { $r2Parsed = $null }
Check 'install_plan_json_without_quiet_still_parses' ($r2.Code -eq 0 -and $null -ne $r2Parsed) 'Json implies Quiet: no human echo before the machine document'
$plainOut = Join-Path $Sandbox "plain_$([Guid]::NewGuid().ToString('N')).txt"
$plainLog = Join-Path $Sandbox "plain_$([Guid]::NewGuid().ToString('N')).log"
& $GuardExe -NoProfile -ExecutionPolicy Bypass -File $GuardSource -Volume C -FsutilPath $mock `
    -RegistryKey $scratchKey -LogPath $plainLog -InstallPlan 1> $plainOut 2> $null
$plainText = if (Test-Path -LiteralPath $plainOut) { Get-Content -Raw -LiteralPath $plainOut } else { '' }
Check 'install_plan_human_mode_echoes_to_stdout' ($plainText -match 'task plan:') 'no -Json/-Quiet: the human echo is still there, so -Json/-Quiet is what removes it'

# ---- cleanup ------------------------------------------------------------
Remove-Item -Path $scratchKey -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ''
if ($fails -eq 0) {
    Write-Host "USN journal guard: all checks PASS (sandbox $Sandbox)"
    exit 0
}
Write-Host "USN journal guard: $fails check(s) FAILED (sandbox $Sandbox)"
exit 1
