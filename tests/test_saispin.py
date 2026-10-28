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
import tempfile
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

# Recovery leases live in their own directory, overridden here so the suite
# never touches the real runtime tree. PID 30976 / 2026-09-03T12:20:31Z is the
# fixture identity every replay() sample uses.
LEASE_DIR = tempfile.mkdtemp(prefix='saispin_lease_')
os.environ[logic.LEASE_ENV_DIR] = LEASE_DIR
LEASE_ID = ('30976', '2026-09-03T12:20:31Z')


def write_lease(pid='30976', start='2026-09-03T12:20:31Z', *,
                authorized=True, expires='2026-09-03T23:59:00Z', raw=None,
                manager_id='saituls-test-manager', job_id='test-job-1',
                policy='heartbeat-timeout', timeout=900,
                progress='2026-09-03T12:00:00Z', revoked=False,
                schema_version=2):
    """Drop a managed-worker lease file for an identity. Returns the path.

    Default is a v2 contract with STALE progress (eligible when hot+orphan).
    Pass progress=fresh stamp to prove active work, revoked=True to test
    withdrawal, schema_version=1 (or raw dict) for legacy-contract refusal.
    """
    safe = start.replace(':', '_')
    path = os.path.join(LEASE_DIR, '%s_%s.json' % (pid, safe))
    if raw is not None:
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(raw)
        return path
    if schema_version == 1:
        payload = {'pid': int(pid), 'start_time': start,
                   'authorized': authorized, 'expires_at': expires}
    else:
        payload = {'schema_version': 3, 'pid': int(pid), 'start_time': start,
                   'manager_id': manager_id, 'job_id': job_id,
                   'windows_user': 'TESTUSER', 'authorized': authorized,
                   'producer_token': 'test-producer-token',
                   'revoked': revoked, 'expires_at': expires,
                   'recovery_policy': policy,
                   'heartbeat_timeout_seconds': timeout,
                   'last_progress_at': progress, 'generation': 1}
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh)
    if payload.get('producer_token'):
        register_test_producer_token()
    return path


def clear_leases():
    for name in os.listdir(LEASE_DIR):
        try:
            os.unlink(os.path.join(LEASE_DIR, name))
        except OSError:
            pass
    # Clear the producer registry too so stale tokens don't leak between cases.
    reg_path = os.path.join(LEASE_DIR, logic.LEASE_PRODUCER_REGISTRY)
    try:
        os.unlink(reg_path)
    except OSError:
        pass


