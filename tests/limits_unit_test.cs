// T-266 / T-267: AI limits model, presentation, account mapping, the 5h window
// keeper, settings persistence, the language contract, background refresh cost
// and degraded startup. Hermetic: the account engine is a .cmd stub that logs
// every call, no LIMISAW is resolvable, and the settings live in a scratch dir.
//
// Compiled with SAITULS.cs + FolderAutomation\*.cs (tests\test_limits.ps1).
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;
using Saituls;

static class LimitsUnitTest
{
    const BindingFlags Any = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;
    static int passes, failures;

    static void Check(bool ok, string name, string detail = "")
    {
        if (ok) { passes++; Console.WriteLine("PASS  " + name); }
        else { failures++; Console.WriteLine("FAIL  " + name + (detail.Length > 0 ? "  -- " + detail : "")); }
    }

    static object Get(object o, string n)
    {
        FieldInfo f = o.GetType().GetField(n, Any);
        if (f != null) return f.GetValue(o);
        PropertyInfo p = o.GetType().GetProperty(n, Any);
        return p != null ? p.GetValue(o, null) : null;
    }
    static void Set(object o, string n, object v) { o.GetType().GetField(n, Any).SetValue(o, v); }
    static object Call(object o, string n, params object[] a)
    {
        foreach (MethodInfo m in o.GetType().GetMethods(Any))
            if (m.Name == n && m.GetParameters().Length == a.Length) return m.Invoke(o, a);
        throw new Exception("no method " + n);
    }
    static void Pump(Func<bool> done, int ms)
    {
        var sw = Stopwatch.StartNew();
        while (!done() && sw.ElapsedMilliseconds < ms) { Application.DoEvents(); Thread.Sleep(15); }
    }

    static double Now() { return Limits.Now(); }

    // A LIMISAW document in the published shape (Limisaw\LimitsExport.cs).
    static string Doc(double checkedAt, params string[] accounts)
    {
        return "{\"schema\":\"sai.limits/snapshot/1\",\"producer\":\"LIMISAW\",\"checked_at_epoch\":"
            + checkedAt.ToString("0", System.Globalization.CultureInfo.InvariantCulture)
            + ",\"accounts\":[" + string.Join(",", accounts) + "]}";
    }
    static string Acc(string key, string provider, string sourceId, string sourcePath, bool ok, string extra, params string[] windows)
    {
        return "{\"key\":\"" + key + "\",\"provider\":\"" + provider + "\",\"provider_label\":\"" + provider + "\",\"name\":\"n\",\"title\":\"t\",\"short\":\"s\","
            + "\"source_id\":\"" + sourceId + "\",\"source_path\":\"" + sourcePath.Replace("\\", "\\\\") + "\",\"ok\":" + (ok ? "true" : "false")
            + (extra.Length > 0 ? "," + extra : "") + ",\"windows\":[" + string.Join(",", windows) + "]}";
    }
    static string Win(string key, string bas, string group, bool available, int rem, double? reset, string gated = null)
    {
        return "{\"metric_id\":\"m/" + key + "\",\"key\":\"" + key + "\",\"base\":\"" + bas + "\",\"label\":\"" + (bas == "five_hour" ? "5h" : "week")
            + "\",\"title\":\"" + key + "\",\"group\":\"" + group + "\",\"group_label\":\"" + group + "\",\"available\":" + (available ? "true" : "false")
            + ",\"remaining\":" + rem + ",\"reset_epoch\":" + (reset.HasValue ? reset.Value.ToString("0", System.Globalization.CultureInfo.InvariantCulture) : "null")
            + ",\"gated_by\":" + (gated == null ? "null" : "\"" + gated + "\"") + ",\"assumed_full\":false,\"is_short\":" + (bas == "five_hour" ? "true" : "false") + "}";
    }

