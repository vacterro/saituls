function Save-PatchImage([string]$Path, [string]$Blob) {
    $hash = Get-PatchHash $Path
    if ($hash) {
        Write-PatchBytes $Blob ([IO.File]::ReadAllBytes($Path))
        if ((Get-PatchHash $Blob) -ne $hash) { throw "SOURCE_DRIFTED: changed while snapshotting $Path" }
    }
    return [pscustomobject]@{ sha256 = $hash; blob = $(if ($hash) { $Blob } else { $null }) }
}

function Assert-PatchNotRunning([object]$Install) {
    # Same injectable inventory the restart decision reads: exact executable
    # path, never the process name alone.
    $running = @(Get-OpenCodeProcessInventory | Where-Object {
        $_.exe -and $_.exe -ieq $Install.exe
    })
    if ($running.Count) { throw 'OPEN_CODE_RUNNING: close this OpenCode installation before mutation' }
}

function Publish-PatchGeneration([object]$Journal) {
    # Optimistic recheck covers staging time. The process lock serializes all
    # SAIPATCH writers; unexpected outside writes always refuse.
    foreach ($target in @($Journal.targets) + @($Journal.state)) {
        if ((Get-PatchHash $target.path) -ne $target.before.sha256) {
            throw "SOURCE_DRIFTED: changed before commit: $($target.path)"
        }
    }
    Write-PatchJson (Get-PatchJournalPath) $Journal
    Invoke-PatchFault 'prepared'
    $n = 0
    foreach ($t in $Journal.targets) {
        Set-PatchImage $t.path $t.after
        if ((Get-PatchHash $t.path) -ne $t.after.sha256) { throw "Runtime verification failed: $($t.path)" }
        $n++
        Invoke-PatchFault "runtime-$n"
    }
    Invoke-PatchFault 'runtime-committed'
    Invoke-PatchFault 'state-write'
    Set-PatchImage $Journal.state.path $Journal.state.after
    Invoke-PatchFault 'ownership-committed'
    Repair-PatchPublication
}

