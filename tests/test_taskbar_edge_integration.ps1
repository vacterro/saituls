<#
    Windows integration harness for TaskbarEdge.

    Validates against the real shell:
      - shell taskbar discovered
      - ABM_GETSTATE detects auto-hide
      - correct per-monitor taskbar resolved
      - reveal call preserves the foreground HWND
      - reveal call does not move or resize the taskbar
      - the taskbar hides again normally afterwards
      - one instance per session, --stop shuts it down
      - idle CPU stays negligible

    -AllowCursorMove additionally drives the real pointer into the bottom edge
    of every monitor and counts actual reveals (the 20-of-20 acceptance run).
    The cursor position is always restored in finally.

    -AllowShellStateChange lets the harness switch auto-hide ON for the duration
    of the run when the user has it off. The previous state is restored in
    finally; the user's preference is never changed permanently.
#>
param(
    [switch]$AllowCursorMove,
    [switch]$AllowShellStateChange,
    [switch]$AllowExplorerRestart,
    [int]$Reveals = 20,
    [int]$IdleSeconds = 20,
    [double]$MaxIdleCpuPercent = 1.0,
    [string]$Csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
)

$ErrorActionPreference = 'Stop'
$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $testsDir
$edgeDir = Join-Path $root 'Scripts\taskbar_edge'
$exe = Join-Path $edgeDir 'TaskbarEdge.exe'

$script:failures = 0
$script:skips = 0

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Host "PASS  $Name $Detail" }
    else { Write-Host "FAIL  $Name $Detail"; $script:failures++ }
}
function Skip([string]$Name, [string]$Why) {
    Write-Host "SKIP  $Name ($Why)"
    $script:skips++
}

Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class EdgeHarness
{
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] public struct APPBARDATA { public uint cbSize; public IntPtr hWnd; public uint uCallbackMessage; public uint uEdge; public RECT rc; public IntPtr lParam; }
    [StructLayout(LayoutKind.Sequential)] public struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags; }
    delegate bool MonEnum(IntPtr h, IntPtr hdc, ref RECT r, IntPtr d);

    [DllImport("shell32.dll")] static extern IntPtr SHAppBarMessage(uint m, ref APPBARDATA d);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] static extern IntPtr MonitorFromWindow(IntPtr h, uint f);
    [DllImport("user32.dll")] static extern bool GetMonitorInfo(IntPtr h, ref MONITORINFO mi);
    [DllImport("user32.dll")] static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonEnum cb, IntPtr d);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr v);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();

    const uint ABM_SETSTATE = 0x0000000A, ABM_GETSTATE = 0x00000004, ABM_GETAUTOHIDEBAREX = 0x0000000B;
    const uint ABE_BOTTOM = 3;
    public const int HiddenSliverSlackPx = 8;

    public static string DpiMode = "none";

    public static void Init()
    {
        try { if (SetProcessDpiAwarenessContext(new IntPtr(-4))) { DpiMode = "per-monitor-v2"; return; } } catch { }
        try { if (SetProcessDPIAware()) { DpiMode = "system"; return; } } catch { }
    }

    static APPBARDATA New() { APPBARDATA a = new APPBARDATA(); a.cbSize = (uint)Marshal.SizeOf(typeof(APPBARDATA)); return a; }

    public static long GetState() { APPBARDATA a = New(); return SHAppBarMessage(ABM_GETSTATE, ref a).ToInt64(); }
    public static bool AutoHide() { return (GetState() & 1) != 0; }

    // Only ever called by the harness under -AllowShellStateChange, and always
    // paired with a restore in finally.
    public static void SetState(long state)
    {
        APPBARDATA a = New();
        a.lParam = new IntPtr(state);
        SHAppBarMessage(ABM_SETSTATE, ref a);
    }

    public sealed class Mon
    {
        public long Handle; public int Left, Top, Right, Bottom; public bool Primary;
        public long Bar; public int BarLeft, BarTop, BarRight, BarBottom; public long BarMonitor;
        public string Rect { get { return Left + "," + Top + "," + Right + "," + Bottom; } }
        public string BarRect { get { return BarLeft + "," + BarTop + "," + BarRight + "," + BarBottom; } }
        public bool BarHidden { get { return BarTop >= Bottom - HiddenSliverSlackPx; } }
        public int MidX { get { return (Left + Right) / 2; } }
        public int MidY { get { return (Top + Bottom) / 2; } }
        public int EdgeY { get { return Bottom - 1; } }
    }

    public static List<Mon> Monitors()
    {
        List<Mon> list = new List<Mon>();
        MonEnum cb = delegate(IntPtr h, IntPtr hdc, ref RECT r, IntPtr d)
        {
            MONITORINFO mi = new MONITORINFO(); mi.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
            if (!GetMonitorInfo(h, ref mi)) return true;
            Mon m = new Mon();
            m.Handle = h.ToInt64();
            m.Left = mi.rcMonitor.Left; m.Top = mi.rcMonitor.Top; m.Right = mi.rcMonitor.Right; m.Bottom = mi.rcMonitor.Bottom;
            m.Primary = (mi.dwFlags & 1) != 0;
            APPBARDATA ab = New(); ab.uEdge = ABE_BOTTOM; ab.rc = mi.rcMonitor;
            IntPtr bar = SHAppBarMessage(ABM_GETAUTOHIDEBAREX, ref ab);
            m.Bar = bar.ToInt64();
            if (bar != IntPtr.Zero && IsWindow(bar))
            {
                RECT br; GetWindowRect(bar, out br);
                m.BarLeft = br.Left; m.BarTop = br.Top; m.BarRight = br.Right; m.BarBottom = br.Bottom;
                m.BarMonitor = MonitorFromWindow(bar, 0).ToInt64();
            }
            list.Add(m);
            return true;
        };
        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, cb, IntPtr.Zero);
        GC.KeepAlive(cb);
        return list;
    }

    public static Mon Refresh(long handle)
    {
        List<Mon> all = Monitors();
        foreach (Mon m in all) if (m.Handle == handle) return m;
        return null;
    }

    public static long Foreground() { return GetForegroundWindow().ToInt64(); }
    public static void Cursor(int x, int y) { SetCursorPos(x, y); }
    public static int[] CursorPos() { POINT p; GetCursorPos(out p); return new int[] { p.X, p.Y }; }
}
'@

