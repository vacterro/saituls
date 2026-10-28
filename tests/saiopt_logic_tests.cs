using System;
using System.Collections.Generic;
using Saituls.Saiopt;

// Deterministic unit tests for SAIOPT's pure logic. Compiled together with
// Scripts\saiopt\OptLogic.cs by tests\test_saiopt_logic.ps1. No desktop, no
// processes, no clock: the rules that decide what SAIOPT may touch are proven
// here, including every "never".

static class SaioptLogicTests
{
    static int failures;
    static int checks;

    static void Check(string name, bool ok, string detail)
    {
        checks++;
        if (ok) Console.WriteLine("PASS  " + name + (detail.Length > 0 ? "  " + detail : ""));
        else { failures++; Console.WriteLine("FAIL  " + name + (detail.Length > 0 ? "  " + detail : "")); }
    }

    static void Check(string name, bool ok) { Check(name, ok, ""); }

    // ---- helpers ---------------------------------------------------------

    static ProcView P(int pid, string image, double core)
    {
        ProcView v = new ProcView();
        v.Key = new ProcKey(pid, 1000 + pid);
        v.Image = image;
        v.SessionId = 1;
        v.BasePriority = Priorities.Normal;
        v.CoreShare = core;
        v.ParentAlive = true;
        return v;
    }

    static List<ProcView> L(params ProcView[] v) { return new List<ProcView>(v); }

    static int Count(List<EaseAction> a, EaseKind k)
    {
        int n = 0;
        foreach (EaseAction x in a) if (x.Kind == k) n++;
        return n;
    }

    static EaseAction First(List<EaseAction> a, EaseKind k)
    {
        foreach (EaseAction x in a) if (x.Kind == k) return x;
        return null;
    }

    // Drives one process hot through enough busy samples to be eased.
    static EaseEngine Eased(ProcView p, out long t)
    {
        EaseEngine e = new EaseEngine(OptConfig.Defaults());
        t = 0;
        for (int i = 0; i < OptConfig.DefaultHogSamples; i++) { t += 2000; e.Observe(L(p), 90, t, "brave.exe"); }
        return e;
    }

    // ---- config ------------------------------------------------------------

    static void Config()
    {
        OptConfig d = OptConfig.Defaults();
        Check("config: easing on by default", d.Ease);
        Check("config: nothing is ended automatically by default", d.SweepAuto.Count == 0);
        Check("config: audio protection on by default", d.ProtectAudio);
        Check("config: eases to BelowNormal by default", d.EasePriority == Priorities.BelowNormal);

        Dictionary<string, string> good = new Dictionary<string, string>();
        good["busy_pct"] = "70"; good["calm_pct"] = "30"; good["hog_core_pct"] = "80"; good["ease_to"] = "idle";
        good["never_ease"] = "Photoshop, blender.exe"; good["sweep_auto"] = "agy"; good["ease"] = "0";
        List<string> w = new List<string>();
        OptConfig c = OptConfig.Parse(good, w);
        Check("config: good values taken", c.BusyPct == 70 && c.CalmPct == 30 && c.HogCorePct == 80 && !c.Ease && w.Count == 0, "warnings=" + w.Count);
        Check("config: ease_to=idle maps to Idle", c.EasePriority == Priorities.Idle);
        Check("config: image lists normalised (.exe, lower case)", c.NeverEase.Contains("photoshop.exe") && c.NeverEase.Contains("blender.exe") && c.SweepAuto.Contains("agy.exe"));

        Dictionary<string, string> bad = new Dictionary<string, string>();
        bad["busy_pct"] = "abc"; bad["hog_core_pct"] = "1"; bad["sample_ms"] = "99999"; bad["ease_to"] = "realtime";
        w.Clear();
        OptConfig b = OptConfig.Parse(bad, w);
        Check("config: bad values fall back, each warned", b.BusyPct == 60 && b.HogCorePct == 50 && b.SampleMs == 2000
            && b.EaseTo == EaseTargets.BelowNormal && w.Count == 4, "warnings=" + w.Count);

        Dictionary<string, string> inverted = new Dictionary<string, string>();
        inverted["busy_pct"] = "40"; inverted["calm_pct"] = "50";
        w.Clear();
        OptConfig inv = OptConfig.Parse(inverted, w);
        Check("config: calm above busy falls back to both defaults", inv.BusyPct == 60 && inv.CalmPct == 35 && w.Count == 1);

        IDictionary<string, string> ini = OptConfig.ParseIni("[saiopt]\r\nbusy_pct=70   ; 20..100\r\nnever_ease=a;b\r\n# c\r\n");
        Check("ini: inline comment stripped only after whitespace", ini["busy_pct"] == "70" && ini["never_ease"] == "a;b");

        string text = "; head\r\n[saiopt]\r\nease=1                ; comment kept\r\nsweep_auto=           ; list\r\n";
        string set = OptConfig.SetIniValue(text, "sweep_auto", "agy.exe");
        Check("ini: SetIniValue rewrites one key and keeps its comment", set.Contains("sweep_auto=agy.exe") && set.Contains("; list") && set.Contains("; comment kept") && set.Contains("; head"));
        string added = OptConfig.SetIniValue(text, "tray", "0");
        Check("ini: SetIniValue appends a missing key", added.Contains("tray=0") && added.Contains("ease=1"));
        Check("ini: round trip parses back", OptConfig.ParseIni(set)["sweep_auto"] == "agy.exe");
    }

