param([switch]$Uninstall, [switch]$KeepLegacy)

# One Explorer cascade for every agent console in Scripts\consoles\consoles.json.
$installer = Join-Path $PSScriptRoot "Installers\INSTALL_CONSOLES_MENU.PS1"
& $installer -Uninstall:$Uninstall -RemoveLegacy:(-not $KeepLegacy)
if (-not $?) { exit 1 }
