<#
.SYNOPSIS
    SAIPATCH Queue Viewer -- the normal GUI surface for OpenCode queue
    observation and native FIFO admission.

.DESCRIPTION
    Default (no arguments or -Command gui) launches the Queue Viewer Cockpit
    (queue_viewer.pyw): a READ-ONLY SQLite observer that admits prompts only
    through the native V2 delivery:"queue" HTTP API. It never mutates the
    OpenCode database directly.

    Legacy direct-DB mutation commands (list, status, drop, clear, up, down,
    add) route to queue_manager.pyw, which performs DIRECT SQLITE MUTATION of
    the OpenCode database. These are LEGACY / UNSAFE-DIRECT-DB: they are not
    part of the supported native queue interface and are advertised as such.

.EXAMPLE
    .\queue.ps1                     # Launches Queue Viewer Cockpit (normal GUI)
    .\queue.ps1 -Command gui        # Same as default
    .\queue.ps1 -LegacyCommand list # LEGACY direct-DB console listing
    .\queue.ps1 -LegacyCommand drop 1  # LEGACY direct-DB mutation (UNSAFE)
#>
param(
    [ValidateSet('gui')]
    [string]$Command = 'gui',

    # Legacy direct-DB console commands. Deliberately NOT reachable via
    # -Command so that the supported default interface cannot mutate the DB.
    [ValidateSet('list', 'status', 'drop', 'clear', 'up', 'down', 'add')]
    [string]$LegacyCommand,
    [string]$Target,
    [string]$Session,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# ── Normal path: Queue Viewer Cockpit (read-only + native admission) ──
$ViewerScript = Join-Path $ScriptDir 'queue_viewer.pyw'
if (-not (Test-Path -LiteralPath $ViewerScript)) {
    throw "Queue Viewer script not found: $ViewerScript"
}

if ($Command -eq 'gui' -and -not $LegacyCommand) {
    $python = Get-Command pythonw -ErrorAction SilentlyContinue
    $exe = if ($python) { $python.Source } else { 'python' }

    $argsList = @($ViewerScript)
    if ($Session) { $argsList += @('--session', $Session) }

    Start-Process -FilePath $exe -ArgumentList $argsList
    Write-Host "Started SAIPATCH Queue Viewer Cockpit." -ForegroundColor Cyan
    exit 0
}

# ── Legacy direct-DB console path (quarantined) ───────────────────────
# queue_manager.pyw mutates the OpenCode SQLite database DIRECTLY for
# add/edit/drop/reorder/clear/delete-session. It is NOT the Queue Viewer
# and MUST NOT be wired back as the default GUI surface.
$LegacyScript = Join-Path $ScriptDir 'queue_manager.pyw'
if (-not (Test-Path -LiteralPath $LegacyScript)) {
    throw "Legacy queue manager script not found: $LegacyScript"
}

Write-Warning "LEGACY DIRECT-DB TOOL -- NOT THE DEFAULT QUEUE VIEWER."
Write-Warning "queue_manager.pyw mutates the OpenCode database directly and can"
Write-Warning "corrupt native queue state. Prefer the Queue Viewer (no arguments)."

$cliArgs = @($LegacyScript, $LegacyCommand)
if ($Target) { $cliArgs += $Target }
if ($Session) { $cliArgs += @('--session', $Session) }
if ($Json) { $cliArgs += '--json' }

& python $cliArgs
exit $LASTEXITCODE
