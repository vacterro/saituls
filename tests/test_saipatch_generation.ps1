param(
    [string]$Action,
    [string]$CaseRoot,
    [string]$Tree = (Join-Path $PSScriptRoot '..\Scripts\saipatch')
)
$ErrorActionPreference = 'Stop'

# Child entry uses only its disposable state/runtime. No real OpenCode or plugin.
if ($Action) {
    $env:LOCALAPPDATA = Join-Path $CaseRoot 'local'
    . (Join-Path $Tree 'lib/state.ps1')
    . (Join-Path $Tree 'lib/contract.ps1')
    . (Join-Path $Tree 'lib/transaction.ps1')
    $install = [pscustomobject]@{ found = $true; root = $CaseRoot; exe = Join-Path $CaseRoot 'opencode.exe'; version = '1.0.0' }
    $patch = Join-Path $CaseRoot 'package'
    $lock = Enter-PatchLock
    try { Repair-PatchPublication } finally { $lock.ReleaseMutex(); $lock.Dispose() }
    $r = Get-PatchStatus $install $patch
    if ($Action -eq 'Apply') { $r = Invoke-PatchApply $install $patch $r }
    if ($Action -eq 'Restore') { $r = Invoke-PatchRestore $install $patch $r }
    if ($Action -eq 'Verify') { $r = Invoke-PatchVerify $install $patch $r }
    $r | ConvertTo-Json -Compress
    exit $(if ($r.state -in @('BROKEN_PATCH', 'SOURCE_DRIFTED', 'UNSUPPORTED_BUILD')) { 3 } else { 0 })
}

$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('saipatch-generation-' + [Guid]::NewGuid().ToString('N'))
$script:checks = 0
function Assert([bool]$Ok, [string]$Name) {
    if (-not $Ok) { throw "FAIL: $Name" }
    $script:checks++; Write-Output "PASS: $Name"
}
function Hash([string]$Path) {
    if ([IO.File]::Exists($Path)) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }
    return 'ABSENT'
}
function New-Case([string]$Name) {
    $dir = Join-Path $sandbox $Name
    [void][IO.Directory]::CreateDirectory((Join-Path $dir 'package'))
    [void][IO.Directory]::CreateDirectory((Join-Path $dir 'runtime'))
    [IO.File]::WriteAllText((Join-Path $dir 'opencode.exe'), 'test build identity')
    # One existing target and one absent target exercise both restore states.
    [IO.File]::WriteAllText((Join-Path $dir 'runtime/index.mjs'), 'original file')
    [IO.File]::WriteAllText((Join-Path $dir 'original.hash'), (Hash (Join-Path $dir 'runtime/index.mjs')))
    [IO.File]::WriteAllText((Join-Path $dir 'package/index.mjs'), 'installed entry')
    [IO.File]::WriteAllText((Join-Path $dir 'package/second.mjs'), 'generation A')
    @{ id = 'test-generation'; version = '1.0'; files = @('index.mjs', 'second.mjs'); supports = @{
        opencode = @('1.0.0'); builds = @{ '1.0.0' = @((Hash (Join-Path $dir 'opencode.exe'))) }
    }} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $dir 'package/manifest.json') -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $dir 'package/common.ps1') -Value 'function Test-PluginContract { [pscustomobject]@{ok=$true} }'
    @'
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
'@ | Set-Content -LiteralPath (Join-Path $dir 'package/apply.ps1') -Encoding UTF8
    return $dir
}
function Run([string]$Dir, [string]$Op, [string]$Fault = '', [string]$Crash = '') {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = 'powershell.exe'
    $start.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Action ' + $Op + ' -CaseRoot "' + $Dir + '" -Tree "' + $Tree + '"'
    $start.UseShellExecute = $false; $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    $start.EnvironmentVariables['SAIPATCH_TEST_FAULT'] = $Fault
    $start.EnvironmentVariables['SAIPATCH_TEST_CRASH'] = $Crash
    $process = [Diagnostics.Process]::Start($start)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(30000)) { $process.Kill(); throw 'Test child timed out' }
    $code = $process.ExitCode; $output = $stdout.Result; $errors = $stderr.Result; $process.Dispose()
    if ($Crash) {
        Assert ($code -eq 91) "crash injected at $Op/$Crash (exit $code)"
        return
    }
    if (-not $output.Trim()) { throw "Missing child stdout: $Op exit=$code $errors" }
    try { return ($output | ConvertFrom-Json) } catch { throw "Invalid child report: $output $errors" }
}
function Snapshot([string]$Dir) {
    (@('runtime/index.mjs','runtime/second.mjs','local/SAITULS/SAIPATCH/state.json') | ForEach-Object { Hash (Join-Path $Dir $_) }) -join ':'
}

