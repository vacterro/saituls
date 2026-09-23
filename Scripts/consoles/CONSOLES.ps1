<#
.SYNOPSIS
    SAITULS agent console launcher. Data-driven, non-elevated.

.DESCRIPTION
    Opens an interactive console for a profile declared in consoles.json.
    The registry stores a bare command name and an argument LIST; neither is
    ever parsed from a shell string, and the command is resolved on PATH
    before anything is launched, so a missing CLI produces a dependency error
    instead of a console that flashes and disappears.

    NOT elevated, deliberately. The existing Explorer-menu OpenCode/Cline
    launcher self-elevates to maximum privilege; that behaviour is NOT
    inherited here and no profile in consoles.json requests it. An autonomous
    AI console running as Administrator by default is a much larger blast
    radius than the convenience is worth. A future profile can opt in with
    requires_admin, and it will have to say so in the registry.

    Environment isolation. Each profile's 'environment' map is applied to
    this launcher process, which exists only to host the one agent, and is
    inherited by the agent. The SAITULS parent process is never touched, and
    HOME is refused as an environment key: overriding it moves far more than
    the agent's configuration.

    Claude account isolation is real but bounded, and the bound is worth
    stating plainly: CLAUDE_CONFIG_DIR separates the two accounts'
    configuration and credentials directories. Upstream Claude Code may still
    discover user-level instructions from paths outside CLAUDE_CONFIG_DIR
    (for example a global CLAUDE.md), so the two profiles are isolated at
    launcher level, not sandboxed from each other. Do not rely on this for
    anything stronger than keeping two accounts' sessions apart.

.PARAMETER SelfTest
    Non-interactive probe. Resolves the command, working directory, title and
    non-secret environment metadata, prints one JSON object, launches
    nothing and sets no environment variable.
#>
[CmdletBinding()]
param(
    [string]$Profile,
    [string]$WorkDir,
    [switch]$SelfTest,
    [switch]$NoNewConsole
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RegistryPath = Join-Path $ScriptDir 'consoles.json'

$SCHEMA_ID = 'saituls.consoles/1'
$SCHEMA_VERSION = 1
$WORKDIR_MODES = @('ask', 'current', 'fixed')
# Refused as environment keys. Each one changes far more than an agent's
# configuration, and a registry typo pointing HOME somewhere new would be a
# very quiet way to break a user's whole shell session.
$FORBIDDEN_ENV = @('HOME', 'USERPROFILE', 'PATH', 'COMSPEC', 'SYSTEMROOT',
    'WINDIR', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'PATHEXT')

# ------------------------------------------------------------------ registry
function Import-ConsoleRegistry([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "consoles.json not found: $Path"
    }
    $doc = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($doc.schema -ne $SCHEMA_ID) {
        throw "unsupported console registry schema '$($doc.schema)' (expected '$SCHEMA_ID')"
    }
    if ([int]$doc.schema_version -ne $SCHEMA_VERSION) {
        throw "unsupported console registry schema_version $($doc.schema_version)"
    }
    if (-not $doc.profiles -or @($doc.profiles).Count -eq 0) {
        throw "console registry declares no profiles"
    }
    foreach ($p in $doc.profiles) {
        if ([string]::IsNullOrWhiteSpace($p.id)) { throw "a console profile has no id" }
        if ($p.id -notmatch '^[A-Za-z0-9_-]+$') { throw "invalid console profile id: $($p.id)" }
        if ([string]::IsNullOrWhiteSpace($p.command)) { throw "profile '$($p.id)' has no command" }
        if ($p.command -match '[\\/]' -or $p.command -match '\s') {
            throw "profile '$($p.id)' command must be a bare executable name resolved on PATH, got '$($p.command)'"
        }
        if ($null -ne $p.arguments) {
            foreach ($a in @($p.arguments)) {
                if ($a -isnot [string]) { throw "profile '$($p.id)' arguments must all be strings" }
            }
        }
        if ($p.working_directory_mode -and ($WORKDIR_MODES -notcontains $p.working_directory_mode)) {
            throw "profile '$($p.id)' has unknown working_directory_mode '$($p.working_directory_mode)'"
        }
        if ($p.environment) {
            foreach ($key in $p.environment.PSObject.Properties.Name) {
                if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
                    throw "profile '$($p.id)' has an invalid environment key '$key'"
                }
                if ($FORBIDDEN_ENV -contains $key.ToUpperInvariant()) {
                    throw "profile '$($p.id)' may not set the environment variable '$key'"
                }
                if ($p.environment.$key -isnot [string]) {
                    throw "profile '$($p.id)' environment value '$key' must be a string"
                }
            }
        }
        foreach ($field in @('requires_path', 'icon')) {
            if ($p.PSObject.Properties[$field] -and $p.$field -isnot [string]) {
                throw "profile '$($p.id)' $field must be a string"
            }
        }
        foreach ($field in @('ensure_directories', 'script_candidates')) {
            if (-not $p.PSObject.Properties[$field]) { continue }
            foreach ($d in @($p.$field)) {
                if ($d -isnot [string] -or [string]::IsNullOrWhiteSpace($d)) {
                    throw "profile '$($p.id)' $field must all be non-empty strings"
                }
            }
        }
    }
    return $doc
}

