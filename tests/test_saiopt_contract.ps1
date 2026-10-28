<#
    Architectural contract for SAIOPT, the background optimizer.

    Source-shape assertions on code with comments stripped, so a note never
    passes or fails a check. They pin what SAIOPT is allowed to be:

      1. The logic is pure (OptLogic.cs: no Win32, no I/O, no clock).
      2. The resident changes exactly two things, each at one call site with
         identity re-verified on the handle: a priority class (and only from
         the class it expects), and -- for a confirmed or armed stale orphan
         -- a process end. Nothing is ended automatically out of the box.
      3. The things "optimizers" do that make PCs worse are absent: no
         working-set trimming or RAM "cleaning", no file deletion outside its
         own state, no services, no HKLM writes, no elevation, no Explorer
         restart, no global primitives.
      4. SAITULS.cs only launches it.

    Behavioural proof: tests\test_saiopt_logic.ps1 (pure rules) and
    SAIOPT.exe --self-test (real machine, own child process only).
#>
$ErrorActionPreference = 'Stop'
$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $testsDir
$dir = Join-Path $root 'Scripts\saiopt'

$script:failures = 0
function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Host "PASS  $Name $Detail" }
    else { Write-Host "FAIL  $Name $Detail"; $script:failures++ }
}

function Get-Code([string]$Path) {
    $text = Get-Content -LiteralPath $Path -Raw
    $text = [regex]::Replace($text, '/\*.*?\*/', '', 'Singleline')
    $text = [regex]::Replace($text, '(?m)^\s*//.*$', '')
    $text = [regex]::Replace($text, '(?m)(?<=[;{}),])\s*//.*$', '')
    return $text
}

function Get-Block([string]$Code, [string]$Header) {
    $start = $Code.IndexOf($Header)
    if ($start -lt 0) { return '' }
    $open = $Code.IndexOf('{', $start)
    if ($open -lt 0) { return '' }
    $depth = 0
    for ($i = $open; $i -lt $Code.Length; $i++) {
        if ($Code[$i] -eq '{') { $depth++ }
        elseif ($Code[$i] -eq '}') {
            $depth--
            if ($depth -eq 0) { return $Code.Substring($start, $i - $start + 1) }
        }
    }
    return ''
}

function Has-Token([string]$Code, [string]$Token) {
    return $Code -cmatch ('(?<![A-Za-z0-9_])' + [regex]::Escape($Token))
}

function Count-Token([string]$Code, [string]$Token) {
    return ([regex]::Matches($Code, '(?<![A-Za-z0-9_])' + [regex]::Escape($Token))).Count
}

$paths = @{
    logic   = Join-Path $dir 'OptLogic.cs'
    app     = Join-Path $dir 'SAIOPT.cs'
    self    = Join-Path $dir 'SelfTest.cs'
    build   = Join-Path $dir 'build.ps1'
    readme  = Join-Path $dir 'README.md'
    saituls = Join-Path $root 'SAITULS.cs'
}
foreach ($k in $paths.Keys) { Check "source present: $(Split-Path -Leaf $paths[$k])" (Test-Path -LiteralPath $paths[$k]) '' }
if ($script:failures -gt 0) { exit 1 }

$logic = Get-Code $paths.logic
$app = Get-Code $paths.app
$self = Get-Code $paths.self
$saituls = Get-Code $paths.saituls
$all = $logic + "`n" + $app + "`n" + $self

# --- 1. pure logic -------------------------------------------------------------
Check 'OptLogic.cs is pure: no P/Invoke' (-not (Has-Token $logic 'DllImport'))
Check 'OptLogic.cs is pure: no I/O' (-not ($logic -cmatch 'System\.IO|File\.|Directory\.|Registry'))
Check 'OptLogic.cs is pure: no clock' (-not ($logic -cmatch 'DateTime\.Now|DateTime\.UtcNow|Environment\.TickCount|Stopwatch'))
Check 'OptLogic.cs is pure: no process API' (-not ($logic -cmatch 'System\.Diagnostics|Process\.'))

