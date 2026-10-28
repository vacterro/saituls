param(
    [string]$ScriptSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Installers\INSTALL_ALL.PS1'),
    [string]$GeneratorSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'i18n\tools\gen_locale_reg.py'),
    [string]$Sandbox = (Join-Path $env:TEMP ('saituls_locale_' + [Guid]::NewGuid().ToString('N')))
)

$ErrorActionPreference = 'Stop'

# Behavioural check for the installed-locale generation (R013).
#
# It runs the REAL Installers\INSTALL_ALL.PS1 against a throwaway tree, with
# exactly two substitutions: the self-elevation block is dropped (the sandbox
# must stay unelevated) and reg.exe is swapped for a recorder that logs the
# file name it was handed. Which generation gets imported, in what order, and
# what the marker says afterwards is the script's own unmodified logic.
#
# The defect: -Lang switched the source directory and nothing else, so
# installing English and then Estonian left both translated key trees in the
# registry, and `-Uninstall` without -Lang removed the English removal set
# while the localized generation stayed orphaned.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_locale_generation.ps1
# Exit: 0 = all PASS, 1 = failures.

function New-Replica([string]$src, [string]$dest) {
    $text = Get-Content -Raw -LiteralPath $src
    $text = $text -replace '(?s)\$isAdmin = .*?\r?\n\}\r?\n', "`$isAdmin = `$true`r`n"
    # The assignment prefix is optional on purpose: an older revision called
    # Start-Process without capturing it, and a recorder that only matches the
    # current shape would log nothing and report instrument failure as a defect.
    $text = $text -replace '(\$proc = )?Start-Process "reg\.exe".*',
        'Add-Content -LiteralPath (Join-Path $env:SAITULS_TEST_LOGDIR "imports.txt") -Value (Split-Path -Leaf $path); $proc = [pscustomobject]@{ ExitCode = 0 }'
    # The AI-agent menu installer is a separate surface; keep it out of scope.
    $text = $text -replace '(?s)if \(\$IncludeAgentMenus -or \$Uninstall\) \{.*?\n\} else \{\r?\n    Write-Host "Optional.*?\r?\n\}', ''
    Set-Content -LiteralPath $dest -Value $text -Encoding UTF8
}

function New-Tree([string]$root, [string]$src) {
    New-Item -ItemType Directory -Path (Join-Path $root 'Installers') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $root 'Registry') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $root 'i18n\reg\et') -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $root 'Installers\INSTALL_AI_AGENT_MENUS.PS1') -Value '# separate transaction fixture; not executed by recorder'
    foreach ($name in 'COPY_PATH', 'PACK') {
        foreach ($dir in (Join-Path $root 'Registry'), (Join-Path $root 'i18n\reg\et')) {
            Set-Content -LiteralPath (Join-Path $dir "$name.REG") -Value 'Windows Registry Editor Version 5.00' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $dir "${name}_REM.REG") -Value 'Windows Registry Editor Version 5.00' -Encoding Ascii
        }
    }
    New-Replica $src (Join-Path $root 'Installers\INSTALL_ALL.PS1')
}

function Invoke-Run([string]$root, [string[]]$argv) {
    $log = Join-Path $root 'log'
    New-Item -ItemType Directory -Path $log -Force | Out-Null
    Remove-Item -LiteralPath (Join-Path $log 'imports.txt') -Force -ErrorAction SilentlyContinue
    $env:SAITULS_TEST_LOGDIR = $log
    $out = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'Installers\INSTALL_ALL.PS1') @argv 2>&1
    $imports = @()
    if (Test-Path -LiteralPath (Join-Path $log 'imports.txt')) {
        $imports = @(Get-Content -LiteralPath (Join-Path $log 'imports.txt'))
    }
    $markerFile = Join-Path $root 'Installers\.installed_lang'
    $marker = if (Test-Path -LiteralPath $markerFile) { (Get-Content -Raw -LiteralPath $markerFile).Trim() } else { '<none>' }
    # R002: the marker is JSON {"lang":...,"files":[...]}; legacy plain codes
    # still read as the language itself.
    $markerLang = if ($marker -eq '<none>') { '<none>' } elseif ($marker.StartsWith('{')) { ([string](($marker | ConvertFrom-Json).lang)) } else { $marker }
    return [pscustomobject]@{ Imports = $imports; Marker = $markerLang; Output = ($out -join "`n") }
}

