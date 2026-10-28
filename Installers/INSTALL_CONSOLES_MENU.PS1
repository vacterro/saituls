[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$VerifyOnly,
    # Also remove the older flat "OpenCode YOLO here" / "Cline YOLO here"
    # entries this cascade replaces. The machine-wide Codex cascade (HKLM
    # CodexHere) needs an elevated Remove-CodexContextMenus.ps1 run.
    [switch]$RemoveLegacy,
    # auto = federated when the control plane is installed; off = local
    # profiles only; on = refuse without the control plane. See CONSOLES.ps1.
    [ValidateSet('auto', 'on', 'off')]
    [string]$Federation = 'auto',
    # Where the cascade lives. Defaults to the real per-user class root; a test
    # points it at a disposable key so a drift/projection assertion never has to
    # touch the menu the operator is using.
    [string]$RegistryClassesRoot = 'HKCU:\Software\Classes',
    # Stable home of the native Explorer gateway (SaiConsoleLauncher.exe). It
    # lives outside the install root on purpose: the verb names only this exe,
    # and the exe finds the current root at click time (sidecar root file).
    [string]$GatewayDir = ''
)

# Explorer cascade "<Open in>" -> one item per enabled profile in
# Scripts\consoles\consoles.json, in file order. Each item opens a plain
# non-elevated PowerShell console window running CONSOLES.ps1 for that
# profile in the clicked folder. Per-user (HKCU), so no elevation is needed
# to install, verify or remove it.
#
# Federation. The item list is built from the SAME expansion the launcher
# uses (ACCOUNTS.ps1), so the menu and the picker can never disagree. When the
# SAI Accounts control plane is installed, each canonical account of a
# federated provider contributes its own item; when it is not, the federated
# templates contribute nothing and this cascade is exactly the local profile
# set it was before. The parent label comes from the translation bundle
# (i18n/strings/strings.<locale>.json, key menu.open_in), not from a hardcoded
# string in consoles.json.
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
$classesRoot = $RegistryClassesRoot.TrimEnd('\')
$parentKeyName = 'SaitulsAgents'
$shellRoots = @('Directory', 'Directory\Background', 'Drive')
$legacyKeys = @('OpenOpenCode', 'OpenCline')

. (Join-Path (Join-Path (Join-Path $rootDir 'Scripts') 'consoles') 'ACCOUNTS.ps1')

function Get-MenuKey([string]$Root) { "$classesRoot\$Root\shell\$parentKeyName" }
function Get-RegExePath([string]$PsPath) { $PsPath -replace '^HKCU:\\', 'HKCU\' }

if ([string]::IsNullOrWhiteSpace($GatewayDir)) {
    $GatewayDir = Join-Path (Join-Path $env:LOCALAPPDATA 'SAITULS') 'bin'
}
$gatewaySource = Join-Path $rootDir 'Scripts\consoles\SaiConsoleLauncher.cs'
$gatewayExe = Join-Path $GatewayDir 'SaiConsoleLauncher.exe'
$gatewaySidecar = Join-Path $GatewayDir 'SaiConsoleLauncher.root.json'

# Compile the gateway with the same .NET Framework csc.exe SAITULS.exe uses,
# stage it, and publish only when the bytes changed. A running copy cannot be
# overwritten but can be renamed aside, so a click in progress never blocks a
# repair. The sidecar names the current install root: moving or reinstalling
# SAITULS rewrites it, and the verb itself never changes.
function Publish-ConsoleGateway {
    $csc = @('C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe',
             'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe') |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $csc) { throw 'no .NET Framework csc.exe found to build SaiConsoleLauncher.exe' }
    if (-not (Test-Path -LiteralPath $gatewaySource -PathType Leaf)) { throw "gateway source missing: $gatewaySource" }
    New-Item -ItemType Directory -Path $GatewayDir -Force | Out-Null
    $fresh = (Test-Path -LiteralPath $gatewayExe -PathType Leaf) -and
        ((Get-Item -LiteralPath $gatewayExe).LastWriteTimeUtc -ge (Get-Item -LiteralPath $gatewaySource).LastWriteTimeUtc) -and
        ((Get-Item -LiteralPath $gatewayExe).Length -gt 4096)
    $staged = Join-Path ([IO.Path]::GetTempPath()) ('SaiConsoleLauncher_' + [Guid]::NewGuid().ToString('N') + '.exe')
    if (-not $fresh) { try {
        $out = & $csc -nologo -target:winexe -optimize+ "-out:$staged" -r:System.dll -r:System.Windows.Forms.dll `
            -r:System.Web.Extensions.dll $gatewaySource 2>&1
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $staged)) { throw "gateway build failed: $($out -join ' ')" }
        $same = (Test-Path -LiteralPath $gatewayExe -PathType Leaf) -and
            ((Get-FileHash -LiteralPath $gatewayExe).Hash -eq (Get-FileHash -LiteralPath $staged).Hash)
        if (-not $same) {
            if (Test-Path -LiteralPath $gatewayExe) {
                Get-ChildItem -LiteralPath $GatewayDir -Filter 'SaiConsoleLauncher.exe.old-*' -ErrorAction SilentlyContinue |
                    Remove-Item -Force -ErrorAction SilentlyContinue
                Move-Item -LiteralPath $gatewayExe -Destination ($gatewayExe + '.old-' + [Guid]::NewGuid().ToString('N')) -Force
            }
            Copy-Item -LiteralPath $staged -Destination $gatewayExe -Force
        }
    } finally { Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue } }
    $sidecar = [ordered]@{ schema = 'saituls.console-gateway/root/1'; root = $rootDir } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($gatewaySidecar, $sidecar, (New-Object Text.UTF8Encoding($false)))
}

function Assert-ConsoleGateway {
    if (-not (Test-Path -LiteralPath $gatewayExe -PathType Leaf)) {
        throw "Console gateway missing: $gatewayExe. Re-run this installer without -VerifyOnly."
    }
    $bound = $null
    try { $bound = (Get-Content -LiteralPath $gatewaySidecar -Raw -Encoding UTF8 | ConvertFrom-Json).root } catch { }
    if ($bound -ne $rootDir) {
        throw "Console gateway root drift: '$gatewaySidecar' names '$bound' but this install is '$rootDir' (launcher '$launcher'). Re-run this installer without -VerifyOnly."
    }
}

function Get-MenuItems {
    if (-not (Test-Path -LiteralPath $registryFile -PathType Leaf)) { throw "consoles.json missing: $registryFile" }
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "Launcher missing: $launcher" }
    $doc = Get-Content -LiteralPath $registryFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($doc.schema -ne 'saituls.consoles/1') { throw "unsupported console registry schema '$($doc.schema)'" }

    $engine = Resolve-SaiAccountsEngine
    if ($Federation -eq 'on' -and -not $engine) {
        throw 'federation was requested but the SAI Accounts control plane is not installed'
    }
    if ($Federation -eq 'off') { $engine = $null }
    $doc.profiles = @(Expand-AccountProfiles -Profiles @($doc.profiles) -Engine $engine `
        -Accounts @(Get-SaiAccountDescriptors $engine))

    $label = Get-ConsolesMenuLabel $rootDir
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
            # A federated profile's command is an absolute engine path, already
            # resolved and already an .exe; only bare names get a PATH lookup.
            if ([System.IO.Path]::IsPathRooted([string]$p.command)) {
                $icon = if (Test-Path -LiteralPath $p.command -PathType Leaf) { $p.command } else { "$powerShellExe,0" }
            } else {
                $cmd = Get-Command "$($p.command).exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                $icon = if ($cmd) { $cmd.Source } else { "$powerShellExe,0" }
            }
        }
        $origin = if ($p.PSObject.Properties['origin'] -and $p.origin -eq 'shared') { 'shared' } else { 'local' }
        # A local console window IS the agent's console: it must be visible, and
        # it stays open for as long as the CLI runs.
        #
        # A federated account's console is opened by the accounts engine in the
        # account's own Windows context, so the PowerShell host underneath is a
        # disposable bootstrap. Starting it hidden is the difference between
        # "click -> the account console appears" and "click -> a black window
        # appears, then a second one": nothing is created that later has to be
        # hidden, and a failure still reaches the operator because the boundary
        # in CONSOLES.ps1 raises a dialog and writes its own diagnostic. The
        # hidden/visible decision is the gateway's (SaiConsoleLauncher.cs),
        # taken from the profile's resolved origin at click time.
        $items += [pscustomobject]@{
            Key     = ('{0:D2}_{1}' -f $index, $p.id)
            Id      = [string]$p.id
            Label   = [string]$p.label
            Origin  = $origin
            Icon    = $icon
            # The verb is the native gateway, the projection id and "%V" --
            # nothing else. No absolute CONSOLES.ps1 path, no PowerShell switch:
            # the gateway resolves the install root at click time, preflights
            # the profile hidden, and only then opens the console (hidden for
            # a shared account bootstrap, visible for a local agent). A drive
            # root's trailing backslash is handled by the gateway's raw
            # command-line parser, so the old "%V." trick is gone.
            Command = "`"$gatewayExe`" $($p.id) `"%V`""
        }
    }
    if ($items.Count -eq 0) { throw 'consoles.json has no enabled profile' }
    return [pscustomobject]@{ Label = $label; Items = $items; Engine = $engine }
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
            $leaf = Get-Item -LiteralPath "$base\shell\$($item.Key)"
            if ([string]$leaf.GetValue('MUIVerb') -ne $item.Label) { throw "Menu item label drift: $base\shell\$($item.Key)" }
            if ([string]$leaf.GetValue('Icon') -ne $item.Icon) { throw "Menu item icon drift: $base\shell\$($item.Key)" }
            $commandLine = (Get-Item -LiteralPath "$base\shell\$($item.Key)\command").GetValue('')
            if ($commandLine -ne $item.Command) {
                # Drift here is the classic post-move failure: the stored verb
                # still names the launcher path this repository had before it
                # moved, so the click starts a PowerShell that cannot find the
                # script and closes. Name both paths so the repair is obvious.
                $stale = if ($commandLine -match '-File\s+"([^"]+)"') { $Matches[1] } elseif ($commandLine -match '^"([^"]+)"') { $Matches[1] } else { '<unparsed>' }
                throw ("Context-menu command drift under $base\shell\$($item.Key): " +
                       "the installed verb launches '$stale' but this build's gateway is '$gatewayExe' (launcher '$launcher'). " +
                       "Re-run this installer without -VerifyOnly to repair all three shell roots.")
            }
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
        # The gateway exists before any verb names it.
        Publish-ConsoleGateway
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
    Assert-ConsoleGateway
    Write-Host "PASS: '$($menu.Label)' cascade with $($menu.Items.Count) agent console(s) via $gatewayExe`: $(($menu.Items | ForEach-Object { $_.Label }) -join ', ')."
} finally {
    if ($registryMutationOwned) { $registryMutationMutex.ReleaseMutex() }
    $registryMutationMutex.Dispose()
}
