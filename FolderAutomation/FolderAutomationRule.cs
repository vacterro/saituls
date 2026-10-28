using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text.RegularExpressions;

// Folder Automation — matching. Pure functions over a metadata snapshot, so
// the whole rule semantics are testable without touching a disk.

namespace Saituls.FolderAutomation
{
    public sealed class FaFileInfo
    {
        public string FullPath = "";
        public string Name = "";
        public string Directory = "";
        public string Extension = "";
        public long Length;
        public DateTime CreatedUtc;
        public DateTime ModifiedUtc;
        public bool IsDirectory;
        public bool Hidden;

        public FaFileInfo() { }

        public static FaFileInfo From(string fullPath)
        {
            return From(fullPath, new SystemFileSystem());
        }

        // Metadata comes through the same abstraction the actions use, so a
        // test double really does describe the files the engine will act on.
        public static FaFileInfo From(string fullPath, IFileSystem fs)
        {
            FaFileInfo f = new FaFileInfo();
            f.FullPath = FaPaths.Normalize(fullPath);
            f.Name = Path.GetFileName(f.FullPath);
            f.Directory = Path.GetDirectoryName(f.FullPath);
            f.Extension = Path.GetExtension(f.Name).ToLowerInvariant();
            try
            {
                FileAttributes a = fs.GetAttributes(f.FullPath);
                f.IsDirectory = (a & FileAttributes.Directory) == FileAttributes.Directory;
                f.Hidden = (a & FileAttributes.Hidden) == FileAttributes.Hidden;
            }
            catch { }
            try
            {
                f.Length = f.IsDirectory ? 0 : fs.FileLength(f.FullPath);
                f.CreatedUtc = fs.FileCreatedUtc(f.FullPath);
                f.ModifiedUtc = fs.FileModifiedUtc(f.FullPath);
            }
            catch { }
            return f;
        }
    }

