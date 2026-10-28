using System;
using System.Globalization;
using System.Threading;

// Folder Automation — schedule arithmetic and run admission control.
// Pure functions plus a tiny gate; the engine owns the timers.

namespace Saituls.FolderAutomation
{
    public sealed class FaTriggerDecision
    {
        public bool RunNow;
        public bool CatchUp;         // this run is a coalesced catch-up, not a normal slot
        public DateTime? NextAt;
        public string Reason = "";
    }

    public static class FaScheduleCalculator
    {
        public static bool TryParseAt(string s, out TimeSpan t)
        {
            t = TimeSpan.Zero;
            return !string.IsNullOrEmpty(s) && TimeSpan.TryParseExact(s.Trim(), @"hh\:mm", CultureInfo.InvariantCulture, out t);
        }

        // The next regular slot strictly after `now`, or null for triggers that
        // are not clock-driven.
        public static DateTime? NextSlot(FaProfile p, DateTime now, DateTime? lastRun)
        {
            if (p == null) return null;
            FaSchedule s = p.Schedule ?? new FaSchedule();
            switch (s.Trigger)
            {
                case FaTrigger.EveryMinutes:
                {
                    int step = s.EveryMinutes < 1 ? 15 : s.EveryMinutes;
                    DateTime baseRun = lastRun.HasValue ? lastRun.Value : now;
                    return baseRun.AddMinutes(step);
                }
                case FaTrigger.Hourly:
                {
                    DateTime top = new DateTime(now.Year, now.Month, now.Day, now.Hour, 0, 0);
                    if (lastRun.HasValue && lastRun.Value < top) return top;
                    return top.AddHours(1);
                }
                case FaTrigger.Daily:
                {
                    TimeSpan t;
                    if (!TryParseAt(s.At, out t)) t = new TimeSpan(3, 0, 0);
                    DateTime slot = new DateTime(now.Year, now.Month, now.Day).Add(t);
                    if (slot <= now) slot = slot.AddDays(1);
                    return slot;
                }
                case FaTrigger.Weekly:
                {
                    TimeSpan t;
                    if (!TryParseAt(s.At, out t)) t = new TimeSpan(3, 0, 0);
                    int delta = ((s.Weekday - (int)now.DayOfWeek) + 7) % 7;
                    DateTime slot = new DateTime(now.Year, now.Month, now.Day).AddDays(delta).Add(t);
                    if (slot <= now) slot = slot.AddDays(7);
                    return slot;
                }
                case FaTrigger.Once:
                {
                    DateTime when;
                    if (string.IsNullOrEmpty(s.RunAt)) return null;
                    if (!DateTime.TryParse(s.RunAt, CultureInfo.InvariantCulture,
                            DateTimeStyles.None, out when)) return null;
                    if (lastRun.HasValue) return null;          // one-shot already fired
                    return when > now ? when : now;
                }
                default:
                    return null;   // Manual / Realtime / Startup / Resume / PathAvailable
            }
        }

        public static int MissedCount(FaProfile p, DateTime now, DateTime? lastRun)
        {
            if (!lastRun.HasValue) return 0;
            return Math.Min(10000, EstimateMissed(p, lastRun.Value, now));
        }

        // Decides whether a due schedule should run right now, and never runs
        // more than one catch-up execution per missed window.
        public static FaTriggerDecision Evaluate(FaProfile p, DateTime now, DateTime? lastRun)
        {
            FaTriggerDecision d = new FaTriggerDecision();
            FaSchedule s = p == null ? null : p.Schedule;
            if (s == null) { d.NextAt = null; d.Reason = "no schedule"; return d; }

            DateTime? next = NextSlot(p, now, lastRun);
            d.NextAt = next;

            switch (s.Trigger)
            {
                case FaTrigger.Manual:
                    d.Reason = "manual only"; return d;
                case FaTrigger.Realtime:
                    d.Reason = "real-time"; return d;
                case FaTrigger.Startup:
                case FaTrigger.Resume:
                case FaTrigger.PathAvailable:
                    d.Reason = "event driven"; return d;
            }

            if (!next.HasValue) { d.Reason = "no further slot"; return d; }

            if (next.Value > now) { d.Reason = "next slot " + next.Value.ToString("HH:mm"); return d; }

            if (lastRun.HasValue)
            {
                // More than one interval elapsed since the last run: the machine
                // was asleep or off.
                int missed = EstimateMissed(p, lastRun.Value, now);
                if (missed > 1)
                {
                    if (s.Missed == FaMissed.Skip)
                    {
                        d.Reason = "skipped " + missed + " missed run(s)";
                        return d;
                    }
                    d.RunNow = true;
                    d.CatchUp = true;
                    d.Reason = "coalesced " + missed + " missed run(s) into one";
                    return d;
                }
                d.RunNow = true;
                d.Reason = "scheduled";
                return d;
            }

            d.RunNow = next.Value <= now;
            d.CatchUp = !d.RunNow;
            d.Reason = d.RunNow ? "scheduled" : "waiting for the first slot";
            return d;
        }

