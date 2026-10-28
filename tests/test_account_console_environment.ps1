param([string]$SourcePath = '', [string]$OutDir = '')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
if(-not $SourcePath){$SourcePath=Join-Path $root 'Scripts\accounts\SaiAccounts.cs'}
if(-not $OutDir){$OutDir=Join-Path $root 'tests\bin\console-environment'}
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$csc=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if(-not (Test-Path -LiteralPath $csc)){throw 'The real compiler is required'}
$exe=Join-Path $OutDir 'console-environment-test.exe'
& $csc -nologo "-out:$exe" -main:AccountConsoleEnvironmentTest -r:System.dll -r:System.Core.dll -r:System.Web.Extensions.dll $SourcePath (Join-Path $root 'tests\account_console_environment_test.cs')
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
& $exe
exit $LASTEXITCODE
