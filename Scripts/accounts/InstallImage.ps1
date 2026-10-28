# Replace an installed executable without stopping processes using its old image.
# The caller owns the exact directory; retained images remain until no longer in use.
function Get-SaiExecutableHash([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
    finally { $sha.Dispose(); $stream.Dispose() }
}

function Install-SaiExecutableImage([string]$Source, [string]$Target, [string]$AllowedDirectory) {
    $directory = (Get-Item -LiteralPath $AllowedDirectory -ErrorAction Stop).FullName
    $destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Target)
    if (-not [string]::Equals([IO.Path]::GetDirectoryName($destination).TrimEnd('\'), $directory.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "Executable replacement target is outside its owned directory: $destination"
    }
    $sourcePath = (Get-Item -LiteralPath $Source -ErrorAction Stop).FullName
    $sourceHash = Get-SaiExecutableHash $sourcePath
    if ((Test-Path -LiteralPath $destination -PathType Leaf) -and
        (Get-SaiExecutableHash $destination) -eq $sourceHash) {
        return [pscustomobject]@{ Status = 'already_correct'; Sha256 = $sourceHash; RetainedImage = $null }
    }
    $suffix = [Guid]::NewGuid().ToString('N')
    $staged = "$destination.new-$suffix"
    $retained = "$destination.old-$suffix"
    $movedOld = $false
    try {
        Copy-Item -LiteralPath $sourcePath -Destination $staged -ErrorAction Stop
        if ((Get-SaiExecutableHash $staged) -ne $sourceHash) { throw 'Staged executable hash mismatch' }
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            Move-Item -LiteralPath $destination -Destination $retained -ErrorAction Stop
            $movedOld = $true
        }
        try { Move-Item -LiteralPath $staged -Destination $destination -ErrorAction Stop }
        catch {
            if ($movedOld -and -not (Test-Path -LiteralPath $destination)) {
                Move-Item -LiteralPath $retained -Destination $destination -ErrorAction Stop
                $movedOld = $false
            }
            throw
        }
        if ((Get-SaiExecutableHash $destination) -ne $sourceHash) { throw 'Installed executable hash mismatch' }
        return [pscustomobject]@{ Status = 'applied'; Sha256 = $sourceHash; RetainedImage = $(if ($movedOld) { $retained } else { $null }) }
    }
    finally {
        # This exact staging file is owned by this call; never delete a retained
        # image while a title guard or another process may still be using it.
        if (Test-Path -LiteralPath $staged -PathType Leaf) { Remove-Item -LiteralPath $staged -ErrorAction SilentlyContinue }
    }
}
