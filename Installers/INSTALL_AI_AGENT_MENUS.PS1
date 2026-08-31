[CmdletBinding()]
param(
    [ValidateSet("All", "OpenCode", "Cline")]
    [string]$Agent = "All",
    [switch]$Uninstall,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$rootDir = (Get-Item -LiteralPath $scriptDir).Parent.FullName
$launcher = Join-Path $rootDir "Scripts\AI_AGENT_LAUNCHER.PS1"
$powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$classesRoot = "HKCU:\Software\Classes"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

$definitions = @{
    OpenCode = @{
        Key = "OpenOpenCode"
        Label = "OpenCode YOLO here"
        Command = "opencode.cmd"
    }
    Cline = @{
        Key = "OpenCline"
        Label = "Cline YOLO here"
        Command = "cline.cmd"
    }
}

$selectedAgents = if ($Agent -eq "All") { @("OpenCode", "Cline") } else { @($Agent) }
$shellTargets = @(
    @{ Root = "Directory"; Argument = "%1" },
    @{ Root = "Directory\Background"; Argument = "%V" },
    @{ Root = "Drive"; Argument = "%1" }
)

function Set-Dword([string]$Path, [string]$Name, [int]$Value) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -Path $Path -Force | Out-Null
    }
    New-ItemProperty -Path $Path -Name $Name -Value $Value -PropertyType DWord -Force | Out-Null
}

function Set-String([string]$Path, [string]$Name, [string]$Value) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -Path $Path -Force | Out-Null
    }
    Set-ItemProperty -Path $Path -Name $Name -Value $Value
}

function Install-WintageConsoleProfile {
    # Golden Default, encoded as Win32 COLORREF (0x00BBGGRR).
    $palette = @(
        0x10181A, 0x4C4C00, 0x207A4A, 0x808000,
        0x20207A, 0x303D45, 0x207A7A, 0x71939C,
        0x40505A, 0x60D0F0, 0x207A4A, 0x808000,
        0x6464D6, 0x303D45, 0x9AC8D4, 0x9AC8D4
    )
    $consoleKeys = @(
        "HKCU:\Console",
        "HKCU:\Console\%SystemRoot%_System32_WindowsPowerShell_v1.0_powershell.exe"
    )

    foreach ($key in $consoleKeys) {
        Set-Dword $key "ForceV2" 1
        Set-Dword $key "CtrlKeyShortcutsDisabled" 0
        Set-Dword $key "ExtendedEditKey" 1
        Set-Dword $key "QuickEdit" 1
        Set-Dword $key "InsertMode" 1
        Set-Dword $key "FilterOnPaste" 1
        Set-Dword $key "InterceptCopyPaste" 1
        Set-Dword $key "LineSelection" 1
        Set-Dword $key "VirtualTerminalLevel" 1
        Set-Dword $key "ScreenColors" 15
        Set-Dword $key "PopupColors" 240
        Set-Dword $key "WindowAlpha" 255
        Set-Dword $key "FontFamily" 54
        Set-Dword $key "FontSize" 1048576
        Set-Dword $key "FontWeight" 400
        Set-String $key "FaceName" "Terminus (TTF) for Windows"
        Set-String $key "FontFace" "Verdana"
        Set-String $key "WintagePalette" "goldendefault"

        for ($index = 0; $index -lt $palette.Count; $index++) {
            Set-Dword $key ("ColorTable{0:D2}" -f $index) $palette[$index]
        }
    }
}

function Set-LegacyClineVisibility([bool]$Hidden) {
    if (-not $isAdmin) {
        if ($Hidden) {
            Write-Warning "Machine-wide legacy ClineHere entry remains visible; rerun elevated once to hide the duplicate."
        }
        return
    }

    foreach ($root in @("Directory", "Directory\Background")) {
        $legacy = "HKLM:\SOFTWARE\Classes\$root\shell\ClineHere"
        if (-not (Test-Path -LiteralPath $legacy)) { continue }
        if ($Hidden) {
            Set-ItemProperty -LiteralPath $legacy -Name "LegacyDisable" -Value ""
        } else {
            Remove-ItemProperty -LiteralPath $legacy -Name "LegacyDisable" -ErrorAction SilentlyContinue
        }
    }
}

