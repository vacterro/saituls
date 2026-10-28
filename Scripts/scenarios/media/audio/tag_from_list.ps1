# =============================================================================
#  TAG.ps1
#  Reads list.txt -> sets Track#, Title from filename.
#  Copies all other ID3v2 tags from the FIRST file to all others.
#  Embeds cover.png if present next to list.txt.
#  Uses ffmpeg (must be in PATH) -- audio stream is copied losslessly.
# =============================================================================

param(
    [Parameter(Mandatory=$true)]
    [string]$FolderPath
)

$ErrorActionPreference = "Continue"

# ── helpers ──────────────────────────────────────────────────────────────────

function Write-Step { param([string]$m) Write-Host "  $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  OK  $m" -ForegroundColor Green }
function Write-Fail { param([string]$m) Write-Host "  ERR $m" -ForegroundColor Red }
function Write-Warn { param([string]$m) Write-Host "  WRN $m" -ForegroundColor Yellow }

# Read all tags from an mp3 via ffprobe -> hashtable
function Get-Tags {
    param([string]$Path)
    $json = & ffprobe -v quiet -print_format json -show_format $Path 2>$null | ConvertFrom-Json
    $tags = @{}
    if ($json -and $json.format -and $json.format.tags) {
        $json.format.tags.PSObject.Properties | ForEach-Object { $tags[$_.Name.ToLower()] = $_.Value }
    }
    return $tags
}

# ── validate ─────────────────────────────────────────────────────────────────

if (-not (Test-Path $FolderPath -PathType Container)) {
    Write-Fail "Folder not found: $FolderPath"; exit 1
}

if (-not (Get-Command ffmpeg  -ErrorAction SilentlyContinue)) { Write-Fail "ffmpeg not in PATH"; exit 1 }
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { Write-Fail "ffprobe not in PATH"; exit 1 }

$listFile  = Join-Path $FolderPath "list.txt"
$coverFile = Join-Path $FolderPath "cover.png"

if (-not (Test-Path $listFile)) { Write-Fail "list.txt not found in $FolderPath"; exit 1 }

Write-Host ""
Write-Host "=== Apply ID3v2 Tags ===" -ForegroundColor White
Write-Host "  Folder : $FolderPath"

# ── parse list.txt ────────────────────────────────────────────────────────────
# Handles lines like:  file 'V:\path\to\track.mp3'
# or plain paths:      V:\path\to\track.mp3

$orderedFiles = @()
foreach ($line in Get-Content $listFile -Encoding UTF8) {
    $line = $line.Trim()
    if ($line -match "file\s+'(.+)'") {
        $orderedFiles += $Matches[1]
    } elseif ($line -match "file\s+`"(.+)`"") {
        $orderedFiles += $Matches[1]
    } elseif ($line.Length -gt 0 -and -not $line.StartsWith("#")) {
        $orderedFiles += $line.Trim("'`"")
    }
}

$totalTracks = $orderedFiles.Count
Write-Host "  Tracks : $totalTracks (from list.txt)"
if ($totalTracks -eq 0) { Write-Fail "No tracks parsed from list.txt"; exit 1 }

# ── read template tags from first file ────────────────────────────────────────
# All shared fields (artist, album, genre, comment, etc.) come from file #1.
# Track and title are set individually per file.

$firstFile = $orderedFiles[0]
if (-not (Test-Path $firstFile)) {
    Write-Warn "First file not found, template tags will be empty: $firstFile"
    $tmpl = @{}
} else {
    Write-Step "Reading template tags from: $(Split-Path -Leaf $firstFile)"
    $tmpl = Get-Tags $firstFile
}

# Fields we handle individually -- do NOT copy from template
$skipFields = @("title","track","tracknumber","track_number")

# Display template tags that will be applied
$sharedFields = $tmpl.Keys | Where-Object { $_ -notin $skipFields }
if ($sharedFields) {
    Write-Host ""
    Write-Host "  Shared tags (from first file):" -ForegroundColor DarkCyan
    foreach ($k in ($sharedFields | Sort-Object)) {
        $v = $tmpl[$k]
        if ($v.Length -gt 60) { $v = $v.Substring(0,57) + "..." }
        Write-Host ("    {0,-20} = {1}" -f $k, $v)
    }
}

$hasCover = Test-Path $coverFile
Write-Host "  Cover  : $(if ($hasCover) { $coverFile } else { 'cover.png not found, skipping' })"
Write-Host ""

# ── process each file ─────────────────────────────────────────────────────────

$ok = 0; $fail = 0

for ($i = 0; $i -lt $orderedFiles.Count; $i++) {
    $srcPath   = $orderedFiles[$i]
    $trackNum  = $i + 1
    $trackStr  = "${trackNum}/${totalTracks}"

    if (-not (Test-Path $srcPath)) {
        Write-Fail "[$trackStr] File not found: $srcPath"
        $fail++
        continue
    }

    $title     = [System.IO.Path]::GetFileNameWithoutExtension($srcPath)
    $dir       = Split-Path -Parent $srcPath
    $ext       = [System.IO.Path]::GetExtension($srcPath)
    $tmpPath   = Join-Path $dir (".__tmp_tag_" + [System.IO.Path]::GetFileName($srcPath))

    Write-Step "[$trackStr] $title"

    # Build -metadata args from template
    $metaArgs = @()
    foreach ($k in $sharedFields) {
        $metaArgs += "-metadata"
        $metaArgs += "${k}=$($tmpl[$k])"
    }
    # Per-file fields
    $metaArgs += @("-metadata", "title=$title")
    $metaArgs += @("-metadata", "track=$trackStr")

    # Build full ffmpeg command
    if ($hasCover) {
        # Embed cover art
        $ffArgs = @(
            "-y", "-hide_banner", "-loglevel", "error",
            "-i", $srcPath,
            "-i", $coverFile,
            "-map", "0:a",           # audio from source
            "-map", "1:v",           # cover from cover.png
            "-codec:a", "copy",      # lossless audio copy
            "-codec:v", "mjpeg",     # cover as MJPEG (standard for MP3)
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)"
        ) + $metaArgs + @("-id3v2_version", "3", $tmpPath)
    } else {
        $ffArgs = @(
            "-y", "-hide_banner", "-loglevel", "error",
            "-i", $srcPath,
            "-map", "0:a",
            "-codec:a", "copy"
        ) + $metaArgs + @("-id3v2_version", "3", $tmpPath)
    }

    & ffmpeg @ffArgs 2>&1 | Out-Null
    $ec = $LASTEXITCODE

    if ($ec -ne 0 -or -not (Test-Path $tmpPath) -or (Get-Item $tmpPath).Length -lt 1000) {
        Write-Fail "  ffmpeg failed (code $ec)"
        Remove-Item $tmpPath -Force -ErrorAction SilentlyContinue
        $fail++
        continue
    }

    # Replace original atomically
    try {
        Remove-Item $srcPath -Force -ErrorAction Stop
        Move-Item   $tmpPath $srcPath -Force -ErrorAction Stop
        Write-Ok "  [$trackStr] $title"
        $ok++
    } catch {
        Write-Fail "  Could not replace original: $_"
        Remove-Item $tmpPath -Force -ErrorAction SilentlyContinue
        $fail++
    }
}

# ── summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Done: $ok OK, $fail failed ===" -ForegroundColor $(if ($fail -eq 0) { "Green" } else { "Yellow" })
Write-Host ""
if ($fail -gt 0) { exit 1 }
