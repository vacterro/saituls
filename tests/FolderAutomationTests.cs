using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using Saituls.FolderAutomation;

// Folder Automation — deterministic test suite.
//
// Compiled together with the engine sources (no GUI), run by
// tests\test_folder_automation.ps1. Every case is named after the guarantee it
// defends, and the exit code is the number of failures.

static class FaTests
{
    static int Passed;
    static readonly List<string> Failures = new List<string>();

    static void Check(string name, bool ok, string detail)
    {
        if (ok) { Passed++; Console.WriteLine("  ok   " + name); return; }
        Failures.Add(name + (string.IsNullOrEmpty(detail) ? "" : " -- " + detail));
        Console.WriteLine("  FAIL " + name + (string.IsNullOrEmpty(detail) ? "" : " -- " + detail));
    }

    static void Eq(string name, object expected, object actual)
    {
        bool ok = Equals(expected, actual);
        Check(name, ok, ok ? "" : "expected [" + expected + "] got [" + actual + "]");
    }

    // ------------------------------------------------------------- builders

    static FaFileInfo F(string path, long length)
    {
        FaFileInfo f = new FaFileInfo();
        f.FullPath = FaPaths.Normalize(path);
        f.Name = Path.GetFileName(f.FullPath);
        f.Directory = Path.GetDirectoryName(f.FullPath);
        f.Extension = Path.GetExtension(f.Name).ToLowerInvariant();
        f.Length = length;
        f.ModifiedUtc = new DateTime(2026, 10, 1, 12, 0, 0, DateTimeKind.Utc);
        f.CreatedUtc = f.ModifiedUtc;
        return f;
    }

    static FaCondition Cond(string kind, string value)
    {
        return new FaCondition { Kind = kind, Value = value };
    }

    static FaCondition Conds(string kind, params string[] values)
    {
        return new FaCondition { Kind = kind, Values = values };
    }

    static FaRule Rule(string name, FaAction action, string destination, params FaCondition[] conditions)
    {
        FaRule r = new FaRule();
        r.Id = FaStore.NewId();
        r.Name = name;
        r.Action = action;
        r.Destination = destination;
        foreach (FaCondition c in conditions) r.Conditions.Add(c);
        return r;
    }

    static FaProfile Profile(string name, string source, params FaRule[] rules)
    {
        FaProfile p = new FaProfile();
        p.Id = FaStore.NewId();
        p.Name = name;
        p.Enabled = true;
        p.Trigger = FaTrigger.Manual;
        p.Schedule.Trigger = FaTrigger.Manual;
        p.Sources.Add(source);
        foreach (FaRule r in rules) p.Rules.Add(r);
        return p;
    }

    static FakeFileSystem FakeFs()
    {
        FakeFileSystem fs = new FakeFileSystem();
        fs.AddDir("C:\\src");
        fs.AddDir("C:\\dst");
        fs.AddDir("D:\\dst");
        return fs;
    }

    static string TmpDir(string tag)
    {
        string p = Path.Combine(Path.GetTempPath(), "saituls-fa-" + tag + "-" +
            Guid.NewGuid().ToString("N").Substring(0, 8));
        Directory.CreateDirectory(p);
        return p;
    }

    static FaEngine Engine(IFileSystem fs, FaConfig cfg, string journalDir)
    {
        FaLog log = new FaLog(Path.Combine(journalDir, "diagnostic.log"));
        FaEngine e = new FaEngine(fs, log, new FaJournal(journalDir));
        e.Config = cfg;
        return e;
    }

    // --------------------------------------------------------------- cases

    static void Case01_ExtensionRule()
    {
        FaProfile p = Profile("t", "C:\\src",
            Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg", ".png")));
        Check("01 extension rule match",
            FaMatcher.Evaluate(p, F("C:\\src\\photo.JPG", 10), DateTime.UtcNow).Outcome == FaOutcome.Matched, "");
        Check("01 extension rule miss",
            FaMatcher.Evaluate(p, F("C:\\src\\notes.txt", 10), DateTime.UtcNow).Outcome == FaOutcome.NoRule, "");
    }

    static void Case02_FilenameRule()
    {
        FaProfile p = Profile("t", "C:\\src",
            Rule("Named", FaAction.Move, "C:\\dst", Cond("filename", "report")));
        Check("02 filename rule",
            FaMatcher.Evaluate(p, F("C:\\src\\report", 10), DateTime.UtcNow).Outcome == FaOutcome.Matched &&
            FaMatcher.Evaluate(p, F("C:\\src\\report2", 10), DateTime.UtcNow).Outcome == FaOutcome.NoRule, "");
    }

    static void Case03_GlobRule()
    {
        FaProfile p = Profile("t", "C:\\src",
            Rule("Clipboard", FaAction.Move, "C:\\dst", Cond("glob", "clipboard_*.png")));
        Check("03 glob rule",
            FaMatcher.Evaluate(p, F("C:\\src\\clipboard_a.png", 10), DateTime.UtcNow).Outcome == FaOutcome.Matched &&
            FaMatcher.Evaluate(p, F("C:\\src\\other.png", 10), DateTime.UtcNow).Outcome == FaOutcome.NoRule, "");
    }

    static void Case04_RegexRule()
    {
        FaProfile p = Profile("t", "C:\\src",
            Rule("Re", FaAction.Move, "C:\\dst", Cond("regex", "^IMG_\\d+\\.jpg$")));
        Check("04 regex rule",
            FaMatcher.Evaluate(p, F("C:\\src\\IMG_12.jpg", 10), DateTime.UtcNow).Outcome == FaOutcome.Matched &&
            FaMatcher.Evaluate(p, F("C:\\src\\IMG_12.png", 10), DateTime.UtcNow).Outcome == FaOutcome.NoRule &&
            FaMatcher.Evaluate(p, F("C:\\src\\DSC_12.jpg", 10), DateTime.UtcNow).Outcome == FaOutcome.NoRule, "");

        FaProfile broken = Profile("t", "C:\\src",
            Rule("Bad", FaAction.Move, "C:\\dst", Cond("regex", "([unclosed")));
        Check("04 malformed regex never matches and never throws",
            FaMatcher.Evaluate(broken, F("C:\\src\\x.jpg", 10), DateTime.UtcNow).Outcome == FaOutcome.NoRule, "");
    }

    static void Case05_AllConditions()
    {
        FaRule r = Rule("R", FaAction.Move, "C:\\dst",
            Cond("filename_startswith", "clipboard_"), Conds("extension", ".png", ".jpg"));
        FaProfile p = Profile("t", "C:\\src", r);
        Check("05 ALL conditions",
            FaMatcher.Evaluate(p, F("C:\\src\\clipboard_a.png", 10), DateTime.UtcNow).Outcome == FaOutcome.Matched &&
            FaMatcher.Evaluate(p, F("C:\\src\\clipboard_a.gif", 10), DateTime.UtcNow).Outcome == FaOutcome.NoRule, "");
    }

    static void Case06_AnyConditions()
    {
        FaRule r = Rule("R", FaAction.Move, "C:\\dst", Conds("extension", ".zip"), Conds("extension", ".rar"));
        r.Match = FaMatch.Any;
        FaProfile p = Profile("t", "C:\\src", r);
        Check("06 ANY conditions",
            FaMatcher.Evaluate(p, F("C:\\src\\a.rar", 10), DateTime.UtcNow).Outcome == FaOutcome.Matched &&
            FaMatcher.Evaluate(p, F("C:\\src\\a.jpg", 10), DateTime.UtcNow).Outcome == FaOutcome.NoRule, "");
    }

    static void Case07_Exclusions()
    {
        FaRule ex = Rule("Never", FaAction.Ignore, "", Conds("extension", ".iso"));
        ex.Match = FaMatch.Exclude;
        FaProfile p = Profile("t", "C:\\src", ex, Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg")));
        Check("07 exclude rule wins over a later match",
            FaMatcher.Evaluate(p, F("C:\\src\\disk.iso", 10), DateTime.UtcNow).Outcome == FaOutcome.Excluded, "");

        p.Exclusions.Add("*.iso");
        FaProfile p2 = Profile("t2", "C:\\src", Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".iso")));
        p2.Exclusions.Add("*.iso");
        Check("07 profile exclusion pattern",
            FaMatcher.Evaluate(p2, F("C:\\src\\disk.iso", 10), DateTime.UtcNow).Outcome == FaOutcome.GlobSkipped, "");
    }

