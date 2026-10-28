using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Diagnostics.Eventing.Reader;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

// SAIOPT --self-test: every Win32 fact the logic relies on, proven on the
// real machine. The only process it changes or ends is a suspended child it
// starts itself for exactly that purpose.

namespace Saituls.Saiopt
{
    static class SelfTest
    {
        static int failures;
        static int skipped;

        static void Check(string name, bool ok, string detail)
        {
            Console.WriteLine((ok ? "PASS  " : "FAIL  ") + name + (string.IsNullOrEmpty(detail) ? "" : "  " + detail));
            if (!ok) failures++;
        }

        static void Info(string text) { Console.WriteLine("INFO  " + text); }
        static void Skip(string text) { Console.WriteLine("SKIP  " + text); skipped++; }

        static RawProc Find(List<RawProc> raw, int pid)
        {
            return raw.Find(delegate(RawProc r) { return r.Pid == pid; });
        }

        public static int Run()
        {
            Check("64-bit process (x64 kernel structures)", IntPtr.Size == 8, "IntPtr.Size=" + IntPtr.Size);
            if (IntPtr.Size != 8) return 1;

            ProcessSampler sampler = new ProcessSampler();
            try
            {
                Sampling(sampler);
                CpuAccounting(sampler);
                SuspendedChildRoundTrip(sampler);
            }
            finally { sampler.Dispose(); }

            DesktopFacts();
            PanelKeyGuard();
            KeepAwakeRead();
            Audio();
            DisksAndEvents();
            Config();

            Console.WriteLine("SELFTEST failures=" + failures + " skipped=" + skipped);
            return failures == 0 ? 0 : 1;
        }

        static void Sampling(ProcessSampler sampler)
        {
            List<RawProc> raw = sampler.Snapshot();
            Process[] ps = Process.GetProcesses();
            Check("one syscall sees every process", Math.Abs(raw.Count - ps.Length) <= 12, "sampler=" + raw.Count + " Process=" + ps.Length);
            foreach (Process p in ps) p.Dispose();

            Process me = Process.GetCurrentProcess();
            RawProc mine = Find(raw, me.Id);
            Check("own process found", mine != null, "pid=" + me.Id);
            if (mine == null) return;
            Check("image name decoded", mine.Image == "saiopt.exe", mine.Image);
            Check("creation time is the identity FILETIME", mine.CreateTime == me.StartTime.ToFileTime(),
                mine.CreateTime + " vs " + me.StartTime.ToFileTime());
            Check("base priority read", mine.BasePriority == me.BasePriority, mine.BasePriority + " vs " + me.BasePriority);
            me.Refresh();
            Check("thread count read", Math.Abs(mine.Threads - me.Threads.Count) <= 3, mine.Threads + " vs " + me.Threads.Count);
            Check("private bytes read", mine.PrivateBytes > (1 << 20) && Math.Abs(mine.PrivateBytes - me.PrivateMemorySize64) < (64L << 20),
                Words.Bytes(mine.PrivateBytes) + " vs " + Words.Bytes(me.PrivateMemorySize64));
            Check("session read", mine.SessionId == me.SessionId, mine.SessionId + " vs " + me.SessionId);
            RawProc parent = Find(raw, mine.ParentPid);
            Check("parent resolved and older", parent != null && parent.CreateTime <= mine.CreateTime,
                parent == null ? "parent " + mine.ParentPid + " gone" : parent.Image);
            Check("own process is not suspended", !mine.Suspended, "");
            me.Dispose();
        }

        static void CpuAccounting(ProcessSampler sampler)
        {
            int pid = Process.GetCurrentProcess().Id;
            RawProc a = Find(sampler.Snapshot(), pid);
            Stopwatch sw = Stopwatch.StartNew();
            double x = 0;
            while (sw.ElapsedMilliseconds < 400) x += Math.Sqrt(x + 1);
            RawProc b = Find(sampler.Snapshot(), pid);
            double share = (b.CpuTicks - a.CpuTicks) / (double)sw.ElapsedMilliseconds / 100.0;
            // Units, not speed: a busy machine may steal half the burn, but a
            // wrong tick unit would read ~6 % or ~600 %.
            Check("CPU share of one core measured", share >= 30 && share <= 400, Math.Round(share) + "% (x=" + (x > 0 ? "ok" : "0") + ")");
        }

