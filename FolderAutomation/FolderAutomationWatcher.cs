using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;

// Folder Automation — event-driven watching with a reconciliation fallback.
//
// FileSystemWatcher is not a reliable delivery mechanism: it drops events when
// its buffer overflows, it misses things across sleep/resume, and it says
// nothing about files that existed before it started. Every one of those paths
// ends in a full rescan, so a dropped event costs latency, never correctness.

namespace Saituls.FolderAutomation
{
    // PERF-004: what one reconcile step did. The clause asks for scanned,
    // admitted, deferred and rejected/error to be counted separately, because
    // one number ("queued 12000 files") cannot tell an operator whether the
    // source was bigger than memory or whether admission simply refused.
    public sealed class FaReconcileReport
    {
        public string Why = "";
        public int Scanned;        // files the walk yielded
        public int Admitted;       // newly queued
        public int Deferred;       // still to reconcile: already queued, or over this step's budget
        public int Rejected;       // the queue is at capacity
        public bool Suspended;     // traversal stopped with tree left; the cursor holds its place
        public bool HitReparsePoint;

        public string Line()
        {
            return "scanned " + Scanned + ", admitted " + Admitted + ", deferred " + Deferred
                + ", rejected " + Rejected + (Suspended ? " (suspended, continues on the next step)" : "");
        }
    }

    public sealed class FaSourceScanner
    {
        readonly FaProfile Profile;
        readonly IFileSystem Fs;
        public bool LastRunHitReparsePoint;
        // Injection seam so the reparse-point rule is testable without creating
        // a privileged junction on the build machine.
        public Func<string, bool> IsReparse = FaPaths.IsReparsePoint;

        // PERF-004: traversal state lives on the instance. An interrupted walk
        // resumes where it stopped instead of restarting from the root, which
        // would only re-admit what the budget just refused and never converge
        // on a source larger than the admission budget.
        readonly Stack<string> PendingDirs = new Stack<string>();
        // The directory being walked and how far into it we got. Without this a
        // suspension would lose the rest of the current directory, which is
        // exactly the leak this clause is about.
        List<string> _names;
        int _nameIndex;
        bool _started;
        int _guard;

        // The walk guard, across every resume of this scan.
        public const int MaxDirsPerScan = 200000;

        public FaSourceScanner(FaProfile profile, IFileSystem fs) { Profile = profile; Fs = fs; }

        public bool Done { get { return _started && _names == null && PendingDirs.Count == 0; } }

        // Streams each discovered file to `admit`, which returns false when it
        // has no capacity left. Traversal then suspends and Done stays false, so
        // the caller can resume it once the queue has drained.
        public void Scan(Func<string, bool> admit)
        {
            if (admit == null) return;
            if (!_started)
            {
                _started = true;
                LastRunHitReparsePoint = false;
                List<string> sources = Profile.Sources ?? new List<string>();
                // Pushed in reverse so the first source is walked first.
                for (int i = sources.Count - 1; i >= 0; i--)
                {
                    string root = FaPaths.Normalize(sources[i]);
                    if (Fs.DirectoryExists(root)) PendingDirs.Push(root);
                }
            }
            while (true)
            {
                if (_names != null && _nameIndex < _names.Count)
                {
                    // Peek, do not advance: a refusal must leave the index on the
                    // entry that was refused, or a resume silently skips it.
                    string n = _names[_nameIndex];
                    bool isDir;
                    try { isDir = Fs.IsDirectory(n); }
                    catch { _nameIndex++; continue; }
                    if (isDir)
                    {
                        if (Profile.Recursive)
                        {
                            if (!Profile.FollowReparse && IsReparse(n)) LastRunHitReparsePoint = true;
                            else PendingDirs.Push(n);
                        }
                    }
                    else if (!admit(FaPaths.Normalize(n))) return;   // out of capacity: keep our place
                    _nameIndex++;
                    continue;
                }
                _names = null;
                _nameIndex = 0;
                if (PendingDirs.Count == 0) return;
                if (_guard++ >= MaxDirsPerScan) { PendingDirs.Clear(); return; }
                string dir = PendingDirs.Pop();
                if (!Profile.FollowReparse && IsReparse(dir))
                {
                    LastRunHitReparsePoint = true;
                    continue;
                }
                // PERF-004: Entries() is already a list. Walk it by index, never
                // a second ToArray() copy of every directory.
                try { _names = Fs.Entries(dir); }
                catch { continue; }
            }
        }

        // A complete walk into a list. The dry run and the RunProfile fallback
        // want the whole set in hand; the reconcile path streams instead.
        public List<string> Enumerate()
        {
            List<string> files = new List<string>();
            Scan(delegate(string f) { files.Add(f); return true; });
            return files;
        }
    }

    public sealed class FaWatcher : IDisposable
    {
        readonly FaProfile Profile;
        readonly Action<string> OnCandidate;
        readonly Action OnOverflow;
        readonly FaLog Log;
        readonly List<FileSystemWatcher> Watches = new List<FileSystemWatcher>();
        readonly Dictionary<string, DateTime> Pending = new Dictionary<string, DateTime>(StringComparer.OrdinalIgnoreCase);
        readonly object Gate = new object();
        Timer FlushTimer;
        bool _disposed;
        int _overflows;

