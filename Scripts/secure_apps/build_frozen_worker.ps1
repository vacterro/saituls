<#
.SYNOPSIS
    Freeze sa_fido_worker.py into the protected-runtime worker BUNDLE
    fido-worker\sa_fido_worker.exe (+ _internal\...).

.DESCRIPTION
    The elevated storage helper must launch the FIDO worker WITHOUT an
    interpreter: a python.exe on PATH, or a user-writable import tree, would
    put the choice of what an elevated process runs back in reach of a
    medium-integrity account.

    The worker is frozen with PyInstaller --onedir, NEVER --onefile. A onefile
    executable is a self-extracting stub: at every start it unpacks its whole
    executable dependency closure -- the Python DLL, every extension module,
    every bundled DLL -- into an ordinary %TEMP%\_MEIxxxxxx directory and then
    loads it FROM THERE. Running that elevated would mean an elevated process
    loading its code out of a directory the medium-integrity user owns, which
    is precisely the property the protected runtime exists to remove. The
    onedir tree is installed whole under the protected ACL instead, and
    nothing is extracted at runtime.

    The elevated Install/Repair transaction copies the COMPLETE bundle into
    %ProgramData%\SAITULS\secure-apps\privileged\fido-worker, applies the
    protected ACL to the whole tree, and binds every file in it -- relative
    path, size and SHA-256 -- into the recursive
    fido_worker_bundle_fingerprint that the runtime manifest and the pin
    carry. Any modification, addition or deletion inside the bundle
    invalidates installation and acceptance.

    This is a BUILD INPUT, run by a developer or CI, never at unlock time. The
    resulting tree is machine-specific and is NOT committed; the installer
    picks it up from beside the source (or from .\dist).

    Requires PyInstaller in the active interpreter:
        python -m pip install pyinstaller

.PARAMETER Python
    The interpreter to freeze with. Defaults to the one on PATH.

.PARAMETER OutDir
    Where the finished fido-worker\ tree is placed. Defaults to the
    secure_apps source directory, which is the first place the installer looks.
#>
[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$OutDir
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $here 'sa_fido_worker.py'
if (-not (Test-Path -LiteralPath $source)) {
    throw "worker source not found: $source"
}
if (-not $OutDir) { $OutDir = $here }
$work = Join-Path $env:TEMP ("saituls_frozen_worker_" + [guid]::NewGuid().ToString('N'))
$dist = Join-Path $work 'dist'
$build = Join-Path $work 'build'
New-Item -ItemType Directory -Path $work -Force | Out-Null

Write-Host "Freezing $source (--onedir) ..."
# --onedir: the whole dependency closure ships as files under the protected
#   ACL. Never --onefile, which would extract that closure into user TEMP
#   before an ELEVATED process loaded it.
# --collect-all fido2: the CTAP/webauthn tree the worker imports lazily.
& $Python -m PyInstaller --onedir --noconfirm --clean `
    --name sa_fido_worker `
    --distpath $dist --workpath $build --specpath $work `
    --collect-all fido2 `
    $source
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$builtDir = Join-Path $dist 'sa_fido_worker'
$builtExe = Join-Path $builtDir 'sa_fido_worker.exe'
if (-not (Test-Path -LiteralPath $builtExe)) {
    throw "PyInstaller did not produce sa_fido_worker.exe in $builtDir"
}

$target = Join-Path $OutDir 'fido-worker'
if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}
Copy-Item -LiteralPath $builtDir -Destination $target -Recurse -Force

# The same canonical rules sa_privtask.bundle_fingerprint and the elevated
# helper's Get-BundleFingerprint use: relative path from the bundle root, '/'
# separators, lowercased, printable ASCII, ordinal sort, one
# {"path","sha256","size"} object each inside
# {"entries":[...],"file_count":N,"schema":"..."}.
$rootFull = ([System.IO.Path]::GetFullPath($target)).TrimEnd('\')
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $map = @{}
    foreach ($item in (Get-ChildItem -LiteralPath $rootFull -Recurse -Force -File)) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "the built bundle contains a reparse point: $($item.FullName)"
        }
        $rel = $item.FullName.Substring($rootFull.Length + 1).Replace('\', '/').ToLowerInvariant()
        foreach ($ch in $rel.ToCharArray()) {
            if (([int]$ch) -lt 0x20 -or ([int]$ch) -gt 0x7E) {
                throw "the built bundle holds a non-ASCII path: $rel"
            }
        }
        $stream = [System.IO.File]::OpenRead($item.FullName)
        try {
            $digest = ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        } finally { $stream.Dispose() }
        $map[$rel] = [pscustomobject]@{ sha256 = $digest; size = [int64]$item.Length }
    }
    $keys = [string[]]@($map.Keys)
    [Array]::Sort($keys, [StringComparer]::Ordinal)
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('{"entries":[')
    for ($i = 0; $i -lt $keys.Length; $i++) {
        if ($i -gt 0) { [void]$sb.Append(',') }
        $entry = $map[$keys[$i]]
        [void]$sb.Append('{"path":"'); [void]$sb.Append($keys[$i])
        [void]$sb.Append('","sha256":"'); [void]$sb.Append($entry.sha256)
        [void]$sb.Append('","size":'); [void]$sb.Append([string]$entry.size)
        [void]$sb.Append('}')
    }
    [void]$sb.Append('],"file_count":'); [void]$sb.Append([string]$keys.Length)
    [void]$sb.Append(',"schema":"saituls.secure-apps.fido-worker-bundle/1"}')
    $bundleFingerprint = ([System.BitConverter]::ToString(
        $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($sb.ToString())))
        ).Replace('-', '').ToLowerInvariant()
} finally { $sha.Dispose() }

$exeSha = (Get-FileHash -LiteralPath (Join-Path $target 'sa_fido_worker.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Built bundle: $target"
Write-Host "Files:        $($keys.Length)"
Write-Host "sa_fido_worker.exe SHA-256: $exeSha"
Write-Host "fido_worker_bundle_fingerprint: $bundleFingerprint"
Write-Host ""
Write-Host "Smoke test (capabilities): pipe a request on stdin --"
Write-Host ('  echo {"op":"capabilities","args":{}} | ' + (Join-Path $target 'sa_fido_worker.exe'))
