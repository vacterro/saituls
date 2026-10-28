using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using System.IO;
using System.Threading;

// Folder Automation — the engine. Owns profiles, schedules, watchers, the
// safety brake and the journal. No WinForms types appear in this file.

namespace Saituls.FolderAutomation
{
    public enum FaNoticeLevel { Info = 0, Warning = 1, Error = 2 }

    // W2-007: an engine lifecycle state a caller can read. Nothing in this
    // enum is optional bookkeeping -- Stop() refuses to publish STOPPED unless
    // every admitted unit of work has actually finished, and STOPPING is the
    // state that tells every admission path to refuse new work.
    public enum FaEngineState { Stopped = 0, Running = 1, Stopping = 2 }

    public sealed class FaNotice
    {
        public FaNoticeLevel Level;
        public string Title = "";
        public string Message = "";
        public string Profile = "";
    }

    public sealed class FaPlanItem
    {
        public string Source = "";
        public string Rule = "";
        public string Action = "";
        public string Destination = "";
        public string Outcome = "";
        public string Message = "";
        public string Warning = "";
        public long Bytes;

        public string Line()
        {
            StringBuilder sb = new StringBuilder();
            sb.Append(Source).Append("  ->  ").Append(Action);
            if (!string.IsNullOrEmpty(Destination)) sb.Append(" ").Append(Destination);
            if (!string.IsNullOrEmpty(Rule)) sb.Append("  [").Append(Rule).Append("]");
            if (!string.IsNullOrEmpty(Warning)) sb.Append("  ! ").Append(Warning);
            if (!string.IsNullOrEmpty(Message)) sb.Append("  (").Append(Message).Append(")");
            return sb.ToString();
        }
    }

    public sealed class FaRunReport
    {
        public string ProfileId = "";
        public string ProfileName = "";
        public bool DryRun;
        public string Trigger = "manual";
        public string StartedUtc = "";
        public string FinishedUtc = "";
        public int Matched;
        public int Actions;
        public int Skipped;
        public int Errors;
        public int Warnings;
        public long Bytes;
        public string PausedReason = "";
        public string Status = "";
        public readonly List<FaPlanItem> Items = new List<FaPlanItem>();

        public string Summary()
        {
            StringBuilder sb = new StringBuilder();
            sb.Append(DryRun ? "DRY RUN " : "").Append(ProfileName).Append(": ")
              .Append(Actions).Append(" action(s), ").Append(Skipped).Append(" skipped, ")
              .Append(Errors).Append(" error(s)");
            if (Bytes > 0) sb.Append(", ").Append(Bytes).Append(" bytes");
            if (!string.IsNullOrEmpty(PausedReason)) sb.Append("; paused: ").Append(PausedReason);
            return sb.ToString();
        }
    }

    // PERF-003: one record per pending candidate.
    //
    // The candidate used to be a bare string in a HashSet, with the retry count
    // in a second dictionary and one runtime-wide FirstSeen timestamp shared by
    // every file. Both halves were wrong: the state that decides a file's fate
    // was not attached to the file, and a "temporary" candidate expired based
    // on when the profile happened to start, not on when it appeared.
    public sealed class FaCandidate
    {
        public string Path = "";                 // normalized
        public DateTime FirstSeenUtc;            // per candidate, not per profile
        public FaStability Sample = FaStability.NotYet;
        public DateTime SampledUtc;              // how fresh Sample is
        public int Attempts;                     // locked-file retries
        public DateTime NextEligibleUtc;          // when this candidate may be checked again
        public string LastFailure = "";           // "locked" | "temporary" | "notyet" | reason
        // Bucket the candidate currently sits in, so a reschedule retires the
        // stale queue entry by identity instead of searching for it.
        public long BucketKey = long.MaxValue;
    }

    // PERF-003: the pending set, indexed by what is checkable now.
    //
    // Items is the authority ("this path is still a candidate"); _due is only an
    // index answering "what is due" in O(log n). A tick therefore takes the
    // candidates that are actually due, oldest first, instead of cloning all
    // 50 000 of them and stat-ing every one.
    public sealed class FaCandidateQueue
    {
        readonly Dictionary<string, FaCandidate> Items = new Dictionary<string, FaCandidate>(StringComparer.OrdinalIgnoreCase);
        readonly SortedDictionary<long, List<FaCandidate>> _due = new SortedDictionary<long, List<FaCandidate>>();
        readonly object Sync = new object();

        public int Count { get { lock (Sync) return Items.Count; } }

        // The record for a queued path, or null. Tests and a status reader use
        // this instead of reaching into the index.
        public FaCandidate Find(string path)
        {
            lock (Sync)
            {
                FaCandidate c;
                return Items.TryGetValue(FaPaths.Normalize(path), out c) ? c : null;
            }
        }

        static long Key(DateTime utc)
        {
            return utc.ToUniversalTime().Ticks / TimeSpan.TicksPerMillisecond;
        }

        // Adds a path, or returns null when it is already queued (duplicate
        // event coalescing, as before).
        public FaCandidate Add(string path, DateTime now)
        {
            path = FaPaths.Normalize(path);
            if (string.IsNullOrEmpty(path)) return null;
            lock (Sync)
            {
                if (Items.ContainsKey(path)) return null;
                if (Items.Count >= MaxPending) return null;
                FaCandidate c = new FaCandidate { Path = path, FirstSeenUtc = now, SampledUtc = now, NextEligibleUtc = now };
                Items[path] = c;
                Schedule(c);
                return c;
            }
        }

        public const int MaxPending = 50000;

        void Schedule(FaCandidate c)
        {
            long k = Key(c.NextEligibleUtc);
            List<FaCandidate> bucket;
            if (!_due.TryGetValue(k, out bucket)) { bucket = new List<FaCandidate>(); _due[k] = bucket; }
            c.BucketKey = k;
            bucket.Add(c);
        }

        public void Reschedule(FaCandidate c)
        {
            lock (Sync) Schedule(c);
        }

        // Retires a candidate outright. The bucket entry it leaves behind is
        // dropped by TryTakeDue, which compares the bucket key it was taken
        // from against the candidate's current one.
        public void Remove(FaCandidate c)
        {
            if (c == null) return;
            lock (Sync) Items.Remove(c.Path);
            c.BucketKey = long.MaxValue;
        }

        // Takes at most `max` candidates whose next check is due, oldest first.
        public List<FaCandidate> TryTakeDue(DateTime now, int max)
        {
            List<FaCandidate> taken = null;
            List<long> spent = null;
            lock (Sync)
            {
                long limit = Key(now);
                foreach (KeyValuePair<long, List<FaCandidate>> kv in _due)
                {
                    if (kv.Key > limit) break;
                    foreach (FaCandidate c in kv.Value)
                    {
                        if (c.BucketKey != kv.Key) continue;          // rescheduled
                        if (!Items.ContainsKey(c.Path)) continue;     // removed
                        if (taken == null) taken = new List<FaCandidate>();
                        taken.Add(c);
                    }
                    if (spent == null) spent = new List<long>();
                    spent.Add(kv.Key);                                 // due buckets are consumed even when empty
                    if (taken != null && taken.Count >= max) break;
                }
                if (spent != null) foreach (long k in spent) _due.Remove(k);
            }
            return taken;
        }

