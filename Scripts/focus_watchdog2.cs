using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.IO;

class FocusWatchdog {
    delegate void WinEventDelegate(IntPtr hWinEventHook, uint eventType, IntPtr hwnd, int idObject, int idChild, uint dwEventThread, uint dwmsEventTime);
    [DllImport("user32.dll")] static extern IntPtr SetWinEventHook(uint eventMin, uint eventMax, IntPtr hmodWinEventProc, WinEventDelegate lpfnWinEventProc, uint idProcess, uint idThread, uint dwFlags);
    [DllImport("user32.dll")] static extern bool UnhookWinEvent(IntPtr hWinEventHook);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint lpdwProcessId);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] static extern int GetWindowText(IntPtr hwnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] static extern int GetClassName(IntPtr hwnd, StringBuilder lpClassName, int nMaxCount);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();

    const uint EVENT_SYSTEM_FOREGROUND = 0x0003;
    const uint EVENT_OBJECT_CREATE = 0x8000;
    const uint WINEVENT_OUTOFCONTEXT = 0;
    const uint WINEVENT_SKIPOWNPROCESS = 2;

    static string logPath;
    static void Log(string msg) {
        try { File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + " " + msg + Environment.NewLine); } catch {}
    }

    static void Callback(IntPtr hWinEventHook, uint eventType, IntPtr hwnd, int idObject, int idChild, uint dwEventThread, uint dwmsEventTime) {
        if (idObject != 0) return; // only windows
        uint pid; GetWindowThreadProcessId(hwnd, out pid);
        string pname = "?";
        try { var p = Process.GetProcessById((int)pid); pname = p.ProcessName; } catch {}
        var title = new StringBuilder(256); GetWindowText(hwnd, title, 256);
        var cls = new StringBuilder(128); GetClassName(hwnd, cls, 128);
        string vis = IsWindowVisible(hwnd) ? "VISIBLE" : "hidden";
        if (eventType == EVENT_SYSTEM_FOREGROUND) {
            Log("FOREGROUND pid=" + pid + " name=" + pname + " title='" + title + "' class='" + cls + "' " + vis);
        } else {
            Log("WINCREATE pid=" + pid + " name=" + pname + " title='" + title + "' class='" + cls + "' " + vis);
        }
    }

    static void Main(string[] args) {
        logPath = args.Length > 0 ? args[0] : "C:\focus-watchdog.log";
        int durationSec = args.Length > 1 ? int.Parse(args[1]) : 300;
        Log("=== watchdog2 start duration=" + durationSec + "s ===");
        var hook1 = SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, IntPtr.Zero, Callback, 0, 0, WINEVENT_OUTOFCONTEXT);
        var hook2 = SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_CREATE, IntPtr.Zero, Callback, 0, 0, WINEVENT_OUTOFCONTEXT);
        var end = DateTime.Now.AddSeconds(durationSec);
        while (DateTime.Now < end) { System.Threading.Thread.Sleep(100); }
        UnhookWinEvent(hook1);
        UnhookWinEvent(hook2);
        Log("=== watchdog2 end ===");
    }
}
