param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition))
)

$ErrorActionPreference = 'Stop'

# T-133 concurrency harness: ONE cross-process registry mutation contract.
#
# 1. The harness holds Global\SAITULS_REGISTRY_MUTATION and launches every
#    replica registry writer; each must refuse BUSY (nonzero exit) and leave
#    its sandbox byte-equivalent -- the lock is taken BEFORE the first mutation.
# 2. Two real Codex-menu transactions are launched concurrently while the lock
#    is free; the final registry state must be exactly ONE complete generation,
#    never an interleaved mix.
# 3. Writers that cannot run headless (INSTALL_GUI form, SAITULS.cs, setup.ps1,
#    INSTALL_ALL) are proven by source-shape assertions: the only lock
#    acquisition paths wrap a bounded mutation, never the form lifetime or the
#    UI thread.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_registry_lock.ps1
# Exit: 0 = all PASS, 1 = failures.

$sandbox = Join-Path $env:TEMP ('saituls_lock_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null

$codexRoot   = "HKCU:\Software\_SAI_LOCK_CLASSES_" + [Guid]::NewGuid().ToString('N')
$aimRoot     = "HKCU:\Software\_SAI_LOCK_AIM_" + [Guid]::NewGuid().ToString('N')
$aimConsole  = "HKCU:\Software\_SAI_LOCK_CONSOLE_" + [Guid]::NewGuid().ToString('N')
$importHive  = "HKCU:\Software\_SAI_LOCK_IMPORT_" + [Guid]::NewGuid().ToString('N')

$fails = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

function Invoke-Writer([string]$replica, [string[]]$extra = @()) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $replica @extra 2>&1
        return [pscustomobject]@{
            Output = ($out | ForEach-Object { "$_" }) -join "`n"
            Flat   = (($out | ForEach-Object { "$_" }) -join '') -replace '\s', ''
            Exit   = $LASTEXITCODE
        }
    } finally { $ErrorActionPreference = $old }
}

# Every key, subkey and value under the sandbox roots, sorted.
function Snapshot {
    $out = @()
    foreach ($k in $codexRoot, $aimRoot, $aimConsole, $importHive) {
        if (-not (Test-Path $k)) { $out += "$k=<absent>"; continue }
        foreach ($item in (Get-ChildItem -Path $k -Recurse -ErrorAction SilentlyContinue)) {
            $props = Get-ItemProperty -Path $item.PSPath
            foreach ($n in ($props.PSObject.Properties.Name | Where-Object { $_ -notlike 'PS*' } | Sort-Object)) {
                $out += "$($item.PSPath)|$n=$($props.$n)"
            }
        }
        $props = Get-ItemProperty -Path $k -ErrorAction SilentlyContinue
        if ($props) {
            foreach ($n in ($props.PSObject.Properties.Name | Where-Object { $_ -notlike 'PS*' } | Sort-Object)) {
                $out += "$k|$n=$($props.$n)"
            }
        }
    }
    return ($out | Sort-Object) -join "`n"
}

# --- replicas --------------------------------------------------------------

function New-CodexReplica([string]$dest, [string]$labelPrefix) {
    $text = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'Add-CodexContextMenus.ps1')
    $text = $text -replace '#Requires -RunAsAdministrator', ''
    $text = $text.Replace('HKLM:\SOFTWARE\Classes', $codexRoot)
    foreach ($v in @('main_codex', 'main_codex2', 'main_codex3_free')) {
        $text = $text.Replace("Label = `"$v`"", "Label = `"$labelPrefix$v`"")
    }
    Set-Content -LiteralPath $dest -Value $text -Encoding UTF8
}

function New-RemoveReplica([string]$dest) {
    $text = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'Remove-CodexContextMenus.ps1')
    # Neutralize self-elevation: the lock contract under test is the MUTATION
    # body, and a UAC prompt cannot be answered from a harness.
    $text = $text -replace '(?s)\$isAdmin = \(\[Security\.Principal\.WindowsPrincipal\].*?Administrator\)', '$isAdmin = $true'
    $text = $text.Replace('HKLM:\SOFTWARE\Classes', $codexRoot)
    Set-Content -LiteralPath $dest -Value $text -Encoding UTF8
}

