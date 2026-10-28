<#
.SYNOPSIS
    Federation glue between the console launcher and the OPTIONAL SAI Accounts
    control plane. Dot-sourced by CONSOLES.ps1 and by the Explorer cascade
    installer; it holds no state and starts nothing.

.DESCRIPTION
    SAI Accounts is federation, not captivity. Every function here treats "the
    control plane is not installed" as the normal STANDALONE path, not as an
    error: the console launcher keeps every local profile it has always had, and
    the shared entries are an addition on top, never a replacement.

    A profile in consoles.json is concrete unless it sets expand_accounts. An
    expanding profile is a TEMPLATE: it renders one concrete profile per
    canonical account of its provider, with the account's own id, so two
    accounts never share a launch path. The template carries no command at all;
    the executable comes from the engine resolver, never from the registry file,
    so editing consoles.json can never choose what gets executed.

    Duplicate discovery. When a canonical account and a local profile resolve to
    the same physical thing - the same provider profile directory, or the same
    Windows user - they are the same account, and the canonical one takes the
    local profile's slot in the list. Display name is never a matching key: two
    accounts may legitimately be called the same thing.

    Security. The engine is only ever asked to list accounts and to launch one
    named account into a named working directory. No credential, token, cookie
    or login code passes through this file, and nothing here is allowed to store
    one.
#>

Set-StrictMode -Off

# Which environment variable pins a provider's identity, per provider. A local
# profile is "the same account" as a canonical one when these point at the same
# directory. Antigravity has no such variable: it is pinned by which Windows
# user runs it, so the current user is the local identity.
$ACCOUNTS_IDENTITY_KEY = @{
    claude = 'CLAUDE_CONFIG_DIR'
    codex  = 'CODEX_HOME'
}

function Get-AccountsLocale {
    if ($env:SAITULS_LANG) { return ([string]$env:SAITULS_LANG).ToLowerInvariant() }
    try {
        $name = [System.Globalization.CultureInfo]::CurrentUICulture.Name
    } catch { $name = 'en' }
    if ($name) { return (($name -split '-')[0]).ToLowerInvariant() }
    return 'en'
}

# The Explorer cascade parent label comes from the same translation bundle as
# every other menu in this toolkit. It used to be a hardcoded Russian string in
# consoles.json, which meant every other locale got a Russian parent over its
# own translated children.
function Get-ConsolesMenuLabel([string]$RepoRoot) {
    $fallback = 'Open in'
    try { $loc = Get-AccountsLocale } catch { return $fallback }
    if (-not $loc) { return $fallback }
    $file = Join-Path $RepoRoot (Join-Path 'i18n' (Join-Path 'strings' "strings.$loc.json"))
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return $fallback }
    try {
        $doc = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json
        $prop = $doc.PSObject.Properties['menu.open_in']
        if ($prop -and $prop.Value) { return [string]$prop.Value }
    } catch { }
    return $fallback
}

# Returns the engine path, or $null when the control plane is not installed.
# $null is a supported answer, not a failure: that is the standalone machine.
function Resolve-SaiAccountsEngine {
    $candidates = @()
    if ($env:SAI_ACCOUNTS_EXE) { $candidates += $env:SAI_ACCOUNTS_EXE }
    # The canonical install wins over a build sitting in the source tree: a
    # stale repo binary must never shadow the deployed control plane.
    $candidates += 'C:\ProgramData\SAI\Accounts\bin\sai-accounts.exe'
    $candidates += (Join-Path (Join-Path (Split-Path -Parent $PSScriptRoot) 'accounts') (Join-Path 'bin' 'sai-accounts.exe'))
    foreach ($candidate in $candidates) {
        $full = [System.Environment]::ExpandEnvironmentVariables([string]$candidate)
        if ($full -and (Test-Path -LiteralPath $full -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $full).ProviderPath
        }
    }
    $onPath = Get-Command 'sai-accounts.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($onPath) { return $onPath.Source }
    return $null
}

