# Accounts runtime UX: compile the fake engine + the flow harness, run it.
#
# Everything here is hermetic. The harness points SAI_ACCOUNTS_EXE at a
# stand-in engine and asserts what SAITULS actually sends it, so no real
# account is touched and no context is ever started.
param([switch]$Reliability, [ValidateRange(1, 20)][int]$Repeat = 1)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$bin = Join-Path $root 'tests\bin'
if (-not (Test-Path $bin)) { New-Item -ItemType Directory -Path $bin | Out-Null }

$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe" }
if (-not (Test-Path $csc)) { Write-Host 'FAIL  csc.exe not found'; exit 1 }

$refs = @('-r:System.dll', '-r:System.Drawing.dll', '-r:System.Windows.Forms.dll',
          '-r:System.Web.Extensions.dll', '-r:System.Core.dll')

Write-Host '--- building fake engine ---'
& $csc -nologo -target:exe -out:"$bin\fake-sai-accounts.exe" -optimize+ @refs `
    (Join-Path $root 'tests\fake_sai_accounts\FakeAccountsEngine.cs')
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL  fake engine did not compile'; exit 1 }

# SAITULS is SAITULS.cs plus the FolderAutomation module; compiling the single
# file alone left the Saituls.FolderAutomation types undefined.
$module = @(Get-ChildItem -LiteralPath (Join-Path $root 'FolderAutomation') -Filter '*.cs' -File |
    Sort-Object Name | ForEach-Object { $_.FullName })
if ($module.Count -eq 0) { Write-Host 'FAIL  FolderAutomation module is missing'; exit 1 }

Write-Host '--- building Accounts flow harness ---'
& $csc -nologo -target:exe -out:"$bin\saituls-accounts-flow-test.exe" -optimize+ @refs `
    "-main:AccountsFlowTest" (Join-Path $root 'SAITULS.cs') @module `
    (Join-Path $root 'tests\saituls_accounts_flow_test.cs')
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL  harness did not compile'; exit 1 }

Write-Host '--- building identity projection test ---'
& $csc -nologo -target:exe -out:"$bin\identity-projection-test.exe" -optimize+ @refs `
    "-main:IdentityProjectionTest" (Join-Path $root 'Scripts\accounts\SaiAccounts.cs') `
    (Join-Path $root 'tests\identity_projection_test.cs')
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL  identity projection test did not compile'; exit 1 }

Write-Host '--- running ---'
& "$bin\identity-projection-test.exe"
if ($LASTEXITCODE -ne 0) { exit 1 }
$code = 0
for ($iteration = 1; $iteration -le $Repeat; $iteration++) {
    if ($Reliability) {
        & "$bin\saituls-accounts-flow-test.exe" --offline-start-and-switch
    } else {
        & "$bin\saituls-accounts-flow-test.exe"
    }
    $code = $LASTEXITCODE
    if ($code -ne 0) { break }
    if ($Repeat -gt 1) { Write-Host "RUNTIME_REPETITION=$iteration/$Repeat PASS" }
}

# The fake never survives its run; a wedged one would hold the harness open.
Get-Process -Name 'fake-sai-accounts' -ErrorAction SilentlyContinue | Stop-Process -Force
exit $code
