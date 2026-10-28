$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$failures = 0

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Host "PASS  $Name $Detail" }
    else { Write-Host "FAIL  $Name $Detail"; $script:failures++ }
}

function Invoke-Builder([string]$FixtureRoot, [string]$Archiver = '') {
    $previous = $env:SAITULS_PAYLOAD_ARCHIVER
    try {
        if ($Archiver) { $env:SAITULS_PAYLOAD_ARCHIVER = $Archiver }
        else { Remove-Item Env:SAITULS_PAYLOAD_ARCHIVER -ErrorAction SilentlyContinue }
        $output = & cmd.exe /d /c "`"$FixtureRoot\BUILD_PAYLOAD.cmd`"" 2>&1 | Out-String
        return @{ Code = $LASTEXITCODE; Output = $output }
    } finally {
        if ($null -eq $previous) { Remove-Item Env:SAITULS_PAYLOAD_ARCHIVER -ErrorAction SilentlyContinue }
        else { $env:SAITULS_PAYLOAD_ARCHIVER = $previous }
    }
}

$fixture = Join-Path ([IO.Path]::GetTempPath()) ("saituls-payload-test-" + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path (Join-Path $fixture 'Scripts') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $fixture 'Bin\App\AV1 CPU') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot 'BUILD_PAYLOAD.cmd') -Destination $fixture
    Copy-Item -LiteralPath (Join-Path $projectRoot 'Scripts\build_payload.ps1') -Destination (Join-Path $fixture 'Scripts')
    # Clean archiver resolution for this run: a Git Bash GNU tar earlier in
    # PATH misreads "V:\..." as a remote host, so the PATH inherited from a
    # dev shell must not decide which tar.exe the builder finds.
    $systemRoot = [Environment]::GetEnvironmentVariable('SystemRoot')
    if ($systemRoot) { $env:Path = "$systemRoot\System32;$env:Path" }
    Set-Content -LiteralPath (Join-Path $fixture 'VERSION') -Value '9.8.7' -Encoding ascii
    $releaseManifest = @(Get-Content -LiteralPath (Join-Path $projectRoot 'PAYLOAD_MANIFEST.txt') | Where-Object { $_ -and -not $_.StartsWith('#') })
    Check 'manifest pairs FFMPEG_RUN BAT with PS1' (-not ($releaseManifest -contains 'Bin/FFMPEG_RUN.BAT') -or $releaseManifest -contains 'Bin/FFMPEG_RUN.PS1')

    $manifest = @('Bin/FFMPEG_RUN.BAT', 'Bin/FFMPEG_RUN.PS1', 'Bin/FFMPEG.EXE', 'Bin/App/AV1 CPU/FFMPEG.EXE')
    Set-Content -LiteralPath (Join-Path $fixture 'PAYLOAD_MANIFEST.txt') -Value $manifest -Encoding utf8
    Set-Content -LiteralPath (Join-Path $fixture 'Bin\FFMPEG_RUN.BAT') -Value 'ffmpeg runner bat' -Encoding ascii
    Set-Content -LiteralPath (Join-Path $fixture 'Bin\FFMPEG_RUN.PS1') -Value 'ffmpeg runner ps1' -Encoding ascii
    Set-Content -LiteralPath (Join-Path $fixture 'Bin\FFMPEG.EXE') -Value 'root ffmpeg' -Encoding ascii
    Set-Content -LiteralPath (Join-Path $fixture 'Bin\App\AV1 CPU\FFMPEG.EXE') -Value 'av1 ffmpeg' -Encoding ascii

    $dist = Join-Path $fixture 'dist'
    New-Item -ItemType Directory -Path $dist | Out-Null
    $published = Join-Path $dist 'SAITULS-payload-9.8.7.zip'

    Set-Content -LiteralPath $published -Value 'old release' -NoNewline -Encoding ascii
    $oldHash = (Get-FileHash -LiteralPath $published -Algorithm SHA256).Hash
    Add-Content -LiteralPath (Join-Path $fixture 'PAYLOAD_MANIFEST.txt') -Value 'Bin/MISSING.EXE'
    $result = Invoke-Builder $fixture
    Check 'missing manifest member returns nonzero' ($result.Code -ne 0) $result.Output.Trim()
    Check 'missing member preserves prior release' ((Get-FileHash -LiteralPath $published -Algorithm SHA256).Hash -eq $oldHash)
    Set-Content -LiteralPath (Join-Path $fixture 'PAYLOAD_MANIFEST.txt') -Value $manifest -Encoding utf8

    $failArchiver = Join-Path $fixture 'fail-archiver.cmd'
    Set-Content -LiteralPath $failArchiver -Encoding ascii -Value @(
        '@echo off',
        '> "%~1" echo partial archive',
        'exit /b 23'
    )
    $result = Invoke-Builder $fixture $failArchiver
    Check 'nonzero archiver result is rejected' ($result.Code -ne 0 -and $result.Output -match 'exit code 23') $result.Output.Trim()
    Check 'partial archiver output preserves prior release' ((Get-FileHash -LiteralPath $published -Algorithm SHA256).Hash -eq $oldHash)

    $badArchiver = Join-Path $fixture 'bad-archive.cmd'
    Set-Content -LiteralPath $badArchiver -Encoding ascii -Value @(
        '@echo off',
        'tar.exe -a -c -f "%~1" -C "%~2" Bin\FFMPEG.EXE',
        'exit /b %errorlevel%'
    )
    $result = Invoke-Builder $fixture $badArchiver
    Check 'successful archiver with wrong members is rejected' ($result.Code -ne 0 -and $result.Output -match 'do not match') $result.Output.Trim()
    Check 'invalid archive preserves prior release' ((Get-FileHash -LiteralPath $published -Algorithm SHA256).Hash -eq $oldHash)

    $result = Invoke-Builder $fixture
    Check 'valid payload build succeeds' ($result.Code -eq 0) $result.Output.Trim()
    Check 'artifact name comes from VERSION' (Test-Path -LiteralPath $published -PathType Leaf)
    Check 'hardcoded 0.0.1 artifact is absent' (-not (Test-Path -LiteralPath (Join-Path $dist 'SAITULS-payload-0.0.1.zip')))

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($published)
    try {
        $actual = @($zip.Entries | Where-Object { -not $_.FullName.EndsWith('/') } | ForEach-Object { $_.FullName.Replace('\', '/') } | Sort-Object)
    } finally { $zip.Dispose() }
    Check 'published ZIP has exact manifest members' (($actual -join '|') -eq (($manifest | Sort-Object) -join '|')) ($actual -join ', ')
    Check 'published ZIP pairs FFMPEG_RUN BAT with PS1' (-not ($actual -contains 'Bin/FFMPEG_RUN.BAT') -or $actual -contains 'Bin/FFMPEG_RUN.PS1')
    Check 'temporary payload artifacts are cleaned' (@(Get-ChildItem -LiteralPath $dist -Filter '*.tmp.zip').Count -eq 0)
} finally {
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}

Write-Host '---'
if ($failures) { Write-Host "FAILED ($failures failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