    // ---- guards ------------------------------------------------------------

    static void Guards()
    {
        OptConfig cfg = OptConfig.Defaults();
        string[] never = { "explorer.exe", "dwm.exe", "csrss.exe", "audiodg.exe", "conhost.exe", "autohotkeyu64.exe",
            "obs64.exe", "saiopt.exe", "taskbaredge.exe", "startmenuexperiencehost.exe", "svchost.exe", "lsass.exe" };
        foreach (string img in never) Check("guard: " + img + " is never eased", Guard.EaseBlock(P(500, img, 300), cfg) != null);

        ProcView self = P(501, "whatever.exe", 300); self.Self = true;
        Check("guard: SAIOPT never eases itself", Guard.EaseBlock(self, cfg) == "self");
        ProcView svc = P(502, "node.exe", 300); svc.SessionId = 0;
        Check("guard: service session is never touched", Guard.EaseBlock(svc, cfg) == "service session");
        ProcView fg = P(503, "brave.exe", 300); fg.Focused = true;
        Check("guard: what you use is never eased", Guard.EaseBlock(fg, cfg) == "in use");
        ProcView loud = P(504, "vlc.exe", 300); loud.Audible = true;
        Check("guard: what you hear is never eased", Guard.EaseBlock(loud, cfg) == "playing sound");
        OptConfig mute = OptConfig.Defaults(); mute.ProtectAudio = false;
        Check("guard: protect_audio=0 lifts only the audio rule", Guard.EaseBlock(loud, mute) == null);
        OptConfig ne = OptConfig.Defaults(); ne.NeverEase.Add("opencode.exe");
        Check("guard: never_ease is honoured", Guard.EaseBlock(P(505, "opencode.exe", 300), ne) == "never_ease");
        Check("guard: an ordinary background hog may be eased", Guard.EaseBlock(P(506, "opencode.exe", 300), cfg) == null);
        ProcView macro = P(507, "pythonw.exe", 300); macro.InputHelper = true;
        Check("guard: the launcher of an input hook (macro controller) is never eased", Guard.EaseBlock(macro, cfg) == "drives your input");
        Check("guard: AutoHotkey variants are input hooks", Guard.IsInputHook("autohotkeyu64.exe") && Guard.IsInputHook("AutoHotkey.exe") && !Guard.IsInputHook("node.exe"));
    }

    // ---- ease ----------------------------------------------------------------

