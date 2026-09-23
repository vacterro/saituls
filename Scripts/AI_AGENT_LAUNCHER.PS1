param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("OpenCode", "Cline")]
    [string]$Agent,

    [Parameter(Mandatory = $true)]
    [string]$WorkDir,

    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"

function Resolve-AgentCommand([string]$Name) {
    $commandName = if ($Name -eq "OpenCode") { "opencode.cmd" } else { "cline.cmd" }
    $command = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $command) {
        throw "$Name command not found on PATH ($commandName). Reinstall the CLI, then rerun the context-menu installer."
    }
    return $command.Source
}

function Get-StablePort([string]$Path) {
    # Collision-safe port allocation. The SHA256-derived port stays as the
    # PREFERRED start, but a hash into 2048 slots is not unique: two project
    # paths can want one port, and two live OpenCode instances cannot share it.
    # A per-user mapping file is the authority:
    #   %LOCALAPPDATA%\SAITULS\opencode-ports.json
    #   [{ path, port, pid, updated }]
    # Rules:
    #   - a path keeps its claimed port across restarts (stable for transport);
    #   - a port claimed by a DIFFERENT path is never handed out while that
    #     owner is alive, and stays stable afterwards (recycled only when every
    #     slot is exhausted and it is unbound);
    #   - a port already bound by any listener is skipped;
    #   - all map access is under a machine-wide mutex, so two simultaneous
    #     launches cannot claim the same port.
    $canonical = $Path.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar).ToLowerInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($canonical)
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    $preferred = 42000 + (([int]$hash[0] -shl 8 -bor [int]$hash[1]) % 2048)

    $mapDir = Join-Path $env:LOCALAPPDATA 'SAITULS'
    $mapFile = Join-Path $mapDir 'opencode-ports.json'
    $mutex = $null
    $held = $false
    try {
        $mutex = New-Object System.Threading.Mutex($false, 'Global\SAITULS_OPENCODE_PORT_MAP')
        $held = $mutex.WaitOne(5000)
        if (-not $held) { return $preferred }   # contested beyond tolerance: deterministic fallback
        $map = @()
        if (Test-Path -LiteralPath $mapFile) {
            try { $map = @(Get-Content -Raw -LiteralPath $mapFile | ConvertFrom-Json) } catch { $map = @() }
        }

        function Test-PortBound([int]$Port) {
            $l = $null
            try {
                $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
                $l.Start()
                return $false
            } catch { return $true }
            finally { if ($l) { try { $l.Stop() } catch { } } }
        }
        function Test-PidAlive([int]$PidValue) {
            if ($PidValue -le 0) { return $false }
            return $null -ne (Get-Process -Id $PidValue -ErrorAction SilentlyContinue)
        }

        $mine = $map | Where-Object { $_.path -eq $canonical } | Select-Object -First 1

        # A single-entry map round-trips as a bare PSCustomObject and, in some
        # cases, without a settable 'port'. Rebuild every entry into a uniform
        # shape once, so all reads and writes below are safe.
        $map = @($map | ForEach-Object {
            [pscustomobject]@{
                path    = [string]$_.path
                port    = $(if ($_.port -is [System.Array]) { [int]@($_.port)[-1] } else { [int]$_.port })
                pid     = $(if ($_.pid -is [System.Array]) { [int]@($_.pid)[-1] } else { [int]$_.pid })
                updated = [string]$_.updated
            }
        })
        $mine = $map | Where-Object { $_.path -eq $canonical } | Select-Object -First 1

        # ConvertFrom-Json in Windows PowerShell 5.1 hands back object[] for a
        # numeric property on a single-entry round trip, so take the last
        # element before coercing. Every port read goes through this helper.
        function ConvertTo-PortNumber($Value) {
            if ($Value -is [System.Array]) { return [int]@($Value)[-1] }
            return [int]$Value
        }

        $port = $null
        if ($mine) {
            # This path's stable claim. Keep it unless it is now unusable:
            # bound by a foreign listener, or claimed by a live foreign entry
            # (a repaired map conflict).
            $conflict = $map | Where-Object {
                $_.path -ne $canonical -and $_.port -eq $mine.port -and (Test-PidAlive ([int]$_.pid))
            } | Select-Object -First 1
            if (-not (Test-PortBound $mine.port) -and -not $conflict) {
                $port = $mine.port
            }
        }

        if ($null -eq $port) {
            $start = if ($mine) { $mine.port } else { $preferred }
            for ($offset = 0; $offset -lt 2048; $offset++) {
                $candidate = 42000 + ((($start - 42000 + $offset) % 2048 + 2048) % 2048)
                if ($mine -and $candidate -eq $mine.port) { continue }
                $conflict = $map | Where-Object {
                    $_.port -eq $candidate -and $_.path -ne $canonical -and (Test-PidAlive ([int]$_.pid))
                } | Select-Object -First 1
                if ($conflict) { continue }
                if (Test-PortBound $candidate) { continue }
                $port = $candidate
                break
            }
            if ($null -eq $port) { $port = $preferred }   # exhausted: deterministic fallback
        }

        $stamp = (Get-Date).ToUniversalTime().ToString('o')
        if ($mine) {
            $mine.port = $port; $mine.pid = $PID; $mine.updated = $stamp
        } else {
            $map += [pscustomobject]@{ path = $canonical; port = $port; pid = $PID; updated = $stamp }
        }
        New-Item -ItemType Directory -Path $mapDir -Force | Out-Null
        $tmp = "$mapFile.tmp"
        $map | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $tmp -Encoding UTF8
        Move-Item -LiteralPath $tmp -Destination $mapFile -Force
        return $port
    }
    finally {
        if ($held) { try { $mutex.ReleaseMutex() } catch { } }
        if ($mutex) { $mutex.Dispose() }
    }
}

