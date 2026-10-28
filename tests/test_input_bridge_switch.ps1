# P0 regression: the Antigravity console input bridge is an explicit opt-in.
#
# The bridge installs process-global low-level keyboard and mouse hooks, and a
# global low-level hook is serviced on the installing thread. The operator
# reported the whole desktop collapsing to ~1-2 FPS around managed AG2 launches,
# and SAISPIN before that, but no per-process CPU/input measurement exists, so
# the hooks are a HYPOTHESIS -- and a hypothesis must not be shipped as the
# default. This test holds the line on two things:
#
#   1. CONTRACT (deterministic, always enforced): a default launch installs NO
#      hooks and the guard's own console window reports it (SAITULS_INPUT_BRIDGE
#      = 2 disabled / 1 installed). The title guard and the vendor session keep
#      working either way.
#   2. RESPONSIVENESS (measured, enforced with a deliberately generous bound):
#      synthetic input injection round-trip stays bounded while bridge-enabled
#      guards run. A bound this loose still catches the reported symptom (1-2 FPS
#      means ~500-1000 ms per event); it is not a claim of a measured speedup.
#
# It proves the switch and the desktop stays usable with the default. It does
# NOT replace the operator's own A/B on the degraded desktop; that experiment is
# theirs to authorize.

param(
    [string]$EnginePath = $env:SAI_ACCOUNTS_ENGINE,
    [int]$Samples = 120,
    [int]$KeyP95CeilingMs = 50,
    [int]$MouseBurstCeilingMs = 1500,
    [int]$MouseBurstEvents = 400
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$engine = if ($EnginePath) { $EnginePath } else { Join-Path $root 'Scripts\accounts\bin\sai-accounts.exe' }
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }

$script:pass = 0
$script:fail = 0
$script:blocked = 0
function Check([string]$name, [bool]$ok, $detail = $null) {
    if ($ok) { $script:pass++; Write-Host ("PASS  " + $name) }
    else {
        $script:fail++
        Write-Host ("FAIL  " + $name)
        if ($null -ne $detail) { Write-Host ("      " + $detail) }
    }
}

if (-not (Test-Path $engine)) {
    Write-Host 'building sai-accounts.exe (not in bin)'
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'Scripts\accounts\build.ps1') | Out-Null
}
if (-not (Test-Path $engine)) { Write-Host 'FAIL  sai-accounts.exe unavailable'; exit 1 }

$work = Join-Path $env:TEMP 'saituls-input-bridge-switch'
New-Item -ItemType Directory -Force -Path (Join-Path $work 'bin') | Out-Null
$agy = Join-Path $work 'bin\agy.exe'
# The same title-churn stand-in the title contract uses: a vendor that owns its
# own console title and can be held alive with a release file.
& $csc -nologo "-out:$agy" -r:System.dll -r:System.Web.Extensions.dll (Join-Path $root 'tests\console_title_churn.cs') 2>&1 | Out-Null
if (-not (Test-Path $agy)) { Write-Host 'FAIL  vendor stand-in did not build'; exit 1 }

Add-Type @"
using System;
using System.Text;
using System.Diagnostics;
using System.Runtime.InteropServices;
public class IBWin {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr GetPropW(IntPtr h, string p);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);

    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT { public int dx, dy; public uint data, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Explicit, Size = 32)] public struct INPUTUNION {
        [FieldOffset(0)] public KEYBDINPUT ki; [FieldOffset(0)] public MOUSEINPUT mi;
    }
    [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public INPUTUNION u; }
    [DllImport("user32.dll")] public static extern uint SendInput(uint n, INPUT[] inputs, int size);

    // Keyboard F24 down/up: a standard unused key, injected only into the
    // session's input queue. Round-trip time is the delay the desktop pays
    // before an injected event is accepted.
    public static double[] KeyLatency(int samples) {
        var ms = new double[samples];
        var down = new INPUT { type = 1, u = new INPUTUNION { ki = new KEYBDINPUT { wVk = 0x87 } } };
        var up = new INPUT { type = 1, u = new INPUTUNION { ki = new KEYBDINPUT { wVk = 0x87, dwFlags = 2 } } };
        var pair = new INPUT[] { down, up };
        int size = Marshal.SizeOf(typeof(INPUT));
        for (int i = 0; i < samples; i++) {
            var sw = Stopwatch.StartNew();
            if (SendInput(2, pair, size) != 2) throw new InvalidOperationException("SendInput unavailable");
            sw.Stop();
            ms[i] = sw.Elapsed.TotalMilliseconds;
            System.Threading.Thread.Sleep(1);
        }
        Array.Sort(ms);
        return ms;
    }

    public static double Percentile(double[] sorted, double p) {
        if (sorted == null || sorted.Length == 0) return -1;
        int idx = (int)Math.Min(sorted.Length - 1, Math.Max(0, Math.Round(p / 100.0 * (sorted.Length - 1))));
        return sorted[idx];
    }

    // A burst of absolute mouse moves to a fixed point: no buttons, no clicks,
    // no movement, but it drives the same low-level hook path a real desktop
    // does. Returns the total wall time for the burst.
    public static double MouseBurst(int events) {
        var msg = new INPUT { type = 0, u = new INPUTUNION { mi = new MOUSEINPUT { dx = 0, dy = 0, flags = 0x0001 | 0x8000 } } };
        var one = new INPUT[] { msg };
        int size = Marshal.SizeOf(typeof(INPUT));
        var sw = Stopwatch.StartNew();
        for (int i = 0; i < events; i++) {
            if (SendInput(1, one, size) != 1) throw new InvalidOperationException("SendInput unavailable");
        }
        sw.Stop();
        return sw.Elapsed.TotalMilliseconds;
    }
}
"@

