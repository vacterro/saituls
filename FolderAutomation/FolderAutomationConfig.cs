using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

// Folder Automation — configuration model, validation and durable storage.
//
// Deliberately WinForms-free: the whole engine folder compiles into a plain
// console/library harness for tests, and into SAITULS.exe for the GUI.

namespace Saituls.FolderAutomation
{
    public enum FaMatch
    {
        All = 0,
        Any = 1,
        Exclude = 2   // matches => the item is skipped entirely (NOT)
    }

    public enum FaAction
    {
        Move = 0,
        Copy = 1,
        Rename = 2,
        Ignore = 3,
        Stop = 4,        // stop processing this item, journal nothing
        Quarantine = 5,  // move into the profile quarantine directory
        Recycle = 6,     // move to the Recycle Bin
        Delete = 7       // permanent; must be explicitly acknowledged
    }

    public enum FaCollision
    {
        Skip = 0,
        Counter = 1,      // photo.jpg -> photo (1).jpg
        Timestamp = 2,
        Replace = 3,
        KeepNewest = 4,
        KeepOldest = 5,
        KeepLarger = 6,
        KeepSmaller = 7
    }

    public enum FaVerify
    {
        None = 0,
        Size = 1,
        Hash = 2
    }

    public enum FaTrigger
    {
        Manual = 0,
        Realtime = 1,
        EveryMinutes = 2,
        Hourly = 3,
        Daily = 4,
        Weekly = 5,
        Startup = 6,
        Resume = 7,
        PathAvailable = 8,
        Once = 9
    }

    public enum FaMissed
    {
        RunAsap = 0,
        Skip = 1,
        RunOnce = 2
    }

    public sealed class FaCondition
    {
        public string Kind = "";      // extension | filename | glob | regex | path | ...
        public string Op = "is_one_of";
        public string Value = "";     // scalar forms
        public string[] Values = new string[0];

        public FaCondition Clone()
        {
            FaCondition c = new FaCondition();
            c.Kind = Kind; c.Op = Op; c.Value = Value;
            c.Values = new string[Values.Length];
            Array.Copy(Values, c.Values, Values.Length);
            return c;
        }
    }

    public sealed class FaRule
    {
        public string Id = "";
        public string Name = "";
        public bool Enabled = true;
        public FaMatch Match = FaMatch.All;
        public FaAction Action = FaAction.Move;
        public string Destination = "";      // supports {year} {month} {day} {ext} {name} {profile} {rule}
        public string RenameTemplate = "";   // used by Rename
        public bool CreateDir = true;
        public bool DangerousAck = false;    // required for Action == Delete
        public List<FaCondition> Conditions = new List<FaCondition>();

        public FaRule Clone()
        {
            FaRule r = new FaRule();
            r.Id = Id; r.Name = Name; r.Enabled = Enabled; r.Match = Match; r.Action = Action;
            r.Destination = Destination; r.RenameTemplate = RenameTemplate;
            r.CreateDir = CreateDir; r.DangerousAck = DangerousAck;
            r.Conditions = new List<FaCondition>();
            foreach (FaCondition c in Conditions) r.Conditions.Add(c.Clone());
            return r;
        }
    }

    public sealed class FaSchedule
    {
        public FaTrigger Trigger = FaTrigger.Manual;
        public int EveryMinutes = 15;
        public string At = "03:30";           // HH:MM for Daily/Weekly
        public int Weekday = 1;               // 0=Sunday .. 6=Saturday
        public string RunAt = "";             // Once: ISO timestamp
        public FaMissed Missed = FaMissed.RunOnce;
        public string LastRun = "";           // ISO timestamp of the last completed run
    }

    public sealed class FaLimits
    {
        public int MaxOps = 500;
        public long MaxBytes = 4294967296L;   // 4 GiB
        public int MaxErrors = 50;
        public int WarnOps = 200;
    }

    public sealed class FaRetry
    {
        public int Attempts = 3;
        public int BackoffMs = 500;
    }

