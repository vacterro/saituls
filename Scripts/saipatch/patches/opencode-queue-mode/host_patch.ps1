# T-144 seam-B host patch builder (fail-closed). Reads the descriptor
# host-patch.json and produces the patched image bytes for the recorded
# executable. Every guard refusal is terminal: a wrong source, a moved anchor
# or a non-length-preserving replacement can never reach a staging directory.
# The bytes are deterministic: for the recorded source image the patched image
# hash is exactly descriptor.patched_sha256, so the engine can verify staged
# bytes against the plan hash with its ordinary target machinery.
#
# Stream scanning (Get-AsciiTokenEvidence in common.ps1) never loads the whole
# executable into memory. The repaired region is copied through a bounded
# sliding window around the single anchor match; byte windows are compared
# directly, never as strings, so encoding cannot invent a false equality.

function Get-HostPatchDescriptor {
    param([Parameter(Mandatory = $true)][string]$PatchDir)
    $path = Join-Path $PatchDir 'host-patch.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "host patch descriptor missing: $path" }
    try {
        $d = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
    } catch {
        throw "BROKEN_PATCH: host patch descriptor unreadable: $($_.Exception.Message)"
    }
    if (-not $d.hook -or -not $d.anchor -or -not $d.replacement -or -not $d.builds) {
        throw 'BROKEN_PATCH: host patch descriptor incomplete'
    }
    # T-144 P0: anchor/replacement windows are stored as exact byte files
    # (anchor-853.bin / replacement-853.bin). Text literals cannot survive the
    # PowerShell->JSON->regex round trip for a 853-byte window with quotes.
    if ($d.anchor.text_file) {
        $binPath = Join-Path $PatchDir $d.anchor.text_file
        if (-not (Test-Path -LiteralPath $binPath -PathType Leaf)) { throw "BROKEN_PATCH: anchor bytes missing: $binPath" }
        $d.anchor | Add-Member -NotePropertyName bytesData -NotePropertyValue ([IO.File]::ReadAllBytes($binPath)) -Force
        if ($d.anchor.bytesData.Length -ne [int]$d.anchor.bytes) {
            throw "BROKEN_PATCH: anchor byte file $($d.anchor.bytesData.Length) != declared $($d.anchor.bytes)"
        }
    } else {
        throw 'BROKEN_PATCH: descriptor lacks anchor.text_file (853-byte window requires exact byte file)'
    }
    if ($d.replacement.text_file) {
        $binPath = Join-Path $PatchDir $d.replacement.text_file
        if (-not (Test-Path -LiteralPath $binPath -PathType Leaf)) { throw "BROKEN_PATCH: replacement bytes missing: $binPath" }
        $d.replacement | Add-Member -NotePropertyName bytesData -NotePropertyValue ([IO.File]::ReadAllBytes($binPath)) -Force
        if ($d.replacement.bytesData.Length -ne [int]$d.replacement.bytes) {
            throw "BROKEN_PATCH: replacement byte file $($d.replacement.bytesData.Length) != declared $($d.replacement.bytes)"
        }
    } else {
        throw 'BROKEN_PATCH: descriptor lacks replacement.text_file'
    }
    return $d
}

function Resolve-HostPatchBuild {
    param(
        [Parameter(Mandatory = $true)][object]$Descriptor,
        [Parameter(Mandatory = $true)][string]$ExePath
    )
    $hash = (Get-FileHash -LiteralPath $ExePath -Algorithm SHA256).Hash.ToLowerInvariant()
    foreach ($property in $Descriptor.builds.PSObject.Properties) {
        $build = $property.Value
        if ($build.target.sha256.ToLowerInvariant() -eq $hash) {
            return [pscustomobject]@{ version = $property.Name; target = $build.target; patched_sha256 = $build.patched_sha256 }
        }
    }
    return $null
}