    public static class FaExtensionGroups
    {
        public static readonly Dictionary<string, string[]> Map = new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase)
        {
            { "image", new string[] { ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".raw", ".svg" } },
            { "video", new string[] { ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts" } },
            { "audio", new string[] { ".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma", ".opus" } },
            { "archive", new string[] { ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".cab" } },
            { "document", new string[] { ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".txt", ".rtf", ".md", ".csv" } },
            { "installer", new string[] { ".exe", ".msi", ".msix", ".appx", ".iso", ".img" } }
        };

        public static bool InGroup(string ext, IEnumerable<string> groupNames)
        {
            foreach (string g in groupNames)
            {
                string[] list;
                if (!Map.TryGetValue((g ?? "").Trim(), out list)) continue;
                foreach (string e in list) if (string.Equals(e, ext, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }
    }

    public static class FaVars
    {
        public static string Expand(string template, FaProfile profile, FaRule rule, FaFileInfo file, DateTime now)
        {
            if (string.IsNullOrEmpty(template)) return template;
            string name = file.Name;
            string stem = name;
            string ext = file.Extension;
            if (!string.IsNullOrEmpty(ext))
            {
                stem = name.Substring(0, name.Length - ext.Length);
                ext = ext.TrimStart('.');           // {ext} yields "jpg", {dot_ext} "."
            }
            string s = template;
            s = Replace(s, "year", now.Year.ToString(CultureInfo.InvariantCulture));
            s = Replace(s, "month", now.Month.ToString("00", CultureInfo.InvariantCulture));
            s = Replace(s, "day", now.Day.ToString("00", CultureInfo.InvariantCulture));
            s = Replace(s, "hour", now.Hour.ToString("00", CultureInfo.InvariantCulture));
            s = Replace(s, "minute", now.Minute.ToString("00", CultureInfo.InvariantCulture));
            s = Replace(s, "name", stem);
            s = Replace(s, "ext", ext);
            s = Replace(s, "dot_ext", "." + ext);
            s = Replace(s, "profile", Safe(profile == null ? "" : profile.Name));
            s = Replace(s, "rule", Safe(rule == null ? "" : rule.Name));
            return s;
        }

        static string Safe(string s)
        {
            if (s == null) return "";
            foreach (char c in Path.GetInvalidFileNameChars()) s = s.Replace(c, '_');
            return s;
        }

        static string Replace(string s, string token, string value)
        {
            return s.Replace("{" + token + "}", value);
        }
    }

    public enum FaOutcome
    {
        None = 0,
        Matched = 1,
        Excluded = 2,     // an Exclude rule matched
        Ignored = 3,      // an Ignore rule matched
        Stopped = 4,      // a Stop rule matched
        TempSkipped = 5,  // *.crdownload etc.
        GlobSkipped = 6,  // profile exclusion pattern
        NoRule = 7
    }

    public sealed class FaDecision
    {
        public FaOutcome Outcome = FaOutcome.None;
        public FaRule Rule;
        public string Destination = "";
        public string NewName = "";
    }

    public static class FaMatcher
    {
        public static bool RuleMatches(FaRule rule, FaFileInfo file, DateTime now)
        {
            if (rule == null || rule.Conditions == null || rule.Conditions.Count == 0) return false;
            if (rule.Match == FaMatch.Any)
            {
                foreach (FaCondition c in rule.Conditions)
                    if (ConditionMatches(c, file, now)) return true;
                return false;
            }
            foreach (FaCondition c in rule.Conditions)
                if (!ConditionMatches(c, file, now)) return false;
            return true;
        }

        // First match wins. An Exclude rule short-circuits with Excluded, which
        // is why exclusions are ordinary ordered rules and not a second system.
        public static FaDecision Evaluate(FaProfile profile, FaFileInfo file, DateTime now)
        {
            FaDecision d = new FaDecision();
            if (profile == null || file == null) return d;

            // Non-recursive profiles only ever act on the direct children of a source.
            if (!profile.Recursive && !IsDirectChildOfSource(profile, file.Directory))
            { d.Outcome = FaOutcome.NoRule; return d; }

            foreach (string pat in profile.Exclusions ?? new List<string>())
                if (!string.IsNullOrEmpty(pat) && FaGlob.IsMatch(file.Name, pat))
                { d.Outcome = FaOutcome.GlobSkipped; return d; }

            // Temporary/download files never reach a rule unless the profile
            // deliberately overrides the pattern list.
            if (DefaultTempPatterns.Matches(file.Name, profile.TempPatternList()))
            { d.Outcome = FaOutcome.TempSkipped; return d; }

            foreach (FaRule r in profile.Rules ?? new List<FaRule>())
            {
                if (!r.Enabled) continue;
                if (!RuleMatches(r, file, now)) continue;
                d.Rule = r;
                if (r.Match == FaMatch.Exclude) { d.Outcome = FaOutcome.Excluded; return d; }
                if (r.Action == FaAction.Ignore) { d.Outcome = FaOutcome.Ignored; return d; }
                if (r.Action == FaAction.Stop) { d.Outcome = FaOutcome.Stopped; return d; }
                d.Outcome = FaOutcome.Matched;
                d.Destination = ResolveDestination(profile, r, file, now);
                d.NewName = r.Action == FaAction.Rename
                    ? FaVars.Expand(r.RenameTemplate, profile, r, file, now)
                    : file.Name;
                return d;
            }
            d.Outcome = FaOutcome.NoRule;
            return d;
        }

        static bool IsDirectChildOfSource(FaProfile p, string dir)
        {
            foreach (string s in p.Sources ?? new List<string>())
                if (string.Equals(FaPaths.Normalize(s), FaPaths.Normalize(dir), StringComparison.OrdinalIgnoreCase))
                    return true;
            return false;
        }

        public static string ResolveDestination(FaProfile profile, FaRule rule, FaFileInfo file, DateTime now)
        {
            string raw = FaVars.Expand(rule.Destination, profile, rule, file, now);
            // A relative destination is taken against the profile root, which is
            // what a user means by "move to Images" with root D:\Sorted.
            if (!string.IsNullOrEmpty(raw) && !Path.IsPathRooted(raw) && !string.IsNullOrEmpty(profile.DestinationRoot))
                raw = Path.Combine(profile.DestinationRoot, raw);
            return FaPaths.Normalize(raw);
        }

        public static bool ConditionMatches(FaCondition c, FaFileInfo file, DateTime now)
        {
            if (c == null) return false;
            switch ((c.Kind ?? "").Trim())
            {
                case "extension": return ExtensionMatches(c, file.Extension);
                case "extension_group": return FaExtensionGroups.InGroup(file.Extension, Split(c));
                case "filename": return StringCompare(c, file.Name);
                case "filename_contains": return file.Name.IndexOf(c.Value ?? "", StringComparison.OrdinalIgnoreCase) >= 0;
                case "filename_startswith": return file.Name.StartsWith(c.Value ?? "", StringComparison.OrdinalIgnoreCase);
                case "filename_endswith": return file.Name.EndsWith(c.Value ?? "", StringComparison.OrdinalIgnoreCase);
                case "glob": return FaGlob.IsMatch(file.Name, c.Value);
                case "regex": return RegexSafe(c.Value, file.Name);
                case "path": return PathMatches(c, file.FullPath);
                case "min_size": return file.Length >= LongOf(c);
                case "max_size": return file.Length <= LongOf(c);
                case "min_age": return file.ModifiedUtc.AddDays(DoubleOf(c) + 1e-9) <= now;
                case "max_age": return file.ModifiedUtc.AddDays(DoubleOf(c)) >= now;
                case "created_after": return file.CreatedUtc >= DateTimeOf(c);
                case "created_before": return file.CreatedUtc <= DateTimeOf(c);
                case "modified_after": return file.ModifiedUtc >= DateTimeOf(c);
                case "modified_before": return file.ModifiedUtc <= DateTimeOf(c);
                case "is_directory": return BoolOf(c) == file.IsDirectory;
                case "hidden": return BoolOf(c) == file.Hidden;
                case "weekday": return IntOf(c) == (int)now.DayOfWeek;
                case "time_window": return InWindow(c, now);
            }
            return false;
        }

        static IEnumerable<string> Split(FaCondition c)
        {
            if (c.Values != null && c.Values.Length > 0) return c.Values;
            return (c.Value ?? "").Split(new char[] { ';', ',' }, StringSplitOptions.RemoveEmptyEntries);
        }

        static bool ExtensionMatches(FaCondition c, string ext)
        {
            string want = ext.StartsWith(".", StringComparison.Ordinal) ? ext : "." + ext;
            foreach (string v in Split(c))
            {
                string s = v.Trim();
                if (s.Length == 0) continue;
                if (!s.StartsWith(".", StringComparison.Ordinal)) s = "." + s;
                if (string.Equals(s, ext, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        static bool StringCompare(FaCondition c, string value)
        {
            string op = (c.Op ?? "is").Trim();
            if (op == "matches") return FaGlob.IsMatch(value, c.Value);
            if (op == "contains") return value.IndexOf(c.Value ?? "", StringComparison.OrdinalIgnoreCase) >= 0;
            if (op == "starts") return value.StartsWith(c.Value ?? "", StringComparison.OrdinalIgnoreCase);
            if (op == "ends") return value.EndsWith(c.Value ?? "", StringComparison.OrdinalIgnoreCase);
            return string.Equals(value, (c.Value ?? "").Trim(), StringComparison.OrdinalIgnoreCase);
        }

        static bool PathMatches(FaCondition c, string fullPath)
        {
            if (fullPath == null) return false;
            if (c.Values != null && c.Values.Length > 0)
            {
                foreach (string v in c.Values)
                {
                    if (string.IsNullOrEmpty(v)) continue;
                    string n = FaPaths.Normalize(v);
                    if (FaPaths.IsInside(fullPath, n)) return true;
                }
                return false;
            }
            if (string.IsNullOrEmpty(c.Value)) return false;
            if ((c.Op ?? "is") == "matches") return FaGlob.IsMatch(fullPath, c.Value);
            if ((c.Op ?? "is") == "is_inside")
                return FaPaths.IsInside(fullPath, FaPaths.Normalize(c.Value));
            return string.Equals(FaPaths.Normalize(c.Value), fullPath, StringComparison.OrdinalIgnoreCase);
        }

        static bool RegexSafe(string pattern, string value)
        {
            if (string.IsNullOrEmpty(pattern) || value == null) return false;
            try { return Regex.IsMatch(value, pattern, RegexOptions.IgnoreCase); }
            catch { return false; }   // a malformed regex must not match, and must not throw
        }

        static long LongOf(FaCondition c)
        {
            long n;
            return long.TryParse((c.Value ?? "").Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out n) ? n : 0;
        }

        static double DoubleOf(FaCondition c)
        {
            double d;
            return double.TryParse((c.Value ?? "").Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out d) ? d : 0;
        }

        static int IntOf(FaCondition c)
        {
            int i;
            return int.TryParse((c.Value ?? "").Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out i) ? i : -1;
        }

        static bool BoolOf(FaCondition c)
        {
            string v = (c.Value ?? "").Trim().ToLowerInvariant();
            if (v == "dir" || v == "folder" || v == "directory") return true;
            return v == "true" || v == "1" || v == "yes";
        }

        static DateTime DateTimeOf(FaCondition c)
        {
            string v = (c.Value ?? "").Trim();
            DateTime d;
            if (DateTime.TryParse(v, CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeLocal | DateTimeStyles.AdjustToUniversal, out d))
                return DateTime.SpecifyKind(d, DateTimeKind.Utc);
            return DateTime.MinValue;
        }

        static bool InWindow(FaCondition c, DateTime now)
        {
            string v = (c.Value ?? "").Trim();          // HH:MM-HH:MM
            int dash = v.IndexOf('-');
            if (dash <= 0) return false;
            TimeSpan from, to;
            if (!TimeSpan.TryParseExact(v.Substring(0, dash).Trim(), @"hh\:mm", CultureInfo.InvariantCulture, out from)) return false;
            if (!TimeSpan.TryParseExact(v.Substring(dash + 1).Trim(), @"hh\:mm", CultureInfo.InvariantCulture, out to)) return false;
            TimeSpan t = now.TimeOfDay;
            if (from <= to) return t >= from && t <= to;
            return t >= from || t <= to;                 // window wraps midnight
        }
    }
}