    public sealed class FaProfile
    {
        public string Id = "";
        public string Name = "";
        public bool Enabled = false;          // new/complex profiles start disabled
        public List<string> Sources = new List<string>();
        public bool Recursive = true;
        public string DestinationRoot = "";   // default for rules with a relative destination
        // Mirror of Schedule.Trigger for the UI. Runtime-only: the schedule is
        // the serialized authority, so a stray "trigger" key is never honoured.
        [System.Web.Script.Serialization.ScriptIgnore]
        public FaTrigger Trigger = FaTrigger.Manual;
        public FaSchedule Schedule = new FaSchedule();
        public int ReconcileMinutes = 30;
        public int QuietSeconds = 10;
        public bool WatchLastWrite = false;
        public FaCollision Collision = FaCollision.Counter;
        public FaVerify Verify = FaVerify.Size;
        public FaLimits Limits = new FaLimits();
        public FaRetry Retry = new FaRetry();
        public string Quarantine = "";
        public bool OverrideTempPatterns = false;
        public List<string> TempPatterns = new List<string>();
        public bool FollowReparse = false;
        public List<string> Exclusions = new List<string>();
        public List<FaRule> Rules = new List<FaRule>();
        // Runtime-only. Never serialized, never part of a saved profile.
        [System.Web.Script.Serialization.ScriptIgnore]
        public string Status = "Disabled";
        [System.Web.Script.Serialization.ScriptIgnore]
        public string StatusDetail = "";

        public FaProfile Clone()
        {
            FaProfile p = new FaProfile();
            p.Id = Id; p.Name = Name; p.Enabled = Enabled; p.Recursive = Recursive;
            p.DestinationRoot = DestinationRoot; p.Trigger = Trigger;
            p.ReconcileMinutes = ReconcileMinutes; p.QuietSeconds = QuietSeconds;
            p.WatchLastWrite = WatchLastWrite; p.Collision = Collision; p.Verify = Verify;
            p.Trigger = Trigger; p.OverrideTempPatterns = OverrideTempPatterns;
            p.FollowReparse = FollowReparse; p.Quarantine = Quarantine;
            p.Schedule = new FaSchedule();
            p.Schedule.Trigger = Schedule.Trigger;
            p.Schedule.EveryMinutes = Schedule.EveryMinutes;
            p.Schedule.At = Schedule.At; p.Schedule.Weekday = Schedule.Weekday;
            p.Schedule.RunAt = Schedule.RunAt; p.Schedule.Missed = Schedule.Missed;
            p.Schedule.LastRun = Schedule.LastRun;
            p.Limits = new FaLimits();
            p.Limits.MaxOps = Limits.MaxOps; p.Limits.MaxBytes = Limits.MaxBytes;
            p.Limits.MaxErrors = Limits.MaxErrors; p.Limits.WarnOps = Limits.WarnOps;
            p.Retry = new FaRetry();
            p.Retry.Attempts = Retry.Attempts; p.Retry.BackoffMs = Retry.BackoffMs;
            p.Sources = new List<string>(Sources);
            p.TempPatterns = new List<string>(TempPatterns);
            p.Exclusions = new List<string>(Exclusions);
            p.Rules = new List<FaRule>();
            foreach (FaRule r in Rules) p.Rules.Add(r.Clone());
            return p;
        }

        public IEnumerable<string> TempPatternList()
        {
            if (OverrideTempPatterns) return TempPatterns;
            return DefaultTempPatterns.Patterns;
        }
    }

    public static class DefaultTempPatterns
    {
        public static readonly string[] Patterns = new string[]
        {
            "*.tmp", "*.temp", "*.part", "*.partial", "*.crdownload", "*.download"
        };

        public static bool Matches(string fileName, IEnumerable<string> patterns)
        {
            if (string.IsNullOrEmpty(fileName) || patterns == null) return false;
            foreach (string p in patterns)
            {
                if (string.IsNullOrEmpty(p)) continue;
                try
                {
                    if (FaGlob.IsMatch(fileName, p)) return true;
                }
                catch { }
            }
            return false;
        }
    }