    static void Case08_FirstMatchOrder()
    {
        FaRule first = Rule("First", FaAction.Move, "C:\\dst\\first", Conds("extension", ".jpg"));
        FaRule second = Rule("Second", FaAction.Move, "C:\\dst\\second", Conds("extension", ".jpg"));
        FaProfile a = Profile("a", "C:\\src", first, second);
        FaProfile b = Profile("b", "C:\\src", second, first);
        Eq("08 first match wins (order a)",
            "First", FaMatcher.Evaluate(a, F("C:\\src\\x.jpg", 1), DateTime.UtcNow).Rule.Name);
        Eq("08 first match wins (order b)",
            "Second", FaMatcher.Evaluate(b, F("C:\\src\\x.jpg", 1), DateTime.UtcNow).Rule.Name);
    }

    static void Case09_TempFiles()
    {
        FaProfile p = Profile("t", "C:\\src", Rule("Any", FaAction.Move, "C:\\dst", Conds("extension", ".crdownload", ".jpg")));
        Eq("09 temporary file skipped", FaOutcome.TempSkipped,
            FaMatcher.Evaluate(p, F("C:\\src\\big.crdownload", 1), DateTime.UtcNow).Outcome);

        p.OverrideTempPatterns = true;
        Eq("09 rule may override the temporary list", FaOutcome.Matched,
            FaMatcher.Evaluate(p, F("C:\\src\\big.crdownload", 1), DateTime.UtcNow).Outcome);
    }

    static void Case10_StabilityGate()
    {
        FakeFileSystem fs = FakeFs();
        fs.AddFile("C:\\src\\a.jpg", 100);
        DateTime t = new DateTime(2026, 10, 4, 12, 0, 0, DateTimeKind.Utc);
        FaStabilityGate gate = new FaStabilityGate(fs);
        gate.Clock = delegate { return t; };

        Eq("10 first observation is not yet stable", FaStability.NotYet, gate.Check("C:\\src\\a.jpg", 5, null));
        t = t.AddSeconds(3);
        Eq("10 still writing inside the quiet period", FaStability.NotYet, gate.Check("C:\\src\\a.jpg", 5, null));
        t = t.AddSeconds(4);
        Eq("10 stable once size and mtime hold for the quiet period", FaStability.Stable, gate.Check("C:\\src\\a.jpg", 5, null));

        fs.Files["C:\\src\\a.jpg"] = 200;
        t = t.AddSeconds(1);
        Eq("10 a size change resets the quiet period", FaStability.NotYet, gate.Check("C:\\src\\a.jpg", 5, null));
        Eq("10 a missing file is dropped", FaStability.Missing, gate.Check("C:\\src\\gone.jpg", 5, null));
    }

    static void Case11_SameVolumeMove()
    {
        FakeFileSystem fs = FakeFs();
        fs.AddFile("C:\\src\\a.jpg", 10);
        FaOpPlan plan = new FaOpPlan { Source = "C:\\src\\a.jpg", DestinationDir = "C:\\dst", Action = FaAction.Move };
        FaOpResult r = FaActions.Execute(fs, plan, null, "t");
        Check("11 same-volume move is a rename, not a copy",
            r.Success && r.Undo == FaUndoKind.MoveBack && fs.Calls.Contains("move C:\\src\\a.jpg -> C:\\dst\\a.jpg") &&
            !HasCall(fs, "copy"), string.Join(" | ", fs.Calls.ToArray()));
    }

    static bool HasCall(FakeFileSystem fs, string prefix)
    {
        foreach (string c in fs.Calls) if (c.StartsWith(prefix, StringComparison.Ordinal)) return true;
        return false;
    }

    static void Case12_CrossVolumeMove()
    {
        FakeFileSystem fs = FakeFs();
        fs.AddFile("C:\\src\\a.jpg", 10);
        FaOpPlan plan = new FaOpPlan
        {
            Source = "C:\\src\\a.jpg", DestinationDir = "D:\\dst", Action = FaAction.Move,
            Verify = FaVerify.Size, IncomingBytes = 10
        };
        FaOpResult r = FaActions.Execute(fs, plan, null, "t");
        int copyAt = IndexOfCall(fs, "copy"), deleteAt = IndexOfCall(fs, "delete");
        Check("12 cross-volume move copies, verifies, then deletes the source",
            r.Success && copyAt >= 0 && deleteAt > copyAt, string.Join(" | ", fs.Calls.ToArray()));

        FakeFileSystem fs2 = FakeFs();
        fs2.AddFile("C:\\src\\a.jpg", 10);
        fs2.HashOf = delegate(string p) { return "different-" + p; };
        FaOpPlan plan2 = new FaOpPlan
        {
            Source = "C:\\src\\a.jpg", DestinationDir = "D:\\dst", Action = FaAction.Move,
            Verify = FaVerify.Hash, IncomingBytes = 10
        };
        FaOpResult r2 = FaActions.Execute(fs2, plan2, null, "t");
        Check("12 a failed verification never deletes the source",
            !r2.Success && IndexOfCall(fs2, "delete") < 0, string.Join(" | ", fs2.Calls.ToArray()));
    }

    static int IndexOfCall(FakeFileSystem fs, string prefix)
    {
        for (int i = 0; i < fs.Calls.Count; i++)
            if (fs.Calls[i].StartsWith(prefix, StringComparison.Ordinal)) return i;
        return -1;
    }

