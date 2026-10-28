# T-265: managed console Ctrl-key contract + operator input-language mirror.
# Hermetic: the "managed hive" is a scratch HKCU key deleted by the test.
param([string]$HelperSource)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$bin = Join-Path $root 'tests\bin'
if (-not (Test-Path $bin)) { New-Item -ItemType Directory -Path $bin | Out-Null }
if (-not $HelperSource) { $HelperSource = Join-Path $root 'Scripts\accounts\SaiAccountsHelper.cs' }
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe" }
if (-not (Test-Path $csc)) { Write-Host 'FAIL  csc.exe not found'; exit 1 }
$out = Join-Path $bin 'console-profile-unit-test.exe'
& $csc -nologo -target:exe "-out:$out" -optimize+ -main:ConsoleProfileUnitTest `
    -r:System.dll -r:System.Core.dll -r:System.Security.dll -r:System.ServiceProcess.dll -r:System.Web.Extensions.dll `
    $HelperSource (Join-Path $root 'tests\console_profile_unit_test.cs')
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL  console profile test did not compile'; exit 1 }
& $out
exit $LASTEXITCODE
