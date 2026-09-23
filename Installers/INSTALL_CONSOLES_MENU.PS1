[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$VerifyOnly,
    # Also remove the older flat "OpenCode YOLO here" / "Cline YOLO here"
    # entries this cascade replaces. The machine-wide Codex cascade (HKLM
    # CodexHere) needs an elevated Remove-CodexContextMenus.ps1 run.
    [switch]$RemoveLegacy
)

# Explorer cascade "<menu_label>" -> one item per enabled profile in
# Scripts\consoles\consoles.json, in file order. Each item opens a plain
# non-elevated PowerShell console window running CONSOLES.ps1 for that
# profile in the clicked folder. Per-user (HKCU), so no elevation is needed
# to install, verify or remove it.
#
# Cascade pattern: SubCommands="" (EMPTY string) + verb subkeys under
# <parent>\shell\ -- the only cascade pattern that renders reliably here
# (same as the toolkit's FFmpeg/DL_YT/Codex menus). Explorer sorts child
# verbs by key name, so every key carries a two-digit order prefix.

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$rootDir = (Get-Item -LiteralPath $scriptDir).Parent.FullName
$launcher = Join-Path $rootDir 'Scripts\consoles\CONSOLES.ps1'
$registryFile = Join-Path $rootDir 'Scripts\consoles\consoles.json'
$powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$classesRoot = 'HKCU:\Software\Classes'
$parentKeyName = 'SaitulsAgents'
$shellRoots = @('Directory', 'Directory\Background', 'Drive')
$legacyKeys = @('OpenOpenCode', 'OpenCline')

function Get-MenuKey([string]$Root) { "$classesRoot\$Root\shell\$parentKeyName" }
function Get-RegExePath([string]$PsPath) { $PsPath -replace '^HKCU:\\', 'HKCU\' }

function Get-MenuItems {
    if (-not (Test-Path -LiteralPath $registryFile -PathType Leaf)) { throw "consoles.json missing: $registryFile" }
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "Launcher missing: $launcher" }
    $doc = Get-Content -LiteralPath $registryFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($doc.schema -ne 'saituls.consoles/1') { throw "unsupported console registry schema '$($doc.schema)'" }
    $label = if ($doc.PSObject.Properties['menu_label'] -and $doc.menu_label) { [string]$doc.menu_label } else { 'Open in' }
    $items = @()
    $index = 0
    foreach ($p in @($doc.profiles)) {
        if ($p.PSObject.Properties['enabled'] -and -not $p.enabled) { continue }
        if ($p.id -notmatch '^[A-Za-z0-9_-]+$') { throw "invalid console profile id: $($p.id)" }
        $index++
        $icon = $null
        if ($p.PSObject.Properties['icon'] -and $p.icon) {
            $candidate = [Environment]::ExpandEnvironmentVariables([string]$p.icon)
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $icon = $candidate }
        }
        if (-not $icon) {
            $cmd = Get-Command "$($p.command).exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            $icon = if ($cmd) { $cmd.Source } else { "$powerShellExe,0" }
        }
        $items += [pscustomobject]@{
            Key     = ('{0:D2}_{1}' -f $index, $p.id)
            Id      = [string]$p.id
            Label   = [string]$p.label
            Icon    = $icon
            # "%V." keeps a drive root ("C:\") from ending in a backslash that
            # would escape the closing quote; CONSOLES.ps1 drops the dot.
            Command = "`"$powerShellExe`" -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -Profile $($p.id) -WorkDir `"%V.`""
        }
    }
    if ($items.Count -eq 0) { throw 'consoles.json has no enabled profile' }
    return [pscustomobject]@{ Label = $label; Items = $items }
}

function Assert-Menu($Menu) {
    foreach ($root in $shellRoots) {
        $base = Get-MenuKey $root
        if (-not (Test-Path -LiteralPath $base)) { throw "Context-menu key missing: $base" }
        $parent = Get-Item -LiteralPath $base
        if ([string]$parent.GetValue('MUIVerb') -ne $Menu.Label) { throw "Menu label drift: $base" }
        if ($null -eq $parent.GetValue('SubCommands')) { throw "Cascade marker missing: $base" }
        $present = @(Get-ChildItem -LiteralPath "$base\shell" | ForEach-Object { $_.PSChildName })
        $expected = @($Menu.Items | ForEach-Object { $_.Key })
        if ($present.Count -ne $expected.Count -or (Compare-Object $present $expected)) { throw "Menu item drift under $base (found: $($present -join ', '))" }
        foreach ($item in $Menu.Items) {
            $commandLine = (Get-Item -LiteralPath "$base\shell\$($item.Key)\command").GetValue('')
            if ($commandLine -ne $item.Command) { throw "Context-menu command drift: $base\shell\$($item.Key)" }
        }
    }
}

