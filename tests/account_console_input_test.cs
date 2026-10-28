using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

// Real conhost/SendInput/clipboard regression. Only new, owned fake-vendor
// consoles are targeted; never a provider session or an unrelated window.
// Clipboard contents are held only for restoration, never printed/saved.
internal static class AccountConsoleInputTest
{
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    const string MarkerA = "SAI_PASTE_A_T271", MarkerB = "SAI_PASTE_B_T271", ShiftMarker = "SAI_SHIFT_T271", ReaderMarker = "SAI_READER_T271";
    [StructLayout(LayoutKind.Sequential)] struct Point { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] struct Coord { public short X, Y; }
    [StructLayout(LayoutKind.Sequential)] struct Rect { public short L, T, R, B; }
    [StructLayout(LayoutKind.Sequential)] struct Selection { public uint Flags; public Coord Anchor; public Rect Region; }
    [StructLayout(LayoutKind.Sequential)] struct LastInput { public uint Size, Time; }
    [StructLayout(LayoutKind.Sequential)] struct Keyboard { public ushort Key, Scan; public uint Flags, Time; public UIntPtr Extra; }
    [StructLayout(LayoutKind.Sequential)] struct Mouse { public int X, Y; public uint Data, Flags, Time; public UIntPtr Extra; }
    [StructLayout(LayoutKind.Explicit)] struct InputData { [FieldOffset(0)] public Keyboard Key; [FieldOffset(0)] public Mouse Mouse; }
    [StructLayout(LayoutKind.Sequential)] struct Input { public uint Type; public InputData Data; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct ConsoleKey { public int Down; public ushort Repeat, Key, Scan; public char Character; public uint State; }
    [StructLayout(LayoutKind.Explicit, Size = 20)] struct ConsoleInput { [FieldOffset(0)] public ushort Type; [FieldOffset(4)] public ConsoleKey Key; }
    [DllImport("user32.dll")] static extern uint SendInput(uint count, Input[] inputs, int size);
    [DllImport("user32.dll")] static extern uint MapVirtualKey(uint code, uint mode);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LastInput input);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr window, ref Point point);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int id);
    [DllImport("user32.dll")] static extern uint GetClipboardSequenceNumber();
    [DllImport("user32.dll")] static extern bool OpenClipboard(IntPtr owner);
    [DllImport("user32.dll")] static extern bool CloseClipboard();
    [DllImport("user32.dll")] static extern bool EmptyClipboard();
    [DllImport("user32.dll")] static extern uint EnumClipboardFormats(uint format);
    [DllImport("user32.dll")] static extern IntPtr GetClipboardData(uint format);
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr SetClipboardData(uint format, IntPtr data);
    [DllImport("user32.dll")] static extern IntPtr CopyImage(IntPtr image, uint type, int width, int height, uint flags);
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr value);
    [DllImport("kernel32.dll")] static extern UIntPtr GlobalSize(IntPtr memory);
    [DllImport("kernel32.dll")] static extern IntPtr GlobalAlloc(uint flags, UIntPtr bytes);
    [DllImport("kernel32.dll")] static extern IntPtr GlobalLock(IntPtr memory);
    [DllImport("kernel32.dll")] static extern bool GlobalUnlock(IntPtr memory);
    [DllImport("kernel32.dll")] static extern IntPtr GlobalFree(IntPtr memory);
    [DllImport("user32.dll")] static extern IntPtr SendMessageTimeout(IntPtr window, uint message, IntPtr wparam, IntPtr lparam, uint flags, uint timeout, out IntPtr result);
    [DllImport("kernel32.dll")] static extern bool FreeConsole();
    [DllImport("kernel32.dll")] static extern bool AttachConsole(uint pid);
    [DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int id);
    [DllImport("kernel32.dll")] static extern bool GetConsoleMode(IntPtr input, out uint mode);
    [DllImport("kernel32.dll")] static extern bool SetConsoleMode(IntPtr input, uint mode);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateFile(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool ReadConsoleW(IntPtr input, [Out] char[] buffer, uint size, out uint count, IntPtr control);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool WriteConsoleInputW(IntPtr input, ConsoleInput[] records, uint size, out uint count);
    [DllImport("kernel32.dll")] static extern bool GetConsoleSelectionInfo(out Selection selection);
    sealed class Fixture : IDisposable
    {
        public Process Guard; public string Result, Release; public IntPtr Window;
        public void Dispose()
        {
            if (Guard == null) return;
            IntPtr ignored;
            SendMessageTimeout(Window, 0x102, (IntPtr)27, IntPtr.Zero, 2, 500, out ignored);
            File.WriteAllText(Release, "release this owned fixture");
            if (!Guard.WaitForExit(5000)) throw new IOException("Owned fixture did not exit naturally");
            Guard.Dispose(); Guard = null;
        }
    }
    // Restore exact native formats, including bitmap and empty notification
    // formats. OLE reserialization loses some clipboard formats on this host.
    // Original bytes/bitmap stay in memory; no clipboard content is logged.
    sealed class ClipboardScope : IDisposable
    {
        sealed class Entry { public uint Format; public byte[] Bytes; public IntPtr Bitmap; public bool Empty; }
        readonly List<Entry> entries = new List<Entry>();
        readonly Control owner = new Control();
        string ownedText;
        uint capturedSequence;
        public int Count { get { return entries.Count; } }
        static void Open(IntPtr hwnd)
        {
            for (int i = 0; i < 50; i++) { if (OpenClipboard(hwnd)) return; Thread.Sleep(10); }
            throw new IOException("Clipboard busy; no mutation");
        }
        public ClipboardScope()
        {
            Open(owner.Handle);
            try
            {
                uint format = 0;
                while ((format = EnumClipboardFormats(format)) != 0)
                {
                    IntPtr value = GetClipboardData(format);
                    var entry = new Entry { Format = format };
                    if (format == 2 || format == 0x82)
                    {
                        entry.Bitmap = CopyImage(value, 0, 0, 0, 0x2000);
                        if (entry.Bitmap == IntPtr.Zero) throw new IOException("Clipboard bitmap snapshot unavailable; no mutation");
                    }
                    else
                    {
                        // These formats contain GDI or owner-private handles.
                        // Refuse the test before mutation instead of losing one.
                        if (format == 3 || format == 14 || format == 0x80 || format == 0x83 || format == 0x8e)
                            throw new IOException("Unsupported native clipboard format; foreground test deferred");
                        ulong size = GlobalSize(value).ToUInt64();
                        if (size > 128 * 1024 * 1024) throw new IOException("Clipboard snapshot too large; no mutation");
                        entry.Empty = value == IntPtr.Zero || size == 0;
                        if (!entry.Empty)
                        {
                            IntPtr data = GlobalLock(value);
                            if (data == IntPtr.Zero) throw new IOException("Clipboard snapshot unavailable; no mutation");
                            try { entry.Bytes = new byte[(int)size]; Marshal.Copy(data, entry.Bytes, 0, entry.Bytes.Length); }
                            finally { GlobalUnlock(value); }
                        }
                    }
                    entries.Add(entry);
                }
                capturedSequence = GetClipboardSequenceNumber();
            }
            catch { Dispose(); throw; }
            finally { CloseClipboard(); }
        }
        static IntPtr Memory(byte[] bytes)
        {
            IntPtr memory = GlobalAlloc(0x42, (UIntPtr)bytes.Length);
            if (memory == IntPtr.Zero) throw new IOException("Clipboard allocation unavailable");
            IntPtr data = GlobalLock(memory);
            if (data == IntPtr.Zero) { GlobalFree(memory); throw new IOException("Clipboard allocation lock unavailable"); }
            try { Marshal.Copy(bytes, 0, data, bytes.Length); } finally { GlobalUnlock(memory); }
            return memory;
        }
        static string OpenedText()
        {
            IntPtr memory = GetClipboardData(13), data = GlobalLock(memory);
                if (data == IntPtr.Zero) return null;
                try { return Marshal.PtrToStringUni(data); } finally { GlobalUnlock(memory); }
        }
        public string Text()
        {
            Open(owner.Handle);
            try { return OpenedText(); } finally { CloseClipboard(); }
        }
        public void Marker(string text)
        {
            IntPtr memory = Memory(Encoding.Unicode.GetBytes(text + "\0"));
            try {
                Open(owner.Handle);
                try {
                    if (ownedText == null ? capturedSequence != GetClipboardSequenceNumber()
                        : !string.Equals(OpenedText(), ownedText, StringComparison.Ordinal))
                        throw new IOException("External clipboard change; no fixture overwrite");
                    if (!EmptyClipboard() || SetClipboardData(13, memory) == IntPtr.Zero) throw new IOException("Fixture clipboard write unavailable");
                    memory = IntPtr.Zero; ownedText = text;
                } finally { CloseClipboard(); }
            } finally { if (memory != IntPtr.Zero) GlobalFree(memory); }
        }
        public void OwnCopy(string text) { ownedText = text; }
        public bool Restore()
        {
            if (ownedText == null) return true; // The clipboard was never changed.
            // Conhost can synthesize an ANSI/locale format and change the sequence
            // when pasting. Compare only our exact known fixture text; refuse to
            // overwrite an external copy with different contents.
            var staged = new List<IntPtr>();
            try {
                foreach (Entry entry in entries)
                {
                    IntPtr value = entry.Empty ? IntPtr.Zero : entry.Bitmap != IntPtr.Zero
                        ? CopyImage(entry.Bitmap, 0, 0, 0, 0x2000) : Memory(entry.Bytes);
                    if (!entry.Empty && value == IntPtr.Zero) throw new IOException("Clipboard restoration staging unavailable");
                    staged.Add(value);
                }
                Open(owner.Handle);
                try {
                    if (!string.Equals(OpenedText(), ownedText, StringComparison.Ordinal)) return false;
                    if (!EmptyClipboard()) throw new IOException("Clipboard restoration unavailable");
                    for (int i = 0; i < entries.Count; i++)
                    {
                        if (!entries[i].Empty && SetClipboardData(entries[i].Format, staged[i]) == IntPtr.Zero)
                            throw new IOException("Clipboard format restoration unavailable: " + entries[i].Format);
                        if (entries[i].Empty) SetClipboardData(entries[i].Format, IntPtr.Zero);
                        staged[i] = IntPtr.Zero;
                    }
                } finally { CloseClipboard(); }
                return true;
            } finally {
                for (int i = 0; i < staged.Count; i++) if (staged[i] != IntPtr.Zero) {
                    if (entries[i].Bitmap != IntPtr.Zero) DeleteObject(staged[i]); else GlobalFree(staged[i]);
                }
            }
        }
        public void Dispose() { foreach (Entry entry in entries) if (entry.Bitmap != IntPtr.Zero) DeleteObject(entry.Bitmap); entries.Clear(); owner.Dispose(); }
    }
    static readonly List<object> Checks = new List<object>();
    static int Failures;
    static ClipboardScope FixtureClipboard;
    static void Check(string name, bool pass)
    { Checks.Add(new { name = name, result = pass ? "PASS" : "FAIL" }); if (!pass) Failures++; }
    static Dictionary<string, object> Read(string path)
    {
        for (int i = 0; i < 50; i++)
        {
            try { return (Dictionary<string, object>)Json.DeserializeObject(File.ReadAllText(path)); }
            catch (IOException) { } catch (ArgumentException) { }
            Thread.Sleep(20);
        }
        throw new IOException("Fixture observation unavailable");
    }
    static bool Value(Fixture fixture, string key) { return Convert.ToBoolean(Read(fixture.Result)[key]); }
    static int Count(Fixture fixture, string key) { return Convert.ToInt32(Read(fixture.Result)[key]); }
    static Fixture Start(Type engine, string enginePath, string folder, string id, bool generic)
    {
        string account = generic ? "claude:profile-dir:1111111111111111" : "antigravity:windows-user:" + new string(id == "A" ? '7' : '8', 16);
        string title = "[" + id + "] owned input contract";
        string result = Path.Combine(folder, "input-" + id + ".json");
        Environment.SetEnvironmentVariable("SAI_INPUT_FIXTURE", "1");
        Environment.SetEnvironmentVariable("SAI_INPUT_RESULT", result);
        Environment.SetEnvironmentVariable("SAI_INPUT_RELEASE", result + ".release");
        Environment.SetEnvironmentVariable("SAI_INPUT_MODE", id == "N" ? "455" : "648");
        // The global compatibility hooks are opt-in since the P0 input/FPS
        // report; this native acceptance run is the explicit opt-in.
        Environment.SetEnvironmentVariable("SAI_AG_CONSOLE_INPUT_BRIDGE", "1");
        ProcessStartInfo start;
        if (generic)
            start = new ProcessStartInfo(enginePath, "console-title-guard --title64 "
                + Convert.ToBase64String(Encoding.UTF8.GetBytes(title)) + " --account " + account
                + " -- \"" + Path.Combine(folder, "bin", "agy.exe") + "\" --dangerously-skip-permissions") { UseShellExecute = true, WorkingDirectory = folder };
        else
            start = (ProcessStartInfo)engine.GetMethod("AntigravityLaunch", BindingFlags.NonPublic | BindingFlags.Static)
                .Invoke(null, new object[] { folder, account, id == "A" ? "AG7" : "AG8" });
        start.WindowStyle = ProcessWindowStyle.Hidden;
        var fixture = new Fixture { Guard = Process.Start(start), Result = result, Release = result + ".release" };
        fixture.Window = (IntPtr)Convert.ToInt64(Read(result)["window"]);
        return fixture;
    }
    static void Focus(Fixture fixture)
    {
        ShowWindow(fixture.Window, 5); SetForegroundWindow(fixture.Window); Thread.Sleep(100);
        if (GetForegroundWindow() != fixture.Window) throw new IOException("Instrument unavailable: own fixture is not foreground");
        FreeConsole(); if (!AttachConsole((uint)fixture.Guard.Id)) throw new IOException("Owned fixture attach failed");
    }
    static void Keys(Fixture fixture, int key, bool shift)
    {
        if (GetForegroundWindow() != fixture.Window) throw new IOException("User changed focus; no keys sent");
        var input = new List<Input>();
        foreach (int vk in shift ? new[] { 0x11, 0x10, key } : new[] { 0x11, key })
            input.Add(new Input { Type = 1, Data = new InputData { Key = new Keyboard { Key = (ushort)vk, Scan = (ushort)MapVirtualKey((uint)vk, 0) } } });
        foreach (int vk in shift ? new[] { key, 0x10, 0x11 } : new[] { key, 0x11 })
            input.Add(new Input { Type = 1, Data = new InputData { Key = new Keyboard { Key = (ushort)vk, Scan = (ushort)MapVirtualKey((uint)vk, 0), Flags = 2 } } });
        if (SendInput((uint)input.Count, input.ToArray(), Marshal.SizeOf(typeof(Input))) != input.Count)
            throw new IOException("Native keyboard instrument unavailable");
        Thread.Sleep(250);
    }
    static void PlainKey(Fixture fixture, int key)
    {
        if (GetForegroundWindow() != fixture.Window) throw new IOException("User changed focus; no plain control sent");
        Input[] input = {
            new Input { Type = 1, Data = new InputData { Key = new Keyboard { Key = (ushort)key, Scan = (ushort)MapVirtualKey((uint)key, 0) } } },
            new Input { Type = 1, Data = new InputData { Key = new Keyboard { Key = (ushort)key, Scan = (ushort)MapVirtualKey((uint)key, 0), Flags = 2 } } }
        };
        if (SendInput(2, input, Marshal.SizeOf(typeof(Input))) != 2) throw new IOException("Plain keyboard control unavailable");
        Thread.Sleep(200);
    }
    static uint Flags() { Selection selected; if (!GetConsoleSelectionInfo(out selected)) throw new IOException("Selection unavailable"); return selected.Flags; }
    static void ProveReader(Fixture fixture)
    {
        FreeConsole(); if (!AttachConsole((uint)fixture.Guard.Id)) throw new IOException("Reader fixture attach failed");
        var keys = new List<ConsoleInput>();
        foreach (char c in ReaderMarker) keys.Add(new ConsoleInput { Type = 1, Key = new ConsoleKey { Down = 1, Repeat = 1, Character = c } });
        uint written; IntPtr input = CreateFile("CONIN$", 0xc0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
        try {
            if (!WriteConsoleInputW(input, keys.ToArray(), (uint)keys.Count, out written) || written != keys.Count)
                throw new IOException("Reader instrument injection unavailable: " + Marshal.GetLastWin32Error());
        } finally { CloseHandle(input); }
        var timer = Stopwatch.StartNew();
        while (!Value(fixture, "readerMarker") && timer.ElapsedMilliseconds < 1000) Thread.Sleep(20);
        if (!Value(fixture, "readerMarker")) throw new IOException("Fixture native console reader positive control failed");
        FreeConsole();
    }
    static void PasteMarker(string text) { FixtureClipboard.Marker(text); }
    static Input Pointer(int x, int y, uint buttons)
    {
        int left = GetSystemMetrics(76), top = GetSystemMetrics(77), width = GetSystemMetrics(78), height = GetSystemMetrics(79);
        return new Input { Type = 0, Data = new InputData { Mouse = new Mouse {
            X = (int)(((long)x - left) * 65535 / (width - 1)), Y = (int)(((long)y - top) * 65535 / (height - 1)),
            Flags = 1 | 0x8000 | 0x4000 | buttons } } };
    }
    static void Drag(Fixture fixture)
    {
        if (GetForegroundWindow() != fixture.Window) throw new IOException("User changed focus; no mouse input sent");
        Point first = new Point { X = 8, Y = 8 }, last = new Point { X = 160, Y = 8 };
        ClientToScreen(fixture.Window, ref first); ClientToScreen(fixture.Window, ref last);
        Input[] down = { Pointer(first.X, first.Y, 0), Pointer(first.X, first.Y, 2) };
        SendInput((uint)down.Length, down, Marshal.SizeOf(typeof(Input))); Thread.Sleep(150);
        Input[] move = { Pointer(last.X, last.Y, 0), Pointer(last.X, last.Y, 4) };
        SendInput((uint)move.Length, move, Marshal.SizeOf(typeof(Input))); Thread.Sleep(150);
    }
    static void TrustChecks(Type engine, string folder)
    {
        MethodInfo ensure = engine.GetMethod("EnsureAntigravityWorkdirTrust", BindingFlags.NonPublic | BindingFlags.Static);
        if (ensure == null) { Check("vendor selected-workdir trust implemented", false); return; }
        string target = Path.Combine(folder, "settings", "settings.json"), workdir = Path.Combine(folder, "Project spaces üõ");
        Directory.CreateDirectory(workdir); Directory.CreateDirectory(Path.GetDirectoryName(target));
        File.WriteAllText(target, "{\"model\":\"keep-model\",\"unknown\":{\"enabled\":false,\"count\":17},\"trustedWorkspaces\":[\"C:\\\\already\"]}");
        string outcome = (string)ensure.Invoke(null, new object[] { target, workdir });
        var settings = Read(target); var trusted = (object[])settings["trustedWorkspaces"];
        Check("trust appends exact Unicode directory and retains existing entries", outcome == "applied" && trusted.Length == 2 && (string)trusted[1] == workdir);
        var unknown = (Dictionary<string, object>)settings["unknown"];
        Check("trust preserves model and unknown settings", (string)settings["model"] == "keep-model" && !Convert.ToBoolean(unknown["enabled"]) && Convert.ToInt32(unknown["count"]) == 17);
        byte[] bytes = File.ReadAllBytes(target); DateTime written = File.GetLastWriteTimeUtc(target);
        outcome = (string)ensure.Invoke(null, new object[] { target, workdir });
        Check("repeated trust is byte and timestamp no-op", outcome == "already_correct" && Convert.ToBase64String(bytes) == Convert.ToBase64String(File.ReadAllBytes(target)) && written == File.GetLastWriteTimeUtc(target));
        File.WriteAllText(target, "{broken JSON"); bytes = File.ReadAllBytes(target);
        outcome = (string)ensure.Invoke(null, new object[] { target, workdir });
        Check("invalid vendor settings preserved for safe fallback", outcome == "unavailable" && Convert.ToBase64String(bytes) == Convert.ToBase64String(File.ReadAllBytes(target)));
        File.WriteAllText(target, "{\"trustedWorkspaces\":{\"wrong\":true}}"); bytes = File.ReadAllBytes(target);
        outcome = (string)ensure.Invoke(null, new object[] { target, workdir });
        Check("unsupported trust type never overwritten", outcome == "invalid_settings" && Convert.ToBase64String(bytes) == Convert.ToBase64String(File.ReadAllBytes(target)));
        // Missing settings belong to the vendor. The engine must not create the
        // file, its directory, or a defaults-only document that pre-empts the
        // official CLI's own first run (the "Welcome to Antigravity" screen).
        string vendorHome = Path.Combine(folder, "vendor-first-run");
        string missingTarget = Path.Combine(vendorHome, ".gemini", "antigravity-cli", "settings.json");
        File.Delete(target);
        outcome = (string)ensure.Invoke(null, new object[] { missingTarget, workdir });
        Check("missing vendor settings stay missing, set to vendor first run", outcome == "missing_settings" && !File.Exists(missingTarget));
        Check("missing settings never create the vendor config directory", !Directory.Exists(vendorHome));
        // A settings path whose parent is not a directory cannot be created and
        // must be reported without a partial file.
        string blockedParent = Path.Combine(folder, "blocked-parent");
        File.WriteAllText(blockedParent, "not a directory");
        string blockedTarget = Path.Combine(blockedParent, "settings.json");
        outcome = (string)ensure.Invoke(null, new object[] { blockedTarget, workdir });
        Check("unwritable settings location is reported with nothing created", outcome == "missing_settings" && !File.Exists(blockedTarget));
        File.WriteAllText(target, "{\"model\":\"keep-model\",\"trustedWorkspaces\":[]}");
        bytes = File.ReadAllBytes(target); outcome = (string)ensure.Invoke(null, new object[] { target, "relative-path" });
        Check("unavailable workdir changes nothing", outcome == "workdir_unavailable" && Convert.ToBase64String(bytes) == Convert.ToBase64String(File.ReadAllBytes(target)));
        // A vendor that keeps rewriting its own preferences while trust is being
        // applied must never lose a field and must never leave a staged document
        // behind: the write is atomic, so a reader sees one whole document.
        bool stop = false, vendorLost = false;
        var vendor = new Thread(delegate() {
            while (!stop)
            {
                try
                {
                    string staged = target + ".vendor-" + Guid.NewGuid().ToString("N");
                    File.WriteAllText(staged, "{\"vendorMarker\":\"alive\",\"trustedWorkspaces\":[]}");
                    var map = (Dictionary<string, object>)Json.DeserializeObject(File.ReadAllText(staged));
                    if (!map.ContainsKey("vendorMarker") || !map.ContainsKey("trustedWorkspaces")) vendorLost = true;
                    File.Replace(staged, target, null);
                }
                catch (IOException) { } catch (ArgumentException) { }
            }
        });
        vendor.IsBackground = true; vendor.Start();
        var concurrent = Stopwatch.StartNew();
        while (concurrent.ElapsedMilliseconds < 1200)
        {
            outcome = (string)ensure.Invoke(null, new object[] { target, workdir });
            if (!(outcome == "applied" || outcome == "already_correct" || outcome == "busy"
                || outcome == "unavailable" || outcome == "missing_settings" || outcome == "invalid_settings")) break;
        }
        stop = true; vendor.Join(2000);
        var settled = Read(target);
        Check("concurrent vendor writer loses neither preference nor trust",
            !vendorLost && (string)settled["vendorMarker"] == "alive" && (settled["trustedWorkspaces"] as object[]) != null);
        Check("no staged settings file is left behind",
            Directory.GetFiles(Path.GetDirectoryName(target), "*.sai-new-*").Length == 0);
    }
    static int FakeVendor(string[] args)
    {
        IntPtr input = GetStdHandle(-10); uint requested = uint.Parse(Environment.GetEnvironmentVariable("SAI_INPUT_MODE"));
        if (!SetConsoleMode(input, requested)) return 2;
        Console.WriteLine("SAI SELECTION FIXTURE: owned console only");
        Console.WriteLine("No provider, no task, no network, no registry writes");
        string path = Environment.GetEnvironmentVariable("SAI_INPUT_RESULT"), release = Environment.GetEnvironmentVariable("SAI_INPUT_RELEASE");
        var tape = new StringBuilder(); object sync = new object(); int copy = 0, paste = 0, select = 0; string readerError = null;
        var reader = new Thread(delegate() {
            try { var chars = new char[1]; uint read;
                while (ReadConsoleW(input, chars, 1, out read, IntPtr.Zero)) { if (read == 0) continue; int c = chars[0]; lock (sync) {
                if (c == 3) copy++; if (c == 22) paste++; if (c == 1) select++;
                tape.Append((char)c); if (tape.Length > 512) tape.Remove(0, tape.Length - 512);
            } } } catch (Exception e) { lock (sync) { readerError = e.GetType().Name; } }
        }); reader.IsBackground = true; reader.Start();
        var time = Stopwatch.StartNew();
        while (!File.Exists(release) && time.ElapsedMilliseconds < 30000)
        {
            uint mode; GetConsoleMode(input, out mode);
            lock (sync)
            {
                // Persist only fixed marker presence/counters, no received text.
                string value = tape.ToString();
                string staged = path + ".tmp";
                try {
                    File.WriteAllText(staged, Json.Serialize(new { window = GetConsoleWindow().ToInt64(), mode = mode,
                        markerA = value.Contains(MarkerA), markerB = value.Contains(MarkerB), shiftMarker = value.Contains(ShiftMarker), readerMarker = value.Contains(ReaderMarker), plainKey = value.Contains("9"),
                        copy = copy, paste = paste, select = select,
                        fixed_args = args.Length == 1 && args[0] == "--dangerously-skip-permissions", sid = WindowsIdentity.GetCurrent().User.Value,
                        reader_error = readerError }));
                    if (File.Exists(path)) File.Replace(staged, path, null); else File.Move(staged, path);
                } catch (IOException) { }
            }
            Thread.Sleep(20);
        }
        return 0;
    }
    [STAThread] static int Main(string[] args)
    {
        if (Environment.GetEnvironmentVariable("SAI_INPUT_FIXTURE") == "1") return FakeVendor(args);
        if (args.Length == 3 && args[0] == "--trust-only")
        {
            Type trustEngine = Assembly.LoadFile(Path.GetFullPath(args[1])).GetType("SaiAccounts");
            TrustChecks(trustEngine, Path.GetFullPath(args[2]));
            Console.WriteLine(Json.Serialize(new { checks = Checks, failures = Failures }));
            return Failures == 0 ? 0 : 1;
        }
        bool menuOnly = args.Length == 3 && args[0] == "--native-menu-control";
        if (menuOnly) args = new[] { args[1], args[2] };
        var output = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true };
        IntPtr previousForeground = GetForegroundWindow(); Point cursor; GetCursorPos(out cursor);
        Fixture a = null, b = null, generic = null, normal = null;
        var report = new Dictionary<string, object>();
        try
        {
            string enginePath = Path.GetFullPath(args[0]), folder = Path.GetFullPath(args[1]);
            Directory.CreateDirectory(Path.Combine(folder, "bin"));
            File.Copy(Assembly.GetExecutingAssembly().Location, Path.Combine(folder, "bin", "agy.exe"), true);
            Environment.SetEnvironmentVariable("SAI_ACCOUNTS_ROOT", folder);
            Type engine = Assembly.LoadFile(enginePath).GetType("SaiAccounts");
            a = Start(engine, enginePath, folder, "A", false);
            b = Start(engine, enginePath, folder, "B", false);
            generic = Start(engine, enginePath, folder, "G", true);
            ProveReader(generic); report["native_reader_control"] = "PASS";
            if (menuOnly)
            {
                FixtureClipboard = new ClipboardScope(); report["saved_clipboard_format_count"] = FixtureClipboard.Count;
                FreeConsole(); if (!AttachConsole((uint)generic.Guard.Id)) throw new IOException("Menu fixture attach failed");
                PasteMarker(ShiftMarker); IntPtr menuIgnored;
                SendMessageTimeout(generic.Window, 0x112, (IntPtr)0xfff1, IntPtr.Zero, 2, 500, out menuIgnored);
                var timer = Stopwatch.StartNew(); while (!Value(generic, "shiftMarker") && timer.ElapsedMilliseconds < 1000) Thread.Sleep(20);
                Check("native menu paste reaches VT input reader", Value(generic, "shiftMarker") && Count(generic, "mode") == 648);
                SendMessageTimeout(generic.Window, 0x112, (IntPtr)0xfff5, IntPtr.Zero, 2, 500, out menuIgnored);
                Check("native menu selects console output without changing input mode", (Flags() & 2) != 0 && Count(generic, "mode") == 648);
                SendMessageTimeout(generic.Window, 0x112, (IntPtr)0xfff0, IntPtr.Zero, 2, 500, out menuIgnored);
                string text = FixtureClipboard.Text(); bool menuCopied = text != null && text.Contains("SAI SELECTION FIXTURE");
                if (menuCopied) FixtureClipboard.OwnCopy(text);
                Check("native menu copies selected output", menuCopied && Flags() == 0);
                report["checks"] = Checks; report["failures"] = Failures;
                return Failures == 0 ? 0 : 1;
            }
            var idle = Stopwatch.StartNew(); var last = new LastInput { Size = (uint)Marshal.SizeOf(typeof(LastInput)) };
            while (idle.ElapsedMilliseconds < 15000) { GetLastInputInfo(ref last); if (unchecked((uint)Environment.TickCount - last.Time) >= 1500) break; Thread.Sleep(50); }
            if (unchecked((uint)Environment.TickCount - last.Time) < 1500) throw new IOException("User input active; foreground test deferred");
            Focus(generic); PlainKey(generic, '9');
            report["plain_keyboard_control"] = Value(generic, "plainKey") ? "PASS" : "UNVERIFIED";
            report["plain_control_observation"] = Read(generic.Result);
            if (!Value(generic, "plainKey")) throw new IOException("SendInput plain-key positive control unavailable; no keyboard subject verdict");
            normal = Start(engine, enginePath, folder, "N", true); Focus(normal); Keys(normal, 'A', false);
            bool goodControl = (Flags() & 2) != 0;
            report["known_good_native_control"] = goodControl ? "PASS" : "UNVERIFIED";
            IntPtr clearControl; SendMessageTimeout(normal.Window, 0x102, (IntPtr)27, IntPtr.Zero, 2, 500, out clearControl);
            if (!goodControl) throw new IOException("Normal-input Ctrl+A positive control unavailable; no keyboard subject verdict");
            FixtureClipboard = new ClipboardScope(); report["saved_clipboard_format_count"] = FixtureClipboard.Count;
            Focus(a); PasteMarker(MarkerA); Keys(a, 'V', false);
            Check("Ctrl+V native text paste in VT input", Value(a, "markerA") && Count(a, "paste") == 0);
            Keys(a, 'A', false); Check("Ctrl+A selects console output", (Flags() & 2) != 0 && Count(a, "select") == 0);
            Keys(a, 'C', false); string copiedText = FixtureClipboard.Text(); bool copied = copiedText != null && copiedText.Contains("SAI SELECTION FIXTURE");
            if (copied) FixtureClipboard.OwnCopy(copiedText);
            Check("Ctrl+C copies selection without reaching child", copied && Count(a, "copy") == 0 && Flags() == 0);
            int before = Count(a, "copy"); Keys(a, 'C', false);
            Check("unselected Ctrl+C reaches child and guard stays", Count(a, "copy") == before + 1 && !a.Guard.HasExited);
            Drag(a); Check("mouse drag selects with QuickEdit disabled", (Flags() & 2) != 0);
            IntPtr ignored; SendMessageTimeout(a.Window, 0x102, (IntPtr)27, IntPtr.Zero, 2, 500, out ignored);
            Focus(b); PasteMarker(MarkerB); Keys(b, 'V', false);
            Check("concurrent accounts route only foreground clipboard input", Value(b, "markerB") && !Value(a, "markerB") && !Value(b, "markerA"));
            Focus(generic); PasteMarker(MarkerA); Keys(generic, 'V', false); Keys(generic, 'A', false);
            Check("generic foreground console retains raw Ctrl+V/A", Count(generic, "paste") > 0 && Count(generic, "select") > 0 && !Value(generic, "markerA") && Flags() == 0);
            Check("STDIN modes and fixed arguments preserved", Count(a, "mode") == 648 && Count(b, "mode") == 648 && Count(generic, "mode") == 648 && Value(a, "fixed_args") && Value(b, "fixed_args"));
            TrustChecks(engine, folder);
            report["checks"] = Checks; report["failures"] = Failures;
            return Failures == 0 ? 0 : 1;
        }
        catch (Exception e) { report["instrument_error"] = e.GetType().Name + ": " + e.Message; report["checks"] = Checks; report["failures"] = Failures; return 2; }
        finally
        {
            FreeConsole();
            IntPtr current = GetForegroundWindow();
            bool ours = a != null && current == a.Window || b != null && current == b.Window || generic != null && current == generic.Window || normal != null && current == normal.Window;
            if (ours) { SetForegroundWindow(previousForeground); SetCursorPos(cursor.X, cursor.Y); }
            report["restored_foreground"] = GetForegroundWindow() == previousForeground;
            try { if (normal != null) normal.Dispose(); if (generic != null) generic.Dispose(); if (b != null) b.Dispose(); if (a != null) a.Dispose(); report["owned_fixtures_exited"] = true; }
            catch (Exception e) { report["cleanup_error"] = e.Message; }
            try {
                if (FixtureClipboard != null) report["clipboard_restored"] = FixtureClipboard.Restore();
            }
            catch (Exception e) { report["clipboard_restore_error"] = e.GetType().Name + ":" + e.HResult.ToString("X"); }
            finally { if (FixtureClipboard != null) FixtureClipboard.Dispose(); }
            output.WriteLine(Json.Serialize(report)); output.Dispose();
        }
    }
}
