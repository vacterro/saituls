<#
.SYNOPSIS
    Installs or updates the SAITULS\SAISPIN scheduled task.

.DESCRIPTION
    Registers Scripts\saispin_watch.ps1 to run at logon and repeat every five
    minutes, hidden, surviving reboots, never in parallel with itself.

    The task is registered in DRY-RUN mode unless -AutoKill is passed, so
    installing the watchdog cannot by itself terminate anything. Arming it is a
    separate, explicit decision.

    Task Scheduler is told not to start a second instance (IgnoreNew), and the
    watcher holds a named mutex as well: two independent guards, because a
    second watcher racing the same state file could each see half a hot streak.

.EXAMPLE
    .\Scripts\Install-SaispinTask.ps1                 # install, dry-run mode
    .\Scripts\Install-SaispinTask.ps1 -AutoKill       # install, armed
    .\Scripts\Install-SaispinTask.ps1 -VerifyOnly     # report, change nothing
    .\Scripts\Install-SaispinTask.ps1 -Remove         # unregister
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Register the task with automatic termination armed. Default is dry-run.
    [switch]$AutoKill,
    # Unregister the task and exit.
    [switch]$Remove,
    # Report what is registered and whether it matches; change nothing.
    [switch]$VerifyOnly,
    [string]$ConfigFile,
    [int]$IntervalMinutes = 0
)

$ErrorActionPreference = 'Stop'

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
$RepoRoot  = Split-Path -Parent $ScriptDir
$Watcher   = Join-Path $ScriptDir 'saispin_watch.ps1'
$Engine    = Join-Path $ScriptDir 'saispin_logic.py'
if (-not $ConfigFile) {
    $ConfigRoot = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:TEMP }
    $ConfigFile = Join-Path $ConfigRoot 'SAITULS\SAISPIN\settings.json'
}
$ConfigFile = [IO.Path]::GetFullPath($ConfigFile)

$TaskPath = '\SAITULS\'
$TaskName = 'SAISPIN'
$FullName = $TaskPath + $TaskName

# Windows PowerShell, not pwsh: the task must run on a stock machine whether or
# not PowerShell 7 is installed. The watcher is compatible with both.
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Get-SaispinTask {
    return Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
}

if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    throw 'The ScheduledTasks module is unavailable; cannot register SAISPIN.'
}

# ------------------------------------------------------------------- removal

if ($Remove) {
    $existing = Get-SaispinTask
    if (-not $existing) {
        Write-Host "Nothing to remove: $FullName is not registered."
        exit 0
    }
    if ($PSCmdlet.ShouldProcess($FullName, 'Unregister scheduled task')) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
        Write-Host "Removed $FullName. The watcher script and its log were left in place."
    }
    exit 0
}

# Keep Task Scheduler cadence in lockstep with the shared policy. An explicit
# interval is accepted only when it matches the validated configuration.
$intervalExplicit = $PSBoundParameters.ContainsKey('IntervalMinutes')
$normalizedOutput = & python $Engine --normalize-settings $ConfigFile 2>&1
if ($LASTEXITCODE -ne 0) {
    throw ('Could not normalize SAISPIN settings (exit ' + $LASTEXITCODE + '): ' + (($normalizedOutput | ForEach-Object { "$_" }) -join ' '))
}
try {
    $normalizedConfig = (($normalizedOutput | Out-String) | ConvertFrom-Json)
} catch {
    throw ('Settings engine returned invalid JSON: ' + $_.Exception.Message)
}
if ($normalizedConfig.warning) {
    Write-Warning ('Settings warning; safe defaults used for task cadence: ' + $normalizedConfig.warning)
}
$configuredInterval = [int]$normalizedConfig.settings.timing.sweep_interval_minutes
if ($intervalExplicit) {
    if ($IntervalMinutes -lt 1 -or $IntervalMinutes -gt 1440) {
        throw 'IntervalMinutes must be between 1 and 1440.'
    }
    if ($IntervalMinutes -ne $configuredInterval) {
        throw "Cadence mismatch: settings request $configuredInterval minute(s), installer requested $IntervalMinutes."
    }
} else {
    $IntervalMinutes = $configuredInterval
}