        public const int DebounceMs = 750;
        public const int MaxPending = 20000;

        public FaWatcher(FaProfile profile, Action<string> onCandidate, Action onOverflow, FaLog log)
        {
            Profile = profile; OnCandidate = onCandidate; OnOverflow = onOverflow; Log = log;
        }

        public int ActiveWatchCount { get { lock (Gate) return Watches.Count; } }
        public int OverflowCount { get { return _overflows; } }

        public bool SourceAvailable
        {
            get
            {
                foreach (string s in Profile.Sources ?? new List<string>())
                    if (!Directory.Exists(FaPaths.Normalize(s))) return false;
                return (Profile.Sources != null && Profile.Sources.Count > 0);
            }
        }

        public void Start()
        {
            lock (Gate)
            {
                if (_disposed) return;
                foreach (string raw in Profile.Sources ?? new List<string>())
                {
                    string dir = FaPaths.Normalize(raw);
                    if (!Directory.Exists(dir)) continue;      // source unavailable: skip, resume later
                    try
                    {
                        FileSystemWatcher w = new FileSystemWatcher(dir);
                        w.NotifyFilter = Profile.WatchLastWrite
                            ? (NotifyFilters.FileName | NotifyFilters.DirectoryName | NotifyFilters.LastWrite)
                            : (NotifyFilters.FileName | NotifyFilters.DirectoryName);
                        w.IncludeSubdirectories = Profile.Recursive;
                        w.InternalBufferSize = 64 * 1024;       // 8 KB default overflows constantly
                        w.Created += OnCreated;
                        w.Renamed += OnRenamed;
                        if (Profile.WatchLastWrite) w.Changed += OnChanged;
                        w.Deleted += OnDeleted;
                        w.Error += OnError;
                        w.EnableRaisingEvents = true;
                        Watches.Add(w);
                    }
                    catch (Exception ex)
                    {
                        if (Log != null) Log.Warn(Profile.Name, "watcher for " + dir + " unavailable: " + ex.Message);
                    }
                }
                if (FlushTimer == null)
                {
                    FlushTimer = new Timer(Flush, null, DebounceMs, DebounceMs);
                }
            }
        }

        // Re-arm after a source reappears; called by the engine's availability
        // poll, not by the user.
        public bool RestartIfNeeded()
        {
            lock (Gate)
            {
                if (_disposed) return false;
                if (Watches.Count > 0 && SourceAvailable) return false;
                StopLocked();
                Start();
                return Watches.Count > 0;
            }
        }

        void OnCreated(object sender, FileSystemEventArgs e) { Enqueue(e.FullPath); }
        void OnChanged(object sender, FileSystemEventArgs e) { Enqueue(e.FullPath); }
        void OnDeleted(object sender, FileSystemEventArgs e)
        {
            lock (Gate) Pending.Remove(e.FullPath);
        }

        void OnRenamed(object sender, RenamedEventArgs e)
        {
            // A rename is a delete plus a create. Only the new name can match a
            // rule; the old one is gone.
            lock (Gate) Pending.Remove(e.OldFullPath);
            Enqueue(e.FullPath);
        }

        void Enqueue(string path)
        {
            if (string.IsNullOrEmpty(path)) return;
            try
            {
                if (Directory.Exists(path)) return;
            }
            catch { }
            bool overflow = false;
            lock (Gate)
            {
                if (_disposed) return;
                if (Pending.Count >= MaxPending)
                {
                    Pending.Clear();
                    overflow = true;
                }
                else
                {
                    Pending[path] = DateTime.UtcNow;
                }
            }
            if (overflow)
            {
                RaiseOverflow("pending event saturation");
            }
        }

        void Flush(object state)
        {
            List<string> batch;
            lock (Gate)
            {
                if (_disposed || Pending.Count == 0) return;
                batch = new List<string>(Pending.Keys);
                Pending.Clear();
            }
            foreach (string p in batch)
            {
                try { if (OnCandidate != null) OnCandidate(p); }
                catch (Exception ex)
                {
                    if (Log != null) Log.Warn(Profile.Name, "candidate handler failed for " + p + ": " + ex.Message);
                }
            }
        }

        void OnError(object sender, ErrorEventArgs e)
        {
            RaiseOverflow(e.GetException() == null ? "unknown" : e.GetException().Message);
        }

        // Buffer overflow and handle loss are the same event from the engine's
        // point of view: this watcher can no longer be trusted, so rescan.
        public void RaiseOverflow(string why)
        {
            _overflows++;
            if (Log != null) Log.Warn(Profile.Name, "watcher error, reconciling: " + why);
            try { if (OnOverflow != null) OnOverflow(); }
            catch { }
        }

        void StopLocked()
        {
            foreach (FileSystemWatcher w in Watches)
            {
                try { w.EnableRaisingEvents = false; } catch { }
                try { w.Dispose(); } catch { }
            }
            Watches.Clear();
        }

        public void Stop()
        {
            lock (Gate)
            {
                StopLocked();
                if (FlushTimer != null) { try { FlushTimer.Dispose(); } catch { } FlushTimer = null; }
            }
        }

        public void Dispose()
        {
            Stop();
            _disposed = true;
        }
    }
}