        public static int EstimateMissed(FaProfile p, DateTime lastRun, DateTime now)
        {
            FaSchedule s = p.Schedule ?? new FaSchedule();
            switch (s.Trigger)
            {
                case FaTrigger.EveryMinutes:
                {
                    int step = s.EveryMinutes < 1 ? 15 : s.EveryMinutes;
                    return Math.Max(1, (int)((now - lastRun).TotalMinutes / step));
                }
                case FaTrigger.Hourly: return Math.Max(1, (int)(now - lastRun).TotalHours);
                case FaTrigger.Daily: return Math.Max(1, (int)(now - lastRun).TotalDays);
                case FaTrigger.Weekly: return Math.Max(1, (int)(now - lastRun).TotalDays / 7);
                default: return 1;
            }
        }
    }

    // W2-006: one mutation authority across processes.
    //
    // FaRunGate serializes inside a single process: two timers, a watcher and
    // the tray. It cannot see a second PROCESS. The CLI (SAITULS.exe --fa run /
    // --fa undo) runs in its own process and mutates the very same trees and
    // journal records, so a second live engine can interleave with it: two
    // writers planning from the same snapshot, each journaling its own copy of
    // the same file, each undo restoring into a state the other already moved
    // on from.
    //
    // The mutex below is the single admission boundary both sides share.
    // "Global\" rather than "Local\" because the tray and the CLI can run in
    // different sessions (service-hosted tray, elevated run) and a Local\ name
    // is invisible across sessions -- the same class of bug this repairs.
    //
    // Acquisition order is fixed and one-directional: the process-wide run
    // gates are always taken first, this mutex second. Nothing that owns the
    // mutex ever acquires a run gate, so no path can wait on a gate while
    // holding the mutex and vice versa.
    public sealed class FaMutationGate : IDisposable
    {
        // Machine-wide, not per-user, so the service session and the
        // interactive session contend for the same authority.
        public const string MutexName = "Global\\Saituls.FolderAutomation.Mutation";

        // Bounded wait. A mutating operation that cannot obtain authority in
        // time reports itself blocked (the caller says so in its report and
        // changes nothing) instead of mutating without authority -- which is
        // the failure this repairs. The bound is generous because a real
        // operation can be long; a wedged peer must degrade to "blocked", not
        // to silent concurrent mutation.
        public static readonly TimeSpan Wait = TimeSpan.FromSeconds(30);

        readonly Mutex _mutex;
        bool _owned;
        bool _disposed;

        FaMutationGate(Mutex m, bool owned)
        {
            _mutex = m;
            _owned = owned;
        }

        public bool WasBlocked { get { return !_owned; } }

        public string Holder { get { return _owned ? null : MutexName; } }

        // Returns a gate; check WasBlocked before relying on holding the
        // authority. Disposing a blocked gate is a no-op, which is what makes
        // the try/finally shape in the caller safe to write straight.
        public static FaMutationGate TryAcquire()
        {
            return TryAcquire(Wait);
        }

        public static FaMutationGate TryAcquire(TimeSpan wait)
        {
            Mutex m = null;
            bool owned = false;
            try
            {
                m = new Mutex(false, MutexName, out owned);
                if (!owned) owned = m.WaitOne(wait);
            }
            catch (AbandonedMutexException)
            {
                // A previous holder died without releasing. That is exactly
                // the case the mutex exists for: the state on disk may be
                // mid-operation, and this process is the only one left to look
                // at it. Take the authority; journal recovery decides what is
                // ambiguous before anything else runs.
                owned = true;
            }
            catch
            {
                // A name that cannot be created or waited on is not authority.
                if (m != null) { try { m.Dispose(); } catch { } }
                return new FaMutationGate(null, false);
            }
            if (!owned)
            {
                try { m.Dispose(); } catch { }
                return new FaMutationGate(null, false);
            }
            return new FaMutationGate(m, true);
        }

        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;
            if (_owned && _mutex != null) { try { _mutex.ReleaseMutex(); } catch { } }
            if (_mutex != null) { try { _mutex.Dispose(); } catch { } }
        }
    }

    // One profile runs at a time. A trigger that arrives mid-run is coalesced
    // into a single follow-up, never a second worker.
    public sealed class FaRunGate
    {
        int _running;
        int _rerun;
        readonly string Name;

        public FaRunGate(string name) { Name = name; }

        public bool IsRunning { get { return Volatile.Read(ref _running) != 0; } }
        public bool RerunRequested { get { return Volatile.Read(ref _rerun) != 0; } }

        public bool TryEnter()
        {
            return Interlocked.CompareExchange(ref _running, 1, 0) == 0;
        }

        public void Exit()
        {
            Interlocked.Exchange(ref _running, 0);
        }

        // Returns true when the caller should run again immediately.
        public bool RequestRerun()
        {
            Interlocked.Exchange(ref _rerun, 1);
            return IsRunning;
        }

        public bool ConsumeRerun()
        {
            return Interlocked.Exchange(ref _rerun, 0) == 1;
        }

        public string ProfileName { get { return Name; } }
    }
}