// SAI Accounts — vendor-neutral account control plane (CLI + broker).
// Contract: Scripts/accounts/SAI_ACCOUNTS.md. Owner: SAITULS.
// Compile (build.ps1 is the authority):
//   csc -nologo -out:sai-accounts.exe -optimize+ -r:System.dll
//       -r:System.Web.Extensions.dll SaiAccounts.cs
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

public static class SaiAccounts
{
    // ---------------------------------------------------------------- paths
    static string RootDir()
    {
        string env = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_ROOT");
        if (!string.IsNullOrEmpty(env)) return env;
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), @"SAI\Accounts");
    }
    static string BinDir() { return Path.Combine(RootDir(), "bin"); }
    static string OperatorSid()
    {
        string env = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_OPERATOR_SID");
        if (!string.IsNullOrEmpty(env)) return env;
        return WindowsIdentity.GetCurrent().User.Value;
    }
    static string ScopeId()
    {
        string env = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_SCOPE");
        if (!string.IsNullOrEmpty(env)) return env;
        return "op-" + Fingerprint(OperatorSid());
    }
    static string ScopeDir() { return Path.Combine(RootDir(), "scopes", ScopeId()); }
    static string RegistryPath() { return Path.Combine(ScopeDir(), "accounts.json"); }
    static string EventsPath() { return Path.Combine(ScopeDir(), "events.log"); }

    // W2-005: the audit outbox. Every event is journaled here durably BEFORE
    // the registry is published, then drained into events.log; an undrained
    // entry is audit-pending, never a failed mutation. Reconciliation drains
    // whatever the last writer left behind, so one event is delivered to
    // events.log exactly once.
    static string AuditOutboxPath() { return Path.Combine(ScopeDir(), "events.pending.log"); }

    static string BinStatePath() { return Path.Combine(ScopeDir(), "bin-state.json"); }
    static string TrustedModePath() { return Path.Combine(ScopeDir(), "trusted_mode.json"); }
    static string AuthorizationsPath() { return Path.Combine(ScopeDir(), "authorizations.json"); }
    static string SessionsPath() { return Path.Combine(ScopeDir(), "sessions.json"); }
    static readonly object _trustedLock = new object();
    static readonly object _authLock = new object();
    static readonly object _sessionLock = new object();
    public static string SharedAgyPath() { return Path.Combine(BinDir(), "agy.exe"); }
    static string SelfExePath() { return Assembly.GetExecutingAssembly().Location; }

    // ------------------------------------------------------------- launch policy
    // The engine owns the Antigravity launch policy, so the flag that decides
    // whether the agent may run a tool without stopping to ask lives here and
    // never travels from the caller. The local consoles.json profile already
    // launched agy with --dangerously-skip-permissions; the canonical account
    // route launched the very same binary with no arguments at all, which is
    // why only the managed console stopped to ask permission on every tool
    // call. Verified against the installed agy.exe: "Auto-approve all tool
    // permission requests without prompting".
    //
    // This is a fixed set, not a default. The caller chooses the account and
    // the workdir and nothing else, so there is no request shape that can add
    // to it, trim it, or swap the binary it applies to.
    static readonly string[] AntigravityInteractiveArgs = { "--dangerously-skip-permissions" };
    static string AntigravityArgumentLine() { return string.Join(" ", AntigravityInteractiveArgs); }

    static string AntigravityConsoleTitle(string workingDirectory, string compactLabel)
    {
        // WorkdirDenialReason already validated this exact path. Derive the
        // label lexically so .NET Framework normalization adds no path-length
        // limit to a Unicode/extended-length directory the launcher accepted.
        string folder = workingDirectory.TrimEnd('\\', '/');
        string project = folder.Substring(folder.LastIndexOfAny(new[] { '\\', '/' }) + 1);
        if (project.Length == 2 && project[1] == ':') project += "\\";
        return "[" + compactLabel + "] Antigravity - " + project;
    }

    static ProcessStartInfo AntigravityLaunch(string workingDirectory, string accountId, string compactLabel)
    {
        if (accountId == null || !System.Text.RegularExpressions.Regex.IsMatch(accountId, "^antigravity:windows-user:[0-9a-f]{16}$")
            || compactLabel == null || !System.Text.RegularExpressions.Regex.IsMatch(compactLabel, "^AG[1-9][0-9]{0,8}$"))
            throw new ArgumentException("canonical Antigravity console identity required");
        if (!File.Exists(SharedAgyPath())) throw new FileNotFoundException("Antigravity executable not found", SharedAgyPath());
        string title = AntigravityConsoleTitle(workingDirectory, compactLabel);
        return new ProcessStartInfo(SelfExePath())
        {
            WorkingDirectory = workingDirectory,
            UseShellExecute = true,
            Arguments = "console-title-guard --title64 " + Convert.ToBase64String(Encoding.UTF8.GetBytes(title))
                + " --account " + accountId + " -- " + JoinArgs(new[] { SharedAgyPath() }, 0)
                + " " + AntigravityArgumentLine()
        };
    }

    static Dictionary<string, object> AntigravityLaunchRequest(Dictionary<string, object> acc,
        List<Dictionary<string, object>> accounts, string workdir)
    {
        return new Dictionary<string, object> {
            { "cmd", "launch" }, { "workdir", workdir },
            { "account_id", S(acc, "account_id") }, { "compact_label", CompactLabel(accounts, acc) }
        };
    }

    // ------------------------------------------------------------- helpers
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024, RecursionLimit = 24 };

    static object Get(object obj, string key)
    {
        var dict = obj as Dictionary<string, object>; object val;
        return dict != null && dict.TryGetValue(key, out val) ? val : null;
    }
    static void Set(Dictionary<string, object> dict, string key, object val) { dict[key] = val; }
    static string S(object obj, string key)
    {
        string v = Get(obj, key) as string;
        return string.IsNullOrEmpty(v) ? null : v;
    }
    static string F(string fmt, params object[] args) { return string.Format(CultureInfo.InvariantCulture, fmt, args); }
    static string NowIso() { return DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture); }

    static string Fingerprint(string value)
    {
        using (var sha = SHA256.Create())
        {
            byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(value == null ? "" : value.Trim().ToLowerInvariant()));
            var hex = new StringBuilder();
            for (int i = 0; i < 8; i++) hex.Append(hash[i].ToString("x2", CultureInfo.InvariantCulture));
            return hex.ToString();
        }
    }

    static Dictionary<string, object> Obj() { return new Dictionary<string, object>(); }
    static void Print(Dictionary<string, object> obj) { Console.WriteLine(Json.Serialize(obj)); }
    static int Fail(string status, string detail)
    {
        var o = Obj(); Set(o, "schema", "sai.accounts/error/1"); Set(o, "status", status);
        if (detail != null) Set(o, "detail", detail);
        Print(o); return 1;
    }

    // ---------------------------------------------------------- providers
    sealed class ProviderDef
    {
        public string Id; public string Display; public string Compact;
        public bool MultiAccount; public bool InteractiveLogin; public bool Quota; public bool Launch; public bool Logout; public bool ContextCreate;
    }
    static readonly Dictionary<string, ProviderDef> Providers = new Dictionary<string, ProviderDef>
    {
        // antigravity is the one provider that can sign out. Measured, not assumed:
// the CLI has no auth/logout verb at all, and the interactive /logout's whole
// durable effect is the one credential target, so the engine owns that delete
// and reports it as a capability instead of pretending the verb does not exist.
        { "antigravity", new ProviderDef { Id = "antigravity", Display = "Antigravity", Compact = "AG", MultiAccount = true, InteractiveLogin = true, Quota = true, Launch = true, Logout = true, ContextCreate = true } },
        { "claude",      new ProviderDef { Id = "claude", Display = "Claude", Compact = "CL", MultiAccount = true, InteractiveLogin = true, Quota = false, Launch = true, Logout = false, ContextCreate = true } },
        { "codex",       new ProviderDef { Id = "codex", Display = "Codex", Compact = "CX", MultiAccount = true, InteractiveLogin = true, Quota = false, Launch = true, Logout = false, ContextCreate = true } },
    };

    // ------------------------------------------------------------- identity
    static bool IsWindowsUserBackend(string backend)
    {
        return backend == "windows_user" || backend == "managed_windows_user";
    }
    static string AccountIdFor(string provider, string backend, string contextIdentity)
    {
        string backendSegment = IsWindowsUserBackend(backend) ? "windows-user" : "profile-dir";
        return provider + ":" + backendSegment + ":" + Fingerprint(contextIdentity);
    }
    static string ContextIdFor(string backend, string contextIdentity)
    {
        if (IsWindowsUserBackend(backend)) return "windows-user:" + Fingerprint(contextIdentity);
        return "current-user";
    }

    // ------------------------------------------------------------- desired state
    // Whether a broker is alive right now is runtime state; whether it SHOULD
    // be is intent, and only intent is persisted. Deriving one from the other
    // is what made Connect a button the operator had to press every session:
    // a broker that died looked exactly like an operator who had turned the
    // account off.
    static bool IsManagedSecondary(Dictionary<string, object> acc)
    {
        string backend = S(acc, "execution_backend");
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        if (backend != "managed_windows_user" && !object.Equals(Get(meta, "managed"), true)) return false;
        string sid = AccountSid(acc);
        if (string.IsNullOrEmpty(sid)) return false;
        return !string.Equals(sid, WindowsIdentity.GetCurrent().User.Value, StringComparison.OrdinalIgnoreCase);
    }

    // An absent field means the operator never expressed a preference, not that
    // they declined: managed secondary accounts default on, so an AG2 that
    // predates this field comes back on its own instead of needing one visit
    // to Accounts to be taught the new shape. An explicit false is the only
    // thing that reads as a decision, and only Disconnect ever writes one.
    static bool AutoConnectOf(Dictionary<string, object> acc)
    {
        object raw = Get(acc, "auto_connect");
        if (raw is bool) return (bool)raw;
        return IsManagedSecondary(acc);
    }

    static int SetAutoConnect(Dictionary<string, object> acc, bool on)
    {
        string id = S(acc, "account_id");
        // The reason travels with the write, not around it: a desired-state
        // value with no recorded cause is exactly what made this unreadable.
        string reason = on ? "operator_connect" : "operator_disconnect";
        var done = Mutate(on ? "auto_connect_enabled" : "auto_connect_disabled", id,
            delegate(List<Dictionary<string, object>> accts, Dictionary<string, object> r)
            {
                foreach (var a in accts)
                    if (string.Equals(S(a, "account_id"), id, StringComparison.Ordinal))
                    {
                        Set(a, "auto_connect", on);
                        Set(a, "auto_connect_reason", reason);
                        Set(a, "auto_connect_changed_at", NowIso());
                    }
                return on ? "auto_connect_enabled" : "auto_connect_disabled";
            });
        return done == null ? 1 : 0;
    }

    // Why the desired state reads the way it does. The value alone cannot
    // answer that: an operator who pressed Disconnect and a broker that merely
    // failed both leave an account wanting no automatic connection, and the
    // operator is owed the difference.
    //
    // An account written before this field existed is answered from the audit
    // log rather than guessed -- events.log is append-only and already records
    // which verb wrote each generation, so that is evidence, not a heuristic.
    // Only a false nobody ever wrote falls through to "unknown", and the UI
    // must not claim a hand behind it.
    static string AutoConnectReasonOf(Dictionary<string, object> acc)
    {
        string stored = S(acc, "auto_connect_reason");
        if (!string.IsNullOrEmpty(stored)) return stored;
        if (!(Get(acc, "auto_connect") is bool) || (bool)Get(acc, "auto_connect")) return "default";

        string id = S(acc, "account_id");
        try
        {
            if (File.Exists(EventsPath()))
            {
                string want = null;
                foreach (string line in File.ReadAllLines(EventsPath()))
                {
                    if (line.IndexOf(id, StringComparison.Ordinal) < 0) continue;
                    if (line.IndexOf("auto_connect_disabled", StringComparison.Ordinal) >= 0) want = "operator_disconnect";
                    else if (line.IndexOf("auto_connect_enabled", StringComparison.Ordinal) >= 0) want = "operator_connect";
                }
                if (want != null) return want;
            }
        }
        catch (IOException) { /* an unreadable audit log must not break the read */ }
        catch (UnauthorizedAccessException) { }
        return "unknown";
    }

    // Generated presentation labels: provider-scoped ordinal, stabilized per
    // account via provider_ordinal so reordering accounts never swaps compact
    // labels or default display names across physical accounts.
    static void EnsureProviderOrdinals(List<Dictionary<string, object>> accounts)
    {
        if (accounts == null) return;
        var usedByProvider = new Dictionary<string, HashSet<int>>(StringComparer.OrdinalIgnoreCase);
        foreach (var acc in accounts)
        {
            string p = S(acc, "provider_id") ?? "";
            HashSet<int> used;
            if (!usedByProvider.TryGetValue(p, out used))
            {
                used = new HashSet<int>();
                usedByProvider[p] = used;
            }
            object raw = Get(acc, "provider_ordinal");
            if (raw is int && (int)raw > 0) used.Add((int)raw);
        }
        foreach (var acc in accounts)
        {
            string p = S(acc, "provider_id") ?? "";
            object raw = Get(acc, "provider_ordinal");
            if (raw is int && (int)raw > 0) continue;
            HashSet<int> used = usedByProvider[p];
            int next = 1;
            while (used.Contains(next)) next++;
            used.Add(next);
            Set(acc, "provider_ordinal", next);
        }
    }

    static int ProviderOrdinal(List<Dictionary<string, object>> accounts, Dictionary<string, object> acc)
    {
        object stored = Get(acc, "provider_ordinal");
        if (stored is int && (int)stored > 0) return (int)stored;
        string provider = S(acc, "provider_id");
        var used = new HashSet<int>();
        foreach (var other in accounts)
        {
            if (!string.Equals(S(other, "provider_id"), provider, StringComparison.OrdinalIgnoreCase)) continue;
            object oStored = Get(other, "provider_ordinal");
            if (oStored is int && (int)oStored > 0) used.Add((int)oStored);
        }
        int next = 1;
        foreach (var other in accounts)
        {
            if (!string.Equals(S(other, "provider_id"), provider, StringComparison.OrdinalIgnoreCase)) continue;
            object oStored = Get(other, "provider_ordinal");
            int ord;
            if (oStored is int && (int)oStored > 0)
            {
                ord = (int)oStored;
            }
            else
            {
                while (used.Contains(next)) next++;
                ord = next;
                used.Add(ord);
            }
            if (ReferenceEquals(other, acc) || string.Equals(S(other, "account_id"), S(acc, "account_id"), StringComparison.OrdinalIgnoreCase))
                return ord;
        }
        return next;
    }
    static string DisplayName(List<Dictionary<string, object>> accounts, Dictionary<string, object> acc)
    {
        string alias = S(acc, "display_name");
        if (alias != null) return alias;
        ProviderDef p; Providers.TryGetValue(S(acc, "provider_id") ?? "", out p);
        return (p != null ? p.Display : S(acc, "provider_id")) + " " + ProviderOrdinal(accounts, acc).ToString(CultureInfo.InvariantCulture);
    }
    static string CompactLabel(List<Dictionary<string, object>> accounts, Dictionary<string, object> acc)
    {
        ProviderDef p; Providers.TryGetValue(S(acc, "provider_id") ?? "", out p);
        return (p != null ? p.Compact : "AC") + ProviderOrdinal(accounts, acc).ToString(CultureInfo.InvariantCulture);
    }

    // ------------------------------------------------------------ registry
    static readonly string[] SecretShaped = { "password", "token", "secret", "cookie", "api_key", "apikey", "credential_blob", "refresh", "authorization" };
    static string FindSecretShaped(object obj, string path)
    {
        var dict = obj as Dictionary<string, object>;
        if (dict != null)
        {
            foreach (var kv in dict)
            {
                string keyLower = kv.Key.ToLowerInvariant();
                foreach (string bad in SecretShaped)
                    if (keyLower.Contains(bad) && !keyLower.Contains("credential_last_written") && !keyLower.Contains("credential_present"))
                        return path + "/" + kv.Key;
                string deep = FindSecretShaped(kv.Value, path + "/" + kv.Key);
                if (deep != null) return deep;
            }
            return null;
        }
        var list = obj as object[];
        if (list != null)
            for (int i = 0; i < list.Length; i++)
            {
                string deep = FindSecretShaped(list[i], path + "[" + i.ToString(CultureInfo.InvariantCulture) + "]");
                if (deep != null) return deep;
            }
        return null;
    }

    static string ContextKeyOf(Dictionary<string, object> acc)
    {
        string provider = S(acc, "provider_id") ?? "";
        string backend = S(acc, "execution_backend") ?? "";
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        if (IsWindowsUserBackend(backend))
            return provider + "|windows_user|" + (S(meta, "windows_sid") ?? S(acc, "execution_context_id") ?? "");
        return provider + "|" + backend + "|" + (S(meta, "profile_locator") ?? S(acc, "execution_context_id") ?? "");
    }

    static Dictionary<string, object> ParseRegistryText(string text, string source)
    {
        Dictionary<string, object> reg;
        try { reg = Json.DeserializeObject(text) as Dictionary<string, object>; }
        catch { throw new InvalidOperationException(source + ": registry JSON is malformed"); }
        if (!string.Equals(S(reg, "schema"), "sai.accounts/registry/2"))
            throw new InvalidOperationException(source + ": unsupported schema '" + S(reg, "schema") + "'");
        if (Get(reg, "accounts") as object[] == null)
            throw new InvalidOperationException(source + ": accounts[] missing");
        string secret = FindSecretShaped(Get(reg, "accounts"), "accounts");
        if (secret != null)
            throw new InvalidOperationException(source + ": secret-shaped field rejected: " + secret);
        var ids = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var contexts = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (object item in (object[])Get(reg, "accounts"))
        {
            var acc = item as Dictionary<string, object>;
            if (acc == null) throw new InvalidOperationException(source + ": malformed account record");
            string id = S(acc, "account_id");
            if (id == null) throw new InvalidOperationException(source + ": account without account_id");
            if (!ids.Add(id)) throw new InvalidOperationException(source + ": duplicate account_id " + id);
            string ctxKey = ContextKeyOf(acc);
            if (!contexts.Add(ctxKey)) throw new InvalidOperationException(source + ": duplicate execution context " + ctxKey);
            string state = S(acc, "operational_state") ?? "ENABLED";
            if (state != "ENABLED" && state != "DISABLED" && state != "FROZEN" && state != "ARCHIVED")
                throw new InvalidOperationException(source + ": invalid operational_state " + state);
        }
        return reg;
    }

    static Dictionary<string, object> LoadRegistry()
    {
        string path = RegistryPath();
        if (!File.Exists(path)) return null;
        return ParseRegistryText(File.ReadAllText(path, Encoding.UTF8), path);
    }

    static void EnsureScopeDir()
    {
        string dir = ScopeDir();
        if (Directory.Exists(dir)) return;
        Directory.CreateDirectory(Path.Combine(RootDir(), "scopes"));
        Directory.CreateDirectory(dir);
        // Least privilege: operator + SYSTEM only, inheritance off.
        var sec = new DirectorySecurity();
        sec.SetAccessRuleProtection(true, false);
        sec.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(OperatorSid()), FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        sec.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-18"), FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        new DirectoryInfo(dir).SetAccessControl(sec);
    }

    // W2-005: the scope lives in ProgramData, visible to every Windows session.
    // A Local\ named object is invisible outside its own session, so two
    // writers for the same operator scope in different sessions used to read
    // the same generation and collide on the shared staging pathname. The name
    // is machine-wide now.
    static string MutationMutexName()
    {
        return "Global\\SAI.Accounts.Mutation." + ScopeId();
    }

    // One reconciliation at a time per scope. The GUI timer and the durable
    // background keeper (Task Scheduler) may fire together; without this the
    // two sweeps could both observe a missing pipe and both ask the helper
    // to start the same context. The broker's own single-instance mutex
    // already makes that harmless, but a duplicate spawn attempt is still a
    // duplicate helper request with an observable second process launch -- so
    // the second sweep exits immediately with deduplicated=true instead of
    // probing or starting anything. Local\, like the mutation and broker
    // mutexes: same-session races are the ones this serializes.
    static string AutoconnectMutexName()
    {
        return "Local\\SAI.Accounts.Autoconnect." + ScopeId();
    }

    // Bounded keeper diagnostics. One line per autoconnect run: timestamp,
    // counts, elapsed, and categorized per-account failures. No SIDs, no
    // usernames, no tokens -- account_id and compact_label name the row and
    // the reason names the failure. Rotated at 256 KiB (one backup), so a
    // 2-minute cadence can never grow it without bound. Failures to write
    // the log never fail the reconciliation itself.
    static string KeeperLogPath()
    {
        // Under RootDir so the hermetic suites (SAI_ACCOUNTS_ROOT) keep
        // their logs inside the scratch plane, never on the live machine.
        return Path.Combine(RootDir(), "logs", "keeper-" + ScopeId() + ".log");
    }
    static void AppendKeeperLog(string line)
    {
        try
        {
            string path = KeeperLogPath();
            string dir = Path.GetDirectoryName(path);
            if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
            const long maxBytes = 256 * 1024;
            try
            {
                var info = new FileInfo(path);
                if (info.Exists && info.Length > maxBytes)
                {
                    string prev = path + ".1";
                    if (File.Exists(prev)) File.Delete(prev);
                    File.Move(path, prev);
                }
            }
            catch { }
            if (line.Length > 2048) line = line.Substring(0, 2048);
            File.AppendAllText(path, line + "\r\n", new UTF8Encoding(false));
        }
        catch { }
    }

    /// Serialized read-modify-write. mutate receives the accounts list and the
    /// registry dictionary; it may throw to abort. Returns the mutation result.
    // W2-005: journaled-before-commit audit outbox. Durability first, then
    // delivery; the caller never sees a failed mutation for a committed state.
    static void AppendAuditOutbox(string line)
    {
        using (var fs = new FileStream(AuditOutboxPath(), FileMode.Append, FileAccess.Write, FileShare.Read))
        using (var sw = new StreamWriter(fs, new UTF8Encoding(false)))
        {
            sw.Write(line + "\n");
            sw.Flush();
            fs.Flush(true);
        }
    }

    // Deliver every outbox line to events.log exactly once, then remove the
    // outbox. Returns the count still pending (0 when delivery succeeded).
    // Under the mutation lock, so only one writer drains at a time.
    static int DrainAuditOutbox()
    {
        string pending = AuditOutboxPath();
        try
        {
            if (!File.Exists(pending)) return 0;
            string[] lines;
            try { lines = File.ReadAllLines(pending, Encoding.UTF8); }
            catch { return PendingAuditLines(pending); }
            if (lines.Length == 0) { try { File.Delete(pending); } catch { } return 0; }
            using (var fs = new FileStream(EventsPath(), FileMode.Append, FileAccess.Write, FileShare.Read))
            using (var sw = new StreamWriter(fs, new UTF8Encoding(false)))
            {
                foreach (string l in lines)
                    if (l.Trim().Length > 0) sw.WriteLine(l);
                sw.Flush();
                fs.Flush(true);
            }
            try { File.Delete(pending); } catch { }
            return PendingAuditLines(pending);
        }
        catch { return PendingAuditLines(pending); }
    }

    static int PendingAuditLines(string pending)
    {
        try
        {
            if (!File.Exists(pending)) return 0;
            int n = 0;
            foreach (string l in File.ReadAllLines(pending, Encoding.UTF8)) if (l.Trim().Length > 0) n++;
            return n;
        }
        catch { return 1; }
    }

    static Dictionary<string, object> Mutate(string action, string accountId, Func<List<Dictionary<string, object>>, Dictionary<string, object>, object> mutate)
    {
        bool owned = false; Mutex mutex = null;
        try
        {
            mutex = new Mutex(true, MutationMutexName(), out owned);
            if (!owned) { mutex.WaitOne(); owned = true; }
            EnsureScopeDir();
            // Reconcile first: a previous writer may have committed a
            // generation whose audit line is still in the outbox.
            DrainAuditOutbox();
            Dictionary<string, object> reg = LoadRegistry();
            if (reg == null)
            {
                reg = Obj();
                Set(reg, "schema", "sai.accounts/registry/2");
                Set(reg, "generation", 0);
                var scope = Obj(); Set(scope, "scope_id", ScopeId()); Set(scope, "operator_sid", OperatorSid());
                Set(reg, "operator_scope", scope);
                Set(reg, "accounts", new object[0]);
            }
            var accountList = new List<Dictionary<string, object>>();
            foreach (object item in (object[])Get(reg, "accounts")) accountList.Add((Dictionary<string, object>)item);

            EnsureProviderOrdinals(accountList);
            object result = mutate(accountList, reg);
            var resultDict = result as Dictionary<string, object>;
            if (resultDict != null && object.Equals(Get(resultDict, "_early_return"), true))
            {
                resultDict.Remove("_early_return");
                return resultDict;
            }
            if (resultDict != null && string.IsNullOrEmpty(accountId))
                accountId = S(resultDict, "account_id");
            EnsureProviderOrdinals(accountList);

            // The account's own clock has to move with its own state. It did
            // not, and that is why a real desired-state change looked
            // unexplained: the account kept an updated_at from the day before
            // while its auto_connect had already been rewritten. Import carries
            // no single subject (accountId is null), so it stamps its own rows.
            // Position reorder moves the list slot without mutating the account's
            // own descriptor fields or updated_at timestamp.
            if (!string.IsNullOrEmpty(accountId) && action != "account_moved")
                foreach (var a in accountList)
                    if (string.Equals(S(a, "account_id"), accountId, StringComparison.Ordinal))
                        Set(a, "updated_at", NowIso());
            // validate + write back
            Set(reg, "accounts", accountList.ToArray());
            string secret = FindSecretShaped(Get(reg, "accounts"), "accounts");
            if (secret != null) throw new InvalidOperationException("secret-shaped field rejected: " + secret);
            ParseRegistryText(Json.Serialize(reg), "mutation");
            int generation = Convert.ToInt32(Get(reg, "generation"), CultureInfo.InvariantCulture) + 1;
            Set(reg, "generation", generation);

            // W2-005: one transaction, one mutation boundary. The event is
            // journaled in the outbox BEFORE the registry is published, so the
            // audit record exists for exactly the generation being committed.
            // A failure after this point can never tell the caller the mutation
            // failed: the committed generation is real, and at worst the audit
            // line stays undrained as audit_pending for the next writer.
            string evtResult = result is string ? (string)result : (resultDict != null ? (S(resultDict, "result") ?? "ok") : "ok");
            var evt = Obj();
            Set(evt, "timestamp", NowIso()); Set(evt, "account_id", accountId ?? "-");
            Set(evt, "action", action); Set(evt, "result", evtResult); Set(evt, "generation", generation);
            string evtLine = Json.Serialize(evt);
            AppendAuditOutbox(evtLine);

            // Unique staging pathname per transaction: two concurrent writers
            // can no longer read-modify-write through one shared .tmp file.
            string tmp = RegistryPath() + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                File.WriteAllText(tmp, Json.Serialize(reg), new UTF8Encoding(true));
                using (var fs = new FileStream(tmp, FileMode.Open, FileAccess.ReadWrite, FileShare.Read)) { fs.Flush(); fs.Flush(true); }
                if (File.Exists(RegistryPath())) File.Replace(tmp, RegistryPath(), null);
                else File.Move(tmp, RegistryPath());
            }
            finally
            {
                try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
            }

            int auditPending = DrainAuditOutbox();

            var response = Obj(); Set(response, "schema", "sai.accounts/mutate/1");
            Set(response, "status", "ok"); Set(response, "account_id", accountId);
            Set(response, "generation", generation); Set(response, "result", evtResult);
            if (auditPending > 0) Set(response, "audit_pending", auditPending);
            if (resultDict != null)
            {
                foreach (var kv in resultDict)
                    if (!response.ContainsKey(kv.Key)) Set(response, kv.Key, kv.Value);
            }
            return response;
        }
        finally
        {
            if (owned) { try { mutex.ReleaseMutex(); } catch { } }
            if (mutex != null) mutex.Close();
        }
    }

    static List<Dictionary<string, object>> AccountsOf(Dictionary<string, object> reg)
    {
        var list = new List<Dictionary<string, object>>();
        if (reg == null) return list;
        foreach (object item in Get(reg, "accounts") as object[] ?? new object[0])
        {
            var acc = item as Dictionary<string, object>;
            if (acc != null) list.Add(acc);
        }
        return list;
    }

    sealed class AmbiguousAccountSelectorException : Exception
    {
        public readonly string Selector;
        public readonly string[] Candidates;
        public AmbiguousAccountSelectorException(string selector, List<Dictionary<string, object>> accounts)
            : base("Account selector matches more than one account")
        {
            Selector = selector;
            var ids = new List<string>();
            foreach (var acc in accounts) ids.Add(S(acc, "account_id"));
            Candidates = ids.ToArray();
        }
    }

    static Dictionary<string, object> FindAccount(Dictionary<string, object> reg, string selector)
    {
        if (string.IsNullOrEmpty(selector)) return null;
        var accounts = AccountsOf(reg);
        // Stable selectors win over presentation: an alias named AG2 must
        // never redirect a command intended for the generated AG2 slot.
        foreach (var acc in accounts)
            if (string.Equals(S(acc, "account_id"), selector, StringComparison.OrdinalIgnoreCase)) return acc;
        var compactMatches = new List<Dictionary<string, object>>();
        foreach (var acc in accounts)
            if (string.Equals(CompactLabel(accounts, acc), selector, StringComparison.OrdinalIgnoreCase)) compactMatches.Add(acc);
        if (compactMatches.Count > 1) throw new AmbiguousAccountSelectorException(selector, compactMatches);
        if (compactMatches.Count == 1) return compactMatches[0];

        // Aliases, display labels and legacy ids are conveniences, not unique
        // identities. Refuse all collisions before a caller can perform an
        // operation; canonical order must never decide which account is used.
        var matches = new List<Dictionary<string, object>>();
        foreach (var acc in accounts)
        {
            var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
            if (string.Equals(S(acc, "display_name"), selector, StringComparison.OrdinalIgnoreCase)
                || string.Equals(DisplayName(accounts, acc), selector, StringComparison.OrdinalIgnoreCase)
                || (meta != null && string.Equals(S(meta, "legacy_id"), selector, StringComparison.OrdinalIgnoreCase)))
                matches.Add(acc);
        }
        if (matches.Count > 1) throw new AmbiguousAccountSelectorException(selector, matches);
        return matches.Count == 1 ? matches[0] : null;
    }

    // ------------------------------------------------------------- P/Invoke
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct StartupInfo
    {
        public uint cb; public string reserved, desktop, title;
        public uint x, y, xSize, ySize, xCount, yCount, fill, flags;
        public ushort show, reservedSize; public IntPtr reservedBytes, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo { public IntPtr process, thread; public uint pid, tid; }
    [StructLayout(LayoutKind.Sequential)]
    struct SecurityAttributes { public int length; public IntPtr descriptor; public int inherit; }
    [StructLayout(LayoutKind.Sequential)]
    struct Limits
    {
        public long processTime, jobTime; public uint flags;
        public UIntPtr minWorking, maxWorking; public uint active;
        public UIntPtr affinity; public uint priority, scheduling;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct ExtendedLimits
    {
        public Limits basic; public ulong readOps, writeOps, otherOps, readBytes, writeBytes, otherBytes;
        public UIntPtr processMemory, jobMemory, peakProcessMemory, peakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct Credential
    {
        public uint flags, type; public IntPtr target, comment; public long written;
        public uint size; public IntPtr blob; public uint persist, count;
        public IntPtr attributes, alias, username;
    }

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CredReadW(string target, uint type, uint flags, out IntPtr credential);
    [DllImport("advapi32.dll")] static extern void CredFree(IntPtr credential);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CredDeleteW(string target, uint type, uint flags);
    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool ImpersonateNamedPipeClient(IntPtr pipe);
    [DllImport("advapi32.dll")] static extern bool RevertToSelf();
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits limits, int size);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CreatePipe(out IntPtr read, out IntPtr write, ref SecurityAttributes attrs, uint size);
    [DllImport("kernel32.dll")] static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);

    // Named, not spelled inline. A Unicode environment block handed to
    // CreateProcess WITHOUT this flag is read as ANSI, the system walks the
    // UTF-16 bytes as single-byte names, finds them malformed and fails the
    // whole call with 87 (ERROR_INVALID_PARAMETER) -- so the flag is the
    // difference between a console that opens and one that never does. As a
    // magic 0x00000400 inside a call it was invisible to reading and to tests;
    // as a name it can be asserted on.
    const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
    const uint CREATE_NEW_CONSOLE = 0x10;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcess(string application, StringBuilder command, IntPtr processAttributes,
        IntPtr threadAttributes, bool inherit, uint flags, IntPtr environment, string cwd,
        ref StartupInfo startup, out ProcessInfo info);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle, uint ms);
    [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AttachConsole(uint dwProcessId);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool FreeConsole();
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr GetConsoleWindow();
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint GetConsoleProcessList(uint[] processList, uint count);
    [DllImport("user32.dll", SetLastError = true)]
    static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll", SetLastError = true)]
    static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll", SetLastError = true)]
    static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", SetLastError = true)]
    static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool SetPropW(IntPtr hWnd, string lpString, IntPtr hData);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool SetWindowTextW(IntPtr hWnd, string lpString);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern ushort GlobalAddAtomW(string lpString);
    [DllImport("kernel32.dll")] static extern ushort GlobalDeleteAtom(ushort atom);
    [DllImport("user32.dll", SetLastError = true)]
    static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll", SetLastError = true)]
    static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool SetConsoleTitleW(string lpConsoleTitle);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    static extern uint GetConsoleTitleW(StringBuilder text, uint count);

    // ------------------------------------------------- antigravity probing

    /// The PROVIDER ACCOUNT behind the stored Antigravity credential.
    ///
    /// Antigravity keeps a Google id_token under `gemini:antigravity`; its
    /// `sub` claim is the provider's own stable account id. It survives token
    /// refresh, reconnect and reboot, and changes only when a different human
    /// signs in — which is exactly the stability an account fingerprint needs
    /// and exactly what a token digest cannot provide.
    ///
    /// Quota is NOT an identity: two distinct accounts legitimately both read
    /// 100%. Only this claim can tell two slots apart, or prove them the same.
    ///
    /// The human-readable label comes from the SAME verified token, so it cannot
    /// disagree with the fingerprint above. It is a LABEL and nothing keys on it:
    /// `sub` stays the identity, which is what makes the fingerprint survive an
    /// email rename while still telling two humans apart.
    ///
    /// This runs inside the broker, i.e. already executing as the account's OWN
    /// Windows user, so it describes that account and nobody else's. Only the
    /// claims are read; no token byte is returned, logged or retained, and the
    /// fingerprint is a one-way digest so no address reaches a consumer. The
    /// email is returned because a local operator UI has to be able to answer
    /// "WHICH Google account is this?" without opening a console; it is never
    /// written to a routine log, and never leaves this process's response.
    static Dictionary<string, object> ProviderIdentityOf(IntPtr credBlob, uint blobSize)
    {
        var identity = Obj();
        Set(identity, "verified", false);
        try
        {
            if (credBlob == IntPtr.Zero || blobSize == 0) return identity;
            // Exact bytes, UTF-8: the blob is not ANSI and may contain bytes that
            // a string cast would truncate or recode.
            var bytes = new byte[blobSize];
            Marshal.Copy(credBlob, bytes, 0, (int)blobSize);
            return IdentityFromBlob(System.Text.Encoding.UTF8.GetString(bytes));
        }
        catch { return identity; }
    }

    /// The same projection, on the blob's TEXT.
    ///
    /// Split out from the pointer path so the rules above -- what identity is,
    /// what it is not, and what may leave this process -- can be tested against
    /// synthetic tokens, instead of against whichever Google account this machine
    /// happens to be signed in as. Testing identity through a real credential
    /// store would prove one fact and call it a rule.
    internal static Dictionary<string, object> IdentityFromBlob(string raw)
    {
        var identity = Obj();
        Set(identity, "verified", false);
        try
        {
            if (string.IsNullOrEmpty(raw)) return identity;
            var parsed = Json.DeserializeObject(raw) as Dictionary<string, object>;
            if (parsed == null) return identity;
            var idToken = Get(parsed, "id_token") as string;
            if (string.IsNullOrEmpty(idToken)) return identity;
            var parts = idToken.Split('.');
            if (parts.Length < 2) return identity;
            var payload = parts[1].Replace('-', '+').Replace('_', '/');
            switch (payload.Length % 4) { case 2: payload += "=="; break; case 3: payload += "="; break; }
            var claims = Json.DeserializeObject(Encoding.UTF8.GetString(Convert.FromBase64String(payload))) as Dictionary<string, object>;
            if (claims == null) return identity;
            var subject = Get(claims, "sub") as string;
            var email = Get(claims, "email") as string;
            var label = string.IsNullOrEmpty(email) ? null : email.Trim();
            if (!string.IsNullOrEmpty(subject))
            {
                Set(identity, "fingerprint", Fingerprint(subject.Trim().ToLowerInvariant()));
                Set(identity, "source", "google_id_token_sub");
                Set(identity, "verified", true);
                if (label != null) Set(identity, "account_label", label);
                return identity;
            }
            if (label != null)
            {
                // No sub: the token is still signed by Google, but the only
                // stable thing in it is the address, so both the fingerprint and
                // the label come from there and the source says so.
                Set(identity, "fingerprint", Fingerprint(label.ToLowerInvariant()));
                Set(identity, "source", "google_id_token_email");
                Set(identity, "verified", true);
                Set(identity, "account_label", label);
            }
            return identity;
        }
        catch { return identity; }
    }

    /// Drops Antigravity's Google credential from THIS Windows user's store.
    ///
    /// Narrow by construction, not by convention: there is no parameter, the
    /// target is a literal, and the only state it can reach is the store of
    /// whichever account this process runs as. Because the broker already runs
    /// as the selected account's own Windows user, "log AG2 out" cannot become
    /// "log AG1 out" -- there is no identifier in the call for one to arrive on.
    ///
    /// Measured, not guessed: the Antigravity profile holds no auth material in
    /// any file (a recursive scan for gemini:antigravity, id_token,
    /// refresh_token, oauth2 and accounts.google returns nothing), and cmdkey
    /// shows exactly one Antigravity target. Deleting it IS the provider
    /// logout; anything wider would be a generic credential-delete API, which
    /// this deliberately is not.
    ///
    /// It returns the identity that was there, so the caller can prove the slot
    /// changed identity rather than merely lost one, and it reports
    /// NOT_LOGGED_IN when there was nothing to drop.
    static Dictionary<string, object> AntigravityLogout()
    {
        var resp = Obj();
        Set(resp, "schema", "sai.accounts/provider_logout/1");
        Set(resp, "provider", "antigravity");
        Set(resp, "sid", WindowsIdentity.GetCurrent().User.Value);

        var before = CheckCredential();
        var beforeIdentity = Get(before, "provider_identity") as Dictionary<string, object>;
        Set(resp, "was_authenticated", object.Equals(Get(before, "credential_present"), true));
        if (beforeIdentity != null) Set(resp, "identity_before", beforeIdentity);

        // The one target, spelled once, never composed from a caller's input.
        if (!CredDeleteW("gemini:antigravity", 1, 0))
        {
            int err = Marshal.GetLastWin32Error();
            if (err != 1168 /* ERROR_NOT_FOUND */)
            {
                Set(resp, "status", "LOGOUT_FAILED");
                Set(resp, "win32_error", err);
                return resp;
            }
        }

        var after = CheckCredential();
        Set(resp, "status", object.Equals(Get(after, "credential_present"), true) ? "LOGOUT_INCOMPLETE" : "logged_out");
        Set(resp, "credential_present", Get(after, "credential_present"));
        return resp;
    }

    // In-process seam for an authenticated launch fixture; the shipped CLI
    // has no environment variable or command that replaces the native reader.
    internal static Func<Dictionary<string, object>> CredentialProbe = ReadCredential;

    public static Dictionary<string, object> CheckCredential()
    {
        return CredentialProbe();
    }

    static Dictionary<string, object> ReadCredential()
    {
        IntPtr ptr; bool present = CredReadW("gemini:antigravity", 1, 0, out ptr);
        var result = Obj();
        Set(result, "target", "gemini:antigravity");
        Set(result, "credential_present", present);
        if (present)
        {
            try
            {
                var cred = (Credential)Marshal.PtrToStructure(ptr, typeof(Credential));
                Set(result, "credential_last_written", cred.written);
                Set(result, "provider_identity", ProviderIdentityOf(cred.blob, cred.size));
            }
            finally { CredFree(ptr); }
        }
        else
        {
            var unknown = Obj(); Set(unknown, "verified", false);
            Set(result, "provider_identity", unknown);
        }
        return result;
    }

    /// Sandboxed capture: job object (kill-on-close), anonymous pipe for
    /// stdout+stderr, suspended start, 1 MiB output cap. Used for /usage and
    /// --version probes against the shared binary.
    public static Dictionary<string, object> RunCapture(string exe, string arguments, int timeoutMs)
    {
        return RunCapture(exe, arguments, timeoutMs, null);
    }

    /// Same sandbox, with an explicit CHILD environment. `envOverrides` maps a
    /// name to its value, or to null to mean "absent in this child". The block
    /// is built here and handed to CreateProcess; this process's own
    /// environment is never touched, which is the whole point -- see
    /// AgyCaptureEnvironment.
    public static Dictionary<string, object> RunCapture(string exe, string arguments, int timeoutMs,
        Dictionary<string, string> envOverrides)
    {
        return RunCapture(exe, arguments, timeoutMs, envOverrides, null, false, 1);
    }

    /// `workingDirectory` null means the caller's own profile (the probe
    /// default). `keepOutputOnFailure` hands back stdout of a non-zero exit:
    /// a JSON-speaking child that refuses still says WHY on stdout, and
    /// dropping that answer is how a refusal turns into "probe_failed".
    /// `maxActiveProcesses` 0 lifts the job's process-count cap (a launcher
    /// script that starts its interpreter, which starts git, is three) while
    /// kill-on-close still reaps the whole tree.
    public static Dictionary<string, object> RunCapture(string exe, string arguments, int timeoutMs,
        Dictionary<string, string> envOverrides, string workingDirectory, bool keepOutputOnFailure, int maxActiveProcesses)
    {
        IntPtr job = IntPtr.Zero, read = IntPtr.Zero, write = IntPtr.Zero, envBlock = IntPtr.Zero;
        ProcessInfo proc = new ProcessInfo();
        var result = Obj();
        Thread reader = null; var output = new StringBuilder(); bool readerFailed = false, tooLarge = false;
        try
        {
            job = CreateJobObject(IntPtr.Zero, null);
            var limits = new ExtendedLimits();
            limits.basic.flags = maxActiveProcesses > 0 ? 0x2008u : 0x2000u;
            limits.basic.active = (uint)Math.Max(0, maxActiveProcesses);
            if (job == IntPtr.Zero || !SetInformationJobObject(job, 9, ref limits, Marshal.SizeOf(limits))) throw new InvalidOperationException("job");
            var attrs = new SecurityAttributes { length = Marshal.SizeOf(typeof(SecurityAttributes)), inherit = 1 };
            if (!CreatePipe(out read, out write, ref attrs, 0) || !SetHandleInformation(read, 1, 0)) throw new InvalidOperationException("pipe");
            var startup = new StartupInfo { cb = (uint)Marshal.SizeOf(typeof(StartupInfo)), flags = 0x100, output = write, error = write, input = IntPtr.Zero };
            Set(result, "process_started_at", NowIso());
            string cwd = workingDirectory ?? Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            envBlock = BuildEnvironmentBlock(envOverrides);
            uint createFlags = 0x08000004;
            if (envBlock != IntPtr.Zero) createFlags |= CREATE_UNICODE_ENVIRONMENT;
            if (!CreateProcess(exe, new StringBuilder("\"" + exe + "\" " + arguments),
                IntPtr.Zero, IntPtr.Zero, true, createFlags, envBlock, cwd, ref startup, out proc)) throw new InvalidOperationException("start");
            if (!AssignProcessToJobObject(job, proc.process)) throw new InvalidOperationException("job-assign");
            CloseHandle(write); write = IntPtr.Zero;
            IntPtr ownedRead = read; read = IntPtr.Zero;
            reader = new Thread((ThreadStart)delegate
            {
                try
                {
                    using (var handle = new Microsoft.Win32.SafeHandles.SafeFileHandle(ownedRead, true))
                    using (var stream = new FileStream(handle, FileAccess.Read))
                    using (var sr = new StreamReader(stream, Encoding.UTF8, true))
                    {
                        char[] buffer = new char[4096]; int count;
                        while ((count = sr.Read(buffer, 0, buffer.Length)) > 0)
                            if (output.Length + count <= 1024 * 1024) output.Append(buffer, 0, count); else tooLarge = true;
                    }
                }
                catch { readerFailed = true; }
            });
            reader.IsBackground = true; reader.Start();
            if (ResumeThread(proc.thread) == uint.MaxValue) throw new InvalidOperationException("resume");
            Set(result, "process_id", proc.pid);
            if (WaitForSingleObject(proc.process, (uint)Math.Max(1000, timeoutMs)) != 0) { Set(result, "status", "probe_timeout"); return result; }
            Set(result, "process_ended_at", NowIso());
            uint code;
            if (!GetExitCodeProcess(proc.process, out code)) throw new InvalidOperationException("exit");
            if (!reader.Join(3000) || readerFailed || tooLarge) { Set(result, "status", "unreadable_usage"); return result; }
            if (code != 0)
            {
                Set(result, "status", "probe_failed"); Set(result, "exit_code", code);
                if (keepOutputOnFailure) Set(result, "stdout", output.ToString());
                return result;
            }
            Set(result, "exit_code", 0);
            Set(result, "stdout", output.ToString());
            Set(result, "status", "captured");
            return result;
        }
        catch (Exception ex)
        {
            Set(result, "status", "guard_or_start_failed");
            Set(result, "error", ex.Message);
            return result;
        }
        finally
        {
            if (proc.process != IntPtr.Zero) { TerminateProcess(proc.process, 1); CloseHandle(proc.process); }
            if (proc.thread != IntPtr.Zero) CloseHandle(proc.thread);
            if (write != IntPtr.Zero) CloseHandle(write);
            if (read != IntPtr.Zero) CloseHandle(read);
            if (job != IntPtr.Zero) CloseHandle(job);
            if (envBlock != IntPtr.Zero) Marshal.FreeHGlobal(envBlock);
            if (reader != null) reader.Join(3000);
        }
    }

    /// A Windows environment block (UTF-16, NAME=VALUE\0 ... \0) for a child,
    /// the current environment with `overrides` applied -- null meaning the
    /// name is ABSENT in the child rather than copied. IntPtr.Zero means
    /// "inherit", which is what every caller without overrides wants.
    static IntPtr BuildEnvironmentBlock(Dictionary<string, string> overrides)
    {
        if (overrides == null) return IntPtr.Zero;
        var merged = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (System.Collections.DictionaryEntry kv in Environment.GetEnvironmentVariables())
        {
            string name = kv.Key as string, value = kv.Value as string;
            // Names starting with '=' are the drive-letter pseudo-variables; a
            // hand-built block must not carry them.
            if (string.IsNullOrEmpty(name) || name[0] == '=' || value == null) continue;
            merged[name] = value;
        }
        foreach (var kv in overrides)
        {
            if (kv.Value == null) merged.Remove(kv.Key);
            else merged[kv.Key] = kv.Value;
        }
        var sb = new StringBuilder();
        foreach (var kv in merged) sb.Append(kv.Key).Append('=').Append(kv.Value).Append('\0');
        sb.Append('\0');
        return Marshal.StringToHGlobalUni(sb.ToString());
    }

    static readonly Regex IsoTime = new Regex(@"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$", RegexOptions.Compiled);

    public static Dictionary<string, object> SafeUsage(string text)
    {
        var result = Obj(); Set(result, "status", "unreadable_usage");
        // "windows" is part of this result's shape on every path. It was only set on
        // the way out at the bottom, so a caller that reads it on an early return --
        // unreadable output, no groups -- got a KeyNotFoundException instead of an
        // empty answer, and the whole connection died with it.
        Set(result, "windows", new object[0]);
        object payload;
        try { payload = Json.DeserializeObject(text); } catch { return result; }
        if (!object.Equals(Get(payload, "status"), "SUCCESS")) return result;
        var groups = Get(Get(Get(payload, "command"), "data"), "groups") as object[];
        if (groups == null) return result;
        var windows = new List<object>();
        for (int i = 0; i < groups.Length; i++)
        {
            var buckets = Get(groups[i], "buckets") as object[];
            if (buckets == null) continue;
            foreach (object bucket in buckets)
            {
                string window = Get(bucket, "window") as string;
                if (window != "5h" && window != "weekly" && window != "monthly" && window != "7d" && window != "30d") continue;
                object raw = Get(bucket, "remaining_fraction");
                if (!(raw is double || raw is decimal || raw is int || raw is long)) continue;
                double fraction = Convert.ToDouble(raw, CultureInfo.InvariantCulture);
                if (double.IsNaN(fraction) || double.IsInfinity(fraction) || fraction < 0 || fraction > 1) continue;
                var row = Obj();
                Set(row, "pool_index", i); Set(row, "window", window);
                Set(row, "remaining_fraction", fraction);
                Set(row, "disabled", object.Equals(Get(bucket, "disabled"), true));
                string reset = Get(bucket, "reset_time") as string;
                if (reset != null && IsoTime.IsMatch(reset)) Set(row, "reset_time", reset);
                windows.Add(row);
            }
        }
        Set(result, "status", windows.Count > 0 ? "authenticated_usage" : "unreadable_usage");
        Set(result, "windows", windows.ToArray());
        return result;
    }

    // The agy scrub is a CHILD contract, never a process-wide one.
    //
    // It used to be Environment.SetEnvironmentVariable over this process's own
    // environment, which is fine for a one-shot CLI and wrong for the broker: a
    // broker is long-lived, so scrubbing it to shape ONE /usage capture also
    // reshaped every child it started afterwards -- including the Antigravity
    // sign-in window. That window then inherited BROWSER=<System32>\where.exe,
    // there was no browser to open the Google sign-in URL with, and Windows
    // answered it with a "Windows cannot find ..." popup while the account
    // stayed AUTH_REQUIRED forever. The overrides below belong to the capture
    // child alone; the sign-in and console children inherit the operator's real
    // environment, apart from transient capture-only console color hints.
    static readonly string[] AgyScrubbedNames = { "SSH_CONNECTION", "SSH_TTY", "AGY_CLI_INTERACTIVE_HEADLESS",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "AGY_ADC_AUTH", "GOOGLE_APPLICATION_CREDENTIALS" };

    static Dictionary<string, string> AgyCaptureEnvironment()
    {
        // null value = the name must be ABSENT in the child, not copied.
        var over = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (string name in AgyScrubbedNames) over[name] = null;
        over["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true";
        over["AGY_CLI_HIDE_ACCOUNT_INFO"] = "true";
        over["BROWSER"] = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "where.exe");
        return over;
    }

    // Why this identity cannot use this directory, as a CLOSED enum, or null
    // when it can. Returning the enum instead of an exception message is what
    // keeps GUI decisions off string matching: NOT_FOUND is a wrong folder,
    // ACCESS_DENIED is a right folder this SID cannot read (provisionable),
    // NOT_READABLE is anything else and is never provisioned against.
    // Callers run under DIFFERENT identities for the front plane and the
    // broker, so this must be evaluated by whoever is actually going to
    // open the directory -- never cached from the operator's own check.
    // Directory.Exists answers FALSE both for a directory that is not there and
    // for one this identity may not enter -- it swallows every error, so
    // concluding "missing" from it is what made a managed account report
    // NOT_FOUND for a project that exists but belongs to somebody else. NOT_FOUND
    // is the one reason that must never reach provisioning, so the reason is
    // read from the system instead of inferred: open the directory for real and
    // ask the kernel which of the two it was.
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr security,
        uint disposition, uint flags, IntPtr template);

    const uint FileListDirectory = 0x00000001, SynchronizeAccess = 0x00100000;
    const uint ShareReadWriteDelete = 0x00000001 | 0x00000002 | 0x00000004;
    const uint OpenExisting = 3, BackupSemantics = 0x02000000;

    // The only paths the access probe may touch, and they are constants, not
    // caller input. The probe answers exactly one question -- "can this
    // identity create, read, modify and delete inside that directory" -- so
    // it must never be able to answer "can it touch that file instead".
    const string ProbeSubdir = ".saituls-access-probe";
    const string ProbeFile = "probe.txt";

    static string WorkdirDenialReason(string workdir)
    {
        if (string.IsNullOrEmpty(workdir)) return "NOT_FOUND";
        string path;
        try { path = Path.GetFullPath(workdir); }
        catch { return "NOT_FOUND"; }

        IntPtr h = CreateFileW(path, FileListDirectory | SynchronizeAccess, ShareReadWriteDelete,
            IntPtr.Zero, OpenExisting, BackupSemantics, IntPtr.Zero);
        if (h != IntPtr.Zero && h != new IntPtr(-1))
        {
            CloseHandle(h);
            try { Directory.GetDirectories(path); return null; }
            catch (UnauthorizedAccessException) { return "ACCESS_DENIED"; }
            catch (Exception) { return "NOT_READABLE"; }
        }

        switch (Marshal.GetLastWin32Error())
        {
            case 2:      // ERROR_FILE_NOT_FOUND
            case 3:      // ERROR_PATH_NOT_FOUND
            case 267:    // ERROR_DIRECTORY
                return "NOT_FOUND";
            case 5:      // ERROR_ACCESS_DENIED
                return "ACCESS_DENIED";
            case 32:     // ERROR_SHARING_VIOLATION
                return "NOT_READABLE";
            default:
                // Unknown denial. Treating it as NOT_FOUND would skip the grant
                // and send the operator off to pick another folder; treating it
                // as ACCESS_DENIED costs one bounded, minimum-rights grant that
                // is a no-op when the rights already exist.
                return "ACCESS_DENIED";
        }
    }

    // The one directory this process can always open: its own user profile.
    // Used ONLY to bootstrap interactive authentication. It is deliberately
    // never offered to, or remembered as, a launch workdir -- a project
    // console that silently opened somewhere else is worse than one that
    // fails and says why.
    static string OwnDirectory()
    {
        try
        {
            string dir = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            if (string.IsNullOrEmpty(dir)) return null;
            if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
            Directory.GetDirectories(dir);
            return dir;
        }
        catch { return null; }
    }

    // ---------------------------------------------------------------- broker
    static string ContextFingerprintOf(Dictionary<string, object> acc)
    {
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        string backend = S(acc, "execution_backend");
        string identity = backend == "windows_user" ? S(meta, "windows_sid") : S(meta, "profile_locator");
        return Fingerprint(identity == null ? "" : identity);
    }
    /// The Windows SID of an account's execution identity.
    ///
    /// provider_metadata.windows_sid is its canonical home, but a registry
    /// written before that field was populated has only the user NAME -- which
    /// is what the operator's machine actually holds: AG1 (unmanaged, "vac34"),
    /// AG2 and AG3 all carry windows_user and no windows_sid. Reading the name
    /// back through Windows is exact (a local account name is unique on a
    /// machine), and without it every launch of such an account compared null
    /// against the current SID, fell through to a broker pipe named after the
    /// fingerprint of nothing, and answered CONTEXT_OFFLINE for ever: the
    /// console never opened and the window it would have opened in had already
    /// closed. The name is a locator, never an authorization: it can only ever
    /// name an account that already exists on this machine.
    static string AccountSid(Dictionary<string, object> acc)
    {
        if (acc == null) return null;
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        string sid = S(meta, "windows_sid");
        if (!string.IsNullOrEmpty(sid)) return sid;
        string user = S(meta, "windows_user");
        if (string.IsNullOrEmpty(user)) return null;
        if (string.Equals(user, Environment.UserName, StringComparison.OrdinalIgnoreCase))
            return WindowsIdentity.GetCurrent().User.Value;
        try { return ((SecurityIdentifier)new NTAccount(user).Translate(typeof(SecurityIdentifier))).Value; }
        catch { return null; }
    }

    static string PipeNameFor(string scopeId, string contextFingerprint)
    {
        return "SAI.Accounts." + scopeId + "." + contextFingerprint;
    }

    static string ReadLineBounded(StreamReader reader, int maxChars, out bool tooLarge)
    {
        tooLarge = false;
        var sb = new StringBuilder();
        char[] one = new char[1];
        while (true)
        {
            int n = reader.Read(one, 0, 1);
            if (n <= 0) break;
            if (one[0] == '\n') break;
            if (one[0] != '\r') sb.Append(one[0]);
            if (sb.Length > maxChars) { tooLarge = true; return null; }
        }
        if (sb.Length == 0) return null;
        return sb.ToString();
    }

    // Is this the client hanging up, rather than the broker failing?
    //
    // A short-lived `sai-accounts` client is supposed to connect, send one
    // request, read one response and close the pipe. Once it has done that --
    // and especially once it has closed -- the server side can still observe a
    // disconnected pipe: from the write that carried the response, from the
    // reader or writer, or from the NamedPipeServerStream itself. The peer got
    // what it came for and left. That is the protocol working, not a failure, so
    // it must not print an error, back off, or put the context in an error state.
    //
    // Two ways the same fact reaches us, which is why matching the HResult alone
    // is not enough:
    //
    //   * with a Win32 code -- ERROR_NO_DATA surfaces as HResult 0x800700E8;
    //   * with none at all. The framework's pipe code raises a plain IOException
    //     built from a localized string: "Pipe is broken." arrives as HResult
    //     0x80131620 (COR_E_IO), whose low word is 5664 and means nothing.
    //
    // Narrow on purpose: three Win32 codes plus the closed set of pipe-disconnect
    // messages the framework can produce, all of them on an IOException (or the
    // Win32Exception it wraps). Everything else keeps the loud path and its
    // backoff -- a blanket `catch (IOException)` would make every normal
    // disconnect quiet while hiding a genuinely broken broker, which is the one
    // failure this broker has to stay able to see.
    static bool IsExpectedPipeDisconnect(Exception ex)
    {
        const int ERROR_BROKEN_PIPE = 109;          // peer closed after we wrote
        const int ERROR_NO_DATA = 232;              // peer closed mid-exchange
        const int ERROR_PIPE_NOT_CONNECTED = 233;   // peer was never there
        for (Exception e = ex; e != null; e = e.InnerException)
        {
            int code = -1;
            var io = e as IOException;
            if (io != null) code = io.HResult & 0xFFFF;
            else
            {
                var w32 = e as System.ComponentModel.Win32Exception;
                if (w32 != null) code = w32.NativeErrorCode;
                else continue;
            }
            if (code == ERROR_BROKEN_PIPE || code == ERROR_NO_DATA
                || code == ERROR_PIPE_NOT_CONNECTED) return true;
            // The Win32 code never reached HResult: match the wording instead,
            // and only the pipe-disconnect wordings.
            string m = (e.Message ?? "").Trim();
            if (m.Length == 0) continue;
            if (m.IndexOf("Pipe is broken", StringComparison.OrdinalIgnoreCase) >= 0) return true;
            if (m.IndexOf("pipe has been ended", StringComparison.OrdinalIgnoreCase) >= 0) return true;
            if (m.IndexOf("pipe has been closed", StringComparison.OrdinalIgnoreCase) >= 0) return true;
            if (m.IndexOf("pipe is being closed", StringComparison.OrdinalIgnoreCase) >= 0) return true;
            if (m.IndexOf("No process is on the other end of the pipe", StringComparison.OrdinalIgnoreCase) >= 0) return true;
        }
        return false;
    }

    static void LogBroker(string msg)
    {
        try
        {
            string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "SAI", "Accounts", "logs");
            Directory.CreateDirectory(dir);
            File.AppendAllText(Path.Combine(dir, "broker.log"), DateTime.UtcNow.ToString("o") + " [" + Process.GetCurrentProcess().Id + "] " + msg + "\r\n");
        }
        catch { }
    }

    public static void RunBroker(string expectedSid, string scopeId, string operatorSid)
    {
        try { RunBrokerInner(expectedSid, scopeId, operatorSid); }
        catch (Exception ex) { LogBroker("RunBroker crash: " + ex.ToString()); }
    }

    static void RunBrokerInner(string expectedSid, string scopeId, string operatorSid)
    {
        LogBroker("RunBroker start. expected=" + expectedSid + " scope=" + scopeId + " op=" + operatorSid);
        string currentSid = WindowsIdentity.GetCurrent().User.Value;
        bool isTest = Environment.GetEnvironmentVariable("SAI_HELPER_TEST_MODE") == "1";
        if (!isTest && !string.Equals(currentSid, expectedSid, StringComparison.OrdinalIgnoreCase))
        {
            var o = Obj(); Set(o, "status", "context_mismatch");
            Set(o, "expected_sid", expectedSid); Set(o, "current_sid", currentSid);
            LogBroker("Context mismatch: current=" + currentSid + " expected=" + expectedSid);
            Print(o); return;
        }
        if (string.IsNullOrEmpty(scopeId)) scopeId = ScopeId();
        string contextFp = Fingerprint(expectedSid);
        string pipeName = PipeNameFor(scopeId, contextFp);

        bool createdNew;
        using (var single = new Mutex(true, "Local\\SAI.Accounts.Broker." + scopeId + "." + contextFp, out createdNew))
        {
            if (!createdNew)
            {
                var o = Obj(); Set(o, "status", "already_running"); Set(o, "pipe", pipeName);
                LogBroker("Already running on " + pipeName);
                Print(o); return;
            }

            try { Console.Title = "SAI Accounts broker " + contextFp; } catch { }
            try { Console.WriteLine("broker sid=" + currentSid + " scope=" + scopeId + " pipe=" + pipeName); } catch { }
            LogBroker("Acquired single mutex, creating pipe " + pipeName);

            var sec = new PipeSecurity();
            sec.AddAccessRule(new PipeAccessRule(WindowsIdentity.GetCurrent().User, PipeAccessRights.FullControl, AccessControlType.Allow));
            sec.AddAccessRule(new PipeAccessRule(new SecurityIdentifier("S-1-5-18"), PipeAccessRights.ReadWrite, AccessControlType.Allow));
            if (!string.IsNullOrEmpty(operatorSid))
            {
                try { sec.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(operatorSid), PipeAccessRights.ReadWrite, AccessControlType.Allow)); }
                catch (Exception ex) { LogBroker("operator ACL rejected: " + ex.Message); }
            }
            // No BUILTIN\Users rule. Ever.

            var allowed = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { currentSid, "S-1-5-18" };
            if (!string.IsNullOrEmpty(operatorSid)) allowed.Add(operatorSid);

            LogBroker("Listening on pipe " + pipeName);

            // One connection at a time is not "simple", it is wrong. "usage" shells
            // out to the agy binary and can hold the broker for 30 seconds, and for
            // that whole window the pipe NAME is out of the namespace -- so doctor,
            // and the GUI's own re-probe right after Connect, both see a perfectly
            // healthy context as OFFLINE. Keep the next instance waiting and serve
            // each connection on its own thread: the name stays resident, and one
            // slow caller can no longer make a live broker look dead.
            var stop = new ManualResetEvent(false);
            var mutating = new object();
            Func<NamedPipeServerStream> newInstance = delegate
            {
                return new NamedPipeServerStream(pipeName, PipeDirection.InOut, BrokerPipeInstances,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 65536, 65536, sec);
            };