        public void Clear()
        {
            lock (Sync) { Items.Clear(); _due.Clear(); }
        }

        // Pops candidates that are due now AND whose stored stability sample
        // proves they are stable and fresh enough. Used by RunProfile, which
        // must not start copying a file the tick has not vouched for, and must
        // not clone the queue either. Everything not vouched goes back.
        public List<string> SnapshotStable(DateTime now, int quietSeconds)
        {
            List<FaCandidate> due = TryTakeDue(now, int.MaxValue);
            if (due == null || due.Count == 0) return new List<string>();
            List<string> paths = new List<string>();
            List<FaCandidate> keep = null;
            lock (Sync)
            {
                foreach (FaCandidate c in due)
                {
                    if (c.Sample == FaStability.Stable && (now - c.SampledUtc).TotalSeconds <= quietSeconds)
                    {
                        Items.Remove(c.Path);
                        paths.Add(c.Path);
                    }
                    else
                    {
                        if (keep == null) keep = new List<FaCandidate>();
                        keep.Add(c);
                    }
                }
                if (keep != null) foreach (FaCandidate c in keep) Schedule(c);
            }
            return paths;
        }
    }

    public sealed class FaProfileRuntime
    {
        public FaProfile Profile;
        public FaWatcher Watcher;
        public FaStabilityGate Gate;
        public FaRunGate RunGate = new FaRunGate("");
        string _status = "Disabled";
        string _detail = "";
        // Mirrored onto the profile itself so every reader -- the Organizer
        // tab, the CLI, a dialog -- sees the same truth instead of one path
        // reading a live field and the other a stale default.
        public string Status
        {
            get { return _status; }
            set { _status = value; if (Profile != null) Profile.Status = value; }
        }
        public string Detail
        {
            get { return _detail; }
            set { _detail = value; if (Profile != null) Profile.StatusDetail = value; }
        }
        public DateTime? NextRun;
        public DateTime? LastRun;
        public DateTime? LastReconcile;
        public string PausedReason = "";
        public bool SafetyPaused;
        public int ConsecutiveErrors;
        // PERF-003: the candidate set, and the explicit state a candidate moves
        // into when it stops being retryable. Deferred is bounded: it exists so
        // an exhausted candidate is reported and remembered, not re-polled.
        public readonly FaCandidateQueue Queue = new FaCandidateQueue();
        public readonly List<FaCandidate> Deferred = new List<FaCandidate>();
        public const int MaxDeferred = 2048;
        // PERF-004: an interrupted reconcile walk, kept so the remaining tree is
        // reached on a later tick instead of being dropped.
        public FaSourceScanner ReconcileCursor;

        public void Defer(FaCandidate c, string reason)
        {
            Queue.Remove(c);
            c.LastFailure = reason;
            Deferred.Add(c);
            while (Deferred.Count > MaxDeferred) Deferred.RemoveAt(0);
        }
    }

    public sealed class FaEngine : IDisposable
    {
        public FaConfig Config = new FaConfig();
        public readonly FaJournal Journal;
        public readonly FaLog Log;
        public IFileSystem Fs;
        public Func<DateTime> Clock = delegate { return DateTime.UtcNow; };
        public event Action<FaNotice> Notice;

        readonly Dictionary<string, FaProfileRuntime> Runtimes = new Dictionary<string, FaProfileRuntime>(StringComparer.OrdinalIgnoreCase);
        readonly object Gate = new object();
        readonly FaRunGate GlobalGate = new FaRunGate("engine");
        Timer TickTimer;
        bool _disposed;
        bool _started;

        // W2-007: explicit lifecycle state and a real quiescence barrier.
        //
        // The old Stop() set a bool, disposed the timer and polled ONE run-gate
        // boolean for at most ten seconds, then reported "stopped" whatever was
        // still happening. A destructively large operation could therefore
        // still be mid-copy while the process said it had stopped, and a timer
        // callback already queued in the thread pool could start new work after
        // the flag was cleared. Both are the same defect: "stopped" was a
        // claim, not an observation.
        //
        // This is the state machine the rest of the class now obeys:
        //   Stopped -> Running (Start), Running -> Stopping -> Stopped (Stop).
        // STOPPING is published before any producer is disposed, every
        // admission path checks it, and each admitted unit of work registers
        // itself in _inflight so Stop() can actually wait for it.
        int _state = (int)FaEngineState.Stopped;
        int _inflight;
        // Set once work was ever admitted. A Stop() that arrives before anything
        // started is a no-op; a Stop() that arrives after work existed has to
        // tear everything down and wait, because that engine has producers.
        volatile bool _startedCompletedOnce;

        public string ConfigError = "";
        public bool ConfigWasBad;
        public bool PausedAll;

        public FaEngine(IFileSystem fs, FaLog log, FaJournal journal)
        {
            Fs = fs ?? new SystemFileSystem();
            Log = log ?? new FaLog(FaPaths.LogPath());
            Journal = journal ?? new FaJournal(FaPaths.JournalDir());
        }

        public void LoadConfig()
        {
            FaStore.LoadResult r = FaStore.Load();
            Config = r.Config;
            ConfigError = r.Error;
            ConfigWasBad = r.PreservedBadCopy;
            if (!string.IsNullOrEmpty(r.Error))
            {
                Log.Error("(config)", r.Error + (r.PreservedBadCopy ? " (kept a copy at " + FaPaths.BadConfigPath() + ")" : ""));
                Raise(FaNoticeLevel.Error, "Folder Automation", r.Error);
            }
            FaStore.Normalize(Config);
            FaValidation v = FaConfigValidator.Validate(Config);
            foreach (string e in v.Errors)
            {
                Log.Error("(config)", e);
                Raise(FaNoticeLevel.Warning, "Folder Automation", e);
            }
        }

        public void SaveConfig()
        {
            FaStore.Normalize(Config);
            FaStore.Save(Config);
        }

        // ---------------------------------------------------------------- life

        public FaEngineState State { get { return (FaEngineState)Volatile.Read(ref _state); } }

        // True only when nothing is admitted and nothing is running. A caller
        // that needs to know the engine really stopped asks this; the state
        // alone cannot promise that a hostile operation has not been admitted
        // in the moment before STOPPING was published.
        public bool Quiescent { get { return Volatile.Read(ref _inflight) == 0; } }

        public int InFlight { get { return Volatile.Read(ref _inflight); } }

