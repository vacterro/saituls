# T-167 / T-164: YouTube download worker.
#
# The real orchestration lives here; Scripts\DL_YT.CMD is a thin compatibility
# launcher and Scripts\DL_YT.PS1 is the single implementation. There is exactly
# ONE download implementation -- the old all-CMD version fought for/codepage
# re-parsing with Unicode URLs, spaces in the toolkit root and playlist/date
# templates and could not validate its own input.
#
# Contract (unchanged for Explorer, Registry\DL_YT.REG passes %1 as the mode):
#   -Mode audio|video            base mode
#   -Playlist                    --yes-playlist, playlist output template
#   -Dated                       upload-date prefixed output template
#   -OutPath <folder>            destination (default: current dir, with the
#                                old system32 / toolkit-root guards)
#   -UrlsFile <file>             advanced/test input: read links from a file
#                                instead of the clipboard
#   -YtDlp/-FFmpeg/-Deno         executable overrides (also SAITULS_YTDLP,
#                                SAITULS_FFMPEG, SAITULS_DENO env vars)
#
# Guarantees:
#   * the source is the CLIPBOARD URL TEXT unless -UrlsFile is given;
#   * the clipboard is validated -- files-copy, prose and non-http schemes are
#     refused with a reason instead of being handed to yt-dlp;
#   * every run owns a unique temp URL list (concurrent runs cannot consume
#     each other's links) and that file is always cleaned up;
#   * the child exit code is propagated, Unicode/spaces survive;
#   * the URL text is never echoed -- only the count.
[CmdletBinding()]
param(
    [string]$Mode = 'video',
    [string]$OutPath = '',
    [string]$UrlsFile = '',
    [switch]$Playlist,
    [switch]$Dated,
    [string]$YtDlp = '',
    [string]$FFmpeg = '',
    [string]$Deno = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root 'Bin'

function Stop-Download([int]$Code, [string]$Message) {
    Write-Output $Message
    Write-Output ("SAITULS_DL_STATUS={0}" -f $Code)
    Write-Output ("SAITULS_DL_ERROR={0}" -f ($Message -replace '\r?\n', ' '))
    exit $Code
}

# ---- mode (legacy composite tokens still arrive from the Registry) ----------
$baseMode = $Mode.ToLowerInvariant()
$flagTail = ''
if ($baseMode -match '^(audio|video)(dated|playlist)?$') {
    if ($Matches[2]) { $flagTail = $Matches[2] }
    $baseMode = $Matches[1]
} else {
    Stop-Download 2 "Unknown download mode '$Mode'. Use audio or video."
}
if ($flagTail -eq 'dated') { $Dated = $true }
if ($flagTail -eq 'playlist') { $Playlist = $true }

# ---- bundled executables (absolute paths wherever possible) -----------------
if (-not $YtDlp) { $YtDlp = $env:SAITULS_YTDLP }
if (-not $YtDlp) { $YtDlp = Join-Path $bin 'yt-dlp.exe' }
if (-not $FFmpeg) { $FFmpeg = $env:SAITULS_FFMPEG }
if (-not $FFmpeg) { $FFmpeg = Join-Path $bin 'FFMPEG.EXE' }
if (-not $Deno) { $Deno = $env:SAITULS_DENO }
if (-not $Deno) { $Deno = Join-Path $bin 'deno.exe' }

if (-not (Test-Path -LiteralPath $YtDlp -PathType Leaf)) {
    Stop-Download 2 "yt-dlp is missing: $YtDlp`nOpen SAITULS and run Install / repair first."
}
if (-not (Test-Path -LiteralPath $FFmpeg -PathType Leaf)) {
    Stop-Download 2 "FFmpeg is missing: $FFmpeg`nOpen SAITULS and run Install / repair first."
}

$jsRuntime = ''
if (Test-Path -LiteralPath $Deno -PathType Leaf) {
    $jsRuntime = 'deno'
} elseif (Get-Command node -ErrorAction SilentlyContinue) {
    $jsRuntime = 'node'
} else {
    Write-Output 'SAITULS_DL_WARNING=No JavaScript runtime (Deno/Node) found; YouTube downloads may fail.'
}

# ---- destination -----------------------------------------------------------
if (-not $OutPath) {
    try { $OutPath = (Get-Location).ProviderPath } catch { $OutPath = $env:USERPROFILE }
}
try { $OutPath = [System.IO.Path]::GetFullPath($OutPath) } catch {
    Stop-Download 2 "The destination folder is not a valid path: $OutPath"
}
$defaultOut = Join-Path $env:USERPROFILE 'Downloads\_YT_VIDEO'
$trimmedOut = $OutPath.TrimEnd('\')
if ($trimmedOut -like '*\System32' -or $trimmedOut -like '*\SysWOW64') { $OutPath = $defaultOut }
elseif ($trimmedOut -eq $root.TrimEnd('\')) { $OutPath = $defaultOut }
try {
    if (-not (Test-Path -LiteralPath $OutPath)) { New-Item -ItemType Directory -Path $OutPath -Force | Out-Null }
} catch {
    Stop-Download 2 "Cannot create the destination folder: $OutPath ($($_.Exception.Message))"
}

# ---- source links: file (advanced/test) or the clipboard -------------------
$sourceText = ''
if ($UrlsFile) {
    if (-not (Test-Path -LiteralPath $UrlsFile -PathType Leaf)) {
        Stop-Download 3 "The link list file does not exist: $UrlsFile"
    }
    # [string] cast: an EMPTY file reads as $null and "$null -split" is fine, but
    # the later .Trim() must never throw on it.
    $sourceText = [string](Get-Content -LiteralPath $UrlsFile -Raw -Encoding UTF8)
} else {
    # @() around Get-Clipboard would otherwise turn its $null "no file drop"
    # answer into a one-element array, so the emptiness check would always claim
    # files were copied. Filter the nulls out before counting.
    $raw = @()
    try { $raw = @(Get-Clipboard -Format FileDropList -ErrorAction Stop) } catch { $raw = @() }
    $files = @($raw | Where-Object { $_ -ne $null -and "$_" -ne '' })
    if ($files.Count -gt 0) {
        Stop-Download 3 "The clipboard holds copied files, not links. Copy the video link as text first."
    }
    try { $sourceText = [string](Get-Clipboard -Raw -Format Text -ErrorAction Stop) } catch { $sourceText = '' }
}
# Normalize: an empty file (or an empty clipboard read) arrives as $null, and
# PowerShell's [string] cast KEEPS it null in 5.1 -- the later .Trim() must
# never throw on it. Interpolation always yields a real string.
$sourceText = "$sourceText"


$urls = New-Object System.Collections.Generic.List[string]
$badSchemes = New-Object System.Collections.Generic.List[string]
foreach ($line in (($sourceText -split "\r?\n"))) {
    $candidate = $line.Trim()
    if (-not $candidate) { continue }
    $match = [regex]::Match($candidate, 'https?://\S+', 'IgnoreCase')
    if ($match.Success) {
        $url = $match.Value.TrimEnd('.', ',', ';', ':', ')', ']', '>', '"', "'")
        if ($url) { $urls.Add($url) }
        continue
    }
    if ($candidate -match '^[A-Za-z][A-Za-z0-9+.\-]*://') { $badSchemes.Add($candidate); continue }
}

if ($badSchemes.Count -gt 0) {
    Stop-Download 3 ("Unsupported link scheme in the clipboard input ({0}); only http:// and https:// URLs are accepted." -f $badSchemes[0])
}
if ($urls.Count -eq 0) {
    if (-not $sourceText.Trim()) {
        Stop-Download 3 "No downloadable URL found in the clipboard. Copy one youtube link (or several, one per line) and try again."
    }
    Stop-Download 3 "No downloadable URL found in the clipboard. The clipboard text has no http/https link."
}

# ---- per-run isolated URL list --------------------------------------------
$tempFile = Join-Path $env:TEMP ("saituls-urls-{0}.txt" -f [guid]::NewGuid().ToString('N'))
try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tempFile, (($urls -join "`n") + "`n"), $utf8)
} catch {
    Stop-Download 2 "Cannot create the per-run link list: $($_.Exception.Message)"
}

Write-Output ("SAITULS_DL_MODE={0}" -f $baseMode)
Write-Output ("SAITULS_DL_DESTINATION={0}" -f $OutPath)
Write-Output ("SAITULS_DL_URLS={0}" -f $urls.Count)
if ($Playlist) { Write-Output 'SAITULS_DL_PLAYLIST=1' }
if ($Dated) { Write-Output 'SAITULS_DL_DATED=1' }

$arguments = New-Object System.Collections.Generic.List[string]
$arguments.Add('-a'); $arguments.Add($tempFile)
if ($Playlist) { $arguments.Add('--yes-playlist') } else { $arguments.Add('--no-playlist') }

if ($Playlist) {
    $template = "$OutPath\%(playlist_title)s\%(playlist_index)s - %(title)s.%(ext)s"
} elseif ($Dated) {
    $template = "$OutPath\%(upload_date>%Y-%m-%d)s - %(title)s.%(ext)s"
} else {
    $template = "$OutPath\%(title)s.%(ext)s"
}
$arguments.Add('-o'); $arguments.Add($template)

if ($baseMode -eq 'audio') {
    $arguments.AddRange([string[]]@('-f', 'bestaudio', '--extract-audio', '--audio-format', 'mp3', '--audio-quality', '0'))
} else {
    $arguments.AddRange([string[]]@(
        '-f', 'bestvideo[vcodec^=av01][height<=1080]+bestaudio/bestvideo[height<=1080]+bestaudio/best',
        '--merge-output-format', 'mkv'))
}
# --quiet keeps progress readable without echoing the source URLs.
$arguments.AddRange([string[]]@('--force-overwrites', '--quiet', '--progress'))
$arguments.Add('--ffmpeg-location'); $arguments.Add($FFmpeg)
if ($jsRuntime -eq 'deno') {
    $arguments.AddRange([string[]]@('--js-runtimes', ("deno:{0}" -f $Deno), '--remote-components', 'ejs:github'))
} elseif ($jsRuntime -eq 'node') {
    $arguments.AddRange([string[]]@('--js-runtimes', 'node', '--remote-components', 'ejs:github'))
}

$code = 1
try {
    $argumentArray = $arguments.ToArray()
    & $YtDlp @argumentArray
    $code = $LASTEXITCODE
} catch {
    $code = 4
    Write-Output ("yt-dlp could not be started: {0}" -f $_.Exception.Message)
} finally {
    # The per-run link list never outlives the run that created it.
    try { Remove-Item -LiteralPath $tempFile -Force -ErrorAction Stop }
    catch { Write-Output ("SAITULS_DL_WARNING=Could not remove the temporary link list: {0}" -f $tempFile) }
}

if ($code -ne 0) {
    Write-Output ""
    Write-Output ("Download failed (yt-dlp exit {0}). See the messages above." -f $code)
    Write-Output ("SAITULS_DL_STATUS={0}" -f $code)
    Write-Output ("SAITULS_DL_ERROR=yt-dlp exited {0}" -f $code)
    exit $code
}

Write-Output ""
Write-Output ("Done. Saved to: {0}" -f $OutPath)
Write-Output 'SAITULS_DL_STATUS=0'
exit 0
