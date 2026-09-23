param(
    [string]$StubRoot = (Join-Path ([IO.Path]::GetTempPath()) 'saituls-ci-stubs')
)

$ErrorActionPreference = 'Stop'

# Negative control for the CI hermetic-prerequisite layer (T-136, SRC-009 R007).
#
# tests\ci_fixtures.ps1 provisions stubs for the external agent CLIs and the
# Vintage skill so the suite never touches a live external service. The stubs
# live under ONE directory on purpose: deleting it must make every dependent
# harness FAIL LOUDLY, never silently skip -- a green suite that silently
# depends on a stub is exactly the failure this fixture layer exists to
# prevent. This harness proves that property on the two stub-dependent seams:
#
#   1. tests\test_regs.py drives AI_AGENT_LAUNCHER.PS1 -SelfTest, whose probe
#      resolves opencode.cmd / cline.cmd from PATH and asserts the Vintage
#      skill: with the stubs gone the launcher checks must FAIL, not skip.
#   2. tests\test_saipatch.ps1 resolves OpenCode through PATH exactly like the
#      production patcher (generation 2.x anchors the contract on the real
#      executable, not on declaration copies): with the stub shim gone the
#      harness must exit nonzero naming 'opencode', never a silent skip.
#
# The stubs are re-provisioned afterwards, so a suite run that follows this
# harness still sees a complete fixture layer.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_ci_hermetic.ps1
# Exit: 0 = all PASS, 1 = failures.

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$binDir = Join-Path $StubRoot 'bin'
$vintageSkill = Join-Path $env:USERPROFILE '.agents\skills\vintage\SKILL.md'

$fails = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

function Invoke-Probe([string]$exePath, [string[]]$arguments) {
    # Children launched through System.Diagnostics so stderr never becomes a
    # terminating error-record in THIS script (native stderr under
    # $ErrorActionPreference='Stop' would kill the harness mid-control).
    # Windows PowerShell 5.1 has no ProcessStartInfo.ArgumentList, so the
    # arguments are joined into one quoted command line.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exePath
    $psi.WorkingDirectory = $repoRoot
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.Arguments = ($arguments | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }) -join ' '
    $p = [System.Diagnostics.Process]::Start($psi)
    $outText = $p.StandardOutput.ReadToEnd()
    $errText = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    return @{ rc = $p.ExitCode; out = $outText; err = $errText }
}

function Remove-StubLayer {
    if (Test-Path -LiteralPath $StubRoot) {
        Remove-Item -LiteralPath $StubRoot -Recurse -Force
    }
    foreach ($fixture in @($vintageSkill)) {
        if (Test-Path -LiteralPath $fixture -PathType Leaf) {
            Remove-Item -LiteralPath $fixture -Force
        }
    }
    # Drop the stub directory from PATH for every child probe this harness
    # launches, so nothing can resolve the deleted stubs through a stale entry.
    $parts = [Collections.ArrayList]@([Environment]::GetEnvironmentVariable('PATH') -split [regex]::Escape([IO.Path]::PathSeparator))
    for ($i = $parts.Count - 1; $i -ge 0; $i--) {
        if ($parts[$i] -eq $binDir) { $parts.RemoveAt($i) }
    }
    $env:PATH = $parts -join [IO.Path]::PathSeparator
}

