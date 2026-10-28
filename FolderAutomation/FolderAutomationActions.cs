using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

// Folder Automation — the only place that mutates the filesystem.
//
// Every mutation goes through IFileSystem, so the cross-volume contract and
// "dry run performs zero mutations" are both testable with a recording double
// instead of a temp directory.

namespace Saituls.FolderAutomation
{
    public interface IFileSystem
    {
        bool FileExists(string path);
        bool DirectoryExists(string path);
        void CreateDirectory(string path);
        long FileLength(string path);
        DateTime FileModifiedUtc(string path);
        DateTime FileCreatedUtc(string path);
        FileAttributes GetAttributes(string path);
        bool CanWrite(string path);              // exclusive-open probe
        void Move(string source, string dest);
        void Copy(string source, string dest, bool overwrite);
        void Delete(string path);
        bool Recycle(string path);               // false when no shell is available
        string Sha256(string path);
        List<string> Entries(string dir);        // files and directories, no recursion
        bool IsDirectory(string path);
    }

    public sealed class SystemFileSystem : IFileSystem
    {
        public bool FileExists(string path) { return File.Exists(path); }
        public bool DirectoryExists(string path) { return Directory.Exists(path); }

        public List<string> Entries(string dir)
        {
            try { return new List<string>(Directory.GetFileSystemEntries(dir)); }
            catch { return new List<string>(); }
        }

        public bool IsDirectory(string path)
        {
            try { return Directory.Exists(path); }
            catch { return false; }
        }

        public void CreateDirectory(string path)
        {
            if (!string.IsNullOrEmpty(path) && !Directory.Exists(path)) Directory.CreateDirectory(path);
        }

        public long FileLength(string path) { return new FileInfo(path).Length; }
        public DateTime FileModifiedUtc(string path) { return File.GetLastWriteTimeUtc(path); }
        public DateTime FileCreatedUtc(string path) { return File.GetCreationTimeUtc(path); }
        public FileAttributes GetAttributes(string path) { return File.GetAttributes(path); }

        public bool CanWrite(string path)
        {
            try
            {
                using (FileStream fs = new FileStream(path, FileMode.Open, FileAccess.Read,
                           FileShare.ReadWrite | FileShare.Delete))
                { return fs.Length >= 0; }
            }
            catch { return false; }
        }

        public void Move(string source, string dest)
        {
            File.Move(source, dest);          // same-volume rename; throws across volumes
        }

        public void Copy(string source, string dest, bool overwrite)
        {
            File.Copy(source, dest, overwrite);
        }

        public void Delete(string path) { File.Delete(path); }

        public bool Recycle(string path)
        {
            return FaShell.Recycle(path);
        }

        public string Sha256(string path)
        {
            using (SHA256 sha = SHA256.Create())
            using (FileStream fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                return BitConverter.ToString(sha.ComputeHash(fs)).Replace("-", "");
        }
    }

    // Test double: records every call, mutates nothing. Two of these make the
    // zero-mutation guarantees provable.
    public sealed class FakeFileSystem : IFileSystem
    {
        public readonly Dictionary<string, long> Files =
            new Dictionary<string, long>(StringComparer.OrdinalIgnoreCase);
        public readonly HashSet<string> Dirs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        public readonly List<string> Calls = new List<string>();
        public bool RecycleSucceeds = true;
        public DateTime DefaultModifiedUtc = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        public readonly HashSet<string> HiddenFiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        public Func<string, string> HashOf = delegate(string p) { return "hash-" + p; };

        public IEnumerable<string> Mutations
        {
            get
            {
                foreach (string c in Calls)
                    if (c.StartsWith("move", StringComparison.Ordinal) ||
                        c.StartsWith("copy", StringComparison.Ordinal) ||
                        c.StartsWith("delete", StringComparison.Ordinal) ||
                        c.StartsWith("mkdir", StringComparison.Ordinal) ||
                        c.StartsWith("recycle", StringComparison.Ordinal)) yield return c;
            }
        }

        public void AddFile(string path, long length) { Files[path] = length; }
        public void AddDir(string path) { Dirs.Add(path); }

