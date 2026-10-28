param([string]$SourcePath = '', [string]$OutDir = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
if (-not $SourcePath) { $SourcePath = Join-Path $root 'SAITULS.cs' }
if (-not $OutDir) { $OutDir = Join-Path $root 'tests\bin\identity-contract' }
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $csc)) { throw 'csc.exe is required' }
$module = @(Get-ChildItem -LiteralPath (Join-Path $root 'FolderAutomation') -Filter '*.cs' -File | Sort-Object Name | ForEach-Object { $_.FullName })
if ($module.Count -eq 0) { throw 'FolderAutomation module is required' }
$exe = Join-Path $OutDir 'account-identity-contract.exe'
& $csc -nologo -target:exe "-out:$exe" -optimize+ -main:AccountIdentityContractTest `
    -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll -r:System.Web.Extensions.dll -r:System.Core.dll `
    $SourcePath @module (Join-Path $root 'tests\account_identity_contract_test.cs')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $exe
exit $LASTEXITCODE
