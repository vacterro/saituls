#Requires -RunAsAdministrator
# Adds the cascaded "Открыть Codex здесь" context menu entry with three
# sub-items (one per isolated Codex account).
#
# Cascade pattern: SubCommands="" (EMPTY string) + verb subkeys under
# <parent>\shell\ — Explorer auto-enumerates the children. This is the only
# cascade pattern that renders reliably on this machine (same pattern the
# toolkit's own FFmpeg/DL_YT menus use); an explicit SubCommands list
# produced an EMPTY submenu here.
#
# Writes to HKLM\SOFTWARE\Classes (Directory + Directory\Background shells).

param(
    # Where the per-account Codex launchers live. Defaults to a sibling
    # directory next to this toolkit; override for any other layout:
    #   .\Add-CodexContextMenus.ps1 -LauncherRoot "D:\my\codex\launchers"
    [string]$LauncherRoot = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSCommandPath)) "_AI_STUFF_AGENTIC")
)

$ErrorActionPreference = "Stop"

$ParentKey   = "CodexHere"
$ParentLabel = "Открыть Codex здесь"

$Items = @(
    @{ Verb = "CodexMain";         Label = "main_codex";       Script = Join-Path $LauncherRoot "main_codex\Start-Codex-Main.ps1" }
    @{ Verb = "CodexAccount2";     Label = "main_codex2";      Script = Join-Path $LauncherRoot "main_codex2\Start-Codex-Account2.ps1" }
    @{ Verb = "CodexAccount3Free"; Label = "main_codex3_free"; Script = Join-Path $LauncherRoot "main_codex3_free\Start-Codex-Account3-Free.ps1" }
)

foreach ($root in @("Directory", "Directory\Background")) {
    $base = "HKLM:\SOFTWARE\Classes\$root\shell\$ParentKey"

    if (Test-Path $base) { Remove-Item $base -Recurse -Force }
    New-Item -Path "$base\shell" -Force | Out-Null
    Set-ItemProperty -Path $base -Name "MUIVerb" -Value $ParentLabel
    Set-ItemProperty -Path $base -Name "SubCommands" -Value ""
    Set-ItemProperty -Path $base -Name "Icon" -Value "powershell.exe"

    foreach ($i in $Items) {
        if (-not (Test-Path -LiteralPath $i.Script)) {
            throw "Launcher script not found: $($i.Script)"
        }

        $verbKey = "$base\shell\$($i.Verb)"
        New-Item -Path "$verbKey\command" -Force | Out-Null
        Set-ItemProperty -Path $verbKey -Name "MUIVerb" -Value $i.Label
        Set-ItemProperty -Path $verbKey -Name "(default)" -Value $i.Label
        Set-ItemProperty -Path $verbKey -Name "HasLUAShield" -Value ""

        # Right-click on a folder icon -> %1 ; right-click on folder background -> %V
        $arg = if ($root -eq "Directory") { "%1" } else { "%V" }

        # Elevation wrapper: outer powershell spawns elevated inner via
        # Start-Process -Verb RunAs. Single -ArgumentList string with embedded
        # quotes keeps spaces in %1/%V intact through the two-hop chain.
        $inner = "-NoExit -ExecutionPolicy Bypass -File `"$($i.Script)`" -WorkDir `"$arg`""
        $command = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command " +
                   "`"Start-Process -Verb RunAs -FilePath 'powershell.exe' -ArgumentList \`"$inner\`"`""
        Set-ItemProperty -Path "$verbKey\command" -Name "(default)" -Value $command
    }
}

Write-Host ""
Write-Host "Done. Cascaded context menu installed (SubCommands='' pattern)."