        public List<string> Entries(string dir)
        {
            List<string> names = new List<string>();
            foreach (string d in Dirs)
                if (string.Equals(Path.GetDirectoryName(d.TrimEnd('\\')), dir, StringComparison.OrdinalIgnoreCase))
                    names.Add(d);
            foreach (string f in Files.Keys)
                if (string.Equals(Path.GetDirectoryName(f.TrimEnd('\\')), dir, StringComparison.OrdinalIgnoreCase))
                    names.Add(f);
            return names;
        }

        public bool IsDirectory(string path) { return Dirs.Contains(path.TrimEnd('\\')); }

        public bool FileExists(string path) { return Files.ContainsKey(path); }
        public bool DirectoryExists(string path) { return Dirs.Contains(path); }
        public void CreateDirectory(string path) { Calls.Add("mkdir " + path); Dirs.Add(path); }
        public long FileLength(string path) { return Files.ContainsKey(path) ? Files[path] : 0; }
        public DateTime FileModifiedUtc(string path) { return DefaultModifiedUtc; }
        public DateTime FileCreatedUtc(string path) { return new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc); }
        public FileAttributes GetAttributes(string path)
        {
            FileAttributes a = Files.ContainsKey(path) ? FileAttributes.Normal : FileAttributes.Directory;
            if (HiddenFiles.Contains(path)) a |= FileAttributes.Hidden;
            return a;
        }
        public bool CanWrite(string path) { return Files.ContainsKey(path); }

        public void Move(string source, string dest)
        {
            Calls.Add("move " + source + " -> " + dest);
            if (!Files.ContainsKey(source)) throw new FileNotFoundException(source);
            long len = Files[source];
            Files.Remove(source); Files[dest] = len;
        }

        public void Copy(string source, string dest, bool overwrite)
        {
            Calls.Add("copy " + source + " -> " + dest + (overwrite ? " overwrite" : ""));
            if (!Files.ContainsKey(source)) throw new FileNotFoundException(source);
            if (!overwrite && Files.ContainsKey(dest)) throw new IOException("exists");
            Files[dest] = Files[source];
        }

        public void Delete(string path) { Calls.Add("delete " + path); Files.Remove(path); }

        public bool Recycle(string path)
        {
            Calls.Add("recycle " + path);
            if (!RecycleSucceeds) return false;
            Files.Remove(path);
            return true;
        }

        public string Sha256(string path) { return HashOf(path); }
    }

    internal static class FaShell
    {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct SHFILEOPSTRUCT
        {
            public IntPtr hwnd;
            public uint wFunc;
            [MarshalAs(UnmanagedType.LPWStr)] public string pFrom;
            [MarshalAs(UnmanagedType.LPWStr)] public string pTo;
            public ushort fFlags;
            [MarshalAs(UnmanagedType.Bool)] public bool fAnyOperationsAborted;
            public IntPtr hNameMappings;
            [MarshalAs(UnmanagedType.LPWStr)] public string lpszProgressTitle;
        }

        [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern int SHFileOperation(ref SHFILEOPSTRUCT op);

        const uint FO_DELETE = 3;
        const ushort FOF_ALLOWUNDO = 0x0040;
        const ushort FOF_NOCONFIRMATION = 0x0010;
        const ushort FOF_SILENT = 0x0004;
        const ushort FOF_NOERRORUI = 0x0400;

        public static bool Recycle(string path)
        {
            try
            {
                SHFILEOPSTRUCT op = new SHFILEOPSTRUCT();
                op.wFunc = FO_DELETE;
                op.pFrom = path + "\0\0";
                op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI;
                int rc = SHFileOperation(ref op);
                return rc == 0 && !op.fAnyOperationsAborted;
            }
            catch { return false; }
        }
    }

    public enum FaStability
    {
        Stable = 0,
        NotYet = 1,
        Temporary = 2,
        Missing = 3,
        Locked = 4
    }

    // "Never move a file because an event said it was created." A file must
    // look identical for the whole quiet period and be openable.
    public sealed class FaStabilityGate
    {
        public Func<DateTime> Clock = delegate { return DateTime.UtcNow; };
        public Action<int> Sleep = delegate(int ms) { System.Threading.Thread.Sleep(ms); };
        readonly Dictionary<string, Sample> Seen = new Dictionary<string, Sample>(StringComparer.OrdinalIgnoreCase);
        readonly IFileSystem Fs;
        readonly object Gate = new object();