$fails = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

# --- generator: strict CLI and fail-closed publication (R014/R015) ----------
#
# The generator resolves its project root as parents[2] of its own file, so a
# copy at <tree>\i18n\tools\ turns <tree> into a throwaway project: its own
# Registry\, reg-map.json and string bundle, none of them the repository's.
#
# The defects: the documented --out PATH was read as a bare positional, so
# `--out DIR` created a literal directory named '--out' and silently dropped
# DIR; unknown flags were ignored; a missing label or bundle key only appended
# a line to the report while the half-substituted file was published anyway;
# and files were written straight into the destination, so a crash mid-loop
# left a mixed generation behind.

$labelCompress = -join @(0x0421, 0x0436, 0x0430, 0x0442, 0x044C | ForEach-Object { [char]$_ })
$labelMp4 = $labelCompress + ' ' + [char]0x0432 + ' MP4'

function Set-GenJson([string]$path, $value) {
    # No BOM: PowerShell 5.1's -Encoding UTF8 prepends one, and a generator
    # that merely crashed on the BOM would make every fail-closed assertion
    # below pass for the wrong reason.
    $json = $value | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function New-GenTree([string]$root, [string]$src) {
    New-Item -ItemType Directory -Path (Join-Path $root 'i18n\tools') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $root 'i18n\strings') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $root 'Registry') -Force | Out-Null
    Copy-Item -LiteralPath $src -Destination (Join-Path $root 'i18n\tools\gen_locale_reg.py')

    $header = 'Windows Registry Editor Version 5.00'
    # Localized install reg and a removal counterpart that deletes the same
    # localized key names: both must carry the whole mapped set.
    Set-Content -LiteralPath (Join-Path $root 'Registry\MENU.REG') -Encoding Unicode `
        -Value @($header, "[HKEY_CLASSES_ROOT\Directory\shell\$labelCompress]", "[HKEY_CLASSES_ROOT\Directory\shell\$labelMp4]")
    Set-Content -LiteralPath (Join-Path $root 'Registry\MENU_REM.REG') -Encoding Unicode `
        -Value @($header, "[-HKEY_CLASSES_ROOT\Directory\shell\$labelCompress]", "[-HKEY_CLASSES_ROOT\Directory\shell\$labelMp4]")
    # ASCII install reg whose removal counterpart deletes an ASCII key name and
    # therefore legitimately maps nothing: the expected no-op.
    Set-Content -LiteralPath (Join-Path $root 'Registry\PLAIN.REG') -Encoding Ascii `
        -Value @($header, '[HKEY_CLASSES_ROOT\Directory\shell\COPY PATH]')
    Set-Content -LiteralPath (Join-Path $root 'Registry\PLAIN_REM.REG') -Encoding Ascii `
        -Value @($header, '[-HKEY_CLASSES_ROOT\Directory\shell\CopyPathAsText]')
    # Nested labels only: the short label appears exclusively INSIDE the long
    # one. Presence must be judged against the original text, or substituting
    # the long label first makes the short one look absent and a legitimate
    # file gets refused.
    Set-Content -LiteralPath (Join-Path $root 'Registry\NESTED.REG') -Encoding Unicode `
        -Value @($header, "[HKEY_CLASSES_ROOT\Directory\shell\$labelMp4]")

    Set-GenJson (Join-Path $root 'i18n\reg-map.json') @{
        'MENU.REG'   = [ordered]@{ $labelCompress = 'menu.compress'; $labelMp4 = 'menu.mp4' }
        'PLAIN.REG'  = @{ 'COPY PATH' = 'menu.copy_path' }
        'NESTED.REG' = [ordered]@{ $labelCompress = 'menu.compress'; $labelMp4 = 'menu.mp4' }
    }
    Set-GenBundle $root @{ 'menu.compress' = 'Pakenda'; 'menu.mp4' = 'Pakenda MP4-ks'; 'menu.copy_path' = 'Kopeeri tee' }
}

function Set-GenBundle([string]$root, [hashtable]$strings) {
    Set-GenJson (Join-Path $root 'i18n\strings\strings.et.json') $strings
}

function Invoke-Gen([string]$root, [string[]]$argv) {
    Push-Location -LiteralPath $root
    # A native command writing to stderr is an ErrorRecord under
    # $ErrorActionPreference = 'Stop', which would abort the harness on the
    # very nonzero exits under test. Only the exit code decides here.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & python (Join-Path $root 'i18n\tools\gen_locale_reg.py') @argv 2>&1
        return [pscustomobject]@{ Code = $LASTEXITCODE; Output = (($out | ForEach-Object { $_.ToString() }) -join "`n") }
    } finally {
        $ErrorActionPreference = $previous
        Pop-Location
    }
}