try {
    # Arrange: provision the full fixture layer, then prove it is present...
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot 'tests\ci_fixtures.ps1') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'ci_fixtures.ps1 provisioning exited nonzero' }
    Check 'fixture layer provisioned' (
        (Test-Path -LiteralPath (Join-Path $binDir 'opencode.cmd')) -and
        (Test-Path -LiteralPath (Join-Path $binDir 'cline.cmd')) -and
        (Test-Path -LiteralPath $vintageSkill -PathType Leaf)
    ) $StubRoot

    $pwshExe = (Get-Command powershell.exe).Source
    $pre = Invoke-Probe (Get-Command python.exe).Source @((Join-Path $repoRoot 'tests\test_regs.py'))
    Check 'test_regs green with stubs present' ($pre.rc -eq 0) ('exit ' + $pre.rc)

    # Act: remove the whole stub layer...
    Remove-StubLayer

    # Assert: the dependent seams fail LOUDLY. test_regs.py must report the
    # AI-launcher checks as failures (it prints FAIL lines and exits 1) -- a
    # silent skip would be the exact defect this control exists to catch.
    $post = Invoke-Probe (Get-Command python.exe).Source @((Join-Path $repoRoot 'tests\test_regs.py'))
    $regsLoud = ($post.rc -ne 0) -and ($post.out -match 'FAIL')
    Check 'test_regs fails loudly with stubs gone' $regsLoud ("exit $($post.rc); FAIL line present: $($post.out -match 'FAIL')")
    Check 'the loud failure names the AI launcher seam' ($post.out -match 'AI launcher') 'test_regs.py output'
    Check 'the loud failure names the Vintage seam' ($post.out -match 'Vintage') 'test_regs.py output'

    # test_saipatch.ps1 resolves OpenCode through PATH like the production
    # patcher: on a clean runner the stub shim is the ONLY resolvable opencode,
    # so with the stub layer gone the harness must exit nonzero and name the
    # missing resolution target -- never a silent skip. The probe reproduces
    # that clean-runner condition explicitly by stripping every PATH entry that
    # carries an opencode launcher (the real install on a developer machine,
    # the stub on CI), while keeping node resolvable for the unit-test step.
    $nodeExe = (Get-Command node.exe).Source
    $nodeOnly = Join-Path ([IO.Path]::GetTempPath()) ('saipatch-node-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $nodeOnly -Force | Out-Null
    try {
        New-Item -ItemType HardLink -Path (Join-Path $nodeOnly 'node.exe') -Target $nodeExe -ErrorAction SilentlyContinue | Out-Null
        if (-not (Test-Path -LiteralPath (Join-Path $nodeOnly 'node.exe'))) {
            Copy-Item -LiteralPath $nodeExe -Destination (Join-Path $nodeOnly 'node.exe') -Force
        }
    } catch {
        Copy-Item -LiteralPath $nodeExe -Destination (Join-Path $nodeOnly 'node.exe') -Force
    }
    $stripped = @($nodeOnly)
    foreach ($dir in ($env:PATH -split [regex]::Escape([IO.Path]::PathSeparator))) {
        if (-not $dir) { continue }
        $hasLauncher = (Test-Path -LiteralPath (Join-Path $dir 'opencode.cmd')) -or
            (Test-Path -LiteralPath (Join-Path $dir 'opencode.exe')) -or
            (Test-Path -LiteralPath (Join-Path $dir 'opencode.ps1'))
        if ($hasLauncher) { continue }
        if ($stripped -notcontains $dir) { $stripped += $dir }
    }
    $savePath = $env:PATH
    try {
        $env:PATH = ($stripped -join [IO.Path]::PathSeparator)
        $postSai = Invoke-Probe $pwshExe @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $repoRoot 'tests\test_saipatch.ps1'))
    }
    finally { $env:PATH = $savePath; Remove-Item -LiteralPath $nodeOnly -Recurse -Force -ErrorAction SilentlyContinue }
    Check 'test_saipatch fails loudly with no resolvable opencode' ($postSai.rc -ne 0) ("exit $($postSai.rc)")
    Check 'the loud failure names the missing opencode resolution' (($postSai.out + $postSai.err) -match 'opencode') 'test_saipatch.ps1 output'
}
finally {
    # Restore: the next suite run needs a complete fixture layer again.
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot 'tests\ci_fixtures.ps1') | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'FAIL  fixture layer not restored after negative control'
        $script:fails++
    }
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails check(s) failed)"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0