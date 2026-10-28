<#
.SYNOPSIS
    Keeps the NTFS USN change journals healthy so Everything (and any other
    USN consumer) never falls back into a constant "rebuild the index" loop.

.DESCRIPTION
    Root cause this guards against: the registry value

        HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\NtfsAllowUsnMinSize1Mb

    set to 1 makes NTFS clamp every USN journal to 1 MB (the normal minimum is
    32 MB). A 1 MB journal wraps almost immediately under normal file churn, so
    Everything re-reads / rebuilds its index constantly and hammers the machine.

    Three operations that used to be one are now separate, and the default is
    READ-ONLY:

      DIAGNOSIS (default; -Diagnose / the legacy -VerifyOnly alias)
        Reads the clamp value and every fixed NTFS volume's journal size.
        Writes nothing to the registry, never touches a journal.
        Exit 0 = healthy, 1 = unhealthy, 2 = state could not be determined,
        3 = the requested operation needs elevation.

      REGISTRY CORRECTION (-CorrectRegistry)
        Removes NtfsAllowUsnMinSize1Mb. That value is the whole recurrence
        mechanism, so its absence is what stops a 1 MB clamp from coming back.
        It is reversible and destroys nothing. NTFS caches the policy until the
        next boot, so removing it in this run is reported as REBOOT REQUIRED
        instead of being followed by a journal resize that cannot take effect.

      JOURNAL REPAIR (-RepairJournals)
        Enlarges journals below MinJournalMB to TargetJournalMB with
        `fsutil usn createjournal` -- NON-DESTRUCTIVE: it sets the maximum size
        and the allocation delta of the journal, it does not delete it. A new
        size is not always visible immediately, so a resize is polled for
        ResizeWaitSeconds before it is called a failure.

    An existing journal is NEVER deleted or recreated automatically. The
    delete+create fallback runs only with -AllowJournalRecreate plus
    -RecreateVolume <drive-letter>, which together are the explicit,
    single-volume operator authorization for that destructive step: deleting
    a journal forces a full volume rescan and can disturb every other USN
    consumer on the volume (including Everything). A journal already at or
    above the healthy minimum is never deleted, even on the confirmed volume.

    A volume whose journal state cannot be read is never mutated: an unreadable
    query is reported with its exit code and first output line, and the run
    exits 2. A volume with no active journal is only reported; it is repaired
    with the same non-destructive createjournal call.

    The installed scheduled task runs the read-only check plus -CorrectRegistry
    and nothing else, so it can never resize or delete a journal by itself. It
    exits with the diagnosis result, so an unhealthy volume shows up as a failed
    task run instead of silent success.

.PARAMETER Diagnose
    Read-only check. This is the default when no mode switch is given.

.PARAMETER CorrectRegistry
    Explicitly remove the NtfsAllowUsnMinSize1Mb clamp value (needs elevation).

.PARAMETER RepairJournals
    Explicitly resize journals that are below MinJournalMB, non-destructively.

.PARAMETER AllowJournalRecreate
    Explicit authorization for the destructive fallback: if a non-destructive
    resize does not take effect, delete and recreate the journal. Requires
    -RepairJournals AND -RecreateVolume <drive-letter>. Never implied, never
    scheduled. The authorization is scoped to exactly one volume.

.PARAMETER RecreateVolume
    Volume-specific destructive confirmation, e.g. -RecreateVolume C. Required
    with -AllowJournalRecreate and names the ONE volume that may be deleted.
    Any other volume is resized non-destructively only. A journal that already
    satisfies MinJournalMB is never deleted even on the confirmed volume:
    being below TargetJournalMB alone is not a deletion reason.

.PARAMETER VerifyOnly
    Legacy alias for read-only diagnosis (kept so old invocations keep their
    old, safe meaning). It wins over any mutating switch, but passing both is
    reported as an invalid invocation instead of silently ignoring one.

.PARAMETER Install
    Register the \SAITULS\USN Journal Guard scheduled task (SYSTEM, at startup
    and daily) and run one read-only check immediately. Needs elevation.

.PARAMETER InstallPlan
    Print the task definition that -Install would register, without registering
    it. Read-only; used by the regression suite.

.PARAMETER Remove
    Unregister the task.

.PARAMETER Volume
    Drive letters to inspect, e.g. C,E. Default: every fixed NTFS volume.

.PARAMETER MinJournalMB
    A journal below this size is unhealthy. Default 32.

.PARAMETER TargetJournalMB
    Size used when enlarging an undersized journal. Default 128.

.PARAMETER AllocationDeltaMB
    Allocation delta used with the resize. Default 8.

.PARAMETER ResizeWaitSeconds
    How long a resize is polled for visibility before it is called a failure.
    Default 15.

.PARAMETER ResizePollSeconds
    Delay between resize visibility polls. Default 3.

.PARAMETER FsutilPath
    fsutil executable to use. Default: resolved from PATH. Injectable so the
    regression suite can drive every branch with mocked output.

