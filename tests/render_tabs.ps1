# Offscreen render of every tab (see tests\render_tabs.cs). Read-only.
param([string]$Out = (Join-Path $env:TEMP 'saituls-render'), [string[]]$Sizes = @('600x460', '720x580', '1000x700'), [string]$Limisaw = '', [string]$Lang = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$bin = Join-Path $root 'tests\bin'
if (-not (Test-Path $bin)) { New-Item -ItemType Directory -Path $bin | Out-Null }
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$mods = @(Get-ChildItem -LiteralPath (Join-Path $root 'FolderAutomation') -Filter '*.cs' -File | Sort-Object Name | ForEach-Object { $_.FullName })
$exe = Join-Path $bin 'render-tabs.exe'
& $csc -nologo -target:exe "-out:$exe" -main:RenderTabs -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll -r:System.Web.Extensions.dll -r:System.Core.dll (Join-Path $root 'SAITULS.cs') @mods (Join-Path $root 'tests\render_tabs.cs')
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL  render harness did not compile'; exit 1 }
if ($Limisaw) { $env:RENDER_LIMISAW = $Limisaw }
if ($Lang) { $env:RENDER_LANG = $Lang }
& $exe $root $Out @Sizes
exit $LASTEXITCODE
