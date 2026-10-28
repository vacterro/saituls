param([string]$Out = (Join-Path $PSScriptRoot '..\.saipen\evidence\T-266-ux\product-normalization'))
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$scratch = Join-Path $env:TEMP ('saituls-normalization-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $scratch 'i18n') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $root 'i18n\strings') -Destination (Join-Path $scratch 'i18n\strings') -Recurse
$mods = @(Get-ChildItem -LiteralPath (Join-Path $root 'FolderAutomation') -Filter '*.cs' -File | Sort-Object Name | ForEach-Object FullName)
$exe = Join-Path $scratch 'normalization-test.exe'
try {
    & "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" -nologo -target:exe "-out:$exe" -main:ProductNormalizationTest -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll -r:System.Web.Extensions.dll -r:System.Core.dll (Join-Path $root 'SAITULS.cs') @mods (Join-Path $PSScriptRoot 'product_normalization_test.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Normalization regression did not compile' }
    & $exe ([IO.Path]::GetFullPath($Out))
    $result = $LASTEXITCODE
} finally {
    $absoluteScratch = [IO.Path]::GetFullPath($scratch)
    $tempPrefix = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    if (-not $absoluteScratch.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($absoluteScratch) -notlike 'saituls-normalization-*') { throw 'Scratch cleanup target is outside owned directory' }
    Remove-Item -LiteralPath $absoluteScratch -Recurse -Force
}
exit $result