var slot = new PipeSlot();
        slot.Current = newInstance();
        while (!stop.WaitOne(0))
        {
            NamedPipeServerStream conn;
            try
            {
                // No timeout overload on this profile, so the wait is blocking.
                // Shutdown breaks it by disposing the waiting instance, which
                // surfaces here as an exception rather than a silent hang.
                slot.Current.WaitForConnection();
            }
catch (Exception ex)
                {
                    if (stop.WaitOne(0)) break;
                    // A client that vanishes mid-connect aborts the wait with the
                    // same narrow set of codes a mid-conversation hangup does. That
                    // is a client event, not a broker fault: same silence, same
                    // reason. Anything outside that set is logged and backed off.
                    if (!IsExpectedPipeDisconnect(ex))
                    {
                        Console.WriteLine("broker loop: " + ex.Message);
                        Thread.Sleep(500);
                    }
                    try { slot.Current.Dispose(); } catch { }
                    try { slot.Current = newInstance(); } catch { Thread.Sleep(200); }
                    continue;
                }
            conn = slot.Current;

            // Refill BEFORE the accepted instance leaves this loop. The two must
            // not overlap: if the refill throws, retrying the wait would run on
            // an instance already handed to a worker, and every retry would
            // consume another one until the name could not be served at all.
            // The cap is transiently reached when many clients connect at once,
            // and every in-flight worker disposes its own instance as it
            // finishes, so this is a wait, not a failure.
            NamedPipeServerStream next = null;
            for (int attempt = 0; attempt < 25 && next == null; attempt++)
            {
                try { next = newInstance(); }
                catch { Thread.Sleep(100); }
            }
            if (next == null)
            {
                Console.WriteLine("broker loop: could not refill after 25 attempts, stopping accept");
                try { conn.Dispose(); } catch { }
                break;
            }
            slot.Current = next;

            // The client closed on us, or not: that is a connection-level event
            // now, not a loop-level one. Logging it here used to make every
            // ordinary disconnect look like a broker failure.
            ThreadStart serve = delegate
            {
                try { ServeBrokerConnection(conn, allowed, currentSid, contextFp, stop, mutating, slot); }
                catch (Exception ex)
                    {
                        // A client that hangs up is this connection ending, not the
                        // broker. Only an exception outside that narrow set is news.
                        if (!IsExpectedPipeDisconnect(ex)) Console.WriteLine("broker connection: " + ex.Message);
                    }
                finally { try { conn.Dispose(); } catch { } }
            };
            var worker = new Thread(serve);
            worker.IsBackground = true;
            worker.Start();
        }
        try { slot.Current.Dispose(); } catch { }
        }
    }

    // Holds the instance currently waiting for a client, so the shutdown path --
    // which runs on a connection thread -- can break the accept loop's blocking
    // wait. A captured local cannot be read by reference from another thread.
    sealed class PipeSlot
    {
        public NamedPipeServerStream Current;
    }

    // Instances kept resident at once. Sized against the callers that can actually
    // be in flight -- a probe, a status, a usage, an agent launch -- plus enough
    // headroom that a burst of clients never transiently exhausts the cap and
    // forces the accept loop to wait on a refill.
    const int BrokerPipeInstances = 64;

    // Serves ONE client. The caller owns the stream and disposes it. Reads are
    // independent; "launch" is serialized because two callers starting the context
    // at the same time would leave the operator with two Antigravity windows.
    static void ServeBrokerConnection(NamedPipeServerStream server, HashSet<string> allowed,
        string currentSid, string contextFp, ManualResetEvent stop, object mutating, PipeSlot slot)
    {
        // Verify the connected client identity, not just the pipe name.
        string clientSid = null;
        try
        {
            if (ImpersonateNamedPipeClient(server.SafePipeHandle.DangerousGetHandle()))
            {
                try
                {
                    using (var client = WindowsIdentity.GetCurrent())
                        clientSid = client.User != null ? client.User.Value : null;
                }
                finally { RevertToSelf(); }
            }
        }
        catch { clientSid = null; }

        // leaveOpen: the caller disposes the stream once this method returns. A
        // reader that closes what it wraps would tear the connection down while
        // the reply is still being written.
        using (var reader = new StreamReader(server, Encoding.UTF8, false, 1024, true))
        using (var writer = new StreamWriter(server, new UTF8Encoding(false), 1024, true) { AutoFlush = true })
        {
            if (clientSid == null || !allowed.Contains(clientSid))
            {
                var denied = Obj(); Set(denied, "status", "ACCESS_DENIED");
                Set(denied, "client_sid", clientSid);
                writer.WriteLine(Json.Serialize(denied));
                return;
            }
            bool tooLarge;
            string line = ReadLineBounded(reader, 8192, out tooLarge);
            if (tooLarge)
            {
                var big = Obj(); Set(big, "status", "REQUEST_TOO_LARGE");
                writer.WriteLine(Json.Serialize(big)); return;
            }
            if (line == null) return;
            Dictionary<string, object> req = null;
            try { req = Json.DeserializeObject(line) as Dictionary<string, object>; } catch { }
            if (req == null)
            {
                var bad = Obj(); Set(bad, "status", "MALFORMED_JSON");
                writer.WriteLine(Json.Serialize(bad)); return;
            }
            string cmd = Get(req, "cmd") as string;
            var resp = Obj(); Set(resp, "context", contextFp);

            if (cmd == "status")
            {
                Set(resp, "status", "ok");
                Set(resp, "credential", CheckCredential());
                // Report the identity we actually run as. Without this
                // nothing outside this process can prove the broker is
                // the managed account rather than whoever launched it.
                Set(resp, "sid", currentSid);
                Set(resp, "pid", Process.GetCurrentProcess().Id);
            }
            else if (cmd == "usage")
            {
                var capture = RunCapture(SharedAgyPath(), "-p \"/usage\" --output-format json --log-file NUL", 30000,
                    AgyCaptureEnvironment());
                string stdout = S(capture, "stdout");
                var usage = SafeUsage(stdout == null ? "" : stdout);
                foreach (var kv in capture) if (kv.Key != "stdout") Set(resp, kv.Key == "status" ? "capture_status" : kv.Key, kv.Value);
                Set(resp, "status", S(usage, "status"));
                Set(resp, "windows", usage["windows"]);
                Set(resp, "credential", CheckCredential());
            }
            else if (cmd == "launch")
            {
                // launch accepts workdir and canonical presentation identity.
                // Any attempt to name a program or vendor arguments is refused.
                string[] banned = { "exe", "command", "command_line", "program", "args", "path", "binary", "script" };
                string bannedField = null;
                foreach (string bf in banned) if (req.ContainsKey(bf)) { bannedField = bf; break; }
                if (bannedField != null)
                {
                    Set(resp, "status", "OPERATION_NOT_ALLOWED");
                    Set(resp, "rejected_field", bannedField);
                    writer.WriteLine(Json.Serialize(resp));
                    return;
                }
                // Missing and inaccessible are different failures and the
                // operator needs to tell them apart: a NOT_FOUND path is a
                // wrong folder, an ACCESS_DENIED path is a valid folder this
                // managed SID cannot read, and only the second one is worth
                // provisioning against. Collapsing them into one status sent
                // the operator to pick a different folder for a permissions
                // problem, which is exactly the dead end this enum removes.
                string workdir = Get(req, "workdir") as string;
                string denial = WorkdirDenialReason(workdir);
                if (denial != null)
                {
                    Set(resp, "status", "WORKDIR_UNAVAILABLE");
                    Set(resp, "workdir", workdir);
                    Set(resp, "reason", denial);
                }
                else
                {
                    lock (mutating)
                    {
                        try
                        {
                            string accountId = S(req, "account_id");
                            string compact = S(req, "compact_label");
                            // The operator's registry is deliberately not readable by
                            // this SID. The canonical engine supplies its derived label;
                            // the broker binds the identity to its own verified context.
                            if (!string.Equals(accountId, "antigravity:windows-user:" + contextFp, StringComparison.Ordinal)
                                || compact == null || !System.Text.RegularExpressions.Regex.IsMatch(compact, "^AG[1-9][0-9]{0,8}$"))
                            {
                                Set(resp, "status", "LAUNCH_IDENTITY_REQUIRED");
                                writer.WriteLine(Json.Serialize(resp));
                                return;
                            }
                            var psi = AntigravityLaunch(workdir, accountId, compact);
                            var proc = Process.Start(psi);
                            Set(resp, "status", "launched");
                            Set(resp, "pid", proc != null ? (object)proc.Id : 0);
                            Set(resp, "launch_policy", AntigravityArgumentLine());
                            Set(resp, "account_id", accountId);
                            Set(resp, "sid", currentSid);
                            Set(resp, "console_title", AntigravityConsoleTitle(workdir, compact));
                        }
                        catch (Exception ex) { Set(resp, "status", "launch_failed"); Set(resp, "error", ex.Message); }
                    }
                }
            }
            else if (cmd == "workdir_probe")
            {
                // The cross-identity filesystem oracle. It runs IN THIS
                // PROCESS, under the managed account's own token, and touches
                // exactly one fixed disposable file in one fixed reserved
                // subdirectory of the named directory. It launches no
                // process, and it takes no program, command, argument or file
                // name from the caller -- so it is not arbitrary execution
                // and not a generic permission API. Reading an ACL back as
                // the operator only proves what the ACL SAYS; this proves what
                // the account can actually DO, which is the whole claim.
                string[] bannedProbe = { "exe", "command", "command_line", "program", "args", "path", "binary", "script", "file", "name", "content" };
                string rejectedProbe = null;
                foreach (string bf in bannedProbe) if (req.ContainsKey(bf)) { rejectedProbe = bf; break; }
                if (rejectedProbe != null)
                {
                    Set(resp, "status", "OPERATION_NOT_ALLOWED");
                    Set(resp, "rejected_field", rejectedProbe);
                    writer.WriteLine(Json.Serialize(resp));
                    return;
                }
                string probeRoot = Get(req, "workdir") as string;
                string probeDenial = WorkdirDenialReason(probeRoot);
                if (probeDenial != null)
                {
                    Set(resp, "status", "WORKDIR_UNAVAILABLE");
                    Set(resp, "workdir", probeRoot);
                    Set(resp, "reason", probeDenial);
                }
                else
                {
                    lock (mutating)
                    {
                        string sub = null, file = null;
                        bool clean = false;
                        try
                        {
                            sub = Path.Combine(Path.GetFullPath(probeRoot), ProbeSubdir);
                            file = Path.Combine(sub, ProbeFile);
                            if (!Directory.Exists(sub)) Directory.CreateDirectory(sub);
                            File.WriteAllText(file, "alpha");
                            bool readBack = File.ReadAllText(file) == "alpha";
                            File.AppendAllText(file, "-beta");
                            bool modified = File.ReadAllText(file) == "alpha-beta";
                            File.Delete(file);
                            Directory.Delete(sub);
                            clean = true;
                            Set(resp, "status", "ok");
                            Set(resp, "read", readBack);
                            Set(resp, "modified", modified);
                            Set(resp, "deleted", true);
                        }
                        catch (Exception ex)
                        {
                            Set(resp, "status", "WORKDIR_ACCESS_DENIED");
                            // The type, never the message: a Windows message
                            // can carry a path or a SID fragment, and this
                            // envelope is read by a GUI and written to a log.
                            Set(resp, "error", ex.GetType().Name);
                        }
                        finally
                        {
                            // A probe that leaves its scratch behind is a
                            // probe that becomes somebody else's litter.
                            if (!clean)
                            {
                                try { if (file != null && File.Exists(file)) File.Delete(file); } catch { }
                                try { if (sub != null && Directory.Exists(sub)) Directory.Delete(sub, true); } catch { }
                            }
                            Set(resp, "cleaned", clean);
                        }
                        Set(resp, "workdir", Path.GetFullPath(probeRoot));
                        Set(resp, "sid", currentSid);
                    }
                }
            }
            else if (cmd == "file_probe")
            {
                // Cross-identity file readability oracle. Runs in this broker process
                // under the managed account's own token. Verifies whether this managed
                // SID can read the specified file, computes SHA256 of read bytes,
                // and reports length and readability. Takes only "path", rejecting any
                // commands, programs, scripts, or content payloads.
                string[] bannedFileProbe = { "exe", "command", "command_line", "program", "args", "binary", "script", "content" };
                string rejectedField = null;
                foreach (string bf in bannedFileProbe) if (req.ContainsKey(bf)) { rejectedField = bf; break; }
                if (rejectedField != null)
                {
                    Set(resp, "status", "OPERATION_NOT_ALLOWED");
                    Set(resp, "rejected_field", rejectedField);
                    writer.WriteLine(Json.Serialize(resp));
                    return;
                }
                string filePath = Get(req, "path") as string;
                if (string.IsNullOrEmpty(filePath))
                {
                    Set(resp, "status", "PATH_REQUIRED");
                }
                else
                {
                    try
                    {
                        filePath = Path.GetFullPath(filePath);
                        if (!File.Exists(filePath))
                        {
                            Set(resp, "status", "FILE_NOT_FOUND");
                            Set(resp, "path", filePath);
                        }
                        else
                        {
                            using (var fs = new FileStream(filePath, FileMode.Open, FileAccess.Read, FileShare.Read))
                            {
                                long len = fs.Length;
                                using (var sha = SHA256.Create())
                                {
                                    byte[] hash = sha.ComputeHash(fs);
                                    string hex = BitConverter.ToString(hash).Replace("-", "").ToLowerInvariant();
                                    Set(resp, "status", "ok");
                                    Set(resp, "readable", true);
                                    Set(resp, "length", len);
                                    Set(resp, "sha256", hex);
                                    Set(resp, "path", filePath);
                                    Set(resp, "sid", currentSid);
                                }
                            }
                        }
                    }
                    catch (UnauthorizedAccessException)
                    {
                        Set(resp, "status", "FILE_ACCESS_DENIED");
                        Set(resp, "readable", false);
                        Set(resp, "path", filePath);
                        Set(resp, "sid", currentSid);
                    }
                    catch (Exception ex)
                    {
                        Set(resp, "status", "FILE_READ_ERROR");
                        Set(resp, "readable", false);
                        Set(resp, "error", ex.GetType().Name);
                        Set(resp, "path", filePath);
                        Set(resp, "sid", currentSid);
                    }
                }
            }
            else if (cmd == "saipen_readiness")
            {
                // SAIPEN readiness as THIS account sees it. Runs the machine
                // runtime's own launcher (a fixed, account-unwritable path) with
                // fixed read-only argv; the caller names at most a workdir that
                // passes the same check as launch. Reading the runtime as the
                // operator proves what the operator can run, never what the
                // managed token can, which is the only question that matters.
                string[] bannedReady = { "exe", "command", "command_line", "program", "args", "path", "binary", "script", "file", "name", "content" };
                string rejectedReady = null;
                foreach (string bf in bannedReady) if (req.ContainsKey(bf)) { rejectedReady = bf; break; }
                if (rejectedReady != null)
                {
                    Set(resp, "status", "OPERATION_NOT_ALLOWED");
                    Set(resp, "rejected_field", rejectedReady);
                    writer.WriteLine(Json.Serialize(resp));
                    return;
                }
                string readyRoot = Get(req, "workdir") as string;
                string readyDenial = readyRoot == null ? null : WorkdirDenialReason(readyRoot);
                if (readyDenial != null)
                {
                    Set(resp, "status", "WORKDIR_UNAVAILABLE");
                    Set(resp, "workdir", readyRoot);
                    Set(resp, "reason", readyDenial);
                }
                else
                {
                    foreach (var kv in SaipenReadinessHere(readyRoot == null ? null : Path.GetFullPath(readyRoot)))
                        Set(resp, kv.Key, kv.Value);
                }
                Set(resp, "sid", currentSid);
            }
            else if (cmd == "auth")
            {
                // Authentication is an account-plane operation, not a project
                // launch. It accepts NO workdir: the caller must not be able to
                // couple "sign in" to a project folder, which is what made the
                // SIGN IN button fail on a project the managed SID cannot read
                // before Antigravity had even opened. The directory is chosen
                // here, from this process's own identity, so it is always one
                // the managed account can open.
                string[] bannedAuth = { "exe", "command", "command_line", "program", "args", "path", "binary", "script", "workdir" };
                string rejected = null;
                foreach (string bf in bannedAuth) if (req.ContainsKey(bf)) { rejected = bf; break; }
                if (rejected != null)
                {
                    Set(resp, "status", "OPERATION_NOT_ALLOWED");
                    Set(resp, "rejected_field", rejected);
                    writer.WriteLine(Json.Serialize(resp));
                    return;
                }
                lock (mutating)
                {
                    string authDir = OwnDirectory();
                    if (authDir == null)
                    {
                        Set(resp, "status", "AUTH_DIRECTORY_UNAVAILABLE");
                    }
                    else
                    {
                        try
                        {
                            var psi = new ProcessStartInfo(SharedAgyPath()) { WorkingDirectory = authDir, UseShellExecute = true };
                            var proc = Process.Start(psi);
                            Set(resp, "status", "launched");
                            Set(resp, "pid", proc != null ? (object)proc.Id : 0);
                            Set(resp, "auth_directory", authDir);
                            Set(resp, "sid", currentSid);
                        }
                        catch (Exception ex) { Set(resp, "status", "auth_launch_failed"); Set(resp, "error", ex.Message); }
                    }
                }
            }
            else if (cmd == "logout")
            {
                // The ban list is deliberately wider than `auth`'s. Auth only had
                // to refuse a project path; here a caller that could name a
                // credential target, a registry path or a file would turn this
                // into the generic credential-delete API the spec forbids. There
                // is nothing to name: the target is a literal inside the store of
                // whoever this broker already runs as.
                string[] bannedLogout = { "exe", "command", "command_line", "program", "args", "path",
                                          "binary", "script", "workdir", "target", "credential",
                                          "registry", "user", "sid", "account" };
                string rejectedLogout = null;
                foreach (string blf in bannedLogout) if (req.ContainsKey(blf)) { rejectedLogout = blf; break; }
                if (rejectedLogout != null)
                {
                    Set(resp, "status", "OPERATION_NOT_ALLOWED");
                    Set(resp, "rejected_field", rejectedLogout);
                }
                else
                {
                    lock (mutating)
                    {
                        foreach (var kv in AntigravityLogout()) Set(resp, kv.Key, kv.Value);
                    }
                }
            }
            else if (cmd == "shutdown")
            {
                Set(resp, "status", "shutting_down");
                writer.WriteLine(Json.Serialize(resp));
                stop.Set();
                // Break the accept loop out of its blocking wait.
                try { if (slot != null && slot.Current != null) slot.Current.Dispose(); } catch { }
                return;
            }
            else
            {
                Set(resp, "status", "OPERATION_NOT_ALLOWED");
                Set(resp, "cmd", cmd);
            }
            writer.WriteLine(Json.Serialize(resp));
        }
    }

    // How long a client waits for the broker's pipe instance to reappear before
    // calling the context offline. Bounded on purpose: a dead broker must still
    // fail fast, just not faster than a live one can be mid-churn.
    const int PipePresenceGraceMs = 1500;

    static Dictionary<string, object> SendPipeCommand(string pipeName, Dictionary<string, object> req, int timeoutMs)
    {
        try
        {
            // The broker creates one pipe instance per connection, so between two
            // requests the name is briefly out of the namespace. Reporting OFFLINE
            // for that gap would make a live broker look dead to its own clients --
            // the same class of bug as logging a normal disconnect as a failure.
            // So give the instance a short bounded window to come back. A broker
            // that is genuinely gone still costs only this much, once.
            int grace = PipePresenceGraceMs;
            while (!System.IO.File.Exists(@"\\.\pipe\" + pipeName) && grace > 0)
            {
                Thread.Sleep(50);
                grace -= 50;
            }
            if (!System.IO.File.Exists(@"\\.\pipe\" + pipeName)) return null;
            using (var client = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.None, TokenImpersonationLevel.Identification))
            {
                client.Connect(timeoutMs <= 0 ? 2000 : Math.Min(timeoutMs, 2000));
                using (var reader = new StreamReader(client, Encoding.UTF8))
                using (var writer = new StreamWriter(client, new UTF8Encoding(false)) { AutoFlush = true })
                {
                    writer.WriteLine(Json.Serialize(req));
                    string raw = reader.ReadLine();
                    if (raw == null) return null;
                    try { return Json.DeserializeObject(raw) as Dictionary<string, object>; }
                    catch { return null; }
                }
            }
        }
        catch { return null; }
    }

    public static string HelperPipeName()
    {
        string env = Environment.GetEnvironmentVariable("SAI_HELPER_PIPE");
        return !string.IsNullOrEmpty(env) ? env : "sai-accounts-helper";
    }

    public static Dictionary<string, object> CallHelper(Dictionary<string, object> req, int timeoutMs = 15000)
    {
        try
        {
            string pipe = HelperPipeName();
            using (var client = new NamedPipeClientStream(".", pipe, PipeDirection.InOut, PipeOptions.None, TokenImpersonationLevel.Impersonation))
            {
                client.Connect(Math.Min(timeoutMs, 3000));
                using (var reader = new StreamReader(client, Encoding.UTF8))
                using (var writer = new StreamWriter(client, new UTF8Encoding(false)) { AutoFlush = true })
                {
                    writer.WriteLine(Json.Serialize(req));
                    string raw = reader.ReadLine();
                    if (raw == null) return null;
                    return Json.DeserializeObject(raw) as Dictionary<string, object>;
                }
            }
        }
        catch { return null; }
    }

    // ------------------------------------------------------------ launching
    static byte[] BuildEnvironmentBlock(Dictionary<string, string> setVars, List<string> removeVars)
    {
        var values = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (System.Collections.DictionaryEntry kv in Environment.GetEnvironmentVariables())
            values[(string)kv.Key] = (string)kv.Value;
        if (removeVars != null)
            foreach (string name in removeVars) values.Remove(name);
        if (setVars != null)
            foreach (var kv in setVars) values[kv.Key] = kv.Value;
        var sb = new StringBuilder();
        foreach (var kv in values) sb.Append(kv.Key).Append('=').Append(kv.Value).Append('\0');
        sb.Append('\0');
        return Encoding.Unicode.GetBytes(sb.ToString());
    }

    static string FindExecutableOnPath(string exeName)
    {
        string pathVar = Environment.GetEnvironmentVariable("PATH") ?? "";
        string pathext = Environment.GetEnvironmentVariable("PATHEXT") ?? ".COM;.EXE;.BAT;.CMD";
        var exts = new List<string>(pathext.Split(new char[] { ';' }, StringSplitOptions.RemoveEmptyEntries));
        if (!exts.Contains("")) exts.Add("");
        string[] dirs = pathVar.Split(new char[] { Path.PathSeparator }, StringSplitOptions.RemoveEmptyEntries);
        foreach (string dir in dirs)
        {
            try
            {
                foreach (string ext in exts)
                {
                    string candidate = Path.Combine(dir.Trim(), exeName + ext);
                    if (File.Exists(candidate)) return candidate;
                }
            }
            catch { }
        }
        return null;
    }

    static void ReconcileClaudeHooksIfNeeded(string locator)
    {
        if (string.IsNullOrEmpty(locator)) return;
        try
        {
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string[] candidates = {
                Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "consoles", "claude_hooks.py"),
                Path.Combine(localAppData, "SAITULS", "consoles", "claude_hooks.py"),
                Path.Combine(RootDir(), "Scripts", "consoles", "claude_hooks.py")
            };
            string scriptPath = null;
            foreach (string c in candidates)
            {
                if (File.Exists(c)) { scriptPath = Path.GetFullPath(c); break; }
            }
            if (scriptPath == null) return;
            string py = FindExecutableOnPath("python");
            if (py == null) return;
            var psi = new ProcessStartInfo(py, "\"" + scriptPath + "\" --profile-dir \"" + locator + "\"")
            {
                CreateNoWindow = true,
                UseShellExecute = false,
                WindowStyle = ProcessWindowStyle.Hidden
            };
            var proc = Process.Start(psi);
            if (proc != null) proc.WaitForExit(5000);
        }
        catch { }
    }

    static void AttachConsoleWindowProperties(uint pid, string accountId, string title)
    {
        ThreadPool.QueueUserWorkItem(_ =>
        {
            try
            {
                ushort atom = GlobalAddAtomW(accountId);
                for (int i = 0; i < 20; i++)
                {
                    Thread.Sleep(50);
                    IntPtr foundHwnd = IntPtr.Zero;
                    EnumWindows((hWnd, lParam) =>
                    {
                        uint procId;
                        GetWindowThreadProcessId(hWnd, out procId);
                        if (procId == pid)
                        {
                            foundHwnd = hWnd;
                            return false;
                        }
                        return true;
                    }, IntPtr.Zero);

                    if (foundHwnd != IntPtr.Zero)
                    {
                        SetPropW(foundHwnd, "SAITULS_ACCOUNT_ID", (IntPtr)atom);
                        if (!string.IsNullOrEmpty(title))
                        {
                            SetWindowTextW(foundHwnd, title);
                        }
                        // Reveal as minimized and without activating so a
                        // fullscreen app is not interrupted; the operator can
                        // restore it from the taskbar when needed.
                        ShowWindow(foundHwnd, 0);
                        break;
                    }

                    FreeConsole();
                    if (AttachConsole(pid))
                    {
                        try
                        {
                            IntPtr hwnd = GetConsoleWindow();
                            if (hwnd != IntPtr.Zero)
                            {
                                SetPropW(hwnd, "SAITULS_ACCOUNT_ID", (IntPtr)atom);
                                if (!string.IsNullOrEmpty(title))
                                {
                                    SetWindowTextW(hwnd, title);
                                }
                                ShowWindow(hwnd, 0);
                                break;
                            }
                        }
                        finally
                        {
                            FreeConsole();
                        }
                    }
                }
            }
            catch { }
        });
    }

    /// The console's own title is written by the vendor TUI, continuously, the
    /// moment it starts: `SetWindowTextW` on the console window once (what
    /// AttachConsoleWindowProperties does) loses the title within seconds and
    /// two accounts then look identical. This enters the console the launcher
    /// already created, runs the real command as its child and keeps putting the
    /// account identity back for as long as that child lives -- the same
    /// contract CONSOLES.ps1's local TitleGuard provides, for the federated path
    /// where the engine, not a long-lived launcher, is what starts the console.
    ///
    /// Invoked as: sai-accounts console-title-guard --title64 <b64> [--account <id>] -- <command> [args...]
    static int CmdConsoleTitleGuard(string[] args)
    {
        string titleB64 = null, accountId = null, bridgeOverride = null;
        int sep = -1;
        for (int i = 1; i < args.Length; i++)
        {
            if (args[i] == "--title64" && i + 1 < args.Length) { titleB64 = args[i + 1]; i++; }
            else if (args[i] == "--account" && i + 1 < args.Length) { accountId = args[i + 1]; i++; }
            else if (args[i] == "--input-bridge" && i + 1 < args.Length) { bridgeOverride = args[i + 1]; i++; }
            else if (args[i] == "--") { sep = i + 1; break; }
        }
        if (sep < 0 || sep >= args.Length)
            return Fail("guard_args", "console-title-guard --title64 <base64> [--account <id>] [--input-bridge on|off] -- <command> [args...]");

        string title = "";
        if (!string.IsNullOrEmpty(titleB64))
        {
            try { title = Encoding.UTF8.GetString(Convert.FromBase64String(titleB64)); }
            catch { return Fail("guard_args", "--title64 is not valid base64"); }
        }

        var child = new string[args.Length - sep];
        Array.Copy(args, sep, child, 0, child.Length);
        string childArgs = JoinArgs(child, 1);

        IntPtr hwnd = GetConsoleWindow();
        ushort atom = 0;
        if (hwnd != IntPtr.Zero && !string.IsNullOrEmpty(accountId))
            atom = GlobalAddAtomW(accountId);
        AssertConsoleTitle(hwnd, title, atom);

        // Ctrl+C still reaches the vendor in this same console. It must not
        // terminate only the guard and leave a live child without title ownership.
        ConsoleCancelEventHandler keepGuard = delegate(object sender, ConsoleCancelEventArgs e) { e.Cancel = true; };
        Console.CancelKeyPress += keepGuard;
        // The global compatibility hooks are an explicit opt-in. Off is the
        // default so a guard whose promise is only "keep the title correct"
        // can never pay for it with desktop input availability (P0).
        bool bridgeEnabled = InputBridgeWanted(bridgeOverride);
        AntigravityConsoleInputBridge inputBridge = null;
        ScopedConsoleController scopedController = null;
        try
        {
            Process proc;
            try
            {
                var psi = new ProcessStartInfo(child[0], childArgs) { UseShellExecute = false };
                NormalizeAntigravityInteractiveColors(psi, accountId);
                ProvideSaipenEntrypoint(psi, accountId);
                    if (IsAntigravityInteractiveChild(psi, accountId))
                    {
                        // Workspace trust is the launching identity's vendor setting,
                        // not a helper write or a change to the fixed permission argv.
                        PrepareAntigravityWorkdirTrust(accountId);
                        if (bridgeEnabled) inputBridge = AntigravityConsoleInputBridge.TryInstall(hwnd);
                        bool controllerEnabled = ControllerWanted(null);
                        if (controllerEnabled) scopedController = ScopedConsoleController.TryCreate(hwnd);
                        PublishControllerState(hwnd, controllerEnabled);
                        PrintConsoleShortcutsBanner();
                    }
                proc = Process.Start(psi);
            }
            catch (Exception ex) { return Fail("guard_start", ex.Message); }
            if (proc == null) return Fail("guard_start", "child did not start");

            // Observable, non-invasive status: the guard's own console window
            // carries the bridge state, so a probe reads it with GetPropW and
            // nothing is printed into the vendor's console.
            PublishInputBridgeState(hwnd, inputBridge != null);

            // Restore vendor title writes within a short, bounded cadence.
            // Unchanged titles cause no writes or rename events (see below).
            // The guard lives exactly as long as the console child.
            bool exited = false;
            while (!exited)
            {
                exited = true;
                try { exited = proc.HasExited; } catch { }
                if (!exited)
                {
                    AssertConsoleTitle(hwnd, title, atom);
                    if (inputBridge != null) inputBridge.Pump();
                    if (scopedController != null) scopedController.Maintain();
                    Thread.Sleep(25);
                }
            }
            try { return proc.ExitCode; } catch { return 0; }
            finally { proc.Dispose(); }
        }
        finally
        {
            if (scopedController != null) scopedController.Dispose();
            if (inputBridge != null) inputBridge.Dispose();
            Console.CancelKeyPress -= keepGuard;
            if (atom != 0) GlobalDeleteAtom(atom);
        }
    }

    // A tool host can export NO_COLOR=1/TERM=dumb for captured output. A real
    // account console must not inherit those temporary capture hints. Keep
    // persisted preferences and every unrelated/authentication variable intact.
    static void NormalizeAntigravityInteractiveColors(ProcessStartInfo psi, string accountId)
    {
        if (!IsAntigravityInteractiveChild(psi, accountId)) return;
        foreach (string name in new[] { "NO_COLOR", "TERM" })
        {
            string value = psi.EnvironmentVariables[name];
            if (value == null || name == "TERM" && !string.Equals(value, "dumb", StringComparison.OrdinalIgnoreCase)) continue;
            try
            {
                if (Environment.GetEnvironmentVariable(name, EnvironmentVariableTarget.User) != null
                    || Environment.GetEnvironmentVariable(name, EnvironmentVariableTarget.Machine) != null) continue;
                psi.EnvironmentVariables.Remove(name);
            }
            catch { /* An unreadable preference is not permission to erase it. */ }
        }
    }

    static bool IsAntigravityInteractiveChild(ProcessStartInfo psi, string accountId)
    {
        return !string.IsNullOrEmpty(accountId) && Regex.IsMatch(accountId, "^antigravity:windows-user:[0-9a-f]{16}$")
            && (string.Equals(Path.GetFullPath(TrimQuotes(psi.FileName)), Path.GetFullPath(SharedAgyPath()), StringComparison.OrdinalIgnoreCase)
                || string.Equals(Path.GetFileName(TrimQuotes(psi.FileName)), "agy.exe", StringComparison.OrdinalIgnoreCase));
    }

    // A guard argv token is commonly quoted (paths with spaces); matching it
    // against a raw path must ignore those quotes.
    static string TrimQuotes(string value)
    {
        if (string.IsNullOrEmpty(value)) return value;
        string v = value.Trim();
        if (v.Length >= 2 && v[0] == '"' && v[v.Length - 1] == '"') v = v.Substring(1, v.Length - 2);
        return v;
    }

    // P0 safety switch for the Antigravity console input bridge. The bridge
    // installs process-global low-level keyboard and mouse hooks, and such a hook
    // is serviced on the installing thread: while that thread is sleeping or
    // draining messages, every desktop input event can wait on it.
    // That is a plausible mechanism for the reported system-wide input/1-2 FPS
    // collapse, NOT a measured cause -- so the bridge is OFF unless an operator
    // explicitly turns it on, and the title guard plus the native vendor session
    // run unaffected either way.
    //
    // Reversible without a rebuild: `--input-bridge on|off` on the guard, or the
    // SAI_AG_CONSOLE_INPUT_BRIDGE variable in the process, User or Machine scope
    // (1/on/true/enabled/yes enables; anything else, or unset, is disabled).
    const string InputBridgeSwitch = "SAI_AG_CONSOLE_INPUT_BRIDGE";
    static bool InputBridgeWanted(string overrideValue)
    {
        string value = overrideValue;
        if (string.IsNullOrEmpty(value)) value = Environment.GetEnvironmentVariable(InputBridgeSwitch);
        if (string.IsNullOrEmpty(value))
        {
            // The managed-SID guard is spawned with a constructed environment
            // block, so a machine-scope switch is the one an operator can set
            // once and have every account honor.
            try { value = Environment.GetEnvironmentVariable(InputBridgeSwitch, EnvironmentVariableTarget.User); } catch { }
        }
        if (string.IsNullOrEmpty(value))
        {
            try { value = Environment.GetEnvironmentVariable(InputBridgeSwitch, EnvironmentVariableTarget.Machine); } catch { }
        }
        if (string.IsNullOrEmpty(value)) return false;
        value = value.Trim().ToLowerInvariant();
        return value == "1" || value == "on" || value == "true" || value == "enabled" || value == "yes";
    }

    // 1 = global compatibility hooks installed, 2 = disabled. Consumed by probes
    // and by the native responsiveness regression harness.
    const string InputBridgeProperty = "SAITULS_INPUT_BRIDGE";
    static void PublishInputBridgeState(IntPtr hwnd, bool installed)
    {
        if (hwnd == IntPtr.Zero) hwnd = GetConsoleWindow();
        if (hwnd == IntPtr.Zero) return;
        try { SetPropW(hwnd, InputBridgeProperty, (IntPtr)(installed ? 1 : 2)); } catch { }
    }

    // The scoped controller is ON by default and this is the documented way to
    // turn it off: same three-scope resolution as the bridge switch, opposite
    // default. It exists so an acceptance oracle can prove WHAT the controller
    // does by watching the mode stop being restored when it is disabled.
    const string ControllerSwitch = "SAI_AG_CONSOLE_CONTROLLER";
    static bool ControllerWanted(string overrideValue)
    {
        string value = overrideValue;
        if (string.IsNullOrEmpty(value)) value = Environment.GetEnvironmentVariable(ControllerSwitch);
        if (string.IsNullOrEmpty(value))
        {
            try { value = Environment.GetEnvironmentVariable(ControllerSwitch, EnvironmentVariableTarget.User); } catch { }
        }
        if (string.IsNullOrEmpty(value))
        {
            try { value = Environment.GetEnvironmentVariable(ControllerSwitch, EnvironmentVariableTarget.Machine); } catch { }
        }
        if (string.IsNullOrEmpty(value)) return true;
        value = value.Trim().ToLowerInvariant();
        return value == "1" || value == "on" || value == "true" || value == "enabled" || value == "yes";
    }

    // 1 = scoped controller running, 2 = deliberately disabled. The window prop
    // stays readable after the fact so a run can classify its own configuration.
    const string ControllerProperty = "SAITULS_CONSOLE_CONTROLLER";
    static void PublishControllerState(IntPtr hwnd, bool enabled)
    {
        if (hwnd == IntPtr.Zero) hwnd = GetConsoleWindow();
        if (hwnd == IntPtr.Zero) return;
        try { SetPropW(hwnd, ControllerProperty, (IntPtr)(enabled ? 1 : 2)); } catch { }
    }

    static string AntigravitySettingsPath()
    {
        string sid = WindowsIdentity.GetCurrent().User.Value;
        using (var profile = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(
            @"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\" + sid))
        {
            string home = profile == null ? null : profile.GetValue("ProfileImagePath") as string;
            if (string.IsNullOrEmpty(home)) return null;
            return Path.Combine(Environment.ExpandEnvironmentVariables(home), @".gemini\antigravity-cli\settings.json");
        }
    }

    static void PrepareAntigravityWorkdirTrust(string accountId)
    {
        try
        {
            // The broker binds this identity already; repeat the fixed SID
            // relationship before any persistent vendor preference write.
            string sid = WindowsIdentity.GetCurrent().User.Value;
            if (!string.Equals(accountId, AccountIdFor("antigravity", "windows_user", sid), StringComparison.Ordinal)) return;
            string path = AntigravitySettingsPath();
            if (path != null) EnsureAntigravityWorkdirTrust(path, Directory.GetCurrentDirectory());
        }
        catch { /* Keep the vendor's normal trust question on unavailable/invalid settings. */ }
    }

    // Append the selected directory to an EXISTING vendor settings file and
    // nothing else. The vendor's preferences are the vendor's: this never
    // creates the file, never creates its directory, and never replaces a
    // malformed document with synthesized defaults. The previous behavior --
    // writing {"trustedWorkspaces":[...]} when the file was missing -- was the
    // "Welcome to Antigravity CLI, choose your color scheme" first-run screen on
    // every launch, because it pre-empted the vendor's own initialization.
    static string EnsureAntigravityWorkdirTrust(string settingsPath, string workdir)
    {
        if (string.IsNullOrEmpty(workdir) || !Path.IsPathRooted(workdir) || !Directory.Exists(workdir)) return "workdir_unavailable";
        string normalized = Path.GetFullPath(workdir);
        // Missing settings stay missing: the official CLI initializes them with
        // the full schema, so the vendor -- not SAITULS -- owns first run.
        if (!File.Exists(settingsPath)) return "missing_settings";
        string mutexName = "Local\\SAI.Antigravity.WorkspaceTrust." + Fingerprint(settingsPath);
        using (var mutex = new Mutex(false, mutexName))
        {
            bool owned = false;
            try
            {
                try { owned = mutex.WaitOne(3000); } catch (AbandonedMutexException) { owned = true; }
                if (!owned) return "busy";
                for (int attempt = 0; attempt < 3; attempt++)
                {
                    // The vendor can delete or recreate its file between reads.
                    // A missing file is never created here, only reported.
                    if (!File.Exists(settingsPath)) return "missing_settings";
                    string original = File.ReadAllText(settingsPath, Encoding.UTF8);
                    var settings = Json.DeserializeObject(original) as Dictionary<string, object>;
                    if (settings == null) return "invalid_settings";
                    var trusted = new List<string>();
                    object existing;
                    if (settings.TryGetValue("trustedWorkspaces", out existing))
                    {
                        var entries = existing as object[];
                        if (entries == null) return "invalid_settings";
                        foreach (object entry in entries)
                        {
                            string directory = entry as string;
                            if (directory == null) return "invalid_settings";
                            trusted.Add(directory);
                            if (string.Equals(directory.TrimEnd('\\', '/'), normalized.TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase))
                                return "already_correct";
                        }
                    }
                    trusted.Add(normalized);
                    settings["trustedWorkspaces"] = trusted.ToArray();
                    string staged = settingsPath + ".sai-new-" + Guid.NewGuid().ToString("N");
                    try
                    {
                        File.WriteAllText(staged, Json.Serialize(settings), new UTF8Encoding(false));
                        using (var file = new FileStream(staged, FileMode.Open, FileAccess.ReadWrite, FileShare.Read)) { file.Flush(true); }
                        // The vendor does not share our mutex. Retry if it changed
                        // preferences while we prepared the adjacent atomic file.
                        string latest = File.Exists(settingsPath) ? File.ReadAllText(settingsPath, Encoding.UTF8) : null;
                        if (!string.Equals(original, latest, StringComparison.Ordinal)) continue;
                        // In-place replace: a failed write leaves the original
                        // file byte-identical, and the staged file is deleted.
                        File.Replace(staged, settingsPath, null);
                        return "applied";
                    }
                    finally { if (File.Exists(staged)) File.Delete(staged); }
                }
                return "busy";
            }
            catch { return "unavailable"; }
            finally { if (owned) mutex.ReleaseMutex(); }
        }
    }

    static void PrintConsoleShortcutsBanner()
    {
        try
        {
            if (!Console.IsOutputRedirected)
            {
                Console.ForegroundColor = ConsoleColor.DarkGray;
                Console.WriteLine("[SAITULS] Shortcuts: Paste: Ctrl+V / Right-Click | Copy: Enter / Ctrl+C with selection | Select: Ctrl+A");
                Console.ResetColor();
            }
        }
        catch { }
    }

    // T-301 correction of the T-300 controller, naming the defect class it
    // eliminates: restoring QuickEdit from ANY raw mode, and posting paste or
    // copy chords the console host already performs, is what duplicated vendor
    // input and fought the TUI's own mode.
    // 1. QuickEdit is restored ONLY from a plain classic mode that cleared it
    //    while leaving the mouse and VT input flags off. ENABLE_MOUSE_INPUT says
    //    the child wants mouse events -- QuickEdit steals them for selection --
    //    and ENABLE_VIRTUAL_TERMINAL_INPUT says the TUI owns selection and every
    //    shortcut chord itself. Restoring under either flag is the uncovered
    //    (mouse-on / QuickEdit-off) T-300 branch that has no recovery attempt.
    // 2. The T-300 Ctrl+V / Ctrl+C translation is removed instead of widened:
    //    conhost pastes on Ctrl+V and copies on Ctrl+C with a selection in
    //    classic mode, so posting those commands again pasted and copied every
    //    keystroke twice, and the chords belong to the vendor TUI in VT mode.
    //    The only chord with no native owner is Ctrl+A -> Select All, skipped
    //    while VT input is on. Zero global hooks either way.
    // 3. The system menu keeps the native Edit commands, posted only when the
    //    operator picks them, which can never race a delivered keystroke.
    sealed class ScopedConsoleController : IDisposable
    {
        const uint WmSysCommand = 0x0112;
        const int Copy = 0xfff0, Paste = 0xfff1, SelectAll = 0xfff5;
        const uint GenericRead = 0x80000000, GenericWrite = 0x40000000;
        const uint FileShareRead = 1, FileShareWrite = 2;
        const uint OpenExisting = 3;

        // CharSet.Ansi marshals the name into ANSI bytes while the W entry
        // point expects UTF-16, which silently fails every CONIN$ open.
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll")] static extern bool GetConsoleMode(IntPtr hConsoleHandle, out uint lpMode);
        [DllImport("kernel32.dll")] static extern bool SetConsoleMode(IntPtr hConsoleHandle, uint dwMode);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] static extern short GetAsyncKeyState(int vKey);
        [DllImport("user32.dll")] static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll")] static extern IntPtr GetSystemMenu(IntPtr hWnd, bool bRevert);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern bool AppendMenuW(IntPtr hMenu, uint uFlags, IntPtr uIDNewItem, string lpNewItem);

        readonly IntPtr window;
        readonly IntPtr hIn;
        bool lastADown;

        ScopedConsoleController(IntPtr hwnd, IntPtr handle)
        {
            window = hwnd;
            hIn = handle;
            ConfigureSystemMenu();
            Maintain();
        }

        public static ScopedConsoleController TryCreate(IntPtr hwnd)
        {
            try
            {
                IntPtr handle = CreateFileW("CONIN$", GenericRead | GenericWrite, FileShareRead | FileShareWrite, IntPtr.Zero, OpenExisting, 0, IntPtr.Zero);
                if (handle == (IntPtr)(-1) || handle == IntPtr.Zero) return null;
                return new ScopedConsoleController(hwnd, handle);
            }
            catch { return null; }
        }

        void ConfigureSystemMenu()
        {
            if (window == IntPtr.Zero) return;
            try
            {
                IntPtr hMenu = GetSystemMenu(window, false);
                if (hMenu != IntPtr.Zero)
                {
                    AppendMenuW(hMenu, 0x0800 /* MF_SEPARATOR */, IntPtr.Zero, null);
                    AppendMenuW(hMenu, 0, (IntPtr)Paste, "Paste\tCtrl+V / Right-Click");
                    AppendMenuW(hMenu, 0, (IntPtr)Copy, "Copy Selection\tEnter / Ctrl+C");
                    AppendMenuW(hMenu, 0, (IntPtr)SelectAll, "Select All\tCtrl+A");
                }
            }
            catch { }
        }

        public void Maintain()
        {
            try
            {
                // 1. QuickEdit restoration: plain classic modes only. The child
                //    clearing the mouse flag is a deliberate "I take mouse
                //    events"; the VT flag is the TUI's whole input protocol.
                //    Pressing QuickEdit back on under either is the defect, so
                //    the uncovered mouse-on/QuickEdit-off branch is left alone
                //    and only a genuine classic restore is attempted.
                uint mode = 0;
                bool vtInput = false;
                if (hIn != IntPtr.Zero && hIn != (IntPtr)(-1) && GetConsoleMode(hIn, out mode))
                {
                    vtInput = (mode & 0x0200) != 0;
                    if ((mode & 0x0010) == 0 && (mode & 0x0040) == 0 && !vtInput)
                    {
                        SetConsoleMode(hIn, mode | 0x0040 | 0x0080 | 0x0020);
                    }
                }

                // 2. Foreground-only, non-VT, owner-less chord. Ctrl+V and
                //    Ctrl+C belong to conhost (classic) or the TUI (VT) and are
                //    never posted here again. GetAsyncKeyState's since-last-call
                //    latch makes even a sub-25ms tap of Ctrl+A an edge, so the
                //    poll cadence cannot drop a short press.
                if (window == IntPtr.Zero || GetForegroundWindow() != window || vtInput)
                {
                    lastADown = false;
                    return;
                }

                bool ctrl = (GetAsyncKeyState(0x11) & 0x8000) != 0;
                bool shift = (GetAsyncKeyState(0x10) & 0x8000) != 0;
                bool alt = (GetAsyncKeyState(0x12) & 0x8000) != 0;

                short aState = GetAsyncKeyState(0x41);
                bool aDown = (aState & 0x8000) != 0;
                // Low bit: pressed since the last query, so a tap shorter than
                // the 25 ms poll is still observed instead of being lost.
                bool aTapped = (aState & 0x0001) != 0;

                // Ctrl+A: Select All (no native owner)
                if (ctrl && !shift && !alt && (aDown || aTapped) && !lastADown)
                {
                    PostMessage(window, WmSysCommand, (IntPtr)SelectAll, IntPtr.Zero);
                }

                lastADown = aDown;
            }
            catch { }
        }

        public void Dispose()
        {
            if (hIn != IntPtr.Zero && hIn != (IntPtr)(-1))
            {
                try { CloseHandle(hIn); } catch { }
            }
        }
    }

    // Conhost gives Ctrl+A/V to a VT-input application and the vendor disables
    // QuickEdit. The operator approved native command compatibility (SRC-091).
    // Hooks observe no text and route only this console's foreground events.
    // STDIN flags, child identity and all other applications remain untouched.
    // NOTE: installed ONLY when InputBridgeWanted() says so -- the global hooks
    // are OFF by default after the P0 input/FPS report.
    sealed class AntigravityConsoleInputBridge : IDisposable
    {
        const uint WmSysCommand = 0x112, WmKeyDown = 0x100, WmKeyUp = 0x101,
            WmSysKeyDown = 0x104, WmSysKeyUp = 0x105, WmLeftDown = 0x201;
        const int Copy = 0xfff0, Paste = 0xfff1, Mark = 0xfff2, SelectAll = 0xfff5;
        [StructLayout(LayoutKind.Sequential)] struct Point { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)] struct Rect { public int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)] struct Coord { public short X, Y; }
        [StructLayout(LayoutKind.Sequential)] struct SmallRect { public short Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)] struct SelectionInfo { public uint Flags; public Coord Anchor; public SmallRect Selection; }
        [StructLayout(LayoutKind.Sequential)] struct Keyboard { public uint Key, Scan, Flags, Time; public UIntPtr Extra; }
        [StructLayout(LayoutKind.Sequential)] struct Mouse { public Point Position; public uint Data, Flags, Time; public UIntPtr Extra; }
        [StructLayout(LayoutKind.Sequential)] struct Message { public IntPtr Window; public uint Id; public UIntPtr WParam; public IntPtr LParam; public uint Time; public Point Position; public uint Private; }
        delegate IntPtr Hook(int code, IntPtr message, IntPtr data);
        [DllImport("user32.dll", SetLastError = true)] static extern IntPtr SetWindowsHookEx(int id, Hook callback, IntPtr module, uint thread);
        [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hook);
        [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr message, IntPtr data);
        [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] static extern short GetAsyncKeyState(int key);
        [DllImport("user32.dll")] static extern bool IsClipboardFormatAvailable(uint format);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr window, StringBuilder name, int capacity);
        [DllImport("user32.dll")] static extern bool PostMessage(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
        [DllImport("user32.dll")] static extern IntPtr SendMessageTimeout(IntPtr window, uint message, IntPtr wparam, IntPtr lparam, uint flags, uint timeout, out IntPtr result);
        [DllImport("user32.dll")] static extern bool ScreenToClient(IntPtr window, ref Point point);
        [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr window, out Rect rect);
        [DllImport("user32.dll")] static extern bool PeekMessage(out Message message, IntPtr window, uint first, uint last, uint remove);
        [DllImport("user32.dll")] static extern bool TranslateMessage(ref Message message);
        [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref Message message);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr GetModuleHandle(string name);
        [DllImport("kernel32.dll")] static extern bool GetConsoleSelectionInfo(out SelectionInfo selection);
        [DllImport("kernel32.dll")] static extern bool GetConsoleMode(IntPtr input, out uint mode);
        [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int id);

        readonly IntPtr window;
        readonly Hook keyCallback, mouseCallback;
        readonly bool[] suppressed = new bool[256];
        IntPtr keyHook, mouseHook;
        AntigravityConsoleInputBridge(IntPtr hwnd)
        { window = hwnd; keyCallback = OnKey; mouseCallback = OnMouse; }
        public static AntigravityConsoleInputBridge TryInstall(IntPtr hwnd)
        {
            if (hwnd == IntPtr.Zero) return null;
            var name = new StringBuilder(64); GetClassName(hwnd, name, name.Capacity);
            if (!string.Equals(name.ToString(), "ConsoleWindowClass", StringComparison.Ordinal)) return null;
            var bridge = new AntigravityConsoleInputBridge(hwnd);
            try
            {
                IntPtr module = GetModuleHandle(null);
                bridge.keyHook = SetWindowsHookEx(13, bridge.keyCallback, module, 0);
                bridge.mouseHook = SetWindowsHookEx(14, bridge.mouseCallback, module, 0);
                if (bridge.keyHook != IntPtr.Zero && bridge.mouseHook != IntPtr.Zero) return bridge;
            }
            catch { }
            bridge.Dispose(); return null;
        }
        static bool Held(int key) { return (GetAsyncKeyState(key) & 0x8000) != 0; }
        static int ShortcutCommand(int key, bool control, bool shift, bool alt, bool selected)
        {
            if (!control || shift || alt) return 0;
            return key == 'V' ? Paste : key == 'A' ? SelectAll : key == 'C' && selected ? Copy : 0;
        }
        IntPtr OnKey(int code, IntPtr message, IntPtr data)
        {
            // Check ownership before decoding any event. Another foreground
            // application's keys are passed on without inspecting their data.
            if (GetForegroundWindow() != window)
            {
                Array.Clear(suppressed, 0, suppressed.Length);
                return CallNextHookEx(keyHook, code, message, data);
            }
            if (code >= 0)
            try
            {
                var key = (Keyboard)Marshal.PtrToStructure(data, typeof(Keyboard));
                uint msg = unchecked((uint)message.ToInt64());
                bool up = msg == WmKeyUp || msg == WmSysKeyUp;
                bool eaten = key.Key < suppressed.Length && suppressed[key.Key];
                if (up && key.Key < suppressed.Length) suppressed[key.Key] = false;
                if (GetForegroundWindow() == window)
                {
                    if (up && eaten) return (IntPtr)1;
                    if (msg == WmKeyDown || msg == WmSysKeyDown)
                    {
                        SelectionInfo selection;
                        bool selected = GetConsoleSelectionInfo(out selection) && (selection.Flags & 2) != 0;
                        int command = ShortcutCommand((int)key.Key, Held(0x11), Held(0x10), Held(0x12), selected);
                        // Native text paste cannot handle the vendor's media
                        // clipboard path. Leave image/file-only Ctrl+V to the
                        // child; inspect format availability, never contents.
                        if (command == Paste && !IsClipboardFormatAvailable(13)) command = 0;
                        if (command != 0 && PostMessage(window, WmSysCommand, (IntPtr)command, IntPtr.Zero))
                        {
                            if (key.Key < suppressed.Length) suppressed[key.Key] = true;
                            return (IntPtr)1;
                        }
                    }
                }
            }
            catch { /* Failed compatibility never eats an unrelated key. */ }
            return CallNextHookEx(keyHook, code, message, data);
        }
        IntPtr OnMouse(int code, IntPtr message, IntPtr data)
        {
            if (code >= 0 && unchecked((uint)message.ToInt64()) == WmLeftDown && GetForegroundWindow() == window)
            try
            {
                var mouse = (Mouse)Marshal.PtrToStructure(data, typeof(Mouse));
                Point point = mouse.Position; Rect client; uint mode;
                if (!Held(0x11) && !Held(0x12) && GetConsoleMode(GetStdHandle(-10), out mode)
                    && (mode & 0x40) == 0 && ((mode & 0x10) == 0 || Held(0x10))
                    && ScreenToClient(window, ref point) && GetClientRect(window, out client)
                    && point.X >= client.Left && point.X < client.Right && point.Y >= client.Top && point.Y < client.Bottom)
                {
                    IntPtr ignored;
                    // Mark must precede the physical button message. Bound the
                    // host call, then pass through the actual drag unchanged.
                    SendMessageTimeout(window, WmSysCommand, (IntPtr)Mark, IntPtr.Zero, 2, 100, out ignored);
                }
            }
            catch { }
            return CallNextHookEx(mouseHook, code, message, data);
        }
        public void Pump()
        {
            Message message;
            for (int i = 0; i < 64 && PeekMessage(out message, IntPtr.Zero, 0, 0, 1); i++)
            { TranslateMessage(ref message); DispatchMessage(ref message); }
        }
        public void Dispose()
        {
            if (keyHook != IntPtr.Zero) { UnhookWindowsHookEx(keyHook); keyHook = IntPtr.Zero; }
            if (mouseHook != IntPtr.Zero) { UnhookWindowsHookEx(mouseHook); mouseHook = IntPtr.Zero; }
        }
    }

    static void AssertConsoleTitle(IntPtr hwnd, string title, ushort atom)
    {
        try
        {
            if (!string.IsNullOrEmpty(title))
            {
                var actual = new StringBuilder(title.Length + 32);
                GetConsoleTitleW(actual, (uint)actual.Capacity);
                if (!string.Equals(actual.ToString(), title, StringComparison.Ordinal)) SetConsoleTitleW(title);
            }
        }
        catch { }
        try
        {
            if (hwnd == IntPtr.Zero) hwnd = GetConsoleWindow();
            if (hwnd == IntPtr.Zero) return;
            if (atom != 0) SetPropW(hwnd, "SAITULS_ACCOUNT_ID", (IntPtr)atom);
            if (!string.IsNullOrEmpty(title))
            {
                var actual = new StringBuilder(title.Length + 32);
                GetWindowTextW(hwnd, actual, actual.Capacity);
                if (!string.Equals(actual.ToString(), title, StringComparison.Ordinal)) SetWindowTextW(hwnd, title);
            }
        }
        catch { }
    }

    /// Command-line tail for a child process: every argument after the first,
    /// quoted only when it must be. The launcher's own argv is already split, so
    /// this round-trips a fixed, non-injected command rather than a shell string.
    static string JoinArgs(string[] argv, int from)
    {
        var sb = new StringBuilder();
        for (int i = from; i < argv.Length; i++)
        {
            if (sb.Length > 0) sb.Append(' ');
            string a = argv[i];
            if (a.Length == 0 || a.IndexOf(' ') >= 0) sb.Append('"').Append(a).Append('"');
            else sb.Append(a);
        }
        return sb.ToString();
    }

    /// Launch an interactive console CLI in a new console with a controlled
    /// environment block. Direct CreateProcess: no shell, so workdir can never
    /// inject metacharacters.
    static Dictionary<string, object> LaunchInNewConsole(string commandLine, string workdir, Dictionary<string, string> env, List<string> scrub, string title, string accountId)
    {
        var result = Obj();
        // This child gets a real new console. Headless callers often carry
        // TERM=dumb, which makes Codex stop at a y/N terminal warning instead
        // of opening the selected account. Shape only this child's environment.
        var consoleEnv = new Dictionary<string, string>(env, StringComparer.OrdinalIgnoreCase);
        string term = Environment.GetEnvironmentVariable("TERM");
        if (!consoleEnv.ContainsKey("TERM") && (string.IsNullOrWhiteSpace(term)
            || string.Equals(term, "dumb", StringComparison.OrdinalIgnoreCase)))
            consoleEnv["TERM"] = "xterm-256color";
        GCHandle envPin = GCHandle.Alloc(BuildEnvironmentBlock(consoleEnv, scrub), GCHandleType.Pinned);
        ProcessInfo proc = new ProcessInfo();
        try
        {
            // Create the new console hidden first. Showing it later with
            // SW_SHOWNOACTIVATE (in AttachConsoleWindowProperties) avoids the
            // console host activating it and stealing focus from a fullscreen app.
            // Ask conhost to create the console hidden. conhost sometimes ignores
            // this, so the window is also hidden immediately after it appears.
            var startup = new StartupInfo { cb = (uint)Marshal.SizeOf(typeof(StartupInfo)), flags = 0x00000001, show = 0, title = title };
            if (!CreateProcess(null, new StringBuilder(commandLine), IntPtr.Zero, IntPtr.Zero, false,
                CREATE_NEW_CONSOLE | CREATE_UNICODE_ENVIRONMENT,
                envPin.AddrOfPinnedObject(), workdir, ref startup, out proc))
            {
                Set(result, "status", "launch_failed");
                Set(result, "error", "CreateProcess failed: " + Marshal.GetLastWin32Error().ToString(CultureInfo.InvariantCulture));
                return result;
            }
            Set(result, "status", "launched");
            Set(result, "pid", proc.pid);
            // conhost activates the new console on creation, stealing focus from a
            // fullscreen app. Capture the previous foreground now and restore it
            // as soon as the console window appears; also show without activating.
            IntPtr prevForeground = GetForegroundWindow();
            uint launchPid = proc.pid;
            ThreadPool.QueueUserWorkItem(state =>
            {
                for (int i = 0; i < 40; i++)
                {
                    Thread.Sleep(25);
                    IntPtr hwnd = IntPtr.Zero;
                    EnumWindows((h, lp) =>
                    {
                        uint wpid;
                        GetWindowThreadProcessId(h, out wpid);
                        if (wpid == launchPid) { hwnd = h; return false; }
                        return true;
                    }, IntPtr.Zero);
                    if (hwnd != IntPtr.Zero)
                    {
                        ShowWindow(hwnd, 4); // SW_SHOWNOACTIVATE
                        if (prevForeground != IntPtr.Zero && prevForeground != hwnd)
                            SetForegroundWindow(prevForeground);
                        break;
                    }
                }
            });
            if (!string.IsNullOrEmpty(accountId))
            {
                AttachConsoleWindowProperties(proc.pid, accountId, title);
            }
            return result;
        }
        finally
        {
            envPin.Free();
            if (proc.thread != IntPtr.Zero) CloseHandle(proc.thread);
            if (proc.process != IntPtr.Zero) CloseHandle(proc.process);
        }
    }

    static Dictionary<string, object> LaunchAccount(Dictionary<string, object> acc, List<Dictionary<string, object>> accounts, string workdir, string authToken = null)
    {
        string provider = S(acc, "provider_id");
        string backend = S(acc, "execution_backend");
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        string state = S(acc, "operational_state") ?? "ENABLED";
        if (state != "ENABLED")
        {
            var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
            Set(o, "status", "LIFECYCLE_FORBIDDEN");
            Set(o, "operational_state", state);
            Set(o, "account_id", S(acc, "account_id"));
            Print(o); return o;
        }
        // Checked HERE, under the caller's own identity, and again inside the
        // broker under the managed one. The front check catches a folder the
        // operator cannot read at all; it can never prove the managed account
        // can, because that is a different SID on a different face of the same
        // tree. Both answers carry the same reason enum.
        string denial = WorkdirDenialReason(workdir);
        if (denial != null)
        {
            var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
            Set(o, "status", "WORKDIR_UNAVAILABLE");
            Set(o, "workdir", workdir);
            Set(o, "reason", denial);
            Print(o); return o;
        }

        bool launchTrusted = false;
        var trustedRootsToGrant = new List<string>();

        if (!string.IsNullOrEmpty(authToken))
        {
            lock (_authLock)
            {
                var auths = LoadAuthorizations();
                var tokensDict = Get(auths, "tokens") as Dictionary<string, object>;
                var tokRecord = tokensDict != null ? Get(tokensDict, authToken) as Dictionary<string, object> : null;
                if (tokRecord == null)
                {
                    var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                    Set(o, "status", "AUTH_TOKEN_NOT_FOUND");
                    Set(o, "error", "Authorization token not found or unrecognized");
                    Print(o); return o;
                }
                if (object.Equals(Get(tokRecord, "used"), true))
                {
                    var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                    Set(o, "status", "AUTH_TOKEN_ALREADY_USED");
                    Set(o, "error", "Authorization token has already been used");
                    Print(o); return o;
                }
                DateTime exp;
                string expStr = S(tokRecord, "expires_at");
                if (string.IsNullOrEmpty(expStr) || !DateTime.TryParse(expStr, null, DateTimeStyles.RoundtripKind, out exp) || DateTime.UtcNow > exp)
                {
                    var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                    Set(o, "status", "AUTH_TOKEN_EXPIRED");
                    Set(o, "error", "Authorization token has expired (lifetime 60s exceeded)");
                    Print(o); return o;
                }
                string tokAccountId = S(tokRecord, "account_id");
                string currentAccountId = S(acc, "account_id");
                if (!string.IsNullOrEmpty(tokAccountId) && !string.Equals(tokAccountId, currentAccountId, StringComparison.OrdinalIgnoreCase))
                {
                    var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                    Set(o, "status", "AUTH_TOKEN_ACCOUNT_MISMATCH");
                    Set(o, "error", "Authorization token account ID does not match target account");
                    Print(o); return o;
                }
                string tokSid = S(tokRecord, "target_sid");
                string targetSidCheck = IsWindowsUserBackend(backend) ? AccountSid(acc) : null;
                if (!string.IsNullOrEmpty(tokSid) && !string.Equals(tokSid, targetSidCheck, StringComparison.OrdinalIgnoreCase))
                {
                    var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                    Set(o, "status", "AUTH_TOKEN_SID_MISMATCH");
                    Set(o, "error", "Authorization token does not match target account SID");
                    Print(o); return o;
                }
                string tokWorkdir = S(tokRecord, "workdir");
                if (!string.IsNullOrEmpty(workdir) && !string.IsNullOrEmpty(tokWorkdir))
                {
                    try
                    {
                        if (!string.Equals(Path.GetFullPath(workdir).TrimEnd('\\', '/'), Path.GetFullPath(tokWorkdir).TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase))
                        {
                            var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                            Set(o, "status", "AUTH_TOKEN_WORKDIR_MISMATCH");
                            Set(o, "error", "Authorization token was bound to a different workdir");
                            Print(o); return o;
                        }
                    }
                    catch
                    {
                        var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                        Set(o, "status", "AUTH_TOKEN_WORKDIR_MISMATCH");
                        Set(o, "error", "Workdir could not be resolved or compared");
                        Print(o); return o;
                    }
                }

                if (object.Equals(Get(tokRecord, "trusted"), true))
                {
                    var tm = LoadTrustedMode();
                    var rec = GetAccountTrustedRecord(tm, currentAccountId);
                    string pref = S(rec, "preference") ?? "OFF";
                    if (pref != "ON")
                    {
                        var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                        Set(o, "status", "TRUSTED_MODE_REVOKED");
                        Set(o, "error", "Trusted mode was disabled after token issuance");
                        Print(o); return o;
                    }
                    long currentGen = 1;
                    if (rec.ContainsKey("generation") && rec["generation"] != null)
                        long.TryParse(rec["generation"].ToString(), out currentGen);
                    long tokGen = 1;
                    if (tokRecord.ContainsKey("policy_generation") && tokRecord["policy_generation"] != null)
                        long.TryParse(tokRecord["policy_generation"].ToString(), out tokGen);
                    if (tokGen != currentGen)
                    {
                        var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                        Set(o, "status", "POLICY_GENERATION_STALE");
                        Set(o, "error", "Policy generation changed after token issuance");
                        Print(o); return o;
                    }

                    launchTrusted = true;
                    var rArr = Get(tokRecord, "trusted_roots") as object[];
                    if (rArr != null)
                    {
                        foreach (object r in rArr) if (r is string) trustedRootsToGrant.Add((string)r);
                    }
                }

                Set(tokRecord, "used", true);
                Set(tokRecord, "consumed_at", DateTime.UtcNow.ToString("o"));
                SaveAuthorizations(auths);
            }
        }

        var resp = Obj(); Set(resp, "schema", "sai.accounts/launch/1");
        Set(resp, "account_id", S(acc, "account_id"));
        Set(resp, "workdir", workdir);
        Set(resp, "launch_trusted", launchTrusted);
        var trArr = new object[trustedRootsToGrant.Count];
        for (int m = 0; m < trustedRootsToGrant.Count; m++) trArr[m] = trustedRootsToGrant[m];
        Set(resp, "trusted_roots", trArr);

        if (provider == "antigravity")
        {
            string targetSid = IsWindowsUserBackend(backend) ? AccountSid(acc) : null;
            string currentSid = WindowsIdentity.GetCurrent().User.Value;
            if (!IsWindowsUserBackend(backend))
            {
                Set(resp, "status", "BACKEND_UNSUPPORTED"); Print(resp); return resp;
            }
            if (launchTrusted && !string.IsNullOrEmpty(targetSid))
            {
                foreach (string root in trustedRootsToGrant)
                {
                    if (string.IsNullOrEmpty(root) || !Directory.Exists(root))
                    {
                        var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                        Set(o, "status", "TRUSTED_GRANT_FAILED");
                        Set(o, "root", root ?? "");
                        Set(o, "error", "Trusted root does not exist: " + root);
                        Print(o); return o;
                    }
                    var hReq = Obj();
                    Set(hReq, "op", "managed_workdir_ensure_access");
                    Set(hReq, "target_sid", targetSid);
                    Set(hReq, "workdir", root);
                    var hResp = CallHelper(hReq, 20000);
                    bool ok = false;
                    if (hResp != null)
                    {
                        string hStat = S(hResp, "status");
                        if (string.Equals(hStat, "access_granted", StringComparison.OrdinalIgnoreCase) ||
                            string.Equals(hStat, "already_granted", StringComparison.OrdinalIgnoreCase) ||
                            string.Equals(hStat, "ok", StringComparison.OrdinalIgnoreCase))
                        {
                            ok = true;
                        }
                    }
                    if (!ok)
                    {
                        var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                        Set(o, "status", "TRUSTED_GRANT_FAILED");
                        Set(o, "root", root);
                        Set(o, "error", hResp != null ? (S(hResp, "error") ?? S(hResp, "status")) : "HELPER_UNREACHABLE");
                        Print(o); return o;
                    }
                }
            }
            if (string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase))
            {
                if (!File.Exists(SharedAgyPath()))
                {
                    Set(resp, "status", "EXECUTABLE_MISSING");
                    Set(resp, "error", "executable not found: " + SharedAgyPath());
                    Print(resp); return resp;
                }
                var cred = CheckCredential();
                if (!object.Equals(Get(cred, "credential_present"), true))
                {
                    Set(resp, "status", "AUTH_REQUIRED");
                    Set(resp, "error", "Account requires authentication before launching");
                    Print(resp); return resp;
                }
                try
                {
                    string compact = CompactLabel(accounts, acc);
                    var psi = AntigravityLaunch(workdir, S(acc, "account_id"), compact);
                    var proc = Process.Start(psi);
                    int procId = proc != null ? proc.Id : 0;
                    Set(resp, "status", "launched");
                    Set(resp, "pid", procId);
                    Set(resp, "execution", "current_user");
                    Set(resp, "launch_policy", AntigravityArgumentLine());
                    Set(resp, "sid", currentSid);
                    Set(resp, "console_title", AntigravityConsoleTitle(workdir, compact));
                    RecordSession(S(acc, "account_id"), currentSid, procId, workdir, launchTrusted, trustedRootsToGrant);
                }
                catch (Exception ex) { Set(resp, "status", "launch_failed"); Set(resp, "error", ex.Message); }
                Print(resp);
                return resp;
            }
            bool isManaged = backend == "managed_windows_user" || object.Equals(Get(meta, "managed"), true);
            if (isManaged)
            {
                // A broker may outlive a helper upgrade. Repair the managed
                // user's persisted defaults before its next visible console,
                // including the already-online path. Never rewrite an
                // external Windows user's preferences.
                var profile = CallHelper(new Dictionary<string, object> {
                    { "op", "managed_console_profile" }, { "target_sid", targetSid }
                }, 5000);
                Set(resp, "console_profile", profile == null ? "HELPER_UNREACHABLE" : S(profile, "status"));
                if (profile != null)
                {
                    Set(resp, "input_languages", S(profile, "input_languages"));
                    Set(resp, "console_presentation", S(profile, "console_presentation"));
                    if (Get(profile, "console_presentation_error") != null)
                        Set(resp, "console_presentation_error", Get(profile, "console_presentation_error"));
                    if (Get(profile, "error") != null) Set(resp, "console_profile_error", Get(profile, "error"));
                }
            }
            string pipe = PipeNameFor(ScopeId(), Fingerprint(targetSid));
            var reply = SendPipeCommand(pipe, AntigravityLaunchRequest(acc, accounts, workdir), 8000);
            if (reply == null)
            {
                if (isManaged && state == "ENABLED")
                {
                    var hReq = Obj();
                    Set(hReq, "op", "managed_context_start");
                    Set(hReq, "target_sid", targetSid);
                    Set(hReq, "scope_id", ScopeId());
                    Set(hReq, "operator_sid", currentSid);
                    var hResp = CallHelper(hReq, 5000);
                    if (hResp != null && string.Equals(S(hResp, "status"), "started"))
                    {
                        foreach (string key in new[] { "console_profile", "input_languages", "console_profile_error", "console_presentation", "console_presentation_error" })
                        {
                            resp.Remove(key);
                            if (Get(hResp, key) != null) Set(resp, key, Get(hResp, key));
                        }
                        var sw = Stopwatch.StartNew();
                        while (sw.ElapsedMilliseconds < 4000)
                        {
                            Thread.Sleep(200);
                            reply = SendPipeCommand(pipe, AntigravityLaunchRequest(acc, accounts, workdir), 8000);
                            if (reply != null) break;
                        }
                    }
                }
            }
            if (reply == null)
            {
                Set(resp, "status", "CONTEXT_OFFLINE"); Set(resp, "execution", "broker");
                Print(resp); Environment.ExitCode = 2; return resp;
            }
            foreach (var kv in reply) Set(resp, kv.Key == "context" ? "broker_context" : kv.Key, kv.Value);
            Set(resp, "execution", "broker");
            int brokerPid = 0;
            if (reply.ContainsKey("pid") && reply["pid"] != null)
                int.TryParse(reply["pid"].ToString(), out brokerPid);
            RecordSession(S(acc, "account_id"), targetSid, brokerPid, workdir, launchTrusted, trustedRootsToGrant);
            Print(resp);
            return resp;
        }

        if (provider == "claude" || provider == "codex")
        {
            string locator = S(meta, "profile_locator");
            if (locator == null) { Set(resp, "status", "PROFILE_LOCATOR_MISSING"); Print(resp); return resp; }
            string cliPath = FindExecutableOnPath(provider);
            if (cliPath == null)
            {
                Set(resp, "status", "EXECUTABLE_MISSING");
                Set(resp, "error", "Executable '" + provider + "' was not found on PATH.");
                Print(resp);
                return resp;
            }
            if (provider == "claude")
            {
                ReconcileClaudeHooksIfNeeded(locator);
            }
            var env = new Dictionary<string, string>();
            var scrub = new List<string>();
            if (provider == "claude") env["CLAUDE_CONFIG_DIR"] = locator;
            else
            {
                env["CODEX_HOME"] = locator;
                // inherited API-key environment stays scrubbed on isolated homes
                object rawScrub = Get(meta, "env_scrub");
                var scrubList = rawScrub as object[];
                if (scrubList != null) foreach (object s in scrubList) scrub.Add((string)s);
            }
            // Presentation labels are DERIVED, never stored: compact_label and
            // display_name are computed from the provider-scoped
            // provider_ordinal for `list`/`get` and are absent from the row
            // itself. Reading them raw here yields the same "[CX] CX" title for
            // every account of a provider that has no alias, so the window
            // identity collapses exactly where the operator needs it most.
            string compact = CompactLabel(accounts, acc);
            string displayName = DisplayName(accounts, acc);
            string accountId = S(acc, "account_id") ?? "";
            string windowTitle = "[" + compact + "] " + displayName + (string.IsNullOrEmpty(workdir) ? "" : " - " + workdir);
            string cliCommand = provider == "claude"
                ? "cmd.exe /c claude --dangerously-skip-permissions"
                : "cmd.exe /c codex --dangerously-bypass-approvals-and-sandbox";
            // The vendor TUI owns the console title from the moment it starts,
            // so a title set once at CreateProcess time (or patched on the
            // window from outside) is gone seconds later and two accounts look
            // identical. console-title-guard is the FIRST process of that
            // console: it labels the window, runs the CLI as its child and puts
            // the identity back for as long as the child lives.
            string self;
            try { self = Process.GetCurrentProcess().MainModule.FileName; }
            catch { self = null; }
            string command;
            if (!string.IsNullOrEmpty(self))
            {
                string guard = Convert.ToBase64String(Encoding.UTF8.GetBytes(windowTitle));
                command = "\"" + self + "\" console-title-guard --title64 " + guard
                        + " --account \"" + accountId.Replace("\"", "") + "\" -- " + cliCommand;
            }
            else command = cliCommand;
            var launch = LaunchInNewConsole(command, workdir, env, scrub, windowTitle, accountId);
            foreach (var kv in launch) Set(resp, kv.Key, kv.Value);
            Set(resp, "execution", "profile_directory");
            Print(resp);
            return resp;
        }

        Set(resp, "status", "PROVIDER_UNSUPPORTED"); Print(resp); return resp;
    }

    // Authentication is a first-class account-plane operation and is shaped
    // like nothing else here on purpose: NO workdir argument, NO arbitrary
    // command, and exactly one resolution target -- the selected account. Only
    // SharedAgyPath() is ever launched. A managed account is served by ITS OWN
    // broker under ITS OWN SID; there is no fallback to the operator's user,
    // because a sign-in performed by the wrong identity is precisely the
    // credential crossover the managed-account model exists to prevent.
    //
    // Like status and usage, auth does not start a context for itself. The
    // caller decides whether to bring the account online first (the GUI does,
    // with a bounded wait), so "auth" stays a pure account-plane operation
    // and can never half-start a context behind the operator's back.
    static Dictionary<string, object> AuthAccount(Dictionary<string, object> acc)
    {
        string provider = S(acc, "provider_id");
        string backend = S(acc, "execution_backend");
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        string state = S(acc, "operational_state") ?? "ENABLED";

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/auth/1");
        Set(resp, "account_id", S(acc, "account_id"));

        // Interactive login happens only in the vendor's own official flow, and
        // only providers that actually have one can answer this.
        if (provider != "antigravity")
        {
            Set(resp, "status", "PROVIDER_UNSUPPORTED");
            Print(resp); return null;
        }
        if (state != "ENABLED")
        {
            Set(resp, "status", "LIFECYCLE_FORBIDDEN");
            Set(resp, "operational_state", state);
            Print(resp); return null;
        }
        if (!IsWindowsUserBackend(backend))
        {
            Set(resp, "status", "BACKEND_UNSUPPORTED");
            Print(resp); return null;
        }
        string targetSid = AccountSid(acc);
        if (string.IsNullOrEmpty(targetSid))
        {
            Set(resp, "status", "CONTEXT_IDENTITY_MISSING");
            Print(resp); return null;
        }

        string currentSid = WindowsIdentity.GetCurrent().User.Value;
        if (string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase))
        {
            string dir = OwnDirectory();
            if (dir == null) { Set(resp, "status", "AUTH_DIRECTORY_UNAVAILABLE"); Print(resp); return null; }
            try
            {
                var proc = Process.Start(new ProcessStartInfo(SharedAgyPath()) { WorkingDirectory = dir, UseShellExecute = true });
                Set(resp, "status", "launched");
                Set(resp, "pid", proc != null ? (object)proc.Id : 0);
                Set(resp, "auth_directory", dir);
                Set(resp, "sid", currentSid);
                Set(resp, "execution", "current_user");
            }
            catch (Exception ex) { Set(resp, "status", "auth_launch_failed"); Set(resp, "error", ex.Message); }
            Print(resp); return resp;
        }

        var reply = SendPipeCommand(PipeNameFor(ScopeId(), Fingerprint(targetSid)),
            new Dictionary<string, object> { { "cmd", "auth" } }, 8000);
        if (reply == null)
        {
            Set(resp, "status", "CONTEXT_OFFLINE");
            Set(resp, "execution", "broker");
            Print(resp); Environment.ExitCode = 2; return null;
        }
        foreach (var kv in reply) Set(resp, kv.Key == "context" ? "broker_context" : kv.Key, kv.Value);
        Set(resp, "execution", "broker");
        Print(resp);
        return resp;
    }

    /// `logout <selector>` -- the provider half of "Switch Google account".
    ///
    /// The guards are AuthAccount's guards on purpose: same lifecycle, same
    /// backend, same SID resolution, so the two halves of a switch cannot
    /// disagree about which account they are acting on. What changes is only the
    /// direction -- auth launches the vendor's own flow, logout drops the
    /// credential that flow would replace.
    ///
    /// It does NOT touch the registry. The account stays configured, ENABLED and
    /// connected; only its provider authentication goes, which is what makes
    /// "log out of Google" different from "remove this account".
    ///
    /// There is no --all, no --target, no --path. The selector is the entire
    /// input surface, and the credential store it reaches is the one belonging to
    /// the Windows user that account already runs as.
    static int CmdLogout(string selector)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        string provider = S(acc, "provider_id");
        string backend = S(acc, "execution_backend");
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        string state = S(acc, "operational_state") ?? "ENABLED";

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/logout/1");
        Set(resp, "account_id", S(acc, "account_id"));

        if (provider != "antigravity") { Set(resp, "status", "PROVIDER_UNSUPPORTED"); Print(resp); return 1; }
        if (state != "ENABLED") { Set(resp, "status", "LIFECYCLE_FORBIDDEN"); Set(resp, "operational_state", state); Print(resp); return 1; }
        if (!IsWindowsUserBackend(backend)) { Set(resp, "status", "BACKEND_UNSUPPORTED"); Print(resp); return 1; }
        string targetSid = AccountSid(acc);
        if (string.IsNullOrEmpty(targetSid)) { Set(resp, "status", "CONTEXT_IDENTITY_MISSING"); Print(resp); return 1; }

        string currentSid = WindowsIdentity.GetCurrent().User.Value;
        if (string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase))
        {
            // The caller's OWN account. The operation is the same narrow one, run
            // in this process, which is already that user -- so it still cannot
            // reach any other store.
            foreach (var kv in AntigravityLogout()) Set(resp, kv.Key, kv.Value);
            Set(resp, "execution", "current_user");
            Print(resp);
            return 0;
        }

        var reply = SendPipeCommand(PipeNameFor(ScopeId(), Fingerprint(targetSid)),
            new Dictionary<string, object> { { "cmd", "logout" } }, 15000);
        if (reply == null)
        {
            Set(resp, "status", "CONTEXT_OFFLINE");
            Set(resp, "execution", "broker");
            Print(resp); return 2;
        }
        foreach (var kv in reply) Set(resp, kv.Key == "context" ? "broker_context" : kv.Key, kv.Value);
        Set(resp, "execution", "broker");
        Print(resp);
        return 0;
    }

    // Grants ONE managed SID access to ONE operator-selected project directory.
    // The engine does the resolving (selector -> exact SID, lifecycle, backend)
    // so the privileged helper only ever sees a SID and a path, and the caller
    // never gets to choose either. Intended to be driven by the GUI in response
    // to a real ACCESS_DENIED from the broker, once, for that same path.
    // ------------------------------------------------- SAIPEN machine runtime
    // The shared SAIPEN runtime lives beside this control plane, under
    // ProgramData, readable by every managed account and writable by none of
    // them. These are its only entry points this engine knows about.
    static string SaipenMachineRoot()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "SAI", "SAIPEN");
    }
    static string SaipenMachineBin() { return Path.Combine(SaipenMachineRoot(), "bin"); }
    static string SaipenMachineLauncher() { return Path.Combine(SaipenMachineBin(), "saipen.cmd"); }

    // The whole readiness question, answered by whichever token runs it: the
    // broker for a managed account, this process for the operator's own.
    // Every step is a read-only SAIPEN command with fixed argv.
    static Dictionary<string, object> SaipenReadinessHere(string workdir)
    {
        var o = Obj();
        Set(o, "status", "ok");
        Set(o, "schema", "sai.accounts/saipen-readiness-probe/1");
        try
        {
            var me = WindowsIdentity.GetCurrent();
            Set(o, "effective_user", me.Name);
            Set(o, "effective_sid", me.User != null ? me.User.Value : null);
            Set(o, "elevated", new WindowsPrincipal(me).IsInRole(WindowsBuiltInRole.Administrator));
        }
        catch (Exception ex) { Set(o, "identity_error", ex.GetType().Name); }
        Set(o, "workdir", workdir);
        string launcher = SaipenMachineLauncher();
        Set(o, "launcher", launcher);
        if (!File.Exists(launcher))
        {
            Set(o, "launcher_present", false);
            return o;
        }
        Set(o, "launcher_present", true);
        Set(o, "machine", SaipenJson(launcher, "host machine status --json", workdir));
        if (workdir != null)
        {
            Set(o, "bootstrap", SaipenJson(launcher, "host bootstrap --json", workdir));
            Set(o, "project", SaipenJson(launcher, "status --json", workdir));
            // A lifecycle WRITE meets gates a read never does (cross-user home
            // substitution, owned-path proofs). The dry run asks those exact
            // gates and writes nothing.
            Set(o, "write_admission", SaipenJson(launcher,
                "checkpoint DEC sai-accounts-readiness-probe --dry-run --json", workdir));
        }
        return o;
    }

    // One machine-runtime command through cmd.exe, so the bytes measured are
    // the launcher the agent itself will run. stdout is SAIPEN's JSON whether
    // it accepts or refuses; anything else is kept as a bounded tail.
    static Dictionary<string, object> SaipenJson(string launcher, string fixedArgs, string workdir)
    {
        string cmd = Path.Combine(Environment.SystemDirectory, "cmd.exe");
        var cap = RunCapture(cmd, "/d /s /c \"\"" + launcher + "\" " + fixedArgs + "\"", 180000,
            new Dictionary<string, string> { { "PYTHONIOENCODING", "utf-8" } }, workdir, true, 0);
        var o = Obj();
        Set(o, "argv", "saipen " + fixedArgs);
        Set(o, "capture_status", S(cap, "status"));
        if (Get(cap, "exit_code") != null) Set(o, "exit_code", Get(cap, "exit_code"));
        if (Get(cap, "error") != null) Set(o, "error", Get(cap, "error"));
        string stdout = Get(cap, "stdout") as string;
        if (stdout == null) return o;
        object parsed = null;
        int start = stdout.IndexOf('{');
        int end = stdout.LastIndexOf('}');
        if (start >= 0 && end > start)
        {
            try { parsed = Json.DeserializeObject(stdout.Substring(start, end - start + 1)); } catch { parsed = null; }
        }
        if (parsed is Dictionary<string, object>) Set(o, "json", parsed);
        else Set(o, "raw_tail", stdout.Length > 4096 ? stdout.Substring(stdout.Length - 4096) : stdout);
        return o;
    }

    static object Dig(object obj, params string[] keys)
    {
        foreach (string k in keys) { obj = Get(obj, k); if (obj == null) return null; }
        return obj;
    }

    // `saipen-readiness <selector> [--workdir <path>]`: six separate facts and
    // one verdict. A verdict is READY only when every fact holds; otherwise it
    // names the FIRST failing class, so the operator is never told to retry cc
    // when nothing in the environment changed.
    static int CmdSaipenReadiness(string selector, string workdir)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var accounts = AccountsOf(reg);
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/saipen-readiness/1");
        Set(resp, "account_id", S(acc, "account_id"));
        Set(resp, "compact_label", CompactLabel(accounts, acc));
        Set(resp, "workdir", workdir);
        var facts = Obj();
        Set(resp, "facts", facts);
        string verdict = null;

        string state = S(acc, "operational_state") ?? "ENABLED";
        string backend = S(acc, "execution_backend");
        Set(facts, "account", Pairs("state", state, "backend", backend));
        if (state != "ENABLED") verdict = "ACCOUNT_DISABLED";

        string targetSid = IsWindowsUserBackend(backend) ? AccountSid(acc) : null;
        string currentSid = WindowsIdentity.GetCurrent().User.Value;
        bool viaBroker = targetSid != null && !string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase);
        Set(resp, "execution", viaBroker ? "broker" : "current_user");

        Dictionary<string, object> probe = null, access = null, cred = null;
        if (viaBroker)
        {
            string pipe = PipeNameFor(ScopeId(), Fingerprint(targetSid));
            var st = SendPipeCommand(pipe, new Dictionary<string, object> { { "cmd", "status" } }, 5000);
            if (st == null)
            {
                Set(facts, "broker", Pairs("state", "UNAVAILABLE"));
                if (verdict == null) verdict = "BROKER_UNAVAILABLE";
            }
            else
            {
                bool identityOk = string.Equals(S(st, "sid"), targetSid, StringComparison.OrdinalIgnoreCase);
                Set(facts, "broker", Pairs("state", "ONLINE", "sid", S(st, "sid"), "identity_verified", identityOk));
                if (!identityOk && verdict == null) verdict = "HOST_IDENTITY_MISMATCH";
                cred = Get(st, "credential") as Dictionary<string, object>;
                if (workdir != null)
                    access = SendPipeCommand(pipe, new Dictionary<string, object> { { "cmd", "workdir_probe" }, { "workdir", workdir } }, 30000);
                var readyReq = new Dictionary<string, object> { { "cmd", "saipen_readiness" } };
                if (workdir != null) readyReq["workdir"] = workdir;
                probe = SendPipeCommand(pipe, readyReq, 600000);
                if (probe != null && S(probe, "status") == "OPERATION_NOT_ALLOWED")
                {
                    // An older broker image: it is alive but predates this op.
                    Set(facts, "broker", Pairs("state", "STALE_IMAGE", "sid", S(st, "sid"), "identity_verified", identityOk));
                    if (verdict == null) verdict = "BROKER_STALE";
                    probe = null;
                }
            }
        }
        else
        {
            Set(facts, "broker", Pairs("state", "NOT_REQUIRED", "sid", currentSid, "identity_verified", true));
            if (workdir != null)
            {
                string denial = WorkdirDenialReason(workdir);
                access = denial == null ? Pairs("status", "ok") : Pairs("status", "WORKDIR_UNAVAILABLE", "reason", denial);
            }
            probe = SaipenReadinessHere(workdir == null ? null : Path.GetFullPath(workdir));
        }

        // Provider authentication: presence of the vendor credential only.
        if (cred != null)
        {
            bool present = object.Equals(Get(cred, "credential_present"), true);
            Set(facts, "credentials", Pairs("present", present));
            if (!present && verdict == null) verdict = "CREDENTIALS_UNAVAILABLE";
        }
        else Set(facts, "credentials", Pairs("present", null, "note", viaBroker ? "broker did not report" : "profile-directory account; checked by its own adapter"));

        // Project access, measured by the account's token where it applies.
        if (workdir != null)
        {
            string ast = access == null ? "UNKNOWN" : S(access, "status");
            Set(facts, "project_access", Pairs("status", ast, "reason", access == null ? null : S(access, "reason"),
                "error", access == null ? null : S(access, "error")));
            if (ast != "ok" && verdict == null) verdict = "PROJECT_ACCESS_DENIED";
        }

        // Machine runtime, as the account resolves it.
        string runtimeState;
        if (probe == null) runtimeState = "UNKNOWN";
        else if (object.Equals(Get(probe, "launcher_present"), false)) runtimeState = "UNAVAILABLE";
        else
        {
            string code = Dig(probe, "machine", "json", "code") as string;
            bool ready = object.Equals(Dig(probe, "machine", "json", "saipen_runtime_ready"), true);
            if (ready) runtimeState = "READY";
            else if (code != null && code.IndexOf("STALE", StringComparison.OrdinalIgnoreCase) >= 0) runtimeState = "STALE";
            else runtimeState = "UNAVAILABLE";
            Set(facts, "saipen_runtime_detail", Pairs("code", code,
                "generation", Dig(probe, "machine", "json", "generation"),
                "current", Dig(probe, "machine", "json", "current"),
                "capture", Dig(probe, "machine", "capture_status"),
                "exit_code", Dig(probe, "machine", "exit_code")));
        }
        Set(facts, "saipen_runtime", Pairs("state", runtimeState));
        if (runtimeState == "UNAVAILABLE" && verdict == null) verdict = "RUNTIME_UNAVAILABLE";
        if (runtimeState == "STALE" && verdict == null) verdict = "RUNTIME_STALE";

        // Host launch: the shared vendor binary exists and the token is the account's.
        bool agy = File.Exists(SharedAgyPath());
        string effective = probe == null ? null : S(probe, "effective_sid");
        bool tokenOk = probe != null && (targetSid == null ? string.Equals(effective, currentSid, StringComparison.OrdinalIgnoreCase)
            : string.Equals(effective, targetSid, StringComparison.OrdinalIgnoreCase));
        Set(facts, "host_launch", Pairs("vendor_binary_present", agy, "effective_sid", effective, "token_matches_account", tokenOk,
            "effective_user", probe == null ? null : S(probe, "effective_user"),
            "elevated", probe == null ? null : Get(probe, "elevated")));
        if (probe != null && !tokenOk && verdict == null) verdict = "HOST_IDENTITY_MISMATCH";

        // Canonical project admission, read-only.
        if (workdir != null && probe != null)
        {
            string boot = Dig(probe, "bootstrap", "json", "code") as string;
            var project = Dig(probe, "project", "json") as Dictionary<string, object>;
            object boardErrors = Get(project, "board_errors");
            bool boardClean = boardErrors is object[] ? ((object[])boardErrors).Length == 0 : boardErrors == null || boardErrors is System.Collections.ICollection && ((System.Collections.ICollection)boardErrors).Count == 0;
            var write = Dig(probe, "write_admission", "json") as Dictionary<string, object>;
            bool writable = write != null && object.Equals(Get(write, "ok"), true) && object.Equals(Get(write, "dry_run"), true);
            bool admitted = boot == "HOST_BOOTSTRAP_BOUND" && project != null && object.Equals(Get(project, "ok"), true)
                && boardClean && !object.Equals(Get(project, "recovery_pending"), true) && writable;
            Set(facts, "project_admission", Pairs("state", admitted ? "ADMITTED" : "DENIED",
                "bootstrap", boot,
                "bootstrap_boundary", Dig(probe, "bootstrap", "json", "boundary"),
                "runtime_home", Dig(probe, "bootstrap", "json", "saipen_home"),
                "legacy_home_inaccessible", Dig(probe, "bootstrap", "json", "binding", "persisted_home_inaccessible"),
                "phase", Get(project, "phase"), "task", Get(project, "task"),
                "next_action", Get(project, "computed_next_action"),
                "write_admission", write == null ? "UNKNOWN" : (writable ? "ADMITTED" : (S(write, "code") ?? "DENIED")),
                "write_detail", write == null ? Dig(probe, "write_admission", "raw_tail") : (writable ? null : Get(write, "detail") ?? Get(write, "message")),
                "error", project == null ? Dig(probe, "project", "raw_tail") : (Get(project, "error") ?? Get(project, "code"))));
            if (!admitted && verdict == null) verdict = "PROJECT_LIFECYCLE_DENIED";
        }
        Set(resp, "probe", probe);
        Set(resp, "verdict", verdict ?? "READY");
        Set(resp, "status", verdict == null ? "ok" : verdict);
        Print(resp);
        if (verdict == null) return 0;
        return verdict == "BROKER_UNAVAILABLE" ? 2 : 1;
    }

    static Dictionary<string, object> Pairs(params object[] kv)
    {
        var o = Obj();
        for (int i = 0; i + 1 < kv.Length; i += 2) Set(o, (string)kv[i], kv[i + 1]);
        return o;
    }

    // A managed Antigravity console gets the machine SAIPEN launcher on its
    // PATH, so `saipen` resolves to the shared runtime and never to a path in
    // another user's profile. Prepended once; absent runtime changes nothing.
    static void ProvideSaipenEntrypoint(ProcessStartInfo psi, string accountId)
    {
        if (!IsAntigravityInteractiveChild(psi, accountId)) return;
        string bin = SaipenMachineBin();
        if (!File.Exists(Path.Combine(bin, "saipen.cmd"))) return;
        string path = psi.EnvironmentVariables["PATH"] ?? "";
        foreach (string part in path.Split(';'))
            if (string.Equals(part.Trim().TrimEnd('\\'), bin.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)) return;
        psi.EnvironmentVariables["PATH"] = bin + (path.Length > 0 ? ";" + path : "");
    }

    static int CmdGrantWorkdir(string selector, string workdir)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        string backend = S(acc, "execution_backend");
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        string state = S(acc, "operational_state") ?? "ENABLED";

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/grant-workdir/1");
        Set(resp, "account_id", S(acc, "account_id"));
        Set(resp, "workdir", workdir);

        if (state != "ENABLED")
        {
            Set(resp, "status", "LIFECYCLE_FORBIDDEN"); Set(resp, "operational_state", state);
            Print(resp); return 1;
        }
        if (string.IsNullOrEmpty(workdir))
        {
            Set(resp, "status", "WORKDIR_REQUIRED"); Print(resp); return 1;
        }
        if (!IsWindowsUserBackend(backend))
        {
            Set(resp, "status", "BACKEND_UNSUPPORTED"); Print(resp); return 1;
        }
        string targetSid = AccountSid(acc);
        if (string.IsNullOrEmpty(targetSid))
        {
            Set(resp, "status", "CONTEXT_IDENTITY_MISSING"); Print(resp); return 1;
        }
        string currentSid = WindowsIdentity.GetCurrent().User.Value;
        if (string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase))
        {
            // Already the operator's own account: there is nothing to grant and
            // nothing that could have been denied.
            Set(resp, "status", "ALREADY_OWNED"); Print(resp); return 0;
        }

        var hReq = Obj();
        Set(hReq, "op", "managed_workdir_ensure_access");
        Set(hReq, "target_sid", targetSid);
        Set(hReq, "workdir", workdir);
        var hResp = CallHelper(hReq, 20000);
        if (hResp == null)
        {
            Set(resp, "status", "HELPER_UNAVAILABLE");
            Print(resp); return 1;
        }
        foreach (var kv in hResp) if (kv.Key != "schema") Set(resp, kv.Key, kv.Value);
        Print(resp);
        string st = S(resp, "status");
        return st == "granted" || st == "already_granted" ? 0 : 1;
    }

    static int CmdRevokeWorkdir(string selector, string workdir)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        string backend = S(acc, "execution_backend");
        var resp = Obj();
        Set(resp, "schema", "sai.accounts/revoke-workdir/1");
        Set(resp, "account_id", S(acc, "account_id"));
        Set(resp, "workdir", workdir);

        if (string.IsNullOrEmpty(workdir))
        {
            Set(resp, "status", "WORKDIR_REQUIRED"); Print(resp); return 1;
        }
        if (!IsWindowsUserBackend(backend))
        {
            Set(resp, "status", "BACKEND_UNSUPPORTED"); Print(resp); return 1;
        }
        string targetSid = AccountSid(acc);
        if (string.IsNullOrEmpty(targetSid))
        {
            Set(resp, "status", "CONTEXT_IDENTITY_MISSING"); Print(resp); return 1;
        }
        string currentSid = WindowsIdentity.GetCurrent().User.Value;
        if (string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase))
        {
            Set(resp, "status", "CANNOT_REVOKE_OWNER"); Print(resp); return 1;
        }

        var hReq = Obj();
        Set(hReq, "op", "managed_workdir_revoke_path_access");
        Set(hReq, "target_sid", targetSid);
        Set(hReq, "workdir", workdir);
        var hResp = CallHelper(hReq, 20000);
        if (hResp == null)
        {
            Set(resp, "status", "HELPER_UNAVAILABLE");
            Print(resp); return 1;
        }
        foreach (var kv in hResp) if (kv.Key != "schema") Set(resp, kv.Key, kv.Value);
        Print(resp);
        string st = S(resp, "status");
        return st == "revoked" || st == "already_revoked" ? 0 : 1;
    }

    // `probe-workdir <selector> --workdir <path>`: what THIS account's token can
    // open, and nothing else. It is the question a caller asks before granting
    // temporary read access to an object inside an operator-private tree:
    // reaching a nested object already requires traverse on every ancestor,
    // so a probe of the immediate parent answers the whole chain. It never
    // grants, never mutates, and never follows a reparse point to justify a
    // different answer.
    static int CmdProbeWorkdir(string selector, string workdir)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/probe-workdir/1");
        Set(resp, "account_id", S(acc, "account_id"));

        string backend = S(acc, "execution_backend");
        if (!IsWindowsUserBackend(backend))
        {
            Set(resp, "status", "BACKEND_UNSUPPORTED");
            Print(resp);
            return 1;
        }
        string targetSid = AccountSid(acc);
        if (string.IsNullOrEmpty(targetSid))
        {
            Set(resp, "status", "CONTEXT_IDENTITY_MISSING");
            Print(resp);
            return 1;
        }
        Set(resp, "target_sid", targetSid);
        if (string.IsNullOrEmpty(workdir))
        {
            Set(resp, "status", "WORKDIR_REQUIRED");
            Print(resp);
            return 1;
        }
        string fullWorkdir;
        try { fullWorkdir = Path.GetFullPath(workdir); }
        catch (Exception ex)
        {
            Set(resp, "status", "WORKDIR_INVALID_PATH");
            Set(resp, "error", ex.Message);
            Print(resp);
            return 1;
        }
        Set(resp, "workdir", fullWorkdir);
        if (!Directory.Exists(fullWorkdir))
        {
            Set(resp, "status", "WORKDIR_NOT_FOUND");
            Print(resp);
            return 1;
        }

        string currentSid = WindowsIdentity.GetCurrent().User.Value;
        if (string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase))
        {
            Set(resp, "status", "ok");
            Set(resp, "read", true);
            Set(resp, "execution", "current_user");
            Print(resp);
            return 0;
        }
        var reply = SendPipeCommand(PipeNameFor(ScopeId(), Fingerprint(targetSid)),
            new Dictionary<string, object> { { "cmd", "workdir_probe" }, { "workdir", fullWorkdir } }, 30000);
        if (reply == null)
        {
            Set(resp, "status", "CONTEXT_OFFLINE");
            Print(resp);
            return 1;
        }
        foreach (var kv in reply) if (kv.Key != "schema") Set(resp, kv.Key, kv.Value);
        Print(resp);
        return S(resp, "status") == "ok" ? 0 : 1;
    }

    // One staging boundary for all three delivery modes. Returns the staging
    // directory path or null with the failure class in `failure`. The DACL is
    // explicit and non-inherited: exactly the operator (FullControl), SYSTEM
    // (FullControl) and the managed SID (Modify|Synchronize). Stripping inherited
    // ACEs is what makes the boundary an isolation boundary rather than a
    // convenience: any other account, managed or not, is refused here.
    static string EnsureHandoffStaging(string fullWorkdir, string compact, string targetSid, out string failure)
    {
        failure = null;
        try
        {
            string safeCompact = string.IsNullOrEmpty(compact) ? "agent" : compact.ToLowerInvariant().Replace(":", "_").Replace(" ", "_");
            string handoffRoot = Path.Combine(fullWorkdir, ".sai-handoff");
            string stagingDir = Path.Combine(handoffRoot, safeCompact);

            Directory.CreateDirectory(handoffRoot);
            string gitignore = Path.Combine(handoffRoot, ".gitignore");
            if (!File.Exists(gitignore))
            {
                File.WriteAllText(gitignore, "*\r\n!.gitignore\r\n", Encoding.UTF8);
            }

            Directory.CreateDirectory(stagingDir);

            // Stale partial files from a crashed copy are litter, not evidence.
            try
            {
                var dirInfo = new DirectoryInfo(stagingDir);
                foreach (var f in dirInfo.GetFiles("*.partial.*.tmp"))
                {
                    if (DateTime.UtcNow - f.LastWriteTimeUtc > TimeSpan.FromHours(1))
                    {
                        try { f.Delete(); } catch { }
                    }
                }
            }
            catch { }

            var targetSidObj = new SecurityIdentifier(targetSid);
            var operatorSidObj = WindowsIdentity.GetCurrent().User;
            var systemSidObj = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);

            var dSec = new DirectorySecurity();
            dSec.SetAccessRuleProtection(true, false);

            dSec.AddAccessRule(new FileSystemAccessRule(
                operatorSidObj,
                FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None,
                AccessControlType.Allow));

            dSec.AddAccessRule(new FileSystemAccessRule(
                systemSidObj,
                FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None,
                AccessControlType.Allow));

            dSec.AddAccessRule(new FileSystemAccessRule(
                targetSidObj,
                FileSystemRights.Modify | FileSystemRights.Synchronize,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None,
                AccessControlType.Allow));

            var dir = new DirectoryInfo(stagingDir);
            dir.SetAccessControl(dSec);
            return stagingDir;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("EnsureHandoffStaging error: " + ex.Message);
            failure = "STAGING_ACL_FAILED";
            return null;
        }
    }

    static int CmdHandoffStage(string selector, string workdir)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var accounts = AccountsOf(reg);
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/handoff-stage/1");
        Set(resp, "account_id", S(acc, "account_id"));
        string compact = CompactLabel(accounts, acc);
        Set(resp, "compact_label", compact);

        string backend = S(acc, "execution_backend");
        if (!IsWindowsUserBackend(backend))
        {
            Set(resp, "status", "BACKEND_UNSUPPORTED");
            Print(resp);
            return 1;
        }
        string targetSid = AccountSid(acc);
        if (string.IsNullOrEmpty(targetSid))
        {
            Set(resp, "status", "CONTEXT_IDENTITY_MISSING");
            Print(resp);
            return 1;
        }
        Set(resp, "target_sid", targetSid);

        if (string.IsNullOrEmpty(workdir))
        {
            Set(resp, "status", "WORKDIR_REQUIRED");
            Print(resp);
            return 1;
        }
        string fullWorkdir;
        try { fullWorkdir = Path.GetFullPath(workdir); }
        catch (Exception ex)
        {
            Set(resp, "status", "WORKDIR_INVALID_PATH");
            Set(resp, "error", ex.Message);
            Print(resp);
            return 1;
        }
        Set(resp, "workdir", fullWorkdir);
        if (!Directory.Exists(fullWorkdir))
        {
            Set(resp, "status", "WORKDIR_NOT_FOUND");
            Print(resp);
            return 1;
        }

        string failure = null;
        string stagingDir = EnsureHandoffStaging(fullWorkdir, compact, targetSid, out failure);
        if (stagingDir == null)
        {
            Set(resp, "status", failure ?? "STAGING_FAILED");
            Print(resp);
            return 1;
        }
        Set(resp, "status", "ok");
        Set(resp, "staging_dir", stagingDir);
        Print(resp);
        return 0;
    }

    static int CmdHandoffFile(string selector, string sourcePath, string workdir)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var accounts = AccountsOf(reg);
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/handoff-file/1");
        Set(resp, "account_id", S(acc, "account_id"));
        string compact = CompactLabel(accounts, acc);
        Set(resp, "compact_label", compact);

        string backend = S(acc, "execution_backend");
        if (!IsWindowsUserBackend(backend))
        {
            Set(resp, "status", "BACKEND_UNSUPPORTED");
            Print(resp);
            return 1;
        }

        string targetSid = AccountSid(acc);
        if (string.IsNullOrEmpty(targetSid))
        {
            Set(resp, "status", "CONTEXT_IDENTITY_MISSING");
            Print(resp);
            return 1;
        }
        Set(resp, "target_sid", targetSid);

        if (string.IsNullOrEmpty(sourcePath))
        {
            Set(resp, "status", "SOURCE_REQUIRED");
            Print(resp);
            return 1;
        }

        string fullSource;
        try
        {
            fullSource = Path.GetFullPath(sourcePath);
        }
        catch (Exception ex)
        {
            Set(resp, "status", "SOURCE_INVALID_PATH");
            Set(resp, "error", ex.Message);
            Print(resp);
            return 1;
        }
        Set(resp, "source_path", fullSource);

        if (Directory.Exists(fullSource))
        {
            Set(resp, "status", "SOURCE_IS_DIRECTORY");
            Print(resp);
            return 1;
        }

        if (!File.Exists(fullSource))
        {
            Set(resp, "status", "SOURCE_NOT_FOUND");
            Print(resp);
            return 1;
        }

        try
        {
            var sAttr = File.GetAttributes(fullSource);
            if ((sAttr & FileAttributes.Directory) != 0)
            {
                Set(resp, "status", "SOURCE_IS_DIRECTORY");
                Print(resp);
                return 1;
            }
            if ((sAttr & FileAttributes.ReparsePoint) != 0)
            {
                Set(resp, "status", "REPARSE_POINT_REFUSED");
                Print(resp);
                return 1;
            }
        }
        catch (Exception ex)
        {
            Set(resp, "status", "SOURCE_ACCESS_FAILED");
            Set(resp, "error", ex.Message);
            Print(resp);
            return 1;
        }

        long sourceLen;
        try
        {
            var fi = new FileInfo(fullSource);
            sourceLen = fi.Length;
            if (sourceLen > 524288000) // 500 MB ceiling
            {
                Set(resp, "status", "FILE_TOO_LARGE");
                Set(resp, "length", sourceLen);
                Print(resp);
                return 1;
            }
        }
        catch (Exception ex)
        {
            Set(resp, "status", "SOURCE_STAT_FAILED");
            Set(resp, "error", ex.Message);
            Print(resp);
            return 1;
        }

        // Determine workdir if not provided
        if (string.IsNullOrEmpty(workdir))
        {
            var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
            if (meta != null)
            {
                workdir = S(meta, "default_workdir") ?? S(meta, "workdir");
            }
        }
        if (string.IsNullOrEmpty(workdir))
        {
            Set(resp, "status", "WORKDIR_REQUIRED");
            Print(resp);
            return 1;
        }

        string fullWorkdir;
        try
        {
            fullWorkdir = Path.GetFullPath(workdir);
        }
        catch (Exception ex)
        {
            Set(resp, "status", "WORKDIR_INVALID_PATH");
            Set(resp, "error", ex.Message);
            Print(resp);
            return 1;
        }
        Set(resp, "workdir", fullWorkdir);

        if (!Directory.Exists(fullWorkdir))
        {
            Set(resp, "status", "WORKDIR_NOT_FOUND");
            Print(resp);
            return 1;
        }

        // One reusable staging boundary for every delivery mode (SEND_FILE,
        // LIVE_LOG_RELAY, TEMPORARY_READ_ACCESS verification). Ownership:
        // the operator, because the caller is the operator.
        string stagingSetupError = null;
        string stagingDir = EnsureHandoffStaging(fullWorkdir, compact, targetSid, out stagingSetupError);
        if (stagingDir == null)
        {
            Set(resp, "status", stagingSetupError == "STAGING_ACL_FAILED" ? "STAGING_ACL_FAILED" : "STAGING_FAILED");
            Print(resp);
            return 1;
        }

        string fileName = Path.GetFileName(fullSource);
        string destPath = Path.Combine(stagingDir, fileName);
        string tempPath = destPath + ".partial." + Guid.NewGuid().ToString("N") + ".tmp";
        string sourceHashHex = null;
        string copyHashHex = null;
        long copiedBytes = 0;

        try
        {
            using (var src = new FileStream(fullSource, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (var dst = new FileStream(tempPath, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            using (var shaSrc = SHA256.Create())
            using (var shaDst = SHA256.Create())
            {
                byte[] buf = new byte[65536];
                int read;
                while ((read = src.Read(buf, 0, buf.Length)) > 0)
                {
                    dst.Write(buf, 0, read);
                    shaSrc.TransformBlock(buf, 0, read, null, 0);
                    shaDst.TransformBlock(buf, 0, read, null, 0);
                    copiedBytes += read;
                }
                shaSrc.TransformFinalBlock(buf, 0, 0);
                shaDst.TransformFinalBlock(buf, 0, 0);
                sourceHashHex = BitConverter.ToString(shaSrc.Hash).Replace("-", "").ToLowerInvariant();
                copyHashHex = BitConverter.ToString(shaDst.Hash).Replace("-", "").ToLowerInvariant();
            }

            if (copiedBytes != sourceLen || !string.Equals(sourceHashHex, copyHashHex, StringComparison.OrdinalIgnoreCase))
            {
                try { File.Delete(tempPath); } catch { }
                Set(resp, "status", "INTEGRITY_MISMATCH");
                Print(resp);
                return 1;
            }

            if (File.Exists(destPath))
            {
                File.Replace(tempPath, destPath, null);
            }
            else
            {
                File.Move(tempPath, destPath);
            }
        }
        catch (Exception ex)
        {
            try { if (File.Exists(tempPath)) File.Delete(tempPath); } catch { }
            Set(resp, "status", "STAGING_COPY_FAILED");
            Set(resp, "error", ex.Message);
            Print(resp);
            return 1;
        }

        Set(resp, "status", "ok");
        Set(resp, "staged_path", destPath);
        Set(resp, "length", copiedBytes);
        Set(resp, "sha256", sourceHashHex);

        string pipe = PipeNameFor(ScopeId(), Fingerprint(targetSid));
        var verifyResp = SendPipeCommand(pipe, new Dictionary<string, object> {
            { "cmd", "file_probe" },
            { "path", destPath }
        }, 5000);

        if (verifyResp != null && S(verifyResp, "status") == "ok")
        {
            Set(resp, "broker_verified", true);
            Set(resp, "broker_readable", true);
        }
        else
        {
            Set(resp, "broker_verified", false);
            if (verifyResp != null) Set(resp, "broker_status", S(verifyResp, "status"));
        }

        Print(resp);
        return 0;
    }

    static int CmdVerifyFile(string selector, string filePath)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var accounts = AccountsOf(reg);
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/verify-file/1");
        Set(resp, "account_id", S(acc, "account_id"));
        string compact = CompactLabel(accounts, acc);
        Set(resp, "compact_label", compact);

        string backend = S(acc, "execution_backend");
        if (!IsWindowsUserBackend(backend))
        {
            Set(resp, "status", "BACKEND_UNSUPPORTED");
            Print(resp);
            return 1;
        }

        string targetSid = AccountSid(acc);
        if (string.IsNullOrEmpty(targetSid))
        {
            Set(resp, "status", "CONTEXT_IDENTITY_MISSING");
            Print(resp);
            return 1;
        }
        Set(resp, "target_sid", targetSid);

        if (string.IsNullOrEmpty(filePath))
        {
            Set(resp, "status", "PATH_REQUIRED");
            Print(resp);
            return 1;
        }

        string fullPath;
        try { fullPath = Path.GetFullPath(filePath); }
        catch (Exception ex)
        {
            Set(resp, "status", "INVALID_PATH");
            Set(resp, "error", ex.Message);
            Print(resp);
            return 1;
        }
        Set(resp, "path", fullPath);

        string pipe = PipeNameFor(ScopeId(), Fingerprint(targetSid));
        var brokerResp = SendPipeCommand(pipe, new Dictionary<string, object> {
            { "cmd", "file_probe" },
            { "path", fullPath }
        }, 8000);

        if (brokerResp == null)
        {
            Set(resp, "status", "BROKER_UNAVAILABLE");
            Print(resp);
            return 1;
        }

        foreach (var kv in brokerResp)
        {
            if (kv.Key != "schema") Set(resp, kv.Key, kv.Value);
        }

        Print(resp);
        return S(resp, "status") == "ok" ? 0 : 1;
    }

    // ------------------------------------------------------------- Trusted Antigravity Mode (T-299)
    static bool IsDiskRoot(string path)
    {
        try
        {
            string full = Path.GetFullPath(path);
            string root = Path.GetPathRoot(full);
            return string.Equals(full.TrimEnd('\\', '/'), root.TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    static bool IsReparsePoint(string path)
    {
        try
        {
            if (!Directory.Exists(path) && !File.Exists(path)) return false;
            var attr = File.GetAttributes(path);
            return (attr & FileAttributes.ReparsePoint) != 0;
        }
        catch { return false; }
    }

    static Dictionary<string, object> LoadTrustedMode()
    {
        string p = TrustedModePath();
        if (File.Exists(p))
        {
            try
            {
                string json = File.ReadAllText(p, Encoding.UTF8);
                var doc = Json.DeserializeObject(json) as Dictionary<string, object>;
                if (doc != null) return doc;
            }
            catch { }
        }
        var init = Obj();
        Set(init, "schema", "sai.accounts/trusted-mode/1");
        Set(init, "accounts", Obj());
        return init;
    }

    static void SaveTrustedMode(Dictionary<string, object> doc)
    {
        string p = TrustedModePath();
        string dir = Path.GetDirectoryName(p);
        if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
        string tmp = p + ".tmp." + Guid.NewGuid().ToString("N");
        File.WriteAllText(tmp, Json.Serialize(doc), new UTF8Encoding(false));
        if (File.Exists(p)) File.Replace(tmp, p, null);
        else File.Move(tmp, p);
    }

    static Dictionary<string, object> GetAccountTrustedRecord(Dictionary<string, object> tmDoc, string accountId)
    {
        var accounts = Get(tmDoc, "accounts") as Dictionary<string, object>;
        if (accounts == null)
        {
            accounts = Obj();
            Set(tmDoc, "accounts", accounts);
        }
        var rec = Get(accounts, accountId) as Dictionary<string, object>;
        if (rec == null)
        {
            rec = Obj();
            Set(rec, "preference", "OFF");
            Set(rec, "status", "OFF");
            Set(rec, "approved_roots", new object[0]);
            Set(rec, "updated_at", DateTime.UtcNow.ToString("o"));
            Set(rec, "last_verified_at", "");
            Set(rec, "last_error", null);
            Set(accounts, accountId, rec);
        }
        return rec;
    }

    static Dictionary<string, object> LoadAuthorizations()
    {
        string p = AuthorizationsPath();
        if (File.Exists(p))
        {
            try
            {
                string json = File.ReadAllText(p, Encoding.UTF8);
                var doc = Json.DeserializeObject(json) as Dictionary<string, object>;
                if (doc != null) return doc;
            }
            catch { }
        }
        var init = Obj();
        Set(init, "schema", "sai.accounts/authorizations/1");
        Set(init, "tokens", Obj());
        return init;
    }

    static void SaveAuthorizations(Dictionary<string, object> doc)
    {
        string p = AuthorizationsPath();
        string dir = Path.GetDirectoryName(p);
        if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
        var tokens = Get(doc, "tokens") as Dictionary<string, object>;
        if (tokens != null)
        {
            var expiredKeys = new List<string>();
            DateTime cutoff = DateTime.UtcNow.AddMinutes(-10);
            foreach (var kv in tokens)
            {
                var tokObj = kv.Value as Dictionary<string, object>;
                if (tokObj != null)
                {
                    string expStr = S(tokObj, "expires_at");
                    DateTime exp;
                    if (DateTime.TryParse(expStr, null, DateTimeStyles.RoundtripKind, out exp) && exp < cutoff)
                        expiredKeys.Add(kv.Key);
                }
            }
            foreach (string k in expiredKeys) tokens.Remove(k);
        }
        string tmp = p + ".tmp." + Guid.NewGuid().ToString("N");
        File.WriteAllText(tmp, Json.Serialize(doc), new UTF8Encoding(false));
        if (File.Exists(p)) File.Replace(tmp, p, null);
        else File.Move(tmp, p);
    }

    static Dictionary<string, object> LoadSessions()
    {
        string p = SessionsPath();
        if (File.Exists(p))
        {
            try
            {
                string json = File.ReadAllText(p, Encoding.UTF8);
                var doc = Json.DeserializeObject(json) as Dictionary<string, object>;
                if (doc != null) return doc;
            }
            catch { }
        }
        var init = Obj();
        Set(init, "schema", "sai.accounts/sessions/1");
        Set(init, "sessions", new object[0]);
        return init;
    }

    static void SaveSessions(Dictionary<string, object> doc)
    {
        string p = SessionsPath();
        string dir = Path.GetDirectoryName(p);
        if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
        string tmp = p + ".tmp." + Guid.NewGuid().ToString("N");
        File.WriteAllText(tmp, Json.Serialize(doc), new UTF8Encoding(false));
        if (File.Exists(p)) File.Replace(tmp, p, null);
        else File.Move(tmp, p);
    }

    static void RecordSession(string accountId, string targetSid, int pid, string workdir, bool trusted, List<string> trustedRoots)
    {
        if (pid <= 0) return;
        lock (_sessionLock)
        {
            try
            {
                var doc = LoadSessions();
                var list = new List<object>();
                var existing = Get(doc, "sessions") as object[];
                if (existing != null)
                {
                    foreach (object o in existing) list.Add(o);
                }
                var sRec = Obj();
                Set(sRec, "session_id", "session_" + Guid.NewGuid().ToString("N"));
                Set(sRec, "account_id", accountId);
                Set(sRec, "target_sid", targetSid);
                Set(sRec, "pid", pid);
                Set(sRec, "workdir", workdir);
                Set(sRec, "trusted", trusted);
                var rootsArr = new object[trustedRoots != null ? trustedRoots.Count : 0];
                if (trustedRoots != null)
                {
                    for (int i = 0; i < trustedRoots.Count; i++) rootsArr[i] = trustedRoots[i];
                }
                Set(sRec, "trusted_roots", rootsArr);
                Set(sRec, "started_at", DateTime.UtcNow.ToString("o"));
                Set(sRec, "status", "running");
                list.Add(sRec);
                var outArr = new object[list.Count];
                for (int i = 0; i < list.Count; i++) outArr[i] = list[i];
                Set(doc, "sessions", outArr);
                SaveSessions(doc);
            }
            catch { }
        }
    }

    static int StopSessionsForAccount(string accountId, string targetSid, string compactLabel)
    {
        int terminated = 0;
        lock (_sessionLock)
        {
            var doc = LoadSessions();
            var sessions = Get(doc, "sessions") as object[];
            if (sessions != null)
            {
                foreach (object o in sessions)
                {
                    var sRec = o as Dictionary<string, object>;
                    if (sRec == null) continue;
                    string sAcc = S(sRec, "account_id");
                    string sSid = S(sRec, "target_sid");
                    string st = S(sRec, "status");
                    if (st != "running") continue;
                    if (!string.Equals(sAcc, accountId, StringComparison.OrdinalIgnoreCase) &&
                        !string.Equals(sSid, targetSid, StringComparison.OrdinalIgnoreCase))
                        continue;

                    int pid = 0;
                    if (sRec.ContainsKey("pid") && sRec["pid"] != null)
                        int.TryParse(sRec["pid"].ToString(), out pid);

                    if (pid > 0 && pid != Process.GetCurrentProcess().Id)
                    {
                        try
                        {
                            var p = Process.GetProcessById(pid);
                            string startTimeStr = S(sRec, "started_at");
                            DateTime startTime;
                            bool isMatching = true;
                            if (DateTime.TryParse(startTimeStr, null, DateTimeStyles.RoundtripKind, out startTime))
                            {
                                if (p.StartTime.ToUniversalTime() < startTime.AddSeconds(-2))
                                    isMatching = false;
                            }
                            if (isMatching && !p.HasExited)
                            {
                                p.Kill();
                                p.WaitForExit(2000);
                                terminated++;
                            }
                        }
                        catch { }
                    }
                    Set(sRec, "status", "stopped");
                    Set(sRec, "stopped_at", DateTime.UtcNow.ToString("o"));
                }
                SaveSessions(doc);
            }
        }
        return terminated;
    }

    static int CountActiveSessionsForAccount(string accountId, string targetSid, string compactLabel)
    {
        int count = 0;
        lock (_sessionLock)
        {
            var doc = LoadSessions();
            var sessions = Get(doc, "sessions") as object[];
            bool changed = false;
            if (sessions != null)
            {
                foreach (object o in sessions)
                {
                    var sRec = o as Dictionary<string, object>;
                    if (sRec == null) continue;
                    string sAcc = S(sRec, "account_id");
                    string sSid = S(sRec, "target_sid");
                    string st = S(sRec, "status");
                    if (st != "running") continue;
                    if (!string.Equals(sAcc, accountId, StringComparison.OrdinalIgnoreCase) &&
                        !string.Equals(sSid, targetSid, StringComparison.OrdinalIgnoreCase))
                        continue;

                    int pid = 0;
                    if (sRec.ContainsKey("pid") && sRec["pid"] != null)
                        int.TryParse(sRec["pid"].ToString(), out pid);

                    if (pid > 0)
                    {
                        try
                        {
                            var p = Process.GetProcessById(pid);
                            if (p.HasExited)
                            {
                                Set(sRec, "status", "exited");
                                changed = true;
                            }
                            else
                            {
                                count++;
                            }
                        }
                        catch
                        {
                            Set(sRec, "status", "exited");
                            changed = true;
                        }
                    }
                }
                if (changed) SaveSessions(doc);
            }
        }
        return count;
    }

    static int CmdTrustedModeStatus(string selector)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var accounts = AccountsOf(reg);
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        string accountId = S(acc, "account_id");
        string targetSid = AccountSid(acc);
        string compact = CompactLabel(accounts, acc);
        var tm = LoadTrustedMode();
        var rec = GetAccountTrustedRecord(tm, accountId);

        string pref = S(rec, "preference") ?? "OFF";
        string recordedStatus = S(rec, "status") ?? "OFF";
        int activeSessions = CountActiveSessionsForAccount(accountId, targetSid, compact);
        string effStatus = recordedStatus;

        if (pref == "OFF")
        {
            if (activeSessions > 0) effStatus = "REVOKE_PENDING";
            else if (Get(rec, "last_error") != null && recordedStatus == "ERROR") effStatus = "ERROR";
            else effStatus = "OFF_VERIFIED";
        }
        else if (pref == "ON")
        {
            var rootsObj = Get(rec, "approved_roots") as object[];
            if (rootsObj == null || rootsObj.Length == 0)
            {
                effStatus = "EMPTY_SCOPE";
            }
            else
            {
                bool rootsValid = true;
                string rootErr = null;
                foreach (object o in rootsObj)
                {
                    string rootPath = o as string;
                    if (string.IsNullOrEmpty(rootPath) || !Directory.Exists(rootPath))
                    {
                        rootsValid = false;
                        rootErr = "Approved root does not exist: " + rootPath;
                        break;
                    }
                    if (IsDiskRoot(rootPath) || IsReparsePoint(rootPath))
                    {
                        rootsValid = false;
                        rootErr = "Approved root is invalid (disk root or junction): " + rootPath;
                        break;
                    }
                }
                if (!rootsValid)
                {
                    effStatus = "ERROR";
                    Set(rec, "last_error", rootErr);
                }
                else
                {
                    effStatus = "ON_VERIFIED";
                }
            }
        }

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/trusted-mode-status/1");
        Set(resp, "account_id", accountId);
        Set(resp, "selector", selector);
        Set(resp, "target_sid", targetSid);
        Set(resp, "preference", pref);
        Set(resp, "status", effStatus);
        Set(resp, "approved_roots", Get(rec, "approved_roots") ?? new object[0]);
        Set(resp, "generation", Get(rec, "generation") ?? 1);
        Set(resp, "updated_at", S(rec, "updated_at") ?? "");
        Set(resp, "last_verified_at", S(rec, "last_verified_at") ?? "");
        Set(resp, "last_error", Get(rec, "last_error"));
        Set(resp, "active_sessions", activeSessions);

        Print(resp);
        return 0;
    }

    static int CmdTrustedModeSet(string selector, string[] args)
    {
        if (args.Length < 3)
            return Fail("trusted_mode_args", "trusted-mode-set <selector> <on|off> [--roots <path1;path2>] [--stop-session]");

        string targetState = args[2].ToLowerInvariant();
        if (targetState != "on" && targetState != "off")
            return Fail("trusted_mode_args", "state must be 'on' or 'off'");

        var reg = RequireRegistry(); if (reg == null) return 1;
        var accounts = AccountsOf(reg);
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        string accountId = S(acc, "account_id");
        string targetSid = AccountSid(acc);
        string compact = CompactLabel(accounts, acc);

        bool stopSession = false;
        string rootsArg = null;
        for (int i = 3; i < args.Length; i++)
        {
            if (args[i] == "--stop-session") stopSession = true;
            else if (args[i] == "--roots" && i + 1 < args.Length) { rootsArg = args[i + 1]; i++; }
        }

        var tm = LoadTrustedMode();
        var rec = GetAccountTrustedRecord(tm, accountId);

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/trusted-mode-set/1");
        Set(resp, "account_id", accountId);
        Set(resp, "selector", selector);
        Set(resp, "target_sid", targetSid);

        if (targetState == "on")
        {
            Set(rec, "preference", "ON");
            long gen = 1;
            if (rec.ContainsKey("generation") && rec["generation"] != null)
                long.TryParse(rec["generation"].ToString(), out gen);
            Set(rec, "generation", gen + 1);
            Set(rec, "updated_at", DateTime.UtcNow.ToString("o"));

            List<string> approved = new List<string>();
            if (!string.IsNullOrEmpty(rootsArg))
            {
                foreach (string raw in rootsArg.Split(new[] { ';', '|' }, StringSplitOptions.RemoveEmptyEntries))
                {
                    string p = raw.Trim();
                    if (string.IsNullOrEmpty(p)) continue;
                    try
                    {
                        string full = Path.GetFullPath(p);
                        if (!Directory.Exists(full))
                        {
                            Set(resp, "status", "ERROR");
                            Set(resp, "error", "WORKDIR_NOT_FOUND: Folder does not exist: " + full);
                            Set(rec, "status", "ERROR");
                            Set(rec, "last_error", S(resp, "error"));
                            SaveTrustedMode(tm);
                            Print(resp);
                            return 1;
                        }
                        if (IsDiskRoot(full))
                        {
                            Set(resp, "status", "ERROR");
                            Set(resp, "error", "DISK_ROOT_REFUSED: Blanket access to disk roots is prohibited: " + full);
                            Set(rec, "status", "ERROR");
                            Set(rec, "last_error", S(resp, "error"));
                            SaveTrustedMode(tm);
                            Print(resp);
                            return 1;
                        }
                        if (IsReparsePoint(full))
                        {
                            Set(resp, "status", "ERROR");
                            Set(resp, "error", "REPARSE_POINT_REFUSED: Symlinks and junctions are prohibited: " + full);
                            Set(rec, "status", "ERROR");
                            Set(rec, "last_error", S(resp, "error"));
                            SaveTrustedMode(tm);
                            Print(resp);
                            return 1;
                        }
                        if (!approved.Contains(full)) approved.Add(full);
                    }
                    catch (Exception ex)
                    {
                        Set(resp, "status", "ERROR");
                        Set(resp, "error", "INVALID_PATH: " + ex.Message);
                        Set(rec, "status", "ERROR");
                        Set(rec, "last_error", S(resp, "error"));
                        SaveTrustedMode(tm);
                        Print(resp);
                        return 1;
                    }
                }
                var objArr = new object[approved.Count];
                for (int j = 0; j < approved.Count; j++) objArr[j] = approved[j];
                Set(rec, "approved_roots", objArr);
            }
            else
            {
                var existing = Get(rec, "approved_roots") as object[];
                if (existing != null)
                {
                    foreach (object o in existing) if (o is string) approved.Add((string)o);
                }
            }

            if (approved.Count == 0)
            {
                Set(rec, "status", "EMPTY_SCOPE");
                Set(rec, "last_verified_at", DateTime.UtcNow.ToString("o"));
                Set(rec, "last_error", null);
            }
            else
            {
                Set(rec, "status", "ON_VERIFIED");
                Set(rec, "last_verified_at", DateTime.UtcNow.ToString("o"));
                Set(rec, "last_error", null);
            }

            SaveTrustedMode(tm);

            Set(resp, "preference", S(rec, "preference"));
            Set(resp, "status", S(rec, "status"));
            Set(resp, "approved_roots", Get(rec, "approved_roots"));
            Set(resp, "generation", Get(rec, "generation"));
            Set(resp, "last_verified_at", S(rec, "last_verified_at"));
            if (Get(rec, "last_error") != null) Set(resp, "error", Get(rec, "last_error"));

            Print(resp);
            return 0;
        }
        else
        {
            Set(rec, "preference", "OFF");
            long gen = 1;
            if (rec.ContainsKey("generation") && rec["generation"] != null)
                long.TryParse(rec["generation"].ToString(), out gen);
            Set(rec, "generation", gen + 1);
            Set(rec, "status", "DISABLING");
            Set(rec, "updated_at", DateTime.UtcNow.ToString("o"));

            int terminated = 0;
            if (stopSession)
            {
                terminated = StopSessionsForAccount(accountId, targetSid, compact);
            }

            var approvedObj = Get(rec, "approved_roots") as object[];
            bool allCleanlyRevoked = true;
            string revokeErr = null;
            if (approvedObj != null && !string.IsNullOrEmpty(targetSid))
            {
                foreach (object o in approvedObj)
                {
                    string root = o as string;
                    if (string.IsNullOrEmpty(root) || !Directory.Exists(root)) continue;

                    var hReq = Obj();
                    Set(hReq, "op", "managed_workdir_revoke_path_access");
                    Set(hReq, "target_sid", targetSid);
                    Set(hReq, "workdir", root);
                    var hResp = CallHelper(hReq, 20000);
                    if (hResp == null)
                    {
                        allCleanlyRevoked = false;
                        revokeErr = "HELPER_UNAVAILABLE during revocation of " + root;
                    }
                    else
                    {
                        string st = S(hResp, "status");
                        if (st != "revoked" && st != "already_revoked")
                        {
                            allCleanlyRevoked = false;
                            revokeErr = "REVOCATION_FAILED on " + root + ": " + st;
                        }
                    }
                }
            }

            int remainingSessions = CountActiveSessionsForAccount(accountId, targetSid, compact);
            if (remainingSessions > 0)
            {
                Set(rec, "status", "REVOKE_PENDING");
                Set(rec, "last_error", "Session active (" + remainingSessions + " processes running); open handles may retain access until session exit");
            }
            else if (!allCleanlyRevoked)
            {
                Set(rec, "status", "ERROR");
                Set(rec, "last_error", revokeErr);
            }
            else
            {
                Set(rec, "status", "OFF_VERIFIED");
                Set(rec, "last_error", null);
            }
            Set(rec, "last_verified_at", DateTime.UtcNow.ToString("o"));

            SaveTrustedMode(tm);

            Set(resp, "preference", S(rec, "preference"));
            Set(resp, "status", S(rec, "status"));
            Set(resp, "approved_roots", Get(rec, "approved_roots"));
            Set(resp, "generation", Get(rec, "generation"));
            Set(resp, "active_sessions", remainingSessions);
            Set(resp, "terminated_count", terminated);
            Set(resp, "last_verified_at", S(rec, "last_verified_at"));
            if (Get(rec, "last_error") != null) Set(resp, "error", Get(rec, "last_error"));

            Print(resp);
            return S(rec, "status") == "OFF_VERIFIED" ? 0 : 1;
        }
    }

    static int CmdAuthorizeLaunch(string selector, string[] args)
    {
        string workdir = null;
        string caller = null;
        for (int i = 2; i < args.Length; i++)
        {
            if (args[i] == "--workdir" && i + 1 < args.Length) { workdir = args[i + 1]; i++; }
            else if (args[i] == "--caller" && i + 1 < args.Length) { caller = args[i + 1]; i++; }
        }

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/authorize-launch/1");

        if (string.IsNullOrEmpty(workdir))
        {
            Set(resp, "status", "WORKDIR_REQUIRED");
            Print(resp); return 1;
        }

        if (string.IsNullOrEmpty(caller) || !string.Equals(caller, "audapack", StringComparison.OrdinalIgnoreCase))
        {
            Set(resp, "status", "CALLER_NOT_ELIGIBLE");
            Set(resp, "error", "Launch authorization is restricted to canonical audapack caller");
            Print(resp); return 1;
        }

        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        string accountId = S(acc, "account_id");
        string targetSid = AccountSid(acc);
        if (string.IsNullOrEmpty(targetSid))
        {
            Set(resp, "status", "MISSING_TARGET_SID");
            Set(resp, "error", "Account has no Windows SID assigned");
            Print(resp); return 1;
        }

        string fullWorkdir;
        try { fullWorkdir = Path.GetFullPath(workdir); }
        catch (Exception ex)
        {
            Set(resp, "status", "WORKDIR_INVALID_PATH");
            Set(resp, "error", ex.Message);
            Print(resp); return 1;
        }

        if (!Directory.Exists(fullWorkdir))
        {
            Set(resp, "status", "WORKDIR_NOT_FOUND");
            Set(resp, "error", "Working directory does not exist: " + fullWorkdir);
            Print(resp); return 1;
        }

        var tm = LoadTrustedMode();
        var rec = GetAccountTrustedRecord(tm, accountId);
        string pref = S(rec, "preference") ?? "OFF";
        string st = S(rec, "status") ?? "OFF";
        long gen = 1;
        if (rec.ContainsKey("generation") && rec["generation"] != null)
            long.TryParse(rec["generation"].ToString(), out gen);

        bool isTrusted = pref == "ON" && (st == "ON_VERIFIED" || st == "ON" || st == "ENABLING");
        var approvedList = new List<string>();
        if (isTrusted)
        {
            var rootsObj = Get(rec, "approved_roots") as object[];
            if (rootsObj != null)
            {
                foreach (object o in rootsObj) if (o is string) approvedList.Add((string)o);
            }
        }

        string token = "launch_tok_" + Guid.NewGuid().ToString("N");
        string sessionId = "session_" + Guid.NewGuid().ToString("N");
        DateTime now = DateTime.UtcNow;
        DateTime expires = now.AddSeconds(60);

        lock (_authLock)
        {
            var auths = LoadAuthorizations();
            var tokensDict = Get(auths, "tokens") as Dictionary<string, object>;
            if (tokensDict == null)
            {
                tokensDict = Obj();
                Set(auths, "tokens", tokensDict);
            }

            var tokRecord = Obj();
            Set(tokRecord, "token", token);
            Set(tokRecord, "session_id", sessionId);
            Set(tokRecord, "account_id", accountId);
            Set(tokRecord, "target_sid", targetSid);
            Set(tokRecord, "workdir", fullWorkdir);
            Set(tokRecord, "caller", "audapack");
            Set(tokRecord, "trusted", isTrusted);
            var rootsArr = new object[approvedList.Count];
            for (int k = 0; k < approvedList.Count; k++) rootsArr[k] = approvedList[k];
            Set(tokRecord, "trusted_roots", rootsArr);
            Set(tokRecord, "policy_generation", gen);
            Set(tokRecord, "created_at", now.ToString("o"));
            Set(tokRecord, "expires_at", expires.ToString("o"));
            Set(tokRecord, "used", false);

            Set(tokensDict, token, tokRecord);
            SaveAuthorizations(auths);
        }

        Set(resp, "status", "authorized");
        Set(resp, "token", token);
        Set(resp, "session_id", sessionId);
        Set(resp, "account_id", accountId);
        Set(resp, "target_sid", targetSid);
        Set(resp, "workdir", fullWorkdir);
        Set(resp, "caller", "audapack");
        Set(resp, "trusted", isTrusted);
        var respRoots = new object[approvedList.Count];
        for (int k = 0; k < approvedList.Count; k++) respRoots[k] = approvedList[k];
        Set(resp, "trusted_roots", respRoots);
        Set(resp, "policy_generation", gen);
        Set(resp, "expires_in_seconds", 60);

        Print(resp);
        return 0;
    }

    static int CmdStopSession(string selector)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var accounts = AccountsOf(reg);
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);

        string accountId = S(acc, "account_id");
        string targetSid = AccountSid(acc);
        string compact = CompactLabel(accounts, acc);

        int stopped = StopSessionsForAccount(accountId, targetSid, compact);

        var resp = Obj();
        Set(resp, "schema", "sai.accounts/stop-session/1");
        Set(resp, "account_id", accountId);
        Set(resp, "selector", selector);
        Set(resp, "target_sid", targetSid);
        Set(resp, "status", "stopped");
        Set(resp, "terminated_count", stopped);
        Print(resp);
        return 0;
    }

    // -------------------------------------------------------------- probing
    static Dictionary<string, object> ProbeAccount(Dictionary<string, object> acc, List<Dictionary<string, object>> accounts, int timeoutMs)
    {
        var entry = DescribeAccount(accounts, acc);
        string provider = S(acc, "provider_id");
        string backend = S(acc, "execution_backend");
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        if (provider != "antigravity" || !IsWindowsUserBackend(backend))
        {
            Set(entry, "quota_state", "UNKNOWN");
            Set(entry, "skipped_reason", "provider_does_not_support_quota");
            return entry;
        }
        string targetSid = AccountSid(acc);
        string currentSid = WindowsIdentity.GetCurrent().User.Value;
        if (string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase))
        {
            var capture = RunCapture(SharedAgyPath(), "-p \"/usage\" --output-format json --log-file NUL", timeoutMs,
                AgyCaptureEnvironment());
            string stdout = S(capture, "stdout");
            var usage = SafeUsage(stdout == null ? "" : stdout);
            var cred = CheckCredential();
            Set(entry, "context_state", "ONLINE");
            Set(entry, "auth_state", object.Equals(Get(cred, "credential_present"), true)
                ? (string.Equals(S(usage, "status"), "authenticated_usage") ? "AUTHENTICATED" : "AUTH_REQUIRED")
                : "AUTH_REQUIRED");
            ApplyUsage(entry, usage);
            Set(entry, "credential", cred);
            return entry;
        }
        string pipe = PipeNameFor(ScopeId(), Fingerprint(targetSid));
        var reply = SendPipeCommand(pipe, new Dictionary<string, object> { { "cmd", "usage" } }, timeoutMs);
        // A probe only reads. Starting the context here made `context stop` a
        // no-op the moment anything asked again: the stop worked, the very next
        // status probe saw no broker, started one, and reported the context the
        // operator had just shut down as ONLINE. Starting is `context start`'s
        // job, and it is reachable by name.
        if (reply == null)
        {
            Set(entry, "context_state", "OFFLINE");
            Set(entry, "auth_state", "UNKNOWN");
            Set(entry, "quota_state", "UNKNOWN");
            Set(entry, "skipped_reason", "CONTEXT_OFFLINE");
            return entry;
        }
        Set(entry, "context_state", "ONLINE");
        var usageObj = Obj();
        Set(usageObj, "status", S(reply, "status"));
        if (Get(reply, "windows") != null) Set(usageObj, "windows", Get(reply, "windows"));
        Set(entry, "auth_state", string.Equals(S(usageObj, "status"), "authenticated_usage") ? "AUTHENTICATED" : "AUTH_REQUIRED");
        ApplyUsage(entry, usageObj);
        var credReply = Get(reply, "credential") as Dictionary<string, object>;
        if (credReply != null) Set(entry, "credential", credReply);
        return entry;
    }

    static void ApplyUsage(Dictionary<string, object> entry, Dictionary<string, object> usage)
    {
        string status = S(usage, "status");
        object[] windows = Get(usage, "windows") as object[];
        Set(entry, "usage_status", status);
        if (windows != null) Set(entry, "windows", windows);
        if (!string.Equals(status, "authenticated_usage")) { Set(entry, "quota_state", string.Equals(status, "probe_timeout") ? "ERROR" : "UNKNOWN"); return; }
        bool anyZero = false, allZeroOrDisabled = true;
        foreach (object w in windows ?? new object[0])
        {
            object raw = Get(w, "remaining_fraction");
            bool disabled = object.Equals(Get(w, "disabled"), true);
            double fraction = raw is double || raw is int || raw is long || raw is decimal ? Convert.ToDouble(raw, CultureInfo.InvariantCulture) : -1;
            if (!disabled && fraction <= 0) anyZero = true;
            if (!disabled && fraction > 0) allZeroOrDisabled = false;
        }
        Set(entry, "quota_state", allZeroOrDisabled ? "EXHAUSTED" : (anyZero ? "LOW" : "READY"));
    }

    static Dictionary<string, object> DescribeAccount(List<Dictionary<string, object>> accounts, Dictionary<string, object> acc)
    {
        var o = Obj();
        Set(o, "account_id", S(acc, "account_id"));
        Set(o, "provider_id", S(acc, "provider_id"));
        Set(o, "display_name", DisplayName(accounts, acc));
        Set(o, "compact_label", CompactLabel(accounts, acc));
        Set(o, "execution_backend", S(acc, "execution_backend"));
        Set(o, "execution_context_id", S(acc, "execution_context_id"));
        Set(o, "operational_state", S(acc, "operational_state") ?? "ENABLED");
        Set(o, "hidden", object.Equals(Get(acc, "hidden"), true));
        Set(o, "auto_connect", AutoConnectOf(acc));
        Set(o, "auto_connect_reason", AutoConnectReasonOf(acc));
        Set(o, "auto_connect_changed_at", S(acc, "auto_connect_changed_at"));
        Set(o, "created_at", S(acc, "created_at"));
        Set(o, "updated_at", S(acc, "updated_at"));
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        if (meta != null)
        {
            var safe = Obj();
            foreach (var kv in meta)
                if (kv.Key == "windows_user" || kv.Key == "profile_locator" || kv.Key == "managed" || kv.Key == "env_scrub" || kv.Key == "legacy_id")
                    Set(safe, kv.Key, kv.Value);
            Set(o, "context_label", S(meta, "windows_user"));
            Set(o, "provider_metadata", safe);
        }
        // The managed Windows SID is resolved once here rather than by every
        // consumer translating an account name on its own: a consumer that
        // guesses the identity is one typo away from naming the wrong account
        // in an ACL.
        string sid = AccountSid(acc);
        if (!string.IsNullOrEmpty(sid)) Set(o, "windows_sid", sid);
        var ci = Get(acc, "configured_identity") as Dictionary<string, object>;
        if (ci != null && !string.IsNullOrEmpty(S(ci, "fingerprint")))
        {
            var safeCi = Obj();
            Set(safeCi, "fingerprint", S(ci, "fingerprint"));
            if (!string.IsNullOrEmpty(S(ci, "account_label")))
                Set(safeCi, "account_label", S(ci, "account_label"));
            if (!string.IsNullOrEmpty(S(ci, "bound_at")))
                Set(safeCi, "bound_at", S(ci, "bound_at"));
            Set(o, "configured_identity", safeCi);
        }
        return o;
    }

    // ------------------------------------------------------------- commands
    static Dictionary<string, object> RequireRegistry()
    {
        var reg = LoadRegistry();
        if (reg == null)
        {
            Fail("REGISTRY_NOT_FOUND", RegistryPath() + " — run 'sai-accounts import' or add an account from SAITULS first");
            return null;
        }
        return reg;
    }

    static int CmdList(string[] args)
    {
        string provider = null; bool all = false;
        for (int i = 1; i < args.Length; i++)
        {
            if (args[i] == "--provider" && i + 1 < args.Length) { provider = args[i + 1]; i++; }
            else if (args[i] == "--all") all = true;
        }
        var reg = LoadRegistry();
        var o = Obj();
        Set(o, "schema", "sai.accounts/list/1");
        Set(o, "registry_path", RegistryPath());
        if (reg == null)
        {
            Set(o, "generation", 0); Set(o, "accounts", new object[0]);
            Set(o, "status", "empty");
            Print(o); return 0;
        }
        var accounts = AccountsOf(reg);
        var listed = new List<object>();
        foreach (var acc in accounts)
        {
            if (!all && string.Equals(S(acc, "operational_state"), "ARCHIVED", StringComparison.OrdinalIgnoreCase)) continue;
            if (provider != null && !string.Equals(S(acc, "provider_id"), provider, StringComparison.OrdinalIgnoreCase)) continue;
            listed.Add(DescribeAccount(accounts, acc));
        }
        Set(o, "generation", Get(reg, "generation"));
        Set(o, "accounts", listed.ToArray());
        Set(o, "status", "ok");
        Print(o); return 0;
    }

    static int CmdGet(string selector)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var accounts = AccountsOf(reg);
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);
        var o = Obj();
        Set(o, "schema", "sai.accounts/get/1");
        Set(o, "generation", Get(reg, "generation"));
        Set(o, "account", DescribeAccount(accounts, acc));
        Print(o); return 0;
    }

    static int CmdSnapshot(string[] args)
    {
        string provider = null; int timeoutMs = 20000;
        for (int i = 1; i < args.Length; i++)
        {
            if (args[i] == "--provider" && i + 1 < args.Length) { provider = args[i + 1]; i++; }
            else if (args[i] == "--timeout" && i + 1 < args.Length)
            {
                int t; if (int.TryParse(args[i + 1], NumberStyles.None, CultureInfo.InvariantCulture, out t)) timeoutMs = Math.Max(2000, Math.Min(60000, t));
                i++;
            }
        }
        var reg = RequireRegistry(); if (reg == null) return 1;
        var accounts = AccountsOf(reg);
        var eligible = new List<Dictionary<string, object>>();
        foreach (var acc in accounts)
        {
            if (provider != null && !string.Equals(S(acc, "provider_id"), provider, StringComparison.OrdinalIgnoreCase)) continue;
            eligible.Add(acc);
        }
        // Bounded concurrency in batches of four: a dead secondary broker must
        // not stall siblings, and the whole snapshot stays O(timeout) per batch.
        var results = new Dictionary<string, object>[eligible.Count];
        for (int start = 0; start < eligible.Count; start += 4)
        {
            int count = Math.Min(4, eligible.Count - start);
            var threads = new Thread[count];
            for (int j = 0; j < count; j++)
            {
                int index = start + j;
                threads[j] = new Thread(delegate()
                {
                    try { results[index] = SnapshotEntry(eligible[index], accounts, timeoutMs); }
                    catch (Exception ex)
                    {
                        var entry = DescribeAccount(accounts, eligible[index]);
                        Set(entry, "quota_state", "ERROR");
                        Set(entry, "error", ex.Message);
                        results[index] = entry;
                    }
                });
                threads[j].IsBackground = true;
                threads[j].Start();
            }
            for (int j = 0; j < count; j++) threads[j].Join(timeoutMs + 2000);
        }

        var listed = new List<object>();
        for (int i = 0; i < results.Length; i++)
            listed.Add(results[i] != null ? results[i] : SkippedSnapshotEntry(accounts, eligible[i], "probe_incomplete"));
        var o = Obj();
        Set(o, "schema", "sai.accounts/snapshot/1");
        Set(o, "generation", Get(reg, "generation"));
        Set(o, "accounts", listed.ToArray());
        Set(o, "checked_at", NowIso());
        Print(o); return 0;
    }

    static Dictionary<string, object> SnapshotEntry(Dictionary<string, object> acc, List<Dictionary<string, object>> accounts, int timeoutMs)
    {
        var entry = DescribeAccount(accounts, acc);
        string state = S(acc, "operational_state") ?? "ENABLED";
        if (state == "FROZEN" || state == "ARCHIVED")
        {
            Set(entry, "quota_state", "UNKNOWN");
            Set(entry, "skipped_reason", "lifecycle_" + state.ToLowerInvariant());
            return entry;
        }
        if (state == "DISABLED")
        {
            Set(entry, "quota_state", "UNKNOWN");
            Set(entry, "skipped_reason", "lifecycle_disabled");
            return entry;
        }
        if (object.Equals(Get(acc, "hidden"), true)) { /* hidden is presentation only: still probed */ }
        return ProbeAccount(acc, accounts, timeoutMs);
    }

    static Dictionary<string, object> SkippedSnapshotEntry(List<Dictionary<string, object>> accounts, Dictionary<string, object> acc, string reason)
    {
        var entry = DescribeAccount(accounts, acc);
        Set(entry, "quota_state", "UNKNOWN");
        Set(entry, "skipped_reason", reason);
        return entry;
    }

    static int CmdStatus(string selector)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);
        Print(StatusEntry(AccountsOf(reg), acc));
        return 0;
    }

    // `status --all`: the same cheap per-account status (credential presence and
    // a 3s broker ping -- never a /usage probe), for every account in ONE
    // process. A UI that polls account state pays one child per refresh
    // instead of one per account. Pings run concurrently so a dead broker
    // costs its own timeout, not everyone's.
    static int CmdStatusAll(string[] args = null)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var accounts = AccountsOf(reg);
        var results = new Dictionary<string, object>[accounts.Count];
        int timeoutMs = 10000;
        string envTimeout = Environment.GetEnvironmentVariable("SAI_STATUS_ALL_TIMEOUT_MS");
        int parsedTimeout;
        if (!string.IsNullOrEmpty(envTimeout) && int.TryParse(envTimeout, out parsedTimeout) && parsedTimeout > 0)
            timeoutMs = parsedTimeout;
        if (args != null)
        {
            for (int i = 2; i < args.Length; i++)
            {
                if (args[i] == "--timeout" && i + 1 < args.Length && int.TryParse(args[i + 1], out parsedTimeout) && parsedTimeout > 0)
                {
                    timeoutMs = parsedTimeout;
                    break;
                }
            }
        }
        if (accounts.Count > 0)
        {
            using (var done = new CountdownEvent(accounts.Count))
            {
                for (int i = 0; i < accounts.Count; i++)
                {
                    int index = i;
                    var t = new Thread(delegate()
                    {
                        try { results[index] = StatusEntry(accounts, accounts[index]); }
                        catch (Exception ex)
                        {
                            var e = DescribeAccount(accounts, accounts[index]);
                            Set(e, "schema", "sai.accounts/status/1");
                            Set(e, "context_state", "ERROR");
                            Set(e, "auth_state", "UNKNOWN");
                            Set(e, "error", ex.GetType().Name);
                            results[index] = e;
                        }
                        finally
                        {
                            try { done.Signal(); } catch { }
                        }
                    });
                    t.IsBackground = true;
                    t.Start();
                }
                done.Wait(timeoutMs);
            }
        }
        var listed = new List<object>();
        for (int i = 0; i < results.Length; i++)
        {
            if (results[i] != null) { listed.Add(results[i]); continue; }
            var e = DescribeAccount(accounts, accounts[i]);
            Set(e, "schema", "sai.accounts/status/1");
            Set(e, "context_state", "ERROR");
            Set(e, "auth_state", "UNKNOWN");
            Set(e, "error", "status_timeout");
            listed.Add(e);
        }
        var o = Obj();
        Set(o, "schema", "sai.accounts/status-all/1");
        Set(o, "generation", Get(reg, "generation"));
        Set(o, "accounts", listed.ToArray());
        Set(o, "checked_at", NowIso());
        Print(o);
        return 0;
    }

    static Dictionary<string, object> StatusEntry(List<Dictionary<string, object>> accounts, Dictionary<string, object> acc)
    {
        var o = DescribeAccount(accounts, acc);
        Set(o, "schema", "sai.accounts/status/1");
        string backend = S(acc, "execution_backend");
        var meta = Get(acc, "provider_metadata") as Dictionary<string, object>;
        if (IsWindowsUserBackend(backend))
        {
            string targetSid = AccountSid(acc);
            string currentSid = WindowsIdentity.GetCurrent().User.Value;
            if (string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase))
            {
                Set(o, "context_state", "ONLINE");
                // The local account answers from this process instead of over the
                // pipe, but it must report the same fields as the managed one.
                // Omitting auth_state here made the operator's own row render as
                // "no auth state" while AG2 showed AUTHENTICATED, from the same
                // credential object.
                var local = CheckCredential();
                Set(o, "credential", local);
                Set(o, "auth_state", object.Equals(Get(local, "credential_present"), true) ? "AUTHENTICATED" : "AUTH_REQUIRED");
            }
            else
            {
                var reply = SendPipeCommand(PipeNameFor(ScopeId(), Fingerprint(targetSid)), new Dictionary<string, object> { { "cmd", "status" } }, 3000);
                if (reply == null) { Set(o, "context_state", "OFFLINE"); Set(o, "auth_state", "UNKNOWN"); }
                else
                {
                    Set(o, "context_state", "ONLINE");
                    var cred = Get(reply, "credential") as Dictionary<string, object>;
                    if (cred != null) { Set(o, "credential", cred); Set(o, "auth_state", object.Equals(Get(cred, "credential_present"), true) ? "AUTHENTICATED" : "AUTH_REQUIRED"); }
                    // A reply carrying no credential object left auth_state
                    // unset entirely, so the envelope said nothing at all about
                    // this account and its row rendered as "no auth state" while
                    // a local account beside it read AUTHENTICATED. Silence is
                    // not an answer -- say UNKNOWN.
                    else Set(o, "auth_state", "UNKNOWN");
                }
            }
        }
        else Set(o, "context_state", "ONLINE");
        return o;
    }

    static int CmdUsage(string selector)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);
        var accounts = AccountsOf(reg);
        string state = S(acc, "operational_state") ?? "ENABLED";
        if (state == "FROZEN" || state == "ARCHIVED") return Fail("LIFECYCLE_FORBIDDEN", "usage probe refused for " + state);
        var entry = ProbeAccount(acc, accounts, 30000);
        Set(entry, "schema", "sai.accounts/usage/1");
        Print(entry);
        return string.Equals(S(entry, "context_state"), "OFFLINE") ? 2 : (string.Equals(S(entry, "usage_status"), "authenticated_usage") ? 0 : 1);
    }

    // Ask the helper to undo a lifecycle operation this engine can no longer
    // register: the Windows user exists, the vault holds its secret, and the
    // registry does not know the account. Returns the helper's answer verbatim
    // (status, failed_steps, error) so the caller reports what actually
    // happened instead of assuming the cleanup worked.
    static Dictionary<string, object> CompensateContextCreate(string opId, string reason)
    {
        if (string.IsNullOrEmpty(opId)) return null;
        try
        {
            var hReq = Obj();
            Set(hReq, "op", "managed_context_create");
            Set(hReq, "operation_id", opId);
            Set(hReq, "compensate_registry_failure", true);
            Set(hReq, "reason", reason);
            var hResp = CallHelper(hReq, 15000);
            if (hResp == null) return Obj(); // helper unreachable: durable record stays recoverable
            return hResp;
        }
        catch (Exception ex)
        {
            var fail = Obj();
            Set(fail, "status", "COMPENSATION_CALL_FAILED");
            Set(fail, "error", ex.Message);
            return fail;
        }
    }

    static int CmdContext(string[] args)
    {
        if (args.Length < 2) { Console.WriteLine("usage: context connect|disconnect|start|stop|create|rotate|migrate|delete|status [selector]"); return 1; }
        string verb = args[1].ToLowerInvariant();

        if (verb == "create")
        {
            string provider = "antigravity";
            string alias = null;
            string customUser = null;
            for (int i = 2; i < args.Length; i++)
            {
                if (args[i] == "--provider" && i + 1 < args.Length) { provider = args[i + 1]; i++; }
                else if (args[i] == "--alias" && i + 1 < args.Length) { alias = args[i + 1]; i++; }
                else if (args[i] == "--username" && i + 1 < args.Length) { customUser = args[i + 1]; i++; }
            }
            if (!string.Equals(provider, "antigravity", StringComparison.OrdinalIgnoreCase))
            {
                return Fail("unsupported_provider", "context create currently supports provider 'antigravity'");
            }
            var hReq = Obj();
            Set(hReq, "op", "managed_context_create");
            Set(hReq, "provider", provider);
            if (!string.IsNullOrEmpty(customUser)) Set(hReq, "username", customUser);
            // One lifecycle operation, one id. The same id is what a retry
            // resumes on and, if the registry write below fails, what the
            // compensating cleanup is addressed to.
            string opId = Guid.NewGuid().ToString("N");
            Set(hReq, "operation_id", opId);
            var hResp = CallHelper(hReq, 15000);
            if (hResp == null || !string.Equals(S(hResp, "status"), "ok"))
            {
                return Fail("helper_context_create_failed", hResp != null ? S(hResp, "error") ?? S(hResp, "status") : "helper_unreachable");
            }
            string sid = S(hResp, "target_sid");
            string winUser = S(hResp, "windows_user");
            string accId = S(hResp, "context_id");

            var newAcc = Obj();
            Set(newAcc, "account_id", accId);
            Set(newAcc, "provider_id", provider);
            Set(newAcc, "display_name", alias);
            Set(newAcc, "execution_backend", "managed_windows_user");
            Set(newAcc, "execution_context_id", "windows-user:" + Fingerprint(sid));
            Set(newAcc, "operational_state", "ENABLED");
            Set(newAcc, "hidden", false);
            // A managed account nobody has to visit to switch on. Written
            // explicitly rather than left to the read-time default so the
            // registry states the intent rather than implying it.
            Set(newAcc, "auto_connect", true);
            Set(newAcc, "auto_connect_reason", "default");
            Set(newAcc, "auto_connect_changed_at", NowIso());
            Set(newAcc, "created_at", NowIso());
            Set(newAcc, "updated_at", NowIso());
            var meta = Obj();
            Set(meta, "windows_user", winUser);
            Set(meta, "windows_sid", sid);
            Set(meta, "managed", true);
            Set(newAcc, "provider_metadata", meta);
            Set(newAcc, "tags", new string[0]);

            var o = Obj();
            // The context exists on the machine now, registered only by the
            // helper. A registry write that dies here must not leave that
            // orphan behind: run the compensating cleanup against the same
            // operation id and say so, plainly, if the cleanup itself fails.
            try
            {
                Mutate("context_created", accId, delegate(List<Dictionary<string, object>> accts, Dictionary<string, object> r)
                {
                    accts.Add(newAcc);
                    return "created";
                });
            }
            catch (Exception ex)
            {
                var comp = CompensateContextCreate(opId, "context create registry write failed: " + ex.Message);
                Set(o, "status", "REGISTRY_FAILED");
                Set(o, "registry_failure", ex.Message);
                Set(o, "compensation", comp);
                Print(o);
                return 1;
            }

            Set(o, "schema", "sai.accounts/context/1");
            Set(o, "status", "created");
            Set(o, "account_id", accId);
            Set(o, "windows_user", winUser);
            Set(o, "windows_sid", sid);
            Set(o, "backend", "managed_windows_user");
            Print(o);
            return 0;
        }

        if (args.Length < 3) { Console.WriteLine("usage: context " + verb + " <selector>"); return 1; }
        var reg2 = RequireRegistry(); if (reg2 == null) return 1;
        var acc = FindAccount(reg2, args[2]);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", args[2]);

        // Connect and Disconnect express intent; start and stop change what is
        // running. Keeping them apart is the whole point: `launch` reaches for
        // start whenever the operator explicitly launches an offline account,
        // and if that wrote the intent then one right-click would silently undo
        // an explicit Disconnect. The buttons speak intent, the launch path
        // speaks runtime, and neither can reach the other's field.
        if (verb == "connect" || verb == "disconnect")
        {
            if (SetAutoConnect(acc, verb == "connect") != 0) return Fail("AUTO_CONNECT_PERSIST_FAILED", args[2]);
            verb = verb == "connect" ? "start" : "stop";
        }

        string backend = S(acc, "execution_backend");
        var meta2 = Get(acc, "provider_metadata") as Dictionary<string, object>;
        var o2 = Obj(); Set(o2, "schema", "sai.accounts/context/1");
        Set(o2, "account_id", S(acc, "account_id"));
        if (!IsWindowsUserBackend(backend)) { Set(o2, "status", "NO_CONTEXT_REQUIRED"); Print(o2); return 0; }
        string targetSid = AccountSid(acc); string user = S(meta2, "windows_user");
        string currentSid = WindowsIdentity.GetCurrent().User.Value;
        if (string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase))
        {
            Set(o2, "status", "already_current_user"); Print(o2); return 0;
        }
        string pipe = PipeNameFor(ScopeId(), Fingerprint(targetSid));

        if (verb == "start")
        {
            bool isManaged = backend == "managed_windows_user" || object.Equals(Get(meta2, "managed"), true);
            if (isManaged)
            {
                var hReq = Obj();
                Set(hReq, "op", "managed_context_start");
                Set(hReq, "target_sid", targetSid);
                Set(hReq, "scope_id", ScopeId());
                Set(hReq, "operator_sid", currentSid);
                var hResp = CallHelper(hReq, 10000);
                if (hResp != null && string.Equals(S(hResp, "status"), "started"))
                {
                    // The helper answers for a spawn, not for a running broker. Wait,
                    // bounded, for the pipe to show up: a launch that never listens
                    // is a failure here, not a button the operator watches for minutes.
                    // Ask the pipe, do not look at the name: a broker that is on its
                    // way out still has its name registered, so File.Exists reported
                    // "online" for a context that died a moment later.
                    Dictionary<string, object> probe = null;
                    int grace = 5000;
                    while (grace > 0)
                    {
                        probe = SendPipeCommand(pipe, new Dictionary<string, object> { { "cmd", "status" } }, 1000);
                        if (probe != null) break;
                        Thread.Sleep(200);
                        grace -= 200;
                    }
                    Set(o2, "execution", "helper");
                    Set(o2, "windows_user", user);
                    Set(o2, "pipe", pipe);
                    Set(o2, "pid", Get(hResp, "pid"));
                    if (probe != null)
                    {
                        Set(o2, "status", "online");
                        Print(o2); return 0;
                    }
                    Set(o2, "status", "start_context_failed");
                    Set(o2, "error", "broker_not_listening_within_5000ms");
                    Print(o2); return 1;
                }
                string hErr = hResp != null ? S(hResp, "error") ?? S(hResp, "status") : "helper_unreachable";
                Set(o2, "status", "start_context_failed");
                Set(o2, "error", hErr);
                Print(o2); return 1;
            }

            string exe = SelfExePath();
            // Short, bounded runas command — never a nested PowerShell payload.
            string runasCmd = "\"" + exe + "\" --broker " + targetSid + " " + ScopeId() + " " + currentSid;
            try
            {
                var psi = new ProcessStartInfo("runas.exe", "/profile /user:" + user + " \"" + runasCmd + "\"")
                {
                    UseShellExecute = true
                };
                Process.Start(psi);
                Set(o2, "status", "launch_requested");
                Set(o2, "execution", "runas");
                Set(o2, "windows_user", user);
                Set(o2, "pipe", pipe);
                Print(o2); return 0;
            }
            catch (Exception ex) { Set(o2, "status", "start_context_failed"); Set(o2, "error", ex.Message); Print(o2); return 1; }
        }

        if (verb == "stop")
        {
            var reply = SendPipeCommand(pipe, new Dictionary<string, object> { { "cmd", "shutdown" } }, 4000);
            Set(o2, "status", reply != null ? S(reply, "status") ?? "stopped" : "not_running");
            Print(o2); return 0;
        }


        if (verb == "rotate")
        {
            var hReq = Obj();
            Set(hReq, "op", "managed_context_rotate_secret");
            Set(hReq, "target_sid", targetSid);
            string rotateOpId = Guid.NewGuid().ToString("N");
            Set(hReq, "operation_id", rotateOpId);
            var hResp = CallHelper(hReq, 15000);
            if (hResp == null || !string.Equals(S(hResp, "status"), "rotated"))
            {
                return Fail("rotate_failed", hResp != null ? S(hResp, "error") ?? S(hResp, "status") : "helper_unreachable");
            }
            Set(o2, "status", "rotated");
            Set(o2, "target_sid", targetSid);
            Set(o2, "operation_id", rotateOpId);
            Print(o2); return 0;
        }

        if (verb == "migrate")
        {
            var hReq = Obj();
            Set(hReq, "op", "managed_context_create");
            Set(hReq, "provider", S(acc, "provider_id"));
            Set(hReq, "migrate_existing", true);
            Set(hReq, "username", user);
            // Migration changes a live profile's password. One operation id
            // means the password change, the staged secret and the registry
            // update are one transaction with a resumable identity.
            string opId = Guid.NewGuid().ToString("N");
            Set(hReq, "operation_id", opId);
            var hResp = CallHelper(hReq, 15000);
            if (hResp == null || !string.Equals(S(hResp, "status"), "ok"))
            {
                return Fail("migrate_failed", hResp != null ? S(hResp, "error") ?? S(hResp, "status") : "helper_unreachable");
            }
            string aid = S(acc, "account_id");
            try
            {
                Mutate("context_migrated", aid, delegate(List<Dictionary<string, object>> accts, Dictionary<string, object> r)
                {
                    foreach (var a in accts)
                    {
                        if (string.Equals(S(a, "account_id"), aid, StringComparison.OrdinalIgnoreCase))
                        {
                            Set(a, "execution_backend", "managed_windows_user");
                            var m = Get(a, "provider_metadata") as Dictionary<string, object>;
                            if (m != null) Set(m, "managed", true);
                            Set(a, "updated_at", NowIso());
                        }
                    }
                    return "migrated";
                });
            }
            catch (Exception ex)
            {
                // The profile is already on the new managed password; the
                // account simply was not marked managed. Do not delete the user
                // -- that would strand the profile -- mark the fact and let the
                // operator see it.
                Set(o2, "status", "REGISTRY_FAILED");
                Set(o2, "registry_failure", ex.Message);
                Set(o2, "windows_user", user);
                Set(o2, "hint", "profile password is rotated and the secret is sealed; re-run migrate to complete the registry update");
                Print(o2);
                return 1;
            }
            Set(o2, "status", "migrated");
            Set(o2, "windows_user", user);
            Print(o2); return 0;
        }

        if (verb == "delete")
        {
            bool force = false;
            for (int i = 3; i < args.Length; i++) if (args[i] == "--force") force = true;
            string aid = S(acc, "account_id");

            if (!string.IsNullOrEmpty(targetSid))
            {
                var probe = SendPipeCommand(pipe, new Dictionary<string, object> { { "cmd", "status" } }, 1000);
                if (probe != null)
                {
                    SendPipeCommand(pipe, new Dictionary<string, object> { { "cmd", "shutdown" } }, 3000);
                    int grace = 3000;
                    bool stopped = false;
                    while (grace > 0)
                    {
                        var checkProbe = SendPipeCommand(pipe, new Dictionary<string, object> { { "cmd", "status" } }, 500);
                        if (checkProbe == null) { stopped = true; break; }
                        Thread.Sleep(200);
                        grace -= 200;
                    }
                    if (!stopped)
                    {
                        return Fail("broker_shutdown_refused", "Broker for " + aid + " refused shutdown");
                    }
                }
            }

            Mutate("context_cleanup_pending", aid, delegate(List<Dictionary<string, object>> accts, Dictionary<string, object> r)
            {
                foreach (var a in accts)
                {
                    if (string.Equals(S(a, "account_id"), aid, StringComparison.OrdinalIgnoreCase))
                    {
                        Set(a, "cleanup_state", "cleanup_pending");
                        Set(a, "updated_at", NowIso());
                    }
                }
                return "cleanup_pending";
            });

            Dictionary<string, object> hResp = null;
            if (!string.IsNullOrEmpty(targetSid))
            {
                var hReq = Obj();
                Set(hReq, "op", "managed_context_remove");
                Set(hReq, "target_sid", targetSid);
                Set(hReq, "delete_windows_account", force);
                hResp = CallHelper(hReq, 15000);
            }

            bool helperOk = false;
            string hStatus = hResp != null ? S(hResp, "status") : "helper_unreachable";
            if (string.IsNullOrEmpty(targetSid) || (hResp != null && string.Equals(hStatus, "removed", StringComparison.OrdinalIgnoreCase)))
            {
                helperOk = true;
            }

            if (!helperOk)
            {
                Mutate("context_cleanup_required", aid, delegate(List<Dictionary<string, object>> accts, Dictionary<string, object> r)
                {
                    foreach (var a in accts)
                    {
                        if (string.Equals(S(a, "account_id"), aid, StringComparison.OrdinalIgnoreCase))
                        {
                            Set(a, "operational_state", "DISABLED");
                            Set(a, "cleanup_state", "cleanup_required");
                            Set(a, "cleanup_error", hStatus);
                            if (hResp != null && Get(hResp, "failed_operations") != null)
                            {
                                Set(a, "cleanup_failed_ops", Get(hResp, "failed_operations"));
                            }
                            Set(a, "updated_at", NowIso());
                        }
                    }
                    return "cleanup_required";
                });
                return Fail("cleanup_failed", "Helper cleanup failed with status: " + hStatus);
            }

            Mutate("context_deleted", aid, delegate(List<Dictionary<string, object>> accts, Dictionary<string, object> r)
            {
                accts.RemoveAll(a => string.Equals(S(a, "account_id"), aid, StringComparison.OrdinalIgnoreCase));
                return "deleted";
            });
            Set(o2, "status", "deleted");
            Print(o2); return 0;
        }

        if (verb == "status")
        {
            var hReq = Obj();
            Set(hReq, "op", "managed_context_status");
            Set(hReq, "target_sid", targetSid);
            Set(hReq, "scope_id", ScopeId());
            var hResp = CallHelper(hReq, 5000);
            if (hResp != null)
            {
                foreach (var kv in hResp) Set(o2, kv.Key, kv.Value);
            }
            else
            {
                Set(o2, "status", "helper_offline");
            }
            Print(o2); return 0;
        }

        return Fail("unknown_context_verb", verb);
    }

    // Startup reconciliation, and the periodic re-run of it.
    //
    // This is the loop the operator used to run by hand: for every managed
    // account that is ENABLED and wants to be connected, bring its broker up
    // if it is not already listening. Three properties matter more than the
    // feature.
    //
    // It is idempotent, because the first thing it does with an account is
    // ask its pipe whether the broker is already there. Running it every
    // minute costs one bounded round trip per account and spawns nothing.
    // It never falls back: an account that cannot be started is reported as
    // itself, offline, under its own label.
    // And it never resurrects an account the operator turned off, because
    // that decision is a persisted field and this reads the field rather
    // than inferring intent from a dead process.
    static int CmdAutoConnect(string[] args)
    {
        int budgetMs = 120000;
        for (int i = 1; i < args.Length; i++)
            if (args[i] == "--budget-ms" && i + 1 < args.Length)
            {
                int parsed;
                if (int.TryParse(args[i + 1], NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed) && parsed > 0) budgetMs = parsed;
                i++;
            }

        var o = Obj();
        Set(o, "schema", "sai.accounts/autoconnect/1");
        // Serialize against a concurrent sweep (GUI timer vs background
        // keeper). Non-blocking: the loser reports deduplicated and starts
        // nothing, so two simultaneous sweeps can never issue two helper
        // start requests for one account.
        bool owned = false; Mutex sweep = null;
        try { sweep = new Mutex(true, AutoconnectMutexName(), out owned); } catch { sweep = null; }
        if (sweep != null && !owned) { try { owned = sweep.WaitOne(0); } catch { owned = true; } }
        if (sweep != null && !owned)
        {
            Set(o, "status", "ok");
            Set(o, "started", 0);
            Set(o, "already_online", 0);
            Set(o, "skipped", 0);
            Set(o, "failed", 0);
            Set(o, "elapsed_ms", 0);
            Set(o, "results", new object[0]);
            Set(o, "deduplicated", true);
            Print(o);
            try { sweep.Close(); } catch { }
            AppendKeeperLog(NowIso() + " deduplicated=true started=0 already=0 skipped=0 failed=0");
            return 0;
        }
        try { return CmdAutoConnectInner(o, budgetMs); }
        finally { if (owned) { try { sweep.ReleaseMutex(); } catch { } } if (sweep != null) sweep.Close(); }
    }

    static int CmdAutoConnectInner(Dictionary<string, object> o, int budgetMs)
    {
        var reg = LoadRegistry();
        var results = new List<object>();
        if (reg == null)
        {
            Set(o, "status", "empty");
            Set(o, "results", new object[0]);
            Print(o);
            AppendKeeperLog(NowIso() + " empty registry");
            return 0;
        }

        var accounts = AccountsOf(reg);
        string currentSid = WindowsIdentity.GetCurrent().User.Value;
        var sw = Stopwatch.StartNew();
        int started = 0, already = 0, skipped = 0, failed = 0;

        foreach (var acc in accounts)
        {
            var row = Obj();
            Set(row, "account_id", S(acc, "account_id"));
            Set(row, "compact_label", CompactLabel(accounts, acc));

            // Order matters, and it is the order of what the operator could
            // have said. Lifecycle first: DISABLED is a thing they set. Then
            // managed-ness: an account with no separate Windows user was never
            // connectable, so reporting it as auto_connect_disabled would
            // accuse them of turning something off that never existed. Only
            // then is silence read as intent, which is the one case where
            // absence really does mean a decision.
            string state = S(acc, "operational_state") ?? "ENABLED";
            if (state != "ENABLED") { Set(row, "outcome", "skipped"); Set(row, "reason", "lifecycle_" + state.ToLowerInvariant()); skipped++; results.Add(row); continue; }
            if (!IsManagedSecondary(acc)) { Set(row, "outcome", "skipped"); Set(row, "reason", "no_managed_context"); skipped++; results.Add(row); continue; }
            if (!AutoConnectOf(acc)) { Set(row, "outcome", "skipped"); Set(row, "reason", "auto_connect_disabled"); skipped++; results.Add(row); continue; }

            string sid = AccountSid(acc);
            string pipe = PipeNameFor(ScopeId(), Fingerprint(sid));
            var statusCmd = new Dictionary<string, object> { { "cmd", "status" } };
            if (SendPipeCommand(pipe, statusCmd, 3000) != null)
            {
                Set(row, "outcome", "already_online"); already++; results.Add(row); continue;
            }
            if (sw.ElapsedMilliseconds > budgetMs)
            {
                Set(row, "outcome", "skipped"); Set(row, "reason", "budget_exhausted"); skipped++; results.Add(row); continue;
            }

            var hReq = Obj();
            Set(hReq, "op", "managed_context_start");
            Set(hReq, "target_sid", sid);
            Set(hReq, "scope_id", ScopeId());
            Set(hReq, "operator_sid", currentSid);
            var hResp = CallHelper(hReq, 10000);
            if (hResp == null || !string.Equals(S(hResp, "status"), "started", StringComparison.Ordinal))
            {
                Set(row, "outcome", "failed");
                Set(row, "reason", hResp == null ? "helper_unreachable" : S(hResp, "error") ?? S(hResp, "status"));
                failed++; results.Add(row); continue;
            }
            // The helper answers for a spawn, not for a listening broker. Ask
            // the pipe, bounded, so a broker that never comes up is a named
            // failure rather than a row that says online and is not.
            Dictionary<string, object> probe = null;
            int grace = 5000;
            while (grace > 0)
            {
                Thread.Sleep(200); grace -= 200;
                probe = SendPipeCommand(pipe, statusCmd, 1000);
                if (probe != null) break;
            }
            if (probe == null) { Set(row, "outcome", "failed"); Set(row, "reason", "broker_not_listening_within_5000ms"); failed++; }
            else { Set(row, "outcome", "started"); started++; }
            results.Add(row);
        }

        Set(o, "status", failed > 0 ? "partial" : "ok");
        Set(o, "started", started);
        Set(o, "already_online", already);
        Set(o, "skipped", skipped);
        Set(o, "failed", failed);
        Set(o, "elapsed_ms", sw.ElapsedMilliseconds);
        Set(o, "results", results.ToArray());
        Print(o);
        try
        {
            var fails = new StringBuilder();
            foreach (object item in results)
            {
                var row = item as Dictionary<string, object>;
                if (row == null || !string.Equals(S(row, "outcome"), "failed", StringComparison.Ordinal)) continue;
                if (fails.Length > 0) fails.Append(';');
                fails.Append(S(row, "account_id") ?? "?").Append(':').Append(S(row, "reason") ?? "failed");
            }
            string line = NowIso() + " started=" + started + " already=" + already
                + " skipped=" + skipped + " failed=" + failed
                + " elapsed_ms=" + sw.ElapsedMilliseconds;
            if (fails.Length > 0) line += " failures=" + fails.ToString();
            AppendKeeperLog(line);
        }
        catch { }
        return failed > 0 ? 2 : 0;
    }

    // -------------------------------------------------------------- import
    static string LegacyRegistryUserPath()
    {
        string env = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_LEGACY_USER");
        if (!string.IsNullOrEmpty(env)) return env;
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"SAI\accounts\antigravity-accounts.json");
    }
    static string LegacyRegistryMachinePath()
    {
        string env = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_LEGACY_MACHINE");
        if (!string.IsNullOrEmpty(env)) return env;
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), @"SAI\Antigravity\accounts.json");
    }

    sealed class ImportPlan
    {
        public List<Dictionary<string, object>> ToAdd = new List<Dictionary<string, object>>();
        public List<string> Already = new List<string>();
        public List<string> Conflicts = new List<string>();
    }

    static Dictionary<string, object> LegacyAccountToDescriptor(Dictionary<string, object> legacy)
    {
        string sid = S(legacy, "windows_sid"); string user = S(legacy, "windows_user");
        string provider = S(legacy, "provider") ?? "antigravity";
        if (sid == null) return null;
        var acc = Obj();
        Set(acc, "account_id", AccountIdFor(provider, "windows_user", sid));
        Set(acc, "provider_id", provider);
        Set(acc, "display_name", null);
        Set(acc, "execution_backend", "windows_user");
        Set(acc, "execution_context_id", ContextIdFor("windows_user", sid));
        Set(acc, "operational_state", string.Equals(S(legacy, "enabled"), "False", StringComparison.OrdinalIgnoreCase) ? "DISABLED" : "ENABLED");
        Set(acc, "hidden", false);
        Set(acc, "created_at", NowIso()); Set(acc, "updated_at", NowIso());
        var meta = Obj();
        Set(meta, "windows_sid", sid);
        if (user != null) Set(meta, "windows_user", user);
        string legacyId = S(legacy, "id");
        if (legacyId != null) Set(meta, "legacy_id", legacyId);
        Set(acc, "provider_metadata", meta);
        Set(acc, "tags", new object[0]);
        return acc;
    }

    static Dictionary<string, object> ProfileAccountDescriptor(string provider, string profileDir, bool scrubKeys, string legacyId)
    {
        string canonical = profileDir.Replace('/', '\\').TrimEnd('\\').ToLowerInvariant();
        var acc = Obj();
        Set(acc, "account_id", AccountIdFor(provider, "profile_directory", canonical));
        Set(acc, "provider_id", provider);
        Set(acc, "display_name", null);
        Set(acc, "execution_backend", "profile_directory");
        Set(acc, "execution_context_id", ContextIdFor("profile_directory", null));
        Set(acc, "operational_state", "ENABLED");
        Set(acc, "hidden", false);
        Set(acc, "created_at", NowIso()); Set(acc, "updated_at", NowIso());
        var meta = Obj();
        Set(meta, "profile_locator", profileDir);
        if (scrubKeys)
            Set(meta, "env_scrub", new object[] { "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN" });
        if (legacyId != null) Set(meta, "legacy_id", legacyId);
        Set(acc, "provider_metadata", meta);
        Set(acc, "tags", new object[0]);
        return acc;
    }

    static void AddLegacyFile(ImportPlan plan, string path, Dictionary<string, object> existing)
    {
        if (!File.Exists(path)) return;
        Dictionary<string, object> reg;
        try { reg = Json.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>; }
        catch (Exception ex) { plan.Conflicts.Add(path + ": unreadable (" + ex.Message + ")"); return; }
        var accounts = Get(reg, "accounts") as object[];
        if (accounts == null) return;
        foreach (object item in accounts)
        {
            var legacy = item as Dictionary<string, object>;
            if (legacy == null) continue;
            var desc = LegacyAccountToDescriptor(legacy);
            if (desc == null) { plan.Conflicts.Add(path + ": account without windows_sid skipped"); continue; }
            MergeIntoPlan(plan, desc, existing, path);
        }
    }

    static void MergeIntoPlan(ImportPlan plan, Dictionary<string, object> desc, Dictionary<string, object> existing, string source)
    {
        string id = S(desc, "account_id");
        if (existing != null && FindAccount(existing, id) != null) { plan.Already.Add(id); return; }
        foreach (var other in plan.ToAdd)
        {
            if (string.Equals(S(other, "account_id"), id, StringComparison.OrdinalIgnoreCase))
            {
                // same identity from two legacy files: merge only when non-conflicting
                var a = Get(desc, "provider_metadata") as Dictionary<string, object>;
                var b = Get(other, "provider_metadata") as Dictionary<string, object>;
                string userA = a != null ? S(a, "windows_user") : null, userB = b != null ? S(b, "windows_user") : null;
                string sidA = a != null ? S(a, "windows_sid") : null, sidB = b != null ? S(b, "windows_sid") : null;
                if (userA != null && userB != null && !string.Equals(userA, userB, StringComparison.OrdinalIgnoreCase))
                    plan.Conflicts.Add(id + ": windows_user '" + userA + "' vs '" + userB + "' (" + source + ")");
                if (sidA == null || sidB == null || !string.Equals(sidA, sidB, StringComparison.OrdinalIgnoreCase))
                    plan.Conflicts.Add(id + ": windows_sid mismatch (" + source + ")");
                return;
            }
        }
        plan.ToAdd.Add(desc);
    }

    static ImportPlan BuildImportPlan(Dictionary<string, object> existing)
    {
        var plan = new ImportPlan();
        AddLegacyFile(plan, LegacyRegistryUserPath(), existing);
        AddLegacyFile(plan, LegacyRegistryMachinePath(), existing);
        // Known Claude/Codex profile homes (import only what exists).
        string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        AddProfile(plan, existing, "claude", Path.Combine(home, ".claude"), null, "claude-1");
        AddProfile(plan, existing, "claude", Path.Combine(home, ".claude-account2"), null, "claude-2");
        AddProfile(plan, existing, "codex", Path.Combine(home, ".codex"), true, "codex-1");
        AddProfile(plan, existing, "codex", Path.Combine(home, ".codex-account2"), true, "codex-2");
        AddProfile(plan, existing, "codex", Path.Combine(home, ".codex-account3free"), true, "codex-3-free");
        return plan;
    }

    static void AddProfile(ImportPlan plan, Dictionary<string, object> existing, string provider, string dir, bool? scrub, string legacyId)
    {
        if (!Directory.Exists(dir)) return;
        var desc = ProfileAccountDescriptor(provider, dir, scrub == true, legacyId);
        MergeIntoPlan(plan, desc, existing, "profiles");
    }

    static int CmdImport(string[] args)
    {
        bool dryRun = false;
        for (int i = 1; i < args.Length; i++) if (args[i] == "--dry-run") dryRun = true;
        var existing = LoadRegistry();
        var plan = BuildImportPlan(existing);
        if (plan.Conflicts.Count > 0)
        {
            var o = Obj();
            Set(o, "schema", "sai.accounts/import/1");
            Set(o, "status", "IMPORT_CONFLICT");
            Set(o, "conflicts", plan.Conflicts.ToArray());
            Set(o, "detail", "conflicting experimental registry records were NOT merged — resolve manually");
            Print(o); return 1;
        }
        var report = Obj();
        Set(report, "schema", "sai.accounts/import/1");
        if (dryRun)
        {
            Set(report, "status", "dry_run");
            Set(report, "would_add", DescribePlan(plan));
            Print(report); return 0;
        }
        if (plan.ToAdd.Count == 0)
        {
            Set(report, "status", "nothing_to_import");
            Set(report, "already_present", plan.Already.ToArray());
            Print(report); return 0;
        }
        var response = Mutate("account_imported", null, delegate(List<Dictionary<string, object>> accounts, Dictionary<string, object> reg)
        {
            accounts.AddRange(plan.ToAdd);
            return "imported " + plan.ToAdd.Count.ToString(CultureInfo.InvariantCulture) + " account(s)";
        });
        Set(report, "status", "ok");
        Set(report, "added", DescribePlan(plan));
        Set(report, "already_present", plan.Already.ToArray());
        Set(report, "generation", Get(response, "generation"));
        Print(report); return 0;
    }

    static object[] DescribePlan(ImportPlan plan)
    {
        var list = new List<object>();
        foreach (var acc in plan.ToAdd)
        {
            var o = Obj();
            Set(o, "account_id", S(acc, "account_id"));
            Set(o, "provider_id", S(acc, "provider_id"));
            Set(o, "execution_backend", S(acc, "execution_backend"));
            list.Add(o);
        }
        return list.ToArray();
    }

    static int CmdRename(string selector, string alias)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);
        string id = S(acc, "account_id");
        var response = Mutate("account_renamed", id, delegate(List<Dictionary<string, object>> accounts, Dictionary<string, object> r)
        {
            foreach (var a in accounts)
                if (string.Equals(S(a, "account_id"), id, StringComparison.OrdinalIgnoreCase))
                {
                    if (alias == "--") Set(a, "display_name", null);
                    else Set(a, "display_name", alias);
                    Set(a, "updated_at", NowIso());
                }
            return alias == "--" ? "alias_cleared" : "aliased";
        });
        Print(response); return 0;
    }

    static int CmdMove(string[] args)
    {
        if (args.Length < 3)
            return Fail("move_args", "move <selector> <up|down|before <target>|after <target>>");
        string selector = args[1];
        string direction = args[2].ToLowerInvariant();
        string targetSelector = null;
        if (direction == "up" || direction == "down")
        {
            if (args.Length != 3)
                return Fail("move_args", "move <selector> <up|down>");
        }
        else if (direction == "before" || direction == "after")
        {
            if (args.Length != 4 || string.IsNullOrEmpty(args[3]))
                return Fail("move_args", "move <selector> " + direction + " <target>");
            targetSelector = args[3];
        }
        else
        {
            return Fail("move_args", "unknown move direction '" + args[2] + "' (expected up, down, before, after)");
        }

        var regCheck = RequireRegistry();
        if (regCheck == null) return 1;

        var response = Mutate("account_moved", null, delegate(List<Dictionary<string, object>> accounts, Dictionary<string, object> r)
        {
            var srcAcc = FindAccount(r, selector);
            if (srcAcc == null)
            {
                var err = Obj();
                Set(err, "_early_return", true);
                Set(err, "_fail_code", "ACCOUNT_NOT_FOUND");
                Set(err, "_fail_detail", selector);
                return err;
            }
            string srcId = S(srcAcc, "account_id");
            int oldIndex = -1;
            for (int i = 0; i < accounts.Count; i++)
            {
                if (string.Equals(S(accounts[i], "account_id"), srcId, StringComparison.OrdinalIgnoreCase))
                {
                    oldIndex = i;
                    srcAcc = accounts[i];
                    break;
                }
            }
            if (oldIndex < 0)
            {
                var err = Obj();
                Set(err, "_early_return", true);
                Set(err, "_fail_code", "ACCOUNT_NOT_FOUND");
                Set(err, "_fail_detail", selector);
                return err;
            }

            int currentGen = Convert.ToInt32(Get(r, "generation"), CultureInfo.InvariantCulture);
            int newIndex = oldIndex;

            if (direction == "up")
            {
                if (oldIndex == 0)
                {
                    var noop = Obj();
                    Set(noop, "_early_return", true);
                    Set(noop, "schema", "sai.accounts/mutate/1");
                    Set(noop, "status", "noop");
                    Set(noop, "account_id", srcId);
                    Set(noop, "generation", currentGen);
                    Set(noop, "from_index", oldIndex);
                    Set(noop, "to_index", oldIndex);
                    Set(noop, "result", "already_at_boundary");
                    return noop;
                }
                newIndex = oldIndex - 1;
                accounts.RemoveAt(oldIndex);
                accounts.Insert(newIndex, srcAcc);
            }
            else if (direction == "down")
            {
                if (oldIndex >= accounts.Count - 1)
                {
                    var noop = Obj();
                    Set(noop, "_early_return", true);
                    Set(noop, "schema", "sai.accounts/mutate/1");
                    Set(noop, "status", "noop");
                    Set(noop, "account_id", srcId);
                    Set(noop, "generation", currentGen);
                    Set(noop, "from_index", oldIndex);
                    Set(noop, "to_index", oldIndex);
                    Set(noop, "result", "already_at_boundary");
                    return noop;
                }
                newIndex = oldIndex + 1;
                accounts.RemoveAt(oldIndex);
                accounts.Insert(newIndex, srcAcc);
            }
            else
            {
                var targetAcc = FindAccount(r, targetSelector);
                if (targetAcc == null)
                {
                    var err = Obj();
                    Set(err, "_early_return", true);
                    Set(err, "_fail_code", "ACCOUNT_NOT_FOUND");
                    Set(err, "_fail_detail", targetSelector);
                    return err;
                }
                string targetId = S(targetAcc, "account_id");
                if (string.Equals(srcId, targetId, StringComparison.OrdinalIgnoreCase))
                {
                    var noop = Obj();
                    Set(noop, "_early_return", true);
                    Set(noop, "schema", "sai.accounts/mutate/1");
                    Set(noop, "status", "noop");
                    Set(noop, "account_id", srcId);
                    Set(noop, "generation", currentGen);
                    Set(noop, "from_index", oldIndex);
                    Set(noop, "to_index", oldIndex);
                    Set(noop, "result", "unchanged");
                    return noop;
                }

                accounts.RemoveAt(oldIndex);
                int targetIdx = -1;
                for (int i = 0; i < accounts.Count; i++)
                {
                    if (string.Equals(S(accounts[i], "account_id"), targetId, StringComparison.OrdinalIgnoreCase))
                    {
                        targetIdx = i;
                        break;
                    }
                }
                if (targetIdx < 0)
                {
                    accounts.Insert(oldIndex, srcAcc);
                    var err = Obj();
                    Set(err, "_early_return", true);
                    Set(err, "_fail_code", "ACCOUNT_NOT_FOUND");
                    Set(err, "_fail_detail", targetSelector);
                    return err;
                }

                int insertIdx = direction == "before" ? targetIdx : targetIdx + 1;
                if (insertIdx == oldIndex)
                {
                    accounts.Insert(oldIndex, srcAcc);
                    var noop = Obj();
                    Set(noop, "_early_return", true);
                    Set(noop, "schema", "sai.accounts/mutate/1");
                    Set(noop, "status", "noop");
                    Set(noop, "account_id", srcId);
                    Set(noop, "generation", currentGen);
                    Set(noop, "from_index", oldIndex);
                    Set(noop, "to_index", oldIndex);
                    Set(noop, "result", "unchanged");
                    return noop;
                }

                accounts.Insert(insertIdx, srcAcc);
                newIndex = insertIdx;
            }

            var moved = Obj();
            Set(moved, "account_id", srcId);
            Set(moved, "from_index", oldIndex);
            Set(moved, "to_index", newIndex);
            Set(moved, "result", "moved");
            return moved;
        });

        string failCode = S(response, "_fail_code");
        if (!string.IsNullOrEmpty(failCode))
            return Fail(failCode, S(response, "_fail_detail") ?? "");
        Print(response);
        return 0;
    }

    static int CmdBindIdentity(string[] args)
    {
        if (args.Length < 2)
            return Fail("bind_identity_args", "bind-identity <selector> [--fingerprint <fp> [--label <label>]] [--clear]");
        string selector = args[1];
        string fpArg = null;
        string labelArg = null;
        bool clear = false;
        for (int i = 2; i < args.Length; i++)
        {
            if (args[i] == "--clear") clear = true;
            else if (args[i] == "--fingerprint" && i + 1 < args.Length) fpArg = args[++i];
            else if (args[i] == "--label" && i + 1 < args.Length) labelArg = args[++i];
            else return Fail("bind_identity_args", "bind-identity <selector> [--fingerprint <fp> [--label <label>]] [--clear]");
        }

        if (clear && (fpArg != null || labelArg != null))
            return Fail("bind_identity_args", "--clear cannot be combined with --fingerprint or --label");
        if (fpArg != null)
        {
            fpArg = fpArg.Trim();
            if (!System.Text.RegularExpressions.Regex.IsMatch(fpArg, "\\A[0-9a-fA-F]{16}\\z"))
                return Fail("IDENTITY_INVALID", "--fingerprint requires the 16 hexadecimal characters of a provider identity");
            fpArg = fpArg.ToLowerInvariant();
        }

        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);
        string id = S(acc, "account_id");

        if (clear)
        {
            var clearResp = Mutate("account_identity_cleared", id, delegate(List<Dictionary<string, object>> accounts, Dictionary<string, object> r)
            {
                foreach (var a in accounts)
                {
                    if (string.Equals(S(a, "account_id"), id, StringComparison.OrdinalIgnoreCase))
                    {
                        a.Remove("configured_identity");
                        Set(a, "updated_at", NowIso());
                    }
                }
                return "cleared";
            });
            Print(clearResp);
            return 0;
        }

        string fp = fpArg != null ? fpArg.Trim() : null;
        string label = labelArg != null ? labelArg.Trim() : null;
        if (string.IsNullOrEmpty(fp))
        {
            string backend = S(acc, "execution_backend");
            if (IsWindowsUserBackend(backend))
            {
                string targetSid = AccountSid(acc);
                string currentSid = WindowsIdentity.GetCurrent().User.Value;
                Dictionary<string, object> cred = null;
                if (string.Equals(targetSid, currentSid, StringComparison.OrdinalIgnoreCase))
                {
                    cred = CheckCredential();
                }
                else
                {
                    var reply = SendPipeCommand(PipeNameFor(ScopeId(), Fingerprint(targetSid)), new Dictionary<string, object> { { "cmd", "status" } }, 3000);
                    if (reply != null)
                        cred = Get(reply, "credential") as Dictionary<string, object>;
                }
                var pi = cred != null ? Get(cred, "provider_identity") as Dictionary<string, object> : null;
                if (pi != null && object.Equals(Get(pi, "verified"), true))
                {
                    fp = S(pi, "fingerprint");
                    if (string.IsNullOrEmpty(label))
                        label = S(pi, "account_label");
                }
            }
            if (string.IsNullOrEmpty(fp))
                return Fail("IDENTITY_UNPROVEN", "runtime provider identity is not available to bind for " + id);
        }

        var response = Mutate("account_identity_bound", id, delegate(List<Dictionary<string, object>> accounts, Dictionary<string, object> r)
        {
            var bound = Obj();
            Set(bound, "fingerprint", fp);
            if (!string.IsNullOrEmpty(label))
                Set(bound, "account_label", label);
            Set(bound, "bound_at", NowIso());
            foreach (var a in accounts)
            {
                if (string.Equals(S(a, "account_id"), id, StringComparison.OrdinalIgnoreCase))
                {
                    Set(a, "configured_identity", bound);
                    Set(a, "updated_at", NowIso());
                }
            }
            return bound;
        });
        Print(response);
        return 0;
    }

    static int CmdSetState(string selector, string state)
    {
        if (state != "ENABLED" && state != "DISABLED" && state != "FROZEN" && state != "ARCHIVED")
            return Fail("invalid_state", state);
        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);
        string id = S(acc, "account_id");
        string action = "account_" + state.ToLowerInvariant();
        if (state == "ENABLED") action = string.Equals(S(acc, "operational_state"), "FROZEN") ? "account_unfrozen" : "account_enabled";
        if (state == "ARCHIVED") action = "account_archived";
        var response = Mutate(action, id, delegate(List<Dictionary<string, object>> accounts, Dictionary<string, object> r)
        {
            foreach (var a in accounts)
                if (string.Equals(S(a, "account_id"), id, StringComparison.OrdinalIgnoreCase))
                {
                    Set(a, "operational_state", state);
                    Set(a, "updated_at", NowIso());
                }
            return state.ToLowerInvariant();
        });
        Print(response); return 0;
    }

    static int CmdSetHidden(string selector, bool hidden)
    {
        var reg = RequireRegistry(); if (reg == null) return 1;
        var acc = FindAccount(reg, selector);
        if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);
        string id = S(acc, "account_id");
        var response = Mutate(hidden ? "account_hidden" : "account_shown", id, delegate(List<Dictionary<string, object>> accounts, Dictionary<string, object> r)
        {
            foreach (var a in accounts)
                if (string.Equals(S(a, "account_id"), id, StringComparison.OrdinalIgnoreCase))
                {
                    Set(a, "hidden", hidden);
                    Set(a, "updated_at", NowIso());
                }
            return hidden ? "hidden" : "shown";
        });
        Print(response); return 0;
    }

    static string FileSha256(string path)
    {
        using (var sha = SHA256.Create())
        using (var fs = File.OpenRead(path))
        {
            byte[] hash = sha.ComputeHash(fs);
            var hex = new StringBuilder();
            foreach (byte b in hash) hex.Append(b.ToString("x2", CultureInfo.InvariantCulture));
            return hex.ToString();
        }
    }

    static int CmdSyncBinary(string[] args)
    {
        string source = null;
        for (int i = 1; i < args.Length; i++)
            if (args[i] == "--source" && i + 1 < args.Length) { source = args[i + 1]; i++; }
        if (source == null)
        {
            string pathEnv = Environment.GetEnvironmentVariable("PATH") ?? "";
            foreach (string dir in pathEnv.Split(';'))
            {
                try
                {
                    string candidate = Path.Combine(dir.Trim(), "agy.exe");
                    if (File.Exists(candidate)) { source = candidate; break; }
                }
                catch { }
            }
        }
        if (source == null || !File.Exists(source)) return Fail("source_not_found", source ?? "agy.exe not on PATH; pass --source");
        Directory.CreateDirectory(BinDir());
        string staged = SharedAgyPath() + ".staged";
        File.Copy(source, staged, true);
        string hash = FileSha256(staged);
        var versionCapture = RunCapture(staged, "--version", 10000);
        string version = S(versionCapture, "stdout");
        if (version != null) version = version.Trim();
        if (File.Exists(SharedAgyPath())) File.Replace(staged, SharedAgyPath(), null);
        else File.Move(staged, SharedAgyPath());
        var state = Obj();
        Set(state, "agy", new object[] { MakeBinaryState(version, hash, source) });
        File.WriteAllText(BinStatePath(), Json.Serialize(state), new UTF8Encoding(true));
        var o = Obj(); Set(o, "schema", "sai.accounts/sync-binary/1");
        Set(o, "status", "synced"); Set(o, "source", source);
        Set(o, "version", version); Set(o, "sha256", hash);
        Print(o); return 0;
    }

    static Dictionary<string, object> MakeBinaryState(string version, string hash, string source)
    {
        var b = Obj();
        Set(b, "version", version); Set(b, "sha256", hash);
        Set(b, "source", source); Set(b, "updated_at", NowIso());
        return b;
    }

    static int CmdDoctor()
    {
        var o = Obj();
        Set(o, "schema", "sai.accounts/doctor/1");
        Set(o, "exe", SelfExePath());
        Set(o, "root", RootDir());
        Set(o, "scope_id", ScopeId());
        Set(o, "operator_sid", OperatorSid());
        var checks = new List<object>();
        Action<string, bool, string> add = delegate(string name, bool ok, string detail)
        {
            var c = Obj(); Set(c, "check", name); Set(c, "ok", ok); Set(c, "detail", detail);
            checks.Add(c);
        };

        // scope ACL
        try
        {
            var rules = new DirectoryInfo(ScopeDir()).GetAccessControl().GetAccessRules(true, true, typeof(SecurityIdentifier));
            bool usersWrite = false;
            foreach (FileSystemAccessRule rule in rules)
                if (rule.IdentityReference.Value == "S-1-5-32-545" && (rule.FileSystemRights & FileSystemRights.Write) != 0 && rule.AccessControlType == AccessControlType.Allow)
                    usersWrite = true;
            add("scope_acl_no_users_write", !usersWrite, usersWrite ? "BUILTIN\\Users has write — repair with reinstall" : "least privilege ok");
        }
        catch (Exception ex) { add("scope_acl_no_users_write", false, ex.Message); }

        // registry
        var reg = LoadRegistry();
        if (reg == null) add("registry", false, "missing — run import");
        else add("registry", true, "generation " + Convert.ToInt32(Get(reg, "generation"), CultureInfo.InvariantCulture) + ", accounts " + AccountsOf(reg).Count);

        // shared binary
        if (File.Exists(SharedAgyPath()))
        {
            string actual = FileSha256(SharedAgyPath());
            string recorded = null;
            try
            {
                var state = Json.DeserializeObject(File.ReadAllText(BinStatePath(), Encoding.UTF8)) as Dictionary<string, object>;
                var agyArr = Get(state, "agy") as object[];
                if (agyArr != null && agyArr.Length > 0) recorded = S(agyArr[0], "sha256");
            }
            catch { }
            add("shared_agy", recorded == null || recorded == actual, "sha256 " + actual.Substring(0, 12) + (recorded == null ? " (no recorded state)" : recorded == actual ? " (matches)" : " MISMATCH recorded " + recorded.Substring(0, 12)));
        }
        else add("shared_agy", false, SharedAgyPath() + " missing — run sync-binary");

        // brokers reachable
        if (reg != null)
        {
            var accounts = AccountsOf(reg);
            foreach (var acc in accounts)
            {
                // Every windows-user backend owns a real broker process that can die
                // independently of the registry. managed_windows_user (the Antigravity
                // contexts) was excluded here, so the one account whose broker actually
                // fails to start was the one account doctor stayed silent about.
                if (!IsWindowsUserBackend(S(acc, "execution_backend"))) continue;
                string sid = AccountSid(acc);
                if (sid == null || string.Equals(sid, WindowsIdentity.GetCurrent().User.Value, StringComparison.OrdinalIgnoreCase)) continue;
                string accountId = S(acc, "account_id") ?? Fingerprint(sid);
                var reply = SendPipeCommand(PipeNameFor(ScopeId(), Fingerprint(sid)), new Dictionary<string, object> { { "cmd", "status" } }, 2000);
                bool reachable = reply != null;
                string detail;
                if (!reachable) detail = accountId + ": offline";
                else
                {
                    // Reachability is not identity. A broker answering on the pipe must
                    // also be running as the managed SID, or it is not this account's.
                    string reported = S(reply, "sid");
                    bool identityOk = reported == null || string.Equals(reported, sid, StringComparison.OrdinalIgnoreCase);
                    detail = accountId + ": online as " + (reported ?? "unknown") + (identityOk ? "" : " — expected " + sid);
                    add("broker_" + accountId, reachable && identityOk, detail);
                    continue;
                }
                add("broker_" + accountId, reachable, detail);
            }
        }

        add("legacy_user_registry_deprecated", true, LegacyRegistryUserPath() + (File.Exists(LegacyRegistryUserPath()) ? " (present, migration input only)" : " (absent)"));
        add("legacy_machine_registry_deprecated", true, LegacyRegistryMachinePath() + (File.Exists(LegacyRegistryMachinePath()) ? " (present, migration input only)" : " (absent)"));

        Set(o, "checks", checks.ToArray());
        bool allOk = true;
        foreach (Dictionary<string, object> c in checks) if (!object.Equals(Get(c, "ok"), true)) allOk = false;
        Set(o, "status", allOk ? "ok" : "attention");
        Print(o); return allOk ? 0 : 1;
    }

    static int CmdCapabilities(string provider)
    {
        ProviderDef def;
        if (!Providers.TryGetValue(provider, out def)) return Fail("unknown_provider", provider);
        var o = Obj();
        Set(o, "schema", "sai.accounts/provider/1");
        Set(o, "provider_id", def.Id);
        Set(o, "supports_multi_account", def.MultiAccount);
        Set(o, "supports_interactive_login", def.InteractiveLogin);
        Set(o, "supports_quota", def.Quota);
        Set(o, "supports_launch", def.Launch);
        Set(o, "supports_logout", def.Logout);
        Set(o, "supports_context_creation", def.ContextCreate);
        Set(o, "supports_managed_context", string.Equals(provider, "antigravity", StringComparison.OrdinalIgnoreCase));
        Set(o, "supports_legacy_context", string.Equals(provider, "antigravity", StringComparison.OrdinalIgnoreCase));
        Print(o); return 0;
    }

    static int CmdHelper(string[] args)
    {
        string sub = args.Length > 1 ? args[1].ToLowerInvariant() : "status";
        if (sub == "status")
        {
            var hReq = Obj(); Set(hReq, "op", "status");
            var hResp = CallHelper(hReq, 2000);
            var o = Obj(); Set(o, "schema", "sai.accounts/helper/1");
            Set(o, "status", hResp != null ? "online" : "offline");
            Set(o, "pipe", HelperPipeName());
            if (hResp != null)
            {
                if (hResp.ContainsKey("authorized_operator_sid")) Set(o, "authorized_operator_sid", hResp["authorized_operator_sid"]);
                if (hResp.ContainsKey("client_sid")) Set(o, "client_sid", hResp["client_sid"]);
                if (hResp.ContainsKey("service_sid")) Set(o, "service_sid", hResp["service_sid"]);
            }
            Print(o);
            return hResp != null ? 0 : 2;
        }
        if (sub == "install")
        {
            string helperExe = Path.Combine(BinDir(), "sai-accounts-helper.exe");
            if (!File.Exists(helperExe)) helperExe = Path.Combine(Path.GetDirectoryName(SelfExePath()), "sai-accounts-helper.exe");
            if (!File.Exists(helperExe)) return Fail("helper_not_found", "sai-accounts-helper.exe not found");
            string opSid = OperatorSid();
            for (int i = 2; i < args.Length; i++)
            {
                if (args[i] == "--operator-sid" && i + 1 < args.Length) { opSid = args[i + 1]; i++; }
            }
            try
            {
                string installArgs = "--install" + (!string.IsNullOrEmpty(opSid) ? " --operator-sid " + opSid : "");
                var psi = new ProcessStartInfo(helperExe, installArgs) { UseShellExecute = true, Verb = "runas" };
                var p = Process.Start(psi);
                p.WaitForExit(15000);
                if (p.ExitCode != 0)
                {
                    var oBad = Obj(); Set(oBad, "schema", "sai.accounts/helper/1");
                    Set(oBad, "status", "install_failed");
                    Set(oBad, "exit_code", p.ExitCode);
                    Print(oBad);
                    return p.ExitCode;
                }

                // Verify from the original NON-ELEVATED operator process that helper status succeeds
                bool online = false;
                Dictionary<string, object> hResp = null;
                for (int i = 0; i < 20; i++)
                {
                    Thread.Sleep(250);
                    var hReq = Obj(); Set(hReq, "op", "status");
                    hResp = CallHelper(hReq, 1000);
                    if (hResp != null && string.Equals(S(hResp, "status"), "online"))
                    {
                        online = true;
                        break;
                    }
                }

                var o = Obj(); Set(o, "schema", "sai.accounts/helper/1");
                Set(o, "status", online ? "installed" : "installed_but_offline");
                Set(o, "verified_online", online);
                Set(o, "operator_sid", opSid);
                if (hResp != null && hResp.ContainsKey("authorized_operator_sid"))
                {
                    Set(o, "authorized_operator_sid", hResp["authorized_operator_sid"]);
                }
                Print(o);
                return online ? 0 : 1;
            }
            catch (Exception ex)
            {
                return Fail("helper_install_failed", ex.Message);
            }
        }
        if (sub == "uninstall")
        {
            string helperExe = Path.Combine(BinDir(), "sai-accounts-helper.exe");
            if (!File.Exists(helperExe)) helperExe = Path.Combine(Path.GetDirectoryName(SelfExePath()), "sai-accounts-helper.exe");
            try
            {
                var psi = new ProcessStartInfo(helperExe, "--uninstall") { UseShellExecute = true, Verb = "runas" };
                var p = Process.Start(psi);
                p.WaitForExit(15000);
                var o = Obj(); Set(o, "schema", "sai.accounts/helper/1");
                Set(o, "status", p.ExitCode == 0 ? "uninstalled" : "uninstall_failed");
                Print(o);
                return p.ExitCode;
            }
            catch (Exception ex)
            {
                return Fail("helper_uninstall_failed", ex.Message);
            }
        }
        return Fail("unknown_helper_verb", sub);
    }

    static int CmdAdd(string[] args)
    {
        string provider = "antigravity";
        string alias = null;
        bool forceManaged = false;
        for (int i = 1; i < args.Length; i++)
        {
            if (args[i] == "--provider" && i + 1 < args.Length) { provider = args[i + 1]; i++; }
            else if (args[i] == "--alias" && i + 1 < args.Length) { alias = args[i + 1]; i++; }
            else if (args[i] == "--managed") { forceManaged = true; }
        }

        var reg = LoadRegistry();
        if (reg == null)
        {
            reg = Obj();
            Set(reg, "schema", "sai.accounts/registry/2");
            Set(reg, "generation", 0);
            var opScope = Obj();
            Set(opScope, "scope_id", ScopeId());
            Set(opScope, "operator_sid", OperatorSid());
            Set(reg, "operator_scope", opScope);
            Set(reg, "accounts", new object[0]);
        }
        var accounts = AccountsOf(reg);

        if (string.Equals(provider, "antigravity", StringComparison.OrdinalIgnoreCase))
        {
            string mySid = WindowsIdentity.GetCurrent().User.Value;
            bool hasCurrentUser = false;
            foreach (var a in accounts)
            {
                if (string.Equals(S(a, "provider_id"), "antigravity", StringComparison.OrdinalIgnoreCase))
                {
                    // AccountSid, not the raw field: an account that names this
                    // machine's own Windows user IS the current user, and
                    // failing to see that here is what makes `add` create a
                    // second, duplicate "current user" account beside it.
                    if (string.Equals(AccountSid(a), mySid, StringComparison.OrdinalIgnoreCase))
                    {
                        hasCurrentUser = true;
                        break;
                    }
                }
            }

            if (!hasCurrentUser && !forceManaged)
            {
                string accId = AccountIdFor("antigravity", "windows_user", mySid);
                var newAcc = Obj();
                Set(newAcc, "account_id", accId);
                Set(newAcc, "provider_id", "antigravity");
                Set(newAcc, "display_name", alias);
                Set(newAcc, "execution_backend", "windows_user");
                Set(newAcc, "execution_context_id", ContextIdFor("windows_user", mySid));
                Set(newAcc, "operational_state", "ENABLED");
                Set(newAcc, "hidden", false);
                Set(newAcc, "created_at", NowIso());
                Set(newAcc, "updated_at", NowIso());
                var meta = Obj();
                Set(meta, "windows_user", Environment.UserName);
                Set(meta, "windows_sid", mySid);
                Set(meta, "managed", false);
                Set(newAcc, "provider_metadata", meta);
                Set(newAcc, "tags", new string[0]);

                Mutate("account_added", accId, delegate(List<Dictionary<string, object>> accts, Dictionary<string, object> r)
                {
                    accts.Add(newAcc);
                    return "ok";
                });
                accounts.Add(newAcc);

                var o = Obj();
                Set(o, "schema", "sai.accounts/add/1");
                Set(o, "status", "created");
                Set(o, "account_id", accId);
                Set(o, "execution_backend", "windows_user");
                Set(o, "display_name", DisplayName(accounts, newAcc));
                Print(o);
                return 0;
            }
            else
            {
                var hReq = Obj();
                Set(hReq, "op", "managed_context_create");
                Set(hReq, "provider", "antigravity");
                string opId = Guid.NewGuid().ToString("N");
                Set(hReq, "operation_id", opId);
                var hResp = CallHelper(hReq, 15000);
                if (hResp == null || !string.Equals(S(hResp, "status"), "ok"))
                {
                    return Fail("helper_managed_create_failed", hResp != null ? S(hResp, "error") ?? S(hResp, "status") : "helper_unreachable");
                }
                string sid = S(hResp, "target_sid");
                string winUser = S(hResp, "windows_user");
                string accId = S(hResp, "context_id");

                var newAcc = Obj();
                Set(newAcc, "account_id", accId);
                Set(newAcc, "provider_id", "antigravity");
                Set(newAcc, "display_name", alias);
                Set(newAcc, "execution_backend", "managed_windows_user");
                Set(newAcc, "execution_context_id", "windows-user:" + Fingerprint(sid));
                Set(newAcc, "operational_state", "ENABLED");
                Set(newAcc, "hidden", false);
                Set(newAcc, "created_at", NowIso());
                Set(newAcc, "updated_at", NowIso());
                var meta = Obj();
                Set(meta, "windows_user", winUser);
                Set(meta, "windows_sid", sid);
                Set(meta, "managed", true);
                Set(newAcc, "provider_metadata", meta);
                Set(newAcc, "tags", new string[0]);

                var o = Obj();
                // See CmdContext create: a context the registry never learned
                // about is not a failed add, it is an orphan Windows account.
                // Compensate against the same operation id, then report.
                try
                {
                    Mutate("account_added", accId, delegate(List<Dictionary<string, object>> accts, Dictionary<string, object> r)
                    {
                        accts.Add(newAcc);
                        return "ok";
                    });
                }
                catch (Exception ex)
                {
                    var comp = CompensateContextCreate(opId, "add registry write failed: " + ex.Message);
                    Set(o, "status", "REGISTRY_FAILED");
                    Set(o, "registry_failure", ex.Message);
                    Set(o, "compensation", comp);
                    Print(o);
                    return 1;
                }
                accounts.Add(newAcc);

                Set(o, "schema", "sai.accounts/add/1");
                Set(o, "status", "created");
                Set(o, "account_id", accId);
                Set(o, "execution_backend", "managed_windows_user");
                Set(o, "windows_user", winUser);
                Set(o, "display_name", DisplayName(accounts, newAcc));
                Print(o);
                return 0;
            }
        }
        return Fail("unsupported_provider", provider);
    }

    // The exit code of a launch-shaped command is a VERDICT, not a receipt.
    //
    // LaunchAccount and AuthAccount answer with a structured envelope, and the
    // reply used to be read as "did it return an object at all": a response of
    // launch_failed, WORKDIR_UNAVAILABLE, CONTEXT_OFFLINE, LIFECYCLE_FORBIDDEN,
    // BACKEND_UNSUPPORTED, PROFILE_LOCATOR_MISSING, PROVIDER_UNSUPPORTED or
    // ACCOUNT_NOT_FOUND still exited 0. A caller that had only the exit status
    // to go on therefore believed it had opened a console that never appeared,
    // on a window that had already closed. A valid JSON envelope is not command
    // success, and this is the one place that decides so.
    //
    // Success is exactly one word: status == "launched". Everything else is a
    // nonzero exit, with the reason already on stdout in the envelope.
    // CONTEXT_OFFLINE keeps its own code 2 (set by LaunchAccount/AuthAccount),
    // so a caller can still tell "this account's Windows context is not running"
    // from "this account refused the launch".
    static int LaunchVerdict(Dictionary<string, object> result)
    {
        if (string.Equals(S(result, "status"), "launched", StringComparison.Ordinal)) return 0;
        int code = Environment.ExitCode;
        if (code != 0) return code;
        return 1;
    }

    static void HideConsoleIfSoleProcess(string[] args)
    {
        try
        {
            if (args != null && args.Length > 0 &&
                string.Equals(args[0], "console-title-guard", StringComparison.OrdinalIgnoreCase))
            {
                return;
            }
            uint[] pids = new uint[2];
            if (GetConsoleProcessList(pids, 2) == 1)
            {
                IntPtr hwnd = GetConsoleWindow();
                if (hwnd != IntPtr.Zero)
                {
                    SetWindowPos(hwnd, IntPtr.Zero, -32000, -32000, 0, 0, 0x0015);
                    ShowWindow(hwnd, 0);
                }
            }
        }
        catch { }
    }

    // ----------------------------------------------------------------- main
    public static int Main(string[] args)
    {
        HideConsoleIfSoleProcess(args);
        try { return MainInner(args); }
        catch (AmbiguousAccountSelectorException ex)
        {
            string action = args.Length > 0 ? args[0].ToLowerInvariant() : "";
            var o = Obj();
            Set(o, "schema", action == "launch" ? "sai.accounts/launch/1"
                : action == "auth" ? "sai.accounts/auth/1" : "sai.accounts/error/1");
            Set(o, "status", "ACCOUNT_SELECTOR_AMBIGUOUS");
            Set(o, "selector", ex.Selector);
            Set(o, "candidates", ex.Candidates);
            Set(o, "detail", "Select a unique account_id or compact_label from 'sai-accounts list --all'.");
            Print(o);
            return 1;
        }
        catch (Exception ex) { return Fail("fatal", ex.Message); }
    }

    static int MainInner(string[] args)
    {
        if (args.Length == 0)
        {
            Console.WriteLine("SAI Accounts — canonical multi-account control plane (see SAI_ACCOUNTS.md)");
            Console.WriteLine("usage: sai-accounts <list|get|snapshot|status|usage|launch|context|autoconnect|helper|add|import|rename|move|bind-identity|");
            Console.WriteLine("                        auth|logout|set-state|set-hidden|archive|restore|sync-binary|capabilities|doctor>");
            Console.WriteLine("       sai-accounts --broker <expected-sid> <scope> [operator-sid]");
            return 1;
        }
        string action = args[0].ToLowerInvariant();

        if (action == "--broker")
        {
            LogBroker("MainInner --broker args=" + string.Join(" ", args));
            if (args.Length < 3) return Fail("broker_args", "--broker <expected-sid> <scope> [operator-sid]");
            string operatorSid = OperatorSid();
            if (args.Length > 3) operatorSid = args[3];
            RunBroker(args[1], args[2], operatorSid);
            return 0;
        }
        if (action == "list") return CmdList(args);
        if (action == "capabilities" && args.Length > 1) return CmdCapabilities(args[1]);
        if (action == "snapshot") return CmdSnapshot(args);
        if (action == "doctor") return CmdDoctor();
        if (action == "import") return CmdImport(args);
        if (action == "sync-binary") return CmdSyncBinary(args);
        if (action == "context") return CmdContext(args);
        if (action == "autoconnect") return CmdAutoConnect(args);
        if (action == "helper") return CmdHelper(args);
        // Not a user verb: the first process of a console the engine created,
        // holding the account's window identity for the console's lifetime.
        if (action == "console-title-guard") return CmdConsoleTitleGuard(args);
        if (action == "add") return CmdAdd(args);
        if (action == "move") return CmdMove(args);
        if (action == "bind-identity") return CmdBindIdentity(args);
        if (action == "status" && args.Length > 1 && args[1] == "--all") return CmdStatusAll(args);
        if (args.Length < 2)
            return Fail("missing_selector", "command '" + action + "' needs an account selector");

        string selector = args[1];
        if (action == "get") return CmdGet(selector);
        if (action == "status") return CmdStatus(selector);
        if (action == "usage") return CmdUsage(selector);
        if (action == "launch")
        {
            string workdir = null;
            string authToken = null;
            for (int i = 2; i < args.Length; i++)
            {
                if (args[i] == "--workdir" && i + 1 < args.Length) { workdir = args[i + 1]; i++; }
                else if (args[i] == "--auth-token" && i + 1 < args.Length) { authToken = args[i + 1]; i++; }
            }
            var reg = RequireRegistry();
            if (reg == null)
            {
                var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                Set(o, "status", "REGISTRY_UNAVAILABLE");
                Set(o, "error", "accounts registry could not be loaded");
                Print(o); return 1;
            }
            var acc = FindAccount(reg, selector);
            if (acc == null)
            {
                var o = Obj(); Set(o, "schema", "sai.accounts/launch/1");
                Set(o, "status", "ACCOUNT_NOT_FOUND");
                Set(o, "selector", selector);
                Set(o, "error", "account not found: " + selector);
                Print(o); return 1;
            }
            var result = LaunchAccount(acc, AccountsOf(reg), workdir, authToken);
            return LaunchVerdict(result);
        }
        if (action == "trusted-mode-status")
        {
            return CmdTrustedModeStatus(selector);
        }
        if (action == "trusted-mode-set")
        {
            return CmdTrustedModeSet(selector, args);
        }
        if (action == "authorize-launch")
        {
            return CmdAuthorizeLaunch(selector, args);
        }
        if (action == "stop-session")
        {
            return CmdStopSession(selector);
        }
        if (action == "saipen-readiness")
        {
            // `saipen-readiness <selector> [--workdir <path>]` and nothing else.
            if (args.Length != 2 && !(args.Length == 4 && args[2] == "--workdir"))
                return Fail("saipen_readiness_args", "saipen-readiness <selector> [--workdir <path>]");
            return CmdSaipenReadiness(selector, args.Length == 4 ? args[3] : null);
        }
        if (action == "grant-workdir")
        {
            // Strict shape: `grant-workdir <selector> --workdir <path>` and
            // nothing else. No extra flag, no positional second argument, so
            // there is no spelling of this command that carries anything the
            // helper was not designed to receive.
            if (args.Length != 4 || args[2] != "--workdir") return Fail("grant_workdir_args", "grant-workdir <selector> --workdir <path>");
            return CmdGrantWorkdir(selector, args[3]);
        }
        if (action == "revoke-workdir")
        {
            if (args.Length != 4 || args[2] != "--workdir") return Fail("revoke_workdir_args", "revoke-workdir <selector> --workdir <path>");
            return CmdRevokeWorkdir(selector, args[3]);
        }
        if (action == "probe-workdir")
        {
            // Same strict shape as grant-workdir: four arguments and a flag that
            // cannot carry anything but a path.
            if (args.Length != 4 || args[2] != "--workdir") return Fail("probe_workdir_args", "probe-workdir <selector> --workdir <path>");
            return CmdProbeWorkdir(selector, args[3]);
        }
        if (action == "handoff-stage")
        {
            // `handoff-stage <selector> --workdir <path>` and nothing else: the
            // same strict shape as grant-workdir, because it establishes the
            // same class of durable filesystem state.
            if (args.Length != 4 || args[2] != "--workdir") return Fail("handoff_stage_args", "handoff-stage <selector> --workdir <path>");
            return CmdHandoffStage(selector, args[3]);
        }
        if (action == "handoff-file" || action == "stage-file")
        {
            if (args.Length < 4) return Fail("handoff_file_args", "handoff-file <selector> --source <path> [--workdir <path>]");
            string source = null;
            string workdir = null;
            for (int i = 2; i < args.Length; i++)
            {
                if (args[i] == "--source" && i + 1 < args.Length) { source = args[i + 1]; i++; }
                else if (args[i] == "--workdir" && i + 1 < args.Length) { workdir = args[i + 1]; i++; }
                else return Fail("handoff_file_args", "unknown or incomplete argument: " + args[i]);
            }
            if (string.IsNullOrEmpty(source)) return Fail("SOURCE_REQUIRED", "--source <path> is required");
            return CmdHandoffFile(selector, source, workdir);
        }
        if (action == "verify-file" || action == "probe-file")
        {
            if (args.Length != 4 || args[2] != "--path") return Fail("verify_file_args", "verify-file <selector> --path <path>");
            return CmdVerifyFile(selector, args[3]);
        }
        if (action == "auth")
        {
            // Exactly one argument and nothing else. A stray --workdir, a
            // program name or a script here would be refused by the shape of
            // the command itself rather than by a later check, so there is no
            // version of "sign in" that can be pointed at a folder.
            if (args.Length > 2) return Fail("auth_args", "auth <selector> takes no workdir and no program");
            var reg = RequireRegistry(); if (reg == null) return 1;
            var acc = FindAccount(reg, selector);
            if (acc == null) return Fail("ACCOUNT_NOT_FOUND", selector);
            var result = AuthAccount(acc);
            return LaunchVerdict(result);
        }
        if (action == "logout")
        {
            // Same shape rule as `auth`, and for the same reason: with no flag to
            // carry a target, path or program, "log out" cannot be aimed at
            // anything except the account the selector names.
            if (args.Length > 2) return Fail("logout_args", "logout <selector> takes no target, no path and no program");
            return CmdLogout(selector);
        }
        if (action == "rename")
        {
            if (args.Length < 3) return Fail("rename_args", "rename <selector> \"<alias>|--\"");
            return CmdRename(selector, args[2]);
        }
        if (action == "set-state")
        {
            if (args.Length < 3) return Fail("set_state_args", "set-state <selector> <ENABLED|DISABLED|FROZEN|ARCHIVED>");
            return CmdSetState(selector, args[2].ToUpperInvariant());
        }
        if (action == "set-hidden")
        {
            if (args.Length < 3) return Fail("set_hidden_args", "set-hidden <selector> <true|false>");
            bool hidden; if (!bool.TryParse(args[2], out hidden)) return Fail("set_hidden_args", "true|false");
            return CmdSetHidden(selector, hidden);
        }
        if (action == "archive") return CmdSetState(selector, "ARCHIVED");
        if (action == "restore") return CmdSetState(selector, "ENABLED");
        if (action == "enable") return CmdSetState(selector, "ENABLED");
        if (action == "disable") return CmdSetState(selector, "DISABLED");
        if (action == "freeze") return CmdSetState(selector, "FROZEN");
        if (action == "unfreeze") return CmdSetState(selector, "ENABLED");

        return Fail("unknown_action", action);
    }
}