    static void Case13_CollisionSkip()
    {
        FakeFileSystem fs = FakeFs();
        fs.AddFile("C:\\src\\a.jpg", 10);
        fs.AddFile("C:\\dst\\a.jpg", 99);
        FaOpPlan plan = new FaOpPlan
        {
            Source = "C:\\src\\a.jpg", DestinationDir = "C:\\dst", Action = FaAction.Move,
            Collision = FaCollision.Skip
        };
        FaOpResult r = FaActions.Execute(fs, plan, null, "t");
        Check("13 collision skip leaves both files untouched",
            r.Skipped && fs.Mutations.Count() == 0 && fs.Files.ContainsKey("C:\\dst\\a.jpg") &&
            fs.Files.ContainsKey("C:\\src\\a.jpg"), string.Join(" | ", fs.Calls.ToArray()));
    }

    static void Case14_CollisionCounter()
    {
        FakeFileSystem fs = FakeFs();
        fs.AddFile("C:\\src\\photo.jpg", 10);
        fs.AddFile("C:\\dst\\photo.jpg", 99);
        FaOpPlan plan = new FaOpPlan
        {
            Source = "C:\\src\\photo.jpg", DestinationDir = "C:\\dst", Action = FaAction.Move,
            Collision = FaCollision.Counter
        };
        FaOpResult r = FaActions.Execute(fs, plan, null, "t");
        Check("14 counter naming keeps the extension",
            r.Success && r.FinalPath.EndsWith("photo (1).jpg", StringComparison.Ordinal), r.FinalPath);
    }

    static void Case15_NoOverwriteByDefault()
    {
        Eq("15 default collision policy is the counter, never replace",
            FaCollision.Counter, new FaProfile().Collision);

        FakeFileSystem fs = FakeFs();
        fs.AddFile("C:\\src\\photo.jpg", 10);
        fs.AddFile("C:\\dst\\photo.jpg", 1);
        fs.AddFile("C:\\dst\\photo (1).jpg", 1);
        FaOpPlan plan = new FaOpPlan
        {
            Source = "C:\\src\\photo.jpg", DestinationDir = "C:\\dst", Action = FaAction.Move,
            Collision = new FaProfile().Collision
        };
        FaOpResult r = FaActions.Execute(fs, plan, null, "t");
        Check("15 nothing is overwritten without an explicit policy",
            r.FinalPath.EndsWith("photo (2).jpg", StringComparison.Ordinal) &&
            fs.Files["C:\\dst\\photo.jpg"] == 1 && fs.Files["C:\\dst\\photo (1).jpg"] == 1, r.FinalPath);
    }

    static void Case16_RecursiveSource()
    {
        string root = TmpDir("rec");
        try
        {
            Directory.CreateDirectory(Path.Combine(root, "sub", "deeper"));
            File.WriteAllText(Path.Combine(root, "top.jpg"), "x");
            File.WriteAllText(Path.Combine(root, "sub", "mid.jpg"), "x");
            File.WriteAllText(Path.Combine(root, "sub", "deeper", "low.jpg"), "x");

            FaProfile deep = Profile("deep", root);
            deep.Recursive = true;
            FaProfile flat = Profile("flat", root);
            flat.Recursive = false;
            IFileSystem fs = new SystemFileSystem();
            Check("16 recursive source sees nested files",
                new FaSourceScanner(deep, fs).Enumerate().Count == 3, "");
            Check("16 non-recursive source sees only the top level",
                new FaSourceScanner(flat, fs).Enumerate().Count == 1, "");
        }
        finally { TryDelete(root); }
    }

    static void TryDelete(string dir)
    {
        try { if (Directory.Exists(dir)) Directory.Delete(dir, true); } catch { }
    }

    static void Case17_DestinationInsideSource()
    {
        FaProfile p = Profile("loop", "D:\\Downloads",
            Rule("Images", FaAction.Move, "D:\\Downloads\\Images", Conds("extension", ".jpg")));
        p.Recursive = true;
        bool blocked = false;
        foreach (FaTopologyIssue i in FaTopology.Inspect(p)) if (i.Blocking) blocked = true;
        Check("17 destination inside a recursive source is rejected", blocked, "");

        p.Exclusions.Add("Images");
        bool stillBlocked = false;
        foreach (FaTopologyIssue i in FaTopology.Inspect(p)) if (i.Blocking) stillBlocked = true;
        Check("17 an explicit exclusion of the destination makes it safe", !stillBlocked, "");
    }

    static void Case18_ProfileCycle()
    {
        FaConfig cfg = new FaConfig();
        FaProfile a = Profile("A", "C:\\a", Rule("r", FaAction.Move, "C:\\b", Conds("extension", ".txt")));
        FaProfile b = Profile("B", "C:\\b", Rule("r", FaAction.Move, "C:\\a", Conds("extension", ".txt")));
        cfg.Profiles.Add(a); cfg.Profiles.Add(b);
        List<string> cycles = FaTopology.CheckCycles(cfg);
        Check("18 A->B / B->A is detected", cycles.Count > 0, string.Join("; ", cycles.ToArray()));

        cfg.Profiles[1].Enabled = false;
        Eq("18 a disabled profile cannot form a cycle", 0, FaTopology.CheckCycles(cfg).Count);
    }

    static void Case19_ReparsePoints()
    {
        Eq("19 reparse following is off by default", false, new FaProfile().FollowReparse);

        string root = TmpDir("reparse");
        try
        {
            Directory.CreateDirectory(Path.Combine(root, "real"));
            Directory.CreateDirectory(Path.Combine(root, "junction"));
            File.WriteAllText(Path.Combine(root, "real", "a.jpg"), "x");
            File.WriteAllText(Path.Combine(root, "junction", "b.jpg"), "x");

            FaProfile p = Profile("t", root);
            p.Recursive = true;
            IFileSystem fs = new SystemFileSystem();
            FaSourceScanner scanner = new FaSourceScanner(p, fs);
            scanner.IsReparse = delegate(string path)
            {
                return string.Equals(FaPaths.Normalize(path), FaPaths.Normalize(Path.Combine(root, "junction")), StringComparison.OrdinalIgnoreCase);
            };
            List<string> files = scanner.Enumerate();
            Check("19 a junction is skipped and reported",
                files.Count == 1 && scanner.LastRunHitReparsePoint, "files=" + files.Count);

            p.FollowReparse = true;
            FaSourceScanner opt = new FaSourceScanner(p, fs);
            opt.IsReparse = scanner.IsReparse;
            Eq("19 following reparse points is an explicit opt-in", 2, opt.Enumerate().Count);
        }
        finally { TryDelete(root); }
    }

