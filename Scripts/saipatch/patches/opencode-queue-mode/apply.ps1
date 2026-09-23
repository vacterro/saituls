param([string]$InstallRoot, [string]$Version, [switch]$Plan, [string]$Stage)

# apply: stage the patch package. -Plan prints the JSON the transaction engine
# backs up and verifies; -Stage writes staged copies for hash comparison. The
# commit itself is a byte-image copy the engine performs after verification.
#
# Targets:
#   1..N. every runtime module in manifest.files (entry plugin + the relative
#      ESM modules it imports), created under <configDir>/opencode/tui-modules
#      (never under plugins/: the SERVER plugin loader scans plugins/ and
#      rejects TUI modules). ESM resolves the relative imports next to the
#      installed entry, so all of them must be co-installed;
#   next: <configDir>/opencode/tui.json, MERGED: every existing key and plugin
#      entry is preserved and exactly one file:// entry for the SAIPATCH
#      entry module is appended. The engine snapshots the original bytes and
#      Restore puts them back byte for byte (or deletes the file when it did
#      not exist);
#   last: the seven completion-sound WAVs, created under
#      <configDir>/opencode/tui-modules/sounds. Same new-file contract as the
#      plugin modules: absent IS the prior state, Restore deletes them;
#   T-144: on the recorded 1.18.29 build ONLY (exact sha256, probed anchor),
#      bin/opencode.exe gains the guarded seam-B dispatch call so native
#      session.next.* transcript events reach the visible store. The target is
#      planned only when host-patch.json guards hold for the CURRENT bytes
#      (Test-HostPatchApplicable); the builder refuses everything else, so any
#      drift, unknown build or double-patch attempt fails Apply closed.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
. (Join-Path $here 'common.ps1')
# The SAIPATCH root (patch dir -> patches/ -> saipatch/) owns lib/state.ps1:
# Get-PatchHash is needed here to hash the current host binary for the probe.
. (Join-Path (Split-Path -Parent (Split-Path -Parent $here)) 'lib\state.ps1')
. (Join-Path $here 'host_patch.ps1')
$hostExe = Join-Path $InstallRoot 'bin/opencode.exe'
$hostPatchApplicable = Test-HostPatchApplicable -PatchDir $here -ExePath $hostExe
$hostPatchBuild = $null
if ($hostPatchApplicable) {
    $hostPatchBuild = Resolve-HostPatchBuild -Descriptor (Get-HostPatchDescriptor -PatchDir $here) -ExePath $hostExe
}
# T-144 fail-closed guard: a baseline build whose anchor probe failed is either
# corrupted or unreadable -- silently dropping the seam target would degrade to
# the proven-broken plugin-only 2.2.0 configuration. (An ALREADY-patched image
# never triggers this: its hash equals a recorded patched_sha256, not a baseline.)
if ((Test-Path -LiteralPath $hostExe -PathType Leaf) -and -not $hostPatchApplicable) {
    $descriptor = Get-HostPatchDescriptor -PatchDir $here
    $liveHash = (Get-FileHash -LiteralPath $hostExe -Algorithm SHA256).Hash.ToLowerInvariant()
    $knownBaselines = @($descriptor.builds.PSObject.Properties | ForEach-Object { $_.Value.target.sha256.ToLowerInvariant() })
    $knownPatched = @($descriptor.builds.PSObject.Properties | ForEach-Object { $_.Value.patched_sha256.ToLowerInvariant() })
    if ($knownBaselines -contains $liveHash) {
        Write-Error "REFUSE: host executable is a recorded baseline but the seam probe failed (anchor unreadable?) -- refusing plugin-only degradation"
        exit 2
    }
    if ($knownPatched -notcontains $liveHash) {
        Write-Error "UNSUPPORTED_BUILD: host executable sha256 $liveHash is neither a recorded baseline nor a recorded patched image -- zero mutation"
        exit 2
    }
}

