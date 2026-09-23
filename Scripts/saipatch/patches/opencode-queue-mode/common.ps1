function Get-QueueConfigDir {
    # OpenCode's global config dir. Proven against the disposable 1.18.29 host:
    # USERPROFILE\.config\opencode carries opencode.json AND tui.json
    # (packages/opencode/src/config/tui.ts reads the tui.json 'plugin' array;
    # the '<config>/plugins' directory is scanned ONLY by the SERVER plugin
    # loader, which rejects TUI modules with "must default export an object
    # with server()").
    $dir = Join-Path $env:USERPROFILE '.config\opencode'
    return $dir
}

function Get-QueuePluginDir {
    # TUI module files live OUTSIDE the server-scanned plugins/ dirs. The TUI
    # host imports exactly what tui.json references, by file:// URL.
    $dir = Join-Path (Get-QueueConfigDir) 'tui-modules'
    return $dir
}

function Get-QueueSoundDir {
    # Patch-owned runtime asset storage: the seven completion-sound WAVs live
    # here so an applied patch never depends on the SAITULS repository
    # remaining present (clause 24). SAIPATCH Apply stages them, Restore
    # removes them; user settings under LOCALAPPDATA are untouched by both.
    return Join-Path (Get-QueuePluginDir) 'sounds'
}

function Get-QueuePluginFile {
    return Join-Path (Get-QueuePluginDir) 'saipatch-native-queue-2x.js'
}

function Get-QueueTuiJsonPath {
    return Join-Path (Get-QueueConfigDir) 'tui.json'
}

function Get-QueuePluginUri {
    $path = (Get-QueuePluginFile) -replace '\\', '/'
    return "file:///$path"
}

# Deterministic merged tui.json: keep every existing key and every existing
# plugin entry untouched, append exactly one SAIPATCH entry when absent, and
# serialize identically on every call so the plan hash matches the staged hash
# byte for byte.
function Join-QueueTuiJson {
    $path = Get-QueueTuiJsonPath
    $existing = New-Object PSObject
    if ([IO.File]::Exists($path)) {
        $raw = Get-Content -Raw -LiteralPath $path
        if ($raw.Trim().Length -gt 0) {
            $parsed = $raw | ConvertFrom-Json
            if ($null -ne $parsed) { $existing = $parsed }
        }
    }
    $entry = Get-QueuePluginUri
    $plugins = @()
    $present = $false
    if ($existing.PSObject.Properties['plugin']) {
        $plugins = @($existing.plugin)
        foreach ($item in $plugins) {
            $spec = if ($item -is [array]) { "$($item[0])" } else { "$item" }
            if ($spec -eq $entry) { $present = $true; break }
        }
    }
    if (-not $present) { $plugins = @($plugins) + @($entry) }
    $merged = $existing.PSObject.Copy()
    if ($merged.PSObject.Properties['plugin']) { $merged.PSObject.Properties.Remove('plugin') }
    $merged | Add-Member -NotePropertyName plugin -NotePropertyValue $plugins
    return (ConvertTo-Json -InputObject $merged -Depth 20)
}

# Search a large file for several exact ASCII tokens in ONE sequential pass.
# The plugin contract needs a dozen token counts from the ~180 MB host binary;
# calling the per-token scanner that many times re-read the whole image per
# status query and dominated SAIPATCH's startup time. Same tail-carry rules as
# Get-AsciiTokenEvidence: a partial token at a chunk boundary is never
# discarded, so boundary-straddling matches stay exact.
function Get-AsciiTokenCounts {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Tokens
    )
    $counts = @{}
    foreach ($t in $Tokens) { $counts[$t] = 0 }
    $keepMax = 0
    foreach ($t in $Tokens) { if ($t.Length - 1 -gt $keepMax) { $keepMax = $t.Length - 1 } }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $buffer = New-Object byte[] (1024 * 1024)
        $tail = ''
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $search = $tail + [Text.Encoding]::ASCII.GetString($buffer, 0, $read)
            foreach ($t in $Tokens) {
                $searchAt = 0
                while (($index = $search.IndexOf($t, $searchAt, [StringComparison]::Ordinal)) -ge 0) {
                    $counts[$t]++
                    $searchAt = $index + $t.Length
                }
            }
            $keep = [Math]::Min([Math]::Max(0, $keepMax), $search.Length)
            $tail = if ($keep -gt 0) { $search.Substring($search.Length - $keep) } else { '' }
        }
    }
    finally { $stream.Dispose() }
    return $counts
}

# Read a small ASCII window around a marker in a large executable. This avoids
# loading OpenCode's roughly 180 MB single-file binary into a PowerShell string.
function Get-AsciiTokenEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Token,
        [int]$Radius = 4096
    )
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $buffer = New-Object byte[] (1024 * 1024)
        $tail = ''
        [long]$offset = 0
        [long]$firstAbsolute = -1
        $count = 0
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $chunk = [Text.Encoding]::ASCII.GetString($buffer, 0, $read)
            $search = $tail + $chunk
            $searchAt = 0
            while (($index = $search.IndexOf($Token, $searchAt, [StringComparison]::Ordinal)) -ge 0) {
                if ($firstAbsolute -lt 0) { $firstAbsolute = $offset - $tail.Length + $index }
                $count++
                $searchAt = $index + $Token.Length
            }
            $keep = [Math]::Min([Math]::Max(0, $Token.Length - 1), $search.Length)
            $tail = if ($keep -gt 0) { $search.Substring($search.Length - $keep) } else { '' }
            $offset += $read
        }
        if ($firstAbsolute -lt 0) { return $null }
        [long]$start = [Math]::Max(0, $firstAbsolute - $Radius)
        [long]$length = [Math]::Min($stream.Length - $start, ($Radius * 2) + $Token.Length)
        $window = New-Object byte[] ([int]$length)
        [void]$stream.Seek($start, [IO.SeekOrigin]::Begin)
        $windowRead = $stream.Read($window, 0, $window.Length)
        [pscustomobject]@{
            count = $count
            window = [Text.Encoding]::ASCII.GetString($window, 0, $windowRead)
        }
    }
    finally { $stream.Dispose() }
}

