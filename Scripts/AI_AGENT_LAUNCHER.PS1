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
    @($resolvedDir, "--auto")
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