    static void EaseRules()
    {
        // Not busy: a hog is left alone.
        EaseEngine idle = new EaseEngine(OptConfig.Defaults());
        int eased = 0;
        for (int i = 1; i <= 20; i++) eased += Count(idle.Observe(L(P(10, "opencode.exe", 200)), 40, i * 2000, "brave.exe"), EaseKind.Ease);
        Check("ease: a hog is not eased while the machine has headroom", eased == 0);

        // Busy: eased on exactly the hog_samples-th consecutive hot sample.
        EaseEngine e = new EaseEngine(OptConfig.Defaults());
        ProcView hog = P(11, "opencode.exe", 200);
        int firstAt = -1;
        for (int i = 1; i <= 6 && firstAt < 0; i++)
            if (Count(e.Observe(L(hog), 85, i * 2000, "brave.exe"), EaseKind.Ease) == 1) firstAt = i;
        Check("ease: eased on the " + OptConfig.DefaultHogSamples + "rd consecutive hot sample, not before", firstAt == OptConfig.DefaultHogSamples, "at=" + firstAt);
        Check("ease: eased once, not every sample", e.EasedCount == 1);

        // One cool sample breaks the streak.
        EaseEngine s = new EaseEngine(OptConfig.Defaults());
        s.Observe(L(P(12, "node.exe", 200)), 90, 2000, null);
        s.Observe(L(P(12, "node.exe", 200)), 90, 4000, null);
        s.Observe(L(P(12, "node.exe", 10)), 90, 6000, null);
        List<EaseAction> a = s.Observe(L(P(12, "node.exe", 200)), 90, 8000, null);
        Check("ease: a cool sample resets the streak", Count(a, EaseKind.Ease) == 0);

        // Only Normal is ours: a governor's Idle ffmpeg and a user's High app stay theirs.
        EaseEngine own = new EaseEngine(OptConfig.Defaults());
        ProcView gov = P(13, "ffmpeg.exe", 800); gov.BasePriority = Priorities.Idle;
        ProcView hi = P(14, "render.exe", 800); hi.BasePriority = Priorities.High;
        int touched = 0;
        for (int i = 1; i <= 10; i++) touched += own.Observe(L(gov, hi), 95, i * 2000, null).Count;
        Check("ease: a process not at Normal is never touched", touched == 0);

        ProcView fg = P(15, "brave.exe", 800); fg.Focused = true;
        ProcView loud = P(16, "vlc.exe", 800); loud.Audible = true;
        ProcView shell = P(17, "explorer.exe", 800);
        ProcView svc = P(18, "svc.exe", 800); svc.SessionId = 0;
        EaseEngine prot = new EaseEngine(OptConfig.Defaults());
        touched = 0;
        for (int i = 1; i <= 10; i++) touched += prot.Observe(L(fg, loud, shell, svc), 99, i * 2000, "brave.exe").Count;
        Check("ease: focused, audible, shell and service processes survive a pegged machine", touched == 0);

        // The reason names the facts.
        long t;
        EaseEngine r = new EaseEngine(OptConfig.Defaults());
        EaseAction ea = null;
        for (int i = 1; i <= 3; i++) { ea = First(r.Observe(L(P(19, "opencode.exe", 150)), 88, i * 2000, "brave.exe"), EaseKind.Ease) ?? ea; }
        Check("ease: the reason says how much, how long, how busy and what you used",
            ea != null && ea.Reason.Contains("1.5 cores") && ea.Reason.Contains("6 s") && ea.Reason.Contains("88%") && ea.Reason.Contains("brave.exe"),
            ea == null ? "none" : ea.Reason);
        Check("ease: target is BelowNormal, origin Normal", ea != null && ea.ToPriority == Priorities.BelowNormal && ea.FromPriority == Priorities.Normal);
        EaseEngine twin = new EaseEngine(OptConfig.Defaults());
        EaseAction tw = null;
        for (int i = 1; i <= 3; i++) { tw = First(twin.Observe(L(P(37, "opencode.exe", 150)), 88, i * 2000, "opencode.exe"), EaseKind.Ease) ?? tw; }
        Check("ease: a sibling of the focused app is named as another window", tw != null && tw.Reason.EndsWith("while you used another opencode.exe window"),
            tw == null ? "none" : tw.Reason);

        long tm;
        EaseEngine helper = Eased(P(38, "pythonw.exe", 200), out tm);
        ProcView ph = P(38, "pythonw.exe", 200); ph.BasePriority = Priorities.BelowNormal; ph.InputHelper = true;
        EaseAction hb = First(helper.Observe(L(ph), 95, tm + 2000, null), EaseKind.Restore);
        Check("restore: a process that starts driving input is restored", hb != null && hb.Reason == "it drives your input");

        EaseEngine keep = Eased(P(39, "node.exe", 200), out tm);
        keep.Cfg = OptConfig.Defaults(); keep.Cfg.NeverEase.Add("node.exe");
        ProcView pk = P(39, "node.exe", 200); pk.BasePriority = Priorities.BelowNormal;
        EaseAction kb = First(keep.Observe(L(pk), 95, tm + 2000, null), EaseKind.Restore);
        Check("restore: Keep fast (never_ease) restores at once", kb != null && kb.Reason == "it is on the never-ease list now");

        // Restore paths.
        ProcView p = P(20, "opencode.exe", 200);
        EaseEngine f = Eased(p, out t);
        ProcView pf = P(20, "opencode.exe", 200); pf.BasePriority = Priorities.BelowNormal; pf.Focused = true;
        EaseAction back = First(f.Observe(L(pf), 90, t + 2000, "opencode.exe"), EaseKind.Restore);
        Check("restore: switching to it restores at once", back != null && back.Reason == "you switched to it" && back.ToPriority == Priorities.Normal);
        Check("restore: restore goes from BelowNormal to Normal", back != null && back.FromPriority == Priorities.BelowNormal);

        EaseEngine au = Eased(P(21, "player.exe", 200), out t);
        ProcView pa = P(21, "player.exe", 200); pa.BasePriority = Priorities.BelowNormal; pa.Audible = true;
        back = First(au.Observe(L(pa), 90, t + 2000, null), EaseKind.Restore);
        Check("restore: starting to play sound restores", back != null && back.Reason == "it started playing sound");

        EaseEngine calm = Eased(P(22, "node.exe", 200), out t);
        ProcView pc = P(22, "node.exe", 200); pc.BasePriority = Priorities.BelowNormal;
        int restores = 0, at = -1;
        for (int i = 1; i <= 15; i++)
        {
            int n = Count(calm.Observe(L(pc), 20, t + i * 2000, null), EaseKind.Restore);
            restores += n;
            if (n > 0 && at < 0) at = i;
        }
        Check("restore: machine calm for calm_samples restores", restores == 1 && at == OptConfig.DefaultCalmSamples, "at=" + at);

        EaseEngine quiet = Eased(P(23, "node.exe", 200), out t);
        ProcView pq = P(23, "node.exe", 5); pq.BasePriority = Priorities.BelowNormal;
        restores = 0; at = -1;
        for (int i = 1; i <= 15; i++)
        {
            List<EaseAction> acts = quiet.Observe(L(pq), 80, t + i * 2000, null);
            if (Count(acts, EaseKind.Restore) > 0 && at < 0) { at = i; restores++; }
        }
        Check("restore: a hog that quiets down is restored after calm_samples", restores == 1 && at == OptConfig.DefaultCalmSamples, "at=" + at);

        // Someone else changed it: forget, never fight, never touch again.
        EaseEngine other = Eased(P(24, "node.exe", 200), out t);
        ProcView po = P(24, "node.exe", 200); po.BasePriority = Priorities.AboveNormal;
        List<EaseAction> fa = other.Observe(L(po), 95, t + 2000, null);
        Check("forget: a priority changed by someone else is left as is", Count(fa, EaseKind.Forget) == 1 && Count(fa, EaseKind.Restore) == 0);
        ProcView pn = P(24, "node.exe", 200);
        touched = 0;
        for (int i = 2; i <= 12; i++) touched += other.Observe(L(pn), 95, t + i * 2000, null).Count;
        Check("forget: ... and is never touched again in its lifetime", touched == 0);

        // Exit: nothing to restore.
        EaseEngine gone = Eased(P(25, "node.exe", 200), out t);
        List<EaseAction> ga = gone.Observe(L(), 95, t + 2000, null);
        Check("exit: an eased process that exits is forgotten, not restored", Count(ga, EaseKind.Forget) == 1 && Count(ga, EaseKind.Restore) == 0 && gone.EasedCount == 0);

        // PID reuse: same pid, new creation time = a stranger.
        EaseEngine reuse = Eased(P(26, "node.exe", 200), out t);
        ProcView stranger = P(26, "node.exe", 200); stranger.Key = new ProcKey(26, 999999);
        List<EaseAction> ra = reuse.Observe(L(stranger), 95, t + 2000, null);
        Check("identity: a reused PID is a new process (old one forgotten, new one not eased yet)",
            Count(ra, EaseKind.Forget) == 1 && Count(ra, EaseKind.Ease) == 0 && reuse.EasedCount == 0);

        // Refused (access denied): never retried.
        EaseEngine refused = new EaseEngine(OptConfig.Defaults());
        ProcView rp = P(27, "elevated.exe", 300);
        int eases = 0;
        for (int i = 1; i <= 3; i++) eases += Count(refused.Observe(L(rp), 95, i * 2000, null), EaseKind.Ease);
        refused.Refuse(rp.Key);
        for (int i = 4; i <= 20; i++) eases += Count(refused.Observe(L(rp), 95, i * 2000, null), EaseKind.Ease);
        Check("refuse: a process SAIOPT could not change is not retried", eases == 1, "eases=" + eases);

        // Cap.
        OptConfig capCfg = OptConfig.Defaults(); capCfg.MaxEased = 2;
        EaseEngine cap = new EaseEngine(capCfg);
        List<ProcView> many = L(P(30, "a.exe", 200), P(31, "b.exe", 200), P(32, "c.exe", 200), P(33, "d.exe", 200));
        for (int i = 1; i <= 5; i++)
        {
            // Mirror the adapter: an applied Ease is what the next sample sees.
            foreach (EaseAction act in cap.Observe(many, 95, i * 2000, null))
                if (act.Kind == EaseKind.Ease)
                    foreach (ProcView v in many) if (v.Key.Equals(act.Key)) v.BasePriority = act.ToPriority;
        }
        Check("cap: never more than max_eased at once", cap.EasedCount == 2, "eased=" + cap.EasedCount);

        // Switching easing off restores; RestoreAll restores everything.
        EaseEngine off = Eased(P(34, "node.exe", 200), out t);
        off.Cfg = OptConfig.Defaults(); off.Cfg.Ease = false;
        ProcView p34 = P(34, "node.exe", 200); p34.BasePriority = Priorities.BelowNormal;
        back = First(off.Observe(L(p34), 95, t + 2000, null), EaseKind.Restore);
        Check("off: switching auto-ease off restores what it eased", back != null && back.Reason == "easing was switched off");
        long t2;
        EaseEngine all = Eased(P(35, "node.exe", 200), out t2);
        List<EaseAction> ra2 = all.RestoreAll("SAIOPT stopped");
        Check("stop: RestoreAll restores every eased process", Count(ra2, EaseKind.Restore) == 1 && all.EasedCount == 0);

        // Adopted from a crashed run: normal rules restore it.
        EaseEngine adopt = new EaseEngine(OptConfig.Defaults());
        EasedEntry en = new EasedEntry();
        en.Key = new ProcKey(36, 1036); en.Image = "node.exe"; en.OriginalPriority = Priorities.Normal; en.EasedPriority = Priorities.BelowNormal;
        adopt.Adopt(en);
        ProcView p36 = P(36, "node.exe", 200); p36.BasePriority = Priorities.BelowNormal; p36.Focused = true;
        back = First(adopt.Observe(L(p36), 95, 2000, null), EaseKind.Restore);
        Check("adopt: a process eased by a previous run is restored by the same rules", back != null && back.ToPriority == Priorities.Normal);
    }

