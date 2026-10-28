param(
    # The tree whose Registry\*.REG and Scripts\ are under test. Defaults to the
    # repository this file lives in; point it at an older checkout to use this
    # same harness as a red control.
    [string]$Repo = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition))
)

$ErrorActionPreference = 'Stop'

# Behavioural check for the Explorer menu launch contract.
#
# Two defects, both of which made a menu entry do nothing:
#   1. the .REG command lines started with a bare `pythonw.exe`. A shell verb
#      resolves a bare executable name only from the Windows directory and
#      System32 -- never from PATH, where a per-user Python lives -- so the
#      entry died with "Application not found" before the worker ever ran;
#   2. `"%1"` on a drive root arrives as `E:"` (whatever the letter), because the
#      trailing backslash escapes the closing quote. Stripping the quote leaves
#      `E:`, which is drive-RELATIVE: its abspath is the process's saved
#      directory for that drive, so a destructive worker could scan somewhere
#      else entirely. The tested root is discovered at run time -- see the case
#      table below for why the letter must not be hardcoded.
#
# The check builds a verb from the SHIPPED command line (same %%ROOT%%
# substitution IMPORT_SAFE performs), registers it under HKCU where the real
# shell resolves it, and invokes it through ShellExecuteEx. The worker is
# swapped for a recorder that only writes its argument, so nothing is deleted.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_shell_menu.ps1
# Exit: 0 = all PASS, 1 = failures.

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class SaiShellExec {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct SHELLEXECUTEINFO {
        public int cbSize; public uint fMask; public IntPtr hwnd;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpVerb;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpFile;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpParameters;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpDirectory;
        public int nShow; public IntPtr hInstApp; public IntPtr lpIDList;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpClass;
        public IntPtr hkeyClass; public uint dwHotKey; public IntPtr hIcon; public IntPtr hProcess;
    }
    [DllImport("shell32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern bool ShellExecuteExW(ref SHELLEXECUTEINFO lpExecInfo);
}
'@

$sandbox = Join-Path $env:TEMP ('saituls_menu_' + [Guid]::NewGuid().ToString('N'))
$out     = Join-Path $sandbox 'recorded.txt'
$fails   = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS  $name  $detail" } else { Write-Host "FAIL  $name  $detail"; $script:fails++ }
}

function Read-RegText([string]$path) {
    $raw = [IO.File]::ReadAllBytes($path)
    if ($raw.Length -ge 2 -and $raw[0] -eq 0xFF -and $raw[1] -eq 0xFE) {
        return [Text.Encoding]::Unicode.GetString($raw, 2, $raw.Length - 2)
    }
    return [Text.Encoding]::UTF8.GetString($raw)
}

# The command value for one class root, with %%ROOT%% substituted and .reg
# escaping undone -- i.e. exactly the string the shell would be handed.
function Get-ShippedCommand([string]$regPath, [string]$classRoot, [string]$root) {
    $text = (Read-RegText $regPath).Replace('%%ROOT%%', $root.Replace('\', '\\'))
    $inTarget = $false
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^\[HKEY_CLASSES_ROOT\\(.+?)\\shell\\[^\\]+\\command\]$') {
            $inTarget = ($Matches[1] -eq $classRoot)
            continue
        }
        if ($inTarget -and $line.StartsWith('@="')) {
            return $line.Substring(3, $line.Length - 4).Replace('\\', '\').Replace('\"', '"')
        }
    }
    return $null
}

try {
    New-Item -ItemType Directory -Path $sandbox -Force | Out-Null

    $shellArg = Join-Path $Repo 'Scripts\shell_arg.py'
    if (-not (Test-Path -LiteralPath $shellArg)) {
        Check 'Scripts\shell_arg.py exists' $false "not found under $Repo"
    } else {
        Copy-Item $shellArg $sandbox -Force
    }

    # The recorder's argument handling is the workers': shell_target(argv[1]).
    $recorder = Join-Path $sandbox 'recorder.pyw'
    @"
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from shell_arg import shell_target
except ImportError:
    def shell_target(raw):
        return raw.strip('"')
t = shell_target(sys.argv[1])
open(r'$out', 'w', encoding='utf-8').write(
    "raw=%r target=%r abspath=%r isdir=%s" % (sys.argv[1], t, os.path.abspath(t), os.path.isdir(t)))
"@ | Set-Content -LiteralPath $recorder -Encoding UTF8

    # DEL_JUNK is the only worker registered on Drive as well as Directory, so it
    # is the one that exposes the drive-root case.
    #
    # The drive letter is DISCOVERED, never hardcoded: any ready fixed drive root
    # reproduces the defect, because `"%1"` on a root always arrives with its
    # trailing backslash escaping the closing quote. A non-system drive is
    # preferred, because a shell-launched process can have its per-drive saved
    # directory for the SYSTEM drive sitting exactly at the root -- and then the
    # naive `.strip('"')` accidentally yields the right answer and the case stops
    # discriminating. Nothing is deleted either way: the worker is a recorder.
    $worker = Join-Path $Repo 'Scripts\DEL_JUNK.PYW'
    $systemRoot = ($env:SystemDrive + '\')
    $fixedRoots = @([IO.DriveInfo]::GetDrives() |
        Where-Object { $_.DriveType -eq 'Fixed' -and $_.IsReady } |
        Select-Object -ExpandProperty Name)
    $driveRoot = @($fixedRoots | Where-Object { $_ -ine $systemRoot })[0]
    if (-not $driveRoot) { $driveRoot = @($fixedRoots)[0] }
    if (-not $driveRoot) { $driveRoot = $systemRoot }
    $cases = @(
        @{ class = 'Drive';     target = $driveRoot; label = "drive root $driveRoot" }
        @{ class = 'Directory'; target = $sandbox;   label = 'folder' }
    )

    foreach ($case in $cases) {
        $shipped = Get-ShippedCommand (Join-Path $Repo 'Registry\DEL_JUNK.REG') $case.class $Repo
        if (-not $shipped) {
            Check "$($case.class): the .REG declares a command" $false 'no command value found'
            continue
        }
        $probe = $shipped.Replace($worker, $recorder)
        $verb  = "SaiMenuProbe$($case.class)"
        $key   = "HKCU:\Software\Classes\$($case.class)\shell\$verb"
        New-Item -Path "$key\command" -Force | Out-Null
        Set-ItemProperty -Path "$key\command" -Name '(default)' -Value $probe

        Remove-Item $out -Force -ErrorAction SilentlyContinue
        $si = New-Object SaiShellExec+SHELLEXECUTEINFO
        $si.cbSize = [Runtime.InteropServices.Marshal]::SizeOf($si)
        $si.fMask  = 0x00000400 -bor 0x00000040   # NO_UI | NOCLOSEPROCESS
        $si.lpVerb = $verb
        $si.lpFile = $case.target
        $si.nShow  = 0
        $launched = [SaiShellExec]::ShellExecuteExW([ref]$si)
        Start-Sleep -Milliseconds 1200
        Remove-Item $key -Recurse -Force -ErrorAction SilentlyContinue

        $launcher = ($probe -split ' ')[0]
        Check "$($case.label): the shell resolves the shipped launcher $launcher" $launched `
            $(if ($launched) { '' } else { "hInstApp=$([int]$si.hInstApp) -- the menu entry cannot start" })

        if (Test-Path $out) {
            $rec     = Get-Content $out -Raw
            $raw     = ([regex]::Match($rec, "raw='([^']*)'")).Groups[1].Value.Replace('\\', '\')
            $target  = ([regex]::Match($rec, "target='([^']*)'")).Groups[1].Value.Replace('\\', '\')
            $abspath = ([regex]::Match($rec, "abspath='([^']*)'")).Groups[1].Value.Replace('\\', '\')
            $isdir   = ([regex]::Match($rec, 'isdir=(\w+)')).Groups[1].Value
            $want    = if ($case.class -eq 'Drive') { $driveRoot } else { $case.target }
            # The assertion is on the NORMALIZER'S OWN OUTPUT, not on abspath.
            # abspath launders the defect: the naive `.strip('"')` yields the
            # drive-RELATIVE `E:`, whose abspath is this process's saved
            # directory for that drive -- and when that saved directory happens
            # to be the root, the wrong answer looks identical to the right one.
            # `target` distinguishes them unconditionally: `E:\` vs `E:`.
            Check "$($case.label): the worker receives the real target" `
                ($target -eq $want -and $abspath -eq $want -and $isdir -eq 'True') `
                "raw='$raw' target='$target' abspath='$abspath' isdir=$isdir"
        } else {
            Check "$($case.label): the worker receives the real target" $false 'the worker never ran'
        }
    }
} finally {
    foreach ($c in 'Drive', 'Directory') {
        Remove-Item "HKCU:\Software\Classes\$c\shell\SaiMenuProbe$c" -Recurse -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '---'
if ($fails) { Write-Host "FAILED ($fails failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
