# console-title-guard contract, hermetic.
#
# A vendor TUI (Claude Code, Codex) owns the console title from the moment it
# starts, so a title written once at CreateProcess time is gone seconds later
# and two accounts of the same provider become indistinguishable windows.
# `sai-accounts console-title-guard` is the first process of that console: it
# labels the window, runs the CLI as its own child, re-asserts the identity for
# as long as the child lives, and exits with the child's exit code.
#
# The child here is a stand-in for the vendor: it rewrites the console title
# every 200 ms. It is deliberately FASTER than the guard's 500 ms re-assert, so
# the assertion is "identity is never lost for longer than a bound", not "the
# vendor never writes" (nothing can prevent the latter inside one console).
# A gate that cannot fail is not a gate, so the negative control runs the same
# child WITHOUT the guard and must show the identity lost for the whole run.
#
# ponytail: 500 ms poll ceiling. Upgrade to SetWinEventHook(EVENT_OBJECT_NAMECHANGE)
# if a real vendor ever rewrites the title fast enough for the bound to be visible.

param([int]$StaleMsLimit = 750, [string]$EnginePath = $env:SAI_TITLE_TEST_ENGINE)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$engine = if ($EnginePath) { $EnginePath } else { Join-Path $root 'Scripts\accounts\bin\sai-accounts.exe' }
$build = Join-Path $root 'Scripts\accounts\build.ps1'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }

$script:pass = 0
$script:fail = 0
function Check([string]$name, [bool]$ok, $detail = $null) {
    if ($ok) { $script:pass++; Write-Host ("PASS  " + $name) }
    else {
        $script:fail++
        Write-Host ("FAIL  " + $name)
        if ($null -ne $detail) { Write-Host ("      " + ($detail -join ' | ')) }
    }
}

if (-not (Test-Path $engine)) {
    Write-Host 'building sai-accounts.exe (not in bin)'
    & powershell -NoProfile -ExecutionPolicy Bypass -File $build | Out-Null
}
if (-not (Test-Path $engine)) { Write-Host 'FAIL  sai-accounts.exe unavailable'; exit 1 }

Add-Type @"
using System;using System.Text;using System.Runtime.InteropServices;
public class TGEnum {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr GetPropW(IntPtr h, string p);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
}
"@

# Every visible ConsoleWindowClass window with its owning pid, title and the
# SAITULS_ACCOUNT_ID atom property -- the same surface an operator sees.
function Get-ConsoleWindows {
    $rows = New-Object System.Collections.ArrayList
    $cb = [TGEnum+EnumProc] {
        param($h, $l)
        $cls = New-Object System.Text.StringBuilder 256
        [void][TGEnum]::GetClassNameW($h, $cls, 256)
        if ($cls.ToString() -eq 'ConsoleWindowClass') {
            $t = New-Object System.Text.StringBuilder 512
            [void][TGEnum]::GetWindowTextW($h, $t, 512)
            [uint32]$own = 0
            [void][TGEnum]::GetWindowThreadProcessId($h, [ref]$own)
            [void]$rows.Add([pscustomobject]@{
                hwnd  = $h.ToInt64()
                pid   = [int]$own
                title = $t.ToString()
                atom  = [TGEnum]::GetPropW($h, 'SAITULS_ACCOUNT_ID').ToInt64()
            })
        }
        return $true
    }
    [void][TGEnum]::EnumWindows($cb, [IntPtr]::Zero)
    return $rows
}

# Sample for $Ms milliseconds; report how long the window owned by $Pid stayed
# away from $Expect, and whether both titles were ever seen on it.
function Measure-Title([int]$OwningPid, [string]$Expect, [int]$Ms) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $worst = 0; $cur = 0; $sawExpect = 0; $sawOther = 0; $sawAtom = 0; $titles = @{}
    while ($sw.ElapsedMilliseconds -lt $Ms) {
        $row = (Get-ConsoleWindows | Where-Object { $_.pid -eq $OwningPid } | Select-Object -First 1)
        if ($row) {
            if (-not $titles.ContainsKey($row.title)) { $titles[$row.title] = 0 }
            $titles[$row.title]++
            if ($row.atom -ne 0) { $sawAtom++ }
            if ($row.title -eq $Expect) { $sawExpect++; if ($cur -gt $worst) { $worst = $cur }; $cur = 0 }
            else { $sawOther++; $cur += 50 }
        }
        Start-Sleep -Milliseconds 50
    }
    if ($cur -gt $worst) { $worst = $cur }
    return [pscustomobject]@{
        worstMs = $worst; sawExpect = $sawExpect; sawOther = $sawOther
        sawAtom = $sawAtom; titles = $titles
    }
}

