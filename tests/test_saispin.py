#!/usr/bin/env python3
"""Self-check for the SAISPIN decision engine.

Run:  python tests/test_saispin.py
Exit: 0 = all PASS, 1 = failures.

Every case here is a rule where the failure mode is a killed process that was
doing its job, so they are written from the incidents that produced the design:

1. Two real runaways (an orphaned Cline hub and an orphaned Python worker) had
   BOTH a dead parent AND a pegged core for hours.
2. Two orphaned ``pytest`` processes had a dead parent and ~0% CPU. They were
   the counterexample: orphanhood on its own proves nothing.
3. A pegged core with a live parent is the normal case -- compilers, encoders,
   indexers, test runs -- and must never be terminated automatically.
"""
import os
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + os.sep + '..')
sys.path.insert(0, os.path.join(ROOT, 'Scripts'))

import saispin_logic as logic                                        # noqa: E402

fails = 0


def check(name, cond, detail=''):
    global fails
    if cond:
        print('PASS ', name, ('  -> ' + detail) if detail else '')
    else:
        fails += 1
        print('FAIL ', name, '  -> ' + detail)


T0 = datetime(2026, 9, 3, 14, 0, 0, tzinfo=timezone.utc)


def stamp(minutes):
    return (T0 + timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')


def sample(minutes, cpu, orphan=True, pid=30976, start='2026-09-03T12:20:31Z',
           name='python.exe', main=False):
    return logic.Sample(
        pid=pid,
        start_time=start,
        name=name,
        cpu_percent=cpu,
        orphan=orphan,
        observed_at=stamp(minutes),
        cmdline='python ... tests/test_app.py',
        is_main_process=main,
    )


def replay(cpus, auto_kill=False, orphan=True, name='python.exe',
           history=None, minutes_per_sample=6, settings=None, **kw):
    """Feed one process a series of samples the way the watcher would.

    Returns the last decision and the state that would have been persisted, so
    a test can assert on both the verdict and what the next sweep will read.
    """
    state = dict(history or {})
    last = None
    for i, cpu in enumerate(cpus):
        one = sample(i * minutes_per_sample, cpu, orphan=orphan, name=name, **kw)
        last = logic.decide(state, one, auto_kill, settings=settings)
        if last.record is None:
            state.pop(one.key, None)
        else:
            state[one.key] = last.record
    return last, state


HOT = 97.0
COLD = 0.4


def main():
    # ---- the sampler arithmetic the whole model rests on -------------------
    check('a 2.9s CPU delta over a 3s window reads as ~97% of one core',
          abs(logic.cpu_percent_core(100.0, 102.9, 3.0) - 96.666) < 0.01,
          '%.3f' % logic.cpu_percent_core(100.0, 102.9, 3.0))
    check('a negative delta reads as 0%, not as a huge number',
          logic.cpu_percent_core(500.0, 10.0, 3.0) == 0.0,
          'PID reuse mid-sweep must not manufacture a hot sample')

    # ---- T1: sustained orphan spin, dry-run -------------------------------
    last, state = replay([HOT] * 6)
    check('T1 six consecutive hot samples on an orphan ALERT under dry-run',
          last.action == logic.ALERT and last.sustained and last.hits == 6,
          'action=%s hits=%d hot=%dm' % (last.action, last.hits, last.hot_seconds // 60))
    check('T1 the streak is persisted under the PID+StartTime identity',
          list(state) == ['30976:2026-09-03T12:20:31Z'],
          'keys=%s' % list(state))

    # ---- T2: a cold sample resets the history -----------------------------
    last, state = replay([HOT, HOT, HOT, COLD, HOT, HOT, HOT])
    check('T2 HOT HOT HOT COLD HOT HOT HOT is IGNORE, not six hits',
          last.action == logic.IGNORE and last.hits == 3,
          'action=%s hits=%d' % (last.action, last.hits))
    check('T2 the cold sample dropped the record instead of pausing it',
          state['30976:2026-09-03T12:20:31Z']['hits'] == 3,
          'hits=%d' % state['30976:2026-09-03T12:20:31Z']['hits'])

    # ---- T3: non-orphan sustained spin ------------------------------------
    last, _ = replay([HOT] * 8, auto_kill=True, orphan=False)
    check('T3 a sustained spin with a live parent ALERTs',
          last.action == logic.ALERT and last.sustained,
          'action=%s' % last.action)
    check('T3 auto-kill does NOT promote it to KILL',
          last.action != logic.KILL,
          'a busy compiler is not a defect: %s' % last.reason)

    # ---- T4: orphan + sustained + auto-kill -------------------------------
    last, _ = replay([HOT] * 6, auto_kill=True)
    check('T4 orphan + sustained + auto-kill KILLs',
          last.action == logic.KILL,
          'action=%s -> %s' % (last.action, last.reason))

    # ---- T5: sleeping orphan (the pytest counterexample) -------------------
    last, state = replay([COLD] * 20, auto_kill=True)
    check('T5 an orphan at ~0% CPU is IGNOREd however long it sits there',
          last.action == logic.IGNORE and not last.hot,
          'action=%s' % last.action)
    check('T5 a sleeping orphan is not even tracked',
          state == {}, 'state=%s' % state)

    # ---- T6: PID reuse ----------------------------------------------------
    _, state = replay([HOT] * 6, auto_kill=True)
    old_key = '30976:2026-09-03T12:20:31Z'
    fresh = sample(36, HOT, start='2026-09-03T18:00:00Z')
    reused = logic.decide(state, fresh, True)
    check('T6 the same PID with a new StartTime is a new identity',
          reused.key != old_key and reused.hits == 1,
          'key=%s hits=%d' % (reused.key, reused.hits))
    check('T6 the new process does not inherit the old strikes -> no KILL',
          reused.action == logic.IGNORE,
          'action=%s -> %s' % (reused.action, reused.reason))
    # The old six-hit entry, re-filed under the NEW identity's key while its own
    # fields still describe the dead process. A partially recovered state file
    # can look exactly like this, and trusting the key alone would hand the new
    # process six strikes it never earned.
    liar = dict(state[old_key])
    check('T6 an entry whose fields disagree with its key is not trusted',
          logic.decide({fresh.key: liar}, fresh, True).hits == 1,
          'stored start_time %r vs sampled %r' % (liar['start_time'], fresh.start_time))

    # ---- T7: broken hot streak ------------------------------------------
    last, _ = replay([HOT, HOT, HOT, COLD, HOT, HOT, HOT, COLD, HOT, HOT],
                     auto_kill=True)
    check('T7 ten samples, seven of them hot, still not sustained',
          last.action == logic.IGNORE and last.hits == 2,
          'action=%s hits=%d' % (last.action, last.hits))

    # ---- T8: allowlisted orphan spin -------------------------------------
    for name in sorted(logic.NEVER_KILL):
        last, _ = replay([HOT] * 6, auto_kill=True, name=name)
        if last.action != logic.ALERT:
            check('T8 %s is never auto-killed' % name, False,
                  'action=%s' % last.action)
            break
    else:
        check('T8 every never-kill image ALERTs instead of dying',
              True, '%d images' % len(logic.NEVER_KILL))

    last, _ = replay([HOT] * 6, auto_kill=True, name='Code.exe', main=True)
    check("T8 VS Code's main process is protected",
          last.action == logic.ALERT, 'action=%s' % last.action)
    last, _ = replay([HOT] * 6, auto_kill=True, name='Code.exe', main=False)
    check('T8 a Code.exe CHILD is still killable when it spins orphaned',
          last.action == logic.KILL, 'action=%s' % last.action)
    check('T8 the allowlist is extensible without touching the engine',
          logic.is_never_kill('myrunner.exe', allowlist=['MyRunner.EXE']),
          'compared case-insensitively')
    check('T8 a process with no readable name is treated as protected',
          logic.is_never_kill(''), 'missing information biases to safety')

    # PID 0 and PID 4 are the Idle and System processes: parentless by nature,
    # and Idle is "hot" for every cycle the machine did NOT use. The first live
    # dry-run tracked PID 0 as the only candidate on the box, which left alone
    # would have alerted every five minutes forever.
    last, state = replay([HOT] * 8, auto_kill=True, pid=0,
                         start='2026-09-01T10:30:39Z', name='System Idle Process')
    check('the Idle process is never tracked and never alerts',
          last.action == logic.IGNORE and state == {} and last.hits == 0,
          'action=%s tracked=%d' % (last.action, len(state)))
    last, _ = replay([HOT] * 8, auto_kill=True, pid=4,
                     start='2026-09-01T10:30:39Z', name='System')
    check('the System process (PID 4) is never tracked either',
          last.action == logic.IGNORE, 'action=%s' % last.action)
    check('the kernel PID guard is by number, not by a localizable name',
          logic.is_never_kill('Бездействие системы', pid=0)
          and logic.is_never_kill('anything', pid=4),
          'a Russian Windows reports PID 0 under a different name entirely')
    check('an unreadable PID is treated as protected',
          logic.is_never_kill('python.exe', pid='n/a'),
          'missing information biases to safety')
    check('an ordinary PID is not protected by the floor',
          not logic.is_never_kill('python.exe', pid=30976), 'PID 30976')

    # ---- elapsed-time gate ------------------------------------------------
    last, _ = replay([HOT] * 6, auto_kill=True, minutes_per_sample=1)
    check('six hot samples inside 5 minutes are not yet sustained',
          last.action == logic.IGNORE and last.hits == 6,
          'hits=%d hot=%ds' % (last.hits, last.hot_seconds))
    last, _ = replay([HOT] * 5, auto_kill=True, minutes_per_sample=25)
    check('five hot samples over 100 minutes are not yet sustained',
          last.action == logic.IGNORE and last.hits == 5,
          'both gates must pass, not either one')
    check('an unparseable first_hot yields 0 elapsed, never a guess',
          logic.decide(
              {'1:A': {'pid': 1, 'start_time': 'A', 'first_hot': 'not-a-time',
                       'hits': 20, 'name': 'x.exe', 'cmdline': ''}},
              logic.Sample(pid=1, start_time='A', name='x.exe',
                           cpu_percent=HOT, orphan=True,
                           observed_at=stamp(0)),
              True).action == logic.IGNORE,
          'a broken stamp must not satisfy the 30-minute gate')

    # ---- stale history ----------------------------------------------------
    # The watcher sweeps every five minutes. A gap far longer than that means
    # nobody was observing: a sleeping laptop, a disabled task, a machine that
    # was off. The streak on the far side of that gap proves nothing about the
    # interval, so it must not be extended into a kill.
    _, state = replay([HOT] * 5, auto_kill=True)
    key = '30976:2026-09-03T12:20:31Z'
    check('a fresh streak is carried across an ordinary sweep gap',
          logic.decide(state, sample(30, HOT), True).hits == 6,
          'six minutes later, still consecutive')
    stale = logic.decide(state, sample(60 * 24 * 2, HOT), True)
    check('a two-day gap starts a fresh streak instead of extending one',
          stale.hits == 1 and stale.action == logic.IGNORE,
          'hits=%d action=%s' % (stale.hits, stale.action))
    check('no kill can come out of a slept-through gap',
          stale.action != logic.KILL,
          'the machine was not watching for those two days')
    # Just inside the tolerance: a couple of missed sweeps is normal.
    ok = logic.decide(state, sample(24 + 25, HOT), True)
    check('a few missed sweeps do not throw the streak away',
          ok.hits == 6, 'hits=%d' % ok.hits)

    # ---- corrupt state ----------------------------------------------------
    _, state = replay([HOT] * 6)
    one = sample(36, HOT)
    untrusted = logic.decide(state, one, True, history_trusted=False)
    check('a rebuilt-after-corruption history downgrades KILL to ALERT',
          untrusted.action == logic.ALERT,
          'action=%s -> %s' % (untrusted.action, untrusted.reason))
    check('a non-dict history is survivable, not a crash',
          logic.decide('garbage', one, True).hits == 1, 'starts a fresh streak')

    # ---- the sweep contract the watcher relies on -------------------------
    hot_one = sample(0, HOT, pid=101, start='S1')
    hot_two = sample(0, HOT, pid=102, start='S2')
    decisions, next_state = logic.sweep({}, [hot_one, hot_two])
    check('sweep decides every sample it is given',
          len(decisions) == 2 and len(next_state) == 2,
          '%d decisions, %d tracked' % (len(decisions), len(next_state)))
    decisions, after = logic.sweep(next_state, [hot_one])
    check('a process missing from the next sweep is forgotten',
          list(after) == ['101:S1'], 'keys=%s' % list(after))
    decisions, after = logic.sweep(next_state, [{
        'pid': 101, 'start_time': 'S1', 'name': 'python.exe',
        'cpu_percent': HOT, 'orphan': True, 'observed_at': stamp(6),
    }])
    check('sweep accepts the watcher raw JSON shape',
          decisions[0].hits == 2, 'hits=%d' % decisions[0].hits)

    # ---- decision matrix, exhaustively ------------------------------------
    matrix = {
        (False, False): logic.IGNORE,
        (True, False): logic.IGNORE,
        (False, True): logic.ALERT,
    }
    ok = True
    for (orphan, spin), expected in matrix.items():
        for auto in (False, True):
            got, _ = replay([HOT] * 6 if spin else [COLD],
                            auto_kill=auto, orphan=orphan)
            if got.action != expected:
                ok = False
                check('matrix orphan=%s spin=%s auto=%s' % (orphan, spin, auto),
                      False, 'expected %s, got %s' % (expected, got.action))
    if ok:
        check('the section 6 matrix holds for every orphan/spin/auto-kill row',
              True, 'orphan+spin gates KILL on auto-kill alone')

    # ---- centralized settings policy --------------------------------------
    defaults, warning = logic.normalize_settings(None)
    check('settings defaults preserve the old decision thresholds',
          warning is None and defaults['thresholds'] == {
              'cpu_percent': logic.HOT_CPU_PERCENT,
              'required_hits': logic.SUSTAINED_HITS,
              'minimum_hot_minutes': logic.SUSTAINED_SECONDS // 60,
          },
          str(defaults['thresholds']))
    check('settings defaults preserve the old sample and stale windows',
          defaults['timing']['sample_seconds'] == logic.SAMPLE_SECONDS
          and defaults['timing']['stale_gap_minutes'] * 60 == logic.STALE_GAP_SECONDS,
          str(defaults['timing']))
    custom = {
        'schema_version': 1,
        'thresholds': {'cpu_percent': 90, 'required_hits': 7, 'minimum_hot_minutes': 35},
        'timing': {'sample_seconds': 4, 'sweep_interval_minutes': 7, 'stale_gap_minutes': 35},
        'notifications': {'enabled': True, 'reminder_minutes': 90},
        'allowlist': [],
        'process_rules': {},
    }
    got, warning = logic.normalize_settings(custom)
    check('custom thresholds and timing validate as one complete policy',
          warning is None and got['thresholds']['required_hits'] == 7
          and got['timing']['sweep_interval_minutes'] == 7,
          str(got))
    last, _ = replay([89.0] * 10, auto_kill=True, settings=custom)
    check('custom CPU threshold keeps lower heat out of the hot streak',
          last.action == logic.IGNORE and last.hits == 0,
          'action=%s hits=%d' % (last.action, last.hits))
    last, _ = replay([HOT] * 6, auto_kill=True, settings=custom)
    check('custom hit and duration gates prevent the old six-sample kill',
          last.action == logic.IGNORE and last.hits == 6,
          'action=%s hits=%d' % (last.action, last.hits))
    last, _ = replay([HOT] * 7, auto_kill=True, settings=custom)
    check('custom hit and duration gates allow a qualified seven-sample kill',
          last.action == logic.KILL and last.hits == 7,
          'action=%s hits=%d' % (last.action, last.hits))

    alert_only = dict(defaults)
    alert_only['process_rules'] = {'python.exe': {'mode': 'alert_only'}}
    last, _ = replay([HOT] * 8, auto_kill=True, settings=alert_only)
    check('per-process alert_only can never become a kill',
          last.action == logic.ALERT and 'alert-only' in last.reason,
          'action=%s' % last.action)
    strict_rule = dict(defaults)
    strict_rule['process_rules'] = {'python.exe': {'cpu_percent': 95, 'required_hits': 8}}
    last, _ = replay([90.0] * 8, auto_kill=True, settings=strict_rule)
    check('per-process thresholds can make a process harder to kill',
          last.action == logic.IGNORE and last.hits == 0,
          'action=%s' % last.action)
    unsafe_override = dict(defaults)
    unsafe_override['process_rules'] = {'python.exe': {'cpu_percent': 50}}
    rejected, warning = logic.normalize_settings(unsafe_override)
    check('a process override cannot weaken global thresholds',
          warning is not None and rejected == defaults,
          str(warning))
    system_override = dict(defaults)
    system_override['process_rules'] = {'system': {
        'mode': 'default', 'cpu_percent': 50, 'required_hits': 2, 'minimum_hot_minutes': 5,
    }}
    last, state = replay([HOT] * 8, auto_kill=True, pid=4, name='System',
                         settings=system_override)
    check('settings cannot override the kernel PID never-kill guard',
          last.action == logic.IGNORE and state == {},
          'action=%s tracked=%d' % (last.action, len(state)))
    invalid, warning = logic.normalize_settings({
        'thresholds': {'required_hits': 0},
        'allowlist': ['should-not-partially-apply.exe'],
    })
    check('malformed settings fail closed without applying partial fields',
          warning is not None and invalid == defaults,
          str(warning))
    inconsistent = dict(defaults)
    inconsistent['timing'] = dict(defaults['timing'], sweep_interval_minutes=60)
    rejected, warning = logic.normalize_settings(inconsistent)
    check('stale-history window cannot be shorter than the scheduled cadence',
          warning is not None and rejected == defaults,
          str(warning))
    legacy, warning = logic.normalize_settings({'Allowlist': ['Legacy.exe'], 'SampleSeconds': 5})
    check('the pre-settings config shape remains readable',
          warning is None and legacy['allowlist'] == ['legacy.exe']
          and legacy['timing']['sample_seconds'] == 5,
          str(legacy))
    cli = subprocess.run(
        [sys.executable, os.path.join(ROOT, 'Scripts', 'saispin_logic.py'),
         '--normalize-settings', '--defaults'],
        capture_output=True, text=True, timeout=15,
    )
    cli_payload = json.loads(cli.stdout or '{}')
    check('headless settings UI can request defaults without waiting on stdin',
          cli.returncode == 0 and cli_payload.get('warning') is None
          and cli_payload.get('settings') == defaults,
          cli.stderr.strip())

    print('---')
    print('PASS (0 failures)' if fails == 0 else 'FAILED (%d failures)' % fails)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