function Invoke-PatchApply([object]$Install, [string]$PatchDir, [object]$Report) {
    $lock = $null
    try {
        $lock = Enter-PatchLock
        Repair-PatchPublication
        $Report = Get-PatchStatus $Install $PatchDir
        # RESTORED is the post-restore AVAILABLE state, truthfully labeled.
        if ($Report.state -notin @('AVAILABLE', 'NEEDS_REAPPLY', 'RESTORED')) { return $Report }
        Assert-PatchNotRunning $Install
        $manifest = Get-Content -Raw -LiteralPath (Join-Path $PatchDir 'manifest.json') | ConvertFrom-Json
        $st = Read-PatchState
        if (-not $st) { $st = [pscustomobject]@{ patches = @() } }
        $prior = $st.patches | Where-Object id -EQ $manifest.id | Select-Object -First 1
        $raw = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PatchDir 'apply.ps1') -InstallRoot $Install.root -Version $Install.version -Plan 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Plan failed: $($raw -join ' ')" }
        $plan = ($raw -join [Environment]::NewLine) | ConvertFrom-Json
        $generation = [Guid]::NewGuid().ToString('N')
        $root = Join-Path (Get-BackupRoot $manifest.id $Install.version) $generation
        [void][IO.Directory]::CreateDirectory($root)
        $stage = Join-Path $root 'stage'
        [void][IO.Directory]::CreateDirectory($stage)
        $out = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PatchDir 'apply.ps1') -InstallRoot $Install.root -Version $Install.version -Stage $stage 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Staging failed: $($out -join ' ')" }
        $records = @(); $originals = @(); $patched = @(); $seen = @{}
        foreach ($t in $plan.targets) {
            $path = [IO.Path]::GetFullPath($t.path)
            if ($seen.ContainsKey($path)) { throw 'SOURCE_DRIFTED: duplicate planned target' }
            $seen[$path] = $true
            $staged = [IO.Path]::GetFullPath((Join-Path $stage $t.file))
            if (-not $staged.StartsWith($stage + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'SOURCE_DRIFTED: stage path escape'
            }
            if ((Get-PatchHash $staged) -ne $t.patched_sha256) { throw "Staged hash mismatch: $($t.file)" }
            $n = $records.Count
            $before = Save-PatchImage $path (Join-Path $root "before-$n")
            $oldHash = $prior.patched_hashes | Where-Object path -EQ $path | Select-Object -First 1
            $expected = if ($oldHash) { $oldHash.sha256 } else { $t.original_sha256 }
            if ($before.sha256 -ne $expected) { throw "SOURCE_DRIFTED: target changed: $path" }
            $original = $prior.original_hashes | Where-Object path -EQ $path | Select-Object -First 1
            if ($oldHash -and -not $original) { throw 'BROKEN_PATCH: previous original record missing' }
            if (-not $original) {
                $original = [pscustomobject]@{ path = $path; original_sha256 = $before.sha256; backup = $before.blob }
            }
            $originals += $original
            $patched += [pscustomobject]@{ path = $path; sha256 = $t.patched_sha256 }
            $records += [pscustomobject]@{
                path = $path; before = $before
                after = [pscustomobject]@{ sha256 = $t.patched_sha256; blob = $staged }
            }
        }
        # Targets removed by the new package restore their recorded originals.
        foreach ($old in $prior.patched_hashes) {
            if ($seen.ContainsKey($old.path)) { continue }
            $original = $prior.original_hashes | Where-Object path -EQ $old.path | Select-Object -First 1
            if (-not $original) { throw 'BROKEN_PATCH: removed target lacks original record' }
            $before = Save-PatchImage $old.path (Join-Path $root ("before-" + $records.Count))
            if ($before.sha256 -ne $old.sha256) { throw "SOURCE_DRIFTED: removed target changed: $($old.path)" }
            $records += [pscustomobject]@{
                path = $old.path; before = $before
                after = [pscustomobject]@{ sha256 = $original.original_sha256; blob = $original.backup }
            }
        }
        $entry = [pscustomobject]@{
            id = $manifest.id; patch_id = $manifest.id; patch_version = $manifest.version
            manifest_hash = Get-PatchHash (Join-Path $PatchDir 'manifest.json')
            version = $Install.version; opencode_version = $Install.version
            build_hash = Get-PatchHash $Install.exe; build_path = $Install.exe
            targets = @($patched | ForEach-Object path)
            original_hashes = $originals; patched_hashes = $patched
            installed_generation = $generation; transaction = 'COMMITTED'
            backup_root = $root; installed_at = (Get-Date).ToUniversalTime().ToString('o')
        }
        $stateBefore = Save-PatchImage (Get-StatePath) (Join-Path $root 'state-before')
        $st.patches = @($st.patches | Where-Object id -NE $manifest.id) + @($entry)
        $stateAfterPath = Join-Path $root 'state-after'
        Write-PatchJson $stateAfterPath $st
        $journal = [pscustomobject]@{
            schema = 1; operation = 'APPLY'; generation = $generation
            targets = $records
            state = [pscustomobject]@{
                path = Get-StatePath; before = $stateBefore
                after = [pscustomobject]@{ sha256 = Get-PatchHash $stateAfterPath; blob = $stateAfterPath }
            }
        }
        Publish-PatchGeneration $journal
        # T-144: persist the runtime transition AFTER the disk commit is
        # proven: patch version, install root, exact executable path, UTC
        # timestamp, and the resulting disk state (committed target hashes).
        Write-PatchRuntimeTransition -Operation 'APPLY' -Manifest $manifest -Install $Install `
            -TargetHashes @($patched) -StateSha256 (Get-PatchHash (Get-StatePath))
        return (Get-PatchStatus $Install $PatchDir)
    } catch {
        $reason = $_.Exception.Message
        try { if ($lock) { Repair-PatchPublication } } catch { $reason += "; recovery retained: $($_.Exception.Message)" }
        $Report.state = if ($reason.StartsWith('SOURCE_DRIFTED:')) { 'SOURCE_DRIFTED' } elseif ($reason.StartsWith('OPEN_CODE_RUNNING:')) { 'OPEN_CODE_RUNNING' } else { 'BROKEN_PATCH' }
        $Report.reason = $reason
        return $Report
    } finally { if ($lock) { $lock.ReleaseMutex(); $lock.Dispose() } }
}

function Invoke-PatchVerify([object]$Install, [string]$PatchDir, [object]$Report) {
    return (Get-PatchStatus $Install $PatchDir)
}

function Invoke-PatchRestore([object]$Install, [string]$PatchDir, [object]$Report) {
    $lock = $null
    try {
        $lock = Enter-PatchLock
        Repair-PatchPublication
        $manifest = Get-Content -Raw -LiteralPath (Join-Path $PatchDir 'manifest.json') | ConvertFrom-Json
        $st = Read-PatchState
        $entry = $st.patches | Where-Object id -EQ $manifest.id | Select-Object -First 1
        if (-not $entry) { $Report.state = 'NOT_INSTALLED'; return $Report }
        Assert-PatchNotRunning $Install
        # Phase A: ALL installed hashes and originals, including the last target.
        # Current package contents and version are not restore authorities.
        if (-not $entry.patched_hashes -or @($entry.targets).Count -ne @($entry.patched_hashes).Count) {
            throw 'BROKEN_PATCH: incomplete installed-generation target list'
        }
        foreach ($t in $entry.patched_hashes) {
            if (@($entry.targets) -notcontains $t.path) { throw 'BROKEN_PATCH: inconsistent target list' }
            if ((Get-PatchHash $t.path) -ne $t.sha256) { throw "SOURCE_DRIFTED: restore target changed: $($t.path)" }
            $original = $entry.original_hashes | Where-Object path -EQ $t.path | Select-Object -First 1
            if (-not $original) { throw "BROKEN_PATCH: no original for $($t.path)" }
            if ($original.original_sha256 -and (Get-PatchHash $original.backup) -ne $original.original_sha256) {
                throw "BROKEN_PATCH: original backup corrupted: $($t.path)"
            }
        }
        # Phase B: prepare rollback images before any live mutation.
        $generation = [Guid]::NewGuid().ToString('N')
        $root = Join-Path (Get-BackupRoot $manifest.id $entry.version) $generation
        [void][IO.Directory]::CreateDirectory($root)
        $records = @()
        foreach ($t in $entry.patched_hashes) {
            $original = $entry.original_hashes | Where-Object path -EQ $t.path | Select-Object -First 1
            $before = Save-PatchImage $t.path (Join-Path $root ("before-" + $records.Count))
            if ($before.sha256 -ne $t.sha256) { throw "SOURCE_DRIFTED: restore target changed during snapshot: $($t.path)" }
            $records += [pscustomobject]@{
                path = $t.path; before = $before
                after = [pscustomobject]@{ sha256 = $original.original_sha256; blob = $original.backup }
            }
        }
        $stateBefore = Save-PatchImage (Get-StatePath) (Join-Path $root 'state-before')
        $st.patches = @($st.patches | Where-Object id -NE $manifest.id)
        $stateAfterPath = Join-Path $root 'state-after'
        Write-PatchJson $stateAfterPath $st
        Publish-PatchGeneration ([pscustomobject]@{
            schema = 1; operation = 'RESTORE'; generation = $generation; targets = $records
            state = [pscustomobject]@{
                path = Get-StatePath; before = $stateBefore
                after = [pscustomobject]@{ sha256 = Get-PatchHash $stateAfterPath; blob = $stateAfterPath }
            }
        })
        # T-144: the Restore transition survives OUTSIDE state.json (the patch
        # entry is gone now) so verify can still prove a pre-restore process
        # is running stale bytes. disk_state = the restored after-images.
        Write-PatchRuntimeTransition -Operation 'RESTORE' -Manifest $manifest -Install $Install `
            -TargetHashes @($records | ForEach-Object {
                [pscustomobject]@{ path = $_.path; sha256 = $_.after.sha256 }
            }) -StateSha256 (Get-PatchHash (Get-StatePath))
        $Report.state = 'AVAILABLE'; $Report.backup = $false; $Report.reason = $null
        return $Report
    } catch {
        $reason = $_.Exception.Message
        try { if ($lock) { Repair-PatchPublication } } catch { $reason += "; recovery retained: $($_.Exception.Message)" }
        $Report.state = if ($reason.StartsWith('SOURCE_DRIFTED:')) { 'SOURCE_DRIFTED' } else { 'BROKEN_PATCH' }
        $Report.reason = $reason
        return $Report
    } finally { if ($lock) { $lock.ReleaseMutex(); $lock.Dispose() } }
}