# Search a large file for one exact byte pattern without loading it whole.
# Returns the absolute offset of the first match and the total match count.
# The needle is mapped 1:1 to a regex over Latin1 (GetEncoding(28591), the
# byte<->char identity codec that exists on .NET Framework where ::Latin1
# does not): scanning runs inside the regex engine instead of an interpreted
# PowerShell byte loop, which makes a 180 MB image a seconds-scale operation.
# Escaping every needle byte makes the pattern literal; the tail carry keeps
# boundary-straddling matches exact (a partial tail is never discarded).
function Find-HostPatchAnchor {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Needle
    )
    $enc = [Text.Encoding]::GetEncoding(28591)
    $pattern = $enc.GetString($Needle)
    $regex = New-Object Text.RegularExpressions.Regex (
        [Text.RegularExpressions.Regex]::Escape($pattern),
        [Text.RegularExpressions.RegexOptions]::None)
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $buffer = New-Object byte[] (4 * 1024 * 1024)
        $tail = New-Object byte[] 0
        [long]$offset = 0
        [long]$first = -1
        $count = 0
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $search = New-Object byte[] ($tail.Length + $read)
            [Array]::Copy($tail, 0, $search, 0, $tail.Length)
            [Array]::Copy($buffer, 0, $search, $tail.Length, $read)
            $text = $enc.GetString($search)
            foreach ($m in $regex.Matches($text)) {
                if ($first -lt 0) { $first = $offset - $tail.Length + $m.Index }
                $count++
            }
            $keep = [Math]::Min([Math]::Max(0, $Needle.Length - 1), $search.Length)
            $tail = New-Object byte[] $keep
            [Array]::Copy($search, $search.Length - $keep, $tail, 0, $keep)
            $offset += $read
        }
        [pscustomobject]@{ first = $first; count = $count }
    }
    finally { $stream.Dispose() }
}

function Test-HostPatchApplicable {
    # All Build-Patch checks without writing anything. Returns $true when the
    # recorded build carries the anchor exactly once and the replacement fits.
    # Any probe failure means NOT applicable: planning is a pure predicate, it
    # must never throw into the engine's classifier.
    param(
        [Parameter(Mandatory = $true)][string]$PatchDir,
        [Parameter(Mandatory = $true)][string]$ExePath
    )
    try {
        if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) { return $false }
        $descriptor = Get-HostPatchDescriptor -PatchDir $PatchDir
        $build = Resolve-HostPatchBuild -Descriptor $descriptor -ExePath $ExePath
        if (-not $build) { return $false }
        if ((Get-PatchHash $ExePath) -ne $build.target.sha256) { return $false }
        $info = Get-Item -LiteralPath $ExePath
        if ($info.Length -ne $build.target.bytes) { return $false }
        $found = Find-HostPatchAnchor -Path $ExePath -Needle $descriptor.anchor.bytesData
        if ($found.count -ne 1) { return $false }
        if ($descriptor.replacement.bytesData.Length -gt $descriptor.anchor.bytesData.Length) { return $false }
        return $true
    } catch { return $false }
}

