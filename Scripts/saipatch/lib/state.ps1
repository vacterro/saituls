function Get-StatePath { Join-Path $env:LOCALAPPDATA 'SAITULS\SAIPATCH\state.json' }

# The interactive menu recomputes status on every keypress, and the host
# executable is ~180 MB: a full SHA256 per keypress made SAIPATCH feel frozen.
# Memoize on (identity, length, mtime) -- any writer (Apply/Restore/OpenCode
# update) changes at least one of them, so a cache hit always reflects the
# current bytes.
$script:PatchHashCache = @{}
function Get-PatchHash([string]$Path) {
    if ([IO.File]::Exists($Path)) {
        $fi = [IO.FileInfo]::new($Path)
        $key = '{0}|{1}|{2}' -f $fi.FullName.ToUpperInvariant(), $fi.Length, $fi.LastWriteTimeUtc.Ticks
        $hit = $script:PatchHashCache[$key]
        if ($hit) { return $hit }
        $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
        $script:PatchHashCache[$key] = $hash
        return $hash
    }
    if (Test-Path -LiteralPath $Path) { throw "Expected regular file: $Path" }
    return $null
}

# Commit prepared bytes before journal/runtime references. Replacement is local.
function Write-PatchBytes([string]$Path, [byte[]]$Bytes) {
    $dir = Split-Path -Parent $Path
    [void][IO.Directory]::CreateDirectory($dir)
    $tmp = Join-Path $dir ([IO.Path]::GetRandomFileName())
    try {
        $stream = [IO.File]::Open($tmp, 'CreateNew', 'Write', 'None')
        try { $stream.Write($Bytes, 0, $Bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($tmp, $Path, [NullString]::Value) }
        else { [IO.File]::Move($tmp, $Path) }
    } finally { if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) } }
}

function Write-PatchJson([string]$Path, [object]$Value) {
    Write-PatchBytes $Path ([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $Value -Depth 30)))
}

function Read-PatchState {
    $p = Get-StatePath
    if (-not (Test-Path -LiteralPath $p)) { return $null }
    try {
        $state = Get-Content -Raw -LiteralPath $p | ConvertFrom-Json
        if ($null -eq $state -or -not $state.PSObject.Properties['patches']) { throw 'missing patches' }
        return $state
    } catch { throw "BROKEN_PATCH: SAIPATCH state is unreadable: $($_.Exception.Message)" }
}

function Write-PatchState([object]$State) { Write-PatchJson (Get-StatePath) $State }

function Get-BackupRoot([string]$patchId, [string]$version) {
    Join-Path $env:LOCALAPPDATA ("SAITULS\SAIPATCH\backups\$patchId\$version")
}

function Get-PatchJournalPath { Join-Path (Split-Path -Parent (Get-StatePath)) 'pending.json' }

# T-144 restart-awareness: the last Apply/Restore transition of this machine,
# persisted OUTSIDE state.json so a Restore (which removes the patch entry)
# still leaves the proof a running process needs. This record is what makes
# "the OpenCode you are looking at predates the bytes on disk" provable.
function Get-PatchRuntimeStatePath { Join-Path (Split-Path -Parent (Get-StatePath)) 'runtime.json' }

function Read-PatchRuntimeTransition {
    $p = Get-PatchRuntimeStatePath
    if (-not [IO.File]::Exists($p)) { return $null }
    try {
        $t = Get-Content -Raw -LiteralPath $p | ConvertFrom-Json
        if ($null -eq $t -or $t.schema -ne 1 -or -not $t.transitioned_at) { return $null }
        return $t
    } catch { return $null }
}

function Write-PatchRuntimeTransition {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('APPLY', 'RESTORE')][string]$Operation,
        [Parameter(Mandatory = $true)][object]$Manifest,
        [Parameter(Mandatory = $true)][object]$Install,
        [Parameter(Mandatory = $true)][object]$TargetHashes,
        [Parameter(Mandatory = $true)][string]$StateSha256
    )
    $doc = [pscustomobject]@{
        schema = 1
        operation = $Operation
        patch_id = [string]$Manifest.id
        patch_version = [string]$Manifest.version
        install_root = [string]$Install.root
        exe = [string]$Install.exe
        transitioned_at = (Get-Date).ToUniversalTime().ToString('o')
        disk_state = [pscustomobject]@{
            targets = @($TargetHashes)
            state_sha256 = $StateSha256
        }
    }
    Write-PatchJson (Get-PatchRuntimeStatePath) $doc
}

