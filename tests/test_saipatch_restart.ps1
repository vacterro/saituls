param(
    [string]$Action,
    [string]$CaseRoot,
    [string]$ProcsFile,
    [switch]$BlindAssert,
    [string]$Tree = (Join-Path $PSScriptRoot '..\Scripts\saipatch')
)
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Child entry: a disposable SAIPATCH state root, a fake install, and INJECTED
# process inventories. Every scenario is deterministic: process metadata (the
# exact executable path and start time) comes from a file, never from live
# process state. -BlindAssert simulates the real access-denied gap where the
# pre-mutation running-check cannot see a process that verify's metadata CAN.
# ---------------------------------------------------------------------------
if ($Action) {
    $env:LOCALAPPDATA = Join-Path $CaseRoot 'local'
    . (Join-Path $Tree 'lib/state.ps1')
    . (Join-Path $Tree 'lib/contract.ps1')
    . (Join-Path $Tree 'lib/transaction.ps1')

    $script:__procs = @()
    if ($ProcsFile -and [IO.File]::Exists($ProcsFile)) {
        # Wrap the list in a container object: PS 5.1 ConvertFrom-Json merges
        # top-level JSON array elements into one accumulated object.
        $raw = (ConvertFrom-Json -InputObject (Get-Content -Raw -LiteralPath $ProcsFile)).procs
        $script:__procs = @($raw | ForEach-Object {
            [pscustomobject]@{
                pid = $_.pid
                exe = $_.exe
                started_at = $(if ($_.started_at) {
                    [DateTime]::Parse($_.started_at,
                        [Globalization.CultureInfo]::InvariantCulture,
                        [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
                } else { $null })
            }
        })
    }
    # THE SEAM under test: the injected inventory replaces live Get-Process.
    function Get-OpenCodeProcessInventory { return $script:__procs }
    if ($BlindAssert) {
        function Assert-PatchNotRunning([object]$Install) { }
    }

    $install = [pscustomobject]@{ found = $true; root = $CaseRoot; exe = Join-Path $CaseRoot 'opencode.exe'; version = '1.0.0' }
    $patch = Join-Path $CaseRoot 'package'
    $lock = Enter-PatchLock
    try { Repair-PatchPublication } finally { $lock.ReleaseMutex(); $lock.Dispose() }
    $r = Get-PatchStatus $install $patch
    if ($Action -eq 'Apply') { $r = Invoke-PatchApply $install $patch $r }
    if ($Action -eq 'Restore') { $r = Invoke-PatchRestore $install $patch $r }
    if ($Action -eq 'Status') { $r = Get-PatchStatus $install $patch }
    $r | ConvertTo-Json -Depth 6 -Compress
    exit 0
}

# ---------------------------------------------------------------------------
# Parent runner
# ---------------------------------------------------------------------------
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('saipatch-restart-' + [Guid]::NewGuid().ToString('N'))
$script:checks = 0
function Assert([bool]$Ok, [string]$Name) {
    if (-not $Ok) { throw "FAIL: $Name" }
    $script:checks++; Write-Output "PASS: $Name"
}
function Hash([string]$Path) {
    if ([IO.File]::Exists($Path)) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }
    return 'ABSENT'
}
function Run([string]$Dir, [string]$Op, [string]$Procs = $null, [switch]$Blind) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $args = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Action ' + $Op + ' -CaseRoot "' + $Dir + '" -Tree "' + $Tree + '"'
    if ($Procs) { $args += ' -ProcsFile "' + $Procs + '"' }
    if ($Blind) { $args += ' -BlindAssert' }
    $start.FileName = 'powershell.exe'
    $start.Arguments = $args
    $start.UseShellExecute = $false; $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(30000)) { $process.Kill(); throw 'Test child timed out' }
    $code = $process.ExitCode; $output = $stdout.Result; $errors = $stderr.Result; $process.Dispose()
    if (-not $output.Trim()) { throw "Missing child stdout: $Op exit=$code $errors" }
    return ($output | ConvertFrom-Json)
}
function New-Case([string]$Name) {
    $dir = Join-Path $sandbox $Name
    [void][IO.Directory]::CreateDirectory((Join-Path $dir 'package'))
    [void][IO.Directory]::CreateDirectory((Join-Path $dir 'runtime'))
    [IO.File]::WriteAllText((Join-Path $dir 'opencode.exe'), 'test build identity')
    [IO.File]::WriteAllText((Join-Path $dir 'runtime/index.mjs'), 'original file')
    [IO.File]::WriteAllText((Join-Path $dir 'original.hash'), (Hash (Join-Path $dir 'runtime/index.mjs')))
    [IO.File]::WriteAllText((Join-Path $dir 'package/index.mjs'), 'installed entry')
    [IO.File]::WriteAllText((Join-Path $dir 'package/second.mjs'), 'generation A')
    @{ id = 'test-restart'; version = '2.5.0'; files = @('index.mjs', 'second.mjs'); supports = @{
        opencode = @('1.0.0'); builds = @{ '1.0.0' = @((Hash (Join-Path $dir 'opencode.exe'))) }
    }} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $dir 'package/manifest.json') -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $dir 'package/common.ps1') -Value 'function Test-PluginContract { [pscustomobject]@{ok=$true} }'
    Set-Content -LiteralPath (Join-Path $dir 'package/apply.ps1') -Value @'
