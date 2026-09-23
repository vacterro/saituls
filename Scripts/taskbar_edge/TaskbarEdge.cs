using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

// TaskbarEdge -- reliable bottom-edge reveal for an auto-hidden Windows taskbar.
//
// Non-elevated, per-user, same interactive session as Explorer. One instance per
// session (named mutex). No window, no message loop, no hook: a bounded poll of
// the real cursor position, the monitor under it, and the documented appbar
// lookup for that monitor's auto-hide bar.
//
// All geometry and state decisions live in EdgeLogic.cs so they can be tested
// without a desktop. This file is the Win32 adapter and the process shell.
//
// Build: Scripts\taskbar_edge\build.ps1

namespace Saituls.TaskbarEdge
{
    static class Win32
    {
        [StructLayout(LayoutKind.Sequential)]
        public struct RECT { public int Left, Top, Right, Bottom; }

        [StructLayout(LayoutKind.Sequential)]
        public struct POINT { public int X, Y; }

        [StructLayout(LayoutKind.Sequential)]
        public struct APPBARDATA
        {
            public uint cbSize;
            public IntPtr hWnd;
            public uint uCallbackMessage;
            public uint uEdge;
            public RECT rc;
            public IntPtr lParam;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct MONITORINFO
        {
            public int cbSize;
            public RECT rcMonitor;
            public RECT rcWork;
            public uint dwFlags;
        }

        public delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, ref RECT rect, IntPtr data);

        [DllImport("shell32.dll")]
        public static extern IntPtr SHAppBarMessage(uint dwMessage, ref APPBARDATA pData);

