// SaiConsoleLauncher.exe -- the native Explorer gateway for the agent console
// cascade (T-264). The HKCU verb is exactly:
//
//     "<gateway dir>\SaiConsoleLauncher.exe" <profile id> "%V"
//
// Nothing else is baked into the registry: the install root is resolved here,
// at click time, so a moved or reinstalled SAITULS root is found again (or
// reported as ROOT_MISSING) instead of starting a PowerShell that cannot find
// a stale absolute CONSOLES.ps1 path and closes.
//
// Contract:
//   * builds a versioned request, schema saituls.console-launch/1, serializes
//     it and launches only from the deserialized copy (the request is the one
//     boundary object; --request <file> consumes a saved one);
//   * preflights root, profile id, folder, folder access and the profile
//     itself (CONSOLES.ps1 -SelfTest, hidden, no window) BEFORE any visible
//     vendor session exists;
//   * every failure is categorized, written to a bounded log and shown in a
//     message box, and the process exits nonzero. Never a flash, never a dead
//     agent session.
//
// Built with the .NET Framework csc.exe as a winexe (no console of its own).
// Test hooks (never set by the installed verb):
//   SAITULS_CONSOLE_ROOT     install root override
//   SAITULS_CONSOLE_LOG      log file path; gateway log goes next to it
//   SAITULS_GATEWAY_NO_DIALOG=1   log only, no modal
//   SAITULS_GATEWAY_DRYRUN=<file> run every check, write the plan, launch nothing
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace Saituls
{
    internal sealed class GatewayFailure : Exception
    {
        public readonly string Category;
        public readonly int Code;
        public GatewayFailure(string category, int code, string message) : base(message)
        {
            Category = category; Code = code;
        }
    }

    internal static class SaiConsoleLauncher
    {
        public const string Schema = "saituls.console-launch/1";
        private const int LogLimit = 512 * 1024;
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        [STAThread]
        private static int Main()
        {
            Dictionary<string, object> request = null;
            try
            {
                string raw = Environment.CommandLine;
                string requestFile = RequestFileArgument(raw);
                string wire = requestFile != null
                    ? File.ReadAllText(requestFile, Encoding.UTF8)
                    : Json.Serialize(BuildRequest(raw));
                request = ConsumeRequest(wire);
                return Run(request);
            }
            catch (GatewayFailure f)
            {
                return Fail(request, f.Category, f.Code, f.Message);
            }
            catch (Exception e)
            {
                return Fail(request, "GATEWAY_EXCEPTION", 99, e.GetType().Name + ": " + e.Message);
            }
        }

        // ------------------------------------------------------------ request
        // Explorer substitutes %V verbatim. A drive root arrives as "C:\" and
        // CommandLineToArgvW reads that trailing \" as an escaped quote, so the
        // raw command line is parsed here instead of trusting argv.
        internal static string[] SplitRaw(string raw)
        {
            string rest = SkipToken(raw.TrimStart());          // the exe itself
            rest = rest.TrimStart();
            string id = TakeToken(ref rest);
            string target = rest.Trim();
            if (target.Length >= 2 && target[0] == '"' && target[target.Length - 1] == '"')
                target = target.Substring(1, target.Length - 2);
            else if (target.StartsWith("\"")) target = target.Substring(1);
            return new[] { id, target };
        }

        private static string SkipToken(string s)
        {
            if (s.StartsWith("\""))
            {
                int end = s.IndexOf('"', 1);
                return end < 0 ? "" : s.Substring(end + 1);
            }
            int sp = s.IndexOfAny(new[] { ' ', '\t' });
            return sp < 0 ? "" : s.Substring(sp);
        }

        private static string TakeToken(ref string s)
        {
            string token;
            if (s.StartsWith("\""))
            {
                int end = s.IndexOf('"', 1);
                token = end < 0 ? s.Substring(1) : s.Substring(1, end - 1);
                s = end < 0 ? "" : s.Substring(end + 1);
            }
            else
            {
                int sp = s.IndexOfAny(new[] { ' ', '\t' });
                token = sp < 0 ? s : s.Substring(0, sp);
                s = sp < 0 ? "" : s.Substring(sp);
            }
            return token;
        }

        private static string RequestFileArgument(string raw)
        {
            string[] parts = SplitRaw(raw);
            return parts[0] == "--request" && parts[1].Length > 0 ? parts[1] : null;
        }

        internal static Dictionary<string, object> BuildRequest(string raw)
        {
            string[] parts = SplitRaw(raw);
            var r = new Dictionary<string, object>();
            r["schema"] = Schema;
            r["profile"] = parts[0];
            r["target"] = parts[1];
            r["source"] = "explorer";
            r["gateway"] = Application.ExecutablePath;
            return r;
        }

        internal static Dictionary<string, object> ConsumeRequest(string wire)
        {
            Dictionary<string, object> r;
            try { r = Json.Deserialize<Dictionary<string, object>>(wire); }
            catch (Exception e) { throw new GatewayFailure("REQUEST_INVALID", 9, "request is not JSON: " + e.Message); }
            if (r == null || !(r.ContainsKey("schema")) || (r["schema"] as string) != Schema)
                throw new GatewayFailure("REQUEST_INVALID", 9, "unsupported request schema (want " + Schema + ")");
            foreach (string k in new[] { "profile", "target" })
                if (!r.ContainsKey(k) || !(r[k] is string))
                    throw new GatewayFailure("REQUEST_INVALID", 9, "request field missing: " + k);
            return r;
        }

        // --------------------------------------------------------------- root
        internal static string ResolveRoot()
        {
            var tried = new List<string>();
            string env = Environment.GetEnvironmentVariable("SAITULS_CONSOLE_ROOT");
            if (!string.IsNullOrEmpty(env)) return CheckRoot(env, tried, true);

            string dir = Path.GetDirectoryName(Application.ExecutablePath);
            string sidecar = Path.Combine(dir, "SaiConsoleLauncher.root.json");
            if (File.Exists(sidecar))
            {
                try
                {
                    var doc = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(sidecar, Encoding.UTF8));
                    string root = doc != null && doc.ContainsKey("root") ? doc["root"] as string : null;
                    if (!string.IsNullOrEmpty(root))
                    {
                        string ok = CheckRoot(root, tried, false);
                        if (ok != null) return ok;
                    }
                }
                catch (Exception) { tried.Add(sidecar + " (unreadable)"); }
            }
            for (string d = dir; !string.IsNullOrEmpty(d); d = Path.GetDirectoryName(d))
            {
                string ok = CheckRoot(d, tried, false);
                if (ok != null) return ok;
            }
            throw new GatewayFailure("ROOT_MISSING", 10,
                "SAITULS install root not found (Scripts\\consoles\\CONSOLES.ps1). Tried: " + string.Join("; ", tried.ToArray()) +
                ". Re-run Installers\\INSTALL_CONSOLES_MENU.PS1 from the current install.");
        }

        private static string CheckRoot(string root, List<string> tried, bool strict)
        {
            string launcher = Path.Combine(root, "Scripts\\consoles\\CONSOLES.ps1");
            if (File.Exists(launcher)) return Path.GetFullPath(root);
            tried.Add(root);
            if (strict)
                throw new GatewayFailure("ROOT_MISSING", 10, "SAITULS install root has no launcher: " + launcher);
            return null;
        }

        // ---------------------------------------------------------------- run
        private static int Run(Dictionary<string, object> request)
        {
            string id = (string)request["profile"];
            string target = (string)request["target"];
            string root = ResolveRoot();
            request["root"] = root;

            if (!Regex.IsMatch(id, "^[A-Za-z0-9_-]+$"))
                throw new GatewayFailure("PROFILE_INVALID", 11, "invalid console profile id: '" + id + "'");

            if (target.Length == 0)
                throw new GatewayFailure("WORKDIR_MISSING", 12, "Explorer passed no folder");
            string dir;
            try { dir = Path.GetFullPath(target); }
            catch (Exception e) { throw new GatewayFailure("WORKDIR_MISSING", 12, "folder path is invalid: " + target + " (" + e.Message + ")"); }
            if (!Directory.Exists(dir))
                throw new GatewayFailure("WORKDIR_MISSING", 12, "folder does not exist: " + dir);
            try { using (var e = Directory.EnumerateFileSystemEntries(dir).GetEnumerator()) e.MoveNext(); }
            catch (Exception e) { throw new GatewayFailure("WORKDIR_INACCESSIBLE", 13, "folder is not accessible: " + dir + " (" + e.Message + ")"); }
            request["workdir"] = dir;

            string launcher = Path.Combine(root, "Scripts\\consoles\\CONSOLES.ps1");
            Dictionary<string, object> row = Preflight(launcher, id, dir);
            string origin = row.ContainsKey("Origin") ? row["Origin"] as string : "local";
            request["origin"] = origin;

            string args = "-NoLogo -NoProfile -ExecutionPolicy Bypass" + (origin == "shared" ? " -WindowStyle Hidden" : "") +
                          " -File " + Quote(launcher) + " -Profile " + id + " -WorkDir " + Quote(dir) + " -FromExplorer";

            string dry = Environment.GetEnvironmentVariable("SAITULS_GATEWAY_DRYRUN");
            if (!string.IsNullOrEmpty(dry))
            {
                request["outcome"] = "DRYRUN";
                request["arguments"] = args;
                File.WriteAllText(dry, Json.Serialize(request), new UTF8Encoding(false));
                Log(request, "DRYRUN", 0, null);
                return 0;
            }

            var psi = new ProcessStartInfo(PowerShell(), args);
            psi.UseShellExecute = false;
            psi.WorkingDirectory = dir;
            // A local console IS the agent's window; a shared one is a hidden
            // bootstrap the accounts engine replaces with the account's console.
            psi.CreateNoWindow = origin == "shared";
            Process child;
            try { child = Process.Start(psi); }
            catch (Exception e) { throw new GatewayFailure("LAUNCH_FAILED", 16, "could not start PowerShell: " + e.Message); }
            request["child_pid"] = child == null ? 0 : child.Id;
            Log(request, "LAUNCHED", 0, null);
            return 0;
        }

        // P0-5: the profile is resolved (unknown/stale id, missing vendor CLI,
        // missing script/engine) in a hidden PowerShell before anything visible
        // is created. CONSOLES.ps1 -SelfTest launches nothing.
        private static Dictionary<string, object> Preflight(string launcher, string id, string dir)
        {
            var psi = new ProcessStartInfo(PowerShell(),
                "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(launcher) +
                " -Profile " + id + " -WorkDir " + Quote(dir) + " -SelfTest");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = Encoding.UTF8;
            psi.WorkingDirectory = dir;
            string stdout, stderr;
            int code;
            try
            {
                using (Process p = Process.Start(psi))
                {
                    var errTask = p.StandardError.ReadToEndAsync();
                    stdout = p.StandardOutput.ReadToEnd();
                    if (!p.WaitForExit(120000)) { try { p.Kill(); } catch (Exception) { } throw new GatewayFailure("PREFLIGHT_TIMEOUT", 14, "profile preflight did not finish in 120 s"); }
                    stderr = errTask.Result;
                    code = p.ExitCode;
                }
            }
            catch (GatewayFailure) { throw; }
            catch (Exception e) { throw new GatewayFailure("PREFLIGHT_FAILED", 14, "could not run the profile preflight: " + e.Message); }

            Dictionary<string, object> payload = null;
            string line = LastJsonLine(stdout);
            if (line != null) { try { payload = Json.Deserialize<Dictionary<string, object>>(line); } catch (Exception) { } }
            if (code != 0 || payload == null || !payload.ContainsKey("profiles"))
                throw new GatewayFailure("PREFLIGHT_FAILED", 14, "profile preflight failed (exit " + code + "): " + Tail(stderr + " " + stdout));
            var rows = payload["profiles"] as ArrayList;
            var row = rows != null && rows.Count > 0 ? rows[0] as Dictionary<string, object> : payload["profiles"] as Dictionary<string, object>;
            if (row == null) throw new GatewayFailure("PREFLIGHT_FAILED", 14, "profile preflight returned no row");
            string error = row.ContainsKey("Error") ? row["Error"] as string : null;
            if (!string.IsNullOrEmpty(error))
            {
                string category = row.ContainsKey("ErrorCategory") ? row["ErrorCategory"] as string : null;
                throw new GatewayFailure(string.IsNullOrEmpty(category) ? "PROFILE_UNRESOLVED" : category, 15, error);
            }
            return row;
        }

        private static string LastJsonLine(string text)
        {
            string found = null;
            foreach (string l in (text ?? "").Split('\n'))
            {
                string t = l.Trim();
                if (t.StartsWith("{") && t.EndsWith("}")) found = t;
            }
            return found;
        }

        private static string Tail(string s)
        {
            s = (s ?? "").Trim();
            return s.Length > 600 ? s.Substring(s.Length - 600) : s;
        }

        private static string PowerShell()
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                "WindowsPowerShell\\v1.0\\powershell.exe");
        }

        // Windows argv quoting: backslashes before the closing quote are doubled,
        // so a drive root "C:\" survives as one argument.
        internal static string Quote(string value)
        {
            var sb = new StringBuilder("\"");
            int slashes = 0;
            foreach (char c in value)
            {
                if (c == '\\') { slashes++; continue; }
                if (c == '"') { sb.Append('\\', slashes * 2 + 1); sb.Append('"'); slashes = 0; continue; }
                sb.Append('\\', slashes); slashes = 0; sb.Append(c);
            }
            sb.Append('\\', slashes * 2);
            return sb.Append('"').ToString();
        }

        // ------------------------------------------------------------ failure
        private static string LogPath()
        {
            string over = Environment.GetEnvironmentVariable("SAITULS_CONSOLE_LOG");
            string dir = !string.IsNullOrEmpty(over) ? Path.GetDirectoryName(Path.GetFullPath(over))
                : Path.Combine(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SAITULS"), "logs");
            return Path.Combine(dir, "console-gateway.log");
        }

        private static void Log(Dictionary<string, object> request, string category, int code, string reason)
        {
            try
            {
                string path = LogPath();
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                if (File.Exists(path) && new FileInfo(path).Length > LogLimit) File.Copy(path, path + ".1", true);
                if (File.Exists(path) && new FileInfo(path).Length > LogLimit) File.Delete(path);
                var row = new Dictionary<string, object>();
                row["schema"] = "saituls.console-gateway/log/1";
                row["timestamp"] = DateTime.UtcNow.ToString("o");
                row["outcome"] = code == 0 ? (category == "DRYRUN" ? "dryrun" : "launched") : "failed";
                row["category"] = category;
                row["exit_code"] = code;
                row["request"] = request;
                row["reason"] = Redact(reason);
                File.AppendAllText(path, Json.Serialize(row) + "\r\n", new UTF8Encoding(false));
            }
            catch (Exception) { }
        }

        private static string Redact(string text)
        {
            if (string.IsNullOrEmpty(text)) return text;
            return Regex.Replace(text,
                @"(?i)\b(password|passwd|secret|token|api[_-]?key|apikey|authorization|bearer|cookie)\b\s*[=:]\s*\S+", "$1=<redacted>");
        }

        private static int Fail(Dictionary<string, object> request, string category, int code, string reason)
        {
            Log(request, category, code, reason);
            if (Environment.GetEnvironmentVariable("SAITULS_GATEWAY_NO_DIALOG") != "1")
            {
                string profile = request != null && request.ContainsKey("profile") ? request["profile"] as string : "?";
                string target = request != null && request.ContainsKey("target") ? request["target"] as string : "?";
                string body = "The agent console could not be started.\r\n\r\nProfile:  " + profile +
                              "\r\nCategory: " + category + "\r\nFolder:   " + target + "\r\n\r\n" + Redact(reason) +
                              "\r\n\r\nDiagnostic: " + LogPath();
                try { MessageBox.Show(body, "SAITULS - console launch failed", MessageBoxButtons.OK, MessageBoxIcon.Error); }
                catch (Exception) { }
            }
            return code;
        }
    }
}