param([string]$InstallRoot,[string]$Version,[switch]$Plan,[string]$Stage)
$ErrorActionPreference='Stop'
$mf=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'manifest.json') -Raw | ConvertFrom-Json
if($Plan){
    $targets=@(foreach($file in $mf.files){
        [pscustomobject]@{
            file=$file; path=Join-Path $InstallRoot "runtime/$file"
            original_sha256=$(if($file -eq 'index.mjs'){(Get-Content -LiteralPath (Join-Path $InstallRoot 'original.hash') -Raw).Trim()}else{$null})
            patched_sha256=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $file) -Algorithm SHA256).Hash
        }
    })
    @{targets=$targets}|ConvertTo-Json -Depth 5 -Compress
}else{foreach($file in $mf.files){Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $Stage $file)}}
'@ -Encoding UTF8
    return $dir
}
function Write-Procs([string]$Name, [object[]]$Procs) {
    $path = Join-Path $sandbox ($Name + '.procs.json')
    @{ procs = @($Procs | ForEach-Object {
        [pscustomobject]@{ pid = $_.pid; exe = $_.exe;
            started_at = $(if ($_.started_at) { $_.started_at.ToString('o') } else { $null }) }
    }) } | ConvertTo-Json -Depth 4 -Compress | Set-Content -LiteralPath $path -Encoding ASCII
    return $path
}
function Get-TransitionStamp([string]$Dir) {
    $rt = Join-Path $Dir 'local/SAITULS/SAIPATCH/runtime.json'
    $t = Get-Content -Raw -LiteralPath $rt | ConvertFrom-Json
    return [DateTime]::Parse($t.transitioned_at,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
}

try {
    [void][IO.Directory]::CreateDirectory($sandbox)

    # ---- A: no running process -> INSTALLED, restart NO --------------------
    $d = New-Case 'a-no-proc'
    Assert ((Run $d 'Apply').state -eq 'INSTALLED') 'A: seed apply INSTALLED'
    $s = Run $d 'Status'
    Assert ($s.state -eq 'INSTALLED' -and -not $s.runtime_restart_required) 'A: no process -> INSTALLED, restart NO'
    Assert ($s.runtime_transition -eq 'APPLY') 'A: APPLY transition recorded'

    # ---- transition record content ------------------------------------------
    $rt = Get-Content -Raw -LiteralPath (Join-Path $d 'local/SAITULS/SAIPATCH/runtime.json') | ConvertFrom-Json
    Assert ($rt.patch_version -eq '2.5.0' -and $rt.install_root -eq $d -and
            $rt.exe -eq (Join-Path $d 'opencode.exe') -and $rt.transitioned_at) 'transition record: version, root, exe, timestamp'
    $targetsOk = @($rt.disk_state.targets).Count -eq 2 -and
        @($rt.disk_state.targets | Where-Object { $_.sha256 }).Count -ge 1
    Assert $targetsOk 'transition record: resulting disk state carries target hashes'
    Assert ($rt.disk_state.state_sha256 -eq (Hash (Join-Path $d 'local/SAITULS/SAIPATCH/state.json'))) 'transition record: ownership file hash proven'

    # ---- B: matching process OLDER than Apply -> INSTALLED_RESTART_REQUIRED -
    $stamp = Get-TransitionStamp $d
    $stale = Write-Procs 'stale' @(
        @{ pid = 100; exe = (Join-Path $d 'opencode.exe'); started_at = $stamp.AddHours(-1) })
    $s = Run $d 'Status' $stale
    Assert ($s.state -eq 'INSTALLED_RESTART_REQUIRED' -and $s.runtime_restart_required -and
            $s.runtime_stale_processes -eq 1) 'B: process older than Apply -> INSTALLED_RESTART_REQUIRED'

    # ---- C: matching process NEWER than Apply -> INSTALLED ------------------
    $fresh = Write-Procs 'fresh' @(
        @{ pid = 100; exe = (Join-Path $d 'opencode.exe'); started_at = $stamp.AddHours(1) })
    $s = Run $d 'Status' $fresh
    Assert ($s.state -eq 'INSTALLED' -and -not $s.runtime_restart_required) 'C: process newer than Apply -> INSTALLED, restart NO'

    # ---- D: unrelated opencode path + unverifiable -> ignored ---------------
    $other = Write-Procs 'other' @(
        @{ pid = 200; exe = (Join-Path $d 'elsewhere\opencode.exe'); started_at = $stamp.AddHours(-5) },
        @{ pid = 201; exe = $null; started_at = $null })
    $s = Run $d 'Status' $other
    Assert ($s.state -eq 'INSTALLED' -and -not $s.runtime_restart_required) 'D: unrelated executable path ignored'
    Assert ($s.runtime_unverified_processes -eq 1) 'D: unverifiable process counted, never matched'

    # ---- E: Restore while old process exists -> RESTORED_RESTART_REQUIRED ---
    $d = New-Case 'e-restore-stale'
    Assert ((Run $d 'Apply').state -eq 'INSTALLED') 'E: seed apply INSTALLED'
    $stamp = Get-TransitionStamp $d
    $stale = Write-Procs 'e-stale' @(
        @{ pid = 300; exe = (Join-Path $d 'opencode.exe'); started_at = $stamp.AddHours(-1) })
    $r = Run $d 'Restore' $stale -Blind
    Assert ($r.state -eq 'AVAILABLE') 'E: restore succeeds when the running check is blind to the process'
    $s = Run $d 'Status' $stale
    Assert ($s.state -eq 'RESTORED_RESTART_REQUIRED' -and $s.runtime_restart_required) 'E: stale process after Restore -> RESTORED_RESTART_REQUIRED'
    $none = Write-Procs 'e-none' @()
    $s = Run $d 'Status' $none
    Assert ($s.state -eq 'RESTORED' -and -not $s.runtime_restart_required) 'E: RESTORED reported when no stale process remains'

    # ---- F: process disappears/restarts -> normal state ---------------------
    $d = New-Case 'f-restart'
    Assert ((Run $d 'Apply').state -eq 'INSTALLED') 'F: seed apply INSTALLED'
    $stamp = Get-TransitionStamp $d
    $stale = Write-Procs 'f-stale' @(
        @{ pid = 400; exe = (Join-Path $d 'opencode.exe'); started_at = $stamp.AddHours(-1) })
    Assert ((Run $d 'Status' $stale).state -eq 'INSTALLED_RESTART_REQUIRED') 'F: pre-restart state is INSTALLED_RESTART_REQUIRED'
    $restarted = Write-Procs 'f-new' @(
        @{ pid = 400; exe = (Join-Path $d 'opencode.exe'); started_at = $stamp.AddHours(1) })
    $s = Run $d 'Status' $restarted
    Assert ($s.state -eq 'INSTALLED' -and -not $s.runtime_restart_required) 'F: restarted process (same pid, new start) -> normal INSTALLED'

    # ---- G: PID reuse alone can never satisfy the proof ---------------------
    $d = New-Case 'g-pid-reuse'
    Assert ((Run $d 'Apply').state -eq 'INSTALLED') 'G: seed apply INSTALLED'
    $stamp = Get-TransitionStamp $d
    $old = Write-Procs 'g-old' @(
        @{ pid = 4242; exe = (Join-Path $d 'opencode.exe'); started_at = $stamp.AddMinutes(-1) })
    $new = Write-Procs 'g-new' @(
        @{ pid = 4242; exe = (Join-Path $d 'opencode.exe'); started_at = $stamp.AddMinutes(1) })
    Assert ((Run $d 'Status' $old).runtime_restart_required) 'G: same pid, older start -> restart required'
    Assert (-not (Run $d 'Status' $new).runtime_restart_required) 'G: same pid, newer start -> proof fails, restart NOT required'

    Write-Output "$($script:checks) restart-awareness checks"
} finally {
    if ([IO.Directory]::Exists($sandbox)) { [void][IO.Directory]::Delete($sandbox, $true) }
}
