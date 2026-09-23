<#
.SYNOPSIS
    SAIPATCH -- standalone patch-management subtool for SAITULS.

.DESCRIPTION
    Detects the locally installed OpenCode, probes patch compatibility, and
    applies / verifies / restores patch packages transactionally. A patch is a
    directory with manifest.json plus probe.ps1 / apply.ps1 / verify.ps1 /
    restore.ps1; the engine below is generic and knows nothing about any one
    patch. No network, no telemetry, no credentials anywhere in this tool.

    Patch states: NOT_INSTALLED AVAILABLE INSTALLED NEEDS_REAPPLY
    UNSUPPORTED_VERSION SOURCE_DRIFTED OPEN_CODE_RUNNING BROKEN_PATCH RESTORABLE
#>
param(
    [ValidateSet('Detect', 'Apply', 'Verify', 'Restore', 'Status', 'Interactive')]
    [string]$Command = 'Interactive',
    [string]$Patch = 'opencode-queue-mode',
    [switch]$Force,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'

$SaipatchRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
. (Join-Path $SaipatchRoot 'lib\resolve.ps1')
. (Join-Path $SaipatchRoot 'lib\state.ps1')
. (Join-Path $SaipatchRoot 'lib\contract.ps1')
. (Join-Path $SaipatchRoot 'lib\transaction.ps1')

function Write-Result([object]$obj) {
    if ($Json) { $obj | ConvertTo-Json -Depth 6 -Compress }
    else {
        Write-Host ("OpenCode : {0}  @  {1}" -f $obj.version, $obj.install_root)
        Write-Host ("Patch    : {0}  [{1}]" -f $obj.patch, $obj.state)
        # T-144 restart-awareness: always show the installed package version
        # and whether running OpenCode processes predate the last transition.
        Write-Host ("SAIPATCH native queue: {0}" -f $obj.patch_version)
        Write-Host ("Runtime restart required: {0}" -f $(if ($obj.runtime_restart_required) { 'YES' } else { 'NO' }))
        Write-Host ("Backup   : {0}" -f $(if ($obj.backup) { 'present' } else { 'none' }))
        if ($obj.reason) { Write-Host ("Reason   : {0}" -f $obj.reason) }
    }
}

$install = Find-OpenCodeInstall
$patchDir = Join-Path $SaipatchRoot "patches\$Patch"
# Every entry point settles an interrupted publication before classifying it.
# Recovery refuses all mutation if any live target has foreign bytes.
try {
    $recoveryLock = Enter-PatchLock
    try { Repair-PatchPublication } finally { $recoveryLock.ReleaseMutex(); $recoveryLock.Dispose() }
} catch {
    Write-Result ([pscustomobject]@{
        patch = $Patch; version = $install.version; install_root = $install.root
        state = 'BROKEN_PATCH'; backup = $false; reason = $_.Exception.Message
    })
    exit 3
}
$report = Get-PatchStatus -Install $install -PatchDir $patchDir

switch ($Command) {
    'Interactive' {
        # A console menu: Detect / Apply / Verify / Restore / Quit. No GUI, no
        # background anything -- the process exits completely on Q.
        function Invoke-Menu([string]$c, [object]$i, [object]$r) {
            switch ($c) {
                'A' {
                    if ($r.state -in @('OPEN_CODE_RUNNING') -and -not $Force) {
                        $r.reason = 'OpenCode is running. Close it first.'
                        Write-Result $r; return
                    }
                    if ($r.state -notin @('AVAILABLE', 'NEEDS_REAPPLY', 'RESTORED')) {
                        $r.reason = "Apply refused in state $($r.state): $($r.reason)"
                        Write-Result $r; return
                    }
                    Write-Result (Invoke-PatchApply -Install $i -PatchDir $patchDir -Report $r)
                }
                'V' { Write-Result (Invoke-PatchVerify -Install $i -PatchDir $patchDir -Report $r) }
                'R' { Write-Result (Invoke-PatchRestore -Install $i -PatchDir $patchDir -Report $r) }
                default { Write-Result $r }
            }
        }
        # Status used to be recomputed twice per keypress (here and inside
        # Invoke-Menu); every recompute re-read the ~180 MB host binary. Compute
        # once, act on it, refresh once after each action.
        $i = $install
        $s = $report
        while ($true) {
            Write-Host ''
            Write-Host '=== SAIPATCH ===' -ForegroundColor Cyan
            Write-Host ("OpenCode : {0}  @  {1}" -f $s.version, $s.install_root)
            Write-Host ("Patch    : {0}  [{1}]" -f $s.patch, $s.state)
            Write-Host ("SAIPATCH native queue: {0}" -f $s.patch_version)
            Write-Host ("Runtime restart required: {0}" -f $(if ($s.runtime_restart_required) { 'YES' } else { 'NO' }))
            if ($s.reason) { Write-Host ("Reason   : {0}" -f $s.reason) }
            Write-Host '[D]etect  [A]pply  [V]erify  [R]estore  [Q]uit'
            $answer = Read-Host 'saipatch'
            $letter = $answer.Trim().ToUpper()
            if ($letter -eq 'Q') { exit 0 }
            if ($letter -in 'A', 'V', 'R', 'D') {
                Invoke-Menu $letter $i $s
                $i = Find-OpenCodeInstall
                $s = Get-PatchStatus -Install $i -PatchDir $patchDir
            }
        }
    }
    'Detect' { Write-Result $report; exit 0 }
    'Status' { Write-Result $report; exit 0 }
    'Apply' {
        if ($report.state -in @('OPEN_CODE_RUNNING') -and -not $Force) {
            $report.reason = 'OpenCode is running. Close it first, or re-run with -Force only when the install format is proven hot-safe.'
            Write-Result $report
            exit 2
        }
        if ($report.state -notin @('AVAILABLE', 'NEEDS_REAPPLY', 'RESTORED')) {
            $report.reason = "Apply refused in state $($report.state): $($report.reason)"
            Write-Result $report
            exit 2
        }
        $result = Invoke-PatchApply -Install $install -PatchDir $patchDir -Report $report
        Write-Result $result
        exit $(if ($result.state -in @('INSTALLED', 'INSTALLED_RESTART_REQUIRED')) { 0 } else { 3 })
    }
    'Verify' {
        $result = Invoke-PatchVerify -Install $install -PatchDir $patchDir -Report $report
        Write-Result $result
        # Restart-required is a truthful, actionable answer, not a verify
        # failure: the disk proof held, memory is just older than the disk.
        exit $(if ($result.state -in @('INSTALLED', 'INSTALLED_RESTART_REQUIRED',
                    'RESTORED', 'RESTORED_RESTART_REQUIRED')) { 0 } else { 3 })
    }
    'Restore' {
        $result = Invoke-PatchRestore -Install $install -PatchDir $patchDir -Report $report
        Write-Result $result
        exit $(if ($result.state -in @('AVAILABLE', 'NOT_INSTALLED',
                    'RESTORED', 'RESTORED_RESTART_REQUIRED')) { 0 } else { 3 })
    }
}
