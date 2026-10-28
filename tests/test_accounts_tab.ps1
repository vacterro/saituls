# SAI Accounts tab (SAITULS): compile + contract checks.
#
# Proves the Accounts surface exists, is wired into the tab bar, never encodes
# provider ordinals, and -- when the control plane is deployed -- reads the
# canonical registry through sai-accounts.exe rather than the legacy files.
param([string]$SourcePath = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$source = if ($SourcePath) { $SourcePath } else { Join-Path $root 'SAITULS.cs' }
$failures = 0

function Assert([bool]$ok, [string]$what) {
    if ($ok) { Write-Host "PASS  $what" }
    else { Write-Host "FAIL  $what"; $script:failures++ }
}

# --- compiles with the reference the Accounts tab needs ------------------
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe" }
Assert (Test-Path $csc) 'csc.exe present (missing compiler is FAIL, never SKIP)'
if ($failures -gt 0) { exit 1 }

$out = Join-Path $env:TEMP ("saituls-accounts-tab-" + $PID + ".exe")
# SAITULS is SAITULS.cs plus the FolderAutomation module; compiling the single
# file alone would prove nothing about the binary that actually ships.
$module = @(Get-ChildItem -LiteralPath (Join-Path $root 'FolderAutomation') -Filter '*.cs' -File |
    Sort-Object Name | ForEach-Object { $_.FullName })
Assert ($module.Count -gt 0) 'the FolderAutomation module ships with the app'
& $csc -nologo -target:winexe -out:"$out" -optimize+ `
    -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll -r:System.Web.Extensions.dll `
    $source @module
Assert ($LASTEXITCODE -eq 0) 'SAITULS.cs plus Folder Automation compile with System.Web.Extensions'
Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue

$text = Get-Content -LiteralPath $source -Raw

# --- tab wiring ------------------------------------------------------------
Assert ($text -match 'TabNames\s*=\s*\{[^}]*"Accounts"') 'Accounts tab registered in TabNames'
Assert ($text -match 'case "Accounts":\s*DrawAccountsTab') 'Accounts tab dispatches to DrawAccountsTab'
Assert ($text -match 'void DrawAccountsTab') 'DrawAccountsTab defined'

# --- registry authority: canonical CLI only, no legacy file reads ----------
Assert ($text -match 'AccountsExe\(\)') 'engine resolution helper exists'
Assert ($text -match 'SAI",\s*"Accounts",\s*"bin",\s*"sai-accounts\.exe"') 'canonical engine path used'
Assert ($text -notmatch 'antigravity-accounts\.json') 'no legacy antigravity registry read in SAITULS.cs'
Assert ($text -notmatch 'SAI\\Antigravity\\accounts\.json') 'no legacy machine registry read in SAITULS.cs'

# --- no provider ordinals: N accounts must work with no code change ---------
$ordinalBranches = [regex]::Matches($text, '(?i)\b(AG|CL|CX)([1-9][0-9])\b')
Assert ($ordinalBranches.Count -eq 0) ('no provider ordinal literals (found: ' + (($ordinalBranches | ForEach-Object { $_.Value }) -join ', ') + ')')

# --- lifecycle verbs are the CLI contract ----------------------------------
foreach ($verb in @('launch', 'set-state', 'set-hidden', 'rename')) {
    Assert ($text -match [regex]::Escape($verb)) ("lifecycle verb routed to CLI: " + $verb)
}

# --- secret hygiene: the tab must never read credential material ----------
Assert ($text -notmatch '(?i)CredRead|CredentialBlob|Password') 'no credential material handled in the tab'

# --- two identity planes: console/quota vs IDE session ---------------------
# A static gate for the half of the contract a pixel test cannot reach: which
# WORDS and which COLOURS the source is allowed to use. A difference between the
# console account and the IDE account is a fact, so no field may encode it as a
# verdict and no mismatch sentence may exist to be looked up.
Assert ($text -notmatch 'IdentityMismatch') 'no IdentityMismatch field or verdict survives'
Assert ($text -notmatch 'RuntimeCollisionWith') 'no RuntimeCollisionWith field survives'
Assert ($text -notmatch '(?i)differs from configured account') 'the "differs from configured account" wording is gone'
Assert ($text -match '"ConsoleAccount"') 'the console plane has an operator label'
Assert ($text -match '"IdeAccount"') 'the IDE plane has an operator label'
Assert ($text -match 'ConsoleIdentity: \{') 'copied diagnostics carry ConsoleIdentity'
Assert ($text -match 'IdeIdentity: \{') 'copied diagnostics carry IdeIdentity as its own field'
Assert ($text -match 'IdentityFp = cur\.ConfiguredIdentityFp') 'the compatibility alias is written from the canonical plane only'
# The ONE warning: the quota duplicate, on both rows, in the danger colour.
# Anything else drawn in DANGERTXT next to an identity would re-introduce the
# false alarm this pass removed.
Assert ($text -match '"QuotaShared",\s*sel\.IdentitySharedWith\)[^;]*Palette\.DANGERTXT') 'the quota-duplicate line is the warning'
# Width fitting can wrap the text expression without changing its color.
# Keep the call/palette boundary exact, including the original unclipped form.
$ideNeutralPattern = 'DrawText\(g,\s*(?:ideDisp|Fit\(g,\s*ideDisp,[^;]*?\)),\s*valueX,\s*oy,\s*Palette\.TEXT2\b'
Assert ($text -match $ideNeutralPattern) 'the IDE account is drawn as a neutral fact, not a danger'
$badColor = [regex]::Replace($text, $ideNeutralPattern, {
    param($match)
    $match.Value.Replace('Palette.TEXT2', 'Palette.DANGERTXT')
})
Assert ($badColor -ne $text -and $badColor -notmatch $ideNeutralPattern) 'negative control: an IDE danger-color mutation is rejected'

# --- the provider fingerprint must reach the operator -----------------------
# The engine proves whose Google account a slot holds, and for a long time the
# tab rendered two AG rows both AUTHENTICATED with no way to tell they were one
# human. The fingerprint is the only thing that says so.
#
# That claim is NOT asserted here. A regex over this file proves the tab contains
# a particular string, not that a fingerprint ever reaches the row -- and one of
# the checks that stood here pinned the helper call itself, so a correct rewrite
# that read the JSON another way would have failed a perfectly good tab. The
# behaviour is covered where it can actually be observed: T29 in
# tests/saituls_accounts_flow_test.cs drives a real status reply through the
# real form and asserts the value lands on the row, including the unproven case
# and the diagnostics text an operator copies into a bug report. This suite
# stays the fast static gate; it no longer pretends to be the evidence.

# --- deployed plane: the canonical registry must answer -------------------
$engine = Join-Path $env:ProgramData 'SAI\Accounts\bin\sai-accounts.exe'
if (Test-Path $engine) {
    $json = & $engine list --all 2>$null
    Assert ($LASTEXITCODE -eq 0 -and $json -match '"accounts"') 'deployed engine answers list --all'
    Assert ($json -match 'antigravity:windows-user:[0-9a-f]{16}') 'canonical antigravity ids present'
    Assert ($json -notmatch 'AG2":\{') 'no ordinal-keyed account entries'
    $json = & $engine capabilities antigravity 2>$null
    Assert ($LASTEXITCODE -eq 0 -and $json -match '"supports_quota":true') 'engine capability query works'
} else {
    Write-Host 'NOTE  canonical engine not deployed; live registry checks did not apply (dev machines only)'
}

Write-Host ''
if ($failures -gt 0) { Write-Host "FAILURES: $failures"; exit 1 }
Write-Host 'Accounts tab checks PASS'
exit 0
