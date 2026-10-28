using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;

// Folder Automation — path normalization and topology safety.
//
// Windows paths lie: C:\PROGRA~1, C:\Program Files\, \\?\C:\Program Files and
// a trailing dot all name the same directory. Every safety decision in this
// engine is made against FaPaths.Normalize output, never against a raw string
// from a config file.

namespace Saituls.FolderAutomation
{
    public static class FaGlob
    {
        static readonly Dictionary<string, Regex> Cache = new Dictionary<string, Regex>(StringComparer.OrdinalIgnoreCase);
        static readonly object Gate = new object();

        public static Regex ToRegex(string glob)
        {
            lock (Gate)
            {
                Regex r;
                if (Cache.TryGetValue(glob, out r)) return r;
                string body = glob.Replace(".", "\\.").Replace("**", "\u0001").Replace("*", "[^\\\\]*")
                                  .Replace("\u0001", ".*").Replace("?", ".");
                r = new Regex("^" + body + "$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
                Cache[glob] = r;
                return r;
            }
        }

        public static bool IsMatch(string value, string glob)
        {
            if (value == null || glob == null) return false;
            return ToRegex(glob).IsMatch(value);
        }
    }

    public static class FaPaths
    {
        public static string DataDir()
        {
            string over = Environment.GetEnvironmentVariable("SAITULS_FA_DIR");
            if (!string.IsNullOrEmpty(over)) return over;
            return Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "SAITULS", "FolderAutomation");
        }

        public static string ConfigPath() { return Path.Combine(DataDir(), "config.json"); }
        public static string JournalDir() { return Path.Combine(DataDir(), "journal"); }
        public static string QuarantinePath() { return Path.Combine(DataDir(), "quarantine"); }
        public static string BadConfigPath() { return Path.Combine(DataDir(), "config.bad.json"); }
        public static string LogPath() { return Path.Combine(DataDir(), "diagnostic.log"); }

        public static void EnsureDir(string dir)
        {
            try { if (!Directory.Exists(dir)) Directory.CreateDirectory(dir); }
            catch { }
        }

