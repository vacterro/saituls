param(
    [Parameter(Mandatory = $true)]
    [string]$Root
)

$ErrorActionPreference = 'Stop'
$stage = $null
$exitCode = 0

function Get-ManifestEntries {
    param([string]$ManifestPath, [string]$ProjectRoot)

    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Payload manifest not found: $ManifestPath"
    }

    $entries = @(
        Get-Content -LiteralPath $ManifestPath | ForEach-Object {
            $entry = $_.Trim().Replace('\', '/')
            if ($entry -and -not $entry.StartsWith('#')) { $entry }
        }
    )
    if ($entries.Count -eq 0) { throw 'Payload manifest is empty.' }

    $seen = @{}
    $rootPrefix = $ProjectRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    foreach ($entry in $entries) {
        $parts = @($entry -split '/')
        if (-not $entry.StartsWith('Bin/', [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::IsPathRooted($entry) -or
            $entry.IndexOfAny([char[]]'*?') -ge 0 -or
            $parts -contains '..' -or $parts -contains '.' -or $parts -contains '') {
            throw "Unsafe payload manifest entry: $entry"
        }

        $key = $entry.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "Duplicate payload manifest entry: $entry" }
        $seen[$key] = $true

        $source = [IO.Path]::GetFullPath((Join-Path $ProjectRoot ($entry.Replace('/', '\'))))
        if (-not $source.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Payload source missing: $entry"
        }
    }
    return $entries
}

function Assert-ArchiveMembers {
    param([string]$ArchivePath, [string[]]$ExpectedEntries)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $actual = @(
            $archive.Entries |
                Where-Object { -not $_.FullName.EndsWith('/') } |
                ForEach-Object { $_.FullName.Replace('\', '/').TrimStart('/') }
        )
    } finally {
        $archive.Dispose()
    }

    $expectedKeys = @($ExpectedEntries | ForEach-Object { $_.ToLowerInvariant() } | Sort-Object)
    $actualKeys = @($actual | ForEach-Object { $_.ToLowerInvariant() } | Sort-Object)
    $difference = @(Compare-Object -ReferenceObject $expectedKeys -DifferenceObject $actualKeys)
    if ($difference.Count -ne 0 -or $actualKeys.Count -ne $expectedKeys.Count) {
        $detail = ($difference | ForEach-Object { "$($_.SideIndicator) $($_.InputObject)" }) -join '; '
        throw "Payload ZIP members do not match PAYLOAD_MANIFEST.txt: $detail"
    }
}

try {
    $Root = [IO.Path]::GetFullPath($Root)
    $versionPath = Join-Path $Root 'VERSION'
    if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
        throw "VERSION not found: $versionPath"
    }
    $version = (Get-Content -LiteralPath $versionPath -Raw).Trim()
    if ($version -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]*$') {
        throw "Invalid release version in VERSION: '$version'"
    }

    $manifestPath = Join-Path $Root 'PAYLOAD_MANIFEST.txt'
    $entries = @(Get-ManifestEntries -ManifestPath $manifestPath -ProjectRoot $Root)

    $dist = Join-Path $Root 'dist'
    [IO.Directory]::CreateDirectory($dist) | Out-Null
    $final = Join-Path $dist "SAITULS-payload-$version.zip"
    $stage = Join-Path $dist (".SAITULS-payload-{0}.{1}.tmp.zip" -f $version, [guid]::NewGuid().ToString('N'))
    $archiveMembers = @($entries | ForEach-Object { $_.Replace('/', '\') })

    Push-Location $Root
    try {
        $customArchiver = $env:SAITULS_PAYLOAD_ARCHIVER
        if ($customArchiver) {
            & $customArchiver $stage $Root $manifestPath
        } elseif (Test-Path -LiteralPath 'C:\Program Files\7-Zip\7z.exe' -PathType Leaf) {
            & 'C:\Program Files\7-Zip\7z.exe' a -tzip -bd -bso0 -bsp0 $stage @archiveMembers
        } else {
            # Resolve the Windows-native bsdtar explicitly: a Git Bash GNU tar
            # earlier in PATH misreads "V:\..." as a remote hostname.
            $tarCandidates = @(
                (Join-Path $env:SystemRoot 'System32\tar.exe'),
                (Get-Command tar.exe -ErrorAction SilentlyContinue).Source
            )
            $tar = $tarCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
            if (-not $tar) { throw 'tar.exe archiver not found (Windows System32\tar.exe expected).' }
            & $tar -a -c -f $stage -C $Root @archiveMembers
        }
        $archiverExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($archiverExit -ne 0) { throw "Archiver failed with exit code $archiverExit." }
    if (-not (Test-Path -LiteralPath $stage -PathType Leaf)) {
        throw 'Archiver reported success but did not create a ZIP.'
    }

    Assert-ArchiveMembers -ArchivePath $stage -ExpectedEntries $entries

    if (Test-Path -LiteralPath $final -PathType Leaf) {
        $backup = Join-Path $dist (".{0}.{1}.previous" -f [IO.Path]::GetFileName($final), [guid]::NewGuid().ToString('N'))
        [IO.File]::Replace($stage, $final, $backup, $true)
        $stage = $null
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    } else {
        [IO.File]::Move($stage, $final)
        $stage = $null
    }

    $size = (Get-Item -LiteralPath $final).Length
    Write-Host ''
    Write-Host "Payload:  $final"
    Write-Host "Size:     $size bytes"
    Write-Host "Members:  $($entries.Count) (validated)"
    Write-Host ''
    Write-Host "Upload this file to the GitHub Release for SAITULS $version."
} catch {
    $exitCode = 1
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
} finally {
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        Remove-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue
    }
}

exit $exitCode
