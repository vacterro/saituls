using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Diagnostics.Eventing.Reader;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.Globalization;
using System.IO;
using System.Management;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

// SAIOPT -- the background optimizer. Win32 adapter, engine loop, tray and
// panel. Every decision lives in OptLogic.cs; this file collects facts,
// applies the few reversible changes the logic asks for, and shows what
// happened in words.
//
// What it changes, ever: the priority class of a background process that is
// hogging CPU while the machine is busy (Normal -> BelowNormal, and back), and
// -- only when you click, or for an image you armed -- it ends a stale orphan.
// What it never does: trim working sets or "clean RAM", delete files, touch
// services or HKLM, restart Explorer, or ask for Administrator.
//
// Build: Scripts\saiopt\build.ps1

namespace Saituls.Saiopt
{
    static class Native
    {
        public const uint PROCESS_TERMINATE = 0x0001;
        public const uint PROCESS_SET_INFORMATION = 0x0200;
        public const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;

        public const uint IDLE_PRIORITY_CLASS = 0x40;
        public const uint BELOW_NORMAL_PRIORITY_CLASS = 0x4000;
        public const uint NORMAL_PRIORITY_CLASS = 0x20;
        public const uint ABOVE_NORMAL_PRIORITY_CLASS = 0x8000;
        public const uint HIGH_PRIORITY_CLASS = 0x80;
        public const uint REALTIME_PRIORITY_CLASS = 0x100;

        public const int SystemProcessInformation = 5;
        public const uint STATUS_INFO_LENGTH_MISMATCH = 0xC0000004;
        public const uint CREATE_SUSPENDED = 0x4;
        public const uint CREATE_NO_WINDOW = 0x08000000;
        public const uint NONANTIALIASED_QUALITY = 3;

        [StructLayout(LayoutKind.Sequential)]
        public struct MEMORYSTATUSEX
        {
            public uint dwLength;
            public uint dwMemoryLoad;
            public ulong ullTotalPhys, ullAvailPhys, ullTotalPageFile, ullAvailPageFile, ullTotalVirtual, ullAvailVirtual, ullAvailExtendedVirtual;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct STARTUPINFO
        {
            public int cb;
            public string lpReserved, lpDesktop, lpTitle;
            public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
            public short wShowWindow, cbReserved2;
            public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct PROCESS_INFORMATION
        {
            public IntPtr hProcess, hThread;
            public int dwProcessId, dwThreadId;
        }

        public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);

