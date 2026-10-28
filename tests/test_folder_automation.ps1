param(
    [string]$Root = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)),
    [switch]$KeepScratch
)

$ErrorActionPreference = 'Stop'

# Folder Automation regression suite.
#
# Compiles the engine sources WITHOUT the WinForms dialogs (the engine is GUI
# free by design, so the whole matching/acting layer can be tested headless),
# runs tests\FolderAutomationTests.cs, and treats its exit code as the number
# of failures. The data directory is redirected to a scratch folder so a test
# run can never touch the real user configuration.

$faDir = Join-Path $Root 'FolderAutomation'
$sources = @(
    (Join-Path $faDir 'FolderAutomationConfig.cs')
    (Join-Path $faDir 'FolderAutomationSafety.cs')
    (Join-Path $faDir 'FolderAutomationRule.cs')
    (Join-Path $faDir 'FolderAutomationActions.cs')
    (Join-Path $faDir 'FolderAutomationJournal.cs')
    (Join-Path $faDir 'FolderAutomationScheduler.cs')
    (Join-Path $faDir 'FolderAutomationWatcher.cs')
    (Join-Path $faDir 'FolderAutomationEngine.cs')
    (Join-Path $Root 'tests\FolderAutomationTests.cs')
)

foreach ($s in $sources) {
    if (-not (Test-Path -LiteralPath $s)) { Write-Host "FAIL  missing source: $s"; exit 1 }
}

$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { Write-Host "FAIL  csc.exe not found at $csc"; exit 1 }

$work = Join-Path ([IO.Path]::GetTempPath()) ("saituls_fa_" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
$exe = Join-Path $work 'FolderAutomationTests.exe'

# csc is a native Windows program: forward slashes in arguments get mangled by
# both bash and PowerShell path conversion, so every path is normalised first.
$normalised = $sources | ForEach-Object { $_.Replace('/', '\') }

& $csc -nologo -target:exe -platform:x64 -optimize+ -warnaserror+ `
    ("-out:" + $exe.Replace('/', '\')) `
    -r:System.dll -r:System.Core.dll `
    @normalised
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL  the Folder Automation engine + tests do not compile'; exit 1 }

$env:SAITULS_FA_DIR = Join-Path $work 'data'
try {
    & $exe
    $code = $LASTEXITCODE
} finally {
    if (-not $KeepScratch) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host '---'
if ($code -ne 0) { Write-Host "FAILED ($code failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0