function New-AgentMenusReplica([string]$dest) {
    $text = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'Installers\INSTALL_AI_AGENT_MENUS.PS1')
    $text = $text.Replace('$classesRoot = "HKCU:\Software\Classes"', "`$classesRoot = '$aimRoot'")
    $text = $text.Replace('"HKCU:\Console",', "'$aimConsole',")
    $text = $text.Replace('"HKCU:\Console\%SystemRoot%_System32_WindowsPowerShell_v1.0_powershell.exe"', "'$aimConsole'")
    $text = $text.Replace('Get-ItemProperty -LiteralPath "HKCU:\Console"', "Get-ItemProperty -LiteralPath '$aimConsole'")
    Set-Content -LiteralPath $dest -Value $text -Encoding UTF8
}

function New-ImportReplica([string]$dest) {
    $text = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'Registry\IMPORT_SAFE.PS1')
    $text = $text -replace '#Requires -RunAsAdministrator', ''
    Set-Content -LiteralPath $dest -Value $text -Encoding UTF8
}

function New-ImportReg([string]$dest) {
    $guid = $importHive -replace '^HKCU:\\Software\\', ''
    $content = "Windows Registry Editor Version 5.00`r`n`r`n" +
               "[HKEY_CURRENT_USER\Software\$guid\Probe]`r`n" +
               "`"ProbeValue`"=`"seed`"`r`n"
    Set-Content -LiteralPath $dest -Value $content -Encoding Unicode
}

function New-Launchers([string]$root) {
    foreach ($pair in @(
            @('main_codex', 'Start-Codex-Main.ps1'),
            @('main_codex2', 'Start-Codex-Account2.ps1'),
            @('main_codex3_free', 'Start-Codex-Account3-Free.ps1'))) {
        $d = Join-Path $root $pair[0]
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $d $pair[1]) -Value '# stub' -Encoding Ascii
    }
}

# --- case 1: held lock refuses every writer with zero mutation -------------

$codexReplica = Join-Path $sandbox 'codex.ps1'
$removeReplica = Join-Path $sandbox 'remove.ps1'
$aimReplica = Join-Path $sandbox 'aim.ps1'
$importReplica = Join-Path $sandbox 'import.ps1'
$importReg = Join-Path $sandbox 'probe.reg'

New-CodexReplica $codexReplica 'genA_'
New-RemoveReplica $removeReplica
New-AgentMenusReplica $aimReplica
New-ImportReplica $importReplica
New-ImportReg $importReg
# Launcher stubs so Add-CodexContextMenus passes PREFILGHT and reaches the lock
# (preflight runs before the mutation block) -- BUSY must be observed at WaitOne,
# not at a missing-launcher preflight refusal.
New-Launchers (Join-Path $sandbox 'good')

$writers = @(
    @{ Name = 'Add-CodexContextMenus';  Replica = $codexReplica;  Extra = @('-LauncherRoot', (Join-Path $sandbox 'good')) },
    @{ Name = 'Remove-CodexContextMenus'; Replica = $removeReplica; Extra = @() },
    @{ Name = 'INSTALL_AI_AGENT_MENUS'; Replica = $aimReplica;   Extra = @() },
    @{ Name = 'IMPORT_SAFE';            Replica = $importReplica; Extra = @('-File', $importReg) }
)

$lock = New-Object Threading.Mutex($true, 'Global\SAITULS_REGISTRY_MUTATION', [ref]$true)
try {
    if (-not $lock) { throw 'could not create the mutation mutex' }
    $before = Snapshot
    foreach ($w in $writers) {
        $r = Invoke-Writer $w.Replica $w.Extra
        Check "$($w.Name) refuses BUSY (nonzero)" ($r.Exit -ne 0) "exit=$($r.Exit)"
        Check "$($w.Name) names the busy lock" ($r.Flat -match 'BUSY') ''
        $after = Snapshot
        Check "$($w.Name) mutates nothing under a held lock" ($after -eq $before) ''
    }
} finally {
    $lock.ReleaseMutex()
    $lock.Dispose()
}

# --- case 2: concurrent transactions serialize into one complete generation --

New-Launchers (Join-Path $sandbox 'good')
$genA = Join-Path $sandbox 'codex_genA.ps1'
$genB = Join-Path $sandbox 'codex_genB.ps1'
New-CodexReplica $genA 'genA_'
New-CodexReplica $genB 'genB_'