# The vendor stand-in: rewrites the shared console title every 200 ms.
$work = Join-Path $env:TEMP 'saituls-title-guard-test'
if (-not (Test-Path $work)) { New-Item -ItemType Directory -Path $work | Out-Null }
$spamCs = Join-Path $work 'titlespam.cs'
$spamExe = Join-Path $work 'titlespam.exe'
@'
using System;using System.Runtime.InteropServices;using System.Threading;
class TitleSpam {
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern bool SetConsoleTitleW(string s);
  static int Main(string[] a) {
    int ms = a.Length > 0 ? int.Parse(a[0]) : 4000;
    int code = a.Length > 1 ? int.Parse(a[1]) : 0;
    int n = 0;
    while (n * 200 < ms) { SetConsoleTitleW("VENDOR_TUI_" + n.ToString()); Thread.Sleep(200); n++; }
    return code;
  }
}
'@ | Set-Content -Path $spamCs -Encoding UTF8
& $csc -nologo "-out:$spamExe" -target:exe $spamCs 2>&1 | Out-Null
if (-not (Test-Path $spamExe)) { Write-Host 'FAIL  vendor stand-in did not build'; exit 1 }

$expect = '[GUARD] Console title guard test'
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($expect))
$account = 'test:console-title-guard:alpha'

# ---------------------------------------------------------------- positive
$guard = Start-Process -FilePath $engine `
    -ArgumentList @('console-title-guard', '--title64', $b64, '--account', $account, '--', "`"$spamExe`"", '5000', '7') `
    -PassThru -WindowStyle Minimized
Start-Sleep -Milliseconds 400
$m = Measure-Title -OwningPid $guard.Id -Expect $expect -Ms 5200
$guard.WaitForExit(10000) | Out-Null
if (-not $guard.HasExited) { $guard.Kill() }

Check 'guard labels the console while the vendor child owns it' ($m.sawExpect -gt 0) @("titles: " + (($m.titles.Keys | Sort-Object) -join ' / '))
Check 'account identity is re-asserted, never lost longer than the bound' ($m.worstMs -le $StaleMsLimit) @("worst=$($m.worstMs)ms limit=${StaleMsLimit}ms")
Check 'window carries the SAITULS_ACCOUNT_ID property while labelled' ($m.sawAtom -gt 0) @("atom samples=$($m.sawAtom)")
Check 'guard exits with the child exit code' ($guard.ExitCode -eq 7) @("exit=$($guard.ExitCode)")

# ---------------------------------------------------------------- negative control
# Same vendor child, no guard: the identity must be lost for the whole run.
$bare = Start-Process -FilePath $spamExe -ArgumentList @('3000', '0') -PassThru -WindowStyle Minimized
Start-Sleep -Milliseconds 400
$c = Measure-Title -OwningPid $bare.Id -Expect $expect -Ms 3000
$bare.WaitForExit(8000) | Out-Null
if (-not $bare.HasExited) { $bare.Kill() }
$cVendorSeen = ($c.titles.Keys | Where-Object { $_ -like 'VENDOR_TUI_*' }).Count
Check 'control (no guard): the vendor title owns the window' ($cVendorSeen -gt 0 -and $c.sawExpect -eq 0) @("titles: " + (($c.titles.Keys | Sort-Object) -join ' / '))
Check 'control (no guard): identity absent for the whole run (gate can fail)' ($c.worstMs -gt $StaleMsLimit) @("worst=$($c.worstMs)ms")

# ---------------------------------------------------------------- argument shape
& $engine console-title-guard --title64 $b64 2>&1 | Out-Null
Check 'missing -- separator is refused' ($LASTEXITCODE -ne 0) @("exit=$LASTEXITCODE")
& $engine console-title-guard --title64 'not base64 !!!' --account $account -- cmd.exe /c exit 2>&1 | Out-Null
Check 'malformed --title64 is refused' ($LASTEXITCODE -ne 0) @("exit=$LASTEXITCODE")

Write-Host ("console-title-guard: {0} passed, {1} failed" -f $script:pass, $script:fail)
if ($script:fail -gt 0) { exit 1 }
exit 0