    // ---- sweep ---------------------------------------------------------------

    static ProcView Orphan(int pid, string image, bool suspended, long ageMs)
    {
        ProcView v = P(pid, image, 0);
        v.ParentAlive = false;
        v.Suspended = suspended;
        v.AgeMs = ageMs;
        v.PrivateBytes = 57L << 20;
        v.CpuTicks = 1000;
        return v;
    }

    static void Sweep()
    {
        OptConfig cfg = OptConfig.Defaults();
        cfg.SweepAuto.Add("agy.exe");
        long hour = 3600000L;
        SweepTracker tr = new SweepTracker(cfg);
        ProcView old = Orphan(40, "agy.exe", true, 5 * 24 * hour);
        List<StaleItem> st = tr.Observe(L(old), 0);
        Check("sweep: a days-old frozen orphan is suggested at first sight", st.Count == 1 && st[0].Reason.StartsWith("suspended 5 d"), st.Count == 1 ? st[0].Reason : "none");
        Check("sweep: ...but never auto-ended on an age estimate", st.Count == 1 && !st[0].Auto);
        st = tr.Observe(L(old), 61 * 60000L);
        Check("sweep: armed + watched frozen for the full threshold -> auto", st.Count == 1 && st[0].Auto);

        SweepTracker unarmed = new SweepTracker(OptConfig.Defaults());
        unarmed.Observe(L(Orphan(41, "agy.exe", true, 0)), 0);
        st = unarmed.Observe(L(Orphan(41, "agy.exe", true, 0)), 61 * 60000L);
        Check("sweep: not armed -> suggested only, never auto", st.Count == 1 && !st[0].Auto);

        SweepTracker young = new SweepTracker(cfg);
        st = young.Observe(L(Orphan(42, "agy.exe", true, 10 * 60000L)), 0);
        Check("sweep: frozen for 10 min is not stale yet", st.Count == 0);

        ProcView withParent = Orphan(43, "agy.exe", true, 5 * 24 * hour); withParent.ParentAlive = true;
        ProcView withWindow = Orphan(44, "agy.exe", true, 5 * 24 * hour); withWindow.HasWindow = true;
        ProcView focused = Orphan(45, "agy.exe", true, 5 * 24 * hour); focused.Focused = true;
        ProcView hard = Orphan(46, "explorer.exe", true, 5 * 24 * hour);
        ProcView svc = Orphan(47, "agy.exe", true, 5 * 24 * hour); svc.SessionId = 0;
        ProcView self = Orphan(48, "saiopt.exe", true, 5 * 24 * hour); self.Self = true;
        SweepTracker safe = new SweepTracker(cfg);
        st = safe.Observe(L(withParent, withWindow, focused, hard, svc, self), 0);
        st = safe.Observe(L(withParent, withWindow, focused, hard, svc, self), 2 * hour);
        Check("sweep: live parent, window, focus, shell image, service, self -> never stale", st.Count == 0, "stale=" + st.Count);

        // Idle console tool orphan.
        OptConfig icfg = OptConfig.Defaults(); icfg.StaleIdleHours = 12;
        SweepTracker idle = new SweepTracker(icfg);
        ProcView node = Orphan(50, "node.exe", false, 0);
        idle.Observe(L(node), 0);
        st = idle.Observe(L(node), 11 * hour);
        Check("sweep: idle orphan console tool under the threshold is not stale", st.Count == 0);
        st = idle.Observe(L(node), 12 * hour + 1000);
        Check("sweep: idle orphan console tool past the threshold is stale", st.Count == 1 && st[0].Reason.StartsWith("no CPU for 12 h"), st.Count == 1 ? st[0].Reason : "none");
        ProcView woke = Orphan(50, "node.exe", false, 0); woke.CpuTicks = 2000;
        st = idle.Observe(L(woke), 13 * hour);
        Check("sweep: one CPU tick resets idleness", st.Count == 0);

        SweepTracker gui = new SweepTracker(icfg);
        ProcView discord = Orphan(51, "discord.exe", false, 0);
        gui.Observe(L(discord), 0);
        st = gui.Observe(L(discord), 48 * hour);
        Check("sweep: an idle orphan that is not a console tool is never stale", st.Count == 0);

        SweepTracker resumed = new SweepTracker(cfg);
        resumed.Observe(L(Orphan(52, "agy.exe", true, 0)), 0);
        resumed.Observe(L(Orphan(52, "agy.exe", false, 0)), 30 * 60000L);
        st = resumed.Observe(L(Orphan(52, "agy.exe", true, 0)), 70 * 60000L);
        Check("sweep: resuming resets the frozen clock", st.Count == 0);

        List<StaleItem> items = new List<StaleItem>();
        for (int i = 0; i < 5; i++) { StaleItem it = new StaleItem(); it.Image = "agy.exe"; it.PrivateBytes = 57L << 20; items.Add(it); }
        StaleItem big = new StaleItem(); big.Image = "node.exe"; big.PrivateBytes = 900L << 20; items.Add(big);
        List<KeyValuePair<string, List<StaleItem>>> groups = SweepTracker.Group(items);
        Check("sweep: grouped per image, largest first", groups.Count == 2 && groups[0].Key == "node.exe" && groups[1].Value.Count == 5
            && SweepTracker.Bytes(groups[1].Value) == 5 * (57L << 20));
    }