.PARAMETER RegistryKey
    Registry key holding the clamp value. Default: the HKLM FileSystem key.
    Injectable so the suite can exercise registry correction on a scratch key.

.PARAMETER RegistryValue
    Name of the clamp value. Default NtfsAllowUsnMinSize1Mb.

.PARAMETER LogPath
    Log file. Default %ProgramData%\SAI\usn-journal-guard.log.

.PARAMETER Json
    Emit one JSON report on stdout (human lines go to the log only).

.PARAMETER Quiet
    Suppress the human log echo on stdout; the log file is still written. Lets
    a caller that parses stdout (-InstallPlan, -Json) get machine output only.
    Implied by -Json, so it is only needed for the non-JSON machine modes.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\host\UsnJournalGuard.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\host\UsnJournalGuard.ps1 -CorrectRegistry
    powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\host\UsnJournalGuard.ps1 -RepairJournals
    powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\host\UsnJournalGuard.ps1 -RepairJournals -AllowJournalRecreate -RecreateVolume C
    powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\host\UsnJournalGuard.ps1 -Install
    powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\host\UsnJournalGuard.ps1 -Remove

.NOTES
    Exit codes: 0 healthy / requested work done, 1 unhealthy or repair
    incomplete (including REBOOT REQUIRED), 2 state could not be determined or
    invalid invocation, 3 elevation required.
#>
[CmdletBinding()]
param(
    [switch]$Diagnose,
    [switch]$CorrectRegistry,
    [switch]$RepairJournals,
    [switch]$AllowJournalRecreate,
    [string]$RecreateVolume = '',
    [switch]$VerifyOnly,
    [switch]$Install,
    [switch]$InstallPlan,
    [switch]$Remove,
    [string[]]$Volume,
    [int]$MinJournalMB = 32,
    [int]$TargetJournalMB = 128,
    [int]$AllocationDeltaMB = 8,
    [int]$ResizeWaitSeconds = 15,
    [int]$ResizePollSeconds = 3,
    [string]$FsutilPath,
    [string]$RegistryKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem',
    [string]$RegistryValue = 'NtfsAllowUsnMinSize1Mb',
    [string]$LogPath,
    [switch]$Json,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$TaskPath = '\SAITULS\'
$TaskName = 'USN Journal Guard'

$ExitOk = 0
$ExitUnhealthy = 1
$ExitError = 2
$ExitElevation = 3

$script:Quiet = ([bool]$Json) -or ([bool]$Quiet)
$script:Problems = New-Object System.Collections.Generic.List[string]
$script:Actions = New-Object System.Collections.Generic.List[string]
$script:VolumeStates = New-Object System.Collections.Generic.List[object]
$script:ToolFailed = $false
$script:ElevationNeeded = $false
$script:RebootRequired = $false
$script:ClampOutstanding = $false
$script:RegistryReadFailed = $false
$script:LastResizeFailure = 'None'
$script:LastFreshState = $null
$script:TargetUnmet = $false
$script:Culture = [System.Globalization.CultureInfo]::InvariantCulture

if (-not $LogPath) { $LogPath = Join-Path (Join-Path $env:ProgramData 'SAI') 'usn-journal-guard.log' }

$script:IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Format-Inv([string]$format, [object[]]$values) {
    return [string]::Format($script:Culture, $format, $values)
}

function Write-Log([string]$line) {
    $stamp = (Get-Date).ToString('o')
    try {
        $dir = Split-Path -Parent $LogPath
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Add-Content -Path $LogPath -Value ("$stamp  $line") -Encoding UTF8
    } catch { }
    if (-not $script:Quiet) { Write-Host $line }
}

function Add-Problem([string]$text) {
    $script:Problems.Add($text) | Out-Null
    Write-Log "PROBLEM: $text"
}

function Add-Action([string]$text) {
    $script:Actions.Add($text) | Out-Null
    Write-Log "ACTION: $text"
}

function Add-ToolProblem([string]$text) {
    $script:ToolFailed = $true
    Add-Problem $text
}

function Resolve-Fsutil {
    if ($FsutilPath) {
        if (-not (Test-Path -LiteralPath $FsutilPath)) { throw "fsutil not found at '$FsutilPath'" }
        return (Resolve-Path -LiteralPath $FsutilPath).Path
    }
    $cmd = Get-Command 'fsutil.exe' -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command 'fsutil' -ErrorAction SilentlyContinue }
    if (-not $cmd) { throw 'fsutil.exe not found on PATH' }
    return $cmd.Source
}