function Get-ConsoleProfile($Registry, [string]$Id) {
    $p = @($Registry.profiles | Where-Object { $_.id -eq $Id }) | Select-Object -First 1
    if (-not $p) { throw "unknown console profile: '$Id'" }
    if ($p.PSObject.Properties['enabled'] -and -not $p.enabled) {
        throw "console profile '$Id' is disabled"
    }
    return $p
}

function Resolve-ConsoleCommand([object]$ConsoleProfile) {
    $names = @($ConsoleProfile.command, "$($ConsoleProfile.command).cmd",
        "$($ConsoleProfile.command).exe", "$($ConsoleProfile.command).bat")
    $found = $null
    foreach ($name in $names) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($cmd) { $found = $cmd.Source; break }
    }
    if (-not $found) {
        throw ("'$($ConsoleProfile.command)' was not found on PATH. " +
               "Install the CLI for '$($ConsoleProfile.label)' and reopen this launcher. " +
               "Looked for: " + ($names -join ', '))
    }
    [void](Resolve-ConsoleScript $ConsoleProfile)
    if ($ConsoleProfile.PSObject.Properties['requires_path'] -and $ConsoleProfile.requires_path) {
        $required = [System.Environment]::ExpandEnvironmentVariables([string]$ConsoleProfile.requires_path)
        if (-not (Test-Path -LiteralPath $required)) {
            throw ("'$($ConsoleProfile.label)' is not installed: $required is missing. " +
                   "Install it and reopen this launcher.")
        }
    }
    return $found
}

# script_candidates: %VAR%-expanded paths, first existing one becomes the
# {script} argument. Lets one profile follow a CLI that lives in different
# places on different machines (an env override first, then known installs).
function Resolve-ConsoleScript([object]$ConsoleProfile) {
    if (-not $ConsoleProfile.PSObject.Properties['script_candidates']) { return $null }
    $tried = @()
    foreach ($c in @($ConsoleProfile.script_candidates)) {
        $path = [System.Environment]::ExpandEnvironmentVariables([string]$c)
        if (-not [string]::IsNullOrWhiteSpace($path) -and $path -notmatch '%' -and
            (Test-Path -LiteralPath $path -PathType Leaf)) { return $path }
        $tried += $path
    }
    throw ("'$($ConsoleProfile.label)' is not installed. Looked for: " + ($tried -join ', '))
}

# Argument list for one launch. Each entry stays one argument: %VAR% is
# expanded and the literal token {workdir} becomes the project folder, and
# nothing is ever split or handed to a shell.
function Resolve-ConsoleArguments([object]$ConsoleProfile, [string]$Dir) {
    $out = @()
    $script = $null
    foreach ($a in @($ConsoleProfile.arguments)) {
        if ($null -eq $a) { continue }
        $value = ([System.Environment]::ExpandEnvironmentVariables([string]$a)).Replace('{workdir}', $Dir)
        if ($value.Contains('{script}')) {
            if (-not $script) { $script = Resolve-ConsoleScript $ConsoleProfile }
            $value = $value.Replace('{script}', $script)
        }
        $out += $value
    }
    return , [string[]]$out
}

# Project folders arrive from Explorer as "%V." so a drive root ("C:\") does
# not end in a backslash that would escape the closing quote. GetFullPath
# drops the trailing dot again.
function Resolve-WorkDir([string]$Dir) {
    $clean = $Dir.Trim().Trim('"')
    return [System.IO.Path]::GetFullPath($clean)
}

function Resolve-ConsoleEnvironment([object]$ConsoleProfile) {
    $map = [ordered]@{}
    if ($ConsoleProfile.environment) {
        foreach ($key in $ConsoleProfile.environment.PSObject.Properties.Name) {
            $raw = [string]$ConsoleProfile.environment.$key
            $map[$key] = [System.Environment]::ExpandEnvironmentVariables($raw)
        }
    }
    return $map
}