# T-148 invariant: prove the installed OpenCode Go provider still owns one
# stable session identity header. Queue admissions, tools, retries and
# reconnects must never rotate it.
function Test-OpenCodeGoSessionContract {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)]$Manifest
    )
    $bad = { param($r) [pscustomobject]@{ ok = $false; reason = $r } }
    $spec = $Manifest.supports.opencodeGoSession
    if ($null -eq $spec) { return (& $bad 'manifest lacks the OpenCode Go session-header contract') }
    $fingerprint = $spec.fingerprints.PSObject.Properties[$Version]
    if ($null -eq $fingerprint) {
        return (& $bad "no OpenCode Go session fingerprint is registered for version $Version")
    }
    $target = Join-Path $InstallRoot ([string]$spec.target)
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        return (& $bad "target file missing: $target")
    }
    $evidence = Get-AsciiTokenEvidence -Path $target -Token ([string]$spec.header)
    if ($null -eq $evidence) {
        return (& $bad "target file $target lacks expected header anchor $($spec.header) for OpenCode $Version")
    }
    if ($evidence.count -ne 1) {
        return (& $bad "target file $target has $($evidence.count) copies of $($spec.header); expected exactly one for OpenCode $Version")
    }
    $window = $evidence.window
    if ([string]$fingerprint.Value -ne 'opencode-go-session-v1') {
        return (& $bad "unknown OpenCode Go session fingerprint '$($fingerprint.Value)' for version $Version")
    }
    $providerAndSession = [regex]::Escape([string]$spec.providerGuard) +
        '\s*\?\s*\{[\s\S]{0,2048}' + [regex]::Escape('"' + [string]$spec.header + '"') +
        '\s*:\s*[$A-Za-z_][$A-Za-z0-9_]*' + [regex]::Escape([string]$spec.sessionSource)
    if ($window -notmatch $providerAndSession) {
        return (& $bad "target file $target changed near expected provider/session anchor for OpenCode $Version")
    }
    if ($window -notmatch [regex]::Escape([string]$spec.preservedHeaders)) {
        return (& $bad "target file $target no longer preserves model headers near the OpenCode Go session header")
    }
    [pscustomobject]@{ ok = $true; reason = $null; target = $target; fingerprint = [string]$spec.fingerprints."$Version" }
}

# Generation 2.x host contract, proved from the disposable executable itself:
# the native v2 session namespace, the queue delivery vocabulary, the native
# scheduler promotion literals, the V2 event vocabulary the ambient projection
# listens to, and the TUI plugin host that loads the plugin file.
function Test-PluginContract {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)]$Manifest
    )
    $bad = { param($r) [pscustomobject]@{ ok = $false; reason = $r } }
    $exe = Join-Path $InstallRoot 'bin/opencode.exe'
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { return (& $bad "executable missing: $exe") }

    $anchors = @(
        @{ token = 'v2.session.prompt'; want = 1; why = 'native prompt admission route' },
        @{ token = 'v2.session.interrupt'; want = 1; why = 'native interruption route' },
        @{ token = 'v2.session.switchAgent'; want = 1; why = 'native agent session setting' },
        @{ token = 'v2.session.switchModel'; want = 1; why = 'native model session setting' },
        @{ token = 'v2.session.messages'; want = 2; why = 'native projected messages route' }
    )
    foreach ($a in $anchors) {
        $evidence = Get-AsciiTokenEvidence -Path $exe -Token $a.token -Radius 512
        if ($null -eq $evidence) { return (& $bad "host lacks $($a.why) anchor ($($a.token))") }
        if ($evidence.count -ne $a.want) {
            return (& $bad "host has $($evidence.count) copies of $($a.token), expected $($a.want)")
        }
    }

    $delivery = Get-AsciiTokenEvidence -Path $exe -Token 'promoteNextQueued' -Radius 512
    if ($null -eq $delivery) { return (& $bad 'host lacks the native scheduler promoteNextQueued seam') }
    $steer = Get-AsciiTokenEvidence -Path $exe -Token 'promoteSteers' -Radius 512
    if ($null -eq $steer) { return (& $bad 'host lacks the native promoteSteers seam') }

    $events = @('session.next.step.started', 'session.next.prompted', 'permission.asked', 'question.asked')
    foreach ($name in $events) {
        $hit = Get-AsciiTokenEvidence -Path $exe -Token $name -Radius 256
        if ($null -eq $hit) { return (& $bad "host lacks required event vocabulary: $name") }
    }

    $go = Test-OpenCodeGoSessionContract -InstallRoot $InstallRoot -Version $Version -Manifest $Manifest
    if (-not $go.ok) { return $go }

    [pscustomobject]@{ ok = $true; reason = $null; sessionTarget = $go.target; sessionFingerprint = $go.fingerprint }
}
