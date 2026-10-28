<#
    Unit harness for the pure SAIOPT logic.

    Compiles Scripts\saiopt\OptLogic.cs together with
    tests\saiopt_logic_tests.cs and runs the result. No desktop, no process
    is sampled or changed: every assertion is deterministic and CI-safe.
#>
param(
    [string]$Csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
)

$ErrorActionPreference = 'Stop'
$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $testsDir
$logic = Join-Path $root 'Scripts\saiopt\OptLogic.cs'
$suite = Join-Path $testsDir 'saiopt_logic_tests.cs'

foreach ($f in @($logic, $suite)) {
    if (-not (Test-Path -LiteralPath $f)) { Write-Host "FAIL  missing source $f"; exit 1 }
}
if (-not (Test-Path -LiteralPath $Csc)) { Write-Host "FAIL  csc.exe not found: $Csc"; exit 1 }

$work = Join-Path ([IO.Path]::GetTempPath()) ("saiopt_logic_" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
try {
    $exe = Join-Path $work 'saiopt_logic_tests.exe'
    & $Csc -nologo -target:exe -out:"$exe" -optimize+ -r:System.dll -r:System.Core.dll "$logic" "$suite"
    if ($LASTEXITCODE -ne 0) { Write-Host "FAIL  csc exit=$LASTEXITCODE"; exit 1 }

    & $exe
    $code = $LASTEXITCODE
    if ($code -ne 0) { Write-Host "FAIL  logic suite exit=$code"; exit $code }
    Write-Host 'SAIOPT_LOGIC_GREEN=TRUE'
    exit 0
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