# Project stays at the head of the title, below the portable 255-character
# OSC ceiling. Same contract as the Explorer-menu launcher's title guard; it
# is restated here rather than shared, because the existing launcher carries
# a self-elevation block that this subsystem must not inherit.
function Get-ProjectTitle([string]$Path, [string]$Suffix) {
    $trimmed = $Path.TrimEnd([System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar)
    $projectName = Split-Path -Leaf $trimmed
    if ([string]::IsNullOrWhiteSpace($projectName)) { $projectName = $Path }
    $title = "$projectName | $Suffix | $Path"
    if ($title.Length -gt 240) { $title = $title.Substring(0, 237) + '...' }
    return $title
}

function Select-WorkingDirectory([string]$Caption, [string]$Start) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = $Caption
    if ($Start -and (Test-Path -LiteralPath $Start -PathType Container)) {
        $dialog.SelectedPath = $Start
    }
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return $null }
    return $dialog.SelectedPath
}

$registry = Import-ConsoleRegistry $RegistryPath

# ------------------------------------------------------------------ selftest
if ($SelfTest) {
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    $rows = @()
    $ids = if ($Profile) { @($Profile) } else { @($registry.profiles.id) }
    foreach ($id in $ids) {
        $p = $null; $command = $null; $err = $null; $resolvedArguments = @()
        $dir = if ($WorkDir) { Resolve-WorkDir $WorkDir } else { (Get-Location).Path }
        try {
            $p = Get-ConsoleProfile $registry $id
            $command = Resolve-ConsoleCommand $p
            $resolvedArguments = Resolve-ConsoleArguments $p $dir
        } catch { $err = $_.Exception.Message; $command = $null }
        $envMap = if ($p) { Resolve-ConsoleEnvironment $p } else { [ordered]@{} }
        $title = if ($p) { Get-ProjectTitle $dir $p.title_suffix } else { $null }
        $projectName = Split-Path -Leaf $dir.TrimEnd('\')
        $rows += [pscustomobject]@{
            Id            = $id
            Label         = if ($p) { $p.label } else { $null }
            Command       = $command
            CommandName   = if ($p) { $p.command } else { $null }
            Resolved      = [bool]$command
            Arguments     = [string[]]@($resolvedArguments)
            ArgumentCount = @(if ($p) { @($p.arguments) } else { @() }).Count
            WorkDir       = $dir
            Title         = $title
            ProjectFirst  = if ($title) { $title.StartsWith($projectName) } else { $false }
            RequiresAdmin = if ($p) { [bool]$p.requires_admin } else { $false }
            Elevated      = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
                                [Security.Principal.WindowsBuiltInRole]::Administrator)
            SelfElevates  = $false
            EnvironmentKeys   = @($envMap.Keys)
            EnvironmentValues = $envMap
            EnvironmentLeaked = @($envMap.Keys | Where-Object {
                                    [System.Environment]::GetEnvironmentVariable($_, 'Process') })
            Error         = $err
        }
    }
    $payload = [pscustomobject]@{
        ok       = (@($rows | Where-Object { $_.Error }).Count -eq 0)
        schema   = $registry.schema
        profiles = $rows
    }
    $payload | ConvertTo-Json -Depth 6 -Compress
    exit 0
}

