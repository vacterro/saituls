param(
    [string]$ScriptSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Installers\INSTALL_AI_AGENT_MENUS.PS1')
)

$ErrorActionPreference = 'Stop'

# Behavioural check for the AI-agent menu install transaction (SRC-007 R013).
#
# The defect: the installer mutated the HKCU Console profile (two keys, ~34
# values each) and wrote all OpenCode menu keys BEFORE resolving the Cline CLI,
# so `-Agent All` with a missing Cline left half an install behind.
#
# The harness rewrites the script's HKCU roots and CLI names into a throwaway
# test surface: a fake `saifakecmd.cmd` "CLI" present on PATH, and the Cline
# entry pointed at a name that never resolves. Console keys are redirected to a
# throwaway key, menu keys to a throwaway classes root. Everything the script
# writes stays inside the sandbox and is removed afterwards.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_agent_menus_transaction.ps1
# Exit: 0 = all PASS, 1 = failures.

$sandbox = Join-Path $env:TEMP ('saituls_aim_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null

$classesRoot = "HKCU:\Software\_SAI_AIM_CLASSES_" + [Guid]::NewGuid().ToString('N')
$consoleKey  = "HKCU:\Software\_SAI_AIM_CONSOLE_" + [Guid]::NewGuid().ToString('N')
$fails = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

function New-Replica([string]$src, [string]$dest, [switch]$FailMidway) {
    $text = Get-Content -Raw -LiteralPath $src
    $text = $text.Replace('$classesRoot = "HKCU:\Software\Classes"', "`$classesRoot = '$classesRoot'")
    # Retarget BOTH console value lists (install function + preflight snapshot):
    # the real HKCU:\Console entries become the throwaway test key. Both entries
    # are retargeted (removing one would leave a trailing comma, a PS 5.1 parse
    # error); writing the test key twice is harmless.
    $text = $text.Replace('"HKCU:\Console",', "'$consoleKey',")
    $text = $text.Replace('"HKCU:\Console\%SystemRoot%_System32_WindowsPowerShell_v1.0_powershell.exe"', "'$consoleKey'")
    # Assert-AgentMenus reads HKCU:\Console literally; retarget that too.
    $text = $text.Replace('Get-ItemProperty -LiteralPath "HKCU:\Console"', "Get-ItemProperty -LiteralPath '$consoleKey'")
    # Make the second CLI deterministically unresolvable: cline.cmd exists on a
    # developer PATH, so the missing-dependency case needs its own name.
    $text = $text.Replace('Command = "cline.cmd"', 'Command = "sai_missing_cli.cmd"')
    if ($FailMidway) {
        # Fault injection: throw AFTER the console mutation and AFTER the first
        # agent's keys are written -- where the real missing-CLI failure lands.
        $marker = 'if ($selectedAgents -contains "Cline") {'
        $text = $text.Replace($marker, "if (`$selectedAgents.Count -gt 1) { throw 'injected install failure' }`r`n    $marker")
    }
    Set-Content -LiteralPath $dest -Value $text -Encoding UTF8
}

function Invoke-Installer([string]$replica, [string[]]$extra = @()) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $replica @extra 2>&1
        return [pscustomobject]@{ Output = ($out | ForEach-Object { "$_" }) -join "`n"; Exit = $LASTEXITCODE }
    } finally { $ErrorActionPreference = $old }
}

function Snapshot {
    $out = @()
    foreach ($k in $classesRoot, $consoleKey) {
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

try {
    # The fake "CLI": present on PATH for the child, so OpenCode resolves.
    $bin = Join-Path $sandbox 'bin'
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $bin 'saifakecmd.cmd') -Value '@echo fake' -Encoding Ascii
    $env:PATH = $bin + [IO.Path]::PathSeparator + $env:PATH

    $replica = Join-Path $sandbox 'Installers\INSTALL_AI_AGENT_MENUS.PS1'
    New-Item -ItemType Directory -Path (Split-Path -Parent $replica) -Force | Out-Null
    # The installer derives the launcher as <root>\Scripts\AI_AGENT_LAUNCHER.PS1
    # from its own location, so mirror that layout inside the sandbox.
    New-Item -ItemType Directory -Path (Join-Path $sandbox 'Scripts') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path (Split-Path -Parent (Split-Path -Parent $ScriptSource)) 'Scripts\AI_AGENT_LAUNCHER.PS1') `
        -Destination (Join-Path $sandbox 'Scripts\AI_AGENT_LAUNCHER.PS1') -Force
    New-Replica $ScriptSource $replica

    # 1. THE DEFECT CASE, FIRST ON A CLEAN TREE: -Agent All with an
    #    unresolvable second CLI must fail without leaving anything behind.
    #    (Running the happy path first would let the failing run rewrite
    #    identical state and mask the defect entirely.)
    $beforeAll = Snapshot
    $rAll = Invoke-Installer $replica @('-Agent', 'All')
    $afterAll = Snapshot
    Check '-Agent All with a missing CLI exits nonzero' ($rAll.Exit -ne 0) "exit=$($rAll.Exit)"
    Check 'output names the missing CLI' ($rAll.Output -match 'CLI missing from PATH') ''
    Check 'a failed All on a clean tree leaves nothing behind' ($afterAll -eq $beforeAll) `
        "changed=$($afterAll -ne $beforeAll)"

    # 2. Happy path: OpenCode-only install succeeds and writes its keys.
    $r1 = Invoke-Installer $replica @('-Agent', 'OpenCode')
    $afterGood = Snapshot
    Check 'single-agent install succeeds' ($r1.Exit -eq 0) "exit=$($r1.Exit) out=$($r1.Output.Substring(0,[Math]::Min(120,$r1.Output.Length)))"
    Check 'menu keys were written' ($afterGood -match 'OpenOpenCode') ''

    # 3. A second failing All on top of the good state must restore THAT state.
    $r2 = Invoke-Installer $replica @('-Agent', 'All')
    $afterFail = Snapshot
    Check 'a later failed All leaves the good state untouched' `
        ($r2.Exit -ne 0 -and $afterFail -eq $afterGood) "changed=$($afterFail -ne $afterGood)"

    # 4. Fault injection mid-commit (after console + first agent): full rollback.
    $faulty = Join-Path $sandbox 'INSTALL_AI_AGENT_MENUS.faulty.ps1'
    New-Replica $ScriptSource $faulty -FailMidway
    $r3 = Invoke-Installer $faulty @('-Agent', 'All')
    $afterFault = Snapshot
    Check 'mid-commit failure exits nonzero' ($r3.Exit -ne 0) "exit=$($r3.Exit)"
    Check 'mid-commit failure rolls everything back' ($afterFault -eq $afterGood) `
        "changed=$($afterFault -ne $afterGood)"
} finally {
    Remove-Item $classesRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $consoleKey -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
