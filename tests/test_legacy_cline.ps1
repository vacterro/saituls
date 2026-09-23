param(
    [string]$InstallerSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Installers\INSTALL_AI_AGENT_MENUS.PS1'),
    [string]$CodexRemoverSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Remove-CodexContextMenus.ps1')
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$sandbox = Join-Path $env:TEMP ('saituls_legacy_cline_' + [guid]::NewGuid().ToString('N'))
$suffix = [guid]::NewGuid().ToString('N')
$userClasses = "HKCU:\Software\_SAI_CLINE_USER_$suffix"
$legacyClasses = "HKCU:\Software\_SAI_CLINE_LEGACY_$suffix"
$consoleKey = "HKCU:\Software\_SAI_CLINE_CONSOLE_$suffix"
$failures = 0
$oldPath = $env:PATH

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Host "PASS  $Name $Detail" }
    else { Write-Host "FAIL  $Name $Detail"; $script:failures++ }
}

function Force-TestAdmin([string]$Text) {
    $pattern = [regex]::new('(?ms)^\$isAdmin = .*?WindowsBuiltInRole\]::Administrator\s*\)\r?$')
    $updated = $pattern.Replace($Text, '$isAdmin = $true', 1)
    if ($updated -eq $Text) { throw 'Could not isolate the script admin check.' }
    return $updated
}

function New-InstallerReplica([string]$Destination) {
    $text = Get-Content -LiteralPath $InstallerSource -Raw
    $text = $text.Replace('$classesRoot = "HKCU:\Software\Classes"', "`$classesRoot = '$userClasses'")
    $text = $text.Replace('$legacyClassesRoot = "HKLM:\SOFTWARE\Classes"', "`$legacyClassesRoot = '$legacyClasses'")
    # The red-control subject predates $legacyClassesRoot; keep its old logic
    # intact while redirecting the same HKLM path into the registry sandbox.
    $text = $text.Replace('"HKLM:\SOFTWARE\Classes\$root\shell\ClineHere"', "'$legacyClasses\' + `$root + '\shell\ClineHere'")
    $text = $text.Replace('"HKCU:\Console",', "'$consoleKey',")
    $text = $text.Replace('"HKCU:\Console\%SystemRoot%_System32_WindowsPowerShell_v1.0_powershell.exe"', "'$consoleKey'")
    $text = $text.Replace('Get-ItemProperty -LiteralPath "HKCU:\Console"', "Get-ItemProperty -LiteralPath '$consoleKey'")
    $text = Force-TestAdmin $text
    Set-Content -LiteralPath $Destination -Value $text -Encoding utf8
}

function New-CodexRemoverReplica([string]$Destination) {
    $text = Get-Content -LiteralPath $CodexRemoverSource -Raw
    $text = $text.Replace('$classesRoot = "HKLM:\SOFTWARE\Classes"', "`$classesRoot = '$legacyClasses'")
    $text = $text.Replace('"HKLM:\SOFTWARE\Classes\$root\shell\$k"', "'$legacyClasses\' + `$root + '\shell\' + `$k")
    $text = Force-TestAdmin $text
    Set-Content -LiteralPath $Destination -Value $text -Encoding utf8
}

function Set-HistoricalCline([string]$Root, [string]$Argument) {
    $key = "$legacyClasses\$Root\shell\ClineHere"
    $command = Join-Path $key 'command'
    New-Item -Path $command -Force | Out-Null
    $label = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String('0J7RgtC60YDRi9GC0YwgQ2xpbmUg0LfQtNC10YHRjA=='))
    Set-Item -LiteralPath $key -Value $label
    Set-ItemProperty -LiteralPath $key -Name 'MUIVerb' -Value $label
    Set-ItemProperty -LiteralPath $key -Name 'Icon' -Value 'powershell.exe'
    Set-ItemProperty -LiteralPath $key -Name 'LegacyDisable' -Value ''
    $line = "powershell.exe -NoExit -ExecutionPolicy Bypass -Command `"Set-Location -LiteralPath '$Argument'; cline`""
    Set-Item -LiteralPath $command -Value $line
}

function Set-ForeignCline {
    $key = "$legacyClasses\Directory\Background\shell\ClineHere"
    $command = Join-Path $key 'command'
    New-Item -Path $command -Force | Out-Null
    Set-Item -LiteralPath $key -Value 'My personal Cline command'
    Set-ItemProperty -LiteralPath $key -Name 'MUIVerb' -Value 'My personal Cline command'
    Set-ItemProperty -LiteralPath $key -Name 'Icon' -Value 'custom-user-icon.exe'
    Set-ItemProperty -LiteralPath $key -Name 'UserMarker' -Value 'KEEP-ME'
    Set-Item -LiteralPath $command -Value 'cmd.exe /d /c echo user-owned'
}

function Get-KeyState([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '<absent>' }
    $item = Get-Item -LiteralPath $Path
    $commandPath = Join-Path $Path 'command'
    $values = [ordered]@{}
    foreach ($name in @('', 'MUIVerb', 'Icon', 'LegacyDisable', 'UserMarker')) {
        $values[$name] = $item.GetValue($name, $null)
    }
    $values['command'] = if (Test-Path -LiteralPath $commandPath) { (Get-Item -LiteralPath $commandPath).GetValue('') } else { $null }
    return ($values | ConvertTo-Json -Compress)
}

function Invoke-Script([string]$Path, [string[]]$Arguments = @()) {
    $output = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Path @Arguments 2>&1
    return [pscustomobject]@{ Exit = $LASTEXITCODE; Output = ($output | ForEach-Object { "$_" }) -join "`n" }
}