function Build-HostPatchImage {
    # Produce the patched image bytes at $Destination from the recorded source
    # at $ExePath. Refuses (exit 2) on any guard violation; the source file is
    # never written. The only difference between source and image is inside the
    # anchor window; the size never changes.
    param(
        [Parameter(Mandatory = $true)][string]$PatchDir,
        [Parameter(Mandatory = $true)][string]$ExePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    $descriptor = Get-HostPatchDescriptor -PatchDir $PatchDir
    if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        Write-Error "REFUSE: host executable missing: $ExePath"; exit 2
    }
    $build = Resolve-HostPatchBuild -Descriptor $descriptor -ExePath $ExePath
    if (-not $build) {
        $observed = (Get-FileHash -LiteralPath $ExePath -Algorithm SHA256).Hash
        $known = @($descriptor.builds.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value.target.sha256)" }) -join ' '
        Write-Error "REFUSE: unknown executable build sha256 $observed (known: $known)"; exit 2
    }
    $sourceHash = Get-PatchHash $ExePath
    if ($sourceHash -ne $build.target.sha256) {
        Write-Error "REFUSE: host sha256 mismatch`n  expected $($build.target.sha256)`n  observed $sourceHash"; exit 2
    }
    $info = Get-Item -LiteralPath $ExePath
    if ($info.Length -ne $build.target.bytes) {
        Write-Error "REFUSE: host size mismatch expected $($build.target.bytes) observed $($info.Length)"; exit 2
    }
    $enc = [Text.Encoding]::GetEncoding(28591)
    $anchor = $descriptor.anchor.bytesData
    $replacement = $descriptor.replacement.bytesData
    if ($replacement.Length -gt $anchor.Length) {
        Write-Error "REFUSE: replacement $($replacement.Length) > anchor $($anchor.Length) (not length-preserving)"; exit 2
    }
    $found = Find-HostPatchAnchor -Path $ExePath -Needle $anchor
    if ($found.count -ne 1) {
        Write-Error "REFUSE: anchor occurs $($found.count) times (need exactly 1)"; exit 2
    }
    $offset = $found.first
    $pad = New-Object byte[] ($anchor.Length - $replacement.Length)
    for ($i = 0; $i -lt $pad.Length; $i++) { $pad[$i] = [byte]$descriptor.replacement.pad_byte }
    $window = New-Object byte[] $anchor.Length
    [Array]::Copy($replacement, 0, $window, 0, $replacement.Length)
    [Array]::Copy($pad, 0, $window, $replacement.Length, $pad.Length)

    $stream = [IO.File]::Open($ExePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $out = [IO.File]::Open($Destination, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            # Copy [0, offset) verbatim.
            $before = $offset
            $buffer = New-Object byte[] (1024 * 1024)
            $stream.Seek(0, [IO.SeekOrigin]::Begin) | Out-Null
            while ($before -gt 0) {
                $take = [int][Math]::Min($buffer.Length, $before)
                [void]$stream.Read($buffer, 0, $take)
                $out.Write($buffer, 0, $take)
                $before -= $take
            }
            # The repaired window.
            $out.Write($window, 0, $window.Length)
            # Copy the tail verbatim.
            [void]$stream.Seek($offset + $anchor.Length, [IO.SeekOrigin]::Begin)
            while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) { $out.Write($buffer, 0, $read) }
            $out.Flush($true)
        }
        finally { $out.Dispose() }
    }
    finally { $stream.Dispose() }

    # Verify the written image: size, window-only difference, uniqueness of the
    # replacement, absence of the original anchor, final hash. The fail-open
    # dispatch needle (hook-present branch + hook-absent original dispatch in
    # one statement) must occur exactly once; the OLD optional-chain call must
    # be gone.
    $imageHash = Get-PatchHash $Destination
    $imageInfo = Get-Item -LiteralPath $Destination
    if ($imageInfo.Length -ne $build.target.bytes) { Write-Error 'REFUSE: patched image size changed'; exit 3 }
    $imageAnchor = Find-HostPatchAnchor -Path $Destination -Needle $anchor
    if ($imageAnchor.count -ne 0) { Write-Error 'REFUSE: original anchor still present in patched image'; exit 3 }
    $failOpenNeedle = [Text.Encoding]::GetEncoding(28591).GetBytes("let K=globalThis.$($descriptor.hook);K?K(w,e=>H(e,w)):H(w.payload,{directory:w.directory,workspace:w.workspace})")
    $hookHits = Find-HostPatchAnchor -Path $Destination -Needle $failOpenNeedle
    if ($hookHits.count -ne 1) { Write-Error "REFUSE: fail-open dispatch site count $($hookHits.count) (need exactly 1)"; exit 3 }
    $oldOptionalChain = [Text.Encoding]::GetEncoding(28591).GetBytes("globalThis.$($descriptor.hook)?.(w,e=>H(e,w))")
    $oldHits = Find-HostPatchAnchor -Path $Destination -Needle $oldOptionalChain
    if ($oldHits.count -ne 0) { Write-Error 'REFUSE: old optional-chain seam still present in patched image'; exit 3 }
    if ($imageHash -ne $build.patched_sha256) {
        Write-Error "REFUSE: patched image hash mismatch`n  expected $($build.patched_sha256)`n  observed $imageHash"; exit 3
    }
    Write-Output "host patch ok: build $($build.version) source $sourceHash"
    Write-Output "anchor offset $offset len $($anchor.Length)"
    Write-Output "patched sha256 $imageHash"
    Write-Output "patched size $($imageInfo.Length)"
}