$manifest = Get-Content -Raw -LiteralPath (Join-Path $here 'manifest.json') | ConvertFrom-Json
$pluginDir = Get-QueuePluginDir
$tuiJsonPath = Get-QueueTuiJsonPath
$soundDir = Get-QueueSoundDir
$soundNames = @('PICKUP01.wav','PICKUP02.wav','PICKUP03.wav','PICKUP04.wav','PICKUP05.wav','PICKUP06.wav','PICKUP07.wav')
# Canonical WAV sources live in the SAIPATCH root, NOT inside the patch
# package: the assets are shared patch-runtime inputs, 266KB total. Derive
# structurally (patch dir -> patches -> saipatch), never a hardcoded path.
$soundRoot = Split-Path -Parent (Split-Path -Parent $here)

function Get-OptionalFileHash([string]$Path) {
    if ([IO.File]::Exists($Path)) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }
    return $null
}

if ($Plan) {
    $merged = Join-QueueTuiJson
    $targets = @()
    # T-144 seam-B host target, FIRST so the transaction snapshot order reads
    # binary then config. original_sha256 is the CURRENT bytes (the exact
    # baseline the descriptor pins); Restore puts those recorded bytes back.
    if ($hostPatchApplicable) {
        $targets += [pscustomobject]@{
            file = 'bin__opencode.exe'
            path = $hostExe
            original_sha256 = (Get-FileHash -LiteralPath $hostExe -Algorithm SHA256).Hash
            patched_sha256 = $hostPatchBuild.patched_sha256
            source = $null                  # built at stage time, never copied
        }
    }
    # Runtime modules: the entry plugin plus every module it imports by relative
    # specifier (ESM resolves them next to the installed entry file). All are
    # created by us: absent IS the prior state, Restore deletes them.
    foreach ($rf in $manifest.files) {
        $src = Join-Path $here $rf
        $targets += [pscustomobject]@{
            file = $rf
            path = Join-Path $pluginDir $rf
            original_sha256 = $null
            patched_sha256 = (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash
            source = $src
        }
    }
    $tuiTarget = [pscustomobject]@{
        file = 'tui.json'
        path = $tuiJsonPath
        original_sha256 = Get-OptionalFileHash $tuiJsonPath
        patched_sha256 = $null             # filled from the merged bytes below
        source = $null                     # merged content is staged, not copied
    }
    $targets += $tuiTarget
    # Completion-sound runtime assets. Same created-by-us contract.
    foreach ($name in $soundNames) {
        $src = Join-Path $soundRoot $name
        $targets += [pscustomobject]@{
            file = "sounds\$name"
            path = Join-Path $soundDir $name
            original_sha256 = $null
            patched_sha256 = (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash
            source = $src
        }
    }
    $mergedBytes = [Text.Encoding]::UTF8.GetBytes($merged)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $tuiTarget.patched_sha256 = [BitConverter]::ToString($sha.ComputeHash($mergedBytes)).Replace('-', '') }
    finally { $sha.Dispose() }
    [pscustomobject]@{ targets = $targets } | ConvertTo-Json -Depth 4 -Compress
    exit 0
}

if ($Stage) {
    # T-144 seam-B host image: deterministic bytes from the exact recorded
    # source; any guard refusal exits nonzero and fails staging closed.
    if ($hostPatchApplicable) {
        Build-HostPatchImage -PatchDir $here -ExePath $hostExe -Destination (Join-Path $Stage 'bin__opencode.exe')
    }
    foreach ($rf in $manifest.files) {
        Copy-Item -LiteralPath (Join-Path $here $rf) -Destination (Join-Path $Stage $rf) -Force
    }
    [void][IO.Directory]::CreateDirectory((Join-Path $Stage 'sounds'))
    foreach ($name in $soundNames) {
        Copy-Item -LiteralPath (Join-Path $soundRoot $name) -Destination (Join-Path $Stage "sounds\$name") -Force
    }
    # Recompute the merge at stage time from the CURRENT tui.json; the engine
    # verifies the staged bytes against the plan hash and the optimistic
    # recheck at publication refuses any concurrent config change.
    [IO.File]::WriteAllText((Join-Path $Stage 'tui.json'), (Join-QueueTuiJson), (New-Object Text.UTF8Encoding($false)))
    exit 0
}

Write-Host 'apply needs -Plan or -Stage'
exit 2