    static void Case20_DryRunMutatesNothing()
    {
        FakeFileSystem fs = FakeFs();
        fs.AddFile("C:\\src\\a.jpg", 10);
        fs.AddFile("C:\\src\\b.png", 10);
        fs.AddFile("C:\\src\\c.txt", 10);
        FaConfig cfg = new FaConfig();
        cfg.Profiles.Add(Profile("t", "C:\\src", Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg", ".png"))));
        string jd = TmpDir("dry");
        try
        {
            FaEngine e = Engine(fs, cfg, jd);
            e.Prepare();
            FaRunReport rep = e.RunProfile(cfg.Profiles[0].Id, true, "test");
            Check("20 dry run reports what it would do",
                rep.Actions == 2 && rep.Items[0].Destination == FaPaths.Normalize("C:\\dst\\a.jpg") &&
                rep.Items[0].Outcome == "planned", rep.Summary() + " first=" + rep.Items[0].Line());
            Check("20 dry run performs zero mutations",
                fs.Mutations.Count() == 0, string.Join(" | ", fs.Mutations.ToArray()));
        }
        finally { TryDelete(jd); }
    }

    static void Case21_DisabledProfileMutatesNothing()
    {
        FakeFileSystem fs = FakeFs();
        fs.AddFile("C:\\src\\a.jpg", 10);
        FaConfig cfg = new FaConfig();
        FaProfile p = Profile("t", "C:\\src", Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg")));
        p.Enabled = false;
        cfg.Profiles.Add(p);
        string jd = TmpDir("off");
        try
        {
            FaEngine e = Engine(fs, cfg, jd);
            e.Prepare();
            FaRunReport rep = e.RunProfile(p.Id, false, "test");
            Check("21 a disabled profile performs zero mutations",
                rep.Status == "Disabled" && fs.Mutations.Count() == 0, rep.Summary());
        }
        finally { TryDelete(jd); }
    }

    static void Case22_MaxOperations()
    {
        FakeFileSystem fs = FakeFs();
        for (int i = 0; i < 6; i++) fs.AddFile("C:\\src\\f" + i + ".jpg", 10);
        FaConfig cfg = new FaConfig();
        FaProfile p = Profile("t", "C:\\src", Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg")));
        p.Limits.MaxOps = 2;
        cfg.Profiles.Add(p);
        string jd = TmpDir("maxops");
        try
        {
            FaEngine e = Engine(fs, cfg, jd);
            e.Prepare();
            FaRunReport rep = e.RunProfile(p.Id, false, "test");
            FaProfileRuntime rt = e.Runtime(p.Id);
            Check("22 the operation limit stops the run",
                rep.Actions == 2 && !string.IsNullOrEmpty(rep.PausedReason), rep.Summary());
            Check("22 the brake pauses that profile, not the engine",
                rt != null && rt.SafetyPaused && !e.PausedAll, "");
            Check("22 the other files were left alone", fs.Files.ContainsKey("C:\\src\\f5.jpg"), "");
        }
        finally { TryDelete(jd); }
    }

    static void Case23_MissingSource()
    {
        FakeFileSystem fs = new FakeFileSystem();     // no C:\src at all
        FaConfig cfg = new FaConfig();
        FaProfile p = Profile("t", "C:\\src", Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg")));
        cfg.Profiles.Add(p);
        string jd = TmpDir("missing");
        try
        {
            FaEngine e = Engine(fs, cfg, jd);
            e.Prepare();
            FaRunReport rep = e.RunProfile(p.Id, false, "test");
            Check("23 a missing source is reported, not fatal",
                rep.Status == "Source unavailable" && fs.Mutations.Count() == 0, rep.Status);
            Check("23 the profile itself survives",
                e.Config.Profiles.Count == 1 && e.Config.Profiles[0].Name == "t", "");
        }
        finally { TryDelete(jd); }
    }

    static void Case24_SourceComesBack()
    {
        string src = TmpDir("gone");
        string dst = TmpDir("back");
        try
        {
            FaConfig cfg = new FaConfig();
            FaProfile p = Profile("t", src, Rule("Images", FaAction.Move, dst, Conds("extension", ".jpg")));
            p.Schedule.Trigger = FaTrigger.Realtime;
            p.ReconcileMinutes = 0;
            p.QuietSeconds = 0;
            cfg.Profiles.Add(p);
            string jd = TmpDir("gonej");
            try
            {
                SystemFileSystem fs = new SystemFileSystem();
                FaEngine e = Engine(fs, cfg, jd);
                e.Prepare();
                Check("24 a present source starts watching",
                    e.Runtime(p.Id).Status == "Watching", e.Runtime(p.Id).Status);

                Directory.Delete(src, true);
                e.Tick();
                Eq("24 a missing source is Source unavailable", "Source unavailable", e.Runtime(p.Id).Status);
                Check("24 the profile is not removed", e.Config.Profiles.Count == 1, "");

                Directory.CreateDirectory(src);
                e.Tick();
                Eq("24 the watcher resumes when the source returns", "Watching", e.Runtime(p.Id).Status);
                e.Stop();
            }
            finally { TryDelete(jd); }
        }
        finally { TryDelete(src); TryDelete(dst); }
    }

    static void Case25_DuplicateEvents()
    {
        FakeFileSystem fs = FakeFs();
        FaConfig cfg = new FaConfig();
        FaProfile p = Profile("t", "C:\\src", Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg")));
        cfg.Profiles.Add(p);
        string jd = TmpDir("dup");
        try
        {
            FaEngine e = Engine(fs, cfg, jd);
            e.Prepare();
            FaProfileRuntime rt = e.Runtime(p.Id);
            e.QueueCandidate(rt, "C:\\src\\a.jpg");
            e.QueueCandidate(rt, "C:\\src\\a.jpg");
            e.QueueCandidate(rt, "C:\\src\\a.jpg");
            Eq("25 duplicate watcher events are coalesced into one candidate", 1, rt.Queue.Count);
        }
        finally { TryDelete(jd); }
    }

    static void Case26_OverflowToReconcile()
    {
        string src = TmpDir("ovf");
        string dst = TmpDir("ovfd");
        try
        {
            File.WriteAllText(Path.Combine(src, "a.jpg"), "x");
            File.WriteAllText(Path.Combine(src, "b.jpg"), "x");
            FaConfig cfg = new FaConfig();
            FaProfile p = Profile("t", src, Rule("Images", FaAction.Move, dst, Conds("extension", ".jpg")));
            p.Schedule.Trigger = FaTrigger.Realtime;
            p.ReconcileMinutes = 0;
            cfg.Profiles.Add(p);
            string jd = TmpDir("ovfj");
            try
            {
                FaEngine e = Engine(new SystemFileSystem(), cfg, jd);
                e.Prepare();
                FaProfileRuntime rt = e.Runtime(p.Id);
                rt.Queue.Clear();

                int reconciles = 0;
                FaWatcher probe = new FaWatcher(p, delegate { }, delegate { reconciles++; },
                    new FaLog(Path.Combine(jd, "d.log")));
                probe.RaiseOverflow("synthetic overflow for the test");
                Eq("26 a watcher overflow asks for a reconciliation", 1, reconciles);

                e.Reconcile(rt, "watcher overflow");
                int queued = rt.Queue.Count;
                Check("26 reconciliation finds files the dropped events missed",
                    queued >= 2, "queued=" + queued);
                probe.Dispose();
                e.Stop();
            }
            finally { TryDelete(jd); }
        }
        finally { TryDelete(src); TryDelete(dst); }
    }

    static void Case27_ScheduleMath()
    {
        FaProfile hourly = Profile("h", "C:\\src");
        hourly.Schedule.Trigger = FaTrigger.Hourly;
        DateTime now = new DateTime(2026, 10, 4, 10, 30, 0);
        Eq("27 hourly lands on the next full hour",
            new DateTime(2026, 10, 4, 11, 0, 0), FaScheduleCalculator.NextSlot(hourly, now, null));

        FaProfile daily = Profile("d", "C:\\src");
        daily.Schedule.Trigger = FaTrigger.Daily;
        daily.Schedule.At = "03:30";
        Eq("27 daily today when still ahead, tomorrow otherwise",
            new DateTime(2026, 10, 5, 3, 30, 0), FaScheduleCalculator.NextSlot(daily, now, null));
        Eq("27 daily later today",
            new DateTime(2026, 10, 4, 3, 30, 0),
            FaScheduleCalculator.NextSlot(daily, new DateTime(2026, 10, 4, 1, 0, 0), null));

        FaProfile weekly = Profile("w", "C:\\src");
        weekly.Schedule.Trigger = FaTrigger.Weekly;
        weekly.Schedule.Weekday = 0;                 // Sunday
        weekly.Schedule.At = "09:00";
        Eq("27 weekly lands on the configured weekday",
            new DateTime(2026, 10, 11, 9, 0, 0), FaScheduleCalculator.NextSlot(weekly, now, null));

        FaProfile every = Profile("e", "C:\\src");
        every.Schedule.Trigger = FaTrigger.EveryMinutes;
        every.Schedule.EveryMinutes = 15;
        Eq("27 every N minutes counts from the last run",
            new DateTime(2026, 10, 4, 10, 20, 0),
            FaScheduleCalculator.NextSlot(every, now, new DateTime(2026, 10, 4, 10, 5, 0)));

        FaProfile manual = Profile("m", "C:\\src");
        manual.Schedule.Trigger = FaTrigger.Manual;
        Eq("27 manual only has no next slot", null, FaScheduleCalculator.NextSlot(manual, now, null));
    }

    static void Case28_MissedRuns()
    {
        FaProfile p = Profile("h", "C:\\src");
        p.Schedule.Trigger = FaTrigger.Hourly;
        DateTime now = new DateTime(2026, 10, 4, 18, 0, 0);
        DateTime last = new DateTime(2026, 10, 4, 6, 0, 0);

        p.Schedule.Missed = FaMissed.RunOnce;
        FaTriggerDecision d = FaScheduleCalculator.Evaluate(p, now, last);
        Check("28 twelve missed hourly runs become exactly one run",
            d.RunNow && d.CatchUp && d.Reason.Contains("coalesced"), d.Reason);

        p.Schedule.Missed = FaMissed.Skip;
        FaTriggerDecision s = FaScheduleCalculator.Evaluate(p, now, last);
        Check("28 missed runs can be skipped entirely", !s.RunNow, s.Reason);

        p.Schedule.Missed = FaMissed.RunAsap;
        FaTriggerDecision a = FaScheduleCalculator.Evaluate(p, now, last);
        Check("28 run-as-soon-as-possible is still a single run", a.RunNow, a.Reason);
        Eq("28 missed count is reported, not silently ignored", 12,
            FaScheduleCalculator.MissedCount(p, now, last));
    }

    static void Case29_ConcurrentTriggers()
    {
        FaRunGate gate = new FaRunGate("t");
        Check("29 the first trigger enters", gate.TryEnter(), "");
        Check("29 a second trigger cannot enter", !gate.TryEnter(), "");
        Check("29 the second trigger is recorded for a rerun", gate.RequestRerun(), "");
        Check("29 the rerun is pending", gate.ConsumeRerun(), "");
        gate.Exit();
        Check("29 the gate frees after the run", gate.TryEnter(), "");

        FaProfile a = Profile("A", "C:\\a", Rule("r", FaAction.Move, "C:\\shared", Conds("extension", ".txt")));
        FaProfile b = Profile("B", "C:\\b", Rule("r", FaAction.Move, "C:\\shared", Conds("extension", ".txt")));
        Check("29 profiles with a shared destination are treated as overlapping",
            FaTopology.Overlaps(a, b), "");

        FaProfile c = Profile("C", "C:\\c", Rule("r", FaAction.Move, "C:\\d", Conds("extension", ".txt")));
        FaProfile d = Profile("D", "C:\\e", Rule("r", FaAction.Move, "C:\\f", Conds("extension", ".txt")));
        Check("29 disjoint profiles do not overlap", !FaTopology.Overlaps(c, d), "");
    }

    static void Case30_MalformedConfig()
    {
        string dir = TmpDir("badcfg");
        try
        {
            Environment.SetEnvironmentVariable("SAITULS_FA_DIR", dir);
            File.WriteAllText(FaPaths.ConfigPath(), "{ this is not json ]]");
            FaStore.LoadResult r = FaStore.Load();
            Check("30 a malformed config reports an error instead of crashing",
                !string.IsNullOrEmpty(r.Error) && r.Config != null, r.Error);
            Check("30 the bad file is preserved for diagnosis",
                r.PreservedBadCopy && File.Exists(FaPaths.BadConfigPath()), "");
            Check("30 the user configuration is not silently reset",
                File.ReadAllText(FaPaths.ConfigPath()).Contains("this is not json"), "");
        }
        finally
        {
            Environment.SetEnvironmentVariable("SAITULS_FA_DIR", null);
            TryDelete(dir);
        }
    }

    static void Case31_AtomicSave()
    {
        string dir = TmpDir("atomic");
        try
        {
            Environment.SetEnvironmentVariable("SAITULS_FA_DIR", dir);
            FaConfig cfg = new FaConfig();
            FaProfile p = Profile("Downloads", "C:\\src", Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg")));
            cfg.Profiles.Add(p);
            FaStore.Save(cfg);
            FaStore.Save(cfg);                      // second save must replace, not append
            Check("31 no temp file is left behind",
                !File.Exists(FaPaths.ConfigPath() + ".tmp"), "");
            FaStore.LoadResult r = FaStore.Load();
            Check("31 the saved config reads back intact",
                r.Error == null && r.Config.Profiles.Count == 1 &&
                r.Config.Profiles[0].Rules[0].Conditions[0].Values.Length == 1,
                r.Error ?? "profiles=" + (r.Config == null ? -1 : r.Config.Profiles.Count));
        }
        finally
        {
            Environment.SetEnvironmentVariable("SAITULS_FA_DIR", null);
            TryDelete(dir);
        }
    }

    static void Case32_JournalPersistence()
    {
        string dir = TmpDir("journal");
        try
        {
            FaJournal j = new FaJournal(dir);
            for (int i = 0; i < 3; i++)
            {
                FaJournalEntry e = new FaJournalEntry();
                e.OpId = "op" + i;
                e.Timestamp = FaJournal.Now();
                e.Source = "C:\\src\\f" + i + ".jpg";
                e.Action = "Move";
                e.Result = "completed";
                e.Undo = "MoveBack";
                e.UndoState = "available";
                j.Append(e);
            }
            FaJournal again = new FaJournal(dir);          // "restart"
            List<FaJournalEntry> recent = again.Recent(10);
            Check("32 history survives a restart", recent.Count == 3, "count=" + recent.Count);
            Check("32 history is newest first", recent[0].OpId == "op2", recent[0].OpId);
        }
        finally { TryDelete(dir); }
    }

    static void Case33_MoveUndo()
    {
        string dir = TmpDir("undo");
        try
        {
            FakeFileSystem fs = FakeFs();
            fs.AddFile("C:\\dst\\a.jpg", 10);
            FaJournal j = new FaJournal(dir);
            FaJournalEntry e = new FaJournalEntry();
            e.OpId = "op1";
            e.Timestamp = FaJournal.Now();
            e.Profile = "t";
            e.Source = "C:\\src\\a.jpg";
            e.Destination = "C:\\dst\\a.jpg";
            e.Action = "Move";
            e.Result = "completed";
            e.Undo = "MoveBack";
            e.UndoState = "available";
            e.OriginalDir = "C:\\src";
            e.OriginalName = "a.jpg";
            j.Append(e);

            FaUndoResult r = j.Undo(fs, "op1");
            Check("33 a move can be undone when the original location is free",
                r.Ok && fs.Files.ContainsKey("C:\\src\\a.jpg") && !fs.Files.ContainsKey("C:\\dst\\a.jpg"),
                r.Message);

            // The same undo twice must not claim success.
            FaUndoResult again = j.Undo(fs, "op1");
            Check("33 an already undone operation does not claim success",
                !again.Ok || j.Recent(1)[0].UndoState == "undone", again.Message);
        }
        finally { TryDelete(dir); }
    }

    static void Case34_RenameUndo()
    {
        string dir = TmpDir("renameundo");
        try
        {
            FakeFileSystem fs = FakeFs();
            fs.AddFile("C:\\src\\IMG_2026.jpg", 10);
            FaJournal j = new FaJournal(dir);
            FaJournalEntry e = new FaJournalEntry();
            e.OpId = "opR";
            e.Timestamp = FaJournal.Now();
            e.Source = "C:\\src\\photo.jpg";
            e.Destination = "C:\\src\\IMG_2026.jpg";
            e.Action = "Rename";
            e.Result = "completed";
            e.Undo = "RenameBack";
            e.UndoState = "available";
            e.OriginalDir = "C:\\src";
            e.OriginalName = "photo.jpg";
            j.Append(e);

            FaUndoResult r = j.Undo(fs, "opR");
            Check("34 a rename restores the previous name",
                r.Ok && fs.Files.ContainsKey("C:\\src\\photo.jpg") && !fs.Files.ContainsKey("C:\\src\\IMG_2026.jpg"),
                r.Message);

            FaJournalEntry recycle = new FaJournalEntry();
            recycle.OpId = "opX";
            recycle.Source = "C:\\src\\x.jpg";
            recycle.Destination = "C:\\dst\\x.jpg";
            recycle.Action = "Recycle";
            recycle.Result = "completed";
            recycle.Undo = "NotReversible";
            recycle.UndoState = "none";
            j.Append(recycle);
            FaUndoResult nr = j.Undo(fs, "opX");
            Check("34 a recycled file is never claimed as reversible",
                !nr.Ok && nr.Message.Contains("not reversible"), nr.Message);
        }
        finally { TryDelete(dir); }
    }

    static void Case35_FailedOperation()
    {
        string dir = TmpDir("fail");
        try
        {
            FakeFileSystem fs = FakeFs();               // source file does not exist
            FaOpPlan plan = new FaOpPlan
            {
                Source = "C:\\src\\gone.jpg", DestinationDir = "C:\\dst", Action = FaAction.Move
            };
            FaOpResult r = FaActions.Execute(fs, plan, null, "t");
            Check("35 a failed operation reports failure, not success", !r.Success && !string.IsNullOrEmpty(r.Message), r.Message);

            FaEngine e = Engine(fs, new FaConfig(), dir);
            Check("35 a failed action leaves no undo claim", !r.Reversible, r.Undo.ToString());
        }
        finally { TryDelete(dir); }
    }

    static void Case36_RestartDoesNotDuplicate()
    {
        string src = TmpDir("rst");
        string dst = TmpDir("rstd");
        string jd = TmpDir("rstj");
        try
        {
            File.WriteAllText(Path.Combine(src, "a.jpg"), "hello");
            FaConfig cfg = new FaConfig();
            FaProfile p = Profile("t", src, Rule("Images", FaAction.Move, dst, Conds("extension", ".jpg")));
            cfg.Profiles.Add(p);

            SystemFileSystem fs = new SystemFileSystem();
            FaEngine first = Engine(fs, cfg, jd);
            first.Prepare();
            FaRunReport one = first.RunProfile(p.Id, false, "test");
            first.Stop();

            Eq("36 the first run moved exactly one file", 1, one.Actions);
            Check("36 the file is really gone from the source",
                !File.Exists(Path.Combine(src, "a.jpg")), "");

            FaEngine second = Engine(fs, cfg, jd);          // "restart"
            second.Prepare();
            FaRunReport two = second.RunProfile(p.Id, false, "test");
            second.Stop();
            Eq("36 a restart does not repeat a completed operation", 0, two.Actions);

            int completed = 0;
            foreach (FaJournalEntry e in new FaJournal(jd).Recent(50))
                if (e.Result == "completed" && e.Action == "Move") completed++;
            Eq("36 the journal holds one completed move", 1, completed);
            Check("36 nothing is left ambiguous by a clean exit",
                new FaJournal(jd).Ambiguous().Count == 0, "");
        }
        finally { TryDelete(src); TryDelete(dst); TryDelete(jd); }
    }

    // ------------------------------------------------------------- extras

    static void Case37_VariableExpansion()
    {
        FaProfile p = Profile("Media", "C:\\src");
        FaRule r = Rule("Images", FaAction.Move, "D:\\Media\\{year}\\{month}\\Images", Conds("extension", ".jpg"));
        p.Rules.Add(r);
        FaDecision d = FaMatcher.Evaluate(p, F("C:\\src\\photo.jpg", 10),
            new DateTime(2026, 3, 7, 10, 0, 0, DateTimeKind.Utc));
        Eq("37 variables expand in a destination",
            FaPaths.Normalize("D:\\Media\\2026\\03\\Images"), d.Destination);

        FaRule rn = Rule("R", FaAction.Rename, "", Conds("extension", ".jpg"));
        rn.RenameTemplate = "{year}{month}{day}_{name}{dot_ext}";
        FaProfile p2 = Profile("p2", "C:\\src", rn);
        FaDecision d2 = FaMatcher.Evaluate(p2, F("C:\\src\\shot.jpg", 10),
            new DateTime(2026, 3, 7, 10, 0, 0, DateTimeKind.Utc));
        Eq("37 variables expand in a rename template", "20260307_shot.jpg", d2.NewName);
    }

    static void Case38_Validation()
    {
        FaConfig cfg = new FaConfig();
        FaProfile p = Profile("", "C:\\src", Rule("Images", FaAction.Move, "", Conds("extension", ".jpg")));
        cfg.Profiles.Add(p);
        FaValidation v = FaConfigValidator.Validate(cfg);
        Check("38 validation rejects an unnamed profile with no destination",
            !v.Ok && v.Errors.Count >= 2, string.Join("; ", v.Errors.ToArray()));

        FaConfig ok = new FaConfig();
        ok.Profiles.Add(Profile("Good", "C:\\src", Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg"))));
        Check("38 a sound profile validates", FaConfigValidator.Validate(ok).Ok,
            string.Join("; ", FaConfigValidator.Validate(ok).Errors.ToArray()));

        FaRule del = Rule("Gone", FaAction.Delete, "", Conds("extension", ".jpg"));
        FaConfig bad = new FaConfig();
        bad.Profiles.Add(Profile("Bad", "C:\\src", del));
        Check("38 permanent delete needs an explicit acknowledgement",
            !FaConfigValidator.Validate(bad).Ok, "");
        del.DangerousAck = true;
        Check("38 an acknowledged permanent delete validates",
            FaConfigValidator.Validate(bad).Ok,
            string.Join("; ", FaConfigValidator.Validate(bad).Errors.ToArray()));
    }

    static void Case39_SystemPathProtection()
    {
        Check("39 C:\\Windows is protected", FaPaths.IsProtected("C:\\Windows\\System32\\drivers"), "");
        Check("39 C:\\Program Files is protected", FaPaths.IsProtected("C:\\Program Files\\Thing"), "");
        Check("39 a normal user folder is not", !FaPaths.IsProtected("C:\\Users\\me\\Downloads"), "");
        Check("39 system profiles are protected", FaPaths.IsSystemProfile("C:\\Users\\Public"), "");

        FaProfile p = Profile("bad", "C:\\Users\\me\\Downloads",
            Rule("r", FaAction.Move, "C:\\Windows\\Temp", Conds("extension", ".jpg")));
        bool blocked = false;
        foreach (FaTopologyIssue i in FaTopology.Inspect(p)) if (i.Blocking) blocked = true;
        Check("39 a protected destination is refused", blocked, "");
    }

    static void Case40_ImportExport()
    {
        FaConfig live = new FaConfig();
        live.Profiles.Add(Profile("Downloads", "C:\\src",
            Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg"))));
        string json = FaStore.Export(live, null);
        Check("40 a profile exports as readable JSON", json.Contains("\"Downloads\""), json.Substring(0, Math.Min(120, json.Length)));

        FaConfig target = new FaConfig();
        FaConfig incoming = new FaConfig();
        incoming.Profiles.Add(Profile("Imported", "Q:\\elsewhere",
            Rule("Images", FaAction.Move, "Q:\\out", Conds("extension", ".jpg"))));
        FaValidation v = FaStore.Import(target, incoming);
        Check("40 an import lands disabled and reports the unreachable source",
            target.Profiles.Count == 1 && !target.Profiles[0].Enabled && v.Warnings.Count > 0,
            string.Join("; ", v.Warnings.ToArray()));

        FaConfig dup = new FaConfig();
        dup.Profiles.Add(Profile("Downloads", "C:\\src",
            Rule("Images", FaAction.Move, "C:\\dst", Conds("extension", ".jpg"))));
        FaStore.Import(target, dup);
        Check("40 importing a duplicate id gives a new id instead of overwriting",
            target.Profiles.Count == 2 &&
            target.Profiles[0].Id != target.Profiles[1].Id, "");
    }

    static void Case41_EndToEndMoveAndUndo()
    {
        string src = TmpDir("e2e");
        string dst = TmpDir("e2ed");
        string jd = TmpDir("e2ej");
        try
        {
            File.WriteAllText(Path.Combine(src, "a.jpg"), "hello");
            File.WriteAllText(Path.Combine(src, "b.jpg"), "world");
            FaConfig cfg = new FaConfig();
            FaProfile p = Profile("Downloads", src, Rule("Images", FaAction.Move, dst, Conds("extension", ".jpg")));
            cfg.Profiles.Add(p);

            SystemFileSystem fs = new SystemFileSystem();
            FaEngine e = Engine(fs, cfg, jd);
            e.Prepare();
            FaRunReport rep41 = e.RunProfile(p.Id, false, "test");
            string detail = rep41.Summary();
            foreach (FaPlanItem it in rep41.Items) detail += " || " + it.Line();
            Check("41 both matching files moved", Directory.GetFiles(dst).Length == 2,
                "got " + Directory.GetFiles(dst).Length + ": " + detail);

            FaUndoResult u = e.Journal.Undo(fs, null);
            Check("41 the last move can be undone", u.Ok, u.Message);
            Eq("41 the file is back in the source", 1, Directory.GetFiles(src).Length);
            e.Stop();
        }
        finally { TryDelete(src); TryDelete(dst); TryDelete(jd); }
    }

    // A watching profile must actually act on what its watcher queued. The
    // engine's tick used to return as soon as a watcher was attached, so every
    // realtime profile stayed "Watching" forever and moved nothing -- and the
    // suite missed it because every other case calls RunProfile directly.
    static void Case42_WatchingProfileDrainsCandidates()
    {
        string src = TmpDir("drain-src");
        string dst = TmpDir("drain-dst");
        string jd = TmpDir("drain-j");
        try
        {
            FaConfig cfg = new FaConfig();
            FaProfile p = Profile("t", src, Rule("Images", FaAction.Move, dst, Conds("extension", ".jpg")));
            p.Schedule.Trigger = FaTrigger.Realtime;
            p.ReconcileMinutes = 0;
            p.QuietSeconds = 0;
            cfg.Profiles.Add(p);

            SystemFileSystem fs = new SystemFileSystem();
            FaEngine e = Engine(fs, cfg, jd);
            e.Prepare();
            FaProfileRuntime rt = e.Runtime(p.Id);
            Eq("42 the profile is watching its source", "Watching", rt.Status);

            string file = Path.Combine(src, "a.jpg");
            File.WriteAllText(file, "jpeg");
            // What FaWatcher's Created handler does. The candidate is queued
            // from the watcher's thread; the tick drains it on the engine's.
            e.QueueCandidate(rt, file);
            Eq("42 the watcher queued the candidate", 1, rt.Queue.Count);

            // PERF-003: the queue now re-probes a fresh candidate once instead of
            // on every tick, so this case lets the re-probe delay elapse.
            e.Tick();
            System.Threading.Thread.Sleep(350);
            e.Tick();

            Check("42 the queued candidate was moved out of the source",
                !File.Exists(file) && File.Exists(Path.Combine(dst, "a.jpg")),
                "source=" + File.Exists(file) + " dest=" + File.Exists(Path.Combine(dst, "a.jpg")));
            Eq("42 the candidate queue is drained", 0, rt.Queue.Count);
            e.Stop();
        }
        finally { TryDelete(src); TryDelete(dst); TryDelete(jd); }
    }

    // PERF-003: a locked candidate backs off instead of being re-probed every
    // tick, and an exhausted retry budget ends in a deferred state rather than
    // an eternal poll. Before the per-candidate record this probe ran against
    // every candidate in the queue on every tick, and the attempt count lived
    // in a dictionary beside the set.
    static void Case43_LockedCandidateBackoff()
    {
        string src = TmpDir("bc-src"), dst = TmpDir("bc-dst");
        try
        {
            string file = Path.Combine(src, "a.jpg");
            File.WriteAllText(file, "jpeg");
            // Held exclusively: the engine's writability probe fails, which is
            // what a file an editor still has open looks like.
            FileStream hold = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.None);
            try
            {
                FaConfig cfg = new FaConfig();
                FaProfile p = Profile("t", src, Rule("Images", FaAction.Move, dst, Conds("extension", ".jpg")));
                p.Schedule.Trigger = FaTrigger.Realtime;
                p.ReconcileMinutes = 0;
                p.QuietSeconds = 0;
                p.Retry.Attempts = 3;
                p.Retry.BackoffMs = 400;
                cfg.Profiles.Add(p);
                string jd = TmpDir("bc-j");
                FaEngine e = Engine(new SystemFileSystem(), cfg, jd);
                try
                {
                    e.Prepare();
                    FaProfileRuntime rt = e.Runtime(p.Id);
                    e.QueueCandidate(rt, file);
                    FaCandidate c = rt.Queue.Find(file);
                    Check("43 a queued candidate carries a record", c != null && c.Path == file && c.Attempts == 0,
                        "candidate=" + (c == null ? "null" : c.Path));

                    e.Tick();                       // first sighting: not quiet yet
                    c = rt.Queue.Find(file);
                    Eq("43 a freshly seen candidate is not eligible for a retry", 0, c.Attempts);

                    System.Threading.Thread.Sleep(350);   // past the re-probe delay
                    e.Tick();                       // held open: locked
                    c = rt.Queue.Find(file);
                    Check("43 a locked candidate is still queued", c != null, "candidate dropped");
                    Eq("43 the first locked attempt is counted", 1, c.Attempts);

                    e.Tick();                       // inside the backoff: no re-probe
                    Eq("43 a locked candidate waits out its backoff", 1, rt.Queue.Find(file).Attempts);

                    System.Threading.Thread.Sleep(500);
                    e.Tick();                       // backoff elapsed: one more attempt
                    Eq("43 the second locked attempt is counted", 2, rt.Queue.Find(file).Attempts);

                    System.Threading.Thread.Sleep(500);
                    e.Tick();                       // third: the limit is reached
                    Check("43 an exhausted candidate is deferred, not polled forever",
                        rt.Queue.Count == 0 && rt.Deferred.Count == 1 && rt.Deferred[0].LastFailure.Contains("locked"),
                        "queue=" + rt.Queue.Count);
                    Check("43 the held file was never touched", File.Exists(file), "file=" + File.Exists(file));
                }
                finally { e.Stop(); TryDelete(jd); }
            }
            finally { hold.Dispose(); }
        }
        finally { TryDelete(src); TryDelete(dst); }
    }

    // PERF-004: scanning and admission are one backpressured pipeline. The walk
    // stops when its admission budget runs out and keeps its place, so a source
    // bigger than the budget converges over several steps instead of being
    // materialized as a whole list and then silently refused.
    static void Case44_StreamingReconcileAdmission()
    {
        string src = TmpDir("stream-src"), dst = TmpDir("stream-dst");
        try
        {
            for (int i = 0; i < 10; i++) File.WriteAllText(Path.Combine(src, "f" + i + ".jpg"), "x");
            FaConfig cfg = new FaConfig();
            FaProfile p = Profile("t", src, Rule("Images", FaAction.Move, dst, Conds("extension", ".jpg")));
            p.Schedule.Trigger = FaTrigger.Manual;
            cfg.Profiles.Add(p);
            string jd = TmpDir("stream-j");
            FaEngine e = Engine(new SystemFileSystem(), cfg, jd);
            try
            {
                e.Prepare();
                FaProfileRuntime rt = e.Runtime(p.Id);
                e.ReconcileAdmissionBudget = 3;

                FaReconcileReport rep = e.ReconcileStep(rt, "test");
                Eq("44 one step admits no more than its budget", 3, rep.Admitted);
                Check("44 traversal suspends instead of dropping the rest", rep.Suspended, rep.Line());
                Eq("44 the remaining files were not dropped, only deferred", 1, rep.Deferred);

                int steps = 1;
                while (rep.Suspended && steps < 20) { rep = e.ReconcileStep(rt, "continue"); steps++; }
                Eq("44 the continuation reaches every file", 10, rt.Queue.Count);
                Check("44 the walk finishes instead of suspending forever", !rep.Suspended && steps < 20,
                    "steps=" + steps + " " + rep.Line());
                Check("44 the counts separate scanned, admitted and deferred",
                    rep.Scanned >= 1 && rep.Admitted >= 1, rep.Line());
            }
            finally { e.Stop(); TryDelete(jd); }
        }
        finally { TryDelete(src); TryDelete(dst); }
    }

    // --------------------------------------------------------------- main

    public static int Main()    {
        Console.WriteLine("Folder Automation tests");
        Case01_ExtensionRule();
        Case02_FilenameRule();
        Case03_GlobRule();
        Case04_RegexRule();
        Case05_AllConditions();
        Case06_AnyConditions();
        Case07_Exclusions();
        Case08_FirstMatchOrder();
        Case09_TempFiles();
        Case10_StabilityGate();
        Case11_SameVolumeMove();
        Case12_CrossVolumeMove();
        Case13_CollisionSkip();
        Case14_CollisionCounter();
        Case15_NoOverwriteByDefault();
        Case16_RecursiveSource();
        Case17_DestinationInsideSource();
        Case18_ProfileCycle();
        Case19_ReparsePoints();
        Case20_DryRunMutatesNothing();
        Case21_DisabledProfileMutatesNothing();
        Case22_MaxOperations();
        Case23_MissingSource();
        Case24_SourceComesBack();
        Case25_DuplicateEvents();
        Case26_OverflowToReconcile();
        Case27_ScheduleMath();
        Case28_MissedRuns();
        Case29_ConcurrentTriggers();
        Case30_MalformedConfig();
        Case31_AtomicSave();
        Case32_JournalPersistence();
        Case33_MoveUndo();
        Case34_RenameUndo();
        Case35_FailedOperation();
        Case36_RestartDoesNotDuplicate();
        Case37_VariableExpansion();
        Case38_Validation();
        Case39_SystemPathProtection();
        Case40_ImportExport();
        Case41_EndToEndMoveAndUndo();
        Case42_WatchingProfileDrainsCandidates();
        Case43_LockedCandidateBackoff();
        Case44_StreamingReconcileAdmission();

        Console.WriteLine();
        Console.WriteLine(Passed + " passed, " + Failures.Count + " failed");
        foreach (string f in Failures) Console.WriteLine("  FAILED: " + f);
        return Failures.Count;
    }
}