        // Enter an admitted unit of work. Returns false when the engine is not
        // RUNNING -- that is the "producers must reject new work once STOPPING
        // begins" rule, implemented in one place instead of at each call site.
        // The double check closes the race with a Stop() that lands between the
        // state read and the increment.
        bool BeginWork()
        {
            _startedCompletedOnce = true;
            if (State != FaEngineState.Running) return false;
            Interlocked.Increment(ref _inflight);
            if (State != FaEngineState.Running)
            {
                Interlocked.Decrement(ref _inflight);
                return false;
            }
            return true;
        }

        void EndWork() { Interlocked.Decrement(ref _inflight); }

        public void Start()
        {
            lock (Gate)
            {
                if (_started) return;
                if (State == FaEngineState.Running) return;
                Interlocked.Exchange(ref _state, (int)FaEngineState.Running);
                _started = true;
                Prepare();
                TickTimer = new Timer(OnTick, null, 1000, 1000);
                Log.Info("(engine)", "started with " + Runtimes.Count + " profile(s)");
            }
        }

        // Everything Start() does except the timer, so a caller (and a test)
        // can drive one tick at a time. It also publishes RUNNING: a prepared
        // engine accepts work, whether that work is driven by the timer or by a
        // caller's own loop, and Stop() has to see the same lifecycle either way.
        public void Prepare()
        {
            Interlocked.Exchange(ref _state, (int)FaEngineState.Running);
            BuildRuntimes();
            RecoverJournal();
            foreach (FaProfileRuntime r in Runtimes.Values) StartProfile(r);
        }

        public void Dispose()
        {
            lock (Gate)
            {
                if (_disposed) return;
                _disposed = true;
            }
            Stop();
        }

        // Returns true when the engine observed quiescence. A caller on the
        // application-exit path can refuse to say "stopped" when it is false;
        // a caller that only cares that the tray is gone can ignore it.
        public bool Stop()
        {
            // 1. Publish STOPPING atomically, BEFORE any producer is disposed.
            //    Every admission path reads this, so a watcher callback or a
            //    queued timer callback that has not started yet refuses work
            //    instead of racing the teardown.
            bool wasRunning = Interlocked.CompareExchange(
                ref _state, (int)FaEngineState.Stopping, (int)FaEngineState.Running) == (int)FaEngineState.Running;

            Timer t;
            lock (Gate) { t = TickTimer; TickTimer = null; _started = false; }

            if (!wasRunning && !_startedCompletedOnce)
            {
                // Nothing was ever admitted (never started, or already stopped).
                // The producers are still released below so a Stop() after a
                // partial Start() leaves nothing behind.
                if (t != null) { try { t.Dispose(); } catch { } }
                return Quiescent;
            }

            // 2. The timer first. Timer.Dispose does NOT wait for a callback
            //    already queued in the thread pool, which is why the wait below
            //    tracks entries and exits rather than trusting the disposal.
            if (t != null) { try { t.Dispose(); } catch { } }

            List<FaProfileRuntime> all;
            lock (Gate) { all = new List<FaProfileRuntime>(Runtimes.Values); }

            // 3. Stop producing: watchers enqueue nothing after this.
            foreach (FaProfileRuntime r in all)
            {
                if (r.Watcher != null) { try { r.Watcher.Dispose(); } catch { } r.Watcher = null; }
            }

            // 4. Wait for every admitted unit -- timer callback, watcher-driven
            //    drain, manual or scheduled run -- not one boolean. The bound is
            //    a report, never a silent success: an operation that outruns it
            //    is stuck, and the state stays STOPPING so the caller can see
            //    that. The wait is also not 10 seconds: a real copy of a large
            //    file is a legitimate reason for a longer stop.
            int waitedMs = 0;
            const int stepMs = 50;
            const int limitMs = 120000;
            while (!Quiescent && waitedMs < limitMs)
            {
                System.Threading.Thread.Sleep(stepMs);
                waitedMs += stepMs;
            }

            bool quiescent = Quiescent;
            Interlocked.Exchange(ref _state, quiescent ? (int)FaEngineState.Stopped : (int)FaEngineState.Stopping);
            if (quiescent)
            {
                Log.Info("(engine)", "stopped; no admitted work remained");
            }
            else
            {
                Log.Error("(engine)", "stop incomplete: " + InFlight + " operation(s) were still admitted after " + (limitMs / 1000) + "s; state left STOPPING");
                Raise(FaNoticeLevel.Error, "Folder Automation",
                    "A folder operation was still running when SAITULS closed. It has been left to finish on its own; the engine was not marked stopped.");
            }
            return quiescent;
        }

        void BuildRuntimes()
        {
            lock (Gate)
            {
                Runtimes.Clear();
                foreach (FaProfile p in Config.Profiles)
                {
                    if (p == null) continue;
                    FaProfileRuntime r = new FaProfileRuntime();
                    r.Profile = p;
                    r.Gate = new FaStabilityGate(Fs);
                    r.RunGate = new FaRunGate(p.Name);
                    DateTime lr;
                    if (DateTime.TryParse(p.Schedule.LastRun, CultureInfo.InvariantCulture,
                            DateTimeStyles.None, out lr)) r.LastRun = lr;
                    Runtimes[p.Id] = r;
                }
            }
        }

        // The same recovery path Prepare() takes, exposed for a caller that has
        // already decided it is mutating (the CLI run path). A dry run must not
        // recover the journal: previewing an operation never decides history.
        public void RecoverJournalForCli() { RecoverJournal(); }

        // A record still pending means the process died mid-operation. It is
        // marked ambiguous and never replayed.
        //
        // W2-006: this is a mutation-authority decision, not a read. Marking a
        // record ambiguous changes what every later undo and every later
        // planner is allowed to believe about the operation history, so it runs
        // under the same cross-process gate as the operations themselves --
        // a CLI run that recovered the journal while a tray run was half way
        // through an operation would decide the history for it.
        void RecoverJournal()
        {
            List<FaJournalEntry> ambiguous = Journal.Ambiguous();
            if (ambiguous.Count == 0) return;
            using (FaMutationGate mutAuthority = FaMutationGate.TryAcquire())
            {
                if (mutAuthority.WasBlocked)
                {
                    Log.Warn("(engine)", "journal recovery skipped: another process holds the mutation authority");
                    return;
                }
                foreach (FaJournalEntry e in ambiguous)
                {
                    Journal.MarkAmbiguous(e);
                    Log.Warn(e.Profile, "operation " + e.OpId + " was in flight when SAITULS stopped; left untouched");
                    Raise(FaNoticeLevel.Warning, "Folder Automation",
                        "An interrupted operation on '" + e.Profile + "' was left untouched: " +
                        Path.GetFileName(e.Source));
                }
            }
        }