        static void SuspendedChildRoundTrip(ProcessSampler sampler)
        {
            Native.STARTUPINFO si = new Native.STARTUPINFO();
            si.cb = Marshal.SizeOf(typeof(Native.STARTUPINFO));
            Native.PROCESS_INFORMATION pi;
            StringBuilder cmd = new StringBuilder("\"" + Environment.SystemDirectory + "\\cmd.exe\" /c exit");
            if (!Native.CreateProcess(null, cmd, IntPtr.Zero, IntPtr.Zero, false, Native.CREATE_SUSPENDED | Native.CREATE_NO_WINDOW,
                IntPtr.Zero, null, ref si, out pi))
            {
                Skip("suspended child could not be started (" + Marshal.GetLastWin32Error() + ")");
                return;
            }
            try
            {
                RawProc child = Find(sampler.Snapshot(), pi.dwProcessId);
                Check("suspended child sampled", child != null, "pid=" + pi.dwProcessId);
                if (child == null) return;
                Check("every-thread-suspended detected", child.Suspended, "threads=" + child.Threads);
                Check("child starts at Normal", child.BasePriority == Priorities.Normal, Priorities.Name(child.BasePriority));
                ProcKey key = new ProcKey(child.Pid, child.CreateTime);
                string err;

                bool eased = Procs.SetPriority(key, Priorities.Normal, Priorities.BelowNormal, out err);
                RawProc after = Find(sampler.Snapshot(), pi.dwProcessId);
                Check("ease applies BelowNormal", eased && after != null && after.BasePriority == Priorities.BelowNormal,
                    eased ? Priorities.Name(after == null ? -1 : after.BasePriority) : err);

                bool again = Procs.SetPriority(key, Priorities.Normal, Priorities.BelowNormal, out err);
                Check("a process not at the expected class is refused", !again, err ?? "");

                bool back = Procs.SetPriority(key, Priorities.BelowNormal, Priorities.Normal, out err);
                after = Find(sampler.Snapshot(), pi.dwProcessId);
                Check("restore brings Normal back exactly", back && after != null && after.BasePriority == Priorities.Normal,
                    back ? Priorities.Name(after == null ? -1 : after.BasePriority) : err);

                ProcKey impostor = new ProcKey(key.Pid, key.CreateTime + 1);
                Check("priority change refuses a reused PID", !Procs.SetPriority(impostor, Priorities.Normal, Priorities.BelowNormal, out err), err ?? "");
                Check("end refuses a reused PID", !Procs.EndProcess(impostor, out err), err ?? "");
                Check("child survived both refusals", Find(sampler.Snapshot(), pi.dwProcessId) != null, "");

                bool ended = Procs.EndProcess(key, out err);
                Native.CloseHandle(pi.hThread);
                pi.hThread = IntPtr.Zero;
                Thread.Sleep(150);
                Check("end ends exactly the verified process", ended && Find(sampler.Snapshot(), pi.dwProcessId) == null, ended ? "" : err);
            }
            finally
            {
                if (pi.hThread != IntPtr.Zero) Native.CloseHandle(pi.hThread);
                if (pi.hProcess != IntPtr.Zero)
                {
                    Native.TerminateProcess(pi.hProcess, 1);
                    Native.CloseHandle(pi.hProcess);
                }
            }
        }

        static void DesktopFacts()
        {
            HashSet<int> windowed = Desktop.WindowedPids();
            Check("visible top-level windows enumerated", windowed.Count >= 1, "processes with windows=" + windowed.Count);
            Info("foreground pid=" + Desktop.ForegroundPid());
            int ms = Desktop.ShellLatencyMs();
            if (Native.FindWindow("Shell_TrayWnd", null) == IntPtr.Zero) Skip("no taskbar in this session");
            else Check("taskbar answers the probe", ms >= 0, ms + " ms");
            int pid, gdi, user;
            if (Desktop.ExplorerStats(out pid, out gdi, out user))
                Info("explorer pid=" + pid + " GDI=" + gdi + " USER=" + user + (ExplorerLimits.Leaking(gdi, user) ? " NEAR LIMIT" : ""));
        }

