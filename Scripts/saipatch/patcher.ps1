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
# W2-002: recovery + classification is ONE indivisible read transaction. This
# used to acquire the mutex, run Repair-PatchPublication, RELEASE it, and only
# then classify -- so a writer starting in between could have its intentionally
# intermediate generation misreported as SOURCE_DRIFTED/BROKEN_PATCH. Mutex
# contention now returns an explicit transient BUSY instead of being relabelled
# as corruption.
$report = Get-StablePatchReport -Install $install -PatchDir $patchDir
if ($report.state -eq 'BUSY') {
    # Exit 4 is transient contention, distinct from exit 3 (a real refusal).
    Write-Result $report
    exit 4
}

switch ($Command) {
    'Interactive' {
        # A console menu: Detect / Apply / Verify / Restore / Quit. No GUI, no
        # background anything -- the process exits completely on Q.
        function Invoke-Menu([string]$c, [object]$i, [object]$r) {
            switch ($c) {
                'A' {
                    # W2-002: no unlocked precheck here either. $r came from a
                    # stable read, and Invoke-PatchApply re-locks and
                    # reclassifies anyway, so a local refusal could only ever
                    # disagree with the authoritative path.
                    Write-Result (Invoke-PatchApply -Install $i -PatchDir $patchDir -Report $r -Force:$Force)
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
                # W2-002: the refresh is a stable read like every other
                # classifier, not a bare unlocked Get-PatchStatus.
                $s = Get-StablePatchReport -Install $i -PatchDir $patchDir
            }
        }
    }
    'Detect' { Write-Result $report; exit 0 }
    'Status' { Write-Result $report; exit 0 }
    'Apply' {
        # W2-002: the unlocked outer precheck is GONE. It could refuse a valid
        # Apply from an intermediate generation, and it also contradicted the
        # locked reclassification inside Invoke-PatchApply -- the only
        # authority that matters. -Force is now honoured inside that locked
        # path instead, so the documented escape hatch still exists.
        $result = Invoke-PatchApply -Install $install -PatchDir $patchDir -Report $report -Force:$Force
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