        void StartProfile(FaProfileRuntime r)
        {
            r.SafetyPaused = false;
            r.PausedReason = "";
            if (!Config.EngineEnabled) { r.Status = "Disabled"; r.Detail = "engine is off"; return; }
            if (!r.Profile.Enabled) { r.Status = "Disabled"; r.Detail = ""; return; }
            if (BlockingIssue(r.Profile) != null) { r.Status = "Configuration error"; r.Detail = "unsafe source/destination"; return; }

            if (NeedsWatch(r.Profile))
            {
                r.Watcher = new FaWatcher(r.Profile,
                    delegate(string path) { QueueCandidate(r, path); },
                    delegate { Reconcile(r, "watcher overflow"); },
                    Log);
                r.Watcher.Start();
                if (!r.Watcher.SourceAvailable)
                {
                    r.Status = "Source unavailable";
                    r.Detail = "";
                }
                else
                {
                    r.Status = r.Profile.Schedule.Trigger == FaTrigger.Realtime || NeedsWatch(r.Profile)
                        ? "Watching" : "Scheduled";
                    r.Detail = "";
                }
                if (r.Profile.ReconcileMinutes > 0) Reconcile(r, "startup reconcile");
            }
            else
            {
                r.Status = "Scheduled";
                r.NextRun = FaScheduleCalculator.NextSlot(r.Profile, Clock(), r.LastRun);
            }
        }

        public static bool NeedsWatch(FaProfile p)
        {
            FaTrigger t = p.Schedule == null ? FaTrigger.Manual : p.Schedule.Trigger;
            return t == FaTrigger.Realtime || t == FaTrigger.Startup || t == FaTrigger.PathAvailable;
        }

        public static FaTopologyIssue BlockingIssue(FaProfile p)
        {
            foreach (FaTopologyIssue i in FaTopology.Inspect(p)) if (i.Blocking) return i;
            return null;
        }

        // ---------------------------------------------------------------- tick

        void OnTick(object state)
        {
            // W2-007: admission, not optimism. A callback that arrives while the
            // engine is STOPPING (or already STOPPED) does nothing at all --
            // this is what a Timer.Dispose cannot promise, because the callback
            // may already be queued in the thread pool.
            if (_disposed) return;
            if (!BeginWork()) return;
            try { Tick(); }
            catch (Exception ex) { Log.Error("(engine)", "tick failed: " + ex.Message); }
            finally { EndWork(); }
        }

        public void Tick()
        {
            DateTime now = Clock();
            List<FaProfileRuntime> all;
            lock (Gate) all = new List<FaProfileRuntime>(Runtimes.Values);

            foreach (FaProfileRuntime r in all)
            {
                if (!r.Profile.Enabled || !Config.EngineEnabled) continue;
                if (PausedAll) { r.Status = "Paused"; continue; }
                if (r.SafetyPaused) { r.Status = "Safety paused"; r.Detail = r.PausedReason; continue; }

                // Source back?
                if (r.Watcher != null)
                {
                    bool avail = r.Watcher.SourceAvailable;
                    if (!avail)
                    {
                        // A source that went away takes its candidates with it: a
                        // queued path we can no longer read must never be acted on.
                        r.Queue.Clear();
                        r.Deferred.Clear();
                        if (r.Status != "Source unavailable")
                        {
                            r.Status = "Source unavailable";
                            r.Watcher.Stop();
                        }
                        continue;
                    }
                    if (r.Status == "Source unavailable")
                    {
                        r.Watcher.RestartIfNeeded();
                        r.Status = "Watching";
                        r.Detail = "";
                        Log.Info(r.Profile.Name, "source is available again; resuming");
                        Reconcile(r, "source returned");
                        if (r.Profile.Schedule.Trigger == FaTrigger.PathAvailable) RunProfile(r.Profile.Id, false, "path-available");
                        continue;
                    }
                    // A healthy watcher still has queued candidates: fall through
                    // to DrainPending. Returning here instead would leave every
                    // realtime profile permanently "Watching" and never act.
                }

                // PERF-004: a reconcile that ran out of admission budget keeps
                // its place. Finishing it here is what makes "the remaining tree
                // is reconciled later" a mechanism instead of a promise.
                if (r.ReconcileCursor != null)
                {
                    FaReconcileReport rep = ReconcileStep(r, "continuation");
                    Log.Info(r.Profile.Name, "reconcile continuation: " + rep.Line());
                }

                // Pending candidates from the watcher or a reconcile.
                DrainPending(r, now);

                // Periodic reconciliation for a profile with a watcher.
                if (r.Profile.ReconcileMinutes > 0 && r.Profile.Schedule.Trigger == FaTrigger.Realtime &&
                    (!r.LastReconcile.HasValue ||
                     (now - r.LastReconcile.Value).TotalMinutes >= r.Profile.ReconcileMinutes))
                    Reconcile(r, "periodic reconcile");

                // Scheduled triggers.
                if (r.Profile.Schedule.Trigger == FaTrigger.Startup) continue;   // fired by Start()
                if (r.Profile.Schedule.Trigger == FaTrigger.Resume) continue;    // fired by the resume hook
                FaTriggerDecision d = FaScheduleCalculator.Evaluate(r.Profile, now, r.LastRun);
                r.NextRun = d.NextAt;
                if (d.RunNow) RunProfile(r.Profile.Id, false, d.CatchUp ? "catch-up" : "scheduled");
            }

            if (GlobalGate.ConsumeRerun()) RunAll(false, "coalesced");
        }

        // PERF-004 will pass an admission budget in; for now the queue's own cap
        // is the only backpressure, and the return value is what a caller counts.
        public bool QueueCandidate(FaProfileRuntime r, string path)
        {
            return r.Queue.Add(path, Clock()) != null;
        }

        // Per-tick work budget. A tree with tens of thousands of candidates must
        // not be stat-ed in one tick: the tick would never finish, and every
        // profile would drift. What is not checked now stays scheduled.
        public const int MaxChecksPerTick = 2000;

        // How soon a "not quiet yet" candidate is re-probed. Two stats, cheap,
        // and the tick itself fires once a second, so this only has to beat
        // that cadence -- which the old code happened to match by accident,
        // because it probed everything.
        public const int NotYetRecheckMs = 250;

        // How long a candidate may keep looking like a half-written file before
        // it is treated as abandoned. This used to be measured from the profile
        // runtime's own start time, which made the deadline unrelated to any
        // particular file.
        public static readonly TimeSpan TemporaryTtl = TimeSpan.FromHours(6);

        // Backoff for a locked file, from the profile's configured retry policy.
        // Constant per attempt: the limit is the attempt count, not the delay,
        // and a growing delay makes the time to exhaust a budget depend on how
        // unlucky the lock was.
        static double BackoffMs(FaProfile p, int attempt)
        {
            int baseMs = p.Retry != null ? p.Retry.BackoffMs : 500;
            if (baseMs <= 0) baseMs = 500;
            return baseMs > 30000 ? 30000 : baseMs;
        }

