<#
.SYNOPSIS
    SAITULS Secure Apps launcher.

.DESCRIPTION
    Opens the Secure Apps window. secure_apps.pyw is a thin launcher for
    secure_apps_gui.py, which holds the window and hosts the single-instance
    session broker. This script owns no security logic either: it
    checks dependencies, reports a useful error when one is missing, and
    starts the subsystem. Exactly the boundary SCENARIOS.ps1 and
    saipatch/queue.ps1 use.

    NOT elevated. The broker runs at the user's normal integrity level so the
    protected application it launches does too; the only elevated component is
    the short-lived storage helper (sa_storage_helper.ps1), which raises one
    UAC prompt when a vault is first attached in a session.

.PARAMETER SelfTest
    Non-interactive probe. Prints one JSON object describing the validated
    registry, dependency status and the privileged-helper preflight, then
    exits. Starts no GUI, touches no disk image, prints no secret.
#>
[CmdletBinding()]
param(
    [switch]$SelfTest,
    [string]$Registry,
    [string]$ManagedRoot
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Gui = Join-Path $ScriptDir 'secure_apps.pyw'
$Cli = Join-Path $ScriptDir 'sa_cli.py'
$Helper = Join-Path $ScriptDir 'sa_storage_helper.ps1'

function Resolve-Python([string[]]$Names) {
    foreach ($name in $Names) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

$pythonw = Resolve-Python @('pythonw.exe', 'pythonw')
$python = Resolve-Python @('python.exe', 'python')

# ------------------------------------------------------------------ selftest
if ($SelfTest) {
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    $cliArgs = @($Cli)
    if ($Registry) { $cliArgs += @('--registry', $Registry) }
    if ($ManagedRoot) { $cliArgs += @('--managed-root', $ManagedRoot) }
    $cliArgs += 'selftest'
    $cliOut = $null
    $cliRc = 1
    if ($python) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $cliOut = & $python @cliArgs 2>&1 | Out-String
            $cliRc = $LASTEXITCODE
        } finally { $ErrorActionPreference = $prev }
    }
    $helperOut = $null
    if (Test-Path -LiteralPath $Helper) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $helperOut = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass `
                -File $Helper -SelfTest 2>&1 | Out-String
        } finally { $ErrorActionPreference = $prev }
    }
    $config = $null
    if ($cliOut) {
        $start = $cliOut.IndexOf('{')
        if ($start -ge 0) { try { $config = $cliOut.Substring($start) | ConvertFrom-Json } catch { } }
    }
    # NB: PowerShell variables are case-insensitive, so this must not be
    # called $helper -- that would clobber the $Helper script path above and
    # make the 'helper' presence check test a JSON object.
    $helperJson = $null
    if ($helperOut) {
        $start = $helperOut.IndexOf('{')
        if ($start -ge 0) { try { $helperJson = $helperOut.Substring($start) | ConvertFrom-Json } catch { } }
    }
    [pscustomobject]@{
        ok        = ($null -ne $python) -and ($null -ne $config) -and ($cliRc -eq 0)
        gui       = (Test-Path -LiteralPath $Gui -PathType Leaf)
        cli       = (Test-Path -LiteralPath $Cli -PathType Leaf)
        helper    = (Test-Path -LiteralPath $Helper -PathType Leaf)
        python    = $python
        pythonw   = $pythonw
        elevated  = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
                        [Security.Principal.WindowsBuiltInRole]::Administrator)
        config    = $config
        helper_preflight = $helperJson
    } | ConvertTo-Json -Depth 8 -Compress
    exit 0
}

# ------------------------------------------------------------------ launch
if (-not (Test-Path -LiteralPath $Gui -PathType Leaf)) {
    throw "Secure Apps window is missing: $Gui"
}
if (-not $pythonw -and -not $python) {
    throw "Python is required for Secure Apps but was not found on PATH. Open the SAITULS Home tab and run Install / repair."
}
$exe = if ($pythonw) { $pythonw } else { $python }

# Dependency preflight with a message that names the fix, not just the failure.
$preflightCmd = "import sys; sys.path.insert(0, r'$ScriptDir'); missing = []; " +
    "for m in ('PyQt6', 'cryptography', 'win32api'):" +
    "  try: __import__(m)" +
    "  except ImportError: missing.append(m);" +
    "try:" +
    "  import sa_auth; from importlib import metadata;" +
    "  v = metadata.version('fido2'); sa_auth.require_supported_fido2(v)" +
    "except Exception as e: missing.append('fido2>=2.0,<3 (%s)' % e);" +
    "if missing: print('MISSING: ' + ', '.join(missing)); sys.exit(1)"

$probe = & (if ($python) { $python } else { $exe }) -c $preflightCmd 2>&1
if ($LASTEXITCODE -ne 0) {
    throw ("Secure Apps dependency preflight failed: $probe`n" +
           "Install required packages with:`n" +
           "  pip install PyQt6 cryptography pywin32 ""fido2>=2.0,<3""")
}

$guiArgs = @($Gui)
if ($Registry) { $guiArgs += @('--registry', $Registry) }
if ($ManagedRoot) { $guiArgs += @('--managed-root', $ManagedRoot) }

Start-Process -FilePath $exe -ArgumentList $guiArgs -WorkingDirectory $ScriptDir
Write-Host "Started SAITULS Secure Apps." -ForegroundColor Cyan
exit 0