    // ---- watch -----------------------------------------------------------------

    static void Watch()
    {
        ShellHealth sh = new ShellHealth(150);
        Check("shell: unknown before any probe", sh.Word(250) == ShellWords.Unknown);
        for (int i = 0; i < 10; i++) sh.Add(3);
        Check("shell: fast answers are RESPONSIVE", sh.Word(250) == ShellWords.Responsive && sh.Median() == 3);
        sh.Add(400);
        Check("shell: one slow answer is not news", sh.Word(250) == ShellWords.Responsive);
        sh.Add(600);
        Check("shell: two slow answers in the last five are SLOW", sh.Word(250) == ShellWords.Slow);
        sh.Add(-1);
        Check("shell: a timeout is NOT RESPONDING at once", sh.Word(250) == ShellWords.Stalled);
        for (int i = 0; i < 5; i++) sh.Add(2);
        Check("shell: recovers once the last five are fine", sh.Word(250) == ShellWords.Responsive);

        DateTime t0 = new DateTime(2026, 9, 23, 4, 25, 0);
        List<DateTime> periodic = new List<DateTime>();
        for (int i = 0; i < 40; i++) periodic.Add(t0.AddSeconds(285 * i + (i % 7 == 0 ? 3 : 0)));
        CadenceResult c = Cadence.Of(periodic);
        Check("cadence: the incident enclosure is periodic at 285 s", c.Periodic && c.MedianSeconds == 285, "median=" + c.MedianSeconds);
        Random rnd = new Random(7);
        List<DateTime> scatter = new List<DateTime>();
        DateTime acc = t0;
        for (int i = 0; i < 40; i++) { acc = acc.AddSeconds(rnd.Next(30, 20000)); scatter.Add(acc); }
        Check("cadence: random scatter is not periodic", !Cadence.Of(scatter).Periodic);
        Check("cadence: fewer than four events never claims a period", Cadence.Of(new List<DateTime> { t0, t0.AddSeconds(60) }).MedianSeconds < 0);

        StorageWatch sw = new StorageWatch();
        DateTime now = new DateTime(2026, 9, 23, 22, 40, 0);
        for (int i = 0; i < 20; i++) sw.Add(11, now.AddSeconds(-285 * i - 60));
        sw.Add(8, now.AddHours(-18));
        Dictionary<int, DiskFacts> disks = new Dictionary<int, DiskFacts>();
        DiskFacts u = new DiskFacts(); u.RaidPort = 11; u.Model = "Samsung SSD 970 EVO SCSI Disk Device"; u.Letters = "U:"; u.Bus = "USB";
        disks[11] = u;
        List<StorageSummary> sums = sw.Summaries(now, disks);
        StorageSummary s11 = sums.Find(delegate(StorageSummary x) { return x.RaidPort == 11; });
        StorageSummary s8 = sums.Find(delegate(StorageSummary x) { return x.RaidPort == 8; });
        Check("storage: a resetting disk is active and named by letter", s11 != null && s11.Active && s11.Who() == "U: Samsung SSD 970 EVO (USB)", s11 == null ? "none" : s11.Who());
        Check("storage: a single old reset is not active", s8 != null && !s8.Active);
        Check("storage: an unmapped port says so instead of guessing", s8 != null && s8.Who() == "RaidPort8 (disk unknown)");
        Check("keep-awake: a present USB disk with periodic resets qualifies", KeepAwakeRule.Qualifies(s11));
        Check("keep-awake: identity is letters + model, not the drifting RaidPort", KeepAwakeRule.Identity(u) == "U:|Samsung SSD 970 EVO SCSI Disk Device");
        Check("keep-awake: CountSince counts resets after a moment", sw.CountSince(11, now.AddSeconds(-285 * 3)) == 3 && sw.CountSince(11, now) == 0 && sw.CountSince(99, now) == 0,
            "n=" + sw.CountSince(11, now.AddSeconds(-285 * 3)));
        u.Bus = "SATA";
        Check("keep-awake: a SATA disk never qualifies (the cure is for USB bridges)", !KeepAwakeRule.Qualifies(s11));
        u.Bus = "USB";
        StorageWatch few = new StorageWatch();
        for (int i = 0; i < 3; i++) few.Add(11, now.AddSeconds(-285 * i - 60));
        Check("keep-awake: fewer than four resets is not a signature", !KeepAwakeRule.Qualifies(few.Summaries(now, disks)[0]));
        StorageWatch random = new StorageWatch();
        Random rr = new Random(3);
        DateTime ra = now.AddHours(-20);
        for (int i = 0; i < 12; i++) { ra = ra.AddSeconds(rr.Next(60, 6000)); random.Add(11, ra); }
        Check("keep-awake: irregular resets (cable, power) do not qualify", !KeepAwakeRule.Qualifies(random.Summaries(now, disks)[0]));
        Dictionary<string, string> kav = new Dictionary<string, string>();
        kav["keep_awake"] = "off"; kav["keep_awake_sec"] = "5";
        List<string> kw = new List<string>();
        OptConfig kc = OptConfig.Parse(kav, kw);
        Check("keep-awake: config off honoured, out-of-range interval falls back", kc.KeepAwake == KeepAwakeModes.Off && kc.KeepAwakeSec == 45 && kw.Count == 1);
        Check("keep-awake: on (auto) by default", OptConfig.Defaults().KeepAwake == KeepAwakeModes.Auto);
        u.Gone = true;
        Check("keep-awake: a gone disk does not qualify", !KeepAwakeRule.Qualifies(s11));
        Check("storage: a vanished disk keeps its name, marked gone", s11.Who() == "U: Samsung SSD 970 EVO (USB) - gone");

        // Loss: one dropped enclosure = one incident.
        LossWatch lw = new LossWatch();
        DateTime d0 = new DateTime(2026, 9, 23, 23, 5, 56);
        lw.Add(d0, LossKinds.PagingError, "disk 8");
        lw.Add(d0, LossKinds.JournalFlush, "S:");
        for (int i = 0; i < 5; i++) lw.Add(d0.AddSeconds(3), LossKinds.LostWrite, i < 2 ? "S:" : (i < 4 ? "E:" : ""));
        lw.Add(d0.AddSeconds(4), LossKinds.JournalFlush, "E:");
        lw.Add(d0.AddMinutes(10), LossKinds.Removed, "disk 9");
        List<LossIncident> inc = lw.Incidents(d0.AddMinutes(20));
        Check("loss: events more than 2 min apart are separate incidents, newest first", inc.Count == 2 && inc[1].First == d0);
        LossIncident big = inc.Count == 2 ? inc[1] : null;
        Check("loss: an incident names its volumes, letters before disk numbers", big != null && big.Where() == "E: S:", big == null ? "" : big.Where());
        Check("loss: an incident counts what happened", big != null && big.LostWrites == 5 && big.JournalFlushes == 2 && big.PagingErrors == 1,
            big == null ? "" : big.What());
        Check("loss: failed writes put data at risk", big != null && big.DataAtRisk);
        Check("loss: a lone removal is a device warning, not data at risk", inc.Count == 2 && !inc[0].DataAtRisk && inc[0].Where() == "disk 9");
        Check("loss: incidents older than 24 h are dropped", lw.Incidents(d0.AddHours(25)).Count == 0);

        NewsGate ng = new NewsGate();
        Check("news: first time is told", ng.ShouldTell("storage:11", "active", 0, 60));
        Check("news: the same news is held back", !ng.ShouldTell("storage:11", "active", 30 * 60000L, 60));
        Check("news: a changed verdict is told at once", ng.ShouldTell("storage:11", "gone", 31 * 60000L, 60));
        Check("news: repeated after remind_min", ng.ShouldTell("storage:11", "gone", 92 * 60000L, 60));

        Check("status: data at risk wins over everything", StatusWords.Of(true, 3, false, ShellWords.Responsive, false, true) == StatusWords.Attention);
        Check("status: a slow shell is ATTENTION", StatusWords.Of(false, 0, false, ShellWords.Slow, false) == StatusWords.Attention);
        Check("status: paused beats easing", StatusWords.Of(true, 2, false, ShellWords.Responsive, false) == StatusWords.Paused);
        Check("status: easing when something is eased", StatusWords.Of(false, 2, false, ShellWords.Responsive, false) == StatusWords.Easing);
        Check("status: calm otherwise", StatusWords.Of(false, 0, false, ShellWords.Responsive, false) == StatusWords.Calm);
        Check("explorer: 6000 GDI objects is near the limit", ExplorerLimits.Leaking(6000, 300) && !ExplorerLimits.Leaking(516, 482));
    }