        sealed class RecordingHost : IPanelHost
        {
            public int Presses;
            public RecordingHost(Engine e) { Engine = e; }
            public Engine Engine { get; private set; }
            public void ConfirmEnd(StaleGroup g) { Presses++; }
            public void ToggleArm(StaleGroup g) { Presses++; }
            public void ToggleAutostart() { Presses++; }
            public void OpenLog() { Presses++; }
            public void ExitApp() { Presses++; }
        }

        // The panel in memory, never shown: a burst of Tab/Space/Enter before
        // any click must press nothing; after a click inside, Enter works.
        static void PanelKeyGuard()
        {
            RecordingHost host = new RecordingHost(new Engine(OptConfig.Defaults(), true));
            Log.Enabled = true;
            using (PanelForm p = new PanelForm(host))
            using (System.Drawing.Bitmap bmp = new System.Drawing.Bitmap(PanelForm.W, PanelForm.H))
            using (System.Drawing.Graphics g = System.Drawing.Graphics.FromImage(bmp))
            {
                p.Render(g, host.Engine.Current);
                int log = p.IndexForTest("Open log");
                Check("panel buttons are located", log >= 0, "");
                System.Windows.Forms.Keys tab = System.Windows.Forms.Keys.Tab, enter = System.Windows.Forms.Keys.Enter, space = System.Windows.Forms.Keys.Space;

                // Exactly the keys that press "Open log" once the panel is
                // armed -- sent before any click. Spaced past the 250 ms press
                // debounce, so the debounce cannot be what stops them.
                p.ShownForTest();
                for (int i = 0; i <= log; i++) p.KeyForTest(tab);
                p.KeyForTest(enter);
                Thread.Sleep(300);
                p.KeyForTest(space);
                Thread.Sleep(300);
                Check("before any click, Tab-to-button + Enter/Space press nothing", host.Presses == 0, "presses=" + host.Presses);

                p.ClickForTest(new System.Drawing.Point(PanelForm.W / 2, 30));     // a click on text, not a button
                p.ShownForTest();                                                  // reset focus, keep the click out of it
                p.ClickForTest(new System.Drawing.Point(PanelForm.W / 2, 30));
                for (int i = 0; i <= log; i++) p.KeyForTest(tab);
                p.KeyForTest(enter);
                Thread.Sleep(300);
                Check("after a click inside, the same keys press the focused button", host.Presses == 1, "presses=" + host.Presses);

                p.ShownForTest();
                for (int i = 0; i <= log; i++) p.KeyForTest(tab);
                p.KeyForTest(enter);
                Check("showing the panel again disarms the keyboard", host.Presses == 1, "presses=" + host.Presses);
            }
        }

        // The keep-awake read on SAIOPT's own volume: finds a file, reads 4 KiB
        // uncached, changes nothing (size and write time are compared).
        static void KeepAwakeRead()
        {
            string letter = Path.GetPathRoot(AppDomain.CurrentDomain.BaseDirectory).TrimEnd('\\');
            string file = DiskTouch.PickFile(letter);
            Check("keep-awake finds a file to read on " + letter, file != null, file ?? "");
            if (file == null) return;
            FileInfo before = new FileInfo(file);
            long len = before.Length;
            DateTime written = before.LastWriteTimeUtc;
            string detail;
            Stopwatch sw = Stopwatch.StartNew();
            bool ok = DiskTouch.Touch(letter, out detail);
            Check("keep-awake read reaches the disk", ok, (ok ? "" : detail + " ") + sw.ElapsedMilliseconds + " ms");
            FileInfo after = new FileInfo(file);
            Check("keep-awake read changes nothing", after.Length == len && after.LastWriteTimeUtc == written, "");
        }

