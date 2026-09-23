#!/usr/bin/env python3
"""T-143 P0A: structured busy/failure taxonomy, proven on real Windows I/O.

Run:  python tests/test_del_junk_busy_recovery.py
Exit: 0 = all PASS, 1 = failures.

The pre-repair failure mode: an ordinary locked file (the everyday case on a
live desktop -- an open log, a running browser holding its cache) produced a
global FAILED outcome that stopped the whole unattended multi-drive run. The
repair: delete failures are classified by exception type + errno + winerror
(never by message text); ordinary contention resolves to candidate-local
SKIPPED_BUSY / PARTIAL_BUSY after a bounded retry, later candidates and later
drives continue, and only unknown destructive I/O, journal failure,
confinement failure, identity uncertainty, reparse escape or filesystem
integrity failure stops the run globally.

Every locking test here holds a REAL Win32 handle (CreateFileW with
dwShareMode=0) so os.remove really fails with ERROR_SHARING_VIOLATION -- no
message-text simulation anywhere.
"""
import ctypes
import importlib.machinery
import importlib.util
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + os.sep + '..')
SCRIPTS = os.path.join(ROOT, 'Scripts')
sys.path.insert(0, SCRIPTS)

import del_junk_errors as errors_mod  # noqa: E402

fails = 0


def check(name, cond, detail=''):
    global fails
    if cond:
        print('PASS ', name, ('  -> ' + detail) if detail else '')
    else:
        fails += 1
        print('FAIL ', name, '  -> ' + detail)
    return bool(cond)