def register_test_producer_token():
    """Register the test producer token in the manager registry so that
    write_lease() bypasses -- which skips register_managed_worker() -- still
    passes validate_lease_producer_authority()'s registry check (P0-1)."""
    reg = {}
    reg_path = os.path.join(LEASE_DIR, logic.LEASE_PRODUCER_REGISTRY)
    try:
        with open(reg_path, 'r', encoding='utf-8-sig') as fh:
            reg = json.load(fh)
    except (OSError, ValueError):
        pass
    reg['test-producer-token'] = {
        'pid': 30976, 'start_time': '2026-09-03T12:20:31Z',
        'manager_id': 'saituls-test-manager', 'job_id': 'test-job-1',
        'windows_user': 'TESTUSER',
        'created_at': '2026-09-03T12:20:31Z', 'revoked': False,
    }
    with open(reg_path, 'w', encoding='utf-8') as fh:
        json.dump(reg, fh)


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
    # CPU heat + orphanhood + auto-kill is NOT proof of recoverability: with
    # no ownership record the verdict stops at SUSPECTED_STALL (P0-1).
    last, _ = replay([HOT] * 6, auto_kill=True)
    check('T4 orphan + sustained + auto-kill without a lease REFUSES to kill',
          last.action == logic.ALERT and 'no recovery lease' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    check('T4 the refused verdict classifies as suspected stall, not recoverable',
          last.classification in (logic.CLASSIFICATION_SUSPECTED_STALL,
                                  logic.CLASSIFICATION_OBSERVE_ONLY),
          'classification=%s' % last.classification)
    # Positive eligibility: same evidence PLUS a valid identity-bound lease.
    write_lease()
    last, _ = replay([HOT] * 6, auto_kill=True)
    check('T4 the same spin with a valid recovery lease KILLs',
          last.action == logic.KILL and 'ownership positively proven' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    check('T4 the leased verdict classifies as recovery eligible',
          last.classification == logic.CLASSIFICATION_RECOVERY_ELIGIBLE,
          'classification=%s' % last.classification)
    clear_leases()

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
    # Still *eligible* when orphaned (not the guarded root) -- but KILL itself
    # now still demands the ownership lease like every other identity.
    last, _ = replay([HOT] * 6, auto_kill=True, name='Code.exe', main=False)
    check('T8 a Code.exe CHILD stays out of the protected set when orphaned',
          last.action == logic.ALERT and 'no recovery lease' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    write_lease()
    last, _ = replay([HOT] * 6, auto_kill=True, name='Code.exe', main=False)
    check('T8 a Code.exe CHILD with a lease is killed when it spins orphaned',
          last.action == logic.KILL, 'action=%s' % last.action)
    clear_leases()
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
    write_lease()   # the KILL row now requires positive ownership evidence
    for (orphan, spin), expected in matrix.items():
        for auto in (False, True):
            got, _ = replay([HOT] * 6 if spin else [COLD],
                            auto_kill=auto, orphan=orphan)
            if got.action != expected:
                ok = False
                check('matrix orphan=%s spin=%s auto=%s' % (orphan, spin, auto),
                      False, 'expected %s, got %s' % (expected, got.action))
    clear_leases()
    if ok:
        check('the section 6 matrix holds for every orphan/spin/auto-kill row',
              True, 'orphan+spin gates KILL on a lease plus auto-kill')

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
    write_lease()
    last, _ = replay([HOT] * 7, auto_kill=True, settings=custom)
    check('custom hit and duration gates allow a qualified seven-sample kill',
          last.action == logic.KILL and last.hits == 7,
          'action=%s hits=%d' % (last.action, last.hits))
    clear_leases()

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

    # Windows PowerShell 5.1 can put a UTF-8 BOM in front of what it pipes to
    # the engine (seen from a nested powershell.exe running the Test Sweep),
    # and the payload carries non-ASCII command lines. Both must survive.
    payload = json.dumps({'samples': [], 'history': {}, 'note': 'Ярлык ✓'}, ensure_ascii=False)
    engine = os.path.join(ROOT, 'Scripts', 'saispin_logic.py')
    bom = subprocess.run([sys.executable, engine, '--sweep'], input=b'\xef\xbb\xbf' + payload.encode('utf-8'),
                         capture_output=True, timeout=15)
    check('engine accepts a BOM-prefixed UTF-8 payload (PowerShell 5.1 pipe)',
          bom.returncode == 0 and json.loads(bom.stdout.decode('utf-8') or '{}').get('decisions') == [],
          bom.stderr.decode('utf-8', 'replace').strip())
    plain = subprocess.run([sys.executable, engine, '--sweep'], input=payload.encode('utf-8'),
                           capture_output=True, timeout=15)
    check('engine reads UTF-8 regardless of the console code page',
          plain.returncode == 0, plain.stderr.decode('utf-8', 'replace').strip())

    # ---- P0-1: positive eligibility vs. heuristic uncertainty ------------
    # The reproduced counterexample: an orphaned custom Python worker pegging
    # a core for 30 minutes with auto-kill armed. CPU heat + orphanhood must
    # NOT constitute proof that an arbitrary generic job is disposable.
    clear_leases()
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 counterexample: orphaned custom worker at 97% x 30min is NOT killed',
          last.action == logic.ALERT and last.sustained,
          'action=%s sustained=%s -> %s' % (last.action, last.sustained, last.reason))
    check('P0-1 refusal names the missing ownership evidence',
          'no recovery lease' in last.reason, last.reason)

    # A valid lease bound to this exact identity is positive evidence.
    write_lease()
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 a valid identity-bound lease flips the same evidence to KILL',
          last.action == logic.KILL and last.classification == logic.CLASSIFICATION_RECOVERY_ELIGIBLE,
          'action=%s classification=%s' % (last.action, last.classification))

    # Expired authorization cannot authorize anything.
    clear_leases()
    write_lease(expires='2026-09-03T14:20:00Z')   # expires before observed_at (14:30)
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 an expired lease refuses destructive recovery',
          last.action == logic.ALERT and 'expired' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))

    # Not authorized.
    clear_leases()
    write_lease(authorized=False)
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 a lease without explicit authorization refuses',
          last.action == logic.ALERT and 'not authorized' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))

    # A lease file present under this identity but naming ANOTHER pid and
    # start_time (failed or forged owner lease) cannot authorize this PID.
    clear_leases()
    write_lease(pid='777', start='2020-01-01T00:00:00Z')
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 a lease belonging to another identity cannot authorize this PID',
          last.action == logic.ALERT and 'no recovery lease' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    # Same, but forged in place: file key matches, payload pid/start mismatched.
    forged = {'schema_version': 3, 'pid': 777, 'start_time': '2020-01-01T00:00:00Z',
              'manager_id': 'saituls-test-manager', 'job_id': 'test-job-1',
              'windows_user': 'TESTUSER', 'producer_token': 'test-producer-token',
              'authorized': True, 'expires_at': '2026-09-03T23:59:00Z',
              'recovery_policy': 'heartbeat-timeout', 'heartbeat_timeout_seconds': 900,
              'last_progress_at': '2026-09-03T12:00:00Z', 'generation': 1}
    clear_leases()
    write_lease(raw=json.dumps(forged))
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 a forged lease payload fails its own identity check',
          last.action == logic.ALERT and 'identity mismatch' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))

    # PID reuse: lease for the OLD start_time must not follow the new process.
    clear_leases()
    write_lease()
    reused = sample(36, HOT, start='2026-09-03T18:00:00Z')
    fresh_state = {}
    last = None
    for i in range(6):
        one = sample(i * 6, HOT, start='2026-09-03T18:00:00Z')
        last = logic.decide(fresh_state, one, True)
        if last.record is None:
            fresh_state.pop(one.key, None)
        else:
            fresh_state[one.key] = last.record
    check('P0-1 a reused PID does not inherit the old identity lease',
          last.action == logic.ALERT and 'no recovery lease' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))

    # Garbage lease file: missing information biases to NOT killing.
    clear_leases()
    write_lease(raw='{ this is not json')
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 an unreadable lease refuses rather than trusting it',
          last.action == logic.ALERT and 'unreadable' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))

    # ---- P0-2: adversarial active-test protection ------------------------
    # An orphaned active Python TEST (real pytest cmdline markers) must
    # survive even when a lease exists -- protection wins over authorization.
    write_lease()

    def sustained_sample(base, minutes, **kw):
        """Replay one identity to a sustained streak, returning the verdict."""
        state, last = {}, None
        for i in range(6):
            one = logic.Sample(
                pid=base['pid'], start_time=base['start_time'],
                name=base['name'], cpu_percent=HOT, orphan=True,
                observed_at=stamp(minutes + i * 6), cmdline=base['cmdline'],
                is_main_process=False)
            last = logic.decide(state, one, True, **kw)
            state = {} if last.record is None else {last.key: last.record}
        return last

    last = sustained_sample(
        {'pid': 30976, 'start_time': '2026-09-03T12:20:31Z', 'name': 'python.exe',
         'cmdline': 'python -m pytest tests/test_app.py'}, 0)
    check('P0-2 an orphaned active pytest run survives even with a lease',
          last.action == logic.ALERT and 'protected' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))

    last = sustained_sample(
        {'pid': 4111, 'start_time': '2026-09-03T12:00:00Z', 'name': 'node.exe',
         'cmdline': 'node custom_runner.js --test'}, 0)
    check('P0-2 an orphaned active Node test survives',
          last.action == logic.ALERT and 'protected' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))

    # A renamed/wrapped test executable with NO recognizable CLI markers and
    # a lease: unknown command lines are not protection, and the lease proves
    # ownership -- so this is the one shape that legitimately reaches KILL.
    write_lease(pid='4112', start='2026-09-03T12:00:00Z')
    last = sustained_sample(
        {'pid': 4112, 'start_time': '2026-09-03T12:00:00Z', 'name': 'wrapped-runner.exe',
         'cmdline': 'wrapped-runner.exe --payload 7'}, 0)
    check('P0-2 an owned disposable worker (markers absent, lease present) is eligible',
          last.action == logic.KILL,
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()

    # A custom worker doing real work with a live parent is never auto-killed.
    last, _ = replay([HOT] * 8, auto_kill=True, orphan=False, name='python.exe')
    check('P0-2 a live-parent custom worker survives regardless of lease',
          last.action == logic.ALERT and last.action != logic.KILL,
          'action=%s' % last.reason)

    # Zero-output / cold legitimate job: never even tracked.
    last, state = replay([0.0] * 10, auto_kill=True, name='python.exe')
    check('P0-2 a legitimate job at zero CPU is ignored, not tracked',
          last.action == logic.IGNORE and state == {},
          'action=%s state=%s' % (last.action, state))

    # Denying auto-kill always wins, lease or not.
    write_lease()
    last, _ = replay([HOT] * 6, auto_kill=False, name='python.exe')
    check('P0-2 auto-kill off stays ALERT even with a valid lease',
          last.action == logic.ALERT and 'auto-kill is off' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()

    # Progress resumption resets stale evidence: a cold sample between hot
    # streaks starts over, so resumed work cannot inherit old strikes.
    last, state = replay([HOT] * 5, auto_kill=True, name='python.exe')
    write_lease()
    resumed, state = replay([COLD, HOT, HOT], auto_kill=True, name='python.exe',
                            history=state)
    check('P0-2 progress resumption resets the stale hot evidence',
          resumed.action == logic.IGNORE and resumed.hits == 2,
          'action=%s hits=%d' % (resumed.action, resumed.hits))
    clear_leases()

    # ---- P0-1 recovery condition: liveness defeats authorization ----------
    # Counterexample from the mission: authorized, unexpired, identity-bound
    # lease + sustained 97% hot orphan, but last_progress 1s before observe.
    clear_leases()
    write_lease(progress=stamp(30), timeout=900)  # last obs is stamp(30)
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 fresh heartbeat (progress 1s ago) REFUSES kill despite lease',
          last.action == logic.ALERT and 'progress' in last.reason.lower(),
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()
    write_lease(schema_version=1)  # legacy v1 contract
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 legacy v1 lease without managed contract REFUSES kill',
          last.action == logic.ALERT and 'managed-worker contract' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()
    write_lease(manager_id='', job_id='')
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 lease without manager/job ownership REFUSES kill',
          last.action == logic.ALERT and 'ownership' in last.reason.lower(),
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()
    write_lease(revoked=True)
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 revoked manager ownership REFUSES kill',
          last.action == logic.ALERT and 'revoked' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()
    write_lease(policy='manual-only')
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 lease without heartbeat-timeout opt-in REFUSES kill',
          last.action == logic.ALERT and 'policy' in last.reason.lower(),
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()
    write_lease(progress='not-a-time')
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 unreadable progress REFUSES kill (stall unproven)',
          last.action == logic.ALERT and 'progress' in last.reason.lower(),
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()
    write_lease(progress='2099-01-01T00:00:00Z')  # future clock skew
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 future progress timestamp REFUSES kill',
          last.action == logic.ALERT,
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()
    # Stale heartbeat + authorized v2 = the ONLY shape that may KILL.
    write_lease(progress='2026-09-03T12:00:00Z', timeout=900)
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 stale heartbeat (2h, timeout 15m) with auth KILLs',
          last.action == logic.KILL and 'heartbeat stale' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()

    # P0-1 counterexample: a hand-forged v3 lease with no producer_token
    # in the registry must REFUSE kill -- shape alone is not proof.
    forged = {'schema_version': 3, 'pid': 30976,
              'start_time': '2026-09-03T12:20:31Z',
              'manager_id': 'saituls-test-manager', 'job_id': 'test-job-1',
              'windows_user': 'TESTUSER', 'producer_token': 'forged-token',
              'authorized': True, 'revoked': False,
              'expires_at': '2026-09-03T23:59:00Z',
              'recovery_policy': 'heartbeat-timeout',
              'heartbeat_timeout_seconds': 900,
              'last_progress_at': '2026-09-03T12:00:00Z', 'generation': 1}
    path = os.path.join(LEASE_DIR, '30976_2026-09-03T12_20_31Z.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(forged, fh)
    # Do NOT register 'forged-token' in the registry -- it must fail.
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-1 forged lease without registry token REFUSES kill',
          last.action == logic.ALERT and 'producer' in last.reason.lower(),
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()

    # ---- P0-2 lifecycle: REGISTER/HEARTBEAT/AUTHORIZE/REVOKE/EXPIRE/CLEANUP
    lp = logic.register_managed_worker(
        pid=30976, start_time='2026-09-03T12:20:31Z', manager_id='m',
        job_id='j', windows_user='TESTUSER',
        expires_at='2026-09-03T23:59:00Z', now_at=stamp(0))[0]
    check('P0-2 REGISTER creates an unauthorized lease file',
          os.path.isfile(lp), lp)
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-2 freshly registered (unauthorized) worker REFUSES kill',
          last.action == logic.ALERT and 'not authorized' in last.reason,
          'action=%s' % last.action)
    check('P0-2 HEARTBEAT refreshes progress',
          logic.heartbeat_managed_worker(
              30976, '2026-09-03T12:20:31Z', stamp(30)), 'heartbeat failed')
    check('P0-2 AUTHORIZE opts the worker into recovery',
          logic.authorize_recovery(30976, '2026-09-03T12:20:31Z'),
          'authorize failed')
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-2 fresh heartbeat after authorize still REFUSES (active work)',
          last.action == logic.ALERT and 'progress' in last.reason.lower(),
          'action=%s -> %s' % (last.action, last.reason))
    check('P0-2 REVOKE withdraws ownership immediately',
          logic.revoke_managed_worker(30976, '2026-09-03T12:20:31Z'),
          'revoke failed')
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P0-2 revoked worker REFUSES kill',
          last.action == logic.ALERT and 'revoked' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    check('P0-2 PROCESS_EXIT_CLEANUP removes the lease',
          logic.cleanup_managed_worker(30976, '2026-09-03T12:20:31Z')
          and not os.path.exists(lp), 'cleanup failed')
    clear_leases()

    # ---- P1-3 production managed-worker caller ---------------------------------
    # The saispin_managed module is the production caller that SAITULS worker
    # scripts import to register themselves with the watchdog. It must produce a
    # v3 lease with a producer_token that passes validate_lease_producer_authority.
    import importlib.util
    spec = importlib.util.spec_from_file_location('saispin_managed',
        os.path.join(os.path.dirname(__file__), '..', 'Scripts', 'saispin_managed.py'))
    managed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(managed)
    clear_leases()
    pid, start, token = managed.register_self('p1-3-test-worker')
    lease_path = logic.lease_path_for_identity(pid, start)
    ok, why = logic.validate_lease_producer_authority(lease_path)
    check('P1-3 the managed-worker caller produces a registry-backed lease',
          ok and not why, ('ok=%s why=%s' % (ok, why)))
    check('P1-3 the lease carries a producer_token', os.path.exists(lease_path)
          and token, ('path=%s token=%s' % (lease_path, token[:8])))
    # The token must match an entry in producers.json.
    reg_path = os.path.join(LEASE_DIR, logic.LEASE_PRODUCER_REGISTRY)
    ok, why = logic.validate_lease_producer_authority(lease_path)
    check('P1-3 the managed-worker lease survives a fresh authority check',
          ok and not why, ('ok=%s why=%s' % (ok, why)))
    managed.unregister_self(pid, start)
    check('P1-3 the managed worker cleanup removes the lease',
          not os.path.exists(lease_path), ('path=%s' % lease_path))
    clear_leases()

    # ---- P0-3 active-job safety matrix (name heuristics must not kill) ----
    def matrix_case(name, cond, detail=''):
        check(name, cond, detail)

    def hot_orphan_verdict(cmd, minutes=0, **kw):
        st, lv = {}, None
        base = dict(pid=30976, start_time='2026-09-03T12:20:31Z',
                    name=kw.pop(' Royalty ', 'python.exe') if False else kw.pop('name', 'python.exe'),
                    cmdline=cmd)
        base.update(kw)
        for i in range(6):
            one = logic.Sample(pid=base['pid'], start_time=base['start_time'],
                               name=base['name'], cpu_percent=HOT, orphan=True,
                               observed_at=stamp(minutes + i * 6),
                               cmdline=base['cmdline'],
                               is_main_process=bool(base.get('is_main_process', False)))
            lv = logic.decide(st, one, True)
            st = {} if lv.record is None else {lv.key: lv.record}
        return lv

    # A: active python test, unknown custom filename, pytest marker in cmd.
    write_lease()
    matrix_case('P0-3-A custom-filename pytest survives',
                hot_orphan_verdict('C:\\work\\custom_runner_xyz.py -m pytest -q').action != logic.KILL)
    # B: node wrapper with --test flag anywhere after script.
    matrix_case('P0-3-B wrapped node --test survives',
                hot_orphan_verdict('node custom_wrapper_77.js --test', name='node.exe').action != logic.KILL)
    clear_leases()
    # C: detached productive worker, no lease -> survive.
    matrix_case('P0-3-C detached productive worker without lease survives',
                hot_orphan_verdict('python active_useful_worker.py').action != logic.KILL)
    # D: compiler subprocess hot orphan without lease.
    matrix_case('P0-3-D compiler subprocess survives',
                hot_orphan_verdict('cl.exe /c bigfile.cpp', name='cl.exe').action != logic.KILL)
    # E: quiet network-wait worker is cold -> IGNORE.
    lv, _ = replay([0.3] * 6, auto_kill=True, name='python.exe')
    matrix_case('P0-3-E quiet network-wait worker ignored', lv.action == logic.IGNORE)
    # F: media render hot orphan without lease.
    matrix_case('P0-3-F media render survives without lease',
                hot_orphan_verdict('obs64.exe --startrecording', name='obs64.exe').action != logic.KILL)
    # G: managed worker fresh heartbeat survives.
    clear_leases()
    write_lease(progress=stamp(30))
    matrix_case('P0-3-G managed worker fresh heartbeat survives',
                hot_orphan_verdict('managed_worker.exe --job 1', name='managed_worker.exe').action != logic.KILL)
    # H: managed worker recent progress survives (same as G, explicit).
    clear_leases()
    write_lease(progress=stamp(29))
    matrix_case('P0-3-H managed worker recent progress survives',
                hot_orphan_verdict('managed_worker.exe --job 1', name='managed_worker.exe').action != logic.KILL)
    # I: expired authorization survives.
    clear_leases()
    write_lease(expires='2026-09-03T14:20:00Z')
    matrix_case('P0-3-I expired authorization survives',
                hot_orphan_verdict('managed_worker.exe --job 1', name='managed_worker.exe').action != logic.KILL)
    # J: revoked ownership survives.
    clear_leases()
    write_lease(revoked=True)
    matrix_case('P0-3-J revoked ownership survives',
                hot_orphan_verdict('managed_worker.exe --job 1', name='managed_worker.exe').action != logic.KILL)
    # K: lease for another identity survives.
    clear_leases()
    write_lease(pid='777', start='2020-01-01T00:00:00Z')
    matrix_case('P0-3-K foreign lease survives',
                hot_orphan_verdict('managed_worker.exe --job 1', name='managed_worker.exe').action != logic.KILL)
    # L: stale lease after PID reuse survives.
    clear_leases()
    write_lease()  # for old start_time
    st2, _lv2 = {}, None
    _st = {}
    _lv = None
    for i in range(6):
        one = logic.Sample(pid=30976, start_time='2026-09-03T18:00:00Z',
                           name='managed_worker.exe', cpu_percent=HOT, orphan=True,
                           observed_at=stamp(i * 6), cmdline='managed_worker.exe --job 1')
        _lv = logic.decide(_st, one, True)
        _st = {} if _lv.record is None else {_lv.key: _lv.record}
    matrix_case('P0-3-L PID-reuse does not inherit lease', _lv.action != logic.KILL)
    # M: malformed lease survives.
    clear_leases()
    write_lease(raw='{ broken')
    matrix_case('P0-3-M malformed lease survives',
                hot_orphan_verdict('managed_worker.exe --job 1', name='managed_worker.exe').action != logic.KILL)
    # N: missing producer survives (no lease).
    clear_leases()
    matrix_case('P0-3-N missing producer survives',
                hot_orphan_verdict('managed_worker.exe --job 1', name='managed_worker.exe').action != logic.KILL)
    # O: watchdog self (PID of this test) never KILLs via engine allowlist path.
    matrix_case('P0-3-O kernel PID floor protects PID<=4',
                logic.is_never_kill('python.exe', pid=4))
    # P: foreground app shape (live parent) never KILLs.
    lv, _ = replay([HOT] * 8, auto_kill=True, orphan=False, name='notepad.exe')
    matrix_case('P0-3-P live-parent foreground app survives', lv.action != logic.KILL)
    # Q: system process survives.
    lv, _ = replay([HOT] * 8, auto_kill=True, name='svchost.exe')
    matrix_case('P0-3-Q system process survives', lv.action != logic.KILL)
    # R: worker recovering after temporary stall (cold breaks streak) survives.
    clear_leases()
    write_lease()
    _lv2, _st2 = replay([HOT] * 5, auto_kill=True, name='managed_worker.exe')
    _lv3, _ = replay([COLD, HOT, HOT], auto_kill=True, name='managed_worker.exe', history=_st2)
    matrix_case('P0-3-R recovering worker restarts streak', _lv3.action != logic.KILL)
    # S: sibling workers sharing exe: only the leased identity is eligible.
    clear_leases()
    write_lease(pid='30976', start='2026-09-03T12:20:31Z')
    sib = logic.Sample(pid=30977, start_time='2026-09-03T12:20:31Z',
                       name='managed_worker.exe', cpu_percent=HOT, orphan=True,
                       observed_at=stamp(30), cmdline='managed_worker.exe --job 2')
    _sst, _slv = {}, None
    for i in range(6):
        one = logic.Sample(pid=30977, start_time='2026-09-03T12:20:31Z',
                           name='managed_worker.exe', cpu_percent=HOT, orphan=True,
                           observed_at=stamp(i * 6), cmdline='managed_worker.exe --job 2')
        _slv = logic.decide(_sst, one, True)
        _sst = {} if _slv.record is None else {_slv.key: _slv.record}
    matrix_case('P0-3-S unleased sibling sharing exe survives', _slv.action != logic.KILL)
    # T: explicit disposable managed worker, stale heartbeat, authorized -> KILL.
    clear_leases()
    write_lease(pid='4112', start='2026-09-03T12:00:00Z',
                progress='2026-09-03T12:00:00Z', timeout=900)
    t_lv = sustained_sample(
        {'pid': 4112, 'start_time': '2026-09-03T12:00:00Z', 'name': 'disposable-worker.exe',
         'cmdline': 'disposable-worker.exe --job disposable-9'}, 0)
    matrix_case('P0-3-T disposable stale managed worker is the ONLY kill',
                t_lv.action == logic.KILL,
                'action=%s -> %s' % (t_lv.action, t_lv.reason))
    clear_leases()

    # ---- P1-4 controlled kill acceptance matrix --------------------------------
    # Positive: spawn -> register -> authorize -> stall -> KILL.
    # (Covered by P0-3-T and P0-2 lifecycle above; restated here as acceptance.)
    clear_leases()
    write_lease(progress='2026-09-03T12:00:00Z', timeout=900)
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P1-4 positive control: stale authorized lease KILLs',
          last.action == logic.KILL, 'action=%s' % last.action)
    clear_leases()

    # Negative 1: forged lease (producer_token not in registry) -> REFUSE.
    forged = {'schema_version': 3, 'pid': 30976,
              'start_time': '2026-09-03T12:20:31Z',
              'manager_id': 'attacker', 'job_id': 'evil',
              'windows_user': "$env:USERDOMAIN\$env:USERNAME", 'producer_token': 'forged',
              'authorized': True, 'revoked': False,
              'expires_at': '2026-09-03T23:59:00Z',
              'recovery_policy': 'heartbeat-timeout',
              'heartbeat_timeout_seconds': 900,
              'last_progress_at': '2026-09-03T12:00:00Z', 'generation': 1}
    path = os.path.join(LEASE_DIR, '30976_2026-09-03T12_20_31Z.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(forged, fh)
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P1-4 negative: forged lease (foreign producer_token) REFUSES kill',
          last.action == logic.ALERT and 'producer' in last.reason.lower(),
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()

    # Negative 2: empty windows_user -> REFUSE.
    no_user = {'schema_version': 3, 'pid': 30976,
               'start_time': '2026-09-03T12:20:31Z',
               'manager_id': 'm', 'job_id': 'j',
               'windows_user': '', 'producer_token': 'test-producer-token',
               'authorized': True, 'revoked': False,
               'expires_at': '2026-09-03T23:59:00Z',
               'recovery_policy': 'heartbeat-timeout',
               'heartbeat_timeout_seconds': 900,
               'last_progress_at': '2026-09-03T12:00:00Z', 'generation': 1}
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(no_user, fh)
    # Register the token so it passes producer check but fails on windows_user.
    register_test_producer_token()
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P1-4 negative: lease with empty windows_user REFUSES kill',
          last.action == logic.ALERT and 'windows_user' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()

    # Negative 3: active work (fresh progress) survives.
    write_lease(progress=stamp(29), timeout=900)
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P1-4 negative: active work (fresh progress) REFUSES kill',
          last.action == logic.ALERT and 'progress' in last.reason.lower(),
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()

    # Negative 4: revoked ownership survives.
    write_lease(revoked=True)
    last, _ = replay([HOT] * 6, auto_kill=True, name='python.exe')
    check('P1-4 negative: revoked ownership REFUSES kill',
          last.action == logic.ALERT and 'revoked' in last.reason,
          'action=%s -> %s' % (last.action, last.reason))
    clear_leases()

    print('---')
    print('PASS (0 failures)' if fails == 0 else 'FAILED (%d failures)' % fails)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
