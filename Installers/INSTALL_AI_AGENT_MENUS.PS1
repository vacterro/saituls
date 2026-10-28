[CmdletBinding()]
param(
    [ValidateSet("All", "OpenCode", "Cline")]
    [string]$Agent = "All",
    [switch]$Uninstall,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"

# Shared cross-process writer contract. Nested scripts on the same thread
# reenter this exact mutex; every acquired level is released in finally.
$registryMutationMutex = New-Object Threading.Mutex($false, 'Global\SAITULS_REGISTRY_MUTATION')
$registryMutationOwned = $false
try {
    try { $registryMutationOwned = $registryMutationMutex.WaitOne(5000) }
    catch [Threading.AbandonedMutexException] { $registryMutationOwned = $true }
    if (-not $registryMutationOwned) { throw 'BUSY: SAITULS registry mutation is already in progress' }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$rootDir = (Get-Item -LiteralPath $scriptDir).Parent.FullName
$launcher = Join-Path $rootDir "Scripts\AI_AGENT_LAUNCHER.PS1"
$powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$classesRoot = "HKCU:\Software\Classes"
$legacyClassesRoot = "HKLM:\SOFTWARE\Classes"
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

function Test-ProjectOwnedLegacyCline([string]$Path, [string]$Argument) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $commandPath = Join-Path $Path "command"
    if (-not (Test-Path -LiteralPath $commandPath)) { return $false }

    $item = Get-Item -LiteralPath $Path
    $commandItem = Get-Item -LiteralPath $commandPath
    # ASCII source stays parse-safe in Windows PowerShell 5.1; this decodes the
    # exact historical UTF-8 label "Open Cline here" in Russian.
    $expectedLabel = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String("0J7RgtC60YDRi9GC0YwgQ2xpbmUg0LfQtNC10YHRjA=="))
    $expectedCommand = "powershell.exe -NoExit -ExecutionPolicy Bypass -Command `"Set-Location -LiteralPath '$Argument'; cline`""
    return (
        [string]$item.GetValue("") -eq $expectedLabel -and
        [string]$item.GetValue("MUIVerb") -eq $expectedLabel -and
        [string]$item.GetValue("Icon") -eq "powershell.exe" -and
        [string]$commandItem.GetValue("") -eq $expectedCommand
    )
}

function Remove-ProjectOwnedLegacyCline {
    $legacyTargets = @(
        @{ Root = "Directory"; Argument = "%1" },
        @{ Root = "Directory\Background"; Argument = "%V" }
    )
    if (-not $isAdmin) {
        $owned = @($legacyTargets | Where-Object {
            $path = "$legacyClassesRoot\$($_.Root)\shell\ClineHere"
            Test-ProjectOwnedLegacyCline -Path $path -Argument $_.Argument
        })
        if ($owned.Count -gt 0) {
            Write-Warning "A project-owned machine-wide ClineHere entry can only be migrated or removed from an elevated run."
        }
        return
    }

    foreach ($target in $legacyTargets) {
        $legacy = "$legacyClassesRoot\$($target.Root)\shell\ClineHere"
        if (-not (Test-Path -LiteralPath $legacy)) { continue }
        if (Test-ProjectOwnedLegacyCline -Path $legacy -Argument $target.Argument) {
            Remove-Item -LiteralPath $legacy -Recurse -Force
            Write-Host "Removed project-owned legacy Cline menu: $legacy"
        } else {
            Write-Verbose "Preserved foreign ClineHere key: $legacy"
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
        Remove-ProjectOwnedLegacyCline
    }
    Write-Host "Removed selected AI-agent context menus. Console/Wintage user preferences were preserved."
    exit 0
}

if (-not $VerifyOnly) {
    # PREFLIGHT: every dependency of every selected agent is resolved BEFORE the
    # first mutation. The old order mutated the console profile and wrote
    # OpenCode keys before resolving Cline, so `-Agent All` with a missing CLI
    # left half an install behind.
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
    }

    # SNAPSHOT: exactly the state this install is about to touch, so a failure
    # can put it back. Console values are read per-name; menu keys are exported
    # with reg.exe (a registry subtree has no filesystem equivalent).
    $consoleKeys = @(
        "HKCU:\Console",
        "HKCU:\Console\%SystemRoot%_System32_WindowsPowerShell_v1.0_powershell.exe"
    )
    $consoleNames = @(
        "ForceV2", "CtrlKeyShortcutsDisabled", "ExtendedEditKey", "QuickEdit",
        "InsertMode", "FilterOnPaste", "InterceptCopyPaste", "LineSelection",
        "VirtualTerminalLevel", "ScreenColors", "PopupColors", "WindowAlpha",
        "FontFamily", "FontSize", "FontWeight", "FaceName", "FontFace",
        "WintagePalette"
    )
    for ($index = 0; $index -lt 16; $index++) {
        $consoleNames += ("ColorTable{0:D2}" -f $index)
    }
    $consoleSnapshot = @{}
    foreach ($key in $consoleKeys) {
        foreach ($name in $consoleNames) {
            $snapshotName = "$key::$name"
            $existing = $null
            try { $existing = (Get-Item -LiteralPath $key -ErrorAction Stop).GetValue($name) } catch { }
            $consoleSnapshot[$snapshotName] = $existing   # $null = did not exist
        }
    }

    $menuSnapshotDir = Join-Path ([IO.Path]::GetTempPath()) ("aimenu_" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $menuSnapshotDir -Force | Out-Null
    $menuExisted = @{}
    foreach ($name in $selectedAgents) {
        $definition = $definitions[$name]
        foreach ($target in $shellTargets) {
            $menuKey = "$classesRoot\$($target.Root)\shell\$($definition.Key)"
            $regPath = ($menuKey -replace '^HKCU:\\Software\\Classes', 'HKCU\Software\Classes')
            if (Test-Path -LiteralPath $menuKey) {
                $file = Join-Path $menuSnapshotDir (($definition.Key) + "_" + ($target.Root -replace '[\\]', '_') + ".reg")
                $p = Start-Process 'reg.exe' -ArgumentList @('export', $regPath, "`"$file`"", '/y') -Wait -PassThru -WindowStyle Hidden
                if ($p.ExitCode -ne 0) { throw "could not snapshot $regPath for rollback" }
                $menuExisted[$menuKey] = $file
            } else {
                $menuExisted[$menuKey] = $null
            }
        }
    }

    try {
        Install-WintageConsoleProfile

        foreach ($name in $selectedAgents) {
            $definition = $definitions[$name]

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
            Remove-ProjectOwnedLegacyCline
        }
    }
    catch {
        # ROLLBACK: undo exactly what this run touched, nothing more.
        foreach ($entry in $menuExisted.GetEnumerator()) {
            $menuKey = $entry.Key
            if ($entry.Value) {
                try {
                    if (Test-Path -LiteralPath $menuKey) { Remove-Item -LiteralPath $menuKey -Recurse -Force }
                    $p = Start-Process 'reg.exe' -ArgumentList @('import', "`"$($entry.Value)`"") -Wait -PassThru -WindowStyle Hidden
                    if ($p.ExitCode -ne 0) { Write-Warning "menu rollback FAILED for $menuKey; snapshot kept at $($entry.Value)" }
                } catch { Write-Warning "menu rollback error for $menuKey : $_" }
            } else {
                try { if (Test-Path -LiteralPath $menuKey) { Remove-Item -LiteralPath $menuKey -Recurse -Force } } catch { }
            }
        }
        foreach ($key in $consoleKeys) {
            if (-not (Test-Path -LiteralPath $key)) { continue }
            foreach ($name in $consoleNames) {
                $was = $consoleSnapshot["$key::$name"]
                $now = $null
                try { $now = (Get-Item -LiteralPath $key).GetValue($name) } catch { }
                if ($null -eq $was) {
                    if ($null -ne $now) { Remove-ItemProperty -LiteralPath $key -Name $name -ErrorAction SilentlyContinue }
                } elseif ($was -ne $now) {
                    Set-ItemProperty -LiteralPath $key -Name $name -Value $was
                }
            }
        }
        Remove-Item -LiteralPath $menuSnapshotDir -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
    Remove-Item -LiteralPath $menuSnapshotDir -Recurse -Force -ErrorAction SilentlyContinue
}

Assert-AgentMenus
Write-Host "PASS: $($selectedAgents -join ', ') context menus, YOLO launcher, project-first title, Wintage and Ctrl+C/Ctrl+V profile."
} finally {
    if ($registryMutationOwned) { $registryMutationMutex.ReleaseMutex() }
    $registryMutationMutex.Dispose()
}