# ------------------------------------------------------------------ picker
if (-not $Profile) {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $C_BG = [System.Drawing.ColorTranslator]::FromHtml('#1A1810')
    $C_TEXT = [System.Drawing.ColorTranslator]::FromHtml('#D4C89A')
    $C_TEXT2 = [System.Drawing.ColorTranslator]::FromHtml('#9C9371')
    $C_RAISED = [System.Drawing.ColorTranslator]::FromHtml('#3D372A')
    $C_SURFACE = [System.Drawing.ColorTranslator]::FromHtml('#332E22')
    $C_BDARK = [System.Drawing.ColorTranslator]::FromHtml('#100E08')
    $C_MUTED = [System.Drawing.ColorTranslator]::FromHtml('#6E674E')

    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'SAITULS - AI consoles'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.StartPosition = 'CenterScreen'
    $form.ClientSize = New-Object System.Drawing.Size(460, 300)
    $form.BackColor = $C_BG
    $form.Font = New-Object System.Drawing.Font('Verdana', 8)
    $form.KeyPreview = $true

    function New-PickerLabel([string]$Text, [int]$X, [int]$Y, $Color, [int]$Size = 8) {
        $l = New-Object System.Windows.Forms.Label
        $l.Text = $Text; $l.Left = $X; $l.Top = $Y; $l.AutoSize = $true
        $l.ForeColor = $Color; $l.BackColor = $C_BG
        $l.Font = New-Object System.Drawing.Font('Verdana', $Size)
        $form.Controls.Add($l); return $l
    }
    New-PickerLabel 'AI CONSOLES' 12 10 $C_TEXT 10 | Out-Null
    New-PickerLabel 'Non-elevated. Each console runs in the folder you pick.' 12 32 $C_TEXT2 | Out-Null

    $list = New-Object System.Windows.Forms.ListBox
    $list.Left = 12; $list.Top = 56; $list.Width = 436; $list.Height = 120
    $list.BackColor = $C_SURFACE; $list.ForeColor = $C_TEXT
    $list.BorderStyle = 'FixedSingle'
    $form.Controls.Add($list)

    $detail = New-Object System.Windows.Forms.Label
    $detail.Left = 12; $detail.Top = 184; $detail.Width = 436; $detail.Height = 64
    $detail.ForeColor = $C_TEXT2; $detail.BackColor = $C_BG
    $form.Controls.Add($detail)

    $visible = @()
    foreach ($p in $registry.profiles) {
        if ($p.PSObject.Properties['enabled'] -and -not $p.enabled) { continue }
        $resolved = $null
        try { $resolved = Resolve-ConsoleCommand $p } catch { }
        $flag = if ($resolved) { '' } else { '  [command missing]' }
        [void]$list.Items.Add("$($p.label)$flag")
        $visible += [pscustomobject]@{ Profile = $p; Resolved = $resolved }
    }
    if ($list.Items.Count -gt 0) { $list.SelectedIndex = 0 }

    function Update-Detail {
        if ($list.SelectedIndex -lt 0) { $detail.Text = ''; return }
        $entry = $visible[$list.SelectedIndex]
        $envText = (@(Resolve-ConsoleEnvironment $entry.Profile).Keys) -join ', '
        if (-not $envText) { $envText = 'none' }
        $lines = @(
            $entry.Profile.description,
            "Command: $($entry.Profile.command)   Resolved: $(if ($entry.Resolved) { $entry.Resolved } else { 'NOT FOUND ON PATH' })",
            "Child environment: $envText   Elevation: none"
        )
        $detail.Text = ($lines -join "`n")
    }
    $list.add_SelectedIndexChanged({ Update-Detail })
    Update-Detail

    function New-PickerButton([string]$Text, [int]$X, [int]$Y, [int]$W) {
        $b = New-Object System.Windows.Forms.Button
        $b.Text = $Text; $b.Left = $X; $b.Top = $Y; $b.Width = $W; $b.Height = 26
        $b.BackColor = $C_RAISED; $b.ForeColor = $C_TEXT
        $b.FlatStyle = 'Flat'; $b.FlatAppearance.BorderColor = $C_BDARK
        $form.Controls.Add($b); return $b
    }
    $btnOpen = New-PickerButton 'Open console' 12 258 130
    $btnClose = New-PickerButton 'Close' 150 258 90
    $script:chosen = $null
    $btnOpen.add_Click({
        if ($list.SelectedIndex -ge 0) {
            $script:chosen = $visible[$list.SelectedIndex].Profile
            $form.Close()
        }
    })
    $btnClose.add_Click({ $form.Close() })
    $form.add_KeyDown({ if ($_.KeyCode -eq 'Escape') { $form.Close() } })
    [void]$form.ShowDialog()
    if (-not $script:chosen) { exit 0 }

    $chosenProfile = $script:chosen
    $mode = if ($chosenProfile.working_directory_mode) { $chosenProfile.working_directory_mode } else { 'ask' }
    $dir = $WorkDir
    if (-not $dir -and $mode -eq 'ask') {
        $dir = Select-WorkingDirectory "$($chosenProfile.label) - pick the project folder" (Get-Location).Path
        if (-not $dir) { exit 0 }
    }
    if (-not $dir) { $dir = (Get-Location).Path }

    # A fresh, non-elevated console for the agent. No -Verb RunAs anywhere in
    # this subsystem: see the elevation note in the header.
    $self = $MyInvocation.MyCommand.Definition
    Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', $self, '-Profile', $chosenProfile.id, '-WorkDir', $dir)
    exit 0
}