# One identity key: provider plus normalized locator. Two accounts collide only
# when both halves are proven equal; a display name never enters into it.
function Get-AccountsIdentityKey([string]$Provider, [string]$Locator) {
    if ([string]::IsNullOrWhiteSpace($Provider) -or [string]::IsNullOrWhiteSpace($Locator)) { return $null }
    $normalized = $Locator.Trim().TrimEnd('\', '/').ToLowerInvariant()
    if (-not $normalized) { return $null }
    return ("{0}|{1}" -f $Provider.ToLowerInvariant(), $normalized)
}

# Never throws. A missing engine, a nonzero exit, unparsable output or a
# truncated read all produce an empty list, which the caller reads as
# "federation is unavailable" and carries on standalone.
function Get-SaiAccountDescriptors([string]$Engine) {
    if ([string]::IsNullOrWhiteSpace($Engine)) { return @() }
    if (-not (Test-Path -LiteralPath $Engine -PathType Leaf)) { return @() }
    $stdout = $null
    $code = 0
    try {
        $stdout = & $Engine 'list' '--all' 2>$null
        $code = $LASTEXITCODE
    } catch { return @() }
    if ($code -ne 0 -or -not $stdout) { return @() }
    $text = (@($stdout) -join "`n").Trim()
    $start = $text.IndexOf('{')
    if ($start -lt 0) { return @() }
    try { $doc = $text.Substring($start) | ConvertFrom-Json } catch { return @() }
    $rows = @()
    foreach ($a in @($doc.accounts)) {
        if (-not $a) { continue }
        $id = [string]$a.account_id
        if ([string]::IsNullOrWhiteSpace($id)) { continue }
        $meta = $a.provider_metadata
        $locator = $null
        if ($meta) {
            if ($meta.PSObject.Properties['profile_locator']) { $locator = [string]$meta.profile_locator }
            elseif ($meta.PSObject.Properties['windows_user']) { $locator = [string]$meta.windows_user }
        }
        if (-not $locator -and $a.PSObject.Properties['context_label']) { $locator = [string]$a.context_label }
        $state = if ($a.operational_state) { [string]$a.operational_state } else { 'ENABLED' }
        $rows += [pscustomobject]@{
            account_id   = $id
            provider_id  = [string]$a.provider_id
            display_name = if ($a.display_name) { [string]$a.display_name } else { $id }
            compact      = [string]$a.compact_label
            state        = $state
            hidden       = [bool]$a.hidden
            # A globally hidden or non-enabled account leaves the normal launch
            # surface. It still exists, and the SAITULS Accounts tab still shows
            # it; a menu entry that can only ever fail is worse than no entry.
            launchable   = (-not [bool]$a.hidden) -and ($state -eq 'ENABLED')
            locator      = $locator
            identity     = Get-AccountsIdentityKey ([string]$a.provider_id) $locator
        }
    }
    return $rows
}

# The identity a LOCAL profile already claims, or $null when it does not claim
# one (cline, opencode, zcode: these are tools, not accounts).
function Get-LocalProfileIdentity([object]$Profile) {
    if (-not $Profile.PSObject.Properties['provider']) { return $null }
    $provider = ([string]$Profile.provider).ToLowerInvariant()
    if (-not $provider) { return $null }
    $locator = $null
    if ($provider -eq 'antigravity') {
        # Nothing pins Antigravity in an environment variable: the account IS
        # the Windows user running the launcher, so that name is its locator.
        $locator = $env:USERNAME
    } elseif ($ACCOUNTS_IDENTITY_KEY.ContainsKey($provider)) {
        $key = $ACCOUNTS_IDENTITY_KEY[$provider]
        $raw = Get-ConsoleEnvironmentValue $Profile $key
        if ($raw) { $locator = [System.Environment]::ExpandEnvironmentVariables($raw) }
    }
    return Get-AccountsIdentityKey $provider $locator
}

# Static profiles in, one flat list of concrete profiles out. Slots are kept:
# a canonical account that matches a local profile inherits that profile's place
# in the menu, so adding accounts does not reshuffle the whole cascade.
# One environment entry, whatever shape the profile's environment map has.
# This exists because an empty IDictionary is TRUTHY in PowerShell, and
# .PSObject.Properties on one reports Count, Keys, Values and IsReadOnly as if
# they were the map's contents. A profile with an empty environment would then
# try to set seven variables that do not exist. Dictionaries enumerate their
# own entries; only a parsed JSON object has properties to enumerate.
function Get-ConsoleEnvironmentValue([object]$Profile, [string]$Key) {
    $env = $null
    if ($Profile.PSObject.Properties['environment']) { $env = $Profile.environment }
    if ($null -eq $env) { return $null }
    if ($env -is [System.Collections.IDictionary]) {
        if (-not $env.Contains($Key)) { return $null }
        return [string]$env[$Key]
    }
    $prop = $env.PSObject.Properties[$Key]
    if (-not $prop) { return $null }
    return [string]$prop.Value
}

function Expand-AccountProfiles {
    param(
        [object[]]$Profiles,
        [string]$Engine,
        [object[]]$Accounts
    )
    $out = @()
    $claimed = @{}
    foreach ($account in @($Accounts)) {
        if ($account.identity) { $claimed[$account.identity] = $account }
    }
    $templates = @{}
    $matchedProfiles = @{}
    foreach ($p in $Profiles) {
        $isTemplate = ($p.PSObject.Properties['expand_accounts'] -and $p.expand_accounts)
        if ($isTemplate) {
            if ($p.PSObject.Properties['provider'] -and $p.provider) {
                $templates[[string]$p.provider] = $p
            }
            continue
        }
        $local = Get-LocalProfileIdentity $p
        if ($local -and $claimed.ContainsKey($local) -and -not $matchedProfiles.ContainsKey($local)) {
            $matchedProfiles[$local] = $p
        }
    }

    $used = @{}
    $emittedCanonical = $false
    foreach ($p in $Profiles) {
        $isTemplate = ($p.PSObject.Properties['expand_accounts'] -and $p.expand_accounts)
        $local = if (-not $isTemplate) { Get-LocalProfileIdentity $p } else { $null }

        if (($isTemplate -or ($local -and $claimed.ContainsKey($local))) -and -not $emittedCanonical) {
            $emittedCanonical = $true
            foreach ($account in @($Accounts)) {
                if ($account.identity -and $used.ContainsKey($account.identity)) { continue }
                $tpl = $null
                if ($account.identity -and $matchedProfiles.ContainsKey($account.identity)) {
                    $tpl = $matchedProfiles[$account.identity]
                } elseif ($templates.ContainsKey([string]$account.provider_id)) {
                    $tpl = $templates[[string]$account.provider_id]
                }
                if (-not $tpl) { continue }
                if ($account.identity) { $used[$account.identity] = $true }
                if (-not $account.launchable) { continue }
                $out += (ConvertTo-AccountProfile $tpl $account $Engine)
            }
        }

        if ($isTemplate) { continue }
        if ($local -and $claimed.ContainsKey($local)) {
            # The canonical account IS this profile's identity, so it also
            # owns its position and whether it is on the launch surface. A
            # globally hidden or non-enabled account takes the local slot with
            # it: the very same account must not stay on offer as a local
            # console while the operator has it disabled.
            $used[$local] = $true
            continue
        }
        $p | Add-Member -NotePropertyName origin -NotePropertyValue 'local' -Force
        $out += $p
    }
    return $out
}

# One concrete profile for one canonical account. The arguments are the
# canonical account id and the working directory, and nothing else: the engine
# owns the command, the environment, the Windows context and the lifecycle
# check, so this file cannot drift from them.
#
# The working directory is passed as `launch <id> --workdir <dir>`, never as a
# positional second argument. SaiAccounts.cs parses a workdir only after the
# --workdir flag, so a positional directory is silently dropped and the engine
# launches with a null workdir (WORKDIR_UNAVAILABLE/NOT_FOUND). The explicit
# flag is also what SAITULS.cs, the account-plane documentation and the tests
# use; one spelling everywhere.
function ConvertTo-AccountProfile([object]$Template, [object]$Account, [string]$Engine) {
    $id = "account-$($Account.account_id -replace '[^A-Za-z0-9_-]', '-')"
    $title = $Account.display_name
    if ($Template.PSObject.Properties['title_suffix'] -and $Template.title_suffix) {
        $title = "$($Account.display_name) $($Template.title_suffix)"
    }
    $description = "Canonical account $($Account.account_id)."
    if ($Template.PSObject.Properties['description'] -and $Template.description) {
        $description = "$($Template.description) $($Account.account_id)"
    }
    $profile = [ordered]@{
        id                     = $id
        label                  = $Account.display_name
        enabled                = $true
        command                = $Engine
        command_source         = 'accounts_engine'
        arguments              = @('launch', [string]$Account.account_id, '--workdir', '{workdir}')
        environment            = ([ordered]@{})
        working_directory_mode = 'ask'
        requires_admin         = $false
        title_suffix           = $title
        description            = $description
        origin                 = 'shared'
        account_id             = [string]$Account.account_id
        provider               = [string]$Account.provider_id
    }
    if ($Template.PSObject.Properties['icon'] -and $Template.icon) { $profile.icon = [string]$Template.icon }
    return [pscustomobject]$profile
}

# ------------------------------------------------------- launch verdict
# The engine's answer, in machine terms.
#
# A valid JSON envelope is NOT a successful launch. SaiAccounts.cs used to
# return exit 0 whenever LaunchAccount produced a response object, so an engine
# that answered `launch_failed`, `WORKDIR_UNAVAILABLE`, `CONTEXT_OFFLINE` or
# `LIFECYCLE_FORBIDDEN` still read as success to a caller that only looked at
# $LASTEXITCODE -- and the console it was supposed to open never appeared, on a
# window that had already closed. The engine's own exit codes are repaired for
# that, but this adapter must not depend on the engine it is talking to being
# the repaired one: a vendored, installed or stale binary has to fail here too,
# which is exactly the version-skew case the exit code cannot cover.
#
# The rule is one line and has no exceptions: success is status 'launched'.
# Everything else is a failure that names its own reason.
# A trailing envelope on stdout, ignoring anything else the engine printed.
function Get-AccountsEnvelope([string]$Text) {
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $lines = @($Text -split "`r?`n")
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        $line = $lines[$i].Trim()
        if (-not $line.StartsWith('{')) { continue }
        try { $doc = $line | ConvertFrom-Json } catch { continue }
        if ($doc -and ($doc.PSObject.Properties['status'] -or $doc.PSObject.Properties['schema'])) { return $doc }
    }
    return $null
}

