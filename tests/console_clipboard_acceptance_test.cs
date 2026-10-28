// T-301: observable Windows console clipboard acceptance suite.
// Replaces the T-300 oracle, which passed cases with unconditional `true`
// literals, round-tripped only the local clipboard, and checked mode flags
// instead of behavior. Every case here has a real input stimulus (SendInput or
// a real mouse drag delivered to an owned foreground console window), an
// independent observable result (clipboard contents, vendor-received bytes,
// console input mode, window properties, process liveness), and a meaningful
// failure condition. A case whose physical condition cannot be produced --
// usually foreground ownership -- reports BLOCKED, never PASS.
//
// Authority: SAIHANDOFF T-301; lineage-ded945ae8243480ab1bfee73baeb29c6.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

public static class ConsoleClipboardAcceptanceTest
{
    const string KnownText = "Eesti: \u00fc\u00f5\u00e4\u00f6 / \u0420\u0443\u0441: \u0442\u0435\u0441\u0442 / \u65e5\u672c: \u3053\u3093\u306b\u3061\u306f";
    // Payloads for the vendor paste acceptance: Unicode, single line and
    // multiline. No em dash: conhost best-fit folds it on some code pages.
    const string VtPastePayload = "VtPaste_\u65e5\u672c_\u00fc\u00f5\u00e4_\u0442\u0435\u0441\u0442";
    const string VtMultilinePayload = "VtMulti_\u65e5\u672c\r\nVtMulti2_\u00fc\u00f5\u00e4";
    const string AccountA = "antigravity:windows-user:1111111111111111";
    const string AccountB = "antigravity:windows-user:2222222222222222";
    static readonly string RunNonce = Guid.NewGuid().ToString("N").Substring(0, 8);
    static readonly string TitleA = "[AG1] Antigravity - Acceptance A :: " + RunNonce;
    static readonly string TitleB = "[AG2] Antigravity - Acceptance B :: " + RunNonce;

    // Console input mode bits (kernel32).
    const uint MouseInputFlag = 0x0010;
    const uint QuickEditFlag = 0x0040;
    const uint VtInputFlag = 0x0200;

    static int passes, fails, blocked;
    static readonly List<string> checks = new List<string>();

    // BLOCKED is a verdict, not a failure: the run could not produce the
    // physical condition (usually foreground ownership) and refuses to claim.
    static void Record(string verdict, string caseId, string name, string detail)
    {
        switch (verdict)
        {
            case "PASS": passes++; break;
            case "FAIL": fails++; break;
            default: blocked++; break;
        }
        Console.WriteLine(verdict + "  [" + caseId + "] " + name + " -- " + detail);
        checks.Add("[" + verdict + "] " + caseId + ": " + name + " -- " + detail);
    }

    static void Check(string caseId, string name, bool ok, string detail) { Record(ok ? "PASS" : "FAIL", caseId, name, detail); }
    static void Blocked(string caseId, string name, string detail) { Record("BLOCKED", caseId, name, detail); }

    [STAThread]
    public static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--mock-vendor") return RunMockVendor(args);
        // Identity mode: compare two executable images as PE images and report
        // the build metadata that is excluded from the verdict.
        if (args.Length > 0 && args[0] == "--identity")
        {
            string primary = args.Length > 1 ? args[1] : StagedEnginePath();
            string secondary = InstalledEnginePath();
            string role = "installed";
            if (secondary == null || SamePath(secondary, primary)) { secondary = StagedEnginePath(); role = "staged"; }
            if (primary == null || secondary == null)
            {
                Console.WriteLine("IDENTITY=INCOMPLETE: an image is missing");
                return 2;
            }
            PeInfo first = ReadPe(primary);
            PeInfo second = ReadPe(secondary);
            string reason = CompareImages(first, second);
            Console.WriteLine(DescribeImages(first, second, role));
            if (reason != null) { Console.WriteLine("IDENTITY=FAIL: " + reason); return 1; }
            Console.WriteLine("IDENTITY=PASS");
            return 0;
        }
        // A shell can hand this process an inherited "ignore Ctrl+C" state.
        // Left alone, every child console inherits it and no synthetic Ctrl+C
        // can ever exercise the interrupt path. Clear it once, here.
        SetConsoleCtrlHandler(IntPtr.Zero, false);
        if (args.Length > 0 && args[0] == "--solo-sigint") return RunSoloSigint();

        // Negative-control mode: one seeded fault per run; the suite succeeds
        // only when the oracle DETECTS the missing behavior. A deliberately
        // broken implementation must not be able to pass the full acceptance.
        if (args.Length > 0 && args[0] == "--negative")
        {
            string fault = args[1];
            string negEnginePath = args[2];
            if (string.IsNullOrEmpty(negEnginePath) || !File.Exists(negEnginePath))
            {
                Console.WriteLine("FAIL: engine executable path required");
                return 1;
            }
            int detected = RunNegative(fault, negEnginePath);
            Console.WriteLine("NEGATIVE-CONTROL " + fault + ": " + (detected == 0 ? "DETECTED" : "NOT DETECTED"));
            return detected;
        }

        string enginePath = args.Length > 0 ? args[0] : null;
        if (string.IsNullOrEmpty(enginePath) || !File.Exists(enginePath))
        {
            Console.WriteLine("FAIL: engine executable path required as argument 0");
            return 1;
        }
        string abiError = VerifyInputAbi();
        if (abiError != null)
        {
            Console.WriteLine("FAIL: INPUT ABI " + abiError);
            return 1;
        }
        Console.WriteLine("=== T-301 CONSOLE CLIPBOARD ACCEPTANCE ===");
        Console.WriteLine("Engine: " + enginePath);

