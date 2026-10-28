using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

// SAIOPT pure logic.
//
// Nothing in this file touches Win32, the clock, the disk or a process: every
// decision is a function of values the adapter (SAIOPT.cs) hands in. That is
// what lets tests\saiopt_logic_tests.cs prove the rules that matter -- what
// gets eased, what is never touched, what is restored and when, what counts
// as stale -- without a desktop and without changing a real process.
//
// The one principle behind every rule: what you are using or hearing keeps
// full speed; what you are not, yields -- but only while the machine is
// actually short of CPU, only by one priority step, and only for as long as
// that stays true. Everything SAIOPT changes it can and does change back.

namespace Saituls.Saiopt
{
    // ---------------------------------------------------------------- config

    public sealed class OptConfig
    {
        public const int DefaultSampleMs = 2000;
        public const int DefaultBusyPct = 60;
        public const int DefaultCalmPct = 35;
        public const int DefaultHogCorePct = 50;
        public const int DefaultHogSamples = 3;
        public const int DefaultCalmSamples = 10;
        public const int DefaultMaxEased = 16;
        public const int DefaultFocusGraceMs = 15000;
        public const int DefaultStaleSuspendedMin = 60;
        public const int DefaultStaleIdleHours = 12;
        public const int DefaultRemindMin = 60;
        public const int DefaultShellSlowMs = 250;
        public const int DefaultKeepAwakeSec = 45;

        public bool Ease = true;
        public int SampleMs = DefaultSampleMs;
        public int BusyPct = DefaultBusyPct;
        public int CalmPct = DefaultCalmPct;
        public int HogCorePct = DefaultHogCorePct;
        public int HogSamples = DefaultHogSamples;
        public int CalmSamples = DefaultCalmSamples;
        public int MaxEased = DefaultMaxEased;
        public int FocusGraceMs = DefaultFocusGraceMs;
        public string EaseTo = EaseTargets.BelowNormal;
        public bool ProtectAudio = true;
        public HashSet<string> NeverEase = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        public bool WatchStorage = true;
        public string KeepAwake = KeepAwakeModes.Auto;
        public int KeepAwakeSec = DefaultKeepAwakeSec;
        public bool WatchShell = true;
        public int ShellSlowMs = DefaultShellSlowMs;
        public int StaleSuspendedMin = DefaultStaleSuspendedMin;
        public int StaleIdleHours = DefaultStaleIdleHours;
        public HashSet<string> SweepAuto = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        public bool Tray = true;
        public bool Balloons = true;
        public int RemindMin = DefaultRemindMin;
        public bool Debug = false;

        public static OptConfig Defaults() { return new OptConfig(); }

        public int EasePriority { get { return EaseTo == EaseTargets.Idle ? Priorities.Idle : Priorities.BelowNormal; } }

        static int Clamp(IDictionary<string, string> v, string key, int def, int min, int max, IList<string> warnings)
        {
            string raw;
            if (v == null || !v.TryGetValue(key, out raw) || raw == null) return def;
            raw = raw.Trim();
            if (raw.Length == 0) return def;
            int parsed;
            if (!int.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed))
            {
                if (warnings != null) warnings.Add(key + "=" + raw + " is not an integer, using " + def);
                return def;
            }
            if (parsed < min || parsed > max)
            {
                if (warnings != null) warnings.Add(key + "=" + parsed + " outside " + min + ".." + max + ", using " + def);
                return def;
            }
            return parsed;
        }

        static bool Flag(IDictionary<string, string> v, string key, bool def)
        {
            string raw;
            if (v == null || !v.TryGetValue(key, out raw) || raw == null) return def;
            raw = raw.Trim();
            if (raw == "1" || string.Equals(raw, "true", StringComparison.OrdinalIgnoreCase)) return true;
            if (raw == "0" || string.Equals(raw, "false", StringComparison.OrdinalIgnoreCase)) return false;
            return def;
        }