        static void Audio()
        {
            HashSet<int> pids = CoreAudio.AudiblePids();
            Info("processes making sound now: " + pids.Count);
            // The probe runs every couple of seconds for days: it must not leak.
            GC.Collect(); GC.WaitForPendingFinalizers(); GC.Collect();
            Process me = Process.GetCurrentProcess();
            int before = me.HandleCount;
            long privBefore = me.PrivateMemorySize64;
            for (int i = 0; i < 40; i++) CoreAudio.AudiblePids();
            GC.Collect(); GC.WaitForPendingFinalizers(); GC.Collect();
            me.Refresh();
            Check("audio probe does not leak handles", me.HandleCount - before < 40, (me.HandleCount - before) + " handles after 40 probes");
            Check("audio probe does not leak memory", me.PrivateMemorySize64 - privBefore < (8L << 20),
                Words.Bytes(Math.Max(0, me.PrivateMemorySize64 - privBefore)) + " after 40 probes");
            me.Dispose();
        }

        static void DisksAndEvents()
        {
            DiskInventory inv = Disks.Inventory();
            Check("storage adapters mapped to disks", inv.ByRaid.Count >= 1, "adapters=" + inv.ByRaid.Count);
            int lettered = 0;
            foreach (string l in inv.LettersByIndex.Values) if (l.Length > 0) lettered++;
            Check("drive letters resolved", lettered >= 1, lettered + " disks with letters");
            List<int> ports = new List<int>(inv.ByRaid.Keys);
            ports.Sort();
            foreach (int p in ports)
            {
                DiskFacts f = inv.ByRaid[p];
                Info("RaidPort" + p + " -> " + (f.Letters.Length > 0 ? f.Letters : "(no letter)") + "  " + Words.ShortModel(f.Model) + "  " + f.Bus);
            }
            try
            {
                int resets = 0, other = 0;
                Dictionary<int, int> byPort = new Dictionary<int, int>();
                LossWatch loss = new LossWatch();
                using (EventLogReader r = new EventLogReader(new EventLogQuery("System", PathType.LogName, Disks.Backfill)))
                {
                    for (EventRecord rec = r.ReadEvent(); rec != null; rec = r.ReadEvent())
                    {
                        using (rec)
                        {
                            DiskEvent e = DiskEvent.From(rec);
                            int port = Disks.RaidPortOf(e);
                            if (port >= 0)
                            {
                                resets++;
                                int c;
                                byPort.TryGetValue(port, out c);
                                byPort[port] = c + 1;
                                continue;
                            }
                            string kind, vols;
                            if (Disks.LossOf(e, null, out kind, out vols))
                            {
                                foreach (string v in Disks.SplitVolumes(vols)) loss.Add(e.Time, kind, v);
                            }
                            else other++;
                        }
                    }
                }
                Check("disk events readable", true, resets + " resets, " + other + " unrelated same-id events ignored (24 h)");
                foreach (KeyValuePair<int, int> kv in byPort)
                {
                    DiskFacts f;
                    Info("RaidPort" + kv.Key + ": " + kv.Value + " resets -> "
                        + (inv.ByRaid.TryGetValue(kv.Key, out f) ? (f.Letters.Length > 0 ? f.Letters + " " : "") + Words.ShortModel(f.Model) : "not present now"));
                }
                foreach (LossIncident li in loss.Incidents(DateTime.Now))
                    Info("incident " + li.First.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + " " + li.Where() + ": " + li.What()
                        + (li.DataAtRisk ? "  DATA AT RISK" : ""));
            }
            catch (Exception ex) { Check("disk events readable", false, ex.Message); }
        }

        static void Config()
        {
            List<string> w = new List<string>();
            Dictionary<string, string> bad = new Dictionary<string, string>();
            bad["busy_pct"] = "abc";
            bad["hog_core_pct"] = "5";
            OptConfig c = OptConfig.Parse(bad, w);
            Check("bad config falls back to safe defaults",
                c.BusyPct == OptConfig.DefaultBusyPct && c.HogCorePct == OptConfig.DefaultHogCorePct && w.Count == 2, w.Count + " warnings");
            Check("nothing is ended automatically out of the box", OptConfig.Defaults().SweepAuto.Count == 0, "");
        }
    }
}
