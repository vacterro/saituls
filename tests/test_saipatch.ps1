param(
    # SAIPATCH tree under test. Point it at an older checkout to reuse this
    # same harness as a red control.
    [string]$Saipatch = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Scripts\saipatch'),
    # Real backend positive control (fixture provider + disposable server).
    # Opt-in because it downloads the provider package and runs a local server;
    # CI stays hermetic without it.
    [switch]$BackendSmoke
)

$ErrorActionPreference = 'Stop'

# Deterministic patcher contract for SAIPATCH + opencode-queue-mode 2.x.
#
# FAKE trees only: a disposable OpenCode root (hard-linked to the kitchen copy
# of the recorded 1.18.29 executable) and a fabricated HOME, so %LOCALAPPDATA%
# state and the TUI plugin directory never touch the developer's real OpenCode
# or real config. The LIVE installation is never patched by these tests.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_saipatch.ps1
# Exit: 0 = all PASS, 1 = failures.

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$sandbox = Join-Path $env:TEMP ('saituls_saipatch_' + [Guid]::NewGuid().ToString('N'))
$patchSrc = Join-Path $Saipatch 'patches\opencode-queue-mode'
$fails = 0
# All recorded baseline builds (per-build descriptor): the full disposable tier
# runs for whichever recorded baseline is resolved on PATH.
$baselineHashes = @(
    '88d2fa691b2d9e32fde6d1039382a850ddf96fe49cd41683c6375fe1dc8ec2a5',
    'c1bdbb18767048e1853af4238311b3f7e16ff2f91b68fb4c7ced3c5175347eea'
)
# Byte-stability snapshot for the live-installation guard (check 33).
$liveExePath = 'c:\nodejs\node_modules\opencode-ai\bin\opencode.exe'
$liveHashAtStart = if (Test-Path -LiteralPath $liveExePath) { (Get-FileHash -LiteralPath $liveExePath -Algorithm SHA256).Hash.ToLower() } else { $null }

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

# Count exact occurrences of a byte needle inside a large file. The needle is
# mapped 1:1 to a compiled regex over Latin1 (GetEncoding(28591), the byte<->char
# identity codec available on .NET Framework): scanning happens inside the regex
# engine instead of an interpreted PowerShell byte loop (a 180 MB image would
# take minutes the naive way, seconds this way).
function Get-ByteNeedleCount([string]$Path, [byte[]]$Needle) {
    $enc = [Text.Encoding]::GetEncoding(28591)
    $regex = New-Object Text.RegularExpressions.Regex (
        [Text.RegularExpressions.Regex]::Escape($enc.GetString($Needle)))
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $buffer = New-Object byte[] (4 * 1024 * 1024)
        $tail = New-Object byte[] 0
        $count = 0
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $search = New-Object byte[] ($tail.Length + $read)
            [Array]::Copy($tail, 0, $search, 0, $tail.Length)
            [Array]::Copy($buffer, 0, $search, $tail.Length, $read)
            $count += $regex.Matches($enc.GetString($search)).Count
            $keep = [Math]::Min([Math]::Max(0, $Needle.Length - 1), $search.Length)
            $tail = New-Object byte[] $keep
            [Array]::Copy($search, $search.Length - $keep, $tail, 0, $keep)
        }
        return $count
    }
    finally { $stream.Dispose() }
}

function Get-PathOpenCodeExe {
    # The production patcher resolves OpenCode exclusively through PATH
    # (lib\resolve.ps1). The harness mirrors that exactly: with the CI stub
    # layer present the stub shim resolves; with it deleted nothing resolves
    # and the harness dies loudly. No silent fallback to other locations.
    $cmd = Get-Command opencode.cmd -ErrorAction SilentlyContinue
    $shim = Get-Command opencode -ErrorAction SilentlyContinue
    $shimPath = if ($cmd) { $cmd.Source } elseif ($shim) { $shim.Source } else { $null }
    if (-not $shimPath) { return $null }
    $shimText = if ([IO.Path]::GetExtension($shimPath) -eq '.cmd') { Get-Content -Raw -LiteralPath $shimPath } else { '' }
    if ($shimText -match '"(?<exe>[^"]*opencode\.exe)"') {
        $raw = $Matches.exe
        if ($raw -match '(?i)%dp0%') {
            $tail = $raw -replace '(?i)^.*%dp0%[\\/]*', ''
            $tail = $tail -replace '[\\/]', [IO.Path]::DirectorySeparatorChar
            $exe = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $shimPath) $tail))
            if (Test-Path -LiteralPath $exe) { return $exe }
        }
        elseif (Test-Path -LiteralPath $raw) { return $raw }
    }
    $candidate = Join-Path (Split-Path -Parent $shimPath) 'node_modules\opencode-ai\bin\opencode.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}