    public sealed class FaConfig
    {
        public const int CurrentSchema = 1;

        public int SchemaVersion = CurrentSchema;
        public bool EngineEnabled = true;
        public List<FaProfile> Profiles = new List<FaProfile>();

        public FaProfile Find(string id)
        {
            foreach (FaProfile p in Profiles)
                if (string.Equals(p.Id, id, StringComparison.OrdinalIgnoreCase)) return p;
            return null;
        }
    }

    public sealed class FaValidation
    {
        public bool Ok = true;               // a clean validation is the normal case
        public readonly List<string> Errors = new List<string>();
        public readonly List<string> Warnings = new List<string>();

        public static FaValidation Of(bool ok) { FaValidation v = new FaValidation(); v.Ok = ok; return v; }
        public void Error(string m) { Errors.Add(m); Ok = false; }
        public void Warn(string m) { Warnings.Add(m); }
    }

    // Validation is deliberately pure: it never touches the disk, so the same
    // rules run for a file on disk, for an imported blob and for a UI edit.
    public static class FaConfigValidator
    {
        public static FaValidation Validate(FaConfig cfg)
        {
            FaValidation v = new FaValidation();
            if (cfg == null) { v.Error("configuration is missing"); return v; }
            if (cfg.SchemaVersion > FaConfig.CurrentSchema)
            {
                v.Error("configuration schema " + cfg.SchemaVersion +
                        " is newer than this build supports (" + FaConfig.CurrentSchema + ")");
                return v;
            }
            if (cfg.Profiles == null) { v.Error("profiles is null"); return v; }

            HashSet<string> ids = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (FaProfile p in cfg.Profiles)
            {
                if (p == null) { v.Error("null profile entry"); continue; }
                string tag = string.IsNullOrEmpty(p.Name) ? "(unnamed)" : p.Name;
                if (string.IsNullOrEmpty(p.Id)) v.Error(tag + ": id is empty");
                else if (!ids.Add(p.Id)) v.Error(tag + ": duplicate id " + p.Id);
                if (string.IsNullOrEmpty(p.Name)) v.Error(tag + ": name is empty");
                if (p.Sources == null || p.Sources.Count == 0) v.Error(tag + ": no source paths");
                if (p.ReconcileMinutes < 0) v.Error(tag + ": reconcile interval is negative");
                if (p.QuietSeconds < 0) v.Error(tag + ": quiet period is negative");
                if (p.Limits == null) { v.Error(tag + ": limits missing"); }
                else
                {
                    if (p.Limits.MaxOps <= 0) v.Error(tag + ": max operations must be > 0");
                    if (p.Limits.MaxErrors < 0) v.Error(tag + ": max errors is negative");
                }
                if (p.Retry == null) p.Retry = new FaRetry();
                else
                {
                    if (p.Retry.Attempts < 1) v.Error(tag + ": retry attempts must be >= 1");
                    if (p.Retry.BackoffMs < 0) v.Error(tag + ": retry backoff is negative");
                }
                if (p.Rules == null) { v.Error(tag + ": rules missing"); continue; }

                HashSet<string> ruleIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (FaRule r in p.Rules)
                {
                    if (r == null) { v.Error(tag + ": null rule"); continue; }
                    string rt = string.IsNullOrEmpty(r.Name) ? "(unnamed rule)" : r.Name;
                    if (string.IsNullOrEmpty(r.Id)) v.Error(tag + ": a rule has no id");
                    else if (!ruleIds.Add(r.Id)) v.Error(tag + ": duplicate rule id " + r.Id);
                    if (r.Conditions == null || r.Conditions.Count == 0)
                        v.Error(tag + " / " + rt + ": no conditions");
                    foreach (FaCondition c in (r.Conditions ?? new List<FaCondition>()))
                    {
                        if (c == null) { v.Error(tag + " / " + rt + ": null condition"); continue; }
                        if (!FaConditionKinds.IsKnown(c.Kind))
                            v.Error(tag + " / " + rt + ": unknown condition '" + c.Kind + "'");
                        if (c.Kind == "regex" && c.Op == "is")
                        {
                            try { new System.Text.RegularExpressions.Regex(c.Value); }
                            catch (Exception ex)
                            { v.Error(tag + " / " + rt + ": invalid regex: " + ex.Message); }
                        }
                    }
                    if (r.Action == FaAction.Delete && !r.DangerousAck)
                        v.Error(tag + " / " + rt + ": permanent delete needs explicit acknowledgement");
                    if ((r.Action == FaAction.Move || r.Action == FaAction.Copy ||
                         r.Action == FaAction.Quarantine) &&
                        r.Action != FaAction.Quarantine &&
                        string.IsNullOrEmpty(r.Destination))
                        v.Error(tag + " / " + rt + ": action needs a destination");
                    if (r.Action == FaAction.Rename && string.IsNullOrEmpty(r.RenameTemplate))
                        v.Error(tag + " / " + rt + ": rename needs a template");
                }
            }
            foreach (string c in FaTopology.CheckCycles(cfg))
                v.Error(c);
            return v;
        }
    }