$registryMutationMutex = New-Object Threading.Mutex($false, 'Global\SAITULS_REGISTRY_MUTATION')
$registryMutationOwned = $false
try {
    try { $registryMutationOwned = $registryMutationMutex.WaitOne(5000) }
    catch [Threading.AbandonedMutexException] { $registryMutationOwned = $true }
    if (-not $registryMutationOwned) { throw 'BUSY: SAITULS registry mutation is already in progress' }

    if ($Uninstall) {
        foreach ($root in $shellRoots) {
            $base = Get-MenuKey $root
            if (Test-Path -LiteralPath $base) { Remove-Item -LiteralPath $base -Recurse -Force }
        }
        Write-Host 'Removed the agent console cascade menu.'
        exit 0
    }

    $menu = Get-MenuItems

    if (-not $VerifyOnly) {
        # SNAPSHOT every root first; any failure restores all of them.
        $backupDir = Join-Path ([IO.Path]::GetTempPath()) ('consolesmenu_' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        $backups = @{}
        foreach ($root in $shellRoots) {
            $base = Get-MenuKey $root
            $backups[$root] = $null
            if (Test-Path -LiteralPath $base) {
                $file = Join-Path $backupDir (($root -replace '[\\]', '_') + '.reg')
                $p = Start-Process 'reg.exe' -ArgumentList @('export', "`"$(Get-RegExePath $base)`"", "`"$file`"", '/y') -Wait -PassThru -WindowStyle Hidden
                if ($p.ExitCode -ne 0) { throw "could not snapshot $base for rollback" }
                $backups[$root] = $file
            }
        }

        try {
            foreach ($root in $shellRoots) {
                $base = Get-MenuKey $root
                if (Test-Path -LiteralPath $base) { Remove-Item -LiteralPath $base -Recurse -Force }
                New-Item -Path "$base\shell" -Force | Out-Null
                Set-ItemProperty -LiteralPath $base -Name 'MUIVerb' -Value $menu.Label
                Set-ItemProperty -LiteralPath $base -Name 'SubCommands' -Value ''
                Set-ItemProperty -LiteralPath $base -Name 'Icon' -Value "$powerShellExe,0"
                foreach ($item in $menu.Items) {
                    $verbKey = "$base\shell\$($item.Key)"
                    New-Item -Path "$verbKey\command" -Force | Out-Null
                    Set-ItemProperty -LiteralPath $verbKey -Name 'MUIVerb' -Value $item.Label
                    Set-ItemProperty -LiteralPath $verbKey -Name 'Icon' -Value $item.Icon
                    Set-ItemProperty -LiteralPath "$verbKey\command" -Name '(default)' -Value $item.Command
                }
            }
        } catch {
            Write-Warning "Install failed: $($_.Exception.Message). Rolling back."
            foreach ($root in $shellRoots) {
                $base = Get-MenuKey $root
                try { if (Test-Path -LiteralPath $base) { Remove-Item -LiteralPath $base -Recurse -Force } } catch { }
                if ($backups[$root]) {
                    $p = Start-Process 'reg.exe' -ArgumentList @('import', "`"$($backups[$root])`"") -Wait -PassThru -WindowStyle Hidden
                    if ($p.ExitCode -ne 0) { Write-Warning "rollback FAILED for $base; snapshot kept at $($backups[$root])" }
                }
            }
            throw
        }
        Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue

        if ($RemoveLegacy) {
            foreach ($root in $shellRoots) {
                foreach ($name in $legacyKeys) {
                    $old = "$classesRoot\$root\shell\$name"
                    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
                }
            }
            if ((Test-Path -LiteralPath 'HKLM:\SOFTWARE\Classes\Directory\shell\CodexHere') -or
                (Test-Path -LiteralPath 'HKLM:\SOFTWARE\Classes\Directory\Background\shell\CodexHere')) {
                Write-Host 'The old machine-wide Codex menu is still installed; run Remove-CodexContextMenus.ps1 as Administrator to drop it.' -ForegroundColor Yellow
            }
        }
    }

    Assert-Menu $menu
    Write-Host "PASS: '$($menu.Label)' cascade with $($menu.Items.Count) agent console(s): $(($menu.Items | ForEach-Object { $_.Label }) -join ', ')."
} finally {
    if ($registryMutationOwned) { $registryMutationMutex.ReleaseMutex() }
    $registryMutationMutex.Dispose()
}