function Get-GenState([string]$dir) {
    if (-not (Test-Path -LiteralPath $dir)) { return '<absent>' }
    ((Get-ChildItem -LiteralPath $dir -Force | Sort-Object Name | ForEach-Object {
        $_.Name + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }) -join '|')
}

function Get-GenText([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return '<absent>' }
    Get-Content -Raw -LiteralPath $path
}

function Test-Generator([string]$root, [string]$src) {
    New-GenTree $root $src
    $dest = Join-Path $root 'i18n\reg\et'

    $ok = Invoke-Gen $root @('et', '--out', $dest)
    Check 'documented --out PATH writes to PATH' `
        ($ok.Code -eq 0 -and (Get-ChildItem -LiteralPath $dest -ErrorAction SilentlyContinue).Count -eq 5) `
        "code=$($ok.Code) files=$((Get-ChildItem -LiteralPath $dest -ErrorAction SilentlyContinue).Count)"
    Check 'no literal --out directory is created' (-not (Test-Path -LiteralPath (Join-Path $root '--out'))) ''
    Check 'install and localized removal reg are both substituted' `
        ((Get-GenText (Join-Path $dest 'MENU.REG')) -match 'Pakenda MP4-ks' -and `
         (Get-GenText (Join-Path $dest 'MENU_REM.REG')) -match 'Pakenda MP4-ks' -and `
         (Get-GenText (Join-Path $dest 'MENU.REG')) -notmatch $labelCompress) ''
    Check 'an expected no-op removal mapping emits no warning' `
        ($ok.Output -notmatch '!!' -and (Get-GenText (Join-Path $dest 'PLAIN_REM.REG')) -match 'CopyPathAsText') `
        ($ok.Output -split "`n" | Where-Object { $_ -match '!!' })
    # A label that exists only nested inside a longer one is still present:
    # the long label wins the substitution and the short one is not an error.
    # The report counts substitutions PERFORMED, so the consumed short label
    # must not be counted as one -- 1 label here, never 2.
    Check 'a label nested inside a longer one is not reported absent' `
        ((Get-GenText (Join-Path $dest 'NESTED.REG')) -match 'Pakenda MP4-ks' -and `
         $ok.Output -notmatch 'NESTED\.REG.*required label absent' -and `
         $ok.Output -match 'NESTED\.REG: 1 labels') `
        ($ok.Output -split "`n" | Where-Object { $_ -match 'NESTED' })

    # Atomic replacement, not a merge into place: a file only the previous
    # generation had must be gone, and no staging directory may survive.
    # New-Item -Force so a generator that published nothing at all still lets
    # the remaining cases run and report instead of aborting the harness here.
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $dest 'STALE.REG') -Value 'stale' -Encoding Ascii
    $again = Invoke-Gen $root @('et', '--out', $dest)
    Check 'a successful run replaces the generation instead of merging' `
        ($again.Code -eq 0 -and -not (Test-Path -LiteralPath (Join-Path $dest 'STALE.REG')) -and `
         (Get-ChildItem -LiteralPath $dest).Count -eq 5) `
        "code=$($again.Code)"
    Check 'no staging or previous-generation directory survives' `
        (-not (Get-ChildItem -LiteralPath (Split-Path -Parent $dest) -Force -Directory | Where-Object { $_.Name -ne 'et' })) `
        ((Get-ChildItem -LiteralPath (Split-Path -Parent $dest) -Force -Directory | ForEach-Object Name) -join ',')

    # A real prior generation, so every "publishes nothing" assertion below is
    # a comparison of bytes that exist rather than absent == absent.
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $dest 'SENTINEL.REG') -Value 'previous generation' -Encoding Ascii
    $before = Get-GenState $dest
    $bad = Invoke-Gen $root @('et', '--out', $dest, '--bogus')
    Check 'an unknown argument exits nonzero' ($bad.Code -ne 0) "code=$($bad.Code)"
    # Two ways to violate this: overwrite the previous generation, or take the
    # flag for the output path and write the whole locale into a directory
    # literally named after it. Reject both.
    $stray = @(Get-ChildItem -LiteralPath $root -Force -Directory | Where-Object { $_.Name -like '--*' })
    Check 'an unknown argument publishes nothing' `
        ((Get-GenState $dest) -eq $before -and $stray.Count -eq 0) `
        (($stray | ForEach-Object Name) -join ',')

    Set-GenBundle $root @{ 'menu.compress' = 'Pakenda'; 'menu.copy_path' = 'Kopeeri tee' }
    # Default destination on purpose (no --out): it resolves to the same $dest,
    # so a fail-open generator writes its half-substituted files exactly where
    # the previous generation lives and the assertion below can see it. Passing
    # --out to a generator that reads a bare positional would divert the damage
    # into a literal '--out' directory and prove nothing.
    $gone = Invoke-Gen $root @('et')
    Check 'a removed mandatory bundle key exits nonzero' `
        ($gone.Code -ne 0 -and $gone.Output -match 'menu\.mp4') "code=$($gone.Code)"
    Check 'a removed mandatory bundle key leaves the previous generation untouched' `
        ((Get-GenState $dest) -eq $before) ''
    Set-GenBundle $root @{ 'menu.compress' = 'Pakenda'; 'menu.mp4' = 'Pakenda MP4-ks'; 'menu.copy_path' = 'Kopeeri tee' }

    # All-or-nothing removal set: a counterpart that deletes only SOME of its
    # parent's localized key names leaves the rest orphaned after uninstall --
    # the exact T-019 reviewer finding -- so it must not be published at all.
    # The absent label must not be a substring of a present one, or the
    # containment check would see it and this case would prove nothing.
    $rem = Join-Path $root 'Registry\MENU_REM.REG'
    Set-Content -LiteralPath $rem -Encoding Unicode `
        -Value @('Windows Registry Editor Version 5.00', "[-HKEY_CLASSES_ROOT\Directory\shell\$labelCompress]")
    $drift = Invoke-Gen $root @('et')
    Check 'a partially localized removal counterpart exits nonzero' `
        ($drift.Code -ne 0 -and $drift.Output -match 'required label absent') "code=$($drift.Code)"
    Check 'a partially localized removal counterpart publishes nothing' `
        ((Get-GenState $dest) -eq $before) ''
}

