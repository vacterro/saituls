param([switch]$Uninstall)

$installer = Join-Path $PSScriptRoot "Installers\INSTALL_AI_AGENT_MENUS.PS1"
& $installer -Agent OpenCode -Uninstall:$Uninstall
if (-not $?) { exit 1 }