try {
    New-Item -ItemType Directory -Path (Join-Path $sandbox 'Installers') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $sandbox 'Scripts') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $sandbox 'bin') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot 'Scripts\AI_AGENT_LAUNCHER.PS1') -Destination (Join-Path $sandbox 'Scripts\AI_AGENT_LAUNCHER.PS1')
    Set-Content -LiteralPath (Join-Path $sandbox 'bin\cline.cmd') -Value '@echo fake cline' -Encoding ascii
    $env:PATH = (Join-Path $sandbox 'bin') + [IO.Path]::PathSeparator + $env:PATH

    $installer = Join-Path $sandbox 'Installers\INSTALL_AI_AGENT_MENUS.PS1'
    $codexRemover = Join-Path $sandbox 'Remove-CodexContextMenus.ps1'
    New-InstallerReplica $installer
    New-CodexRemoverReplica $codexRemover

    $historical = "$legacyClasses\Directory\shell\ClineHere"
    $foreign = "$legacyClasses\Directory\Background\shell\ClineHere"
    $codex = "$legacyClasses\Directory\shell\CodexHere"
    Set-HistoricalCline 'Directory' '%1'
    Set-ForeignCline
    New-Item -Path (Join-Path $codex 'command') -Force | Out-Null
    Set-Item -LiteralPath $codex -Value 'Codex fixture'
    $historicalBefore = Get-KeyState $historical
    $foreignBefore = Get-KeyState $foreign

    $result = Invoke-Script $codexRemover
    Check 'Codex remover succeeds' ($result.Exit -eq 0) $result.Output
    Check 'Codex remover removes CodexHere' (-not (Test-Path -LiteralPath $codex))
    Check 'Codex remover never changes historical ClineHere' ((Get-KeyState $historical) -eq $historicalBefore)
    Check 'Codex remover never changes foreign ClineHere' ((Get-KeyState $foreign) -eq $foreignBefore)

    $result = Invoke-Script $installer @('-Agent', 'Cline')
    Check 'Cline install succeeds' ($result.Exit -eq 0) $result.Output
    Check 'Cline install migrates the exact historical key' (-not (Test-Path -LiteralPath $historical))
    Check 'Cline install preserves a foreign same-name key' ((Get-KeyState $foreign) -eq $foreignBefore)

    Set-HistoricalCline 'Directory' '%1'
    $result = Invoke-Script $installer @('-Agent', 'Cline', '-Uninstall')
    Check 'Cline uninstall succeeds' ($result.Exit -eq 0) $result.Output
    Check 'Cline uninstall removes project-owned legacy remnants' (-not (Test-Path -LiteralPath $historical))
    Check 'Cline uninstall preserves a foreign same-name key' ((Get-KeyState $foreign) -eq $foreignBefore)
    Check 'Cline uninstall removes its HKCU menu' (-not (Test-Path -LiteralPath "$userClasses\Directory\shell\OpenCline"))

    Set-HistoricalCline 'Directory' '%1'
    $result = Invoke-Script $installer @('-Agent', 'All', '-Uninstall')
    Check 'aggregate All uninstall succeeds' ($result.Exit -eq 0) $result.Output
    Check 'aggregate All uninstall removes project-owned ClineHere' (-not (Test-Path -LiteralPath $historical))
    Check 'aggregate All uninstall preserves foreign ClineHere' ((Get-KeyState $foreign) -eq $foreignBefore)

    $aggregate = Get-Content -LiteralPath (Join-Path $projectRoot 'Installers\INSTALL_ALL.PS1') -Raw
    Check 'INSTALL_ALL aggregate delegates through All uninstall' ($aggregate -match '& \$agentMenuInstaller -Agent All -Uninstall:\$Uninstall')
} finally {
    $env:PATH = $oldPath
    Remove-Item -LiteralPath $userClasses -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $legacyClasses -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $consoleKey -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $sandbox) { Remove-Item -LiteralPath $sandbox -Recurse -Force }
}

Write-Host '---'
if ($failures) { Write-Host "FAILED ($failures failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
