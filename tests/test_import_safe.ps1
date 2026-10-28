param(
    # The IMPORT_SAFE.PS1 under test. Point it at an older checkout to use this
    # same harness as a red control.
    [string]$Importer = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Registry\IMPORT_SAFE.PS1')
)

$ErrorActionPreference = 'Continue'

# Behavioural check for the %%ROOT%% import wrapper.
#
# The defect: the wrapper substituted the toolkit root into the .reg text as a
# raw path. Inside quoted .reg VALUE data a backslash is an escape character, so
# `V:\a\b` produced value lines reg.exe could not parse. reg.exe SKIPS an
# unparseable value line and still exits 0 -- so every import reported
# "Imported OK", wrote no command value, and left whatever an earlier install had
# put there. That is how the Explorer menus kept pointing at a stale command
# after the .REG files had been corrected.
#
# The harness imports through the real wrapper into a throwaway HKCU key and
# reads the value back, because the exit code is exactly the signal that lied.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_import_safe.ps1
# Exit: 0 = all PASS, 1 = failures.

$sandbox = Join-Path $env:TEMP ('saituls_import_' + [Guid]::NewGuid().ToString('N'))
$testKey = '_SAITULS_IMPORT_TEST_' + [Guid]::NewGuid().ToString('N')
$hkcu    = "HKEY_CURRENT_USER\Software\$testKey"
$root    = $null                              # set inside the try, under $sandbox
$fails   = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

function Read-Back([string]$name) {
    try {
        return (Get-ItemProperty -LiteralPath "Registry::$hkcu\shell\Probe\command" -ErrorAction Stop).$name
    } catch { return $null }
}

try {
    New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
    # The wrapper resolves -Root on disk, so the fake root has to exist. Spaces
    # and several separators are deliberate: those are what break naive escaping.
    $root = Join-Path $sandbox 'a b\__K\__CODE\__SAITULS'
    New-Item -ItemType Directory -Path $root -Force | Out-Null

    # A .REG in the repository's own shape: %%ROOT%%-tokenized command value.
    $reg = Join-Path $sandbox 'PROBE.REG'
    $lines = @(
        'Windows Registry Editor Version 5.00'
        ''
        "[$hkcu\shell\Probe]"
        '@="PROBE"'
        ''
        "[$hkcu\shell\Probe\command]"
        '@="pyw.exe \"%%ROOT%%\\Scripts\\DEL_JUNK.PYW\" \"%1\""'
        ''
    )
    [IO.File]::WriteAllText($reg, ($lines -join "`r`n"), [Text.Encoding]::UTF8)

    $out = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass `
        -File $Importer -File $reg -Root $root 2>&1
    $rc = $LASTEXITCODE
    $text = ($out | ForEach-Object { "$_" }) -join ' '

    $expected = "pyw.exe `"$root\Scripts\DEL_JUNK.PYW`" `"%1`""
    $actual = Read-Back '(default)'

    Check 'the import writes the command value it claimed to write' `
        ($actual -eq $expected) "value=$(if ($null -eq $actual) { '<NOT WRITTEN>' } else { "'$actual'" })"

    # The wrapper must not report success when the value did not land: a green
    # exit code with an empty key is the failure mode this test exists for.
    Check 'success is not reported when a value did not land' `
        (($actual -eq $expected) -or ($rc -ne 0)) "exit=$rc said='$($text.Trim())'"

    # A second identical import must stay correct (installers re-run).
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass `
        -File $Importer -File $reg -Root $root 2>&1 | Out-Null
    Check 're-importing the same file is idempotent' ((Read-Back '(default)') -eq $expected)
} finally {
    reg delete "HKCU\Software\$testKey" /f 2>&1 | Out-Null
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
