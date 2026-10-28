# P0 Phase-4: bounded, low-overhead desktop responsiveness probe.
#
# The operator reported the whole desktop collapsing to ~1-2 FPS around managed
# AG2 launches. Earlier work only measured SendInput submission time, which is
# NOT the same as rendered desktop frames. This probe captures a small, cheap
# set of whole-system signals so an AG2-closed baseline can be compared with an
# AG2-launch window, and it does so without a heavyweight polling loop that
# could itself cause a stall.
#
# It is deliberately bounded (~40 s) and read-only. It launches nothing; the
# caller decides what is running during each capture.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\desktop_freeze_probe.ps1 -Label baseline -Seconds 20
#
# Metrics (locale-neutral; this host has no perfmon/WMI-perf classes):
#   cpu_total_pct   whole-system busy %% from GetSystemTimes deltas
#   dwm_cpu_pct     DWM CPU %% = process CPU-time delta / (elapsed * ncpu)
#   processes/threads  counts and churn from Get-Process
#   input_burst_ms  synthetic mouse-move burst round trip (best of 3)
param(
    [string]$Label = 'capture',
    [int]$Seconds = 20,
    [string]$OutDir = "$PSScriptRoot\..\evidence"
)
$ErrorActionPreference = 'Stop'

Add-Type @"
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
public class DfbInput {
    [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT { public int dx, dy; public uint data, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Explicit, Size = 28)] public struct INPUTUNION { [FieldOffset(0)] public MOUSEINPUT mi; }
    [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public INPUTUNION u; }
    [DllImport("user32.dll")] public static extern uint SendInput(uint n, INPUT[] inputs, int size);
    public static double MouseBurst(int events) {
        var msg = new INPUT { type = 0, u = new INPUTUNION { mi = new MOUSEINPUT { flags = 0x0001 | 0x8000 } } };
        var one = new INPUT[] { msg };
        int size = Marshal.SizeOf(typeof(INPUT));
        var sw = Stopwatch.StartNew();
        for (int i = 0; i < events; i++) if (SendInput(1, one, size) != 1) throw new InvalidOperationException("SendInput unavailable");
        sw.Stop();
        return sw.Elapsed.TotalMilliseconds;
    }
}
public class DfbSys {
    [StructLayout(LayoutKind.Sequential)] struct FILETIME { public uint lo, hi; }
    [DllImport("kernel32.dll")] static extern bool GetSystemTimes(out FILETIME idle, out FILETIME kernel, out FILETIME user);
    static long L(FILETIME f) { return ((long)f.hi << 32) | f.lo; }
    // [idleTicks, kernelTicks, userTicks]; kernel includes idle.
    public static long[] Times() { FILETIME i, k, u; GetSystemTimes(out i, out k, out u); return new long[] { L(i), L(k), L(u) }; }
}
"@

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }

Write-Host "=== desktop_freeze_probe label=$Label seconds=$Seconds ==="
# Locale-neutral sampling. Perfmon counter paths are localized (this host is
# Russian) and its WMI perf classes are unavailable ("Invalid class"), so CPU is
# derived from GetSystemTimes deltas and DWM from its process CPU time.
function Stats($arr) {
    if (-not $arr -or @($arr).Count -eq 0) { return [pscustomobject]@{ min = $null; avg = $null; max = $null; n = 0 } }
    $a = @($arr | Sort-Object)
    return [pscustomobject]@{ min = [Math]::Round($a[0], 2); avg = [Math]::Round((@($arr) | Measure-Object -Average).Average, 2); max = [Math]::Round($a[-1], 2); n = @($arr).Count }
}
$cpu = @(); $dwm = @(); $procs = @(); $threads = @()
$ticks = [Math]::Max(3, [int]($Seconds / 2))
$numCpu = [Math]::Max(1, [Environment]::ProcessorCount)
$dwmProc = Get-Process dwm -ErrorAction SilentlyContinue | Select-Object -First 1
$prevSys = [DfbSys]::Times()
$prevDwm = if ($dwmProc) { try { $dwmProc.TotalProcessorTime.TotalMilliseconds } catch { 0 } } else { 0 }
$prevAt = Get-Date
for ($i = 0; $i -lt $ticks; $i++) {
    Start-Sleep -Seconds 2
    $now = Get-Date
    $elapsedMs = [Math]::Max(1.0, ($now - $prevAt).TotalMilliseconds)
    $curSys = [DfbSys]::Times()
    $dIdle = $curSys[0] - $prevSys[0]; $dKern = $curSys[1] - $prevSys[1]; $dUser = $curSys[2] - $prevSys[2]
    $dTotal = $dKern + $dUser
    if ($dTotal -gt 0) { $cpu += [Math]::Round(100.0 * (1.0 - ($dIdle / $dTotal)), 2) }
    if ($dwmProc) {
        try { $dwmProc.Refresh(); $curDwm = $dwmProc.TotalProcessorTime.TotalMilliseconds
              $dwm += [Math]::Round(100.0 * ($curDwm - $prevDwm) / ($elapsedMs * $numCpu), 2); $prevDwm = $curDwm } catch { }
    }
    $all = Get-Process -ErrorAction SilentlyContinue
    $procs += @($all).Count
    $threads += [int](($all | ForEach-Object { try { $_.Threads.Count } catch { 0 } }) | Measure-Object -Sum).Sum
    $prevSys = $curSys; $prevAt = $now
}
$result = [ordered]@{ label = $Label; seconds = $Seconds; captured = (Get-Date).ToString('o'); ncpu = $numCpu }
foreach ($map in @(@('cpu_total_pct', $cpu), @('dwm_cpu_pct', $dwm), @('processes', $procs), @('threads', $threads))) {
    $st = Stats $map[1]
    $result[$map[0]] = $st
    Write-Host ("{0,-20} min={1} avg={2} max={3} n={4}" -f $map[0], $st.min, $st.avg, $st.max, $st.n)
}

# Synthetic input round trip: best (min) of three bursts reduces "input stack
# waking up" noise; a saturated input pipeline (the 1-2 FPS symptom) would push
# this into the hundreds of ms and stay there.
$bursts = @()
for ($i = 0; $i -lt 3; $i++) {
    try { $bursts += [DfbInput]::MouseBurst(400) } catch { Write-Host "input injection unavailable: $($_.Exception.Message)" }
    Start-Sleep -Milliseconds 200
}
$best = if ($bursts.Count) { [Math]::Round(($bursts | Sort-Object)[0], 2) } else { $null }
$result['input_burst_best_ms'] = $best
$result['input_bursts_ms'] = @($bursts | ForEach-Object { [Math]::Round($_, 2) })
Write-Host ("input_burst_best_ms        = {0}  (all: {1})" -f $best, (($result['input_bursts_ms']) -join ', '))

# Process churn snapshot for this window: how many sai/agy/dwm procs exist now.
$result['sai_accounts_procs'] = @(Get-Process sai-accounts -ErrorAction SilentlyContinue).Count
$result['agy_procs'] = @(Get-Process agy -ErrorAction SilentlyContinue).Count
$result['dwm_threads'] = (Get-Process dwm -ErrorAction SilentlyContinue | Select-Object -First 1).Threads.Count

$path = Join-Path $OutDir ("desktop_freeze_{0}_{1}.json" -f $Label, (Get-Date -Format 'yyyyMMdd_HHmmss'))
$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $path -Encoding UTF8
Write-Host "wrote $path"
