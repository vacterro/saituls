<#
    Unit harness for the pure TaskbarEdge logic.

    Compiles Scripts\taskbar_edge\EdgeLogic.cs together with
    tests\taskbar_edge_logic_tests.cs and runs the result. No desktop, no
    shell, no cursor: every assertion is deterministic and CI-safe.
#>
param(
    [string]$Csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
)

$ErrorActionPreference = 'Stop'
$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $testsDir
$logic = Join-Path $root 'Scripts\taskbar_edge\EdgeLogic.cs'
$suite = Join-Path $testsDir 'taskbar_edge_logic_tests.cs'

foreach ($f in @($logic, $suite)) {
    if (-not (Test-Path -LiteralPath $f)) { Write-Host "FAIL  missing source $f"; exit 1 }
}
if (-not (Test-Path -LiteralPath $Csc)) { Write-Host "FAIL  csc.exe not found: $Csc"; exit 1 }

$work = Join-Path ([IO.Path]::GetTempPath()) ("taskbar_edge_logic_" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
try {
    $exe = Join-Path $work 'taskbar_edge_logic_tests.exe'
    & $Csc -nologo -target:exe -out:"$exe" -optimize+ -r:System.dll "$logic" "$suite"
    if ($LASTEXITCODE -ne 0) { Write-Host "FAIL  csc exit=$LASTEXITCODE"; exit 1 }

    & $exe
    $code = $LASTEXITCODE
    if ($code -ne 0) { Write-Host "FAIL  logic suite exit=$code"; exit $code }
    Write-Host 'TASKBAR_EDGE_LOGIC_GREEN=TRUE'
    exit 0
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