# Pure verdict: text in, decision out. No process, no state, no engine.
function Get-AccountLaunchVerdict {
    param(
        [string]$Stdout,
        [int]$ExitCode = 0,
        [string]$Stderr = ''
    )
    $verdict = [pscustomobject]@{
        Ok       = $false
        Schema   = $null
        Status   = $null
        EngineReason = $null
        Reason   = $null
        Pid      = 0
        ExitCode = $ExitCode
        Category = 'ACCOUNT_LAUNCH_FAILED'
    }
    $doc = Get-AccountsEnvelope $Stdout
    if (-not $doc) {
        $verdict.Status = 'NO_ENVELOPE'
        $verdict.Category = 'ACCOUNT_ENGINE_NO_ENVELOPE'
        $reason = "the SAI Accounts engine returned no launch envelope (exit code $ExitCode)"
        if ($Stderr -and $Stderr.Trim()) { $reason = $reason + ': ' + $Stderr.Trim() }
        $verdict.Reason = $reason
        return $verdict
    }
    if ($doc.PSObject.Properties['schema']) { $verdict.Schema = [string]$doc.schema }
    if ($doc.PSObject.Properties['status']) { $verdict.Status = [string]$doc.status }
    if ($doc.PSObject.Properties['reason']) { $verdict.EngineReason = [string]$doc.reason }
    if ($doc.PSObject.Properties['pid']) { $verdict.Pid = [int]$doc.pid }
    if ($verdict.Schema -and $verdict.Schema -ne 'sai.accounts/launch/1') {
        $verdict.Category = 'ACCOUNT_ENGINE_SCHEMA_MISMATCH'
        $verdict.Reason = "the SAI Accounts engine answered with schema '$($verdict.Schema)' instead of 'sai.accounts/launch/1'"
        return $verdict
    }
    # The exact success word, and nothing else. A status of 'launched' with a
    # nonzero exit code is an inconsistent engine and is not trusted either:
    # one of its two answers is wrong, and the operator is owed both of them.
    if ($verdict.Status -eq 'launched') {
        if ($ExitCode -ne 0) {
            $verdict.Category = 'ACCOUNT_ENGINE_INCONSISTENT'
            $verdict.Reason = "the SAI Accounts engine reported 'launched' and then exited $ExitCode"
            return $verdict
        }
        $verdict.Ok = $true
        $verdict.Category = 'LAUNCHED'
        return $verdict
    }
    $statusCategories = @{
        'ACCOUNT_NOT_FOUND'       = 'STALE_EXPLORER_MENU_ENTRY'
        'ACCOUNT_SELECTOR_AMBIGUOUS' = 'ACCOUNT_SELECTOR_AMBIGUOUS'
        'LIFECYCLE_FORBIDDEN'     = 'ACCOUNT_DISABLED'
        'WORKDIR_UNAVAILABLE'     = 'WORKDIR_UNAVAILABLE'
        'CONTEXT_OFFLINE'         = 'CONTEXT_OFFLINE'
        'AUTH_REQUIRED'           = 'AUTH_REQUIRED'
        'EXECUTABLE_MISSING'      = 'EXECUTABLE_MISSING'
        'MALFORMED_PROFILE'       = 'MALFORMED_PROFILE'
        'PROFILE_LOCATOR_MISSING' = 'MALFORMED_PROFILE'
        'BACKEND_UNSUPPORTED'     = 'BACKEND_UNSUPPORTED'
        'PROVIDER_UNSUPPORTED'    = 'PROVIDER_UNSUPPORTED'
        'REGISTRY_UNAVAILABLE'    = 'REGISTRY_UNAVAILABLE'
        'launch_failed'           = 'ACCOUNT_LAUNCH_FAILED'
    }
    if ($verdict.Status -and $statusCategories.ContainsKey($verdict.Status)) {
        $verdict.Category = $statusCategories[$verdict.Status]
    }
    $verdict.Reason = "the SAI Accounts engine refused the launch: $($verdict.Status)"
    foreach ($field in @('error', 'detail', 'reason', 'operational_state', 'execution')) {
        if ($doc.PSObject.Properties[$field] -and [string]$doc.$field) {
            $verdict.Reason = $verdict.Reason + " ($field=$([string]$doc.$field))"
        }
    }
    if ($ExitCode -ne 0) { $verdict.Reason = $verdict.Reason + " [exit $ExitCode]" }
    return $verdict
}

