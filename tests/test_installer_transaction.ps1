param(
    [string]$ScriptSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Installers\INSTALL_ALL.PS1'),
    [string]$Sandbox = (Join-Path $env:TEMP ('saituls_installer_tx_' + [Guid]::NewGuid().ToString('N')))
)

$ErrorActionPreference = 'Stop'

# Behavioural check for the INSTALL_ALL.PS1 preflight / snapshot / marker
# contract (SRC-009 R002, CORE-002).
#
# It runs the REAL Installers\INSTALL_ALL.PS1 against a throwaway tree with the
# same two substitutions tests\test_locale_generation.ps1 uses (self-elevation
# dropped, reg.exe swapped for a recorder), plus one more: the recorder can be
# told to FAIL partway through, so a mutation failure mid-generation is
# reproducible without touching the machine's real registry at all.
#
# Three cases, each required to exit nonzero, print the failing component and
# leave the tree byte-identical to the pre-run snapshot:
#   (a) a missing source REG -- refused before the first mutation;
#   (b) a failing reg import midway -- the earlier "successful" imports are
#       rolled back;
#   (c) a read-only .installed_lang -- the marker write is part of the managed
#       state, so the whole run rolls back and reports the marker failure.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_installer_transaction.ps1
# Exit: 0 = all PASS, 1 = failures.

