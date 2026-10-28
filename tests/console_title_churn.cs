using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

// Acts as a vendor TUI which rewrites its own title after every response.
internal static class ConsoleTitleChurn
{
    [DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int length);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr GetProp(IntPtr hwnd, string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern uint GlobalGetAtomName(ushort atom, StringBuilder text, int length);
    [DllImport("kernel32.dll")] static extern uint GetConsoleProcessList(uint[] pids, uint length);
    [DllImport("kernel32.dll")] static extern bool GenerateConsoleCtrlEvent(uint eventCode, uint processGroup);
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int handle);
    [DllImport("kernel32.dll")] static extern bool GetConsoleMode(IntPtr handle, out uint mode);

    static int Main(string[] args)
    {
        bool antigravity = args.Length == 1 && args[0] == "--dangerously-skip-permissions";
        if (args.Length != 3 && !antigravity) return 2;
        string expectedTitle = antigravity ? Environment.GetEnvironmentVariable("SAI_TITLE_FIXTURE_TITLE") : args[0];
        string expectedAccount = antigravity ? Environment.GetEnvironmentVariable("SAI_TITLE_FIXTURE_ACCOUNT") : args[1];
        string resultFile = antigravity ? Environment.GetEnvironmentVariable("SAI_TITLE_FIXTURE_RESULT") : args[2];
        bool testControlC = antigravity && Environment.GetEnvironmentVariable("SAI_TITLE_FIXTURE_CTRL_C") == "1";
        int controlCReceived = 0;
        Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) { Interlocked.Increment(ref controlCReceived); e.Cancel = true; };
        uint inputModeBefore, inputModeAfter;
        bool inputReadable = GetConsoleMode(GetStdHandle(-10), out inputModeBefore);
        uint[] consolePids = new uint[16];
        uint consoleProcesses = GetConsoleProcessList(consolePids, (uint)consolePids.Length);
        IntPtr window = GetConsoleWindow();
        var samples = new List<object>();
        int failures = 0;
        string readyFile = Environment.GetEnvironmentVariable("SAI_TITLE_FIXTURE_READY");
        if (!string.IsNullOrEmpty(readyFile)) File.WriteAllText(readyFile, "ready");
        for (int i = 0; i < 20; i++)
        {
            // This private console contains only the owned fixture and guard.
            // A vendor can handle Ctrl+C without exiting; its guard must survive.
            if (testControlC && i == 4 && !GenerateConsoleCtrlEvent(1, 0)) failures++;
            Console.Title = "VENDOR_RESPONSE_TITLE_" + i;
            var watch = Stopwatch.StartNew();
            bool restored = false;
            string actualTitle = "", actualWindow = "", actualAccount = "";
            do
            {
                actualTitle = Console.Title;
                var text = new StringBuilder(2048);
                GetWindowText(window, text, text.Capacity);
                actualWindow = text.ToString();
                text.Length = 0;
                ushort atom = unchecked((ushort)GetProp(window, "SAITULS_ACCOUNT_ID").ToInt64());
                if (atom != 0) GlobalGetAtomName(atom, text, text.Capacity);
                actualAccount = text.ToString();
                restored = window != IntPtr.Zero && actualTitle == expectedTitle
                    && actualWindow == expectedTitle && actualAccount == expectedAccount;
                if (!restored) Thread.Sleep(5);
            } while (!restored && watch.ElapsedMilliseconds < 150);
            if (!restored) failures++;
            samples.Add(new { iteration = i, restored = restored, latency_ms = watch.ElapsedMilliseconds,
                console_title = actualTitle, window_title = actualWindow, account_id = actualAccount });
            Thread.Sleep(35);
        }
        string releaseFile = Environment.GetEnvironmentVariable("SAI_TITLE_FIXTURE_RELEASE");
        if (!string.IsNullOrEmpty(releaseFile))
        {
            var deadline = Stopwatch.StartNew();
            while (!File.Exists(releaseFile) && deadline.ElapsedMilliseconds < 15000) Thread.Sleep(10);
            if (!File.Exists(releaseFile)) failures++;
        }
        bool inputReadableAfter = GetConsoleMode(GetStdHandle(-10), out inputModeAfter);
        uint remainingConsoleProcesses = GetConsoleProcessList(consolePids, (uint)consolePids.Length);
        File.WriteAllText(resultFile, new JavaScriptSerializer().Serialize(new {
            failures = failures, samples = samples, expected_title = expectedTitle, expected_account = expectedAccount,
            cwd = Directory.GetCurrentDirectory(), sid = System.Security.Principal.WindowsIdentity.GetCurrent().User.Value,
            console_process_count = consoleProcesses, console_process_count_after = remainingConsoleProcesses,
            ctrl_c_received = controlCReceived, input_readable = inputReadable && inputReadableAfter,
            input_mode_before = inputModeBefore, input_mode_after = inputModeAfter, arguments = args,
            no_color = Environment.GetEnvironmentVariable("NO_COLOR"), term = Environment.GetEnvironmentVariable("TERM"),
            colorterm = Environment.GetEnvironmentVariable("COLORTERM"),
            preserved_marker = Environment.GetEnvironmentVariable("SAI_TITLE_FIXTURE_PRESERVE")
        }), new UTF8Encoding(false));
        int exitCode;
        return failures != 0 ? 1 : antigravity && int.TryParse(Environment.GetEnvironmentVariable("SAI_TITLE_FIXTURE_EXIT"), out exitCode) ? exitCode : 0;
    }
}