try {
    [void][IO.Directory]::CreateDirectory($sandbox)
    $d = New-Case 'generation'
    Assert ((Run $d 'Apply').state -eq 'INSTALLED') 'initial generation installed'
    $entry = (Get-Content -Raw -LiteralPath (Join-Path $d 'local/SAITULS/SAIPATCH/state.json') | ConvertFrom-Json).patches[0]
    Assert ([bool]($entry.patch_id -and $entry.patch_version -and $entry.manifest_hash -and $entry.build_hash -and $entry.installed_generation -and $entry.transaction)) 'generation ownership metadata complete'
    $before = Snapshot $d
    $index = Hash (Join-Path $d 'package/index.mjs')
    [IO.File]::WriteAllText((Join-Path $d 'package/second.mjs'), 'generation B')
    Assert ((Hash (Join-Path $d 'package/index.mjs')) -eq $index) 'historical oracle: index.mjs unchanged'
    foreach ($op in @('Detect','Status','Verify')) { Assert ((Run $d $op).state -eq 'NEEDS_REAPPLY') "$op agrees on second-target source change" }
    Assert ((Run $d 'Apply' 'state-write').state -eq 'BROKEN_PATCH') 'state-write failure reported'
    Assert ((Snapshot $d) -eq $before) 'failed Reapply restores exact previous runtime and state bytes'
    Assert ((Run $d 'Apply').state -eq 'INSTALLED') 'Reapply publishes generation B'
    $before = Snapshot $d
    [IO.File]::AppendAllText((Join-Path $d 'runtime/second.mjs'), ' foreign edit')
    $drift = Snapshot $d
    Assert ((Run $d 'Restore').state -eq 'SOURCE_DRIFTED') 'late-target drift refuses Restore'
    Assert ((Snapshot $d) -eq $drift) 'late-target Restore refusal mutates zero files'
    Copy-Item -LiteralPath (Join-Path $d 'package/second.mjs') -Destination (Join-Path $d 'runtime/second.mjs') -Force
    Assert ((Snapshot $d) -eq $before) 'fixture restored to installed generation'
    [IO.File]::WriteAllText((Join-Path $d 'package/second.mjs'), 'uninstalled generation C')
    Assert ((Run $d 'Restore' 'runtime-1').state -eq 'BROKEN_PATCH') 'Restore partial failure reported'
    Assert ((Snapshot $d) -eq $before) 'failed Restore rolls back attempted restore'
    Assert ((Run $d 'Restore').state -eq 'AVAILABLE') 'Restore uses recorded generation despite source changes'
    Assert ((Hash (Join-Path $d 'runtime/index.mjs')) -eq (Get-Content -Raw -LiteralPath (Join-Path $d 'original.hash')).Trim()) 'original file restored byte-exact'
    Assert (-not [IO.File]::Exists((Join-Path $d 'runtime/second.mjs'))) 'originally absent target restored to absence'

    foreach ($op in @('Apply','Restore','Reapply')) {
        foreach ($point in @('prepared','runtime-1','runtime-committed','state-write','ownership-committed')) {
            $d = New-Case "$op-$point"
            if ($op -ne 'Apply') { Assert ((Run $d 'Apply').state -eq 'INSTALLED') "$op/$point seed" }
            if ($op -eq 'Reapply') { [IO.File]::WriteAllText((Join-Path $d 'package/second.mjs'), 'generation B') }
            $before = Snapshot $d
            $actionName = if ($op -eq 'Restore') { 'Restore' } else { 'Apply' }
            Run $d $actionName '' $point
            $status = Run $d 'Status'
            if ($point -eq 'ownership-committed') {
                $expected = if ($op -eq 'Restore') { 'AVAILABLE' } else { 'INSTALLED' }
                Assert ($status.state -eq $expected) "$op/$point retains committed generation"
            } else { Assert ((Snapshot $d) -eq $before) "$op/$point recovery restores exact pre-run bytes" }
            Assert (-not [IO.File]::Exists((Join-Path $d 'local/SAITULS/SAIPATCH/pending.json'))) "$op/$point journal settled"
            $recovered = Snapshot $d
            $null = Run $d 'Status'
            Assert ((Snapshot $d) -eq $recovered) "$op/$point recovery idempotent"
        }
    }
    $d = New-Case 'build-drift'
    [IO.File]::AppendAllText((Join-Path $d 'opencode.exe'), 'drift')
    foreach($op in @('Detect','Status','Verify')) { Assert ((Run $d $op).state -eq 'UNSUPPORTED_BUILD') "$op rejects same-version foreign executable" }
    Write-Output "PASS: $script:checks generation/publication checks"
} finally {
    $resolved = [IO.Path]::GetFullPath($sandbox)
    if (-not $resolved.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()), [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolved) -notlike 'saipatch-generation-*') { throw 'Unsafe fixture cleanup target' }
    if ([IO.Directory]::Exists($resolved)) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
