param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$BackupRoot = '',
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'

if (-not $BackupRoot) {
    $ini = Join-Path $Root 'SAITULS.ini'
    if (Test-Path -LiteralPath $ini -PathType Leaf) {
        $setting = Get-Content -LiteralPath $ini | Where-Object { $_ -match '^\s*PayloadBackup\s*=' } | Select-Object -Last 1
        if ($setting) { $BackupRoot = ($setting -split '=', 2)[1].Trim() }
    }
}
if (-not $BackupRoot) { throw 'Payload backup path is not configured' }
if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw "SAITULS root missing: $Root" }
if (-not (Test-Path -LiteralPath $BackupRoot -PathType Container)) { throw "Payload backup missing: $BackupRoot" }

$manifest = Join-Path $Root 'PAYLOAD_MANIFEST.txt'
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "Payload manifest missing: $manifest" }

$rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
$backupFull = [IO.Path]::GetFullPath($BackupRoot).TrimEnd('\') + '\'
if ($rootFull -ieq $backupFull) { throw 'Payload backup must differ from SAITULS root' }

$relativePaths = @('SAITULS.exe') + @(Get-Content -LiteralPath $manifest | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
$relativePaths = @($relativePaths | Select-Object -Unique)
$items = @()
foreach ($relative in $relativePaths) {
    if ([IO.Path]::IsPathRooted($relative)) { throw "Invalid rooted payload path: $relative" }
    $source = [IO.Path]::GetFullPath((Join-Path $backupFull $relative))
    $destination = [IO.Path]::GetFullPath((Join-Path $rootFull $relative))
    if (-not $source.StartsWith($backupFull, [StringComparison]::OrdinalIgnoreCase) -or -not $destination.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Payload path escapes root: $relative"
    }
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Backup payload missing: $relative" }
    $sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
    $destinationHash = if (Test-Path -LiteralPath $destination -PathType Leaf) { (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash } else { '' }
    if ($sourceHash -cne $destinationHash) {
        $items += [pscustomobject]@{ Relative = $relative; Source = $source; Destination = $destination; Hash = $sourceHash }
    }
}

if ($VerifyOnly) {
    if ($items.Count) { throw "Payload differs from backup: $($items.Relative -join ', ')" }
    Write-Host "Payload verified: $($relativePaths.Count) file(s)."
    exit 0
}

foreach ($item in $items) {
    $parent = Split-Path -Parent $item.Destination
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $stage = $item.Destination + '.restore_' + [Guid]::NewGuid().ToString('N')
    try {
        Copy-Item -LiteralPath $item.Source -Destination $stage
        if ((Get-FileHash -LiteralPath $stage -Algorithm SHA256).Hash -cne $item.Hash) { throw "Copied payload hash mismatch: $($item.Relative)" }
        Move-Item -LiteralPath $stage -Destination $item.Destination -Force
    } finally {
        Remove-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Payload restored: $($items.Count) changed; $($relativePaths.Count) verified."
exit 0
