# T-167: Merge Audio worker. Contract: EXACTLY TWO audio streams; video,
# subtitles, data and attachments are preserved by stream copy; the two audio
# streams are merged into one stereo AAC track. Anything else fails BEFORE the
# source is touched. Supported flow:
#   unique scratch -> ffmpeg -> ffprobe validation -> replace source.
# The source is never replaced merely because ffmpeg exited 0.
param(
    # Optional overrides (test seam / explicit invocation). The shipped
    # launchers pass nothing and get the bundled pair.
    [string]$FFmpeg = '',
    [string]$FFprobe = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$scriptsDir = $PSScriptRoot
$root = Split-Path -Parent $scriptsDir
if (-not $FFmpeg) { $FFmpeg = Join-Path $root 'Bin\FFMPEG.EXE' }
if (-not $FFprobe) { $FFprobe = Join-Path $root 'Bin\FFPROBE.EXE' }

function Stop-Merge([int]$Code, [string]$Message, [string]$Scratch) {
    if ($Scratch -and (Test-Path -LiteralPath $Scratch)) {
        Remove-Item -LiteralPath $Scratch -Force -ErrorAction SilentlyContinue
    }
    Write-Output $Message
    exit $Code
}

$in = $env:MERGE_IN
if (-not $in) { Stop-Merge 10 'Merge Audio: input path missing (env MERGE_IN not set)' $null }
if (-not (Test-Path -LiteralPath $in -PathType Leaf)) {
    Stop-Merge 2 "Файл не найден: $in" $null
}
if (-not (Test-Path -LiteralPath $FFmpeg -PathType Leaf)) {
    Stop-Merge 5 'FFmpeg не найден. Откройте SAITULS и запустите установку/ремонт.' $null
}
if (-not (Test-Path -LiteralPath $FFprobe -PathType Leaf)) {
    Stop-Merge 5 'FFprobe не найден (нужен для проверки дорожек). Откройте SAITULS и запустите установку/ремонт.' $null
}
$in = (Resolve-Path -LiteralPath $in).ProviderPath

# ffprobe JSON stream dump: one call carries the whole topology
$dump = Join-Path $env:TEMP ("ma_streams_{0}.json" -f [guid]::NewGuid().ToString('N'))
$p = Start-Process -FilePath $FFprobe -ArgumentList @(
    '-v', 'error', '-show_entries', 'stream=index,codec_type,channels,codec_name',
    '-show_entries', 'format=duration', '-of', 'json', $in) `
    -NoNewWindow -Wait -PassThru -RedirectStandardOutput $dump -RedirectStandardError "$dump.err"
$err = $p.ExitCode
if ($err -ne 0) {
    Remove-Item $dump, "$dump.err" -Force -ErrorAction SilentlyContinue
    Stop-Merge 3 'Файл не читается (повреждён или не медиа). Слияние отменено.' $null
}
try {
    $meta = (Get-Content $dump -Raw | ConvertFrom-Json)
} catch {
    Remove-Item $dump, "$dump.err" -Force -ErrorAction SilentlyContinue
    Stop-Merge 3 'Файл не читается (повреждён или не медиа). Слияние отменено.' $null
}
Remove-Item $dump, "$dump.err" -Force -ErrorAction SilentlyContinue

$audio = @($meta.streams | Where-Object { $_.codec_type -eq 'audio' })
$video = @($meta.streams | Where-Object { $_.codec_type -eq 'video' })

if ($audio.Count -eq 0) {
    Stop-Merge 3 'В файле нет аудиодорожек - нечего объединять.' $null
}
if ($audio.Count -eq 1) {
    Stop-Merge 3 'В файле только одна аудиодорожка - объединять нечего. Нужны ровно две.' $null
}
if ($audio.Count -ne 2) {
    Stop-Merge 3 ("В файле {0} аудиодорожек. Контракт Merge Audio: ровно две. Слияние 3+ дорожек не поддерживается." -f $audio.Count) $null
}
if ($video.Count -eq 0) {
    Stop-Merge 3 'Файл без видеодорожки не поддерживается контрактом Merge Audio.' $null
}

# ---- unique scratch output (same dir keeps the final move on one volume) --
$guid = [guid]::NewGuid().ToString('N')
$inDir = Split-Path -Parent $in
$inExt = [IO.Path]::GetExtension($in)
$scratch = Join-Path $inDir ("_merge_{0}{1}" -f $guid, $inExt)

# map: both audio -> amerge -> stereo; video/subs/data/attachments copied verbatim
$argList = @('-hide_banner', '-nostdin', '-y', '-i', $in,
    '-filter_complex', '[0:a:0][0:a:1]amerge=inputs=2,aformat=channel_layouts=stereo[aout]',
    '-map', '0:v?', '-map', '0:s?', '-map', '0:d?', '-map', '0:t?', '-map', '[aout]',
    '-map_metadata', '0',
    '-c:v', 'copy', '-c:s', 'copy', '-c:d', 'copy', '-c:t', 'copy',
    '-c:a', 'aac', '-b:a', '320k', $scratch)
$proc = Start-Process -FilePath $FFmpeg -ArgumentList $argList -NoNewWindow -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    Stop-Merge 1 'Объединение не удалось. Исходный файл не изменён.' $scratch
}

# ---- validate the scratch BEFORE the source may be replaced ---------------
$dump2 = Join-Path $env:TEMP ("ma_probe_{0}.json" -f [guid]::NewGuid().ToString('N'))
$pv = Start-Process -FilePath $FFprobe -ArgumentList @(
    '-v', 'error', '-show_entries', 'stream=codec_type,channels',
    '-show_entries', 'format=duration', '-of', 'json', $scratch) `
    -NoNewWindow -Wait -PassThru -RedirectStandardOutput $dump2 -RedirectStandardError "$dump2.err"
if ($pv.ExitCode -ne 0) {
    Remove-Item $dump2, "$dump2.err" -Force -ErrorAction SilentlyContinue
    Stop-Merge 1 'Результат не проходит проверку ffprobe. Исходный файл не изменён.' $scratch
}
try { $outMeta = Get-Content $dump2 -Raw | ConvertFrom-Json } catch {
    Remove-Item $dump2, "$dump2.err" -Force -ErrorAction SilentlyContinue
    Stop-Merge 1 'Результат не проходит проверку ffprobe. Исходный файл не изменён.' $scratch
}
Remove-Item $dump2, "$dump2.err" -Force -ErrorAction SilentlyContinue
$outAudio = @($outMeta.streams | Where-Object { $_.codec_type -eq 'audio' })
$duration = 0.0
if ($outMeta.format -and $outMeta.format.duration) { $duration = [double]$outMeta.format.duration }
if ($outAudio.Count -ne 1 -or [int]$outAudio[0].channels -ne 2 -or $duration -le 0) {
    Stop-Merge 1 'Результат не содержит одну стереодорожку. Исходный файл не изменён.' $scratch
}

# ---- only now replace the source, by this run's own output ----------------
try {
    Move-Item -LiteralPath $scratch -Destination $in -Force -ErrorAction Stop
} catch {
    Stop-Merge 4 ("Не удалось заменить исходный файл. Результат остаётся на диске: {0}" -f $scratch) $null
}
Write-Output 'Готово: две аудиодорожки объединены в одну.'
exit 0