    public static class FaConditionKinds
    {
        public static readonly string[] All = new string[]
        {
            "extension", "filename", "glob", "regex", "path",
            "min_size", "max_size", "min_age", "max_age",
            "created_after", "created_before", "modified_after", "modified_before",
            "is_directory", "hidden",
            "filename_contains", "filename_startswith", "filename_endswith",
            "extension_group", "weekday", "time_window"
        };

        public static bool IsKnown(string kind)
        {
            return Array.IndexOf(All, kind ?? "") >= 0;
        }
    }

    // Atomic write: temp file -> flush to disk -> replace. A crash mid-save
    // leaves the previous config intact rather than a truncated one.
    public static class FaStore
    {
        public static void WriteAtomic(string path, string text)
        {
            FaPaths.EnsureDir(Path.GetDirectoryName(path));
            string tmp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                using (FileStream fs = new FileStream(tmp, FileMode.Create, FileAccess.Write, FileShare.None))
                using (StreamWriter sw = new StreamWriter(fs, new UTF8Encoding(false)))
                {
                    sw.Write(text);
                    sw.Flush();
                    fs.Flush(true);
                }
                if (File.Exists(path))
                {
                    bool replaced = false;
                    try
                    {
                        File.Replace(tmp, path, null);
                        replaced = true;
                    }
                    catch { }

                    if (!replaced)
                    {
                        string bak = path + "." + Guid.NewGuid().ToString("N") + ".bak";
                        File.Move(path, bak);
                        try
                        {
                            File.Move(tmp, path);
                            try { File.Delete(bak); } catch { }
                        }
                        catch
                        {
                            try { File.Move(bak, path); } catch { }
                            throw;
                        }
                    }
                }
                else
                {
                    File.Move(tmp, path);
                }
            }
            finally
            {
                if (File.Exists(tmp)) try { File.Delete(tmp); } catch { }
            }
        }

        public static void Save(FaConfig cfg)
        {
            JavaScriptSerializer js = new JavaScriptSerializer();
            js.MaxJsonLength = 8 * 1024 * 1024;
            WriteAtomic(FaPaths.ConfigPath(), js.Serialize(cfg));
        }

        public sealed class LoadResult
        {
            public FaConfig Config;
            public string Error;
            public bool PreservedBadCopy;
        }