$r0 = Invoke-Writer $genA @('-LauncherRoot', (Join-Path $sandbox 'good'))
Check 'seed generation installs with a free lock' ($r0.Exit -eq 0) "exit=$($r0.Exit)"

$pA = Start-Process powershell.exe -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$genA,'-LauncherRoot',(Join-Path $sandbox 'good')) -PassThru -WindowStyle Hidden
$pB = Start-Process powershell.exe -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$genB,'-LauncherRoot',(Join-Path $sandbox 'good')) -PassThru -WindowStyle Hidden
$pA.WaitForExit(120000) | Out-Null
$pB.WaitForExit(120000) | Out-Null

$final = Snapshot
$labels = @($final -split "`n" | Where-Object { $_ -match '\|MUIVerb=' })
$genALabels = @($labels | Where-Object { $_ -match 'genA_' })
$genBLabels = @($labels | Where-Object { $_ -match 'genB_' })
Check 'both concurrent transactions completed' ($pA.ExitCode -eq 0 -and $pB.ExitCode -eq 0) "A=$($pA.ExitCode) B=$($pB.ExitCode)"
Check 'no interleaved generation: labels are one complete set' `
    (($genALabels.Count -eq 0 -and $genBLabels.Count -eq 6) -or ($genBLabels.Count -eq 0 -and $genALabels.Count -eq 6)) `
    "A=$($genALabels.Count) B=$($genBLabels.Count) total=$($labels.Count)"

# --- case 3: source-shape assertions for the headless writers ---------------

$guiText = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'Installers\INSTALL_GUI.PS1')
Check 'INSTALL_GUI takes the lock per action, never for the form lifetime' `
    ((([regex]::Matches($guiText, 'WaitOne')).Count -eq 1) -and
     (([regex]::Matches($guiText, 'Invoke-RegistryTransaction \{')).Count -ge 2) -and
     ($guiText -match 'function Invoke-RegistryTransaction')) ''

$allText = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'Installers\INSTALL_ALL.PS1')
$allWait = ([regex]::Match($allText, 'WaitOne')).Index
$allFirstMutation = ([regex]::Match($allText, 'foreach \(\$file in @\(\$previousRegFiles \+ \$regFiles\)\)')).Index
Check 'INSTALL_ALL acquires the lock before its first mutation' `
    ($allWait -ge 0 -and $allFirstMutation -gt $allWait) ''

$setupText = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'setup.ps1')
$setupLock = ([regex]::Match($setupText, [regex]::Escape('Global\SAITULS_REGISTRY_MUTATION'))).Index
$setupRunKey = ([regex]::Match($setupText, [regex]::Escape('$runKey.SetValue('))).Index
Check 'setup.ps1 Run-key write sits inside the shared lock' `
    ($setupLock -ge 0 -and $setupRunKey -gt $setupLock) ''

$csText = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'SAITULS.cs')
$csLockCount = ([regex]::Matches($csText, [regex]::Escape('Global\SAITULS_REGISTRY_MUTATION'))).Count
Check 'SAITULS.cs serializes both Run-key writers through the shared lock' ($csLockCount -ge 2) "locks=$csLockCount"

$importText = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'Registry\IMPORT_SAFE.PS1')
$importLock = ([regex]::Match($importText, [regex]::Escape('Global\SAITULS_REGISTRY_MUTATION'))).Index
$importCall = ([regex]::Match($importText, [regex]::Escape('reg import $tmp'))).Index
Check 'IMPORT_SAFE takes the lock before reg import' `
    ($importLock -ge 0 -and $importCall -gt $importLock) ''

$agentsText = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'Installers\INSTALL_AI_AGENT_MENUS.PS1')
$agentsLock = ([regex]::Match($agentsText, [regex]::Escape('Global\SAITULS_REGISTRY_MUTATION'))).Index
$agentsWrite = ([regex]::Match($agentsText, [regex]::Escape('Install-WintageConsoleProfile'))).Index
$agentsWrite2 = ([regex]::Match($agentsText, [regex]::Escape('Remove-Item -LiteralPath $menuKey -Recurse -Force'))).Index
Check 'INSTALL_AI_AGENT_MENUS acquires the lock before its first mutation' `
    ($agentsLock -ge 0 -and $agentsLock -lt $agentsWrite -and $agentsLock -lt $agentsWrite2) ''

Remove-Item -Path $codexRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path $aimRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path $aimConsole -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path $importHive -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
