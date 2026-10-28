using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;

internal static class AccountConsoleEnvironmentTest
{
    [DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window, int show);
    static int Main(string[] args)
    {
        if (args.Length == 2 && args[0] == "--probe")
        {
            ShowWindow(GetConsoleWindow(), 0);
            File.WriteAllText(args[1], new JavaScriptSerializer().Serialize(new {
                term = Environment.GetEnvironmentVariable("TERM"),
                home = Environment.GetEnvironmentVariable("CODEX_HOME"),
                secret = Environment.GetEnvironmentVariable("SAI_ENV_CANARY")
            }));
            return 0;
        }
        string oldTerm = Environment.GetEnvironmentVariable("TERM");
        string oldCanary = Environment.GetEnvironmentVariable("SAI_ENV_CANARY");
        string folder = Path.Combine(Path.GetTempPath(), "sai-console-env-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(folder);
        int failed = 0;
        try
        {
            var method = typeof(SaiAccounts).GetMethod("LaunchInNewConsole", BindingFlags.Static | BindingFlags.NonPublic);
            foreach (string input in new[] { "dumb", "DUMB", null, "wezterm" })
            {
                Environment.SetEnvironmentVariable("TERM", input);
                Environment.SetEnvironmentVariable("SAI_ENV_CANARY", "synthetic-parent-only");
                string output = Path.Combine(folder, "probe.json");
                if (File.Exists(output)) File.Delete(output);
                string home = Path.Combine(folder, "isolated-codex-home");
                var env = new Dictionary<string, string> { { "CODEX_HOME", home } };
                var scrub = new List<string> { "SAI_ENV_CANARY" };
                string command = "\"" + Assembly.GetExecutingAssembly().Location + "\" --probe \"" + output + "\"";
                var result = (Dictionary<string, object>)method.Invoke(null, new object[] {
                    command, folder, env, scrub, "Owned terminal environment fixture", "codex:profile-dir:0000000000000002"
                });
                if (!object.Equals(result["status"], "launched")) throw new Exception("Native child did not launch");
                try
                {
                    using (var childProcess = Process.GetProcessById(Convert.ToInt32(result["pid"])))
                        if (!childProcess.WaitForExit(5000)) throw new Exception("Native probe child did not exit");
                }
                catch (ArgumentException) { /* The child already exited before lookup. */ }
                for (int i = 0; i < 200 && !File.Exists(output); i++) Thread.Sleep(10);
                if (!File.Exists(output)) throw new Exception("Real child did not report its environment");
                var child = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(output));
                string expected = input == "wezterm" ? input : "xterm-256color";
                bool ok = object.Equals(child["term"], expected) && object.Equals(child["home"], home)
                    && child["secret"] == null && Environment.GetEnvironmentVariable("TERM") == input
                    && Environment.GetEnvironmentVariable("SAI_ENV_CANARY") == "synthetic-parent-only"
                    && !env.ContainsKey("TERM") && env.Count == 1;
                Console.WriteLine((ok ? "PASS " : "FAIL ") + "native child TERM=" + (input ?? "<unset>")
                    + "; isolated home, scrub and parent/override purity");
                if (!ok) failed++;
            }
        }
        finally
        {
            Environment.SetEnvironmentVariable("TERM", oldTerm);
            Environment.SetEnvironmentVariable("SAI_ENV_CANARY", oldCanary);
            Directory.Delete(folder, true);
        }
        failed += SaipenEntrypointCases();
        Console.WriteLine("CONSOLE_ENV_FAIL=" + failed);
        return failed == 0 ? 0 : 1;
    }

    // T-287: a managed Antigravity console resolves `saipen` to the machine
    // runtime. Prepended exactly once, never duplicated, never applied to a
    // child that is not the shared Antigravity binary.
    static int SaipenEntrypointCases()
    {
        int failed = 0;
        var provide = typeof(SaiAccounts).GetMethod("ProvideSaipenEntrypoint", BindingFlags.Static | BindingFlags.NonPublic);
        var agyPath = (string)typeof(SaiAccounts).GetMethod("SharedAgyPath", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic).Invoke(null, null);
        string bin = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "SAI", "SAIPEN", "bin");
        bool installed = File.Exists(Path.Combine(bin, "saipen.cmd"));
        const string account = "antigravity:windows-user:0123456789abcdef";

        var fresh = new ProcessStartInfo(agyPath) { UseShellExecute = false };
        fresh.EnvironmentVariables["PATH"] = @"C:\Windows\System32";
        provide.Invoke(null, new object[] { fresh, account });
        string expected = installed ? bin + @";C:\Windows\System32" : @"C:\Windows\System32";
        bool ok1 = fresh.EnvironmentVariables["PATH"] == expected;
        Console.WriteLine((ok1 ? "PASS " : "FAIL ") + "managed console PATH gets the machine launcher (installed=" + installed + ")");
        if (!ok1) failed++;

        var present = new ProcessStartInfo(agyPath) { UseShellExecute = false };
        present.EnvironmentVariables["PATH"] = bin.ToUpperInvariant() + @"\;C:\Windows";
        provide.Invoke(null, new object[] { present, account });
        bool ok2 = present.EnvironmentVariables["PATH"] == bin.ToUpperInvariant() + @"\;C:\Windows";
        Console.WriteLine((ok2 ? "PASS " : "FAIL ") + "an existing launcher entry is not duplicated");
        if (!ok2) failed++;

        var other = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "cmd.exe")) { UseShellExecute = false };
        other.EnvironmentVariables["PATH"] = @"C:\Windows";
        provide.Invoke(null, new object[] { other, account });
        bool ok3 = other.EnvironmentVariables["PATH"] == @"C:\Windows";
        Console.WriteLine((ok3 ? "PASS " : "FAIL ") + "a non-Antigravity child is untouched");
        if (!ok3) failed++;
        return failed;
    }
}
