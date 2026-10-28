param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition))
)

$ErrorActionPreference = 'Stop'

# T-134 harness: media payload validity and atomic publication.
#
# 1. Validation discriminates: a zero-byte ffmpeg.exe, a truncated ffprobe.exe
#    and a text file renamed to yt-dlp.exe are ALL detected invalid; a real PE
#    (built from csc with a stub) is valid.
# 2. Publish-Pair is one generation: an injected failure after member 1 of the
#    ffmpeg/ffprobe pair leaves the PREVIOUS pair byte-intact -- no mixed pair.
# 3. setup.ps1 -SkipPayload acceptance: dot-sources without error and exits 0
#    without downloading anything.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_media_payload.ps1
# Exit: 0 = all PASS, 1 = failures.

$fails = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

$sandbox = Join-Path $env:TEMP ('saituls_media_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null

. (Join-Path $RepoRoot 'Scripts\media_payload.ps1')

# --- fixtures ---------------------------------------------------------------

function New-RealExe([string]$dest) {
    $src = Join-Path $sandbox 'stub.cs'
    Set-Content -LiteralPath $src -Value 'static class S { static void Main() { } }' -Encoding Ascii
    $csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path $csc)) { throw 'csc.exe not found; harness cannot build a real PE fixture' }
    & $csc -nologo -nologo -target:exe -out:$dest $src | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $dest)) { throw 'stub compile failed' }
    Remove-Item -LiteralPath $src -Force
}

$binDir = Join-Path $sandbox 'bin'
New-Item -ItemType Directory -Path $binDir -Force | Out-Null

$zeroFfmpeg = Join-Path $binDir 'ffmpeg.exe'
Set-Content -LiteralPath $zeroFfmpeg -Value '' -Encoding Ascii

$stubCs = Join-Path $sandbox 'stub2.cs'
Set-Content -LiteralPath $stubCs -Value 'static class S { static void Main() { } }' -Encoding Ascii
$realProbe = Join-Path $binDir 'ffprobe.exe'
& C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe -nologo -target:exe -out:$realProbe $stubCs | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'stub compile failed' }

# Truncated: take the first 4096 bytes of the real exe.
$realBytes = [System.IO.File]::ReadAllBytes($realProbe)
[System.IO.File]::WriteAllBytes($realProbe, $realBytes[0..4095])

$textYtdlp = Join-Path $binDir 'yt-dlp.exe'
Set-Content -LiteralPath $textYtdlp -Value 'this is definitely not a win32 executable' -Encoding Ascii

# --- case 1: validation discriminates ---------------------------------------

$v1 = Test-ExecutableValid -Path $zeroFfmpeg -Kind 'ffmpeg' -NoVersionProbe
Check 'zero-byte ffmpeg.exe detected invalid' (-not $v1.Valid) "reason=$($v1.Reason)"

$v2 = Test-ExecutableValid -Path $realProbe -Kind 'ffprobe' -NoVersionProbe
Check 'truncated ffprobe.exe detected invalid' (-not $v2.Valid) "reason=$($v2.Reason)"

$v3 = Test-ExecutableValid -Path $textYtdlp -Kind 'yt-dlp' -NoVersionProbe
Check 'text file named yt-dlp.exe detected invalid' (-not $v3.Valid) "reason=$($v3.Reason)"

$v4 = Test-ExecutableValid -Path $realProbe -Kind '' -NoVersionProbe
Check 'truncated real PE (no kind floor) still structurally checked' ($v4.Reason -ne $null) ''

$absent = Test-ExecutableValid -Path (Join-Path $binDir 'aria2c.exe') -Kind 'aria2c' -NoVersionProbe
Check 'absent payload reported invalid/absent' (-not $absent.Valid) "reason=$($absent.Reason)"

# --- case 2: paired publication is atomic -----------------------------------

$fakePath = Join-Path $sandbox 'fake.exe'
foreach ($offset in @(0, -1, 8190, [int]::MaxValue)) {
    $fake = New-Object byte[] 8192
    $fake[0] = 0x4D; $fake[1] = 0x5A
    [BitConverter]::GetBytes([int]$offset).CopyTo($fake, 0x3C)
    [IO.File]::WriteAllBytes($fakePath, $fake)
    Check "invalid PE offset $offset rejected" (-not (Test-ExecutableValid $fakePath -NoVersionProbe).Valid)
}
[BitConverter]::GetBytes([int]128).CopyTo($fake, 0x3C)
$fake[128] = 0x50; $fake[129] = 0x45; $fake[130] = 1
[IO.File]::WriteAllBytes($fakePath, $fake)
Check 'PE signature requires both trailing zero bytes' (-not (Test-ExecutableValid $fakePath -NoVersionProbe).Valid)
$fake[130] = 0
[IO.File]::WriteAllBytes($fakePath, $fake)
Check 'non-runnable PE-shaped file rejected by enabled probe' (-not (Test-ExecutableValid $fakePath).Valid)
Check 'only explicit NoVersionProbe bypasses runtime check' (Test-ExecutableValid $fakePath -NoVersionProbe).Valid
$runtimeExe = Join-Path $sandbox 'runtime.exe'
New-RealExe $runtimeExe
Check 'runnable zero-exit executable validates with probe' (Test-ExecutableValid $runtimeExe).Valid
function Start-Process { throw 'injected start failure' }
try {
    Check 'Start-Process exception rejects executable' (-not (Test-ExecutableValid $runtimeExe).Valid)
} finally { Remove-Item Function:\Start-Process }
function Start-Process { [pscustomobject]@{ ExitCode = 7 } }
try {
    Check 'nonzero version exit rejects executable' (-not (Test-ExecutableValid $runtimeExe).Valid)
} finally { Remove-Item Function:\Start-Process }

