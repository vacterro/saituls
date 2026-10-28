# Compile in isolation and prove that only the exact output broker is stopped.
param([string]$BuildScript = '', [switch]$Deploy)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
if (-not $BuildScript) { $BuildScript = Join-Path $root 'Scripts\accounts\build.ps1' }
$fixtureRoot = Join-Path $env:TEMP ('sai-build-contract-' + [guid]::NewGuid().ToString('N'))
$outDir = Join-Path $fixtureRoot 'bin'
New-Item -ItemType Directory -Path $outDir -Force | Out-Null
$outputImage = Join-Path $outDir 'sai-accounts.exe'
$fixtureProgramData = Join-Path $fixtureRoot 'programdata'
$installedImage = Join-Path $fixtureProgramData 'SAI\Accounts\bin\sai-accounts.exe'
$script:stoppedIds = [System.Collections.Generic.List[int]]::new()
$script:processFixtures = @(
    [pscustomobject]@{ProcessId=501; ExecutablePath=$outputImage; CommandLine='sai-accounts.exe --broker synthetic scope'},
    [pscustomobject]@{ProcessId=502; ExecutablePath=$installedImage; CommandLine='sai-accounts.exe --broker synthetic scope'},
    [pscustomobject]@{ProcessId=503; ExecutablePath=(Join-Path $fixtureRoot 'other\sai-accounts.exe'); CommandLine='sai-accounts.exe --broker synthetic scope'},
    [pscustomobject]@{ProcessId=504; ExecutablePath=$outputImage; CommandLine='sai-accounts.exe console-title-guard'},
    [pscustomobject]@{ProcessId=505; ExecutablePath=$null; CommandLine='sai-accounts.exe --broker synthetic scope'},
    [pscustomobject]@{ProcessId=506; ExecutablePath=$installedImage; CommandLine='sai-accounts.exe console-title-guard'},
    [pscustomobject]@{ProcessId=$PID; ExecutablePath=$outputImage; CommandLine='sai-accounts.exe --broker synthetic scope'}
)

# These seams intercept only process discovery and termination in the build
# script. The real compiler and all three real sources are still exercised.
# No process named by a fixture can be terminated on the operator's machine.
$fixtureState = [pscustomobject]@{ Records=$script:processFixtures; Stops=$script:stoppedIds; Discoveries=0 }
Set-Item -Path Function:\Get-CimInstance -Value ({
    [CmdletBinding()] param([string]$ClassName, [string]$Filter)
    $fixtureState.Discoveries++
    $fixtureState.Records
}.GetNewClosure())
Set-Item -Path Function:\Stop-Process -Value ({
    [CmdletBinding()] param([int[]]$Id, [switch]$Force)
    foreach ($processId in $Id) { $fixtureState.Stops.Add($processId) }
}.GetNewClosure())

# A disposable ProgramData and absent SCM service isolate the real deployment
# copy/hash path from this machine's installed plane and service.
function Get-Service {
    [CmdletBinding()] param([string]$Name)
    return $null
}
$oldProgramData = $env:ProgramData
$env:ProgramData = $fixtureProgramData
$oldRoot = $env:SAI_ACCOUNTS_ROOT
$oldScope = $env:SAI_ACCOUNTS_SCOPE
$env:SAI_ACCOUNTS_ROOT = Join-Path $fixtureRoot 'plane'
$env:SAI_ACCOUNTS_SCOPE = 'op-build-contract'
$failures = 0
function Check([bool]$ok, [string]$what) {
    if ($ok) { Write-Host "PASS  $what" }
    else { Write-Host "FAIL  $what"; $script:failures++ }
}
try {
    & $BuildScript -OutDir $outDir -Deploy:$Deploy
    Check ($LASTEXITCODE -eq 0) 'the isolated engine/helper/shim build succeeds'
    $expectedDiscoveries = if ($Deploy) { 2 } else { 1 }
    Check ($fixtureState.Discoveries -eq $expectedDiscoveries) 'every build/deploy process selection inspected the isolated fixtures'
    $expectedStops = if ($Deploy) { '501,502' } else { '501' }
    Check (($script:stoppedIds -join ',') -eq $expectedStops) 'only brokers of the exact output/install images are stopped'
    Check ($script:stoppedIds.Contains(502) -eq [bool]$Deploy) 'installed brokers stop only for deployment'
    Check (-not $script:stoppedIds.Contains(503)) 'brokers from another isolated build survive'
    Check (-not $script:stoppedIds.Contains(504)) 'the account console title guard survives'
    Check (-not $script:stoppedIds.Contains(506)) 'the installed console title guard survives deployment'
    Check (-not $script:stoppedIds.Contains(505)) 'an unreadable executable path never grants ownership'
    Check (-not $script:stoppedIds.Contains($PID)) 'the caller cannot terminate itself'
    foreach ($name in @('sai-accounts.exe', 'sai-accounts-helper.exe', 'sai-antigravity.exe')) {
        Check (Test-Path -LiteralPath (Join-Path $outDir $name) -PathType Leaf) "real output exists: $name"
    }
    if ($Deploy) {
        $targets = @{
            'sai-accounts.exe' = 'SAI\Accounts\bin\sai-accounts.exe'
            'sai-accounts-helper.exe' = 'SAI\Accounts\bin\sai-accounts-helper.exe'
            'sai-antigravity.exe' = 'SAI\Antigravity\bin\sai-antigravity.exe'
        }
        foreach ($name in $targets.Keys) {
            $target = Join-Path $fixtureProgramData $targets[$name]
            $exists = Test-Path -LiteralPath $target -PathType Leaf
            Check $exists "isolated deployment exists: $name"
            if ($exists) {
                Check ((Get-FileHash -LiteralPath $target).Hash -eq
                    (Get-FileHash -LiteralPath (Join-Path $outDir $name)).Hash) "deployed bytes match the real build: $name"
            }
        }
    }
} finally {
    $env:ProgramData = $oldProgramData
    $env:SAI_ACCOUNTS_ROOT = $oldRoot
    $env:SAI_ACCOUNTS_SCOPE = $oldScope
    $resolvedFixture = [System.IO.Path]::GetFullPath($fixtureRoot)
    $resolvedTemp = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    if ((-not $resolvedFixture.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase)) -or
        [System.IO.Path]::GetFileName($resolvedFixture) -notlike 'sai-build-contract-*') {
        throw 'Refusing cleanup outside the owned build fixture'
    }
    Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
}
Write-Host "BUILD_SCOPE_FAIL=$failures"
if ($failures -gt 0) { exit 1 }
exit 0