        struct Sample
        {
            public long Length;
            public DateTime Modified;
            public DateTime ChangedAt;
        }

        public FaStabilityGate(IFileSystem fs) { Fs = fs; }

        public void Forget(string path)
        {
            lock (Gate) Seen.Remove(path);
        }

        public FaStability Check(string path, int quietSeconds, IEnumerable<string> tempPatterns)
        {
            if (!Fs.FileExists(path)) { Forget(path); return FaStability.Missing; }
            if (DefaultTempPatterns.Matches(Path.GetFileName(path), tempPatterns)) return FaStability.Temporary;

            long len; DateTime mod;
            try { len = Fs.FileLength(path); mod = Fs.FileModifiedUtc(path); }
            catch { return FaStability.Locked; }

            DateTime now = Clock();
            lock (Gate)
            {
                Sample s;
                if (!Seen.TryGetValue(path, out s) || s.Length != len || s.Modified != mod)
                {
                    Seen[path] = new Sample { Length = len, Modified = mod, ChangedAt = now };
                    return FaStability.NotYet;
                }
                if ((now - s.ChangedAt).TotalSeconds < quietSeconds) return FaStability.NotYet;
            }
            // A writer holding the file exclusively is still working on it.
            if (!Fs.CanWrite(path)) return FaStability.Locked;
            return FaStability.Stable;
        }
    }

    public enum FaUndoKind
    {
        None = 0,
        MoveBack = 1,
        RenameBack = 2,
        DeleteCopy = 3,     // remove the copy we made
        NotReversible = 4   // recycled or permanently deleted
    }

    public sealed class FaOpResult
    {
        public bool Success;
        public bool Skipped;
        public string Message = "";
        public string FinalPath = "";
        public string DeletedPath = "";
        public long Bytes;
        public FaUndoKind Undo = FaUndoKind.None;
        public bool Reversible { get { return Undo == FaUndoKind.MoveBack || Undo == FaUndoKind.RenameBack || Undo == FaUndoKind.DeleteCopy; } }
    }

    public sealed class FaOpPlan
    {
        public string Source = "";
        public string DestinationDir = "";     // for move/copy
        public string NewName = "";            // for rename (file name, no directory)
        public FaAction Action = FaAction.Move;
        public FaCollision Collision = FaCollision.Counter;
        public FaVerify Verify = FaVerify.Size;
        public FaRetry Retry = new FaRetry();
        public bool CreateDir = true;
        public bool DryRun;
        public string QuarantineDir = "";
        public DateTime Now = DateTime.UtcNow;
        public long IncomingBytes;
        public DateTime IncomingModifiedUtc;
    }

    public static class FaActions
    {
        public static FaOpResult Execute(IFileSystem fs, FaOpPlan plan, FaLog log, string profile)
        {
            FaOpResult res = new FaOpResult();
            res.FinalPath = plan.Source;

            try
            {
                switch (plan.Action)
                {
                    case FaAction.Ignore:
                    case FaAction.Stop:
                        res.Success = true; res.Skipped = true;
                        res.Message = "no action";
                        return res;
                    case FaAction.Rename:
                        return DoRename(fs, plan, res, log, profile);
                    case FaAction.Move:
                    case FaAction.Quarantine:
                        return DoMove(fs, plan, res, log, profile);
                    case FaAction.Copy:
                        return DoCopy(fs, plan, res, log, profile);
                    case FaAction.Recycle:
                        return DoRecycle(fs, plan, res, log, profile);
                    case FaAction.Delete:
                        return DoDelete(fs, plan, res, log, profile);
                }
                res.Message = "unsupported action";
                return res;
            }
            catch (Exception ex)
            {
                res.Success = false;
                res.Message = ex.Message;
                if (log != null) log.Error(profile, "action failed for " + plan.Source + ": " + ex.Message);
                return res;
            }
        }

