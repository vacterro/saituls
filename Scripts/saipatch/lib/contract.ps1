# All public state queries share this classifier. Recorded generation hashes
# establish ownership; current package hashes only identify a newer generation.

# Test-PluginContract reads the ~180 MB host executable token-by-token (a
# dozen full scans) and the plan probe is a child powershell that hashes the
# host binary again. The interactive menu recomputes both on every keypress,
# which is why SAIPATCH felt frozen. Both depend only on (install root, exe
# bytes, manifest bytes, tui.json bytes, patch source bytes) -- cache them on
# exactly that key. Any mutation (Apply, Restore, host update) changes at
# least one key component, so a hit always reflects current bytes.
$script:PatchContractCache = @{}
$script:PatchPlanCache = @{}

# The generic engine must not hard-depend on any one patch package's
# common.ps1 helpers: verify.ps1 and the transaction harness source ONLY this
# library. When the patch package does not provide Get-QueueTuiJsonPath, fall
# back to the canonical OpenCode config path it canonically resolves to.
if (-not (Get-Command Get-QueueTuiJsonPath -ErrorAction SilentlyContinue)) {
    function Get-QueueTuiJsonPath {
        return (Join-Path $env:USERPROFILE '.config\opencode\tui.json')
    }
}

function Get-PatchStatus([object]$Install, [string]$PatchDir) {
    $manifestPath = Join-Path $PatchDir 'manifest.json'
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    $report = [pscustomobject]@{
        patch = $manifest.id; patch_version = [string]$manifest.version
        version = $Install.version; install_root = $Install.root
        state = 'NOT_INSTALLED'; supported = @($manifest.supports.opencode)
        backup = $false; reason = $null
        runtime_restart_required = $false; runtime_stale_processes = 0
        runtime_unverified_processes = 0; runtime_transition = $null
    }
    try {
        if (-not $Install.found) { $report.reason = 'OpenCode is not installed'; return $report }
        if ($manifest.supports.opencode -notcontains $Install.version) {
            $report.state = 'UNSUPPORTED_VERSION'; $report.reason = "OpenCode $($Install.version) is not supported"; return $report
        }
        $st = Read-PatchState
        $entries = @($st.patches | Where-Object { $_.id -eq $manifest.id })
        if ($entries.Count -gt 1) { throw 'BROKEN_PATCH: duplicate ownership records' }
        $entry = $entries | Select-Object -First 1
        $exeHash = Get-PatchHash $Install.exe
        # T-144: any recorded patched_sha256 (per-build) is a supported runtime
        # state. Anything else that is not a recorded baseline stays
        # UNSUPPORTED_BUILD -- fail closed.
        $hostPatchedHashes = @()
        try {
            . (Join-Path $PatchDir 'host_patch.ps1')
            $d = Get-HostPatchDescriptor -PatchDir $PatchDir
            $hostPatchedHashes = @($d.builds.PSObject.Properties | ForEach-Object { $_.Value.patched_sha256 })
        } catch { $hostPatchedHashes = @() }
        if ($manifest.supports.builds) {
            $builds = $manifest.supports.builds.PSObject.Properties[$Install.version]
            if (-not $builds -or @($builds.Value) -notcontains $exeHash) {
                if ($hostPatchedHashes -notcontains $exeHash) {
                    $report.state = 'UNSUPPORTED_BUILD'; $report.reason = "Unrecognized executable SHA256: $exeHash"; return $report
                }
            }
        }
        if ($entry.build_hash -and $entry.build_hash -ne $exeHash) {
            if ($hostPatchedHashes -notcontains $exeHash) {
                $report.state = 'UNSUPPORTED_BUILD'; $report.reason = 'Executable differs from recorded supported build'; return $report
            }
        }
        . (Join-Path $PatchDir 'common.ps1')
        $contractKey = '{0}|{1}|{2}|{3}' -f $Install.root, $Install.version, $exeHash, (Get-PatchHash $manifestPath)
        $contract = $script:PatchContractCache[$contractKey]
        if (-not $contract) {
            $contract = Test-PluginContract -InstallRoot $Install.root -Version $Install.version -Manifest $manifest
            $script:PatchContractCache[$contractKey] = $contract
        }
        if (-not $contract.ok) { $report.state = 'SOURCE_DRIFTED'; $report.reason = $contract.reason; return $report }
        if ($entry) {
            if (-not $entry.patched_hashes -or @($entry.targets).Count -ne @($entry.patched_hashes).Count) {
                throw 'BROKEN_PATCH: incomplete installed generation'
            }
            foreach ($t in $entry.patched_hashes) {
                if (@($entry.targets) -notcontains $t.path) { throw 'BROKEN_PATCH: inconsistent target list' }
                $hash = Get-PatchHash $t.path
                if (-not $hash) { throw "BROKEN_PATCH: installed target missing: $($t.path)" }
                if ($hash -ne $t.sha256) { throw "SOURCE_DRIFTED: installed target changed: $($t.path)" }
            }
            if (-not $entry.backup_root -or -not (Test-Path -LiteralPath $entry.backup_root -PathType Container)) {
                throw 'BROKEN_PATCH: installed generation backup missing'
            }
            $report.backup = $true
        }
        # The plan probe is a child powershell that re-reads and re-hashes the
        # host binary; its output is a pure function of (host bytes, patch
        # package bytes, tui.json bytes), so cache it on exactly that key.
        $tuiJsonPath = Get-QueueTuiJsonPath
        $tuiKey = if ([IO.File]::Exists($tuiJsonPath)) { Get-PatchHash $tuiJsonPath } else { '<absent>' }
        $srcKey = (Get-ChildItem -LiteralPath $PatchDir -Recurse -File |
            Sort-Object FullName |
            ForEach-Object { '{0}:{1}:{2}' -f $_.Name, $_.Length, $_.LastWriteTimeUtc.Ticks }) -join ';'
        $planKey = '{0}|{1}|{2}|{3}|{4}|{5}' -f $Install.root, $Install.version, $exeHash, (Get-PatchHash $manifestPath), $tuiKey, $srcKey
        $plan = $script:PatchPlanCache[$planKey]
        if (-not $plan) {
            $output = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PatchDir 'apply.ps1') -InstallRoot $Install.root -Version $Install.version -Plan 2>&1
            if ($LASTEXITCODE -ne 0) { throw "SOURCE_DRIFTED: plan failed: $($output -join ' ')" }
            $plan = ($output -join [Environment]::NewLine) | ConvertFrom-Json
            if (-not $plan.targets) { throw 'SOURCE_DRIFTED: empty patch plan' }
            $script:PatchPlanCache[$planKey] = $plan
        }
        if ($entry) {
            $changed = $entry.patch_version -ne $manifest.version -or $entry.manifest_hash -ne (Get-PatchHash $manifestPath) -or $entry.version -ne $Install.version
            # T-144: the seam target is planned only while the host is a
            # recorded baseline; after a successful apply the probe (by design)
            # stops planning it. A target present ONLY in the installed
            # generation is therefore tolerated exactly when it is the seam
            # image AND its current bytes still hash to one of the recorded
            # patched_sha256 values. Any other entry-only target, any
            # plan-only target, or any seam-byte drift classifies NEEDS_REAPPLY.
            $planPaths = @($plan.targets | ForEach-Object { $_.path })
            $entryPaths = @($entry.targets)
            $onlyInPlan = @($planPaths | Where-Object { $entryPaths -notcontains $_ })
            $onlyInEntry = @($entryPaths | Where-Object { $planPaths -notcontains $_ })
            if ($onlyInPlan.Count -gt 0) { $changed = $true }
            if ($onlyInEntry.Count -gt 0) {
                if ($onlyInEntry.Count -ne 1) { $changed = $true }
                else {
                    $seamHash = $entry.patched_hashes | Where-Object { $_.path -eq $onlyInEntry[0] } | Select-Object -First 1
                    if (-not $seamHash -or $hostPatchedHashes -notcontains $seamHash.sha256 -or (Get-PatchHash $onlyInEntry[0]) -ne $seamHash.sha256) { $changed = $true }
                }
            }
            foreach ($t in $plan.targets) {
                $installed = $entry.patched_hashes | Where-Object path -EQ $t.path | Select-Object -First 1
                if (-not $installed -or $installed.sha256 -ne $t.patched_sha256) { $changed = $true }
            }
            $report.state = if ($changed) { 'NEEDS_REAPPLY' } else { 'INSTALLED' }
        } else {
            foreach ($t in $plan.targets) {
                if ((Get-PatchHash $t.path) -ne $t.original_sha256) { throw "SOURCE_DRIFTED: unowned target: $($t.path)" }
            }
            $report.state = 'AVAILABLE'
        }
        # T-144 restart-awareness: classify the RUNNING processes against the
        # last Apply/Restore transition. The disk state above is already
        # proven; this only answers "is what runs in memory still that state".
        $runtime = Test-PatchRuntimeRestart -Transition (Read-PatchRuntimeTransition) -Install $Install
        $report.runtime_restart_required = $runtime.restart_required
        $report.runtime_stale_processes = $runtime.stale_processes
        $report.runtime_unverified_processes = $runtime.unverified_processes
        $report.runtime_transition = $runtime.transition
        if ($runtime.restart_required -and $report.state -eq 'INSTALLED') {
            $report.state = 'INSTALLED_RESTART_REQUIRED'
        }
        elseif ($runtime.restart_required -and $report.state -eq 'AVAILABLE') {
            # No installed entry + a process older than the last transition:
            # whatever that process runs, it is not these bytes.
            $report.state = 'RESTORED_RESTART_REQUIRED'
        }
        elseif (-not $runtime.restart_required -and $report.state -eq 'AVAILABLE' -and
                $runtime.transition -eq 'RESTORE') {
            # A recorded restore of this exact installation, no stale process.
            $report.state = 'RESTORED'
        }
    } catch {
        $report.reason = $_.Exception.Message
        $report.state = if ($report.reason.StartsWith('SOURCE_DRIFTED:')) { 'SOURCE_DRIFTED' } else { 'BROKEN_PATCH' }
    }
    return $report
}