$script:targetPid = 0
$script:bridgeProp = -1
$script:bridgeTitle = ''
function Read-GuardWindow([int]$OwnerPid) {
    $script:targetPid = $OwnerPid
    $script:bridgeProp = -1
    $script:bridgeTitle = ''
    $cb = [IBWin+EnumProc] {
        param($h, $l)
        $cls = New-Object System.Text.StringBuilder 256
        [void][IBWin]::GetClassNameW($h, $cls, 256)
        if ($cls.ToString() -eq 'ConsoleWindowClass') {
            [uint32]$own = 0
            [void][IBWin]::GetWindowThreadProcessId($h, [ref]$own)
            if ($own -eq $script:targetPid) {
                $script:bridgeProp = [IBWin]::GetPropW($h, 'SAITULS_INPUT_BRIDGE').ToInt64()
                $t = New-Object System.Text.StringBuilder 512
                [void][IBWin]::GetWindowTextW($h, $t, 512)
                $script:bridgeTitle = $t.ToString()
            }
        }
        return $true
    }
    [void][IBWin]::EnumWindows($cb, [IntPtr]::Zero)
    return [pscustomobject]@{ property = $script:bridgeProp; title = $script:bridgeTitle }
}

# Launch one real Antigravity guard for the stand-in vendor. `$BridgeEnv` and
# `$ExtraArgs` are the two reversible ways to reach the switch.
function Start-Guard([string]$label, [string]$digits, [string]$bridgeEnv, [string[]]$extraArgs) {
    $title = "[" + $label + "] Antigravity - owned input bridge regression"
    $account = "antigravity:windows-user:" + ($digits * 16)
    $result = Join-Path $work ("churn-" + $label + ".json")
    $release = Join-Path $work ("release-" + $label)
    if (Test-Path $release) { Remove-Item -Force $release }
    if (Test-Path $result) { Remove-Item -Force $result }
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($title))
    $env:SAI_ACCOUNTS_ROOT = $work
    $env:SAI_TITLE_FIXTURE_TITLE = $title
    $env:SAI_TITLE_FIXTURE_ACCOUNT = $account
    $env:SAI_TITLE_FIXTURE_RESULT = $result
    $env:SAI_TITLE_FIXTURE_RELEASE = $release
    if ($bridgeEnv) { $env:SAI_AG_CONSOLE_INPUT_BRIDGE = $bridgeEnv }
    else { Remove-Item Env:\SAI_AG_CONSOLE_INPUT_BRIDGE -ErrorAction SilentlyContinue }
    $arguments = @('console-title-guard', '--title64', $b64, '--account', $account) + $extraArgs +
                 @('--', "`"$agy`"", '--dangerously-skip-permissions')
    $process = Start-Process -FilePath $engine -ArgumentList $arguments -PassThru -WindowStyle Minimized
    return [pscustomobject]@{ process = $process; release = $release; title = $title }
}

function Stop-Guard($guard) {
    [IO.File]::WriteAllText($guard.release, 'release this owned fixture')
    if (-not $guard.process.WaitForExit(8000)) { $guard.process.Kill(); $guard.process.WaitForExit(3000) | Out-Null }
    $guard.process.Dispose()
}

function Settled-BridgeProperty($guard) {
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    $last = -1
    while ($deadline.ElapsedMilliseconds -lt 3000) {
        $info = Read-GuardWindow $guard.process.Id
        if ($info.property -gt 0) {
            $last = $info.property
            if ($info.title -eq $guard.title) { return [pscustomobject]@{ property = $last; title = $info.title } }
        }
        Start-Sleep -Milliseconds 50
    }
    return [pscustomobject]@{ property = $last; title = (Read-GuardWindow $guard.process.Id).title }
}

# Best of two consecutive batches per phase: a session's first injection batch
# after an idle period measures the input stack waking up, not the hook chain.
# Both phases get the same treatment so the off/on comparison stays fair.
function Measure-Input {
    $bestP95 = [double]::MaxValue; $bestBurst = [double]::MaxValue
    for ($round = 0; $round -lt 2; $round++) {
        $p95 = [IBWin]::Percentile([IBWin]::KeyLatency($Samples), 95)
        $burst = [IBWin]::MouseBurst($MouseBurstEvents)
        if ($p95 -lt $bestP95) { $bestP95 = $p95 }
        if ($burst -lt $bestBurst) { $bestBurst = $burst }
    }
    return [pscustomobject]@{ p95 = $bestP95; burst = $bestBurst }
}

$guards = New-Object System.Collections.ArrayList
try {
    # ---------------------------------------------------- contract: default OFF
    $a = Start-Guard 'IB1' '7' $null @()
    $guards.Add($a) | Out-Null
    $info = Settled-BridgeProperty $a
    Check 'a default launch installs no global input hooks (state 2)' ($info.property -eq 2) @("property=$($info.property)")
    Check 'the guard still owns the console title with the bridge disabled' ($info.title -eq $a.title) @("title='$($info.title)'")

    # ----------------------------------------- contract: env switch turns it ON
    $b = Start-Guard 'IB2' '8' '1' @()
    $guards.Add($b) | Out-Null
    $infoB = Settled-BridgeProperty $b
    Check 'SAI_AG_CONSOLE_INPUT_BRIDGE=1 installs the compatibility hooks (state 1)' ($infoB.property -eq 1) @("property=$($infoB.property)")

    Stop-Guard $a; $guards.Remove($a) | Out-Null

    # ------------------------------------- contract: explicit guard flag ON/OFF
    $c = Start-Guard 'IB3' '7' $null @('--input-bridge', 'on')
    $guards.Add($c) | Out-Null
    $infoC = Settled-BridgeProperty $c
    Check '--input-bridge on installs the hooks (state 1)' ($infoC.property -eq 1) @("property=$($infoC.property)")
    Stop-Guard $c; $guards.Remove($c) | Out-Null

    $d = Start-Guard 'IB4' '7' '1' @('--input-bridge', 'off')
    $guards.Add($d) | Out-Null
    $infoD = Settled-BridgeProperty $d
    Check '--input-bridge off overrides the environment (state 2)' ($infoD.property -eq 2) @("property=$($infoD.property)")

    # ------------------------------------------- measurement: input round trip
    # Guards for this phase: bridge DISABLED (the shipped default) and one
    # bridge-ENABLED guard for the A/B numbers. A guard whose child is the
    # churn stand-in lives until its release file appears.
    $off = Start-Guard 'IB5' '8' $null @()
    $guards.Add($off) | Out-Null
    Settled-BridgeProperty $off | Out-Null

    $offResult = $null
    try { $offResult = Measure-Input }
    catch { $script:blocked++; Write-Host ('BLOCKED  input injection unavailable: ' + $_.Exception.Message) }

    if ($null -ne $offResult) {
        Check 'input injection stays responsive with the default (hooks off)' `
            ($offResult.p95 -le $KeyP95CeilingMs -and $offResult.burst -le $MouseBurstCeilingMs) `
            @("p95=$($offResult.p95)ms ceiling=${KeyP95CeilingMs}ms burst=$($offResult.burst)ms ceiling=${MouseBurstCeilingMs}ms")

        # A/B with the hooks explicitly on (guard IB2). Reported, and bounded
        # only by a generous ceiling: this is evidence, not a claimed fix.
        $onResult = $null
        try { $onResult = Measure-Input } catch { }
        if ($null -ne $onResult) {
            Write-Host ("AB  bridge=off p95={0}ms burst={1}ms | bridge=on p95={2}ms burst={3}ms" -f `
                $offResult.p95, $offResult.burst, $onResult.p95, $onResult.burst)
            Check 'input injection stays within the coarse ceiling with the hooks on' `
                ($onResult.p95 -le ($KeyP95CeilingMs * 10) -and $onResult.burst -le ($MouseBurstCeilingMs * 4)) `
                @("p95=$($onResult.p95)ms burst=$($onResult.burst)ms")
        }
    }
}
finally {
    foreach ($g in $guards) { try { Stop-Guard $g } catch { } }
}

Write-Host ("input-bridge-switch: {0} passed, {1} failed, {2} blocked" -f $script:pass, $script:fail, $script:blocked)
if ($script:fail -gt 0) { exit 1 }
exit 0