function Assert-AgentMenus {
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Launcher missing: $launcher"
    }

    foreach ($name in $selectedAgents) {
        $definition = $definitions[$name]
        $resolved = Get-Command $definition.Command -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $resolved) {
            throw "$name CLI missing from PATH: $($definition.Command)"
        }

        foreach ($target in $shellTargets) {
            $commandKey = "$classesRoot\$($target.Root)\shell\$($definition.Key)\command"
            if (-not (Test-Path -LiteralPath $commandKey)) {
                throw "Context-menu key missing: $commandKey"
            }
            $commandLine = (Get-Item -LiteralPath $commandKey).GetValue("")
            if ($commandLine -notlike "*$launcher*" -or $commandLine -notlike "*-Agent $name*") {
                throw "Context-menu command drift: $commandKey"
            }
        }
    }

    $console = Get-ItemProperty -LiteralPath "HKCU:\Console"
    if ($console.ForceV2 -ne 1 -or $console.CtrlKeyShortcutsDisabled -ne 0 -or
        $console.InterceptCopyPaste -ne 1 -or $console.WintagePalette -ne "goldendefault") {
        throw "Console Ctrl+C/Ctrl+V or Wintage profile drift detected under HKCU\Console"
    }

    $vintageSkill = Join-Path $env:USERPROFILE ".agents\skills\vintage\SKILL.md"
    if (-not (Test-Path -LiteralPath $vintageSkill -PathType Leaf)) {
        throw "Global vintage skill missing: $vintageSkill"
    }
}

if ($Uninstall) {
    foreach ($name in $selectedAgents) {
        $definition = $definitions[$name]
        foreach ($target in $shellTargets) {
            $menuKey = "$classesRoot\$($target.Root)\shell\$($definition.Key)"
            if (Test-Path -LiteralPath $menuKey) {
                Remove-Item -LiteralPath $menuKey -Recurse -Force
            }
        }
    }
    if ($selectedAgents -contains "Cline") {
        Set-LegacyClineVisibility $false
    }
    Write-Host "Removed selected AI-agent context menus. Console/Wintage user preferences were preserved."
    exit 0
}

if (-not $VerifyOnly) {
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Launcher missing: $launcher"
    }
    Install-WintageConsoleProfile

    foreach ($name in $selectedAgents) {
        $definition = $definitions[$name]
        $resolved = Get-Command $definition.Command -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $resolved) {
            throw "$name CLI missing from PATH: $($definition.Command)"
        }

        foreach ($target in $shellTargets) {
            $menuKey = "$classesRoot\$($target.Root)\shell\$($definition.Key)"
            $commandKey = "$menuKey\command"
            New-Item -Path $commandKey -Force | Out-Null
            Set-ItemProperty -Path $menuKey -Name "MUIVerb" -Value $definition.Label
            Set-ItemProperty -Path $menuKey -Name "(default)" -Value $definition.Label
            Set-ItemProperty -Path $menuKey -Name "Icon" -Value "$powerShellExe,0"

            $commandLine = "`"$powerShellExe`" -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -Agent $name -WorkDir `"$($target.Argument)`""
            Set-ItemProperty -Path $commandKey -Name "(default)" -Value $commandLine
        }
    }

    if ($selectedAgents -contains "Cline") {
        Set-LegacyClineVisibility $true
    }
}

Assert-AgentMenus
Write-Host "PASS: $($selectedAgents -join ', ') context menus, YOLO launcher, project-first title, Wintage and Ctrl+C/Ctrl+V profile."
