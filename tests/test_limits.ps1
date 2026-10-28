# T-266/T-267: AI limits model, 5h keeper, settings, language, refresh cost.
# Hermetic (stub engine, scratch settings). See tests\limits_unit_test.cs.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$bin = Join-Path $root 'tests\bin'
if (-not (Test-Path $bin)) { New-Item -ItemType Directory -Path $bin | Out-Null }
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe" }
$mods = @(Get-ChildItem -LiteralPath (Join-Path $root 'FolderAutomation') -Filter '*.cs' -File | Sort-Object Name | ForEach-Object { $_.FullName })
$exe = Join-Path $bin 'limits-unit-test.exe'
& $csc -nologo -target:exe "-out:$exe" -main:LimitsUnitTest -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll -r:System.Web.Extensions.dll -r:System.Core.dll (Join-Path $root 'SAITULS.cs') @mods (Join-Path $root 'tests\limits_unit_test.cs')
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL  limits test did not compile'; exit 1 }
& $exe
exit $LASTEXITCODE