function Invoke-Fsutil {
    param([string[]]$Arguments)
    $exe = $script:Fsutil
    try {
        $out = (& $exe @Arguments 2>&1 | Out-String).Trim()
    } catch {
        # The tool itself could not be executed (missing binary, blocked
        # execution, broken mock). That is an execution failure, not a journal
        # state: exit -1 keeps it distinguishable from any real fsutil code.
        $msg = $_.Exception.Message
        return [pscustomobject]@{ ExitCode = -1; Output = $msg; FirstLine = "fsutil execution failed: $msg"; ExecutionFailed = $true }
    }
    $rc = $LASTEXITCODE
    if ($null -eq $rc) { $rc = 0 }
    $first = ($out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    return [pscustomobject]@{ ExitCode = [int]$rc; Output = $out; FirstLine = [string]$first; ExecutionFailed = $false }
}

# The field order of `fsutil usn queryjournal` is fixed (Journal ID, First Usn,
# Next Usn, Lowest Valid Usn, Max Usn, Maximum Size, Allocation Delta) and only
# the labels are localized, so the size is read by POSITION and then validated
# against a real invariant (maximum size can never exceed max USN). That removes
# the old dependency on the English string 'Maximum Size'.
function Get-UsnFieldValues([string]$text) {
    # USN fields are unsigned 64-bit values. Parsing as signed int64 overflows
    # on values above 0x7FFFFFFFFFFFFFFF (a valid Journal ID or Max Usn), which
    # silently dropped the field and misclassified a healthy journal.
    $values = New-Object System.Collections.Generic.List[uint64]
    foreach ($line in ($text -split "`r?`n")) {
        $m = [regex]::Match($line, '0x([0-9a-fA-F]+)')
        if ($m.Success) {
            try { $values.Add([uint64][Convert]::ToUInt64($m.Groups[1].Value, 16)) } catch { }
        }
    }
    return $values
}

function Get-JournalState([string]$vol) {
    $r = Invoke-Fsutil -Arguments @('usn', 'queryjournal', $vol)
    $state = [ordered]@{
        Volume = $vol; State = 'Unreadable'; MaxBytes = [int64]-1; MaxMB = $null
        Healthy = $false; Detail = ''
    }
    if ($r.ExecutionFailed -or $r.ExitCode -ne 0) {
        $state.Detail = Format-Inv 'query exit {0}: {1}' @($r.ExitCode, $r.FirstLine)
        return [pscustomobject]$state
    }
    $values = Get-UsnFieldValues $r.Output
    if ($values.Count -ge 7 -and $values[5] -gt 0 -and $values[5] -le $values[4] -and $values[5] -le [int64]::MaxValue) {
        $state.State = 'Active'
        $state.MaxBytes = [int64]$values[5]
        $state.MaxMB = [math]::Round($values[5] / 1MB, 2)
        return [pscustomobject]$state
    }
    if ($values.Count -ge 7 -and $values[5] -eq 0) {
        # A volume with no active journal reports the same fields with a zero
        # maximum size. Nothing is deleted to repair that: createjournal is the
        # non-destructive way to give the volume a journal again.
        $state.State = 'Absent'
        $state.Detail = 'no active journal'
        return [pscustomobject]$state
    }
    $state.State = 'Unparsed'
    $raw = ($r.Output -split "`r?`n" | Select-Object -First 3) -join ' | '
    $state.Detail = "unrecognized fsutil output: $raw"
    $script:LastUnparsed = $r.Output
    return [pscustomobject]$state
}

function Get-FixedNtfsVolumes {
    Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue |
        Where-Object { $_.DriveType -eq 3 -and $_.FileSystem -eq 'NTFS' } |
        ForEach-Object { $_.DeviceID.TrimEnd(':') }
}

function Get-ClampValue {
    # Returns the clamp value, or $null when the value is genuinely absent.
    # A missing/unreadable KEY is not absence: it sets RegistryReadFailed so
    # the run exits 2 instead of mistaking "could not read" for "healthy".
    $script:RegistryReadFailed = $false
    try {
        if (-not (Test-Path -LiteralPath $RegistryKey -ErrorAction Stop)) {
            $script:RegistryReadFailed = $true
            return $null
        }
    } catch {
        $script:RegistryReadFailed = $true
        return $null
    }
    try {
        $item = Get-ItemProperty -LiteralPath $RegistryKey -ErrorAction Stop
    } catch {
        $script:RegistryReadFailed = $true
        return $null
    }
    if ($item.PSObject.Properties.Match($RegistryValue).Count -eq 0) {
        return $null  # key present, value absent: the clamp is gone
    }
    return $item.$RegistryValue
}

function Test-NumericParameters {
    if ($MinJournalMB -lt 1 -or $MinJournalMB -gt 4096) { return "MinJournalMB must be 1..4096 MB (got $MinJournalMB)" }
    if ($TargetJournalMB -lt $MinJournalMB -or $TargetJournalMB -gt 65536) { return "TargetJournalMB must be MinJournalMB..65536 MB (got $TargetJournalMB, min $MinJournalMB)" }
    if ($AllocationDeltaMB -lt 1 -or $AllocationDeltaMB -gt $TargetJournalMB) { return "AllocationDeltaMB must be 1..TargetJournalMB MB (got $AllocationDeltaMB)" }
    if ($ResizeWaitSeconds -lt 0 -or $ResizeWaitSeconds -gt 600) { return "ResizeWaitSeconds must be 0..600 (got $ResizeWaitSeconds)" }
    if ($ResizePollSeconds -lt 0 -or $ResizePollSeconds -gt 600) { return "ResizePollSeconds must be 0..600 (got $ResizePollSeconds)" }
    return $null
}

function Get-TaskPlan {
    $scriptPath = $PSCommandPath
    return [ordered]@{
        taskPath = $TaskPath
        taskName = $TaskName
        description = 'Keeps the NTFS USN journal clamp (NtfsAllowUsnMinSize1Mb) from coming back so Everything never loops rebuilding its index. Read-only plus explicit registry correction; never opens, resizes or deletes a journal.'
        action = [ordered]@{
            execute = 'powershell.exe'
            arguments = ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -CorrectRegistry' -f $scriptPath)
        }
        triggers = @(
            [ordered]@{ type = 'boot' }
            [ordered]@{ type = 'daily'; at = '06:00' }
        )
        principal = [ordered]@{ userId = 'SYSTEM'; logonType = 'ServiceAccount'; runLevel = 'Highest' }
        settings = [ordered]@{
            startWhenAvailable = $true
            multipleInstances = 'IgnoreNew'
            executionTimeLimitMinutes = 30
        }
    }
}

function Normalize-VolumeToken([string]$token) {
    if ($null -eq $token) { return '' }
    return ($token.Trim().TrimEnd(':')).ToUpperInvariant()
}

function Get-NormalizedRecreateVolume {
    return (Normalize-VolumeToken $RecreateVolume)
}

function Test-RecreateScope([string]$vol) {
    $want = Get-NormalizedRecreateVolume
    if ([string]::IsNullOrWhiteSpace($want)) { return $false }
    return ((Normalize-VolumeToken $vol) -eq $want)
}

function Get-Mode {
    # -Install / -InstallPlan / -Remove are exclusive one-shot modes.
    $exclusive = @()
    if ($Install) { $exclusive += 'Install' }
    if ($InstallPlan) { $exclusive += 'InstallPlan' }
    if ($Remove) { $exclusive += 'Remove' }
    if ($exclusive.Count -gt 1) {
        throw ('invalid invocation: ' + ($exclusive -join ' + ') + ' cannot be combined')
    }
    if ($exclusive.Count -eq 1) {
        $others = @()
        if ($Diagnose) { $others += 'Diagnose' }
        if ($CorrectRegistry) { $others += 'CorrectRegistry' }
        if ($RepairJournals) { $others += 'RepairJournals' }
        if ($AllowJournalRecreate) { $others += 'AllowJournalRecreate' }
        if ($RecreateVolume) { $others += 'RecreateVolume' }
        if ($VerifyOnly) { $others += 'VerifyOnly' }
        if ($others.Count -gt 0) {
            throw ('invalid invocation: ' + $exclusive[0] + ' cannot be combined with ' + ($others -join ' + '))
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($RecreateVolume) -and (-not $AllowJournalRecreate -or -not $RepairJournals)) {
        throw 'invalid invocation: -RecreateVolume requires -RepairJournals with -AllowJournalRecreate (volume-scoped destructive confirmation only)'
    }
    if ($AllowJournalRecreate -and -not $RepairJournals) {
        throw 'invalid invocation: -AllowJournalRecreate requires -RepairJournals (it authorizes the repair fallback only)'
    }
    if ($AllowJournalRecreate -and $RepairJournals) {
        $want = Get-NormalizedRecreateVolume
        if ([string]::IsNullOrWhiteSpace($want)) {
            throw 'invalid invocation: -AllowJournalRecreate requires -RecreateVolume <drive-letter> (destructive authorization is scoped to exactly one volume); nothing was changed'
        }
        if ($want -notmatch '^[A-Z]$') {
            throw "invalid invocation: -RecreateVolume must be a single drive letter (got '$RecreateVolume'); nothing was changed"
        }
    }
    if ($VerifyOnly -and ($CorrectRegistry -or $RepairJournals -or $AllowJournalRecreate -or (-not [string]::IsNullOrWhiteSpace($RecreateVolume)))) {
        throw 'invalid invocation: -VerifyOnly is read-only and cannot be combined with a mutating switch'
    }

    if ($Remove) { return 'Remove' }
    if ($Install) { return 'Install' }
    if ($InstallPlan) { return 'InstallPlan' }
    if ($CorrectRegistry -and $RepairJournals) { return 'CorrectRegistry+RepairJournals' }
    if ($CorrectRegistry) { return 'CorrectRegistry' }
    if ($RepairJournals) { return 'RepairJournals' }
    return 'Diagnose'
}

function Invoke-JournalResize([string]$vol, [int64]$minBytes, [int64]$targetBytes, [int64]$deltaBytes) {
    $script:LastResizeFailure = 'None'
    $script:LastFreshState = $null
    $badParams = Test-NumericParameters
    if ($badParams) {
        Add-ToolProblem "$vol : resize refused, invalid parameters ($badParams); nothing was written"
        $script:LastResizeFailure = 'InvalidParams'
        return $false
    }
    $r = Invoke-Fsutil -Arguments @('usn', 'createjournal', "m=$targetBytes", "a=$deltaBytes", $vol)
    if ($r.ExecutionFailed -or $r.ExitCode -ne 0) {
        Add-Problem (Format-Inv '{0}: createjournal failed exit {1}: {2}' @($vol, $r.ExitCode, $r.FirstLine))
        if (-not $script:IsAdmin) { $script:ElevationNeeded = $true }
        # The resize was never issued: a later delete would destroy a journal
        # over a permission or execution problem, so it stays forbidden.
        $script:LastResizeFailure = 'CreateFailed'
        return $false
    }
    Add-Action (Format-Inv '{0}: requested non-destructive resize to {1} MB' @($vol, [math]::Round($targetBytes / 1MB, 2)))

    $deadline = (Get-Date).AddSeconds($ResizeWaitSeconds)
    $sawUnreadable = $false
    $state = $null
    while ($true) {
        $state = Get-JournalState $vol
        # Success is the REQUESTED target, not merely the unhealthy threshold:
        # a resize that lands below the target is still a failure.
        if ($state.State -eq 'Active' -and $state.MaxBytes -ge $targetBytes) {
            Add-Action (Format-Inv '{0}: journal now {1} MB' @($vol, $state.MaxMB))
            $script:LastResizeFailure = 'None'
            $script:LastFreshState = $state
            return $true
        }
        if ($state.State -eq 'Unreadable' -or $state.State -eq 'Unparsed') { $sawUnreadable = $true }
        if ((Get-Date) -ge $deadline) { break }
        Start-Sleep -Seconds $ResizePollSeconds
    }
    $script:LastFreshState = $state
    if ($sawUnreadable) {
        Add-Problem (Format-Inv '{0}: journal state became unreadable after resize (last state {1}); nothing further will be done' @($vol, $state.State))
        $script:LastResizeFailure = 'Unreadable'
    } else {
        Add-Problem (Format-Inv '{0}: resize not visible after {1}s (last state {2})' @($vol, $ResizeWaitSeconds, $state.State))
        $script:LastResizeFailure = 'NotVisible'
    }
    return $false
}

function Invoke-JournalRecreate([string]$vol, [int64]$minBytes, [int64]$targetBytes, [int64]$deltaBytes) {
    # Narrow authorization: recreation is allowed only when a non-destructive
    # resize was actually issued and simply is not visible yet, AND the caller
    # holds a volume-scoped confirmation for exactly this volume, AND a fresh
    # re-read proves the journal is still unhealthy. Any other failure
    # (permission, execution error, unreadable follow-up, bad parameters,
    # outstanding clamp, pending reboot, already-healthy journal, wrong volume)
    # refuses the delete. Target-not-reached and journal-unhealthy are separate
    # conditions: a journal at or above MinJournalMB is healthy even when it is
    # below the preferred TargetJournalMB, and is never deleted for that alone.
    if (-not (Test-RecreateScope $vol)) {
        $want = Get-NormalizedRecreateVolume
        Add-Problem (Format-Inv '{0}: recreate refused, destructive authorization is scoped to volume {1}; nothing was deleted' @($vol, $want))
        return $false
    }
    if ($script:LastResizeFailure -ne 'NotVisible') {
        Add-Problem (Format-Inv '{0}: recreate refused (resize failure was {1}, not a verified issued-but-not-visible resize); nothing was deleted' @($vol, $script:LastResizeFailure))
        return $false
    }
    $freshClamp = Get-ClampValue
    if ($script:RegistryReadFailed) {
        Add-ToolProblem "$vol : recreate refused, the registry clamp state could not be re-read; nothing was deleted"
        return $false
    }
    if (($null -ne $freshClamp) -or $script:ClampOutstanding -or $script:RebootRequired) {
        Write-Log "$vol : recreate refused, the registry clamp is still in place or a reboot is pending; nothing was deleted"
        Add-Problem "$vol : recreate refused while the registry clamp is outstanding; nothing was deleted"
        return $false
    }
    $fresh = Get-JournalState $vol
    $script:LastFreshState = $fresh
    if ($fresh.State -ne 'Active' -and $fresh.State -ne 'Absent') {
        Add-ToolProblem (Format-Inv '{0}: recreate refused, the journal state is {1} ({2}); nothing was deleted' @($vol, $fresh.State, $fresh.Detail))
        return $false
    }
    if ($fresh.State -eq 'Active' -and $fresh.MaxBytes -ge $minBytes) {
        Add-Problem (Format-Inv '{0}: recreate refused, journal already healthy at {1} MB (minimum {2} MB); below-target size alone is never a deletion reason; nothing was deleted' @($vol, $fresh.MaxMB, ($minBytes / 1MB)))
        return $false
    }
    Write-Log "WARNING: $vol : recreating the journal (deletejournal + createjournal) under explicit volume-scoped authorization; this forces a full volume rescan for every USN consumer"
    $d = Invoke-Fsutil -Arguments @('usn', 'deletejournal', '/n', $vol)
    if ($d.ExecutionFailed -or $d.ExitCode -ne 0) {
        Add-Problem (Format-Inv '{0}: deletejournal failed exit {1}: {2}' @($vol, $d.ExitCode, $d.FirstLine))
        if (-not $script:IsAdmin) { $script:ElevationNeeded = $true }
        return $false
    }
    Add-Action "$vol : deleted the journal under explicit -AllowJournalRecreate authorization"
    if (-not (Invoke-JournalResize $vol $minBytes $targetBytes $deltaBytes)) { return $false }
    return $true
}

function Invoke-Check {
    param([int64]$MinBytes, [int64]$TargetBytes, [int64]$DeltaBytes, [bool]$DoRepair, [bool]$DoCorrectRegistry)

    $clamp = Get-ClampValue
    if ($script:RegistryReadFailed) {
        Add-ToolProblem "registry state could not be read ($RegistryKey\$RegistryValue); nothing was written to any volume"
        return
    }
    $clampPresent = ($null -ne $clamp)
    if ($clampPresent) {
        Add-Problem "registry $RegistryValue present = $clamp (journals are clamped while it exists)"
        $script:ClampOutstanding = $true
        if ($DoCorrectRegistry) {
            try {
                Remove-ItemProperty -Path $RegistryKey -Name $RegistryValue -ErrorAction Stop
                $clampPresent = ($null -ne (Get-ClampValue))
                if ($clampPresent) {
                    Add-ToolProblem "registry $RegistryValue could not be removed from $RegistryKey"
                } else {
                    Add-Action "removed registry value $RegistryValue (was $clamp)"
                    $script:ClampOutstanding = $false
                    # NTFS caches the policy: the journals cannot be resized in
                    # this same run, and saying so is the point.
                    $script:RebootRequired = $true
                }
            } catch {
                Add-ToolProblem "registry removal failed: $($_.Exception.Message)"
                if (-not $script:IsAdmin) { $script:ElevationNeeded = $true }
            }
        }
    }

    $volumes = @()
    if ($Volume) {
        $volumes = @($Volume | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().TrimEnd(':') } | Where-Object { $_ })
    } else {
        $volumes = @(Get-FixedNtfsVolumes)
    }
    if ($volumes.Count -eq 0) { Add-ToolProblem 'no fixed NTFS volume found to inspect'; return }

    foreach ($d in $volumes) {
        $vol = "${d}:"
        $state = Get-JournalState $vol
        $state.Healthy = $false
        $state | Add-Member -NotePropertyName TargetMB -NotePropertyValue ([math]::Round($TargetBytes / 1MB, 2)) -Force
        $state | Add-Member -NotePropertyName TargetMet -NotePropertyValue ($state.State -eq 'Active' -and $state.MaxBytes -ge $TargetBytes) -Force
        $problemIndex = -1

        switch ($state.State) {
            'Unreadable' {
                Add-ToolProblem "$vol : journal state could not be read ($($state.Detail)); nothing was written to this volume"
                $state.Detail = "state could not be read ($($state.Detail))"
            }
            'Unparsed' {
                Add-ToolProblem "$vol : could not read a journal size from fsutil output ($($state.Detail)); nothing was written to this volume"
            }
            'Absent' {
                $script:Problems.Add("$vol : no active journal") | Out-Null
                $problemIndex = $script:Problems.Count - 1
                Write-Log "PROBLEM: $vol : no active journal"
            }
            'Active' {
                if ($state.MaxBytes -lt $MinBytes) {
                    $text = Format-Inv '{0}: journal {1} MB < {2} MB' @($vol, $state.MaxMB, $MinJournalMB)
                    $script:Problems.Add($text) | Out-Null
                    $problemIndex = $script:Problems.Count - 1
                    Write-Log "PROBLEM: $text"
                } else {
                    $state.Healthy = $true
                }
            }
        }

        if ($DoRepair -and -not $state.Healthy) {
            if ($state.State -eq 'Unreadable' -or $state.State -eq 'Unparsed') {
                Write-Log "$vol : repair skipped, the journal state could not be read"
            } elseif ($clampPresent) {
                Write-Log "$vol : repair skipped, the registry clamp is still in place (reboot required first)"
            } elseif ($script:RebootRequired) {
                Write-Log "$vol : repair skipped, the clamp was removed in this run and NTFS still has the old policy cached until reboot"
            } else {
                $script:LastFreshState = $null
                $ok = Invoke-JournalResize $vol $MinBytes $TargetBytes $DeltaBytes
                if (-not $ok -and $AllowJournalRecreate) {
                    if (Test-RecreateScope $vol) {
                        $ok = Invoke-JournalRecreate $vol $MinBytes $TargetBytes $DeltaBytes
                    } else {
                        Write-Log "$vol : destructive recreate NOT authorized for this volume (authorization scoped to $(Get-NormalizedRecreateVolume):); only non-destructive resize was attempted"
                    }
                } elseif (-not $ok) {
                    Write-Log "$vol : destructive recreate NOT performed; pass -AllowJournalRecreate with -RecreateVolume $d to authorize volume-scoped delete+create for an explicit repair run"
                }
                # Refresh the reported state from the freshest verified read so
                # the final health reflects the actual final journal size.
                $fresh = $script:LastFreshState
                if ($null -ne $fresh) {
                    $state.State = $fresh.State
                    $state.MaxBytes = $fresh.MaxBytes
                    $state.MaxMB = $fresh.MaxMB
                    $state.Detail = $fresh.Detail
                }
                $nowHealthy = ($state.State -eq 'Active' -and $state.MaxBytes -ge $MinBytes)
                $nowAtTarget = ($state.State -eq 'Active' -and $state.MaxBytes -ge $TargetBytes)
                $state.TargetMet = $nowAtTarget
                if ($ok -and $nowAtTarget) {
                    # Repaired to the requested target: it is no longer an
                    # outstanding problem, so it must not be reported as one.
                    # The repair itself stays in actions, and the original
                    # detection stays in the log.
                    if ($problemIndex -ge 0) { $script:Problems.RemoveAt($problemIndex); $problemIndex = -1 }
                    $state.Healthy = $true
                } elseif ($nowHealthy) {
                    # Healthy but below the preferred target (e.g. 1 MiB ->
                    # 32 MiB with a 128 MiB target): health is truthful, the
                    # unmet target is reported separately and the repair counts
                    # as incomplete. This is never a deletion reason.
                    $state.Healthy = $true
                    $state.TargetMet = $false
                    $script:TargetUnmet = $true
                    if ($problemIndex -ge 0) { $script:Problems.RemoveAt($problemIndex); $problemIndex = -1 }
                    $msg = Format-Inv '{0}: journal healthy at {1} MB but below requested target {2} MB' @($vol, $state.MaxMB, ([math]::Round($TargetBytes / 1MB, 2)))
                    $script:Problems.Add($msg) | Out-Null
                    Write-Log "PROBLEM: $msg"
                } else {
                    $state.Healthy = $false
                    $state.TargetMet = $false
                }
            }
        }
        $script:VolumeStates.Add($state) | Out-Null
    }

    if ($clampPresent -and -not $DoCorrectRegistry) {
        Write-Log "registry clamp still present; run with -CorrectRegistry to remove it (needs elevation)"
    }
    if ($script:RebootRequired) {
        Write-Log 'REBOOT REQUIRED: the clamp value was removed, but NTFS only re-reads it at boot. Until then the journals stay clamped; run -RepairJournals after the reboot.'
    }
}

# The exit code follows the FINAL snapshot, not the diagnostic trail: a run
# that detected an undersized journal and repaired it to the requested target
# is a success, while anything still outstanding is not. An unhealthy volume
# can never be silent, a healthy-but-below-target journal is repair-incomplete
# (exit 1 via TargetUnmet), and an unknown registry state is exit 2, never a
# false healthy 0.
function Get-ExitCode {
    if ($script:ElevationNeeded) { return $ExitElevation }
    if ($script:ToolFailed) { return $ExitError }
    if ($script:RegistryReadFailed) { return $ExitError }
    if ($script:RebootRequired) { return $ExitUnhealthy }
    if ($script:ClampOutstanding) { return $ExitUnhealthy }
    if ($script:TargetUnmet) { return $ExitUnhealthy }
    if (@($script:VolumeStates | Where-Object { -not $_.Healthy }).Count -gt 0) { return $ExitUnhealthy }
    if ($script:VolumeStates.Count -eq 0 -and $script:Problems.Count -gt 0) { return $ExitUnhealthy }
    return $ExitOk
}

# Additive schema notes (schema name unchanged: saituls.usn-journal-guard.v1):
#   volumes[].targetMB / volumes[].targetMet: requested target vs actual size.
#   targetUnmet (bool): true when a journal is healthy but below the requested
#     target; exit is 1 (repair incomplete) while healthy stays true.
# The report MUST describe the same snapshot used to calculate the exit code:
# callers pass the final clamp value + readFailed captured BEFORE Get-ExitCode
# and this function performs no registry reads of its own.
function Write-JsonReport([int]$ExitCode, [string]$Mode, [object]$ClampValue, [bool]$ClampReadFailed, [string]$Fsutil) {
    $report = [ordered]@{
        schema = 'saituls.usn-journal-guard.v1'
        mode = $Mode
        exitCode = $ExitCode
        elevated = $script:IsAdmin
        psVersion = $PSVersionTable.PSVersion.ToString()
        fsutil = $Fsutil
        rebootRequired = $script:RebootRequired
        targetUnmet = $script:TargetUnmet
        registry = [ordered]@{
            path = $RegistryKey
            name = $RegistryValue
            present = ($null -ne $ClampValue)
            value = $ClampValue
            readFailed = $ClampReadFailed
        }
        volumes = @($script:VolumeStates | ForEach-Object {
            [ordered]@{
                volume = $_.Volume; state = $_.State; maxBytes = $_.MaxBytes
                maxMB = $_.MaxMB; healthy = $_.Healthy; detail = $_.Detail
                targetMB = $_.TargetMB; targetMet = $_.TargetMet
            }
        })
        problems = @($script:Problems)
        actions = @($script:Actions)
    }
    $report | ConvertTo-Json -Depth 6
}

# ---- main ----------------------------------------------------------------

$exitCode = $ExitOk
try {
    $mode = Get-Mode
} catch {
    Write-Host "ERROR: $($_.Exception.Message)"
    exit $ExitError
}

try {
    $resolvedFsutil = Resolve-Fsutil
} catch {
    Write-Host "ERROR: $($_.Exception.Message)"
    exit $ExitError
}
$script:Fsutil = $resolvedFsutil

$minBytes = [int64]$MinJournalMB * 1MB
$targetBytes = [int64]$TargetJournalMB * 1MB
$deltaBytes = [int64]$AllocationDeltaMB * 1MB

# Invalid sizes or polling parameters must fail before anything is read for
# repair or written anywhere: no mutation happens below this line on exit 2.
$paramError = Test-NumericParameters
if ($paramError) {
    Write-Host "ERROR: invalid invocation: $paramError"
    exit $ExitError
}

if ($mode -eq 'Remove') {
    $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Log "scheduled task $TaskPath$TaskName is not registered; nothing to remove"
        exit $ExitOk
    }
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Log "removed scheduled task $TaskPath$TaskName"
    exit $ExitOk
}

