param(
    [string]$GuiSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Installers\INSTALL_GUI.PS1'),
    [string]$CodexRemoverSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'Remove-CodexContextMenus.ps1')
)

$ErrorActionPreference = 'Stop'

# Behavioural check for the self-elevation result boundary (SRC-009 R016 W2-007).
#
# The defect: Installers\INSTALL_GUI.PS1 and Remove-CodexContextMenus.ps1 started
# an elevated copy of themselves with a bare `Start-Process -Verb RunAs` and then
# exited. The parent therefore reported completion of the LAUNCH, not of the
# registry operation: it exited 0 while the elevated child was still importing,
# and a dismissed UAC prompt was indistinguishable from a finished install.
#
# The elevated child cannot be started from a test, so each subject is replicated
# with (a) its admin probe pinned and (b) a stub `Start-Process` function that
# shadows the cmdlet, records the switches it was called with and then returns a
# chosen ExitCode, returns $null, or throws ERROR_CANCELLED. The parent's exit
# code and printed output are the script's own unmodified logic.
#
# The Remove-Codex child IS runnable: its HKLM root is rewritten to a throwaway
# HKCU key, so the machine's real shell configuration is never touched.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_self_elevation_result.ps1
# Exit: 0 = all PASS, 1 = failures.

$sandbox = Join-Path $env:TEMP ('saituls_elev_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
$testRoot = "HKCU:\Software\_SAITULS_ELEV_TEST_" + [Guid]::NewGuid().ToString('N')
$fails = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

function Set-AdminProbe([string]$text, [string]$value) {
    # Same idiom tests\test_legacy_cline.ps1 uses: pin the probe, keep the branch.
    $pattern = [regex]::new('(?ms)^\$isAdmin = .*?WindowsBuiltInRole\]::Administrator\s*\)')
    $updated = $pattern.Replace($text, "`$isAdmin = $value", 1)
    if ($updated -eq $text) { throw "could not isolate the admin probe in the subject" }
    return $updated
}

function New-ParentReplica([string]$src, [string]$dest, [string]$marker, [string]$behaviour) {
    $stub = @"
function Start-Process {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)] `$FilePath,
        `$ArgumentList,
        `$Verb,
        `$WindowStyle,
        [switch] `$Wait,
        [switch] `$PassThru
    )
    Set-Content -LiteralPath '$marker' -Value "wait=`$(`$Wait.IsPresent) passthru=`$(`$PassThru.IsPresent) verb=`$Verb" -Encoding Ascii
    $behaviour
}
"@
    $text = Set-AdminProbe (Get-Content -Raw -LiteralPath $src) '$false'
    # The stub goes immediately BEFORE the pinned probe, never at the top of the
    # file: a subject whose own param() block is no longer the first statement
    # does not parse, and `param` then runs as an unknown command while the rest
    # of the script continues -- instrument failure that reads as a subject pass.
    $anchor = '$isAdmin = $false'
    if (-not $text.Contains($anchor)) { throw "pinned probe anchor missing in $src" }
    $text = $text.Replace($anchor, $stub + "`r`n" + $anchor)
    Set-Content -LiteralPath $dest -Value $text -Encoding UTF8
    Assert-Parses $dest
}

# A replica that cannot be parsed proves nothing about the subject.
function Assert-Parses([string]$path) {
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errors)
    if ($errors -and $errors.Count -gt 0) {
        throw "replica $path does not parse: $($errors[0].Message)"
    }
}

function New-CodexChildReplica([string]$dest) {
    $text = Set-AdminProbe (Get-Content -Raw -LiteralPath $CodexRemoverSource) '$true'
    $text = $text.Replace('$classesRoot = "HKLM:\SOFTWARE\Classes"', "`$classesRoot = '$testRoot'")
    Set-Content -LiteralPath $dest -Value $text -Encoding UTF8
    Assert-Parses $dest
}

function Invoke-Subject([string]$path, [string[]]$arguments = @()) {
    # Native stderr arrives as error records, so with 'Stop' in force a subject
    # that writes one would abort the HARNESS. The child's exit code is the verdict.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $path @arguments 2>&1
        $text = ($out | ForEach-Object { "$_" }) -join "`n"
        # A broken replica produces CommandNotFound noise and then keeps running,
        # which would satisfy every "no success line" assertion for the wrong
        # reason. Instrument failure must abort the harness, not pass as evidence.
        if ($text -match 'is not recognized as the name of a cmdlet') {
            throw "replica $path failed to run as a script: $text"
        }
        return [pscustomobject]@{ Output = $text; Exit = $LASTEXITCODE }
    } finally { $ErrorActionPreference = $old }
}

# A parent that exited before the child finished, or swallowed its result, must
# never print a terminal success line -- that is the user-visible half of the bug.
function Test-NoSuccessLine([string]$output) {
    return -not ($output -match 'Done!' -or $output -match 'Done\.' -or $output -match 'Successfully')
}