        void DrainPending(FaProfileRuntime r, DateTime now)
        {
            if (r.Queue.Count == 0) return;
            List<FaCandidate> due = r.Queue.TryTakeDue(now, MaxChecksPerTick);
            if (due == null || due.Count == 0) return;

            int quiet = Math.Max(0, r.Profile.QuietSeconds);
            int maxAttempts = r.Profile.Retry != null ? Math.Max(1, r.Profile.Retry.Attempts) : 1;
            List<string> ready = new List<string>();
            int exhausted = 0, expired = 0;
            foreach (FaCandidate c in due)
            {
                FaStability s = r.Gate.Check(c.Path, quiet, r.Profile.TempPatternList());
                c.SampledUtc = now;
                c.Sample = s;
                if (s == FaStability.Stable)
                {
                    ready.Add(c.Path);
                    r.Queue.Remove(c);
                }
                else if (s == FaStability.Missing)
                {
                    r.Gate.Forget(c.Path);
                    r.Queue.Remove(c);
                }
                else if (s == FaStability.Locked)
                {
                    // Locked attempts are counted on the candidate itself and the
                    // next check is backed off, so a file held by another process
                    // costs one stat per backoff instead of one per tick.
                    c.Attempts++;
                    c.LastFailure = "locked";
                    if (c.Attempts >= maxAttempts)
                    {
                        r.Defer(c, "locked; retry limit reached after " + c.Attempts + " attempt(s)");
                        exhausted++;
                    }
                    else
                    {
                        c.NextEligibleUtc = now.AddMilliseconds(BackoffMs(r.Profile, c.Attempts));
                        r.Queue.Reschedule(c);
                    }
                }
                else if (s == FaStability.Temporary)
                {
                    // A download in progress. Keep watching while it is young;
                    // after the TTL it is an abandoned partial file, which is a
                    // deferred state rather than a permanent one.
                    c.LastFailure = "temporary";
                    if (now - c.FirstSeenUtc >= TemporaryTtl)
                    {
                        expired++;
                        r.Defer(c, "still a temporary/partial file after " + (int)TemporaryTtl.TotalHours + "h");
                    }
                    else
                    {
                        c.NextEligibleUtc = now.AddSeconds(1);
                        r.Queue.Reschedule(c);
                    }
                }
                else
                {
                    // NotYet: same bytes have not held for the quiet period yet.
                    c.LastFailure = "notyet";
                    c.NextEligibleUtc = now.AddMilliseconds(NotYetRecheckMs);
                    r.Queue.Reschedule(c);
                }
            }
            if (exhausted > 0)
                Log.Warn(r.Profile.Name, exhausted + " candidate(s) exhausted the locked retry limit and were deferred; a reconcile re-arms them");
            if (expired > 0)
                Log.Warn(r.Profile.Name, expired + " candidate(s) stayed temporary past the TTL and were deferred; a reconcile re-arms them");
            if (ready.Count == 0) return;
            // W2-007: a watcher-driven drain that is already running when the
            // engine starts stopping is admitted work -- it finishes. One that
            // has not started is refused. Both halves matter: the first keeps a
            // half-finished copy from being abandoned, the second keeps a
            // closing engine from starting new copies at all.
            if (!BeginWork()) return;
            if (!GlobalGate.TryEnter())
            {
                EndWork();
                GlobalGate.RequestRerun();
                return;
            }
            try
            {
                if (!r.RunGate.TryEnter())
                {
                    r.RunGate.RequestRerun();
                    return;
                }
                try
                {
                    FaRunReport report = ProcessFiles(r, ready, false, "watch");
                }
                finally { r.RunGate.Exit(); }
            }
            finally { GlobalGate.Exit(); EndWork(); }
        }

        // PERF-004: scanning and admission are one streaming pipeline, with the
        // admission budget as the backpressure. The old walk built a List of
        // every file under every source and only then queued them, so on a
        // source holding more paths than the queue allows the peak memory was
        // the whole tree, twice, and the refusal was silent.
        //
        // One step admits at most this many files and keeps its place. A caller
        // that needs the whole source reconciled calls it until Suspended is
        // false; the tick does that on its own.
        public int ReconcileAdmissionBudget = 5000;

        public FaReconcileReport ReconcileStep(FaProfileRuntime r, string why)
        {
            FaReconcileReport rep = new FaReconcileReport();
            rep.Why = why;
            DateTime now = Clock();
            FaSourceScanner scanner = r.ReconcileCursor;
            if (scanner == null)
            {
                scanner = new FaSourceScanner(r.Profile, Fs);
                r.ReconcileCursor = scanner;
            }
            int budget = ReconcileAdmissionBudget;
            scanner.Scan(delegate(string f)
            {
                rep.Scanned++;
                // Already pending: nothing to admit, and it must not eat budget,
                // or a resumed walk over a partly-queued tree would crawl.
                if (r.Queue.Find(f) != null) { rep.Deferred++; return true; }
                if (budget <= 0) { rep.Deferred++; return false; }        // out of budget: suspend here
                budget--;
                if (r.Queue.Add(f, now) != null) { rep.Admitted++; return true; }
                // The queue itself is full. Suspending is the backpressure: the
                // walk keeps its place and resumes once the queue drains.
                rep.Rejected++;
                return false;
            });
            rep.Suspended = !scanner.Done;
            rep.HitReparsePoint = scanner.LastRunHitReparsePoint;
            if (!rep.Suspended) r.ReconcileCursor = null;
            r.LastReconcile = now;
            return rep;
        }

        public void Reconcile(FaProfileRuntime r, string why)
        {
            FaReconcileReport rep = ReconcileStep(r, why);
            if (rep.HitReparsePoint)
                Log.Info(r.Profile.Name, "reconciliation skipped reparse points (junctions/symlinks) by default");
            Log.Info(r.Profile.Name, "reconciliation (" + why + "): " + rep.Line());
        }

        public void Reconcile(string profileId, string why)
        {
            FaProfileRuntime r = Runtime(profileId);
            if (r != null) Reconcile(r, why);
        }

        // ---------------------------------------------------------------- runs

        public FaProfileRuntime Runtime(string profileId)
        {
            lock (Gate) { return Runtimes.ContainsKey(profileId) ? Runtimes[profileId] : null; }
        }

        public IEnumerable<FaProfileRuntime> AllRuntimes()
        {
            lock (Gate) return new List<FaProfileRuntime>(Runtimes.Values);
        }

        public void ReloadProfiles()
        {
            bool wasStarted = _started;
            Stop();
            BuildRuntimes();
            // W2-006: rebuilding profiles is also a mutation-authority decision.
            // A profile list that changes under a running operation (sources,
            // rules, destinations) invalidates that operation's plan and the
            // journal record it is about to write; the same gate protects both.
            using (FaMutationGate mutAuthority = FaMutationGate.TryAcquire())
            {
                if (mutAuthority.WasBlocked)
                {
                    Log.Warn("(engine)", "profile reload skipped: another process holds the mutation authority");
                    return;
                }
                if (wasStarted && Config.EngineEnabled) Start();
            }
        }

