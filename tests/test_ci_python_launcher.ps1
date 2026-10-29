param([string]$Implementation = (Join-Path $PSScriptRoot 'ci_python_launcher.ps1'))
$ErrorActionPreference = 'Stop'
. $Implementation
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
public static class SaiLauncherErrorMode {
    [DllImport("kernel32.dll")] public static extern uint SetErrorMode(uint mode);
}
'@
$previousErrorMode = [SaiLauncherErrorMode]::SetErrorMode(0x8003)
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('saituls-python-launcher-' + [Guid]::NewGuid().ToString('N'))
$fails = 0
function Check([string]$Name, [bool]$Ok) {
    if ($Ok) { Write-Host "PASS  $Name" } else { Write-Host "FAIL  $Name"; $script:fails++ }
}
New-Item -ItemType Directory -Path $sandbox | Out-Null
try {
    $launcher = Join-Path $sandbox 'isolated launcher.exe'
    $recorder = Join-Path $sandbox 'argument recorder.pyw'
    $result = Join-Path $sandbox 'result.json'
    # The same recorder tests the real installed interpreter and isolated shim.
    @'
import json, pathlib, sys
pathlib.Path(__file__).with_name('result.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')
sys.exit(23)
'@ | Set-Content -LiteralPath $recorder -Encoding UTF8
    $pythonw = (Get-Command pythonw.exe -CommandType Application | Select-Object -First 1).Source
    New-SaiCiPythonLauncher $pythonw $launcher
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = $sandbox
    # A real installation must not be rescued by interpreter DLLs on PATH.
    # The installed pythonw still finds its DLLs beside itself; a copied
    # pythonw in an unrelated directory must prove independent launchability.
    $psi.EnvironmentVariables['PATH'] = $env:SystemRoot + ';' + (Join-Path $env:SystemRoot 'System32')
    # End with a quoted drive root exactly like the Explorer verb. The shim
    # must preserve the Python parser's raw input, not repair this quoting.
    $psi.Arguments = '"' + $recorder + '" "with spaces" "" "' + [char]0x00F5 + '" "C:\"'
    $psi.FileName = $pythonw
    $direct = [Diagnostics.Process]::Start($psi)
    if (-not $direct.WaitForExit(10000)) { throw 'Direct interpreter timed out' }
    Check 'known-good installed interpreter records arguments' (Test-Path -LiteralPath $result)
    if (-not (Test-Path -LiteralPath $result)) { throw 'Known-good interpreter unavailable' }
    $expected = [IO.File]::ReadAllText($result)
    Check 'known-good interpreter returns deliberate nonzero status' ($direct.ExitCode -eq 23)
    Remove-Item -LiteralPath $result -Force
    $psi.FileName = $launcher
    $forwarded = [Diagnostics.Process]::Start($psi)
    if (-not $forwarded.WaitForExit(10000)) { throw 'Isolated launcher timed out' }
    Check 'isolated launcher runs the real Python worker' (Test-Path -LiteralPath $result)
    Check 'isolated launcher preserves raw Windows arguments' ((Test-Path -LiteralPath $result) -and [IO.File]::ReadAllText($result) -ceq $expected)
    Check 'isolated launcher propagates child failure' ($forwarded.ExitCode -eq 23)
    $direct.Dispose()
    $forwarded.Dispose()
} finally {
    [void][SaiLauncherErrorMode]::SetErrorMode($previousErrorMode)
    $resolved = [IO.Path]::GetFullPath($sandbox)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path $resolved -Leaf) -notlike 'saituls-python-launcher-*') { throw 'Unsafe cleanup target' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
if ($fails) { Write-Host "FAILED ($fails failures)"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
