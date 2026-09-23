# T-134 media payload validity and atomic publication (SRC-009 R011 W2-002).
# Function library only: presence is NOT validity. A staged download is validated
# (size + PE 'MZ'/signature sanity + optional --version probe) BEFORE any
# destination write, and the coupled ffmpeg/ffprobe pair is published as ONE
# generation -- a failure part-way through rolls the destination back to the
# previous generation so no mixed pair is ever left behind.
#
# Dot-source from setup.ps1; run directly only by the test harness.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Minimum meaningful size per payload kind. A handful of bytes is never a real
# tool, and a truncated download must not be mistaken for a ready one.
$MIN_SIZE = @{
    'ffmpeg'  = 30MB
    'ffprobe' = 10MB
    'yt-dlp'  = 5MB
    'aria2c'  = 1MB
    'deno'    = 10MB
}

function Test-ExecutableValid {
    param(
        [string]$Path,
        [string]$Kind = '',
        [switch]$NoVersionProbe
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{ Valid = $false; Reason = 'absent' }
    }
    # Read the header only first; full read only when size warrants it.
    $info = Get-Item -LiteralPath $Path
    if ($info.Length -lt 2048) {
        return [pscustomobject]@{ Valid = $false; Reason = "too small ($($info.Length) bytes)" }
    }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    # A text file renamed to .exe does not start with the PE 'MZ' marker.
    if ($bytes[0] -ne 0x4D -or $bytes[1] -ne 0x5A) {
        return [pscustomobject]@{ Valid = $false; Reason = 'no PE MZ header' }
    }
    # e_lfanew -> PE signature 'PE\0\0'. A corrupt or truncated image lacks it.
    $e_lfanew = [BitConverter]::ToInt32($bytes, 0x3C)
    if ($e_lfanew -le 0 -or $e_lfanew -gt $bytes.Length - 4) {
        return [pscustomobject]@{ Valid = $false; Reason = 'invalid PE offset' }
    }
    if ($bytes[$e_lfanew] -ne 0x50 -or $bytes[$e_lfanew + 1] -ne 0x45 -or
        $bytes[$e_lfanew + 2] -ne 0 -or $bytes[$e_lfanew + 3] -ne 0) {
        return [pscustomobject]@{ Valid = $false; Reason = 'no PE signature' }
    }
    if ($MIN_SIZE.ContainsKey($Kind) -and $info.Length -lt $MIN_SIZE[$Kind]) {
        return [pscustomobject]@{ Valid = $false; Reason = "smaller than minimum for $Kind ($($info.Length) bytes)" }
    }
    if (-not $NoVersionProbe) {
        try {
            $tmpOut = Join-Path $env:TEMP ("ver_$([Guid]::NewGuid().ToString('N')).txt")
            $p = Start-Process -FilePath $Path -ArgumentList '--version' -Wait -PassThru -WindowStyle Hidden -RedirectStandardOutput $tmpOut -ErrorAction Stop
            if ($p.ExitCode -ne 0) {
                return [pscustomobject]@{ Valid = $false; Reason = "$Kind --version exited $($p.ExitCode)" }
            }
        } catch {
            return [pscustomobject]@{ Valid = $false; Reason = "version probe failed: $($_.Exception.Message)" }
        } finally {
            Remove-Item -LiteralPath $tmpOut -Force -ErrorAction SilentlyContinue
        }
    }
    return [pscustomobject]@{ Valid = $true; Reason = '' }
}

function Publish-Pair {
    param(
        [string]$StagingDir,
        [string]$DestDir,
        [string[]]$Members
    )
    # Publish the member set as ONE generation. Each current destination member is
    # backed up; the new members are copied over. If any copy fails, every member
    # already moved is restored from its backup so the previous generation stays
    # byte-intact -- no mixed pair, no half-written binary.
    $backupDir = Join-Path $env:TEMP ("mediabak_$([Guid]::NewGuid().ToString('N'))")
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    try {
        $backedUp = @{}
        foreach ($m in $Members) {
            $src = Join-Path $StagingDir $m
            $dst = Join-Path $DestDir $m
            if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
                throw "staged member missing: $m"
            }
            if (Test-Path -LiteralPath $dst -PathType Leaf) {
                Copy-Item -LiteralPath $dst -Destination (Join-Path $backupDir $m) -Force
            }
            $backedUp[$m] = $true
        }
        foreach ($m in $Members) {
            $src = Join-Path $StagingDir $m
            $dst = Join-Path $DestDir $m
            try {
                Copy-Item -LiteralPath $src -Destination $dst -Force
            } catch {
                foreach ($b in $backedUp.Keys) {
                    $bdst = Join-Path $DestDir $b
                    $bsrc = Join-Path $backupDir $b
                    if (Test-Path -LiteralPath $bsrc) {
                        Copy-Item -LiteralPath $bsrc -Destination $bdst -Force
                    } else {
                        Remove-Item -LiteralPath $bdst -Force -ErrorAction SilentlyContinue
                    }
                }
                throw "publish of $m failed: $($_.Exception.Message)"
            }
        }
        return $true
    } finally {
        Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