function New-Replica([string]$src, [string]$dest) {
    $text = Get-Content -Raw -LiteralPath $src
    $text = $text -replace '(?s)\$isAdmin = .*?\r?\n\}\r?\n', "`$isAdmin = `$true`r`n"
    # The recorder honours a fail-at name: the Nth import of that file exits 1
    # instead of recording, which is how case (b) forces a mid-run failure.
    $text = $text -replace '(\$proc = )?Start-Process "reg\.exe".*',
        (@'
if ($env:SAITULS_TEST_FAILREG -and (Split-Path -Leaf $path) -eq $env:SAITULS_TEST_FAILREG -and -not $env:SAITULS_TEST_FAILREG_DONE) {
    $env:SAITULS_TEST_FAILREG_DONE = '1'
    throw "reg import failed with exit code 1 for $(Split-Path -Leaf $path)"
}
Add-Content -LiteralPath (Join-Path $env:SAITULS_TEST_LOGDIR "imports.txt") -Value (Split-Path -Leaf $path); $proc = [pscustomobject]@{ ExitCode = 0 }
'@)
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

function Get-TreeState([string]$root) {
    ((Get-ChildItem -LiteralPath (Join-Path $root 'Registry'), (Join-Path $root 'Installers') -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { -not $_.PSIsContainer } | Sort-Object FullName | ForEach-Object {
            $_.Name + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
        }) -join '|')
}

function Invoke-Run([string]$root, [string[]]$argv, [string]$failReg = '') {
    $log = Join-Path $root 'log'
    New-Item -ItemType Directory -Path $log -Force | Out-Null
    Remove-Item -LiteralPath (Join-Path $log 'imports.txt') -Force -ErrorAction SilentlyContinue
    $env:SAITULS_TEST_LOGDIR = $log
    $env:SAITULS_TEST_FAILREG = $failReg
    $env:SAITULS_TEST_FAILREG_DONE = $null
    # Native stderr is not a terminating error here; the exit code and the
    # captured output are the verdict, exactly as the shipped script prints it.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'Installers\INSTALL_ALL.PS1') @argv 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
        $env:SAITULS_TEST_FAILREG = $null
        $env:SAITULS_TEST_FAILREG_DONE = $null
    }
    $imports = @()
    if (Test-Path -LiteralPath (Join-Path $log 'imports.txt')) {
        $imports = @(Get-Content -LiteralPath (Join-Path $log 'imports.txt'))
    }
    $markerFile = Join-Path $root 'Installers\.installed_lang'
    $marker = if (Test-Path -LiteralPath $markerFile -PathType Leaf) { (Get-Content -Raw -LiteralPath $markerFile).Trim() } else { '<none>' }
    return [pscustomobject]@{ Code = $code; Imports = $imports; Marker = $marker; MarkerLang = (Get-MarkerLang $marker); Output = (($out | ForEach-Object { $_.ToString() }) -join "`n") }
}

function Get-MarkerLang([string]$marker) {
    if ($marker -eq '<none>') { return '<none>' }
    if ($marker.StartsWith('{')) { return ([string](($marker | ConvertFrom-Json).lang)) }
    return $marker
}

$fails = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

try {
    New-Tree $Sandbox $ScriptSource
    $treeRoot = $Sandbox
    $markerFile = Join-Path $treeRoot 'Installers\.installed_lang'

    # --- sanity: the happy path still works on this replica ------------------
    $ok = Invoke-Run $treeRoot @()
    Check 'sanity: english install exits 0 and records en' `
        ($ok.Code -eq 0 -and $ok.MarkerLang -eq 'en' -and $ok.Imports.Count -eq 2) `
        "code=$($ok.Code) marker=$($ok.Marker) imports=$($ok.Imports -join ',')"
    $afterSanity = Get-TreeState $treeRoot

    # --- (a) a missing source REG is refused before any mutation -------------
    # The marker records WHICH files the en generation applied; deleting one of
    # them must make the uninstall refuse BEFORE any mutation, because its key
    # could otherwise never be removed (the old behavior silently "succeeded"
    # with the key left behind).
    Remove-Item -LiteralPath (Join-Path $treeRoot 'Registry\COPY_PATH.REG') -Force
    $missing = Invoke-Run $treeRoot @('-Uninstall')
    Check 'missing manifest source REG exits nonzero' ($missing.Code -ne 0) "code=$($missing.Code)"
    Check 'missing manifest source REG names the file' ($missing.Output -match 'COPY_PATH\.REG') ($missing.Output -split "`n" | Select-Object -First 4)
    Check 'missing manifest source REG mutates nothing' `
        (($missing.Imports.Count -eq 0) -and ($missing.MarkerLang -eq 'en')) `
        "imports=$($missing.Imports -join ',') marker=$($missing.Marker)"
    # restore the file for the later cases
    Set-Content -LiteralPath (Join-Path $treeRoot 'Registry\COPY_PATH.REG') -Value 'Windows Registry Editor Version 5.00' -Encoding Ascii

    # --- (b) a failing reg import midway rolls back --------------------------
    $failAt = Invoke-Run $treeRoot @('-Lang', 'et')   # establish et as recorded
    if ($failAt.Code -ne 0) { throw "could not establish the et generation: $($failAt.Output)" }
    $midfail = Invoke-Run $treeRoot @('-Uninstall')
    Check 'uninstall on a good tree succeeds' ($midfail.Code -eq 0) "code=$($midfail.Code) output=$($midfail.Output -split "`n" | Select-Object -Last 2)"
    # re-establish en, then force the SECOND import to fail and prove rollback
    $reen = Invoke-Run $treeRoot @()
    if ($reen.Code -ne 0) { throw "could not re-establish the en generation: $($reen.Output)" }
    $beforeFail = Get-TreeState $treeRoot
    $forced = Invoke-Run $treeRoot @('-Uninstall') -failReg 'PACK_REM.REG'
    Check 'a mid-run import failure exits nonzero' ($forced.Code -ne 0) "code=$($forced.Code)"
    Check 'a mid-run import failure names the failing import' ($forced.Output -match 'PACK_REM\.REG') ($forced.Output -split "`n" | Select-Object -First 4)
    Check 'a mid-run import failure leaves the tree byte-identical' `
        ((Get-TreeState $treeRoot) -eq $beforeFail) 'tree hash mismatch'
    Check 'a mid-run import failure preserves the marker' ($forced.MarkerLang -eq 'en') "marker=$($forced.MarkerLang)"

    # --- (c) an unwritable marker fails the run and rolls back ---------------
    # A read-only attribute is NOT enough: Set-Content -Force overwrites it
    # (proved on this machine), so the marker path is made unavailable the way
    # a real failure does -- the path is occupied by a directory, so the write
    # throws regardless of Force.
    $beforeRo = Get-TreeState $treeRoot
    $preBytes = [System.IO.File]::ReadAllBytes($markerFile)
    Remove-Item -LiteralPath $markerFile -Force
    New-Item -ItemType Directory -Path $markerFile -Force | Out-Null
    $roRun = Invoke-Run $treeRoot @('-Lang', 'et')
    Remove-Item -LiteralPath $markerFile -Recurse -Force -ErrorAction SilentlyContinue
    [System.IO.File]::WriteAllBytes($markerFile, $preBytes)
    Check 'an unwritable marker exits nonzero' ($roRun.Code -ne 0) "code=$($roRun.Code)"
    Check 'an unwritable marker failure names the marker' ($roRun.Output -match 'installed language') ($roRun.Output -split "`n" | Select-Object -First 4)
    Check 'an unwritable marker failure leaves the tree byte-identical' `
        ((Get-TreeState $treeRoot) -eq $beforeRo) 'tree hash mismatch'
}
finally {
    Remove-Item -LiteralPath $Sandbox -Recurse -Force -ErrorAction SilentlyContinue
    $env:SAITULS_TEST_FAILREG = $null
    $env:SAITULS_TEST_FAILREG_DONE = $null
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails check(s) failed)"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