    static void WordsTests()
    {
        Check("words: share of a core", Words.CoreShare(55) == "55% of a core" && Words.CoreShare(210) == "2.1 cores");
        Check("words: bytes", Words.Bytes(285L << 20) == "285 MB" && Words.Bytes(3L << 30) == "3.0 GB");
        Check("words: durations", Words.Duration(6000) == "6 s" && Words.Duration(285000) == "4m45s" && Words.Duration(3 * 3600000L) == "3 h"
            && Words.Duration(5 * 86400000L) == "5 d", Words.Duration(285000));
        string tip = Words.Tooltip(new string('x', 100));
        Check("words: tray tooltip never exceeds 63 characters", tip.Length == 63 && Words.Tooltip("SAIOPT: CALM") == "SAIOPT: CALM");
        Check("words: model names are shortened", Words.ShortModel("Samsung SSD 970 EVO SCSI Disk Device") == "Samsung SSD 970 EVO");
        ProcKey k;
        Check("identity: ProcKey round-trips through text", ProcKey.TryParse(new ProcKey(11700, 134346683699022206L).ToString(), out k)
            && k.Pid == 11700 && k.CreateTime == 134346683699022206L);
    }

    static int Main()
    {
        Config();
        Guards();
        EaseRules();
        Sweep();
        Watch();
        WordsTests();
        Console.WriteLine("SAIOPT_LOGIC checks=" + checks + " failures=" + failures);
        return failures == 0 ? 0 : 1;
    }
}
