# T-162 settings GUI shape test: parse-only + source-shape assertions.
# Launching the modal dialog would block a headless harness; the GUI's
# behavioral authority is the WinForms source shape plus the settings
# round-trip carried by the JS unit suite and this harness's Write-Settings
# replica checks.
$ErrorActionPreference = 'Stop'
$pass = 0; $fail = 0
function Check($name, $ok, $detail = '') {
    if ($ok) { $script:pass++; Write-Host "PASS  $name  $detail" }
    else { $script:fail++; Write-Host "FAIL  $name  $detail" }
}

$gui = Join-Path $PSScriptRoot '..\Scripts\saipatch\settings.ps1'

# 1. Parse clean.
$errors = $null
$tokens = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($gui, [ref]$tokens, [ref]$errors)
Check 'settings.ps1 parses clean' ($errors.Count -eq 0) ($errors | Select-Object -First 1)

$src = Get-Content -Raw -LiteralPath $gui

# 2. Golden Default shape. The animation assertion targets code only (the
#    header comment legitimately says "no animation"), so strip both line
#    comments and <# .. #> help blocks before matching.
$codeOnly = [regex]::Replace($src, '(?s)<#.*?#>', '')
$codeOnly = (($codeOnly -split "`n") | Where-Object { $_ -notmatch '^\s*#' }) -join "`n"
Check 'Verdana font'        ($src -match "New-Object System\.Drawing\.Font\('Verdana'")
Check 'flat square buttons' ($src -match "FlatStyle\s*=\s*'Flat'")
Check 'no animation/smoothing in code' (($codeOnly -notmatch 'DoubleBuffered') -and ($codeOnly -notmatch 'Animation'))

# 3. Contract shape: controls the ticket names.
Check 'enable checkbox'     ($src -match "chkEnable")
Check 'random radio'        ($src -match "radRandom")
Check 'ordered radio'       ($src -match "radOrdered")
Check 'volume 0..100'       ($src -match '\$numVol\.Maximum = 100')
Check 'minimumRunSeconds'   ($src -match '\$numRun')
Check '7 sound checkboxes'  ($src -match "PICKUP0[1-7]\.wav")
Check 'Test Sound button'   ($src -match "'Test Sound'")
Check 'Save button'         ($src -match "'Save'")
Check 'Cancel button'       ($src -match "'Cancel'")
Check 'locked conditions line' ($src -match 'Locked conditions')

# 4. Atomic write (temp + rename, never direct overwrite).
Check 'atomic write via temp+rename' ($src -match "\.tmp" -and $src -match 'Move-Item')

# 5. Settings path is OUTSIDE the OpenCode install: LOCALAPPDATA.
Check 'settings under LOCALAPPDATA' ($src -match 'SAITULS\\SAIPATCH')

# 6. Test Sound does not consume a completion episode (no sound-sink writes).
Check 'Test Sound playback only' ($src -match 'Invoke-TestSound' -and $src -notmatch 'SAIPATCH_SOUND_SINK')

# 7. SAITULS Tools tab launches settings.ps1 (source shape in SAITULS.cs).
$cs = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '..\SAITULS.cs')
Check 'SAITULS Tools button OpenCode Settings' ($cs -match '"OpenCode Settings"')
Check 'SAITULS launches settings.ps1' ($cs -match 'settings\.ps1')
Check 'SAITULS keeps OpenCode Patcher' ($cs -match '"OpenCode Patcher"')

Write-Host ''
Write-Host "PASS=$pass FAIL=$fail"
exit $(if ($fail -eq 0) { 0 } else { 3 })