# --------------------------------------------------------------- verification

# Everything the task must satisfy, checked against what is actually registered
# rather than against what this script intended to register.
function Assert-SaispinTask {
    $task = Get-SaispinTask
    if (-not $task) { throw "Not registered: $FullName" }

    $action = @($task.Actions)[0]
    if ($action.Execute -ne $PowerShellExe) {
        throw "Action drift: expected $PowerShellExe, found $($action.Execute)"
    }
    if ($action.Arguments -notlike "*$Watcher*") {
        throw "Action drift: the task does not run $Watcher"
    }
    $configArgument = '-ConfigFile "' + $ConfigFile + '"'
    if ($action.Arguments.IndexOf($configArgument, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "Config drift: task does not pass the shared settings file $ConfigFile"
    }
    if ($action.WorkingDirectory -ne $RepoRoot) {
        throw "Working directory drift: expected $RepoRoot, found $($action.WorkingDirectory)"
    }

    $armed = $action.Arguments -like '*-AutoKill*'
    if ($armed -ne [bool]$AutoKill) {
        throw ("Mode drift: registered as " + $(if ($armed) { 'auto-kill' } else { 'dry-run' }) +
               ", requested " + $(if ($AutoKill) { 'auto-kill' } else { 'dry-run' }))
    }

    $logon = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
    if ($logon.Count -eq 0) { throw 'Trigger drift: no logon trigger' }

    $repeating = @($task.Triggers | Where-Object { $_.Repetition -and $_.Repetition.Interval })
    if ($repeating.Count -eq 0) { throw 'Trigger drift: no repetition interval' }

    # Exact cadence (T-135 8.1): "some repetition interval exists" used to
    # pass while the task actually ran at a different cadence. Task Scheduler
    # stores the interval in the ISO-8601 "PT<minutes>M" / "P<days>D" form,
    # which [TimeSpan]::Parse on Windows PowerShell 5.1 cannot decode -- so the
    # observed value is normalised with [System.Xml.XmlConvert]::ToTimeSpan
    # first, then compared EXACTLY with the request. The failure prints BOTH
    # values in hh:mm:ss so the drift is visible.
    $requested = New-TimeSpan -Minutes $IntervalMinutes
    foreach ($t in $repeating) {
        $raw = $t.Repetition.Interval
        $observed = $null
        try { $observed = [System.Xml.XmlConvert]::ToTimeSpan($raw) } catch { }
        $observedText = if ($null -ne $observed) { '{0:hh\:mm\:ss}' -f $observed } else { $raw }
        if ($null -eq $observed -or $observed -ne $requested) {
            throw ("Cadence drift: expected {0:hh\:mm\:ss}, observed {1}" -f $requested, $observedText)
        }
    }

    if ($task.Settings.MultipleInstances -ne 'IgnoreNew') {
        throw "Concurrency drift: MultipleInstances is $($task.Settings.MultipleInstances), expected IgnoreNew"
    }
    if (-not $task.Settings.Hidden) { throw 'Settings drift: the task is not hidden' }
    if (-not $task.Settings.StartWhenAvailable) {
        throw 'Settings drift: StartWhenAvailable is off, so a missed run after a reboot never happens'
    }
    # A five-minute watcher must not be stopped for running "too long" or
    # skipped because the machine is on battery.
    if ($task.Settings.ExecutionTimeLimit -ne 'PT0S' -and $task.Settings.ExecutionTimeLimit) {
        throw "Settings drift: ExecutionTimeLimit is $($task.Settings.ExecutionTimeLimit), expected none"
    }
    if ($task.Settings.DisallowStartIfOnBatteries) {
        throw 'Settings drift: DisallowStartIfOnBatteries is on'
    }

    return @{
        Task    = $task
        Mode    = if ($armed) { 'auto-kill' } else { 'dry-run' }
        Command = $action.Execute + ' ' + $action.Arguments
    }
}

if ($VerifyOnly) {
    $report = Assert-SaispinTask
    Write-Host "PASS: $FullName registered in $($report.Mode) mode, repeating every $IntervalMinutes minute(s)."
    Write-Host "  $($report.Command)"
    exit 0
}

# ---------------------------------------------------------------- installation

foreach ($required in @($Watcher, $Engine)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required file missing: $required"
    }
}