        static string DirFor(FaOpPlan plan)
        {
            if (plan.Action == FaAction.Quarantine)
                return string.IsNullOrEmpty(plan.QuarantineDir) ? FaPaths.QuarantinePath() : plan.QuarantineDir;
            return plan.DestinationDir;
        }

        static FaOpResult DoRename(IFileSystem fs, FaOpPlan plan, FaOpResult res, FaLog log, string profile)
        {
            string dir = Path.GetDirectoryName(plan.Source);
            string want = string.IsNullOrEmpty(plan.NewName) ? Path.GetFileName(plan.Source) : plan.NewName;
            string dest = FaPaths.Normalize(Path.Combine(dir, want));
            if (string.Equals(dest, plan.Source, StringComparison.OrdinalIgnoreCase))
            {
                res.Success = true; res.Skipped = true; res.Message = "name already correct";
                res.FinalPath = dest;
                return res;
            }
            string resolved = Resolve(fs, dest, plan, res);
            if (resolved == null) { res.Skipped = true; res.Message = "collision: skipped"; return res; }
            if (plan.DryRun)
            {
                res.Success = true; res.FinalPath = resolved;
                res.Undo = FaUndoKind.RenameBack; res.Message = "would rename";
                return res;
            }
            Retry(fs, plan, delegate { fs.Move(plan.Source, resolved); }, log, profile);
            res.Success = true; res.FinalPath = resolved;
            res.Undo = FaUndoKind.RenameBack;
            return res;
        }

        static FaOpResult DoMove(IFileSystem fs, FaOpPlan plan, FaOpResult res, FaLog log, string profile)
        {
            string dir = DirFor(plan);
            string dest = FaPaths.Normalize(Path.Combine(dir, Path.GetFileName(plan.Source)));
            string resolved = Resolve(fs, dest, plan, res);
            if (resolved == null) { res.Skipped = true; res.Message = "collision: skipped"; return res; }
            res.Bytes = plan.IncomingBytes;
            if (plan.DryRun)
            {
                res.Success = true; res.FinalPath = resolved;
                res.Undo = FaUndoKind.MoveBack; res.Message = "would move";
                return res;
            }
            if (plan.CreateDir) fs.CreateDirectory(Path.GetDirectoryName(resolved));

            if (FaPaths.SameVolume(plan.Source, resolved))
            {
                if (fs.FileExists(resolved))
                {
                    string bak = resolved + ".fa_bak_" + Guid.NewGuid().ToString("N");
                    Retry(fs, plan, delegate { fs.Move(resolved, bak); }, log, profile);
                    try
                    {
                        Retry(fs, plan, delegate { fs.Move(plan.Source, resolved); }, log, profile);
                        try { fs.Delete(bak); } catch { }
                    }
                    catch
                    {
                        try { fs.Move(bak, resolved); } catch { }
                        throw;
                    }
                }
                else
                {
                    Retry(fs, plan, delegate { fs.Move(plan.Source, resolved); }, log, profile);
                }
                res.Success = true; res.FinalPath = resolved;
                res.Undo = FaUndoKind.MoveBack;
                return res;
            }

            // Cross-volume: copy, verify, only then delete. Anything less and a
            // failure halfway through loses the file.
            CopyVerified(fs, plan, resolved, log, profile);
            Retry(fs, plan, delegate { fs.Delete(plan.Source); }, log, profile);
            res.Success = true; res.FinalPath = resolved;
            res.Undo = FaUndoKind.MoveBack;
            return res;
        }