        public static string Normalize(string path)
        {
            if (string.IsNullOrEmpty(path)) return "";
            string p = path.Trim().Trim('"');
            if (p.StartsWith(@"\\?\", StringComparison.Ordinal))
                p = p.Substring(4);
            try { p = Environment.ExpandEnvironmentVariables(p); } catch { }
            try
            {
                p = p.Replace('/', Path.DirectorySeparatorChar);
                p = Path.GetFullPath(p);
            }
            catch { return p.TrimEnd('\\', '/'); }
            // GetFullPath keeps a trailing separator on a root like C:\.
            if (p.Length > 3) p = p.TrimEnd('\\', '/');
            return p;
        }

        public static string VolumeOf(string normalized)
        {
            if (string.IsNullOrEmpty(normalized)) return "";
            string root = Path.GetPathRoot(normalized);
            return root == null ? "" : root.ToUpperInvariant();
        }

        public static bool SameVolume(string a, string b)
        {
            return string.Equals(VolumeOf(Normalize(a)), VolumeOf(Normalize(b)), StringComparison.OrdinalIgnoreCase);
        }

        // "is b inside a" with a separator boundary, so C:\Foo is not treated as
        // containing C:\Foobar.
        public static bool IsInside(string candidate, string root)
        {
            string c = Normalize(candidate);
            string r = Normalize(root);
            if (c.Length == 0 || r.Length == 0) return false;
            if (string.Equals(c, r, StringComparison.OrdinalIgnoreCase)) return true;
            if (r.EndsWith("\\", StringComparison.Ordinal))
                return c.StartsWith(r, StringComparison.OrdinalIgnoreCase);
            return c.StartsWith(r + "\\", StringComparison.OrdinalIgnoreCase);
        }

        public static bool IsProtected(string path)
        {
            string p = Normalize(path);
            foreach (string guard in ProtectedRoots())
                if (IsInside(p, guard)) return true;
            return false;
        }

        static IEnumerable<string> ProtectedRoots()
        {
            // The Windows directory itself, never "Windows\.." -- collapsing that
            // parent step yields the drive root and would mark every file on C:
            // as protected, which would silence the whole module.
            string win = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            if (!string.IsNullOrEmpty(win)) yield return Normalize(win);
            yield return "C:\\Program Files";
            yield return "C:\\Program Files (x86)";
            yield return "C:\\ProgramData";
            yield return "C:\\Users\\All Users";
            string sys = null;
            try { sys = Environment.GetEnvironmentVariable("SystemRoot"); } catch { }
            if (!string.IsNullOrEmpty(sys)) yield return Normalize(Path.Combine(sys, "System32"));
            yield return Normalize(Path.Combine(AppDomain.CurrentDomain.BaseDirectory));
        }

        public static bool IsSystemProfile(string path)
        {
            // Well-known service/shared accounts. The user's own profile is not
            // on this list: organizing Downloads must not need a ceremony.
            return Regex.IsMatch(Normalize(path),
                @"^C:\\Users\\(Default|Public|Default User|All Users|DefaultAppPool|systemprofile|wdagutilityaccount)\\?",
                RegexOptions.IgnoreCase);
        }

        public static bool IsReparsePoint(string path)
        {
            try
            {
                FileAttributes a = File.GetAttributes(path);
                return (a & FileAttributes.ReparsePoint) == FileAttributes.ReparsePoint;
            }
            catch { return false; }
        }
    }

    public sealed class FaTopologyIssue
    {
        public string Profile = "";
        public string Detail = "";
        public bool Blocking;
    }

    // Static configuration checks. These run before a profile is enabled and
    // on every edit, so a self-feeding rule set never reaches the filesystem.
    public static class FaTopology
    {
        // The literal part of a destination, i.e. everything before the first
        // {variable}. A destination of D:\Media\{year}\Images can only ever
        // create paths under D:\Media, so the prefix is what the topology check
        // must reason about.
        public static string LiteralPrefix(string destination)
        {
            if (string.IsNullOrEmpty(destination)) return "";
            int i = destination.IndexOf('{');
            string head = i >= 0 ? destination.Substring(0, i) : destination;
            return FaPaths.Normalize(head);
        }

        public static List<string> DestinationsOf(FaProfile p)
        {
            List<string> outp = new List<string>();
            if (p == null) return outp;
            if (!string.IsNullOrEmpty(p.DestinationRoot)) outp.Add(FaPaths.Normalize(p.DestinationRoot));
            foreach (FaRule r in (p.Rules ?? new List<FaRule>()))
            {
                if (!string.IsNullOrEmpty(r.Destination)) outp.Add(LiteralPrefix(r.Destination));
            }
            return outp;
        }

        public static List<string> SourcesOf(FaProfile p)
        {
            List<string> outp = new List<string>();
            if (p == null) return outp;
            foreach (string s in (p.Sources ?? new List<string>()))
                if (!string.IsNullOrEmpty(s)) outp.Add(FaPaths.Normalize(s));
            return outp;
        }

        public static List<FaTopologyIssue> Inspect(FaProfile p)
        {
            List<FaTopologyIssue> issues = new List<FaTopologyIssue>();
            List<string> sources = SourcesOf(p);
            foreach (string src in sources)
            {
                if (FaPaths.IsProtected(src) || FaPaths.IsSystemProfile(src))
                    issues.Add(new FaTopologyIssue
                    {
                        Profile = p.Name,
                        Detail = "source is a protected system location: " + src,
                        Blocking = true
                    });
            }
            foreach (string dst in DestinationsOf(p))
            {
                if (string.IsNullOrEmpty(dst)) continue;
                if (FaPaths.IsProtected(dst) || FaPaths.IsSystemProfile(dst))
                    issues.Add(new FaTopologyIssue
                    {
                        Profile = p.Name,
                        Detail = "destination is a protected system location: " + dst,
                        Blocking = true
                    });
                foreach (string src in sources)
                {
                    if (!FaPaths.IsInside(dst, src)) continue;
                    bool excluded = false;
                    // The exclusion is written the way the user sees it: relative
                    // to the source, which is also how the scanner skips it.
                    string rel = dst.Length > src.Length ? dst.Substring(src.Length).TrimStart('\\') : "";
                    foreach (string ex in (p.Exclusions ?? new List<string>()))
                    {
                        if (string.IsNullOrEmpty(ex)) continue;
                        if (FaGlob.IsMatch(rel, ex) || FaGlob.IsMatch(dst, ex)) excluded = true;
                    }
                    if (p.Recursive && excluded) continue;   // explicit, safe exclusion
                    issues.Add(new FaTopologyIssue
                    {
                        Profile = p.Name,
                        Detail = "destination '" + dst + "' is inside the recursive source '" + src +
                                 "'; files would be re-processed indefinitely",
                        Blocking = true
                    });
                }
            }
            return issues;
        }

        // Cross-profile cycles: A->B and B->A (and longer rings).
        public static List<string> CheckCycles(FaConfig cfg)
        {
            List<string> found = new List<string>();
            if (cfg == null || cfg.Profiles == null) return found;

            List<FaProfile> live = new List<FaProfile>();
            foreach (FaProfile p in cfg.Profiles) if (p != null && p.Enabled) live.Add(p);

            for (int i = 0; i < live.Count; i++)
            {
                for (int j = 0; j < live.Count; j++)
                {
                    if (i == j) continue;
                    FaProfile a = live[i], b = live[j];
                    List<string> aTo = new List<string>();
                    foreach (string d in DestinationsOf(a))
                        foreach (string s in SourcesOf(b))
                            if (FaPaths.IsInside(d, s) || FaPaths.IsInside(s, d)) aTo.Add(d);
                    if (aTo.Count == 0) continue;
                    // A->B where B pushes anything back into A.
                    foreach (string d in DestinationsOf(b))
                    {
                        foreach (string sa in SourcesOf(a))
                        {
                            if (!FaPaths.IsInside(d, sa) && !FaPaths.IsInside(sa, d)) continue;
                            found.Add("cycle between profiles '" + a.Name + "' and '" + b.Name +
                                      "': " + aTo[0] + " <-> " + d);
                            break;
                        }
                    }
                }
            }
            return found;
        }

        // Two profiles may run at the same time only when their source and
        // destination trees do not overlap.
        public static bool Overlaps(FaProfile a, FaProfile b)
        {
            foreach (string sa in SourcesOf(a))
            {
                foreach (string d in DestinationsOf(b))
                    if (FaPaths.IsInside(sa, d) || FaPaths.IsInside(d, sa)) return true;
                foreach (string sb in SourcesOf(b))
                    if (FaPaths.IsInside(sa, sb) || FaPaths.IsInside(sb, sa)) return true;
            }
            // Two profiles writing into a common tree race on the same collision
            // counters, so a shared destination is an overlap too.
            foreach (string da in DestinationsOf(a))
                foreach (string db in DestinationsOf(b))
                    if (FaPaths.IsInside(da, db) || FaPaths.IsInside(db, da)) return true;
            return false;
        }
    }

    // Bounded diagnostic log. History and diagnostics are deliberately
    // separate files with separate retention.
    public sealed class FaLog
    {
        public const long MaxBytes = 512 * 1024;
        readonly string Path;
        readonly object Gate = new object();

        public FaLog(string path) { Path = path; }

        public void Write(string profile, string level, string message)
        {
            lock (Gate)
            {
                try
                {
                    FaPaths.EnsureDir(System.IO.Path.GetDirectoryName(Path));
                    if (File.Exists(Path) && new FileInfo(Path).Length > MaxBytes) Rotate();
                    File.AppendAllText(Path,
                        DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " [" + level + "] " +
                        (string.IsNullOrEmpty(profile) ? "-" : profile) + " " + message + Environment.NewLine);
                }
                catch { }
            }
        }

        public void Info(string profile, string m) { Write(profile, "INFO", m); }
        public void Warn(string profile, string m) { Write(profile, "WARN", m); }
        public void Error(string profile, string m) { Write(profile, "ERR", m); }

        void Rotate()
        {
            try
            {
                string old = Path + ".1";
                if (File.Exists(old)) File.Delete(old);
                File.Move(Path, old);
            }
            catch { }
        }
    }
}