param(
    [string]$ScriptSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Add-CodexContextMenus.ps1')
)

$ErrorActionPreference = 'Stop'

# Behavioural check for the Codex cascade installer's preflight and rollback.
#
# Two defects, both about a half-applied menu:
#   1. the script deleted the existing `CodexHere` subtree and only then validated
#      each launcher path from inside the same write loop, so a wrong
#      `-LauncherRoot` turned a working cascade into an empty parent key;
#   2. the two shell roots were written one after another with no transaction, so
#      a registry failure while writing the second left the first converted and
#      the second deleted-and-half-written, with no way back.
#
# The real script writes HKLM\SOFTWARE\Classes. This harness rewrites that ONE
# literal to a throwaway HKCU key, so the machine's actual shell configuration
# is never touched and the key is removed afterwards. Delete order, preflight,
# rollback and exit code are the script's own unmodified logic.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_codex_menu_preflight.ps1
# Exit: 0 = all PASS, 1 = failures.

$testRoot = "HKCU:\Software\_SAITULS_MENU_TEST_" + [Guid]::NewGuid().ToString('N')
$sandbox = Join-Path $env:TEMP ('saituls_menu_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox | Out-Null

function New-Replica([string]$src, [string]$dest, [string]$regRoot, [switch]$FailOnSecondRoot) {
    $text = Get-Content -Raw -LiteralPath $src
    $text = $text -replace '#Requires -RunAsAdministrator', ''
    $text = $text.Replace('HKLM:\SOFTWARE\Classes', $regRoot)
    if ($FailOnSecondRoot) {
        # Fault injection, not a rewrite of the logic under test: a registry write
        # cannot be made to fail on demand inside HKCU without an ACL the current
        # user is not allowed to set. The throw lands AFTER the second root has
        # been deleted and partially recreated -- exactly where a real
        # access-denied or corrupted-hive failure would land -- and the rollback
        # path it exercises is the script's own.
        $marker = 'Set-ItemProperty -Path $base -Name "Icon" -Value "powershell.exe"'
        if (-not $text.Contains($marker)) { throw "fault-injection marker not found in $src" }
        $text = $text.Replace($marker, $marker + "`r`n    if (`$root -like '*Background') { throw 'injected registry failure' }")
    }
    Set-Content -LiteralPath $dest -Value $text -Encoding UTF8
}

function New-Launchers([string]$root, [string[]]$present) {
    foreach ($pair in @(
            @('main_codex', 'Start-Codex-Main.ps1'),
            @('main_codex2', 'Start-Codex-Account2.ps1'),
            @('main_codex3_free', 'Start-Codex-Account3-Free.ps1'))) {
        if ($present -contains $pair[0]) {
            $d = Join-Path $root $pair[0]
            New-Item -ItemType Directory -Path $d -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $d $pair[1]) -Value '# stub' -Encoding Ascii
        }
    }
}

# Every key, subkey and value under both shells, sorted: value-equivalence, not
# "the parent key still exists" -- the defect left the parent key in place and
# emptied it.
function Snapshot([string]$regRoot) {
    $out = @()
    foreach ($r in 'Directory', 'Directory\Background') {
        $k = "$regRoot\$r\shell\CodexHere"
        if (-not (Test-Path $k)) { $out += "$r=<absent>"; continue }
        foreach ($item in (Get-ChildItem -Path $k -Recurse -ErrorAction SilentlyContinue)) {
            $props = Get-ItemProperty -Path $item.PSPath
            foreach ($n in ($props.PSObject.Properties.Name | Where-Object { $_ -notlike 'PS*' } | Sort-Object)) {
                $out += "$($item.PSPath.Replace('Microsoft.PowerShell.Core\Registry::',''))|$n=$($props.$n)"
            }
        }
        $props = Get-ItemProperty -Path $k
        foreach ($n in ($props.PSObject.Properties.Name | Where-Object { $_ -notlike 'PS*' } | Sort-Object)) {
            $out += "$r|$n=$($props.$n)"
        }
    }
    return ($out | Sort-Object) -join "`n"
}

function Invoke-Installer([string]$replica, [string]$root) {
    # Native stderr arrives as error records, so with 'Stop' in force a subject
    # that throws would abort the HARNESS -- instrument failure reported as a
    # subject failure. The child's own exit code stays the verdict.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $replica -LauncherRoot $root 2>&1
        $lines = @($out | ForEach-Object { "$_" })
        # Write-Warning wraps at the child console width and breaks wherever that
        # width falls -- observed splitting a path as
        # 'Start-Codex-Account3-Free.p' + 's1'. A substring assertion against the
        # wrapped text therefore passes or fails by terminal geometry, not by
        # behaviour. Flat removes every wrap AND every space, so a whitespace-free
        # needle (a launcher file name) matches at any width.
        return [pscustomobject]@{
            Output = $lines -join "`n"
            Flat   = ($lines -join '') -replace '\s', ''
            Exit   = $LASTEXITCODE
        }
    } finally { $ErrorActionPreference = $old }
}

$fails = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

try {
    $replica = Join-Path $sandbox 'Add-CodexContextMenus.ps1'
    New-Replica $ScriptSource $replica $testRoot

    $good = Join-Path $sandbox 'good'
    New-Launchers $good @('main_codex', 'main_codex2', 'main_codex3_free')
    $r1 = Invoke-Installer $replica $good
    $before = Snapshot $testRoot
    Check 'complete launcher set installs' ($r1.Exit -eq 0 -and $before -match 'CodexMain') "exit=$($r1.Exit)"
    Check 'both shells carry all three verbs' `
        (($before -split "`n" | Where-Object { $_ -match 'CodexAccount3Free' }).Count -ge 2) ''

    $bad = Join-Path $sandbox 'bad'
    New-Launchers $bad @('main_codex', 'main_codex2')
    $r2 = Invoke-Installer $replica $bad
    $after = Snapshot $testRoot
    Check 'missing launcher exits nonzero' ($r2.Exit -ne 0) "exit=$($r2.Exit)"
    Check 'missing launcher names what it could not find' `
        ($r2.Flat -match 'Start-Codex-Account3-Free\.ps1') ''
    Check 'existing cascade is value-equivalent after the refusal' ($after -eq $before) `
        "changed=$($after -ne $before)"

    # A registry failure part-way through must roll BOTH shells back, not leave
    # the first one converted.
    $faulty = Join-Path $sandbox 'Add-CodexContextMenus.faulty.ps1'
    New-Replica $ScriptSource $faulty $testRoot -FailOnSecondRoot
    $r3 = Invoke-Installer $faulty $good
    $afterFault = Snapshot $testRoot
    Check 'a mid-install registry failure exits nonzero' ($r3.Exit -ne 0) "exit=$($r3.Exit)"
    Check 'both shells are value-equivalent after a mid-install failure' `
        ($afterFault -eq $before) "changed=$($afterFault -ne $before)"
} finally {
    Remove-Item -Path $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