        [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
        [DllImport("user32.dll")] public static extern IntPtr MonitorFromPoint(POINT p, uint flags);
        [DllImport("user32.dll")] public static extern IntPtr MonitorFromWindow(IntPtr hwnd, uint flags);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO mi);
        [DllImport("user32.dll")] public static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc cb, IntPtr data);
        [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT r);
        [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
        [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr child, string cls, string title);
        [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [DllImport("user32.dll")] public static extern IntPtr PostMessage(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
        [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr hwnd, ref POINT p);

        [DllImport("user32.dll", SetLastError = true)] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
        [DllImport("shcore.dll", SetLastError = true)] public static extern int SetProcessDpiAwareness(int value);
        [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();

        [DllImport("kernel32.dll", SetLastError = true)] public static extern bool AttachConsole(int pid);
        [DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr GetStdHandle(int nStdHandle);
        [DllImport("kernel32.dll", SetLastError = true)] public static extern uint GetFileType(IntPtr handle);

        public const uint ABM_GETSTATE = 0x00000004;
        public const uint ABM_GETAUTOHIDEBAREX = 0x0000000B;
        public const uint ABE_BOTTOM = 3;
        public const long ABS_AUTOHIDE = 0x0000001;

        public const uint MONITOR_DEFAULTTONULL = 0;
        public const uint MONITORINFOF_PRIMARY = 1;

        public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
        public const uint SWP_NOSIZE = 0x0001;
        public const uint SWP_NOMOVE = 0x0002;
        public const uint SWP_NOACTIVATE = 0x0010;
        public const uint SWP_FRAMECHANGED = 0x0020;
        public const uint SWP_SHOWWINDOW = 0x0040;

        public const uint WM_MOUSEMOVE = 0x0200;
        public const int ATTACH_PARENT_PROCESS = -1;
        public const int STD_OUTPUT_HANDLE = -11;
        public const uint FILE_TYPE_UNKNOWN = 0;

        public static EdgeRect ToEdgeRect(RECT r) { return new EdgeRect(r.Left, r.Top, r.Right, r.Bottom); }
        public static RECT ToRect(EdgeRect r) { RECT o; o.Left = r.Left; o.Top = r.Top; o.Right = r.Right; o.Bottom = r.Bottom; return o; }
    }

    sealed class MonitorFacts
    {
        public IntPtr Handle;
        public EdgeRect Full;
        public EdgeRect Work;
        public bool Primary;
    }

    static class Desktop
    {
        public static string DpiMode = "none";

        // Per-Monitor V2 first so GetCursorPos and the monitor rectangles share
        // one physical coordinate space on mixed-DPI layouts. Older shells fall
        // back; the process must never fail to start over DPI.
        public static void ApplyDpiAwareness()
        {
            try
            {
                if (Win32.SetProcessDpiAwarenessContext(new IntPtr(-4))) { DpiMode = "per-monitor-v2"; return; }
            }
            catch { }
            try
            {
                if (Win32.SetProcessDpiAwareness(2) == 0) { DpiMode = "per-monitor"; return; }
            }
            catch { }
            try
            {
                if (Win32.SetProcessDPIAware()) { DpiMode = "system"; return; }
            }
            catch { }
            DpiMode = "none";
        }

        public static bool AutoHideActive()
        {
            Win32.APPBARDATA ab = new Win32.APPBARDATA();
            ab.cbSize = (uint)Marshal.SizeOf(typeof(Win32.APPBARDATA));
            long state = Win32.SHAppBarMessage(Win32.ABM_GETSTATE, ref ab).ToInt64();
            return (state & Win32.ABS_AUTOHIDE) != 0;
        }

        public static bool TryCursorMonitor(out Win32.POINT pt, out IntPtr hMonitor, out EdgeRect full)
        {
            full = new EdgeRect();
            hMonitor = IntPtr.Zero;
            if (!Win32.GetCursorPos(out pt)) return false;
            hMonitor = Win32.MonitorFromPoint(pt, Win32.MONITOR_DEFAULTTONULL);
            if (hMonitor == IntPtr.Zero) return false;
            Win32.MONITORINFO mi = new Win32.MONITORINFO();
            mi.cbSize = Marshal.SizeOf(typeof(Win32.MONITORINFO));
            if (!Win32.GetMonitorInfo(hMonitor, ref mi)) return false;
            full = Win32.ToEdgeRect(mi.rcMonitor);
            return true;
        }

        public static List<MonitorFacts> EnumerateMonitors()
        {
            List<MonitorFacts> list = new List<MonitorFacts>();
            Win32.MonitorEnumProc cb = delegate(IntPtr hMon, IntPtr hdc, ref Win32.RECT rect, IntPtr data)
            {
                Win32.MONITORINFO mi = new Win32.MONITORINFO();
                mi.cbSize = Marshal.SizeOf(typeof(Win32.MONITORINFO));
                if (Win32.GetMonitorInfo(hMon, ref mi))
                {
                    MonitorFacts m = new MonitorFacts();
                    m.Handle = hMon;
                    m.Full = Win32.ToEdgeRect(mi.rcMonitor);
                    m.Work = Win32.ToEdgeRect(mi.rcWork);
                    m.Primary = (mi.dwFlags & Win32.MONITORINFOF_PRIMARY) != 0;
                    list.Add(m);
                }
                return true;
            };
            Win32.EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, cb, IntPtr.Zero);
            GC.KeepAlive(cb);
            return list;
        }
    }

    // Resolves the bottom auto-hide bar for one monitor. Handles are never
    // trusted across time: every lookup revalidates, and a changed identity
    // bumps the generation so debounce state is dropped.
    sealed class TaskbarResolver
    {
        const int RevalidateMs = 2000;

        sealed class Entry
        {
            public IntPtr Hwnd;
            public string Method;
            public long ResolvedAtMs;
        }

        readonly Dictionary<long, Entry> cache = new Dictionary<long, Entry>();
        long generation = 1;
        uint lastShellPid;
        bool sawShellPid;

        public long Generation { get { return generation; } }

        public IntPtr Resolve(IntPtr hMonitor, EdgeRect monitorFull, long nowMs, out string method)
        {
            long key = hMonitor.ToInt64();
            Entry e;
            if (cache.TryGetValue(key, out e) && e.Hwnd != IntPtr.Zero && Win32.IsWindow(e.Hwnd)
                && (nowMs - e.ResolvedAtMs) < RevalidateMs)
            {
                method = e.Method;
                return e.Hwnd;
            }

            string how;
            IntPtr found = Discover(hMonitor, monitorFull, out how);

            // Explorer's own identity is part of the generation: a restart
            // recreates every bar even when a stale handle value repeats.
            IntPtr tray = Win32.FindWindowEx(IntPtr.Zero, IntPtr.Zero, "Shell_TrayWnd", null);
            if (tray != IntPtr.Zero)
            {
                uint pid;
                Win32.GetWindowThreadProcessId(tray, out pid);
                if (sawShellPid && pid != lastShellPid) generation++;
                lastShellPid = pid;
                sawShellPid = true;
            }

            if (e == null) { e = new Entry(); cache[key] = e; }
            else if (e.Hwnd != found) generation++;

            e.Hwnd = found;
            e.Method = how;
            e.ResolvedAtMs = nowMs;
            method = how;
            return found;
        }

        public void Invalidate()
        {
            cache.Clear();
            generation++;
        }

        static IntPtr Discover(IntPtr hMonitor, EdgeRect monitorFull, out string method)
        {
            // Documented lookup first: ask the shell which auto-hide appbar owns
            // the bottom edge of exactly this monitor rectangle.
            Win32.APPBARDATA ab = new Win32.APPBARDATA();
            ab.cbSize = (uint)Marshal.SizeOf(typeof(Win32.APPBARDATA));
            ab.uEdge = Win32.ABE_BOTTOM;
            ab.rc = Win32.ToRect(monitorFull);
            IntPtr bar = Win32.SHAppBarMessage(Win32.ABM_GETAUTOHIDEBAREX, ref ab);
            if (BelongsTo(bar, hMonitor, monitorFull, false))
            {
                method = "ABM_GETAUTOHIDEBAREX";
                return bar;
            }

            // Fallback only: enumerate the shell tray classes and map each one
            // back to its monitor. Used when the documented lookup is
            // unavailable or returns nothing on this shell.
            IntPtr tray = Win32.FindWindowEx(IntPtr.Zero, IntPtr.Zero, "Shell_TrayWnd", null);
            if (BelongsTo(tray, hMonitor, monitorFull, true))
            {
                method = "Shell_TrayWnd";
                return tray;
            }
            IntPtr sec = IntPtr.Zero;
            while ((sec = Win32.FindWindowEx(IntPtr.Zero, sec, "Shell_SecondaryTrayWnd", null)) != IntPtr.Zero)
            {
                if (BelongsTo(sec, hMonitor, monitorFull, true))
                {
                    method = "Shell_SecondaryTrayWnd";
                    return sec;
                }
            }

            method = "none";
            return IntPtr.Zero;
        }

        // A bar is only ever accepted for the monitor it actually sits on --
        // revealing another monitor's taskbar is the one thing this helper must
        // never do.
        static bool BelongsTo(IntPtr hwnd, IntPtr hMonitor, EdgeRect monitorFull, bool requireBottomShape)
        {
            if (hwnd == IntPtr.Zero || !Win32.IsWindow(hwnd)) return false;
            if (Win32.MonitorFromWindow(hwnd, Win32.MONITOR_DEFAULTTONULL) != hMonitor) return false;
            if (!requireBottomShape) return true;
            Win32.RECT r;
            if (!Win32.GetWindowRect(hwnd, out r)) return false;
            return EdgeGeometry.LooksLikeBottomBar(monitorFull, Win32.ToEdgeRect(r));
        }
    }

    static class Reveal
    {
        // Primary strategy, measured on the target Windows 10 shell: refresh the
        // bar's topmost z-order. No move, no resize, no activation, no
        // SetForegroundWindow, and Explorer keeps owning the hide animation.
        public static bool Run(IntPtr bar, string strategy, out string method)
        {
            method = strategy;
            if (bar == IntPtr.Zero || !Win32.IsWindow(bar)) { method = "none"; return false; }

            bool ok = Win32.SetWindowPos(bar, Win32.HWND_TOPMOST, 0, 0, 0, 0,
                Win32.SWP_NOMOVE | Win32.SWP_NOSIZE | Win32.SWP_NOACTIVATE | Win32.SWP_SHOWWINDOW | Win32.SWP_FRAMECHANGED);

            if (ok && strategy == RevealStrategies.TopmostMouseMove)
            {
                Win32.POINT p;
                if (Win32.GetCursorPos(out p))
                {
                    Win32.POINT c = p;
                    if (Win32.ScreenToClient(bar, ref c))
                        Win32.PostMessage(bar, Win32.WM_MOUSEMOVE, IntPtr.Zero, (IntPtr)((c.Y << 16) | (c.X & 0xFFFF)));
                }
            }
            return ok;
        }
    }

    static class Paths
    {
        public static string ExeDir
        {
            get { return AppDomain.CurrentDomain.BaseDirectory; }
        }

        public static string ConfigPath
        {
            get { return Path.Combine(ExeDir, "taskbar_edge.ini"); }
        }

        public static string StateDir
        {
            get
            {
                string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                return Path.Combine(Path.Combine(local, "SAITULS"), "taskbar_edge");
            }
        }

        public static string StatusPath { get { return Path.Combine(StateDir, "status.txt"); } }
        public static string LogPath { get { return Path.Combine(StateDir, "taskbar_edge.log"); } }
    }

    // Transitions only, and only when debug is on. Bounded file: never an
    // unattended log that grows without limit.
    static class DebugLog
    {
        const long MaxBytes = 256 * 1024;
        static bool enabled;

        public static void Enable(bool on) { enabled = on; }

        public static void Line(string text)
        {
            if (!enabled) return;
            try
            {
                Directory.CreateDirectory(Paths.StateDir);
                string path = Paths.LogPath;
                if (File.Exists(path) && new FileInfo(path).Length > MaxBytes)
                {
                    try { File.Delete(path + ".1"); }
                    catch { }
                    try { File.Move(path, path + ".1"); }
                    catch { try { File.Delete(path); } catch { } }
                }
                File.AppendAllText(path,
                    DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ", CultureInfo.InvariantCulture) + " " + text + Environment.NewLine,
                    Encoding.UTF8);
            }
            catch { }
        }
    }

    sealed class StatusRecord
    {
        public int Pid;
        public long StartedUnixMs;
        public long Generation = 1;
        public string LastRevealMethod = "none";
        public string LastRevealResult = "none";
        public long LastRevealUnixMs;
        public string TaskbarMethod = "none";
        public string Strategy = RevealStrategies.Topmost;
        public string DpiMode = "none";

        public static long UnixNowMs()
        {
            return (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalMilliseconds;
        }

        public string Serialize()
        {
            StringBuilder sb = new StringBuilder();
            sb.AppendLine("pid=" + Pid.ToString(CultureInfo.InvariantCulture));
            sb.AppendLine("started_unix_ms=" + StartedUnixMs.ToString(CultureInfo.InvariantCulture));
            sb.AppendLine("written_unix_ms=" + UnixNowMs().ToString(CultureInfo.InvariantCulture));
            sb.AppendLine("generation=" + Generation.ToString(CultureInfo.InvariantCulture));
            sb.AppendLine("taskbar_method=" + TaskbarMethod);
            sb.AppendLine("reveal_strategy=" + Strategy);
            sb.AppendLine("last_reveal_method=" + LastRevealMethod);
            sb.AppendLine("last_reveal_result=" + LastRevealResult);
            sb.AppendLine("last_reveal_unix_ms=" + LastRevealUnixMs.ToString(CultureInfo.InvariantCulture));
            sb.AppendLine("dpi_awareness=" + DpiMode);
            return sb.ToString();
        }

        public void Write()
        {
            try
            {
                Directory.CreateDirectory(Paths.StateDir);
                string tmp = Paths.StatusPath + ".tmp";
                File.WriteAllText(tmp, Serialize(), Encoding.UTF8);
                if (File.Exists(Paths.StatusPath))
                {
                    try { File.Replace(tmp, Paths.StatusPath, null); return; }
                    catch { try { File.Delete(Paths.StatusPath); } catch { } }
                }
                File.Move(tmp, Paths.StatusPath);
            }
            catch { }
        }

        public static IDictionary<string, string> Read()
        {
            try
            {
                if (!File.Exists(Paths.StatusPath)) return null;
                return EdgeConfig.ParseIni(File.ReadAllText(Paths.StatusPath, Encoding.UTF8));
            }
            catch { return null; }
        }
    }

    static class Program
    {
        public const string MutexName = "Local\\SaitulsTaskbarEdge";
        public const string StopEventName = "Local\\SaitulsTaskbarEdgeStop";

        // Built as a winexe so the always-running watcher never flashes a
        // console. The diagnostic modes still have to print: borrow the parent
        // console only when stdout is not already a real (redirected) handle,
        // because AttachConsole would otherwise replace a test harness's pipe.
        static void OpenConsole()
        {
            bool haveStdout = false;
            try
            {
                IntPtr h = Win32.GetStdHandle(Win32.STD_OUTPUT_HANDLE);
                haveStdout = h != IntPtr.Zero && h != new IntPtr(-1) && Win32.GetFileType(h) != Win32.FILE_TYPE_UNKNOWN;
            }
            catch { }
            if (!haveStdout)
            {
                try { Win32.AttachConsole(Win32.ATTACH_PARENT_PROCESS); }
                catch { }
            }
            try
            {
                StreamWriter w = new StreamWriter(Console.OpenStandardOutput());
                w.AutoFlush = true;
                Console.SetOut(w);
            }
            catch { }
        }

        static EdgeConfig LoadConfig(out List<string> warnings)
        {
            warnings = new List<string>();
            try
            {
                if (!File.Exists(Paths.ConfigPath))
                {
                    WriteDefaultConfig();
                    return EdgeConfig.Defaults();
                }
                return EdgeConfig.Parse(EdgeConfig.ParseIni(File.ReadAllText(Paths.ConfigPath, Encoding.UTF8)), warnings);
            }
            catch (Exception ex)
            {
                warnings.Add("config unreadable (" + ex.GetType().Name + "), using defaults");
                return EdgeConfig.Defaults();
            }
        }

        static void WriteDefaultConfig()
        {
            try
            {
                StringBuilder sb = new StringBuilder();
                sb.AppendLine("; TaskbarEdge advanced settings. Delete this file to restore defaults.");
                sb.AppendLine("; Out-of-range or unreadable values fall back to the default shown here.");
                sb.AppendLine("[taskbar_edge]");
                sb.AppendLine("edge_pixels=" + EdgeConfig.DefaultEdgePixels + "      ; " + EdgeConfig.MinEdgePixels + ".." + EdgeConfig.MaxEdgePixels);
                sb.AppendLine("poll_ms=" + EdgeConfig.DefaultPollMs + "          ; " + EdgeConfig.MinPollMs + ".." + EdgeConfig.MaxPollMs);
                sb.AppendLine("dwell_ms=" + EdgeConfig.DefaultDwellMs + "         ; " + EdgeConfig.MinDwellMs + ".." + EdgeConfig.MaxDwellMs);
                sb.AppendLine("cooldown_ms=" + EdgeConfig.DefaultCooldownMs + "     ; " + EdgeConfig.MinCooldownMs + ".." + EdgeConfig.MaxCooldownMs);
                sb.AppendLine("reveal_strategy=" + RevealStrategies.Topmost + "  ; " + RevealStrategies.Topmost + " | " + RevealStrategies.TopmostMouseMove);
                sb.AppendLine("debug=0            ; 1 records transitions only");
                File.WriteAllText(Paths.ConfigPath, sb.ToString(), Encoding.UTF8);
            }
            catch { }
        }

        static int Main(string[] args)
        {
            string mode = "run";
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i].Trim().ToLowerInvariant();
                if (a == "--status" || a == "-status" || a == "/status") mode = "status";
                else if (a == "--self-test" || a == "-self-test" || a == "/self-test") mode = "selftest";
                else if (a == "--stop" || a == "-stop" || a == "/stop") mode = "stop";
                else if (a == "--help" || a == "-h" || a == "-?" || a == "/?") mode = "help";
            }

            if (mode != "run") OpenConsole();

            switch (mode)
            {
                case "help": return Help();
                case "stop": return Stop();
                case "status": return Status();
                case "selftest": return SelfTest();
                default: return Run();
            }
        }

        static int Help()
        {
            Console.WriteLine("TaskbarEdge -- reliable bottom-edge reveal for an auto-hidden taskbar.");
            Console.WriteLine("  TaskbarEdge.exe              start the per-session watcher");
            Console.WriteLine("  TaskbarEdge.exe --status     report desktop and helper state");
            Console.WriteLine("  TaskbarEdge.exe --self-test  verify shell discovery and reveal invariants");
            Console.WriteLine("  TaskbarEdge.exe --stop       ask the running instance to exit");
            Console.WriteLine("Config: " + Paths.ConfigPath);
            return 0;
        }

        static bool WatcherRunning()
        {
            Mutex m = null;
            try
            {
                m = Mutex.OpenExisting(MutexName);
                return true;
            }
            catch (WaitHandleCannotBeOpenedException) { return false; }
            catch (UnauthorizedAccessException) { return true; }
            catch { return false; }
            finally { if (m != null) m.Close(); }
        }

        static int Stop()
        {
            if (!WatcherRunning())
            {
                Console.WriteLine("stopped=already");
                return 0;
            }
            try
            {
                using (EventWaitHandle stop = EventWaitHandle.OpenExisting(StopEventName))
                    stop.Set();
                Console.WriteLine("stopped=signalled");
                return 0;
            }
            catch (WaitHandleCannotBeOpenedException)
            {
                Console.WriteLine("stopped=no-channel");
                return 1;
            }
            catch (Exception ex)
            {
                Console.WriteLine("stopped=error " + ex.GetType().Name);
                return 1;
            }
        }

        static int Status()
        {
            Desktop.ApplyDpiAwareness();
            List<string> warnings;
            EdgeConfig cfg = LoadConfig(out warnings);

            bool running = WatcherRunning();
            Console.WriteLine("running=" + (running ? "true" : "false"));

            bool autoHide = Desktop.AutoHideActive();
            Console.WriteLine("autohide=" + (autoHide ? "on" : "off"));

            List<MonitorFacts> monitors = Desktop.EnumerateMonitors();
            Console.WriteLine("monitor_count=" + monitors.Count.ToString(CultureInfo.InvariantCulture));

            Win32.POINT pt; IntPtr hMon; EdgeRect full;
            bool haveMon = Desktop.TryCursorMonitor(out pt, out hMon, out full);
            if (haveMon)
            {
                bool primary = false;
                for (int i = 0; i < monitors.Count; i++) if (monitors[i].Handle == hMon) primary = monitors[i].Primary;
                Console.WriteLine("current_monitor=" + hMon.ToInt64().ToString(CultureInfo.InvariantCulture)
                    + " rect=" + full.ToString() + " primary=" + (primary ? "true" : "false"));
                Console.WriteLine("cursor_edge=" + (EdgeGeometry.InBottomEdgeStrip(full, pt.X, pt.Y, cfg.EdgePixels) ? "inside" : "outside"));
            }
            else
            {
                Console.WriteLine("current_monitor=none");
                Console.WriteLine("cursor_edge=unknown");
            }

            TaskbarResolver resolver = new TaskbarResolver();
            if (haveMon)
            {
                string method;
                IntPtr bar = resolver.Resolve(hMon, full, 0, out method);
                bool valid = bar != IntPtr.Zero && Win32.IsWindow(bar);
                Console.WriteLine("taskbar_hwnd=" + bar.ToInt64().ToString(CultureInfo.InvariantCulture)
                    + " valid=" + (valid ? "true" : "false") + " discovery=" + method);
                if (valid)
                {
                    Win32.RECT r;
                    if (Win32.GetWindowRect(bar, out r))
                        Console.WriteLine("taskbar_state=" + (EdgeGeometry.IsBottomTaskbarHidden(full, Win32.ToEdgeRect(r)) ? "hidden" : "revealed"));
                }
            }
            else
            {
                Console.WriteLine("taskbar_hwnd=0 valid=false discovery=none");
            }

            IDictionary<string, string> st = StatusRecord.Read();
            Console.WriteLine("last_reveal_method=" + Get(st, "last_reveal_method", "none"));
            Console.WriteLine("last_reveal_result=" + Get(st, "last_reveal_result", "none"));
            Console.WriteLine("generation=" + Get(st, "generation", running ? "unknown" : "0"));
            Console.WriteLine("helper_pid=" + (running ? Get(st, "pid", "unknown") : "-"));
            Console.WriteLine("dpi_awareness=" + Desktop.DpiMode);
            Console.WriteLine("config=edge_pixels=" + cfg.EdgePixels + " poll_ms=" + cfg.PollMs
                + " dwell_ms=" + cfg.DwellMs + " cooldown_ms=" + cfg.CooldownMs
                + " reveal_strategy=" + cfg.RevealStrategy + " debug=" + (cfg.Debug ? "1" : "0"));
            for (int i = 0; i < warnings.Count; i++) Console.WriteLine("config_warning=" + warnings[i]);
            return 0;
        }

        static string Get(IDictionary<string, string> map, string key, string def)
        {
            string v;
            if (map != null && map.TryGetValue(key, out v) && v != null && v.Length > 0) return v;
            return def;
        }

        static int SelfTest()
        {
            int failures = 0;
            int skipped = 0;
            Desktop.ApplyDpiAwareness();
            List<string> warnings;
            EdgeConfig cfg = LoadConfig(out warnings);

            failures += Check("dpi awareness applied", Desktop.DpiMode != "none", "mode=" + Desktop.DpiMode);

            List<MonitorFacts> monitors = Desktop.EnumerateMonitors();
            failures += Check("monitors enumerated", monitors.Count >= 1, "count=" + monitors.Count);

            bool autoHide = Desktop.AutoHideActive();
            Console.WriteLine("INFO  ABM_GETSTATE autohide=" + (autoHide ? "on" : "off"));

            TaskbarResolver resolver = new TaskbarResolver();
            int bars = 0;
            bool perMonitorOk = true;
            IntPtr probeBar = IntPtr.Zero;
            EdgeRect probeMonitor = new EdgeRect();
            for (int i = 0; i < monitors.Count; i++)
            {
                string method;
                IntPtr bar = resolver.Resolve(monitors[i].Handle, monitors[i].Full, 0, out method);
                if (bar == IntPtr.Zero) { Console.WriteLine("INFO  monitor " + monitors[i].Full + " has no bottom taskbar"); continue; }
                bars++;
                if (Win32.MonitorFromWindow(bar, Win32.MONITOR_DEFAULTTONULL) != monitors[i].Handle) perMonitorOk = false;
                Console.WriteLine("INFO  monitor " + monitors[i].Full + " bar=" + bar.ToInt64() + " via " + method);
                if (probeBar == IntPtr.Zero) { probeBar = bar; probeMonitor = monitors[i].Full; }
            }
            failures += Check("shell taskbar discovered", bars >= 1, "bars=" + bars);
            failures += Check("each bar maps back to its own monitor", perMonitorOk, "");

            if (probeBar == IntPtr.Zero)
            {
                Console.WriteLine("SKIP  reveal invariants (no bottom taskbar to probe)");
                skipped++;
            }
            else
            {
                Win32.RECT before;
                Win32.GetWindowRect(probeBar, out before);
                IntPtr fgBefore = Win32.GetForegroundWindow();

                string method;
                bool ok = Reveal.Run(probeBar, cfg.RevealStrategy, out method);
                Thread.Sleep(120);

                Win32.RECT after;
                Win32.GetWindowRect(probeBar, out after);
                IntPtr fgAfter = Win32.GetForegroundWindow();

                failures += Check("reveal call reported success", ok, "method=" + method);
                failures += Check("reveal call preserves foreground HWND", fgAfter == fgBefore,
                    "before=" + fgBefore.ToInt64() + " after=" + fgAfter.ToInt64());
                failures += Check("reveal call does not move the taskbar",
                    before.Left == after.Left && before.Right == after.Right, "");
                failures += Check("reveal call does not resize the taskbar",
                    (before.Right - before.Left) == (after.Right - after.Left)
                    && (before.Bottom - before.Top) == (after.Bottom - after.Top), "");

                // The one strategy-disqualifying outcome: a bar that stays up on
                // its own. Only decidable when the cursor is not at the edge.
                Win32.POINT pt; IntPtr hMon; EdgeRect full;
                bool haveMon = Desktop.TryCursorMonitor(out pt, out hMon, out full);
                bool cursorAtEdge = haveMon && EdgeGeometry.InBottomEdgeStrip(full, pt.X, pt.Y, cfg.EdgePixels);
                if (!autoHide)
                {
                    Console.WriteLine("SKIP  native hide resumes (auto-hide is off)");
                    skipped++;
                }
                else if (cursorAtEdge)
                {
                    Console.WriteLine("SKIP  native hide resumes (cursor is at the bottom edge)");
                    skipped++;
                }
                else
                {
                    Thread.Sleep(1200);
                    Win32.RECT settled;
                    Win32.GetWindowRect(probeBar, out settled);
                    failures += Check("reveal does not pin the taskbar visible",
                        EdgeGeometry.IsBottomTaskbarHidden(probeMonitor, Win32.ToEdgeRect(settled)),
                        "rect=" + Win32.ToEdgeRect(settled));
                }
            }

            List<string> cfgWarn = new List<string>();
            Dictionary<string, string> bad = new Dictionary<string, string>();
            bad["edge_pixels"] = "999";
            bad["poll_ms"] = "abc";
            EdgeConfig fallback = EdgeConfig.Parse(bad, cfgWarn);
            failures += Check("bad config falls back to safe defaults",
                fallback.EdgePixels == EdgeConfig.DefaultEdgePixels && fallback.PollMs == EdgeConfig.DefaultPollMs && cfgWarn.Count == 2, "");

            Console.WriteLine("SELFTEST failures=" + failures + " skipped=" + skipped);
            return failures == 0 ? 0 : 1;
        }

        static int Check(string name, bool ok, string detail)
        {
            Console.WriteLine((ok ? "PASS  " : "FAIL  ") + name + (detail.Length > 0 ? "  " + detail : ""));
            return ok ? 0 : 1;
        }

        static int Run()
        {
            bool createdNew;
            using (Mutex single = new Mutex(true, MutexName, out createdNew))
            {
                if (!createdNew)
                {
                    // Reuse the instance that already owns this session and
                    // leave successfully -- a second launch is never an error.
                    return 0;
                }

                using (EventWaitHandle stop = new EventWaitHandle(false, EventResetMode.ManualReset, StopEventName))
                {
                    // A leftover signalled event from a previous --stop would
                    // otherwise kill this instance on its first wait.
                    stop.Reset();
                    Desktop.ApplyDpiAwareness();
                    List<string> warnings;
                    EdgeConfig cfg = LoadConfig(out warnings);
                    DebugLog.Enable(cfg.Debug);
                    for (int i = 0; i < warnings.Count; i++) DebugLog.Line("CONFIG_WARNING " + warnings[i]);

                    StatusRecord status = new StatusRecord();
                    status.Pid = Process.GetCurrentProcess().Id;
                    status.StartedUnixMs = StatusRecord.UnixNowMs();
                    status.Strategy = cfg.RevealStrategy;
                    status.DpiMode = Desktop.DpiMode;
                    status.Write();

                    Watch(cfg, stop, status);

                    status.Write();
                    return 0;
                }
            }
        }

        static void Watch(EdgeConfig cfg, EventWaitHandle stop, StatusRecord status)
        {
            const long HeartbeatMs = 60000;
            EdgeStateMachine machine = new EdgeStateMachine(cfg);
            TaskbarResolver resolver = new TaskbarResolver();
            Stopwatch clock = Stopwatch.StartNew();
            long lastHeartbeat = 0;

            while (!stop.WaitOne(cfg.PollMs))
            {
                long now = clock.ElapsedMilliseconds;

                EdgeSample s = new EdgeSample();
                s.NowMs = now;
                s.Generation = resolver.Generation;

                Win32.POINT pt; IntPtr hMon; EdgeRect full;
                if (Desktop.TryCursorMonitor(out pt, out hMon, out full))
                {
                    s.HaveMonitor = true;
                    s.MonitorId = hMon.ToInt64();
                    s.InStrip = EdgeGeometry.InBottomEdgeStrip(full, pt.X, pt.Y, cfg.EdgePixels);
                }

                IntPtr bar = IntPtr.Zero;
                string discovery = "none";

                // Shell work only happens while the cursor is actually on the
                // strip. Off the edge the loop is three cheap cursor/monitor
                // calls, which is what keeps idle cost at nothing.
                if (s.HaveMonitor && s.InStrip)
                {
                    s.AutoHide = Desktop.AutoHideActive();
                    bar = resolver.Resolve(hMon, full, now, out discovery);
                    s.Generation = resolver.Generation;
                    s.TaskbarValid = bar != IntPtr.Zero && Win32.IsWindow(bar);
                    if (s.TaskbarValid)
                    {
                        Win32.RECT r;
                        if (Win32.GetWindowRect(bar, out r))
                            s.TaskbarHidden = EdgeGeometry.IsBottomTaskbarHidden(full, Win32.ToEdgeRect(r));
                    }
                }

                EdgeDecision d = machine.Observe(s);

                if (d.Has(EdgeEvent.TaskbarRediscovered))
                {
                    status.Generation = resolver.Generation;
                    DebugLog.Line("TASKBAR_REDISCOVERED generation=" + resolver.Generation);
                    status.Write();
                }
                if (d.Has(EdgeEvent.EdgeEnter)) DebugLog.Line("EDGE_ENTER monitor=" + s.MonitorId);
                if (d.Has(EdgeEvent.EdgeExit)) DebugLog.Line("EDGE_EXIT");

                if (d.Reveal)
                {
                    DebugLog.Line("REVEAL_REQUEST monitor=" + s.MonitorId + " hwnd=" + bar.ToInt64() + " via=" + discovery);
                    string method;
                    bool ok = Reveal.Run(bar, cfg.RevealStrategy, out method);
                    DebugLog.Line(ok ? "REVEAL_OK method=" + method : "REVEAL_FAIL method=" + method);
                    status.TaskbarMethod = discovery;
                    status.LastRevealMethod = method;
                    status.LastRevealResult = ok ? "ok" : "fail";
                    status.LastRevealUnixMs = StatusRecord.UnixNowMs();
                    status.Generation = resolver.Generation;
                    status.Write();
                    lastHeartbeat = now;

                    // A failed reveal means the handle we hold is no longer the
                    // real bar. Drop it so the next sample rediscovers.
                    if (!ok) resolver.Invalidate();
                }

                if (now - lastHeartbeat >= HeartbeatMs)
                {
                    lastHeartbeat = now;
                    status.Generation = resolver.Generation;
                    status.Write();
                }
            }

            DebugLog.Line("STOPPED");
        }
    }
}