        [DllImport("ntdll.dll")] public static extern int NtQuerySystemInformation(int cls, IntPtr buffer, int length, out int returnLength);
        [DllImport("kernel32.dll")] public static extern bool GetSystemTimes(out long idle, out long kernel, out long user);
        [DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
        [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
        [DllImport("kernel32.dll", SetLastError = true)] public static extern uint GetPriorityClass(IntPtr h);
        [DllImport("kernel32.dll", SetLastError = true)] public static extern bool SetPriorityClass(IntPtr h, uint cls);
        [DllImport("kernel32.dll")] public static extern bool GetProcessTimes(IntPtr h, out long create, out long exit, out long kernel, out long user);
        [DllImport("kernel32.dll", SetLastError = true)] public static extern bool TerminateProcess(IntPtr h, uint code);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern uint QueryDosDevice(string name, StringBuilder target, int max);
        [DllImport("kernel32.dll")] public static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX m);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool CreateProcess(string app, StringBuilder cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags,
            IntPtr env, string dir, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr sa,
            uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool ReadFile(Microsoft.Win32.SafeHandles.SafeFileHandle h, IntPtr buffer, int count, out int read, IntPtr overlapped);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetFilePointerEx(Microsoft.Win32.SafeHandles.SafeFileHandle h, long distance, out long newPos, uint method);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool GetFileSizeEx(Microsoft.Win32.SafeHandles.SafeFileHandle h, out long size);
        [DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr VirtualAlloc(IntPtr addr, UIntPtr size, uint type, uint protect);
        [DllImport("kernel32.dll", SetLastError = true)] public static extern bool VirtualFree(IntPtr addr, UIntPtr size, uint type);
        public const uint GENERIC_READ = 0x80000000;
        public const uint FILE_SHARE_ALL = 0x7;
        public const uint OPEN_EXISTING = 3;
        public const uint FILE_FLAG_NO_BUFFERING = 0x20000000;
        [DllImport("kernel32.dll", SetLastError = true)] public static extern bool AttachConsole(int pid);
        [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int n);
        [DllImport("kernel32.dll")] public static extern uint GetFileType(IntPtr h);

        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out int pid);
        [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string title);
        [DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, IntPtr w, IntPtr l, uint flags, uint timeout, out IntPtr result);
        [DllImport("user32.dll")] public static extern uint GetGuiResources(IntPtr h, uint flags);
        [DllImport("user32.dll")] public static extern bool DestroyIcon(IntPtr h);
        [DllImport("user32.dll")] public static extern bool ReleaseCapture();
        [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr w, IntPtr l);
        [DllImport("gdi32.dll", CharSet = CharSet.Unicode)]
        public static extern IntPtr CreateFont(int h, int w, int esc, int orient, int weight, uint italic, uint underline, uint strike,
            uint charset, uint outPrec, uint clipPrec, uint quality, uint pitch, string face);
        [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr h);

        public static uint ClassOf(int basePriority)
        {
            switch (basePriority)
            {
                case Priorities.Idle: return IDLE_PRIORITY_CLASS;
                case Priorities.BelowNormal: return BELOW_NORMAL_PRIORITY_CLASS;
                case Priorities.AboveNormal: return ABOVE_NORMAL_PRIORITY_CLASS;
                case Priorities.High: return HIGH_PRIORITY_CLASS;
                case Priorities.Realtime: return REALTIME_PRIORITY_CLASS;
                default: return NORMAL_PRIORITY_CLASS;
            }
        }

        public static int BaseOf(uint cls)
        {
            switch (cls)
            {
                case IDLE_PRIORITY_CLASS: return Priorities.Idle;
                case BELOW_NORMAL_PRIORITY_CLASS: return Priorities.BelowNormal;
                case NORMAL_PRIORITY_CLASS: return Priorities.Normal;
                case ABOVE_NORMAL_PRIORITY_CLASS: return Priorities.AboveNormal;
                case HIGH_PRIORITY_CLASS: return Priorities.High;
                case REALTIME_PRIORITY_CLASS: return Priorities.Realtime;
                default: return -1;
            }
        }
    }

    // ------------------------------------------------------------ processes

    sealed class RawProc
    {
        public int Pid, ParentPid, SessionId, BasePriority, Threads, Handles;
        public long CreateTime, CpuTicks, PrivateBytes, WorkingSet;
        public string Image;
        public bool Suspended;
    }

    // One NtQuerySystemInformation call returns every process with its CPU
    // times, base priority, parent, session, memory and per-thread wait state.
    // That is one syscall per sample instead of ~450 OpenProcess calls, and no
    // handle to anybody else's process just to look at it.
    sealed class ProcessSampler : IDisposable
    {
        // x64 SYSTEM_PROCESS_INFORMATION / SYSTEM_THREAD_INFORMATION offsets.
        const int OffThreads = 0x04, OffCreate = 0x20, OffUser = 0x28, OffKernel = 0x30;
        const int OffNameLen = 0x38, OffNameBuf = 0x40, OffBasePri = 0x48, OffPid = 0x50, OffParent = 0x58;
        const int OffHandles = 0x60, OffSession = 0x64, OffWorkingSet = 0x90, OffPrivate = 0xC8;
        const int ProcHeader = 0x100, ThreadSize = 0x50, OffThreadState = 0x44, OffWaitReason = 0x48;
        const int StateWaiting = 5, WaitSuspended = 5;

        IntPtr buf = IntPtr.Zero;
        int size;
        readonly Dictionary<ProcKey, string> names = new Dictionary<ProcKey, string>();

        public List<RawProc> Snapshot()
        {
            if (IntPtr.Size != 8) throw new PlatformNotSupportedException("SAIOPT reads x64 kernel structures and must run as a 64-bit process");
            if (buf == IntPtr.Zero) { size = 1 << 20; buf = Marshal.AllocHGlobal(size); }
            for (int attempt = 0; ; attempt++)
            {
                int ret;
                int st = Native.NtQuerySystemInformation(Native.SystemProcessInformation, buf, size, out ret);
                if (st == 0) break;
                if ((uint)st == Native.STATUS_INFO_LENGTH_MISMATCH && attempt < 8)
                {
                    Marshal.FreeHGlobal(buf);
                    buf = IntPtr.Zero;
                    size = Math.Max(ret + (256 << 10), size * 2);
                    buf = Marshal.AllocHGlobal(size);
                    continue;
                }
                throw new InvalidOperationException("NtQuerySystemInformation failed 0x" + ((uint)st).ToString("X8"));
            }

            List<RawProc> list = new List<RawProc>(512);
            HashSet<ProcKey> live = new HashSet<ProcKey>();
            long offset = 0;
            while (true)
            {
                IntPtr p = new IntPtr(buf.ToInt64() + offset);
                int next = Marshal.ReadInt32(p, 0);
                RawProc r = new RawProc();
                r.Threads = Marshal.ReadInt32(p, OffThreads);
                r.CreateTime = Marshal.ReadInt64(p, OffCreate);
                r.CpuTicks = Marshal.ReadInt64(p, OffUser) + Marshal.ReadInt64(p, OffKernel);
                r.BasePriority = Marshal.ReadInt32(p, OffBasePri);
                r.Pid = (int)Marshal.ReadIntPtr(p, OffPid).ToInt64();
                r.ParentPid = (int)Marshal.ReadIntPtr(p, OffParent).ToInt64();
                r.Handles = Marshal.ReadInt32(p, OffHandles);
                r.SessionId = Marshal.ReadInt32(p, OffSession);
                r.WorkingSet = Marshal.ReadIntPtr(p, OffWorkingSet).ToInt64();
                r.PrivateBytes = Marshal.ReadIntPtr(p, OffPrivate).ToInt64();

                ProcKey key = new ProcKey(r.Pid, r.CreateTime);
                live.Add(key);
                string name;
                if (!names.TryGetValue(key, out name))
                {
                    int len = (ushort)Marshal.ReadInt16(p, OffNameLen);
                    IntPtr sp = Marshal.ReadIntPtr(p, OffNameBuf);
                    if (r.Pid == 0) name = "idle";
                    else if (sp == IntPtr.Zero || len == 0) name = "system";
                    else name = Marshal.PtrToStringUni(sp, len / 2);
                    name = name.ToLowerInvariant();
                    names[key] = name;
                }
                r.Image = name;

                int suspended = 0;
                long tbase = p.ToInt64() + ProcHeader;
                for (int t = 0; t < r.Threads; t++)
                {
                    IntPtr th = new IntPtr(tbase + (long)t * ThreadSize);
                    if (Marshal.ReadInt32(th, OffThreadState) == StateWaiting && Marshal.ReadInt32(th, OffWaitReason) == WaitSuspended)
                        suspended++;
                }
                r.Suspended = r.Threads > 0 && suspended == r.Threads;
                list.Add(r);
                if (next == 0) break;
                offset += next;
            }

            if (names.Count > live.Count + 512)
            {
                List<ProcKey> dead = new List<ProcKey>();
                foreach (ProcKey k in names.Keys) if (!live.Contains(k)) dead.Add(k);
                foreach (ProcKey k in dead) names.Remove(k);
            }
            return list;
        }

        public void Dispose()
        {
            if (buf != IntPtr.Zero) { Marshal.FreeHGlobal(buf); buf = IntPtr.Zero; }
        }
    }

    static class Procs
    {
        // The only place a priority is changed. Identity is re-verified on the
        // handle (PID + creation time) and the current class must still be the
        // one we expect -- anything else means somebody else owns it now.
        public static bool SetPriority(ProcKey key, int expectBase, int toBase, out string error)
        {
            error = null;
            IntPtr h = Native.OpenProcess(Native.PROCESS_SET_INFORMATION | Native.PROCESS_QUERY_LIMITED_INFORMATION, false, key.Pid);
            if (h == IntPtr.Zero) { error = "no access (" + Marshal.GetLastWin32Error() + ")"; return false; }
            try
            {
                long c, x, k, u;
                if (!Native.GetProcessTimes(h, out c, out x, out k, out u) || c != key.CreateTime) { error = "not the same process any more"; return false; }
                int cur = Native.BaseOf(Native.GetPriorityClass(h));
                if (cur != expectBase) { error = "priority is " + Priorities.Name(cur) + " now"; return false; }
                if (!Native.SetPriorityClass(h, Native.ClassOf(toBase))) { error = "SetPriorityClass failed (" + Marshal.GetLastWin32Error() + ")"; return false; }
                return true;
            }
            finally { Native.CloseHandle(h); }
        }

        // The only place SAIOPT ends a process: a stale orphan you confirmed,
        // or one whose image you armed. Identity re-verified on the handle.
        public static bool EndProcess(ProcKey key, out string error)
        {
            error = null;
            IntPtr h = Native.OpenProcess(Native.PROCESS_TERMINATE | Native.PROCESS_QUERY_LIMITED_INFORMATION, false, key.Pid);
            if (h == IntPtr.Zero) { error = "no access (" + Marshal.GetLastWin32Error() + ")"; return false; }
            try
            {
                long c, x, k, u;
                if (!Native.GetProcessTimes(h, out c, out x, out k, out u) || c != key.CreateTime) { error = "not the same process any more"; return false; }
                if (!Native.TerminateProcess(h, 1)) { error = "TerminateProcess failed (" + Marshal.GetLastWin32Error() + ")"; return false; }
                return true;
            }
            finally { Native.CloseHandle(h); }
        }

        public static long FileTimeNow() { return DateTime.UtcNow.ToFileTimeUtc(); }
    }

    // ---------------------------------------------------------------- audio

    // Which processes are making sound right now (Core Audio session meters).
    // A process you can hear keeps full speed: easing a player that is
    // playing is how music starts to crackle.
    static class CoreAudio
    {
        [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
        class MMDeviceEnumerator { }

        [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IMMDeviceEnumerator
        {
            [PreserveSig] int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
        }

        [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IMMDeviceCollection
        {
            [PreserveSig] int GetCount(out int count);
            [PreserveSig] int Item(int index, out IMMDevice device);
        }

        [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IMMDevice
        {
            [PreserveSig] int Activate(ref Guid iid, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
        }

        [ComImport, Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IAudioSessionManager2
        {
            [PreserveSig] int GetAudioSessionControl(IntPtr sessionGuid, int flags, out IntPtr control);
            [PreserveSig] int GetSimpleAudioVolume(IntPtr sessionGuid, int flags, out IntPtr volume);
            [PreserveSig] int GetSessionEnumerator(out IAudioSessionEnumerator sessions);
        }

        [ComImport, Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IAudioSessionEnumerator
        {
            [PreserveSig] int GetCount(out int count);
            [PreserveSig] int GetSession(int index, [MarshalAs(UnmanagedType.IUnknown)] out object session);
        }

        [ComImport, Guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IAudioSessionControl2
        {
            [PreserveSig] int GetState(out int state);
            [PreserveSig] int GetDisplayName(out IntPtr name);
            [PreserveSig] int SetDisplayName(IntPtr name, IntPtr ctx);
            [PreserveSig] int GetIconPath(out IntPtr path);
            [PreserveSig] int SetIconPath(IntPtr path, IntPtr ctx);
            [PreserveSig] int GetGroupingParam(out Guid g);
            [PreserveSig] int SetGroupingParam(IntPtr g, IntPtr ctx);
            [PreserveSig] int RegisterAudioSessionNotification(IntPtr n);
            [PreserveSig] int UnregisterAudioSessionNotification(IntPtr n);
            [PreserveSig] int GetSessionIdentifier(out IntPtr id);
            [PreserveSig] int GetSessionInstanceIdentifier(out IntPtr id);
            [PreserveSig] int GetProcessId(out int pid);
        }

        [ComImport, Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IAudioMeterInformation
        {
            [PreserveSig] int GetPeakValue(out float peak);
        }

        const int eRender = 0, DEVICE_STATE_ACTIVE = 1, CLSCTX_ALL = 23, AudioSessionStateActive = 1;

        static void Release(object o)
        {
            if (o != null && Marshal.IsComObject(o))
            {
                try { Marshal.ReleaseComObject(o); }
                catch { }
            }
        }

        // Every COM object is released in finally: this runs every couple of
        // seconds for days, and a leaked RCW per call is a leak SAIOPT would
        // have to report about itself.
        public static HashSet<int> AudiblePids()
        {
            HashSet<int> pids = new HashSet<int>();
            IMMDeviceEnumerator en = null;
            IMMDeviceCollection col = null;
            try
            {
                en = (IMMDeviceEnumerator)new MMDeviceEnumerator();
                if (en.EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, out col) != 0 || col == null) return pids;
                int n;
                if (col.GetCount(out n) != 0) return pids;
                for (int i = 0; i < n; i++)
                {
                    IMMDevice dev = null;
                    object mgrObj = null;
                    IAudioSessionEnumerator se = null;
                    try
                    {
                        if (col.Item(i, out dev) != 0 || dev == null) continue;
                        Guid iid = typeof(IAudioSessionManager2).GUID;
                        if (dev.Activate(ref iid, CLSCTX_ALL, IntPtr.Zero, out mgrObj) != 0 || mgrObj == null) continue;
                        IAudioSessionManager2 mgr = (IAudioSessionManager2)mgrObj;
                        if (mgr.GetSessionEnumerator(out se) != 0 || se == null) continue;
                        int count;
                        if (se.GetCount(out count) != 0) continue;
                        for (int s = 0; s < count; s++)
                        {
                            object sess = null;
                            try
                            {
                                if (se.GetSession(s, out sess) != 0 || sess == null) continue;
                                IAudioSessionControl2 c2 = sess as IAudioSessionControl2;
                                int state, pid;
                                if (c2 == null || c2.GetState(out state) != 0 || state != AudioSessionStateActive) continue;
                                if (c2.GetProcessId(out pid) != 0 || pid == 0) continue;
                                IAudioMeterInformation meter = sess as IAudioMeterInformation;
                                float peak = 0;
                                if (meter != null) meter.GetPeakValue(out peak);
                                if (peak > 0.0005f) pids.Add(pid);
                            }
                            finally { Release(sess); }
                        }
                    }
                    finally { Release(se); Release(mgrObj); Release(dev); }
                }
            }
            catch { }
            finally { Release(col); Release(en); }
            return pids;
        }
    }

    // -------------------------------------------------------------- desktop

    static class Desktop
    {
        public static int ForegroundPid()
        {
            IntPtr fg = Native.GetForegroundWindow();
            if (fg == IntPtr.Zero) return 0;
            int pid;
            Native.GetWindowThreadProcessId(fg, out pid);
            return pid;
        }

        // Processes that own a visible top-level window (minimized counts: it
        // is still something you can get back to). Used only by Sweep.
        public static HashSet<int> WindowedPids()
        {
            HashSet<int> pids = new HashSet<int>();
            Native.EnumWindowsProc cb = delegate(IntPtr h, IntPtr l)
            {
                if (Native.IsWindowVisible(h))
                {
                    int pid;
                    Native.GetWindowThreadProcessId(h, out pid);
                    if (pid != 0) pids.Add(pid);
                }
                return true;
            };
            Native.EnumWindows(cb, IntPtr.Zero);
            GC.KeepAlive(cb);
            return pids;
        }

        // WM_NULL round trip to the taskbar. -1 = no answer within 2 s (or no
        // taskbar at all). SMTO_ABORTIFHUNG returns at once for a hung shell.
        public static int ShellLatencyMs()
        {
            IntPtr tray = Native.FindWindow("Shell_TrayWnd", null);
            if (tray == IntPtr.Zero) return -1;
            Stopwatch sw = Stopwatch.StartNew();
            IntPtr r;
            IntPtr ok = Native.SendMessageTimeout(tray, 0, IntPtr.Zero, IntPtr.Zero, 0x0002, 2000, out r);
            if (ok == IntPtr.Zero) return -1;
            return (int)Math.Min(int.MaxValue, sw.ElapsedMilliseconds);
        }

        public static bool ExplorerStats(out int pid, out int gdi, out int user)
        {
            pid = 0; gdi = 0; user = 0;
            IntPtr tray = Native.FindWindow("Shell_TrayWnd", null);
            if (tray == IntPtr.Zero) return false;
            Native.GetWindowThreadProcessId(tray, out pid);
            IntPtr h = Native.OpenProcess(Native.PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
            if (h == IntPtr.Zero) return true;
            try
            {
                gdi = (int)Native.GetGuiResources(h, 0);
                user = (int)Native.GetGuiResources(h, 1);
            }
            finally { Native.CloseHandle(h); }
            return true;
        }
    }

    // --------------------------------------------------------------- storage

    sealed class DiskInventory
    {
        public Dictionary<int, DiskFacts> ByRaid = new Dictionary<int, DiskFacts>();
        public Dictionary<int, string> LettersByIndex = new Dictionary<int, string>();
    }

    // One event, flattened on the watcher thread so the engine thread can
    // resolve it against its own disk inventory.
    sealed class DiskEvent
    {
        public int Id;
        public string Provider = "";
        public DateTime Time;
        public List<string> Props = new List<string>();

        public static DiskEvent From(EventRecord r)
        {
            DiskEvent e = new DiskEvent();
            e.Id = r.Id;
            e.Provider = r.ProviderName ?? "";
            e.Time = r.TimeCreated.HasValue ? r.TimeCreated.Value : DateTime.Now;
            try
            {
                foreach (EventProperty p in r.Properties)
                {
                    string v = p.Value as string;
                    if (v != null) e.Props.Add(v);
                }
            }
            catch { }
            return e;
        }
    }

    static class Disks
    {
        static readonly Regex RaidRx = new Regex(@"\\Device\\RaidPort(\d+)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        static readonly Regex HarddiskRx = new Regex(@"\\Harddisk(\d+)\\", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        static readonly Regex DiskNRx = new Regex(@"^\s*Disk\s+(\d+)\b", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        static readonly Regex PathLetterRx = new Regex(@"^([A-Za-z]):", RegexOptions.CultureInvariant);
        static readonly Regex VolumeRx = new Regex(@"^([A-Za-z]):$", RegexOptions.CultureInvariant);

        // Storage resets, lost writes, journal flush failures, paging errors,
        // surprise removals. The provider is checked per id below: Event 129
        // is also the time service's, and 50/51 exist for other providers.
        public const string Query = "*[System[(EventID=129 or EventID=50 or EventID=51 or EventID=140 or EventID=157)]]";
        public const string Backfill = "*[System[(EventID=129 or EventID=50 or EventID=51 or EventID=140 or EventID=157) and TimeCreated[timediff(@SystemTime) <= 86400000]]]";

        public static int RaidPortOf(string text)
        {
            if (string.IsNullOrEmpty(text)) return -1;
            Match m = RaidRx.Match(text);
            int n;
            return m.Success && int.TryParse(m.Groups[1].Value, NumberStyles.Integer, CultureInfo.InvariantCulture, out n) ? n : -1;
        }

        public static int RaidPortOf(DiskEvent e)
        {
            if (e.Id != 129) return -1;
            foreach (string p in e.Props)
            {
                int n = RaidPortOf(p);
                if (n >= 0) return n;
            }
            return -1;
        }

        public static int RaidPortOf(EventRecord r) { return RaidPortOf(DiskEvent.From(r)); }

        // "E: S:" -> E:, S:   "disk 10" stays whole   "" -> one unnamed entry.
        public static List<string> SplitVolumes(string volumes)
        {
            List<string> list = new List<string>();
            if (string.IsNullOrEmpty(volumes)) { list.Add(""); return list; }
            if (volumes.StartsWith("disk ", StringComparison.OrdinalIgnoreCase)) { list.Add(volumes); return list; }
            foreach (string v in volumes.Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries)) list.Add(v);
            return list;
        }

        // Last known disks, kept across runs: a disk that drops off before
        // SAIOPT's first look at it today is still named by what it was.
        public static void Save(DiskInventory inv, string path)
        {
            StringBuilder sb = new StringBuilder();
            foreach (KeyValuePair<int, DiskFacts> kv in inv.ByRaid)
                sb.Append("raid|").Append(kv.Key.ToString(CultureInfo.InvariantCulture)).Append('|').Append(kv.Value.Model.Replace("|", "/"))
                  .Append('|').Append(kv.Value.Letters).Append('|').Append(kv.Value.Bus).Append("\r\n");
            foreach (KeyValuePair<int, string> kv in inv.LettersByIndex)
                if (kv.Value.Length > 0) sb.Append("disk|").Append(kv.Key.ToString(CultureInfo.InvariantCulture)).Append('|').Append(kv.Value).Append("\r\n");
            Paths.AtomicWrite(path, sb.ToString());
        }

        public static DiskInventory Load(string path)
        {
            DiskInventory inv = new DiskInventory();
            try
            {
                if (!File.Exists(path)) return inv;
                foreach (string line in File.ReadAllLines(path))
                {
                    string[] f = line.Split('|');
                    int n;
                    if (f.Length >= 5 && f[0] == "raid" && int.TryParse(f[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out n))
                    {
                        DiskFacts d = new DiskFacts();
                        d.RaidPort = n; d.Model = f[2]; d.Letters = f[3]; d.Bus = f[4]; d.Gone = true;
                        inv.ByRaid[n] = d;
                    }
                    else if (f.Length >= 3 && f[0] == "disk" && int.TryParse(f[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out n))
                        inv.LettersByIndex[n] = f[2];
                }
            }
            catch { }
            return inv;
        }

        // kind + the volume letters it concerns ("" when Windows did not say).
        public static bool LossOf(DiskEvent e, DiskInventory inv, out string kind, out string volumes)
        {
            kind = null;
            volumes = "";
            if (e.Id == 50 && string.Equals(e.Provider, "Ntfs", StringComparison.OrdinalIgnoreCase))
            {
                kind = LossKinds.LostWrite;
                foreach (string p in e.Props)
                {
                    Match m = PathLetterRx.Match(p);
                    if (m.Success) { volumes = m.Groups[1].Value.ToUpperInvariant() + ":"; break; }
                }
            }
            else if (e.Id == 140 && string.Equals(e.Provider, "Microsoft-Windows-Ntfs", StringComparison.OrdinalIgnoreCase))
            {
                kind = LossKinds.JournalFlush;
                foreach (string p in e.Props)
                {
                    Match m = VolumeRx.Match(p.Trim());
                    if (m.Success) { volumes = m.Groups[1].Value.ToUpperInvariant() + ":"; break; }
                }
            }
            else if ((e.Id == 51 || e.Id == 157) && string.Equals(e.Provider, "disk", StringComparison.OrdinalIgnoreCase))
            {
                kind = e.Id == 51 ? LossKinds.PagingError : LossKinds.Removed;
                foreach (string p in e.Props)
                {
                    Match m = HarddiskRx.Match(p);
                    if (!m.Success) m = DiskNRx.Match(p);
                    int idx;
                    if (m.Success && int.TryParse(m.Groups[1].Value, NumberStyles.Integer, CultureInfo.InvariantCulture, out idx))
                    {
                        string l;
                        volumes = inv != null && inv.LettersByIndex.TryGetValue(idx, out l) && l.Length > 0 ? l : "disk " + idx;
                        break;
                    }
                }
            }
            return kind != null;
        }

        static string BusOf(int scsiPort)
        {
            try
            {
                using (RegistryKey k = Registry.LocalMachine.OpenSubKey(@"HARDWARE\DEVICEMAP\Scsi\Scsi Port " + scsiPort.ToString(CultureInfo.InvariantCulture)))
                {
                    string drv = k == null ? null : k.GetValue("Driver") as string;
                    if (drv == null) return "";
                    drv = drv.ToLowerInvariant();
                    if (drv == "uaspstor" || drv == "usbstor") return "USB";
                    if (drv == "stornvme") return "NVMe";
                    if (drv == "storahci" || drv.StartsWith("iastor")) return "SATA";
                    return drv;
                }
            }
            catch { return ""; }
        }

        // RaidPort -> the disk that owns it now. \\.\Scsi<port>: is a symbolic
        // link to the storport \Device\RaidPort<N>, and N drifts away from the
        // port number after a USB re-plug -- the link is the only reliable way
        // from an event back to a drive letter (same fix as Shell Doctor).
        // Letters come from MSFT_Partition: the Win32_DiskDrive associator
        // chain answers "Not found" on this very machine.
        public static DiskInventory Inventory()
        {
            DiskInventory inv = new DiskInventory();
            Dictionary<int, int> portToRaid = new Dictionary<int, int>();
            for (int port = 0; port < 64; port++)
            {
                StringBuilder sb = new StringBuilder(1024);
                if (Native.QueryDosDevice("Scsi" + port.ToString(CultureInfo.InvariantCulture) + ":", sb, sb.Capacity) > 0)
                {
                    int raid = RaidPortOf(sb.ToString());
                    if (raid >= 0) portToRaid[port] = raid;
                }
            }

            Dictionary<int, List<string>> letters = new Dictionary<int, List<string>>();
            try
            {
                using (ManagementObjectSearcher s = new ManagementObjectSearcher(new ManagementScope(@"root\Microsoft\Windows\Storage"),
                    new ObjectQuery("SELECT DiskNumber, DriveLetter FROM MSFT_Partition")))
                using (ManagementObjectCollection parts = s.Get())
                {
                    foreach (ManagementObject p in parts)
                    {
                        using (p)
                        {
                            object dl = p["DriveLetter"];
                            if (dl == null) continue;
                            char c = Convert.ToChar(dl, CultureInfo.InvariantCulture);
                            if (c == '\0' || !char.IsLetter(c)) continue;
                            int n = Convert.ToInt32(p["DiskNumber"], CultureInfo.InvariantCulture);
                            List<string> l;
                            if (!letters.TryGetValue(n, out l)) { l = new List<string>(); letters[n] = l; }
                            l.Add(char.ToUpperInvariant(c) + ":");
                        }
                    }
                }
            }
            catch { }

            try
            {
                using (ManagementObjectSearcher s = new ManagementObjectSearcher("SELECT Index, SCSIPort, Model FROM Win32_DiskDrive"))
                using (ManagementObjectCollection disks = s.Get())
                {
                    foreach (ManagementObject d in disks)
                    {
                        using (d)
                        {
                            int index = Convert.ToInt32(d["Index"], CultureInfo.InvariantCulture);
                            List<string> l;
                            string lt = "";
                            if (letters.TryGetValue(index, out l)) { l.Sort(StringComparer.OrdinalIgnoreCase); lt = string.Join(" ", l.ToArray()); }
                            inv.LettersByIndex[index] = lt;

                            string model = Convert.ToString(d["Model"], CultureInfo.InvariantCulture) ?? "";
                            if (model.IndexOf("Storage Space", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                            int port = Convert.ToInt32(d["SCSIPort"], CultureInfo.InvariantCulture);
                            int raid;
                            if (!portToRaid.TryGetValue(port, out raid)) continue;
                            DiskFacts f;
                            if (!inv.ByRaid.TryGetValue(raid, out f))
                            {
                                f = new DiskFacts();
                                f.RaidPort = raid;
                                f.Model = model;
                                f.Bus = BusOf(port);
                                inv.ByRaid[raid] = f;
                            }
                            else if (!f.Model.EndsWith(" +", StringComparison.Ordinal)) f.Model += " +";
                            if (lt.Length > 0) f.Letters = (f.Letters.Length > 0 ? f.Letters + " " : "") + lt;
                        }
                    }
                }
            }
            catch { }
            return inv;
        }

        // A disk that vanished keeps its last known name and letters, marked
        // gone: "U: Samsung SSD 970 EVO (USB) - gone" beats "RaidPort11".
        public static DiskInventory Merge(DiskInventory old, DiskInventory now)
        {
            if (old == null) return now;
            foreach (KeyValuePair<int, DiskFacts> kv in old.ByRaid)
            {
                if (now.ByRaid.ContainsKey(kv.Key)) continue;
                DiskFacts f = kv.Value;
                f.Gone = true;
                now.ByRaid[kv.Key] = f;
            }
            foreach (KeyValuePair<int, string> kv in old.LettersByIndex)
                if (!now.LettersByIndex.ContainsKey(kv.Key) && kv.Value.Length > 0) now.LettersByIndex[kv.Key] = kv.Value;
            return now;
        }
    }

    // One uncached 4 KiB READ from an existing file on the volume. Read-only
    // by construction (GENERIC_READ, OPEN_EXISTING): nothing on the disk is
    // ever written, created or changed. The read has to reach the device, so
    // the system cache is bypassed (NO_BUFFERING, page-aligned buffer).
    static class DiskTouch
    {
        static readonly Dictionary<string, string> chosen = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        // Files that could not be opened (in use exclusively, access denied):
        // never picked again.
        static readonly HashSet<string> refused = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        static readonly Random rnd = new Random();

        // A file of reasonable size near the top of the volume. Bounded: at
        // most 3000 entries are looked at, reparse points and system folders
        // are skipped.
        public static string PickFile(string letter)
        {
            string best = null;
            long bestLen = 0;
            Queue<string> dirs = new Queue<string>();
            dirs.Enqueue(letter + "\\");
            int seen = 0;
            while (dirs.Count > 0 && seen < 3000)
            {
                string d = dirs.Dequeue();
                IEnumerable<string> entries;
                try { entries = Directory.EnumerateFileSystemEntries(d); }
                catch { continue; }
                try
                {
                    foreach (string e in entries)
                    {
                        if (++seen >= 3000) break;
                        FileAttributes a;
                        try { a = File.GetAttributes(e); }
                        catch { continue; }
                        if ((a & FileAttributes.ReparsePoint) != 0) continue;
                        // pagefile.sys, hiberfil.sys, swapfile.sys and friends are
                        // held open by the kernel; system files are not ours to touch.
                        if ((a & FileAttributes.Directory) == 0 && ((a & FileAttributes.System) != 0 || refused.Contains(e))) continue;
                        if ((a & FileAttributes.Directory) != 0)
                        {
                            string name = Path.GetFileName(e);
                            if (name.StartsWith("$") || string.Equals(name, "System Volume Information", StringComparison.OrdinalIgnoreCase)) continue;
                            dirs.Enqueue(e);
                            continue;
                        }
                        long len;
                        try { len = new FileInfo(e).Length; }
                        catch { continue; }
                        if (len >= (16L << 20)) return e;
                        if (len > bestLen && len >= (64L << 10)) { best = e; bestLen = len; }
                    }
                }
                catch { }
            }
            return best;
        }

        public static bool Touch(string letter, out string detail)
        {
            detail = null;
            string file;
            if (!chosen.TryGetValue(letter, out file) || file == null || !File.Exists(file))
            {
                file = PickFile(letter);
                chosen[letter] = file;
                if (file == null) { detail = "no readable file on " + letter; return false; }
            }
            IntPtr buf = Native.VirtualAlloc(IntPtr.Zero, new UIntPtr(4096), 0x3000, 0x04);
            if (buf == IntPtr.Zero) { detail = "VirtualAlloc failed"; return false; }
            try
            {
                using (Microsoft.Win32.SafeHandles.SafeFileHandle h = Native.CreateFile(file, Native.GENERIC_READ, Native.FILE_SHARE_ALL,
                    IntPtr.Zero, Native.OPEN_EXISTING, Native.FILE_FLAG_NO_BUFFERING, IntPtr.Zero))
                {
                    if (h.IsInvalid)
                    {
                        detail = "open failed (" + Marshal.GetLastWin32Error() + ") " + file;
                        chosen.Remove(letter);
                        refused.Add(file);
                        return false;
                    }
                    long size, pos;
                    if (!Native.GetFileSizeEx(h, out size) || size < 4096) { chosen.Remove(letter); detail = "file too small"; return false; }
                    long block;
                    lock (rnd) block = (long)(rnd.NextDouble() * (size / 4096 - 1));
                    Native.SetFilePointerEx(h, block * 4096, out pos, 0);
                    int read;
                    if (!Native.ReadFile(h, buf, 4096, out read, IntPtr.Zero)) { detail = "read failed (" + Marshal.GetLastWin32Error() + ")"; return false; }
                    detail = file;
                    return true;
                }
            }
            finally { Native.VirtualFree(buf, UIntPtr.Zero, 0x8000); }
        }
    }

    // --------------------------------------------------------------- paths

    static class Paths
    {
        public static string ExeDir { get { return AppDomain.CurrentDomain.BaseDirectory; } }
        public static string ExePath { get { return Path.Combine(ExeDir, "SAIOPT.exe"); } }
        public static string ConfigPath { get { return Path.Combine(ExeDir, "saiopt.ini"); } }

        public static string StateDir
        {
            get
            {
                string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                return Path.Combine(Path.Combine(local, "SAITULS"), "saiopt");
            }
        }

        public static string StatusPath { get { return Path.Combine(StateDir, "status.txt"); } }
        public static string EasedPath { get { return Path.Combine(StateDir, "eased.txt"); } }
        public static string LogPath { get { return Path.Combine(StateDir, "saiopt.log"); } }
        public static string DisksPath { get { return Path.Combine(StateDir, "disks.txt"); } }
        public static string SeenPath { get { return Path.Combine(StateDir, "seen.txt"); } }
        public static string AwakePath { get { return Path.Combine(StateDir, "keepawake.txt"); } }

        public static void AtomicWrite(string path, string text)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            string tmp = path + ".tmp";
            File.WriteAllText(tmp, text, new UTF8Encoding(false));
            if (File.Exists(path))
            {
                try { File.Replace(tmp, path, null); return; }
                catch { try { File.Delete(path); } catch { } }
            }
            File.Move(tmp, path);
        }
    }

    // Bounded: never an unattended log that grows without limit.
    static class Log
    {
        const long MaxBytes = 512 * 1024;
        static readonly object gate = new object();
        public static bool Enabled = true;

        public static void Line(string text)
        {
            if (!Enabled) return;
            lock (gate)
            {
                try
                {
                    Directory.CreateDirectory(Paths.StateDir);
                    string path = Paths.LogPath;
                    if (File.Exists(path) && new FileInfo(path).Length > MaxBytes)
                    {
                        try { File.Delete(path + ".1"); } catch { }
                        try { File.Move(path, path + ".1"); } catch { try { File.Delete(path); } catch { } }
                    }
                    File.AppendAllText(path, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture) + "  " + text + Environment.NewLine,
                        new UTF8Encoding(false));
                }
                catch { }
            }
        }
    }

    static class Autostart
    {
        const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        public const string ValueName = "SaitulsSaiopt";

        public static bool Enabled()
        {
            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(RunKey))
                    return k != null && k.GetValue(ValueName) != null;
            }
            catch { return false; }
        }

        public static bool Set(bool on)
        {
            try
            {
                using (RegistryKey k = Registry.CurrentUser.CreateSubKey(RunKey))
                {
                    if (on) k.SetValue(ValueName, "\"" + Paths.ExePath + "\"");
                    else k.DeleteValue(ValueName, false);
                }
                return true;
            }
            catch { return false; }
        }
    }

    // --------------------------------------------------------------- engine

    sealed class EasedRow
    {
        public string Image;
        public string Priority;
        public double Core;
        public DateTime Since;
    }

    sealed class StaleGroup
    {
        public string Image;
        public int Count;
        public long Bytes;
        public string Reason;
        public bool Armed;
        public List<ProcKey> Keys = new List<ProcKey>();
    }

    sealed class AwakeRow
    {
        public string Identity;
        public bool On;                 // being kept awake now
        public bool Declined;           // you switched it off
        public DateTime Since;
        public int ResetsSince;
        public string Failure;          // last read failure, if any
    }

    sealed class Snapshot
    {
        public DateTime At = DateTime.Now;
        public string Status = StatusWords.Calm;
        public string StatusWhy = "starting";
        public int CpuPct, MemPct;
        public double CommitUsedGb, CommitLimitGb;
        public string ShellWord = ShellWords.Unknown;
        public int ShellLastMs, ShellMedianMs;
        public List<EasedRow> Eased = new List<EasedRow>();
        public List<StorageSummary> Storage = new List<StorageSummary>();
        public List<LossIncident> Loss = new List<LossIncident>();
        public HashSet<string> Seen = new HashSet<string>();
        public Dictionary<int, AwakeRow> Awake = new Dictionary<int, AwakeRow>();
        public int ExplorerGdi, ExplorerUser, ExplorerRestarts;
        public bool ExplorerLeak;
        public List<StaleGroup> Stale = new List<StaleGroup>();
        public List<string> Activity = new List<string>();
        public bool EaseOn = true;
        public bool Paused;
        public DateTime PausedUntil;
        public int Processes;
    }

    sealed class Engine
    {
        public OptConfig Cfg;
        public readonly List<string> Warnings = new List<string>();
        readonly bool readOnly;
        readonly EaseEngine ease;
        readonly SweepTracker sweep;
        readonly StorageWatch storage = new StorageWatch();
        readonly LossWatch loss = new LossWatch();
        readonly HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
        // identity -> true (keep awake) / false (you said no). Persisted.
        readonly Dictionary<string, bool> awakeChoice = new Dictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
        readonly Dictionary<string, DateTime> awakeSince = new Dictionary<string, DateTime>(StringComparer.OrdinalIgnoreCase);
        volatile string[] awakeLetters = new string[0];
        readonly Dictionary<string, string> awakeFailures = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        readonly ShellHealth shell = new ShellHealth(150);
        readonly NewsGate news = new NewsGate();
        readonly ProcessSampler sampler = new ProcessSampler();
        readonly ConcurrentQueue<Action> inbox = new ConcurrentQueue<Action>();
        readonly AutoResetEvent wake = new AutoResetEvent(false);
        readonly ManualResetEvent stopping = new ManualResetEvent(false);
        readonly Stopwatch clock = Stopwatch.StartNew();
        readonly List<string> activity = new List<string>();
        readonly int selfPid = Process.GetCurrentProcess().Id;
        readonly DateTime started = DateTime.Now;

        Dictionary<ProcKey, long> prevTicks = new Dictionary<ProcKey, long>();
        long prevAtMs = -1;
        long sysIdle, sysKernel, sysUser;
        bool haveSys;
        readonly Dictionary<ProcKey, long> focusSeen = new Dictionary<ProcKey, long>();
        readonly Dictionary<ProcKey, long> audibleSeen = new Dictionary<ProcKey, long>();
        DiskInventory inv = new DiskInventory();
        long disksAtMs = -1;
        int explorerPid, explorerRestarts, explorerGdi, explorerUser;
        DateTime pausedUntil = DateTime.MinValue;
        List<StaleItem> lastStale = new List<StaleItem>();
        EventLogWatcher watcher;
        DateTime iniStamp = DateTime.MinValue;
        long iniCheckedMs = -100000;
        long statusAtMs = -100000;
        readonly Dictionary<string, long> errorsTold = new Dictionary<string, long>();
        volatile Snapshot snap = new Snapshot();

        public event Action<string, string> Tell;
        public Snapshot Current { get { return snap; } }

        public Engine(OptConfig cfg, bool readOnly)
        {
            Cfg = cfg;
            this.readOnly = readOnly;
            ease = new EaseEngine(cfg);
            sweep = new SweepTracker(cfg);
            if (readOnly) Log.Enabled = false;
        }

        public void Post(Action a) { inbox.Enqueue(a); wake.Set(); }
        public void RequestStop() { stopping.Set(); }
        public bool Stopping { get { return stopping.WaitOne(0); } }

        // ------------------------------------------------------ lifecycle

        public void Run()
        {
            try { Start(); }
            catch (Exception ex) { Log.Line("START_ERROR " + ex.Message); }
            while (!stopping.WaitOne(0))
            {
                Drain();
                try { Tick(); }
                catch (Exception ex) { Throttled("TICK_ERROR " + ex.GetType().Name + ": " + ex.Message); }
                WaitHandle.WaitAny(new WaitHandle[] { stopping, wake }, Cfg.SampleMs);
            }
            Drain();
            Shutdown();
        }

        void Start()
        {
            Log.Line("START pid=" + selfPid + " ease=" + (Cfg.Ease ? "on" : "off") + " busy_pct=" + Cfg.BusyPct
                + " hog_core_pct=" + Cfg.HogCorePct + (readOnly ? " read-only" : ""));
            foreach (string w in Warnings) Log.Line("CONFIG_WARNING " + w);
            if (!readOnly) AdoptPreviousRun();
            try { if (File.Exists(Paths.SeenPath)) foreach (string l in File.ReadAllLines(Paths.SeenPath)) if (l.Trim().Length > 0) seen.Add(l.Trim()); }
            catch { }
            try
            {
                if (File.Exists(Paths.AwakePath))
                    foreach (string l in File.ReadAllLines(Paths.AwakePath))
                    {
                        int bar = l.IndexOf('|');
                        if (bar < 2) continue;
                        string head = l.Substring(0, bar);
                        if (head == "on" || head == "no") awakeChoice[l.Substring(bar + 1)] = head == "on";
                    }
            }
            catch { }
            if (!readOnly)
            {
                Thread keeper = new Thread(KeepAwakeLoop);
                keeper.IsBackground = true;
                keeper.Name = "SAIOPT keep-awake";
                keeper.Start();
            }
            RefreshDisks();
            if (Cfg.WatchStorage) StartStorageWatch();
            ThreadPool.QueueUserWorkItem(delegate { ShellProbeLoop(); });
        }

        void Shutdown()
        {
            if (!readOnly)
            {
                Apply(ease.RestoreAll("SAIOPT stopped"));
                SaveEased();
                WriteStatus(false);
            }
            try { if (watcher != null) { watcher.Enabled = false; watcher.Dispose(); } }
            catch { }
            sampler.Dispose();
            Log.Line("STOP");
        }

        void Drain()
        {
            Action a;
            while (inbox.TryDequeue(out a))
            {
                try { a(); }
                catch (Exception ex) { Throttled("COMMAND_ERROR " + ex.Message); }
            }
        }

        void Throttled(string msg)
        {
            long now = clock.ElapsedMilliseconds;
            long at;
            if (errorsTold.TryGetValue(msg, out at) && now - at < 600000) return;
            errorsTold[msg] = now;
            Log.Line(msg);
        }

        void ShellProbeLoop()
        {
            while (!stopping.WaitOne(2000))
            {
                if (!Cfg.WatchShell) continue;
                int ms = Desktop.ShellLatencyMs();
                inbox.Enqueue(delegate { shell.Add(ms); });
            }
        }

        void RefreshDisks()
        {
            if (inv.ByRaid.Count == 0 && inv.LettersByIndex.Count == 0) inv = Disks.Load(Paths.DisksPath);
            inv = Disks.Merge(inv, Disks.Inventory());
            disksAtMs = clock.ElapsedMilliseconds;
            if (!readOnly)
            {
                try { Disks.Save(inv, Paths.DisksPath); }
                catch (Exception ex) { Throttled("DISKS_STATE_ERROR " + ex.Message); }
            }
        }

        void Ingest(DiskEvent e, bool live)
        {
            int port = Disks.RaidPortOf(e);
            if (port >= 0)
            {
                storage.Add(port, e.Time);
                if (live) Log.Line("STORAGE_RESET RaidPort" + port);
                if (live && !inv.ByRaid.ContainsKey(port)) disksAtMs = -1000000;
                return;
            }
            string kind, volumes;
            // A disk NUMBER in an old event is resolved to letters only live:
            // after a re-plug the same number belongs to another disk, and
            // today's map would pin yesterday's failure on the wrong drive.
            if (!Disks.LossOf(e, live ? inv : null, out kind, out volumes)) return;
            foreach (string v in Disks.SplitVolumes(volumes)) loss.Add(e.Time, kind, v);
            if (live)
            {
                Log.Line("DISK_" + kind.ToUpperInvariant().Replace('-', '_') + " " + (volumes.Length > 0 ? volumes : "?") + " (" + e.Provider + " " + e.Id + ")");
                disksAtMs = -1000000;   // re-enumerate soon: a device may have gone
            }
        }

        void StartStorageWatch()
        {
            try
            {
                using (EventLogReader reader = new EventLogReader(new EventLogQuery("System", PathType.LogName, Disks.Backfill)))
                {
                    for (EventRecord r = reader.ReadEvent(); r != null; r = reader.ReadEvent())
                        using (r) Ingest(DiskEvent.From(r), false);
                }
            }
            catch (Exception ex) { Log.Line("STORAGE_BACKFILL_ERROR " + ex.Message); }

            try
            {
                watcher = new EventLogWatcher(new EventLogQuery("System", PathType.LogName, Disks.Query));
                watcher.EventRecordWritten += delegate(object s, EventRecordWrittenEventArgs e)
                {
                    if (e.EventRecord == null) return;
                    DiskEvent de;
                    using (EventRecord r = e.EventRecord) de = DiskEvent.From(r);
                    Post(delegate { Ingest(de, true); });
                };
                watcher.Enabled = true;
            }
            catch (Exception ex) { Log.Line("STORAGE_WATCH_ERROR " + ex.Message); }
        }

        // ------------------------------------------------------ commands

        public void Pause(TimeSpan span)
        {
            Post(delegate
            {
                pausedUntil = DateTime.Now + span;
                Apply(ease.RestoreAll("easing paused"));
                SaveEased();
                Activity("paused easing until " + pausedUntil.ToString("HH:mm", CultureInfo.InvariantCulture));
            });
        }

        public void Resume()
        {
            Post(delegate
            {
                pausedUntil = DateTime.MinValue;
                Activity("resumed easing");
            });
        }

        public void EndStale(List<ProcKey> keys, string why)
        {
            Post(delegate
            {
                int ended = 0;
                long bytes = 0;
                string image = null;
                foreach (ProcKey k in keys)
                {
                    StaleItem it = lastStale.Find(delegate(StaleItem s) { return s.Key.Equals(k); });
                    if (it == null) continue;           // no longer stale: never ended on old evidence
                    string err;
                    if (Procs.EndProcess(k, out err))
                    {
                        ended++; bytes += it.PrivateBytes; image = it.Image;
                        Log.Line("ENDED " + it.Image + " pid=" + k.Pid + " (" + it.Reason + ") " + why);
                    }
                    else Log.Line("END_FAILED " + it.Image + " pid=" + k.Pid + " " + err);
                }
                if (ended > 0) Activity("ended " + ended + "x stale " + image + " (" + Words.Bytes(bytes) + ") - " + why);
            });
        }

        // You saw it: the incident stays listed but no longer turns the status
        // red. Remembered across restarts; incidents age out after 24 h anyway.
        public void MarkSeen(string subject)
        {
            Post(delegate
            {
                seen.Add(subject);
                if (readOnly) return;
                try
                {
                    List<string> keep = new List<string>();
                    foreach (LossIncident li in loss.Incidents(DateTime.Now)) if (seen.Contains(li.Subject)) keep.Add(li.Subject);
                    Paths.AtomicWrite(Paths.SeenPath, string.Join("\r\n", keep.ToArray()) + "\r\n");
                }
                catch (Exception ex) { Throttled("SEEN_STATE_ERROR " + ex.Message); }
            });
        }

        // Its own thread: a read on a disk that is mid-reset can block for
        // tens of seconds, and that must never stall sampling or easing.
        void KeepAwakeLoop()
        {
            while (!stopping.WaitOne(Math.Max(15, Cfg.KeepAwakeSec) * 1000))
            {
                foreach (string letter in awakeLetters)
                {
                    if (stopping.WaitOne(0)) return;
                    string detail;
                    bool ok;
                    try { ok = DiskTouch.Touch(letter, out detail); }
                    catch (Exception ex) { ok = false; detail = ex.GetType().Name; }
                    string l = letter, d = detail;
                    Post(delegate
                    {
                        if (ok) { awakeFailures.Remove(l); return; }
                        awakeFailures[l] = d;
                        Throttled("KEEP_AWAKE_READ_FAILED " + l + " " + d);
                    });
                }
            }
        }

        public void SetKeepAwake(string identity, bool on)
        {
            Post(delegate
            {
                awakeChoice[identity] = on;
                SaveAwake();
                Activity((on ? "keeping " : "stopped keeping ") + identity.Split('|')[0] + " awake - you chose");
            });
        }

        void SaveAwake()
        {
            if (readOnly) return;
            try
            {
                StringBuilder sb = new StringBuilder();
                foreach (KeyValuePair<string, bool> kv in awakeChoice) sb.Append(kv.Value ? "on|" : "no|").Append(kv.Key).Append("\r\n");
                Paths.AtomicWrite(Paths.AwakePath, sb.ToString());
            }
            catch (Exception ex) { Throttled("AWAKE_STATE_ERROR " + ex.Message); }
        }

        // Earn, then hold: a USB disk with the periodic-reset signature is
        // kept awake from then on (unless you said no); the letters actually
        // read are the ones of disks present right now.
        void UpdateKeepAwake(List<StorageSummary> sums)
        {
            if (Cfg.KeepAwake == KeepAwakeModes.Auto)
            {
                foreach (StorageSummary s in sums)
                {
                    if (!KeepAwakeRule.Qualifies(s)) continue;
                    string id = KeepAwakeRule.Identity(s.Disk);
                    if (awakeChoice.ContainsKey(id)) continue;
                    awakeChoice[id] = true;
                    SaveAwake();
                    Activity("keeping " + s.Disk.Letters + " awake - it resets every " + Words.Duration((long)(s.Cadence.MedianSeconds * 1000))
                        + " when idle");
                    Log.Line("KEEP_AWAKE_ON " + id);
                }
            }
            List<string> letters = new List<string>();
            if (Cfg.KeepAwake != KeepAwakeModes.Off)
            {
                foreach (DiskFacts d in inv.ByRaid.Values)
                {
                    bool on;
                    if (d.Gone || d.Letters.Length == 0 || !awakeChoice.TryGetValue(KeepAwakeRule.Identity(d), out on) || !on) continue;
                    string first = d.Letters.Split(' ')[0];
                    letters.Add(first);
                    if (!awakeSince.ContainsKey(first)) awakeSince[first] = DateTime.Now;
                }
            }
            foreach (string k in new List<string>(awakeSince.Keys)) if (!letters.Contains(k)) awakeSince.Remove(k);
            awakeLetters = letters.ToArray();
        }

        public void KeepFast(string image)
        {
            Post(delegate
            {
                HashSet<string> set = new HashSet<string>(Cfg.NeverEase, StringComparer.OrdinalIgnoreCase);
                set.Add(image);
                WriteConfigValue("never_ease", OptConfig.FormatImageList(set));
                Activity(image + " will always keep full speed");
            });
        }

        public void SetEase(bool on)
        {
            Post(delegate
            {
                WriteConfigValue("ease", on ? "1" : "0");
                Activity(on ? "auto-ease switched on" : "auto-ease switched off");
            });
        }

        public void ArmSweep(string image, bool on)
        {
            Post(delegate
            {
                HashSet<string> set = new HashSet<string>(Cfg.SweepAuto, StringComparer.OrdinalIgnoreCase);
                if (on) set.Add(image); else set.Remove(image);
                WriteConfigValue("sweep_auto", OptConfig.FormatImageList(set));
                Activity(on ? "stale " + image + " will be ended automatically" : "stale " + image + " is back to ask-first");
            });
        }

        void WriteConfigValue(string key, string value)
        {
            if (readOnly) return;
            string text = File.Exists(Paths.ConfigPath) ? File.ReadAllText(Paths.ConfigPath) : Program.DefaultConfigText();
            File.WriteAllText(Paths.ConfigPath, OptConfig.SetIniValue(text, key, value), new UTF8Encoding(false));
            iniCheckedMs = -100000;
            ReloadConfig(clock.ElapsedMilliseconds);
        }

        void ReloadConfig(long now)
        {
            if (now - iniCheckedMs < 5000) return;
            iniCheckedMs = now;
            try
            {
                if (!File.Exists(Paths.ConfigPath)) return;
                DateTime stamp = File.GetLastWriteTimeUtc(Paths.ConfigPath);
                if (stamp == iniStamp) return;
                bool first = iniStamp == DateTime.MinValue;
                iniStamp = stamp;
                if (first) return;
                List<string> warnings = new List<string>();
                OptConfig c = OptConfig.Parse(OptConfig.ParseIni(File.ReadAllText(Paths.ConfigPath)), warnings);
                Cfg = c; ease.Cfg = c; sweep.Cfg = c;
                Log.Line("CONFIG_RELOADED ease=" + (c.Ease ? "on" : "off") + " sweep_auto=" + OptConfig.FormatImageList(c.SweepAuto));
                foreach (string w in warnings) Log.Line("CONFIG_WARNING " + w);
            }
            catch (Exception ex) { Throttled("CONFIG_ERROR " + ex.Message); }
        }

        // ------------------------------------------------------ the tick

        void Tick()
        {
            long now = clock.ElapsedMilliseconds;
            ReloadConfig(now);
            if (pausedUntil != DateTime.MinValue && DateTime.Now >= pausedUntil)
            {
                pausedUntil = DateTime.MinValue;
                Activity("easing resumed after the pause");
            }
            bool paused = pausedUntil != DateTime.MinValue;

            List<RawProc> raw = sampler.Snapshot();
            double busy = MachineBusy();
            double elapsed = prevAtMs < 0 ? 0 : now - prevAtMs;

            Dictionary<int, RawProc> byPid = new Dictionary<int, RawProc>(raw.Count);
            foreach (RawProc r in raw) byPid[r.Pid] = r;
            Dictionary<int, List<int>> children = new Dictionary<int, List<int>>();
            foreach (RawProc r in raw)
            {
                RawProc parent;
                if (r.Pid == 0 || !byPid.TryGetValue(r.ParentPid, out parent) || parent.CreateTime > r.CreateTime || parent.Pid == r.Pid) continue;
                List<int> l;
                if (!children.TryGetValue(r.ParentPid, out l)) { l = new List<int>(); children[r.ParentPid] = l; }
                l.Add(r.Pid);
            }

            int fgPid = Desktop.ForegroundPid();
            RawProc fgRoot = Root(fgPid, byPid);
            HashSet<int> focus = Family(new int[] { fgPid }, byPid, children);
            HashSet<int> windowed = Desktop.WindowedPids();
            HashSet<int> inputHelpers = new HashSet<int>();
            foreach (RawProc r in raw)
            {
                RawProc parent;
                if (Guard.IsInputHook(r.Image) && byPid.TryGetValue(r.ParentPid, out parent) && parent.CreateTime <= r.CreateTime)
                    inputHelpers.Add(parent.Pid);
            }
            bool needAudio = Cfg.ProtectAudio && !paused && (busy >= Cfg.CalmPct || ease.EasedCount > 0);
            HashSet<int> audible = needAudio ? Family(CoreAudio.AudiblePids(), byPid, children) : new HashSet<int>();

            long fileNow = Procs.FileTimeNow();
            List<ProcView> views = new List<ProcView>(raw.Count);
            Dictionary<ProcKey, long> ticks = new Dictionary<ProcKey, long>(raw.Count);
            foreach (RawProc r in raw)
            {
                if (r.Pid == 0) continue;
                ProcView v = new ProcView();
                v.Key = new ProcKey(r.Pid, r.CreateTime);
                v.Image = r.Image;
                v.ParentPid = r.ParentPid;
                RawProc parent;
                v.ParentAlive = byPid.TryGetValue(r.ParentPid, out parent) && parent.CreateTime <= r.CreateTime && parent.Pid != r.Pid;
                v.SessionId = r.SessionId;
                v.BasePriority = r.BasePriority;
                v.CpuTicks = r.CpuTicks;
                v.PrivateBytes = r.PrivateBytes;
                v.AgeMs = r.CreateTime > 0 ? Math.Max(0, (fileNow - r.CreateTime) / 10000) : 0;
                v.Self = r.Pid == selfPid;
                v.Suspended = r.Suspended;
                v.HasWindow = windowed.Contains(r.Pid);
                v.InputHelper = inputHelpers.Contains(r.Pid);
                long prev;
                if (elapsed > 0 && prevTicks.TryGetValue(v.Key, out prev) && r.CpuTicks >= prev)
                    v.CoreShare = (r.CpuTicks - prev) / elapsed / 100.0;
                ticks[v.Key] = r.CpuTicks;

                if (focus.Contains(r.Pid)) focusSeen[v.Key] = now;
                long seenAt;
                v.Focused = focusSeen.TryGetValue(v.Key, out seenAt) && now - seenAt <= Cfg.FocusGraceMs;
                if (audible.Contains(r.Pid)) audibleSeen[v.Key] = now;
                v.Audible = audibleSeen.TryGetValue(v.Key, out seenAt) && now - seenAt <= 10000;
                views.Add(v);
            }
            prevTicks = ticks;
            prevAtMs = now;
            Prune(focusSeen, ticks);
            Prune(audibleSeen, ticks);

            // Ease
            if (!readOnly && !paused && elapsed > 0)
            {
                List<EaseAction> acts = ease.Observe(views, busy, now, fgRoot != null ? fgRoot.Image : null);
                if (acts.Count > 0) { Apply(acts); SaveEased(); }
            }

            // Sweep
            lastStale = sweep.Observe(views, now);
            if (!readOnly)
            {
                List<ProcKey> auto = new List<ProcKey>();
                foreach (StaleItem it in lastStale) if (it.Auto) auto.Add(it.Key);
                if (auto.Count > 0) EndStaleNow(auto);
            }

            // Watch
            int pid, gdi, user;
            if (Desktop.ExplorerStats(out pid, out gdi, out user))
            {
                if (explorerPid != 0 && pid != explorerPid)
                {
                    explorerRestarts++;
                    Activity("Explorer restarted");
                }
                explorerPid = pid; explorerGdi = gdi; explorerUser = user;
            }
            if (now - disksAtMs > 600000) RefreshDisks();
            storage.Prune(DateTime.Now);
            loss.Prune(DateTime.Now);

            if (Cfg.WatchStorage) UpdateKeepAwake(storage.Summaries(DateTime.Now, inv.ByRaid));
            Publish(now, busy, raw.Count);
            if (!readOnly && now - statusAtMs >= 30000) { WriteStatus(true); statusAtMs = now; }
        }

        void EndStaleNow(List<ProcKey> keys)
        {
            int ended = 0;
            long bytes = 0;
            string image = null;
            foreach (ProcKey k in keys)
            {
                StaleItem it = lastStale.Find(delegate(StaleItem s) { return s.Key.Equals(k); });
                if (it == null) continue;
                string err;
                if (Procs.EndProcess(k, out err))
                {
                    ended++; bytes += it.PrivateBytes; image = it.Image;
                    Log.Line("ENDED_AUTO " + it.Image + " pid=" + k.Pid + " (" + it.Reason + ")");
                }
                else Log.Line("END_FAILED " + it.Image + " pid=" + k.Pid + " " + err);
            }
            if (ended > 0)
            {
                string line = "ended " + ended + "x stale " + image + " (" + Words.Bytes(bytes) + ") - armed by you";
                Activity(line);
                Announce("sweep:" + image + ":" + DateTime.Now.Ticks, "done", "SAIOPT", "Ended " + ended + "x stale " + image
                    + " (" + Words.Bytes(bytes) + "). Armed in saiopt.ini (sweep_auto).");
            }
        }

        static void Prune(Dictionary<ProcKey, long> map, Dictionary<ProcKey, long> live)
        {
            if (map.Count <= live.Count) return;
            List<ProcKey> dead = new List<ProcKey>();
            foreach (ProcKey k in map.Keys) if (!live.ContainsKey(k)) dead.Add(k);
            foreach (ProcKey k in dead) map.Remove(k);
        }

        static RawProc Root(int pid, Dictionary<int, RawProc> byPid)
        {
            RawProc r;
            if (pid == 0 || !byPid.TryGetValue(pid, out r)) return null;
            for (int guard = 0; guard < 16; guard++)
            {
                RawProc parent;
                if (!byPid.TryGetValue(r.ParentPid, out parent) || parent.CreateTime > r.CreateTime || parent.Pid == r.Pid
                    || parent.Image != r.Image) break;
                r = parent;
            }
            return r;
        }

        // A family is the topmost same-image ancestor (a browser's main
        // process, an Electron app's main) and everything below it: the
        // renderer that decodes your video is protected with the window
        // that shows it.
        static HashSet<int> Family(IEnumerable<int> seeds, Dictionary<int, RawProc> byPid, Dictionary<int, List<int>> children)
        {
            HashSet<int> set = new HashSet<int>();
            foreach (int seed in seeds)
            {
                RawProc root = Root(seed, byPid);
                if (root == null) continue;
                Stack<int> stack = new Stack<int>();
                stack.Push(root.Pid);
                while (stack.Count > 0)
                {
                    int p = stack.Pop();
                    if (!set.Add(p)) continue;
                    List<int> kids;
                    if (children.TryGetValue(p, out kids)) foreach (int k in kids) stack.Push(k);
                }
            }
            return set;
        }

        double MachineBusy()
        {
            long idle, kernel, user;
            if (!Native.GetSystemTimes(out idle, out kernel, out user)) return 0;
            double busy = 0;
            if (haveSys)
            {
                long di = idle - sysIdle, total = (kernel - sysKernel) + (user - sysUser);
                if (total > 0) busy = Math.Max(0, Math.Min(100, 100.0 * (total - di) / total));
            }
            sysIdle = idle; sysKernel = kernel; sysUser = user; haveSys = true;
            return busy;
        }

        void Apply(List<EaseAction> acts)
        {
            foreach (EaseAction a in acts)
            {
                string err;
                switch (a.Kind)
                {
                    case EaseKind.Ease:
                        if (Procs.SetPriority(a.Key, Priorities.Normal, a.ToPriority, out err))
                        {
                            Log.Line("EASED " + a.Image + " pid=" + a.Key.Pid + " -> " + Priorities.Name(a.ToPriority) + " | " + a.Reason);
                            Activity("eased " + a.Image + " - " + a.Reason);
                        }
                        else
                        {
                            ease.Refuse(a.Key);
                            Log.Line("EASE_SKIPPED " + a.Image + " pid=" + a.Key.Pid + " " + err);
                        }
                        break;
                    case EaseKind.Restore:
                        if (Procs.SetPriority(a.Key, a.FromPriority, a.ToPriority, out err))
                        {
                            Log.Line("RESTORED " + a.Image + " pid=" + a.Key.Pid + " -> " + Priorities.Name(a.ToPriority) + " | " + a.Reason);
                            Activity("restored " + a.Image + " - " + a.Reason);
                        }
                        else Log.Line("RESTORE_SKIPPED " + a.Image + " pid=" + a.Key.Pid + " " + err);
                        break;
                    default:
                        Log.Line("FORGOT " + a.Image + " pid=" + a.Key.Pid + " | " + a.Reason);
                        if (a.Reason.IndexOf("someone else", StringComparison.Ordinal) >= 0)
                            Activity(a.Image + ": " + a.Reason);
                        break;
                }
            }
        }

        void Activity(string line)
        {
            activity.Insert(0, DateTime.Now.ToString("HH:mm", CultureInfo.InvariantCulture) + "  " + line);
            if (activity.Count > 60) activity.RemoveRange(60, activity.Count - 60);
        }

        void Announce(string subject, string verdict, string title, string text)
        {
            if (!Cfg.Balloons) return;
            if (!news.ShouldTell(subject, verdict, clock.ElapsedMilliseconds, Cfg.RemindMin)) return;
            Action<string, string> t = Tell;
            if (t != null) t(title, text);
        }

        // ------------------------------------------------------ state

        void AdoptPreviousRun()
        {
            if (!File.Exists(Paths.EasedPath)) return;
            List<EasedEntry> entries = ReadEased();
            if (entries.Count == 0) return;
            List<RawProc> raw = sampler.Snapshot();
            int adopted = 0;
            foreach (EasedEntry e in entries)
            {
                RawProc r = raw.Find(delegate(RawProc x) { return x.Pid == e.Key.Pid && x.CreateTime == e.Key.CreateTime; });
                if (r == null || r.BasePriority != e.EasedPriority) continue;
                e.SinceMs = clock.ElapsedMilliseconds;
                ease.Adopt(e);
                adopted++;
            }
            Log.Line("ADOPTED " + adopted + " of " + entries.Count + " processes eased by the previous run");
            SaveEased();
        }

        public static List<EasedEntry> ReadEased()
        {
            List<EasedEntry> list = new List<EasedEntry>();
            try
            {
                foreach (string line in File.ReadAllLines(Paths.EasedPath))
                {
                    string[] f = line.Split('|');
                    if (f.Length < 4) continue;
                    ProcKey key;
                    int orig, eased;
                    if (!ProcKey.TryParse(f[0], out key)) continue;
                    if (!int.TryParse(f[2], NumberStyles.Integer, CultureInfo.InvariantCulture, out orig)) continue;
                    if (!int.TryParse(f[3], NumberStyles.Integer, CultureInfo.InvariantCulture, out eased)) continue;
                    EasedEntry e = new EasedEntry();
                    e.Key = key; e.Image = f[1]; e.OriginalPriority = orig; e.EasedPriority = eased;
                    list.Add(e);
                }
            }
            catch { }
            return list;
        }

        // Crash safety: every process SAIOPT holds eased is on disk, so a
        // killed SAIOPT (or a reboot of it) restores them on the next start.
        void SaveEased()
        {
            if (readOnly) return;
            try
            {
                StringBuilder sb = new StringBuilder();
                foreach (EasedEntry e in ease.Eased())
                    sb.Append(e.Key).Append('|').Append(e.Image).Append('|')
                      .Append(e.OriginalPriority.ToString(CultureInfo.InvariantCulture)).Append('|')
                      .Append(e.EasedPriority.ToString(CultureInfo.InvariantCulture)).Append("\r\n");
                Paths.AtomicWrite(Paths.EasedPath, sb.ToString());
            }
            catch (Exception ex) { Throttled("STATE_ERROR " + ex.Message); }
        }

        void WriteStatus(bool running)
        {
            try
            {
                Snapshot s = snap;
                StringBuilder sb = new StringBuilder();
                sb.AppendLine("pid=" + selfPid);
                sb.AppendLine("running=" + (running ? "true" : "false"));
                sb.AppendLine("started=" + started.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture));
                sb.AppendLine("written=" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture));
                sb.AppendLine("status=" + s.Status);
                sb.AppendLine("status_why=" + s.StatusWhy);
                sb.AppendLine("eased=" + s.Eased.Count);
                sb.AppendLine("shell=" + s.ShellWord + " " + s.ShellLastMs + "ms");
                sb.AppendLine("cpu_pct=" + s.CpuPct);
                sb.AppendLine("stale=" + s.Stale.Count);
                foreach (StorageSummary st in s.Storage)
                    sb.AppendLine("storage=" + st.Who() + " last_hour=" + st.LastHour + " last_24h=" + st.Last24h + (st.Active ? " ACTIVE" : ""));
                Paths.AtomicWrite(Paths.StatusPath, sb.ToString());
            }
            catch { }
        }

        void Publish(long now, double busy, int processes)
        {
            Snapshot s = new Snapshot();
            s.At = DateTime.Now;
            s.Processes = processes;
            s.CpuPct = (int)Math.Round(busy);
            Native.MEMORYSTATUSEX m = new Native.MEMORYSTATUSEX();
            m.dwLength = (uint)Marshal.SizeOf(typeof(Native.MEMORYSTATUSEX));
            if (Native.GlobalMemoryStatusEx(ref m))
            {
                s.MemPct = (int)m.dwMemoryLoad;
                s.CommitLimitGb = m.ullTotalPageFile / 1073741824.0;
                s.CommitUsedGb = (m.ullTotalPageFile - m.ullAvailPageFile) / 1073741824.0;
            }
            s.ShellWord = Cfg.WatchShell ? shell.Word(Cfg.ShellSlowMs) : ShellWords.Unknown;
            s.ShellLastMs = shell.Last;
            s.ShellMedianMs = shell.Median();

            foreach (EasedEntry e in ease.Eased())
            {
                EasedRow row = new EasedRow();
                row.Image = e.Image;
                row.Priority = Priorities.Name(e.EasedPriority);
                row.Core = e.CoreShare;
                row.Since = DateTime.Now.AddMilliseconds(-(now - e.SinceMs));
                s.Eased.Add(row);
            }

            s.Storage = Cfg.WatchStorage ? storage.Summaries(DateTime.Now, inv.ByRaid) : new List<StorageSummary>();
            s.Loss = Cfg.WatchStorage ? loss.Incidents(DateTime.Now) : new List<LossIncident>();
            s.Seen = new HashSet<string>(seen);
            foreach (StorageSummary st in s.Storage)
            {
                if (st.Disk == null) continue;
                string id = KeepAwakeRule.Identity(st.Disk);
                bool choice;
                bool known = awakeChoice.TryGetValue(id, out choice);
                if (!known && !KeepAwakeRule.Qualifies(st)) continue;
                AwakeRow row = new AwakeRow();
                row.Identity = id;
                string first = st.Disk.Letters.Length > 0 ? st.Disk.Letters.Split(' ')[0] : "";
                row.Declined = known && !choice;
                DateTime since;
                row.On = !st.Disk.Gone && awakeSince.TryGetValue(first, out since);
                if (row.On)
                {
                    row.Since = awakeSince[first];
                    row.ResetsSince = storage.CountSince(st.RaidPort, row.Since);
                }
                string fail;
                if (awakeFailures.TryGetValue(first, out fail)) row.Failure = fail;
                s.Awake[st.RaidPort] = row;
            }
            s.ExplorerGdi = explorerGdi;
            s.ExplorerUser = explorerUser;
            s.ExplorerRestarts = explorerRestarts;
            s.ExplorerLeak = ExplorerLimits.Leaking(explorerGdi, explorerUser);

            foreach (KeyValuePair<string, List<StaleItem>> kv in SweepTracker.Group(lastStale))
            {
                StaleGroup g = new StaleGroup();
                g.Image = kv.Key;
                g.Count = kv.Value.Count;
                g.Bytes = SweepTracker.Bytes(kv.Value);
                g.Reason = kv.Value[0].Reason;
                g.Armed = Cfg.SweepAuto.Contains(kv.Key);
                foreach (StaleItem it in kv.Value) g.Keys.Add(it.Key);
                s.Stale.Add(g);
            }
            s.Activity = new List<string>(activity);
            s.EaseOn = Cfg.Ease;
            s.Paused = pausedUntil != DateTime.MinValue;
            s.PausedUntil = pausedUntil;

            bool storageActive = false;
            StorageSummary worst = null;
            foreach (StorageSummary st in s.Storage)
                if (st.Active) { storageActive = true; if (worst == null) worst = st; }
            LossIncident risk = null;
            foreach (LossIncident li in s.Loss)
                if (li.DataAtRisk && !seen.Contains(li.Subject) && (DateTime.Now - li.Last).TotalHours <= 6) { risk = li; break; }
            s.Status = StatusWords.Of(s.Paused, s.Eased.Count, storageActive, s.ShellWord, s.ExplorerLeak, risk != null);
            s.StatusWhy = Why(s, worst, busy, risk);
            snap = s;

            // A failed write is told once per incident, however old SAIOPT
            // finds it: the popup Windows showed may have gone unseen.
            foreach (LossIncident li in s.Loss)
            {
                if (!li.DataAtRisk || seen.Contains(li.Subject)) continue;
                if (!Cfg.Balloons || !news.ShouldTell(li.Subject, "loss", clock.ElapsedMilliseconds, 1000000)) continue;
                Action<string, string> t = Tell;
                if (t != null) t("SAIOPT - writes lost on " + li.Where(), "Windows could not save data on " + li.Where() + " at "
                    + li.First.ToString("HH:mm", CultureInfo.InvariantCulture) + " (" + li.What() + "). When the disk is back, run chkdsk /f on it "
                    + "and check files written around that time.");
            }

            if (worst != null)
                Announce("storage:" + worst.RaidPort, "active", "SAIOPT - disk resets",
                    worst.Who() + " reset " + worst.LastHour + "x in the last hour. Each reset freezes Explorer and Start. "
                    + (worst.Disk != null && worst.Disk.Bus == "USB" ? "Use a rear USB port without a hub, or replace the enclosure." : "Check the cable and the drive health."));
            if (s.ShellWord == ShellWords.Stalled)
                Announce("shell", "stalled", "SAIOPT - taskbar not answering", "The taskbar did not answer within 2 s. "
                    + (storageActive ? "A disk is resetting right now." : "Nothing on the disks explains it."));
            if (s.ExplorerLeak)
                Announce("explorer-leak", "leak", "SAIOPT - Explorer near its limit", "Explorer holds " + explorerGdi + " GDI and "
                    + explorerUser + " USER objects (limit 10000). Menus and icons fail before it crashes; restart Explorer when convenient.");
        }

        string Why(Snapshot s, StorageSummary worst, double busy, LossIncident risk)
        {
            if (risk != null) return "writes lost on " + risk.Where() + " at " + risk.First.ToString("HH:mm", CultureInfo.InvariantCulture);
            if (worst != null) return worst.Who() + " keeps resetting - the shell freezes each time";
            if (s.ShellWord == ShellWords.Stalled) return "the taskbar is not answering";
            if (s.ShellWord == ShellWords.Slow) return "the taskbar answers slowly (" + s.ShellLastMs + " ms)";
            if (s.ExplorerLeak) return "Explorer is near its GDI/USER limit";
            if (s.Paused) return "easing paused until " + s.PausedUntil.ToString("HH:mm", CultureInfo.InvariantCulture);
            if (s.Eased.Count > 0) return s.Eased.Count + " background process" + (s.Eased.Count == 1 ? "" : "es") + " yield to what you use";
            if (!Cfg.Ease) return "watching only - auto-ease is off";
            if (busy >= Cfg.BusyPct) return "busy, but nothing in the background is hogging";
            return "the machine has headroom";
        }
    }

    // ------------------------------------------------------------------- UI

    static class Pal
    {
        public static readonly Color BG = C(0x1A1810);
        public static readonly Color BG_SOFT = C(0x232018);
        public static readonly Color RAISED = C(0x3D372A);
        public static readonly Color BDARK = C(0x100E08);
        public static readonly Color BHL = C(0xF0D060);
        public static readonly Color BEVEL = C(0x75663D);
        public static readonly Color BMUTED = C(0x5A5040);
        public static readonly Color TEXT = C(0xD4C89A);
        public static readonly Color TEXT2 = C(0x9C9371);
        public static readonly Color MUTED = C(0x6E674E);
        public static readonly Color DANGERTXT = C(0xD66464);
        public static readonly Color COMPARE = C(0x14120C);
        public static readonly Color LINK = C(0xF0D060);
        static Color C(int rgb) { return Color.FromArgb((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF); }
    }

    // What the panel needs from whoever hosts it: the running app, or the
    // read-only --shot renderer.
    interface IPanelHost
    {
        Engine Engine { get; }
        void ConfirmEnd(StaleGroup g);
        void ToggleArm(StaleGroup g);
        void ToggleAutostart();
        void OpenLog();
        void ExitApp();
    }

    // Golden Default, the SAITULS way: Verdana without antialiasing, square
    // corners, flat fills, two-pixel bevels, no animation. Status is a word.
    sealed class PanelForm : Form
    {
        public const int W = 460, H = 470;
        readonly IPanelHost app;
        readonly Dictionary<int, Font> fonts = new Dictionary<int, Font>();
        readonly Dictionary<int, Font> bolds = new Dictionary<int, Font>();
        readonly StringFormat centered = new StringFormat();
        readonly List<Btn> buttons = new List<Btn>();
        readonly System.Windows.Forms.Timer repaint = new System.Windows.Forms.Timer();
        int focus = -1;
        int lastActivate;
        public bool Asking;
        public int HiddenAtTick;
        // Keys only press buttons after a real click inside the panel. The
        // panel comes up in front of whatever has focus; on this machine that
        // can be a game with a macro typing Tab/Space/Enter 60 times a second,
        // and those keys must never toggle a setting or confirm an End.
        bool pointerArmed;

        sealed class Btn { public Rectangle R; public Action A; public string Label; }

        public PanelForm(IPanelHost app)
        {
            this.app = app;
            Text = "SAIOPT";
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            StartPosition = FormStartPosition.Manual;
            ClientSize = new Size(W, H);
            KeyPreview = true;
            BackColor = Pal.BG;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint, true);
            centered.Alignment = StringAlignment.Center;
            centered.LineAlignment = StringAlignment.Center;
            repaint.Interval = 1000;
            repaint.Tick += delegate { if (Visible) Invalidate(); };
            repaint.Start();
        }

        public void ShowNearTray()
        {
            pointerArmed = false;
            focus = -1;
            Rectangle wa = Screen.FromPoint(Cursor.Position).WorkingArea;
            Location = new Point(wa.Right - W - 12, wa.Bottom - H - 12);
            Show();
            Activate();
            Invalidate();
        }

        protected override void OnDeactivate(EventArgs e)
        {
            base.OnDeactivate(e);
            if (!Asking) { HiddenAtTick = Environment.TickCount; Hide(); }
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; Hide(); return; }
            base.OnFormClosing(e);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                repaint.Dispose();
                foreach (Font f in fonts.Values) f.Dispose();
                foreach (Font f in bolds.Values) f.Dispose();
                centered.Dispose();
            }
            base.Dispose(disposing);
        }

        static Font PixelFont(int pt, bool bold)
        {
            IntPtr hf = Native.CreateFont(-(pt * 96 / 72), 0, 0, 0, bold ? 700 : 400, 0, 0, 0, 1, 0, 0, Native.NONANTIALIASED_QUALITY, 0, "Verdana");
            try { using (Font wrapped = Font.FromHfont(hf)) return (Font)wrapped.Clone(); }
            finally { if (hf != IntPtr.Zero) Native.DeleteObject(hf); }
        }

        Font F(int pt, bool bold)
        {
            Dictionary<int, Font> cache = bold ? bolds : fonts;
            Font f;
            if (!cache.TryGetValue(pt, out f)) { f = PixelFont(pt, bold); cache[pt] = f; }
            return f;
        }

        void Txt(Graphics g, string s, int x, int y, Color c, int pt, bool bold)
        {
            using (SolidBrush b = new SolidBrush(c)) g.DrawString(s ?? "", F(pt, bold), b, (float)x, (float)y);
        }

        void Txt(Graphics g, string s, int x, int y, Color c, int pt) { Txt(g, s, x, y, c, pt, false); }

        void TxtRight(Graphics g, string s, int right, int y, Color c, int pt, bool bold)
        {
            Txt(g, s, right - TextW(g, s, pt, bold), y, c, pt, bold);
        }

        int TextW(Graphics g, string s, int pt, bool bold)
        {
            return (int)Math.Ceiling(g.MeasureString(s ?? "", F(pt, bold)).Width);
        }

        string Fit(Graphics g, string s, int pt, int maxW)
        {
            if (s == null) return "";
            if (TextW(g, s, pt, false) <= maxW) return s;
            for (int n = s.Length - 1; n > 0; n--)
            {
                string t = s.Substring(0, n).TrimEnd() + "...";
                if (TextW(g, t, pt, false) <= maxW) return t;
            }
            return "";
        }

        void Bevel(Graphics g, Rectangle r, bool raised)
        {
            Color hi = raised ? Pal.BEVEL : Pal.BDARK;
            Color lo = raised ? Pal.BDARK : Pal.BEVEL;
            using (Pen p1 = new Pen(hi)) g.DrawRectangle(p1, r.X, r.Y, r.Width - 1, r.Height - 1);
            using (Pen p2 = new Pen(lo)) g.DrawRectangle(p2, r.X + 1, r.Y + 1, r.Width - 3, r.Height - 3);
        }

        void Button(Graphics g, Rectangle r, string label, bool pressed, Action a)
        {
            using (SolidBrush bg = new SolidBrush(pressed ? Pal.COMPARE : Pal.RAISED))
                g.FillRectangle(bg, r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4);
            Bevel(g, r, !pressed);
            using (SolidBrush br = new SolidBrush(pressed ? Pal.LINK : Pal.TEXT))
                g.DrawString(label, F(10, false), br, new RectangleF(r.X + 2, r.Y + 1, r.Width - 4, r.Height - 2), centered);
            Btn b = new Btn();
            b.R = r;
            b.A = a;
            b.Label = label;
            buttons.Add(b);
        }

        void Section(Graphics g, int x, int y, int w, string title)
        {
            Txt(g, title, x, y, Pal.TEXT, 11, true);
            int tw = TextW(g, title, 11, true);
            using (Pen p = new Pen(Pal.BMUTED)) g.DrawLine(p, x + tw + 8, y + 8, x + w, y + 8);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.TextRenderingHint = TextRenderingHint.SingleBitPerPixelGridFit;
            g.SmoothingMode = SmoothingMode.None;
            g.InterpolationMode = InterpolationMode.NearestNeighbor;
            g.Clear(Pal.BG);
            buttons.Clear();
            Render(g, app.Engine.Current);
            if (focus >= 0 && focus < buttons.Count)
            {
                Rectangle r = buttons[focus].R;
                using (Pen p = new Pen(Pal.TEXT)) { p.DashStyle = DashStyle.Dot; g.DrawRectangle(p, r.X + 3, r.Y + 3, r.Width - 7, r.Height - 7); }
            }
        }

        public void Render(Graphics g, Snapshot s)
        {
            Bevel(g, new Rectangle(0, 0, W, H), true);
            using (SolidBrush strip = new SolidBrush(Pal.BG_SOFT)) g.FillRectangle(strip, 2, 2, W - 4, 24);
            Txt(g, "SAIOPT", 10, 6, Pal.TEXT, 12, true);
            Txt(g, "background optimizer", 10 + TextW(g, "SAIOPT", 12, true) + 8, 8, Pal.MUTED, 10);
            Button(g, new Rectangle(W - 28, 4, 22, 20), "x", false, delegate { Hide(); });

            int x = 12, w = W - 24, y = 34;

            // Status is a word, with the reason next to it.
            Txt(g, "Status", x, y + 2, Pal.TEXT2, 10);
            Color sc = s.Status == StatusWords.Attention ? Pal.DANGERTXT : s.Status == StatusWords.Calm ? Pal.LINK : s.Status == StatusWords.Paused ? Pal.TEXT2 : Pal.TEXT;
            Txt(g, s.Status, x + 52, y, sc, 12, true);
            int sw = TextW(g, s.Status, 12, true);
            Txt(g, Fit(g, s.StatusWhy, 10, w - 62 - sw), x + 62 + sw, y + 2, Pal.MUTED, 10);
            y += 20;
            string commit = s.CommitLimitGb > 0 ? "   Commit " + Math.Round(s.CommitUsedGb).ToString(CultureInfo.InvariantCulture) + " of "
                + Math.Round(s.CommitLimitGb).ToString(CultureInfo.InvariantCulture) + " GB" : "";
            Txt(g, "CPU " + s.CpuPct + "%   Memory " + s.MemPct + "%" + commit + "   " + s.Processes + " processes", x, y, Pal.TEXT2, 10);
            y += 15;
            string shellLine;
            if (s.ShellWord == ShellWords.Unknown) shellLine = "Taskbar: measuring...";
            else if (s.ShellLastMs < 0 || s.ShellWord == ShellWords.Stalled) shellLine = "Taskbar: no answer within 2 s - " + s.ShellWord;
            else shellLine = "Taskbar answers in " + s.ShellLastMs + " ms (median " + Math.Max(0, s.ShellMedianMs) + " ms) - " + s.ShellWord;
            Txt(g, shellLine, x, y, s.ShellWord == ShellWords.Responsive ? Pal.TEXT2 : s.ShellWord == ShellWords.Unknown ? Pal.MUTED : Pal.DANGERTXT, 10);
            y += 22;

            // EASE
            Section(g, x, y, w - 128, "EASE");
            Button(g, new Rectangle(x + w - 120, y - 3, 120, 20), (s.EaseOn ? "[X] " : "[ ] ") + "Auto-ease", s.EaseOn,
                delegate { app.Engine.SetEase(!s.EaseOn); });
            y += 22;
            if (s.Eased.Count == 0)
            {
                string idle = s.Paused ? "Paused until " + s.PausedUntil.ToString("HH:mm", CultureInfo.InvariantCulture) + " - nothing is eased."
                    : s.EaseOn ? "Nothing eased - background work runs at normal priority."
                    : "Auto-ease is off - SAIOPT only watches.";
                Txt(g, idle, x, y, Pal.MUTED, 10);
                y += 15;
            }
            else
            {
                int shown = 0;
                foreach (EasedRow r in s.Eased)
                {
                    if (shown == 4) { Txt(g, "+" + (s.Eased.Count - 4) + " more", x, y, Pal.MUTED, 10); y += 15; break; }
                    string img = r.Image;
                    int prioX = x + 108;
                    int coreX = prioX + TextW(g, "BelowNormal", 10, false) + 4;
                    Txt(g, Fit(g, img, 10, prioX - x - 8), x, y + 1, Pal.TEXT, 10);
                    Txt(g, r.Priority, prioX, y + 1, Pal.TEXT2, 10);
                    Txt(g, Words.CoreShare(r.Core), coreX, y + 1, Pal.TEXT2, 10);
                    TxtRight(g, r.Since.ToString("HH:mm", CultureInfo.InvariantCulture), x + w - 78, y + 1, Pal.MUTED, 10, false);
                    Button(g, new Rectangle(x + w - 72, y - 1, 72, 17), "Keep fast", false, delegate { app.Engine.KeepFast(img); });
                    y += 17;
                    shown++;
                }
            }
            y += 8;

            // WATCH
            Section(g, x, y, w, "WATCH");
            y += 22;
            int told = 0;
            foreach (LossIncident li in s.Loss)
            {
                if (told == 2) break;
                told++;
                bool wasSeen = s.Seen.Contains(li.Subject);
                bool red = li.DataAtRisk && !wasSeen;
                string verdict = (li.DataAtRisk ? "WRITES LOST" : "dropped") + " at " + li.First.ToString("HH:mm", CultureInfo.InvariantCulture);
                int btnW = li.DataAtRisk ? 50 : 0;
                int vw = TextW(g, verdict, 10, red);
                Txt(g, Fit(g, li.Where(), 10, w - vw - btnW - 18), x, y, red ? Pal.DANGERTXT : Pal.TEXT, 10, true);
                TxtRight(g, verdict, x + w - (btnW > 0 ? btnW + 6 : 0), y, red ? Pal.DANGERTXT : Pal.TEXT2, 10, red);
                if (li.DataAtRisk)
                {
                    string subject = li.Subject;
                    Button(g, new Rectangle(x + w - btnW, y - 2, btnW, 17), "Seen", wasSeen,
                        delegate { if (!wasSeen) app.Engine.MarkSeen(subject); });
                }
                y += 15;
                Txt(g, Fit(g, li.What(), 10, w - 10), x + 10, y, Pal.TEXT2, 10);
                y += 15;
                if (li.DataAtRisk)
                {
                    Txt(g, Fit(g, "Run chkdsk /f when it is back; check files written then.", 10, w - 10), x + 10, y, Pal.MUTED, 10);
                    y += 15;
                }
            }
            foreach (StorageSummary st in s.Storage)
            {
                if (told == 3) break;
                told++;
                string head = st.Who();
                AwakeRow aw;
                s.Awake.TryGetValue(st.RaidPort, out aw);
                bool holding = aw != null && aw.On && aw.ResetsSince == 0 && aw.Failure == null;
                string state = holding ? "KEPT AWAKE" : st.Active ? "RESETTING" : "reset earlier";
                bool loud = !holding && st.Active;
                int btn = aw != null && !st.Disk.Gone ? TextW(g, "[X] Keep awake", 10, false) + 12 : 0;
                int stw = TextW(g, state, 10, loud);
                Txt(g, Fit(g, head, 10, w - stw - btn - 18), x, y, Pal.TEXT, 10, true);
                TxtRight(g, state, x + w - (btn > 0 ? btn + 6 : 0), y, holding ? Pal.LINK : loud ? Pal.DANGERTXT : Pal.TEXT2, 10, loud);
                if (btn > 0)
                {
                    string ident = aw.Identity;
                    bool isOn = !aw.Declined;
                    Button(g, new Rectangle(x + w - btn, y - 2, btn, 17), (isOn ? "[X] " : "[ ] ") + "Keep awake", isOn,
                        delegate { app.Engine.SetKeepAwake(ident, !isOn); });
                }
                y += 15;
                string cad = st.Cadence != null && st.Cadence.Periodic && st.Cadence.MedianSeconds > 0
                    ? ", every " + Words.Duration((long)(st.Cadence.MedianSeconds * 1000)) : "";
                Txt(g, Fit(g, st.LastHour + " in the last hour, " + st.Last24h + " in 24 h" + cad + ", last "
                    + st.LastTime.ToString("HH:mm", CultureInfo.InvariantCulture), 10, w), x + 10, y, Pal.TEXT2, 10);
                y += 15;
                if (aw != null && aw.On)
                {
                    string how = aw.Failure != null ? "keep-awake read failing: " + aw.Failure
                        : "4 KiB read every " + app.Engine.Cfg.KeepAwakeSec + " s: " + aw.ResetsSince + " resets since "
                          + aw.Since.ToString("HH:mm", CultureInfo.InvariantCulture);
                    Txt(g, Fit(g, how, 10, w - 10), x + 10, y, aw.Failure != null || aw.ResetsSince > 0 ? Pal.DANGERTXT : Pal.MUTED, 10);
                    y += 15;
                }
                else if (st.Active)
                {
                    string fix = st.Disk != null && st.Disk.Bus == "USB"
                        ? "Each reset freezes Explorer and Start. Rear USB port, no hub - or a new enclosure."
                        : "Each reset freezes Explorer and Start. Check the cable and the drive.";
                    Txt(g, Fit(g, fix, 10, w - 10), x + 10, y, Pal.MUTED, 10);
                    y += 15;
                }
            }
            if (told == 0) { Txt(g, "Disks: no storage resets in the last 24 h.", x, y, Pal.TEXT2, 10); y += 15; }
            string ex = "Explorer: GDI " + s.ExplorerGdi + ", USER " + s.ExplorerUser
                + (s.ExplorerRestarts > 0 ? ", restarted " + s.ExplorerRestarts + "x since SAIOPT started" : "");
            Txt(g, ex, x, y, s.ExplorerLeak ? Pal.DANGERTXT : Pal.TEXT2, 10);
            y += 23;

            // SWEEP
            Section(g, x, y, w, "SWEEP");
            y += 22;
            if (s.Stale.Count == 0) { Txt(g, "Nothing stale.", x, y, Pal.MUTED, 10); y += 15; }
            else
            {
                int shown = 0;
                foreach (StaleGroup grp in s.Stale)
                {
                    if (shown == 3) { Txt(g, "+" + (s.Stale.Count - 3) + " more kinds", x, y, Pal.MUTED, 10); y += 15; break; }
                    StaleGroup gcopy = grp;
                    string line = grp.Count + "x " + grp.Image + " - " + grp.Reason + " - " + Words.Bytes(grp.Bytes);
                    Txt(g, Fit(g, line, 10, w - 136), x, y + 2, Pal.TEXT, 10);
                    Button(g, new Rectangle(x + w - 128, y, 50, 18), "End", false, delegate { app.ConfirmEnd(gcopy); });
                    Button(g, new Rectangle(x + w - 74, y, 74, 18), gcopy.Armed ? "[X] Auto" : "[ ] Auto", gcopy.Armed,
                        delegate { app.ToggleArm(gcopy); });
                    y += 20;
                    shown++;
                }
            }
            y += 8;

            // ACTIVITY fills what is left above the bar.
            int barY = H - 34;
            Section(g, x, y, w, "ACTIVITY");
            y += 22;
            if (s.Activity.Count == 0) Txt(g, "Nothing yet. Easing starts when the machine is busy.", x, y, Pal.MUTED, 10);
            foreach (string line in s.Activity)
            {
                if (y + 15 > barY - 4) break;
                Txt(g, Fit(g, line, 10, w), x, y, Pal.TEXT2, 10);
                y += 15;
            }

            using (Pen p = new Pen(Pal.BMUTED)) g.DrawLine(p, 2, barY - 6, W - 3, barY - 6);
            bool auto = Autostart.Enabled();
            int bx = x;
            Button(g, new Rectangle(bx, barY, 188, 24), (auto ? "[X] " : "[ ] ") + "Start with Windows", auto, delegate { app.ToggleAutostart(); });
            bx += 194;
            if (s.Paused) Button(g, new Rectangle(bx, barY, 86, 24), "Resume", true, delegate { app.Engine.Resume(); });
            else Button(g, new Rectangle(bx, barY, 86, 24), "Pause 1 h", false, delegate { app.Engine.Pause(TimeSpan.FromHours(1)); });
            bx += 92;
            Button(g, new Rectangle(bx, barY, 74, 24), "Open log", false, delegate { app.OpenLog(); });
            bx += 80;
            Button(g, new Rectangle(bx, barY, W - 12 - bx, 24), "Exit", false, delegate { app.ExitApp(); });
        }

        // Self-test hooks: drive the real key and mouse paths without a window
        // on screen (SelfTest.PanelKeyGuard).
        internal bool KeyForTest(Keys k) { Message m = new Message(); return ProcessCmdKey(ref m, k); }
        internal void ClickForTest(Point p) { OnMouseDown(new MouseEventArgs(MouseButtons.Left, 1, p.X, p.Y, 0)); }
        internal void ShownForTest() { pointerArmed = false; focus = -1; }
        internal int IndexForTest(string label)
        {
            for (int i = 0; i < buttons.Count; i++) if (buttons[i].Label == label) return i;
            return -1;
        }

        void Press(int index) { Press(index, "keyboard"); }

        // Every press is logged with how it arrived, so a changed setting is
        // always attributable ("mouse" or "keyboard").
        void Press(int index, string via)
        {
            if (index < 0 || index >= buttons.Count || buttons[index].A == null) return;
            int now = Environment.TickCount;
            if (unchecked(now - lastActivate) < 250) return;
            lastActivate = now;
            Log.Line("PANEL " + via + ": " + (buttons[index].Label ?? "?"));
            buttons[index].A();
            Invalidate();
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (e.Button != MouseButtons.Left) return;
            pointerArmed = true;
            for (int i = 0; i < buttons.Count; i++)
                if (buttons[i].R.Contains(e.Location)) { focus = i; Press(i, "mouse"); return; }
            if (e.Y < 26)
            {
                Native.ReleaseCapture();
                Native.SendMessage(Handle, 0xA1, new IntPtr(2), IntPtr.Zero);
            }
        }

        protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
        {
            if (keyData == Keys.Escape) { Hide(); return true; }
            bool nav = keyData == Keys.Tab || keyData == (Keys.Tab | Keys.Shift) || keyData == Keys.Enter || keyData == Keys.Space;
            if (nav && !pointerArmed) return true;      // swallowed: not yours until you click in here
            if (keyData == Keys.Tab || keyData == (Keys.Tab | Keys.Shift))
            {
                if (buttons.Count > 0)
                {
                    int step = keyData == Keys.Tab ? 1 : -1;
                    focus = focus < 0 ? (step > 0 ? 0 : buttons.Count - 1) : (focus + step + buttons.Count) % buttons.Count;
                    Invalidate();
                }
                return true;
            }
            if ((keyData == Keys.Enter || keyData == Keys.Space) && focus >= 0) { Press(focus); return true; }
            return base.ProcessCmdKey(ref msg, keyData);
        }
    }

    sealed class SaioptApp : ApplicationContext, IPanelHost
    {
        public Engine Engine { get; private set; }
        readonly NotifyIcon tray;
        readonly Icon iconCalm, iconAttention, iconQuiet;
        readonly Control marshal = new Control();
        readonly System.Windows.Forms.Timer ui = new System.Windows.Forms.Timer();
        readonly Thread engineThread;
        readonly RegisteredWaitHandle showWait, stopWait;
        PanelForm panel;
        bool exiting;

        public SaioptApp(Engine engine, EventWaitHandle showEvent, EventWaitHandle stopEvent, bool openPanel)
        {
            Engine = engine;
            IntPtr forceHandle = marshal.Handle;
            iconCalm = MakeIcon(Pal.BHL);
            iconAttention = MakeIcon(Pal.DANGERTXT);
            iconQuiet = MakeIcon(Pal.MUTED);

            if (engine.Cfg.Tray)
            {
                tray = new NotifyIcon();
                tray.Icon = iconCalm;
                tray.Text = "SAIOPT: starting";
                ContextMenu menu = new ContextMenu();
                menu.MenuItems.Add("Open SAIOPT", delegate { ShowPanel(); });
                menu.MenuItems.Add("Pause easing 1 h", delegate { Engine.Pause(TimeSpan.FromHours(1)); });
                menu.MenuItems.Add("Resume easing", delegate { Engine.Resume(); });
                menu.MenuItems.Add("-");
                menu.MenuItems.Add("Exit (restores everything)", delegate { ExitApp(); });
                tray.ContextMenu = menu;
                tray.MouseClick += delegate(object s, MouseEventArgs e)
                {
                    if (e.Button != MouseButtons.Left) return;
                    if (panel != null && panel.Visible) { panel.Hide(); return; }
                    // The click that deactivated (and so hid) the panel must not reopen it.
                    if (panel != null && unchecked(Environment.TickCount - panel.HiddenAtTick) < 300) return;
                    ShowPanel();
                };
                tray.Visible = true;
            }

            engine.Tell += delegate(string title, string text)
            {
                OnUi(delegate
                {
                    if (tray != null) tray.ShowBalloonTip(10000, title, text.Length > 250 ? text.Substring(0, 249) + "~" : text, ToolTipIcon.Warning);
                });
            };

            showWait = ThreadPool.RegisterWaitForSingleObject(showEvent, delegate { OnUi(ShowPanel); }, null, -1, false);
            stopWait = ThreadPool.RegisterWaitForSingleObject(stopEvent, delegate { OnUi(ExitApp); }, null, -1, true);
            SystemEvents.SessionEnding += OnSessionEnding;

            engineThread = new Thread(Engine.Run);
            engineThread.IsBackground = true;
            engineThread.Name = "SAIOPT engine";
            engineThread.Start();

            ui.Interval = 1000;
            ui.Tick += delegate { RefreshTray(); };
            ui.Start();
            if (openPanel) ShowPanel();
        }

        void OnUi(Action a)
        {
            try { if (!marshal.IsDisposed) marshal.BeginInvoke(a); }
            catch { }
        }

        static Icon MakeIcon(Color bars)
        {
            using (Bitmap bmp = new Bitmap(16, 16, PixelFormat.Format32bppArgb))
            {
                using (Graphics g = Graphics.FromImage(bmp))
                {
                    g.Clear(Color.Transparent);
                    int[] heights = { 6, 10, 14 };
                    for (int i = 0; i < 3; i++)
                    {
                        int x = 1 + i * 5, h = heights[i];
                        using (SolidBrush dark = new SolidBrush(Pal.BDARK)) g.FillRectangle(dark, x, 15 - h, 4, h + 1);
                        using (SolidBrush fill = new SolidBrush(bars)) g.FillRectangle(fill, x + 1, 16 - h, 2, h - 1);
                    }
                }
                IntPtr h2 = bmp.GetHicon();
                try { using (Icon tmp = Icon.FromHandle(h2)) return (Icon)tmp.Clone(); }
                finally { Native.DestroyIcon(h2); }
            }
        }

        void RefreshTray()
        {
            if (tray == null) return;
            Snapshot s = Engine.Current;
            Icon want = s.Status == StatusWords.Attention ? iconAttention : (s.Status == StatusWords.Paused || !s.EaseOn) ? iconQuiet : iconCalm;
            if (tray.Icon != want) tray.Icon = want;
            string text = "SAIOPT: " + s.Status;
            if (s.Eased.Count > 0) text += ", " + s.Eased.Count + " eased";
            if (s.ShellWord != ShellWords.Unknown) text += s.ShellLastMs >= 0 ? " - taskbar " + s.ShellLastMs + " ms" : " - taskbar silent";
            text = Words.Tooltip(text);
            if (tray.Text != text) tray.Text = text;
        }

        public void ShowPanel()
        {
            if (exiting) return;
            if (panel == null || panel.IsDisposed) panel = new PanelForm(this);
            panel.ShowNearTray();
        }

        public void ToggleAutostart()
        {
            bool on = !Autostart.Enabled();
            Log.Line("AUTOSTART " + (on ? "on" : "off"));
            if (!Autostart.Set(on)) Ask("Could not change the Run entry in HKCU.", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }

        public void OpenLog()
        {
            try
            {
                if (File.Exists(Paths.LogPath)) Process.Start("notepad.exe", "\"" + Paths.LogPath + "\"");
                else Process.Start("explorer.exe", "\"" + Paths.StateDir + "\"");
            }
            catch { }
        }

        // Every question that can end a process or arm an automatic action
        // defaults to Cancel: a stray Enter or Space answers "no".
        DialogResult Ask(string text, MessageBoxButtons buttons, MessageBoxIcon icon)
        {
            if (panel != null) panel.Asking = true;
            MessageBoxDefaultButton safe = buttons == MessageBoxButtons.OK ? MessageBoxDefaultButton.Button1 : MessageBoxDefaultButton.Button2;
            try { return MessageBox.Show(panel != null && panel.Visible ? (IWin32Window)panel : null, text, "SAIOPT", buttons, icon, safe); }
            finally { if (panel != null) { panel.Asking = false; if (panel.Visible) panel.Activate(); } }
        }

        // Nothing is ended on one click without saying exactly what, and why.
        public void ConfirmEnd(StaleGroup g)
        {
            StringBuilder sb = new StringBuilder();
            sb.AppendLine("End " + g.Count + " stale " + g.Image + " (" + Words.Bytes(g.Bytes) + ")?");
            sb.AppendLine();
            sb.AppendLine("Why: " + g.Reason + ", no window.");
            int shown = 0;
            foreach (ProcKey k in g.Keys)
            {
                if (shown++ == 8) { sb.AppendLine("  ..."); break; }
                sb.AppendLine("  PID " + k.Pid.ToString(CultureInfo.InvariantCulture));
            }
            sb.AppendLine();
            sb.AppendLine("Each one is checked again right before it is ended; one that woke up is left alone.");
            if (Ask(sb.ToString(), MessageBoxButtons.OKCancel, MessageBoxIcon.Question) == DialogResult.OK)
                Engine.EndStale(new List<ProcKey>(g.Keys), "you asked");
        }

        public void ToggleArm(StaleGroup g)
        {
            if (g.Armed) { Engine.ArmSweep(g.Image, false); return; }
            string text = "End stale " + g.Image + " automatically from now on?\r\n\r\n"
                + "Only orphans (parent gone) with no window qualify, and only after SAIOPT itself has watched them "
                + "stay frozen or idle for the full threshold. Undo here or in saiopt.ini (sweep_auto).";
            if (Ask(text, MessageBoxButtons.OKCancel, MessageBoxIcon.Question) == DialogResult.OK)
                Engine.ArmSweep(g.Image, true);
        }

        void OnSessionEnding(object sender, SessionEndingEventArgs e)
        {
            Engine.RequestStop();
            engineThread.Join(5000);
        }

        public void ExitApp()
        {
            if (exiting) return;
            exiting = true;
            ui.Stop();
            Engine.RequestStop();
            engineThread.Join(8000);
            SystemEvents.SessionEnding -= OnSessionEnding;
            showWait.Unregister(null);
            stopWait.Unregister(null);
            if (tray != null) { tray.Visible = false; tray.Dispose(); }
            if (panel != null) panel.Dispose();
            iconCalm.Dispose(); iconAttention.Dispose(); iconQuiet.Dispose();
            ExitThread();
        }
    }

    // ------------------------------------------------------------- program

    static class Program
    {
        public const string MutexName = "Local\\SaitulsSaiopt";
        public const string StopEventName = "Local\\SaitulsSaioptStop";
        public const string ShowEventName = "Local\\SaitulsSaioptShow";

        public static string DefaultConfigText()
        {
            StringBuilder sb = new StringBuilder();
            sb.AppendLine("; SAIOPT settings. Delete this file to restore defaults; edits apply within 5 s.");
            sb.AppendLine("; Out-of-range or unreadable values fall back to the default shown here.");
            sb.AppendLine("[saiopt]");
            sb.AppendLine("ease=1                ; 1 = ease background CPU hogs while the machine is busy");
            sb.AppendLine("busy_pct=60           ; 20..100  machine CPU % at which easing may start");
            sb.AppendLine("calm_pct=35           ; 5..95    below this for calm_samples, everything is restored");
            sb.AppendLine("hog_core_pct=50       ; 10..6400 share of ONE core that makes a background process a hog");
            sb.AppendLine("hog_samples=3         ; 1..60    consecutive hot samples before easing");
            sb.AppendLine("calm_samples=10       ; 1..300   quiet samples before a restore");
            sb.AppendLine("sample_ms=2000        ; 500..10000");
            sb.AppendLine("ease_to=below_normal  ; below_normal | idle");
            sb.AppendLine("max_eased=16          ; 1..64");
            sb.AppendLine("focus_grace_ms=15000  ; 0..600000  an app you just left keeps full speed this long");
            sb.AppendLine("protect_audio=1       ; 1 = a process you can hear is never eased");
            sb.AppendLine("never_ease=           ; extra exe names, comma separated");
            sb.AppendLine("watch_storage=1       ; storage resets (Event 129) with the real drive letter");
            sb.AppendLine("keep_awake=auto       ; auto | off  keep a USB disk with periodic idle resets busy with a tiny read");
            sb.AppendLine("keep_awake_sec=45     ; 15..240"); 
            sb.AppendLine("watch_shell=1         ; taskbar round-trip every 2 s");
            sb.AppendLine("shell_slow_ms=250     ; 20..5000");
            sb.AppendLine("stale_suspended_min=60 ; 5..10080  orphan frozen this long = stale");
            sb.AppendLine("stale_idle_hours=12   ; 1..720    orphan console tool without CPU this long = stale");
            sb.AppendLine("sweep_auto=           ; exe names ended automatically when stale (empty = always ask)");
            sb.AppendLine("tray=1");
            sb.AppendLine("balloons=1");
            sb.AppendLine("remind_min=60         ; 5..1440  the same news is told once per this long");
            sb.AppendLine("debug=0");
            return sb.ToString();
        }

        static OptConfig LoadConfig(List<string> warnings)
        {
            try
            {
                if (!File.Exists(Paths.ConfigPath))
                {
                    try { File.WriteAllText(Paths.ConfigPath, DefaultConfigText(), new UTF8Encoding(false)); }
                    catch { }
                    return OptConfig.Defaults();
                }
                return OptConfig.Parse(OptConfig.ParseIni(File.ReadAllText(Paths.ConfigPath)), warnings);
            }
            catch (Exception ex)
            {
                warnings.Add("config unreadable (" + ex.GetType().Name + "), using defaults");
                return OptConfig.Defaults();
            }
        }

        static void OpenConsole()
        {
            bool haveStdout = false;
            try
            {
                IntPtr h = Native.GetStdHandle(-11);
                haveStdout = h != IntPtr.Zero && h != new IntPtr(-1) && Native.GetFileType(h) != 0;
            }
            catch { }
            if (!haveStdout) { try { Native.AttachConsole(-1); } catch { } }
            try
            {
                StreamWriter w = new StreamWriter(Console.OpenStandardOutput());
                w.AutoFlush = true;
                Console.SetOut(w);
            }
            catch { }
        }

        static bool Running()
        {
            Mutex m = null;
            try { m = Mutex.OpenExisting(MutexName); return true; }
            catch (WaitHandleCannotBeOpenedException) { return false; }
            catch (UnauthorizedAccessException) { return true; }
            catch { return false; }
            finally { if (m != null) m.Close(); }
        }

        [STAThread]
        static int Main(string[] args)
        {
            string mode = "run";
            string shotPath = null;
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i].Trim().ToLowerInvariant().TrimStart('-', '/');
                if (a == "panel") mode = "panel";
                else if (a == "status") mode = "status";
                else if (a == "self-test" || a == "selftest") mode = "selftest";
                else if (a == "stop") mode = "stop";
                else if (a == "restore") mode = "restore";
                else if (a == "shot" && i + 1 < args.Length) { mode = "shot"; shotPath = args[++i]; }
                else if (a == "help" || a == "h" || a == "?") mode = "help";
            }
            if (mode != "run" && mode != "panel") OpenConsole();

            switch (mode)
            {
                case "help": return Help();
                case "status": return Status();
                case "stop": return Stop();
                case "restore": return RestoreCli();
                case "selftest": return SelfTest.Run();
                case "shot": return Shot(shotPath);
            }

            using (EventWaitHandle show = new EventWaitHandle(false, EventResetMode.AutoReset, ShowEventName))
            {
                bool createdNew;
                using (Mutex single = new Mutex(true, MutexName, out createdNew))
                {
                    if (!createdNew)
                    {
                        // Already running: a second launch (the SAITULS button,
                        // a double-click) just brings the panel up.
                        if (mode == "panel") show.Set();
                        return 0;
                    }
                    using (EventWaitHandle stop = new EventWaitHandle(false, EventResetMode.ManualReset, StopEventName))
                    {
                        stop.Reset();
                        Application.EnableVisualStyles();
                        Application.SetCompatibleTextRenderingDefault(false);
                        List<string> warnings = new List<string>();
                        OptConfig cfg = LoadConfig(warnings);
                        Engine engine = new Engine(cfg, false);
                        engine.Warnings.AddRange(warnings);
                        Application.Run(new SaioptApp(engine, show, stop, mode == "panel"));
                        return 0;
                    }
                }
            }
        }

        static int Help()
        {
            Console.WriteLine("SAIOPT -- background optimizer for SAITULS.");
            Console.WriteLine("  SAIOPT.exe              run quietly in the tray");
            Console.WriteLine("  SAIOPT.exe --panel      run (or reach the running copy) and open the panel");
            Console.WriteLine("  SAIOPT.exe --status     report state");
            Console.WriteLine("  SAIOPT.exe --self-test  verify sampling, priority round trip, audio, disks, shell probe");
            Console.WriteLine("  SAIOPT.exe --stop       stop the running copy (it restores every eased process)");
            Console.WriteLine("  SAIOPT.exe --restore    restore processes a crashed copy left eased (only when not running)");
            Console.WriteLine("  SAIOPT.exe --shot FILE  render the panel from live, read-only data to a PNG");
            Console.WriteLine("Config: " + Paths.ConfigPath);
            return 0;
        }

        static int Status()
        {
            Console.WriteLine("running=" + (Running() ? "true" : "false"));
            Console.WriteLine("autostart=" + (Autostart.Enabled() ? "on" : "off"));
            try
            {
                if (File.Exists(Paths.StatusPath)) Console.Write(File.ReadAllText(Paths.StatusPath));
                else Console.WriteLine("status_file=none");
            }
            catch (Exception ex) { Console.WriteLine("status_file_error=" + ex.Message); }
            List<EasedEntry> eased = File.Exists(Paths.EasedPath) ? Engine.ReadEased() : new List<EasedEntry>();
            foreach (EasedEntry e in eased)
                Console.WriteLine("eased_process=" + e.Image + " pid=" + e.Key.Pid + " " + Priorities.Name(e.EasedPriority));
            return 0;
        }

        static int Stop()
        {
            if (!Running()) { Console.WriteLine("stopped=already"); return 0; }
            try
            {
                using (EventWaitHandle stop = EventWaitHandle.OpenExisting(StopEventName)) stop.Set();
                Console.WriteLine("stopped=signalled");
                return 0;
            }
            catch (Exception ex) { Console.WriteLine("stopped=error " + ex.GetType().Name); return 1; }
        }

        static int RestoreCli()
        {
            if (Running())
            {
                Console.WriteLine("SAIOPT is running; --stop restores everything it eased.");
                return 1;
            }
            List<EasedEntry> list = File.Exists(Paths.EasedPath) ? Engine.ReadEased() : new List<EasedEntry>();
            int ok = 0;
            foreach (EasedEntry e in list)
            {
                string err;
                bool done = Procs.SetPriority(e.Key, e.EasedPriority, e.OriginalPriority, out err);
                if (done) ok++;
                Console.WriteLine((done ? "RESTORED " : "SKIPPED  ") + e.Image + " pid=" + e.Key.Pid + (done ? "" : " (" + err + ")"));
            }
            try { if (File.Exists(Paths.EasedPath)) File.Delete(Paths.EasedPath); } catch { }
            Console.WriteLine("restored=" + ok + " of " + list.Count);
            return 0;
        }

        // Read-only engine: two real samples, no priority change, no sweep,
        // no state written -- then the panel painted to a PNG.
        static int Shot(string path)
        {
            List<string> warnings = new List<string>();
            OptConfig cfg = LoadConfig(warnings);
            Engine engine = new Engine(cfg, true);
            Thread t = new Thread(engine.Run);
            t.IsBackground = true;
            t.Start();
            Thread.Sleep(Math.Max(4500, cfg.SampleMs * 2 + 500));
            engine.RequestStop();
            t.Join(5000);
            using (Bitmap bmp = new Bitmap(PanelForm.W, PanelForm.H))
            using (Graphics g = Graphics.FromImage(bmp))
            {
                g.TextRenderingHint = TextRenderingHint.SingleBitPerPixelGridFit;
                g.SmoothingMode = SmoothingMode.None;
                g.Clear(Pal.BG);
                using (PanelForm p = new PanelForm(new ShotHost(engine)))
                    p.Render(g, engine.Current);
                bmp.Save(path, ImageFormat.Png);
            }
            Console.WriteLine("shot=" + path);
            return 0;
        }

        // Painting a shot only reads Engine; every action is inert.
        sealed class ShotHost : IPanelHost
        {
            public ShotHost(Engine e) { Engine = e; }
            public Engine Engine { get; private set; }
            public void ConfirmEnd(StaleGroup g) { }
            public void ToggleArm(StaleGroup g) { }
            public void ToggleAutostart() { }
            public void OpenLog() { }
            public void ExitApp() { }
        }
    }
}