# --- 2. the only two changes, each at one guarded call site --------------------
Check 'SetPriorityClass is called at exactly one site' ((Count-Token $all 'Native.SetPriorityClass(') -eq 1) ("sites=" + (Count-Token $all 'Native.SetPriorityClass('))
$setPri = Get-Block $app 'public static bool SetPriority('
Check 'that site is Procs.SetPriority' ($setPri -cmatch 'Native\.SetPriorityClass\(')
Check 'priority change re-verifies identity on the handle' ($setPri -cmatch 'GetProcessTimes' -and $setPri -cmatch 'c != key\.CreateTime')
Check 'priority change requires the expected current class' ($setPri -cmatch 'GetPriorityClass' -and $setPri -cmatch 'cur != expectBase')

Check 'TerminateProcess in the resident is at exactly one site' ((Count-Token $app 'Native.TerminateProcess(') -eq 1)
$end = Get-Block $app 'public static bool EndProcess('
Check 'that site is Procs.EndProcess' ($end -cmatch 'Native\.TerminateProcess\(')
Check 'ending re-verifies identity on the handle' ($end -cmatch 'GetProcessTimes' -and $end -cmatch 'c != key\.CreateTime')
$selfTerm = Get-Block $self 'static void SuspendedChildRoundTrip('
Check 'the self-test only ever terminates its own child' ((Count-Token $self 'Native.TerminateProcess(') -eq 1 -and $selfTerm -cmatch 'TerminateProcess\(pi\.hProcess')
$endStale = Get-Block $app 'public void EndStale('
Check 'a confirmed End acts only on processes still stale now' ($endStale -cmatch 'lastStale\.Find')
Check 'automatic ending needs arming AND SAIOPT''s own observation' ($logic -cmatch 'it\.Auto = observed && Cfg\.SweepAuto\.Contains')
Check 'nothing is armed out of the box (code default)' ($logic -cmatch 'public HashSet<string> SweepAuto = new HashSet<string>' -and -not ($logic -cmatch 'SweepAuto\.Add'))
Check 'nothing is armed out of the box (written default ini)' ($app -cmatch '"sweep_auto=\s+;')
Check 'only a process at Normal is ever eased' ($logic -cmatch 'p\.BasePriority != Priorities\.Normal\) continue')
Check 'a priority changed by someone else is never fought' ($logic -cmatch 'p\.BasePriority != t\.Entry\.EasedPriority' -and $logic -cmatch 'Refused = true')
Check 'stopping restores every eased process' ((Get-Block $app 'void Shutdown()') -cmatch 'RestoreAll')
Check 'eased set survives a crash (state file + adoption)' ($app -cmatch 'void SaveEased\(\)' -and $app -cmatch 'void AdoptPreviousRun\(\)' -and $app -cmatch 'Paths\.EasedPath')

$hard = Get-Block $logic 'static readonly HashSet<string> Hard'
foreach ($img in @('explorer.exe', 'dwm.exe', 'csrss.exe', 'audiodg.exe', 'conhost.exe', 'autohotkeyu64.exe', 'saiopt.exe', 'taskbaredge.exe')) {
    Check "hard guard covers $img" ($hard -cmatch [regex]::Escape('"' + $img + '"'))
}
Check 'service session is never touched' ($logic -cmatch 'SessionId == 0')