# Prove the watcher actually runs on this machine before scheduling it every
# five minutes. A task that fails silently in the background is worse than no
# task, and the self-test touches nothing.
& $PowerShellExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Watcher -SelfTest | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "The watcher self-test failed (exit $LASTEXITCODE); not registering a task that does not work."
}

$modeArg = if ($AutoKill) { ' -AutoKill' } else { ' -DryRun' }
$quotedWatcher = '"' + $Watcher + '"'
$quotedConfig = '"' + $ConfigFile + '"'
$arguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden " +
             "-ExecutionPolicy Bypass -File $quotedWatcher$modeArg -ConfigFile $quotedConfig"

$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $arguments -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn
# A logon trigger has no repetition of its own, so it borrows one from a
# throwaway -Once trigger. This is the documented idiom; building the repetition
# object by hand is version-fragile.
$span = New-TimeSpan -Minutes $IntervalMinutes
$repeatSource = $null
foreach ($duration in @([TimeSpan]::MaxValue, (New-TimeSpan -Days 3650))) {
    try {
        $repeatSource = New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval $span -RepetitionDuration $duration
        break
    } catch {
        continue
    }
}
if (-not $repeatSource) { throw 'Could not build a repeating trigger on this Windows build.' }
$trigger.Repetition = $repeatSource.Repetition

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$description = 'SAISPIN: detects processes that lost their parent and keep burning CPU. ' +
               $(if ($AutoKill) { 'Auto-kill ARMED.' } else { 'Dry-run: alerts only.' })

if ($PSCmdlet.ShouldProcess($FullName, 'Register scheduled task')) {
    # T-135: transactional replacement. Unregistering first then registering
    # destroyed a valid prior task whenever the replacement failed. The complete
    # existing definition is captured and restored on any post-destructive
    # failure; with no prior task a failure leaves nothing half-registered.
    $prior = Get-SaispinTask
    $priorBackup = $null
    if ($prior) {
        try {
            $priorBackup = @{
                Xml = Export-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop
            }
            if ([string]::IsNullOrWhiteSpace($priorBackup.Xml)) {
                throw 'Empty rollback snapshot'
            }
            $snapshot = [xml]$priorBackup.Xml
            if (-not $snapshot.Task.Actions -or -not $snapshot.Task.Principals -or
                -not $snapshot.Task.Settings) {
                throw 'Incomplete rollback snapshot'
            }
            # Password credentials cannot be exported from Task Scheduler.
            if (@($snapshot.Task.Principals.Principal | Where-Object {
                $_.LogonType -in @('Password', 'InteractiveTokenOrPassword')
            }).Count) {
                throw 'Rollback credentials unavailable for password task'
            }
        } catch {
            throw "Cannot capture complete prior task snapshot; replacement refused: $($_.Exception.Message)"
        }
    }
    if ($prior) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
    }
    try {
        Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
            -Action $action -Trigger $trigger -Settings $settings `
            -Description $description | Out-Null
        # Prove the registration actually matches before reporting success.
        $report = Assert-SaispinTask
        Write-Host "Registered $FullName in $($report.Mode) mode, at logon and every $IntervalMinutes minute(s)."
        Write-Host "  $($report.Command)"
        if (-not $AutoKill) {
            Write-Host '  Nothing will be terminated. Re-run with -AutoKill to arm it.'
        }
    } catch {
        $replacementError = $_
        # Remove a partial new task before restoring the saved definition.
        $cleanupError = $null
        try {
            if (Get-SaispinTask) { Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false }
        } catch { $cleanupError = $_ }
        if ($priorBackup) {
            try {
                Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
                    -Xml $priorBackup.Xml | Out-Null
            } catch {
                $rollbackError = $_
                throw "Replacement failed ($($replacementError.Exception.Message)) and restoring the previous task also failed: $($rollbackError.Exception.Message)"
            }
        }
        if ($cleanupError) {
            throw "Replacement failed ($($replacementError.Exception.Message)) and removing the partial task also failed: $($cleanupError.Exception.Message)"
        }
        throw $replacementError
    }
}
