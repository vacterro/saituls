// sai-antigravity.exe compatibility shim.
//
// Older consumers (LIMISAW ProbeAntigravity) call:
//     sai-antigravity usage <account-ref>
// and expect the legacy response contract:
//     {"status":"CONTEXT_OFFLINE"}         broker absent for that context
//     {"status":"AUTH_REQUIRED", ...}      context online, credential missing
//     {... "windows": [...]}               quota payload (usage.windows shape)
//
// The canonical engine is sai-accounts.exe; this shim only TRANSLATES the
// response envelope. It executes exactly one allowlisted forward:
//     sai-accounts usage <ref>
// No other verb is forwarded, and no arguments are interpreted here.
// Resolution order for the engine exe: SAI_ACCOUNTS_EXE env override,
// same directory, C:\ProgramData\SAI\Accounts\bin, PATH.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Web.Script.Serialization;

namespace SaiAccounts.Shim
{
    static class AntigravityShim
    {
        static int Main(string[] args)
        {
            if (args.Length < 1 || args[0] != "usage")
            {
                Console.Error.WriteLine("sai-antigravity (compat shim): only 'usage <account-ref>' is supported; use sai-accounts.exe for everything else");
                return 2;
            }
            string accountRef = args.Length > 1 ? args[1] : "";

            string engine = ResolveEngine();
            if (engine.Length == 0)
            {
                WriteJson(new Dictionary<string, object>
                {
                    { "status", "SHIM_ENGINE_MISSING" },
                    { "context_online", false }
                });
                return 1;
            }

            var psi = new ProcessStartInfo
            {
                FileName = engine,
                Arguments = "usage \"" + accountRef.Replace("\"", "") + "\"",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            foreach (string name in new[] { "SAI_ACCOUNTS_ROOT", "SAI_ACCOUNTS_SCOPE", "SAI_ACCOUNTS_OPERATOR_SID" })
            {
                string v = Environment.GetEnvironmentVariable(name);
                if (v != null) psi.EnvironmentVariables[name] = v;
            }
            string stdout;
            try
            {
                using (var p = Process.Start(psi))
                {
                    // Both pipes are drained on their own threads, and the
                    // deadline waits on the PROCESS, not on the reads.
                    //
                    // The order used to be ReadToEnd() then WaitForExit(120000),
                    // which put the budget out of reach in exactly the case it
                    // exists for: a hung engine never closes stdout, so the read
                    // never returned and the wait below was never reached. The
                    // legacy consumer saw an unanswered call forever, which is
                    // the "connect does not respond for 3+ minutes" report.
                    //
                    // Draining stderr matters just as much. The engine writes
                    // diagnostics there and an undrained pipe fills at ~64KB, so
                    // the engine would block mid-write while this process blocked
                    // reading stdout -- a deadlock neither side can escape, and
                    // one that a hung engine reaches easily.
                    string outText = null, errText = null;
                    var drained = new CountdownEvent(2);
                    var outReader = new Thread(delegate() {
                        try { outText = p.StandardOutput.ReadToEnd(); } catch { }
                        finally { try { drained.Signal(); } catch { } }
                    });
                    var errReader = new Thread(delegate() {
                        try { errText = p.StandardError.ReadToEnd(); } catch { }
                        finally { try { drained.Signal(); } catch { } }
                    });
                    outReader.IsBackground = true;
                    errReader.IsBackground = true;
                    outReader.Start();
                    errReader.Start();

                    int budget = BudgetMs();
                    if (!p.WaitForExit(budget))
                    {
                        try { p.Kill(); } catch { }
                        // A bounded grace so the readers stop touching a stream
                        // this block is about to dispose. They are background
                        // threads, so a reader still stuck costs nothing.
                        drained.Wait(5000);
                        WriteJson(new Dictionary<string, object>
                        {
                            { "status", "SHIM_ENGINE_TIMEOUT" },
                            { "error", "engine did not exit within " + budget + "ms" },
                            { "context_online", false }
                        });
                        return 1;
                    }
                    // The process is gone, so both pipes close; this only gives
                    // the readers a moment to finish, and is never the hang.
                    drained.Wait(5000);
                    stdout = outText;
                }
            }
            catch (Exception ex)
            {
                WriteJson(new Dictionary<string, object>
                {
                    { "status", "SHIM_ENGINE_FAILED" },
                    { "error", ex.Message },
                    { "context_online", false }
                });
                return 1;
            }

            var serializer = new JavaScriptSerializer();
            Dictionary<string, object> payload = null;
            try { payload = serializer.DeserializeObject(stdout) as Dictionary<string, object>; }
            catch { }
            if (payload == null)
            {
                WriteJson(new Dictionary<string, object>
                {
                    { "status", "SHIM_BAD_PAYLOAD" },
                    { "context_online", false }
                });
                return 1;
            }

            // Translate the canonical envelope into the legacy contract.
            string contextState = Str(payload, "context_state");
            string skipped = Str(payload, "skipped_reason");
            string authState = Str(payload, "auth_state");
            string usageStatus = Str(payload, "usage_status");

            if (contextState == "OFFLINE" || skipped == "CONTEXT_OFFLINE")
            {
                WriteJson(new Dictionary<string, object>
                {
                    { "status", "CONTEXT_OFFLINE" },
                    { "context_online", false },
                    { "account_id", Str(payload, "account_id") }
                });
                return 0;
            }
            if (authState == "AUTH_REQUIRED")
            {
                WriteJson(new Dictionary<string, object>
                {
                    { "status", "AUTH_REQUIRED" },
                    { "context_online", true },
                    { "account_id", Str(payload, "account_id") }
                });
                return 0;
            }

            var mapped = new Dictionary<string, object>();
            Set(mapped, "status", usageStatus != null ? usageStatus : "ok");
            Set(mapped, "context_online", true);
            Set(mapped, "account_id", Str(payload, "account_id"));
            object windows;
            if (payload.TryGetValue("windows", out windows) && windows != null) Set(mapped, "windows", windows);
            object credential;
            if (payload.TryGetValue("credential", out credential) && credential != null) Set(mapped, "credential", credential);
            WriteJson(mapped);
            return 0;
        }

        // How long the shim waits for the engine to exit. The deadline exists
        // only for a wedged engine, and 120s is long enough that no healthy
        // call ever sees it.
        //
        // It is overridable because that deadline is otherwise untestable: a
        // hung engine makes the real one cost two minutes of wall clock per
        // assertion, and a budget nobody will ever wait out is a budget nobody
        // checks. The floor keeps a typo from turning the timeout into a
        // no-op; the ceiling keeps it from being used to hold a caller longer
        // than the contract promises.
        static int BudgetMs()
        {
            int ms;
            string raw = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_SHIM_TIMEOUT_MS");
            if (!int.TryParse(raw, out ms) || ms < 250 || ms > 120000) return 120000;
            return ms;
        }

        static string ResolveEngine()
        {
            string forced = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_EXE");
            if (!string.IsNullOrEmpty(forced) && File.Exists(forced)) return forced;
            string beside = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "sai-accounts.exe");
            if (File.Exists(beside)) return beside;
            string canonical = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "SAI", "Accounts", "bin", "sai-accounts.exe");
            if (File.Exists(canonical)) return canonical;
            string onPath = FindOnPath("sai-accounts.exe");
            return onPath ?? "";
        }

        static string FindOnPath(string name)
        {
            foreach (string dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
            {
                if (dir.Trim().Length == 0) continue;
                try
                {
                    string candidate = Path.Combine(dir.Trim(), name);
                    if (File.Exists(candidate)) return candidate;
                }
                catch { }
            }
            return null;
        }

        static string Str(Dictionary<string, object> o, string key)
        {
            object v;
            if (o != null && o.TryGetValue(key, out v) && v != null) return v as string;
            return null;
        }

        static void Set(Dictionary<string, object> o, string key, object v)
        {
            if (v != null) o[key] = v;
        }

        static void WriteJson(Dictionary<string, object> o)
        {
            Console.WriteLine(new JavaScriptSerializer().Serialize(o));
        }
    }
}
