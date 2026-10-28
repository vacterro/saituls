param(
    # Every stub this script owns lives under one directory, so a run can be
    # undone by deleting it -- and so a suite that silently depends on a stub
    # fails loudly the moment it is gone.
    [string]$StubRoot = (Join-Path ([IO.Path]::GetTempPath()) 'saituls-ci-stubs'),
    [switch]$Verify,
    # Writing into the Windows directory needs elevation, so it is opt-in: CI
    # runners are elevated, developer machines already have the launcher from
    # the real Python installer (Include_launcher=1).
    [switch]$IncludePythonLauncher,
    [switch]$PublishPath
)

$ErrorActionPreference = 'Stop'

# Every external prerequisite of the test suite, declared in ONE place.
#
# The audit finding: CI provisioned checkout + Python and nothing else, while
# tests/test_regs.py drives AI_AGENT_LAUNCHER.PS1 -SelfTest, which resolves the
# real opencode.cmd / cline.cmd from PATH and probes a user-profile Vintage
# skill; tests/test_shell_menu.ps1 launches a shell verb whose command line
# starts with pyw.exe (resolvable only from the Windows directory / System32);
# tests/test_saipatch.ps1 needs node; three harnesses need the .NET Framework
# csc.exe. A clean runner had none of those, so a green CI run depended on a
# developer machine's ambient state.
#
# Two modes:
#   (default)  provision what can be provisioned, then verify everything.
#   -Verify    assert only -- name each missing prerequisite, exit nonzero.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\ci_fixtures.ps1 -Verify
# Exit: 0 = every prerequisite present, 1 = at least one missing.

$binDir = Join-Path $StubRoot 'bin'
$stubs = @(
    @{ Name = 'opencode.cmd'; Path = (Join-Path $binDir 'opencode.cmd') }
    @{ Name = 'cline.cmd';    Path = (Join-Path $binDir 'cline.cmd') }
)
$vintageSkill = Join-Path $env:USERPROFILE '.agents\skills\vintage\SKILL.md'
$pywExe = Join-Path $env:SystemRoot 'pyw.exe'
$cscExe = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
# tests\test_saipatch.ps1 copies these two declaration files into every fake
# HOME, because they are what Scripts\saipatch\...\common.ps1 reads to prove the
# plugin contract. They ship with a real OpenCode install, never with this
# repository, so a clean runner has neither and the harness dies on the copy.

function New-StubCommand([string]$path, [string]$name) {
    # A resolvable Application command is all the suite needs: the launcher and
    # the menu installer resolve it with Get-Command and never run it. Exiting 0
    # keeps an accidental invocation from looking like an agent crash.
    $lines = @(
        '@echo off'
        "rem SAITULS test fixture -- a resolvable stand-in for the real $name."
        "rem Created by tests\ci_fixtures.ps1. Not an agent CLI."
        "echo SAITULS test stub: $name %*"
        'exit /b 0'
    )
    Set-Content -LiteralPath $path -Value $lines -Encoding Ascii
}

function New-StubSkill([string]$path) {
    $lines = @(
        '# vintage (test fixture)'
        ''
        'Placeholder created by tests\ci_fixtures.ps1 so the AI-agent launcher and'
        'menu installer can assert a global Vintage skill on a clean machine. The'
        'real skill is a user artifact and is not part of this repository.'
    )
    New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
    Set-Content -LiteralPath $path -Value $lines -Encoding UTF8
}

function Resolve-Application([string]$name) {
    return (Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1)
}

if (-not $Verify) {
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
    foreach ($stub in $stubs) {
        if (Test-Path -LiteralPath $stub.Path) {
            Write-Host "KEEP  $($stub.Name) stub already present  $($stub.Path)"
        } else {
            New-StubCommand $stub.Path $stub.Name
            Write-Host "MAKE  $($stub.Name) stub  $($stub.Path)"
        }
    }

    if (Test-Path -LiteralPath $vintageSkill -PathType Leaf) {
        Write-Host "KEEP  Vintage skill already present  $vintageSkill"
    } else {
        New-StubSkill $vintageSkill
        Write-Host "MAKE  Vintage skill fixture  $vintageSkill"
    }

    # Generation 2.x of the SAIPATCH harness anchors its host contract on the
    # real executable resolved from PATH; it no longer copies plugin/SDK
    # declaration files, so nothing here provisions or removes them.

    if ($IncludePythonLauncher) {
        if (Test-Path -LiteralPath $pywExe -PathType Leaf) {
            Write-Host "KEEP  Windows Python launcher already present  $pywExe"
        } else {
            $pythonw = Resolve-Application 'pythonw.exe'
            if (-not $pythonw) { throw 'pythonw.exe is not on PATH; cannot provision the Windows Python launcher' }
            Copy-Item -LiteralPath $pythonw.Source -Destination $pywExe -Force
            Write-Host "MAKE  Windows Python launcher  $pywExe (from $($pythonw.Source))"
        }
    }

    # Prepend for THIS process and, on a GitHub runner, for the following steps.
    $env:PATH = $binDir + [IO.Path]::PathSeparator + $env:PATH
    if ($PublishPath) {
        if ($env:GITHUB_PATH) {
            Add-Content -LiteralPath $env:GITHUB_PATH -Value $binDir
            Write-Host "PATH  published to GITHUB_PATH  $binDir"
        } else {
            Write-Host "PATH  prepended for this process only  $binDir"
        }
    }
    Write-Host '---'
}

$fails = 0
function Assert-Prereq([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

foreach ($stub in $stubs) {
    $resolved = Resolve-Application $stub.Name
    Assert-Prereq "$($stub.Name) resolves as an application" ($null -ne $resolved) `
        $(if ($resolved) { $resolved.Source } else { 'not on PATH -- run this script without -Verify, or install the real CLI' })
}
Assert-Prereq 'the global Vintage skill exists' (Test-Path -LiteralPath $vintageSkill -PathType Leaf) $vintageSkill
Assert-Prereq 'the Windows Python launcher exists' (Test-Path -LiteralPath $pywExe -PathType Leaf) `
    "$pywExe -- a shell verb resolves pyw.exe only from the Windows directory and System32"
$node = Resolve-Application 'node.exe'
Assert-Prereq 'node resolves as an application' ($null -ne $node) `
    $(if ($node) { $node.Source } else { 'not on PATH -- tests\test_saipatch.ps1 imports the staged plugin with node' })
Assert-Prereq 'the .NET Framework compiler exists' (Test-Path -LiteralPath $cscExe -PathType Leaf) $cscExe

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails prerequisite(s) missing)"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
