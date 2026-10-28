param(
    [switch]$Uninstall,
    [string]$Lang,
    [switch]$IncludeAgentMenus
)
$ErrorActionPreference = 'Stop'

# Self-elevate: reg import into HKLM/HKCR silently fails without admin rights
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $psArgs = @('-ExecutionPolicy','Bypass','-NoProfile','-File',"`"$($MyInvocation.MyCommand.Definition)`"")
    if ($Uninstall) { $psArgs += '-Uninstall' }
    if ($Lang) { $psArgs += @('-Lang', $Lang) }
    if ($IncludeAgentMenus) { $psArgs += '-IncludeAgentMenus' }
    # -Wait -PassThru and the child's own exit code: without them the parent
    # returned 0 the moment the elevated child STARTED, so a cancelled UAC prompt
    # or a failed install was indistinguishable from success to anything calling
    # this script (setup.ps1 among them).
    $elevated = Start-Process powershell.exe -ArgumentList $psArgs -Verb RunAs -Wait -PassThru
    exit $elevated.ExitCode
}

# Shared cross-process writer contract. Nested scripts on the same thread
# reenter this exact mutex; every acquired level is released in finally.
$registryMutationMutex = New-Object Threading.Mutex($false, 'Global\SAITULS_REGISTRY_MUTATION')
$registryMutationOwned = $false
try {
    try { $registryMutationOwned = $registryMutationMutex.WaitOne(5000) }
    catch [Threading.AbandonedMutexException] { $registryMutationOwned = $true }
    if (-not $registryMutationOwned) { throw 'BUSY: SAITULS registry mutation is already in progress' }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$rootDir = (Get-Item $scriptDir).Parent.FullName
$registryDir = Join-Path $rootDir "Registry"
$payloadBackup = $env:SAITULS_PAYLOAD_BACKUP
$settingsPath = Join-Path $rootDir 'SAITULS.ini'
if (-not $payloadBackup -and (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
    $payloadSetting = Get-Content -LiteralPath $settingsPath | Where-Object { $_ -match '^\s*PayloadBackup\s*=' } | Select-Object -Last 1
    if ($payloadSetting) { $payloadBackup = ($payloadSetting -split '=', 2)[1].Trim() }
}
if ($payloadBackup) {
    $payloadRestore = Join-Path $rootDir 'Scripts\restore_payload.ps1'
    if (-not (Test-Path -LiteralPath $payloadRestore -PathType Leaf)) { throw "Payload restore script missing: $payloadRestore" }
    & $payloadRestore -Root $rootDir -BackupRoot $payloadBackup
    if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) { throw "Payload restore failed with exit code $LASTEXITCODE" }
}

# T-034: regs are relocation-proof -- they carry %%ROOT%% where the
# toolkit root used to be hardcoded (V:\...\__SAITULS). Substitute the
# real root (backslash-escaped for reg value syntax) into a temp copy and
# import that, so the install set works from any drive/path.
function Import-RegWithRoot([string]$path) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
        $text = [System.Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2)
        $encoding = 'utf16'
    } else {
        $text = [System.Text.Encoding]::Default.GetString($bytes)
        $encoding = 'ansi'
    }
    $text = $text.Replace('%%ROOT%%', $rootDir.Replace('\', '\\'))
    $tmp = Join-Path $env:TEMP ("ctxmenu_" + [System.IO.Path]::GetRandomFileName() + ".reg")
    if ($encoding -eq 'utf16') {
        [System.IO.File]::WriteAllBytes($tmp, [System.Text.Encoding]::Unicode.GetPreamble() + [System.Text.Encoding]::Unicode.GetBytes($text))
    } else {
        [System.IO.File]::WriteAllBytes($tmp, [System.Text.Encoding]::Default.GetBytes($text))
    }
    try {
        # -PassThru and the exit code are the whole point: reg.exe reports a
        # rejected or partially applied import ONLY through its exit status, so
        # without this check a denied import looked exactly like a successful one
        # and the script still printed "Done!".
        $proc = Start-Process "reg.exe" -ArgumentList "import `"$tmp`"" -Wait -PassThru -WindowStyle Hidden
        if ($proc.ExitCode -ne 0) {
            throw "reg import failed with exit code $($proc.ExitCode) for $(Split-Path -Leaf $path)"
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

# R013: the installed locale generation is recorded so a later run knows what
# is actually in the registry. Without it, switching locale layered two
# translated menu trees (English keys stayed while Estonian keys were added),
# and uninstall without -Lang removed English REMs while a localized
# generation stayed orphaned.
# ponytail: marker is a plain file, not registry -- good until roaming profiles need it; then move to HKCU.
$markerPath = Join-Path $scriptDir ".installed_lang"
function Get-InstalledLang {
    # The marker is JSON {"lang": "...", "files": [...]} since R002; legacy
    # markers were plain language codes and carry no manifest.
    try {
        if (Test-Path -LiteralPath $markerPath) {
            $v = (Get-Content -LiteralPath $markerPath -Raw -ErrorAction Stop).Trim()
            if (-not $v) { return 'en' }
            if ($v.StartsWith('{')) {
                $rec = $v | ConvertFrom-Json
                if ($rec.lang) { return $rec.lang }
                return 'en'
            }
            return $v
        }
    } catch { }
    return 'en'
}
function Get-InstalledManifest {
    try {
        if (Test-Path -LiteralPath $markerPath) {
            $v = (Get-Content -LiteralPath $markerPath -Raw -ErrorAction Stop).Trim()
            if ($v.StartsWith('{')) {
                $rec = $v | ConvertFrom-Json
                return @($rec.files)
            }
        }
    } catch { }
    return @()
}
function Set-InstalledLang([string]$lang, [string[]]$files = @()) {
    # R002: fail-closed. A swallowed marker write used to print "Done!" while
    # the record still named the PREVIOUS generation, so the next run removed a
    # tree that was never installed and orphaned the one that was. The marker is
    # the only memory of what is actually in the registry; if it cannot be
    # written, the run has failed and must say so.
    $rec = @{ lang = $lang; files = @($files) } | ConvertTo-Json -Compress
    $tempMarker = Join-Path $scriptDir ([IO.Path]::GetRandomFileName())
    try {
        $data = [Text.Encoding]::UTF8.GetBytes($rec)
        $stream = [IO.File]::Open($tempMarker, 'CreateNew', 'Write', 'None')
        try { $stream.Write($data, 0, $data.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        if ([IO.File]::Exists($markerPath)) { [IO.File]::Replace($tempMarker, $markerPath, [NullString]::Value) }
        else { [IO.File]::Move($tempMarker, $markerPath) }
    } catch {
        throw "Could not record installed language '$lang' at ${markerPath}: $($_.Exception.Message)"
    } finally {
        if ([IO.File]::Exists($tempMarker)) { [IO.File]::Delete($tempMarker) }
    }
    # Prove the write landed before the run claims success.
    $written = $null
    try { $written = (Get-Content -LiteralPath $markerPath -Raw -ErrorAction Stop).Trim() } catch { }
    if (-not $written -or -not $written.StartsWith('{')) {
        throw "Installed-language marker did not persist the $lang manifest at $markerPath"
    }
}
function Clear-InstalledLang {
    if (Test-Path -LiteralPath $markerPath) { Remove-Item -LiteralPath $markerPath -Force -ErrorAction Stop }
}

# R002: snapshot of the managed registry state plus the marker, taken BEFORE
# the first mutation, so any mutation or marker failure can roll back to the
# exact pre-run bytes instead of leaving a half-applied generation behind.
function Read-RegSource([string]$Path) {
    $reader = New-Object IO.StreamReader($Path, [Text.Encoding]::Default, $true)
    try { return $reader.ReadToEnd() } finally { $reader.Dispose() }
}

function Get-ManagedRegistryState([object[]]$Files) {
    # Each explicit REG key is a mutation surface. HKCR is a merged view, so
    # capture both physical backings rather than guessing which one reg.exe uses.
    $keys = @{}
    foreach ($file in $Files) {
        $text = Read-RegSource $file.FullName
        foreach ($match in [regex]::Matches($text, '(?m)^\[(-?)(HKEY_[^\]]+)\]\s*$')) {
            $key = $match.Groups[2].Value
            $paths = if ($key.StartsWith('HKEY_CLASSES_ROOT\')) {
                @($key.Replace('HKEY_CLASSES_ROOT\', 'HKEY_CURRENT_USER\Software\Classes\'),
                  $key.Replace('HKEY_CLASSES_ROOT\', 'HKEY_LOCAL_MACHINE\Software\Classes\'))
            } else { @($key) }
            foreach ($p in $paths) {
                $keys[$p] = $true
                if ($match.Groups[1].Value -eq '-' -and (Test-Path -LiteralPath "Registry::$p")) {
                    Get-ChildItem -LiteralPath "Registry::$p" -Recurse -ErrorAction Stop | ForEach-Object { $keys[$_.Name] = $true }
                }
                # reg.exe also creates absent ancestors. Record those, but never
                # snapshot the values/subtrees of unrelated existing parents.
                $parent = $p
                while ($parent.Contains('\')) {
                    $parent = $parent.Substring(0, $parent.LastIndexOf('\'))
                    if (Test-Path -LiteralPath "Registry::$parent") { break }
                    $keys[$parent] = $true
                }
            }
        }
    }
    $state = @()
    foreach ($p in ($keys.Keys | Sort-Object)) {
        $exists = Test-Path -LiteralPath "Registry::$p"
        $values = @()
        if ($exists) {
            $key = Get-Item -LiteralPath "Registry::$p" -ErrorAction Stop
            try {
                $values = @($key.GetValueNames() | ForEach-Object {
                    [pscustomobject]@{ Name = $_; Kind = $key.GetValueKind($_)
                        Value = $key.GetValue($_, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) }
                })
            } finally { $key.Close() }
        }
        $state += [pscustomobject]@{ Path = "Registry::$p"; Presence = $(if($exists){'PRESENT'}else{'ABSENT'}); Values = $values }
    }
    return $state
}
function Restore-ManagedRegistryState($snapshot) {
    # Recreate removed keys first, then restore value names, types and data.
    foreach ($entry in @($snapshot | Where-Object Presence -EQ 'PRESENT' | Sort-Object { $_.Path.Length })) {
        if (-not (Test-Path -LiteralPath $entry.Path)) { New-Item -Path $entry.Path -Force -ErrorAction Stop | Out-Null }
        $physical = $entry.Path.Replace('Registry::', '')
        $split = $physical.IndexOf('\')
        $base = switch ($physical.Substring(0, $split)) {
            'HKEY_CURRENT_USER' { [Microsoft.Win32.Registry]::CurrentUser }
            'HKEY_LOCAL_MACHINE' { [Microsoft.Win32.Registry]::LocalMachine }
            'HKEY_USERS' { [Microsoft.Win32.Registry]::Users }
            'HKEY_CURRENT_CONFIG' { [Microsoft.Win32.Registry]::CurrentConfig }
            default { throw "Unsupported managed hive: $physical" }
        }
        $key = $base.OpenSubKey($physical.Substring($split + 1), $true)
        if (-not $key) { throw "Cannot reopen managed key for rollback: $physical" }
        try {
            foreach ($name in $key.GetValueNames()) {
                if (@($entry.Values | ForEach-Object Name) -notcontains $name) { $key.DeleteValue($name, $false) }
            }
            foreach ($v in $entry.Values) { $key.SetValue($v.Name, $v.Value, $v.Kind) }
            $key.Flush()
        } finally { $key.Close() }
    }
    # A failed install must not leave newly-created keys behind. Child-first
    # removal leaves unrelated existing ancestor trees untouched.
    foreach ($entry in @($snapshot | Where-Object Presence -EQ 'ABSENT' | Sort-Object { $_.Path.Length } -Descending)) {
        if (Test-Path -LiteralPath $entry.Path) { Remove-Item -LiteralPath $entry.Path -Recurse -Force -ErrorAction Stop }
    }
}

$failures = @()
$previousLang = Get-InstalledLang
$hadMarker = Test-Path -LiteralPath $markerPath

# Uninstall without -Lang removes the RECORDED generation, not English by
# default: that default orphaned every localized install. An explicit -Lang
# always wins, with a warning when it disagrees with the record.
$effectiveLang = $Lang
if ($Uninstall) {
    if (-not $effectiveLang -and $previousLang -ne 'en') {
        $effectiveLang = $previousLang
        Write-Host "Removing recorded '$previousLang' generation (no -Lang given)." -ForegroundColor Yellow
    } elseif ($effectiveLang -and $hadMarker -and $effectiveLang -ne $previousLang) {
        Write-Warning "Recorded installed language is '$previousLang' but -Lang '$effectiveLang' was given; trusting -Lang."
    }
}

# T-019: localized install AND uninstall -- use i18n/reg/<Lang>/ when it
# exists (generated by i18n/tools/gen_locale_reg.py from the 33-locale
# bundle). Both branches need it: localized regs rename shell keys, so the
# *_REM.REG counterparts must be the localized ones or uninstall orphans
# the renamed menu keys (reviewer finding, T-019).
$generation = 'en'
if ($effectiveLang) {
    $locDir = Join-Path $rootDir ("i18n\reg\" + $effectiveLang)
    if (Test-Path $locDir) {
        $registryDir = $locDir
        $generation = $effectiveLang
        Write-Host "Using localized registry files for language: $effectiveLang" -ForegroundColor Yellow
    } else {
        Write-Warning "No localized registry files for '$effectiveLang' at $locDir -- run: python i18n/tools/gen_locale_reg.py $effectiveLang ; falling back to English Registry/"
    }
}

# Resolve the old removal set now; execution starts only after full snapshot.
$previousRegFiles = @()
$prevDir = if ($previousLang -ne 'en') { Join-Path $rootDir ("i18n\reg\" + $previousLang) } else { Join-Path $rootDir 'Registry' }
if (-not $Uninstall -and $hadMarker -and $previousLang -ne $generation) {
    Write-Host "Removing previous '$previousLang' generation before installing '$generation'..." -ForegroundColor Yellow
    if (-not (Test-Path -LiteralPath $prevDir -PathType Container)) { throw "Previous generation source missing: $prevDir" }
    $previousRegFiles = @(Get-ChildItem -LiteralPath $prevDir -Filter '*_REM*.reg' -File)
    if (-not $previousRegFiles) { throw "Previous generation removal files missing: $prevDir" }
}

if ($Uninstall) {
    Write-Host "Uninstalling ContextMenu features..." -ForegroundColor Cyan
    # R002: the preflight needs the EXPECTED file set, not just what survived on
    # disk -- a deleted source REG would otherwise drop out of the list silently
    # and its key would never be removed. Enumerate by name from the directory
    # listing contract, then verify each one exists as a leaf before mutating.
    $expectedNames = @((Get-ChildItem -Path $registryDir -Filter "*_REM*.reg" -File).Name)
    $regFiles = @((Get-ChildItem -Path $registryDir -Filter "*_REM*.reg" -File))
} else {
    Write-Host "Installing ContextMenu features..." -ForegroundColor Green
    $expectedNames = @((Get-ChildItem -Path $registryDir -Filter "*.reg" -File | Where-Object { $_.Name -notmatch "_REM" -and $_.Name -notmatch "_UTF16" -and $_.Name -notmatch "_REWRITTEN" -and $_.Name -ne "FFMPEG.REG" }).Name)
    $regFiles = @((Get-ChildItem -Path $registryDir -Filter "*.reg" -File | Where-Object { $_.Name -notmatch "_REM" -and $_.Name -notmatch "_UTF16" -and $_.Name -notmatch "_REWRITTEN" -and $_.Name -ne "FFMPEG.REG" }))
}

# R002 PREFLIGHT: every REG source, agent-CLI requirement, launcher and
# destination is resolved BEFORE the first mutation. The manifest check is the
# teeth: the marker records WHICH files the installed generation applied, so a
# later run can refuse when any of them is gone -- the old preflight could not
# see a deleted source REG at all, because it simply drops out of the
# Get-ChildItem list and its keys silently survive the uninstall.
$missingSources = @()
$manifest = @(Get-InstalledManifest)
foreach ($name in $manifest) {
    if ([IO.Path]::GetFileName($name) -ne $name -or $name -notmatch '\.reg$') { throw "Invalid recorded REG source name: $name" }
    foreach ($dir in @($prevDir, $registryDir) | Select-Object -Unique) {
        if (-not (Test-Path -LiteralPath (Join-Path $dir $name) -PathType Leaf)) { $missingSources += "recorded generation: $name in $dir" }
    }
    if ($Uninstall -or $previousRegFiles.Count) {
        $removal = $name -replace '\.reg$', '_REM.REG'
        $removalDir = if ($Uninstall) { $registryDir } else { $prevDir }
        if (-not (Test-Path -LiteralPath (Join-Path $removalDir $removal) -PathType Leaf)) { $missingSources += "recorded removal: $removal in $removalDir" }
    }
}
if ($manifest.Count -gt 0) {
    $checkDirs = @()
    if ($Uninstall) {
        # The recorded generation's removal set must all be present.
        $checkDirs += @($registryDir)
        if ($registryDir -ne (Join-Path $rootDir 'Registry') -and (Test-Path (Join-Path $rootDir 'Registry'))) {
            $checkDirs += (Join-Path $rootDir 'Registry')
        }
    } else {
        $checkDirs += @($registryDir)
    }
    foreach ($name in $manifest) {
        $found = $false
        foreach ($d in $checkDirs) {
            if ((Test-Path -LiteralPath (Join-Path $d $name) -PathType Leaf)) { $found = $true; break }
        }
        if (-not $found) { $missingSources += "recorded generation: $name" }
    }
}
if (-not (Test-Path -LiteralPath $registryDir -PathType Container)) {
    $missingSources += "registry source directory missing: $registryDir"
}
if (Test-Path -LiteralPath $markerPath -PathType Container) { $missingSources += "installed language marker destination is a directory: $markerPath" }
# Resolve file references and interpreter launchers without executing any payload.
# Media executables are provisioned by setup; here only declared launcher files
# and commands used by the selected REG generation are prerequisites.
if (-not $Uninstall) {
    foreach ($file in $regFiles) {
        $text = Read-RegSource $file.FullName
        foreach ($match in [regex]::Matches($text, '%%ROOT%%((?:\\\\[^"\r\n\\]+)+\.(?:pyw|ps1|cmd|bat|exe))')) {
            $relative = $match.Groups[1].Value.Replace('\\', '\').TrimStart('\')
            if (-not (Test-Path -LiteralPath (Join-Path $rootDir $relative) -PathType Leaf)) { $missingSources += "$($file.Name) launcher: $relative" }
        }
        foreach ($cli in @('pyw.exe', 'powershell.exe', 'cmd.exe')) {
            if ($text -match [regex]::Escape($cli) -and -not (Get-Command $cli -ErrorAction SilentlyContinue)) { $missingSources += "$($file.Name) required CLI: $cli" }
        }
    }
}
if (-not (Get-Command reg.exe -ErrorAction SilentlyContinue)) { $missingSources += 'required CLI: reg.exe' }
if ($Uninstall -or $IncludeAgentMenus) {
    $agentMenuInstaller = Join-Path $scriptDir 'INSTALL_AI_AGENT_MENUS.PS1'
    if (-not (Test-Path -LiteralPath $agentMenuInstaller -PathType Leaf)) { $missingSources += "separate agent-menu installer: $agentMenuInstaller" }
    if ($IncludeAgentMenus -and -not $Uninstall) {
        if (-not (Test-Path -LiteralPath (Join-Path $rootDir 'Scripts/AI_AGENT_LAUNCHER.PS1') -PathType Leaf)) { $missingSources += 'agent-menu launcher: Scripts/AI_AGENT_LAUNCHER.PS1' }
        foreach ($cli in @('opencode.cmd','cline.cmd')) { if (-not (Get-Command $cli -ErrorAction SilentlyContinue)) { $missingSources += "agent-menu CLI: $cli" } }
    }
}
if ($missingSources.Count -gt 0) {
    Write-Host "FAILED: $($missingSources.Count) source file(s) missing before any mutation:" -ForegroundColor Red
    $missingSources | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
# Prove marker-file and directory write access before touching the registry.
# The probe is unrelated to the live marker and is removed before snapshotting.
if ([IO.File]::Exists($markerPath)) {
    $markerProbe = [IO.File]::Open($markerPath, 'Open', 'ReadWrite', 'Read')
    $markerProbe.Dispose()
}
$destinationProbe = Join-Path $scriptDir ([IO.Path]::GetRandomFileName())
try {
    $stream = [IO.File]::Open($destinationProbe, 'CreateNew', 'Write', 'None')
    try { $stream.Flush($true) } finally { $stream.Dispose() }
} finally { if ([IO.File]::Exists($destinationProbe)) { [IO.File]::Delete($destinationProbe) } }
# A failed reg import midway used to leave the previous locale generation half
# replaced with no way back; the snapshot plus rollback below restores the
# exact pre-run state instead.
$preRegistrySnapshot = Get-ManagedRegistryState @($previousRegFiles + $regFiles)
$preMarkerBytes = $null
if (Test-Path -LiteralPath $markerPath) {
    $preMarkerBytes = [System.IO.File]::ReadAllBytes($markerPath)
}

# Every failure is collected rather than thrown at the first one: a partially
# applied menu set is the state the user is actually in, and they need the whole
# list to fix it. But the script must not end up claiming success.
# ($failures was opened above: the previous-generation removal feeds it too.)
foreach ($file in @($previousRegFiles + $regFiles)) {
    Write-Host "Applying $($file.Name)..."
    try {
        Import-RegWithRoot $file.FullName
    } catch {
        $failures += "$($file.Name): $($_.Exception.Message)"
        Write-Warning $failures[-1]
    }
}


if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host "FAILED: $($failures.Count) operation(s) did not complete:" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    # R002: a mutation failure rolls the managed keys AND the marker back to
    # the exact pre-run bytes; the user gets one consistent state, not a
    # half-applied generation.
    try {
        Restore-ManagedRegistryState $preRegistrySnapshot
        if ($null -ne $preMarkerBytes) {
            [System.IO.File]::WriteAllBytes($markerPath, $preMarkerBytes)
        } else {
            Remove-Item -LiteralPath $markerPath -Force -ErrorAction SilentlyContinue
        }
        Write-Host "Rolled back to the pre-run registry and marker state." -ForegroundColor Yellow
    } catch {
        Write-Warning "Rollback itself failed: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 2
    exit 1
}

# Record the generation actually in the registry: a clean install stamps
# itself, and an uninstall with nothing left clears the record. A failed
# run leaves the previous marker untouched -- claiming a generation that
# only half-applied is how orphans started.
try {
    if ($Uninstall) { Clear-InstalledLang } else { Set-InstalledLang $generation @($regFiles | ForEach-Object { $_.Name }) }
} catch {
    # R002: the marker is part of the managed state. A failed marker write
    # rolls the whole run back -- publishing new keys under a stale record is
    # the orphan-generation defect, not a warning-worthy hiccup.
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    try {
        Restore-ManagedRegistryState $preRegistrySnapshot
        if ($null -ne $preMarkerBytes) {
            [System.IO.File]::WriteAllBytes($markerPath, $preMarkerBytes)
        } else {
            Remove-Item -LiteralPath $markerPath -Force -ErrorAction SilentlyContinue
        }
        Write-Host "Rolled back to the pre-run registry and marker state." -ForegroundColor Yellow
    } catch {
        Write-Warning "Rollback itself failed: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 2
    exit 1
}

# Agent menus own an explicitly separate transaction after core commit.
# Its failure never claims that the core generation was rolled back.
Write-Host "Core registry generation and marker committed."
if ($IncludeAgentMenus -or $Uninstall) {
    $agentMenuInstaller = Join-Path $scriptDir "INSTALL_AI_AGENT_MENUS.PS1"
    try {
        & $agentMenuInstaller -Agent All -Uninstall:$Uninstall
        if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) {
            throw "exit code $LASTEXITCODE"
        }
    } catch {
        $failures += "AI-agent context menus: $($_.Exception.Message)"
        Write-Warning $failures[-1]
    }
} else {
    Write-Host "Optional OpenCode/Cline menus skipped (use -IncludeAgentMenus after installing both CLIs)." -ForegroundColor Gray
}

if ($failures.Count -gt 0) {
    Write-Host "FAILED: separate AI-agent menu transaction; core generation remains committed." -ForegroundColor Red
    exit 1
}

Write-Host "Done!" -ForegroundColor Green
Start-Sleep -Seconds 2
exit 0
} finally {
    if ($registryMutationOwned) { $registryMutationMutex.ReleaseMutex() }
    $registryMutationMutex.Dispose()
}