        static void CopyVerified(IFileSystem fs, FaOpPlan plan, string dest, FaLog log, string profile)
        {
            if (fs.FileExists(dest))
            {
                string tmp = dest + ".fa_tmp_" + Guid.NewGuid().ToString("N");
                try
                {
                    Retry(fs, plan, delegate { fs.Copy(plan.Source, tmp, false); }, log, profile);
                    if (plan.Verify == FaVerify.Size && fs.FileLength(tmp) != plan.IncomingBytes)
                        throw new IOException("size mismatch after copy to " + tmp);
                    if (plan.Verify == FaVerify.Hash)
                    {
                        string a = fs.Sha256(plan.Source), b = fs.Sha256(tmp);
                        if (!string.Equals(a, b, StringComparison.OrdinalIgnoreCase))
                            throw new IOException("hash mismatch after copy to " + tmp);
                    }
                    string bak = dest + ".fa_bak_" + Guid.NewGuid().ToString("N");
                    Retry(fs, plan, delegate { fs.Move(dest, bak); }, log, profile);
                    try
                    {
                        Retry(fs, plan, delegate { fs.Move(tmp, dest); }, log, profile);
                        try { fs.Delete(bak); } catch { }
                    }
                    catch
                    {
                        try { fs.Move(bak, dest); } catch { }
                        throw;
                    }
                }
                finally
                {
                    if (fs.FileExists(tmp)) try { fs.Delete(tmp); } catch { }
                }
            }
            else
            {
                Retry(fs, plan, delegate { fs.Copy(plan.Source, dest, false); }, log, profile);
                if (plan.Verify == FaVerify.None) return;
                if (!fs.FileExists(dest))
                    throw new IOException("copy to " + dest + " did not produce a file");
                if (plan.Verify == FaVerify.Size && fs.FileLength(dest) != plan.IncomingBytes)
                    throw new IOException("size mismatch after copy to " + dest);
                if (plan.Verify == FaVerify.Hash)
                {
                    string a = fs.Sha256(plan.Source), b = fs.Sha256(dest);
                    if (!string.Equals(a, b, StringComparison.OrdinalIgnoreCase))
                        throw new IOException("hash mismatch after copy to " + dest);
                }
            }
            if (log != null) log.Info(profile, "verified cross-volume copy to " + dest);
        }

        static FaOpResult DoCopy(IFileSystem fs, FaOpPlan plan, FaOpResult res, FaLog log, string profile)
        {
            string dest = FaPaths.Normalize(Path.Combine(DirFor(plan), Path.GetFileName(plan.Source)));
            string resolved = Resolve(fs, dest, plan, res);
            if (resolved == null) { res.Skipped = true; res.Message = "collision: skipped"; return res; }
            res.Bytes = plan.IncomingBytes;
            if (plan.DryRun)
            {
                res.Success = true; res.FinalPath = resolved;
                res.Undo = FaUndoKind.DeleteCopy; res.Message = "would copy";
                return res;
            }
            if (plan.CreateDir) fs.CreateDirectory(Path.GetDirectoryName(resolved));
            if (fs.FileExists(resolved))
            {
                string tmp = resolved + ".fa_tmp_" + Guid.NewGuid().ToString("N");
                try
                {
                    Retry(fs, plan, delegate { fs.Copy(plan.Source, tmp, false); }, log, profile);
                    string bak = resolved + ".fa_bak_" + Guid.NewGuid().ToString("N");
                    Retry(fs, plan, delegate { fs.Move(resolved, bak); }, log, profile);
                    try
                    {
                        Retry(fs, plan, delegate { fs.Move(tmp, resolved); }, log, profile);
                        try { fs.Delete(bak); } catch { }
                    }
                    catch
                    {
                        try { fs.Move(bak, resolved); } catch { }
                        throw;
                    }
                }
                finally
                {
                    if (fs.FileExists(tmp)) try { fs.Delete(tmp); } catch { }
                }
            }
            else
            {
                Retry(fs, plan, delegate { fs.Copy(plan.Source, resolved, false); }, log, profile);
            }
            res.Success = true; res.FinalPath = resolved;
            res.Undo = FaUndoKind.DeleteCopy;
            return res;
        }

        static FaOpResult DoRecycle(IFileSystem fs, FaOpPlan plan, FaOpResult res, FaLog log, string profile)
        {
            if (plan.DryRun)
            {
                res.Success = true; res.Undo = FaUndoKind.NotReversible;
                res.Message = "would move to Recycle Bin";
                return res;
            }
            bool ok = false;
            Retry(fs, plan, delegate { ok = fs.Recycle(plan.Source); }, log, profile);
            if (!ok) throw new IOException("Recycle Bin is not available on this system");
            res.Success = true; res.Undo = FaUndoKind.NotReversible;
            res.Message = "moved to Recycle Bin (not programmatically reversible)";
            return res;
        }

