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

# Preflight EVERY launcher before touching the registry. The install deletes the
# existing CodexHere subtree before it writes the new one, and the per-item
# validation used to sit inside that same loop -- so a wrong -LauncherRoot turned
# a working cascade into an empty parent key, with Directory wiped and
# Directory\Background possibly still intact. Nothing is removed until all three
# scripts are known to exist.
$missing = @($Items | Where-Object { -not (Test-Path -LiteralPath $_.Script) } | ForEach-Object { $_.Script })
if ($missing.Count -gt 0) {
    Write-Warning "Launcher script(s) not found under -LauncherRoot '$LauncherRoot'; the registry was NOT changed:"
    $missing | ForEach-Object { Write-Warning "  - $_" }
    exit 1
}

# The two shell roots are ONE menu, so they are ONE transaction. Each root is
# deleted before being rewritten, so a failure part-way through used to leave
# Directory converted and Directory\Background deleted-and-half-written, with no
# way back. Both subtrees are exported first and restored on any failure.
$Roots = @("Directory", "Directory\Background")
$ClassesRoot = "HKLM:\SOFTWARE\Classes"
$backupDir = Join-Path $env:TEMP ("codexmenu_" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

function Get-RegPath([string]$root) { "$ClassesRoot\$root\shell\$ParentKey" }
# reg.exe wants the hive without the PowerShell provider colon, derived from the
# same one root so the two can never disagree.
function Get-RegExportPath([string]$root) { (Get-RegPath $root) -replace '^([A-Z]+):', '$1' }

function Backup-Root([string]$root) {
    $ps = Get-RegPath $root
    if (-not (Test-Path $ps)) { return $null }
    $file = Join-Path $backupDir (($root -replace '[\\]', '_') + '.reg')
    # reg.exe, not Copy-Item: a registry subtree has no filesystem equivalent,
    # and the exit code is the only report of a failed export.
    $p = Start-Process 'reg.exe' -ArgumentList @('export', (Get-RegExportPath $root), "`"$file`"", '/y') -Wait -PassThru -WindowStyle Hidden
    if ($p.ExitCode -ne 0) { throw "could not export the existing '$root' menu for rollback (reg exit $($p.ExitCode))" }
    return $file
}

function Restore-Root([string]$root, [string]$file) {
    $ps = Get-RegPath $root
    try { if (Test-Path $ps) { Remove-Item $ps -Recurse -Force } } catch { }
    if ($null -eq $file) { return }   # it did not exist before; absent IS the prior state
    $p = Start-Process 'reg.exe' -ArgumentList @('import', "`"$file`"") -Wait -PassThru -WindowStyle Hidden
    if ($p.ExitCode -ne 0) { Write-Warning "rollback of '$root' FAILED (reg exit $($p.ExitCode)); the export is kept at $file" }
}

function Write-Root([string]$root) {
    $base = Get-RegPath $root
    if (Test-Path $base) { Remove-Item $base -Recurse -Force }
    New-Item -Path "$base\shell" -Force | Out-Null
    Set-ItemProperty -Path $base -Name "MUIVerb" -Value $ParentLabel
    Set-ItemProperty -Path $base -Name "SubCommands" -Value ""
    Set-ItemProperty -Path $base -Name "Icon" -Value "powershell.exe"

    foreach ($i in $Items) {
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

$backups = @{}
# T-133: every project registry writer participates in ONE cross-process
# mutation contract. The lock is taken for exactly this one transaction.
$registryMutationMutex = New-Object Threading.Mutex($false, 'Global\SAITULS_REGISTRY_MUTATION')
$registryMutationOwned = $false
try {
    try { $registryMutationOwned = $registryMutationMutex.WaitOne(5000) }
    catch [Threading.AbandonedMutexException] { $registryMutationOwned = $true }
    if (-not $registryMutationOwned) { Write-Host 'BUSY: SAITULS registry mutation is already in progress' -ForegroundColor Yellow; exit 3 }
    try {
        foreach ($root in $Roots) { $backups[$root] = Backup-Root $root }
        foreach ($root in $Roots) { Write-Root $root }
    } catch {
        Write-Warning "Install failed: $($_.Exception.Message)"
        Write-Warning "Rolling both shell menus back to their previous state..."
        foreach ($root in $Roots) { Restore-Root $root $backups[$root] }
        Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue
        exit 1
    }
} finally {
    if ($registryMutationOwned) { $registryMutationMutex.ReleaseMutex() }
    $registryMutationMutex.Dispose()
}

Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done. Cascaded context menu installed (SubCommands='' pattern)."