$subjects = @(
    @{ Name = 'INSTALL_GUI.PS1'; Source = $GuiSource; Args = @() },
    @{ Name = 'Remove-CodexContextMenus.ps1'; Source = $CodexRemoverSource; Args = @('-IncludeLegacy') }
)

try {
    foreach ($subject in $subjects) {
        $name = $subject.Name
        $text = Get-Content -Raw -LiteralPath $subject.Source
        Check "$name elevates with -Wait -PassThru -ErrorAction Stop" `
            ($text -match '-Verb RunAs -Wait -PassThru -ErrorAction Stop')
        Check "$name exits with the elevated child's own code" `
            ($text -match 'exit \$elevated\.ExitCode')

        # The child's exit code must survive the privilege boundary unchanged.
        foreach ($code in 0, 1, 7) {
            $marker = Join-Path $sandbox ("marker_{0}_{1}.txt" -f $name, $code)
            $replica = Join-Path $sandbox ("parent_{0}_{1}.ps1" -f $name, $code)
            New-ParentReplica $subject.Source $replica $marker "return [pscustomobject]@{ ExitCode = $code }"
            $r = Invoke-Subject $replica $subject.Args
            Check "$name propagates child exit $code" ($r.Exit -eq $code) "exit=$($r.Exit)"
            $seen = if (Test-Path -LiteralPath $marker) { (Get-Content -Raw -LiteralPath $marker).Trim() } else { '<no call>' }
            Check "$name waited for the child and asked for the process object (code $code)" `
                ($seen -match 'wait=True' -and $seen -match 'passthru=True' -and $seen -match 'verb=RunAs') $seen
            if ($code -ne 0) {
                Check "$name prints no success line when the child failed ($code)" `
                    (Test-NoSuccessLine $r.Output) $r.Output
            }
        }

        # A dismissed UAC prompt is a failure, not a silent success.
        $marker = Join-Path $sandbox ("marker_{0}_cancel.txt" -f $name)
        $replica = Join-Path $sandbox ("parent_{0}_cancel.ps1" -f $name)
        New-ParentReplica $subject.Source $replica $marker 'throw (New-Object System.ComponentModel.Win32Exception 1223)'
        $r = Invoke-Subject $replica $subject.Args
        Check "$name exits nonzero when elevation is cancelled" ($r.Exit -ne 0) "exit=$($r.Exit)"
        Check "$name says nothing was changed on cancellation" ($r.Output -match 'Nothing was changed') $r.Output
        Check "$name prints no success line on cancellation" (Test-NoSuccessLine $r.Output) $r.Output

        # Start-Process can return nothing at all, and it can return a process
        # object that carries no ExitCode; `exit $null` is `exit 0`, so both are
        # the original bug reached by a different route.
        foreach ($case in @(
            @{ Label = 'no elevated process was returned'; Behaviour = 'return $null' },
            @{ Label = 'the elevated process reports no exit code'; Behaviour = 'return [pscustomobject]@{ ExitCode = $null }' }
        )) {
            $slug = ($case.Label -replace '[^a-z]', '')
            $marker = Join-Path $sandbox ("marker_{0}_{1}.txt" -f $name, $slug)
            $replica = Join-Path $sandbox ("parent_{0}_{1}.ps1" -f $name, $slug)
            New-ParentReplica $subject.Source $replica $marker $case.Behaviour
            $r = Invoke-Subject $replica $subject.Args
            Check "$name exits nonzero when $($case.Label)" ($r.Exit -ne 0) "exit=$($r.Exit)"
            Check "$name says nothing was changed when $($case.Label)" `
                ($r.Output -match 'Nothing was changed') $r.Output
            Check "$name prints no success line when $($case.Label)" (Test-NoSuccessLine $r.Output) $r.Output
        }
    }

    # The terminal success line belongs to the elevated child, after the mutation.
    $child = Join-Path $sandbox 'codex_child.ps1'
    New-CodexChildReplica $child
    $codexKey = "$testRoot\Directory\shell\CodexHere"
    New-Item -Path (Join-Path $codexKey 'command') -Force | Out-Null
    $r = Invoke-Subject $child
    Check 'the elevated Remove-Codex child exits 0 after removing the key' `
        ($r.Exit -eq 0 -and -not (Test-Path -LiteralPath $codexKey)) "exit=$($r.Exit)"
    Check 'the elevated Remove-Codex child is the only one printing success' `
        ($r.Output -match 'Done\.') $r.Output

    $gui = Get-Content -Raw -LiteralPath $GuiSource
    Check 'INSTALL_GUI reports its real result only after the dialog closes' `
        ($gui -match '(?ms)\$form\.ShowDialog\(\) \| Out-Null.*exit \$script:failed')
    Check 'INSTALL_GUI counts every failed and skipped operation into its exit code' `
        (([regex]::Matches($gui, '\$script:failed \+=')).Count -eq 3)
} finally {
    Remove-Item -Path $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