try {
    New-Tree $Sandbox $ScriptSource

    $r1 = Invoke-Run $Sandbox @()
    Check 'english install records generation en' ($r1.Marker -eq 'en') "marker=$($r1.Marker)"
    Check 'english install imports only the install set' `
        (($r1.Imports | Where-Object { $_ -match '_REM' }).Count -eq 0 -and $r1.Imports.Count -eq 2) `
        "imports=$($r1.Imports -join ',')"

    $r2 = Invoke-Run $Sandbox @('-Lang', 'et')
    $remCount = ($r2.Imports | Where-Object { $_ -match '_REM' }).Count
    Check 'locale switch purges the previous generation' ($remCount -eq 2) `
        "REMs=$remCount imports=$($r2.Imports -join ',')"
    Check 'locale switch records et' ($r2.Marker -eq 'et') "marker=$($r2.Marker)"
    Check 'purge happens before the new install' `
        ($r2.Imports.Count -eq 4 -and $r2.Imports[0] -match '_REM' -and $r2.Imports[-1] -notmatch '_REM') `
        "order=$($r2.Imports -join ',')"

    $r3 = Invoke-Run $Sandbox @('-Uninstall')
    Check 'uninstall without -Lang removes the recorded generation' `
        ($r3.Output -match "Removing recorded 'et' generation" -and ($r3.Imports | Where-Object { $_ -match '_REM' }).Count -eq 2) `
        "imports=$($r3.Imports -join ',')"
    Check 'uninstall clears the marker' ($r3.Marker -eq '<none>') "marker=$($r3.Marker)"

    Test-Generator (Join-Path $Sandbox 'gen') $GeneratorSource
} finally {
    Remove-Item -LiteralPath $Sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