[EdgeHarness]::Init()
Write-Host "INFO  harness dpi=$([EdgeHarness]::DpiMode)"

if (-not (Test-Path -LiteralPath $exe)) {
    & (Join-Path $edgeDir 'build.ps1') -Csc $Csc | Out-Null
}
Check 'TaskbarEdge.exe present' (Test-Path -LiteralPath $exe) $exe
if (-not (Test-Path -LiteralPath $exe)) { exit 1 }

$savedCursor = [EdgeHarness]::CursorPos()
$savedState = [EdgeHarness]::GetState()
$stateChanged = $false
$watcher = $null
$tmpOut = Join-Path ([IO.Path]::GetTempPath()) ("taskbar_edge_it_" + [Guid]::NewGuid().ToString('N') + '.txt')

function Invoke-Edge([string[]]$EdgeArgs) {
    $p = Start-Process -FilePath $exe -ArgumentList $EdgeArgs -Wait -PassThru -NoNewWindow -RedirectStandardOutput $tmpOut
    $text = ''
    if (Test-Path -LiteralPath $tmpOut) { $text = (Get-Content -LiteralPath $tmpOut -Raw) }
    return [pscustomobject]@{ ExitCode = $p.ExitCode; Output = $text }
}

function Wait-BarHidden([long]$Handle, [int]$TimeoutMs) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt $TimeoutMs) {
        $m = [EdgeHarness]::Refresh($Handle)
        if ($null -ne $m -and $m.BarHidden) { return $true }
        Start-Sleep -Milliseconds 40
    }
    return $false
}