# ------------------------------------------------------------------ launch
$consoleProfile = Get-ConsoleProfile $registry $Profile
$mode = if ($consoleProfile.working_directory_mode) { $consoleProfile.working_directory_mode } else { 'ask' }
if (-not $WorkDir -and $mode -eq 'ask') {
    $WorkDir = Select-WorkingDirectory "$($consoleProfile.label) - pick the project folder" (Get-Location).Path
    if (-not $WorkDir) { exit 0 }
}
if (-not $WorkDir) { $WorkDir = (Get-Location).Path }
$resolvedDir = Resolve-WorkDir $WorkDir
if (-not (Test-Path -LiteralPath $resolvedDir -PathType Container)) {
    throw "Project directory not found: $resolvedDir"
}

# Resolve BEFORE launching: a missing CLI is a message, not a vanished window.
$command = Resolve-ConsoleCommand $consoleProfile
$arguments = Resolve-ConsoleArguments $consoleProfile $resolvedDir
$title = Get-ProjectTitle $resolvedDir $consoleProfile.title_suffix

if ($consoleProfile.requires_admin) {
    # Only a profile that explicitly declares it; none ship declaring it.
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        throw ("profile '$($consoleProfile.id)' declares requires_admin. Start this " +
               "console from an elevated shell; the launcher does not elevate itself.")
    }
}

# Child environment. This process exists only to host this one agent, so the
# variables set here reach the agent and nothing else; the SAITULS process
# that started the picker is untouched.
# An empty value REMOVES the variable from the child (for example an
# inherited OPENAI_API_KEY that would override an isolated Codex login).
foreach ($entry in (Resolve-ConsoleEnvironment $consoleProfile).GetEnumerator()) {
    if ([string]::IsNullOrEmpty($entry.Value)) {
        Remove-Item -Path ("Env:" + $entry.Key) -ErrorAction SilentlyContinue
    } else {
        Set-Item -Path ("Env:" + $entry.Key) -Value $entry.Value
    }
}
if ($consoleProfile.PSObject.Properties['ensure_directories']) {
    foreach ($d in @($consoleProfile.ensure_directories)) {
        $expanded = [System.Environment]::ExpandEnvironmentVariables([string]$d)
        if (-not (Test-Path -LiteralPath $expanded -PathType Container)) {
            New-Item -ItemType Directory -Path $expanded -Force | Out-Null
        }
    }
}

Set-Location -LiteralPath $resolvedDir
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

# Ctrl+C must keep interrupting the agent (ENABLE_PROCESSED_INPUT) and
# Ctrl+V / right-click must keep pasting (QuickEdit + Insert). Neither is
# assumed: the launcher sets the flags and installs no Ctrl+C handler of its
# own, so the agent owns the signal.
if (-not ('SaitulsConsole.Mode' -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Threading;

namespace SaitulsConsole
{
    public static class Mode
    {
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr GetStdHandle(int nStdHandle);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetConsoleMode(IntPtr handle, out uint mode);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetConsoleMode(IntPtr handle, uint mode);

        private const int STD_INPUT_HANDLE = -10;
        private const uint ENABLE_PROCESSED_INPUT = 0x0001;
        private const uint ENABLE_QUICK_EDIT_MODE = 0x0040;
        private const uint ENABLE_INSERT_MODE = 0x0020;
        private const uint ENABLE_EXTENDED_FLAGS = 0x0080;

        public static bool EnableCopyPasteAndBreak()
        {
            IntPtr h = GetStdHandle(STD_INPUT_HANDLE);
            uint mode;
            if (!GetConsoleMode(h, out mode)) { return false; }
            mode |= ENABLE_PROCESSED_INPUT | ENABLE_EXTENDED_FLAGS
                  | ENABLE_QUICK_EDIT_MODE | ENABLE_INSERT_MODE;
            return SetConsoleMode(h, mode);
        }
    }

    public sealed class TitleGuard : IDisposable
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetConsoleTitleW(string title);

        private readonly string title;
        private readonly Timer timer;

        public TitleGuard(string value)
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
try { [void][SaitulsConsole.Mode]::EnableCopyPasteAndBreak() } catch { }

$guard = [SaitulsConsole.TitleGuard]::new($title)
$exitCode = 0
try {
    & $command @arguments
    $exitCode = $LASTEXITCODE
} finally {
    $guard.Dispose()
}

if ($exitCode -ne 0) {
    # A console that vanishes makes a failed start indistinguishable from a
    # clean exit, so a nonzero result keeps the window and says what happened.
    Write-Warning "$($consoleProfile.label) exited with code $exitCode."
    Write-Host 'Press Enter to close this window...' -ForegroundColor Yellow
    Read-Host | Out-Null
}
exit $exitCode
