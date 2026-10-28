<#
    Builds TaskbarEdge.exe from the two sources in this folder.

    winexe on purpose: the watcher runs for the whole session and must never
    show a console window. The diagnostic modes attach to the parent console
    (or write to a redirected handle) instead.
#>
param(
    [string]$Csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe',
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $OutputPath) { $OutputPath = Join-Path $here 'TaskbarEdge.exe' }

if (-not (Test-Path -LiteralPath $Csc)) {
    Write-Error "csc.exe not found: $Csc"
    exit 1
}

$sources = @(
    (Join-Path $here 'EdgeLogic.cs'),
    (Join-Path $here 'TaskbarEdge.cs')
)
foreach ($s in $sources) {
    if (-not (Test-Path -LiteralPath $s)) { Write-Error "missing source: $s"; exit 1 }
}

# The helper has no window, but Task Manager and the Run key still show an
# icon. Borrow the toolkit's when it is next to us.
$icon = Join-Path (Split-Path -Parent (Split-Path -Parent $here)) 'SAITULS.ico'
$iconArg = @()
if (Test-Path -LiteralPath $icon) { $iconArg = @("-win32icon:$icon") }

& $Csc -nologo -target:winexe -out:"$OutputPath" -optimize+ -warnaserror- `
    -r:System.dll @iconArg @sources
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host "BUILD FAIL csc exit=$code"
    exit $code
}
Write-Host "BUILD OK $OutputPath"
exit 0
