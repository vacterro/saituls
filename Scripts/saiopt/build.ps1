<#
    Builds SAIOPT.exe from the sources in this folder.

    x64 on purpose: the sampler reads 64-bit SYSTEM_PROCESS_INFORMATION
    records. winexe on purpose: the resident never shows a console; the
    diagnostic modes attach to the parent console instead.
#>
param(
    [string]$Csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe',
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $OutputPath) { $OutputPath = Join-Path $here 'SAIOPT.exe' }

if (-not (Test-Path -LiteralPath $Csc)) {
    Write-Error "csc.exe not found: $Csc"
    exit 1
}

$sources = @('OptLogic.cs', 'SAIOPT.cs', 'SelfTest.cs') | ForEach-Object { Join-Path $here $_ }
foreach ($s in $sources) {
    if (-not (Test-Path -LiteralPath $s)) { Write-Error "missing source: $s"; exit 1 }
}

$icon = Join-Path (Split-Path -Parent (Split-Path -Parent $here)) 'SAITULS.ico'
$iconArg = @()
if (Test-Path -LiteralPath $icon) { $iconArg = @("-win32icon:$icon") }

& $Csc -nologo -target:winexe -platform:x64 -out:"$OutputPath" -optimize+ -warnaserror- `
    -r:System.dll -r:System.Core.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll -r:System.Management.dll `
    @iconArg @sources
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host "BUILD FAIL csc exit=$code"
    exit $code
}
Write-Host "BUILD OK $OutputPath"
exit 0