        // Undo is a mutating operation and therefore enters the same
        // cross-process authority as a run. Returning to a state the running
        // engine has not observed is exactly the interleaving this prevents.
        public FaUndoResult Undo(string opId)
        {
            using (FaMutationGate mutAuthority = FaMutationGate.TryAcquire())
            {
                if (mutAuthority.WasBlocked)
                {
                    return new FaUndoResult
                    {
                        Ok = false,
                        Message = "undo refused: another process holds the Folder Automation mutation authority"
                    };
                }
                return Journal.Undo(Fs, opId);
            }
        }

        // Recovery decisions -- which pending records are ambiguous -- follow the
        // same admission rule as the operations that created them.
        public List<FaJournalEntry> AmbiguousOperations()
        {
            using (FaMutationGate mutAuthority = FaMutationGate.TryAcquire())
            {
                if (mutAuthority.WasBlocked) return new List<FaJournalEntry>();
                return Journal.Ambiguous();
            }
        }

        public void MarkAmbiguous(FaJournalEntry e)
        {
            using (FaMutationGate mutAuthority = FaMutationGate.TryAcquire())
            {
                if (mutAuthority.WasBlocked) return;
                Journal.MarkAmbiguous(e);
            }
        }

        public FaRunReport RunAll(bool dryRun, string trigger)
        {
            List<FaProfileRuntime> all;
            lock (Gate) all = new List<FaProfileRuntime>(Runtimes.Values);
            List<FaRunReport> reports = new List<FaRunReport>();
            // Serial on purpose: profiles whose trees overlap must not interleave.
            foreach (FaProfileRuntime r in all)
                if (r.Profile.Enabled) reports.Add(RunProfile(r.Profile.Id, dryRun, trigger));
            return reports.Count > 0 ? reports[0] : null;
        }

        public FaRunReport RunProfile(string profileId, bool dryRun, string trigger)
        {
            FaProfileRuntime r = Runtime(profileId);
            if (r == null) return null;
            FaRunReport report = new FaRunReport();
            report.ProfileId = r.Profile.Id;
            report.ProfileName = r.Profile.Name;
            report.DryRun = dryRun;
            report.Trigger = trigger;
            report.StartedUtc = FaJournal.Now();

            // A disabled profile never mutates anything. A dry run is still
            // allowed, because previewing is exactly what a new profile needs
            // before it is switched on.
            if (!r.Profile.Enabled && !dryRun)
            {
                report.Status = "Disabled";
                report.FinishedUtc = FaJournal.Now();
                return report;
            }
            if (PausedAll) { report.Status = "Paused"; report.FinishedUtc = FaJournal.Now(); return report; }
            if (r.SafetyPaused)
            {
                report.Status = "Safety paused";
                report.PausedReason = r.PausedReason;
                report.FinishedUtc = FaJournal.Now();
                return report;
            }

            if (!GlobalGate.TryEnter())
            {
                // Coalesce: one queued follow-up, never a second worker.
                GlobalGate.RequestRerun();
                report.Status = "Coalesced";
                report.FinishedUtc = FaJournal.Now();
                return report;
            }
            // W2-006: the cross-process mutation authority is taken INSIDE the
            // in-process gates and held only around the mutating section. It is
            // never held while waiting on a run gate, and never across a
            // caller-visible wait, so a blocked writer degrades to a report
            // instead of a second engine operating on the same trees.
            //
            // W2-007: a dry run never holds the cross-process lock and never
            // registers as admitted work, so a preview cannot block another
            // process and cannot hold a stop open. A mutating run IS admitted
            // work, so a Stop() arriving mid-run waits for it -- and a run that
            // finds the engine STOPPING is refused here instead of starting.
            bool admitted = dryRun || BeginWork();
            try
            {
                if (!admitted)
                {
                    r.Status = "Stopping";
                    r.Detail = "the engine is stopping";
                    report.Status = "Stopped";
                    report.PausedReason = r.Detail;
                    GlobalGate.Exit();
                    return report;
                }
                using (FaMutationGate mutAuthority = dryRun ? null : FaMutationGate.TryAcquire())
                {
                    if (mutAuthority != null && mutAuthority.WasBlocked)
                    {
                        // Not authority. Say so, and do nothing: a CLI or tray run
                        // without the mutation lock is precisely the interleaving
                        // this gate exists to prevent.
                        r.Status = "Waiting for mutation lock";
                        r.Detail = "another process holds the Folder Automation mutation authority";
                        report.Status = "Blocked";
                        report.PausedReason = r.Detail;
                        Log.Warn(r.Profile.Name, "run skipped: " + r.Detail + " (" + (mutAuthority.Holder ?? "unavailable") + ")");
                        GlobalGate.RequestRerun();
                        return report;
                    }
                    try
                    {
                        if (!r.RunGate.TryEnter())
                        {
                            r.RunGate.RequestRerun();
                            report.Status = "Coalesced";
                            report.FinishedUtc = FaJournal.Now();
                            return report;
                        }
                        try
                        {
                            if (!Config.EngineEnabled) { report.Status = "Engine off"; return report; }
                            if (!dryRun)
                            {
                                FaTopologyIssue issue = BlockingIssue(r.Profile);
                                if (issue != null)
                                {
                                    r.Status = "Configuration error";
                                    r.Detail = issue.Detail;
                                    report.Status = "Configuration error";
                                    report.PausedReason = issue.Detail;
                                    return report;
                                }
                            }

                            List<string> files;
                            if (dryRun)
                            {
                                FaSourceScanner scanner = new FaSourceScanner(r.Profile, Fs);
                                files = scanner.Enumerate();
                            }
                            else
                            {
                                // PERF-003: a run takes only the candidates the
                                // stability gate has vouched for, and only when
                                // they are due. The old code cloned the whole
                                // pending set (50 000 entries a tick) and cleared
                                // it, so a scheduled run could empty a queue of
                                // thousands of backing-out files in one shot.
                                files = r.Queue.SnapshotStable(Clock(), Math.Max(0, r.Profile.QuietSeconds));
                                if (files.Count == 0)
                                {
                                    FaSourceScanner scanner = new FaSourceScanner(r.Profile, Fs);
                                    files = scanner.Enumerate();
                                }
                            }

                            bool missing = false;
                            foreach (string s in r.Profile.Sources ?? new List<string>())
                                if (!Fs.DirectoryExists(FaPaths.Normalize(s))) missing = true;
                            if (missing)
                            {
                                r.Status = "Source unavailable";
                                report.Status = "Source unavailable";
                                report.PausedReason = "a source path is not reachable";
                                Log.Warn(r.Profile.Name, "run skipped: a source path is not reachable");
                                return report;
                            }

                            ProcessFiles(r, files, dryRun, trigger, report);

                            if (!dryRun)
                            {
                                r.LastRun = Clock();
                                r.Profile.Schedule.LastRun = FaJournal.Now();
                                r.Status = r.Watcher != null ? "Watching" : "Scheduled";
                                try { SaveConfig(); } catch { }
                            }
                            report.Status = r.Status;
                        }
                        finally { r.RunGate.Exit(); }
                    }
                    finally { GlobalGate.Exit(); }
                }
            }
            finally
            {
                if (admitted) EndWork();
                report.FinishedUtc = FaJournal.Now();
            }
            return report;
        }