    [STAThread]
    static int Main()
    {
        double now = Now();

        // ---- 1. no fabricated percentage; unavailable is "--" ------------------
        var unavailable = new LimitWindow { Available = false, Remaining = 0 };
        Check(Limits.Shown(unavailable, false) == "--" && Limits.Shown(unavailable, true) == "--",
            "a window without a reading reads --, in Left and in Used", Limits.Shown(unavailable, false));
        var spent = new LimitWindow { Available = true, Remaining = 0 };
        Check(Limits.Shown(spent, false) == "0%", "a real 0% reading still reads 0%");

        // ---- 2/4. remaining vs used is presentation only -----------------------
        var w30 = new LimitWindow { Available = true, Remaining = 30, MetricId = "a/5h" };
        Check(Limits.Shown(w30, false) == "30%" && Limits.Shown(w30, true) == "70%", "Left shows remaining, Used shows 100 - remaining");
        var inferredFull = new LimitWindow { Available = true, Remaining = 100, AssumedFull = true };
        Check(Limits.Shown(inferredFull, false) == "~100%" && Limits.Shown(inferredFull, true) == "~0%",
            "a value inferred after reset is visibly qualified in both Left and Used presentation");
        inferredFull.Available = false;
        Check(Limits.Shown(inferredFull, false) == "--", "an inferred flag cannot make an unavailable window a percentage");

        // ---- 2/3. real windows parse and pools stay independent ------------------
        string json = Doc(now,
            Acc("antigravity/AG1", "antigravity", "antigravity:windows-user:S-1-5-21-1-2-3-1002", "S-1-5-21-1-2-3-1002", true, "",
                Win("five_hour@claude_and_gpt_models", "five_hour", "claude_and_gpt_models", true, 0, null, "weekly@claude_and_gpt_models"),
                Win("weekly@claude_and_gpt_models", "weekly", "claude_and_gpt_models", true, 0, now + 86400 * 3),
                Win("five_hour@gemini_models", "five_hour", "gemini_models", true, 82, now + 3 * 3600),
                Win("weekly@gemini_models", "weekly", "gemini_models", true, 64, now + 86400 * 5)),
            Acc("claude/aaa", "claude", "aaa", @"C:\Users\x\.claude", true, "",
                Win("five_hour", "five_hour", "", true, 55, now + 1800), Win("weekly", "weekly", "", true, 78, now + 86400)),
            Acc("claude/bbb", "claude", "bbb", @"C:\Users\x\.claude-account2", true, "",
                Win("five_hour", "five_hour", "", true, 55, now + 1800), Win("weekly", "weekly", "", true, 78, now + 86400)),
            Acc("codex/ccc", "codex", "ccc", @"C:\Users\x\.codex", false, "\"auth_failed\":true,\"status\":\"AUTH_REQUIRED\",\"auth_failure_reason\":\"Not logged in\""),
            Acc("codex/ddd", "codex", "ddd", @"C:\Users\x\.codex2", false, "\"error\":\"CONTEXT_OFFLINE\""),
            Acc("zcode/z", "zcode", "z", "", false, "\"carried\":true,\"carried_at\":\"10:00\"",
                Win("five_hour", "five_hour", "", true, 40, now + 600)));
        LimitsSnapshot snap = Limits.ParseLimisaw(json, "LIMISAW");
        Check(snap != null && snap.Accounts.Count == 6, "the published document parses", snap == null ? "null" : snap.Accounts.Count.ToString());
        LimitAccount ag = snap.Accounts[0];
        Check(ag.Windows.Count == 4, "four AG windows stay four (two pools x two windows)", ag.Windows.Count.ToString());
        LimitWindow g5 = ag.Windows.Find(x => x.Key == "five_hour@gemini_models");
        LimitWindow c5 = ag.Windows.Find(x => x.Key == "five_hour@claude_and_gpt_models");
        Check(g5 != null && g5.Remaining == 82 && c5 != null && c5.GatedBy == "weekly@claude_and_gpt_models",
            "a spent Claude/GPT weekly locks its own 5h and leaves the Gemini 5h at 82%");
        Check(g5 != null && c5 != null && g5.Remaining != c5.Remaining && ag.Windows.TrueForAll(x => x.Group.Length > 0),
            "no window of one pool takes another pool's number");
        Check(Limits.ParseLimisaw("{\"schema\":\"other/1\"}", "x") == null, "an unknown schema is refused, not guessed");

        // ---- 6/7. states are words, never 0% ---------------------------------
        Check(Limits.State(snap.Accounts[3], false) == "SIGN_IN", "an auth-rejected account reads SIGN IN REQUIRED");
        Check(Limits.State(snap.Accounts[4], false) == "OFFLINE", "an offline context reads OFFLINE");
        Check(Limits.State(snap.Accounts[5], false) == "STALE", "a carried reading reads STALE");
        Check(Limits.State(snap.Accounts[1], true) == "STALE", "an old snapshot reads STALE");
        Check(Limits.State(snap.Accounts[1], false) == "OK", "a fresh reading reads OK");
        Check(snap.Accounts[3].Windows.Count == 0, "sign-in-required carries no invented window");

        // ---- 5. reset rendering (LIMISAW CountdownText) ------------------------
        Check(Limits.Countdown(now + 30, now) == "<1m", "reset under a minute reads <1m");
        Check(Limits.Countdown(now + 12 * 60 + 5, now) == "12m", "reset in 12 minutes reads 12m");
        Check(Limits.Countdown(now + 3 * 3600, now) == "3h", "reset in 3 hours reads 3h");
        Check(Limits.Countdown(now + 2 * 86400, now) == "2d", "reset in 2 days reads 2d");
        Check(Limits.Countdown(now - 60, now) == "--" && Limits.Countdown(null, now) == "--", "a past or unknown reset reads --, never 0m");

        // ---- 8/9. identity comes from the plane, never from numbers -------------
        var refs = new List<SaiAccountRef>
        {
            new SaiAccountRef { Id = "antigravity:windows-user:1f17", Compact = "AG1", Provider = "antigravity", LegacyId = "antigravity:windows-user:S-1-5-21-1-2-3-1002" },
            new SaiAccountRef { Id = "claude:profile-dir:aa", Compact = "CL1", Provider = "claude", ProfileLocator = @"c:\users\x\.claude\" },
            new SaiAccountRef { Id = "claude:profile-dir:bb", Compact = "CL2", Provider = "claude", ProfileLocator = @"C:\Users\x\.claude-account2" },
        };
        Limits.MapToSai(snap, refs);
        Check(snap.Accounts[0].SaiCompact == "AG1", "AG reading maps to AG1 through the plane's legacy id");
        Check(snap.Accounts[1].SaiCompact == "CL1" && snap.Accounts[2].SaiCompact == "CL2",
            "two Claude readings with IDENTICAL numbers map to two different accounts by profile directory");
        Check(snap.Accounts[3].SaiAccountId == "", "a reading with no identity match stays unmapped even though it is the same provider");
        refs[1].Compact = "CL2"; refs[2].Compact = "CL1";
        Limits.MapToSai(snap, refs);
        Check(snap.Accounts[1].SaiCompact == "CL2" && snap.Accounts[2].SaiCompact == "CL1",
            "reordering canonical accounts refreshes compact labels without changing the reading's identity");
        refs[1].Compact = "CL1"; refs[2].Compact = "CL2"; Limits.MapToSai(snap, refs);

        // ---- 10. global hide vs local hide are separate ------------------------
        var local = Limits.Visible(snap.Accounts, new List<string> { "claude/aaa" }, false, false);
        Check(!local.Exists(x => x.Key == "claude/aaa") && local.Exists(x => x.Key == "claude/bbb"), "a local hide removes only that card");
        var spentFilter = Limits.Visible(snap.Accounts, null, true, false);
        Check(!spentFilter.Exists(x => x.Key == "codex/ccc"), "Hide spent drops an account with no quota");

        // ---- metric selection (LIMISAW ResolveReading) -------------------------
        LimitAccount owner;
        LimitWindow low = Limits.Resolve(snap.Accounts, "lowest", null, out owner);
        Check(low != null && low.Remaining == 40, "lowest remaining minimises over every eligible reading (uncapped)", low == null ? "null" : low.Remaining.ToString());
        LimitWindow pinned = Limits.Resolve(snap.Accounts, g5.MetricId, null, out owner);
        Check(pinned == g5 && owner == ag, "an explicit metric pin wins over a lower reading on another account");
        var showUsedFirst = Limits.Resolve(snap.Accounts, "lowest", null, out owner);
        Check(object.ReferenceEquals(low, showUsedFirst), "the Left/Used switch does not take part in selection");
        LimitWindow any = Limits.Resolve(snap.Accounts, "any", null, out owner);
        Check(any != null && any.Remaining > 0 && owner.Key == "antigravity/AG1", "any available takes the first usable reading in order");
        LimitWindow keep = Limits.Resolve(snap.Accounts, "any", "m/weekly", out owner);
        Check(keep != null && keep.MetricId == "m/weekly", "any available keeps the reading already shown while it is usable");
        var hiddenSel = Limits.Resolve(Limits.Visible(snap.Accounts, new List<string> { "zcode/z" }, false, false), "lowest", null, out owner);
        Check(hiddenSel != null && hiddenSel.Remaining != 40, "a hidden reading does not take part in automatic selection");

        string fallback;
        pinned = Limits.Resolve(snap.Accounts, local, g5.MetricId, null,
            new List<string> { g5.MetricId }, null, out owner, out fallback);
        Check(pinned == g5 && owner == ag && fallback == "",
            "an explicit pin stays authoritative even when its picture reading is locally hidden");
        var sa = new LimitAccount { Key = "a", Ok = true, Windows = new List<LimitWindow> {
            new LimitWindow { MetricId = "a/5h", Base = "five_hour", Available = true, Remaining = 70 },
            new LimitWindow { MetricId = "a/week", Base = "weekly", Available = true, Remaining = 35 } } };
        var sb = new LimitAccount { Key = "b", Ok = true, Windows = new List<LimitWindow> {
            new LimitWindow { MetricId = "b/5h", Base = "five_hour", Available = true, Remaining = 10 },
            new LimitWindow { MetricId = "b/week", Base = "weekly", Available = false } } };
        var selectionAccounts = new List<LimitAccount> { sa, sb };
        var hiddenReadings = new List<string> { "b/5h" };
        var selected = Limits.Resolve(selectionAccounts, selectionAccounts, "lowest", null,
            hiddenReadings, null, out owner, out fallback);
        Check(selected == sa.Windows[1] && owner == sa, "an individually hidden reading is excluded from automatic lowest selection");
        selected = Limits.Resolve(selectionAccounts, selectionAccounts, "any", null, null,
            new[] { "b/5h", "a/week" }, out owner, out fallback);
        Check(selected == sb.Windows[0] && owner == sb, "any available respects explicit reading order rather than source or percentage order");
        var ordered = Limits.Readings(selectionAccounts, null, new[] { "a/week", "missing/reading", "a/week" });
        Check(ordered.Count == 4 && ordered[0].Window == sa.Windows[1] && ordered[1].Window == sa.Windows[0]
            && ordered[2].Window == sb.Windows[0], "unknown or duplicate order entries do not drop new readings or reorder the unconfigured remainder");
        selected = Limits.Resolve(selectionAccounts, selectionAccounts, "b/week", null, hiddenReadings,
            null, out owner, out fallback);
        Check(selected == sa.Windows[1] && fallback == "PIN_UNAVAILABLE", "an unreadable pin has a disclosed fallback to the lowest selected reading");
        selected = Limits.Resolve(selectionAccounts, selectionAccounts, "gone/5h", null, hiddenReadings,
            null, out owner, out fallback);
        Check(selected == sa.Windows[1] && fallback == "PIN_GONE", "a vanished pin has a distinct disclosed fallback and is not silently rewritten");

        // ---- SAI Accounts fallback normalization -------------------------------
        string sai = "{\"schema\":\"sai.accounts/snapshot/1\",\"accounts\":[" +
            "{\"account_id\":\"ag:1\",\"provider_id\":\"antigravity\",\"compact_label\":\"AG1\",\"usage_status\":\"authenticated_usage\",\"windows\":[" +
            "{\"pool_index\":0,\"window\":\"5h\",\"remaining_fraction\":1,\"disabled\":true}," +
            "{\"pool_index\":0,\"window\":\"weekly\",\"remaining_fraction\":0,\"reset_time\":\"2099-01-01T00:00:00Z\"}," +
            "{\"pool_index\":1,\"window\":\"5h\",\"remaining_fraction\":0.25,\"reset_time\":\"2000-01-01T00:00:00Z\"}," +
            "{\"pool_index\":1,\"window\":\"weekly\",\"remaining_fraction\":0.5,\"reset_time\":\"2099-01-01T00:00:00Z\"}]}," +
            "{\"account_id\":\"ag:2\",\"provider_id\":\"antigravity\",\"compact_label\":\"AG2\",\"usage_status\":\"unreadable_usage\",\"auth_state\":\"AUTH_REQUIRED\"}," +
            "{\"account_id\":\"ag:3\",\"provider_id\":\"antigravity\",\"compact_label\":\"AG3\",\"context_state\":\"OFFLINE\",\"skipped_reason\":\"CONTEXT_OFFLINE\"}]}";
        LimitsSnapshot ss = Limits.ParseSaiSnapshot(sai, now);
        Check(ss != null && ss.Accounts.Count == 3, "SAI Accounts snapshot parses");
        LimitWindow p0five = ss.Accounts[0].Windows.Find(x => x.Base == "five_hour" && x.Group == "pool1");
        Check(p0five != null && p0five.GatedBy.Length > 0 && p0five.Remaining == 0, "a disabled 5h in a spent pool is locked, not 100% free");
        LimitWindow p1five = ss.Accounts[0].Windows.Find(x => x.Base == "five_hour" && x.Group == "pool2");
        Check(p1five != null && p1five.Remaining == 100 && p1five.AssumedFull, "an elapsed reset reads full (assumed)");
        Check(Limits.State(ss.Accounts[1], false) == "SIGN_IN" && ss.Accounts[1].Windows.Count == 0, "unauthenticated AG reads SIGN IN, no 0%");
        Check(Limits.State(ss.Accounts[2], false) == "OFFLINE", "offline AG reads OFFLINE, no 0%");
        string oldSnapshot = "{\"schema\":\"sai.accounts/snapshot/1\",\"checked_at\":\""
            + DateTime.UtcNow.AddHours(-1).ToString("o") + "\",\"accounts\":[]}";
        LimitsSnapshot oldSai = Limits.ParseSaiSnapshot(oldSnapshot, now);
        Check(oldSai != null && oldSai.CheckedAtEpoch < now - 3500,
            "a one-hour-old account snapshot keeps its source timestamp instead of becoming freshly checked");
        string invalidSnapshot = "{\"schema\":\"sai.accounts/snapshot/1\",\"accounts\":[{\"provider_id\":\"antigravity\","
            + "\"account_id\":\"a\",\"usage_status\":\"authenticated_usage\",\"windows\":["
            + "{\"window\":\"5h\",\"remaining_fraction\":-0.2},{\"window\":\"weekly\",\"remaining_fraction\":1.5}]}]}";
        LimitsSnapshot invalidSai = Limits.ParseSaiSnapshot(invalidSnapshot, now);
        Check(invalidSai != null && invalidSai.Accounts[0].Windows.TrueForAll(x => !x.Available)
            && !invalidSai.Accounts[0].HasQuota, "out-of-range fractions are unavailable rather than invented 0% or 100% readings");
        Check(!Limits.IsFresh(oldSai, now, 720), "reading an old source file does not make its contents fresh");
        Check(!Limits.IsFresh(new LimitsSnapshot { CheckedAtEpoch = 0 }, now, 720)
            && !Limits.IsFresh(new LimitsSnapshot { CheckedAtEpoch = now + 3600 }, now, 720)
            && !Limits.IsFresh(new LimitsSnapshot { CheckedAtEpoch = double.NaN }, now, 720),
            "missing, future and non-finite timestamps cannot claim fresh metrics");
        var invalidLimi = Limits.ParseLimisaw(Doc(now, Acc("bad", "claude", "bad", "", true, "",
            Win("five_hour", "five_hour", "", true, -5, null), Win("weekly", "weekly", "", true, 105, null))), "LIMISAW");
        Check(invalidLimi.Accounts[0].Windows.TrueForAll(x => !x.Available),
            "the normalized source also rejects invalid percentages instead of clamping them");
        var rejectedAuth = Limits.ParseSaiSnapshot(invalidSnapshot.Replace("authenticated_usage", "auth_required").Replace("-0.2", "0.5").Replace("1.5", "0.9"), now);
        Check(rejectedAuth.Accounts[0].Windows.TrueForAll(x => !x.Available) && !rejectedAuth.Accounts[0].HasQuota,
            "a failed authenticated probe cannot offer spendable windows even when it carries numeric fields");

        // ---- carry: a failed sweep keeps the last good numbers, marked ----------
        LimitsSnapshot prev = Limits.ParseLimisaw(Doc(now, Acc("claude/aaa", "claude", "aaa", "", true, "", Win("five_hour", "five_hour", "", true, 70, now + 100))), "LIMISAW");
        LimitsSnapshot failed = Limits.ParseLimisaw(Doc(now, Acc("claude/aaa", "claude", "aaa", "", false, "\"error\":\"timeout\"")), "LIMISAW");
        Limits.Carry(failed, prev, now);
        Check(failed.Accounts[0].Carried && failed.Accounts[0].Windows.Count == 1 && failed.Accounts[0].Windows[0].Remaining == 70
            && Limits.State(failed.Accounts[0], false) == "STALE", "a failed read keeps the last good window, marked STALE");

        // ---- T-267 5h keeper decisions (ZAICODE quotaAutoPing rules) -----------
        Func<string, double?, int, LimitAccount> five = (prov, reset, rem) => new LimitAccount
        {
            Provider = prov, Ok = true,
            Windows = new List<LimitWindow> {
                new LimitWindow { Base = "five_hour", Available = true, Remaining = rem, ResetEpoch = reset },
                new LimitWindow { Base = "weekly", Available = true, Remaining = 50, ResetEpoch = now + 86400 } }
        };
        var st = new KeeperAccountState();
        Check(FiveHourKeeper.Decide(five("claude", now + 3600, 60), st, now) == "5h window is running", "Claude: no ping while the 5h window runs");
        Check(FiveHourKeeper.Decide(five("claude", now - 10, 100), st, now) == "", "Claude: ping once the 5h reset has passed");
        FiveHourKeeper.RecordPing(st, five("claude", now - 10, 100), true, now, "ok");
        Check(FiveHourKeeper.Decide(five("claude", now - 10, 100), st, now + 120) == "already pinged for this reset", "Claude: one ping per reset");
        var stNull = new KeeperAccountState { LastResetEpoch = now - 50 };
        Check(FiveHourKeeper.Decide(five("claude", null, 100), stNull, now) == "", "Claude: an idle window with no reset time uses the reset it last saw");
        var stc = new KeeperAccountState();
        Check(FiveHourKeeper.Decide(five("codex", now + 5 * 3600, 100), stc, now) == "waiting for a second reading", "Codex: first reading only records");
        Check(FiveHourKeeper.Decide(five("codex", now + 5 * 3600 + 300, 100), stc, now + 300) == "", "Codex: a sliding reset means an idle window -> ping");
        var stc2 = new KeeperAccountState { LastResetEpoch = now + 5 * 3600 };
        Check(FiveHourKeeper.Decide(five("codex", now + 5 * 3600, 70), stc2, now + 300) == "5h window is running", "Codex: a fixed reset means the window runs -> no ping");
        var weeklySpent = five("codex", now + 5 * 3600 + 400, 100);
        weeklySpent.Windows[1].Remaining = 0;
        var stc3 = new KeeperAccountState { LastResetEpoch = now + 5 * 3600 };
        Check(FiveHourKeeper.Decide(weeklySpent, stc3, now + 400) == "a longer window is spent", "never ping while the weekly is spent");
        var stf = new KeeperAccountState { LastFailureAt = now - 60 };
        Check(FiveHourKeeper.Decide(five("claude", now - 10, 100), stf, now) == "cooling down after a failed ping", "15 minutes of cool-down after a failed ping");
        Check(FiveHourKeeper.Decide(five("antigravity", now - 10, 100), new KeeperAccountState(), now) == "unsupported", "Antigravity is not pinged");
        var stale = five("claude", now - 10, 100); stale.Carried = true;
        Check(FiveHourKeeper.Decide(stale, new KeeperAccountState(), now) == "no fresh reading", "never ping from a stale reading");
        ProcessStartInfo cl = FiveHourKeeper.PingCommand("claude", @"C:\p\.claude-x", "claude.exe");
        Check(cl != null && cl.EnvironmentVariables["CLAUDE_CONFIG_DIR"] == @"C:\p\.claude-x" && cl.Arguments.Contains("--max-turns 1"),
            "Claude ping runs the vendor CLI in the account's own profile directory", cl == null ? "null" : cl.Arguments);

        // ---- 13/14. settings persist under LIMISAW's names and meanings ----------
        string dir = Path.Combine(Path.GetTempPath(), "saituls-limits-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        string cacheOverrideBefore = Environment.GetEnvironmentVariable("SAITULS_LIMITS_DIR");
        Environment.SetEnvironmentVariable("SAITULS_LIMITS_DIR", Path.Combine(dir, "cache"));
        try
        {
            string cacheDir = Path.Combine(dir, "cache");
            Directory.CreateDirectory(cacheDir);
            File.WriteAllText(Path.Combine(cacheDir, "snapshot.json"), Doc(now - 7200));
            double cacheStamp = Math.Floor(now - 200);
            File.WriteAllText(Path.Combine(cacheDir, "saituls-export.json"), Doc(cacheStamp));
            LimitsSnapshot cached = Limits.ReadCache(cacheDir, now);
            Check(cached != null && cached.CheckedAtEpoch == cacheStamp,
                "startup reads the newest existing canonical export without assigning it a new timestamp");
            File.WriteAllText(Path.Combine(cacheDir, "snapshot.json"), Doc(now + 3600));
            cached = Limits.ReadCache(cacheDir, now);
            Check(cached != null && cached.CheckedAtEpoch == cacheStamp,
                "a future-dated published file cannot replace the valid startup cache");
            File.WriteAllText(Path.Combine(cacheDir, "saituls-export.json"), Doc(now - 7200));
            cached = Limits.ReadCache(cacheDir, now);
            Check(cached != null && !Limits.IsFresh(cached, now, 720),
                "an old startup cache remains old and is displayed with stale status");
            File.WriteAllText(Path.Combine(cacheDir, "snapshot.json"), "invalid JSON");
            File.WriteAllText(Path.Combine(cacheDir, "saituls-export.json"), "{\"schema\":\"other\"}");
            Check(Limits.ReadCache(cacheDir, now) == null,
                "malformed or unsupported startup caches cannot create quota readings");
            File.WriteAllText(Path.Combine(dir, "LIMISAW.ini"), "[limisaw]\r\nShowUsed=1\r\nTrayMetric=any\r\nHideSpentAccounts=1\r\nOnlyWithFiveHour=0\r\n");
            var s = new SaitulsSettings(dir);
            s.Load();
            s.LimitsHidden = "codex/local-only";
            s.SeedLimitsFrom(Path.Combine(dir, "LIMISAW.ini"));
            Check(s.LimitsShowUsed && s.LimitsMetric == "any" && s.LimitsHideSpent && !s.LimitsOnlyFiveHour && s.LimitsHidden == "codex/local-only",
                "first run seeds LIMISAW presentation while preserving existing local account hides");
            s.LimitsShowUsed = false; s.LimitsHidden = "claude/aaa|zcode/z"; s.KeepFiveHour = "claude:profile-dir:aa"; s.Language = "ru";
            s.Save();
            var r = new SaitulsSettings(dir); r.Load();
            Check(!r.LimitsShowUsed && r.LimitsMetric == "any" && r.LimitsHidden == "claude/aaa|zcode/z" && r.KeepFiveHour == "claude:profile-dir:aa" && r.Language == "ru",
                "limits display settings, 5h keeper choice and language persist");
            r.SeedLimitsFrom(Path.Combine(dir, "LIMISAW.ini"));
            Check(!r.LimitsShowUsed, "the LIMISAW seed runs once and never overrides a later choice");
            r.LimitsMetric = g5.MetricId; r.Save();
            var pinnedSettings = new SaitulsSettings(dir); pinnedSettings.Load();
            Check(pinnedSettings.LimitsMetric == g5.MetricId, "a manual metric pin survives settings save and reload");
            pinnedSettings.LimitsReadingHidden = "a/5h|a/5h|absent/week";
            pinnedSettings.LimitsReadingOrder = "absent/week|b/5h";
            LimitsPresentation preferences = LimitsPresentation.FromSettings(pinnedSettings);
            LimitsPresentation parsedPreferences = LimitsPresentation.Parse(preferences.ToJson());
            Check(parsedPreferences != null && parsedPreferences.Metric == g5.MetricId
                && parsedPreferences.HiddenReadings.Count == 2 && parsedPreferences.HiddenReadings.Contains("absent/week")
                && parsedPreferences.ReadingOrder[0] == "absent/week" && parsedPreferences.HiddenAccounts.Count == 2,
                "the neutral preferences contract preserves a manual pin, local hides and unavailable ordered IDs");
            Check(LimitsPresentation.Parse("{\"schema\":\"sai.limits/presentation/2\"}") == null
                && LimitsPresentation.Parse("{\"schema\":\"sai.limits/presentation/1\",\"number_mode\":\"other\"}") == null
                && LimitsPresentation.Parse("{\"schema\":\"sai.limits/presentation/1\",\"hide_spent_accounts\":\"false\"}") == null
                && LimitsPresentation.Parse("{\"schema\":\"sai.limits/presentation/1\",\"reading_order\":[17]}") == null,
                "unknown contract versions and invalid known-field types cannot become saved display preferences");
            var minimalPreferences = LimitsPresentation.Parse("{\"schema\":\"sai.limits/presentation/1\",\"future_field\":true}");
            Check(minimalPreferences != null && !minimalPreferences.ShowUsed && minimalPreferences.Metric == "lowest"
                && minimalPreferences.HiddenReadings.Count == 0, "omitted preferences use canonical defaults and future fields are harmless");
            parsedPreferences.ShowUsed = true;
            parsedPreferences.ApplyTo(pinnedSettings);
            var diskBeforeSave = new SaitulsSettings(dir); diskBeforeSave.Load();
            Check(!diskBeforeSave.LimitsShowUsed && pinnedSettings.LimitsShowUsed && pinnedSettings.LimitsMetric == g5.MetricId,
                "applying a neutral model changes presentation only and does not silently write the INI");
            pinnedSettings.Save();
            var diskAfterSave = new SaitulsSettings(dir); diskAfterSave.Load();
            Check(diskAfterSave.LimitsShowUsed && diskAfterSave.LimitsMetric == g5.MetricId
                && diskAfterSave.LimitsReadingHidden == "a/5h|absent/week" && diskAfterSave.Language == "ru",
                "the adapter saves through existing INI keys without changing language or account intent");
            r.LimitsReadingHidden = "a/5h|b/week"; r.LimitsReadingOrder = "b/5h|a/week"; r.Save();
            pinnedSettings.Load();
            Check(pinnedSettings.LimitsReadingHidden == r.LimitsReadingHidden && pinnedSettings.LimitsReadingOrder == r.LimitsReadingOrder,
                "per-reading visibility and ordering persist independently of the account visibility");
            string seedDir = Path.Combine(dir, "seed-pin"); Directory.CreateDirectory(seedDir);
            string seedFile = Path.Combine(seedDir, "LIMISAW.ini");
            File.WriteAllText(seedFile, "[limisaw]\r\nTrayMetric=a/week\r\nTrayHidden=b/5h\r\nTrayItems=a/week|b/5h\r\n");
            var migrated = new SaitulsSettings(seedDir); migrated.Load(); migrated.SeedLimitsFrom(seedFile);
            Check(migrated.LimitsMetric == "a/week" && migrated.LimitsReadingHidden == "b/5h" && migrated.LimitsReadingOrder == "a/week|b/5h",
                "first-run migration keeps LIMISAW's pin, reading hides and explicit order");

            // ---- 15. one language, propagated -------------------------------------
            AccountsLoc.Apply("ru");
            Check(AccountsLoc.T("TabHome") == "Главная" && Environment.GetEnvironmentVariable("SAITULS_LANG") == "ru",
                "language ru applies to the UI and is handed to child tools as SAITULS_LANG");
            AccountsLoc.Apply("en");
            Check(AccountsLoc.T("TabHome") == "Home" && Environment.GetEnvironmentVariable("SAITULS_LANG") == "en", "switching back to English applies at once");

            // ---- 11/12. background refresh cost, with a logging stub engine --------
            string log = Path.Combine(dir, "calls.log");
            string stub = Path.Combine(dir, "sai-accounts-stub.cmd");
            // A1 carries the one identity the 5h keeper must run in: its own CLI
            // profile directory (section 20 pings in exactly that directory).
            string keeperProfile = Path.Combine(dir, ".claude-account1");
            string list = "{\"schema\":\"sai.accounts/list/1\",\"accounts\":[" +
                "{\"account_id\":\"a1\",\"provider_id\":\"claude\",\"display_name\":\"Claude 1\",\"compact_label\":\"CL1\",\"execution_backend\":\"profile_directory\",\"operational_state\":\"ENABLED\",\"hidden\":false,"
                + "\"provider_metadata\":{\"profile_locator\":\"" + keeperProfile.Replace("\\", "\\\\") + "\"}}," +
                "{\"account_id\":\"a2\",\"provider_id\":\"claude\",\"display_name\":\"Claude 2\",\"compact_label\":\"CL2\",\"execution_backend\":\"profile_directory\",\"operational_state\":\"ENABLED\",\"hidden\":true}," +
                "{\"account_id\":\"a3\",\"provider_id\":\"codex\",\"display_name\":\"Codex 1\",\"compact_label\":\"CX1\",\"execution_backend\":\"profile_directory\",\"operational_state\":\"ENABLED\",\"hidden\":false}]}";
            string all = "{\"schema\":\"sai.accounts/status-all/1\",\"accounts\":[" +
                "{\"account_id\":\"a1\",\"compact_label\":\"CL1\",\"display_name\":\"Claude 1\",\"operational_state\":\"ENABLED\",\"hidden\":false,\"context_state\":\"ONLINE\"}," +
                "{\"account_id\":\"a2\",\"compact_label\":\"CL2\",\"display_name\":\"Claude 2\",\"operational_state\":\"ENABLED\",\"hidden\":true,\"context_state\":\"ONLINE\"}," +
                "{\"account_id\":\"a3\",\"compact_label\":\"CX1\",\"display_name\":\"Codex 1\",\"operational_state\":\"ENABLED\",\"hidden\":false,\"context_state\":\"ONLINE\"}]}";
            File.WriteAllText(Path.Combine(dir, "list.json"), list);
            File.WriteAllText(Path.Combine(dir, "all.json"), all);
            string slowFlag = Path.Combine(dir, "slow.flag");
            File.WriteAllText(stub,
                "@echo off\r\n>>\"" + log + "\" echo %*\r\n" +
                "if \"%1\"==\"list\" type \"" + Path.Combine(dir, "list.json") + "\" & exit /b 0\r\n" +
                "if \"%1 %2\"==\"status --all\" (\r\n" +
                "  if exist \"" + slowFlag + "\" ping 127.0.0.1 -n 30 >nul\r\n" +
                "  type \"" + Path.Combine(dir, "all.json") + "\"\r\n" +
                "  exit /b 0\r\n" +
                ")\r\n" +
                "echo {\"status\":\"ok\"}\r\nexit /b 0\r\n");
            Environment.SetEnvironmentVariable("SAI_ACCOUNTS_EXE", stub);
            Environment.SetEnvironmentVariable("SAI_LIMISAW_EXE", Path.Combine(dir, "no-limisaw.exe"));
            var fs = new SaitulsSettings(dir); fs.Load(); fs.RootPath = dir; fs.AccountKeeper = true; fs.LimisawPath = "";
            fs.KeepFiveHour = ""; fs.LimitsHidden = ""; fs.LimitsHideSpent = false; fs.LimitsOnlyFiveHour = false;
            var form = new SaitulsForm(fs, null);
            IntPtr h = form.Handle;
            ((System.Windows.Forms.Timer)Get(form, "_tick")).Stop();
            Call(form, "LoadAccounts");
            Pump(() => false, 300);
            int before = File.Exists(log) ? File.ReadAllLines(log).Length : 0;
            Set(form, "_accountsPollAt", DateTime.MinValue);
            Call(form, "PollStatusTick");
            Pump(() => !(bool)Get(form, "_accountsPolling"), 15000);
            string[] calls = File.Exists(log) ? File.ReadAllLines(log) : new string[0];
            var after = new List<string>(); for (int i = before; i < calls.Length; i++) after.Add(calls[i].Trim());
            Check(after.Count == 1 && after[0] == "status --all", "one refresh = one `status --all` child, no per-row probes", string.Join(" | ", after.ToArray()));
            var rows = (IList)Get(form, "_accounts");
            Check(rows != null && rows.Count == 3 && (string)Get(rows[0], "ContextState") == "ONLINE" && (string)Get(rows[2], "ContextState") == "ONLINE",
                "status --all fills every row");

            // Hidden in the tray, keeper task ON, no 5h keeper armed: a tick starts nothing.
            int beforeTicks = File.ReadAllLines(log).Length;
            int launchesBefore = (int)Get(form, "ChildLaunches");
            for (int i = 0; i < 3; i++) { Call(form, "BackgroundTick"); Pump(() => false, 200); }
            string[] tickCalls = File.ReadAllLines(log);
            var extra = new List<string>(); for (int i = beforeTicks; i < tickCalls.Length; i++) extra.Add(tickCalls[i].Trim());
            Check(extra.Count == 0 && (int)Get(form, "ChildLaunches") == launchesBefore,
                "tray-hidden idle ticks start no child processes", string.Join(" | ", extra.ToArray()));

            // ...but an armed 5h keeper needs fresh readings even while hidden.
            fs.KeepFiveHour = "a1";
            Call(form, "BackgroundTick");
            Pump(() => !(bool)Get(form, "_limitsBusy"), 15000);
            string[] armedCalls = File.ReadAllLines(log);
            Check(armedCalls.Length > beforeTicks && armedCalls[armedCalls.Length - 1].Trim().StartsWith("snapshot"),
                "an armed 5h keeper still reads limits while hidden (one bounded source call)");
            fs.KeepFiveHour = "";
            Set(form, "_limits", null);

            // A paint never starts a child either.
            int beforePaint = File.ReadAllLines(log).Length;
            foreach (string tab in new[] { "Home", "Accounts", "Settings" })
            {
                Set(form, "CurrentTab", tab);
                using (var bmp = new Bitmap(720, 580)) using (Graphics g = Graphics.FromImage(bmp))
                    Call(form, "OnPaint", new PaintEventArgs(g, new Rectangle(0, 0, 720, 580)));
            }
            Pump(() => false, 800);
            Check(File.ReadAllLines(log).Length == beforePaint, "painting Home, Accounts and Settings starts no engine call",
                (File.ReadAllLines(log).Length - beforePaint) + " calls");
            var coldPaint = new SaitulsForm(fs, null);
            IntPtr coldHandle = coldPaint.Handle;
            ((System.Windows.Forms.Timer)Get(coldPaint, "_tick")).Stop();
            coldPaint.ClientSize = new Size(720, 580);
            int beforeColdPaint = File.ReadAllLines(log).Length;
            foreach (string tab in new[] { "Home", "Accounts", "Tools" })
            {
                Set(coldPaint, "CurrentTab", tab);
                using (var bmp = new Bitmap(720, 580)) using (Graphics g = Graphics.FromImage(bmp))
                    Call(coldPaint, "OnPaint", new PaintEventArgs(g, new Rectangle(0, 0, 720, 580)));
            }
            Pump(() => false, 500);
            Check(File.ReadAllLines(log).Length == beforeColdPaint && !(bool)Get(coldPaint, "_mediaProbeStarted")
                && !(bool)Get(coldPaint, "_accountsLoading") && !(bool)Get(coldPaint, "_clipDepsStarted"),
                "a cold paint also starts no account, media or clipboard probes before the background scheduler runs");
            var mediaClock = Stopwatch.StartNew();
            object coldMedia = Call(coldPaint, "CurrentMediaProbe");
            mediaClock.Stop();
            Check(coldMedia == null && mediaClock.ElapsedMilliseconds < 500 && !(bool)Get(coldPaint, "_mediaProbeDone"),
                "a media action uses pending readiness without running version probes on the UI thread");
            Call(coldPaint, "StopBackground"); coldPaint.Dispose();

            // Global hide (plane) and local hide (this app) are separate: CL2 is
            // hidden by the plane and must not be a limits row; a local hide of
            // CX1 must not touch the plane's hidden flag.
            Set(form, "_limits", Limits.ParseLimisaw(Doc(Now(),
                Acc("claude/a", "claude", "a1", "", true, "", Win("five_hour", "five_hour", "", true, 50, Now() + 600)),
                Acc("claude/b", "claude", "a2", "", true, "", Win("five_hour", "five_hour", "", true, 50, Now() + 600)),
                Acc("codex/c", "codex", "a3", "", true, "", Win("five_hour", "five_hour", "", true, 50, Now() + 600))), "LIMISAW"));
            var shown = (List<LimitAccount>)Call(form, "LimitsShown");
            Check(!shown.Exists(a => a.Key == "claude/b") && shown.Exists(a => a.Key == "claude/a"), "a globally hidden account is withdrawn from limits",
                string.Join(",", shown.ConvertAll(a => a.Key + "=" + a.SaiAccountId).ToArray()));
            fs.LimitsHidden = "codex/c";
            shown = (List<LimitAccount>)Call(form, "LimitsShown");
            Check(!shown.Exists(a => a.Key == "codex/c") && !(bool)Get(rows[2], "Hidden"), "a local meter hide does not change the account's global visibility");
            Set(form, "_limits", new LimitsSnapshot { Source = "test", CheckedAtEpoch = Now(), Accounts = selectionAccounts });
            fs.LimitsReadingHidden = ""; fs.LimitsReadingOrder = "";
            int beforePreferences = (int)Get(form, "ChildLaunches");
            Call(form, "PinLimitReading", "a/week");
            Call(form, "ToggleLimitReadingHidden", "b/5h");
            Call(form, "MoveLimitReading", "b/5h", -1);
            var savedPreferences = new SaitulsSettings(dir); savedPreferences.Load();
            Check(savedPreferences.LimitsMetric == "a/week" && SaitulsSettings.SplitList(savedPreferences.LimitsReadingHidden).Contains("b/5h")
                && SaitulsSettings.SplitList(savedPreferences.LimitsReadingOrder)[1] == "b/5h"
                && (int)Get(form, "ChildLaunches") == beforePreferences,
                "normal settings actions persist pin, hide and order without starting a provider probe");

            // Reading management is accessible without pushing every normal
            // preference under a long multi-account reading list on startup.
            form.ClientSize = new Size(600, 460);
            Set(form, "CurrentTab", "Settings");
            Action paintSettings = () => {
                using (var bmp = new Bitmap(600, 460)) using (Graphics g = Graphics.FromImage(bmp))
                    Call(form, "OnPaint", new PaintEventArgs(g, new Rectangle(0, 0, 600, 460)));
            };
            paintSettings();
            var settingsButtons = (IList)Get(form, "Buttons");
            object disclosureButton = null;
            bool filtersVisible = false;
            foreach (object button in settingsButtons)
            {
                string label = (string)Get(button, "Label") ?? "";
                if (label.StartsWith("[+] " + AccountsLoc.T("ReadingSettings"))) disclosureButton = button;
                if (label.EndsWith(AccountsLoc.T("HideSpent")) && !((Rectangle)Get(button, "R")).IsEmpty) filtersVisible = true;
            }
            Check(disclosureButton != null && !((Rectangle)Get(disclosureButton, "R")).IsEmpty && filtersVisible,
                "minimum-size Settings exposes reading management and account filters before opening the long list");
            ((Action)Get(disclosureButton, "A"))();
            paintSettings();
            settingsButtons = (IList)Get(form, "Buttons");
            bool pinVisible = false;
            foreach (object button in settingsButtons)
            {
                string label = (string)Get(button, "Label") ?? "";
                if (label.EndsWith(AccountsLoc.T("PinReading")) && !((Rectangle)Get(button, "R")).IsEmpty) pinVisible = true;
            }
            Check(pinVisible && (int)Get(form, "ChildLaunches") == beforePreferences && fs.LimitsMetric == "a/week",
                "opening the reading list exposes pin actions without probing providers or changing the saved pin");

            // ---- 17. layout: the minimum size fits 1366x768 at 150% scaling ---------
            Check(form.MinimumSize.Width <= 1366 * 100 / 150 && form.MinimumSize.Height <= (768 - 40) * 100 / 150,
                "minimum window fits a 1366x768 screen at 150% (logical " + form.MinimumSize + ")");
            // ---- 17b. discriminating background process lifecycle test -------------
            // 1. start a deliberately slow fake `status --all`;
            // 2. close/stop the background subsystem;
            // 3. prove the child is terminated;
            // 4. prove `_trackedChildren` drains to zero;
            // 5. prove `ChildLaunches` includes the status child;
            // 6. prove no provider/IDE process is touched.
            File.WriteAllText(slowFlag, "1");
            int launchesBeforeSlow = (int)Get(form, "ChildLaunches");
            Set(form, "_accountsPollAt", DateTime.MinValue);
            Set(form, "_statusAllSupported", (bool?)true);

            // Independent process simulating a provider or IDE
            var psiExternal = new ProcessStartInfo("cmd.exe", "/c ping 127.0.0.1 -n 30 >nul")
            {
                CreateNoWindow = true, UseShellExecute = false
            };
            Process externalIde = Process.Start(psiExternal);

            Call(form, "PollStatusTick");
            Process probeChild = null;
            Pump(() => {
                var tracked = (List<Process>)Get(form, "_trackedChildren");
                lock (tracked)
                {
                    if (tracked.Count > 0) { probeChild = tracked[0]; return true; }
                }
                return false;
            }, 10000);

            Check(probeChild != null, "slow status --all child was launched and tracked in _trackedChildren");
            Check((int)Get(form, "ChildLaunches") > launchesBeforeSlow, "ChildLaunches includes the status child");

            var mediaDone = new ManualResetEvent(false);
            ThreadPool.QueueUserWorkItem(delegate {
                try { Call(form, "ProbeMediaTracked", Path.Combine(Environment.SystemDirectory, "WindowsPowerShell", "v1.0", "powershell.exe"), "-NoProfile -Command \"Start-Sleep -Seconds 30\"", 0L, 40000); }
                finally { mediaDone.Set(); }
            });
            Pump(() => {
                var tracked = (List<Process>)Get(form, "_trackedChildren");
                lock (tracked) return tracked.Count >= 2;
            }, 5000);
            var mediaTracked = (List<Process>)Get(form, "_trackedChildren");
            lock (mediaTracked) Check(mediaTracked.Count >= 2, "media readiness children share the cancellable status-probe lifecycle");

            Call(form, "StopBackground");
            Pump(() => {
                var tracked = (List<Process>)Get(form, "_trackedChildren");
                lock (tracked) return tracked.Count == 0 && (probeChild == null || probeChild.HasExited);
            }, 5000);

            Check(probeChild != null && probeChild.HasExited, "closing/stopping background terminates the status child");
            var remainingTracked = (List<Process>)Get(form, "_trackedChildren");
            Check(remainingTracked != null && remainingTracked.Count == 0, "_trackedChildren drains to zero on background stop");
            Pump(() => mediaDone.WaitOne(0), 5000);
            Check(mediaDone.WaitOne(0), "closing the form releases an in-flight media readiness worker");
            if (mediaDone.WaitOne(0)) mediaDone.Dispose();
            Check(externalIde != null && !externalIde.HasExited, "background stop does NOT touch provider/IDE processes");
            if (externalIde != null && !externalIde.HasExited)
            {
                try { externalIde.Kill(); externalIde.Dispose(); } catch { }
            }
            if (File.Exists(slowFlag)) File.Delete(slowFlag);

            form.Dispose();

            // An intentionally slow first-party command exercises the real UI
            // action path without accessing the operator's clipboard or resident.
            string clipRoot = Path.Combine(dir, "slow-clipboard");
            string clipMutexBefore = Environment.GetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_MUTEX");
            string clipStopBefore = Environment.GetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_STOP_EVENT");
            string clipStateBefore = Environment.GetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_STATE_DIR");
            Environment.SetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_MUTEX", @"Local\SaitulsClipboardPlusUnit-" + Guid.NewGuid().ToString("N"));
            Environment.SetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_STOP_EVENT", @"Local\SaitulsClipboardPlusUnitStop-" + Guid.NewGuid().ToString("N"));
            Environment.SetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_STATE_DIR", clipRoot);
            string clipScriptDir = Path.Combine(clipRoot, "Scripts", "saipatch");
            Directory.CreateDirectory(clipScriptDir);
            string clipCalls = Path.Combine(clipRoot, "calls.txt");
            File.WriteAllText(Path.Combine(clipScriptDir, "clipboard+.pyw"),
                "import pathlib, sys, time\n" +
                "root = pathlib.Path(__file__).parents[2]\n" +
                "with (root / 'calls.txt').open('a') as f: f.write(sys.argv[1] + '\\n')\n" +
                "if sys.argv[1] == '--check-deps': time.sleep(2)\n" +
                "elif sys.argv[1] != '--start': time.sleep(20)\n");
            var clipSettings = new SaitulsSettings(clipRoot); clipSettings.Load();
            var clipForm = new SaitulsForm(clipSettings, null);
            IntPtr clipHandle = clipForm.Handle;
            ((System.Windows.Forms.Timer)Get(clipForm, "_tick")).Stop();
            var startClock = Stopwatch.StartNew();
            Call(clipForm, "ClipboardStartAction");
            startClock.Stop();
            Check(startClock.ElapsedMilliseconds < 500,
                "Clipboard+ Start returns while a slow dependency check runs", startClock.ElapsedMilliseconds + "ms");
            Call(clipForm, "ClipboardStartAction");
            Call(clipForm, "ClipboardStopAction");
            Pump(() => (bool)Get(clipForm, "_clipDepsDone"), 10000);
            string clipLog = File.Exists(clipCalls) ? File.ReadAllText(clipCalls) : "";
            Check(clipLog.Split(new[] { "--check-deps" }, StringSplitOptions.None).Length == 2,
                "repeated Start shares one in-flight readiness check", clipLog);
            Check(!clipLog.Contains("--start"),
                "Stop cancels a queued start before dependencies finish", clipLog);
            // The fake one-shot sleeps without touching a real clipboard.
            var copyClock = Stopwatch.StartNew();
            Call(clipForm, "ClipboardSafeCopyAction");
            copyClock.Stop();
            Check(copyClock.ElapsedMilliseconds < 500,
                "Clipboard+ Safe Copy returns while the command is pending", copyClock.ElapsedMilliseconds + "ms");
            Process clipChild = null;
            Pump(() => {
                var tracked = (List<Process>)Get(clipForm, "_trackedChildren");
                lock (tracked) { if (tracked.Count > 0) clipChild = tracked[0]; }
                return clipChild != null && File.ReadAllText(clipCalls).Contains("--sanitize-text-once");
            }, 5000);
            int copyLaunches = (int)Get(clipForm, "ChildLaunches");
            Call(clipForm, "ClipboardSafeCopyAction");
            Check(clipChild != null && (int)Get(clipForm, "ChildLaunches") == copyLaunches,
                "repeated Safe Copy does not launch overlapping clipboard commands");
            Call(clipForm, "StopBackground");
            Pump(() => clipChild != null && clipChild.HasExited, 5000);
            Check(clipChild != null && clipChild.HasExited,
                "closing cancels an in-flight Clipboard+ one-shot command");
            clipForm.Dispose();
            Environment.SetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_MUTEX", clipMutexBefore);
            Environment.SetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_STOP_EVENT", clipStopBefore);
            Environment.SetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_STATE_DIR", clipStateBefore);

            // Profiles after the fourth must remain selectable, and scrolling
            // must include the text below the final action button.
            var faBefore = Saituls.Program.Fa;
            var faTest = new Saituls.FolderAutomation.FaEngine(
                new Saituls.FolderAutomation.SystemFileSystem(),
                new Saituls.FolderAutomation.FaLog(Path.Combine(dir, "fa-test.log")),
                new Saituls.FolderAutomation.FaJournal(Path.Combine(dir, "fa-journal")));
            for (int i = 1; i <= 10; i++) faTest.Config.Profiles.Add(new Saituls.FolderAutomation.FaProfile { Id = "profile-" + i, Name = "Profile " + i });
            Saituls.Program.Fa = faTest;
            var faForm = new SaitulsForm(fs, null);
            IntPtr faHandle = faForm.Handle;
            ((System.Windows.Forms.Timer)Get(faForm, "_tick")).Stop();
            faForm.ClientSize = new Size(600, 460);
            Set(faForm, "CurrentTab", "Organizer");
            using (var bmp = new Bitmap(600, 460)) using (Graphics g = Graphics.FromImage(bmp))
                Call(faForm, "OnPaint", new PaintEventArgs(g, new Rectangle(0, 0, 600, 460)));
            foreach (object button in (IList)Get(faForm, "Buttons"))
            {
                var rect = (Rectangle)Get(button, "R");
                if (rect.Width == 250 && rect.Height == 20) ((Action)Get(button, "A"))();
            }
            Check((string)Get(faForm, "_faSelected") == "profile-10",
                "Organizer keeps profiles after the fourth reachable");
            var faHeights = (Dictionary<string, int>)Get(faForm, "_tabContentH");
            Check(faHeights["Organizer"] >= 500,
                "Organizer scrolling includes engine status below the last button", faHeights["Organizer"].ToString());
            Call(faForm, "StopBackground"); faForm.Dispose();
            Saituls.Program.Fa = faBefore;
            faTest.Dispose();

            // Editing an existing condition must preserve its operator. These
            // are model-only saves: no real automation configuration is written.
            string faLanguageBefore = AccountsLoc.Setting;
            AccountsLoc.Apply("ru");
            var editedCondition = new Saituls.FolderAutomation.FaCondition { Kind = "regex", Op = "matches", Value = "\\.txt$" };
            using (var conditionDialog = new Saituls.FolderAutomation.FaConditionForm(editedCondition))
            {
                conditionDialog.Show(); Application.DoEvents();
                Check(conditionDialog.Text == "Изменить условие",
                    "Organizer condition dialog follows the main Russian language choice", conditionDialog.Text);
                ((Button)conditionDialog.AcceptButton).PerformClick();
                Check(conditionDialog.Condition.Kind == "regex" && conditionDialog.Condition.Op == "matches"
                    && conditionDialog.Condition.Value == "\\.txt$",
                    "saving an unchanged condition preserves its canonical kind, operator and value", conditionDialog.Condition.Op);
            }
            var editedRule = new Saituls.FolderAutomation.FaRule { Id = "dialog-rule", Name = "kept rule", Match = Saituls.FolderAutomation.FaMatch.Exclude, Action = Saituls.FolderAutomation.FaAction.Copy, Destination = Path.Combine(dir, "dialog-destination") };
            editedRule.Conditions.Add(editedCondition);
            using (var ruleDialog = new Saituls.FolderAutomation.FaRuleEditorForm(editedRule))
            {
                ruleDialog.Show(); Application.DoEvents();
                Check(ruleDialog.Text == "Изменить правило", "Organizer rule dialog follows the main language choice", ruleDialog.Text);
                ((Button)ruleDialog.AcceptButton).PerformClick();
                Check(ruleDialog.Rule.Match == Saituls.FolderAutomation.FaMatch.Exclude && ruleDialog.Rule.Action == Saituls.FolderAutomation.FaAction.Copy
                    && ruleDialog.Rule.Name == "kept rule" && ruleDialog.Rule.Conditions[0].Op == "matches",
                    "translated rule choices preserve canonical actions, matching and user data");
            }
            var editedProfile = new Saituls.FolderAutomation.FaProfile { Id = "dialog-only", Name = "kept profile", Enabled = false };
            editedProfile.Sources.Add(dir);
            editedProfile.Rules.Add(editedRule);
            editedProfile.Schedule.Trigger = Saituls.FolderAutomation.FaTrigger.Manual;
            using (var profileDialog = new Saituls.FolderAutomation.FaProfileEditorForm(editedProfile, new Saituls.FolderAutomation.FaConfig()))
            {
                profileDialog.Show(); Application.DoEvents();
                Check(profileDialog.Text == "Изменить профиль — kept profile", "Organizer profile dialog follows the main language choice", profileDialog.Text);
                bool manualLabelSelected = false;
                foreach (Control profileChild in profileDialog.Controls)
                {
                    var profileTabs = profileChild as TabControl;
                    if (profileTabs == null) continue;
                    foreach (TabPage page in profileTabs.TabPages)
                        foreach (Control child in page.Controls)
                        {
                            var combo = child as ComboBox;
                            if (combo != null && combo.Items.Contains("Только вручную"))
                            {
                                manualLabelSelected = (string)combo.SelectedItem == "Только вручную";
                                combo.SelectedItem = "Только вручную";
                            }
                        }
                }
                Check(manualLabelSelected, "an existing manual profile is displayed as manual in the translated schedule");
                ((Button)profileDialog.AcceptButton).PerformClick();
                Check(profileDialog.DialogResult == DialogResult.OK && profileDialog.Profile.Name == "kept profile"
                    && profileDialog.Profile.Sources.Count == 1 && profileDialog.Profile.Sources[0] == dir
                    && profileDialog.Profile.Rules[0].Action == Saituls.FolderAutomation.FaAction.Copy,
                    "translated profile save preserves paths and rules without writing operator configuration");
                Check(profileDialog.Profile.Schedule.Trigger == editedProfile.Schedule.Trigger
                    && profileDialog.Profile.Schedule.Missed == editedProfile.Schedule.Missed,
                    "choosing the translated manual option preserves the manual trigger and missed-run policy", profileDialog.Profile.Schedule.Trigger.ToString());
            }
            AccountsLoc.Apply(faLanguageBefore);

            // ---- 19. T-266 A: the one localization boundary -------------------------
            // Canonical bundles override the compatibility EN/RU tables. Any
            // locale other than the two compatibility languages uses its bundle and
            // falls back to English for everything the bundle does not carry; an
            // absent, unreadable or malformed bundle is "no bundle" -- English
            // answers and nothing blocks the app from starting.
            string baseDir = AppDomain.CurrentDomain.BaseDirectory;
            string bundleDir = Path.Combine(baseDir, "i18n", "strings");
            bool ownedBundleDir = !Directory.Exists(bundleDir);
            Directory.CreateDirectory(bundleDir);
            string xxkFile = Path.Combine(bundleDir, "strings.xxk.json");
            string xxmFile = Path.Combine(bundleDir, "strings.xxm.json");
            File.WriteAllText(xxkFile, "{\"TabHome\":\"Kodu\",\"Keep5h\":\"Hoia 5h aken lahti\",\"Bad\":12}");
            typeof(AccountsLoc).GetField("_choices", Any).SetValue(null, null);
            AccountsLoc.Apply("xxk");
            Check(AccountsLoc.T("TabHome") == "Kodu" && AccountsLoc.T("Keep5h").Length > 0,
                "a locale with a bundle is served from the canonical file, not from a copy inside the app");
            Check(AccountsLoc.T("TabAccounts") == "Accounts",
                "a key the bundle does not carry answers in English", AccountsLoc.T("TabAccounts"));
            Check(AccountsLoc.T("NoSuchKeyAtAll") == "NoSuchKeyAtAll",
                "an unknown key answers with its own name, never with invented text", AccountsLoc.T("NoSuchKeyAtAll"));
            File.WriteAllText(xxmFile, "{\"TabHome\": ");
            AccountsLoc.Apply("xxm");
            Check(AccountsLoc.T("TabHome") == "Home", "a malformed bundle is no bundle: English answers", AccountsLoc.T("TabHome"));
            var ls = new SaitulsSettings(dir); ls.Load(); ls.RootPath = dir; ls.Language = "xxm";
            bool lOk = true; string lWhy = "";
            try
            {
                var lf = new SaitulsForm(ls, null);
                IntPtr hlf = lf.Handle;
                ((System.Windows.Forms.Timer)Get(lf, "_tick")).Stop();
                foreach (string tab in new[] { "Home", "Settings" })
                {
                    Set(lf, "CurrentTab", tab);
                    using (var bmp = new Bitmap(700, 560)) using (Graphics g = Graphics.FromImage(bmp))
                        Call(lf, "OnPaint", new PaintEventArgs(g, new Rectangle(0, 0, 700, 560)));
                }
                Call(lf, "StopBackground");
                lf.Dispose();
            }
            catch (Exception ex) { lOk = false; lWhy = (ex.InnerException ?? ex).GetType().Name + ": " + (ex.InnerException ?? ex).Message; }
            Check(lOk, "a malformed bundle never keeps the app from starting: Home and Settings still paint", lWhy);
            AccountsLoc.Apply("xxn");
            Check(AccountsLoc.T("TabHome") == "Home" && AccountsLoc.T("TabSettings") == "Settings",
                "a locale with no bundle at all answers in English");
            AccountsLoc.Apply("auto");
            string autoLang = AccountsLoc.CurrentLang;
            Check(Array.IndexOf(AccountsLoc.Choices, autoLang) >= 0 && Environment.GetEnvironmentVariable("SAITULS_LANG") == autoLang,
                "auto follows Windows and hands the resolved language to child tools", autoLang);
            AccountsLoc.Apply("en");
            try { if (File.Exists(xxkFile)) File.Delete(xxkFile); } catch { }
            try { if (File.Exists(xxmFile)) File.Delete(xxmFile); } catch { }
            if (ownedBundleDir) { try { Directory.Delete(Path.Combine(baseDir, "i18n"), true); } catch { } }
            typeof(AccountsLoc).GetField("_choices", Any).SetValue(null, null);
            ((Dictionary<string, Dictionary<string, string>>)typeof(AccountsLoc).GetField("Bundles", Any).GetValue(null)).Clear();

            // ---- 20. T-267: the 5h keeper ------------------------------------------
            // Opt-in per account, Claude and Codex only, always in the account's own
            // profile directory, and its ping is a child THIS window owns. The vendor
            // CLI is a fake built here: it logs the profile directory it was handed
            // and the arguments it was called with, then sleeps or fails on demand.
            // It is put first on PATH as claude.exe, so no real CLI is reachable and
            // no real quota can be spent by this test.
            string fakeDir = Path.Combine(dir, "fake-cli");
            Directory.CreateDirectory(fakeDir);
            string fakeExe = Path.Combine(fakeDir, "claude.exe");
            string fakeLog = Path.Combine(dir, "fake-cli.log");
            string profileDir = keeperProfile;
            Directory.CreateDirectory(profileDir);
            string fakeSrc = Path.Combine(dir, "fakecli.cs");
            File.WriteAllText(fakeSrc,
                "using System;using System.IO;using System.Threading;\r\n" +
                "class FakeCli{static int Main(string[] a){\r\n" +
                " string lg=Environment.GetEnvironmentVariable(\"FAKE_CLI_LOG\");\r\n" +
                " string home=Environment.GetEnvironmentVariable(\"CLAUDE_CONFIG_DIR\");\r\n" +
                " if(home==null)home=Environment.GetEnvironmentVariable(\"CODEX_HOME\");\r\n" +
                " bool help=false;foreach(string x in a)if(x==\"--help\")help=true;\r\n" +
                " try{File.AppendAllText(lg,(home==null?\"\":home)+\";\"+string.Join(\" \",a)+Environment.NewLine);}catch{}\r\n" +
                " if(help){Console.Write(\"usage: claude [options]\\n  --print  --model <model>  --output-format <format>  --max-turns <n>  --safe-mode  --strict-mcp-config  --disable-slash-commands  --no-session-persistence  --tools <tools>...\");return 0;}\r\n" +
                " int sl=0;int.TryParse(Environment.GetEnvironmentVariable(\"FAKE_CLI_SLEEP\"),out sl);if(sl>0)Thread.Sleep(sl);\r\n" +
                " int ec=0;int.TryParse(Environment.GetEnvironmentVariable(\"FAKE_CLI_EXIT\"),out ec);\r\n" +
                " string rej=Environment.GetEnvironmentVariable(\"FAKE_CLI_REJECT\");\r\n" +
                " if(rej!=null&&rej.Length>0){foreach(string x in a)if(x==rej){Console.Error.Write(\"error: invalid model: \"+x);return 2;}}\r\n" +
                " if(ec==0&&Environment.GetEnvironmentVariable(\"FAKE_CLI_SILENT\")!=\"1\")Console.Write(\"{\\\"type\\\":\\\"result\\\",\\\"is_error\\\":false,\\\"result\\\":\\\"ok\\\"}\");\r\n" +
                " return ec;}}\r\n");
            string cscPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), @"Microsoft.NET\Framework64\v4.0.30319\csc.exe");
            bool built = false; string buildWhy = "";
            if (File.Exists(cscPath))
            {
                try
                {
                    Process cp = Process.Start(new ProcessStartInfo(cscPath,
                        "/nologo /target:exe /out:\"" + fakeExe + "\" \"" + fakeSrc + "\"")
                    { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true });
                    built = cp != null && cp.WaitForExit(60000) && cp.ExitCode == 0;
                    if (!built && cp != null) buildWhy = "exit " + cp.ExitCode;
                }
                catch (Exception ex) { buildWhy = ex.Message; }
            }
            else buildWhy = "no csc";
            Check(built, "the fake vendor CLI builds, so this test can never touch a real CLI or real quota", buildWhy);

            // The fake CLI holds its own log open while it writes: read it
            // with sharing, or the reader races the writer.
            Func<string, List<string>> FakeLog = p => {
                var lines = new List<string>();
                if (!File.Exists(p)) return lines;
                using (var logStream = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                using (var sr = new StreamReader(logStream))
                {
                    string line;
                    while ((line = sr.ReadLine()) != null) lines.Add(line);
                }
                return lines;
            };
            Func<List<string>, string> PingLine = lines => { string found = ""; foreach (string l in lines) if (found.Length == 0 && l.Contains("-p hi")) found = l; return found; };
            Func<Process, bool> Exited = p => { if (p == null) return false; try { return p.HasExited; } catch { return true; } };
            Func<string, double, string> keeperDoc = (loc, n) => Doc(n,
                Acc("claude/keeptest", "claude", "a1", loc, true, "",
                    Win("five_hour", "five_hour", "", true, 100, n - 10),
                    Win("weekly", "weekly", "", true, 50, n + 86400)));

            // (a) the command itself: a windowless, redirected child in the
            //     account's own profile directory, and .cmd shims still work.
            ProcessStartInfo pexe = FiveHourKeeper.PingCommand("claude", @"C:\p\.claude-x", Path.Combine(fakeDir, "claude.exe"));
            ProcessStartInfo pshim = FiveHourKeeper.PingCommand("claude", @"C:\p\.claude-x", Path.Combine(fakeDir, "claude.cmd"));
            Check(pexe != null && pexe.FileName.Equals(Path.Combine(fakeDir, "claude.exe")) && pexe.CreateNoWindow && !pexe.UseShellExecute
                && pexe.EnvironmentVariables["CLAUDE_CONFIG_DIR"] == @"C:\p\.claude-x"
                && pexe.RedirectStandardOutput && pexe.RedirectStandardError && pexe.RedirectStandardInput,
                "a ping is a windowless, redirected child in the account's own profile directory (no console flash)");
            Check(pshim != null && pshim.FileName.Equals(Path.Combine(Environment.SystemDirectory, "cmd.exe"), StringComparison.OrdinalIgnoreCase)
                && pshim.Arguments.StartsWith("/c \"") && pshim.CreateNoWindow,
                "an npm-style .cmd CLI is a shell shim: the ping goes through cmd.exe, still windowless");
            Check(FiveHourKeeper.PingCommand("antigravity", @"C:\p\x", "ag.exe") == null
                && FiveHourKeeper.PingCommand("claude", "", "claude.exe") == null,
                "no ping is built for Antigravity, nor without a profile directory");

            string pathBefore = Environment.GetEnvironmentVariable("PATH");
            Environment.SetEnvironmentVariable("PATH", fakeDir + ";" + pathBefore);
            Environment.SetEnvironmentVariable("FAKE_CLI_LOG", fakeLog);
            Environment.SetEnvironmentVariable("FAKE_CLI_SLEEP", "0");
            Environment.SetEnvironmentVariable("FAKE_CLI_EXIT", "0");

            // (b) off by default: nothing armed, nothing pinged
            fs.KeepFiveHour = ""; fs.KeepFiveHourState = "";
            var fk1 = new SaitulsForm(fs, null);
            IntPtr hk1 = fk1.Handle;
            ((System.Windows.Forms.Timer)Get(fk1, "_tick")).Stop();
            Call(fk1, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk1, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            int logBeforeOff = FakeLog(fakeLog).Count;
            int launchesBeforeOff = (int)Get(fk1, "ChildLaunches");
            Call(fk1, "KeepFiveHourTick");
            Pump(() => false, 600);
            Check(FakeLog(fakeLog).Count == logBeforeOff && (int)Get(fk1, "ChildLaunches") == launchesBeforeOff,
                "the 5h keeper is off by default: no account armed, no ping, no child");
            fk1.Dispose();

            // (c) armed, reset passed: exactly one ping child, in the account's own
            //     profile, and closing the window kills that child
            fs.KeepFiveHour = "a1"; fs.KeepFiveHourState = "";
            Environment.SetEnvironmentVariable("FAKE_CLI_SLEEP", "60000");
            var fk2 = new SaitulsForm(fs, null);
            IntPtr hk2 = fk2.Handle;
            ((System.Windows.Forms.Timer)Get(fk2, "_tick")).Stop();
            Call(fk2, "LoadAccounts");
            Pump(() => false, 400);
            int beforeStalePing = (int)Get(fk2, "ChildLaunches");
            int staleLogCount = FakeLog(fakeLog).Count;
            foreach (double timestamp in new[] { 0.0, now - 7200, now + 3600 })
            {
                var staleSnapshot = Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW");
                staleSnapshot.CheckedAtEpoch = timestamp;
                Set(fk2, "_limits", staleSnapshot);
                Call(fk2, "KeepFiveHourTick");
                Pump(() => false, 100);
            }
            Check((int)Get(fk2, "ChildLaunches") == beforeStalePing && FakeLog(fakeLog).Count == staleLogCount,
                "an armed keeper sends no request from old, missing or future snapshot timestamps");
            Set(fk2, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            Check(File.Exists(fakeExe) && (string)Call(fk2, "CliOnPath", "claude") == fakeExe,
                "the CLI resolved for the ping is this test's fake, not a real vendor CLI");
            int launchesBeforePing = (int)Get(fk2, "ChildLaunches");
            Call(fk2, "KeepFiveHourTick");
            var pingLog = new List<string>();
            Pump(() => { pingLog = FakeLog(fakeLog); return PingLine(pingLog).Length > 0; }, 15000);
            Process pingChild = null;
            Pump(() => {
                var tracked = (List<Process>)Get(fk2, "_trackedChildren");
                lock (tracked) { if (tracked.Count > 0) { pingChild = tracked[tracked.Count - 1]; return true; } }
                return false;
            }, 5000);
            Check(pingChild != null && (int)Get(fk2, "ChildLaunches") > launchesBeforePing,
                "an armed account whose 5h reset has passed starts one ping child");
            string pingLine = PingLine(pingLog);
            Check(pingLine.Length > 0 && pingLine.StartsWith(profileDir, StringComparison.OrdinalIgnoreCase)
                && pingLine.Contains("--max-turns 1") && pingLine.Contains("-p hi")
                && pingLine.Contains("--model haiku") && !pingLine.Contains("claude-haiku-4-5-20251001")
                && pingLine.Contains("--tools") && pingLine.Contains("--safe-mode"),
                "the ping runs the vendor CLI in the account's exact profile directory, with the CLI's own model family and no tools",
                string.Join(" | ", pingLog.ToArray()));
            Call(fk2, "StopBackground");
            Pump(() => {
                var tracked = (List<Process>)Get(fk2, "_trackedChildren");
                lock (tracked) return tracked.Count == 0;
            }, 5000);
            Check(Exited(pingChild), "exit cancels the keeper's own in-flight ping child");
            Pump(() => !(bool)Get(fk2, "_keepBusy"), 20000);
            Check(!(bool)Get(fk2, "_keepBusy"), "a cancelled ping still settles: the keeper never stays busy forever");
            fk2.Dispose();

            // (d) a failed ping is a durable, human-readable sentence -- and it is
            //     not retried while the cool-down holds
            Environment.SetEnvironmentVariable("FAKE_CLI_SLEEP", "0");
            Environment.SetEnvironmentVariable("FAKE_CLI_EXIT", "3");
            fs.KeepFiveHour = "a1"; fs.KeepFiveHourState = "";
            var fk3 = new SaitulsForm(fs, null);
            IntPtr hk3 = fk3.Handle;
            ((System.Windows.Forms.Timer)Get(fk3, "_tick")).Stop();
            Call(fk3, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk3, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            Call(fk3, "KeepFiveHourTick");
            Pump(() => !(bool)Get(fk3, "_keepBusy") && FakeLog(fakeLog).Count > 1, 20000);
            KeeperAccountState stFail = null;
            ((Dictionary<string, KeeperAccountState>)Call(fk3, "KeepStates")).TryGetValue("a1", out stFail);
            Check(stFail != null && stFail.LastResult == "ping failed (exit 3)",
                "a failed ping keeps a human-readable answer", stFail == null ? "no state" : stFail.LastResult);
            string durable = (string)Get(fs, "KeepFiveHourState");
            Check(durable.Contains("ping%20failed"), "the answer is persisted escaped, so its own text cannot break the record", durable);

            // A restart must show the same sentence: same settings file, new app.
            var rs = new SaitulsSettings(dir); rs.Load(); rs.RootPath = dir;
            var fk4 = new SaitulsForm(rs, null);
            IntPtr hk4 = fk4.Handle;
            ((System.Windows.Forms.Timer)Get(fk4, "_tick")).Stop();
            string durableLine = (string)Call(fk4, "KeepLastDurable");
            Check(durableLine.StartsWith("a1") && durableLine.Contains("ping failed (exit 3)"),
                "the last answer is readable in a later session, without a new ping", durableLine);
            fk4.Dispose();
            int beforeCooldown = FakeLog(fakeLog).Count;
            Call(fk3, "KeepFiveHourTick");
            Pump(() => false, 700);
            Check(FakeLog(fakeLog).Count == beforeCooldown, "a failed ping is not retried while the cool-down holds");
            fk3.Dispose();

            // A sentence carrying the record's own separators still round-trips.
            string nasty = "ping failed (exit 1); retry=yes, cooling down 50%";
            rs.KeepFiveHourState = "a1=100,0,0,0," + Uri.EscapeDataString(nasty);
            rs.Save();
            var rs2 = new SaitulsSettings(dir); rs2.Load();
            var fk5 = new SaitulsForm(rs2, null);
            IntPtr hk5 = fk5.Handle;
            ((System.Windows.Forms.Timer)Get(fk5, "_tick")).Stop();
            KeeperAccountState stNasty = null;
            ((Dictionary<string, KeeperAccountState>)Call(fk5, "KeepStates")).TryGetValue("a1", out stNasty);
            Check(stNasty != null && stNasty.LastResult == nasty,
                "an answer containing the record's own separators still round-trips", stNasty == null ? "none" : stNasty.LastResult);
            fk5.Dispose();

            // (e) disabled and frozen accounts are never pinged
            fs.KeepFiveHour = "a1"; fs.KeepFiveHourState = "";
            Environment.SetEnvironmentVariable("FAKE_CLI_EXIT", "0");
            var fk6 = new SaitulsForm(fs, null);
            IntPtr hk6 = fk6.Handle;
            ((System.Windows.Forms.Timer)Get(fk6, "_tick")).Stop();
            Call(fk6, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk6, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            var rows2 = (IList)Get(fk6, "_accounts");
            int logBeforeStates = FakeLog(fakeLog).Count;
            int launchesBeforeStates = (int)Get(fk6, "ChildLaunches");
            Set(rows2[0], "State", "DISABLED");
            Call(fk6, "KeepFiveHourTick");
            Set(rows2[0], "State", "FROZEN");
            Call(fk6, "KeepFiveHourTick");
            Set(rows2[0], "State", "ARCHIVED");
            Call(fk6, "KeepFiveHourTick");
            Pump(() => false, 700);
            Check(FakeLog(fakeLog).Count == logBeforeStates && (int)Get(fk6, "ChildLaunches") == launchesBeforeStates,
                "a disabled, frozen or archived account is never pinged");
            Check(((Dictionary<string, KeeperAccountState>)Call(fk6, "KeepStates")).Count == 0,
                "an account that may not be pinged gets no keeper state either");
            fk6.Dispose();

            // (f) one ping per reset, never one per tick
            fs.KeepFiveHourState = "";
            var fk7 = new SaitulsForm(fs, null);
            IntPtr hk7 = fk7.Handle;
            ((System.Windows.Forms.Timer)Get(fk7, "_tick")).Stop();
            Call(fk7, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk7, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            int logBeforeOnce = FakeLog(fakeLog).Count;
            Call(fk7, "KeepFiveHourTick");
            Pump(() => !(bool)Get(fk7, "_keepBusy") && FakeLog(fakeLog).Count > logBeforeOnce, 20000);
            KeeperAccountState stOk = null;
            ((Dictionary<string, KeeperAccountState>)Call(fk7, "KeepStates")).TryGetValue("a1", out stOk);
            string keepLast = (string)Get(fk7, "_keepLast");
            Check(stOk != null && stOk.LastResult.StartsWith("the request was accepted") && stOk.LastPingedReset > 0
                && stOk.OutcomeConfidence == KeeperOutcomes.AcceptedUnverified && stOk.LastAcceptedAt > 0,
                "a successful ping records the accepted request, without claiming the window started",
                stOk == null ? "none" : stOk.LastResult);
            Check(keepLast.StartsWith("CL1"), "the answer names the account it belongs to", keepLast);
            int afterFirst = FakeLog(fakeLog).Count;
            Call(fk7, "KeepFiveHourTick");
            Pump(() => false, 500);
            Call(fk7, "KeepFiveHourTick");
            Pump(() => false, 500);
            Check(FakeLog(fakeLog).Count == afterFirst, "one ping per reset, never one per tick");
            fk7.Dispose();

            // ---- 21. T-303: the corrected 5h window keeper ----------------------
            // The red case first: an opted-in Claude account with a fresh,
            // valid, FULL 5h quota, no reset time at all, no earlier reset and
            // a full weekly used to be stuck forever, because a reset the
            // vendor only publishes AFTER the first request can never arrive
            // for an account that never got that request.
            var redAccount = new LimitAccount
            {
                Provider = "claude", Ok = true,
                Windows = new List<LimitWindow> {
                    new LimitWindow { Base = "five_hour", Available = true, Remaining = 100, ResetEpoch = null },
                    new LimitWindow { Base = "weekly", Available = true, Remaining = 100, ResetEpoch = now + 86400 } }
            };
            var redState = new KeeperAccountState();
            KeeperDecision red = FiveHourKeeper.DecideState(redAccount, redState, now);
            Check(red.Eligible && red.Initial && red.State == KeeperStates.IdleCandidate,
                "ONE_BOUNDED_INITIAL_PRIMING_ELIGIBLE: a 100% Claude account with no reset may prime once",
                red.State + " / " + red.Reason);
            Check(FiveHourKeeper.DecideState(redAccount, new KeeperAccountState(), now).Eligible
                && !FiveHourKeeper.DecideState(redAccount, new KeeperAccountState { AttemptAt = now - 5, OutcomeConfidence = KeeperOutcomes.UnknownOutcome }, now).Eligible,
                "the one bootstrap is not a licence to send again: an account that already tried is not a fresh candidate");

            // A vendor-confirmed reset is authoritative and is never called an
            // estimate; a predicted one is always ESTIMATED.
            var confirmedAccount = new LimitAccount
            {
                Provider = "claude", Ok = true,
                Windows = new List<LimitWindow> { new LimitWindow { Base = "five_hour", Available = true, Remaining = 100, ResetEpoch = now - 60 } }
            };
            var confState = new KeeperAccountState();
            KeeperDecision conf = FiveHourKeeper.DecideState(confirmedAccount, confState, now);
            Check(conf.Eligible && !conf.ResetEstimated && confState.LastVerifiedReset > 0,
                "a vendor-confirmed reset is authoritative and is never labelled an estimate");
            var estState = new KeeperAccountState
            {
                PrimedOnce = true, LastAcceptedAt = now - 100, AttemptAt = now - 100,
                OutcomeConfidence = KeeperOutcomes.AcceptedUnverified, State = KeeperStates.PrimingAccepted
            };
            KeeperDecision est = FiveHourKeeper.DecideState(redAccount, estState, now);
            Check(!est.Eligible && est.ResetEstimated && Math.Abs(est.EstimatedReset - (now - 100 + 5 * 3600)) < 1,
                "a predicted window end is ESTIMATED, and an unconfirmed window is never pinged again",
                est.State + " / " + est.Reason);
            // The renewal that follows it: when the estimated window has
            // passed, one bounded request is due -- and it is a renewal, not
            // a second bootstrap.
            var renewState = new KeeperAccountState
            {
                PrimedOnce = true, LastAcceptedAt = now - 5 * 3600 - 120, AttemptAt = now - 5 * 3600 - 120,
                OutcomeConfidence = KeeperOutcomes.AcceptedUnverified, State = KeeperStates.PrimingAccepted
            };
            KeeperDecision renew = FiveHourKeeper.DecideState(redAccount, renewState, now);
            Check(renew.Eligible && renew.ResetEstimated && !renew.Initial,
                "a renewal becomes eligible once the ESTIMATED window has passed",
                renew.State + " / " + renew.Reason);
            // The attempt budget is the last fence against a retry storm.
            var budgetState = new KeeperAccountState { Attempts = FiveHourKeeper.MaxAttempts, AttemptAt = now - 4000, OutcomeConfidence = KeeperOutcomes.FailedOutcome };
            Check(FiveHourKeeper.DecideState(redAccount, budgetState, now).State == KeeperStates.Blocked,
                "the attempt budget stops a retry storm: no more than three attempts per window");
            // A spent weekly or a spent 5h never sends.
            var spentWeekly = new LimitAccount
            {
                Provider = "claude", Ok = true,
                Windows = new List<LimitWindow> {
                    new LimitWindow { Base = "five_hour", Available = true, Remaining = 100, ResetEpoch = null },
                    new LimitWindow { Base = "weekly", Available = true, Remaining = 0, ResetEpoch = now + 86400 } }
            };
            Check(FiveHourKeeper.DecideState(spentWeekly, new KeeperAccountState(), now).State == KeeperStates.Blocked,
                "a spent weekly never sends a priming request");
            var spentFive = new LimitAccount
            {
                Provider = "claude", Ok = true,
                Windows = new List<LimitWindow> { new LimitWindow { Base = "five_hour", Available = true, Remaining = 0, ResetEpoch = null } }
            };
            Check(!FiveHourKeeper.DecideState(spentFive, new KeeperAccountState(), now).Eligible,
                "a spent 5h window never sends a priming request");
            // A known active window is not primed.
            var running = new LimitAccount
            {
                Provider = "claude", Ok = true,
                Windows = new List<LimitWindow> { new LimitWindow { Base = "five_hour", Available = true, Remaining = 60, ResetEpoch = now + 3600 } }
            };
            Check(!FiveHourKeeper.DecideState(running, new KeeperAccountState(), now).Eligible,
                "a window the vendor reports as running is not primed");
            // The Codex keeper keeps its own rules.
            var codexIdle = new LimitAccount
            {
                Provider = "codex", Ok = true,
                Windows = new List<LimitWindow> {
                    new LimitWindow { Base = "five_hour", Available = true, Remaining = 100, ResetEpoch = now + 5 * 3600 },
                    new LimitWindow { Base = "weekly", Available = true, Remaining = 50, ResetEpoch = now + 86400 } }
            };
            var codexState = new KeeperAccountState { LastResetEpoch = now + 5 * 3600 - 400 };
            Check(FiveHourKeeper.DecideState(codexIdle, codexState, now).Eligible,
                "Codex: a sliding reset still means an idle window");
            var codexRunning = new LimitAccount
            {
                Provider = "codex", Ok = true,
                Windows = new List<LimitWindow> {
                    new LimitWindow { Base = "five_hour", Available = true, Remaining = 70, ResetEpoch = now + 5 * 3600 },
                    new LimitWindow { Base = "weekly", Available = true, Remaining = 50, ResetEpoch = now + 86400 } }
            };
            Check(!FiveHourKeeper.DecideState(codexRunning, new KeeperAccountState { LastResetEpoch = now + 5 * 3600 }, now).Eligible,
                "Codex: a fixed reset still means the window runs");

            // (a) a missing profile mapping is a blocker, never a silent skip
            string profileTwo = Path.Combine(dir, ".claude-account2");
            Directory.CreateDirectory(profileTwo);
            Func<string, double, string> twoDoc = (loc, n) => Doc(n,
                Acc("claude/keeptest", "claude", "a1", loc, true, "",
                    Win("five_hour", "five_hour", "", true, 100, n - 10),
                    Win("weekly", "weekly", "", true, 50, n + 86400)),
                Acc("claude/keeptest2", "claude", "a2", "", true, "",
                    Win("five_hour", "five_hour", "", true, 100, n - 10),
                    Win("weekly", "weekly", "", true, 50, n + 86400)));
            fs.KeepFiveHour = "a2"; fs.KeepFiveHourState = "";
            var fk8 = new SaitulsForm(fs, null);
            IntPtr hk8 = fk8.Handle;
            ((System.Windows.Forms.Timer)Get(fk8, "_tick")).Stop();
            Call(fk8, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk8, "_limits", Limits.ParseLimisaw(twoDoc(profileDir, now), "LIMISAW"));
            int logBeforeNoMap = FakeLog(fakeLog).Count;
            Call(fk8, "KeepFiveHourTick");
            Pump(() => false, 600);
            KeeperAccountState stNoMap = null;
            ((Dictionary<string, KeeperAccountState>)Call(fk8, "KeepStates")).TryGetValue("a2", out stNoMap);
            Check(stNoMap != null && stNoMap.LastDecision.Contains("profile directory") && stNoMap.State == KeeperStates.Blocked
                && FakeLog(fakeLog).Count == logBeforeNoMap,
                "an account without a mapped profile directory reports a blocker and sends nothing",
                stNoMap == null ? "no state" : stNoMap.LastDecision);
            fk8.Dispose();

            // (b) a successful priming records durable evidence, and a restart
            //     cannot duplicate it
            fs.KeepFiveHour = "a1"; fs.KeepFiveHourState = "";
            Environment.SetEnvironmentVariable("FAKE_CLI_EXIT", "0");
            var fk9 = new SaitulsForm(fs, null);
            IntPtr hk9 = fk9.Handle;
            ((System.Windows.Forms.Timer)Get(fk9, "_tick")).Stop();
            Call(fk9, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk9, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            int logBeforePrime = FakeLog(fakeLog).Count;
            Call(fk9, "KeepFiveHourTick");
            Pump(() => !(bool)Get(fk9, "_keepBusy") && FakeLog(fakeLog).Count > logBeforePrime, 20000);
            KeeperAccountState stPrime = null;
            ((Dictionary<string, KeeperAccountState>)Call(fk9, "KeepStates")).TryGetValue("a1", out stPrime);
            Check(stPrime != null && stPrime.LastAcceptedAt > 0 && stPrime.PrimedOnce && stPrime.AttemptId.Length > 0
                && stPrime.ProfileId.StartsWith(profileDir, StringComparison.OrdinalIgnoreCase),
                "a successful priming records the attempt, the profile and the accepted request",
                stPrime == null ? "no state" : stPrime.AttemptId + " / " + stPrime.ProfileId);
            string durableState = (string)Get(fs, "KeepFiveHourState");
            Check(durableState.Contains("PRIMING_ACCEPTED") && durableState.Contains("ACCEPTED_UNVERIFIED"),
                "the admission record is durable, not only in memory", durableState);
            int logAfterPrime = FakeLog(fakeLog).Count;
            fk9.Dispose();
            var restartSettings = new SaitulsSettings(dir); restartSettings.Load(); restartSettings.RootPath = dir;
            var fk10 = new SaitulsForm(restartSettings, null);
            IntPtr hk10 = fk10.Handle;
            ((System.Windows.Forms.Timer)Get(fk10, "_tick")).Stop();
            Call(fk10, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk10, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            Call(fk10, "KeepFiveHourTick");
            Pump(() => false, 800);
            Check(FakeLog(fakeLog).Count == logAfterPrime,
                "a restart cannot duplicate a priming: the durable record is read back before anything is sent");
            fk10.Dispose();

            // (c) a persistence that cannot be saved stops the request
            var badSettings = new SaitulsSettings(dir); badSettings.Load(); badSettings.RootPath = dir;
            badSettings.KeepFiveHour = "a1"; badSettings.KeepFiveHourState = "";
            badSettings.IniPath = Path.Combine(dir, "no-such-folder", "SAITULS.ini");
            var fk11 = new SaitulsForm(badSettings, null);
            IntPtr hk11 = fk11.Handle;
            ((System.Windows.Forms.Timer)Get(fk11, "_tick")).Stop();
            Call(fk11, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk11, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            int logBeforeBlocked = FakeLog(fakeLog).Count;
            Call(fk11, "KeepFiveHourTick");
            Pump(() => false, 800);
            KeeperAccountState stBlocked = null;
            ((Dictionary<string, KeeperAccountState>)Call(fk11, "KeepStates")).TryGetValue("a1", out stBlocked);
            Check(stBlocked != null && stBlocked.LastDecision.Contains("BLOCKED_PERSISTENCE")
                && FakeLog(fakeLog).Count == logBeforeBlocked,
                "BLOCKED_PERSISTENCE: a request that cannot be admitted on disk is never sent",
                stBlocked == null ? "no state" : stBlocked.LastDecision);
            fk11.Dispose();

            // (d) separate profiles never mix, and an unrelated account spends nothing
            fs.KeepFiveHour = "a1|a2"; fs.KeepFiveHourState = "";
            var fk12 = new SaitulsForm(fs, null);
            IntPtr hk12 = fk12.Handle;
            ((System.Windows.Forms.Timer)Get(fk12, "_tick")).Stop();
            Call(fk12, "LoadAccounts");
            Pump(() => false, 400);
            var rows12 = (IList)Get(fk12, "_accounts");
            Set(rows12[0], "ProfileLocator", profileDir);
            Set(rows12[1], "ProfileLocator", profileTwo);
            Set(fk12, "_limits", Limits.ParseLimisaw(twoDoc(profileDir, now), "LIMISAW"));
            int logBeforeTwo = FakeLog(fakeLog).Count;
            Call(fk12, "KeepFiveHourTick");
            Pump(() => !(bool)Get(fk12, "_keepBusy") && FakeLog(fakeLog).Count > logBeforeTwo, 20000);
            Call(fk12, "KeepFiveHourTick");
            Pump(() => !(bool)Get(fk12, "_keepBusy") && FakeLog(fakeLog).Count > logBeforeTwo + 1, 20000);
            var twoLog = FakeLog(fakeLog);
            int inFirst = 0, inSecond = 0, pings = 0;
            foreach (string line in twoLog)
            {
                if (line.IndexOf("-p hi", StringComparison.Ordinal) < 0) continue;
                pings++;
                if (line.StartsWith(profileDir, StringComparison.OrdinalIgnoreCase)) inFirst++;
                if (line.StartsWith(profileTwo, StringComparison.OrdinalIgnoreCase)) inSecond++;
            }
            Check(inFirst >= 1 && inSecond >= 1 && inFirst + inSecond == pings,
                "each account's request runs in its own profile directory, and no account spends another one's quota",
                string.Join(" | ", twoLog.ToArray()));
            fk12.Dispose();

            // (e) a changed model name stays compatible: the CLI's own family
            //     alias first, the dated id only when the alias is refused
            fs.KeepFiveHour = "a1"; fs.KeepFiveHourState = "";
            Environment.SetEnvironmentVariable("FAKE_CLI_REJECT", "haiku");
            var fk13 = new SaitulsForm(fs, null);
            IntPtr hk13 = fk13.Handle;
            ((System.Windows.Forms.Timer)Get(fk13, "_tick")).Stop();
            Call(fk13, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk13, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            int logBeforeModel = FakeLog(fakeLog).Count;
            Call(fk13, "KeepFiveHourTick");
            Pump(() => !(bool)Get(fk13, "_keepBusy") && FakeLog(fakeLog).Count > logBeforeModel, 20000);
            var modelLog = FakeLog(fakeLog);
            bool triedAlias = false, triedDated = false;
            foreach (string line in modelLog)
            {
                if (line.IndexOf("-p hi", StringComparison.Ordinal) < 0) continue;
                if (line.Contains("--model haiku")) triedAlias = true;
                if (line.Contains("--model claude-haiku-4-5-20251001")) triedDated = true;
            }
            KeeperAccountState stModel = null;
            ((Dictionary<string, KeeperAccountState>)Call(fk13, "KeepStates")).TryGetValue("a1", out stModel);
            Check(triedAlias && triedDated && stModel != null && stModel.OutcomeConfidence == KeeperOutcomes.AcceptedUnverified,
                "a retired model name does not break the keeper: the alias is tried, the dated id is the fallback",
                stModel == null ? "no state" : stModel.LastResult);
            fk13.Dispose();
            Environment.SetEnvironmentVariable("FAKE_CLI_REJECT", null);

            // (f) the native UI is truthful about the keeper
            var fk14 = new SaitulsForm(fs, null);
            IntPtr hk14 = fk14.Handle;
            ((System.Windows.Forms.Timer)Get(fk14, "_tick")).Stop();
            Call(fk14, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk14, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            Call(fk14, "KeepFiveHourTick");
            Pump(() => !(bool)Get(fk14, "_keepBusy"), 20000);
            string diag = (string)Call(fk14, "KeeperDiagnostics", "a1");
            Check(diag.Contains("keeper ON") && diag.Contains("last request") && diag.Contains("accepted")
                && diag.Contains("RUNNING_VERIFIED"),
                "the keeper's own line tells the operator what it is doing, without claiming a started window", diag);
            KeeperAccountState stDiag = null;
            ((Dictionary<string, KeeperAccountState>)Call(fk14, "KeepStates")).TryGetValue("a1", out stDiag);
            if (stDiag != null) { stDiag.LastVerifiedReset = 0; stDiag.LastEstimatedReset = now + 3600; }
            string diagEst = (string)Call(fk14, "KeeperDiagnostics", "a1");
            Check(diagEst.Contains("ESTIMATED") && !diagEst.Contains("vendor confirmed"),
                "a predicted reset is shown as ESTIMATED, never as a vendor-confirmed one", diagEst);
            fk14.Dispose();

            // (g) an exit code of zero alone proves nothing: an answer with no
            //     result envelope is an UNKNOWN outcome, and it is not replayed
            fs.KeepFiveHour = "a1"; fs.KeepFiveHourState = "";
            Environment.SetEnvironmentVariable("FAKE_CLI_SILENT", "1");
            var fk15 = new SaitulsForm(fs, null);
            IntPtr hk15 = fk15.Handle;
            ((System.Windows.Forms.Timer)Get(fk15, "_tick")).Stop();
            Call(fk15, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk15, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            int logBeforeSilent = FakeLog(fakeLog).Count;
            Call(fk15, "KeepFiveHourTick");
            Pump(() => !(bool)Get(fk15, "_keepBusy") && FakeLog(fakeLog).Count > logBeforeSilent, 20000);
            KeeperAccountState stSilent = null;
            ((Dictionary<string, KeeperAccountState>)Call(fk15, "KeepStates")).TryGetValue("a1", out stSilent);
            int logAfterSilent = FakeLog(fakeLog).Count;
            Call(fk15, "KeepFiveHourTick");
            Pump(() => false, 800);
            Check(stSilent != null && stSilent.OutcomeConfidence == KeeperOutcomes.UnknownOutcome
                && stSilent.LastResult.Contains("unknown") && FakeLog(fakeLog).Count == logAfterSilent,
                "an exit code of zero without a result envelope is an unknown outcome, and it is not replayed",
                stSilent == null ? "no state" : stSilent.LastResult);
            fk15.Dispose();
            Environment.SetEnvironmentVariable("FAKE_CLI_SILENT", null);

            // (h) one owner per account: when the other keeper owns it, this
            //     one never sends
            fs.KeepFiveHour = "a1"; fs.KeepFiveHourState = "";
            fs.KeepFiveHourOwner = "ZAICODE";
            var fk16 = new SaitulsForm(fs, null);
            IntPtr hk16 = fk16.Handle;
            ((System.Windows.Forms.Timer)Get(fk16, "_tick")).Stop();
            Call(fk16, "LoadAccounts");
            Pump(() => false, 400);
            Set(fk16, "_limits", Limits.ParseLimisaw(keeperDoc(profileDir, now), "LIMISAW"));
            int logBeforeOwner = FakeLog(fakeLog).Count;
            Call(fk16, "KeepFiveHourTick");
            Pump(() => false, 800);
            KeeperAccountState stOwner = null;
            ((Dictionary<string, KeeperAccountState>)Call(fk16, "KeepStates")).TryGetValue("a1", out stOwner);
            Check(stOwner != null && stOwner.LastDecision.Contains("ZAICODE") && FakeLog(fakeLog).Count == logBeforeOwner,
                "an account owned by the other keeper is never sent to from here",
                stOwner == null ? "no state" : stOwner.LastDecision);
            fk16.Dispose();
            fs.KeepFiveHourOwner = "SAITULS";

            Environment.SetEnvironmentVariable("PATH", pathBefore);

            // ---- 22. T-304 A1: the result envelope is READ as JSON -------------
            // The old classifier searched the raw text for "is_error":true, so
            // every other legal whitespace form slipped through, a foreign
            // event with a nested result was taken for an envelope, and a
            // success was accepted without a successful exit.
            Func<string, bool, string, bool, string> Cl = (outText, exitOk, errText, timedOut) =>
                FiveHourKeeper.ClassifyOutcome("claude", exitOk, outText, errText, timedOut);
            Check(Cl("{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"ok\"}", true, "", false)
                    == KeeperOutcomes.AcceptedUnverified,
                "a valid success envelope is an accepted request");
            string[] errorEnvelopes = new string[] {
                "{\"type\":\"result\",\"is_error\":true,\"result\":\"failed\"}",
                "{\"type\":\"result\",\"is_error\" : true,\"result\":\"failed\"}",
                "{\"type\":\"result\",\"is_error\"  :  true ,\"result\":\"failed\"}",
                "{\"type\":\"result\",\"is_error\":\n true,\"result\":\"failed\"}",
                "{\"type\":\"result\",\"is_error\":true}" };
            bool errorsRead = true;
            foreach (string envelope in errorEnvelopes)
                if (Cl(envelope, true, "", false) != KeeperOutcomes.FailedOutcome) errorsRead = false;
            Check(errorsRead,
                "is_error=true is read as a boolean whatever whitespace the vendor prints");
            Check(Cl("{\"type\":\"result\",\"is_error\":false,\"result\":\"failed\"}", true, "", false)
                    == KeeperOutcomes.AcceptedUnverified,
                "a success envelope with a result field is an accepted request");
            Check(Cl("{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}", false, "", false)
                    == KeeperOutcomes.FailedOutcome,
                "a successful envelope under a failed exit is a failure, not an acceptance");
            Check(Cl("{\"type\":\"assistant\",\"message\":{\"is_error\":true,\"result\":\"nested\"}}", true, "", false)
                    == KeeperOutcomes.UnknownOutcome,
                "a nested result of another event is not an envelope");
            Check(Cl("{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"hi\"}]}}", true, "", false)
                    == KeeperOutcomes.UnknownOutcome,
                "an ordinary assistant event establishes nothing");
            Check(Cl("{\"type\":\"system\",\"subtype\":\"init\"}\n{\"type\":\"assistant\",\"message\":{}}\n"
                    + "{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}", true, "", false)
                    == KeeperOutcomes.AcceptedUnverified,
                "the result envelope is found among several records");
            Check(Cl("{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"", true, "", false)
                    == KeeperOutcomes.UnknownOutcome,
                "a truncated envelope establishes nothing");
            Check(Cl("", true, "", false) == KeeperOutcomes.UnknownOutcome
                    && Cl("   \n  ", true, "", false) == KeeperOutcomes.UnknownOutcome,
                "an empty answer establishes nothing");
            Check(Cl("Claude Code v2.1.0\nready\n", true, "", false) == KeeperOutcomes.UnknownOutcome,
                "CLI diagnostics are not a result envelope");
            Check(Cl("{\"type\":\"result\",\"is_error\":\"false\",\"result\":\"ok\"}", true, "", false)
                    == KeeperOutcomes.UnknownOutcome
                    && Cl("{\"type\":\"result\",\"result\":\"ok\"}", true, "", false) == KeeperOutcomes.UnknownOutcome,
                "a verdict that is not a boolean establishes nothing");
            Check(Cl("", true, "{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}", false)
                    == KeeperOutcomes.UnknownOutcome,
                "stderr is never read as a result envelope");
            Check(Cl("{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}", true, "", true)
                    == KeeperOutcomes.UnknownOutcome,
                "a timeout establishes nothing");
            Check(FiveHourKeeper.ClassifyOutcome("codex", true, "OK", "", false) == KeeperOutcomes.AcceptedUnverified
                    && FiveHourKeeper.ClassifyOutcome("codex", false, "OK", "", false) == KeeperOutcomes.FailedOutcome,
                "the Codex keeper keeps its own plain-text rule");

            // ---- 18. optional integrations missing: startup still works -------------
            Environment.SetEnvironmentVariable("SAI_ACCOUNTS_EXE", Path.Combine(dir, "missing.exe"));
            var bare = new SaitulsSettings(dir); bare.Load(); bare.RootPath = dir;
            bool ok = true; string why = "";
            try
            {
                var f2 = new SaitulsForm(bare, null);
                IntPtr h2 = f2.Handle;
                ((System.Windows.Forms.Timer)Get(f2, "_tick")).Stop();
                Check(Call(f2, "AccountsExe") == null,
                    "an explicit missing account-engine override actually exercises the unavailable-engine state");
                Directory.CreateDirectory(Path.Combine(dir, "Bin"));
                File.WriteAllText(Path.Combine(dir, "Bin", "ARIA2C.EXE"), "presence-only fixture");
                object readyMedia = Activator.CreateInstance(typeof(SaitulsForm).GetNestedType("MediaProbeResult", Any), true);
                Set(readyMedia, "Ready", true);
                Set(f2, "_lastProbe", readyMedia); Set(f2, "_mediaProbeDone", true);
                Set(f2, "CurrentTab", "Home");
                using (var bmp = new Bitmap(600, 460)) using (Graphics g = Graphics.FromImage(bmp))
                    Call(f2, "OnPaint", new PaintEventArgs(g, new Rectangle(0, 0, 600, 460)));
                bool homeRepair = false;
                foreach (object button in (IList)Get(f2, "Buttons"))
                    if ((string)Get(button, "Label") == AccountsLoc.T("InstallRepair")) homeRepair = true;
                Check(homeRepair, "missing account service offers repair on Home even when ordinary tools are ready");
                Set(f2, "CurrentTab", "Accounts");
                using (var bmp = new Bitmap(600, 460)) using (Graphics g = Graphics.FromImage(bmp))
                    Call(f2, "OnPaint", new PaintEventArgs(g, new Rectangle(0, 0, 600, 460)));
                bool accountsRepair = false;
                foreach (object button in (IList)Get(f2, "Buttons"))
                    if ((string)Get(button, "Label") == AccountsLoc.T("InstallRepair")) accountsRepair = true;
                Check(accountsRepair, "missing account service offers an action that can repair it in Accounts");
                foreach (string tab in new[] { "Home", "Accounts", "Settings", "Tools", "Explorer menus" })
                {
                    Set(f2, "CurrentTab", tab);
                    using (var bmp = new Bitmap(600, 460)) using (Graphics g = Graphics.FromImage(bmp))
                        Call(f2, "OnPaint", new PaintEventArgs(g, new Rectangle(0, 0, 600, 460)));
                }
                Call(f2, "StopBackground");
                f2.Dispose();
            }
            catch (Exception ex) { ok = false; why = (ex.InnerException ?? ex).GetType().Name + ": " + (ex.InnerException ?? ex).Message; }
            Check(ok, "no account service and no LIMISAW: every tab still opens and paints", why);
        }
        finally
        {
            Environment.SetEnvironmentVariable("SAITULS_LIMITS_DIR", cacheOverrideBefore);
            try { Directory.Delete(dir, true); } catch { }
        }

        Console.WriteLine("passed " + passes + ", failed " + failures);
        return failures == 0 ? 0 : 1;
    }
}
