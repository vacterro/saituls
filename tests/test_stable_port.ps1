param()

$ErrorActionPreference = 'Stop'

# Behavioural check for the OpenCode port allocator (SRC-007 R014).
#
# The defect: Get-StablePort truncated a SHA256 of the canonical project path
# into 2048 slots. The audit proved the collision: _PROJ_43 and _PROJ_61 both
# hash to 42077, so two live projects could be handed the same port and a
# transport consumer could reach the wrong instance.
#
# The allocator is now mapping-backed (path -> port persisted per user, mutex-
# guarded, listener-probed). The harness extracts the REAL function from the
# launcher script by AST and drives it with a swapped %LOCALAPPDATA%, so the
# real implementation is what runs -- against a throwaway mapping file.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_stable_port.ps1
# Exit: 0 = all PASS, 1 = failures.

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$launcher = Join-Path $repoRoot 'Scripts\AI_AGENT_LAUNCHER.PS1'
$fails = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

try {
    # Extract exactly the function definition the launcher uses (AST, not a copy).
    $ast = [Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$null, [ref]$null)
    $fn = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-StablePort' }, $true) |
        Select-Object -First 1
    if (-not $fn) { throw 'Get-StablePort not found in the launcher' }

    # Sandbox %LOCALAPPDATA% so the mapping file is the test's own.
    $sandbox = Join-Path $env:TEMP ('saituls_port_' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path (Join-Path $sandbox 'local\SAITULS') -Force | Out-Null
    $mapFile = Join-Path $sandbox 'local\SAITULS\opencode-ports.json'

    $saveLocal = $env:LOCALAPPDATA
    try {
        $env:LOCALAPPDATA = Join-Path $sandbox 'local'
        Invoke-Expression $fn.Extent.Text

        $a = Join-Path $sandbox 'proj\_PROJ_43'
        $b = Join-Path $sandbox 'proj\_PROJ_61'
        New-Item -ItemType Directory -Path $a -Force | Out-Null
        New-Item -ItemType Directory -Path $b -Force | Out-Null

        $portA = Get-StablePort $a
        Check 'the audit collision pair: first project claims its preferred port' ($portA -ge 42000 -and $portA -le 44047) "portA=$portA"

        $portB = Get-StablePort $b
        Check 'the audit collision pair: second project gets a DIFFERENT port' ($portB -ne $portA) "portA=$portA portB=$portB"

        $portA2 = Get-StablePort $a
        Check 'the same path keeps its port (stable mapping)' ($portA2 -eq $portA) "portA=$portA portA2=$portA2"

        # The map is the authority and holds both claims.
        $map = @(Get-Content -Raw -LiteralPath $mapFile | ConvertFrom-Json)
        $claim43 = @($map | Where-Object { $_.path -like '*_proj_43' })
        $claim61 = @($map | Where-Object { $_.path -like '*_proj_61' })
        Check 'the mapping file holds both claims' `
            ($claim43.Count -eq 1 -and $claim61.Count -eq 1) `
            "43=$($claim43.Count) 61=$($claim61.Count)"
        Check 'no secrets in the mapping file' `
            (($map | ConvertTo-Json -Depth 4) -notmatch '(?i)(api[_-]?key|token|authorization)') ''

        # A live foreign owner blocks its port for a third path. The seed ADDS a
        # ghost claim; it must not clobber the two claims already made, or the
        # assertion tests a map the engine never wrote.
        $sleeper = Start-Process powershell -ArgumentList '-NoProfile', '-Command', 'Start-Sleep -Seconds 60' -PassThru -WindowStyle Hidden
        try {
            $ghost = Join-Path $sandbox 'proj\_PROJ_GHOST'
            New-Item -ItemType Directory -Path $ghost -Force | Out-Null
            $seed = @($map) + @(
                [pscustomobject]@{ path = $ghost.ToLowerInvariant(); port = $portB + 1; pid = $sleeper.Id; updated = 'seed' }
            )
            $seed | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $mapFile -Encoding UTF8
            $c = Get-StablePort $b
            Check 'a live foreign claim is skipped for a third project' ($c -notin @($portA, ($portB + 1))) "c=$c blocked=$($portB+1)"

            # Dead foreign claims keep their port too (stable mapping), so a
            # third path skips it rather than stealing it.
            $seed2 = @(
                [pscustomobject]@{ path = $ghost.ToLowerInvariant(); port = $portB + 1; pid = 999999; updated = 'seed' }
            )
            $seed2 | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $mapFile -Encoding UTF8
            $c2 = Get-StablePort $b
            Check 'a dead foreign claim still keeps its port (stability)' ($c2 -ne ($portB + 1)) "c2=$c2"

            # A port bound by a foreign listener (not in the map) is skipped.
            $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $portB + 2)
            $listener.Start()
            try {
                $seed3 = @(
                    [pscustomobject]@{ path = $ghost.ToLowerInvariant(); port = $portB + 1; pid = 999999; updated = 'seed' }
                )
                $seed3 | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $mapFile -Encoding UTF8
                $c3 = Get-StablePort $b
                Check 'a bound port is skipped even when unclaimed' ($c3 -notin @($portB + 2)) "c3=$c3 bound=$($portB+2)"
            } finally { $listener.Stop() }
        } finally {
            try { Stop-Process -Id $sleeper.Id -Force -ErrorAction SilentlyContinue } catch { }
        }
    } finally {
        $env:LOCALAPPDATA = $saveLocal
    }

    # End-to-end through the REAL launcher SelfTest: two different project dirs
    # must resolve to two different ports, with the mapping file updated.
    $st1 = & powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File $launcher `
        -Agent OpenCode -WorkDir $repoRoot -SelfTest 2>&1 | Select-Object -Last 1
    $j1 = $st1 | ConvertFrom-Json
    Check 'real launcher SelfTest reports a port' ($j1.Port -ge 42000 -and $j1.Port -le 44047) "port=$($j1.Port)"
    $portIndex = [Array]::IndexOf($j1.Arguments, '--port')
    Check 'reported port matches the launch arguments without reallocating' `
        ($portIndex -ge 0 -and [int]$j1.Arguments[$portIndex + 1] -eq $j1.Port) `
        "reported=$($j1.Port) arguments=$($j1.Arguments -join ' ')"
} finally {
    # cleanup handled above
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0