        static FaOpResult DoDelete(IFileSystem fs, FaOpPlan plan, FaOpResult res, FaLog log, string profile)
        {
            if (plan.DryRun)
            {
                res.Success = true; res.Undo = FaUndoKind.NotReversible;
                res.Message = "would permanently delete";
                return res;
            }
            Retry(fs, plan, delegate { fs.Delete(plan.Source); }, log, profile);
            res.Success = true; res.Undo = FaUndoKind.NotReversible;
            res.Message = "permanently deleted";
            return res;
        }

        // Returns the path to use, or null when the policy is Skip.
        // 'Replace' is the only policy that may overwrite, and it is opt-in
        // per profile or per rule -- never a default.
        public static string Resolve(IFileSystem fs, string desired, FaOpPlan plan, FaOpResult res)
        {
            if (!fs.FileExists(desired)) return desired;

            switch (plan.Collision)
            {
                case FaCollision.Skip:
                    return null;
                case FaCollision.Replace:
                    res.DeletedPath = desired;
                    return desired;
                case FaCollision.Counter:
                    return Counter(fs, desired);
                case FaCollision.Timestamp:
                    return Timestamp(desired, plan.Now.ToLocalTime());
                case FaCollision.KeepNewest:
                case FaCollision.KeepOldest:
                case FaCollision.KeepLarger:
                case FaCollision.KeepSmaller:
                    return Keep(fs, desired, plan, res);
            }
            return null;
        }

        public static string Counter(IFileSystem fs, string desired)
        {
            string dir = Path.GetDirectoryName(desired);
            string name = Path.GetFileNameWithoutExtension(desired);
            string ext = Path.GetExtension(desired);
            for (int i = 1; i < 10000; i++)
            {
                string cand = Path.Combine(dir, name + " (" + i + ")" + ext);
                if (!fs.FileExists(cand)) return cand;
            }
            return Path.Combine(dir, name + " (" + Guid.NewGuid().ToString("N").Substring(0, 8) + ")" + ext);
        }

        public static string Timestamp(string desired, DateTime when)
        {
            string dir = Path.GetDirectoryName(desired);
            string name = Path.GetFileNameWithoutExtension(desired);
            string ext = Path.GetExtension(desired);
            return Path.Combine(dir, name + " " + when.ToString("yyyy-MM-dd HHmmss") + ext);
        }

        static string Keep(IFileSystem fs, string desired, FaOpPlan plan, FaOpResult res)
        {
            bool incomingWins;
            switch (plan.Collision)
            {
                case FaCollision.KeepNewest:
                    incomingWins = plan.IncomingModifiedUtc >= fs.FileModifiedUtc(desired);
                    break;
                case FaCollision.KeepOldest:
                    incomingWins = plan.IncomingModifiedUtc <= fs.FileModifiedUtc(desired);
                    break;
                case FaCollision.KeepLarger:
                    incomingWins = plan.IncomingBytes >= fs.FileLength(desired);
                    break;
                default:
                    incomingWins = plan.IncomingBytes <= fs.FileLength(desired);
                    break;
            }
            if (!incomingWins)
            {
                res.Skipped = true;
                res.Message = "collision: kept the existing file";
                res.DeletedPath = "";
                return null;
            }
            // The existing file is about to be replaced; the journal records it
            // so history shows what was displaced.
            res.DeletedPath = desired;
            return desired;
        }

        // Bounded retries with backoff. A locked file must not become an
        // endless error loop, and must never be left half-deleted.
        public static void Retry(IFileSystem fs, FaOpPlan plan, Action body, FaLog log, string profile)
        {
            int attempts = plan.Retry == null || plan.Retry.Attempts < 1 ? 1 : plan.Retry.Attempts;
            int backoff = plan.Retry == null ? 0 : Math.Max(0, plan.Retry.BackoffMs);
            Exception last = null;
            for (int i = 0; i < attempts; i++)
            {
                try { body(); return; }
                catch (IOException ex) { last = ex; }
                catch (UnauthorizedAccessException ex) { last = ex; }
                if (i + 1 < attempts && backoff > 0) System.Threading.Thread.Sleep(backoff * (i + 1));
            }
            throw last ?? new IOException("operation failed");
        }
    }
}