def load_worker():
    path = os.path.join(SCRIPTS, 'DEL_JUNK.PYW')
    spec = importlib.util.spec_from_loader(
        'del_junk_busy_recovery', importlib.machinery.SourceFileLoader(
            'del_junk_busy_recovery', path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WORKER = load_worker()
with open(os.path.join(SCRIPTS, 'DEL_JUNK.PYW'), encoding='utf-8') as _fh:
    SOURCE = _fh.read()
with open(os.path.join(SCRIPTS, 'del_junk_errors.py'), encoding='utf-8') as _fh:
    ERRORS_SOURCE = _fh.read()
with open(os.path.join(SCRIPTS, 'del_junk_walk.py'), encoding='utf-8') as _fh:
    WALK_SOURCE = _fh.read()

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class RealExclusiveLock:
    """A real Win32 handle with dwShareMode=0: the file cannot be deleted
    until close() -- os.remove fails with the real ERROR_SHARING_VIOLATION."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.handle = ctypes.windll.kernel32.CreateFileW(
            self.path, GENERIC_READ, 0, None, OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL, None)
        if self.handle == INVALID_HANDLE_VALUE or self.handle is None:
            raise OSError('CreateFileW failed for %s' % self.path)
        return self

    def __exit__(self, *exc):
        if self.handle not in (None, INVALID_HANDLE_VALUE):
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None
        return False


def make_junk_root(prefix, names=('orphan.part',), stale_days=30):
    """A throwaway root whose aged *.part files are junk under SAFE_DISK."""
    root = tempfile.mkdtemp(prefix=prefix)
    old = time.time() - stale_days * 86400
    paths = []
    for name in names:
        path = os.path.join(root, name)
        with open(path, 'w') as handle:
            handle.write('x' * 1024)
        os.utime(path, (old, old))
        paths.append(path)
    return root, paths


def read_journal(journal):
    with open(journal, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def run_stream(root, journal, **kwargs):
    return WORKER.stream_drive(root, WORKER.all_disks_policy(), journal, **kwargs)


def test_classification_matrix():
    """Structured classification only: type/errno/winerror, never text."""
    cls = errors_mod.classify_delete_error
    R, C, F = (errors_mod.DELETE_RECOVERABLE_BUSY, errors_mod.DELETE_CHANGED,
               errors_mod.DELETE_FATAL)

    def winerror_exc(code, text):
        return OSError(0, text, None, code)

    check('FileNotFoundError classifies DELETE_CHANGED',
          cls(FileNotFoundError()) == C)
    check('PermissionError (access denied) is recoverable busy',
          cls(PermissionError()) == R)
    # Windows Python auto-subclasses OSError(winerror=2/3) to FileNotFoundError,
    # so codes 2 and 3 classify DELETE_CHANGED -- exactly the SKIPPED_CHANGED
    # mapping the repair spec requires for FILE_NOT_FOUND / PATH_NOT_FOUND.
    check('winerror 2 (ERROR_FILE_NOT_FOUND) maps to changed',
          cls(winerror_exc(2, ' completely different story ')) == C)
    check('winerror 3 (ERROR_PATH_NOT_FOUND) maps to changed',
          cls(winerror_exc(3, ' completely different story ')) == C)
    for code, name in ((5, 'ERROR_ACCESS_DENIED'),
                       (32, 'ERROR_SHARING_VIOLATION'),
                       (33, 'ERROR_LOCK_VIOLATION'),
                       (145, 'ERROR_DIR_NOT_EMPTY')):
        check('winerror %d (%s) classified without reading text' % (code, name),
              cls(winerror_exc(code, ' completely different story ')) == R)
    check('unknown winerror fails closed to DELETE_FATAL',
          cls(winerror_exc(59, 'unexpected network error')) == F)
    check('plain OSError (no code) fails closed to DELETE_FATAL',
          cls(OSError('mystery failure')) == F)
    # OSError(errno=2) also auto-subclasses to FileNotFoundError on this
    # runtime: the changed mapping, not busy, is the truthful one.
    check('errno ENOENT (non-Windows shape) maps to changed',
          cls(OSError(2, 'no file')) == C)
    same = [cls(winerror_exc(32, 'text A')), cls(winerror_exc(32, 'text B'))]
    check('same code different message text classifies identically',
          same[0] == same[1] == R)
    body = inspect.getsource(cls).split('"""')[2]
    check('classify_delete_error code never reads exception text',
          'str(' not in body and '.args' not in body
          and 'winerror' in body and 'errno' in body)


def test_locked_file_then_later_candidates_same_root():
    """Locked file -> SKIPPED_BUSY; later candidates on the SAME drive still
    run; bounded retry really retried; stop signal never armed."""
    root, (locked, later) = make_junk_root(
        'saituls_busy_file_', names=('locked.part', 'later.part'))
    journal = os.path.join(root + '_j', 'run.journal.jsonl')
    os.makedirs(os.path.dirname(journal), exist_ok=True)
    stop_fatal = {'stop': False}
    attempts = {'locked': 0}
    real_remove = os.remove

    def counting_remove(path, *a, **k):
        if os.path.normcase(path) == os.path.normcase(locked):
            attempts['locked'] += 1
        return real_remove(path, *a, **k)

    try:
        with RealExclusiveLock(locked), \
                patch.object(WORKER.os, 'remove', counting_remove):
            result = run_stream(root, journal, stop_fatal=stop_fatal)
        events = [(r['event'], r['path']) for r in read_journal(journal)]
        check('locked file journalled SKIPPED_BUSY with same identity intact',
              ('SKIPPED_BUSY', locked) in events and os.path.exists(locked),
              repr(events))
        check('later candidate on the same drive was still deleted',
              ('DELETED', later) in events and not os.path.exists(later),
              repr(events))
        check('busy outcome ran the bounded retry (1 + %d retries)'
              % len(WORKER._RETRY_DELAYS),
              attempts['locked'] == 1 + len(WORKER._RETRY_DELAYS),
              'attempts=%d' % attempts['locked'])
        check('drive result PARTIAL, never FAILED, with busy_skips=1',
              result['result'] == 'PARTIAL' and result.get('busy_skips') == 1,
              repr(result['result']))
        check('busy outcome did NOT arm the global stop signal',
              stop_fatal['stop'] is False)
        check('terminal busy outcomes leave zero dangling intents',
              WORKER.dangling_intents(read_journal(journal)) == [])
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(os.path.dirname(journal), ignore_errors=True)


def test_locked_root_then_second_root_continues():
    """A busy candidate on the first drive must not stop later drives."""
    root1, (locked1,) = make_junk_root('saituls_busy_d1_', names=('a.part',))
    root2, (gone2,) = make_junk_root('saituls_busy_d2_', names=('b.part',))
    journal_dir = tempfile.mkdtemp(prefix='saituls_busy_multi_j_')
    journal = os.path.join(journal_dir, 'multi.journal.jsonl')
    stop_fatal = {'stop': False}
    try:
        with RealExclusiveLock(locked1):
            r1 = run_stream(root1, journal, stop_fatal=stop_fatal)
            r2 = run_stream(root2, journal, stop_fatal=stop_fatal)
        check('first drive finishes PARTIAL with its busy warning',
              r1['result'] == 'PARTIAL' and r1.get('busy_skips') == 1,
              repr(r1['result']))
        check('second drive still fully processed (DELETED, not CANCELLED)',
              r2['result'] == 'CLEANED' and not os.path.exists(gone2),
              repr(r2['result']))
        check('global stop signal still unarmed after both drives',
              stop_fatal['stop'] is False)
    finally:
        shutil.rmtree(root1, ignore_errors=True)
        shutil.rmtree(root2, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_disappearing_candidate():
    """A candidate really unlinked between INTENT and deletion reports
    SKIPPED_CHANGED (real unlink, not message simulation)."""
    root, (victim,) = make_junk_root('saituls_busy_vanish_', names=('v.part',))
    journal_dir = tempfile.mkdtemp(prefix='saituls_busy_vanish_j_')
    journal = os.path.join(journal_dir, 'v.journal.jsonl')
    real_record = WORKER.journal_record

    def unlink_after_intent(path, event, *args, **kwargs):
        ok = real_record(path, event, *args, **kwargs)
        if event == 'INTENT' and os.path.exists(victim):
            os.unlink(victim)
        return ok

    try:
        with patch.object(WORKER, 'journal_record', unlink_after_intent):
            result = run_stream(root, journal)
        events = [r['event'] for r in read_journal(journal)]
        check('vanished candidate reports SKIPPED_CHANGED',
              'SKIPPED_CHANGED' in events and 'DELETED' not in events,
              repr(events))
        check('vanished candidate stays a terminal outcome (no dangling)',
              WORKER.dangling_intents(read_journal(journal)) == [])
        check('drive result PARTIAL with the race recorded as a warning',
              result['result'] == 'PARTIAL' and result['errors'],
              repr(result['result']))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_locked_child_after_partial_directory_mutation():
    """Locked child after some children were removed: PARTIAL_BUSY (the truth
    about mutation), directory survives, nothing rolled back, no global stop.

    The iterative deleter pops children LIFO, so the locked child is named to
    sort FIRST: the other child is removed before the busy failure, making the
    partial-mutation branch deterministic.
    """
    root = tempfile.mkdtemp(prefix='saituls_busy_dir_')
    cache = os.path.join(root, 'gpucache')
    os.makedirs(cache)
    old = time.time() - 30 * 86400
    deletable = os.path.join(cache, 'z_gone.bin')
    locked = os.path.join(cache, 'a_held.bin')
    for path in (deletable, locked):
        with open(path, 'w') as handle:
            handle.write('x' * 512)
        os.utime(path, (old, old))
    journal_dir = tempfile.mkdtemp(prefix='saituls_busy_dir_j_')
    journal = os.path.join(journal_dir, 'd.journal.jsonl')
    stop_fatal = {'stop': False}
    try:
        with RealExclusiveLock(locked):
            result = run_stream(root, journal, stop_fatal=stop_fatal)
        events = [(r['event'], r['path']) for r in read_journal(journal)]
        check('partially mutated directory reports PARTIAL_BUSY',
              ('PARTIAL_BUSY', cache) in events, repr(events))
        check('the deletable child really was removed before the busy stop',
              not os.path.exists(deletable))
        check('the directory itself survives with its locked child',
              os.path.isdir(cache) and os.path.exists(locked))
        check('no rollback lie: no SKIPPED_BUSY for the mutated tree',
              ('SKIPPED_BUSY', cache) not in events, repr(events))
        check('drive PARTIAL, global stop unarmed',
              result['result'] == 'PARTIAL' and stop_fatal['stop'] is False,
              repr(result['result']))
        check('PARTIAL_BUSY is terminal: zero dangling intents',
              WORKER.dangling_intents(read_journal(journal)) == [])
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_unknown_fatal_io_stops_globally():
    """An unexplained destructive I/O failure (unknown winerror) after a
    durable INTENT still stops the whole run: FAILED + IO_FAILURE_STOP."""
    root, (fatal, later) = make_junk_root(
        'saituls_busy_fatal_', names=('fatal.part', 'later.part'))
    journal_dir = tempfile.mkdtemp(prefix='saituls_busy_fatal_j_')
    journal = os.path.join(journal_dir, 'f.journal.jsonl')
    stop_fatal = {'stop': False}
    real_remove = os.remove

    def fatal_remove(path, *a, **k):
        if os.path.normcase(path) == os.path.normcase(fatal):
            raise OSError(0, 'unknown device failure', None, 59)
        return real_remove(path, *a, **k)

    try:
        with patch.object(WORKER.os, 'remove', fatal_remove):
            result = run_stream(root, journal, stop_fatal=stop_fatal)
        events = [(r['event'], r['path']) for r in read_journal(journal)]
        check('unknown winerror reports FAILED for its candidate',
              ('FAILED', fatal) in events and os.path.exists(fatal),
              repr(events))
        check('fatal I/O records the run-level IO_FAILURE_STOP',
              any(r['event'] == 'IO_FAILURE_STOP' for r in read_journal(journal)))
        check('fatal I/O arms the global stop signal',
              stop_fatal['stop'] is True)
        check('later candidate was NOT attempted after fatal I/O',
              ('DELETED', later) not in events and os.path.exists(later),
              repr(events))
        # Drive level: aborted before any deletion there -> CANCELLED-shaped
        # result with the IO_FAILURE_STOP reason; the run level maps the
        # reason to the global FAILED/PARTIAL decision.
        check('drive abort carries IO_FAILURE_STOP reason',
              result.get('reason') == 'IO_FAILURE_STOP'
              and result['result'] in ('FAILED', 'CANCELLED'),
              repr(result['result']))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_journal_failure_refuses_deletion():
    """A journal that cannot durably record the INTENT refuses the delete."""
    root, (target,) = make_junk_root('saituls_busy_journal_', names=('j.part',))
    journal_dir = tempfile.mkdtemp(prefix='saituls_busy_journal_j_')
    journal = os.path.join(journal_dir, 'j.journal.jsonl')

    def refusing_record(path, event, *args, **kwargs):
        if event == 'INTENT':
            return False
        return True

    try:
        with patch.object(WORKER, 'journal_record', refusing_record):
            result = run_stream(root, journal)
        check('journal failure: candidate survives, nothing deleted',
              os.path.exists(target) and result['result'] == 'JOURNAL_UNAVAILABLE',
              repr(result['result']))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_run_level_truth_for_busy_only_runs():
    """Full unattended run over two drives with a locked candidate: result
    PARTIAL, completed_with_warnings=true, stopped_early=false, and the later
    drive really was still traversed."""
    root1, (locked1,) = make_junk_root('saituls_busy_run_d1_', names=('a.part',))
    root2, (gone2,) = make_junk_root('saituls_busy_run_d2_', names=('b.part',))
    journal_dir = tempfile.mkdtemp(prefix='saituls_busy_run_j_')
    flag_journal = os.path.join(journal_dir, 'busy.journal.jsonl')

    class FakeProgress:
        def __init__(self, *a, **k):
            pass
        def update(self, *a, **k):
            pass
        def cancelled(self):
            return False
        def set_stage(self, stage):
            pass
        def close(self):
            pass

    saved = {}
    saved['ready'] = WORKER.all_disks_destructive_ready
    saved['progress'] = WORKER.ScanProgress
    saved['journal_path'] = WORKER.journal_path
    saved['eligible'] = WORKER.eligible_drives
    saved['prev'] = WORKER.incomplete_previous_run
    saved['msg'] = {n: getattr(WORKER.messagebox, n)
                    for n in ('showinfo', 'showerror', 'showwarning', 'askyesno')}
    messages = []
    WORKER.all_disks_destructive_ready = lambda now=None: (True, [])
    WORKER.ScanProgress = FakeProgress
    WORKER.journal_path = lambda directory=None: flag_journal
    WORKER.eligible_drives = lambda: (
        [dict(root=root1, drive_type=3, drive_type_name='FIXED', ready=True,
              writable=True, total_bytes=1, free_bytes=1, is_system=False),
         dict(root=root2, drive_type=3, drive_type_name='FIXED', ready=True,
              writable=True, total_bytes=1, free_bytes=1, is_system=False)],
        [])
    WORKER.incomplete_previous_run = lambda directory=None: None
    for n in ('showinfo', 'showerror', 'showwarning'):
        setattr(WORKER.messagebox, n,
                lambda *a, **k: messages.append(str(a)))
    WORKER.messagebox.askyesno = lambda *a, **k: True
    try:
        with RealExclusiveLock(locked1):
            rc = WORKER.run_all_disks(None, preview_only=False)
        records = read_journal(flag_journal)
        run_end = [r for r in records if r['event'] == 'RUN_END']
        check('run over both drives exits 2 (truthful PARTIAL contract)',
              rc == 2, 'rc=%r' % rc)
        check('RUN_END recorded with the warning truth fields',
              len(run_end) == 1
              and run_end[0]['payload'].get('completed_with_warnings') is True
              and run_end[0]['payload'].get('stopped_early') is False
              and run_end[0]['payload'].get('result') == 'PARTIAL',
              repr(run_end))
        check('second drive was traversed after the busy first drive',
              any(r['event'] == 'DRIVE_END'
                  and r.get('payload', {}).get('root') == root2
                  for r in records)
              and not os.path.exists(gone2), repr(
                  [r['event'] for r in records]))
    finally:
        WORKER.all_disks_destructive_ready = saved['ready']
        WORKER.ScanProgress = saved['progress']
        WORKER.journal_path = saved['journal_path']
        WORKER.eligible_drives = saved['eligible']
        WORKER.incomplete_previous_run = saved['prev']
        for n, fn in saved['msg'].items():
            setattr(WORKER.messagebox, n, fn)
        shutil.rmtree(root1, ignore_errors=True)
        shutil.rmtree(root2, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_no_test_string_branch_in_production():
    """The production taxonomy must never branch on message text and must not
    carry the historical test-string escape hatch."""
    production = SOURCE + ERRORS_SOURCE + WALK_SOURCE
    check('no "injected deletion failure" branch in production source',
          'injected deletion failure' not in production)
    check('stop decision is event-driven, not string-driven',
          re.search(r'event\s*==\s*["\']FAILED["\']', SOURCE) is not None
          and 'in outcome.get(' not in SOURCE)


if __name__ == '__main__':
    test_classification_matrix()
    test_locked_file_then_later_candidates_same_root()
    test_locked_root_then_second_root_continues()
    test_disappearing_candidate()
    test_locked_child_after_partial_directory_mutation()
    test_unknown_fatal_io_stops_globally()
    test_journal_failure_refuses_deletion()
    test_run_level_truth_for_busy_only_runs()
    test_no_test_string_branch_in_production()
    print('---')
    print('PASS (0 failures)' if fails == 0 else 'FAILED (%d)' % fails)
    sys.exit(0 if fails == 0 else 1)
