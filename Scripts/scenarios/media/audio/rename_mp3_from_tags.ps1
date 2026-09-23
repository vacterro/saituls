<#
.SYNOPSIS
    Renames MP3 files based on their metadata (Artist and Title).
.DESCRIPTION
    T-166 launch contract: the launcher's positional target folder is the
    authority. An invalid explicit target must refuse: nonzero exit, no
    rename, no silent fallback to the caller's CWD.
#>
param(
    [Parameter(Position=0)]
    [string]$TargetFolder
)

$ErrorActionPreference = 'Stop'

if (-not $TargetFolder) {
    Write-Error "No target folder supplied. Usage: rename_mp3_from_tags.ps1 <folder>"
    exit 2
}

$folder = $null
if (Test-Path -LiteralPath $TargetFolder -PathType Container) {
    $folder = (Resolve-Path -LiteralPath $TargetFolder).Path
}
if (-not $folder) {
    Write-Error "Target is not an existing folder: $TargetFolder"
    exit 2
}

$shell = New-Object -ComObject Shell.Application
$dir = $shell.Namespace($folder)

if (-not $dir) {
    Write-Error "Failed to get Shell namespace for $folder"
    exit 1
}

$renamed = 0
Get-ChildItem -LiteralPath $folder -Filter *.mp3 | ForEach-Object {
    try {
        $file = $_
        $item = $dir.ParseName($file.Name)

        # Try common column indexes for Artist and Title
        # 13 = Artist, 21 = Title (most Windows builds)
        $artist = $dir.GetDetailsOf($item, 13)
        $title  = $dir.GetDetailsOf($item, 21)

        if (![string]::IsNullOrWhiteSpace($title)) {

            # Remove invalid filename characters
            $cleanTitle = $title -replace '[\\/:*?"<>|]', ''
            $newName = "$cleanTitle.mp3"

            if ($file.Name -ne $newName) {

                # Handle duplicate names safely
                $counter = 1
                $baseName = $cleanTitle

                while (Test-Path -LiteralPath (Join-Path $folder $newName)) {
                    $newName = "$baseName ($counter).mp3"
                    $counter++
                }

                Rename-Item -LiteralPath $file.FullName -NewName $newName
                $renamed++
                Write-Output "[+] Renamed: $($file.Name) -> $newName"
            }
        }
    } catch {
        Write-Warning "[-] Error processing $($_.Name): $($_.Exception.Message)"
    }
}

Write-Output "Done. Renamed: $renamed"
exit 0