        ClipboardSnapshot saved;
        string snapshotError;
        bool snapshotOk = SnapshotClipboard(out saved, out snapshotError);
        if (!snapshotOk) Console.WriteLine("CLIPBOARD_SNAPSHOT=FAILED: " + snapshotError);
        string tempDir = Path.Combine(Path.GetTempPath(), "saituls-accept-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(tempDir);
        string restoration = "NOT_RUN";
        try { RunSuite(enginePath, tempDir); }
        finally
        {
            restoration = snapshotOk ? RestoreClipboard(saved) : "SKIPPED_SNAPSHOT_FAILED";
            Console.WriteLine("CLIPBOARD_RESTORATION=" + restoration);
            if (Environment.GetEnvironmentVariable("SAI_KEEP_TEMP") != "1")
            {
                try { Directory.Delete(tempDir, true); } catch { }
            }
        }

        // Driving the real clipboard on a live desktop is only safe when the
        // operator treats this session as disposable, or when the original
        // content was verifiably restored. Anything else is classified, not
        // rounded up to PASS.
        bool disposable = Environment.GetEnvironmentVariable("SAI_CLIPBOARD_DISPOSABLE") == "1";
        bool restorationSafe = restoration == "VERIFIED" || restoration == "NOT_REQUIRED" || disposable;

        int blockedEnv = 0, blockedInteraction = 0;
        foreach (string line in checks)
        {
            // Only a blocked case is a blocked case: a passing case that
            // merely mentions the desktop is not an environment block.
            if (!line.StartsWith("[BLOCKED]", StringComparison.Ordinal)) continue;
            if (line.IndexOf("input unavailable", StringComparison.OrdinalIgnoreCase) >= 0
                || line.IndexOf("desktop", StringComparison.OrdinalIgnoreCase) >= 0
                || line.IndexOf("foreign printable", StringComparison.OrdinalIgnoreCase) >= 0
                || line.IndexOf("clipboard setup", StringComparison.OrdinalIgnoreCase) >= 0) blockedEnv++;
            else blockedInteraction++;
        }
        if (blockedEnv + blockedInteraction > blocked) blockedInteraction = blocked - blockedEnv;
        string status = fails > 0 ? "FAIL"
            : blocked > 0 ? (blockedEnv > 0 ? "BLOCKED_ENVIRONMENT" : "BLOCKED_INTERACTION")
            : !restorationSafe ? "INCOMPLETE"
            : "PASS";
        Console.WriteLine("===============================================");
        Console.WriteLine("STATUS=" + status);
        Console.WriteLine("ACCEPTANCE SUMMARY: " + passes + " passed, " + fails + " failed, " + blocked + " blocked");
        Console.WriteLine("BLOCKED_ENVIRONMENT=" + blockedEnv + " BLOCKED_INTERACTION=" + blockedInteraction
            + " INCOMPLETE=" + (restorationSafe ? 0 : 1));
        Console.WriteLine("CLIPBOARD_RESTORATION_CLASS=" + (restorationSafe ? "SAFE" : "UNVERIFIED"));
        foreach (string line in checks) Console.WriteLine("  " + line);
        var jsonChecks = new List<string>();
        foreach (string line in checks) jsonChecks.Add("\"" + line.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"");
        Console.WriteLine("JSON_RESULT={\"passes\":" + passes + ",\"fails\":" + fails + ",\"blocked\":" + blocked
            + ",\"blocked_environment\":" + blockedEnv + ",\"blocked_interaction\":" + blockedInteraction
            + ",\"incomplete\":" + (restorationSafe ? 0 : 1) + ",\"status\":\"" + status + "\""
            + ",\"clipboard_restoration\":\"" + restoration.Replace("\\", "\\\\").Replace("\"", "\\\"")
            + "\",\"checks\":[" + string.Join(",", jsonChecks) + "]}");
        if (fails > 0) return 1;
        if (blocked > 0 || !restorationSafe) return 2;
        return 0;
    }

    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] static extern IntPtr GetFocus();
    [DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    static extern IntPtr CreateFileW2(string name, uint access, uint share, IntPtr sec, uint disp, uint flags, IntPtr tmpl);
    [DllImport("kernel32.dll")] static extern uint GetFileType(IntPtr h);

    static string B64(string text) { return Convert.ToBase64String(Encoding.UTF8.GetBytes(text)); }

    // ------------------------------------------------------------- pair model

    // A disposable guard + mock-vendor pair. The guard is started with its own
    // new console, so the runner's console is never hijacked and the disposable
    // window is fully owned by this test.
    sealed class Pair : IDisposable
    {
        public string Dir, Sync, ReleasePath, Recv, Title;
        // The vendor copy this pair started: a private copy of the runner in
        // the pair's own temp directory. It must never outlive the pair.
        public string VendorPath;
        public Process Guard;
        public IntPtr Window;

        public static Pair Start(string enginePath, string dir, string role, string title, string account,
            Dictionary<string, string> extraEnv = null)
        {
            Directory.CreateDirectory(dir);
            var pair = new Pair
            {
                Dir = dir,
                Sync = Path.Combine(dir, "sync.txt"),
                ReleasePath = Path.Combine(dir, "release.txt"),
                Recv = Path.Combine(dir, "recv.txt"),
                Title = title,
            };
            string selfExe = Process.GetCurrentProcess().MainModule.FileName;
            string agyPath = Path.Combine(dir, "agy.exe");
            File.Copy(selfExe, agyPath, true);
            pair.VendorPath = agyPath;
            string cmdLine = "\"" + enginePath + "\" console-title-guard --title64 " + B64(title)
                + " --account " + account + " -- \"" + agyPath + "\" --mock-vendor " + role
                + " \"" + pair.Sync + "\" \"" + pair.ReleasePath + "\" \"" + pair.Recv + "\"";
            pair.Guard = StartWithNewConsole(cmdLine, extraEnv);
            if (pair.Guard == null) throw new InvalidOperationException("guard did not start");

            var sw = Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < 20000)
            {
                pair.Window = FindConsoleWindow(pair.Guard, pair.Title);
                if (pair.Window != IntPtr.Zero && AwaitSync(pair.Sync, "ready=1", 500)) break;
                Thread.Sleep(100);
            }
            return pair;
        }

        public void Release() { try { File.WriteAllText(ReleasePath, "release"); } catch { } }

        public void Dispose()
        {
            Release();
            try { if (Guard != null && !Guard.HasExited) Guard.WaitForExit(3000); } catch { }
            try { if (Guard != null && !Guard.HasExited) Guard.Kill(); } catch { }
            // The vendor child is a private copy in this pair's temp dir. If it
            // outlives the pair it keeps a console window on the operator's
            // desktop, so it is stopped by that exact path.
            StopVendorCopy();
            try { Guard.Dispose(); } catch { }
        }

        void StopVendorCopy()
        {
            if (string.IsNullOrEmpty(VendorPath)) return;
            try
            {
                foreach (Process p in Process.GetProcesses())
                {
                    try
                    {
                        if (p.MainModule == null) continue;
                        if (!string.Equals(p.MainModule.FileName, VendorPath, StringComparison.OrdinalIgnoreCase)) continue;
                        try { p.Kill(); } catch { }
                    }
                    catch { }
                    finally { try { p.Dispose(); } catch { } }
                }
            }
            catch { }
        }
    }

    static bool AwaitSync(string path, string marker, int timeoutMs)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            try
            {
                if (File.Exists(path) && File.ReadAllText(path).Contains(marker)) return true;
            }
            catch { }
            Thread.Sleep(50);
        }
        return false;
    }

    static string SyncField(string syncText, string field)    {
        foreach (string part in syncText.Split(';'))
        {
            int eq = part.IndexOf('=');
            if (eq > 0 && part.Substring(0, eq) == field) return part.Substring(eq + 1);
        }
        return null;
    }

    // The vendor's own recorded mode, geometry and selection state.
    static string Field(Pair pair, string name) { return SyncField(ReadText(pair.Sync), name); }

    // The vendor rewrites its sync file every 100 ms. A read that lands in
    // the middle of that write sees a torn file; a missing field is a race,
    // not a verdict. Wait for the field to come back.
    static string FieldWait(Pair pair, string name, int timeoutMs = 4000)
    {
        var sw = Stopwatch.StartNew();
        string value = null;
        while (true)
        {
            value = Field(pair, name);
            if (value != null) return value;
            if (sw.ElapsedMilliseconds >= timeoutMs) return value;
            Thread.Sleep(50);
        }
    }

    static uint VendorMode(Pair pair, int timeoutMs = 5000)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            string mode = Field(pair, "mode");
            uint value;
            if (mode != null && uint.TryParse(mode.Replace("0x", ""), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out value))
                return value;
            Thread.Sleep(100);
        }
        return 0;
    }

    // The controller polls at 25 ms and the vendor records its mode at 100 ms,
    // so a single early read races the restore. A settled read waits for the
    // value to stop changing instead of trusting the first observable.
    static uint VendorModeSettled(Pair pair, int timeoutMs = 4000)
    {
        var sw = Stopwatch.StartNew();
        uint last = 0;
        int stable = 0;
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            uint mode = VendorMode(pair, 400);
            if (mode == last && mode != 0 && sw.ElapsedMilliseconds >= 500) { if (++stable >= 3) return mode; }
            else stable = 0;
            last = mode;
            Thread.Sleep(60);
        }
        return last;
    }

    // Physical input gate: this acceptance drives real foreground keyboard and
    // mouse. On a session with no interactive input desktop (unattended agent:
    // no window can take focus, SendInput lands nowhere) every input case is
    // BLOCKED, never PASS, per the mission brief.
    static string inputUnavailability = ProbeInputUnavailability();

    static string VerifyInputAbi()
    {
        int keySize = Marshal.SizeOf(typeof(KeyInput));
        int mouseSize = Marshal.SizeOf(typeof(MouseInput));
        int inputSize = Marshal.SizeOf(typeof(INPUT));
        int unionSize = Marshal.SizeOf(typeof(InputUnion));
        int unionOffset = Marshal.OffsetOf(typeof(INPUT), "u").ToInt32();
        if (IntPtr.Size == 8)
        {
            if (keySize != 24 || mouseSize != 32 || inputSize != 40 || unionSize != 32 || unionOffset != 8)
                return "x64 layout mismatch: KeyInput=" + keySize + " MouseInput=" + mouseSize
                    + " INPUT=" + inputSize + " union=" + unionSize + " offset=" + unionOffset;
        }
        else
        {
            if (keySize != 16 || mouseSize != 24 || inputSize != 28 || unionSize != 24 || unionOffset != 4)
                return "x86 layout mismatch: KeyInput=" + keySize + " MouseInput=" + mouseSize
                    + " INPUT=" + inputSize + " union=" + unionSize + " offset=" + unionOffset;
        }
        return null;
    }

    static string ProbeInputUnavailability()
    {
        // Classifications: INTERACTIVE_AVAILABLE (null), DESKTOP_UNAVAILABLE,
        // DESKTOP_ACCESS_DENIED, FOREGROUND_UNAVAILABLE, INPUT_INJECTION_DENIED,
        // TEST_WINDOW_UNAVAILABLE, INCONCLUSIVE. Desktop access and input
        // injection are reported separately; no elevation mismatch is inferred.
        try
        {
            IntPtr desktop = OpenInputDesktop(0, false, 0x0001);
            if (desktop == IntPtr.Zero)
            {
                int err = Marshal.GetLastWin32Error();
                if (err == 5 || err == 1314)
                    return "DESKTOP_ACCESS_DENIED: OpenInputDesktop failed err=" + err;
                return "DESKTOP_UNAVAILABLE: OpenInputDesktop failed err=" + err;
            }
            try
            {
                var name = new StringBuilder(256);
                uint size = 0;
                if (!GetUserObjectInformation(desktop, 2, name, (uint)(name.Capacity * 2), out size)
                    || !string.Equals(name.ToString(), "Default", StringComparison.Ordinal))
                {
                    return "DESKTOP_UNAVAILABLE: input desktop is not Default (name=" + name + ")";
                }
            }
            finally { CloseDesktop(desktop); }

            // A disposable, test-owned foreground window; the runner console is
            // not used as the injection target and its foreground is restored.
            string delivery = OracleKeyDelivery(false);
            return delivery;
        }
        catch (Exception ex) { return "INCONCLUSIVE: " + ex.Message; }
    }

    static Form NewProbeForm(string title)
    {
        return new Form
        {
            Text = title,
            TopMost = true,
            ShowInTaskbar = false,
            Width = 160,
            Height = 80,
            StartPosition = FormStartPosition.Manual,
            Location = new System.Drawing.Point(40, 40),
            KeyPreview = true,
        };
    }

    // The only acceptable proof of injected input is a test-owned window that
    // observed the key. A positive SendInput return count alone is not delivery.
    // `deliberatelyUnfocused` is the negative-control fault: SendInput succeeds,
    // the intended probe is not foreground, and the oracle must reject it.
    static string OracleKeyDelivery(bool deliberatelyUnfocused)
    {
        IntPtr previous = GetForegroundWindow();
        Form probe = null, blocker = null;
        try
        {
            bool received = false;
            probe = NewProbeForm("SAITULS probe " + RunNonce);
            probe.KeyDown += delegate (object sender, KeyEventArgs e)
            {
                if (e.KeyCode == Keys.F24) received = true;
            };
            probe.Show();
            IntPtr hwnd = probe.Handle;
            if (hwnd == IntPtr.Zero) return "TEST_WINDOW_UNAVAILABLE: probe window handle unavailable";

            uint ownerPid;
            GetWindowThreadProcessId(hwnd, out ownerPid);
            if (ownerPid != (uint)Process.GetCurrentProcess().Id)
                return "TEST_WINDOW_UNAVAILABLE: probe window does not belong to the test runner";

            if (!OwnWindow(hwnd, 4000)) return "FOREGROUND_UNAVAILABLE: test window cannot become foreground";

            IntPtr expected = hwnd;
            if (deliberatelyUnfocused)
            {
                blocker = NewProbeForm("SAITULS blocker " + RunNonce);
                blocker.Show();
                if (!OwnWindow(blocker.Handle, 4000))
                    return "FOREGROUND_UNAVAILABLE: blocker window cannot become foreground";
                expected = blocker.Handle;
            }

            if (GetForegroundWindow() != expected)
                return "FOREGROUND_CHANGED: dispatch target lost foreground before SendInput";

            received = false;
            bool inputOk = Tap(0x87, false, false);           // F24: harmless, observable
            var sw = Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < 750 && !received)
            {
                Application.DoEvents();
                Thread.Sleep(10);
            }
            if (!inputOk || !lastInputOk)
                return "INPUT_INJECTION_DENIED: SendInput failed err=" + lastInputError + " count=" + lastInputCount;
            if (GetForegroundWindow() != expected)
                return "FOREGROUND_CHANGED: foreground moved during SendInput dispatch";
            if (!received)
                return deliberatelyUnfocused
                    ? "DELIVERY_NOT_OBSERVED: SendInput accepted " + lastInputCount + " events, unfocused probe received none"
                    : "INPUT_INJECTION_DENIED: F24 not delivered to test window";
            return null;
        }
        finally
        {
            try { if (blocker != null) { blocker.Close(); blocker.Dispose(); } } catch { }
            try { if (probe != null) { probe.Close(); probe.Dispose(); } } catch { }
            if (previous != IntPtr.Zero && GetForegroundWindow() != previous)
            {
                try { SetForegroundWindow(previous); } catch { }
            }
        }
    }

    static bool VendorSelected(Pair pair, int timeoutMs)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            string sel = Field(pair, "sel");
            if (sel == "1") return true;
            // A sync sample written before the drag lands is stale evidence,
            // not proof of absence: keep polling until the timeout.
            Thread.Sleep(50);
        }
        return false;
    }

    static List<string> RecvLines(Pair pair)
    {
        string text = ReadText(pair.Recv);
        var lines = new List<string>();
        if (text.Length == 0) return lines;
        foreach (string line in text.Split('\n'))
        {
            string trimmed = line.TrimEnd('\r');
            if (trimmed.Length > 0) lines.Add(trimmed);
        }
        return lines;
    }

    static string WaitLine(Pair pair, int skip, int timeoutMs, out string rawTail)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            List<string> lines = RecvLines(pair);
            if (lines.Count > skip) { rawTail = string.Join(" | ", lines); return lines[skip]; }
            Thread.Sleep(50);
        }
        rawTail = string.Join(" | ", RecvLines(pair));
        return null;
    }

    static string ClipWait(Func<string> read, Func<string, bool> present, int timeoutMs)
    {
        var sw = Stopwatch.StartNew();
        string last = null;
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            try
            {
                last = read();
                if (present(last)) return last;
            }
            catch { }
            Thread.Sleep(50);
        }
        return last;
    }

    // ------------------------------------------------------------ window/input

    static bool OwnWindow(IntPtr hwnd, int timeoutMs)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            SetForegroundWindow(hwnd);
            if (GetForegroundWindow() == hwnd) return true;
            Thread.Sleep(100);
        }
        return false;
    }

    static bool ForegroundStill(IntPtr hwnd) { return hwnd != IntPtr.Zero && GetForegroundWindow() == hwnd; }

    // Geometry from the vendor's own recorded console metrics: characters are
    // screen-space cell rectangles, so a drag across N cells selects N cells.
    // False means the test-owned console lost foreground before or during the
    // drag; the caller reports BLOCKED instead of scoring a delivery miss.
    static bool DragSelect(Pair pair, int row, int startCol, int colCount)
    {
        string colsText = FieldWait(pair, "cols"), rowsText = FieldWait(pair, "rows");
        if (colsText == null || rowsText == null) return false;
        if (!ForegroundStill(pair.Window)) return false;
        int cols = int.Parse(colsText), rows = int.Parse(rowsText);

        WinRect client;
        GetClientRect(pair.Window, out client);
        int clientW = client.Right - client.Left, clientH = client.Bottom - client.Top;
        double cellW = (double)clientW / cols, cellH = (double)clientH / rows;

        var topLeft = new WinPoint { X = client.Left, Y = client.Top };
        ClientToScreen(pair.Window, ref topLeft);
        int y = topLeft.Y + (int)((row + 0.5) * cellH);
        int x0 = topLeft.X + (int)((startCol + 0.25) * cellW);
        int x1 = topLeft.X + (int)((startCol + colCount - 0.25) * cellW);

        SetCursorPos(x0, y);
        Thread.Sleep(30);
        if (!ForegroundStill(pair.Window)) return false;
        Mouse(0x0002);                              // MOUSEEVENTF_LEFTDOWN
        for (int step = 1; step <= 3; step++)
        {
            SetCursorPos(x0 + (x1 - x0) * step / 3, y);
            Thread.Sleep(40);
        }
        Mouse(0x0004);                              // MOUSEEVENTF_LEFTUP
        Thread.Sleep(120);
        return ForegroundStill(pair.Window);
    }

    // Esc clears a QuickEdit selection inside the owned console. The previous
    // version clicked a virtual-screen corner, which could send a real click to
    // an unrelated operator window.
    static bool ClickClear(Pair pair)
    {
        if (!ForegroundStill(pair.Window)) return false;
        Tap(0x1B);                                   // VK_ESCAPE
        Thread.Sleep(80);
        return ForegroundStill(pair.Window);
    }

    static int lastInputError;
    static int lastInputCount;
    static bool lastInputOk = true;
    static string lastPasteNote;
    static bool SendInputs(INPUT[] inputs)
    {
        uint sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT)));
        lastInputCount = (int)sent;
        if (sent != (uint)inputs.Length)
        {
            lastInputError = Marshal.GetLastWin32Error();
            lastInputOk = false;
            return false;
        }
        lastInputOk = true;
        return true;
    }

    static bool Tap(short vk, bool withCtrl = false, bool zeroDwell = false)
    {
        bool ok = true;
        if (withCtrl)
        {
            var cdown = new INPUT { type = 1, u = new InputUnion { key = new KeyInput { wVk = 0x11, dwFlags = 0 } } };
            ok &= SendInputs(new[] { cdown });
        }
        var down = new INPUT { type = 1, u = new InputUnion { key = new KeyInput { wVk = vk, dwFlags = 0 } } };
        var up = new INPUT { type = 1, u = new InputUnion { key = new KeyInput { wVk = vk, dwFlags = 2 } } };
        ok &= SendInputs(new[] { down });
        if (zeroDwell) ok &= SendInputs(new[] { up });
        else { Thread.Sleep(15); ok &= SendInputs(new[] { up }); }
        if (withCtrl)
        {
            var cup = new INPUT { type = 1, u = new InputUnion { key = new KeyInput { wVk = 0x11, dwFlags = 2 } } };
            ok &= SendInputs(new[] { cup });
        }
        Thread.Sleep(15);
        return ok;
    }

    static bool Chord(short[] vks, bool zeroDwell = false)
    {
        bool ok = true;
        foreach (short vk in vks) { var d = new INPUT { type = 1, u = new InputUnion { key = new KeyInput { wVk = vk, dwFlags = 0 } } }; ok &= SendInputs(new[] { d }); }
        if (!zeroDwell) Thread.Sleep(60);
        for (int i = vks.Length - 1; i >= 0; i--) { var u = new INPUT { type = 1, u = new InputUnion { key = new KeyInput { wVk = vks[i], dwFlags = 2 } } }; ok &= SendInputs(new[] { u }); }
        if (!zeroDwell) Thread.Sleep(80);
        return ok;
    }

    static bool Mouse(uint flags)
    {
        var m = new INPUT { type = 0, u = new InputUnion { mouse = new MouseInput { dwFlags = flags } } }; return SendInputs(new[] { m });
    }

    static IntPtr WindowProp(IntPtr hwnd, string name)
    {
        if (hwnd == IntPtr.Zero) return IntPtr.Zero;
        return GetPropW(hwnd, name);
    }

    // ------------------------------------------------------------------- suite

    static void RunSuite(string enginePath, string root)
    {
        string dir = Path.Combine(root, "work");
        Directory.CreateDirectory(dir);

        // Case N: QuickEdit restored from a plain classic mode the vendor cleared.
        var probeClassic = CaseModeRestore(enginePath, Path.Combine(dir, "classic"), "classic-render", TitleA, AccountA, null);
        uint? modeClassic = probeClassic == null ? (uint?)null : probeClassic.Mode;
        Check("N.QuickEditRestored", "classic mode regains QuickEdit without touching mouse/VT flags",
            modeClassic.HasValue && (modeClassic.Value & QuickEditFlag) != 0 && (modeClassic.Value & MouseInputFlag) == 0
                && (modeClassic.Value & VtInputFlag) == 0,
            modeClassic.HasValue ? "mode=0x" + modeClassic.Value.ToString("X4") : "instrumentation missing");

        // Case M: mouse-on / QuickEdit-off (the uncovered T-300 branch).
        var probeMouse = CaseModeRestore(enginePath, Path.Combine(dir, "mouse"), "mouse-render", TitleA, AccountA, null);
        uint? modeMouse = probeMouse == null ? (uint?)null : probeMouse.Mode;
        Check("M.MouseOwnerPreserved", "mouse-input vendor keeps its mode (no forced QuickEdit)",
            modeMouse.HasValue && (modeMouse.Value & QuickEditFlag) == 0 && (modeMouse.Value & MouseInputFlag) != 0,
            modeMouse.HasValue ? "mode=0x" + modeMouse.Value.ToString("X4") : "instrumentation missing");

        // Case L: VT vendor mode must not be forced into QuickEdit.
        var probeVt = CaseModeRestore(enginePath, Path.Combine(dir, "vt"), "vt-render", TitleA, AccountA, null);
        uint? modeVt = probeVt == null ? (uint?)null : probeVt.Mode;
        Check("L.VtModePreserved", "VT-input vendor keeps VT and gets no forced QuickEdit",
            modeVt.HasValue && (modeVt.Value & VtInputFlag) != 0 && (modeVt.Value & QuickEditFlag) == 0,
            modeVt.HasValue ? "mode=0x" + modeVt.Value.ToString("X4") : "instrumentation missing");

        // Case O: controller deliberately off -- the matched negative probe.
        var probeOff = CaseModeRestore(enginePath, Path.Combine(dir, "off"), "classic-render", TitleA, AccountA,
            new Dictionary<string, string> { { "SAI_AG_CONSOLE_CONTROLLER", "off" } });
        uint? modeOff = probeOff == null ? (uint?)null : probeOff.Mode;
        Check("O.ControllerOffIsObservable", "controller-off run restores nothing and publishes its state",
            modeOff.HasValue && (modeOff.Value & QuickEditFlag) == 0 && probeOff != null && probeOff.ControllerProp == (IntPtr)2,
            modeOff.HasValue ? "mode=0x" + modeOff.Value.ToString("X4") + " prop=" + (probeOff == null ? "?" : probeOff.ControllerProp.ToString()) : "instrumentation missing");

        // Case R: no global hook regression (bridge off by default, property proves it).
        Check("R.NoGlobalHooksDefault", "compatibility bridge stays disabled by default",
            probeClassic != null && probeClassic.BridgeProp == (IntPtr)2,
            "prop=" + (probeClassic == null ? "?" : probeClassic.BridgeProp.ToString()));

        // --- copy/paste/interrupt on a live console -------------------------

        // Case A: real mouse drag selects, Enter copies exact buffer text.
        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "copy"), "classic-render", TitleA, AccountA))
        {
            if (inputUnavailability != null)
                Blocked("A.DragCopy", "real foreground keyboard/mouse delivery", "input unavailable: " + inputUnavailability);
            else if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000))
            {
                Blocked("A.DragCopy", "mouse selection + copy", "window ownership unavailable");
            }
            else
            {
                uint mode = VendorModeSettled(pair);
                string texrowText = FieldWait(pair, "texrow");
                if ((mode & QuickEditFlag) == 0 || texrowText == null)
                {
                    Blocked("A.DragCopy", "mouse selection + copy", "QuickEdit not established; mode=0x" + mode.ToString("X4"));
                }
                else
                {
                    int texrow = int.Parse(texrowText);
                    // The console must be activated before the drag: an
                    // unactivated input queue swallows the first click and the
                    // drag selects nothing, which is a test precondition, not
                    // a product failure.
                    if (!ActivateConsole(pair))
                        Blocked("A.DragCopy", "mouse selection + copy", "foreground changed while activating console");
                    else
                    {
                        bool dragged = DragSelect(pair, texrow, 0, CellWidth(KnownText));
                        bool selected = dragged && VendorSelected(pair, 2000);
                        if (!dragged || !ForegroundStill(pair.Window))
                        {
                            Blocked("A.DragCopy", "mouse selection + copy", "foreground changed during drag");
                        }
                        else
                        {
                            Tap(0x0D);                               // Enter copies the selection
                            string clip = ClipWait(() => GetClipboardText(), t => t != null, 3000);
                            string core = clip != null && clip.EndsWith("\r\n") ? clip.Substring(0, clip.Length - 2) : clip;
                            bool copied = core == KnownText;
                            Check("A.DragCopy", "real drag selects and Enter copies the exact rendered text",
                                selected && copied, "selected=" + selected + " clip=" + Short(clip) + " len=" + (clip == null ? 0 : clip.Length));
                        }
                    }

                    // Case B: Ctrl+C with a live selection copies again through the
                    // same oracle the negative controls use.
                    string copyDetail = OracleCopyWithSelection(pair);
                    if (copyDetail != null && copyDetail.StartsWith("BLOCKED:", StringComparison.Ordinal))
                        Blocked("B.CtrlCWithSelection", "Ctrl+C with selection copies", copyDetail.Substring(8));
                    else
                        Check("B.CtrlCWithSelection", "Ctrl+C with a fresh selection copies, console and vendor survive",
                            copyDetail == null && !pair.Guard.HasExited,
                            (copyDetail ?? "copied exact text") + " guardAlive=" + !pair.Guard.HasExited);

                    ClickClear(pair);
                }

                // Case P: a zero-dwell Ctrl+A tap still selects (poll-latch check).
                bool tapOk = ActivateConsole(pair) && Tap(0x41, true, true);  // real Ctrl+A, zero dwell
                bool allSelected = tapOk && VendorSelected(pair, 2500);
                Check("P.ShortPressCtrlA", "sub-25ms Ctrl+A tap still selects all",
                    tapOk && allSelected && lastInputOk,
                    "tapOk=" + tapOk + " selected=" + allSelected + " inputOk=" + lastInputOk
                        + " foreground=" + (ForegroundStill(pair.Window) ? "owned" : "lost"));
            }
        }

        // Case C: Ctrl+C without selection interrupts; nothing dies.
        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "sigint"), "classic-sigint", TitleA, AccountA))
        {
            if (inputUnavailability != null)
                Blocked("C.CtrlCInterrupt", "real foreground keyboard/mouse delivery", "input unavailable: " + inputUnavailability);
            else if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000))
            {
                Blocked("C.CtrlCInterrupt", "Ctrl+C without selection interrupts", "window ownership unavailable");
            }
            else
            {
                if (!ActivateConsole(pair))
                {
                    Blocked("C.CtrlCInterrupt", "Ctrl+C without selection interrupts", "foreground changed while activating console");
                }
                else
                {
                    // The activation click itself anchors a QuickEdit selection;
                    // Ctrl+C would then copy instead of interrupting. Clear it.
                    ClickClear(pair);
                    bool selectedBefore = VendorSelected(pair, 500);
                    string sentinel = "SigintSentinel_" + RunNonce;
                    string setError;
                    TrySetClipboardText(sentinel, out setError);
                    bool chordOk = Chord(new short[] { 0x11, 0x43 });   // Ctrl+C, no selection present
                    string raw;
                    Thread.Sleep(1200);
                    raw = ReadText(pair.Recv);
                    bool sigint = ReadText(pair.Recv).Contains("SIGINT");
                    bool copied = GetClipboardText() != sentinel;
                    // Diagnostic only: Ctrl+Break uses the other console signal
                    // path, so it separates "chord never reached conhost" from
                    // "conhost chose copy/no-op for this Ctrl+C".
                    bool breakOk = !sigint && Chord(new short[] { 0x11, 0x03 });
                    if (breakOk) Thread.Sleep(800);
                    bool sigint2 = ReadText(pair.Recv).Contains("SIGINT");
                    bool alive = !pair.Guard.HasExited;
                    Check("C.CtrlCInterrupt", "Ctrl+C without selection reaches the child as interrupt",
                        chordOk && lastInputOk && sigint && alive && ForegroundStill(pair.Window),
                        "sigint=" + sigint + " inputOk=" + lastInputOk + " guardAlive=" + alive
                            + " foreground=" + (ForegroundStill(pair.Window) ? "owned" : "lost")
                            + " clipboardCopied=" + copied + " selectedBefore=" + selectedBefore
                            + " trace=" + Short(ReadText(pair.Recv + ".raw")) + " breakOk=" + breakOk + " sigintAfterBreak=" + sigint2
                            + " recv=" + raw);
                }
            }
        }

        // Cases D/G: single-line Unicode and Cyrillic/East-Asian fidelity paste.
        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "paste"), "classic-reader", TitleA, AccountA))
        {
            if (inputUnavailability != null)
                Blocked("D.PasteUnicode", "real foreground keyboard/mouse delivery", "input unavailable: " + inputUnavailability);
            else if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000))
            {
                Blocked("D.PasteUnicode", "real Ctrl+V paste", "window ownership unavailable");
                Blocked("G.UnicodeFidelity", "Estonian/Russian/Japanese paste", "window ownership unavailable");
            }
            else if (!ActivateConsole(pair))
            {
                Blocked("D.PasteUnicode", "real Ctrl+V paste", "foreground could not be acquired for dispatch");
                Blocked("G.UnicodeFidelity", "Estonian/Russian/Japanese paste", "foreground could not be acquired for dispatch");
                Blocked("E.MultilinePaste", "multiline paste", "foreground could not be acquired for dispatch");
                Blocked("F.LargePaste", "large multiline paste", "foreground could not be acquired for dispatch");
            }
            else
            {
                string dGot = "";
                if (!RequireClipboard("D.PasteUnicode", "real Ctrl+V pastes clipboard text into the console", KnownText))
                {
                    Blocked("G.UnicodeFidelity", "Estonian/Russian/Japanese paste", "clipboard setup failed");
                    Blocked("E.MultilinePaste", "multiline paste", "clipboard setup failed");
                    Blocked("F.LargePaste", "large multiline paste", "clipboard setup failed");
                }
                else
                {
                    int before = RawInput(pair).Length;
                    if (ForegroundStill(pair.Window) && Chord(new short[] { 0x11, 0x56 }))
                        dGot = WaitRawAppend(pair, before, 5000);
                    PasteVerdict("D.PasteUnicode", "real Ctrl+V pastes clipboard text into the console",
                        dGot, KnownText, false, ForegroundStill(pair.Window));

                    // Each subsequent paste is compared only against the bytes it
                    // appended, so an earlier paste cannot mask a later defect.
                    string rus = "\u0422\u0435\u0441\u0442\u043e\u0432\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430 12345";
                    string est = "Eesti: \u00f5\u00e4\u00f6\u00fc - t\u00e4hestik";
                    string jpn = "\u65e5\u672c\u8a9e: \u3053\u3093\u306b\u3061\u306f\u4e16\u754c";
                    string line1 = null, line2 = null, line3 = null;
                    if (RequireClipboard("G.UnicodeFidelity", "Estonian/Russian/Japanese paste byte-exact", rus))
                    {
                        int gb = RawInput(pair).Length;
                        if (ForegroundStill(pair.Window) && Chord(new short[] { 0x11, 0x56 }))
                            line1 = WaitRawAppend(pair, gb, 5000);
                    }
                    if (RequireClipboard("G.UnicodeFidelity", "Estonian/Russian/Japanese paste byte-exact", est))
                    {
                        int gb = RawInput(pair).Length;
                        if (ForegroundStill(pair.Window) && Chord(new short[] { 0x11, 0x56 }))
                            line2 = WaitRawAppend(pair, gb, 5000);
                    }
                    if (RequireClipboard("G.UnicodeFidelity", "Estonian/Russian/Japanese paste byte-exact", jpn))
                    {
                        int gb = RawInput(pair).Length;
                        if (ForegroundStill(pair.Window) && Chord(new short[] { 0x11, 0x56 }))
                            line3 = WaitRawAppend(pair, gb, 5000);
                    }
                    string gReason = null;
                    if (!ForegroundStill(pair.Window)) gReason = "BLOCKED:the console lost the foreground during the paste";
                    else if (line1 == null || line2 == null || line3 == null) gReason = "FAIL:a paste never arrived";
                    else if (line1 != rus) gReason = PasteMismatch(line1, rus, false);
                    else if (line2 != est) gReason = PasteMismatch(line2, est, false);
                    else if (line3 != jpn) gReason = PasteMismatch(line3, jpn, false);
                    if (gReason != null && gReason.StartsWith("BLOCKED:", StringComparison.Ordinal))
                        Blocked("G.UnicodeFidelity", "Estonian/Russian/Japanese paste byte-exact", gReason.Substring(8));
                    else
                        Check("G.UnicodeFidelity", "Estonian/Russian/Japanese paste byte-exact",
                            gReason == null,
                            gReason ?? ("ru=" + (line1 == rus) + " et=" + (line2 == est) + " ja=" + (line3 == jpn)));

                    // Case E: multiline paste arrives as separate lines, no auto-submit.
                    string multi = "PasteLine1\nPasteLine2\nPasteLine3";
                    string eGot = null;
                    if (RequireClipboard("E.MultilinePaste", "multiline paste keeps each line, no trailing submit", multi))
                    {
                        int eb = RawInput(pair).Length;
                        if (ForegroundStill(pair.Window) && Chord(new short[] { 0x11, 0x56 }))
                            eGot = WaitRawAppend(pair, eb, 5000);
                    }
                    PasteVerdict("E.MultilinePaste", "multiline paste keeps each line, no trailing submit",
                        eGot, multi, true, ForegroundStill(pair.Window));

                    // Case F: large multiline (~4KB) paste.
                    var big = new StringBuilder();
                    for (int i = 0; i < 100; i++) big.Append("Block_").Append(i.ToString("D4")).Append("_0123456789ABCDEF\n");
                    string expectedBig = big.ToString();
                    string fGot = null;
                    if (RequireClipboard("F.LargePaste", "~4KB multiline paste arrives complete and ordered", expectedBig))
                    {
                        int fb = RawInput(pair).Length;
                        if (ForegroundStill(pair.Window) && Chord(new short[] { 0x11, 0x56 }))
                            fGot = WaitRawAppend(pair, fb, 10000);
                    }
                    PasteVerdict("F.LargePaste", "~4KB multiline paste arrives complete and ordered",
                        fGot, expectedBig, true, ForegroundStill(pair.Window));
                }
            }
        }

        // Case I: clipboard contention must not hang or corrupt the paste.
        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "contention"), "classic-reader", TitleA, AccountA))
        {
            if (inputUnavailability != null)
                Blocked("I.ClipboardContention", "real foreground keyboard/mouse delivery", "input unavailable: " + inputUnavailability);
            else if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000))
            {
                Blocked("I.ClipboardContention", "paste under clipboard contention", "window ownership unavailable");
            }
            else
            {
                var stop = new ManualResetEventSlim(false);
                var hammer = new Thread(() =>
                {
                    while (!stop.IsSet)
                    {
                        string ignored;
                        TrySetClipboardText("CONTEST_RIVAL", out ignored);
                        Thread.Sleep(60);
                    }
                });
                hammer.SetApartmentState(ApartmentState.STA);
                hammer.Start();
                bool primed = RequireClipboard("I.ClipboardContention",
                    "paste under contention completes with one whole line", "Ours_7567");
                string line = null;
                if (primed)
                {
                    int ib = RawInput(pair).Length;
                    if (ForegroundStill(pair.Window) && Chord(new short[] { 0x11, 0x56 }))
                        line = WaitRawAppend(pair, ib, 6000);
                }
                stop.Set(); hammer.Join(1000);
                bool complete = line == "Ours_7567" || line == "CONTEST_RIVAL";
                Check("I.ClipboardContention", "paste under contention completes with one whole line",
                    complete && !pair.Guard.HasExited,
                    "got=" + Short(line) + " guardAlive=" + !pair.Guard.HasExited
                        + " foreground=" + (ForegroundStill(pair.Window) ? "owned" : "lost"));
            }
        }

        // Case J: two concurrent consoles stay isolated and carry their identity.
        using (var pairA = Pair.Start(enginePath, Path.Combine(dir, "dualA"), "classic-reader", TitleA, AccountA))
        using (var pairB = Pair.Start(enginePath, Path.Combine(dir, "dualB"), "classic-reader", TitleB, AccountB))
        {
            if (inputUnavailability != null)
                Blocked("J.TwoConsoles", "two simultaneous managed consoles", "input unavailable: " + inputUnavailability);
            else if (pairA.Window == IntPtr.Zero || pairB.Window == IntPtr.Zero
                || !OwnWindow(pairA.Window, 4000) || !OwnWindow(pairB.Window, 4000))
            {
                Blocked("J.TwoConsoles", "two simultaneous managed consoles", "window ownership unavailable");
            }
            else
            {
                OwnWindow(pairA.Window, 4000);                  // A owns the keyboard
                bool primed = RequireClipboard("J.TwoConsoles",
                    "concurrent consoles isolate paste and keep identity", "OnlyForA_8841");
                string gotA = null;
                if (primed)
                {
                    int jb = RawInput(pairA).Length;
                    if (ForegroundStill(pairA.Window) && Chord(new short[] { 0x11, 0x56 }))
                        gotA = WaitRawAppend(pairA, jb, 5000);
                }
                bool bUntouched = RecvLines(pairB).Count == 0;
                Check("J.TwoConsoles", "concurrent consoles isolate paste and keep identity",
                    gotA == "OnlyForA_8841" && bUntouched
                        && pairA.Window != pairB.Window
                        && WindowProp(pairA.Window, "SAITULS_ACCOUNT_ID") != IntPtr.Zero
                        && WindowProp(pairA.Window, "SAITULS_ACCOUNT_ID") != WindowProp(pairB.Window, "SAITULS_ACCOUNT_ID")
                        && WindowTitle(pairA.Window) == TitleA && WindowTitle(pairB.Window) == TitleB,
                    "a=" + Short(gotA) + " bUntouched=" + bUntouched
                        + " titleA=" + WindowTitle(pairA.Window) + " titleB=" + WindowTitle(pairB.Window)
                        + " foreground=" + (ForegroundStill(pairA.Window) ? "A" : "lost"));

                // Case K: non-foreground console ignores the chord.
                string scope = OracleForegroundScoping(pairA, pairB);
                if (scope != null && scope.StartsWith("BLOCKED:", StringComparison.Ordinal))
                    Blocked("K.ForegroundScoping", "keys reach only the foreground console", scope.Substring(8));
                else
                    Check("K.ForegroundScoping", "keys reach only the foreground console", scope == null,
                        scope ?? "B untouched while A foreground");
            }
        }

        // Case Q: opt-in bridge delivers paste to a VT vendor (legacy path).
        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "bridge"), "vt-render", TitleA, AccountA,
            new Dictionary<string, string> { { "SAI_AG_CONSOLE_INPUT_BRIDGE", "on" } }))
        {
            if (inputUnavailability != null)
                Blocked("Q.BridgePaste", "real foreground keyboard/mouse delivery", "input unavailable: " + inputUnavailability);
            else if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000))
            {
                Blocked("Q.BridgePaste", "explicit bridge paste into VT vendor", "window ownership unavailable");
            }
            else
            {
                string raw = null;
                string got = null;
                string bridgeText = "BridgePaste\u00e4\u00f6\u00fc";
                if (RequireClipboard("Q.BridgePaste", "opt-in bridge delivers exact text to the VT vendor", bridgeText)
                    && ForegroundStill(pair.Window))
                {
                    int before = RawInput(pair).Length;
                    if (Chord(new short[] { 0x11, 0x56 }))
                        got = WaitRawAppend(pair, before, 5000);
                }
                Check("Q.BridgePaste", "opt-in bridge delivers exact text to the VT vendor",
                    got == bridgeText,
                    "got=" + Short(got) + " raw=" + Short(raw)
                        + " foreground=" + (ForegroundStill(pair.Window) ? "owned" : "lost"));
            }
        }

        // Case S: VT vendor without the bridge -- what the TUI actually receives.
        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "vtpaste"), "vt-render", TitleA, AccountA))
        {
            if (inputUnavailability != null)
                Blocked("S.VtPasteDefault", "real foreground keyboard/mouse delivery", "input unavailable: " + inputUnavailability);
            else if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000))
            {
                Blocked("S.VtPasteDefault", "default-path paste into VT vendor", "window ownership unavailable");
            }
            else
            {
                string vt = OracleVtRawChord(pair, "VtNoBridge_77");
                if (vt != null && vt.StartsWith("BLOCKED:", StringComparison.Ordinal))
                    Blocked("S.VtPasteDefault", "default-path VT input", vt.Substring(8));
                else
                    Check("S.VtPasteDefault", "VT vendor receives the raw chord and no hook-injected text",
                        vt == null, vt ?? "raw 0x16 only, clipboard text not injected");
            }
        }

        // Case W: real vendor outcomes. A raw 0x16 is protocol ownership, so
        // every W case below scores the vendor's own composer or clipboard.
        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "w1"), "vt-render", TitleA, AccountA))
        {
            Verdict("W1.VtComposerPasteCtrlV", "Ctrl+V pastes the real clipboard content into the vendor composer",
                OracleVtComposerPaste(pair, VtPastePayload + "_w1_" + RunNonce, new short[] { 0x11, 0x56 }),
                "one paste, exact Unicode payload, no duplicate, no submission");
        }

        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "w2"), "vt-render", TitleA, AccountA))
        {
            Verdict("W2.VtComposerPasteCtrlShiftV", "Ctrl+Shift+V pastes the real clipboard content into the vendor composer",
                OracleVtComposerPaste(pair, VtPastePayload + "_w2_" + RunNonce, new short[] { 0x11, 0x10, 0x56 }),
                "one paste, exact Unicode payload, no duplicate, no submission");
        }

        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "w3"), "vt-render", TitleA, AccountA))
        {
            Verdict("W3.VtComposerMultilinePaste", "a multiline clipboard paste lands whole and submits nothing",
                OracleVtComposerPaste(pair, VtMultilinePayload + "_w3_" + RunNonce, new short[] { 0x11, 0x56 }),
                "both lines, exact order, no trailing submit");
        }

        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "w4"), "vt-render", TitleA, AccountA))
        {
            Verdict("W4.VtCopyWithSelection", "Ctrl+C copies the vendor selection to the real clipboard",
                OracleVtComposerCopy(pair, new short[] { 0x11, 0x43 }),
                "clipboard holds the exact selected text, one copy chord");
        }

        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "w5"), "vt-render", TitleA, AccountA))
        {
            Verdict("W5.VtCopyCtrlShiftC", "Ctrl+Shift+C copies the vendor selection to the real clipboard",
                OracleVtComposerCopy(pair, new short[] { 0x11, 0x10, 0x43 }),
                "same 0x03 chord as Ctrl+C: the console cannot tell them apart, the vendor owns the copy");
        }

        // Case W6: right-click paste. The console supports it only where the
        // conhost line editor is active, so the case measures the mode that
        // supports it and reports the managed plain-classic mode next to it.
        string rightClickLine = null;
        string rightClickManaged = null;
        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "w6a"), "classic-line", TitleA, AccountA))
            rightClickLine = OracleClassicRightClickPaste(pair, "RightClickLine_" + RunNonce, true);
        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "w6b"), "classic-reader", TitleA, AccountA))
            rightClickManaged = OracleClassicRightClickPaste(pair, "RightClickManaged_" + RunNonce, false);
        if (rightClickLine != null && rightClickLine.StartsWith("BLOCKED:", StringComparison.Ordinal))
            Blocked("W6.ClassicRightClickPaste", "right-click pastes in a QuickEdit console where the console supports it",
                rightClickLine.Substring(8) + " (managed plain-classic mode: " + (rightClickManaged ?? "pasted") + ")");
        else
            Check("W6.ClassicRightClickPaste", "right-click pastes in a QuickEdit console where the console supports it",
                rightClickLine == null,
                (rightClickLine ?? "one paste, exact payload")
                    + " | managed plain-classic mode (no ENABLE_LINE_INPUT): " + (rightClickManaged ?? "pasted"));

        // Case X: who owns which chord in each console mode. The owner is
        // read from the observed behaviour, never assumed.
        {
            var matrix = new List<string>();
            bool complete = true;
            bool blockedOwner = false;
            using (var classic = Pair.Start(enginePath, Path.Combine(dir, "x-classic"), "classic-reader", TitleA, AccountA))
            {
                if (classic.Window == IntPtr.Zero || !OwnWindow(classic.Window, 4000))
                {
                    complete = false;
                    blockedOwner = true;
                    matrix.Add("classic=BLOCKED:window ownership unavailable");
                }
                else
                {
                    string classicOwner = OracleOwnershipClassic(classic);
                    if (classicOwner == null) { complete = false; blockedOwner = true; matrix.Add("classic[BLOCKED:no observation]"); }
                    else if (classicOwner.StartsWith("BLOCKED:", StringComparison.Ordinal)) { complete = false; blockedOwner = true; matrix.Add("classic[" + classicOwner + "]"); }
                    else if (classicOwner.StartsWith("FAIL:", StringComparison.Ordinal)) { complete = false; matrix.Add("classic[" + classicOwner + "]"); }
                    else matrix.Add("classic[" + classicOwner + "]");
                }
            }
            using (var vt = Pair.Start(enginePath, Path.Combine(dir, "x-vt"), "vt-render", TitleA, AccountA))
            {
                if (vt.Window == IntPtr.Zero || !OwnWindow(vt.Window, 4000))
                {
                    complete = false;
                    blockedOwner = true;
                    matrix.Add("vt=BLOCKED:window ownership unavailable");
                }
                else
                {
                    string vtOwner = OracleOwnershipVt(vt);
                    if (vtOwner == null) { complete = false; blockedOwner = true; matrix.Add("vt[BLOCKED:no observation]"); }
                    else if (vtOwner.StartsWith("BLOCKED:", StringComparison.Ordinal)) { complete = false; blockedOwner = true; matrix.Add("vt[" + vtOwner + "]"); }
                    else if (vtOwner.StartsWith("FAIL:", StringComparison.Ordinal)) { complete = false; matrix.Add("vt[" + vtOwner + "]"); }
                    else matrix.Add("vt[" + vtOwner + "]");
                }
            }
            // A blocked owner is an unfinished observation, never a pass: the
            // ownership matrix is only complete when every mode was observed.
            if (blockedOwner)
                Blocked("X.ShortcutOwnership", "every console mode has one identified owner per chord",
                    string.Join(" ", matrix.ToArray()));
            else
                Check("X.ShortcutOwnership", "every console mode has one identified owner per chord",
                    complete, string.Join(" ", matrix.ToArray()));
        }

        // Case Y: the paste-trace classifier carries its own red control. A
        // gate that cannot fail is not a gate, and this classifier decides
        // whether a raw control character is ever counted as pasted content.
        Check("Y.PasteClassifierSelfCheck", "the paste oracle refuses a raw chord, a duplicate, foreign input and a line break",
            SelfCheckPasteClassifier(), "classifier self-check");

        // Case Y2: the environment-versus-product discrimination carries its
        // own red control. A gate that cannot fail is not a gate: this one
        // must call a duplicate and a missing payload product defects, and
        // foreign printable input a disturbed desktop.
        Check("Y2.PasteMismatchSelfCheck", "a paste mismatch is scored as a product defect or a disturbed desktop, never as a pass",
            SelfCheckPasteMismatch(), "mismatch self-check");

        // Case T: idle cost of the 25ms controller poll.
        using (var pair = Pair.Start(enginePath, Path.Combine(dir, "idle"), "classic-render", TitleA, AccountA))
        {
            try
            {
                pair.Guard.Refresh();
                TimeSpan before = pair.Guard.TotalProcessorTime;
                Thread.Sleep(1500);
                pair.Guard.Refresh();
                TimeSpan after = pair.Guard.TotalProcessorTime;
                double used = (after - before).TotalMilliseconds;
                Check("T.IdleCost", "idle controller poll stays cheap", used < 150.0, "cpu ms per 1500 ms = " + used.ToString("F1"));
            }
            catch (Exception ex)
            {
                Blocked("T.IdleCost", "idle controller poll stays cheap", ex.Message);
            }
        }

        // Case U: teardown/relaunch -- two sequential pairs both exit cleanly.
        for (int round = 0; round < 2; round++)
        {
            int exitReported;
            using (var pair = Pair.Start(enginePath, Path.Combine(dir, "cycle" + round), "classic-render", TitleA, AccountA))
            {
                pair.Release();
                bool exited = pair.Guard.WaitForExit(8000);
                pair.Guard.Refresh();
                int guardPid = pair.Guard.Id;
                exitReported = exited && pair.Guard.HasExited ? NativeExitCode(guardPid) : -1;
                Check("U.TeardownRelaunch[" + round + "]", "guard + vendor exit together, no orphans",
                    exited && exitReported == 0, "exited=" + exited + " code=" + exitReported + " pid=" + guardPid);
            }
        }

        // Case U2: no vendor copy of this run may outlive the run. An orphan
        // keeps a console window on the operator's desktop, so the leak is a
        // verdict, not a footnote.
        {
            // A released vendor exits on its own within a poll interval, so
            // the count is observed over a short window, not once.
            int orphans = CountVendorOrphans(dir);
            for (int i = 0; i < 30 && orphans > 0; i++) { Thread.Sleep(200); orphans = CountVendorOrphans(dir); }
            Check("U2.NoOrphanVendors", "no vendor copy of this run outlives the run",
                orphans == 0, "orphan vendor processes=" + orphans);
        }

        // Case V: installed binary identity, never a silent pass. The images
        // are compared as images: the PE is parsed, the build specific bytes
        // are identified from the file structure, and every byte outside them
        // must be identical.
        {
            string installed = InstalledEnginePath();
            string staged = StagedEnginePath();
            string other = installed;
            string otherRole = "installed";
            if (installed == null || SamePath(installed, enginePath))
            {
                other = staged;
                otherRole = "staged";
            }
            if (other == null)
            {
                Blocked("V.InstalledBinary", "installed binary is the same program as the tested build",
                    "no installed image at " + (installed ?? "<unset>") + " and no staged image");
            }
            else
            {
                PeInfo primary = ReadPe(enginePath);
                PeInfo secondary = ReadPe(other);
                string detail = CompareImages(primary, secondary);
                string summary = DescribeImages(primary, secondary, otherRole);
                if (detail != null) Check("V.InstalledBinary", "installed binary is the same program as the tested build", false, detail + " | " + summary);
                else Check("V.InstalledBinary", "installed binary is the same program as the tested build", true, summary);
            }
        }
    }

    static bool SamePath(string a, string b)
    {
        if (a == null || b == null) return false;
        try { return string.Equals(Path.GetFullPath(a), Path.GetFullPath(b), StringComparison.OrdinalIgnoreCase); }
        catch { return string.Equals(a, b, StringComparison.OrdinalIgnoreCase); }
    }

    // The evidence behind the identity verdict: origin, architecture,
    // signature, the build metadata that was excluded, and the hashes.
    static string DescribeImages(PeInfo primary, PeInfo secondary, string secondaryRole)
    {
        var parts = new List<string>();
        parts.Add("under-test=" + primary.Path);
        parts.Add(secondaryRole + "=" + secondary.Path);
        parts.Add("machine=0x" + primary.Machine.ToString("X4")
            + (primary.Machine == 0x014C ? "(x86)" : primary.Machine == 0x8664 ? "(x64)" : primary.Machine == 0xAA64 ? "(arm64)" : ""));
        parts.Add("format=" + primary.OptionalMagicName + " subsystem=" + primary.SubsystemName);
        parts.Add("signature=" + primary.Signature);
        parts.Add("version=" + (primary.Version == "" ? "?" : primary.Version) + " product=" + (primary.Product == "" ? "?" : primary.Product));
        parts.Add("sections=[" + string.Join(";", primary.SectionList.ToArray()) + "]");
        parts.Add("build-metadata=PE timestamp at " + primary.TimeStampOffset + " (0x" + primary.TimeStamp.ToString("X8")
            + ") + MVID GUID at " + primary.MvidOffset + " (#GUID heap " + primary.GuidHeapSize + " bytes)");
        parts.Add("raw=" + primary.Sha256 + " / " + secondary.Sha256);
        parts.Add("normalized=" + Sha256Hex(primary.Bytes, BuildMetadataMask(primary)) + " / "
            + Sha256Hex(secondary.Bytes, BuildMetadataMask(secondary)));
        return string.Join(" | ", parts.ToArray());
    }

    sealed class ModeProbe { public uint Mode; public IntPtr ControllerProp, BridgeProp; }

    // Probe: settle a disposable console and report the vendor's recorded mode
    // plus the guard's window properties, all captured while the window lives.
    static ModeProbe CaseModeRestore(string enginePath, string dir, string role, string title, string account, Dictionary<string, string> env)
    {
        using (var pair = Pair.Start(enginePath, dir, role, title, account, env))
        {
            if (pair.Window == IntPtr.Zero) return null;
            var probe = new ModeProbe
            {
                ControllerProp = WindowProp(pair.Window, "SAITULS_CONSOLE_CONTROLLER"),
                BridgeProp = WindowProp(pair.Window, "SAITULS_INPUT_BRIDGE"),
            };
            var sw = Stopwatch.StartNew();
            uint last = 0;
            while (sw.ElapsedMilliseconds < 4000)
            {
                last = VendorMode(pair, 1000);
                // Once the vendor reports readiness, give the controller two
                // poll cycles to act before sampling the settled mode.
                if (Field(pair, "ready") == "1") { Thread.Sleep(300); last = VendorModeSettled(pair); break; }
            }
            probe.Mode = last;
            return last == 0 ? null : probe;
        }
    }

    // ------------------------------------------------- shared acceptance oracles
    //
    // The positive suite and the negative controls call exactly these functions.
    // Null means the oracle accepted the observation; otherwise the string is
    // the independent reason it rejected it. "BLOCKED:" is a physical-condition
    // failure (no interactive desktop, lost foreground), never a product defect.

    static string OracleCopyWithSelection(Pair pair)
    {
        if (inputUnavailability != null) return "BLOCKED:input unavailable: " + inputUnavailability;
        if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000))
            return "BLOCKED:window ownership unavailable";
        if (!ForegroundStill(pair.Window)) return "BLOCKED:foreground changed before dispatch";

        string texrowText = FieldWait(pair, "texrow");
        if (texrowText == null) return "instrumentation missing: texrow";
        int texrow = int.Parse(texrowText);
        // Activate the console input queue first: an unactivated console
        // swallows the first click and the drag selects nothing.
        if (!ActivateConsole(pair)) return "BLOCKED:foreground changed while activating console";
        // A previous copy can leave the line selected; Esc clears it so the
        // fresh drag starts from an unselected baseline.
        ClickClear(pair);
        if (!DragSelect(pair, texrow, 0, CellWidth(KnownText)))
            return "BLOCKED:foreground changed during drag";
        if (!VendorSelected(pair, 2000))
        {
            // A drag that selects an empty region is a mis-aimed drag until
            // proven otherwise: one retry rules it out. A selection that
            // stays empty while the console holds the foreground is a defect.
            if (!DragSelect(pair, texrow, 0, CellWidth(KnownText)))
                return "BLOCKED:foreground changed during the retried drag";
            if (!VendorSelected(pair, 2000)) return "no selection after drag";
        }

        string sentinel = "SentinelBeforeCtrlC_" + RunNonce;
        string error;
        if (!TrySetClipboardText(sentinel, out error)) return "clipboard setup failed: " + error;
        if (!ForegroundStill(pair.Window)) return "BLOCKED:foreground changed before dispatch";
        bool chordOk = !suppressCopyChord && Chord(new short[] { 0x11, 0x43 });       // Ctrl+C
        if (!chordOk) return "copy chord was not dispatched";
        string clip = ClipWait(() => GetClipboardText(), t => t != null && t != sentinel, 3000);
        if (clip == null) return "clipboard read failed after copy";
        string core = clip.EndsWith("\r\n") ? clip.Substring(0, clip.Length - 2) : clip;
        if (core != KnownText)
        {
            // A copy that lost the console mid-flight belongs to a disturbed
            // desktop. The console still in front means the copy is wrong.
            if (!ForegroundStill(pair.Window))
                return "BLOCKED:the console lost the foreground during the copy";
            return "copied text differs len=" + clip.Length + " got=" + Short(core)
                + " formats=" + ClipboardFormats()
                + " screen=" + Short(ReadText(Path.Combine(Path.GetDirectoryName(pair.Recv), "screen.txt")));
        }
        return null;
    }

    // A console window can be foreground while its input queue is still not
    // activated. A neutral click on a blank bottom-left cell activates it
    // without changing rendered text or sending a character.
    //
    // The console is often NOT foreground when the case starts: the window
    // was created by a background process, so the foreground lock does not
    // hand it focus. Requiring foreground as a precondition turned that
    // normal state into a BLOCKED verdict. Activation therefore performs the
    // click (a click grants foreground to the clicked window) and falls back
    // to SetForegroundWindow, then verifies.
    static bool ActivateConsole(Pair pair)
    {
        if (pair.Window == IntPtr.Zero) return false;
        for (int attempt = 0; attempt < 3; attempt++)
        {
            if (!ForegroundStill(pair.Window))
            {
                SetForegroundWindow(pair.Window);
                Thread.Sleep(150);
            }
            // Click only into the console we already own: a blind click at
            // these coordinates would land on whatever window is on top.
            if (!ForegroundStill(pair.Window)) continue;
            WinRect client;
            if (GetClientRect(pair.Window, out client))
            {
                var point = new WinPoint { X = client.Left + 20, Y = client.Bottom - 20 };
                ClientToScreen(pair.Window, ref point);
                SetCursorPos(point.X, point.Y);
                Thread.Sleep(30);
                Mouse(0x0002);
                Thread.Sleep(50);
                Mouse(0x0004);
                Thread.Sleep(120);
            }
            if (ForegroundStill(pair.Window)) return true;
        }
        return false;
    }

    // A single paste must arrive exactly once and must not inject a newline.
    // This is the oracle that rejects suppression, truncation and duplication.
    static string OraclePasteLine(Pair pair, string text)
    {
        if (inputUnavailability != null) return "BLOCKED:input unavailable: " + inputUnavailability;
        if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000))
            return "BLOCKED:window ownership unavailable";
        if (!ForegroundStill(pair.Window)) return "BLOCKED:foreground changed before dispatch";

        string error;
        if (!TrySetClipboardText(text, out error)) return "clipboard setup failed: " + error;
        if (GetClipboardText() != text) return "clipboard stale before dispatch";
        if (!ForegroundStill(pair.Window)) return "BLOCKED:foreground changed before dispatch";

        int before = RawInput(pair).Length;
        if (!Chord(new short[] { 0x11, 0x56 }))       // Ctrl+V
            return "SendInput refused: count=" + lastInputCount + " err=" + lastInputError;

        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < 5000 && RawInput(pair).Length == before) Thread.Sleep(50);
        Thread.Sleep(400);                            // a duplicate would land here
        string raw = RawInput(pair);
        string got = raw.Length > before ? raw.Substring(before) : "";
            string note;
            string verdict = ClassifyPasteTrace(got, text, out note);
            // Foreign printable input in the trace is a disturbed desktop, not
            // a product defect: the environment is on trial, never the product.
            if (verdict != null)
                return verdict.StartsWith("INTERFERENCE:", StringComparison.Ordinal)
                    ? "BLOCKED:" + verdict.Substring("INTERFERENCE:".Length)
                    : verdict;
            lastPasteNote = note;
            return null;
    }

    // What arrived after the paste chord, classified. Pure text in, verdict
    // out, so the oracle carries its own red control: a raw control character
    // is never pasted content, one payload plus chord residue is one clean
    // paste, two payloads are a duplicate paste, and any foreign printable
    // input is interference that must never be scored as a paste.
    static string ClassifyPasteTrace(string got, string text, out string note)
    {
        note = null;
        if (got == null) got = "";
        int count = CountOccurrences(got, text);
        if (count == 0) return "payload not present in input trace len=" + got.Length;
        if (count > 1) return "payload duplicated (" + count + " copies)";
        if (got.IndexOf('\r') >= 0 || got.IndexOf('\n') >= 0) return "paste injected a line break";
        int at = got.IndexOf(text, StringComparison.Ordinal);
        string extra = got.Substring(0, at) + got.Substring(at + text.Length);
        foreach (char c in extra)
        {
            // Printable leftovers are foreign input (a person typing into the
            // disposable console), never part of the pasted content.
            if (c >= ' ') return "INTERFERENCE: foreign printable input in the paste trace len=" + extra.Length;
        }
        if (extra.Length > 0) note = "one paste plus " + extra.Length + " chord control chars";
        return null;
    }

    // The classifier's own red control: every way it must refuse, and the one
    // shape it must accept.
    static bool SelfCheckPasteClassifier()
    {
        string note;
        if (ClassifyPasteTrace("\u0016", "Payload", out note) == null) return false;        // raw chord is not a paste
        if (ClassifyPasteTrace("PayloadPayload", "Payload", out note) == null) return false;// duplicate paste
        if (ClassifyPasteTrace("Payloadx", "Payload", out note) == null) return false;       // foreign input
        if (ClassifyPasteTrace("Payload\r", "Payload", out note) == null) return false;      // line break
        if (ClassifyPasteTrace("Payload", "Payload", out note) != null) return false;       // clean paste
        if (ClassifyPasteTrace("\u0016Payload", "Payload", out note) != null) return false; // chord residue is fine
        return true;
    }

    // The positive suite asks a different question: did the right payload
    // arrive, and if not, whose fault is it. A trace that holds the payload
    // plus foreign printable characters belongs to a disturbed desktop
    // (somebody else was at the keyboard), a repeated payload is a product
    // defect, and a missing payload is a product defect.
    // Null means the payload arrived exactly as expected.
    static string PasteMismatch(string got, string expected, bool multiline)
    {
        if (expected == null) return "FAIL:no expectation";
        if (got == null) return "FAIL:no input arrived";
        string g = multiline ? NormalizeNewlines(got) : got;
        string e = multiline ? NormalizeNewlines(expected) : expected;
        if (g == e) return null;
        int count = CountOccurrences(g, e);
        if (count > 1) return "FAIL:the payload was pasted " + count + " times";
        if (count == 0) return "FAIL:the payload is not in the input trace len=" + g.Length;
        int at = g.IndexOf(e, StringComparison.Ordinal);
        string residue = g.Substring(0, at) + g.Substring(at + e.Length);
        foreach (char c in residue)
        {
            // Printable leftovers are foreign input: a person typing into the
            // disposable console, never part of the pasted content.
            if (c >= ' ') return "BLOCKED:foreign printable input in the paste trace len=" + residue.Length;
        }
        return "FAIL:the input trace holds more than the payload";
    }

    // One paste, one verdict. A lost foreground is a physical condition, so
    // the case is blocked instead of failed.
    static void PasteVerdict(string caseId, string name, string got, string expected, bool multiline, bool foregroundHeld)
    {
        if (!foregroundHeld) { Blocked(caseId, name, "the console lost the foreground during the paste"); return; }
        string reason = PasteMismatch(got, expected, multiline);
        if (reason == null) { Check(caseId, name, true, "the payload arrived exactly once"); return; }
        if (reason.StartsWith("BLOCKED:", StringComparison.Ordinal)) Blocked(caseId, name, reason.Substring(8));
        else Check(caseId, name, false, reason);
    }

    // The discrimination must be able to fail in both directions: a duplicate
    // and a missing payload are product defects, foreign printable input is a
    // disturbed desktop, and a clean trace passes.
    static bool SelfCheckPasteMismatch()
    {
        if (PasteMismatch("Payload", "Payload", false) != null) return false;
        if (!PasteMismatch("PayloadPayload", "Payload", false).StartsWith("FAIL:", StringComparison.Ordinal)) return false;
        if (!PasteMismatch("", "Payload", false).StartsWith("FAIL:", StringComparison.Ordinal)) return false;
        if (!PasteMismatch("Payloadx", "Payload", false).StartsWith("BLOCKED:", StringComparison.Ordinal)) return false;
        if (PasteMismatch("a\r\nb", "a\nb", true) != null) return false;                    // CRLF is the same payload
        if (!PasteMismatch("a\nbx", "a\nb", true).StartsWith("BLOCKED:", StringComparison.Ordinal)) return false;
        return true;
    }

    // Default VT behavior: conhost hands Ctrl+V to the TUI as raw 0x16 and the
    // clipboard text must NOT arrive through any hook.
    static string OracleVtRawChord(Pair pair, string payload)
    {
        if (inputUnavailability != null) return "BLOCKED:input unavailable: " + inputUnavailability;
        if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000))
            return "BLOCKED:window ownership unavailable";
        if (!ActivateConsole(pair)) return "BLOCKED:foreground changed while activating console";

        string error;
        if (!TrySetClipboardText(payload, out error)) return "clipboard setup failed: " + error;
        if (!ForegroundStill(pair.Window)) return "BLOCKED:foreground changed before dispatch";

        int before = RawInput(pair).Length;
        if (!Chord(new short[] { 0x11, 0x56 }))
            return "SendInput refused: count=" + lastInputCount + " err=" + lastInputError;
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < 2500 && RawInput(pair).Length == before) Thread.Sleep(50);
        Thread.Sleep(200);
        string raw = RawInput(pair);
        string got = raw.Length > before ? raw.Substring(before) : "";
        if (CountOccurrences(got, payload) > 0)
            return "clipboard text leaked into VT input while the bridge is off";
        if (got.IndexOf('\u0016') < 0) return "VT vendor did not receive Ctrl+V as raw input";
        return null;
    }

    static string OracleVtPasteOnce(Pair pair, string text)
    {
        if (inputUnavailability != null) return "BLOCKED:input unavailable: " + inputUnavailability;
        if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000))
            return "BLOCKED:window ownership unavailable";
        if (!ActivateConsole(pair)) return "BLOCKED:foreground changed while activating console";

        string error;
        if (!TrySetClipboardText(text, out error)) return "clipboard setup failed: " + error;
        if (GetClipboardText() != text) return "clipboard stale before dispatch";
        if (!ForegroundStill(pair.Window)) return "BLOCKED:foreground changed before dispatch";
        int before = RawInput(pair).Length;
        if (!Chord(new short[] { 0x11, 0x56 })) return "SendInput refused: count=" + lastInputCount;

        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < 2500 && RawInput(pair).Length == before) Thread.Sleep(50);
        Thread.Sleep(300);
        string raw = RawInput(pair);
        string got = raw.Length > before ? raw.Substring(before) : "";
        int count = CountOccurrences(got, text);
        if (count == 0) return "vendor received no pasted text len=" + got.Length;
        if (count > 1) return "vendor received the payload " + count + " times";
        if (got != text) return "payload differs: expected len=" + text.Length + " got len=" + got.Length;
        return null;
    }

    static int CountOccurrences(string haystack, string needle)
    {
        if (string.IsNullOrEmpty(haystack) || string.IsNullOrEmpty(needle)) return 0;
        int count = 0, at = 0;
        while ((at = haystack.IndexOf(needle, at, StringComparison.Ordinal)) >= 0)
        {
            count++;
            at += needle.Length;
        }
        return count;
    }

    // ------------------------------------------------- VT vendor paste outcome
    //
    // A raw 0x16 in the key stream proves protocol ownership, never a paste.
    // The observable of a paste is the vendor's own composer: what the
    // clipboard actually delivered, exactly once, with no submission.

    static string OracleVtComposerPaste(Pair pair, string text, short[] chord)
    {
        if (inputUnavailability != null) return "BLOCKED:input unavailable: " + inputUnavailability;
        if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000)) return "BLOCKED:window ownership unavailable";
        if (!ActivateConsole(pair)) return "BLOCKED:foreground changed while activating console";

        string error;
        if (!TrySetClipboardText(text, out error)) return "clipboard setup failed: " + error;
        if (GetClipboardText() != text) return "clipboard stale before dispatch";
        if (!ForegroundStill(pair.Window)) return "BLOCKED:foreground changed before dispatch";

        int before = RawInput(pair).Length;
        if (!Chord(chord)) return "SendInput refused: count=" + lastInputCount + " err=" + lastInputError;

        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < 6000 && ComposerPastes(pair).Count == 0) Thread.Sleep(50);
        Thread.Sleep(700);                       // a duplicate would land here

        List<string> pastes = ComposerPastes(pair);
        if (pastes.Count == 0) return "vendor composer recorded no paste; the chord delivered no content records=" + ComposerDump(pair);
        if (pastes.Count > 1) return "vendor pasted " + pastes.Count + " times";
        string pasted = UnescapePayload(pastes[0]);
        if (pasted != text) return "pasted payload differs: got len=" + pasted.Length + " expected len=" + text.Length
            + " got=" + Short(pasted) + " records=" + ComposerDump(pair);

        string raw = RawInput(pair);
        string got = raw.Length > before ? raw.Substring(before) : "";
        int chords = CountOccurrences(got, "\u0016");
        if (chords != 1) return "expected exactly one paste chord in the key stream, saw " + chords;
        if (got.IndexOf('\r') >= 0 || got.IndexOf('\n') >= 0) return "paste submitted the composer";
        return null;
    }

    // Copy in a VT vendor: the vendor owns the selection, so Ctrl+A must
    // reach the TUI, the console must NOT select anything, and the copy chord
    // must leave the vendor's own text on the clipboard.
    static string OracleVtComposerCopy(Pair pair, short[] chord)
    {
        if (inputUnavailability != null) return "BLOCKED:input unavailable: " + inputUnavailability;
        if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000)) return "BLOCKED:window ownership unavailable";
        if (!ActivateConsole(pair)) return "BLOCKED:foreground changed while activating console";

        if (!Chord(new short[] { 0x11, 0x41 })) return "SendInput refused: count=" + lastInputCount;
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < 3000 && !ComposerHasRecord(pair, "SELECTED:")) Thread.Sleep(50);
        if (!ComposerHasRecord(pair, "SELECTED:")) return "vendor recorded no selection after Ctrl+A";
        if (VendorSelected(pair, 300)) return "the console took the selection; in VT mode the vendor owns it";

        string sentinel = "SentinelBeforeCtrlC_" + RunNonce;
        string error;
        if (!TrySetClipboardText(sentinel, out error)) return "clipboard setup failed: " + error;
        if (!ForegroundStill(pair.Window)) return "BLOCKED:foreground changed before dispatch";
        int before = RawInput(pair).Length;
        if (!Chord(chord)) return "SendInput refused: count=" + lastInputCount + " err=" + lastInputError;

        string clip = ClipWait(() => GetClipboardText(), t => t != null && t.Length > 0 && t != sentinel, 4000);
        if (clip == null) return "clipboard read failed after copy";
        if (clip != KnownText) return "copied text differs: got=" + Short(clip) + " expected=" + Short(KnownText)
            + " formats=" + ClipboardFormats() + " records=" + ComposerDump(pair);
        List<string> copies = ComposerCopies(pair);
        if (copies.Count != 1) return "vendor recorded " + copies.Count + " copy operations";
        if (UnescapePayload(copies[0]) != KnownText) return "vendor copy record differs: " + Short(copies[0]);

        string raw = RawInput(pair);
        string got = raw.Length > before ? raw.Substring(before) : "";
        int chords = CountOccurrences(got, "\u0003");
        if (chords != 1) return "expected exactly one copy chord in the key stream, saw " + chords;
        return null;
    }

    // Right-click paste. With the conhost line editor active the pasted text
    // lands in the console's input line and reaches the app on Enter; without
    // it there is no line to fill and the console hands nothing over.
    static string OracleClassicRightClickPaste(Pair pair, string text, bool commitLine)
    {
        if (inputUnavailability != null) return "BLOCKED:input unavailable: " + inputUnavailability;
        if (pair.Window == IntPtr.Zero || !OwnWindow(pair.Window, 4000)) return "BLOCKED:window ownership unavailable";
        if (!ActivateConsole(pair)) return "BLOCKED:foreground changed while activating console";
        string error;
        if (!TrySetClipboardText(text, out error)) return "clipboard setup failed: " + error;
        if (GetClipboardText() != text) return "clipboard stale before dispatch";
        if (!ClickClear(pair)) return "BLOCKED:foreground changed while clearing the selection";
        if (VendorSelected(pair, 300)) return "a selection was still active; right-click would copy, not paste";
        if (!ForegroundStill(pair.Window)) return "BLOCKED:foreground changed before dispatch";

        if (!RightClick(pair)) return "SendInput refused: count=" + lastInputCount + " err=" + lastInputError;
        Thread.Sleep(1200);
        if (commitLine && !Tap(0x0D)) return "SendInput refused for Enter";

        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < 5000 && ReadText(pair.Recv).IndexOf(text, StringComparison.Ordinal) < 0) Thread.Sleep(100);
        Thread.Sleep(400);
        string received = ReadText(pair.Recv);
        int count = CountOccurrences(received, text);
        if (count == 0)
        {
            if (ConsoleDialogOpen(pair)) return "BLOCKED:conhost is asking the operator to confirm the paste";
            return "right-click paste produced no input (mode=" + FieldWait(pair, "mode", 500) + " recv=" + Short(received) + ")";
        }
        if (count > 1) return "right-click paste delivered " + count + " copies";
        return null;
    }

    static bool RightClick(Pair pair)
    {
        WinRect client;
        if (!GetClientRect(pair.Window, out client)) return false;
        var point = new WinPoint { X = client.Left + 20, Y = client.Bottom - 20 };
        ClientToScreen(pair.Window, ref point);
        SetCursorPos(point.X, point.Y);
        Thread.Sleep(30);
        if (!Mouse(0x0008)) return false;           // MOUSEEVENTF_RIGHTDOWN
        Thread.Sleep(40);
        if (!Mouse(0x0010)) return false;           // MOUSEEVENTF_RIGHTUP
        Thread.Sleep(120);
        return true;
    }

    // A conhost paste confirmation is a modal dialog in the console process.
    // The test reports it as blocked instead of failing the product.
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);

    static bool ConsoleDialogOpen(Pair pair)
    {
        uint consolePid;
        GetWindowThreadProcessId(pair.Window, out consolePid);
        if (consolePid == 0) return false;
        bool found = false;
        EnumWindows(delegate (IntPtr hwnd, IntPtr lParam)
        {
            uint pid;
            GetWindowThreadProcessId(hwnd, out pid);
            if (pid != consolePid || !IsWindowVisible(hwnd)) return true;
            var name = new StringBuilder(64);
            GetClassName(hwnd, name, name.Capacity);
            if (name.ToString() == "#32770") { found = true; return false; }
            return true;
        }, IntPtr.Zero);
        return found;
    }

    // One oracle result, one verdict: null is a pass, "BLOCKED:" is a
    // physical condition, anything else is a product-level failure.
    static void Verdict(string caseId, string name, string detail, string evidence)
    {
        if (detail == null) Check(caseId, name, true, evidence);
        else if (detail.StartsWith("BLOCKED:", StringComparison.Ordinal)) Blocked(caseId, name, detail.Substring(8));
        else Check(caseId, name, false, detail);
    }

    // Who owns the chords in a classic QuickEdit console: the console
    // synthesizes the paste and owns the selection, so the vendor never
    // touches the clipboard itself.
    static string OracleOwnershipClassic(Pair pair)
    {
        if (inputUnavailability != null) return "BLOCKED:input unavailable: " + inputUnavailability;
        if (!ActivateConsole(pair)) return "BLOCKED:foreground changed while activating console";

        ClickClear(pair);
        string texrowText = FieldWait(pair, "texrow");
        if (texrowText == null) return "BLOCKED:instrumentation missing: texrow";
        if (!DragSelect(pair, int.Parse(texrowText), 0, CellWidth(KnownText)))
            return "BLOCKED:foreground changed during drag";
        bool consoleSelects = VendorSelected(pair, 2000);
        if (!consoleSelects)
        {
            // One retry rules out a mis-aimed drag. An empty selection that
            // survives it, with the console still in front, is a defect.
            if (!DragSelect(pair, int.Parse(texrowText), 0, CellWidth(KnownText)))
                return "BLOCKED:foreground changed during the retried drag";
            consoleSelects = VendorSelected(pair, 2000);
        }

        string sentinel = "SentinelOwnerCopy_" + RunNonce;
        string error;
        if (!TrySetClipboardText(sentinel, out error)) return "FAIL:clipboard setup failed: " + error;
        if (!Chord(new short[] { 0x11, 0x43 })) return "FAIL:SendInput refused";
        string clip = ClipWait(() => GetClipboardText(), t => t != null && t.Length > 0 && t != sentinel, 4000);
        bool consoleCopied = clip != null && clip == KnownText;
        bool vendorCopied = ComposerCopies(pair).Count > 0;

        string payload = "OwnerPaste_" + RunNonce;
        if (!TrySetClipboardText(payload, out error)) return "FAIL:clipboard setup failed: " + error;
        int before = RawInput(pair).Length;
        if (!Chord(new short[] { 0x11, 0x56 })) return "FAIL:SendInput refused";
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < 4000 && RawInput(pair).Length == before) Thread.Sleep(50);
        Thread.Sleep(300);
        string raw = RawInput(pair);
        string got = raw.Length > before ? raw.Substring(before) : "";
        bool consolePasted = got.IndexOf(payload, StringComparison.Ordinal) >= 0;
        bool vendorPasted = ComposerPastes(pair).Count > 0;

        if (!ForegroundStill(pair.Window))
            return "BLOCKED:the console lost the foreground during the ownership case";
        if (!consoleSelects) return "FAIL:no console selection after a mouse drag";
        if (!consoleCopied) return "FAIL:the console did not copy its own selection";
        if (vendorCopied) return "FAIL:the vendor also claims the copy in a classic console";
        if (!consolePasted) return "FAIL:the console did not synthesize the pasted text";
        if (vendorPasted) return "FAIL:the vendor also claims the paste in a classic console";
        return "selection=console copy=console paste=console mouse=console";
    }

    // Who owns the chords in a VT console: the console delivers the chord and
    // the vendor reads the clipboard, owns the selection and owns the copy.
    static string OracleOwnershipVt(Pair pair)
    {
        if (inputUnavailability != null) return "BLOCKED:input unavailable: " + inputUnavailability;
        if (!ActivateConsole(pair)) return "BLOCKED:foreground changed while activating console";

        if (!Chord(new short[] { 0x11, 0x41 })) return "FAIL:SendInput refused";
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < 3000 && !ComposerHasRecord(pair, "SELECTED:")) Thread.Sleep(50);
        if (!ComposerHasRecord(pair, "SELECTED:")) return "FAIL:the vendor received no Ctrl+A";
        bool consoleSelects = VendorSelected(pair, 400);

        string payload = "OwnerVtPaste_" + RunNonce;
        string error;
        if (!TrySetClipboardText(payload, out error)) return "FAIL:clipboard setup failed: " + error;
        int before = RawInput(pair).Length;
        if (!Chord(new short[] { 0x11, 0x56 })) return "FAIL:SendInput refused";
        sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < 4000 && ComposerPastes(pair).Count == 0) Thread.Sleep(50);
        Thread.Sleep(300);
        string raw = RawInput(pair);
        string got = raw.Length > before ? raw.Substring(before) : "";
        bool consolePasted = got.IndexOf(payload, StringComparison.Ordinal) >= 0;
        bool vendorPasted = ComposerPastes(pair).Count == 1;
        bool chordDelivered = got.IndexOf("\u0016", StringComparison.Ordinal) >= 0;

        string sentinel = "SentinelVtCopy_" + RunNonce;
        if (!TrySetClipboardText(sentinel, out error)) return "FAIL:clipboard setup failed: " + error;
        if (!Chord(new short[] { 0x11, 0x43 })) return "FAIL:SendInput refused";
        string clip = ClipWait(() => GetClipboardText(), t => t != null && t.Length > 0 && t != sentinel, 4000);
        bool vendorCopied = ComposerCopies(pair).Count == 1;

        if (!chordDelivered) return "FAIL:the console did not deliver the paste chord";
        if (consoleSelects) return "FAIL:the console holds a selection in VT mode; the vendor must own it";
        if (!vendorPasted) return "FAIL:the vendor did not paste";
        if (consolePasted) return "FAIL:the console synthesized the paste text in VT mode";
        if (!vendorCopied) return "FAIL:the vendor did not copy its selection";
        if (clip != KnownText) return "FAIL:the clipboard does not hold the vendor selection";
        return "selection=vendor copy=vendor paste=vendor mouse=vendor";
    }

    // Keys belong to the live foreground window, not to whichever window the
    // caller intended. Returning WRONG_FOREGROUND before dispatch is the fault
    // detector; the bystander check catches mis-delivery after it.
    static string OracleForegroundScoping(Pair target, Pair bystander)
    {
        if (inputUnavailability != null) return "BLOCKED:input unavailable: " + inputUnavailability;
        if (target.Window == IntPtr.Zero || bystander.Window == IntPtr.Zero)
            return "BLOCKED:window ownership unavailable";
        IntPtr live = GetForegroundWindow();
        if (live != target.Window)
            return "WRONG_FOREGROUND: live=0x" + live.ToInt64().ToString("X")
                + " intended=0x" + target.Window.ToInt64().ToString("X");
        if (VendorSelected(bystander, 300)) return "bystander already selected before dispatch";

        Chord(new short[] { 0x11, 0x41 });             // Ctrl+A
        if (VendorSelected(bystander, 1200)) return "WRONG_FOREGROUND: bystander console received keys";
        if (!VendorSelected(target, 1500)) return "foreground console received no selection";
        return null;
    }

    static string OracleQuickEditRestored(string enginePath, string dir, Dictionary<string, string> env = null)
    {
        ModeProbe probe = CaseModeRestore(enginePath, dir, "classic-render", TitleA, AccountA, env);
        if (probe == null) return "instrumentation missing";
        if ((probe.Mode & QuickEditFlag) == 0) return "QuickEdit not restored mode=0x" + probe.Mode.ToString("X4");
        if ((probe.Mode & (MouseInputFlag | VtInputFlag)) != 0)
            return "unexpected mode bits mode=0x" + probe.Mode.ToString("X4");
        return null;
    }

    static string OracleClipboardWrite(string text)
    {
        string error;
        if (!TrySetClipboardText(text, out error)) return error;
        string got = GetClipboardText(out error);
        if (got == null) return "clipboard read failed: " + error;
        if (got != text) return "CLIPBOARD_STALE: readback differs from the requested value";
        return null;
    }

    // A restore cycle as an oracle result: write, snapshot, restore, and let
    // the restoration verdict be the observable. A restore that cannot be
    // proven is a rejected cycle, never a silent pass. The controlled fault
    // feeds the verifier a wrong expectation, so this cycle must report the
    // mismatch instead of VERIFIED.
    static string OracleClipboardRestoreCycle(string text)
    {
        string error;
        if (!TrySetClipboardText(text, out error)) return "clipboard setup failed: " + error;
        ClipboardSnapshot probe;
        string snapError;
        if (!SnapshotClipboard(out probe, out snapError)) return "clipboard snapshot failed: " + snapError;
        string verdict = RestoreClipboard(probe);
        if (verdict == "VERIFIED") return null;
        return "clipboard restoration not proven: " + verdict;
    }

    static int RunNegativeCase(Func<string> baseline, Func<string> fault)
    {
        string before = baseline();
        Console.WriteLine("  baseline-before: " + (before ?? "PASS"));
        if (before != null) return before.StartsWith("BLOCKED:", StringComparison.Ordinal) ? 2 : 3;

        string rejected = fault();
        Console.WriteLine("  fault-activated: " + (rejected ?? "NOT REJECTED"));
        if (rejected == null) return 1;
        if (rejected.StartsWith("BLOCKED:", StringComparison.Ordinal)) return 2;

        string after = baseline();
        Console.WriteLine("  baseline-after: " + (after ?? "PASS"));
        if (after != null) return after.StartsWith("BLOCKED:", StringComparison.Ordinal) ? 2 : 3;
        return 0;
    }

    static string WithPair(string enginePath, string dir, string role, Dictionary<string, string> env, Func<Pair, string> body)
    {
        using (var pair = Pair.Start(enginePath, dir, role, TitleA, AccountA, env))
        {
            if (pair.Window == IntPtr.Zero) return "BLOCKED:window ownership unavailable";
            return body(pair);
        }
    }

    static string WithTwoPairs(string enginePath, string dirA, string dirB, Func<Pair, Pair, string> body)
    {
        using (var a = Pair.Start(enginePath, dirA, "classic-reader", TitleA, AccountA))
        using (var b = Pair.Start(enginePath, dirB, "classic-reader", TitleB, AccountB))
        {
            if (a.Window == IntPtr.Zero || b.Window == IntPtr.Zero)
                return "BLOCKED:window ownership unavailable";
            return body(a, b);
        }
    }

    // --------------------------------------------------------- negative faults
    //
    // Every control is baseline -> deterministic injected fault -> the SAME
    // oracle must reject -> fault removed -> baseline passes again. A control
    // that merely observes normal behavior (empty clipboard, Ctrl+C without a
    // selection) is not a defect injection and is not counted here.

    // The fault verdict and the clipboard verdict are two different facts.
    // Exit codes: 0 detected, 1 not detected, 2 blocked, 3 harness error,
    // 4 clipboard restoration failure (a harness error for the aggregate).
    static int RunNegative(string fault, string enginePath)
    {
        string root = Path.Combine(Path.GetTempPath(), "saituls-neg-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        ClipboardSnapshot saved;
        string snapshotError;
        bool snapshotOk = SnapshotClipboard(out saved, out snapshotError);
        if (!snapshotOk)
        {
            // Without a restorable clipboard this test would destroy the
            // operator's clipboard. Refuse to run it at all.
            Console.WriteLine("  FAULT_RESULT=HARNESS_ERROR");
            Console.WriteLine("  CLIPBOARD_SAFETY=UNSAFE: snapshot failed: " + snapshotError);
            Console.WriteLine("  CLIPBOARD_RESTORATION=SKIPPED_SNAPSHOT_FAILED");
            if (Environment.GetEnvironmentVariable("SAI_KEEP_TEMP") != "1") { try { Directory.Delete(root, true); } catch { } }
            return 3;
        }

        int detection = 3;
        string restoration = "NOT_RUN";
        try
        {
            int n = 0;
            switch (fault)
            {
                case "COPY_DISPATCH_SUPPRESSED":
                    // Fault: the oracle selects real text but the copy chord is
                    // deliberately never dispatched. A correct oracle must not
                    // accept the untouched clipboard.
                    detection = RunNegativeCase(
                        () => WithPair(enginePath, Path.Combine(root, "copy-base-" + (n++).ToString()), "classic-reader", null, p => OracleCopyWithSelection(p)),
                        () => WithPair(enginePath, Path.Combine(root, "copy-fault-" + (n++).ToString()), "classic-reader", null,
                            p =>
                            {
                                suppressCopyChord = true;
                                try { return OracleCopyWithSelection(p); }
                                finally { suppressCopyChord = false; }
                            }));
                    break;

                case "PASTE_DISPATCH_SUPPRESSED":
                    // Fault: the vendor never consumes console input.
                    detection = RunNegativeCase(
                        () => WithPair(enginePath, Path.Combine(root, "paste-base-" + (n++).ToString()), "classic-reader", null,
                            p => OraclePasteLine(p, "PasteOracle_" + RunNonce)),
                        () => WithPair(enginePath, Path.Combine(root, "paste-fault-" + (n++).ToString()), "classic-reader",
                            new Dictionary<string, string> { { "SAI_TEST_FAULT", "suppress-input" } },
                            p => OraclePasteLine(p, "PasteOracle_" + RunNonce)));
                    break;

                case "CLIPBOARD_READ_RETURNS_STALE_DATA":
                    // Fault: the write path stores a different value than the test
                    // requested; the read-back oracle must reject the stale result.
                    detection = RunNegativeCase(
                        () => OracleClipboardWrite("FreshClip_" + RunNonce),
                        () =>
                        {
                            clipFault = ClipFault.WriteMismatch;
                            try { return OracleClipboardWrite("FreshClip_" + RunNonce); }
                            finally { clipFault = ClipFault.None; }
                        });
                    break;

                case "PASTE_PAYLOAD_TRUNCATED":
                    // Fault: the vendor records only the first four characters.
                    detection = RunNegativeCase(
                        () => WithPair(enginePath, Path.Combine(root, "trunc-base-" + (n++).ToString()), "classic-reader", null,
                            p => OraclePasteLine(p, "TruncatePayload_" + RunNonce)),
                        () => WithPair(enginePath, Path.Combine(root, "trunc-fault-" + (n++).ToString()), "classic-reader",
                            new Dictionary<string, string> { { "SAI_TEST_FAULT", "truncate" } },
                            p => OraclePasteLine(p, "TruncatePayload_" + RunNonce)));
                    break;

                case "PASTE_PAYLOAD_DUPLICATED":
                    // Fault: the vendor records the same line twice.
                    detection = RunNegativeCase(
                        () => WithPair(enginePath, Path.Combine(root, "dup-base-" + (n++).ToString()), "classic-reader", null,
                            p => OraclePasteLine(p, "DuplicatePayload_" + RunNonce)),
                        () => WithPair(enginePath, Path.Combine(root, "dup-fault-" + (n++).ToString()), "classic-reader",
                            new Dictionary<string, string> { { "SAI_TEST_FAULT", "duplicate" } },
                            p => OraclePasteLine(p, "DuplicatePayload_" + RunNonce)));
                    break;

                case "WRONG_FOREGROUND_TARGET":
                    // Fault: the oracle is pointed at B while A still owns the
                    // foreground. The live foreground check must refuse to score it.
                    detection = RunNegativeCase(
                        () => WithTwoPairs(enginePath, Path.Combine(root, "fg-base-a-" + (n++).ToString()), Path.Combine(root, "fg-base-b-" + (n++).ToString()),
                            (a, b) =>
                            {
                                if (!OwnWindow(a.Window, 4000)) return "BLOCKED:window ownership unavailable";
                                return OracleForegroundScoping(a, b);
                            }),
                        () => WithTwoPairs(enginePath, Path.Combine(root, "fg-fault-a-" + (n++).ToString()), Path.Combine(root, "fg-fault-b-" + (n++).ToString()),
                            (a, b) =>
                            {
                                if (!OwnWindow(a.Window, 4000)) return "BLOCKED:window ownership unavailable";
                                return OracleForegroundScoping(b, a);   // intended target is not foreground
                            }));
                    break;

                case "CONTROLLER_DISABLED":
                    // Fault: the scoped controller is disabled; the same mode
                    // oracle must see QuickEdit remain unrestored.
                    detection = RunNegativeCase(
                        () => OracleQuickEditRestored(enginePath, Path.Combine(root, "ctl-base-" + (n++).ToString())),
                        () => OracleQuickEditRestored(enginePath, Path.Combine(root, "ctl-fault-" + (n++).ToString()),
                            new Dictionary<string, string> { { "SAI_AG_CONSOLE_CONTROLLER", "off" } }));
                    break;

                case "SENDINPUT_DELIVERY_REFUSED":
                    // Fault: SendInput is accepted by the kernel but the intended
                    // probe is not foreground, so no test-owned window observes it.
                    detection = RunNegativeCase(
                        () => OracleKeyDelivery(false),
                        () => OracleKeyDelivery(true));
                    break;

                case "CLIPBOARD_RESTORATION_FAILURE":
                    // Fault: the clipboard restore cannot be proven. The
                    // injection is controlled: the real restore still runs and
                    // the operator's clipboard is untouched, but the verifier
                    // is fed a deliberately wrong expectation, so the
                    // restoration must be reported as failed instead of
                    // VERIFIED. The aggregate must never be a pass then.
                    detection = RunNegativeCase(
                        () => OracleClipboardRestoreCycle("RestoreProbe_" + RunNonce),
                        () =>
                        {
                            restorationFault = true;
                            try { return OracleClipboardRestoreCycle("RestoreProbe_" + RunNonce); }
                            finally { restorationFault = false; }
                        });
                    break;

                default:
                    Console.WriteLine("FAIL: unknown fault " + fault);
                    detection = 1;
                    break;
            }
        }
        finally
        {
            restoration = RestoreClipboard(saved);
            Console.WriteLine("  CLIPBOARD_RESTORATION=" + restoration);
            if (Environment.GetEnvironmentVariable("SAI_KEEP_TEMP") != "1") { try { Directory.Delete(root, true); } catch { } }
        }

        Console.WriteLine("  FAULT_RESULT=" + DetectionName(detection));
        Console.WriteLine("  CLIPBOARD_SAFETY=" + (restoration == "VERIFIED" ? "SAFE" : "UNSAFE"));
        // A clipboard that cannot be proven restored is a harness failure for
        // the aggregate, and no further destructive clipboard test may run.
        if (restoration != "VERIFIED") return 4;
        return detection;
    }

    static string DetectionName(int detection)
    {
        switch (detection)
        {
            case 0: return "DETECTED";
            case 1: return "NOT_DETECTED";
            case 2: return "BLOCKED";
            case 4: return "RESTORATION_FAILURE";
            default: return "HARNESS_ERROR";
        }
    }

    // -------------------------------------------------------- mock vendor

    // Roles, each printing the known text at a recorded cursor row:
    //   classic-render 0x00A0 ext+insert, QuickEdit off, mouse off, VT off
    //   mouse-render   0x0090 ext+mouse,  QuickEdit off            (owner branch)
    //   classic-reader 0x00E0 ext+QuickEdit+insert (paste-ready, records lines)
    //   classic-sigint 0x00E0 + Console.CancelKeyPress witness
    //   vt-render      0x0288 like agy.exe, records raw input bytes
    static string ConsoleProcesses()
    {
        var pids = new uint[32];
        uint count = GetConsoleProcessList(pids, (uint)pids.Length);
        var parts = new List<string>();
        for (uint i = 0; i < count && i < pids.Length; i++) parts.Add(pids[i].ToString());
        return string.Join(",", parts.ToArray());
    }

    static string sigRecv;
    static bool RawCtrl(uint type)
    {
        try { File.AppendAllText(sigRecv, "CTRL_EVENT:" + type + "\n"); } catch { }
        return false;   // let the next handler (the runtime) see it too
    }

    // Isolation probe: run the signal mock in its own console with no product
    // guard in front of it. If Ctrl+C still produces no CTRL_C_EVENT, the
    // suppression is the environment/conhost, not the managed launcher.
    static int RunSoloSigint()
    {
        string dir = Path.Combine(Path.GetTempPath(), "saituls-solo-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        string sync = Path.Combine(dir, "sync.txt");
        string release = Path.Combine(dir, "release.txt");
        string recv = Path.Combine(dir, "recv.txt");
        string self = Process.GetCurrentProcess().MainModule.FileName;
        string title = "SIGINT-SOLO-" + Guid.NewGuid().ToString("N").Substring(0, 8);
        string cmd = "\"" + self + "\" --mock-vendor classic-sigint \"" + sync + "\" \""
            + release + "\" \"" + recv + "\"";
        var proc = StartWithNewConsole(cmd, null, title);
        var pair = new Pair { Dir = dir, Sync = sync, ReleasePath = release, Recv = recv, Title = title, Guard = proc };
        try
        {
            var sw = Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < 15000)
            {
                pair.Window = FindConsoleWindow(proc, title);
                if (pair.Window != IntPtr.Zero && AwaitSync(sync, "ready=1", 500)) break;
                Thread.Sleep(100);
            }
            if (pair.Window == IntPtr.Zero) { Console.WriteLine("SOLO: no window"); return 2; }
            if (!OwnWindow(pair.Window, 4000)) { Console.WriteLine("SOLO: no foreground"); return 2; }
            ActivateConsole(pair);
            ClickClear(pair);
            bool sent = Chord(new short[] { 0x11, 0x43 });
            Thread.Sleep(1200);
            bool sigint = ReadText(recv).Contains("SIGINT");
            if (!sigint)
            {
                Chord(new short[] { 0x11, 0x03 });
                Thread.Sleep(800);
            }
            bool sigintBreak = ReadText(recv).Contains("SIGINT");
            Console.WriteLine("SOLO: sent=" + sent + " ctrlC=" + sigint + " ctrlBreak=" + sigintBreak
                + " recv=" + Short(ReadText(recv)));
            return sigint ? 0 : 1;
        }
        finally
        {
            try { File.WriteAllText(release, "release"); } catch { }
            try { if (proc != null && !proc.HasExited) proc.WaitForExit(3000); } catch { }
            try { if (proc != null && !proc.HasExited) proc.Kill(); } catch { }
        }
    }

    static int RunMockVendor(string[] args)
    {
        string role = args[1], sync = args[2], release = args[3], recv = args.Length > 4 ? args[4] : null;
        try
        {
            return RunMockVendorInner(role, sync, release, recv);
        }
        catch (Exception ex)
        {
            try { File.WriteAllText(Path.Combine(Path.GetDirectoryName(sync), "vendor-error.txt"), ex.ToString()); } catch { }
            return 3;
        }
    }

    static int RunMockVendorInner(string role, string sync, string release, string recv)
    {
        File.WriteAllText(Path.Combine(Path.GetDirectoryName(sync), "agy-stages.txt"), "start\n");
        // The mock must render Unicode into the console buffer exactly; the
        // default OEM output code page turns Estonian and Japanese into '?'.
        // conhost transcodes pasted clipboard text through the console INPUT
        // code page, so both sides must be UTF-8 before any I/O.
        try { SetConsoleOutputCP(65001); } catch { }
        try { SetConsoleCP(65001); } catch { }
        try { Console.OutputEncoding = Encoding.UTF8; } catch { }
        try { Console.InputEncoding = Encoding.UTF8; } catch { }
        IntPtr hIn = CreateFileW("CONIN$", 0xC0000000u, 3u, IntPtr.Zero, 3u, 0, IntPtr.Zero);
        IntPtr hOut = CreateFileW("CONOUT$", 0xC0000000u, 3u, IntPtr.Zero, 3u, 0, IntPtr.Zero);
        uint startupMode;
        switch (role)
        {
            case "mouse-render": startupMode = 0x0090; break;
            case "vt-render": startupMode = 0x0288; break;
            case "classic-reader": startupMode = 0x00E5; break;   // no ENABLE_LINE_INPUT: TUI-style
            case "classic-line": startupMode = 0x00E7; break;     // + ENABLE_LINE_INPUT: conhost line editor
            case "classic-sigint": startupMode = 0x00E5; break;
            default: startupMode = 0x00A0; break;
        }
        if (hIn != (IntPtr)(-1) && hIn != IntPtr.Zero) SetConsoleMode(hIn, startupMode);

        if (role == "classic-sigint")
        {
            sigRecv = recv;
            SetConsoleCtrlHandler(RawCtrl, true);
            try { File.AppendAllText(recv, "SIGREADY\n"); } catch { }
            Console.CancelKeyPress += delegate (object sender, ConsoleCancelEventArgs e)
            {
                e.Cancel = true;   // the child stays alive; the interrupt is the fact
                try { File.AppendAllText(recv, "SIGINT\n"); } catch { }
            };
        }

        // Render through WriteConsoleW: the vendor's job is to put exact
        // Unicode into the buffer, and a codepage-mediated Console.WriteLine
        // would fold accents on some console defaults before the test sees it.
        uint written;
        foreach (string line in new[] { KnownText, "SENTINEL-OK" })
            try { WriteConsoleW(hOut, line + "\r\n", (uint)(line.Length + 2), out written, IntPtr.Zero); } catch { }
        try { Console.Out.Flush(); } catch { }
        // Console metrics throw when the host gives no output handle; the
        // vendor's job is recording, so defaults replace a dead query.
        int cols = 120, rows = 30, texrow = 0;
        try { cols = Math.Max(1, Console.WindowWidth); } catch { }
        try { rows = Math.Max(1, Console.WindowHeight); } catch { }
        try { texrow = Math.Max(0, Console.CursorTop - 2); } catch { }

        bool reading = role == "classic-reader" || role == "vt-render" || role == "classic-sigint" || role == "classic-line";
        // A VT vendor TUI (agy.exe included) owns copy and paste itself: the
        // console hands it the raw chord and nothing else. The mock models that
        // vendor so the acceptance can observe a real paste/copy OUTCOME
        // instead of accepting a lone control character as pasted content.
        bool vtVendor = role == "vt-render";
        string composerPath = Path.Combine(Path.GetDirectoryName(sync), "composer.txt");
        string tuiSelection = null;
        // Negative-control faults are deterministic and owned by the test; the
        // vendor records which one is active so rejection is never inferred.
        string testFault = (Environment.GetEnvironmentVariable("SAI_TEST_FAULT") ?? "").Trim().ToLowerInvariant();
        bool suppressInput = testFault == "suppress-input";
        bool truncatePayload = testFault == "truncate";
        bool duplicatePayload = testFault == "duplicate";
        var stop = new ManualResetEventSlim(false);
        StreamWriter rawTrace = null;
        if (reading)
        {
            try
            {
                rawTrace = new StreamWriter(recv + ".raw", false, new UTF8Encoding(false));
                rawTrace.AutoFlush = true;
            }
            catch { rawTrace = null; }

            var reader = new Thread(() =>
            {
                if (role == "classic-line")
                {
                    // A classic console application: it blocks in a line read,
                    // so the conhost line editor runs and the right-click paste
                    // has a line to fill. The line comes back on Enter.
                    while (!stop.IsSet && !File.Exists(release))
                    {
                        try
                        {
                            // Wait for input instead of blocking in the read:
                            // a vendor that never returns from ReadConsoleW
                            // ignores the release file and outlives the test
                            // as an orphan console window.
                            if (hIn != (IntPtr)(-1) && hIn != IntPtr.Zero
                                && WaitForSingleObject(hIn, 200) != 0) continue;
                            var consoleLine = new StringBuilder(1024);
                            uint read = 0;
                            if (ReadConsoleW(hIn, consoleLine, 1024, out read, IntPtr.Zero) && read > 0)
                            {
                                string payload = consoleLine.ToString(0, (int)read).TrimEnd('\r', '\n');
                                if (rawTrace != null) { rawTrace.Write(payload); rawTrace.Flush(); }
                                try { File.AppendAllText(recv, payload + "\n"); } catch { }
                            }
                        }
                        catch (Exception ex)
                        {
                            try { File.AppendAllText(Path.Combine(Path.GetDirectoryName(recv), "reader-error.txt"), ex.ToString() + "\n"); } catch { }
                            Thread.Sleep(50);
                        }
                    }
                    return;
                }
                var line = new StringBuilder();
                bool lastCr = false;
                int rawLine = 0;
                while (!stop.IsSet)
                {
                    try
                    {
                        if (!suppressInput && Console.KeyAvailable)
                        {
                            char c = Console.ReadKey(true).KeyChar;
                            // The fault must be visible in the same witness the
                            // oracle reads: the raw KEY_EVENT trace.
                            if (rawTrace != null && (!truncatePayload || rawLine < 4))
                            {
                                rawTrace.Write(c);
                                if (duplicatePayload) rawTrace.Write(c);
                                rawTrace.Flush();
                            }
                            if (c == '\r' || c == '\n') rawLine = 0; else rawLine++;
                            if (vtVendor)
                            {
                                // Ctrl+V / Ctrl+Shift+V both arrive as 0x16; the
                                // vendor decides paste. Ctrl+A selects (the product
                                // deliberately posts no Ctrl+A while VT input is on),
                                // Ctrl+C copies that selection to the clipboard.
                                if (c == '\u0016')
                                {
                                    int attempts;
                                    bool openFailed;
                                    string pasted = ClipboardTextRaw(out attempts, out openFailed);
                                    Remember(composerPath, "PASTE-READ:attempts=" + attempts
                                        + ":openfail=" + openFailed + ":len=" + pasted.Length);
                                    Remember(composerPath, "PASTE:" + EscapePayload(pasted));
                                }
                                else if (c == '\u0003')
                                {
                                    if (tuiSelection != null)
                                    {
                                        string writeDetail;
                                        bool wrote = SetClipboardTextRaw(tuiSelection, out writeDetail);
                                        Remember(composerPath, "COPY-WRITE:" + (wrote ? "ok" : writeDetail));
                                        if (wrote) Remember(composerPath, "COPY:" + EscapePayload(tuiSelection));
                                    }
                                    else Remember(composerPath, "COPY-NONE");
                                }
                                else if (c == '\u0001')
                                {
                                    tuiSelection = KnownText;
                                    Remember(composerPath, "SELECTED:" + KnownText);
                                }
                            }
                            if (c == '\r')
                            {
                                string payload = truncatePayload && line.Length > 4
                                    ? line.ToString(0, 4) : line.ToString();
                                try { File.AppendAllText(recv, payload + "\n"); } catch { }
                                if (duplicatePayload)
                                {
                                    try { File.AppendAllText(recv, payload + "\n"); } catch { }
                                }
                                line.Clear(); lastCr = true;
                            }
                            else if (c == '\n' && lastCr) { lastCr = false; }
                            else { if (lastCr) lastCr = false; line.Append(c); }
                        }
                        else Thread.Sleep(10);
                    }
                    catch (Exception ex) { try { File.AppendAllText(Path.Combine(Path.GetDirectoryName(recv), "reader-error.txt"), ex.ToString() + "\n"); } catch { } Thread.Sleep(10); }
                }
            });
            reader.IsBackground = true;
            reader.Start();
        }

        // Watchdog: this vendor is disposable. If the main thread is stuck
        // (a console read that never returns, for example), the process must
        // still die instead of leaving a console window on the desktop.
        var watchdog = new Thread(() =>
        {
            var born = Stopwatch.StartNew();
            while (born.ElapsedMilliseconds < 180000)
            {
                try { if (File.Exists(release)) { Thread.Sleep(1500); Environment.Exit(0); } } catch { }
                Thread.Sleep(250);
            }
            Environment.Exit(0);
        });
        watchdog.IsBackground = true;
        watchdog.Start();

        var syncStop = Stopwatch.StartNew();
        int iterations = 0;
        while (syncStop.ElapsedMilliseconds < 90000)
        {
            try
            {
                if (File.Exists(release)) break;
                iterations++;
                uint mode = 0;
                if (hIn != (IntPtr)(-1) && hIn != IntPtr.Zero) GetConsoleMode(hIn, out mode);
                SelectionInfo selInfo;
                GetConsoleSelectionInfo(out selInfo);
                uint selFlags = selInfo.Flags;
                string text = "ready=1;mode=0x" + mode.ToString("X4") + ";sel=" + (((selFlags & 2) != 0) ? 1 : 0)
                    + ";texrow=" + texrow + ";cols=" + cols + ";rows=" + rows
                    + ";fault=" + (testFault.Length == 0 ? "none" : testFault)
                    + ";cp=" + GetConsoleCP() + ";ocp=" + GetConsoleOutputCP()
                    + ";pid=" + Process.GetCurrentProcess().Id + ";procs=" + ConsoleProcesses()
                    + ";time=" + syncStop.ElapsedMilliseconds;
                File.WriteAllText(sync + ".tmp", text);
                // Publish atomically: a reader that lands inside the write
                // must never see a half-written instrument file.
                try
                {
                    if (File.Exists(sync)) File.Replace(sync + ".tmp", sync, null);
                    else File.Move(sync + ".tmp", sync);
                }
                catch
                {
                    try { File.Delete(sync + ".tmp"); } catch { }
                    File.WriteAllText(sync, text);
                }
                // Ground truth for the copy oracle: what the console buffer
                // actually holds, independent of conhost's clipboard encoding.
                var row = new StringBuilder(cols);
                uint readChars;
                if (Environment.GetEnvironmentVariable("SAI_SCREEN_TRACE") == "1"
                    && hOut != (IntPtr)(-1) && hOut != IntPtr.Zero
                    && ReadConsoleOutputCharacterW(hOut, row, (uint)cols, new Coord { X = 0, Y = (short)texrow }, out readChars))
                {
                    File.WriteAllText(Path.Combine(Path.GetDirectoryName(sync), "screen.txt"), row.ToString(0, (int)readChars));
                }
            }
            catch (Exception loopEx)
            {
                try { File.AppendAllText(Path.GetDirectoryName(sync) + "agy-loop-error.txt", loopEx.ToString() + "\n---\n"); } catch { }
            }
            Thread.Sleep(100);
        }
        stop.Set();
        try { if (rawTrace != null) rawTrace.Dispose(); } catch { }
        try { if (hIn != (IntPtr)(-1) && hIn != IntPtr.Zero) CloseHandle(hIn); } catch { }
        return 0;
    }

    // ------------------------------------------------- mock vendor clipboard
    // The mock reads and writes the real OS clipboard through Win32 directly:
    // a TUI does not run on an STA message pump, and the observable of a paste
    // or a copy must be the actual clipboard, not a local round-trip.
    [DllImport("user32.dll", SetLastError = true)] static extern bool OpenClipboard(IntPtr hWndNewOwner);
    [DllImport("user32.dll", SetLastError = true)] static extern bool CloseClipboard();
    [DllImport("user32.dll", SetLastError = true)] static extern bool EmptyClipboard();
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr GetClipboardData(uint uFormat);
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr SetClipboardData(uint uFormat, IntPtr hMem);
    [DllImport("kernel32.dll")] static extern IntPtr GlobalAlloc(uint uFlags, UIntPtr dwBytes);
    [DllImport("kernel32.dll")] static extern IntPtr GlobalFree(IntPtr hMem);
    [DllImport("kernel32.dll")] static extern IntPtr GlobalLock(IntPtr hMem);
    [DllImport("kernel32.dll")] static extern bool GlobalUnlock(IntPtr hMem);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr lstrlenW(IntPtr lpString);
    const uint CF_UNICODETEXT = 13;

    static string ClipboardTextRaw(out int attempts, out bool openFailed)
    {
        attempts = 0;
        openFailed = false;
        for (int attempt = 0; attempt < 40; attempt++)
        {
            attempts++;
            if (!OpenClipboard(IntPtr.Zero)) { openFailed = true; Thread.Sleep(25); continue; }
            try
            {
                IntPtr handle = GetClipboardData(CF_UNICODETEXT);
                // An empty clipboard is a transient state between the writer's
                // EmptyClipboard and its SetClipboardData: wait for the value.
                if (handle == IntPtr.Zero) { Thread.Sleep(25); continue; }
                IntPtr locked = GlobalLock(handle);
                if (locked == IntPtr.Zero) { Thread.Sleep(25); continue; }
                try
                {
                    int length = lstrlenW(locked).ToInt32();
                    return length <= 0 ? "" : Marshal.PtrToStringUni(locked, length);
                }
                finally { GlobalUnlock(handle); }
            }
            catch { return ""; }
            finally { CloseClipboard(); }
        }
        return "";
    }

    static string ClipboardTextRaw()
    {
        int attempts;
        bool openFailed;
        return ClipboardTextRaw(out attempts, out openFailed);
    }

    static bool SetClipboardTextRaw(string text, out string detail)
    {
        detail = "";
        for (int attempt = 0; attempt < 20; attempt++)
        {
            if (!OpenClipboard(IntPtr.Zero)) { detail = "open-failed"; Thread.Sleep(25); continue; }
            IntPtr memory = IntPtr.Zero;
            try
            {
                // The memory is filled BEFORE the clipboard is emptied: a
                // failed write must not leave the operator's clipboard empty.
                int bytes = (text.Length + 1) * 2;
                memory = GlobalAlloc(0x0002 /* GMEM_MOVEABLE */, (UIntPtr)bytes);
                if (memory == IntPtr.Zero) { detail = "alloc-failed"; return false; }
                IntPtr locked = GlobalLock(memory);
                if (locked == IntPtr.Zero) { detail = "lock-failed"; return false; }
                var buffer = Encoding.Unicode.GetBytes(text + "\0");
                Marshal.Copy(buffer, 0, locked, buffer.Length);
                GlobalUnlock(memory);
                if (!EmptyClipboard()) { detail = "empty-failed"; return false; }
                if (SetClipboardData(CF_UNICODETEXT, memory) == IntPtr.Zero)
                {
                    detail = "set-failed err=" + Marshal.GetLastWin32Error();
                    memory = IntPtr.Zero;
                    Thread.Sleep(25);
                    continue;   // the clipboard is empty now: retry the whole write
                }
                memory = IntPtr.Zero;   // ownership transferred to the clipboard
                return true;
            }
            catch (Exception ex) { detail = "exception: " + ex.Message; return false; }
            finally { if (memory != IntPtr.Zero) GlobalFree(memory); CloseClipboard(); }
        }
        return false;
    }

    static void Remember(string path, string record)
    {
        try { File.AppendAllText(path, record + "\n"); } catch { }
    }

    // A record is one line, and a pasted payload may contain line breaks, so
    // the vendor escapes them and the oracle unescapes before comparing.
    static string EscapePayload(string text)
    {
        if (text == null) return "";
        return text.Replace("\\", "\\\\").Replace("\r", "\\r").Replace("\n", "\\n");
    }

    static string UnescapePayload(string text)
    {
        if (text == null) return "";
        var sb = new StringBuilder();
        for (int i = 0; i < text.Length; i++)
        {
            if (text[i] == '\\' && i + 1 < text.Length)
            {
                char next = text[i + 1];
                if (next == 'r') { sb.Append('\r'); i++; continue; }
                if (next == 'n') { sb.Append('\n'); i++; continue; }
                if (next == '\\') { sb.Append('\\'); i++; continue; }
            }
            sb.Append(text[i]);
        }
        return sb.ToString();
    }

    static List<string> ComposerRecords(Pair pair)
    {
        var records = new List<string>();
        string path = Path.Combine(Path.GetDirectoryName(pair.Recv), "composer.txt");
        string text = ReadText(path);
        foreach (string line in text.Split('\n'))
        {
            string trimmed = line.TrimEnd('\r');
            if (trimmed.Length > 0) records.Add(trimmed);
        }
        return records;
    }

    // What the vendor's paste produced, exactly: every PASTE record, in order,
    // with no interpretation and no reconstruction from the key trace.
    static List<string> ComposerPastes(Pair pair)
    {
        var pastes = new List<string>();
        foreach (string record in ComposerRecords(pair))
            if (record.StartsWith("PASTE:", StringComparison.Ordinal)) pastes.Add(record.Substring(6));
        return pastes;
    }

    static List<string> ComposerCopies(Pair pair)
    {
        var copies = new List<string>();
        foreach (string record in ComposerRecords(pair))
            if (record.StartsWith("COPY:", StringComparison.Ordinal)) copies.Add(record.Substring(5));
        return copies;
    }

    static bool ComposerHasRecord(Pair pair, string prefix)
    {
        foreach (string record in ComposerRecords(pair))
            if (record.StartsWith(prefix, StringComparison.Ordinal)) return true;
        return false;
    }

    // The vendor's own record of what it did, verbatim: the witness behind a
    // paste or copy verdict, and the first thing to read when one disagrees.
    static string ComposerDump(Pair pair)
    {
        List<string> records = ComposerRecords(pair);
        if (records.Count == 0) return "<no vendor record>";
        return string.Join(" | ", records.ToArray());
    }

    // ------------------------------------------------- installed binary identity
    //
    // The image is compared as an image: the PE is parsed, the build specific
    // bytes are identified from the file structure itself, and the two files
    // are compared byte for byte outside those bytes. A raw hash difference
    // is never a verdict on its own, and a differing byte that is NOT build
    // metadata is a real code difference.

    sealed class PeInfo
    {
        public string Path;
        public byte[] Bytes;
        public string Error;
        public int Lfanew;
        public ushort Machine;
        public ushort SectionCount;
        public uint TimeStamp;
        public int TimeStampOffset = -1;
        public ushort Characteristics;
        public ushort OptionalMagic;
        public string OptionalMagicName = "";
        public ushort Subsystem;
        public string SubsystemName = "";
        public int CertificateOffset, CertificateSize;
        public readonly List<string> SectionList = new List<string>();
        public int CliRva, CliSize;
        public int MetadataOffset = -1, MetadataSize;
        public int GuidHeapOffset = -1, GuidHeapSize;
        public int MvidOffset = -1;          // file offset of the 16 byte MVID
        public string Sha256 = "";
        public string NormalizedSha256 = "";
        public readonly List<string> Differences = new List<string>();
        public string Version = "", Product = "", Company = "";
        public string Signature = "";
    }

    static ushort U16(byte[] b, int o) { return (ushort)(b[o] | (b[o + 1] << 8)); }
    static uint U32(byte[] b, int o) { return (uint)(b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)); }
    static string Ascii(byte[] b, int o, int len)
    {
        var sb = new StringBuilder();
        for (int i = 0; i < len && b[o + i] != 0; i++) sb.Append((char)b[o + i]);
        return sb.ToString();
    }

    static PeInfo ReadPe(string path)
    {
        var pe = new PeInfo { Path = path };
        try
        {
            pe.Bytes = File.ReadAllBytes(path);
        }
        catch (Exception ex)
        {
            pe.Error = "read failed: " + ex.Message;
            return pe;
        }
        byte[] b = pe.Bytes;
        if (b.Length < 0x40 || b[0] != (byte)'M' || b[1] != (byte)'Z') { pe.Error = "no MZ header"; return pe; }
        pe.Lfanew = (int)U32(b, 0x3C);
        if (pe.Lfanew <= 0 || pe.Lfanew + 24 >= b.Length) { pe.Error = "bad e_lfanew " + pe.Lfanew; return pe; }
        if (b[pe.Lfanew] != (byte)'P' || b[pe.Lfanew + 1] != (byte)'E' || b[pe.Lfanew + 2] != 0 || b[pe.Lfanew + 3] != 0)
        { pe.Error = "no PE signature"; return pe; }

        int coff = pe.Lfanew + 4;
        pe.Machine = U16(b, coff);
        pe.SectionCount = U16(b, coff + 2);
        pe.TimeStamp = U32(b, coff + 4);
        pe.TimeStampOffset = coff + 4;
        pe.Characteristics = U16(b, coff + 20);
        int optSize = U16(b, coff + 16);
        int opt = coff + 20;
        pe.OptionalMagic = U16(b, opt);
        pe.OptionalMagicName = pe.OptionalMagic == 0x10B ? "PE32" : pe.OptionalMagic == 0x20B ? "PE32+" : "0x" + pe.OptionalMagic.ToString("X4");
        pe.Subsystem = U16(b, opt + 68);
        pe.SubsystemName = pe.Subsystem == 3 ? "CONSOLE" : pe.Subsystem == 2 ? "WINDOWS_GUI" : pe.Subsystem.ToString();
        int ddOff = pe.OptionalMagic == 0x20B ? opt + 112 : opt + 96;
        uint ddCount = U32(b, ddOff - 4);
        if (ddCount > 14)
        {
            pe.CertificateOffset = (int)U32(b, ddOff + 0 * 8);
            pe.CertificateSize = (int)U32(b, ddOff + 0 * 8 + 4);
            pe.CliRva = (int)U32(b, ddOff + 14 * 8);
            pe.CliSize = (int)U32(b, ddOff + 14 * 8 + 4);
        }
        pe.Signature = pe.CertificateSize > 0
            ? "signed (certificate table " + pe.CertificateSize + " bytes)"
            : "not signed (certificate table empty)";

        int sectOff = opt + optSize;
        var sections = new List<int[]>();
        for (int i = 0; i < pe.SectionCount; i++)
        {
            int s = sectOff + i * 40;
            if (s + 40 > b.Length) break;
            string name = Ascii(b, s, 8);
            int vaddr = (int)U32(b, s + 12);
            int vsize = (int)U32(b, s + 8);
            int rawptr = (int)U32(b, s + 20);
            int rawsize = (int)U32(b, s + 16);
            uint sc = U32(b, s + 36);
            sections.Add(new[] { vaddr, vsize, rawptr, rawsize });
            pe.SectionList.Add(name + " vaddr=0x" + vaddr.ToString("X8") + " vsize=0x" + vsize.ToString("X8")
                + " raw=0x" + rawptr.ToString("X8") + "+0x" + rawsize.ToString("X8")
                + " chars=0x" + sc.ToString("X8") + ((sc & 0x20000000) != 0 ? " EXEC" : ""));
        }
        Func<int, int> rvaToOffset = delegate (int rva)
        {
            foreach (int[] sec in sections)
                if (rva >= sec[0] && rva < sec[0] + Math.Max(sec[1], sec[3])) return sec[2] + (rva - sec[0]);
            return -1;
        };

        if (pe.CliRva != 0)
        {
            int cli = rvaToOffset(pe.CliRva);
            if (cli > 0 && cli + 16 <= b.Length)
            {
                int metaRva = (int)U32(b, cli + 8);
                int metaSize = (int)U32(b, cli + 12);
                int meta = rvaToOffset(metaRva);
                if (meta > 0 && meta + 20 <= b.Length)
                {
                    pe.MetadataOffset = meta;
                    pe.MetadataSize = metaSize;
                    // Metadata root: signature, version, then the stream table.
                    int versionLength = (int)U32(b, meta + 12);
                    int p = meta + 16 + versionLength;
                    p = (p + 3) & ~3;
                    int streamCount = U16(b, p + 2);
                    int q = p + 4;
                    for (int i = 0; i < streamCount; i++)
                    {
                        if (q + 8 > b.Length) break;
                        int streamOffset = (int)U32(b, q);
                        int streamSize = (int)U32(b, q + 4);
                        int nameAt = q + 8;
                        string streamName = Ascii(b, nameAt, 32);
                        if (streamName == "#GUID" && streamSize >= 16)
                        {
                            pe.GuidHeapOffset = meta + streamOffset;
                            pe.GuidHeapSize = streamSize;
                            pe.MvidOffset = meta + streamOffset;   // the MVID is the first GUID
                        }
                        int nameLen = 0;
                        while (nameAt + nameLen < b.Length && b[nameAt + nameLen] != 0) nameLen++;
                        q = (nameAt + nameLen + 1 + 3) & ~3;
                    }
                }
            }
        }
        try
        {
            var vi = System.Diagnostics.FileVersionInfo.GetVersionInfo(path);
            pe.Version = vi.FileVersion ?? "";
            pe.Product = vi.ProductName ?? "";
            pe.Company = vi.CompanyName ?? "";
        }
        catch { }
        pe.Sha256 = Sha256Hex(pe.Bytes);
        return pe;
    }

    static string Sha256Hex(byte[] data)
    {
        using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(data)).Replace("-", "");
    }

    static string Sha256Hex(byte[] data, List<int[]> mask)
    {
        var copy = (byte[])data.Clone();
        foreach (int[] region in mask)
            for (int i = region[0]; i < region[0] + region[1] && i < copy.Length; i++) copy[i] = 0;
        return Sha256Hex(copy);
    }

    static List<int[]> BuildMetadataMask(PeInfo pe)
    {
        var mask = new List<int[]>();
        if (pe.TimeStampOffset >= 0) mask.Add(new[] { pe.TimeStampOffset, 4 });
        if (pe.MvidOffset >= 0) mask.Add(new[] { pe.MvidOffset, 16 });
        return mask;
    }

    static bool InMask(List<int[]> mask, int offset)
    {
        foreach (int[] region in mask) if (offset >= region[0] && offset < region[0] + region[1]) return true;
        return false;
    }

    // The verdict for a pair of images. Null means the two executables are the
    // same program: identical bytes, or identical everywhere outside the
    // build metadata that the file structure itself identifies.
    static string CompareImages(PeInfo primary, PeInfo other)
    {
        if (primary.Error != null) return "primary image unreadable: " + primary.Error;
        if (other.Error != null) return "other image unreadable: " + other.Error;
        if (primary.Bytes.Length != other.Bytes.Length)
            return "image size differs: " + primary.Bytes.Length + " vs " + other.Bytes.Length;

        List<int[]> mask = BuildMetadataMask(primary);
        List<int[]> otherMask = BuildMetadataMask(other);
        // The same regions must be build metadata in both images.
        if (mask.Count != otherMask.Count) return "build metadata layout differs between the two images";
        for (int i = 0; i < mask.Count; i++)
            if (mask[i][0] != otherMask[i][0] || mask[i][1] != otherMask[i][1])
                return "build metadata offsets differ: " + mask[i][0] + "+" + mask[i][1]
                    + " vs " + otherMask[i][0] + "+" + otherMask[i][1];

        int differences = 0;
        var outside = new List<string>();
        for (int i = 0; i < primary.Bytes.Length; i++)
        {
            if (primary.Bytes[i] == other.Bytes[i]) continue;
            differences++;
            if (!InMask(mask, i) && outside.Count < 8)
                outside.Add("offset " + i + " (0x" + i.ToString("X") + ")");
        }
        string normalizedPrimary = Sha256Hex(primary.Bytes, mask);
        string normalizedOther = Sha256Hex(other.Bytes, otherMask);
        if (outside.Count > 0)
            return "REAL CODE DIFFERENCE: " + differences + " differing bytes outside the build metadata, first: "
                + string.Join(", ", outside.ToArray());
        if (normalizedPrimary != normalizedOther)
            return "normalized images differ (" + normalizedPrimary + " vs " + normalizedOther + ")";
        return null;
    }

    // The engine managed location of the installed broker binary, resolved the
    // way the product resolves it.
    static string InstalledEnginePath()
    {
        string root = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_ROOT");
        if (string.IsNullOrEmpty(root))
            root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), @"SAI\Accounts");
        string exe = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_EXE");
        if (!string.IsNullOrEmpty(exe) && File.Exists(exe)) return exe;
        string inBin = Path.Combine(root, "bin", "sai-accounts.exe");
        if (File.Exists(inBin)) return inBin;
        return null;
    }

    static string StagedEnginePath()
    {
        // The runner lives in tests\ or tests\bin\; the repository root is the
        // ancestor that holds Scripts\accounts\bin. Walk up instead of
        // assuming a fixed depth.
        string dir = Path.GetDirectoryName(
            Path.GetFullPath(Process.GetCurrentProcess().MainModule.FileName));
        for (int i = 0; i < 5 && !string.IsNullOrEmpty(dir); i++)
        {
            string candidate = Path.Combine(dir, "Scripts", "accounts", "bin", "sai-accounts.exe");
            if (File.Exists(candidate)) return candidate;
            string parent = Path.GetDirectoryName(dir);
            if (parent == dir) break;
            dir = parent;
        }
        return null;
    }



    // Live vendor copies started under this suite's own temp root. The path
    // prefix is the suite's own directory, so an unrelated process (the
    // operator's Antigravity, a ZAICODE host) is never counted or touched.
    static int CountVendorOrphans(string suiteRoot)
    {
        int count = 0;
        string prefix = Path.GetFullPath(suiteRoot);
        if (!prefix.EndsWith(Path.DirectorySeparatorChar.ToString(), StringComparison.Ordinal))
            prefix += Path.DirectorySeparatorChar;
        try
        {
            foreach (Process p in Process.GetProcesses())
            {
                try
                {
                    if (p.MainModule == null) continue;
                    string path = p.MainModule.FileName;
                    if (string.IsNullOrEmpty(path)) continue;
                    if (path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) count++;
                }
                catch { }
                finally { try { p.Dispose(); } catch { } }
            }
        }
        catch { }
        return count;
    }

    static int SafeExit(Process p) { try { return p.ExitCode; } catch { return -1; } }

    // The guard is started by raw CreateProcess and adopted through
    // GetProcessById, so the managed Handle/ExitCode path throws once the
    // process is gone; the kernel's own exit code on a fresh query handle is
    // the observable.
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetExitCodeProcess(IntPtr h, out uint code);
    static int NativeExitCode(int pid)
    {
        IntPtr h = OpenProcess(0x0400, false, pid);
        if (h == IntPtr.Zero) return -1;
        try
        {
            uint c;
            return GetExitCodeProcess(h, out c) && c != 259 ? (int)c : -1;
        }
        finally { CloseHandle(h); }
    }

    // Clipboard operations are setup, not evidence. A write is only successful
    // when a read-back returns exactly what was written; a silent exception is
    // CLIPBOARD_LOCKED, an empty read is CLIPBOARD_EMPTY, and a different value
    // is CLIPBOARD_STALE. Tests never continue on an unverified setup.
    enum ClipFault { None, WriteMismatch }
    static ClipFault clipFault;
    static bool suppressCopyChord;   // negative-control injection only
    static bool restorationFault;    // controlled restoration-failure injection only

    sealed class ClipboardSnapshot
    {
        public DataObject Data;
        public string Text;
        public readonly List<string> Formats = new List<string>();
        public readonly List<string> LostFormats = new List<string>();
        // A format name is not a value: the fidelity token is what the
        // restoration has to reproduce, byte for byte where that is possible.
        public readonly Dictionary<string, object> Tokens = new Dictionary<string, object>(StringComparer.Ordinal);
    }

    static bool SnapshotClipboard(out ClipboardSnapshot snap, out string error)
    {
        snap = new ClipboardSnapshot();
        error = null;
        for (int attempt = 0; attempt < 20; attempt++)
        {
            try
            {
                IDataObject source = Clipboard.GetDataObject();
                var copy = new DataObject();
                if (source != null)
                {
                    foreach (string format in source.GetFormats())
                    {
                        try
                        {
                            object value = source.GetData(format, true);
                            if (value == null)
                            {
                                // A format without a readable value carries no
                                // content (Locale and friends). It must come
                                // back, but there is nothing to compare.
                                snap.Formats.Add(format);
                                continue;
                            }
                            copy.SetData(format, false, value);
                            snap.Formats.Add(format);
                            snap.Tokens[format] = FidelityToken(value);
                        }
                        catch { snap.LostFormats.Add(format); }
                    }
                }
                snap.Data = copy;
                try { snap.Text = Clipboard.ContainsText() ? Clipboard.GetText() : null; }
                catch { snap.Text = null; }
                return true;
            }
            catch (Exception ex)
            {
                error = "CLIPBOARD_LOCKED: " + ex.Message;
                Thread.Sleep(25);
            }
        }
        return false;
    }

    // A comparable form of a clipboard value. Null means the value cannot be
    // compared, and then the restoration can never be claimed as verified.
    static object FidelityToken(object value)
    {
        if (value == null) return null;
        if (value is string) return value;
        if (value is byte[])
        {
            var bytes = (byte[])value;
            var copy = new byte[bytes.Length];
            Array.Copy(bytes, copy, bytes.Length);
            return copy;
        }
        if (value is MemoryStream)
        {
            var stream = (MemoryStream)value;
            var bytes = stream.ToArray();
            stream.Position = 0;      // leave the stream usable for the restore
            return bytes;
        }
        return null;
    }

    // Restoration is claimed only after checking the live clipboard: the text
    // value, every value that can be compared, and every format that could be
    // read back at snapshot time. A format that is present but whose value
    // cannot be established is named, never rounded up to VERIFIED.
    static string RestoreClipboard(ClipboardSnapshot snap)
    {
        if (snap == null) return "NOT_REQUIRED";
        // Another application can hold the clipboard for a moment, so a lost
        // value is retried before it is called a loss. A value that never
        // comes back is a real failure and stays one.
        var loss = new List<string>();
        for (int round = 0; round < 3; round++)
        {
            for (int attempt = 0; attempt < 20; attempt++)
            {
                try { Clipboard.SetDataObject(snap.Data, true); break; }
                catch (Exception ex)
                {
                    if (attempt == 19 && round == 2) return "RESTORE_FAILED: " + ex.Message;
                    Thread.Sleep(25);
                }
            }
            loss = new List<string>();
            try
            {
                IDataObject live = Clipboard.GetDataObject();
                var formats = new HashSet<string>(live == null ? new string[0] : live.GetFormats());
                string text = Clipboard.ContainsText() ? Clipboard.GetText() : null;
                // The controlled regression: the real restore already ran, but the
                // verifier is handed a wrong expectation, so the restoration must
                // be reported as failed instead of VERIFIED.
                string expectedText = restorationFault && snap.Text != null ? snap.Text + "<injected-restoration-fault>" : snap.Text;
                if (expectedText != null && !string.Equals(text, expectedText, StringComparison.Ordinal)) loss.Add("text-value");
                foreach (string format in snap.Formats)
                {
                    if (!formats.Contains(format)) { loss.Add(format); continue; }
                    object token = snap.Tokens[format];
                    if (token == null) continue;      // a format without a value
                    object now = null;
                    try { now = live.GetData(format, true); } catch { loss.Add("unreadable:" + format); continue; }
                    object nowToken = FidelityToken(now);
                    if (nowToken == null) { loss.Add("value-unverified:" + format); continue; }
                    if (!SameValue(token, nowToken)) loss.Add("value-differs:" + format);
                }
                foreach (string format in snap.LostFormats) loss.Add("unsnapshotted:" + format);
            }
            catch (Exception ex)
            {
                if (round == 2) return "RESTORE_UNVERIFIED: " + ex.Message;
                Thread.Sleep(50);
                continue;
            }
            if (loss.Count == 0) return "VERIFIED";
            Thread.Sleep(50);
        }
        return "LOSSY(" + string.Join(",", loss.ToArray()) + ")";
    }

    static bool SameValue(object a, object b)
    {
        if (a is string && b is string) return string.Equals((string)a, (string)b, StringComparison.Ordinal);
        if (a is byte[] && b is byte[])
        {
            var x = (byte[])a; var y = (byte[])b;
            if (x.Length != y.Length) return false;
            for (int i = 0; i < x.Length; i++) if (x[i] != y[i]) return false;
            return true;
        }
        return false;
    }

    static string ClipboardFormats()
    {
        try
        {
            IDataObject live = Clipboard.GetDataObject();
            if (live == null) return "none";
            var parts = new List<string>();
            foreach (string format in live.GetFormats())
            {
                string value;
                try { value = live.GetData(format) as string; } catch { value = null; }
                parts.Add(format + (value == null ? "" : "[" + value.Length + ":"
                    + BitConverter.ToString(Encoding.Unicode.GetBytes(value), 0, Math.Min(16, value.Length * 2)) + "]"));
            }
            return string.Join(";", parts.ToArray());
        }
        catch (Exception ex) { return "error:" + ex.Message; }
    }

    static string GetClipboardText(out string error)
    {
        error = null;
        for (int attempt = 0; attempt < 20; attempt++)
        {
            try { return Clipboard.GetText(); }
            catch (Exception ex) { error = "CLIPBOARD_LOCKED: " + ex.Message; Thread.Sleep(25); }
        }
        return null;
    }

    static string GetClipboardText()
    {
        string error;
        return GetClipboardText(out error);
    }

    static bool TrySetClipboardText(string text, out string error)
    {
        error = null;
        // The negative control injects a deterministic wrong write; the read-back
        // check must reject it exactly like a real stale clipboard.
        string written = clipFault == ClipFault.WriteMismatch ? "InjectedStale_" + RunNonce : text;
        for (int attempt = 0; attempt < 20; attempt++)
        {
            try { Clipboard.SetText(written); }
            catch (Exception ex)
            {
                error = "CLIPBOARD_LOCKED: " + ex.Message;
                Thread.Sleep(25);
                continue;
            }
            string readError;
            string got = GetClipboardText(out readError);
            if (got == null) { error = readError ?? "CLIPBOARD_READ_FAILED"; Thread.Sleep(25); continue; }
            if (string.Equals(got, written, StringComparison.Ordinal)) return true;
            error = got.Length == 0
                ? "CLIPBOARD_EMPTY after write len=" + written.Length
                : "CLIPBOARD_STALE: wrote len=" + written.Length + " read len=" + got.Length;
            Thread.Sleep(25);
        }
        if (error == null) error = "CLIPBOARD_WRITE_FAILED";
        return false;
    }

    // Positive cases call this and record a FAIL instead of asserting against a
    // clipboard they failed to prime. Returns false so the case can stop.
    static bool RequireClipboard(string caseId, string name, string text)
    {
        string error;
        if (TrySetClipboardText(text, out error)) return true;
        Check(caseId, name, false, "clipboard setup failed: " + error);
        return false;
    }

    static string Short(string text)
    {
        if (text == null) return "<null>";
        string flat = text.Replace("\r", "\\r").Replace("\n", "\\n");
        return flat.Length > 80 ? flat.Substring(0, 80) + "..." : flat;
    }

    // Raw KEY_EVENT characters observed by the test-owned mock, before any
    // line-input buffering. This is the independent delivery witness.
    static string RawInput(Pair pair)
    {
        string path = pair.Recv + ".raw";
        try
        {
            if (!File.Exists(path)) return "";
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                       FileShare.ReadWrite | FileShare.Delete))
            using (var reader = new StreamReader(stream, new UTF8Encoding(false, false)))
                return reader.ReadToEnd();
        }
        catch { return ""; }
    }

    // Appended characters after `before`, once the trace stops growing. A slow
    // large paste is complete only when it is both present and stable.
    static string WaitRawAppend(Pair pair, int before, int timeoutMs)
    {
        var sw = Stopwatch.StartNew();
        int last = before, stable = 0;
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            int now = RawInput(pair).Length;
            if (now != last) { last = now; stable = 0; }
            else if (now > before && ++stable >= 4) break;
            Thread.Sleep(50);
        }
        string raw = RawInput(pair);
        return raw.Length > before ? raw.Substring(before) : "";
    }

    // Screen columns consumed by a string. A drag selection is addressed in
    // cells, not chars: CJK and kana occupy two cells each, so selecting
    // KnownText.Length columns would clip the tail of the line.
    static int CellWidth(string text)
    {
        int width = 0;
        foreach (char c in text) width += IsWide(c) ? 2 : 1;
        return width;
    }

    static bool IsWide(char c)
    {
        return (c >= 0x1100 && c <= 0x115F) || (c >= 0x2E80 && c <= 0xA4CF)
            || (c >= 0xAC00 && c <= 0xD7A3) || (c >= 0xF900 && c <= 0xFAFF)
            || (c >= 0xFE30 && c <= 0xFE6F) || (c >= 0xFF00 && c <= 0xFF60)
            || (c >= 0xFFE0 && c <= 0xFFE6);
    }

    static string NormalizeNewlines(string text)
    {
        return text == null ? null : text.Replace("\r\n", "\n").Replace('\r', '\n');
    }

    static string ReadText(string path)    {
        try { return File.Exists(path) ? File.ReadAllText(path) : ""; }
        catch { return ""; }   // a vendor mid-write is not evidence of absence
    }

    static string Hash(SHA256 sha, string path)
    {
        return BitConverter.ToString(sha.ComputeHash(File.ReadAllBytes(path))).Replace("-", "");
    }

    static string WindowTitle(IntPtr hwnd)
    {
        var sb = new StringBuilder(256);
        GetWindowTextW(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }

    // ----------------------------------------------------------- platform API

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct StartupInfo
    {
        public uint cb; public string reserved, desktop, title;
        public uint x, y, xSize, ySize, xCount, yCount, fill, flags;
        public ushort show, reservedSize; public IntPtr reservedBytes, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr process, thread; public uint pid, tid; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct SelectionInfo
    {
        public uint Flags; public Coord Anchor; public SmallRect Selection;
    }
    [StructLayout(LayoutKind.Sequential)] struct Coord { public short X, Y; }
    [StructLayout(LayoutKind.Sequential)] struct SmallRect { public short Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct WinPoint { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] struct WinRect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct KeyInput { public short wVk; public short wScan; public uint dwFlags; public uint time; public IntPtr dwExtra; }
    [StructLayout(LayoutKind.Sequential)] struct MouseInput { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtra; }
    // Inner union keeps its natural alignment; INPUT is sequential so the CLR
    // places the union at 8 on x64 and 4 on x86 without hard-coded offsets.
    [StructLayout(LayoutKind.Explicit)] struct InputUnion
    {
        [FieldOffset(0)] public MouseInput mouse;
        [FieldOffset(0)] public KeyInput key;
    }
    [StructLayout(LayoutKind.Sequential)] struct INPUT
    {
        public uint type;
        public InputUnion u;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcess(string app, string cmd, IntPtr procSec, IntPtr threadSec, bool inherit,
        uint flags, IntPtr env, string cwd, ref StartupInfo si, out ProcessInfo pi);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr sec, uint disp, uint flags, IntPtr tmpl);
    [DllImport("kernel32.dll")] static extern bool GetConsoleMode(IntPtr h, out uint mode);
    [DllImport("kernel32.dll")] static extern bool SetConsoleMode(IntPtr h, uint mode);
    [DllImport("kernel32.dll")] static extern uint GetConsoleCP();
    [DllImport("kernel32.dll")] static extern uint GetConsoleOutputCP();
    [DllImport("kernel32.dll")] static extern bool SetConsoleCP(uint cp);
    delegate bool CtrlProc(uint type);
    [DllImport("kernel32.dll")] static extern bool SetConsoleCtrlHandler(CtrlProc handler, bool add);
    [DllImport("kernel32.dll", EntryPoint = "SetConsoleCtrlHandler")] static extern bool SetConsoleCtrlHandler(IntPtr handler, bool add);
    [DllImport("kernel32.dll")] static extern uint GetConsoleProcessList(uint[] pids, uint count);
    [DllImport("kernel32.dll")] static extern bool SetConsoleOutputCP(uint cp);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, EntryPoint = "ReadConsoleOutputCharacterW")]
    static extern bool ReadConsoleOutputCharacterW(IntPtr h, StringBuilder buffer, uint length, Coord at, out uint read);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    static extern bool WriteConsoleW(IntPtr h, string text, uint length, out uint written, IntPtr reserved);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool ReadConsoleW(IntPtr h, StringBuilder buffer, int chars, out uint read, IntPtr inputControl);
    [DllImport("kernel32.dll", SetLastError = true)] static extern uint WaitForSingleObject(IntPtr h, uint milliseconds);
    [DllImport("kernel32.dll")] static extern bool GetConsoleSelectionInfo(out SelectionInfo info);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr OpenInputDesktop(uint dwFlags, bool fInherit, uint dwDesiredAccess);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] static extern bool GetUserObjectInformation(IntPtr hObj, int nIndex, StringBuilder pvInfo, uint nLength, out uint lpnLengthNeeded);
    [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr hDesktop);
    [DllImport("kernel32.dll")] static extern bool ProcessIdToSessionId(uint pid, out uint sessionId);
    [DllImport("user32.dll", SetLastError = true)] static extern uint SendInput(uint n, INPUT[] inputs, int size);
    [DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hwnd, out WinRect rect);
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr hwnd, ref WinPoint point);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr GetPropW(IntPtr hwnd, string name);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowTextW(IntPtr hwnd, StringBuilder text, int max);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern bool EnumWindows(EnumProc proc, IntPtr lParam);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder name, int max);
    delegate bool EnumProc(IntPtr hwnd, IntPtr lParam);

    static Process StartWithNewConsole(string cmdLine, Dictionary<string, string> extraEnv, string title = null)
    {
        // The disposable guard needs no full parent environment. A minimal
        // block is deliberate: it removes any chance an odd inherited entry
        // breaks the launch, and it makes the injected switches the only
        // non-default variables the guard can see.
        var block = new StringBuilder();
        var names = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (string name in new[]
        {
            "PATH", "PATHEXT", "SystemRoot", "SystemDrive", "COMSPEC",
            "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
        })
        {
            string value = Environment.GetEnvironmentVariable(name);
            if (value != null) names[name] = value;
        }
        if (extraEnv != null)
            foreach (var kv in extraEnv) names[kv.Key] = kv.Value;
        foreach (var kv in names) block.Append(kv.Key).Append('=').Append(kv.Value).Append('\0');
        block.Append('\0');

        byte[] raw = Encoding.Unicode.GetBytes(block.ToString());
        GCHandle envPin = GCHandle.Alloc(raw, GCHandleType.Pinned);
        try
        {
            var si = new StartupInfo { cb = (uint)Marshal.SizeOf(typeof(StartupInfo)), title = title };
            ProcessInfo pi;
            if (!CreateProcess(null, cmdLine, IntPtr.Zero, IntPtr.Zero, false,
                0x00000010 /*CREATE_NEW_CONSOLE*/ | 0x00000400 /*CREATE_UNICODE_ENVIRONMENT*/,
                envPin.AddrOfPinnedObject(), null, ref si, out pi))
            {
                Console.WriteLine("CreateProcess failed err=" + Marshal.GetLastWin32Error() + " cmd=" + cmdLine);
                return null;
            }
            CloseHandle(pi.thread);
            return Process.GetProcessById((int)pi.pid);
        }
        finally { envPin.Free(); }
    }

    static IntPtr FindConsoleWindow(Process guard, string expectedTitle)
    {
        IntPtr found = IntPtr.Zero;
        uint guardSession = 0;
        try { ProcessIdToSessionId((uint)guard.Id, out guardSession); } catch { return IntPtr.Zero; }

        EnumWindows(delegate (IntPtr hwnd, IntPtr lParam)
        {
            uint windowPid;
            GetWindowThreadProcessId(hwnd, out windowPid);

            var className = new StringBuilder(64);
            GetClassName(hwnd, className, className.Capacity);
            if (className.ToString() != "ConsoleWindowClass") return true;

            Process owner = null;
            try
            {
                if (windowPid == 0) return true;
                owner = Process.GetProcessById((int)windowPid);
                if (owner.HasExited) return true;
            }
            catch { return true; }
            if (owner == null) return true;

            uint ownerSession = 0;
            if (!ProcessIdToSessionId(windowPid, out ownerSession) || ownerSession != guardSession) return true;

            string ownerName = owner.ProcessName.ToLowerInvariant();
            if (ownerName != "conhost" && windowPid != (uint)guard.Id) return true;

            if (!string.Equals(WindowTitle(hwnd), expectedTitle, StringComparison.Ordinal)) return true;

            // Ambiguous match: refuse to guess.
            if (found != IntPtr.Zero) { found = IntPtr.Zero; return false; }
            found = hwnd;
            return true;
        }, IntPtr.Zero);
        return found;
    }
}