# Injectable process inventory seam. Production reads REAL Windows process
# metadata: the exact executable path (never the process name alone) and the
# process start time. Deterministic tests replace this one function; nothing
# else in the restart decision touches live process state.
function Get-OpenCodeProcessInventory {
    $items = @()
    foreach ($p in @(Get-Process opencode -ErrorAction SilentlyContinue)) {
        try {
            $items += [pscustomobject]@{
                pid = $p.Id
                exe = $p.MainModule.FileName
                started_at = $p.StartTime.ToUniversalTime()
            }
        } catch {
            # Exited between listing and query, or metadata denied: the exact
            # path can NOT be established, so this process is unverifiable --
            # it is counted, never matched, never fatal.
            $items += [pscustomobject]@{ pid = $p.Id; exe = $null; started_at = $null }
        }
    }
    return $items
}

# The restart decision. A process is STALE only when its exact executable path
# equals the patched installation AND its start time predates the recorded
# transition. A reused PID carries a fresh start time, so PID alone can never
# satisfy the proof.
function Test-PatchRuntimeRestart {
    param($Transition, [object]$Install)
    $result = [pscustomobject]@{
        matched = $false; restart_required = $false
        stale_processes = 0; unverified_processes = 0; transition = $null
    }
    if (-not $Transition) { return $result }
    if (-not $Install.exe -or [string]$Transition.exe -ne $Install.exe) { return $result }
    if ([string]$Transition.install_root -ne [string]$Install.root) { return $result }
    $result.matched = $true
    $result.transition = [string]$Transition.operation
    $stamp = [DateTime]::Parse($Transition.transitioned_at,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    foreach ($proc in @(Get-OpenCodeProcessInventory)) {
        if (-not $proc.exe) { $result.unverified_processes++; continue }
        if ($proc.exe -ine $Install.exe) { continue }
        if ($proc.started_at -and $proc.started_at.ToUniversalTime() -lt $stamp) {
            $result.restart_required = $true
            $result.stale_processes++
        }
    }
    return $result
}

function Enter-PatchLock {
    $identity = [IO.Path]::GetFullPath((Get-StatePath)).ToUpperInvariant()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity))).Replace('-', '') }
    finally { $sha.Dispose() }
    $mutex = New-Object Threading.Mutex($false, "Global\SAITULS_SAIPATCH_$hash")
    try {
        try { $held = $mutex.WaitOne(5000) } catch [Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { throw 'BUSY: another SAIPATCH transaction owns this state' }
        return $mutex
    } catch { $mutex.Dispose(); throw }
}

function Invoke-PatchFault([string]$Point) {
    # Inert outside explicitly configured disposable harnesses.
    if ($env:SAIPATCH_TEST_FAULT -eq $Point) { throw "Injected failure: $Point" }
    if ($env:SAIPATCH_TEST_CRASH -eq $Point) { [Environment]::Exit(91) }
}

function Set-PatchImage([string]$Path, [object]$Image) {
    if ((Get-PatchHash $Path) -eq $Image.sha256) { return }
    if ($null -eq $Image.sha256) {
        if ([IO.File]::Exists($Path)) { [IO.File]::Delete($Path) }
    } else {
        if ((Get-PatchHash $Image.blob) -ne $Image.sha256) { throw "BROKEN_PATCH: backup hash mismatch: $($Image.blob)" }
        Write-PatchBytes $Path ([IO.File]::ReadAllBytes($Image.blob))
    }
}

# State publication is the commit point: before it restore previous generation;
# after it retain the new one. Validate ALL images before recovery mutates.
function Repair-PatchPublication {
    $path = Get-PatchJournalPath
    if (-not [IO.File]::Exists($path)) { return }
    $j = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
    if ($j.schema -ne 1 -or $j.state.path -ne (Get-StatePath) -or -not $j.targets) {
        throw 'BROKEN_PATCH: invalid publication journal'
    }
    $stateHash = Get-PatchHash $j.state.path
    $committed = $stateHash -eq $j.state.after.sha256
    if (-not $committed -and $stateHash -ne $j.state.before.sha256) { throw 'SOURCE_DRIFTED: ownership changed during publication' }
    foreach ($t in $j.targets) {
        $hash = Get-PatchHash $t.path
        if ($hash -ne $t.before.sha256 -and $hash -ne $t.after.sha256) { throw "SOURCE_DRIFTED: recovery target changed: $($t.path)" }
        foreach ($image in @($t.before, $t.after)) {
            if ($image.sha256 -and (Get-PatchHash $image.blob) -ne $image.sha256) { throw 'BROKEN_PATCH: publication image corrupted' }
        }
    }
    foreach ($t in $j.targets) {
        $image = if ($committed) { $t.after } else { $t.before }
        Set-PatchImage $t.path $image
    }
    # The ownership file was untouched or atomically committed; preserve bytes.
    [IO.File]::Delete($path)
}