        public static LoadResult Load()
        {
            LoadResult r = new LoadResult();
            string path = FaPaths.ConfigPath();
            if (!File.Exists(path)) { r.Config = new FaConfig(); return r; }
            string text;
            try { text = File.ReadAllText(path); }
            catch (Exception ex) { r.Error = "cannot read config: " + ex.Message; return r; }
            try
            {
                JavaScriptSerializer js = new JavaScriptSerializer();
                js.MaxJsonLength = 8 * 1024 * 1024;
                r.Config = js.Deserialize<FaConfig>(text) ?? new FaConfig();
                if (r.Config.Profiles == null) r.Config.Profiles = new List<FaProfile>();
                Normalize(r.Config);
            }
            catch (Exception ex)
            {
                r.Error = "malformed configuration: " + ex.Message;
                // Keep the bad file for diagnosis; never silently reset the user.
                try
                {
                    FaPaths.EnsureDir(FaPaths.DataDir());
                    File.Copy(path, FaPaths.BadConfigPath(), true);
                    r.PreservedBadCopy = true;
                }
                catch { }
                r.Config = new FaConfig();
            }
            return r;
        }

        // A profile that arrives from disk with null collections must not take
        // the engine down; fill the shapes the engine assumes.
        public static void Normalize(FaConfig cfg)
        {
            foreach (FaProfile p in cfg.Profiles)
            {
                if (p.Sources == null) p.Sources = new List<string>();
                if (p.Exclusions == null) p.Exclusions = new List<string>();
                if (p.TempPatterns == null) p.TempPatterns = new List<string>();
                if (p.Rules == null) p.Rules = new List<FaRule>();
                if (p.Limits == null) p.Limits = new FaLimits();
                if (p.Retry == null) p.Retry = new FaRetry();
                if (p.Schedule == null) p.Schedule = new FaSchedule();
                // Schedule.Trigger is the only authority. The profile-level
                // Trigger is a read-only mirror for the UI, so a hand-written
                // "trigger" key in a config can never silently disagree with
                // the schedule the engine actually runs.
                p.Trigger = p.Schedule.Trigger;
                foreach (FaRule r in p.Rules)
                {
                    if (r.Conditions == null) r.Conditions = new List<FaCondition>();
                    foreach (FaCondition c in r.Conditions)
                    {
                        if (c.Values == null) c.Values = new string[0];
                    }
                }
            }
        }

        public static string Export(FaConfig cfg, List<string> profileIds)
        {
            FaConfig outc = new FaConfig();
            outc.SchemaVersion = FaConfig.CurrentSchema;
            outc.EngineEnabled = cfg.EngineEnabled;
            foreach (FaProfile p in cfg.Profiles)
                if (profileIds == null || profileIds.Contains(p.Id)) outc.Profiles.Add(p.Clone());
            JavaScriptSerializer js = new JavaScriptSerializer();
            js.MaxJsonLength = 8 * 1024 * 1024;
            return js.Serialize(outc);
        }

        // Imported profiles are merged, never adopted blindly: a profile whose
        // id already exists is kept disabled with a new id so the user decides.
        public static FaValidation Import(FaConfig target, FaConfig source)
        {
            FaValidation v = new FaValidation();
            if (source == null) { v.Error("nothing to import"); return v; }
            FaStore.Normalize(source);
            HashSet<string> ids = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (FaProfile p in target.Profiles) ids.Add(p.Id);

            foreach (FaProfile p in source.Profiles)
            {
                FaProfile c = p.Clone();
                if (string.IsNullOrEmpty(c.Id) || ids.Contains(c.Id)) c.Id = NewId();
                ids.Add(c.Id);
                // Every imported profile lands disabled. Enabling an imported
                // destructive rule set is a user decision, never a side effect
                // of opening a file.
                bool wasEnabled = c.Enabled;
                c.Enabled = false;
                if (wasEnabled) v.Warn((c.Name) + " was imported disabled; enable it yourself after checking the rules.");
                foreach (string s in (c.Sources ?? new List<string>()))
                {
                    if (string.IsNullOrEmpty(s)) v.Warn(c.Name + ": empty source path dropped");
                    else if (!Directory.Exists(FaPaths.Normalize(s)))
                        v.Warn(c.Name + ": source not reachable right now: " + s);
                }
                FaStore.Normalize(new FaConfig { Profiles = new List<FaProfile> { c } });
                target.Profiles.Add(c);
            }
            return v;
        }

        public static string NewId() { return Guid.NewGuid().ToString("N"); }
    }
}