function Get-ProjectTitle([string]$Path, [string]$Name) {
    $trimmed = $Path.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $projectName = Split-Path -Leaf $trimmed
    if ([string]::IsNullOrWhiteSpace($projectName)) {
        $projectName = $Path
    }

    # Project stays at the head. Keep the string below the 255-character
    # portable OSC title ceiling used by terminal applications.
    $title = "$projectName | $Name YOLO | $Path"
    if ($title.Length -gt 240) {
        $title = $title.Substring(0, 237) + "..."
    }
    return $title
}

$resolvedDir = [System.IO.Path]::GetFullPath($WorkDir)
if (-not (Test-Path -LiteralPath $resolvedDir -PathType Container)) {
    throw "Project directory not found: $resolvedDir"
}

# Maximum-privilege elevation: relaunch self as Administrator when the
# context-menu host is not elevated. SelfTest stays non-elevated so the
# test suite can probe the resolved command without a UAC prompt.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $SelfTest -and -not $isAdmin) {
    $pwsh = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $selfArgs = "-NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Agent $Agent -WorkDir `"$resolvedDir`""
    Start-Process -FilePath $pwsh -ArgumentList $selfArgs -Verb RunAs
    exit
}

$agentCommand = Resolve-AgentCommand $Agent
$arguments = if ($Agent -eq "OpenCode") {
    $ocPort = Get-StablePort $resolvedDir
    @($resolvedDir, "--auto", "--port", "$ocPort")
} else {
    @("--cwd", $resolvedDir, "--auto-approve", "true", "--tui")
}
$windowTitle = Get-ProjectTitle $resolvedDir $Agent
$vintageSkill = Join-Path $env:USERPROFILE ".agents\skills\vintage\SKILL.md"

if (-not ("ContextMenu.ConsoleTitleGuard" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Threading;

namespace ContextMenu
{
    public sealed class ConsoleTitleGuard : IDisposable
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetConsoleTitleW(string title);

        private readonly string title;
        private readonly Timer timer;

        public ConsoleTitleGuard(string value)
        {
            title = value;
            SetConsoleTitleW(title);
            timer = new Timer(_ => SetConsoleTitleW(title), null, 250, 250);
        }

        public void Dispose()
        {
            timer.Dispose();
            SetConsoleTitleW(title);
        }
    }
}
"@
}

if ($SelfTest) {
    $trimmedDir = $resolvedDir.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $projectName = Split-Path -Leaf $trimmedDir
    if ([string]::IsNullOrWhiteSpace($projectName)) { $projectName = $resolvedDir }
    [pscustomobject]@{
        Agent = $Agent
        Command = $agentCommand
        Arguments = $arguments
        Port = if ($Agent -eq "OpenCode") { $ocPort } else { $null }
        WorkDir = $resolvedDir
        Title = $windowTitle
        ProjectFirst = $windowTitle.StartsWith($projectName)
        TitleGuard = $null -ne ("ContextMenu.ConsoleTitleGuard" -as [type])
        VintageSkill = Test-Path -LiteralPath $vintageSkill -PathType Leaf
    } | ConvertTo-Json -Compress
    exit 0
}

Set-Location -LiteralPath $resolvedDir
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $Host.UI.RawUI.BackgroundColor = "Black"
    $Host.UI.RawUI.ForegroundColor = "White"
} catch {
    # Redirected hosts have no RawUI. Interactive Explorer launches do.
}

$titleGuard = [ContextMenu.ConsoleTitleGuard]::new($windowTitle)
try {
    & $agentCommand @arguments
    $agentExitCode = $LASTEXITCODE
} finally {
    $titleGuard.Dispose()
}

if ($agentExitCode -ne 0) {
    Write-Warning "$Agent exited with code $agentExitCode."
    Write-Host "Press Enter to close this window..." -ForegroundColor Yellow
    Read-Host | Out-Null
}
exit $agentExitCode
