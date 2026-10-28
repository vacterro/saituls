param(
    [string]$CommandPath = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Scripts\NEW_PROJ.CMD')
)

$ErrorActionPreference = 'Stop'
$failures = 0
$required = @('ae', 'c4d', '_output', '_input')

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Host "PASS  $Name $Detail" }
    else { Write-Host "FAIL  $Name $Detail"; $script:failures++ }
}

function New-Target {
    # The bang and space are load-bearing: paths like these break naive CMD expansion.
    $dir = Join-Path ([IO.Path]::GetTempPath()) ("newproj !dir " + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    return $dir
}

function Invoke-NewProj([string]$Target) {
    $out = & cmd.exe /d /c "call `"$CommandPath`" `"$Target`" <NUL 2>&1"
    return [pscustomobject]@{ Code = $LASTEXITCODE; Out = ($out -join "`n") }
}

function Test-Skeleton([string]$Target) {
    foreach ($name in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $Target "_new_project\$name") -PathType Container)) { return $false }
    }
    return $true
}

# Happy path: a complete skeleton and success.
$t = New-Target
try {
    $r = Invoke-NewProj $t
    Check 'empty target gets the full four-folder skeleton' ($r.Code -eq 0 -and (Test-Skeleton $t)) "exit=$($r.Code)"
} finally { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue }

# Idempotent rerun over a complete skeleton preserves content and returns 0.
$t = New-Target
try {
    Invoke-NewProj $t | Out-Null
    $keep = Join-Path $t '_new_project\_input\keep.txt'
    Set-Content -LiteralPath $keep -Value 'keep' -NoNewline
    $r = Invoke-NewProj $t
    $intact = (Test-Path -LiteralPath $keep) -and (Get-Content -LiteralPath $keep -Raw) -eq 'keep'
    Check 'rerun over a complete skeleton stays idempotent' ($r.Code -eq 0 -and (Test-Skeleton $t) -and $intact) "exit=$($r.Code)"
} finally { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue }

# Per-folder failure injection: a file where a required folder belongs.
foreach ($name in $required) {
    $t = New-Target
    try {
        $root = Join-Path $t '_new_project'
        New-Item -ItemType Directory -Path $root -Force | Out-Null
        $blocker = Join-Path $root $name
        Set-Content -LiteralPath $blocker -Value 'pre-existing' -NoNewline
        $r = Invoke-NewProj $t
        $preserved = (Test-Path -LiteralPath $blocker -PathType Leaf) -and (Get-Content -LiteralPath $blocker -Raw) -eq 'pre-existing'
        $leftovers = @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue)
        Check "blocked $name folder fails without overwriting or leaving a partial skeleton" `
            ($r.Code -ne 0 -and $preserved -and $leftovers.Count -eq 0) "exit=$($r.Code) leftovers=$($leftovers.Count)"
    } finally { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue }
}

# Rollback removes only what this run created.
$t = New-Target
try {
    $root = Join-Path $t '_new_project'
    New-Item -ItemType Directory -Path (Join-Path $root 'ae') -Force | Out-Null
    $mine = Join-Path $root 'ae\mine.txt'
    Set-Content -LiteralPath $mine -Value 'mine' -NoNewline
    Set-Content -LiteralPath (Join-Path $root 'c4d') -Value 'blocker' -NoNewline
    $r = Invoke-NewProj $t
    $kept = (Test-Path -LiteralPath $mine) -and (Get-Content -LiteralPath $mine -Raw) -eq 'mine'
    $created = @('_output', '_input') | Where-Object { Test-Path -LiteralPath (Join-Path $root $_) }
    Check 'rollback keeps pre-existing folders and drops only self-created ones' `
        ($r.Code -ne 0 -and $kept -and $created.Count -eq 0) "exit=$($r.Code) leftovers=$($created.Count)"
} finally { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue }

# The skeleton root itself blocked by a file.
$t = New-Target
try {
    $root = Join-Path $t '_new_project'
    Set-Content -LiteralPath $root -Value 'not a folder' -NoNewline
    $r = Invoke-NewProj $t
    $preserved = (Test-Path -LiteralPath $root -PathType Leaf) -and (Get-Content -LiteralPath $root -Raw) -eq 'not a folder'
    Check 'file at the skeleton root fails without replacing it' ($r.Code -ne 0 -and $preserved) "exit=$($r.Code)"
} finally { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue }

# Argument guards.
$missing = Join-Path ([IO.Path]::GetTempPath()) ("newproj_absent_" + [Guid]::NewGuid().ToString('N'))
$r = Invoke-NewProj $missing
Check 'missing target folder is rejected' ($r.Code -ne 0) "exit=$($r.Code)"
Check 'target folder is never conjured up' (-not (Test-Path -LiteralPath $missing))

$r = Invoke-NewProj ''
Check 'empty argument is rejected' ($r.Code -ne 0) "exit=$($r.Code)"

Write-Host '---'
if ($failures) { Write-Host "FAILED ($failures failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