# Windows argv quoting, MSVCRT rules. Start-Process takes one string, so a
# workdir with spaces has to survive the join itself: the engine parses argv[2]
# and argv[3] positionally and a split path is silently the wrong folder.
# A value with no space, tab or quote is passed through untouched, so a drive
# root ('C:\') never becomes a trailing backslash escaping its own quote.
function ConvertTo-AccountsArgument([string]$Value) {
    if ($Value -and $Value -notmatch '[ \t"]') { return $Value }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $slashes = 0
    foreach ($ch in @($Value.ToCharArray())) {
        if ($ch -eq '\') { $slashes++; continue }
        if ($ch -eq '"') {
            if ($slashes -gt 0) { [void]$sb.Append('\' * ($slashes * 2 + 1)) } else { [void]$sb.Append('\') }
            [void]$sb.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) { [void]$sb.Append('\' * $slashes); $slashes = 0 }
        [void]$sb.Append($ch)
    }
    if ($slashes -gt 0) { [void]$sb.Append('\' * ($slashes * 2)) }
    [void]$sb.Append('"')
    return $sb.ToString()
}

# Run one federated launch and answer with the verdict above. Bounded: an
# engine that never answers is a failure the operator can read, not a hidden
# window that outlives the click.
function Invoke-AccountsEngineLaunch {
    param(
        [string]$Engine,
        [string[]]$Arguments,
        [string]$WorkDir,
        [int]$TimeoutMs = 60000
    )
    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    $proc = $null
    try {
        $line = (@($Arguments) | ForEach-Object { ConvertTo-AccountsArgument ([string]$_) }) -join ' '
        $proc = Start-Process -FilePath $Engine -ArgumentList $line -WorkingDirectory $WorkDir `
            -NoNewWindow -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        # Without this the Process object reports an EMPTY ExitCode for a child
        # whose streams were redirected -- measured on Windows PowerShell 5.1,
        # not assumed. An empty code would read as 0, and 0 is the value this
        # whole function exists to stop trusting.
        try { $proc.EnableRaisingEvents = $true } catch { }
        if (-not $proc.WaitForExit($TimeoutMs)) {
            try { $proc.Kill() } catch { }
            return [pscustomobject]@{
                Ok = $false; Schema = 'sai.accounts/launch/1'; Status = 'ENGINE_TIMEOUT'; Pid = 0; ExitCode = -1
                Category = 'ACCOUNT_ENGINE_TIMEOUT'
                Reason = "the SAI Accounts engine did not answer within $([int]($TimeoutMs / 1000)) s"
            }
        }
        # -1, never 0: an exit code that could not be read is unknown, and an
        # unknown code over a 'launched' envelope is an inconsistent engine, not
        # a success.
        $exitCode = -1
        try { if ($null -ne $proc.ExitCode) { $exitCode = [int]$proc.ExitCode } } catch { }
        $stdout = $null; $stderr = $null
        try { $stdout = [IO.File]::ReadAllText($outFile, [Text.Encoding]::UTF8) } catch { }
        try { $stderr = [IO.File]::ReadAllText($errFile, [Text.Encoding]::UTF8) } catch { }
        return Get-AccountLaunchVerdict -Stdout $stdout -ExitCode $exitCode -Stderr $stderr
    } catch {
        return [pscustomobject]@{
            Ok = $false; Schema = 'sai.accounts/launch/1'; Status = 'ENGINE_NOT_STARTED'; Pid = 0; ExitCode = -1
            Category = 'ACCOUNT_ENGINE_NOT_STARTED'
            Reason = "the SAI Accounts engine could not be started: $($_.Exception.Message)"
        }
    } finally {
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-AccountGrantVerdict {
    param([string]$Stdout, [int]$ExitCode, [string]$AccountId, [string]$WorkDir)
    $verdict = [pscustomobject]@{
        Ok = $false; Schema = $null; Status = 'NO_ENVELOPE'; EngineReason = $null
        Category = 'ACCOUNT_ENGINE_NO_ENVELOPE'
        Reason = 'The account service returned no workdir grant result.'; ExitCode = $ExitCode; Pid = 0
    }
    $doc = Get-AccountsEnvelope $Stdout
    if (-not $doc) { return $verdict }
    $verdict.Schema = [string]$doc.schema
    $verdict.Status = [string]$doc.status
    if ($verdict.Schema -ne 'sai.accounts/grant-workdir/1') {
        $verdict.Category = 'ACCOUNT_ENGINE_SCHEMA_MISMATCH'
        $verdict.Reason = 'The account service returned an unexpected workdir grant response.'
        return $verdict
    }
    if ([string]$doc.account_id -cne $AccountId -or [string]$doc.workdir -cne $WorkDir) {
        $verdict.Category = 'ACCOUNT_ENGINE_INCONSISTENT'
        $verdict.Reason = 'The account service returned a grant for a different account or folder.'
        return $verdict
    }
    $verdict.Category = $verdict.Status
    $messages = @{
        ACL_READ_FAILED = 'SAITULS could not read this project folder''s access rules.'
        ACL_MUTATION_FAILED = 'SAITULS could not grant this account access to this project folder.'
        EFFECTIVE_ACCESS_DENIED = 'An existing access rule still blocks this account from this project folder.'
        WORKDIR_REPARSE_REFUSED = 'SAITULS did not modify this folder because its path resolves through a protected or reparse target.'
        WORKDIR_TARGET_REFUSED = 'SAITULS did not modify this protected project folder.'
        WORKDIR_NOT_FOUND = 'The selected project folder no longer exists.'
        WORKDIR_NOT_READABLE = 'SAITULS could not read the selected project folder.'
        HELPER_UNAVAILABLE = 'The account service could not update this project folder''s access rules.'
        LIFECYCLE_FORBIDDEN = 'This account is not enabled for project launches.'
        BACKEND_UNSUPPORTED = 'This account cannot use automatic folder access.'
        CONTEXT_IDENTITY_MISSING = 'This account has no managed Windows identity.'
    }
    if ($messages.ContainsKey($verdict.Status)) { $verdict.Reason = $messages[$verdict.Status] }
    else { $verdict.Reason = "The account service refused folder access: $($verdict.Status)." }
    if ($verdict.Status -in @('granted', 'already_granted')) {
        if ($ExitCode -eq 0) { $verdict.Ok = $true; $verdict.Reason = $null }
        else { $verdict.Category = 'ACCOUNT_ENGINE_INCONSISTENT'; $verdict.Reason = "The account service reported a grant but exited $ExitCode." }
    }
    return $verdict
}

function Invoke-AccountsEngineGrantWorkdir {
    param([string]$Engine, [string]$AccountId, [string]$WorkDir, [int]$TimeoutMs = 60000)
    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $line = (@('grant-workdir', $AccountId, '--workdir', $WorkDir) |
            ForEach-Object { ConvertTo-AccountsArgument ([string]$_) }) -join ' '
        $proc = Start-Process -FilePath $Engine -ArgumentList $line -WorkingDirectory $WorkDir `
            -NoNewWindow -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        try { $proc.EnableRaisingEvents = $true } catch { }
        if (-not $proc.WaitForExit($TimeoutMs)) {
            try { $proc.Kill() } catch { }
            return [pscustomobject]@{
                Ok = $false; Schema = $null; Status = 'ENGINE_TIMEOUT'; Category = 'ACCOUNT_ENGINE_TIMEOUT'
                Reason = 'The account service timed out while granting folder access.'; ExitCode = -1; Pid = 0
            }
        }
        $exitCode = -1
        try { if ($null -ne $proc.ExitCode) { $exitCode = [int]$proc.ExitCode } } catch { }
        $stdout = [IO.File]::ReadAllText($outFile, [Text.Encoding]::UTF8)
        return Get-AccountGrantVerdict -Stdout $stdout -ExitCode $exitCode -AccountId $AccountId -WorkDir $WorkDir
    } catch {
        return [pscustomobject]@{
            Ok = $false; Schema = $null; Status = 'ENGINE_NOT_STARTED'; Category = 'ACCOUNT_ENGINE_NOT_STARTED'
            Reason = "The account service could not grant folder access: $($_.Exception.Message)"; ExitCode = -1; Pid = 0
        }
    } finally {
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-AccountsEngineLaunchWithWorkdirRepair {
    param([string]$Engine, [string]$AccountId, [string]$WorkDir)
    $arguments = @('launch', $AccountId, '--workdir', $WorkDir)
    $initial = Invoke-AccountsEngineLaunch -Engine $Engine -Arguments $arguments -WorkDir $WorkDir
    $result = [pscustomobject]@{ Verdict = $initial; InitialLaunch = $initial; Grant = $null; Retry = $null }
    if ($initial.Schema -ne 'sai.accounts/launch/1' -or
        $initial.Status -ne 'WORKDIR_UNAVAILABLE' -or $initial.EngineReason -ne 'ACCESS_DENIED') {
        return $result
    }
    $grant = Invoke-AccountsEngineGrantWorkdir -Engine $Engine -AccountId $AccountId -WorkDir $WorkDir
    $result.Grant = $grant
    if (-not $grant.Ok) { $result.Verdict = $grant; return $result }
    $retry = Invoke-AccountsEngineLaunch -Engine $Engine -Arguments $arguments -WorkDir $WorkDir
    $result.Retry = $retry
    $result.Verdict = $retry
    if ($retry.Status -eq 'WORKDIR_UNAVAILABLE' -and $retry.EngineReason -eq 'ACCESS_DENIED') {
        $retry.Category = 'WORKDIR_GRANT_INEFFECTIVE'
        $retry.Reason = 'Access was granted, but this account still cannot enter this project folder. An existing access rule is blocking it.'
    }
    return $result
}
