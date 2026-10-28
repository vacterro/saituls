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

    Federation. SAI Accounts is an OPTIONAL control plane. When it is
    installed, a profile marked 'expand_accounts' renders one console per
    canonical account and launches through sai-accounts, which owns the
    identity, the environment, the Windows context and the lifecycle check.
    When it is not installed those templates contribute nothing and every
    local profile here works exactly as before: SAITULS is never a
    prerequisite for opening a console, and a canonical account that points
    at the same profile directory as a local one is shown once, not twice.

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
    [switch]$NoNewConsole,
    # auto = use the control plane when it is installed, ignore it when it is
    # not. off  = local profiles only, even with the control plane installed
    # (the standalone path, and how the standalone suite is run on a machine
    # that does have one). on   = refuse to run without the control plane.
    [ValidateSet('auto', 'on', 'off')]
    [string]$Federation = 'auto',
    # Set by the Explorer cascade command (see INSTALL_CONSOLES_MENU.PS1). It
    # arms the launch boundary below: every failure from that point on ends in
    # a durable dialog plus one sanitized line in a bounded local log, instead
    # of an error printed into a console that closes before it can be read. A
    # click in Explorer has no terminal to scroll back through, so a message
    # that is only printed is a message that was never delivered.
    [switch]$FromExplorer,
    # Diagnostics only: keep the durable log line, skip the modal dialog. The
    # test suite has no interactive window station; this is never how the
    # Explorer entry is installed.
    [switch]$NoErrorDialog
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RegistryPath = Join-Path $ScriptDir 'consoles.json'

# Federation glue. Dot-sourced, not a module: engine resolution, account
# expansion, the launch verdict and the locale label live there so this
# launcher and the Explorer cascade installer cannot disagree about what the
# menu shows.
. (Join-Path $ScriptDir 'ACCOUNTS.ps1')

# ------------------------------------------------------------ boundary
# Explorer invocation has no durable surface of its own. Before this boundary
# existed, any throw in preflight (unknown or stale profile id, launcher path
# drift, missing CLI, unreadable project folder, a federated launch the engine
# refused) printed an error into the console Explorer had just created and that
# console closed immediately: the operator saw a black window flash and
# nothing else. Every Explorer-context launch now ends in exactly one of two
# states -- LAUNCHED, or a durable failure that names its own reason.
#
# The log is bounded and sanitized: no environment map, no credential, no
# token, no child command line. Only the fields that turn the next
# "a black window appeared and vanished" into a ten-second diagnosis.
$script:ConsoleLogFile = $env:SAITULS_CONSOLE_LOG
if ([string]::IsNullOrWhiteSpace($script:ConsoleLogFile)) {
    $logBase = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($logBase)) { $logBase = [System.IO.Path]::GetTempPath() }
    $script:ConsoleLogFile = Join-Path (Join-Path $logBase 'SAITULS') (Join-Path 'logs' 'consoles-launch.log')
}
$script:ConsoleLogLimit = 512KB
$script:LauncherClock = [System.Diagnostics.Stopwatch]::StartNew()
$script:LauncherPath = $PSCommandPath

# Secret-shaped assignments are masked wherever they appear in a reason. The
# log is meant to be pasted into a report, so it must never be the place a
# token leaks out of.
function Protect-ConsoleDiagnosticText([string]$Text) {
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    try {
        return [regex]::Replace($Text,
            '(?i)\b(password|passwd|secret|token|api[_-]?key|apikey|authorization|bearer|cookie)\b\s*[=:]\s*\S+',
            '$1=<redacted>')
    } catch { return $Text }
}

