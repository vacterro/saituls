using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

class FocusWatchdog3
{
    delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc p, IntPtr l);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();

    static string logPath;
    static readonly HashSet<IntPtr> seenWindows = new HashSet<IntPtr>();
    static IntPtr lastFg = IntPtr.Zero;

    static void Log(string msg)
    {
        try { File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + " " + msg + Environment.NewLine); }
        catch { }
    }

    static string Info(IntPtr h)
    {
        uint pid;
        GetWindowThreadProcessId(h, out pid);
        string name = "?";
        try { name = Process.GetProcessById((int)pid).ProcessName; } catch { }
        var t = new StringBuilder(256);
        GetWindowText(h, t, 256);
        var c = new StringBuilder(128);
        GetClassName(h, c, 128);
        return "pid=" + pid + " name=" + name + " class='" + c + "' title='" + t + "' visible=" + IsWindowVisible(h);
    }

    static void Main(string[] args)
    {
        logPath = args.Length > 0 ? args[0] : "C:\\focus-watchdog3.log";
        int seconds = args.Length > 1 ? int.Parse(args[1]) : 600;
        Log("=== watchdog3 start duration=" + seconds + "s ===");
        var end = DateTime.Now.AddSeconds(seconds);
        while (DateTime.Now < end)
        {
            IntPtr fg = GetForegroundWindow();
            if (fg != lastFg)
            {
                Log("FOREGROUND " + Info(fg));
                lastFg = fg;
            }

            var current = new List<IntPtr>();
            EnumWindows((h, l) =>
            {
                if (IsWindowVisible(h)) current.Add(h);
                return true;
            }, IntPtr.Zero);

            foreach (IntPtr h in current)
            {
                if (!seenWindows.Contains(h))
                {
                    seenWindows.Add(h);
                    string info = Info(h);
                    // Log every new visible window; console windows are the
                    // interesting ones but any focus thief is suspect.
                    Log("NEWVISIBLE " + info);
                }
            }
            Thread.Sleep(10);
        }
        Log("=== watchdog3 end ===");
    }
}