        // The one place a file becomes an operation.
        FaRunReport ProcessFiles(FaProfileRuntime r, List<string> files, bool dryRun, string trigger)
        {
            return ProcessFiles(r, files, dryRun, trigger, new FaRunReport());
        }

        FaRunReport ProcessFiles(FaProfileRuntime r, List<string> files, bool dryRun, string trigger, FaRunReport report)
        {
            report.ProfileId = r.Profile.Id;
            report.ProfileName = r.Profile.Name;
            report.DryRun = dryRun;
            report.Trigger = trigger;
            report.StartedUtc = FaJournal.Now();

            int ops = 0;
            long bytes = 0;
            int errors = 0;
            FaLimits lim = r.Profile.Limits ?? new FaLimits();

            foreach (string path in files)
            {
                if (lim.MaxOps > 0 && ops >= lim.MaxOps)
                {
                    Trip(r, "operation limit reached (" + lim.MaxOps + ")", report, dryRun);
                    break;
                }

                if (!Fs.FileExists(path)) { r.Gate.Forget(path); continue; }

                FaFileInfo file = FaFileInfo.From(path, Fs);

                // Runtime loop guard: never act on a file that already sits in
                // one of this profile's destinations.
                foreach (string dst in FaTopology.DestinationsOf(r.Profile))
                    if (!string.IsNullOrEmpty(dst) && FaPaths.IsInside(file.Directory, dst))
                    {
                        r.Gate.Forget(path);
                        continue;
                    }

                FaDecision d = FaMatcher.Evaluate(r.Profile, file, Clock());
                switch (d.Outcome)
                {
                    case FaOutcome.Excluded:
                    case FaOutcome.Ignored:
                    case FaOutcome.Stopped:
                    case FaOutcome.GlobSkipped:
                    case FaOutcome.TempSkipped:
                    case FaOutcome.NoRule:
                    case FaOutcome.None:
                        r.Gate.Forget(path);
                        continue;
                }

                FaPlanItem item = new FaPlanItem();
                item.Source = file.FullPath;
                item.Rule = d.Rule == null ? "" : d.Rule.Name;
                item.Action = d.Rule.Action.ToString();
                // Destination is always the full TARGET FILE, never the folder:
                // the dry run, the journal and the undo record all need the exact
                // path, and taking GetDirectoryName of a folder would silently
                // target its parent.
                item.Destination = d.Rule.Action == FaAction.Rename
                    ? FaPaths.Normalize(Path.Combine(file.Directory, d.NewName))
                    : d.Rule.Action == FaAction.Recycle || d.Rule.Action == FaAction.Delete
                        ? file.FullPath
                        : FaPaths.Normalize(Path.Combine(d.Destination, file.Name));

                if (FaPaths.IsProtected(item.Destination))
                {
                    item.Warning = "destination is a protected system location; skipped";
                    report.Warnings++;
                    report.Items.Add(item);
                    continue;
                }

                FaOpPlan plan = new FaOpPlan();
                plan.Source = file.FullPath;
                plan.Action = d.Rule.Action;
                plan.DestinationDir = Path.GetDirectoryName(item.Destination);
                plan.NewName = Path.GetFileName(item.Destination);
                plan.Collision = d.Rule.Action == FaAction.Rename ? r.Profile.Collision : r.Profile.Collision;
                plan.Verify = r.Profile.Verify;
                plan.Retry = r.Profile.Retry ?? new FaRetry();
                plan.CreateDir = d.Rule.CreateDir;
                plan.DryRun = dryRun;
                plan.QuarantineDir = string.IsNullOrEmpty(r.Profile.Quarantine) ? FaPaths.QuarantinePath() : r.Profile.Quarantine;
                plan.Now = Clock();
                plan.IncomingBytes = file.Length;
                plan.IncomingModifiedUtc = file.ModifiedUtc;

                string opId = FaStore.NewId();
                FaJournalEntry pend = new FaJournalEntry();
                pend.OpId = opId;
                pend.Timestamp = FaJournal.Now();
                pend.ProfileId = r.Profile.Id;
                pend.Profile = r.Profile.Name;
                pend.RuleId = d.Rule.Id;
                pend.Rule = d.Rule.Name;
                pend.Source = file.FullPath;
                pend.Destination = item.Destination;
                pend.Action = d.Rule.Action.ToString();
                pend.Result = dryRun ? "dry-run" : "pending";
                pend.Undo = "None";
                pend.UndoState = "none";
                pend.OriginalDir = file.Directory;
                pend.OriginalName = file.Name;
                pend.Bytes = file.Length;
                pend.DryRun = dryRun;
                if (!dryRun)
                {
                    if (!Journal.Append(pend))
                    {
                        item.Outcome = "failed";
                        item.Message = "Journal write failed before mutation";
                        errors++;
                        r.ConsecutiveErrors++;
                        report.Errors++;
                        Log.Error(r.Profile.Name, file.Name + ": Journal write failed before mutation");
                        continue;
                    }
                }

                FaOpResult res = FaActions.Execute(Fs, plan, dryRun ? null : Log, r.Profile.Name);

                if (!res.Success)
                {
                    item.Outcome = "failed";
                    item.Message = res.Message;
                    errors++;
                    r.ConsecutiveErrors++;
                    report.Errors++;
                    Log.Error(r.Profile.Name, file.Name + ": " + res.Message);
                    if (!dryRun)
                    {
                        pend.Result = "failed";
                        pend.Error = res.Message;
                        if (!Journal.Append(pend))
                        {
                            r.SafetyPaused = true;
                            r.PausedReason = "Journal write failed on operation completion";
                        }
                    }
                }
                else if (res.Skipped)
                {
                    item.Outcome = "skipped";
                    item.Message = res.Message;
                    report.Skipped++;
                }
                else
                {
                    item.Outcome = dryRun ? "planned" : "done";
                    item.Message = res.Message;
                    report.Actions++;
                    ops++;
                    bytes += res.Bytes;
                    if (lim.WarnOps > 0 && ops == lim.WarnOps)
                    {
                        Log.Warn(r.Profile.Name, "this run has already touched " + ops + " files");
                        Raise(FaNoticeLevel.Info, "Folder Automation",
                            "'" + r.Profile.Name + "' has moved " + ops + " files in one run");
                    }
                    if (!dryRun)
                    {
                        pend.Result = "completed";
                        pend.Undo = res.Undo.ToString();
                        pend.UndoState = res.Reversible ? "available" : "none";
                        pend.Destination = res.FinalPath;
                        pend.DisplacedPath = res.DeletedPath;
                        pend.Bytes = res.Bytes;
                        pend.Error = "";
                        if (!Journal.Append(pend))
                        {
                            r.SafetyPaused = true;
                            r.PausedReason = "Journal write failed on operation completion";
                        }
                        if (!string.IsNullOrEmpty(res.DeletedPath) && !dryRun)
                        {
                            Log.Info(r.Profile.Name, "replaced existing file " + res.DeletedPath);
                        }
                    }
                }
                if (!dryRun) r.Gate.Forget(path);
                report.Items.Add(item);
                report.Matched++;

                if (lim.MaxBytes > 0 && bytes >= lim.MaxBytes)
                {
                    Trip(r, "byte limit reached (" + lim.MaxBytes + ")", report, dryRun);
                    break;
                }
                if (lim.MaxErrors > 0 && errors >= lim.MaxErrors)
                {
                    Trip(r, "error limit reached (" + lim.MaxErrors + ")", report, dryRun);
                    break;
                }
            }

            report.Actions = ops;
            report.Bytes = bytes;
            report.Errors = errors;
            report.FinishedUtc = FaJournal.Now();
            report.Status = r.Status;
            if (dryRun) Log.Info(r.Profile.Name, "dry run: " + report.Summary());
            return report;
        }