# One line per launch attempt, appended and rotated. Never throws: a launcher
# that cannot write its own diagnostic must still report the failure it was
# called to report.
function Write-ConsoleDiagnostic($Fields) {
    try {
        $dir = Split-Path -Parent $script:ConsoleLogFile
        if ($dir -and -not (Test-Path -LiteralPath $dir -PathType Container)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        if (Test-Path -LiteralPath $script:ConsoleLogFile -PathType Leaf) {
            if ((Get-Item -LiteralPath $script:ConsoleLogFile).Length -gt $script:ConsoleLogLimit) {
                Move-Item -LiteralPath $script:ConsoleLogFile `
                    -Destination ($script:ConsoleLogFile + '.1') -Force
            }
        }
        $row = [ordered]@{
            schema     = 'saituls.consoles/launch-log/1'
            timestamp  = (Get-Date).ToUniversalTime().ToString('o')
            outcome    = [string]$Fields.Outcome
            category   = [string]$Fields.Category
            profile    = [string]$Fields.ProfileId
            account    = [string]$Fields.AccountId
            workdir    = [string]$Fields.WorkDir
            launcher   = $script:LauncherPath
            engine     = [string]$Fields.EnginePath
            status     = [string]$Fields.Status
            exit_code  = $Fields.ExitCode
            child_pid  = $Fields.ChildPid
            source     = $(if ($FromExplorer) { 'explorer' } else { 'cli' })
            elapsed_ms = $script:LauncherClock.ElapsedMilliseconds
            reason     = Protect-ConsoleDiagnosticText ([string]$Fields.Reason)
            initial_launch = $Fields.InitialLaunch
            grant      = $Fields.Grant
            retry      = $Fields.Retry
        }
        Add-Content -LiteralPath $script:ConsoleLogFile -Value ($row | ConvertTo-Json -Compress -Depth 4) `
            -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch { }
}

# A failure that carries its own machine category, so the boundary reports a
# decision ('STALE_EXPLORER_MENU_ENTRY') rather than a sentence it had to
# interpret.
function New-ConsoleFailure([string]$Category, [string]$Message) {
    $ex = New-Object System.ApplicationException($Message)
    $ex.Data['category'] = $Category
    return $ex
}

function Get-ConsoleFailureCategory($ErrorRecord) {
    try {
        if ($ErrorRecord -and $ErrorRecord.Exception -and $ErrorRecord.Exception.Data -and
            $ErrorRecord.Exception.Data.Contains('category')) {
            return [string]$ErrorRecord.Exception.Data['category']
        }
    } catch { }
    return 'SCRIPT_PREFLIGHT_EXCEPTION'
}

function Show-ConsoleLaunchFailure([string]$ProfileId, [string]$AccountId, [string]$Category,
                                  [string]$Reason, [string]$WorkDir) {
    if ($Category -eq 'WORKDIR_GRANT_INEFFECTIVE' -or $script:LaunchFailure.Grant -or
        ($Category -eq 'WORKDIR_UNAVAILABLE' -and $script:LaunchFailure.EngineReason -eq 'NOT_FOUND')) {
        $message = $Reason
        if ($Category -eq 'WORKDIR_UNAVAILABLE' -and $script:LaunchFailure.EngineReason -eq 'NOT_FOUND') {
            $message = 'The selected project folder no longer exists.'
        }
        $lines = @('The agent console could not be started.', '',
            (Protect-ConsoleDiagnosticText $message), '', "Folder: $WorkDir", '')
    } else {
        $lines = @(
            'The agent console could not be started.',
            '',
            "Profile:  $ProfileId",
            "Account:  $AccountId",
            "Category: $Category",
            "Folder:   $WorkDir",
            '',
            (Protect-ConsoleDiagnosticText $Reason),
            ''
        )
    }
    if ($Category -eq 'STALE_EXPLORER_MENU_ENTRY') {
        $lines += @(
            'This Explorer entry points at a profile or account that no longer',
            'exists, or is no longer launchable. Rebuild the menu: SAITULS >',
            'Explorer menus > Install or repair, or run',
            'Installers\INSTALL_CONSOLES_MENU.PS1.',
            ''
        )
    }
    $lines += "Diagnostic: $script:ConsoleLogFile"
    $body = ($lines -join "`r`n")
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        [void][System.Windows.Forms.MessageBox]::Show($body, 'SAITULS - console launch failed',
            [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
        return $true
    } catch {
        try { [Console]::Error.WriteLine($body) } catch { }
        return $false
    }
}

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
        # A template names no executable at all. Its command comes from the
        # engine resolver, so the registry file can never choose what runs.
        if ($p.PSObject.Properties['expand_accounts'] -and $p.expand_accounts) {
            if ($p.PSObject.Properties['command']) {
                throw "account template '$($p.id)' must not carry a command"
            }
            if ($p.PSObject.Properties['arguments']) {
                throw "account template '$($p.id)' must not carry arguments"
            }
            if ([string]::IsNullOrWhiteSpace([string]$p.provider)) {
                throw "account template '$($p.id)' must name a provider"
            }
        } else {
            if ([string]::IsNullOrWhiteSpace($p.command)) { throw "profile '$($p.id)' has no command" }
            if ($p.command -match '[\\/]' -or $p.command -match '\s') {
                throw "profile '$($p.id)' command must be a bare executable name resolved on PATH, got '$($p.command)'"
            }
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
    if (-not $p) {
        # A profile id generated from a canonical account (see ACCOUNTS.ps1)
        # that no longer expands is a menu entry left over from an account that
        # was deleted, renamed, hidden, disabled or frozen after the cascade was
        # written. The shell can still hand it back, so it must fail as a named
        # condition rather than as a black window.
        if ($Id -like 'account-*') {
            throw (New-ConsoleFailure 'STALE_EXPLORER_MENU_ENTRY' (
                "stale Explorer menu entry: unknown console profile '$Id'. " +
                "The cascade was built for an account that is no longer launchable; rebuild the menu."))
        }
        throw (New-ConsoleFailure 'PROFILE_UNKNOWN' "unknown console profile: '$Id'")
    }
    if ($p.PSObject.Properties['enabled'] -and -not $p.enabled) {
        throw (New-ConsoleFailure 'PROFILE_DISABLED' "console profile '$Id' is disabled")
    }
    return $p
}

function Resolve-ConsoleCommand([object]$ConsoleProfile) {
    # A federated profile already carries the engine's absolute path, resolved
    # by ACCOUNTS.ps1. It is never taken from consoles.json, and it is never
    # looked up on PATH: this is the control plane's own binary.
    if ($ConsoleProfile.PSObject.Properties['command_source'] -and
        $ConsoleProfile.command_source -eq 'accounts_engine') {
        $engine = [string]$ConsoleProfile.command
        if (-not $engine -or -not (Test-Path -LiteralPath $engine -PathType Leaf)) {
            throw (New-ConsoleFailure 'ACCOUNT_ENGINE_NOT_INSTALLED' ("The SAI Accounts control plane is not installed: '$engine' is missing. " +
                   "Install it, or remove the shared profile. Local profiles keep working either way."))
        }
        return $engine
    }
    $names = @($ConsoleProfile.command, "$($ConsoleProfile.command).cmd",
        "$($ConsoleProfile.command).exe", "$($ConsoleProfile.command).bat")
    $found = $null
    foreach ($name in $names) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($cmd) { $found = $cmd.Source; break }
    }
    if (-not $found) {
        throw (New-ConsoleFailure 'EXECUTABLE_MISSING' ("'$($ConsoleProfile.command)' was not found on PATH. " +
               "Install the CLI for '$($ConsoleProfile.label)' and reopen this launcher. " +
               "Looked for: " + ($names -join ', ')))
    }
    [void](Resolve-ConsoleScript $ConsoleProfile)
    if ($ConsoleProfile.PSObject.Properties['requires_path'] -and $ConsoleProfile.requires_path) {
        $required = [System.Environment]::ExpandEnvironmentVariables([string]$ConsoleProfile.requires_path)
        if (-not (Test-Path -LiteralPath $required)) {
            throw (New-ConsoleFailure 'REQUIRES_PATH_MISSING' ("'$($ConsoleProfile.label)' is not installed: $required is missing. " +
                   "Install it and reopen this launcher."))
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
        if ($path -notmatch '%' -and (Test-Path -LiteralPath $path -PathType Leaf)) { return $path }
        $tried += $path
    }
    throw (New-ConsoleFailure 'SCRIPT_CANDIDATES_MISSING' ("'$($ConsoleProfile.label)' is not installed. Looked for: " + ($tried -join ', ')))
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
    $env = $null
    if ($ConsoleProfile.PSObject.Properties['environment']) { $env = $ConsoleProfile.environment }
    if ($null -eq $env) { return $map }
    if ($env -is [System.Collections.IDictionary]) {
        foreach ($key in $env.Keys) {
            $map[[string]$key] = [System.Environment]::ExpandEnvironmentVariables([string]$env[$key])
        }
        return $map
    }
    foreach ($key in $env.PSObject.Properties.Name) {
        $raw = [string]$env.$key
        $map[$key] = [System.Environment]::ExpandEnvironmentVariables($raw)
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

# Everything from here to the end of the file is inside the boundary. `exit`
# inside a try is not catchable in PowerShell, so the success paths below still
# leave with their own exit code; only a throw reaches the handler.
try {
$registry = Import-ConsoleRegistry $RegistryPath

# Federation. The control plane is optional, and $null engine is the ordinary
# standalone machine: the templates below then expand to nothing and this list
# is exactly the local profile set it has always been.
$accountsEngine = Resolve-SaiAccountsEngine
if ($Federation -eq 'on' -and -not $accountsEngine) {
    throw 'federation was requested but the SAI Accounts control plane is not installed'
}
if ($Federation -eq 'off') { $accountsEngine = $null }
$accountRows = @(Get-SaiAccountDescriptors $accountsEngine)
$registry.profiles = @(Expand-AccountProfiles -Profiles @($registry.profiles) `
    -Engine $accountsEngine -Accounts $accountRows)

# ------------------------------------------------------------------ selftest
if ($SelfTest) {
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    $rows = @()
    $ids = if ($Profile) { @($Profile) } else { @($registry.profiles.id) }
    foreach ($id in $ids) {
        $p = $null; $command = $null; $err = $null; $errCategory = $null
        try {
            $p = Get-ConsoleProfile $registry $id
            $command = Resolve-ConsoleCommand $p
        } catch { $err = $_.Exception.Message; $errCategory = Get-ConsoleFailureCategory $_ }
        $dir = if ($WorkDir) { Resolve-WorkDir $WorkDir } else { (Get-Location).Path }
        $envMap = if ($p) { Resolve-ConsoleEnvironment $p } else { [ordered]@{} }
        $title = if ($p) { Get-ProjectTitle $dir $p.title_suffix } else { $null }
        $projectName = Split-Path -Leaf $dir.TrimEnd('\')
        # Resolve-ConsoleArguments returns a wrapped array so PowerShell cannot
        # unroll it; unwrap it here or a three-argument profile reports as one
        # space-joined string, which is exactly what this output exists to
        # prevent.
        $flatArgs = @()
        if ($p) { $flatArgs = @(Resolve-ConsoleArguments $p $dir)[0] }
        $rows += [pscustomobject]@{
            Id            = $id
            Label         = if ($p) { $p.label } else { $null }
            Command       = $command
            CommandName   = if ($p) { $p.command } else { $null }
            Resolved      = [bool]$command
            Arguments     = [string[]]$flatArgs
            ArgumentCount = @(if ($p) { @($p.arguments) } else { @() }).Count
            WorkDir       = $dir
            Title         = $title
            ProjectFirst  = if ($title) { $title.StartsWith($projectName) } else { $false }
            RequiresAdmin = if ($p) { [bool]$p.requires_admin } else { $false }
            Origin        = if ($p -and $p.PSObject.Properties['origin']) { [string]$p.origin } else { 'local' }
            AccountId     = if ($p -and $p.PSObject.Properties['account_id']) { [string]$p.account_id } else { $null }
            Elevated      = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
                                [Security.Principal.WindowsBuiltInRole]::Administrator)
            SelfElevates  = $false
            EnvironmentKeys   = @($envMap.Keys)
            EnvironmentValues = $envMap
            EnvironmentLeaked = @($envMap.Keys | Where-Object {
                                    [System.Environment]::GetEnvironmentVariable($_, 'Process') })
            Error         = $err
            ErrorCategory = $errCategory
        }
    }
    $payload = [pscustomobject]@{
        ok       = (@($rows | Where-Object { $_.Error }).Count -eq 0)
        schema   = $registry.schema
        # Federation is reported, never required: engine $null with shared
        # 0 is a healthy standalone machine, not a degraded one.
        federation = [pscustomobject]@{
            requested      = $Federation
            engine         = $accountsEngine
            accounts_seen  = @($accountRows).Count
            shared_profiles = @($rows | Where-Object { $_.Origin -eq 'shared' }).Count
        }
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
        # Origin is shown once, in the list, and in full in the detail pane.
        # A shared account is centrally managed; a local one is this machine's
        # own configuration and stays editable here.
        if ($p.PSObject.Properties['origin'] -and $p.origin -eq 'shared') { $flag = "$flag  [shared]" }
        [void]$list.Items.Add("$($p.label)$flag")
        $visible += [pscustomobject]@{ Profile = $p; Resolved = $resolved }
    }
    if ($list.Items.Count -gt 0) { $list.SelectedIndex = 0 }

    function Update-Detail {
        if ($list.SelectedIndex -lt 0) { $detail.Text = ''; return }
        $entry = $visible[$list.SelectedIndex]
        $envText = (@(Resolve-ConsoleEnvironment $entry.Profile).Keys) -join ', '
        if (-not $envText) { $envText = 'none' }
        $origin = if ($entry.Profile.PSObject.Properties['origin']) { [string]$entry.Profile.origin } else { 'local' }
        $lines = @(
            $entry.Profile.description,
            "Command: $($entry.Profile.command)   Resolved: $(if ($entry.Resolved) { $entry.Resolved } else { 'NOT FOUND ON PATH' })",
            "Origin: $origin   Child environment: $envText   Elevation: none"
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

# Federated launch. The console the operator asked for is opened BY the
# engine, in the account's own Windows context; this process is a bootstrap and
# must not add a window of its own (the Explorer entry starts it hidden for
# exactly this reason). The engine is asked once, bounded, and its structured
# answer -- not its exit code -- decides success: a valid JSON envelope is not a
# successful launch, and an older engine that answers launch_failed and still
# exits 0 must not read as one here either.
$federated = ($consoleProfile.PSObject.Properties['command_source'] -and
              $consoleProfile.command_source -eq 'accounts_engine')
$accountId = $null
if ($consoleProfile.PSObject.Properties['account_id']) {
    $accountId = [string]$consoleProfile.account_id
}

if ($federated) {
    $attempt = Invoke-AccountsEngineLaunchWithWorkdirRepair -Engine $command -AccountId $accountId -WorkDir $resolvedDir
    $verdict = $attempt.Verdict
    $stages = @{
        InitialLaunch = @{ status = $attempt.InitialLaunch.Status; reason = $attempt.InitialLaunch.EngineReason }
        Grant = $(if ($attempt.Grant) { @{ status = $attempt.Grant.Status } } else { $null })
        Retry = $(if ($attempt.Retry) { @{ status = $attempt.Retry.Status; reason = $attempt.Retry.EngineReason } } else { $null })
    }
    if (-not $verdict.Ok) {
        # Hand the engine's own fields to the boundary and let IT write the one
        # line for this attempt. Writing here as well produced two records for
        # one click, and the boundary's copy was the poorer of the two: it could
        # not know the engine path, the status word or the exit code it had
        # just observed.
        $script:LaunchFailure = [pscustomobject]@{
            Status = $verdict.Status; EngineReason = $verdict.EngineReason
            Engine = $command; Pid = $verdict.Pid
            ExitCode = $verdict.ExitCode; WorkDir = $resolvedDir
            InitialLaunch = $stages.InitialLaunch; Grant = $stages.Grant; Retry = $stages.Retry
        }
        throw (New-ConsoleFailure $verdict.Category $verdict.Reason)
    }
    Write-ConsoleDiagnostic @{
        Outcome   = 'launched'
        Category  = 'LAUNCHED'
        Status    = $verdict.Status
        ProfileId = $consoleProfile.id
        AccountId = $accountId
        WorkDir   = $resolvedDir
        EnginePath = $command
        ChildPid  = $verdict.Pid
        ExitCode  = $verdict.ExitCode
        Reason    = $null
        InitialLaunch = $stages.InitialLaunch
        Grant = $stages.Grant
        Retry = $stages.Retry
    }
    exit 0
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
        private const uint ENABLE_MOUSE_INPUT = 0x0010;
        private const uint ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200;

        public static bool EnableCopyPasteAndBreak()
        {
            IntPtr h = GetStdHandle(STD_INPUT_HANDLE);
            uint mode;
            if (!GetConsoleMode(h, out mode)) { return false; }
            mode |= ENABLE_PROCESSED_INPUT | ENABLE_EXTENDED_FLAGS
                  | ENABLE_QUICK_EDIT_MODE | ENABLE_INSERT_MODE;
            return SetConsoleMode(h, mode);
        }

        public static bool MaintainQuickEdit()
        {
            IntPtr h = GetStdHandle(STD_INPUT_HANDLE);
            uint mode;
            if (!GetConsoleMode(h, out mode)) { return false; }
            // T-301: the uncovered mouse-on / QuickEdit-off branch. Restoring
            // QuickEdit steals mouse events from an application that asked for
            // them, and it fights a VT-input TUI that owns its own selection, so
            // restoration is a plain-classic-mode repair and nothing else.
            if ((mode & ENABLE_MOUSE_INPUT) == 0
                && (mode & ENABLE_VIRTUAL_TERMINAL_INPUT) == 0
                && (mode & ENABLE_QUICK_EDIT_MODE) == 0)
            {
                return SetConsoleMode(h, mode | ENABLE_QUICK_EDIT_MODE | ENABLE_EXTENDED_FLAGS | ENABLE_INSERT_MODE);
            }
            return true;
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
            Mode.MaintainQuickEdit();
            timer = new Timer(_ => {
                SetConsoleTitleW(title);
                Mode.MaintainQuickEdit();
            }, null, 100, 100);
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

# Resolved and about to run: the console this window IS. Recorded before the
# child starts, so a crash inside the agent still leaves a trace of the launch
# that produced it.
Write-ConsoleDiagnostic @{
    Outcome   = 'launched'
    Category  = 'LAUNCHED'
    ProfileId = $consoleProfile.id
    AccountId = $accountId
    WorkDir   = $resolvedDir
    EnginePath = $null
    Status    = 'local'
    ChildPid  = 0
    ExitCode  = $null
    Reason    = $null
}

$guard = [SaitulsConsole.TitleGuard]::new($title)
$exitCode = 0
try {
    & $command @arguments
    $exitCode = $LASTEXITCODE
} finally {
    $guard.Dispose()
}

Write-ConsoleDiagnostic @{
    Outcome   = $(if ($exitCode -eq 0) { 'exited' } else { 'exited_nonzero' })
    Category  = 'CHILD_EXIT'
    ProfileId = $consoleProfile.id
    AccountId = $accountId
    WorkDir   = $resolvedDir
    EnginePath = $null
    Status    = 'local'
    ChildPid  = 0
    ExitCode  = $exitCode
    Reason    = $null
}

if ($exitCode -ne 0) {
    # A console that vanishes makes a failed start indistinguishable from a
    # clean exit, so a nonzero result keeps the window and says what happened.
    Write-Warning "$($consoleProfile.label) exited with code $exitCode."
    Write-Host 'Press Enter to close this window...' -ForegroundColor Yellow
    Read-Host | Out-Null
}
exit $exitCode
}
catch {
    # The boundary. One failure, one durable outcome: a sanitized log line plus,
    # for a click in Explorer, a dialog that stays until it is read. Nothing is
    # echoed into a window that is about to close, and the exit code is nonzero
    # so a caller that only looks at the exit status still sees a failure.
    $category = Get-ConsoleFailureCategory $_
    $reason = $_.Exception.Message
    $failedAccount = $null
    $failedDir = $WorkDir
    $engineStatus = $null
    $enginePath = $null
    $childPid = 0
    $exitCode = 1
    if ($script:LaunchFailure) {
        $engineStatus = $script:LaunchFailure.Status
        $enginePath = $script:LaunchFailure.Engine
        $childPid = $script:LaunchFailure.Pid
        $exitCode = $script:LaunchFailure.ExitCode
        $failedDir = $script:LaunchFailure.WorkDir
    }
    try {
        if ($registry -and $Profile) {
            $failed = @($registry.profiles | Where-Object { $_.id -eq $Profile }) | Select-Object -First 1
            if ($failed -and $failed.PSObject.Properties['account_id']) {
                $failedAccount = [string]$failed.account_id
            }
        }
    } catch { }
    Write-ConsoleDiagnostic @{
        Outcome   = 'failed'
        Category  = $category
        Status    = $engineStatus
        ProfileId = $Profile
        AccountId = $failedAccount
        WorkDir   = $failedDir
        EnginePath = $enginePath
        ChildPid  = $childPid
        ExitCode  = $exitCode
        Reason    = $reason
        InitialLaunch = $(if ($script:LaunchFailure) { $script:LaunchFailure.InitialLaunch } else { $null })
        Grant = $(if ($script:LaunchFailure) { $script:LaunchFailure.Grant } else { $null })
        Retry = $(if ($script:LaunchFailure) { $script:LaunchFailure.Retry } else { $null })
    }
    if ($FromExplorer) {
        if (-not $NoErrorDialog) {
            [void](Show-ConsoleLaunchFailure $Profile $failedAccount $category $reason $failedDir)
        }
        exit 1
    }
    # A caller that owns a console reads the message there; the durable dialog
    # belongs to the caller that has no console to read (Explorer, and any
    # other GUI launcher that passes -FromExplorer).
    Write-Host ''
    Write-Host ("${category}: $reason") -ForegroundColor Red
    Write-Host "Diagnostic: $script:ConsoleLogFile" -ForegroundColor DarkGray
    exit 1
}