if ($mode -eq 'InstallPlan') {
    $plan = Get-TaskPlan
    Write-Log ('task plan: ' + ($plan | ConvertTo-Json -Depth 6 -Compress))
    $plan | ConvertTo-Json -Depth 6
    exit $ExitOk
}

if ($mode -eq 'Install') {
    $plan = Get-TaskPlan
    $action = New-ScheduledTaskAction -Execute $plan.action.execute -Argument $plan.action.arguments
    $triggers = @(
        (New-ScheduledTaskTrigger -AtStartup),
        (New-ScheduledTaskTrigger -Daily -At $plan.triggers[1].at)
    )
    $principal = New-ScheduledTaskPrincipal -UserId $plan.principal.userId -LogonType $plan.principal.logonType -RunLevel $plan.principal.runLevel
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $plan.settings.executionTimeLimitMinutes)
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Action $action `
        -Trigger $triggers -Principal $principal -Settings $settings `
        -Description $plan.description -Force | Out-Null
    Write-Log "installed scheduled task $TaskPath$TaskName (action: -CorrectRegistry, read-only diagnosis plus clamp removal; never resizes or deletes a journal)"
    # Immediate READ-ONLY check. Journal repair is a deliberate, explicit,
    # post-reboot action and is never part of the install.
    Invoke-Check -MinBytes $minBytes -TargetBytes $targetBytes -DeltaBytes $deltaBytes -DoRepair $false -DoCorrectRegistry $false
    # Single final snapshot: the JSON report must describe the same state the
    # exit code was calculated from. No registry read happens after this.
    $finalClamp = Get-ClampValue
    $finalReadFailed = [bool]$script:RegistryReadFailed
    $exitCode = Get-ExitCode
    if ($Json) { Write-JsonReport $exitCode $mode $finalClamp $finalReadFailed $script:Fsutil }
    Write-Log (Format-Inv 'install check exit {0} ({1} problem(s))' @($exitCode, $script:Problems.Count))
    exit $exitCode
}

$doCorrectRegistry = ($mode -eq 'CorrectRegistry' -or $mode -eq 'CorrectRegistry+RepairJournals')
$doRepair = ($mode -eq 'RepairJournals' -or $mode -eq 'CorrectRegistry+RepairJournals')

Invoke-Check -MinBytes $minBytes -TargetBytes $targetBytes -DeltaBytes $deltaBytes `
    -DoRepair $doRepair -DoCorrectRegistry $doCorrectRegistry
# Single final snapshot: the JSON report must describe the same state the exit
# code was calculated from. No registry read happens after this point.
$finalClamp = Get-ClampValue
$finalReadFailed = [bool]$script:RegistryReadFailed
$exitCode = Get-ExitCode

if ($script:Problems.Count -eq 0) {
    Write-Log (Format-Inv 'USN journals healthy on {0} volume(s) ({1} MB minimum)' @($script:VolumeStates.Count, $MinJournalMB))
} else {
    Write-Log (Format-Inv 'USN journal guard finished with {0} problem(s); exit {1}' @($script:Problems.Count, $exitCode))
}
if ($Json) { Write-JsonReport $exitCode $mode $finalClamp $finalReadFailed $script:Fsutil }
exit $exitCode