function New-FakeHome([string]$dir, [string]$version, [string]$exeSource, [switch]$PlainExe) {
    # A HOME with its own bin shim pointing at a fabricated package root. With
    # -PlainExe the root carries a tiny text stand-in (unsupported-build path);
    # otherwise the root hard-links the disposable 1.18.29 executable so the
    # build fingerprint, host anchors and session contract are the REAL bytes.
    $c = Join-Path $dir 'home'
    New-Item -ItemType Directory -Path (Join-Path $c 'bin') -Force | Out-Null
    $root = Join-Path $c 'root\node_modules\opencode-ai'
    New-Item -ItemType Directory -Path (Join-Path $root 'bin') -Force | Out-Null
    @{ name = 'opencode-ai'; version = $version; bin = @{ opencode = './bin/opencode.exe' } } |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $root 'package.json') -Encoding UTF8
    $target = Join-Path $root 'bin\opencode.exe'
    if ($PlainExe) {
        Set-Content -LiteralPath $target -Value 'not a real opencode build' -Encoding Ascii
    }
    else {
        if (-not (Test-Path -LiteralPath $exeSource)) { throw "disposable exe source missing: $exeSource" }
        try {
            New-Item -ItemType HardLink -Path $target -Target $exeSource | Out-Null
        }
        catch [IO.IOException], [System.ComponentModel.Win32Exception] {
            # Cross-volume sources cannot hard-link; a copy keeps the same bytes.
            Copy-Item -LiteralPath $exeSource -Destination $target -Force
        }
    }
    @"
@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0
"%dp0%..\root\node_modules\opencode-ai\bin\opencode.exe"   %*
"@ | Set-Content -LiteralPath (Join-Path $c 'bin\opencode.cmd') -Encoding Ascii
    return $c
}

function Invoke-Patcher([string]$homeDir, [string]$patchTree, [string[]]$argv) {
    # The patcher resolves OpenCode through PATH. The fake home carries its own
    # shim pointing at a fake root, prepended for the child only, so state.json
    # and the plugin tree stay inside the fake home.
    $saveProfile = $env:USERPROFILE
    $saveLocal = $env:LOCALAPPDATA
    $savePath = $env:PATH
    try {
        $env:USERPROFILE = $homeDir
        $env:LOCALAPPDATA = Join-Path $homeDir 'local'
        $env:PATH = (Join-Path $homeDir 'bin') + [IO.Path]::PathSeparator + $env:PATH
        $out = & powershell -NoLogo -NoProfile -ExecutionPolicy Bypass `
            -File (Join-Path $patchTree 'patcher.ps1') @argv 2>&1
        return @{ out = (($out | ForEach-Object { "$_" }) -join "`n"); rc = $LASTEXITCODE }
    }
    finally {
        $env:USERPROFILE = $saveProfile
        $env:LOCALAPPDATA = $saveLocal
        $env:PATH = $savePath
    }
}

function Get-HomeSnapshot([string]$homeDir) {
    # Byte-identity oracle over everything under the fake home EXCEPT the
    # engine's own state/backups (LOCALAPPDATA inside the home).
    $configTree = Join-Path $homeDir '.config'
    if (-not (Test-Path -LiteralPath $configTree)) { return '' }
    $rows = @()
    foreach ($file in (Get-ChildItem -LiteralPath $configTree -Recurse -File)) {
        $rows += "$($file.FullName)|$((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash)"
    }
    return ($rows | Sort-Object) -join "`n"
}