        public static HashSet<string> ParseImageList(string raw)
        {
            HashSet<string> set = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (string.IsNullOrEmpty(raw)) return set;
            foreach (string part in raw.Split(new char[] { ',', ';', ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries))
            {
                string name = part.Trim().Trim('"');
                if (name.Length == 0) continue;
                if (!name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) name += ".exe";
                set.Add(name.ToLowerInvariant());
            }
            return set;
        }

        public static string FormatImageList(IEnumerable<string> images)
        {
            List<string> list = new List<string>(images);
            list.Sort(StringComparer.OrdinalIgnoreCase);
            return string.Join(",", list.ToArray());
        }

        public static OptConfig Parse(IDictionary<string, string> v, IList<string> warnings)
        {
            OptConfig c = new OptConfig();
            c.Ease = Flag(v, "ease", true);
            c.SampleMs = Clamp(v, "sample_ms", DefaultSampleMs, 500, 10000, warnings);
            c.BusyPct = Clamp(v, "busy_pct", DefaultBusyPct, 20, 100, warnings);
            c.CalmPct = Clamp(v, "calm_pct", DefaultCalmPct, 5, 95, warnings);
            if (c.CalmPct >= c.BusyPct)
            {
                if (warnings != null) warnings.Add("calm_pct=" + c.CalmPct + " must be below busy_pct=" + c.BusyPct
                    + ", using " + DefaultCalmPct + "/" + DefaultBusyPct);
                c.CalmPct = DefaultCalmPct;
                c.BusyPct = DefaultBusyPct;
            }
            c.HogCorePct = Clamp(v, "hog_core_pct", DefaultHogCorePct, 10, 6400, warnings);
            c.HogSamples = Clamp(v, "hog_samples", DefaultHogSamples, 1, 60, warnings);
            c.CalmSamples = Clamp(v, "calm_samples", DefaultCalmSamples, 1, 300, warnings);
            c.MaxEased = Clamp(v, "max_eased", DefaultMaxEased, 1, 64, warnings);
            c.FocusGraceMs = Clamp(v, "focus_grace_ms", DefaultFocusGraceMs, 0, 600000, warnings);
            string to;
            if (v != null && v.TryGetValue("ease_to", out to) && to != null && to.Trim().Length > 0)
            {
                to = to.Trim().ToLowerInvariant();
                if (EaseTargets.IsKnown(to)) c.EaseTo = to;
                else if (warnings != null) warnings.Add("ease_to=" + to + " unknown, using " + EaseTargets.BelowNormal);
            }
            c.ProtectAudio = Flag(v, "protect_audio", true);
            string list;
            if (v != null && v.TryGetValue("never_ease", out list)) c.NeverEase = ParseImageList(list);
            c.WatchStorage = Flag(v, "watch_storage", true);
            string ka;
            if (v != null && v.TryGetValue("keep_awake", out ka) && ka != null && ka.Trim().Length > 0)
            {
                ka = ka.Trim().ToLowerInvariant();
                if (ka == KeepAwakeModes.Auto || ka == KeepAwakeModes.Off) c.KeepAwake = ka;
                else if (warnings != null) warnings.Add("keep_awake=" + ka + " unknown, using " + KeepAwakeModes.Auto);
            }
            c.KeepAwakeSec = Clamp(v, "keep_awake_sec", DefaultKeepAwakeSec, 15, 240, warnings);
            c.WatchShell = Flag(v, "watch_shell", true);
            c.ShellSlowMs = Clamp(v, "shell_slow_ms", DefaultShellSlowMs, 20, 5000, warnings);
            c.StaleSuspendedMin = Clamp(v, "stale_suspended_min", DefaultStaleSuspendedMin, 5, 10080, warnings);
            c.StaleIdleHours = Clamp(v, "stale_idle_hours", DefaultStaleIdleHours, 1, 720, warnings);
            if (v != null && v.TryGetValue("sweep_auto", out list)) c.SweepAuto = ParseImageList(list);
            c.Tray = Flag(v, "tray", true);
            c.Balloons = Flag(v, "balloons", true);
            c.RemindMin = Clamp(v, "remind_min", DefaultRemindMin, 5, 1440, warnings);
            c.Debug = Flag(v, "debug", false);
            return c;
        }

        // Same tolerant reader as TaskbarEdge: "key=value", ';'/'#' comments
        // (inline only after whitespace), [section] headers ignored.
        public static IDictionary<string, string> ParseIni(string text)
        {
            Dictionary<string, string> map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (text == null) return map;
            string[] lines = text.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i].Trim();
                if (line.Length == 0 || line[0] == ';' || line[0] == '#' || line[0] == '[') continue;
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                string key = line.Substring(0, eq).Trim();
                string val = StripInlineComment(line.Substring(eq + 1)).Trim();
                if (key.Length > 0) map[key] = val;
            }
            return map;
        }

        static string StripInlineComment(string value)
        {
            for (int i = 1; i < value.Length; i++)
                if ((value[i] == ';' || value[i] == '#') && (value[i - 1] == ' ' || value[i - 1] == '\t'))
                    return value.Substring(0, i);
            return value;
        }

        // Rewrites one key in an existing ini text, keeping every other line,
        // comment and annotation byte-for-byte. Appends the key when absent.
        public static string SetIniValue(string text, string key, string value)
        {
            if (text == null) text = "";
            string[] lines = text.Replace("\r\n", "\n").Split('\n');
            bool done = false;
            for (int i = 0; i < lines.Length; i++)
            {
                string t = lines[i].TrimStart();
                if (t.Length == 0 || t[0] == ';' || t[0] == '#' || t[0] == '[') continue;
                int eq = t.IndexOf('=');
                if (eq <= 0 || !string.Equals(t.Substring(0, eq).Trim(), key, StringComparison.OrdinalIgnoreCase)) continue;
                string rest = t.Substring(eq + 1);
                string comment = "";
                for (int j = 1; j < rest.Length; j++)
                    if ((rest[j] == ';' || rest[j] == '#') && (rest[j - 1] == ' ' || rest[j - 1] == '\t'))
                    {
                        comment = "   " + rest.Substring(j);
                        break;
                    }
                lines[i] = key + "=" + value + comment;
                done = true;
            }
            string joined = string.Join("\r\n", lines);
            if (!done)
            {
                if (joined.Length > 0 && !joined.EndsWith("\r\n")) joined += "\r\n";
                joined += key + "=" + value + "\r\n";
            }
            return joined;
        }
    }

    public static class KeepAwakeModes
    {
        public const string Auto = "auto";
        public const string Off = "off";
    }

    // Some USB bridges (JMicron JMS583 here) put an idle drive to sleep on a
    // ~5 min firmware timer and then fail to wake it inside the storport
    // timeout: a reset (Event 129) every ~285 s, each one a multi-second
    // stall for Explorer and Start. Measured on this machine: resets every
    // 4m45s idle, ZERO in 15 min with one 4 KiB read every 45 s. So a disk
    // that shows exactly that signature -- USB, periodic resets, present now
    // -- is kept busy with a tiny read. It is remembered by what it is
    // (letters + model), not by its RaidPort number, which changes on every
    // re-plug; and once earned it stays on, so the cure does not age out of
    // the 24 h window and switch itself off.
    public static class KeepAwakeRule
    {
        public const int MinResets = 4;

        public static bool Qualifies(StorageSummary s)
        {
            return s != null && s.Disk != null && !s.Disk.Gone && s.Disk.Bus == "USB" && s.Disk.Letters.Length > 0
                && s.Cadence != null && s.Cadence.Periodic && s.Cadence.Count >= MinResets;
        }

        public static string Identity(DiskFacts d)
        {
            return d == null ? "" : d.Letters + "|" + d.Model;
        }
    }

    public static class EaseTargets
    {
        public const string BelowNormal = "below_normal";
        public const string Idle = "idle";
        public static bool IsKnown(string s) { return s == BelowNormal || s == Idle; }
    }

    // Process base priority as the kernel reports it per priority class.
    public static class Priorities
    {
        public const int Idle = 4;
        public const int BelowNormal = 6;
        public const int Normal = 8;
        public const int AboveNormal = 10;
        public const int High = 13;
        public const int Realtime = 24;

        public static string Name(int basePriority)
        {
            switch (basePriority)
            {
                case Idle: return "Idle";
                case BelowNormal: return "BelowNormal";
                case Normal: return "Normal";
                case AboveNormal: return "AboveNormal";
                case High: return "High";
                case Realtime: return "Realtime";
                default: return "base " + basePriority.ToString(CultureInfo.InvariantCulture);
            }
        }
    }

    // ------------------------------------------------------------- identity

    // PID is not identity; PID + creation time is. A reused PID starts from
    // zero instead of inheriting a dead process's state (same rule as SAISPIN).
    public struct ProcKey : IEquatable<ProcKey>
    {
        public readonly int Pid;
        public readonly long CreateTime;

        public ProcKey(int pid, long createTime) { Pid = pid; CreateTime = createTime; }

        public bool Equals(ProcKey o) { return Pid == o.Pid && CreateTime == o.CreateTime; }
        public override bool Equals(object o) { return o is ProcKey && Equals((ProcKey)o); }
        public override int GetHashCode() { return Pid ^ CreateTime.GetHashCode(); }
        public override string ToString()
        {
            return Pid.ToString(CultureInfo.InvariantCulture) + ":" + CreateTime.ToString(CultureInfo.InvariantCulture);
        }

        public static bool TryParse(string s, out ProcKey key)
        {
            key = new ProcKey();
            if (s == null) return false;
            int c = s.IndexOf(':');
            if (c <= 0) return false;
            int pid; long ct;
            if (!int.TryParse(s.Substring(0, c), NumberStyles.Integer, CultureInfo.InvariantCulture, out pid)) return false;
            if (!long.TryParse(s.Substring(c + 1), NumberStyles.Integer, CultureInfo.InvariantCulture, out ct)) return false;
            key = new ProcKey(pid, ct);
            return true;
        }
    }

    // One process in one sample, already resolved by the adapter.
    public sealed class ProcView
    {
        public ProcKey Key;
        public int ParentPid;
        public bool ParentAlive;
        public string Image = "";       // lower-case exe name
        public int SessionId;
        public int BasePriority = Priorities.Normal;
        public double CoreShare;        // % of ONE core over the last interval
        public long CpuTicks;           // cumulative user+kernel, 100 ns
        public long AgeMs;
        public long PrivateBytes;
        public bool Self;               // SAIOPT itself or one of its siblings
        public bool Focused;            // foreground family, or inside the focus grace
        public bool Audible;            // rendering sound right now
        public bool InputHelper;        // launched an input-hook tool (AutoHotkey): part of your input path
        public bool Suspended;          // every thread suspended
        public bool HasWindow;          // owns a visible top-level window
    }

    // ---------------------------------------------------------------- guards

    public static class Guard
    {
        // Never eased and never swept, whatever the config says. The shell,
        // the session, input and audio plumbing, the console hosts that draw
        // every terminal, the tools you use to look at processes, and anything
        // that owns a low-level input hook -- slowing one of those is felt as
        // "the whole PC lags", which is the exact thing SAIOPT exists to stop.
        static readonly HashSet<string> Hard = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "system", "idle", "registry", "memory compression", "secure system",
            "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe",
            "svchost.exe", "fontdrvhost.exe", "dwm.exe", "explorer.exe", "sihost.exe", "ctfmon.exe",
            "taskhostw.exe", "runtimebroker.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
            "searchapp.exe", "searchhost.exe", "searchui.exe", "textinputhost.exe", "applicationframehost.exe",
            "lockapp.exe", "logonui.exe", "consent.exe", "dllhost.exe", "wudfhost.exe",
            "audiodg.exe", "conhost.exe", "openconsole.exe", "windowsterminal.exe",
            "taskmgr.exe", "procexp.exe", "procexp64.exe", "perfmon.exe", "resmon.exe",
            "autohotkey.exe", "autohotkey64.exe", "autohotkeyu32.exe", "autohotkeyu64.exe", "autohotkeya32.exe",
            "obs32.exe", "obs64.exe",
            "saiopt.exe", "saituls.exe", "taskbaredge.exe"
        };

        public static bool IsHard(string image) { return image != null && Hard.Contains(image); }

        // Input-hook tools. Their launcher (a macro controller, a script
        // host) is on the same input path and is protected with them.
        public static bool IsInputHook(string image)
        {
            return image != null && image.StartsWith("autohotkey", StringComparison.OrdinalIgnoreCase);
        }

        // null = may be eased. Otherwise a short reason.
        public static string EaseBlock(ProcView p, OptConfig cfg)
        {
            if (p.Self) return "self";
            if (p.SessionId == 0) return "service session";
            if (p.Key.Pid <= 4) return "kernel";
            if (IsHard(p.Image)) return "shell/system";
            if (cfg.NeverEase.Contains(p.Image)) return "never_ease";
            if (p.Focused) return "in use";
            if (p.InputHelper) return "drives your input";
            if (cfg.ProtectAudio && p.Audible) return "playing sound";
            return null;
        }
    }

    // ---------------------------------------------------------------- ease

    public enum EaseKind { Ease, Restore, Forget }

    public sealed class EaseAction
    {
        public EaseKind Kind;
        public ProcKey Key;
        public string Image;
        public int FromPriority;
        public int ToPriority;
        public double CoreShare;
        public string Reason;
    }

    public sealed class EasedEntry
    {
        public ProcKey Key;
        public string Image;
        public int OriginalPriority;
        public int EasedPriority;
        public long SinceMs;
        public double CoreShare;
    }

    public sealed class EaseEngine
    {
        sealed class Track
        {
            public int Hot;
            public int Quiet;
            public bool Eased;
            public bool Refused;        // adapter could not change it: never retried
            public EasedEntry Entry;
        }

        readonly Dictionary<ProcKey, Track> tracks = new Dictionary<ProcKey, Track>();
        int calmStreak;
        public OptConfig Cfg;

        public EaseEngine(OptConfig cfg) { Cfg = cfg ?? OptConfig.Defaults(); }

        public int EasedCount
        {
            get { int n = 0; foreach (Track t in tracks.Values) if (t.Eased) n++; return n; }
        }

        public List<EasedEntry> Eased()
        {
            List<EasedEntry> list = new List<EasedEntry>();
            foreach (Track t in tracks.Values) if (t.Eased) list.Add(t.Entry);
            list.Sort(delegate(EasedEntry a, EasedEntry b) { return b.CoreShare.CompareTo(a.CoreShare); });
            return list;
        }

        // Adopts a process eased by a previous SAIOPT run (state file) so that
        // it is restored by the normal rules instead of being stranded.
        public void Adopt(EasedEntry e)
        {
            Track t = new Track();
            t.Eased = true;
            t.Entry = e;
            tracks[e.Key] = t;
        }

        // The adapter could not apply an Ease (access denied, gone). The
        // process is left alone for the rest of its life.
        public void Refuse(ProcKey key)
        {
            Track t;
            if (!tracks.TryGetValue(key, out t)) { t = new Track(); tracks[key] = t; }
            t.Eased = false;
            t.Entry = null;
            t.Refused = true;
        }

        static EaseAction Act(EaseKind kind, Track t, ProcView p, string reason)
        {
            EaseAction a = new EaseAction();
            a.Kind = kind;
            a.Key = t.Entry != null ? t.Entry.Key : p.Key;
            a.Image = t.Entry != null ? t.Entry.Image : p.Image;
            a.FromPriority = kind == EaseKind.Ease ? (p != null ? p.BasePriority : Priorities.Normal) : (t.Entry != null ? t.Entry.EasedPriority : 0);
            a.ToPriority = kind == EaseKind.Ease ? 0 : (t.Entry != null ? t.Entry.OriginalPriority : Priorities.Normal);
            a.CoreShare = p != null ? p.CoreShare : 0;
            a.Reason = reason;
            return a;
        }

        public List<EaseAction> Observe(IList<ProcView> procs, double machineBusyPct, long nowMs, string focusImage)
        {
            List<EaseAction> actions = new List<EaseAction>();
            bool busy = machineBusyPct >= Cfg.BusyPct;
            if (machineBusyPct < Cfg.CalmPct) calmStreak++; else calmStreak = 0;
            bool machineCalm = calmStreak >= Cfg.CalmSamples;

            HashSet<ProcKey> seen = new HashSet<ProcKey>();
            int eased = EasedCount;

            for (int i = 0; i < procs.Count; i++)
            {
                ProcView p = procs[i];
                seen.Add(p.Key);
                Track t;
                if (!tracks.TryGetValue(p.Key, out t)) { t = new Track(); tracks[p.Key] = t; }
                if (t.Refused) continue;

                if (t.Eased)
                {
                    t.Entry.CoreShare = p.CoreShare;
                    // Somebody else (the user, the app, a governor) set a new
                    // priority after us. It is theirs now: forget, never fight.
                    if (p.BasePriority != t.Entry.EasedPriority)
                    {
                        actions.Add(Act(EaseKind.Forget, t, p, "priority changed by someone else - left as is"));
                        t.Eased = false; t.Entry = null; t.Refused = true; eased--;
                        continue;
                    }
                    string back = null;
                    if (p.Focused) back = "you switched to it";
                    else if (Cfg.ProtectAudio && p.Audible) back = "it started playing sound";
                    else if (!Cfg.Ease) back = "easing was switched off";
                    else if (Cfg.NeverEase.Contains(p.Image)) back = "it is on the never-ease list now";
                    else if (p.InputHelper) back = "it drives your input";
                    else if (machineCalm) back = "the machine has headroom again";
                    else
                    {
                        if (p.CoreShare < Cfg.HogCorePct / 2.0) t.Quiet++; else t.Quiet = 0;
                        if (t.Quiet >= Cfg.CalmSamples) back = "it quieted down";
                    }
                    if (back != null)
                    {
                        actions.Add(Act(EaseKind.Restore, t, p, back));
                        t.Eased = false; t.Entry = null; t.Hot = 0; t.Quiet = 0; eased--;
                    }
                    continue;
                }

                if (p.CoreShare >= Cfg.HogCorePct) t.Hot++; else t.Hot = 0;
                if (!Cfg.Ease || !busy || t.Hot < Cfg.HogSamples || eased >= Cfg.MaxEased) continue;
                // Only a process still at Normal is ours to touch. Anything else
                // was set on purpose by somebody -- including a governor that
                // already manages it (GOV.ps1 and ffmpeg) -- and stays theirs.
                if (p.BasePriority != Priorities.Normal) continue;
                if (Guard.EaseBlock(p, Cfg) != null) continue;

                EasedEntry e = new EasedEntry();
                e.Key = p.Key;
                e.Image = p.Image;
                e.OriginalPriority = p.BasePriority;
                e.EasedPriority = Cfg.EasePriority;
                e.SinceMs = nowMs;
                e.CoreShare = p.CoreShare;
                t.Eased = true; t.Entry = e; t.Quiet = 0;
                eased++;
                string why = Words.CoreShare(p.CoreShare) + " for " + Words.Duration((long)t.Hot * Cfg.SampleMs)
                    + " with the machine at " + Math.Round(machineBusyPct).ToString(CultureInfo.InvariantCulture) + "%";
                if (!string.IsNullOrEmpty(focusImage))
                    why += string.Equals(focusImage, p.Image, StringComparison.OrdinalIgnoreCase)
                        ? " while you used another " + focusImage + " window" : " while you used " + focusImage;
                EaseAction a = Act(EaseKind.Ease, t, p, why);
                a.ToPriority = e.EasedPriority;
                actions.Add(a);
            }

            // Gone processes: nothing to restore, drop the bookkeeping.
            List<ProcKey> dead = new List<ProcKey>();
            foreach (KeyValuePair<ProcKey, Track> kv in tracks)
                if (!seen.Contains(kv.Key)) dead.Add(kv.Key);
            foreach (ProcKey k in dead)
            {
                Track t = tracks[k];
                if (t.Eased)
                {
                    EaseAction a = Act(EaseKind.Forget, t, null, "it exited");
                    actions.Add(a);
                }
                tracks.Remove(k);
            }
            return actions;
        }

        // Shutdown / pause: every process still eased goes back to where it was.
        public List<EaseAction> RestoreAll(string reason)
        {
            List<EaseAction> actions = new List<EaseAction>();
            foreach (Track t in tracks.Values)
            {
                if (!t.Eased) continue;
                actions.Add(Act(EaseKind.Restore, t, null, reason));
                t.Eased = false; t.Entry = null; t.Hot = 0; t.Quiet = 0;
            }
            return actions;
        }
    }

    // ---------------------------------------------------------------- sweep

    public sealed class StaleItem
    {
        public ProcKey Key;
        public string Image;
        public long PrivateBytes;
        public string Reason;
        public bool Auto;            // eligible for automatic ending (armed + observed)
    }

    // "Stale" means dead weight nobody can reach any more: an orphan (parent
    // gone) with no visible window that is either frozen (every thread
    // suspended) or a console tool that has not used a single CPU tick for
    // hours. A sleeping orphan with a window, a live parent, or any recent
    // CPU is NOT stale -- the SAISPIN rule that most orphans are harmless
    // holds here too.
    public sealed class SweepTracker
    {
        sealed class Seen
        {
            public long FirstMs;
            public long SuspendedSinceMs = -1;
            public bool SuspendedAtFirstSight;
            public long LastTicks = -1;
            public long IdleSinceMs;
        }

        // Console tools whose orphans are safe to call idle: an interactive
        // program would have a window, a resident would not be a console tool.
        static readonly HashSet<string> ConsoleTools = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "node.exe", "python.exe", "python3.exe", "py.exe", "agy.exe", "bun.exe", "deno.exe",
            "cmd.exe", "powershell.exe", "pwsh.exe", "git.exe", "ssh.exe", "npm.exe", "npx.exe",
            "uv.exe", "pytest.exe", "dotnet.exe", "go.exe", "cargo.exe", "curl.exe"
        };

        public static bool IsConsoleTool(string image) { return image != null && ConsoleTools.Contains(image); }

        readonly Dictionary<ProcKey, Seen> seen = new Dictionary<ProcKey, Seen>();
        public OptConfig Cfg;

        public SweepTracker(OptConfig cfg) { Cfg = cfg ?? OptConfig.Defaults(); }

        public List<StaleItem> Observe(IList<ProcView> procs, long nowMs)
        {
            List<StaleItem> stale = new List<StaleItem>();
            HashSet<ProcKey> live = new HashSet<ProcKey>();
            long suspendMs = (long)Cfg.StaleSuspendedMin * 60000L;
            long idleMs = (long)Cfg.StaleIdleHours * 3600000L;

            for (int i = 0; i < procs.Count; i++)
            {
                ProcView p = procs[i];
                live.Add(p.Key);
                Seen s;
                if (!seen.TryGetValue(p.Key, out s))
                {
                    s = new Seen();
                    s.FirstMs = nowMs;
                    s.IdleSinceMs = nowMs;
                    s.SuspendedAtFirstSight = p.Suspended;
                    seen[p.Key] = s;
                }
                if (p.Suspended) { if (s.SuspendedSinceMs < 0) s.SuspendedSinceMs = nowMs; }
                else { s.SuspendedSinceMs = -1; s.SuspendedAtFirstSight = false; }
                if (p.CpuTicks != s.LastTicks) { s.LastTicks = p.CpuTicks; s.IdleSinceMs = nowMs; }

                if (p.Self || p.SessionId == 0 || p.Key.Pid <= 4 || Guard.IsHard(p.Image)) continue;
                if (p.ParentAlive || p.HasWindow || p.Focused) continue;

                string reason = null;
                bool observed = false;
                if (p.Suspended)
                {
                    long observedMs = nowMs - s.SuspendedSinceMs;
                    // Already frozen when we first looked: the process age is the
                    // best lower bound we have. Good enough to SUGGEST, never to
                    // act on unattended.
                    long knownMs = s.SuspendedAtFirstSight ? Math.Max(observedMs, p.AgeMs) : observedMs;
                    if (knownMs >= suspendMs)
                    {
                        reason = "suspended " + Words.Duration(knownMs) + ", parent gone";
                        observed = observedMs >= suspendMs;
                    }
                }
                else if (IsConsoleTool(p.Image))
                {
                    long idle = nowMs - s.IdleSinceMs;
                    if (idle >= idleMs)
                    {
                        reason = "no CPU for " + Words.Duration(idle) + ", parent gone";
                        observed = true;
                    }
                }
                if (reason == null) continue;

                StaleItem it = new StaleItem();
                it.Key = p.Key;
                it.Image = p.Image;
                it.PrivateBytes = p.PrivateBytes;
                it.Reason = reason;
                it.Auto = observed && Cfg.SweepAuto.Contains(p.Image);
                stale.Add(it);
            }

            List<ProcKey> gone = new List<ProcKey>();
            foreach (ProcKey k in seen.Keys) if (!live.Contains(k)) gone.Add(k);
            foreach (ProcKey k in gone) seen.Remove(k);
            return stale;
        }

        // Stale items grouped per image, largest first: "5x agy.exe, 285 MB".
        public static List<KeyValuePair<string, List<StaleItem>>> Group(IList<StaleItem> items)
        {
            Dictionary<string, List<StaleItem>> map = new Dictionary<string, List<StaleItem>>(StringComparer.OrdinalIgnoreCase);
            foreach (StaleItem it in items)
            {
                List<StaleItem> l;
                if (!map.TryGetValue(it.Image, out l)) { l = new List<StaleItem>(); map[it.Image] = l; }
                l.Add(it);
            }
            List<KeyValuePair<string, List<StaleItem>>> list = new List<KeyValuePair<string, List<StaleItem>>>(map);
            list.Sort(delegate(KeyValuePair<string, List<StaleItem>> a, KeyValuePair<string, List<StaleItem>> b)
            {
                return Bytes(b.Value).CompareTo(Bytes(a.Value));
            });
            return list;
        }

        public static long Bytes(IList<StaleItem> items)
        {
            long n = 0;
            foreach (StaleItem it in items) n += it.PrivateBytes;
            return n;
        }
    }

    // ---------------------------------------------------------------- watch

    public static class ShellWords
    {
        public const string Responsive = "RESPONSIVE";
        public const string Slow = "SLOW";
        public const string Stalled = "NOT RESPONDING";
        public const string Unknown = "UNKNOWN";
    }

    // Taskbar round-trip history. -1 = the probe timed out.
    public sealed class ShellHealth
    {
        readonly int capacity;
        readonly List<int> ms = new List<int>();

        public ShellHealth(int capacity) { this.capacity = Math.Max(4, capacity); }

        public void Add(int latencyMs)
        {
            ms.Add(latencyMs);
            if (ms.Count > capacity) ms.RemoveAt(0);
        }

        public int Count { get { return ms.Count; } }
        public int Last { get { return ms.Count == 0 ? 0 : ms[ms.Count - 1]; } }
        public IList<int> Samples { get { return ms.AsReadOnly(); } }

        // Judged on the last few probes, so one hiccup is not news and a real
        // stall shows within seconds.
        public string Word(int slowMs)
        {
            if (ms.Count == 0) return ShellWords.Unknown;
            int n = Math.Min(5, ms.Count);
            int worst = 0;
            int slow = 0;
            for (int i = ms.Count - n; i < ms.Count; i++)
            {
                if (ms[i] < 0) return ShellWords.Stalled;
                if (ms[i] > worst) worst = ms[i];
                if (ms[i] >= slowMs) slow++;
            }
            return slow >= 2 ? ShellWords.Slow : ShellWords.Responsive;
        }

        public int Median()
        {
            if (ms.Count == 0) return 0;
            List<int> ok = new List<int>();
            foreach (int v in ms) if (v >= 0) ok.Add(v);
            if (ok.Count == 0) return -1;
            ok.Sort();
            return ok[ok.Count / 2];
        }
    }

    public sealed class CadenceResult
    {
        public int Count;
        public double MedianSeconds = -1;
        public bool Periodic;
    }

    public static class Cadence
    {
        // The Shell Doctor definition, ported: a fixed interval (>= 60 % of
        // gaps within max(10 %, 5 s) of the median) means something trips on a
        // timer -- bridge firmware, a power state, a poller.
        public static CadenceResult Of(IList<DateTime> times)
        {
            CadenceResult r = new CadenceResult();
            if (times == null || times.Count == 0) return r;
            List<DateTime> sorted = new List<DateTime>(times);
            sorted.Sort();
            r.Count = sorted.Count;
            if (sorted.Count < 4) return r;
            List<double> gaps = new List<double>();
            for (int i = 1; i < sorted.Count; i++) gaps.Add((sorted[i] - sorted[i - 1]).TotalSeconds);
            gaps.Sort();
            double median = gaps[gaps.Count / 2];
            r.MedianSeconds = Math.Round(median);
            if (median > 0)
            {
                double tol = Math.Max(median * 0.1, 5);
                int near = 0;
                foreach (double g in gaps) if (Math.Abs(g - median) <= tol) near++;
                r.Periodic = (double)near / gaps.Count >= 0.6;
            }
            return r;
        }
    }

    public sealed class DiskFacts
    {
        public int RaidPort = -1;
        public string Model = "";
        public string Letters = "";     // "U:" or "S: T:"
        public string Bus = "";
        public bool Gone;               // was here earlier, not enumerated now
    }

    public sealed class StorageSummary
    {
        public int RaidPort;
        public DiskFacts Disk;          // null = no current disk owns that port
        public int LastHour;
        public int Last24h;
        public DateTime LastTime;
        public CadenceResult Cadence;
        public bool Active;             // worth the user's attention right now

        public string Who()
        {
            if (Disk == null) return "RaidPort" + RaidPort.ToString(CultureInfo.InvariantCulture) + " (disk unknown)";
            string who = Disk.Letters.Length > 0 ? Disk.Letters + " " : "";
            who += Words.ShortModel(Disk.Model);
            if (Disk.Bus.Length > 0) who += " (" + Disk.Bus + ")";
            if (Disk.Gone) who += " - gone";
            return who;
        }
    }

    // Storage resets (Event 129 and friends) per storport adapter. Every reset
    // is a multi-second I/O stall for anything touching that disk -- Explorer
    // and Start first, because they enumerate drives constantly.
    public sealed class StorageWatch
    {
        readonly Dictionary<int, List<DateTime>> byPort = new Dictionary<int, List<DateTime>>();
        public const int ActiveWindowMinutes = 30;

        public void Add(int raidPort, DateTime time)
        {
            List<DateTime> l;
            if (!byPort.TryGetValue(raidPort, out l)) { l = new List<DateTime>(); byPort[raidPort] = l; }
            if (l.Count > 0 && l[l.Count - 1] == time) return;
            l.Add(time);
            if (l.Count > 2000) l.RemoveRange(0, l.Count - 2000);
        }

        public int CountSince(int raidPort, DateTime since)
        {
            List<DateTime> l;
            if (!byPort.TryGetValue(raidPort, out l)) return 0;
            int n = 0;
            foreach (DateTime t in l) if (t >= since) n++;
            return n;
        }

        public void Prune(DateTime now)
        {
            foreach (List<DateTime> l in byPort.Values) l.RemoveAll(delegate(DateTime t) { return (now - t).TotalHours > 24; });
        }

        public List<StorageSummary> Summaries(DateTime now, IDictionary<int, DiskFacts> disks)
        {
            List<StorageSummary> list = new List<StorageSummary>();
            foreach (KeyValuePair<int, List<DateTime>> kv in byPort)
            {
                StorageSummary s = new StorageSummary();
                s.RaidPort = kv.Key;
                DiskFacts d;
                s.Disk = disks != null && disks.TryGetValue(kv.Key, out d) ? d : null;
                List<DateTime> recent = new List<DateTime>();
                foreach (DateTime t in kv.Value)
                {
                    double h = (now - t).TotalHours;
                    if (h <= 24 && h >= -0.1) { recent.Add(t); s.Last24h++; }
                    if (h <= 1 && h >= -0.1) s.LastHour++;
                    if (t > s.LastTime) s.LastTime = t;
                }
                if (s.Last24h == 0) continue;
                s.Cadence = Cadence.Of(recent);
                s.Active = (now - s.LastTime).TotalMinutes <= ActiveWindowMinutes && (s.LastHour >= 2 || s.Cadence.Periodic);
                list.Add(s);
            }
            list.Sort(delegate(StorageSummary a, StorageSummary b) { return b.LastTime.CompareTo(a.LastTime); });
            return list;
        }
    }

    // Writes Windows could not complete: NTFS delayed-write failures (50),
    // journal flush failures (140), paging errors (disk 51) and surprise
    // removals (disk 157). One dropped enclosure produces dozens of these in
    // a few seconds; they are one incident, told once, with the volumes named.
    public static class LossKinds
    {
        public const string LostWrite = "lost-write";
        public const string JournalFlush = "journal-flush";
        public const string PagingError = "paging-error";
        public const string Removed = "removed";
    }

    public sealed class LossIncident
    {
        public DateTime First;
        public DateTime Last;
        public readonly SortedDictionary<string, bool> Volumes = new SortedDictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
        public int LostWrites, JournalFlushes, PagingErrors, Removals;

        public string Subject { get { return "loss:" + First.Ticks.ToString(CultureInfo.InvariantCulture); } }

        // Letters when Windows named any ("E: S:"); otherwise the bare disk
        // numbers the disk driver used ("disk 10").
        public string Where()
        {
            List<string> letters = new List<string>();
            List<string> others = new List<string>();
            foreach (string v in Volumes.Keys)
                if (v.Length == 2 && v[1] == ':') letters.Add(v); else others.Add(v);
            List<string> use = letters.Count > 0 ? letters : others;
            return use.Count == 0 ? "a disk" : string.Join(" ", use.ToArray());
        }

        public string What()
        {
            List<string> parts = new List<string>();
            if (LostWrites > 0) parts.Add(LostWrites + " lost write" + (LostWrites == 1 ? "" : "s"));
            if (JournalFlushes > 0) parts.Add(JournalFlushes + " journal flush failure" + (JournalFlushes == 1 ? "" : "s"));
            if (PagingErrors > 0) parts.Add(PagingErrors + " paging error" + (PagingErrors == 1 ? "" : "s"));
            if (Removals > 0) parts.Add(Removals + " surprise removal" + (Removals == 1 ? "" : "s"));
            return string.Join(", ", parts.ToArray());
        }

        // Only a failed write means data may be gone; a lone paging error or
        // removal is a warning about the device, not about your files.
        public bool DataAtRisk { get { return LostWrites > 0 || JournalFlushes > 0; } }
    }

    public sealed class LossWatch
    {
        public const int BurstGapSeconds = 120;
        readonly List<KeyValuePair<DateTime, KeyValuePair<string, string>>> events = new List<KeyValuePair<DateTime, KeyValuePair<string, string>>>();

        public void Add(DateTime time, string kind, string volume)
        {
            events.Add(new KeyValuePair<DateTime, KeyValuePair<string, string>>(time, new KeyValuePair<string, string>(kind, volume ?? "")));
            if (events.Count > 5000) events.RemoveRange(0, events.Count - 5000);
        }

        public void Prune(DateTime now)
        {
            events.RemoveAll(delegate(KeyValuePair<DateTime, KeyValuePair<string, string>> e) { return (now - e.Key).TotalHours > 24; });
        }

        public List<LossIncident> Incidents(DateTime now)
        {
            List<KeyValuePair<DateTime, KeyValuePair<string, string>>> sorted = new List<KeyValuePair<DateTime, KeyValuePair<string, string>>>(events);
            sorted.Sort(delegate(KeyValuePair<DateTime, KeyValuePair<string, string>> a, KeyValuePair<DateTime, KeyValuePair<string, string>> b)
            {
                return a.Key.CompareTo(b.Key);
            });
            List<LossIncident> list = new List<LossIncident>();
            LossIncident cur = null;
            foreach (KeyValuePair<DateTime, KeyValuePair<string, string>> e in sorted)
            {
                if ((now - e.Key).TotalHours > 24) continue;
                if (cur == null || (e.Key - cur.Last).TotalSeconds > BurstGapSeconds)
                {
                    cur = new LossIncident();
                    cur.First = e.Key;
                    list.Add(cur);
                }
                cur.Last = e.Key;
                string kind = e.Value.Key, vol = e.Value.Value;
                if (vol.Length > 0) cur.Volumes[vol] = true;
                if (kind == LossKinds.LostWrite) cur.LostWrites++;
                else if (kind == LossKinds.JournalFlush) cur.JournalFlushes++;
                else if (kind == LossKinds.PagingError) cur.PagingErrors++;
                else if (kind == LossKinds.Removed) cur.Removals++;
            }
            list.Reverse();
            return list;
        }
    }

    // "The same news is told once" (SAISPIN rule): a repeat balloon for the
    // same subject is held back for remind_min; a changed verdict is new
    // information and is told immediately.
    public sealed class NewsGate
    {
        sealed class Told { public string Verdict; public long AtMs; }
        readonly Dictionary<string, Told> told = new Dictionary<string, Told>(StringComparer.Ordinal);

        public bool ShouldTell(string subject, string verdict, long nowMs, int remindMin)
        {
            Told t;
            if (told.TryGetValue(subject, out t) && t.Verdict == verdict && nowMs - t.AtMs < (long)remindMin * 60000L)
                return false;
            if (t == null) { t = new Told(); told[subject] = t; }
            t.Verdict = verdict;
            t.AtMs = nowMs;
            return true;
        }
    }

    // ---------------------------------------------------------------- status

    public static class StatusWords
    {
        public const string Calm = "CALM";
        public const string Easing = "EASING";
        public const string Attention = "ATTENTION";
        public const string Paused = "PAUSED";

        public static string Of(bool paused, int eased, bool storageActive, string shellWord, bool explorerLeak)
        {
            return Of(paused, eased, storageActive, shellWord, explorerLeak, false);
        }

        public static string Of(bool paused, int eased, bool storageActive, string shellWord, bool explorerLeak, bool dataAtRisk)
        {
            if (dataAtRisk || storageActive || shellWord == ShellWords.Stalled || shellWord == ShellWords.Slow || explorerLeak)
                return Attention;
            if (paused) return Paused;
            if (eased > 0) return Easing;
            return Calm;
        }
    }

    // Explorer's GDI/USER objects are capped at 10 000 per process; a shell
    // near the cap stops drawing menus and icons long before it crashes.
    public static class ExplorerLimits
    {
        public const int LeakWarn = 6000;
        public static bool Leaking(int gdi, int user) { return gdi >= LeakWarn || user >= LeakWarn; }
    }

    // ---------------------------------------------------------------- words

    public static class Words
    {
        static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        public static string CoreShare(double pctOfOneCore)
        {
            if (pctOfOneCore >= 150) return (pctOfOneCore / 100.0).ToString("0.0", Inv) + " cores";
            return Math.Round(pctOfOneCore).ToString(Inv) + "% of a core";
        }

        public static string Bytes(long b)
        {
            if (b >= 1L << 30) return (b / (double)(1L << 30)).ToString("0.0", Inv) + " GB";
            if (b >= 1L << 20) return Math.Round(b / (double)(1L << 20)).ToString(Inv) + " MB";
            if (b >= 1L << 10) return Math.Round(b / 1024.0).ToString(Inv) + " KB";
            return b.ToString(Inv) + " B";
        }

        public static string Duration(long ms)
        {
            if (ms < 0) ms = 0;
            long s = ms / 1000;
            if (s < 60) return s.ToString(Inv) + " s";
            long m = s / 60;
            if (m < 60) return m.ToString(Inv) + "m" + (s % 60 == 0 ? "" : (s % 60).ToString("00", Inv) + "s");
            long h = m / 60;
            if (h < 48) return h.ToString(Inv) + " h" + (m % 60 == 0 ? "" : " " + (m % 60).ToString(Inv) + " min");
            return (h / 24).ToString(Inv) + " d";
        }

        public static string ShortModel(string model)
        {
            if (string.IsNullOrEmpty(model)) return "disk";
            string m = model.Replace(" SCSI Disk Device", "").Replace(" USB Device", "").Trim();
            return m.Length > 32 ? m.Substring(0, 32) : m;
        }

        // Tray tooltips are rejected past 63 characters, and a rejected
        // tooltip aborts the whole icon update (UI.md, "Shell limits").
        public static string Tooltip(string text)
        {
            if (text == null) return "";
            return text.Length <= 63 ? text : text.Substring(0, 62) + "~";
        }
    }
}