        void Trip(FaProfileRuntime r, string reason, FaRunReport report, bool dryRun)
        {
            report.PausedReason = reason;
            report.Warnings++;
            if (dryRun) return;
            r.SafetyPaused = true;
            r.PausedReason = reason;
            r.Status = "Safety paused";
            Log.Error(r.Profile.Name, "safety limit tripped: " + reason + "; this profile is paused");
            FaJournalEntry e = new FaJournalEntry();
            e.OpId = FaStore.NewId();
            e.Timestamp = FaJournal.Now();
            e.ProfileId = r.Profile.Id;
            e.Profile = r.Profile.Name;
            e.Action = "SafetyPause";
            e.Result = "completed";
            e.Undo = "None";
            e.UndoState = "none";
            e.Error = reason;
            Journal.Append(e);
            Raise(FaNoticeLevel.Warning, "Folder Automation",
                "'" + r.Profile.Name + "' was paused by a safety limit: " + reason);
        }

        public void ClearSafetyPause(string profileId)
        {
            FaProfileRuntime r = Runtime(profileId);
            if (r == null) return;
            r.SafetyPaused = false;
            r.PausedReason = "";
            r.ConsecutiveErrors = 0;
            r.Status = r.Profile.Enabled ? "Watching" : "Disabled";
        }

        public void NotifyResume()
        {
            foreach (FaProfileRuntime r in AllRuntimes())
                if (r.Profile.Enabled && r.Profile.Schedule.Trigger == FaTrigger.Resume)
                    RunProfile(r.Profile.Id, false, "resume");
        }

        public void NotifyStartup()
        {
            foreach (FaProfileRuntime r in AllRuntimes())
                if (r.Profile.Enabled && r.Profile.Schedule.Trigger == FaTrigger.Startup)
                    RunProfile(r.Profile.Id, false, "startup");
        }

        // ------------------------------------------------------------- helpers

        public FaProfile FindProfile(string idOrName)
        {
            if (string.IsNullOrEmpty(idOrName)) return null;
            foreach (FaProfile p in Config.Profiles)
            {
                if (string.Equals(p.Id, idOrName, StringComparison.OrdinalIgnoreCase)) return p;
                if (string.Equals(p.Name, idOrName, StringComparison.OrdinalIgnoreCase)) return p;
            }
            return null;
        }

        public List<FaRunReport> RunEveryEnabled(bool dryRun, string trigger)
        {
            List<FaRunReport> outp = new List<FaRunReport>();
            foreach (FaProfile p in Config.Profiles)
            {
                if (!p.Enabled) continue;
                FaRunReport r = RunProfile(p.Id, dryRun, trigger);
                if (r != null) outp.Add(r);
            }
            return outp;
        }

        void Raise(FaNoticeLevel level, string title, string message)
        {
            Action<FaNotice> h = Notice;
            if (h == null) return;
            try { h(new FaNotice { Level = level, Title = title, Message = message }); }
            catch { }
        }
    }

    // Starter templates. Shipped as an explicit user choice, never enabled
    // behind the user's back.
    public static class FaTemplates
    {
        public static List<FaProfile> DownloadsByType(string source)
        {
            List<FaProfile> outp = new List<FaProfile>();
            outp.Add(Build("Downloads by type", source, FaTrigger.Realtime, new FaRule[]
            {
                Image("Images"), Video("Video"), Audio("Audio"),
                Archive("Archives"), Document("Documents"), Installer("Installers")
            }));
            return outp;
        }

        public static List<FaProfile> Clipboard(string source, string destination)
        {
            FaProfile p = Build("Clipboard", source, FaTrigger.Realtime, new FaRule[] { ClipboardRule(destination) });
            return new List<FaProfile> { p };
        }

        static FaProfile Build(string name, string source, FaTrigger trigger, FaRule[] rules)
        {
            FaProfile p = new FaProfile();
            p.Id = FaStore.NewId();
            p.Name = name;
            p.Enabled = false;              // the user turns it on after a dry run
            p.Recursive = true;
            p.Trigger = trigger;
            p.Schedule.Trigger = trigger;
            p.Sources.Add(source ?? "");
            foreach (FaRule r in rules) p.Rules.Add(r);
            return p;
        }

        static FaRule ByExtension(string ruleName, string destination, params string[] exts)
        {
            FaRule r = new FaRule();
            r.Id = FaStore.NewId();
            r.Name = ruleName;
            r.Action = FaAction.Move;
            r.Destination = destination;
            r.Conditions.Add(new FaCondition { Kind = "extension", Values = exts });
            return r;
        }

        static FaRule Image(string d) { return ByExtension("Images", d, ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"); }
        static FaRule Video(string d) { return ByExtension("Video", d, ".mp4", ".mkv", ".mov", ".avi", ".webm"); }
        static FaRule Audio(string d) { return ByExtension("Audio", d, ".mp3", ".flac", ".wav", ".m4a", ".ogg"); }
        static FaRule Archive(string d) { return ByExtension("Archives", d, ".zip", ".7z", ".rar", ".tar", ".gz"); }
        static FaRule Document(string d) { return ByExtension("Documents", d, ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt", ".md"); }
        static FaRule Installer(string d) { return ByExtension("Installers", d, ".exe", ".msi", ".iso"); }

        static FaRule ClipboardRule(string destination)
        {
            FaRule r = new FaRule();
            r.Id = FaStore.NewId();
            r.Name = "Clipboard screenshots";
            r.Action = FaAction.Move;
            r.Destination = destination;
            r.Match = FaMatch.All;
            r.Conditions.Add(new FaCondition { Kind = "filename_startswith", Value = "clipboard_" });
            r.Conditions.Add(new FaCondition
            {
                Kind = "extension",
                Values = new string[] { ".png", ".jpg", ".jpeg" }
            });
            return r;
        }
    }
}