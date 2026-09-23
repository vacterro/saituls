$installer = Join-Path $PSScriptRoot "Installers\INSTALL_CONSOLES_MENU.PS1"
& $installer -Uninstall
if (-not $?) { exit 1 }