# --- 3. what it must never be ---------------------------------------------------
foreach ($t in @('EmptyWorkingSet', 'SetProcessWorkingSetSize', 'NtSetSystemInformation', 'SetSystemFileCacheSize',
                 'ServiceController', 'runas', 'requireAdministrator', 'taskkill')) {
    Check "never uses $t" (-not (Has-Token $all $t))
}
Check 'never kills through Process.Kill' (-not ($all -cmatch '\.Kill\('))
Check 'never writes HKLM' (-not ($all -cmatch 'LocalMachine\.CreateSubKey|LocalMachine\.DeleteSubKey|LocalMachine\.OpenSubKey\([^)]*,\s*true'))
Check 'HKCU autostart only' ((Get-Block $app 'static class Autostart') -cmatch 'Registry\.CurrentUser')
Check 'no Global\ primitive' (-not ($all -cmatch 'Global\\\\'))
Check 'per-session Local\ mutex' ($app -cmatch '"Local\\\\SaitulsSaiopt"')
Check 'never restarts Explorer' (-not ($all -cmatch 'explorer\.exe"?\s*,\s*"/restart|Stop-Process|RestartManager'))
$deletes = [regex]::Matches($app, '(?m)^.*File\.Delete\(.*$') | ForEach-Object { $_.Value.Trim() }
$foreign = @($deletes | Where-Object { $_ -notmatch 'File\.Delete\((path|Paths\.StatusPath|Paths\.EasedPath|path \+ "\.1")' })
Check 'deletes only its own state/log files' ($foreign.Count -eq 0) ($foreign -join ' | ')
Check 'tray tooltip goes through the 63-char cap' ((Get-Block $app 'void RefreshTray()') -cmatch 'Words\.Tooltip')
$touch = Get-Block $app 'static class DiskTouch'
Check 'keep-awake opens files read-only' ($touch -cmatch 'Native\.GENERIC_READ' -and $touch -cmatch 'Native\.OPEN_EXISTING' -and -not ($all -cmatch 'GENERIC_WRITE'))
Check 'keep-awake bypasses the cache so the read reaches the disk' ($touch -cmatch 'FILE_FLAG_NO_BUFFERING' -and $touch -cmatch 'VirtualAlloc')
Check 'keep-awake reads on its own thread, never the engine loop' ($app -cmatch 'new Thread\(KeepAwakeLoop\)' -and -not ((Get-Block $app 'void Tick()') -cmatch 'DiskTouch'))
Check 'keep-awake is earned by the USB periodic-reset signature only' ((Get-Block $logic 'public static bool Qualifies(') -cmatch 'Bus == "USB"' -and (Get-Block $logic 'public static bool Qualifies(') -cmatch 'Cadence\.Periodic')
$keys = Get-Block $app 'protected override bool ProcessCmdKey('
Check 'panel keys press nothing until a real click inside it' ($keys -cmatch 'if \(nav && !pointerArmed\) return true' -and (Get-Block $app 'protected override void OnMouseDown(') -cmatch 'pointerArmed = true' -and (Get-Block $app 'public void ShowNearTray()') -cmatch 'pointerArmed = false')
Check 'confirmations default to Cancel' ((Get-Block $app 'DialogResult Ask(') -cmatch 'MessageBoxDefaultButton\.Button2' -and (Get-Block $app 'public void ConfirmEnd(') -cmatch 'MessageBoxButtons\.OKCancel' -and (Get-Block $app 'public void ToggleArm(') -cmatch 'MessageBoxButtons\.OKCancel')
Check 'x64 build (reads 64-bit kernel records)' ((Get-Content -LiteralPath $paths.build -Raw) -cmatch '-platform:x64')

# --- 4. SAITULS.cs only launches it ---------------------------------------------
# ToolDefs columns are localization KEYS (AccountsLoc.T(ToolDefs[i,0])), so the
# row is matched on its key plus the launcher binding it must carry -- a
# stronger claim than the old literal-label match, which had gone stale.
Check 'SAITULS Tools has an Optimizer button' ($saituls -cmatch '\{ "ToolOptimizer",\s+"saiopt",\s+"saiopt" \}')
Check 'the Optimizer button label key resolves to a real word' ($saituls -cmatch '\{ "ToolOptimizer",\s+"Optimizer" \}')
$launch = Get-Block $saituls 'if (mode == "saiopt")'
Check 'SAITULS launches Scripts\saiopt\SAIOPT.exe --panel' ($launch -cmatch '"Scripts", "saiopt", "SAIOPT\.exe"' -and $launch -cmatch '"--panel"')
foreach ($t in @('SetPriorityClass', 'NtQuerySystemInformation', 'TerminateProcess', 'IAudioSessionManager2')) {
    Check "SAITULS.cs contains no $t" (-not (Has-Token $saituls $t))
}

Write-Host "SAIOPT_CONTRACT failures=$script:failures"
if ($script:failures -gt 0) { exit 1 }
exit 0
