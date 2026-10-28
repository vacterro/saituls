#!/usr/bin/env python3
"""Managed Antigravity: auto-connect intent, YOLO launch parity, console profile.

Run:  python tests/test_managed_console_context.py
Exit: 0 = all PASS, 1 = failures.

Companion to test_sai_accounts.py, which owns the registry/broker/migration
contract. This file owns the three defects that made managed AG2 behave like a
hostile fresh Windows account every run:

  1. CONNECT ONCE, THEN FORGET  -- auto_connect as persisted DESIRED state,
     separate from whether a broker happens to be alive right now. Items 1-12.
  2. MANAGED ANTIGRAVITY IS NOT YOLO -- the launch policy the local
     consoles.json profile always had, owned by the engine and unreachable by
     the caller. Items 13-18.
  3. CTRL+V / CLIPBOARD -- the managed account's own console defaults, written
     through the helper for exactly one SID and one fixed allowlist.
     Items 19-24.

Items 25-29 keep the clipboard fix scoped: no unscoped keyboard hook,
AutoHotkey, synthesized paste, hive damage or consent dialog. SRC-091's
explicit T-271 exception permits native commands only in the Antigravity
guard's own foreground console, preserving the input mode and account SID.

Reuses Plane/HelperServer/pipe_request from test_sai_accounts.py rather than
reimplementing the harness.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_sai_accounts import (  # noqa: E402
    ROOT, EXE, HELPER_EXE, Plane, HelperServer, Broker,
    pipe_request, pipe_open, pipe_write, pipe_read_line, check, build,
    FAILURES, PASSES,
)

ENGINE = os.path.join(ROOT, 'Scripts', 'accounts', 'SaiAccounts.cs')
HELPER_SRC = os.path.join(ROOT, 'Scripts', 'accounts', 'SaiAccountsHelper.cs')
GUI_SRC = os.path.join(ROOT, 'SAITULS.cs')
CONSOLES_JSON = os.path.join(ROOT, 'Scripts', 'consoles', 'consoles.json')
INSTALL_MENUS = os.path.join(ROOT, 'Installers', 'INSTALL_AI_AGENT_MENUS.PS1')

YOLO_FLAG = '--dangerously-skip-permissions'

# The console contract the managed account is allowed to receive, and nothing
# else. Mirrors ManagedConsoleDwords / ManagedConsoleSubkeys in the helper.
CONSOLE_ALLOWLIST = [
    'ForceV2', 'CtrlKeyShortcutsDisabled', 'ExtendedEditKey', 'QuickEdit',
    'InsertMode', 'FilterOnPaste', 'InterceptCopyPaste', 'LineSelection',
    'VirtualTerminalLevel',
]

FAKE_AGY_CS = r'''
using System;
using System.IO;
using System.Text;

// Stand-in for the real agy.exe. Records the exact command line it was started
// with, so the launch-contract oracle is the child itself rather than the
// engine's own idea of what it passed. Exit code encodes nothing: the engine
// launches it through ShellExecute, so only argv is observable.
public static class FakeAgy
{
    public static int Main(string[] args)
    {
        try
        {
            string dir = AppDomain.CurrentDomain.BaseDirectory;
            var sb = new StringBuilder();
            sb.Append(Environment.CommandLine).Append('\n');
            sb.Append("cwd=").Append(Directory.GetCurrentDirectory()).Append('\n');
            File.WriteAllText(Path.Combine(dir, "agy-argv.txt"), sb.ToString());

            // The environment this child was actually handed. A capture probe
            // and a sign-in window are supposed to receive DIFFERENT ones, and
            // the only witness worth trusting is the child itself.
            var ev = new StringBuilder();
            foreach (string name in new[] { "BROWSER", "AGY_CLI_HIDE_ACCOUNT_INFO",
                                            "AGY_CLI_DISABLE_AUTO_UPDATE", "GEMINI_API_KEY" })
            {
                string v = Environment.GetEnvironmentVariable(name);
                ev.Append(name).Append('=').Append(v == null ? "<unset>" : v).Append('\n');
            }
            File.WriteAllText(Path.Combine(dir, "agy-env.txt"), ev.ToString());
        }
        catch { }
        return 0;
    }
}
'''

CSC = os.path.join(os.environ.get('WINDIR', r'C:\Windows'),
                   'Microsoft.NET', 'Framework64', 'v4.0.30319', 'csc.exe')


def pipe_name_for(scope, sid):
    """The pipe the broker for `sid` registers -- same derivation as the engine."""
    fp = hashlib.sha256(sid.strip().lower().encode()).hexdigest()[:16]
    return 'SAI.Accounts.' + scope + '.' + fp


def install_fake_agy(plane):
    """Drop the argv-recording stand-in where SharedAgyPath() will look."""
    bindir = os.path.join(plane.dir, 'bin')
    os.makedirs(bindir, exist_ok=True)
    src = os.path.join(plane.dir, 'FakeAgy.cs')
    with open(src, 'w', encoding='utf-8') as fh:
        fh.write(FAKE_AGY_CS)
    target = os.path.join(bindir, 'agy.exe')
    r = subprocess.run([CSC, '/nologo', '/target:exe', '/out:' + target, src],
                       capture_output=True, text=True)
    return target if r.returncode == 0 and os.path.exists(target) else None


def read_agy_argv(plane, timeout_s=15.0):
    """The recorded child command line, or None if agy never ran."""
    path = os.path.join(plane.dir, 'bin', 'agy-argv.txt')
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    return fh.read()
            except (IOError, OSError):
                pass
        time.sleep(0.1)
    return None


def clear_agy_argv(plane):
    path = os.path.join(plane.dir, 'bin', 'agy-argv.txt')
    if os.path.exists(path):
        os.remove(path)


def read_agy_env(plane, timeout_s=15.0):
    """The environment the stand-in agy was started with, or None."""
    path = os.path.join(plane.dir, 'bin', 'agy-env.txt')
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    pairs = [l.split('=', 1) for l in fh.read().splitlines() if '=' in l]
                return dict(pairs)
            except (IOError, OSError):
                pass
        time.sleep(0.1)
    return None


def clear_agy_env(plane):
    path = os.path.join(plane.dir, 'bin', 'agy-env.txt')
    if os.path.exists(path):
        os.remove(path)


def source(path):
    with open(path, 'r', encoding='utf-8-sig', errors='replace') as fh:
        return fh.read()


def account_of(plane, acc_id):
    for a in plane.registry()['accounts']:
        if a['account_id'] == acc_id:
            return a
    return None


def managed_plane(scope='op-yolo', install_agy=True):
    """A plane with one real managed account created through the engine."""
    p = Plane(scope=scope)
    helper = HelperServer(p)
    ok = helper.start()
    if not ok:
        p.cleanup()
        raise RuntimeError('helper did not come up')
    if install_agy and not install_fake_agy(p):
        helper.stop()
        p.cleanup()
        raise RuntimeError('fake agy.exe did not build')
    code, out, _, _ = p.run('context', 'create', '--provider', 'antigravity',
                            '--alias', 'AG Managed')
    if code != 0 or not out or out.get('status') != 'created':
        helper.stop()
        p.cleanup()
        raise RuntimeError('context create failed: %r' % (out,))
    p.helper = helper
    p.managed_id = out['account_id']
    p.managed_sid = out['windows_sid']
    return p


def stop_broker(p, sid):
    name = pipe_name_for(p.scope, sid)
    try:
        pipe_request(name, {'cmd': 'shutdown'}, timeout_s=5)
    except Exception:
        pass
    # Wait for the pipe to actually go away: a broker that is on its way out
    # still has its name registered, which is exactly the stale-online failure
    # this suite exists to catch.
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if pipe_open(name, timeout_s=0.3):
                time.sleep(0.2)
                continue
        except Exception:
            return
        return
    return


# ============================================================ AUTO-CONNECT 1-12
def test_auto_connect_defaults():
    p = managed_plane(scope='op-ac-default', install_agy=False)
    try:
        # 1. A new managed account is born connected-by-intent.
        acc = account_of(p, p.managed_id)
        check('ac01-new-managed-defaults-auto-connect-true',
              acc.get('auto_connect') is True, str(acc)[:250])

        code, out, _, _ = p.run('get', p.managed_id)
        check('ac01b-list-get-exposes-auto-connect',
              code == 0 and out['account'].get('auto_connect') is True, str(out)[:200])
    finally:
        p.helper.stop()
        p.cleanup()


def test_auto_connect_migration():
    p = managed_plane(scope='op-ac-migrate', install_agy=False)
    try:
        # 2. An account that predates the field has expressed no preference, and
        #    absence is not a decision: it defaults on.
        raw = p.registry()
        stripped = 0
        for a in raw['accounts']:
            if a['account_id'] == p.managed_id:
                a.pop('auto_connect', None)
                stripped += 1
        p.write_registry(raw)
        code, out, _, _ = p.run('get', p.managed_id)
        check('ac02-absent-field-defaults-true-for-managed',
              stripped == 1 and code == 0 and out['account'].get('auto_connect') is True,
              str(out)[:250])
    finally:
        p.helper.stop()
        p.cleanup()


def test_auto_connect_explicit_false_is_a_decision():
    p = managed_plane(scope='op-ac-false', install_agy=False)
    try:
        # 3. Explicit false survives the read-time default. This is the only
        #    thing that reads as the operator having said no.
        raw = p.registry()
        for a in raw['accounts']:
            if a['account_id'] == p.managed_id:
                a['auto_connect'] = False
        p.write_registry(raw)
        code, out, _, _ = p.run('get', p.managed_id)
        check('ac03-explicit-false-honored',
              code == 0 and out['account'].get('auto_connect') is False, str(out)[:250])

        code, out, _, _ = p.run('autoconnect', '--budget-ms', '5000')
        rows = {r['account_id']: r for r in out.get('results', [])}
        row = rows.get(p.managed_id, {})
        check('ac03b-autoconnect-skips-explicit-false',
              row.get('outcome') == 'skipped' and row.get('reason') == 'auto_connect_disabled',
              str(row))
    finally:
        p.helper.stop()
        p.cleanup()


def test_connect_disconnect_persist():
    p = managed_plane(scope='op-ac-intent', install_agy=False)
    try:
        # 4. Connect records intent AND starts the broker.
        code, out, _, _ = p.run('context', 'connect', p.managed_id)
        started = out is not None and out.get('status') in ('launched', 'online', 'started')
        acc = account_of(p, p.managed_id)
        check('ac04-connect-persists-true', acc.get('auto_connect') is True, str(acc)[:200])
        check('ac04b-connect-actually-starts', code == 0 and started, str(out)[:250])
        pipe_name_for(p.scope, p.managed_sid)  # no-op: name derivation is covered below
        online = False
        try:
            pipe_open(pipe_name_for(p.scope, p.managed_sid), timeout_s=10)
            online = True
        except Exception:
            pass
        check('ac04c-broker-listening-after-connect', online, 'no pipe')

        # 5. Disconnect records intent AND stops the broker.
        code, out, _, _ = p.run('context', 'disconnect', p.managed_id)
        acc = account_of(p, p.managed_id)
        check('ac05-disconnect-persists-false', acc.get('auto_connect') is False, str(acc)[:200])
        stopped = True
        try:
            stop_broker(p, p.managed_sid)
            h = pipe_open(pipe_name_for(p.scope, p.managed_sid), timeout_s=1.0)
            kernel_close(h)
            stopped = False
        except Exception:
            pass
        check('ac05b-broker-gone-after-disconnect', stopped, 'pipe still answers')

        # And the intent survives a brand new process reading the same file --
        # the "close SAITULS, do not Disconnect, reopen" case, reduced to the
        # one thing a fresh process can disagree about.
        code, out, _, _ = p.run('autoconnect', '--budget-ms', '4000')
        rows = {r['account_id']: r for r in out.get('results', [])}
        check('ac05c-disconnected-account-not-resurrected',
              rows.get(p.managed_id, {}).get('outcome') == 'skipped', str(rows.get(p.managed_id)))

        # 6. Connect again restores it.
        code, out, _, _ = p.run('context', 'connect', p.managed_id)
        acc = account_of(p, p.managed_id)
        check('ac06-reconnect-persists-true-again', acc.get('auto_connect') is True, str(acc)[:200])
    finally:
        stop_broker(p, p.managed_sid)
        p.helper.stop()
        p.cleanup()


def kernel_close(handle):
    import ctypes
    ctypes.windll.kernel32.CloseHandle(handle)


def test_autoconnect_reconciliation():
    p = managed_plane(scope='op-ac-recon', install_agy=False)
    try:
        # 7. Reconciliation brings an offline, wanted account back on its own.
        code, out, _, _ = p.run('autoconnect', '--budget-ms', '60000')
        rows = {r['account_id']: r for r in out.get('results', [])}
        row = rows.get(p.managed_id, {})
        check('ac07-autoconnect-starts-offline-wanted-account',
              code == 0 and row.get('outcome') == 'started' and out.get('started') == 1,
              str(out)[:400])
        check('ac07b-autoconnect-exit-zero-when-all-good', code == 0, str(out)[:200])

        # 8. Idempotence: a second pass finds it alive and leaves it alone.
        code, out2, _, _ = p.run('autoconnect', '--budget-ms', '60000')
        rows2 = {r['account_id']: r for r in out2.get('results', [])}
        row2 = rows2.get(p.managed_id, {})
        check('ac08-second-pass-reports-already-online',
              code == 0 and row2.get('outcome') == 'already_online'
              and out2.get('already_online') == 1 and out2.get('started') == 0,
              str(out2)[:400])

        # 9. Kill only this broker; the next pass brings it back. Whatever else
        #    exists in the plane is untouched.
        stop_broker(p, p.managed_sid)
        code, out3, _, _ = p.run('autoconnect', '--budget-ms', '60000')
        rows3 = {r['account_id']: r for r in out3.get('results', [])}
        check('ac09-autoconnect-restarts-a-dead-broker',
              rows3.get(p.managed_id, {}).get('outcome') == 'started', str(out3)[:400])
    finally:
        stop_broker(p, p.managed_sid)
        p.helper.stop()
        p.cleanup()


def run_pair(p, *args):
    """Two autoconnect sweeps fired at the same moment, like the GUI timer
    racing the background keeper. Returns both (code, payload) pairs."""
    outs = [None, None]

    def one(i):
        code, payload, _, _ = p.run(*args)
        outs[i] = (code, payload)
    threads = [threading.Thread(target=one, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    return outs


def test_autoconnect_concurrent_sweeps():
    p = managed_plane(scope='op-ac-race', install_agy=False)
    try:
        # Warm: the wanted account is online before the race starts.
        code, out, _, _ = p.run('autoconnect', '--budget-ms', '60000')
        check('race00-warmup-online', code == 0, str(out)[:200])
        gen0 = p.registry()['generation']

        # A. Two sweeps over a healthy plane: nothing starts, nothing fails,
        #    the loser reports deduplicated instead of probing or spawning.
        (c1, o1), (c2, o2) = run_pair(p, 'autoconnect', '--budget-ms', '60000')
        rows1 = {r['account_id']: r for r in (o1 or {}).get('results', [])}
        rows2 = {r['account_id']: r for r in (o2 or {}).get('results', [])}
        ok_outcomes = True
        for o, rows in ((o1, rows1), (o2, rows2)):
            row = rows.get(p.managed_id, {})
            if row.get('outcome') == 'already_online':
                continue
            if not rows and o.get('deduplicated') is True:
                continue
            ok_outcomes = False
        check('raceA-concurrent-sweeps-start-nothing',
              c1 == 0 and c2 == 0
              and (o1 or {}).get('started', 9) == 0 and (o2 or {}).get('started', 9) == 0
              and (o1 or {}).get('failed', 9) == 0 and (o2 or {}).get('failed', 9) == 0
              and ok_outcomes,
              '%r / %r' % (o1, o2)[:400])
        check('raceAb-race-writes-no-registry',
              p.registry()['generation'] == gen0
              and account_of(p, p.managed_id).get('auto_connect') is True,
              'generation moved or intent moved')

        # B. Kill the broker, race again: exactly one sweep restores it.
        #    (started, deduplicated) when they overlap, (started,
        #    already_online) when they serialize -- both are one broker.
        stop_broker(p, p.managed_sid)
        (c1, o1), (c2, o2) = run_pair(p, 'autoconnect', '--budget-ms', '60000')
        total_started = (o1 or {}).get('started', 9) + (o2 or {}).get('started', 9)
        total_failed = (o1 or {}).get('failed', 9) + (o2 or {}).get('failed', 9)
        try:
            pipe_open(pipe_name_for(p.scope, p.managed_sid), timeout_s=10)
            listening = True
        except Exception:
            listening = False
        check('raceB-crashed-broker-restored-exactly-once',
              c1 == 0 and c2 == 0 and total_started == 1 and total_failed == 0
              and listening,
              '%r / %r' % (o1, o2)[:400])
        check('raceBb-restore-writes-no-registry',
              p.registry()['generation'] == gen0, 'generation moved')

        # C. auto_connect == false: neither sweep may resurrect it.
        p.run('context', 'disconnect', p.managed_id)
        gen1 = p.registry()['generation']
        (c1, o1), (c2, o2) = run_pair(p, 'autoconnect', '--budget-ms', '60000')
        rows1 = {r['account_id']: r for r in (o1 or {}).get('results', [])}
        rows2 = {r['account_id']: r for r in (o2 or {}).get('results', [])}
        check('raceC-disconnected-account-never-restored',
              (o1 or {}).get('started', 9) == 0 and (o2 or {}).get('started', 9) == 0
              and rows1.get(p.managed_id, {}).get('outcome', 'deduplicated') in ('skipped', 'deduplicated')
              and rows2.get(p.managed_id, {}).get('outcome', 'deduplicated') in ('skipped', 'deduplicated'),
              '%r / %r' % (o1, o2)[:400])
        check('raceCb-skip-writes-no-registry',
              p.registry()['generation'] == gen1
              and account_of(p, p.managed_id).get('auto_connect') is False,
              'generation moved or intent moved')

        # D. Every run above left one bounded log line: timestamp plus counts,
        #    and no secret-shaped material.
        log = os.path.join(p.dir, 'logs', 'keeper-' + p.scope + '.log')
        lines = []
        try:
            with open(log, 'r', encoding='utf-8') as fh:
                lines = [l for l in fh.read().splitlines() if l.strip()]
        except (IOError, OSError):
            pass
        check('raceD-keeper-log-records-runs',
              len(lines) >= 5 and all('started=' in l for l in lines),
              '%s lines=%d' % (log, len(lines)))
        check('raceDb-keeper-log-carries-no-secrets',
              not any(s in ' '.join(lines).lower()
                      for s in ('password', 'token', 'secret', 'credential', 'apikey')),
              lines[-1][:200] if lines else 'no log')
    finally:
        stop_broker(p, p.managed_sid)
        p.helper.stop()
        p.cleanup()


def test_autoconnect_lifecycle_and_scope():
    p = managed_plane(scope='op-ac-life', install_agy=False)
    try:
        # A current-user account in the same registry: it has no separate context
        # and reconciliation must never invent one.
        p.legacy('user', [ag_account_local(p)])
        p.run('import')

        # 10. Lifecycle is read before intent, so a non-ENABLED account reports
        #     the reason the operator set.
        for state in ('DISABLED', 'FROZEN', 'ARCHIVED'):
            p.run('context', 'disconnect', p.managed_id)
            code, _, _, _ = p.run('freeze', p.managed_id)
            raw = p.registry()
            for a in raw['accounts']:
                if a['account_id'] == p.managed_id:
                    a['operational_state'] = state
                    a['auto_connect'] = True
            p.write_registry(raw)
            code, out, _, _ = p.run('autoconnect', '--budget-ms', '4000')
            rows = {r['account_id']: r for r in out.get('results', [])}
            row = rows.get(p.managed_id, {})
            check('ac10-lifecycle-%s-skipped' % state.lower(),
                  row.get('outcome') == 'skipped'
                  and row.get('reason') == 'lifecycle_' + state.lower(), str(row))

        # Put it back so later assertions read a sane registry.
        raw = p.registry()
        for a in raw['accounts']:
            if a['account_id'] == p.managed_id:
                a['operational_state'] = 'ENABLED'
                a['auto_connect'] = True
        p.write_registry(raw)

        # 11. The operator's own account is never a managed context, so there is
        #     nothing to reconcile and nothing to start for it. Import also pulls
        #     in the operator's real Claude/Codex profile directories; none of
        #     them is a managed Windows account either, so none may be started.
        code, out, _, _ = p.run('autoconnect', '--budget-ms', '4000')
        rows = {r['account_id']: r for r in out.get('results', [])}
        current_user = None
        for a in p.registry()['accounts']:
            if (a.get('provider_metadata') or {}).get('windows_sid', '').lower() == p.sid.lower():
                current_user = a['account_id']
        others = {a: r for a, r in rows.items() if a != p.managed_id}
        check('ac11-current-user-account-not-started',
              current_user is not None
              and rows.get(current_user, {}).get('outcome') == 'skipped'
              and rows[current_user].get('reason') == 'no_managed_context',
              str(rows.get(current_user)) + ' sid=' + p.sid)
        check('ac11b-no-unmanaged-account-was-started',
              not any(r.get('outcome') == 'started' for a, r in others.items()
                      if a != p.managed_id),
              str({a: r.get('reason') for a, r in others.items()
                   if a != p.managed_id})[:400])
    finally:
        stop_broker(p, p.managed_sid)
        p.helper.stop()
        p.cleanup()


def ag_account_local(plane):
    return {'id': 'antigravity:windows-user:' + plane.sid, 'backend': 'windows_user',
            'windows_sid': plane.sid, 'provider': 'antigravity', 'label': 'Antigravity',
            'compact_label': 'AG1', 'windows_user': os.environ.get('USERNAME', 'operator'), 'enabled': True}


def test_launch_does_not_re_enable_disconnected():
    p = managed_plane(scope='op-ac-launch')
    try:
        # 12. An explicit launch after Disconnect must work, and must not quietly
        #     turn the account back on. Intent and runtime are different fields.
        p.run('context', 'disconnect', p.managed_id)
        code, out, _, _ = p.run('launch', p.managed_id, '--workdir', p.dir)
        launched = out is not None and out.get('status') == 'launched'
        acc = account_of(p, p.managed_id)
        check('ac12-launch-after-disconnect-still-works', code == 0 and launched, str(out)[:250])
        check('ac12b-launch-left-auto-connect-false',
              acc.get('auto_connect') is False, str(acc)[:200])

        # And the launcher never falls back to the operator's own account.
        check('ac12c-launch-reported-the-requested-account',
              out is not None and out.get('account_id') == p.managed_id, str(out)[:200])
    finally:
        stop_broker(p, p.managed_sid)
        p.helper.stop()
        p.cleanup()


def test_autoconnect_budget_and_empty():
    p = Plane(scope='op-ac-empty')
    try:
        # An absent registry is a valid state, not a crash.
        code, out, _, _ = p.run('autoconnect')
        check('ac13-empty-registry-is-empty-not-error',
              code == 0 and out.get('status') == 'empty', str(out)[:200])

        p.legacy('user', [ag_account_local(p)])
        p.run('import')
        code, out, _, _ = p.run('autoconnect', '--budget-ms', '1')
        check('ac14-budget-bounded-and-reported',
              code in (0, 2) and isinstance(out.get('elapsed_ms'), int)
              and out.get('status') in ('ok', 'partial'), str(out)[:250])
    finally:
        p.cleanup()


# ==================================================== DESIRED-STATE INVARIANT
# The single rule this file exists to defend: a runtime failure never writes
# the desired state. Everything below drives a real failure at a real managed
# account and then asks the registry whether the operator's intent moved. Only
# the two intent verbs may change it.
def test_runtime_failure_never_writes_desired_state():
    p = managed_plane(scope='op-ac-invariant', install_agy=False)
    try:
        p.run('context', 'connect', p.managed_id)
        acc = account_of(p, p.managed_id)
        check('inv01-connect-writes-true', acc.get('auto_connect') is True, str(acc)[:200])
        check('inv02-connect-records-its-reason',
              acc.get('auto_connect_reason') == 'operator_connect', str(acc)[:220])
        before = json.dumps(acc, sort_keys=True)

        # A broker that is not there, a helper that cannot start it, an
        # account with no credential, and a workdir that does not exist: four
        # different failures, one shared requirement -- intent is untouched.
        p.run('autoconnect', '--budget-ms', '2000')
        p.run('status', p.managed_id)
        p.run('launch', p.managed_id, '--workdir', os.path.join(p.dir, 'no-such-dir'))
        p.run('context', 'stop', p.managed_id)

        acc = account_of(p, p.managed_id)
        check('inv03-crash-and-startup-failure-leave-intent-alone',
              acc.get('auto_connect') is True, str(acc)[:200])
        check('inv04-auth-and-workdir-failure-leave-intent-alone',
              json.dumps(acc, sort_keys=True) == before,
              'registry changed under runtime failures')

        # 13. Lifecycle is its own axis: freezing the account must not be
        #     mistaken for an opinion about its connection preference.
        p.run('set-state', p.managed_id, 'FROZEN')
        acc = account_of(p, p.managed_id)
        check('inv05-freeze-does-not-rewrite-auto-connect',
              acc.get('operational_state') == 'FROZEN' and acc.get('auto_connect') is True,
              str(acc)[:220])
        check('inv06-freeze-does-not-forge-a-reason',
              acc.get('auto_connect_reason') == 'operator_connect', str(acc)[:220])
        p.run('set-state', p.managed_id, 'ENABLED')

        # 11/12. The two verbs are the only writers, and each records itself.
        p.run('context', 'disconnect', p.managed_id)
        acc = account_of(p, p.managed_id)
        check('inv07-disconnect-writes-false',
              acc.get('auto_connect') is False, str(acc)[:200])
        check('inv08-disconnect-names-the-hand-that-did-it',
              acc.get('auto_connect_reason') == 'operator_disconnect', str(acc)[:220])

        # An account nobody ever touched reads as the default, not as a
        # decision: absence must not be dressed up as a refusal.
        raw = p.registry()
        for a in raw['accounts']:
            if a['account_id'] == p.managed_id:
                del a['auto_connect']
                del a['auto_connect_reason']
        p.write_registry(raw)
        code, out, _, _ = p.run('get', p.managed_id)
        got = (out or {}).get('account') or {}
        check('inv09-absent-means-default-not-a-decision',
              got.get('auto_connect') is True and got.get('auto_connect_reason') == 'default',
              str(got)[:220])
    finally:
        p.helper.stop()
        p.cleanup()


def test_provenance_and_clock():
    p = managed_plane(scope='op-ac-prov', install_agy=False)
    try:
        # An account written before provenance existed is answered from the
        # append-only audit log, which already recorded which verb wrote each
        # generation. That is evidence; guessing would not be.
        p.run('context', 'disconnect', p.managed_id)
        raw = p.registry()
        for a in raw['accounts']:
            if a['account_id'] == p.managed_id:
                a.pop('auto_connect_reason', None)
                a.pop('auto_connect_changed_at', None)
        p.write_registry(raw)
        code, out, _, _ = p.run('get', p.managed_id)
        got = (out or {}).get('account') or {}
        check('prov01-legacy-false-is-read-from-the-audit-log',
              got.get('auto_connect_reason') == 'operator_disconnect', str(got)[:250])

        # A false nobody ever wrote must NOT be reported as somebody's choice.
        raw = p.registry()
        for a in raw['accounts']:
            if a['account_id'] == p.managed_id:
                a['auto_connect'] = False
        p.write_registry(raw)
        os.remove(os.path.join(p.dir, 'scopes', p.scope, 'events.log'))
        code, out, _, _ = p.run('get', p.managed_id)
        got = (out or {}).get('account') or {}
        check('prov02-unprovable-false-says-so',
              got.get('auto_connect') is False and got.get('auto_connect_reason') == 'unknown',
              str(got)[:250])

        # The account's own clock has to move with its own state. It did not,
        # and a stale updated_at is what made a real change look unexplained.
        p.run('context', 'connect', p.managed_id)
        acc = account_of(p, p.managed_id)
        stamp = acc.get('auto_connect_changed_at')
        code, out, _, _ = p.run('get', p.managed_id)
        updated = ((out or {}).get('account') or {}).get('updated_at')
        check('prov03-desired-state-write-moves-updated_at',
              bool(updated) and updated >= acc.get('created_at', ''),
              'updated_at=%r created_at=%r' % (updated, acc.get('created_at')))
        check('prov04-desired-state-write-stamps-when-it-happened',
              bool(stamp) and stamp >= acc.get('created_at', ''), 'changed_at=%r' % stamp)
    finally:
        p.helper.stop()
        p.cleanup()


# ==================================================================== YOLO 13-18
def test_yolo_current_user_launch():
    p = Plane(scope='op-yolo-cur')
    try:
        agy = install_fake_agy(p)
        if not agy:
            check('yolo13-current-user-launch-carries-yolo', False, 'fake agy.exe did not build')
            return
        fixture_exe = os.path.join(p.dir, 'credential-launch-fixture.exe')
        compiled = subprocess.run([
            CSC, '/nologo', '/target:exe', '/main:CredentialProbeFixture',
            '/r:System.dll', '/r:System.Core.dll', '/r:System.Web.Extensions.dll',
            '/out:' + fixture_exe, ENGINE,
            os.path.join(ROOT, 'tests', 'credential_probe_fixture.cs'),
        ], capture_output=True, text=True)
        check('yolo13-fixture-builds', compiled.returncode == 0, compiled.stdout + compiled.stderr)
        if compiled.returncode:
            return
        p.executable = fixture_exe
        p.legacy('user', [ag_account_local(p)])
        p.run('import')
        acc_id = p.registry()['accounts'][0]['account_id']

        clear_agy_argv(p)
        p.env['SAI_FIXTURE_CREDENTIAL_PRESENT'] = '0'
        denied_code, denied, _, _ = p.run('launch', acc_id, '--workdir', p.dir)
        check('yolo13-authentication-is-required',
              denied_code != 0 and denied.get('status') == 'AUTH_REQUIRED'
              and not os.path.exists(os.path.join(p.dir, 'bin', 'agy-argv.txt')), str(denied))
        p.env['SAI_FIXTURE_CREDENTIAL_PRESENT'] = '1'
        code, out, _, _ = p.run('launch', acc_id, '--workdir', p.dir)
        argv = read_agy_argv(p)
        check('yolo13-current-user-launch-carries-yolo',
              code == 0 and out.get('status') == 'launched' and argv is not None
              and YOLO_FLAG in argv, (argv or json.dumps(out))[:1000])
        check('yolo13b-launch-policy-reported', out.get('launch_policy') == YOLO_FLAG,
              str(out)[:200])
        check('yolo13c-exact-workdir-survives',
              argv is not None and ('cwd=' + os.path.realpath(p.dir).lower()) in argv.lower(),
              (argv or '')[:300])
        # Exactly the fixed set: no caller-shaped extras rode along.
        check('yolo13d-no-extra-arguments',
              argv is not None and argv.strip().splitlines()[0].count('--') == 1, (argv or '')[:300])
    finally:
        p.cleanup()


def test_yolo_managed_broker_launch():
    p = managed_plane(scope='op-yolo-mgd')
    try:
        if not os.path.exists(os.path.join(p.dir, 'bin', 'agy.exe')):
            install_fake_agy(p)

        # 14. The managed route, through a real broker process.
        code, out, _, _ = p.run('context', 'start', p.managed_id)
        check('yolo14-managed-broker-started', code == 0 and out is not None
              and out.get('status') in ('launched', 'online', 'started'), str(out)[:250])

        clear_agy_argv(p)
        name = pipe_name_for(p.scope, p.managed_sid)
        _, listed, _, _ = p.run('list', '--all')
        managed_row = next(row for row in listed['accounts'] if row['account_id'] == p.managed_id)
        identity = {'account_id': managed_row['account_id'], 'compact_label': managed_row['compact_label']}
        deadline = time.time() + 15
        reply = None
        while time.time() < deadline:
            try:
                reply = pipe_request(name, dict(identity, cmd='launch', workdir=p.dir), timeout_s=8)
                break
            except Exception:
                time.sleep(0.2)
        argv = read_agy_argv(p)
        check('yolo14b-managed-launch-carries-yolo',
              reply is not None and reply.get('status') == 'launched'
              and argv is not None and YOLO_FLAG in argv,
              str(reply)[:200] + ' / ' + (argv or 'no argv')[:300])

        # 15. A caller cannot add to, trim, or swap the fixed set. `arguments` is
        #     not a name the broker denies -- it simply has no reader, which is
        #     the stronger property: there is nothing to deny.
        clear_agy_argv(p)
        reply2 = None
        try:
            reply2 = pipe_request(name, {
                'cmd': 'launch', 'workdir': p.dir,
                'account_id': identity['account_id'], 'compact_label': identity['compact_label'],
                'arguments': '--safe-mode',
                'launch_policy': '',
            }, timeout_s=8)
        except Exception as exc:
            reply2 = {'status': 'exception: %s' % exc}
        argv2 = read_agy_argv(p)
        check('yolo15-caller-cannot-override-fixed-argument-set',
              reply2 is not None and reply2.get('status') == 'launched'
              and reply2.get('launch_policy') == YOLO_FLAG
              and argv2 is not None
              and YOLO_FLAG in argv2
              and '--safe-mode' not in argv2,
              str(reply2)[:200] + ' / ' + (argv2 or 'no argv')[:300])

        # 16. Arbitrary-command fields stay refused on the broker.
        refused = []
        for field in ('exe', 'program', 'command', 'command_line', 'script', 'binary'):
            try:
                rr = pipe_request(name, {'cmd': 'launch', 'workdir': p.dir,
                                         field: 'calc.exe'}, timeout_s=5)
                refused.append((field, (rr or {}).get('status')))
            except Exception as exc:
                refused.append((field, 'exc:%s' % exc))
        check('yolo16-broker-still-refuses-arbitrary-execution-fields',
              all(s == 'OPERATION_NOT_ALLOWED' for _, s in refused), str(refused))

        clear_agy_argv(p)
        mismatched = pipe_request(name, dict(identity, cmd='launch', workdir=p.dir,
                                            account_id='antigravity:windows-user:' + '0' * 16), timeout_s=5)
        check('title-managed-broker-refuses-identity-crossover',
              mismatched.get('status') == 'LAUNCH_IDENTITY_REQUIRED' and read_agy_argv(p, timeout_s=0.2) is None,
              str(mismatched))
        invalid_label = pipe_request(name, dict(identity, cmd='launch', workdir=p.dir,
                                               compact_label='AG2 -- calc.exe'), timeout_s=5)
        check('title-label-cannot-inject-guard-arguments',
              invalid_label.get('status') == 'LAUNCH_IDENTITY_REQUIRED', str(invalid_label))
    finally:
        stop_broker(p, p.managed_sid)
        p.helper.stop()
        p.cleanup()


def test_yolo_engine_policy_contract():
    src = source(ENGINE)
    # 17. The flag lives in the engine as a fixed set, applied at both Antigravity
    #     launch sites, and reachable from neither a request field nor a CLI flag.
    check('yolo17-engine-owns-fixed-flag',
          'AntigravityInteractiveArgs' in src and YOLO_FLAG in src
          and len(re.findall(r'AntigravityLaunch\(workdir,', src)) == 2,
          'launch policy not centralized')
    check('yolo17b-no-cli-argument-pass-through',
          not re.search(r'Get\(args,\s*"--args|Environment\.GetEnvironmentVariable\([^)]*ARGS',
                        src),
          'engine exposes an argument pass-through')
    check('yolo17c-both-managed-and-canonical-routes-share-one-policy',
          src.count('AntigravityLaunch(') == 3 and src.count('AntigravityArgumentLine()') >= 3,
          'AntigravityLaunch sites: %d' % src.count('AntigravityLaunch('))

    # 18. The Explorer route and the canonical route are not two policies.
    #     The local profile carries the flag; the federated template carries no
    #     command at all, because its executable and arguments come from this
    #     engine -- which is why a right-click cannot drift from Connect.
    with open(CONSOLES_JSON, encoding='utf-8-sig') as fh:
        doc = json.load(fh)
    profiles = {p.get('id'): p for p in doc['profiles']}
    agy = profiles.get('antigravity', {})
    shared = profiles.get('shared-antigravity', {})
    check('yolo18-local-profile-keeps-yolo', YOLO_FLAG in (agy.get('arguments') or []),
          str(agy.get('arguments')))
    check('yolo18b-federated-profile-defers-to-the-engine',
          bool(shared.get('expand_accounts'))
          and not shared.get('command') and not shared.get('arguments'),
          str(shared)[:250])

    # 19. Claude and Codex keep their existing YOLO defaults, untouched.
    claude = [profiles[k] for k in profiles if profiles[k].get('provider') == 'claude'
              and profiles[k].get('command')]
    codex = [profiles[k] for k in profiles if profiles[k].get('provider') == 'codex'
             and profiles[k].get('command')]
    check('yolo19-claude-codex-policy-unchanged',
          claude and codex
          and all(YOLO_FLAG in (x.get('arguments') or []) for x in claude)
          and all('--dangerously-bypass-approvals-and-sandbox' in (x.get('arguments') or [])
                  for x in codex),
          'claude=%s codex=%s' % ([x.get('arguments') for x in claude],
                                  [x.get('arguments') for x in codex]))


# ========================================================= CONSOLE PROFILE 19-24
def test_console_profile_contract():
    src = source(HELPER_SRC)
    # 20. The op takes a SID and a fixed allowlist. No request field can widen it
    #     into a registry API, because no field is read beyond target_sid.
    block = op_block(src, 'managed_console_profile')
    check('con20-op-reads-only-target-sid',
          block is not None and 'target_sid' in block
          and not re.search(r'Get\(req,\s*"(key|subkey|value|value_name|path|reg_key)"', block),
          (block or 'op not found')[:300])

    # 21. Exactly the nine allowlisted values, nothing else writable.
    dwords = re.search(r'ManagedConsoleDwords\s*=\s*\{([^}]*)\}', src)
    names = re.findall(r'"([^"]+)"', dwords.group(1)) if dwords else []
    check('con21-allowlist-is-exactly-the-contract',
          names == CONSOLE_ALLOWLIST, str(names))
    # T-265: the input-language mirror is a second, separately-checked writer
    # (con26 below). Its value names come from the OPERATOR'S hive behind strict
    # patterns, so it is cut out of the console-allowlist scan rather than being
    # allowed to widen it.
    mirror = helper_member(src, 'static string MirrorOperatorInputLanguages(')
    console_src = src.replace(mirror, '') if mirror else src
    setters = re.findall(r'\.SetValue\(\s*([A-Za-z_][A-Za-z0-9_]*)', console_src)
    variable_setters = [s for s in setters if s not in CONSOLE_ALLOWLIST]
    # The one variable value-name is the loop over the compile-time allowlist.
    # That is the whole design: no name reaches SetValue except from a
    # constant, so no request field can become a value to write.
    check('con21b-no-value-name-outside-the-constant-allowlist',
          set(variable_setters) <= {'name'}
          and re.search(r'foreach \(string name in ManagedConsoleDwords\)', src),
          str(sorted(set(variable_setters))))

    # 21c. T-265: CtrlKeyShortcutsDisabled is the inverted switch. Written as 1
    #      it turned Ctrl+C / Ctrl+V / Ctrl+A off in every managed console.
    value_fn = helper_member(src, 'static int ManagedConsoleValue(')
    check('con21c-ctrl-key-shortcuts-stay-enabled',
          value_fn is not None
          and re.search(r'"CtrlKeyShortcutsDisabled",\s*StringComparison\.Ordinal\)\s*\?\s*0\s*:\s*1', value_fn)
          and 'int want = ManagedConsoleValue(name);' in src
          and 'k.SetValue(name, want, RegistryValueKind.DWord)' in src
          and 'k.SetValue(name, 1, RegistryValueKind.DWord)' not in src,
          (value_fn or 'ManagedConsoleValue missing')[:300])

    # 26. T-265: the input-language mirror reads the OPERATOR'S hive only, never
    #     the request, and every written value passes a strict pattern first.
    check('con26-language-mirror-reads-operator-hive-only',
          mirror is not None
          and 'Registry.Users.OpenSubKey(operatorSid)' in mirror
          and not re.search(r'\breq\b', mirror)
          and 'S-1-5-21-' in mirror,
          (mirror or 'mirror missing')[:300])
    check('con26b-language-values-are-pattern-gated',
          mirror is not None
          and all(p in mirror for p in ('KeyboardLayoutId.IsMatch', 'KeyboardToggleValue.IsMatch',
                                        'LanguageTag.IsMatch', 'InputMethodId.IsMatch'))
          and set(re.findall(r'\.SetValue\(\s*([A-Za-z_][A-Za-z0-9_.]*)', mirror)) <= {'slot', 't', 'im.Key'},
          str(set(re.findall(r'\.SetValue\(\s*([A-Za-z_][A-Za-z0-9_.]*)', mirror or ''))))
    check('con26c-language-mirror-runs-with-the-console-profile',
          'Set(resp, "input_languages", MirrorOperatorInputLanguages(operatorSid, userKey));' in src,
          'mirror is not invoked from EnsureManagedConsoleProfile')

    # 22. Subkeys are the default Console profile plus the derived per-exe key.
    subkeys = re.search(r'ManagedConsoleSubkeys\s*=\s*\{([^}]*)\}', src)
    subs = re.findall(r'"([^"]+)"', subkeys.group(1)) if subkeys else []
    check('con22-default-profile-plus-per-exe-subkey',
          len(subs) == 2 and subs[0] == 'Console' and subs[1].endswith('agy.exe'), str(subs))
    callers = [c for c in re.findall(r'EnsureManagedConsoleProfile\(([^;]*?)\);', src, re.S)
               if not c.strip().startswith('string')]
    check('con22b-per-exe-key-derived-from-the-trusted-path-only',
          src.count('ConsoleSubkeyFor(') == 3  # definition + vendor + actual guard image
          and 'ConsoleSubkeyFor(BrokerExePath())' in src
          and len(callers) == 3  # pre-logon, post-logon retry, explicit repair
          and all('SharedAgyPath()' in c for c in callers),
          str(callers))

    # 23. A hive this operation did not load is never unloaded.
    unload = re.search(r'if\s*\(loadedHere\)\s*(?:\{[^}]*\}|[^\n]*RegUnLoadKeyW)', src, re.S)
    check('con23-unload-only-what-this-operation-loaded',
          unload is not None and src.count('RegUnLoadKeyW') == 2,  # decl + the guarded call
          'RegUnLoadKeyW call sites: %d' % src.count('RegUnLoadKeyW'))

    # 23b. The managed hive is opened WITH the rights the write needs. The
    #      framework's OpenBaseKey and the one-argument OpenSubKey hand back
    #      read-plus-create-subkey only: enough to find HKU\<sid> and to create
    #      Console\ beneath it, then UnauthorizedAccessException at SetValue.
    #      The rights-taking OpenBaseKey overload does not exist on .NET 4.0,
    #      which is what this helper targets -- so the handle is taken directly.
    #      The hermetic helper never reaches this line: the synthetic test SID
    #      has no ProfileList entry and returns PROFILE_NOT_FOUND first. A
    #      read-only handle is exactly the kind of defect that survives a green
    #      suite and lands on the live machine.
    opener = re.search(r'RegOpenKeyExW\(HKEY_USERS, targetSid, 0, (\w+), out h\)', src)
    check('con23b-managed-hive-opened-for-write',
          opener is not None and opener.group(1) == 'ManagedHiveRights'
          and 'SafeRegistryHandle(h, true)' in src,
          'opener: %s' % (opener.group(0) if opener else 'absent'))
    rights = re.search(r'const int ManagedHiveRights\s*=\s*([^;]*);', src)
    check('con23c-write-rights-requested-explicitly',
          rights is not None
          and '0x20019' in rights.group(1) and '0x20006' in rights.group(1)
          and 'KEY_READ' in src and 'KEY_WRITE' in src,
          (rights.group(1) if rights else 'no rights constant')[:200])
    check('con23d-no-default-rights-open-in-the-write-path',
          not re.search(r'OpenBaseKey\(RegistryHive\.Users[^,)]*\)', src)
          and not re.search(r'hku\.OpenSubKey\(targetSid\)', src),
          'a read-only hive open is still present')

    # 24. The allowlist mirrors the operator-side installer, so managed and local
    #     consoles end up with the same contract.
    menus = source(INSTALL_MENUS)
    mirrored = [n for n in CONSOLE_ALLOWLIST if n in menus]
    check('con24-matches-the-operator-side-profile',
          len(mirrored) >= 7, 'only %d of %d mirrored' % (len(mirrored), len(CONSOLE_ALLOWLIST)))


def op_block(src, op_name):
    m = re.search(r'if \(op == "%s"\)(.*?)\n        \}' % re.escape(op_name), src, re.S)
    return m.group(1) if m else None


def helper_member(src, signature):
    """One member of the helper (4-space class indentation): from its
    signature to the next class-level member or section comment."""
    i = src.find(signature)
    if i < 0:
        return None
    ends = [e for e in (src.find(m, i + len(signature))
                        for m in ('\n    static ', '\n    public ', '\n    private ', '\n    // ---'))
            if e > 0]
    return src[i:min(ends)] if ends else src[i:]


def method_body(src, signature):
    """The text of one method, from its signature to the next member."""
    i = src.find(signature)
    if i < 0:
        return None
    j = src.find('\n        void ', i + len(signature))
    k = src.find('\n        static ', i + len(signature))
    ends = [e for e in (j, k) if e > 0]
    return src[i:min(ends)] if ends else src[i:i + 6000]


def test_console_profile_structured_failures():
    p = Plane(scope='op-con-fail')
    helper = HelperServer(p)
    try:
        if not helper.start():
            check('con25-helper-up', False, 'helper did not start')
            return

        # 25. Well-known SIDs are refused before anything is touched.
        for sid, why in (('S-1-1-0', 'Everyone'), ('S-1-5-32-545', 'BUILTIN\\Users'),
                         ('S-1-5-18', 'SYSTEM')):
            r = helper.request({'op': 'managed_console_profile', 'target_sid': sid})
            check('con25-well-known-sid-refused-%s' % why.split('\\')[-1].lower(),
                  r.get('status') == 'WELL_KNOWN_SID_REFUSED', str(r))

        # 26. A malformed SID is refused outright. An unknown-but-well-formed SID is
        #     refused further down; under the test-mode helper the user lookup
        #     short-circuits, so it surfaces at the ProfileList check instead --
        #     either way it is refused before any key is opened.
        r_bad = helper.request({'op': 'managed_console_profile', 'target_sid': 'not-a-sid'})
        r_unk = helper.request({'op': 'managed_console_profile', 'target_sid': 'S-1-5-21-0-0-0-0'})
        check('con26-malformed-sid-refused',
              r_bad.get('status') == 'INVALID_TARGET_SID', str(r_bad))
        check('con26b-unknown-sid-refused-before-any-write',
              r_unk.get('status') in ('UNKNOWN_TARGET_SID', 'PROFILE_NOT_FOUND')
              and not r_unk.get('values_written'), str(r_unk))

        # 27. The op is not a registry API. A banned field is rejected; any
        #     other extra field is simply ignored, and the result is byte-for-
        #     byte what the same request without it returns. There is no field
        #     that reaches a key name or a value.
        sid = 'S-1-5-21-999999999-888888888-777777777-1234'
        plain = helper.request({'op': 'managed_console_profile', 'target_sid': sid})
        noisy = helper.request({'op': 'managed_console_profile', 'target_sid': sid,
                                'subkey': r'Software\Anything', 'value': 'X',
                                'value_name': 'Run', 'key': 'K', 'hive': 'HKLM'})
        check('con27-extra-fields-are-ignored-not-honoured',
              noisy.get('status') == plain.get('status')
              and noisy.get('keys') == plain.get('keys'), str(noisy)[:250])
        r_banned = helper.request({'op': 'managed_console_profile', 'target_sid': sid,
                                   'path': r'C:\Windows', 'command': 'cmd.exe'})
        check('con27b-banned-fields-still-rejected',
              r_banned.get('status') == 'OPERATION_NOT_ALLOWED', str(r_banned))

        # 28. A managed SID whose profile does not exist yields a named failure,
        #     not an exception and not a silent success.
        r_missing = helper.request({'op': 'managed_console_profile',
                                    'target_sid': 'S-1-5-21-999999999-888888888-777777777-1234'})
        check('con28-missing-profile-is-a-named-failure',
              r_missing.get('status') in ('PROFILE_NOT_FOUND', 'PROFILE_NOT_PROVISIONED',
                                          'CONSOLE_PROFILE_FAILED'),
              str(r_missing))
    finally:
        helper.stop()
        p.cleanup()


# ================================================================ RUNTIME 25-29
def test_runtime_constraints():
    gui = source(GUI_SRC)
    helper = source(HELPER_SRC)
    engine = source(ENGINE)
    menus = source(INSTALL_MENUS)

    def member(src, signature):
        """Balanced member for this bounded, non-interpolated guard source."""
        start = src.find(signature)
        if start < 0:
            return ''
        opening = src.find('{', start + len(signature))
        depth = 0
        for i in range(opening, len(src)):
            if src[i] == '{':
                depth += 1
            elif src[i] == '}':
                depth -= 1
                if depth == 0:
                    return src[start:i + 1]
        return ''

    # 29. The original profile-only repair prohibited all hook tokens. SRC-091
    #     R001 explicitly authorizes native Ctrl+V/A/C and mouse selection in
    #     the canonical Antigravity own foreground console, so the approved
    #     own-foreground compatibility layer has TWO classes: the opt-in global
    #     bridge and the default-on scoped controller (added by T-300, after
    #     this gate was written). Both may read key state, but only while their
    #     own console is the foreground window, and neither may install a hook
    #     or read key text. Every other location stays prohibited.
    hook_tokens = ['LowLevelKeyboardProc', 'WH_KEYBOARD_LL', 'SetWindowsHookEx',
                   'RegisterRawInputDevices', 'GetAsyncKeyState']

    def input_policy_violations(src, extra_sources=()):
        """Every way a source tree can break the own-foreground input policy."""
        br = member(src, 'sealed class AntigravityConsoleInputBridge')
        ctl = member(src, 'sealed class ScopedConsoleController')
        rest = src
        for cls in (br, ctl):
            if cls:
                rest = rest.replace(cls, '')
        bad = ['token-outside-approved-layer:' + t for t in hook_tokens
               if t in rest or any(t in other for other in extra_sources)]
        if ctl:
            # A comment is not a read and a P/Invoke declaration is not a
            # read either: only the call sites count.
            code = re.sub(r'//[^\n]*', '', ctl)
            code = re.sub(r'\[DllImport\([^\]]*\)\]\s*static\s+extern\s+[^;]*;', '', code)
            gate = code.find('GetForegroundWindow() != window')
            read = code.find('GetAsyncKeyState')
            if gate < 0:
                bad.append('controller-without-foreground-gate')
            elif 0 <= read < gate:
                bad.append('controller-reads-key-state-before-foreground-gate')
            for t in ('SetWindowsHookEx', 'LowLevelKeyboardProc',
                      'RegisterRawInputDevices', 'WH_KEYBOARD_LL'):
                if t in ctl:
                    bad.append('controller-installs-hook:' + t)
            for t in ('SendInput(', 'ReadConsole', 'GetClipboardData'):
                if t in ctl:
                    bad.append('controller-touches-text-or-clipboard:' + t)
        return bad

    bridge = member(engine, 'sealed class AntigravityConsoleInputBridge')
    violations = input_policy_violations(engine, (gui, helper))
    present = [v.split(':', 1)[1]
               for v in violations if v.startswith('token-outside-approved-layer:')]
    key = member(bridge, 'IntPtr OnKey(') if bridge else ''
    mouse = member(bridge, 'IntPtr OnMouse(') if bridge else ''
    guard = member(engine, 'static int CmdConsoleTitleGuard(')
    install_scope = member(guard, 'if (IsAntigravityInteractiveChild(psi, accountId))')
    child_match = member(engine, 'static bool IsAntigravityInteractiveChild(')
    owner_check = 'if (GetForegroundWindow() != window)'
    scoped = not bridge or (
        owner_check in key and key.index(owner_check) < key.index('Marshal.PtrToStructure')
        and 'Array.Clear(suppressed, 0, suppressed.Length)' in key
        and 'GetForegroundWindow() == window' in mouse
        and 'if (bridgeEnabled) inputBridge = AntigravityConsoleInputBridge.TryInstall(hwnd)' in install_scope
        and 'AntigravityConsoleInputBridge.TryInstall(' not in guard.replace(install_scope, '')
        and '^antigravity:windows-user:[0-9a-f]{16}$' in child_match
        and 'Path.GetFullPath(SharedAgyPath())' in child_match
        and not any(t in bridge for t in ('SetConsoleMode(', 'ReadConsole', 'GetClipboardData', 'SendInput('))
        and 'UnhookWindowsHookEx(keyHook)' in bridge
        and 'UnhookWindowsHookEx(mouseHook)' in bridge)
    check('rt29-input-compatibility-is-own-foreground-only', not violations and scoped,
          'violations=%r scoped=%s' % (violations, scoped))

    # 29d. A gate that cannot fail is not a gate: the refined policy above must
    #      still go red on a known-bad source. Each subject below is a mutation
    #      of the real engine that breaks the policy in exactly one way.
    ctl_body = member(engine, 'sealed class ScopedConsoleController')
    bad_subjects = {
        'hook-install-inside-controller':
            engine.replace(ctl_body, ctl_body.replace(
                'readonly IntPtr window;',
                '[DllImport("user32.dll")] static extern IntPtr SetWindowsHookEx(int id);\n'
                '        readonly IntPtr window;')),
        'foreground-gate-removed':
            engine.replace('GetForegroundWindow() != window', 'false'),
        'key-state-read-outside-approved-layer':
            engine + '\n    static short GetAsyncKeyState(int key) { return 0; }\n',
        'synthetic-input-inside-controller':
            engine.replace(ctl_body, ctl_body.replace(
                'readonly IntPtr window;',
                'void Poke() { SendInput(1, null, 0); }\n        readonly IntPtr window;')),
    }
    missed = sorted(name for name, src in bad_subjects.items()
                    if not input_policy_violations(src))
    check('rt29d-policy-gate-fails-on-known-bad-inputs', not missed,
          'mutations not caught: %r' % (missed,))

    # 29c. P0: the global hooks are OFF unless an operator explicitly opts in,
    #      and the guard's state is observable without a log line inside the
    #      vendor's console.
    wanted = member(engine, 'static bool InputBridgeWanted(string overrideValue)')
    check('rt29c-global-input-hooks-are-opt-in-and-observable',
          'return false;' in wanted
          and 'InputBridgeSwitch' in wanted
          and '"SAI_AG_CONSOLE_INPUT_BRIDGE"' in engine
          and '--input-bridge' in guard
          and 'PublishInputBridgeState(hwnd, inputBridge != null)' in guard
          and 'SAITULS_INPUT_BRIDGE' in engine,
          'wanted=%r' % (wanted[:160],))

    # 30. No AutoHotkey dependency anywhere in the payload or the build.
    ahk = []
    for base, dirs, files in os.walk(os.path.join(ROOT, 'Scripts')):
        for f in files:
            if f.lower().endswith('.ahk'):
                ahk.append(os.path.join(base, f))
    check('rt30-no-autohotkey-scripts-in-tree', not ahk, str(ahk[:3]))
    check('rt30b-no-autohotkey-dependency',
          not re.search(r'autohotkey', (gui + helper + engine + menus), re.I),
          'AutoHotkey referenced')

    # 31. Paste is not synthesized. SendKeys/clipboard-driving is a workaround,
    #     not a fix, and the spec forbids it in normal operation.
    synth = [t for t in ('SendKeys', 'SendWait', 'WScript.Shell', 'clipboard.SetText',
                         'Set-Clipboard')
             if t in gui or t in helper or t in engine]
    check('rt31-no-synthesized-paste', not synth, str(synth))

    # 32. No per-launch consent dialog for the broker, and no new modal in the
    #     Accounts path.
    connect_body = re.sub(r'//.*', '', method_body(gui, 'void AccountsContext('))
    check('rt32-no-broker-consent-prompt',
          bool(connect_body)
          and not re.search(r'(connect|start).{0,40}(may start|allow the broker|confirm)', connect_body, re.I)
          and 'MessageBox' not in connect_body,
          'a consent prompt crept into the connect path')

    # 33. The sweep is bounded and runs off the UI thread, so startup never blocks
    #     on a broker and no popup storm can happen.
    sweep = re.search(r'void AutoConnectSweep\(bool force\)(.*?)\n        \}', gui, re.S)
    body = sweep.group(1) if sweep else ''
    check('rt33-sweep-is-async-and-throttled',
          'QueueUserWorkItem' in body and '_autoConnectSweepRunning' in body
          and '_autoConnectSweepAt' in body, body[:250])
    check('rt33b-sweep-budget-is-bounded', '--budget-ms' in body, 'no budget passed')

    # 34. The buttons speak intent verbs; start/stop stay reachable for the launch
    #     path, so a launch cannot undo a Disconnect.
    check('rt34-buttons-speak-intent-verbs',
          gui.count('AccountsContext("connect")') >= 1
          and gui.count('AccountsContext("disconnect")') >= 1,
          'connect=%d disconnect=%d' % (gui.count('AccountsContext("connect")'),
                                        gui.count('AccountsContext("disconnect")')))
    ctx = method_body(gui, 'void AccountsContext(string verb)')
    check('rt34b-verb-is-forwarded-unchanged',
          '"context " + verb' in ctx and 'verb == "connect" ? ' not in ctx.split('context')[0],
          ctx[-400:] if ctx else 'method not found')


# ---------------------------------------------------------------------------
# T-264: the agy environment scrub is a CHILD contract.
#
# Both live failures came from making it process-wide. A broker is a long-lived
# process: scrubbing ITS environment to shape one /usage capture also reshaped
# every child it started afterwards, including the Antigravity sign-in window.
# That window then inherited BROWSER=<System32>\where.exe, had no browser to
# open the Google sign-in URL with, and answered with a "Windows cannot find"
# popup while the account stayed AUTH_REQUIRED -- which is exactly the
# "third Google account cannot sign in" failure. The overrides now travel with
# the capture child only, so the sign-in child keeps the operator's own
# environment. Measured x3, because a leak that only shows up on the second
# probe is still a leak.
BROWSER_CANARY = r'C:\t264-canary\browser.exe'
KEY_CANARY = 'T264-CANARY-KEY-9fabb0d5'
SCRUBBED_BROWSER = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'where.exe')


def test_agy_scrub_is_a_child_contract_not_a_process_one():
    p = Plane(scope='op-t264-env')
    broker = None
    try:
        if not install_fake_agy(p):
            check('t264a-fake-agy-builds', False, 'the compiler refused the stand-in')
            return

        env = dict(p.env)
        env['BROWSER'] = BROWSER_CANARY
        env['GEMINI_API_KEY'] = KEY_CANARY
        env['AGY_CLI_HIDE_ACCOUNT_INFO'] = 'operator-choice'
        p.env = env

        broker = Broker(p)
        broker.start()
        name = broker.pipe_name()
        ready = False
        deadline = time.time() + 20
        while time.time() < deadline and not ready:
            try:
                pipe_request(name, {'cmd': 'status'}, timeout_s=2)
                ready = True
            except OSError:
                time.sleep(0.2)
        check('t264b-broker-online', ready, 'pipe ' + name + ' never answered')
        if not ready:
            return

        drifts = []
        for round_no in (1, 2, 3):
            clear_agy_env(p)
            pipe_request(name, {'cmd': 'usage'}, timeout_s=20)
            usage = read_agy_env(p)
            ok = (usage is not None
                  and usage.get('BROWSER', '').lower() == SCRUBBED_BROWSER.lower()
                  and usage.get('AGY_CLI_HIDE_ACCOUNT_INFO') == 'true'
                  and usage.get('AGY_CLI_DISABLE_AUTO_UPDATE') == 'true'
                  and usage.get('GEMINI_API_KEY') == '<unset>')
            check('t264c%d-usage-capture-child-still-gets-the-scrub' % round_no, ok,
                  str(usage))

            clear_agy_env(p)
            reply = pipe_request(name, {'cmd': 'auth'}, timeout_s=20)
            auth = read_agy_env(p)
            ok2 = (auth is not None
                   and auth.get('BROWSER') == BROWSER_CANARY
                   and auth.get('GEMINI_API_KEY') == KEY_CANARY
                   and auth.get('AGY_CLI_HIDE_ACCOUNT_INFO') == 'operator-choice')
            check('t264d%d-sign-in-child-keeps-the-normal-environment' % round_no, ok2,
                  str(auth) + ' / ' + str(reply)[:120])
            if not (ok and ok2):
                drifts.append(str(round_no))
        check('t264e-usage-and-auth-do-not-drift-across-rounds', not drifts, ','.join(drifts))
    finally:
        if broker is not None:
            broker.stop()
        p.cleanup()


def test_the_engine_never_mutates_its_own_environment():
    """Structural half of the same rule: no source path may mutate it again."""
    src = source(ENGINE)
    # Comment lines are excluded: the fix's own explanation names the call it
    # removed, and a check that cannot tell code from the note about the code
    # would forbid describing the defect it exists to prevent.
    code = '\n'.join(l for l in src.splitlines() if not l.strip().startswith('//'))
    check('t264f-engine-does-not-set-process-environment-variables',
          'Environment.SetEnvironmentVariable' not in code,
          'a process-wide environment write is back in the engine')
    check('t264g-capture-block-is-built-for-the-child',
          'BuildEnvironmentBlock(' in src and 'AgyCaptureEnvironment()' in src,
          'the child environment block is not wired into RunCapture')


def main():
    ok, log = build()
    if not ok:
        print('FAIL  sai-accounts.exe build')
        print(log[-800:])
        return 1
    print('built sai-accounts.exe')

    test_auto_connect_defaults()
    test_auto_connect_migration()
    test_auto_connect_explicit_false_is_a_decision()
    test_connect_disconnect_persist()
    test_autoconnect_reconciliation()
    test_autoconnect_concurrent_sweeps()
    test_autoconnect_lifecycle_and_scope()
    test_launch_does_not_re_enable_disconnected()
    test_autoconnect_budget_and_empty()
    test_runtime_failure_never_writes_desired_state()
    test_provenance_and_clock()

    test_yolo_current_user_launch()
    test_yolo_managed_broker_launch()
    test_yolo_engine_policy_contract()

    test_console_profile_contract()
    test_console_profile_structured_failures()

    test_runtime_constraints()

    test_agy_scrub_is_a_child_contract_not_a_process_one()
    test_the_engine_never_mutates_its_own_environment()

    print()
    print('PASS %d / FAIL %d' % (len(PASSES), len(FAILURES)))
    if FAILURES:
        for f in FAILURES:
            print('  FAILED: ' + f)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