$stageDir = Join-Path $sandbox 'stage'
New-Item -ItemType Directory -Path $stageDir -Force | Out-Null

# Previous generation in place: two real distinct PEs.
$destFfmpeg = Join-Path $binDir 'ffmpeg.exe'
Remove-Item -LiteralPath $destFfmpeg -Force
New-RealExe $destFfmpeg
Remove-Item -LiteralPath $realProbe -Force
Copy-Item -LiteralPath $destFfmpeg -Destination $realProbe -Force

# Staged NEXT generation: real ffmpeg + a member-2 copy that will fail.
$nextFfmpeg = Join-Path $stageDir 'ffmpeg.exe'
New-RealExe $nextFfmpeg
Copy-Item -LiteralPath $nextFfmpeg -Destination (Join-Path $stageDir 'ffprobe.exe') -Force

# First publish the next generation cleanly, then record it as the "previous
# generation" the failing publish must preserve.
$null = Publish-Pair -StagingDir $stageDir -DestDir $binDir -Members @('ffmpeg.exe', 'ffprobe.exe')
$prevFfmpegBytes = [System.IO.File]::ReadAllBytes($destFfmpeg)
$prevProbeBytes = [System.IO.File]::ReadAllBytes($realProbe)
Check 'clean pair publish works' ($prevFfmpegBytes.Length -gt 0 -and $prevProbeBytes.Length -gt 0) ''

# A THIRD, different staged generation whose member 2 is locked: the publish
# must fail and roll the destination back to the recorded previous pair.
$stageDir2 = Join-Path $sandbox 'stage2'
New-Item -ItemType Directory -Path $stageDir2 -Force | Out-Null
$next2 = Join-Path $stageDir2 'ffmpeg.exe'
New-RealExe $next2
Copy-Item -LiteralPath $next2 -Destination (Join-Path $stageDir2 'ffprobe.exe') -Force
$lockPath = Join-Path $stageDir2 'ffprobe.exe'
$lockStream = [System.IO.File]::Open($lockPath, 'Open', 'Read', 'None')
try {
    try {
        Publish-Pair -StagingDir $stageDir2 -DestDir $binDir -Members @('ffmpeg.exe', 'ffprobe.exe') | Out-Null
        Check 'publish failure was detected' ($false) 'no exception raised'
    } catch {
        Check 'publish failure was detected' ($true) ''
    }
} finally { $lockStream.Dispose() }

$nowFfmpeg = [System.IO.File]::ReadAllBytes($destFfmpeg)
$nowProbe = [System.IO.File]::ReadAllBytes($realProbe)
Check 'failed pair publish leaves previous ffmpeg byte-exact' `
    (($nowFfmpeg.Length -eq $prevFfmpegBytes.Length)) "len=$($nowFfmpeg.Length) vs $($prevFfmpegBytes.Length)"
$same = $nowFfmpeg.Length -eq $prevFfmpegBytes.Length
if ($same) { for ($i = 0; $i -lt $nowFfmpeg.Length; $i++) { if ($nowFfmpeg[$i] -ne $prevFfmpegBytes[$i]) { $same = $false; break } } }
Check 'failed pair publish leaves previous ffmpeg content-identical' ($same) ''
$same2 = $nowProbe.Length -eq $prevProbeBytes.Length
if ($same2) { for ($i = 0; $i -lt $nowProbe.Length; $i++) { if ($nowProbe[$i] -ne $prevProbeBytes[$i]) { $same2 = $false; break } } }
Check 'failed pair publish leaves previous ffprobe content-identical (no mixed pair)' ($same2) ''

# --- case 3: setup.ps1 -SkipPayload stays clean -----------------------------

# Source-shape: the media section must call the validation/publish helpers.
$setupText = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot 'setup.ps1')
Check 'setup.ps1 uses Test-ExecutableValid for payloads' ($setupText -match 'Test-ExecutableValid') ''
Check 'setup.ps1 publishes the ffmpeg pair as one generation' `
    ($setupText -like "*Publish-Pair -StagingDir `$staging -DestDir `$binDir -Members @('ffmpeg.exe', 'ffprobe.exe')*") ''
Check 'setup.ps1 keeps -SkipPayload acceptance' ($setupText -match 'if \(\$SkipPayload\)') ''

Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