try {
    New-Item -ItemType Directory -Path $sandbox -Force | Out-Null

    # Tier selection: the harness resolves OpenCode through PATH exactly like
    # the production patcher. When the resolved executable IS the recorded
    # baseline build the full disposable matrix runs; otherwise (CI stub shim)
    # the harness runs tier-A only and SKIPs the build-anchored matrix
    # explicitly. No fake green: every skip names its reason.
    $pathExe = Get-PathOpenCodeExe
    Check '0. OpenCode resolves on PATH (stub layer or real install)' ($null -ne $pathExe) "$pathExe"
    if ($null -eq $pathExe) { throw 'opencode shim not found on PATH: the SAIPATCH harness resolves OpenCode like the production patcher and cannot run without it' }
    $pathHash = (Get-FileHash -LiteralPath $pathExe -Algorithm SHA256).Hash.ToLower()
    $baselineHash = $pathHash
    $fullTier = $baselineHashes -contains $pathHash
    $descriptorPre = Get-Content -Raw -LiteralPath (Join-Path $patchSrc 'host-patch.json') | ConvertFrom-Json
    $descriptorBuild = $descriptorPre.builds.PSObject.Properties | Where-Object { $_.Value.target.sha256.ToLower() -eq $pathHash } | Select-Object -First 1
    $expectedPatchedHash = if ($descriptorBuild) { $descriptorBuild.Value.patched_sha256 } else { $null }
    Write-Host "INFO  resolved build tier: $(if ($fullTier) { "FULL ($($descriptorBuild.Name) baseline on PATH)" } else { 'A (stub/unknown build: engine+unit tier only)' })  $pathExe"
    if ($BackendSmoke -and -not $fullTier) { throw '-BackendSmoke requires the recorded baseline build on PATH; refused on a stub tier' }

    # --- Unit contract of the 2.x plugin itself ------------------------------
    $pluginFile = Join-Path $patchSrc 'saipatch-native-queue-2x.js'
    Check '1. single-file 2.x plugin exists in the package' (Test-Path -LiteralPath $pluginFile) $pluginFile
    $unit = & node --test (Join-Path $patchSrc 'saipatch-native-queue-2x.test.mjs') (Join-Path $patchSrc 'saipatch-completion-sound.test.mjs') 2>&1
    $unitOut = (($unit | ForEach-Object { "$_" }) -join "`n")
    $unitRc = $LASTEXITCODE
    Check '2. plugin unit tests green (projection, queue semantics, ambient, wiring, completion sound)' ($unitRc -eq 0) "exit=$unitRc"

    $mf = Get-Content -Raw -LiteralPath (Join-Path $patchSrc 'manifest.json') | ConvertFrom-Json
    Check '3. manifest is a truthful major generation 2.x' ($mf.version -match '^2\.' -and $mf.generation -eq 2) $mf.version
    Check '4. manifest supports exactly the recorded baseline builds' (
        $mf.supports.opencode -contains '1.18.29' -and $mf.supports.opencode -contains '1.18.30' -and
        $mf.supports.builds.'1.18.29' -contains '88d2fa691b2d9e32fde6d1039382a850ddf96fe49cd41683c6375fe1dc8ec2a5' -and
        $mf.supports.builds.'1.18.30' -contains 'c1bdbb18767048e1853af4238311b3f7e16ff2f91b68fb4c7ced3c5175347eea') ($mf.supports.opencode -join ',')
    $package = Get-Content -Raw -LiteralPath (Join-Path $patchSrc 'package.json') | ConvertFrom-Json
    Check '5. manifest and package versions agree' ($mf.version -eq $package.version) $package.version
    Check '6. manifest carries no secrets' (
        (Get-Content -Raw -LiteralPath (Join-Path $patchSrc 'manifest.json')) -notmatch '(?i)\b(api[_-]?key|token|secret|password)\b') ''
    # T-144 §8: transitive package-closure test — from manifest + entry module,
    # resolve every relative import/export and require source exists, Plan includes,
    # Stage contains. Also validate explicitly-declared non-ESM runtime helpers
    # and the seven sound assets. Fails on any missing runtime file.
    $entry = Join-Path $patchSrc 'saipatch-native-queue-2x.js'
    $imports = @()
    $text = Get-Content -Raw -LiteralPath $entry
    # Static relative imports/exports, including the multiline form:
    #   import { ... } from "./foo.js"
    #   export { ... } from "./bar.js"
    $static = [regex]::Matches($text, '(?:import\s+(?:[^"\n]*?\{[^}]*\}?|[^"\s,]+(?:\s*,\s*[^"\s,]+)*))\s*from\s+"\.\/([^"]+)"|export\s+\{[^}]*\}\s*from\s+"\.\/([^"]+)"')
    foreach ($m in $static) { $imports += @($m.Groups[1].Value, $m.Groups[2].Value) | Where-Object { $_ } }
    # Dynamic relative import with string literal: import("./foo.js")
    $dynamic = [regex]::Matches($text, 'import\("\.?\/([^")]+)"')
    foreach ($m in $dynamic) { $imports += $m.Groups[1].Value }
    $imports = @($imports | Sort-Object -Unique)
    $missing = @()
    foreach ($imp in $imports) {
        $src = Join-Path $patchSrc $imp
        if (-not (Test-Path -LiteralPath $src)) { $missing += "source $imp" }
        if ($mf.files -notcontains $imp) { $missing += "manifest.files $imp" }
    }
    # Explicit non-ESM runtime helpers
    $runtimeAssets = @('saipatch-attention-flash.ps1')
    if ($mf.PSObject.Properties['runtimeAssets']) { $runtimeAssets = @($mf.runtimeAssets) }
    foreach ($h in $runtimeAssets) {
        if (-not (Test-Path -LiteralPath (Join-Path $patchSrc $h))) { $missing += "helper source $h" }
        if ($mf.files -notcontains $h) { $missing += "manifest.files $h" }
    }
    # Seven sound assets (canonical names from apply.ps1)
    $soundNames = @('PICKUP01.wav','PICKUP02.wav','PICKUP03.wav','PICKUP04.wav','PICKUP05.wav','PICKUP06.wav','PICKUP07.wav')
    $soundRoot = Split-Path -Parent (Split-Path -Parent $patchSrc)
    foreach ($s in $soundNames) {
        if (-not (Test-Path -LiteralPath (Join-Path $soundRoot $s))) { $missing += "sound source $s" }
    }
    Check '8a. package-closure textual scan (best-effort; staged-import oracle 9e/9f is authoritative): discovered imports + helpers + sounds present in source/manifest' ($missing.Count -eq 0) ($missing -join '; ')
    $retiredFingerprints = @('index.mjs', 'queue.mjs', 'turn.mjs', 'plugin.test.mjs', 'queue.test.mjs')
    $leftover = @($retiredFingerprints | Where-Object { Test-Path -LiteralPath (Join-Path $patchSrc $_) })
    Check '7. retired 1.x custom-FIFO sources are gone from the installable package' ($leftover.Count -eq 0) ($leftover -join ',')
    $pluginText = Get-Content -Raw -LiteralPath $pluginFile
    Check '8. 2.x plugin has no legacy 1.x FIFO/busy scheduler state vocabulary' (
        $pluginText -cnotmatch 'PENDING|DISPATCHING|ACCEPTED') ''
    Check '9. 2.x plugin uses the native delivery vocabulary' (
        $pluginText -match 'delivery:\s*steer\s*:\s*"queue"' -or $pluginText -match '"queue"' ) ''
    # T-144: the binary seam descriptor must carry the exact recorded build,
    # the guarded hook call and the deterministic patched-image hash.
    $hostPatch = Get-Content -Raw -LiteralPath (Join-Path $patchSrc 'host-patch.json') | ConvertFrom-Json
    $hostBuildsOk = $true
    foreach ($b in $hostPatch.builds.PSObject.Properties) {
        if ($b.Value.target.sha256 -notmatch '^[0-9a-f]{64}$' -or
            $b.Value.patched_sha256 -notmatch '^[0-9a-f]{64}$') { $hostBuildsOk = $false }
    }
    Check '9b. host-patch descriptor pins every recorded build and patched image' (
        $hostBuildsOk -and
        @($hostPatch.builds.PSObject.Properties).Count -eq 2 -and
        $hostPatch.hook -eq '__SPB' -and
        (Test-Path -LiteralPath (Join-Path $patchSrc $hostPatch.anchor.text_file)) -and
        (Test-Path -LiteralPath (Join-Path $patchSrc $hostPatch.replacement.text_file))) (@($hostPatch.builds.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value.patched_sha256)" }) -join ' ')
    # T-144 P0: the seam replacement must be FAIL-OPEN — the hook-absent branch
    # must dispatch the ORIGINAL H with the exact original context, and the
    # old optional-chain (event-dropping) form must be gone from the window.
    $repBytes = [IO.File]::ReadAllBytes((Join-Path $patchSrc $hostPatch.replacement.text_file))
    $repText = [Text.Encoding]::ASCII.GetString($repBytes)
    Check '9c. seam replacement is fail-open with exact original context (hook-absent branch present)' (
        $repText -match [regex]::Escape('K?K(w,e=>H(e,w)):H(w.payload,{directory:w.directory,workspace:w.workspace})')) ''
    Check '9d. old event-dropping optional-chain seam absent from replacement window' (
        $repText -notmatch [regex]::Escape('globalThis.__SPB?.(w,e=>H(e,w))')) ''
    # T-144 §9: staged import test — stage the package to a disposable dir,
    # then import the entry module FROM that dir (no repository module
    # resolution escape). Require Node ESM import succeeds. RED control:
    # remove saipatch-attention.js from copied stage -> import fails.
    $stageDir = Join-Path $sandbox 'stage-import-test'
    if (Test-Path -LiteralPath $stageDir) { Remove-Item -LiteralPath $stageDir -Recurse -Force }
    New-Item -ItemType Directory -Path $stageDir | Out-Null
    # Module/package closure only: the staged ESM import oracle needs the
    # runtime modules, not the sound assets (sounds are resolved lazily at
    # play time; module import succeeds without them).
    foreach ($rf in $mf.files) {
        Copy-Item -LiteralPath (Join-Path $patchSrc $rf) -Destination (Join-Path $stageDir $rf) -Force
    }
    $stageEntry = Join-Path $stageDir 'saipatch-native-queue-2x.js'
    $stageUri = 'file:///' + ($stageEntry -replace '\\', '/')
    & node -e "import(process.argv[1]).then(() => { console.log('IMPORT_OK') }).catch(e => { console.error(e.message); process.exit(1) })" $stageUri
    Check '9e. staged import: entry module imports from stage directory (only packaged files)' ($LASTEXITCODE -eq 0) ''
    # RED control: remove attention module
    $brokenStage = Join-Path $sandbox 'stage-import-broken'
    if (Test-Path -LiteralPath $brokenStage) { Remove-Item -LiteralPath $brokenStage -Recurse -Force }
    New-Item -ItemType Directory -Path $brokenStage | Out-Null
    foreach ($rf in $mf.files) { if ($rf -ne 'saipatch-attention.js') { Copy-Item -LiteralPath (Join-Path $patchSrc $rf) -Destination (Join-Path $brokenStage $rf) -Force } }
    $brokenEntry = Join-Path $brokenStage 'saipatch-native-queue-2x.js'
    $brokenUri = 'file:///' + ($brokenEntry -replace '\\', '/')
    $brokenRc = & node -e "import(process.argv[1]).then(() => { console.log('IMPORT_OK') }).catch(e => { console.error(e.message); process.exit(1) })" $brokenUri
    $brokenOut = "$brokenRc"
    Check '9f. staged import RED control: removing saipatch-attention.js breaks import' ($LASTEXITCODE -ne 0) $brokenOut

    # --- Disposable matrix against the recorded build ------------------------
    $fakeHome = New-FakeHome $sandbox $descriptorBuild.Name $pathExe
    $shimDir = Join-Path $fakeHome 'bin'
    $fakeRootExe = Join-Path $fakeHome 'root\node_modules\opencode-ai\bin\opencode.exe'

    if (-not $fullTier) {
        foreach ($name in @(
                '10. Detect reports AVAILABLE on the disposable recorded build',
                '11. Apply installs transactionally',
                '12. installed plugin file matches the staged package bytes',
                '13. every pre-existing config file byte-identical after Apply',
                '14. tui.json/opencode.json absent: zero config mutation',
                '15. Verify reports INSTALLED',
                '16. cold launch of the patched disposable host succeeds',
                '19. Restore returns the home to AVAILABLE',
                '20. config tree byte-identical after Restore',
                '21. plugin file removed by Restore',
                '22. disposable host executable untouched (hash stable)',
                '23. cold launch of the restored host succeeds',
                '24. Reapply after Restore installs again',
                '25. Verify after Reapply reports INSTALLED',
                '26. second root applies cleanly',
                '27. tampered installed plugin fails closed as SOURCE_DRIFTED',
                '28. Restore refuses drifted target (exit nonzero)',
                '29. Verify after refused restore still SOURCE_DRIFTED (no silent heal)')) {
            Write-Host "SKIP  $name (reason: PATH-resolved build is not the recorded baseline; tier-A requires no disposable 180MB artifact)"
        }
    }
    else {

    $detect = Invoke-Patcher $fakeHome $Saipatch @('Detect', '-Json')
    $detectJson = $detect.out | ConvertFrom-Json
    Check '10. Detect reports AVAILABLE on the disposable recorded build' (
        $detect.rc -eq 0 -and $detectJson.state -eq 'AVAILABLE') $detectJson.state

    $before = Get-HomeSnapshot $fakeHome
    $apply = Invoke-Patcher $fakeHome $Saipatch @('Apply', '-Json')
    $applyJson = $apply.out | ConvertFrom-Json
    Check '11. Apply installs transactionally' ($apply.rc -eq 0 -and $applyJson.state -eq 'INSTALLED') $applyJson.state

    $pluginInstalled = Join-Path $fakeHome '.config\opencode\tui-modules\saipatch-native-queue-2x.js'
    $tuiJsonInstalled = Join-Path $fakeHome '.config\opencode\tui.json'
    $pluginUri = 'file:///' + ($pluginInstalled -replace '\\', '/')
    $installedHash = if (Test-Path -LiteralPath $pluginInstalled) { (Get-FileHash -LiteralPath $pluginInstalled -Algorithm SHA256).Hash } else { $null }
    Check '12. installed plugin file matches the staged package bytes' (
        $installedHash -eq (Get-FileHash -LiteralPath $pluginFile -Algorithm SHA256).Hash) $installedHash

    # T-162: the entry plugin imports relative ESM modules; ESM resolves them
    # next to the installed entry, so every runtime module must be co-staged.
    $runtimeFiles = @($mf.files)
    $modulesOk = $true
    foreach ($rf in $runtimeFiles) {
        $installedModule = Join-Path (Join-Path $fakeHome '.config\opencode\tui-modules') $rf
        $srcModule = Join-Path $patchSrc $rf
        if (-not (Test-Path -LiteralPath $installedModule) -or
            (Get-FileHash -LiteralPath $installedModule -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $srcModule -Algorithm SHA256).Hash) {
            $modulesOk = $false
        }
    }
    Check '12b. every runtime module (entry + relative ESM imports) staged byte-identical' $modulesOk ($runtimeFiles -join ',')

    Check '13. every pre-existing config file byte-identical after Apply' (
        -not $before -or (
            ($before -split "`n") | Where-Object { $_ } |
                ForEach-Object { (Get-HomeSnapshot $fakeHome) -split "`n" -contains $_ } |
                Where-Object { -not $_ }).Count -eq 0) ''
    $configFiles = @(Get-ChildItem -LiteralPath (Join-Path $fakeHome '.config\opencode') -File -ErrorAction SilentlyContinue)
    $alien = @($configFiles | Where-Object { $_.Name -ne 'tui.json' })
    Check '14. opencode.json absent and no config file mutated except tui.json' ($alien.Count -eq 0) ($alien.name -join ',')
    $tuiJson = $null
    try { $tuiJson = Get-Content -Raw -LiteralPath $tuiJsonInstalled | ConvertFrom-Json } catch {}
    Check '14b. tui.json carries exactly the SAIPATCH tui-module entry' (
        $null -ne $tuiJson -and @($tuiJson.plugin).Count -eq 1 -and @($tuiJson.plugin)[0] -eq $pluginUri) ($tuiJson.plugin -join ',')

    # T-144 seam-B host patch: the installer must ALSO patch the disposable
    # executable binary (fail-closed descriptor) and Restore must put the exact
    # original bytes back.
    $hostPatchDescriptor = Get-Content -Raw -LiteralPath (Join-Path $patchSrc 'host-patch.json') | ConvertFrom-Json
    $fakeRootExeHash = (Get-FileHash -LiteralPath $fakeRootExe -Algorithm SHA256).Hash
    Check '11h. Apply patched the disposable host binary to the descriptor image' (
        $fakeRootExeHash -eq $expectedPatchedHash) $fakeRootExeHash
    $hostPatchCallSite = $null
    $oldSeamCount = $null
    try {
        $needle = [Text.Encoding]::GetEncoding(28591).GetBytes('let K=globalThis.__SPB;K?K(w,e=>H(e,w)):H(w.payload,{directory:w.directory,workspace:w.workspace})')
        $hostPatchCallSite = Get-ByteNeedleCount -Path $fakeRootExe -Needle $needle
        $oldNeedle = [Text.Encoding]::GetEncoding(28591).GetBytes('globalThis.__SPB' + [char]63 + '.(w,e=>H(e,w))')
        $oldSeamCount = Get-ByteNeedleCount -Path $fakeRootExe -Needle $oldNeedle
    } catch { $hostPatchCallSite = -1 }
    Check '11i. fail-open dispatch site occurs exactly once (byte-level proof); old optional-chain gone' ($hostPatchCallSite -eq 1 -and $oldSeamCount -eq 0) "failOpen=$hostPatchCallSite old=$oldSeamCount"

    # T-162: the seven completion-sound WAVs are staged as patch-owned runtime
    # assets under tui-modules\sounds; bytes match the package sources.
    $soundNames = @('PICKUP01.wav','PICKUP02.wav','PICKUP03.wav','PICKUP04.wav','PICKUP05.wav','PICKUP06.wav','PICKUP07.wav')
    $soundDirInstalled = Join-Path $fakeHome '.config\opencode\tui-modules\sounds'
    $soundOk = $true
    foreach ($name in $soundNames) {
        $installedWav = Join-Path $soundDirInstalled $name
        $srcWav = Join-Path $Saipatch $name
        if (-not (Test-Path -LiteralPath $installedWav) -or
            (Get-FileHash -LiteralPath $installedWav -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $srcWav -Algorithm SHA256).Hash) {
            $soundOk = $false
        }
    }
    Check '14c. Apply stages all seven WAVs byte-identical into tui-modules\sounds' $soundOk ''
    Check '14d. staged WAVs are valid RIFF/WAVE' (
        (@($soundNames | ForEach-Object {
            $bytes = [IO.File]::ReadAllBytes((Join-Path $soundDirInstalled $_))
            ([Text.Encoding]::ASCII.GetString($bytes, 0, 4) -eq 'RIFF') -and ([Text.Encoding]::ASCII.GetString($bytes, 8, 4) -eq 'WAVE')
        }) -notcontains $false)) ''

    $verify = Invoke-Patcher $fakeHome $Saipatch @('Verify', '-Json')
    $verifyJson = $verify.out | ConvertFrom-Json
    Check '15. Verify reports INSTALLED' ($verify.rc -eq 0 -and $verifyJson.state -eq 'INSTALLED') $verifyJson.state

    $cold = Start-Process -FilePath $fakeRootExe -ArgumentList '--version' -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $sandbox 'cold.txt')
    $coldOut = (Get-Content -Raw -LiteralPath (Join-Path $sandbox 'cold.txt') -ErrorAction SilentlyContinue)
    Check '16. cold launch of the patched disposable host succeeds' ($cold.ExitCode -eq 0 -and $coldOut -match $descriptorBuild.Name.Replace('.', '\.')) "exit=$($cold.ExitCode) out=$($coldOut.Trim())"

    if ($BackendSmoke) {
        $evidence = Join-Path $sandbox 'backend-evidence'
        $smoke = & python (Join-Path $repoRoot 'tests\saipatch_backend_smoke.py') --exe $fakeRootExe --evidence $evidence 2>&1
        $smokeOut = (($smoke | ForEach-Object { "$_" }) -join "`n")
        $smokeDetail = ($smokeOut -split "`n" | Select-Object -Last 3) -join ' | '
        Check '17. real backend positive control: cc1..cc5 strict native FIFO' ($LASTEXITCODE -eq 0) $smokeDetail
        $verdict = Join-Path $evidence 'fifo-verdict.json'
        Check '18. fifo-verdict.json recorded PASS' ((Test-Path -LiteralPath $verdict) -and (Get-Content -Raw -LiteralPath $verdict) -match '"PASS"') ''
    }
    else {
        Write-Host 'SKIP  17/18. backend positive control not requested (-BackendSmoke)'
    }

    $restore = Invoke-Patcher $fakeHome $Saipatch @('Restore', '-Json')
    $restoreJson = $restore.out | ConvertFrom-Json
    Check '19. Restore returns the home to AVAILABLE' ($restore.rc -eq 0 -and $restoreJson.state -in @('AVAILABLE', 'NOT_INSTALLED')) $restoreJson.state
    Check '20. config tree byte-identical after Restore' ((Get-HomeSnapshot $fakeHome) -eq $before) ''
    Check '21. plugin file removed and tui.json deleted by Restore' (
        -not (Test-Path -LiteralPath $pluginInstalled) -and -not (Test-Path -LiteralPath $tuiJsonInstalled)) ''
    $soundRestored = @($soundNames | Where-Object { Test-Path -LiteralPath (Join-Path $soundDirInstalled $_) })
    Check '21b. Restore removes the staged WAV assets exactly' ($soundRestored.Count -eq 0) ($soundRestored -join ',')
    $modulesRestored = @($runtimeFiles | Where-Object {
            Test-Path -LiteralPath (Join-Path (Join-Path $fakeHome '.config\opencode\tui-modules') $_) })
    Check '21c. Restore removes every staged runtime module exactly' ($modulesRestored.Count -eq 0) ($modulesRestored -join ',')
    $restoredExeHash = (Get-FileHash -LiteralPath $fakeRootExe -Algorithm SHA256).Hash
    Check '21h. Restore put back the exact original host binary bytes' ($restoredExeHash -eq $baselineHash) $restoredExeHash
    Check '22. disposable host executable untouched (hash stable)' (
        $restoredExeHash -eq $baselineHash) ''

    $cold2 = Start-Process -FilePath $fakeRootExe -ArgumentList '--version' -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $sandbox 'cold2.txt')
    Check '23. cold launch of the restored host succeeds' ($cold2.ExitCode -eq 0) "exit=$($cold2.ExitCode)"

    $reapply = Invoke-Patcher $fakeHome $Saipatch @('Apply', '-Json')
    $reapplyJson = $reapply.out | ConvertFrom-Json
    Check '24. Reapply after Restore installs again' ($reapply.rc -eq 0 -and $reapplyJson.state -eq 'INSTALLED') $reapplyJson.state
    $verify2 = Invoke-Patcher $fakeHome $Saipatch @('Verify', '-Json')
    $verify2Json = $verify2.out | ConvertFrom-Json
    Check '25. Verify after Reapply reports INSTALLED' ($verify2.rc -eq 0 -and $verify2Json.state -eq 'INSTALLED') $verify2Json.state

    # --- Fail-closed paths (full tier only: they need an installed generation)
    $driftHome2 = New-FakeHome (Join-Path $sandbox 'drift') $descriptorBuild.Name $pathExe
    $installedPlugin = Join-Path $driftHome2 '.config\opencode\tui-modules\saipatch-native-queue-2x.js'
    $apply2 = Invoke-Patcher $driftHome2 $Saipatch @('Apply', '-Json')
    Check '26. second root applies cleanly' ($apply2.rc -eq 0) ''
    # T-144 fail-closed gate: one flipped byte inside the patched call site must
    # be a state the patcher refuses — neither the recorded baseline nor the
    # descriptor image, so Detect reports UNSUPPORTED_BUILD and Apply refuses.
    $tamperHome = New-FakeHome (Join-Path $sandbox 'tamper') $descriptorBuild.Name $pathExe
    $tamperExe = Join-Path $tamperHome 'root\node_modules\opencode-ai\bin\opencode.exe'
    # The fake-home root hard-links the disposable master; break the link before
    # the in-place byte flip so only THIS sandbox copy is mutated, never the
    # kitchen copy (Remove-Item drops one link, the master stays intact).
    Remove-Item -LiteralPath $tamperExe -Force
    Copy-Item -LiteralPath $pathExe -Destination $tamperExe -Force
    $bytes = [IO.File]::ReadAllBytes($tamperExe)
    # Flip one byte INSIDE the recorded anchor (not the call site: the tampered
    # image is the BASELINE + 1 byte, which is neither baseline nor descriptor
    # image). Index 8 = the 'f' of the anchor's function name; __SPB (if any)
    # stays untouched so the flip cannot accidentally land in live state.
    $anchorText = $hostPatchDescriptor.anchor.text
    $enc = [Text.Encoding]::GetEncoding(28591)
    $text = $enc.GetString($bytes)
    $found = $text.IndexOf($anchorText)
    if ($found -lt 0) { throw 'tamper setup: anchor not found' }
    $bytes[$found + 8] = 95  # 'f' -> '_' inside 'function Lw'
    [IO.File]::WriteAllBytes($tamperExe, $bytes)
    $tamperedHash = (Get-FileHash -LiteralPath $tamperExe -Algorithm SHA256).Hash
    Check '26h. tampered host binary is neither baseline nor descriptor image' (
        $tamperedHash -ne $baselineHash -and $tamperedHash -ne $expectedPatchedHash) $tamperedHash
    $detectTamper = Invoke-Patcher $tamperHome $Saipatch @('Detect', '-Json')
    $detectTamperJson = $detectTamper.out | ConvertFrom-Json
    Check '26i. Detect refuses the tampered host binary as UNSUPPORTED_BUILD' (
        $detectTamperJson.state -eq 'UNSUPPORTED_BUILD') $detectTamperJson.state
    $applyTamper = Invoke-Patcher $tamperHome $Saipatch @('Apply')
    Check '26j. Apply refuses the tampered host binary (exit nonzero)' ($applyTamper.rc -ne 0) "rc=$($applyTamper.rc)"
    Set-Content -LiteralPath $installedPlugin -Value 'export const tampered = true;' -Encoding UTF8
    $verify3 = Invoke-Patcher $driftHome2 $Saipatch @('Verify', '-Json')
    $verify3Json = $verify3.out | ConvertFrom-Json
    Check '27. tampered installed plugin fails closed as SOURCE_DRIFTED' ($verify3Json.state -eq 'SOURCE_DRIFTED') $verify3Json.state
    $restore3 = Invoke-Patcher $driftHome2 $Saipatch @('Restore', '-Json')
    Check '28. Restore refuses drifted target (exit nonzero)' ($restore3.rc -ne 0) "rc=$($restore3.rc)"
    $verify4 = Invoke-Patcher $driftHome2 $Saipatch @('Verify', '-Json')
    $verify4Json = $verify4.out | ConvertFrom-Json
    Check '29. Verify after refused restore still SOURCE_DRIFTED (no silent heal)' ($verify4Json.state -eq 'SOURCE_DRIFTED') $verify4Json.state
    }

    # --- Fail-closed paths that need no supported build -----------------------
    $plainHome = New-FakeHome (Join-Path $sandbox 'plain') '1.18.29' $null -PlainExe
    $detect2 = Invoke-Patcher $plainHome $Saipatch @('Detect', '-Json')
    $detect2Json = $detect2.out | ConvertFrom-Json
    Check '30. unrecognized executable refuses as UNSUPPORTED_BUILD' (
        $detect2Json.state -eq 'UNSUPPORTED_BUILD' -and $detect2.rc -eq 0) $detect2Json.state
    $apply3 = Invoke-Patcher $plainHome $Saipatch @('Apply')
    Check '31. Apply refuses unrecognized build' ($apply3.rc -ne 0) "rc=$($apply3.rc)"

    $newerHome = New-FakeHome (Join-Path $sandbox 'newer') '9.0.0' $pathExe
    $detect3 = Invoke-Patcher $newerHome $Saipatch @('Detect', '-Json')
    $detect3Json = $detect3.out | ConvertFrom-Json
    Check '32. newer OpenCode version refuses as UNSUPPORTED_VERSION' ($detect3Json.state -eq 'UNSUPPORTED_VERSION') $detect3Json.state

    # Live installation must be untouched by everything above. The guard is
    # byte-STABILITY during this run plus zero plugin ownership -- whichever
    # OpenCode version the user has installed (T-144 corrective: the live host
    # may legitimately be a newer build than the recorded baseline; the patcher
    # refuses it, and the harness must not call that a failure).
    $live = 'c:\nodejs\node_modules\opencode-ai\bin\opencode.exe'
    if (Test-Path -LiteralPath $live) {
        $liveHash = (Get-FileHash -LiteralPath $live -Algorithm SHA256).Hash.ToLower()
        Check '33. live OpenCode executable hash unchanged during the run' (
            $liveHash -eq $liveHashAtStart) $liveHash
        Check '34. live OpenCode has no SAIPATCH plugin file' (
            -not (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.config\opencode\tui-modules\saipatch-native-queue-2x.js'))) ''
    }
}
catch {
    Write-Host "FAIL  harness error: $($_.Exception.Message)"; $script:fails++
    Write-Host $_.InvocationInfo.PositionMessage
}
finally {
    if (Test-Path -LiteralPath $sandbox) {
        try { Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue } catch {}
    }
}
if ($fails -gt 0) { Write-Host "FAILURES: $fails"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