function Wait-BarRevealed([long]$Handle, [int]$TimeoutMs) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt $TimeoutMs) {
        $m = [EdgeHarness]::Refresh($Handle)
        if ($null -ne $m -and -not $m.BarHidden) { return $true }
        Start-Sleep -Milliseconds 20
    }
    return $false
}

try {
    # --- auto-hide precondition -------------------------------------------
    $autoHide = [EdgeHarness]::AutoHide()
    Check 'ABM_GETSTATE readable' ($savedState -ge 0) "state=$savedState"
    if (-not $autoHide -and $AllowShellStateChange) {
        [EdgeHarness]::SetState($savedState -bor 1)
        $stateChanged = $true
        Start-Sleep -Milliseconds 700
        $autoHide = [EdgeHarness]::AutoHide()
        Write-Host "INFO  auto-hide temporarily enabled for this run (restored in finally)"
    }
    Check 'ABM_GETSTATE detects auto-hide' $autoHide "autohide=$autoHide"

    # --- discovery ---------------------------------------------------------
    $monitors = [EdgeHarness]::Monitors()
    Check 'monitors enumerated' ($monitors.Count -ge 1) "count=$($monitors.Count)"
    $withBar = @($monitors | Where-Object { $_.Bar -ne 0 })
    Check 'shell taskbar discovered' ($withBar.Count -ge 1) "bars=$($withBar.Count)/$($monitors.Count)"

    $perMonitorOk = $true
    foreach ($m in $withBar) {
        Write-Host "INFO  monitor $($m.Rect) primary=$($m.Primary) bar=$($m.Bar) rect=$($m.BarRect)"
        if ($m.BarMonitor -ne $m.Handle) { $perMonitorOk = $false }
    }
    Check 'correct per-monitor taskbar resolved' $perMonitorOk ''

    $negative = @($monitors | Where-Object { $_.Left -lt 0 -or $_.Top -lt 0 })
    if ($negative.Count -gt 0) {
        Check 'monitors with negative coordinates resolve a bar' (@($negative | Where-Object { $_.Bar -ne 0 }).Count -eq $negative.Count) "negative=$($negative.Count)"
    } else {
        Skip 'monitors with negative coordinates' 'this layout has none'
    }

    # --- helper self-test ---------------------------------------------------
    $st = Invoke-Edge @('--self-test')
    Write-Host ($st.Output.TrimEnd())
    Check 'TaskbarEdge --self-test exits 0' ($st.ExitCode -eq 0) "exit=$($st.ExitCode)"
    Check 'self-test reports no failures' ($st.Output -match 'SELFTEST failures=0') ''
    Check 'self-test: reveal preserves foreground HWND' ($st.Output -match 'PASS  reveal call preserves foreground HWND') ''
    Check 'self-test: reveal does not move the taskbar' ($st.Output -match 'PASS  reveal call does not move the taskbar') ''
    Check 'self-test: reveal does not resize the taskbar' ($st.Output -match 'PASS  reveal call does not resize the taskbar') ''

    # --- single instance ----------------------------------------------------
    $watcher = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 600
    Check 'watcher started' (-not $watcher.HasExited) "pid=$($watcher.Id)"

    $second = Start-Process -FilePath $exe -Wait -PassThru -WindowStyle Hidden
    Check 'second launch exits successfully' ($second.ExitCode -eq 0) "exit=$($second.ExitCode)"
    $live = @(Get-Process -Name 'TaskbarEdge' -ErrorAction SilentlyContinue)
    Check 'only one instance in the session' ($live.Count -eq 1) "processes=$($live.Count)"

    $stat = Invoke-Edge @('--status')
    Check 'status reports the watcher running' ($stat.Output -match 'running=true') ''
    Check 'status reports auto-hide state' ($stat.Output -match 'autohide=(on|off)') ''
    Check 'status reports taskbar validity' ($stat.Output -match 'taskbar_hwnd=\d+ valid=(true|false)') ''
    Check 'status reports a generation' ($stat.Output -match 'generation=') ''

    # --- live reveal --------------------------------------------------------
    if (-not $AllowCursorMove) {
        Skip 'bottom-edge reveal loop' 'pass -AllowCursorMove to drive the real pointer'
    } elseif (-not $autoHide) {
        Skip 'bottom-edge reveal loop' 'auto-hide is off'
    } else {
        $fgBefore = [EdgeHarness]::Foreground()
        $totalOk = 0
        $totalTried = 0
        foreach ($m in $withBar) {
            $ok = 0
            for ($i = 0; $i -lt $Reveals; $i++) {
                [EdgeHarness]::Cursor($m.MidX, $m.MidY) | Out-Null
                if (-not (Wait-BarHidden $m.Handle 2500)) { break }
                [EdgeHarness]::Cursor($m.MidX, $m.EdgeY) | Out-Null
                if (Wait-BarRevealed $m.Handle 900) { $ok++ }
                $totalTried++
            }
            $totalOk += $ok
            Check "bottom edge reveals on monitor $($m.Rect)" ($ok -eq $Reveals) "$ok/$Reveals"

            # Only this monitor's bar may be up.
            $others = @([EdgeHarness]::Monitors() | Where-Object { $_.Handle -ne $m.Handle -and $_.Bar -ne 0 -and -not $_.BarHidden })
            Check "no other monitor's taskbar revealed from $($m.Rect)" ($others.Count -eq 0) "others_up=$($others.Count)"

            [EdgeHarness]::Cursor($m.MidX, $m.MidY) | Out-Null
            Check "taskbar hides again after the cursor leaves $($m.Rect)" (Wait-BarHidden $m.Handle 3000) ''
        }
        Check 'foreground application kept focus across the reveal loop' ([EdgeHarness]::Foreground() -eq $fgBefore) ''
        if ($totalTried -gt 0 -and $totalOk -eq $totalTried -and $Reveals -ge 20) {
            Write-Host "BOTTOM_EDGE_REVEAL_20_OF_20=TRUE  ($totalOk/$totalTried across $($withBar.Count) monitors)"
        } else {
            Write-Host "BOTTOM_EDGE_REVEAL_20_OF_20=FALSE ($totalOk/$totalTried)"
        }
    }

    # --- Explorer restart recovery ------------------------------------------
    # Opt-in: restarting Explorer closes the user's open File Explorer windows.
    # The helper must survive it and resolve the NEW taskbar handles without a
    # SAITULS restart.
    if (-not $AllowExplorerRestart) {
        Skip 'Explorer restart recovery' 'pass -AllowExplorerRestart to kill and relaunch Explorer'
    } else {
        $before = (Invoke-Edge @('--status')).Output
        $barBefore = [regex]::Match($before, 'taskbar_hwnd=(\d+)').Groups[1].Value
        Stop-Process -Name 'explorer' -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        if (-not (Get-Process -Name 'explorer' -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath (Join-Path $env:WINDIR 'explorer.exe') | Out-Null
        }
        # Give the shell time to recreate every tray window.
        $sw = [Diagnostics.Stopwatch]::StartNew()
        while ($sw.Elapsed.TotalSeconds -lt 30) {
            $mons = [EdgeHarness]::Monitors()
            if (@($mons | Where-Object { $_.Bar -ne 0 }).Count -ge 1) { break }
            Start-Sleep -Milliseconds 500
        }
        Start-Sleep -Seconds 2

        Check 'watcher survives an Explorer restart' (-not $watcher.HasExited) "pid=$($watcher.Id)"
        $after = (Invoke-Edge @('--status')).Output
        $barAfter = [regex]::Match($after, 'taskbar_hwnd=(\d+)').Groups[1].Value
        Check 'a valid taskbar is resolved after the restart' ($after -match 'valid=true') "hwnd=$barAfter"
        Check 'the taskbar handle really changed' ($barBefore -ne $barAfter) "before=$barBefore after=$barAfter"

        if ($AllowCursorMove -and $autoHide) {
            $m = [EdgeHarness]::Monitors() | Where-Object { $_.Bar -ne 0 } | Select-Object -First 1
            if ($null -ne $m) {
                [EdgeHarness]::Cursor($m.MidX, $m.MidY) | Out-Null
                Wait-BarHidden $m.Handle 3000 | Out-Null
                [EdgeHarness]::Cursor($m.MidX, $m.EdgeY) | Out-Null
                Check 'reveal still works after the Explorer restart' (Wait-BarRevealed $m.Handle 1500) ''
                [EdgeHarness]::Cursor($m.MidX, $m.MidY) | Out-Null
            }
        } else {
            Skip 'reveal still works after the Explorer restart' 'needs -AllowCursorMove and auto-hide'
        }
    }

    # --- idle CPU -----------------------------------------------------------
    if ($IdleSeconds -le 0) {
        Skip 'idle CPU negligible' 'IdleSeconds=0'
    } else {
        [EdgeHarness]::Cursor($savedCursor[0], $savedCursor[1]) | Out-Null
        $proc = Get-Process -Id $watcher.Id -ErrorAction SilentlyContinue
        if ($null -eq $proc) {
            Check 'idle CPU negligible' $false 'watcher gone'
        } else {
            $t0 = $proc.TotalProcessorTime
            Start-Sleep -Seconds $IdleSeconds
            $proc.Refresh()
            $t1 = $proc.TotalProcessorTime
            $cpu = [Math]::Round((($t1 - $t0).TotalMilliseconds / ($IdleSeconds * 1000.0)) * 100.0, 3)
            Check 'idle CPU negligible' ($cpu -le $MaxIdleCpuPercent) "cpu=$cpu% over ${IdleSeconds}s (limit $MaxIdleCpuPercent%)"
            $ws = [Math]::Round($proc.WorkingSet64 / 1MB, 1)
            Write-Host "INFO  watcher working set ${ws} MB"
        }
    }

    # --- stop ---------------------------------------------------------------
    $stop = Invoke-Edge @('--stop')
    Check '--stop is accepted' ($stop.ExitCode -eq 0) $stop.Output.Trim()
    $gone = $watcher.WaitForExit(5000)
    Check 'watcher exits on --stop' $gone ''
    if ($gone) { $watcher = $null }

    # --- native hide still owns the bar -------------------------------------
    if ($autoHide) {
        $probe = $withBar | Select-Object -First 1
        if ($null -ne $probe) {
            [EdgeHarness]::Cursor($probe.MidX, $probe.MidY) | Out-Null
            Check 'taskbar hides again normally after the helper stops' (Wait-BarHidden $probe.Handle 3000) ''
        }
    } else {
        Skip 'taskbar hides again normally' 'auto-hide is off'
    }
}
finally {
    if ($null -ne $watcher -and -not $watcher.HasExited) {
        try { Start-Process -FilePath $exe -ArgumentList '--stop' -Wait -WindowStyle Hidden | Out-Null } catch { }
        try { if (-not $watcher.WaitForExit(3000)) { $watcher.Kill() } } catch { }
    }
    if ($stateChanged) {
        try { [EdgeHarness]::SetState($savedState) } catch { }
        Write-Host "INFO  auto-hide state restored to $savedState"
    }
    try { [EdgeHarness]::Cursor($savedCursor[0], $savedCursor[1]) | Out-Null } catch { }
    Remove-Item -LiteralPath $tmpOut -Force -ErrorAction SilentlyContinue
}

Write-Host "TASKBAR_EDGE_INTEGRATION failures=$script:failures skips=$script:skips"
if ($script:failures -eq 0) { Write-Host 'TASKBAR_EDGE_INTEGRATION_GREEN=TRUE'; exit 0 }
exit 1
