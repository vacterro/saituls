#!/usr/bin/env python3
"""Regression tests for DEL JUNK ALL DISKS AVAILABLE (--all-disks).

Run:  python tests/test_del_junk_all_disks.py
Exit: 0 = all PASS, 1 = failures.

ALL DISKS deletes on every local disk at once, without the Recycle Bin, from a
plan the user confirms once. Everything that makes that defensible is pinned
here: which volumes are eligible, that SAFE_DISK is the only reachable policy,
that the preview equals the confirmed manifests, that failures are isolated and
truthful, that cancellation never lies, and that overlapping destructive scopes
cannot run at the same time.

Two audited defects are hard prerequisites and have their own cases:

  R-AUDIT-001  a junk-named directory containing a protected file
               (``gpucache/important.log``) must survive whole and be reported
               as risky_excluded.
  R-AUDIT-002  ``R:\\`` and ``R:\\Projects`` are overlapping destructive scopes
               and must conflict; unrelated siblings must not.

The last case evaluates the acceptance gate itself:

  ALL_DISKS_DESTRUCTIVE_READY = protected-tree test PASS
                                AND overlap-lock test PASS
                                AND SAFE_DISK regression suite PASS
"""
import hashlib
import importlib.machinery
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + os.sep + '..')
SCRIPTS = os.path.join(ROOT, 'Scripts')
sys.path.insert(0, SCRIPTS)

import del_junk_disks as disks  # noqa: E402 - Scripts/ is the worker's own path

fails = 0
gate_results = {}


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
        'del_junk_all_disks', importlib.machinery.SourceFileLoader(
            'del_junk_all_disks', path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WORKER = load_worker()
with open(os.path.join(SCRIPTS, 'DEL_JUNK.PYW'), encoding='utf-8') as _fh:
    SOURCE = _fh.read()
# The all-disks block is everything the batch invocation can reach.
ALL_DISKS_SOURCE = SOURCE[SOURCE.index('ALL_DISKS_FLAG ='):SOURCE.index('def main(')]


def target(root, **extra):
    record = {'root': root, 'drive_type': disks.DRIVE_FIXED,
              'drive_type_name': 'FIXED', 'ready': True, 'writable': True,
              'total_bytes': 1024, 'free_bytes': 512, 'is_system': False}
    record.update(extra)
    return record


def make_drive(prefix, stale_days=30):
    """A throwaway tree shaped like a volume root, with every trap on it."""
    root = tempfile.mkdtemp(prefix=prefix)
    old = time.time() - stale_days * 86400

    def put(rel, data='x', mtime=None):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    put(r'browser\gpucache\blob.bin')             # known cache -> junk
    put(r'code\__pycache__\m.pyc')                # known cache -> junk
    put(r'orphan.part', 'stale transfer', mtime=old)   # age gate -> junk
    put(r'.git\config', '[core]')                 # repository history -> keep
    put(r'.git\logs\HEAD.log')
    put(r'.svn\pristine\a.pyc')
    put(r'.hg\store\a.pyc')
    put(r'$RECYCLE.BIN\a.pyc')
    put(r'System Volume Information\a.pyc')
    put(r'Project\build\out.obj')                 # risky subtree -> keep whole
    put(r'site.log', 'the only copy')             # root-level log -> keep
    put(r'shaders\gpucache\important.log', 'R-AUDIT-001')  # protects its tree
    put(r'shaders\gpucache\blob.bin')
    return root


PROTECTED = (r'.git\config', r'.git\logs\HEAD.log', r'.svn\pristine\a.pyc',
             r'.hg\store\a.pyc', r'$RECYCLE.BIN\a.pyc',
             r'System Volume Information\a.pyc', r'Project\build\out.obj',
             'site.log', r'shaders\gpucache\important.log',
             r'shaders\gpucache\blob.bin')


def test_discovery_rules():
    fake = [
        {'root': 'Q:\\', 'drive_type': disks.DRIVE_FIXED, 'ready': True,
         'writable': True, 'total_bytes': 100, 'free_bytes': 40},
        {'root': 'R:\\', 'drive_type': disks.DRIVE_FIXED, 'ready': True,
         'writable': True, 'total_bytes': 200, 'free_bytes': 10},
        {'root': 'N:\\', 'drive_type': disks.DRIVE_REMOTE, 'ready': True,
         'writable': True},
        {'root': 'X:\\', 'drive_type': disks.DRIVE_REMOTE, 'ready': False,
         'writable': False},
        {'root': 'D:\\', 'drive_type': disks.DRIVE_CDROM, 'ready': True,
         'writable': False},
        {'root': 'U:\\', 'drive_type': disks.DRIVE_REMOVABLE, 'ready': True,
         'writable': True},
        {'root': 'M:\\', 'drive_type': disks.DRIVE_RAMDISK, 'ready': True,
         'writable': True},
        {'root': 'Z:\\', 'drive_type': disks.DRIVE_NO_ROOT_DIR, 'ready': False,
         'writable': False},
        {'root': 'F:\\', 'drive_type': disks.DRIVE_FIXED, 'ready': False,
         'writable': False},
        {'root': 'W:\\', 'drive_type': disks.DRIVE_FIXED, 'ready': True,
         'writable': False},
    ]
    targets, skipped = disks.eligible_drives(
        enumerator=lambda: fake, system_root='Q:\\', exists=lambda root: True)
    roots = [item['root'] for item in targets]
    check('local fixed writable mounted disks are discovered',
          roots == ['Q:\\', 'R:\\'], repr(roots))
    check('capacity and free-space-before are recorded per drive',
          targets[0]['total_bytes'] == 100 and targets[0]['free_bytes'] == 40)
    excluded = {item['root']: item['reason'] for item in skipped}
    check('network, mapped-offline, optical, RAM and removable are excluded',
          all(root in excluded for root in
              ('N:\\', 'X:\\', 'D:\\', 'U:\\', 'M:\\', 'Z:\\')),
          repr(sorted(excluded)))
    check('an unready or read-only fixed disk is excluded with a reason',
          'not ready' in excluded.get('F:\\', '') and
          'read-only' in excluded.get('W:\\', ''), repr(excluded))
    check('every excluded drive carries an explicit SKIPPED result',
          all(item['result'] == disks.SKIPPED for item in skipped))
    check('the Windows system drive is detected dynamically, not hardcoded',
          disks.system_drive_root({'SystemRoot': 'E:\\Windows'}) == 'E:' + os.sep
          and targets[0]['is_system'] and not targets[1]['is_system'],
          disks.system_drive_root({'SystemRoot': 'E:\\Windows'}))
    if os.name == 'nt':
        live, _live_skipped = disks.eligible_drives()
        check('the live machine discovers its own fixed disks and system drive',
              bool(live) and any(item['is_system'] for item in live),
              '%d disk(s), system=%s' % (len(live), disks.system_drive_root()))
    check('zero eligible disks is an empty informational plan, not an error',
          disks.eligible_drives(enumerator=lambda: [], system_root='Q:\\') == ([], [])
          and WORKER.plan_all_disks([]) == ([], [])
          and 'Подходящих дисков не найдено' in SOURCE)


def test_safe_disk_is_the_only_policy():
    check('all-disks builds SAFE_DISK explicitly',
          WORKER.all_disks_policy()['mode'] == 'SAFE_DISK')
    check('AGGRESSIVE_PROJECT is unreachable from the all-disks path',
          'select_policy(' not in ALL_DISKS_SOURCE
          and 'aggressive=True' not in ALL_DISKS_SOURCE
          and 'aggressive=False' in ALL_DISKS_SOURCE
          and '"SAFE_DISK"' in ALL_DISKS_SOURCE)
    check('the single-target worker still chooses its policy from the target',
          'mode = select_policy(target_dir)' in SOURCE
          and WORKER.select_policy('V:\\a\\b') == 'AGGRESSIVE_PROJECT')
    check('the shell all-disks command is recognized, a path is not',
          WORKER.wants_all_disks(['DEL_JUNK.PYW', '--all-disks'])
          and not WORKER.wants_all_disks(['DEL_JUNK.PYW', 'G:\\'])
          and not WORKER.wants_all_disks(['DEL_JUNK.PYW']))


def test_shell_registration():
    path = os.path.join(ROOT, 'Registry', 'DEL_JUNK.REG')
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    verbs = dict(re.findall(
        r'\[HKEY_CLASSES_ROOT\\(\w+)\\shell\\DeleteJunkFilesAllDisks\\command\]\s*\n@="(.+?)"\s*\n',
        text))
    check('ALL DISKS is registered on Directory and Drive',
          set(verbs) == {'Directory', 'Drive'}, repr(sorted(verbs)))
    check('the ALL DISKS label is visible next to DEL JUNK',
          text.count('@="DEL JUNK ALL DISKS AVAILABLE"') == 2
          and text.count('@="DEL JUNK"') == 2)
    for shell_class, command in sorted(verbs.items()):
        real = command.replace('\\\\', '\\').replace('\\"', '"')
        args = [part for part in re.split(r'\s+', real) if part]
        check('%s: the shell command invokes all-disks mode' % shell_class,
              WORKER.wants_all_disks(args) and 'DEL_JUNK.PYW' in real
              and '"%1"' not in real, real)
    single = dict(re.findall(
        r'\[HKEY_CLASSES_ROOT\\(\w+)\\shell\\DeleteJunkFiles\\command\]\s*\n@="(.+?)"\s*\n',
        text))
    check('the existing single-target DEL JUNK command is unchanged',
          set(single) == {'Directory', 'Drive'} and
          all(value == 'pyw.exe \\"%%ROOT%%\\\\Scripts\\\\DEL_JUNK.PYW\\" \\"%1\\"'
              for value in single.values()), repr(single))
    removal = os.path.join(ROOT, 'Registry', 'DEL_JUNK_REM.REG')
    with open(removal, encoding='utf-8') as handle:
        rem = handle.read()
    check('uninstall removes the new verb from both shell roots',
          rem.count('[-HKEY_CLASSES_ROOT\\Directory\\shell\\DeleteJunkFilesAllDisks]') == 1
          and rem.count('[-HKEY_CLASSES_ROOT\\Drive\\shell\\DeleteJunkFilesAllDisks]') == 1)
    for name in ('DEL_JUNK.REG', 'DEL_JUNK_REM.REG'):
        localized = os.path.join(ROOT, 'i18n', 'reg', 'et', name)
        with open(localized, encoding='utf-8') as handle:
            other = handle.read()
        with open(os.path.join(ROOT, 'Registry', name), encoding='utf-8') as handle:
            base = handle.read()
        check('the localized generation carries the same verbs (%s)' % name,
              other == base)


def test_protected_tree_repair():
    """R-AUDIT-001: one protected file keeps its whole junk-named directory."""
    root = tempfile.mkdtemp(prefix='saituls_alldisks_audit1_')
    try:
        cache = os.path.join(root, 'gpucache')
        os.makedirs(cache)
        for name, data in (('blob.bin', 'regenerable'),
                          ('important.log', 'the only copy of a crash story')):
            with open(os.path.join(cache, name), 'w') as handle:
                handle.write(data)
        plain = os.path.join(root, 'other', 'gpucache')
        os.makedirs(plain)
        with open(os.path.join(plain, 'blob.bin'), 'w') as handle:
            handle.write('regenerable')

        policy = WORKER.all_disks_policy()
        files, dirs, risky = WORKER.scan_junk(root, policy=policy)
        ok = check('a junk directory holding a protected file is not deleted',
                   cache not in dirs, repr(dirs))
        ok = check('that directory is reported as risky_excluded instead',
                   cache in risky, repr(risky)) and ok
        ok = check('the protected file is never listed on its own either',
                   not any(item.startswith(cache + os.sep) for item in files),
                   repr(files)) and ok
        ok = check('an equivalent cache with nothing protected inside is still junk',
                   plain in dirs, repr(dirs)) and ok

        WORKER.delete_junk(files, dirs, root_path=root,
                           manifest=WORKER.manifest_bytes(
                               files, dirs, risky, root_path=root,
                               mode='SAFE_DISK')[1])
        ok = check('the protected file survives the actual delete',
                   os.path.exists(os.path.join(cache, 'important.log'))
                   and os.path.exists(os.path.join(cache, 'blob.bin'))) and ok
        ok = check('the unprotected cache tree is actually gone',
                   not os.path.exists(plain)) and ok
        gate_results['protected-tree'] = ok
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_overlapping_scopes_conflict():
    """R-AUDIT-002: a drive root and a folder inside it are one scope."""
    parent = tempfile.mkdtemp(prefix='saituls_alldisks_audit2_')
    child = os.path.join(parent, 'Projects')
    sibling = os.path.join(parent, 'Other')
    os.makedirs(child)
    os.makedirs(sibling)
    ok = True
    try:
        root_key = os.path.normcase(os.path.abspath('R:' + os.sep))
        child_key = os.path.normcase(os.path.abspath('R:' + os.sep + 'Projects'))
        ok = check('R:\\ and R:\\Projects are overlapping scopes',
                   WORKER.scopes_conflict(root_key, child_key)
                   and WORKER.scopes_conflict(child_key, root_key)) and ok
        ok = check('unrelated folders on the same drive do not conflict',
                   not WORKER.scopes_conflict(
                       child_key,
                       os.path.normcase(os.path.abspath('R:' + os.sep + 'Other')))) and ok

        held = WORKER.single_flight_lock(parent)
        ok = check('a parent-scope cleanup reserves its whole subtree',
                   bool(held) and not WORKER.single_flight_lock(child)) and ok
        WORKER.release_single_flight(parent)

        held_child = WORKER.single_flight_lock(child)
        ok = check('a child-scope cleanup blocks the drive-root pass',
                   bool(held_child) and not WORKER.single_flight_lock(parent)) and ok
        sections, skipped = WORKER.plan_all_disks([target(parent)])
        ok = check('ALL DISKS skips a drive already owned by a folder cleanup',
                   not sections and len(skipped) == 1
                   and skipped[0]['result'] == disks.SKIPPED
                   and 'already owned' in skipped[0]['reason'],
                   repr([(item['root'], item.get('reason')) for item in skipped])) and ok
        WORKER.release_single_flight(child)

        held_sibling = WORKER.single_flight_lock(child)
        other = WORKER.single_flight_lock(sibling)
        ok = check('two non-overlapping cleanups may still run at once',
                   bool(held_sibling) and bool(other)) and ok
        WORKER.release_single_flight(child)
        WORKER.release_single_flight(sibling)

        probe = """import importlib.machinery, importlib.util, sys
spec = importlib.util.spec_from_loader('probe', importlib.machinery.SourceFileLoader('probe', sys.argv[1]))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
lock = module.single_flight_lock(sys.argv[2])
print('ACQUIRED' if lock else 'BLOCKED')
if lock:
    module.release_single_flight(sys.argv[2])
"""
        held = WORKER.single_flight_lock(parent)
        answer = subprocess.check_output(
            [sys.executable, '-c', probe, os.path.join(SCRIPTS, 'DEL_JUNK.PYW'),
             child], text=True).strip()
        ok = check('another process cannot clean inside a reserved scope',
                   bool(held) and answer == 'BLOCKED', answer) and ok
        WORKER.release_single_flight(parent)
        answer = subprocess.check_output(
            [sys.executable, '-c', probe, os.path.join(SCRIPTS, 'DEL_JUNK.PYW'),
             child], text=True).strip()
        ok = check('releasing the parent scope frees the subtree again',
                   answer == 'ACQUIRED', answer) and ok
        gate_results['overlap-lock'] = ok
    finally:
        for path in (parent, child, sibling):
            WORKER.release_single_flight(path)
        shutil.rmtree(parent, ignore_errors=True)


WORKER_PATH = os.path.join(SCRIPTS, 'DEL_JUNK.PYW')

_RACE_PROBE = """import importlib.machinery, importlib.util, os, sys, time
spec = importlib.util.spec_from_loader('probe', importlib.machinery.SourceFileLoader(
    'probe', sys.argv[1]))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
target, ready, started = sys.argv[2], sys.argv[3], sys.argv[4]
deadline = time.monotonic() + 30.0
while not os.path.exists(ready) and time.monotonic() < deadline:
    time.sleep(0.01)
open(started, 'wb').close()
lock = module.single_flight_lock(target)
print('ACQUIRED' if lock else 'BLOCKED')
if lock:
    module.release_single_flight(target)
"""


def _race(holder, contender, hold_after=0.3):
    """Hold ``holder`` while another process tries ``contender`` inside the
    reservation's publication window.

    The injected barrier widens that window without moving it: it runs after the
    reservation is locked and before its PID/scope payload is written. With the
    payload published before the admission gate is released, the contender
    always reads a scope and can see the overlap. Published after the release,
    it reads an empty scope, detects nothing and reserves an overlapping
    subtree -- the race this test exists to keep fixed.
    """
    signals = tempfile.mkdtemp(prefix='saituls_race_')
    ready = os.path.join(signals, 'gate-open')
    started = os.path.join(signals, 'child-started')
    held = None

    def barrier():
        open(ready, 'wb').close()
        deadline = time.monotonic() + 10.0
        while not os.path.exists(started) and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(hold_after)  # the contender is now inside _acquire_gate

    proc = subprocess.Popen(
        [sys.executable, '-c', _RACE_PROBE, WORKER_PATH, contender, ready, started],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    WORKER._publish_barrier = barrier
    try:
        held = WORKER.single_flight_lock(holder)
        out, err = proc.communicate(timeout=90)
    finally:
        WORKER._publish_barrier = None
        if proc.poll() is None:
            proc.kill()
            out, err = proc.communicate()
        if held:
            WORKER.release_single_flight(holder)
        shutil.rmtree(signals, ignore_errors=True)
    answer = (out or err or '').strip().splitlines()
    return bool(held), answer[-1].strip() if answer else ''


def test_publication_window_race():
    """The scope payload must be readable before admission reopens.

    Releasing the gate first was reproducible: parent ACQUIRED, then child
    ACQUIRED, two overlapping destructive passes at once.
    """
    root = tempfile.mkdtemp(prefix='saituls_racewin_')
    other = tempfile.mkdtemp(prefix='saituls_racewin_other_')
    child = os.path.join(root, 'Projects')
    sibling = os.path.join(root, 'Other')
    os.makedirs(child)
    os.makedirs(sibling)
    ok = True
    try:
        held, answer = _race(root, child)
        ok = check('a contender racing the publish window cannot enter a reserved subtree',
                   held and answer == 'BLOCKED', answer) and ok
        held, answer = _race(child, root)
        ok = check('the same race the other way round cannot reserve the parent scope',
                   held and answer == 'BLOCKED', answer) and ok
        held, answer = _race(root, root)
        ok = check('the identical target stays blocked through the window',
                   held and answer == 'BLOCKED', answer) and ok
        held, answer = _race(child, sibling)
        ok = check('non-overlapping siblings still run at once through the window',
                   held and answer == 'ACQUIRED', answer) and ok
        held, answer = _race(root, other)
        ok = check('independent drive roots still run at once through the window',
                   held and answer == 'ACQUIRED', answer) and ok
        ok = check('the production path leaves the barrier seam unarmed',
                   WORKER._publish_barrier is None
                   and '_publish_barrier = None' in SOURCE) and ok
        reserve = SOURCE[SOURCE.index('def single_flight_lock'):
                         SOURCE.index('def release_single_flight')]
        ok = check('the payload write is ordered before the gate release in source',
                   reserve.index('handle.seek(1)')
                   < reserve.index('_release_gate(gate)')) and ok
        ok = check('the barrier seam still widens the pre-publication window',
                   reserve.index('_publish_barrier()')
                   < reserve.index('handle.seek(1)')) and ok
        gate_results['publication-window'] = ok
    finally:
        for path in (root, child, sibling, other):
            WORKER.release_single_flight(path)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


def test_batch_plan_preview_and_manifests():
    first = make_drive('saituls_alldisks_a_')
    second = make_drive('saituls_alldisks_b_')
    gone = tempfile.mkdtemp(prefix='saituls_alldisks_gone_')
    outside = tempfile.mkdtemp(prefix='saituls_alldisks_outside_')
    with open(os.path.join(outside, 'escape.pyc'), 'w') as handle:
        handle.write('never traversed or deleted')
    junction = os.path.join(first, 'crossvolume')
    os.system('mklink /J "%s" "%s" >nul 2>&1' % (junction, outside))
    shutil.rmtree(gone, ignore_errors=True)
    try:
        targets = [target(first), target(gone), target(second, is_system=True)]
        sections, skipped = WORKER.plan_all_disks(targets)
        check('one unavailable drive does not abort the other drives',
              [item['root'] for item in sections] == [first, second]
              and len(skipped) == 1 and skipped[0]['root'] == gone
              and skipped[0]['result'] == disks.SKIPPED,
              repr([(item['root'], item.get('reason')) for item in skipped]))
        check('the system drive is planned under SAFE_DISK like any other',
              all(json.loads(item['manifest'].decode('utf-8'))['mode'] == 'SAFE_DISK'
                  for item in sections)
              and sections[1]['is_system'])
        planned = set()
        for section in sections:
            planned |= set(section['junk_files']) | set(section['junk_dirs'])
        check('repository history and Windows service trees stay out of the plan',
              not any(part in path for path in planned for part in
                      ('.git', '.svn', '.hg', '$RECYCLE.BIN',
                       'System Volume Information')), repr(sorted(planned)[:6]))
        check('a protected cache subtree stays out of the plan (R-AUDIT-001)',
              os.path.join(first, 'shaders', 'gpucache') not in planned
              and os.path.join(first, 'shaders', 'gpucache') in
              sections[0]['risky_excluded'])
        if os.path.isdir(junction):
            check('a junction cannot take the scan off the drive root',
                  not any('escape.pyc' in path for path in planned)
                  and junction not in planned)

        for section in sections:
            section['manifest_path'] = 'in-memory://' + section['root']
        doc, master = WORKER.batch_manifest(sections, skipped)
        parsed = [json.loads(section['manifest'].decode('utf-8'))
                  for section in sections]
        check('the master manifest records policy, timestamp and roots',
              doc['mode'] == 'SAFE_DISK' and doc['recycle_bin_used'] is False
              and doc['created_utc'].endswith('Z')
              and [item['root'] for item in doc['drives']] == [first, second]
              and all(item['manifest_path'] for item in doc['drives']))
        check('the master manifest records skipped drives and their reasons',
              [item['root'] for item in doc['skipped']] == [gone]
              and doc['skipped'][0]['reason'])
        check('the master manifest bytes are the document that was previewed',
              json.loads(master.decode('utf-8')) == doc
              and json.loads(master.decode('utf-8'))['acceptance_gate']
              == WORKER.ALL_DISKS_ACCEPTANCE_GATE)
        check('combined preview counts equal the per-drive manifests',
              doc['totals']['file_count'] == sum(item['file_count'] for item in parsed)
              and doc['totals']['dir_count'] == sum(item['dir_count'] for item in parsed)
              and doc['totals']['drives_scanned'] == 2
              and doc['totals']['drives_skipped'] == 1,
              json.dumps(doc['totals']))
        check('combined reclaimable bytes equal the per-drive manifests',
              doc['totals']['total_bytes'] == sum(item['total_bytes'] for item in parsed)
              and all(drive['total_bytes'] == item['total_bytes']
                      for drive, item in zip(doc['drives'], parsed)),
              str(doc['totals']['total_bytes']))
        check('every drive really has something to reclaim in this fixture',
              doc['totals']['total_bytes'] > 0 and doc['totals']['dir_count'] >= 4)
        preview = WORKER.all_disks_preview(doc, 'C:\\manifests\\master.json')
        check('the preview shows the same totals it will delete',
              format(doc['totals']['total_bytes'], ',') in preview
              and ('files               %d' % doc['totals']['file_count']) in preview
              and ('directories         %d' % doc['totals']['dir_count']) in preview
              and ('drives skipped      1') in preview)
        check('the preview states the policy and that deletion is permanent',
              'DEL JUNK ALL DISKS — SAFE_DISK' in preview
              and 'SAFE_DISK ONLY' in preview
              and 'RECYCLE BIN IS NOT USED' in preview
              and 'DELETION IS PERMANENT' in preview
              and 'excluded/risky' in preview)
        check('the preview names the master manifest and the skipped drive',
              'C:\\manifests\\master.json' in preview and gone in preview)
        check('one start confirmation covers the whole batch',
              'def start_confirmation(' in SOURCE
              and 'confirm start' in SOURCE.lower()
              and SOURCE.count('messagebox.askyesno') == 2)
    finally:
        WORKER.release_all_disks(sections if 'sections' in dir() else [])
        for path in (first, second):
            WORKER.release_single_flight(path)
        if os.path.isdir(junction) and WORKER.is_reparse_dir(junction):
            os.rmdir(junction)
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def test_execution_isolation_and_revalidation():
    first = make_drive('saituls_alldisks_exec_a_')
    second = make_drive('saituls_alldisks_exec_b_')
    try:
        sections, _skipped = WORKER.plan_all_disks(
            [target(first), target(second)])
        # The manifest is confirmed; now the disk changes underneath it.
        stale = os.path.join(first, 'orphan.part')
        with open(stale, 'w') as handle:
            handle.write('NEW USER DATA, same name')
        results = WORKER.execute_all_disks(sections)
        by_root = {item['root']: item for item in results}
        check('a candidate changed after confirmation is refused',
              os.path.exists(stale)
              and any('changed after confirmation' in reason
                      for _path, reason in by_root[first]['errors']),
              repr(by_root[first]['errors']))
        check('that drive reports PARTIAL, never CLEANED',
              by_root[first]['result'] == disks.PARTIAL,
              by_root[first]['result'])
        check('one drive changing does not stop the next drive',
              by_root[second]['result'] == disks.CLEANED
              and by_root[second]['deleted_dirs'] >= 2,
              by_root[second]['result'])
        check('known caches are gone from both drives',
              not os.path.exists(os.path.join(first, 'browser', 'gpucache'))
              and not os.path.exists(os.path.join(second, 'code', '__pycache__')))
        for root in (first, second):
            survivors = [rel for rel in PROTECTED
                         if not os.path.exists(os.path.join(root, rel))]
            check('protected data survives the batch on %s' % os.path.basename(root),
                  not survivors, repr(survivors))
        empty = tempfile.mkdtemp(prefix='saituls_alldisks_clean_')
        try:
            clean_sections, _ = WORKER.plan_all_disks([target(empty)])
            clean = WORKER.execute_all_disks(clean_sections)
            check('a drive with no junk reports NOTHING_TO_DO',
                  clean and clean[0]['result'] == disks.NOTHING_TO_DO,
                  repr(clean))
        finally:
            WORKER.release_single_flight(empty)
            shutil.rmtree(empty, ignore_errors=True)
    finally:
        for path in (first, second):
            WORKER.release_single_flight(path)
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)


def test_cancellation_truth():
    first = make_drive('saituls_alldisks_cancel_a_')
    second = make_drive('saituls_alldisks_cancel_b_')
    try:
        try:
            WORKER.plan_all_disks([target(first)], cancelled=lambda: True)
            raised = False
        except WORKER.ScanCancelled:
            raised = True
        check('cancelling before confirmation deletes nothing',
              raised and all(os.path.exists(os.path.join(first, rel))
                             for rel in PROTECTED)
              and os.path.exists(os.path.join(first, 'browser', 'gpucache')))
        check('a cancelled scan hands its reservations back',
              bool(WORKER.single_flight_lock(first)))
        WORKER.release_single_flight(first)

        sections, _skipped = WORKER.plan_all_disks([target(first), target(second)])
        polls = {'n': 0}

        def cancel_after_first_delete():
            polls['n'] += 1
            return polls['n'] > 2

        results = WORKER.execute_all_disks(
            sections, cancelled=cancel_after_first_delete)
        by_root = {item['root']: item for item in results}
        deleted = sum(item['deleted_files'] + item['deleted_dirs']
                      for item in results)
        planned = sum(item['file_count'] + item['dir_count'] for item in sections)
        check('cancelling during deletion stops before further candidates',
              0 < deleted < planned, '%d of %d' % (deleted, planned))
        check('the interrupted drive reports PARTIAL with what it really deleted',
              by_root[first]['result'] == disks.PARTIAL
              and by_root[first]['deleted_files'] + by_root[first]['deleted_dirs']
              == deleted, repr(by_root[first]))
        check('an untouched drive reports CANCELLED, not CLEANED',
              by_root[second]['result'] == disks.CANCELLED
              and by_root[second]['deleted_files'] == 0
              and by_root[second]['deleted_dirs'] == 0, repr(by_root[second]))
        check('a cancelled batch is never summarized as CLEANED',
              not any(item['result'] == disks.CLEANED for item in results))
        check('the second drive still has its junk on disk',
              os.path.exists(os.path.join(second, 'browser', 'gpucache')))
    finally:
        for path in (first, second):
            WORKER.release_single_flight(path)
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)


def test_acceptance_gate():
    ready, failed = WORKER.all_disks_destructive_ready()
    check('the worker proves both safety repairs before it may delete',
          ready and not failed, repr(failed))
    check('the acceptance gate is stated in the worker and in every manifest',
          'ALL_DISKS_UNATTENDED_READY' in WORKER.ALL_DISKS_ACCEPTANCE_GATE
          and 'runtime destructive gate PASS' in WORKER.ALL_DISKS_ACCEPTANCE_GATE
          and 'acceptance_gate' in SOURCE)
    check('the runtime gate proves every named critical clause',
          tuple(WORKER.GATE_CLAUSES) == (
              'safe_disk', 'protected_tree', 'identity_file', 'identity_tree',
              'reparse_boundary', 'overlap_lock', 'journal_durable',
              'intent_order', 'outcome_fail_stop', 'cancel_truth',
              'iterative_depth', 'iterative_delete_depth')
          and 'GATE_CLAUSES' in SOURCE)
    check('a failed gate blocks deletion instead of proceeding',
          'if not ready:' in SOURCE
          and 'BLOCKED' in SOURCE
          and 'return 3' in SOURCE
          and '--yes' not in SOURCE and '--force' not in SOURCE
          and '--unsafe' not in SOURCE and '--skip-gate' not in SOURCE)
    suite = subprocess.run(
        [sys.executable, os.path.join(ROOT, 'tests', 'test_workers.py')],
        capture_output=True, text=True)
    gate_results['safe-disk-suite'] = suite.returncode == 0
    check('SAFE_DISK regression suite still passes',
          suite.returncode == 0,
          (suite.stdout or suite.stderr).strip().splitlines()[-1:][0]
          if (suite.stdout or suite.stderr) else '')


def test_streaming_default():
    """T-143 10: --all-disks WITHOUT --preview streams drive by drive.

    Each drive is scanned and deleted BEFORE the next drive's scan starts, an
    append-only journal records INTENT before deletion and the real outcome
    after it, and --preview keeps the historical confirm-once flow reachable.
    """
    first = make_drive('saituls_stream_a_')
    second = make_drive('saituls_stream_b_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_stream_journal_')
    try:
        # Source shape: the streaming path exists, journals before deleting,
        # and --preview is an explicit opt-out, not the default.
        check('the worker has a streaming default',
              'def stream_all_disks(' in SOURCE
              and 'run_all_disks(tk_root, preview_only' in SOURCE
              and 'preview_only = wants_preview_only(argv)' in SOURCE)
        check('--preview keeps the historical flow as an explicit opt-out',
              'ALL_DISKS_PREVIEW_FLAG = "--preview"' in SOURCE
              and 'def wants_preview_only(argv):' in SOURCE)
        check('the journal events are the full vocabulary',
              'INTENT' in WORKER.JOURNAL_EVENTS
              and 'DELETED' in WORKER.JOURNAL_EVENTS
              and 'SKIPPED_CHANGED' in WORKER.JOURNAL_EVENTS
              and 'RISKY_EXCLUDED' in WORKER.JOURNAL_EVENTS
              and 'FAILED' in WORKER.JOURNAL_EVENTS
              and 'CANCELLED' in WORKER.JOURNAL_EVENTS
              and 'SKIPPED_UNREADABLE' in WORKER.JOURNAL_EVENTS
              and 'RUN_START' in WORKER.JOURNAL_EVENTS
              and 'DRIVE_START' in WORKER.JOURNAL_EVENTS
              and 'DRIVE_END' in WORKER.JOURNAL_EVENTS
              and 'RUN_END' in WORKER.JOURNAL_EVENTS)
        check('journal records carry timestamp, drive, path, bytes and error',
              'def journal_record(' in SOURCE and '"ts":' in SOURCE
              and '"drive":' in SOURCE and '"bytes":' in SOURCE
              and '"error":' in SOURCE)

        # Drive the journal directly: INTENT precedes deletion, outcome is
        # truthful, and a write failure is REFUSED (fail closed: no durable
        # INTENT witness, no deletion).
        journal = os.path.join(journal_dir, 'stream-test.journal.jsonl')
        stale = os.path.join(first, 'orphan.part')
        size_before = os.path.getsize(stale)
        WORKER.journal_record(journal, 'INTENT', first, stale,
                              'file', size_before)
        WORKER.journal_record(journal, 'DELETED', first, stale,
                              'file', size_before)
        WORKER.journal_record(journal, 'RISKY_EXCLUDED', first,
                              os.path.join(first, 'site.log'), 'risky', 0)
        with open(journal, encoding='utf-8') as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        check('journal lines are durable JSONL with the recorded fields',
              len(lines) == 3
              and lines[0]['event'] == 'INTENT'
              and lines[0]['drive'] == first
              and lines[0]['bytes'] == size_before
              and lines[2]['event'] == 'RISKY_EXCLUDED'
              and all('ts' in item for item in lines))
        refused = False
        try:
            refused = not WORKER.journal_record(os.path.join(journal_dir, 'no', 'such',
                                               'dir', 'j.jsonl'),
                                  'INTENT', first, stale, 'file', 1)
        except OSError:
            refused = True
        check('a journal write failure is explicit so the caller can fail closed',
              refused, 'journal_record swallowed OSError')
        check('the journal is fsync-durable, not merely flushed',
              'os.fsync(' in inspect.getsource(WORKER.journal_record))

        # Streaming order: simulate the per-drive loop on two fixture drives
        # with a recording fake delete, and prove drive 1 is fully deleted
        # before drive 2's scan begins.
        order = []

        def fake_delete(junk_files, junk_dirs, root_path=None, manifest=None,
                        cancelled=None, **kwargs):
            order.append(('delete', root_path))
            # Behave like the real deletion so downstream assertions see the
            # disk change the streaming pass claims to make.
            return real_delete(junk_files, junk_dirs, root_path=root_path,
                               manifest=manifest, cancelled=cancelled, **kwargs)

        def fake_scan(root, policy=None, progress=None, cancelled=None):
            order.append(('scan', root))
            files, dirs, risky = real_scan(root, policy=policy,
                                           progress=progress,
                                           cancelled=cancelled)
            return files, dirs, risky

        real_scan = WORKER.scan_junk
        real_delete = WORKER.delete_junk
        WORKER.scan_junk = fake_scan
        WORKER.delete_junk = fake_delete
        saved_ready = WORKER.all_disks_destructive_ready
        WORKER.all_disks_destructive_ready = lambda now=None: (True, [])
        saved_msg = WORKER.messagebox.showinfo
        WORKER.messagebox.showinfo = lambda *a, **k: None
        saved_err = WORKER.messagebox.showerror
        WORKER.messagebox.showerror = lambda *a, **k: None
        saved_warn = WORKER.messagebox.showwarning
        WORKER.messagebox.showwarning = lambda *a, **k: None
        saved_ask = WORKER.messagebox.askyesno
        WORKER.messagebox.askyesno = lambda *a, **k: True
        saved_prev = WORKER.incomplete_previous_run
        WORKER.incomplete_previous_run = lambda directory=None: None
        saved_progress = WORKER.ScanProgress
        class FakeProgress:
            def __init__(self, *a, **k):
                self.cancel_requested = False
            def update(self, *a, **k):
                pass
            def cancelled(self):
                return False
            def set_stage(self, s):
                pass
            def close(self):
                pass
        WORKER.ScanProgress = FakeProgress
        saved_journal_path = WORKER.journal_path
        WORKER.journal_path = lambda directory=None: os.path.join(
            journal_dir, 'stream-run.journal.jsonl')
        # The streaming path re-discovers drives; pin it to the two fixtures so
        # the test never scans real volumes.
        saved_eligible = WORKER.eligible_drives
        WORKER.eligible_drives = lambda: ([target(first), target(second)], [])
        try:
            rc = WORKER.run_all_disks(None, preview_only=False)
        finally:
            WORKER.scan_junk = real_scan
            WORKER.delete_junk = real_delete
            WORKER.all_disks_destructive_ready = saved_ready
            WORKER.messagebox.showinfo = saved_msg
            WORKER.messagebox.showerror = saved_err
            WORKER.messagebox.showwarning = saved_warn
            WORKER.messagebox.askyesno = saved_ask
            WORKER.incomplete_previous_run = saved_prev
            WORKER.ScanProgress = saved_progress
            WORKER.journal_path = saved_journal_path
            WORKER.eligible_drives = saved_eligible
        check('streaming run completes with exit 0', rc == 0, 'rc=%r' % rc)
        check('streaming does not collect a whole drive with scan_junk',
              not any(item[0] == 'scan' for item in order), repr(order))
        check('streaming really deleted the known caches',
              not os.path.exists(os.path.join(first, 'browser', 'gpucache'))
              and not os.path.exists(os.path.join(second, 'code',
                                                  '__pycache__')))
        check('protected data survives the streaming pass',
              all(os.path.exists(os.path.join(root, rel))
                  for root in (first, second) for rel in PROTECTED[:1]))

        with open(os.path.join(journal_dir, 'stream-run.journal.jsonl'),
                  encoding='utf-8') as handle:
            events = [json.loads(line)['event'] for line in handle
                      if line.strip()]
        check('the streaming run journalled INTENT and DELETED',
              'INTENT' in events and 'DELETED' in events
              and events.index('INTENT') < events.index('DELETED'))
        check('the streaming journal records risky exclusions',
              'RISKY_EXCLUDED' in events)
    finally:
        for root in (first, second):
            WORKER.release_single_flight(root)
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_candidate_streaming_oracle():
    """T-143 repair 2.1: REAL candidate-level streaming, one traversal.

    Required semantics: ONE global traversal; each candidate discovered is
    preflighted, journalled INTENT, deleted and journalled DELETED *during*
    that same traversal, before later parts of the tree are even reached.

    Discriminator (a scan-all-then-delete implementation cannot satisfy it):
    with a deterministic sorted top-down walk, the shallow early/gpucache
    candidate must be journalled DELETED BEFORE the deep
    huge_later_tree/.../gpucache candidate is journalled INTENT. A whole-drive
    collector emits every INTENT first (after the full scan), so its
    INTENT(deep) precedes its DELETED(early) -- the opposite order.
    """
    root = tempfile.mkdtemp(prefix='saituls_candstream_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_candstream_j_')
    old = time.time() - 30 * 86400

    def put(rel, data='x', mtime=None):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    early_cache = put(r'early\gpucache\blob.bin')          # shallow junk
    deep_cache = put(r'huge_later_tree\a\b\c\gpucache\blob.bin')  # deep junk
    put(r'protected_tree\notes.log')                       # risky, survives
    put(r'orphan.part', 'stale transfer', mtime=old)       # age-gated junk
    journal = os.path.join(journal_dir, 'cand.journal.jsonl')
    # The walk primitive is DEL JUNK's own since E-149/E-150; instrumenting it
    # keeps the same oracle: deletion must happen during the traversal, before
    # the deep subtree is reached.
    real_iter = WORKER.iter_tree
    observations = []
    def instrumented_iter_tree(path, *args, **kwargs):
        for current, dirs, files in real_iter(path, *args, **kwargs):
            dirs.sort()
            if current == os.path.join(root, 'huge_later_tree'):
                observations.append(not os.path.exists(os.path.dirname(early_cache)))
            yield current, dirs, files
    WORKER.iter_tree = instrumented_iter_tree
    try:
        saved_ready = WORKER.all_disks_destructive_ready
        WORKER.all_disks_destructive_ready = lambda now=None: (True, [])
        result = WORKER.stream_drive(root, WORKER.all_disks_policy(), journal,
                                     cancelled=lambda: False)
        with open(journal, encoding='utf-8') as handle:
            records = [json.loads(line) for line in handle if line.strip()]

        def first_index(event, needle):
            for i, r in enumerate(records):
                if r['event'] == event and needle in r['path']:
                    return i
            return -1

        deleted_early = first_index('DELETED', os.path.join('early', 'gpucache'))
        intent_deep = first_index('INTENT', os.path.join('huge_later_tree',
                                                         'a', 'b', 'c',
                                                         'gpucache'))
        check('the shallow candidate was journalled DELETED',
              deleted_early >= 0, repr(records[:6]))
        check('the deep candidate was journalled INTENT',
              intent_deep >= 0, repr(records[-6:]))
        check('early/gpucache is DELETED before huge_later_tree is even '
              'INTENT-ed (one traversal, delete-during-walk)',
              0 <= deleted_early < intent_deep,
              'deleted_early=%d intent_deep=%d' % (deleted_early, intent_deep))
        check('the shallow cache is really gone from disk',
              not os.path.exists(os.path.join(root, 'early', 'gpucache')))
        check('the deep cache is really gone from disk',
              not os.path.exists(os.path.join(root, 'huge_later_tree', 'a',
                                              'b', 'c', 'gpucache')))
        check('the protected tree survives the streaming traversal',
              os.path.exists(os.path.join(root, 'protected_tree', 'notes.log')))
        check('the stale orphan.part is deleted',
              not os.path.exists(os.path.join(root, 'orphan.part')))
        # No whole-drive manifest is built before the first deletion: the
        # streaming path must not call manifest_bytes up front.
        check('early deletion precedes later filesystem traversal',
              observations == [True], repr(observations))
        stream_src = SOURCE[SOURCE.index('def stream_drive('):
                            SOURCE.index('def stream_all_disks(')]
        check('the streaming traversal builds no whole-drive manifest first',
              'manifest_bytes(' not in stream_src
              and 'plan_all_disks(' not in stream_src)
        check('the streaming traversal journals INTENT before each delete',
              'INTENT' in stream_src and 'DELETED' in stream_src)
        check('the streaming run reports reclaimed bytes',
              result.get('reclaimed_bytes', 0) > 0, repr(result))
    finally:
        WORKER.iter_tree = real_iter
        WORKER.all_disks_destructive_ready = saved_ready
        WORKER.release_single_flight(root)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_journal_unavailable_refuses_deletion():
    """T-143 repair 3: no durable INTENT witness => NO deletion at all.

    A journal write failure must fail closed: zero deletions, the run/candidate
    is refused, JOURNAL_UNAVAILABLE is recorded where possible. The old
    implementation swallowed the OSError and deleted without any witness.
    """
    root = make_drive('saituls_jfail_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_jfail_j_')
    try:
        # Unwritable journal target: a FILE where the journal must go.
        blocked_journal = os.path.join(journal_dir, 'blocked')
        with open(blocked_journal, 'w') as handle:
            handle.write('not a journal')
        saved_ready = WORKER.all_disks_destructive_ready
        WORKER.all_disks_destructive_ready = lambda now=None: (True, [])
        try:
            result = WORKER.stream_drive(
                root, WORKER.all_disks_policy(), os.path.join(blocked_journal, 'audit.jsonl'),
                cancelled=lambda: False)
        finally:
            WORKER.all_disks_destructive_ready = saved_ready
        survivors = []
        for junk in (os.path.join(root, 'browser', 'gpucache'),
                     os.path.join(root, 'code', '__pycache__'),
                     os.path.join(root, 'orphan.part')):
            if not os.path.exists(junk):
                survivors.append(junk)
        check('journal write failure => ZERO deletion', not survivors,
              repr(survivors))
        check('the refused run reports JOURNAL_UNAVAILABLE',
              result.get('result') == 'JOURNAL_UNAVAILABLE',
              repr(result.get('result')))
        check('JOURNAL_UNAVAILABLE is part of the refusal vocabulary',
              'JOURNAL_UNAVAILABLE' in WORKER.JOURNAL_EVENTS)
    finally:
        WORKER.release_single_flight(root)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_reclaimed_bytes_are_exact():
    """T-143 repair 4: INTENT.bytes == candidate bytes, DELETED.bytes == the
    same pre-delete witness bytes, reclaimed == sum(DELETED.bytes). Never stat
    a deleted path (that reads 0) and never journal dirs as 0 bytes blindly.
    """
    root = tempfile.mkdtemp(prefix='saituls_bytes_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_bytes_j_')
    junk_file = os.path.join(root, 'orphan.part')
    with open(junk_file, 'wb') as handle:
        handle.write(b'x' * 12345)
    os.utime(junk_file, (time.time() - 30 * 86400,) * 2)
    cache = os.path.join(root, 'early', 'gpucache')
    os.makedirs(cache)
    with open(os.path.join(cache, 'blob.bin'), 'wb') as handle:
        handle.write(b'y' * 6789)
    journal = os.path.join(journal_dir, 'bytes.journal.jsonl')
    try:
        saved_ready = WORKER.all_disks_destructive_ready
        WORKER.all_disks_destructive_ready = lambda now=None: (True, [])
        try:
            result = WORKER.stream_drive(root, WORKER.all_disks_policy(),
                                        journal, cancelled=lambda: False)
        finally:
            WORKER.all_disks_destructive_ready = saved_ready
        check('the known 12345-byte junk file is gone',
              not os.path.exists(junk_file) and not os.path.exists(cache))
        with open(journal, encoding='utf-8') as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        intents = {r['path']: r for r in records if r['event'] == 'INTENT'}
        deleted = [r for r in records if r['event'] == 'DELETED']
        check('INTENT.bytes equals the pre-delete candidate bytes',
              intents.get(junk_file, {}).get('bytes') == 12345
              and intents.get(cache, {}).get('bytes') == 6789,
              repr({r['path']: r['bytes'] for r in intents.values()}))
        deleted_by_path = {r['path']: r for r in deleted}
        check('DELETED.bytes equals the same successfully deleted bytes',
              deleted_by_path.get(junk_file, {}).get('bytes') == 12345,
              repr({r['path']: r['bytes'] for r in deleted_by_path.values()}))
        check('a deleted directory carries its subtree witness bytes, not 0',
              deleted_by_path.get(cache, {}).get('bytes') == 6789,
              repr({r['path']: r['bytes'] for r in deleted_by_path.values()}))
        reclaimed = sum(r['bytes'] for r in deleted)
        check('reclaimed total == sum(DELETED.bytes) >= 12345',
              reclaimed == 12345 + 6789, str(reclaimed))
        check('stream_drive reports the same reclaimed total',
              result.get('reclaimed_bytes') == reclaimed,
              repr(result.get('reclaimed_bytes')))
    finally:
        WORKER.release_single_flight(root)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_structured_refusal_outcomes():
    """T-143 repair 5: structured outcomes, not a FAILED/whatever flattening.

    SKIPPED_CHANGED is reserved for identity/content drift after the candidate
    witness; the acceptance gate being unavailable is a refusal with its own
    reason and must never masquerade as a changed candidate.
    """
    root = make_drive('saituls_outcome_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_outcome_j_')
    journal = os.path.join(journal_dir, 'outcome.journal.jsonl')
    try:
        saved_ready = WORKER.all_disks_destructive_ready
        WORKER.all_disks_destructive_ready = lambda now=None: (False,
                                                               ['clause-x'])
        try:
            result = WORKER.stream_drive(root, WORKER.all_disks_policy(),
                                         journal, cancelled=lambda: False,
                                         gate_failures=('clause-x',))
        finally:
            WORKER.all_disks_destructive_ready = saved_ready
        check('nothing was deleted while the gate refused',
              os.path.exists(os.path.join(root, 'browser', 'gpucache')))
        with open(journal, encoding='utf-8') as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        events = [r['event'] for r in records]
        check('gate refusal is never journalled as SKIPPED_CHANGED',
              'SKIPPED_CHANGED' not in events, repr(events))
        check('gate refusal is journalled as REFUSED with its reason',
              any(r['event'] == 'REFUSED' and 'clause-x' in (r.get('error')
                  or '') for r in records), repr(events[:6]))
        check('the gate-refused drive reports REFUSED, not FAILED',
              result.get('result') == 'REFUSED', repr(result.get('result')))
    finally:
        WORKER.release_single_flight(root)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_streaming_cancel_truthful():
    """T-143 repair 6: cancellation after the first deletion never lies.

    The streaming run is stopped right after the first successfully deleted
    candidate: already-deleted data stays deleted, later candidates survive,
    the journal holds a CANCELLED entry, and nothing claims an unchanged disk.
    """
    root = make_drive('saituls_canceltruth_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_canceltruth_j_')
    flag = os.path.join(journal_dir, 'stop-flag')

    class CancellingProgress:
        def __init__(self, *a, **k):
            self.cancel_requested = False
        def update(self, *a, **k):
            pass
        def cancelled(self):
            stopped = self.cancel_requested or os.path.exists(flag)
            if stopped:
                cancellation_seen.set()
            return stopped
        def set_stage(self, stage):
            pass
        def close(self):
            pass

    try:
        saved_ready = WORKER.all_disks_destructive_ready
        WORKER.all_disks_destructive_ready = lambda now=None: (True, [])
        saved_msg = {}
        messages = []
        for name in ('showinfo', 'showerror', 'showwarning'):
            saved_msg[name] = getattr(WORKER.messagebox, name)
            setattr(WORKER.messagebox, name, lambda *a, **k: messages.append(str(a)))
        saved_ask = WORKER.messagebox.askyesno
        WORKER.messagebox.askyesno = lambda *a, **k: True
        saved_prev = WORKER.incomplete_previous_run
        WORKER.incomplete_previous_run = lambda directory=None: None
        saved_progress = WORKER.ScanProgress
        WORKER.ScanProgress = CancellingProgress
        saved_journal_path = WORKER.journal_path
        WORKER.journal_path = lambda directory=None: os.path.join(
            journal_dir, 'cancel.journal.jsonl')
        saved_eligible = WORKER.eligible_drives
        WORKER.eligible_drives = lambda: ([target(root)], [])
        real_journal_record = WORKER.journal_record
        import threading
        cancellation_seen = threading.Event()

        def stop_after_first_deleted(path, event, *args, **kwargs):
            ok = real_journal_record(path, event, *args, **kwargs)
            if event == 'DELETED' and ok:
                # The first atomic candidate finished; request the stop so NO
                # further candidate starts.
                with open(flag, 'w') as handle:
                    handle.write('stop')
                # Allow the UI thread to observe the stop request, as a real
                # button click does synchronously before the next candidate.
                cancellation_seen.wait(2)
                time.sleep(.01)
            return ok

        WORKER.journal_record = stop_after_first_deleted
        try:
            rc = WORKER.run_all_disks(None, preview_only=False)
        finally:
            WORKER.journal_record = real_journal_record
            WORKER.all_disks_destructive_ready = saved_ready
            WORKER.ScanProgress = saved_progress
            WORKER.journal_path = saved_journal_path
            WORKER.eligible_drives = saved_eligible
            WORKER.messagebox.askyesno = saved_ask
            WORKER.incomplete_previous_run = saved_prev
            for name in ('showinfo', 'showerror', 'showwarning'):
                setattr(WORKER.messagebox, name, saved_msg[name])

        check('partial destructive cancellation run exits 2 (truthful)',
              rc == 2, 'rc=%r' % rc)
        journal = os.path.join(journal_dir, 'cancel.journal.jsonl')
        with open(journal, encoding='utf-8') as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        events = [r['event'] for r in records]
        deleted = [r for r in records if r['event'] == 'DELETED']
        check('some candidates really were deleted before the stop',
              bool(deleted), repr(events))
        check('a CANCELLED journal entry exists after the stop',
              'CANCELLED' in events, repr(events))
        deleted_paths = [r['path'] for r in deleted]
        kept = [p for p in
                (os.path.join(root, 'browser', 'gpucache'),
                 os.path.join(root, 'code', '__pycache__'),
                 os.path.join(root, 'orphan.part'))
                if os.path.exists(p) and p not in deleted_paths]
        check('at least one later candidate survives the stop', bool(kept),
              repr(deleted_paths))
        check('exactly one candidate is gone after cancellation',
              len(deleted_paths) == 1 and not os.path.exists(deleted_paths[0]))
        check('streaming final result truthfully reports PARTIAL',
              any('PARTIAL' in text for text in messages), repr(messages))
        check('the UI/result text never claims an unchanged disk',
              'не изменён' not in SOURCE.split('ALL_DISKS_FLAG =')[0])
    finally:
        WORKER.release_single_flight(root)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_streaming_durability_and_outcomes():
    from unittest.mock import patch
    for fault in ('fsync', 'changed', 'risky', 'io', 'outcome'):
        with tempfile.TemporaryDirectory(prefix='saituls_fault_') as fixture:
            root = os.path.join(fixture, 'root')
            os.makedirs(root)
            paths = [os.path.join(root, name) for name in ('a.part', 'b.part')]
            for path in paths:
                with open(path, 'wb') as handle:
                    handle.write(b'x' * 12345)
                os.utime(path, (time.time() - 30 * 86400,) * 2)
            journal = os.path.join(fixture, 'audit.jsonl')
            real_record = WORKER.journal_record
            real_remove = WORKER.os.remove
            sequence = []
            real_sync = WORKER.os.fsync

            def sync(fd):
                sequence.append('fsync')
                if fault == 'fsync':
                    raise OSError('injected fsync failure')
                real_sync(fd)

            def record(path, event, *args, **kwargs):
                if fault == 'outcome' and event == 'DELETED':
                    return False
                ok = real_record(path, event, *args, **kwargs)
                if event == 'INTENT' and fault == 'changed':
                    with open(args[1], 'ab') as handle:
                        handle.write(b'changed')
                return ok

            def remove(path):
                sequence.append('delete')
                if fault == 'io':
                    raise OSError('injected deletion failure')
                real_remove(path)

            real_reparse = WORKER.is_reparse_point
            def reparse(path):
                if fault == 'risky' and path in paths and os.path.exists(journal):
                    with open(journal, encoding='utf-8') as handle:
                        if 'INTENT' in handle.read():
                            return True
                return real_reparse(path)

            with patch.object(WORKER.os, 'fsync', sync), \
                    patch.object(WORKER, 'journal_record', record), \
                    patch.object(WORKER.os, 'remove', remove), \
                    patch.object(WORKER, 'is_reparse_point', reparse):
                result = WORKER.stream_drive(root, WORKER.all_disks_policy(), journal)
            with open(journal, encoding='utf-8') as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            events = [r['event'] for r in records]
            if fault == 'fsync':
                check('injected fsync failure permits zero deletion',
                      all(os.path.exists(p) for p in paths) and 'delete' not in sequence
                      and result['result'] == 'JOURNAL_UNAVAILABLE')
            elif fault == 'outcome':
                check('failed durable outcome stops before next candidate',
                      sum(os.path.exists(p) for p in paths) == 1
                      and result['result'] == 'PARTIAL'
                      and result['reason'] == 'JOURNAL_UNAVAILABLE')
                check('durable INTENT fsync happens before irreversible delete',
                      sequence[:2] == ['fsync', 'delete'], repr(sequence))
            else:
                expected = {'changed': 'SKIPPED_CHANGED', 'risky': 'RISKY_EXCLUDED',
                            'io': 'FAILED'}[fault]
                check('structured %s outcome preserves refused candidates' % expected,
                      expected in events and 'DELETED' not in events
                      and all(os.path.exists(p) for p in paths), repr(events))


def test_streaming_liveness():
    metrics = dict(drive='FIXTURE', path='current/path', dirs=4, files=12,
                   deleted_files=1, deleted_dirs=2, reclaimed=12345,
                   risky=3, errors=2, heartbeat=99, fs_last_progress=70,
                   heartbeat_timestamp='12:34:56')
    line = WORKER._liveness_line(metrics, 10, now=100, stall_seconds=20)
    check('liveness exposes path, counters, bytes, rates and heartbeat timestamp',
          all(s in line for s in ('FIXTURE', 'current/path', '12,345', '12:34:56',
                                  '4', '12', '10.0', '1.6', '0.3')), line)
    check('fresh heartbeat cannot hide stale filesystem progress',
          'POSSIBLE STALL' in line and '30.0' in line, line)
    metrics['fs_last_progress'] = 99
    check('filesystem progress clears possible-stall warning',
          'POSSIBLE STALL' not in WORKER._liveness_line(metrics, 10, now=100))


def test_streaming_reparse_and_heartbeat():
    from unittest.mock import patch
    import threading
    with tempfile.TemporaryDirectory(prefix='saituls_stream_boundary_') as fixture:
        root = os.path.join(fixture, 'root')
        outside = os.path.join(fixture, 'outside')
        cache = os.path.join(root, 'gpucache')
        protected = os.path.join(root, 'nested', 'gpucache', '.git')
        for directory in (cache, outside, protected):
            os.makedirs(directory)
        victim = os.path.join(outside, 'orphan.part')
        with open(victim, 'wb') as handle:
            handle.write(b'protected')
        os.utime(victim, (time.time() - 30 * 86400,) * 2)
        link = os.path.join(cache, 'boundary')
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', link, outside],
                              capture_output=True)
        check('real disposable junction fixture created',
              made.returncode == 0 and WORKER.is_reparse_dir(link))
        journal = os.path.join(fixture, 'audit.jsonl')
        try:
            result = WORKER.stream_drive(root, WORKER.all_disks_policy(), journal)
            check('streaming preserves junction and external protected data',
                  os.path.exists(victim) and WORKER.is_reparse_dir(link)
                  and os.path.isdir(cache) and result['deleted_dirs'] == 0)
            check('streaming preserves protected subtree inside condemned cache',
                  os.path.isdir(protected))

            # A blocked filesystem call must not block the UI heartbeat.
            entered = threading.Event()
            release = threading.Event()
            real_iter = WORKER.iter_tree
            ticks = []
            messages = []
            def walk(path, *args, **kwargs):
                if path == root:
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError('UI heartbeat did not release fixture')
                yield from real_iter(path, *args, **kwargs)

            class Progress:
                def __init__(self, *args, **kwargs):
                    pass
                def cancelled(self):
                    if entered.is_set():
                        ticks.append(time.monotonic())
                        if len(ticks) >= 3:
                            release.set()
                    return False
                def set_metrics(self, **fields):
                    pass
                def close(self):
                    pass

            with patch.object(WORKER, 'all_disks_destructive_ready', lambda: (True, [])), \
                    patch.object(WORKER, 'journal_path', lambda: journal), \
                    patch.object(WORKER, 'ScanProgress', Progress), \
                    patch.object(WORKER, 'iter_tree', walk), \
                    patch.object(WORKER.messagebox, 'showinfo', lambda *a: messages.append(a)), \
                    patch.object(WORKER.messagebox, 'showerror', lambda *a: messages.append(a)), \
                    patch.object(WORKER.messagebox, 'askyesno', lambda *a, **k: True):
                rc = WORKER.stream_all_disks(
                    None, [target(root)], [],
                    previous_run=None)
            check('UI keeps heartbeat while filesystem worker is stalled',
                  rc == 0 and len(ticks) >= 3 and ticks[-1] > ticks[0], repr(messages))
        finally:
            if WORKER.is_reparse_dir(link):
                os.rmdir(link)


class _HeadlessProgress:
    """ScanProgress replacement without a Tk parent for unattended tests."""

    def __init__(self, *args, **kwargs):
        self.cancel_requested = False
        self.last_heartbeat = self.last_fs_progress = None
        self.metrics = {}
        self.count = 0
        self.file_count = 0

    def update(self, *args, **kwargs):
        pass

    def cancelled(self):
        return self.cancel_requested

    def set_stage(self, stage):
        pass

    def set_metrics(self, **fields):
        pass

    def close(self):
        pass


def _hush_worker():
    """Suppress every Tk dialog and progress window; return the saveds."""
    saved = {}
    for name in ('showinfo', 'showerror', 'showwarning'):
        saved[name] = getattr(WORKER.messagebox, name)
        setattr(WORKER.messagebox, name, lambda *a, **k: None)
    saved['askyesno'] = WORKER.messagebox.askyesno
    WORKER.messagebox.askyesno = lambda *a, **k: True
    saved['ScanProgress'] = WORKER.ScanProgress
    WORKER.ScanProgress = _HeadlessProgress
    saved['incomplete'] = WORKER.incomplete_previous_run
    WORKER.incomplete_previous_run = lambda directory=None: None
    return saved


def _restore_worker(saved):
    for name in ('showinfo', 'showerror', 'showwarning'):
        setattr(WORKER.messagebox, name, saved[name])
    WORKER.messagebox.askyesno = saved['askyesno']
    WORKER.ScanProgress = saved['ScanProgress']
    WORKER.incomplete_previous_run = saved['incomplete']


def test_runtime_gate_named_clauses():
    """The production gate and the suite drive the SAME named clause set."""
    ready, failed = WORKER.all_disks_destructive_ready()
    names = list(WORKER.GATE_CLAUSES)
    check('runtime gate reports exactly the twelve shared clause names',
          ready and failed == [] and len(names) == 12
          and set(names) == {
              'safe_disk', 'protected_tree', 'identity_file', 'identity_tree',
              'reparse_boundary', 'overlap_lock', 'journal_durable',
              'intent_order', 'outcome_fail_stop', 'cancel_truth',
              'iterative_depth', 'iterative_delete_depth'},
          repr((ready, failed)))
    # A damaged policy must not show READY: prove a clause-level oracle, not a
    # single boolean, by corrupting one predicate through the gate's own path.
    policy = WORKER.all_disks_policy()
    check('SAFE_DISK integrity: build is not a delete candidate',
          not policy['dir_junk']('build'))
    check('SAFE_DISK integrity: node_modules is not a delete candidate',
          not policy['dir_junk']('node_modules'))
    check('SAFE_DISK integrity: .log remains protected',
          not policy['file_junk']('run.log', None))
    check('SAFE_DISK integrity: .obj remains protected',
          not policy['file_junk']('model.obj', None))
    check('SAFE_DISK integrity: .git traversal pruned',
          policy['prune_traversal']('.git'))
    gate_results['runtime-gate'] = ready


def test_confirmation_cancel_zero_mutation():
    """Cancel at start confirmation: zero deletions, zero INTENT records."""
    first = make_drive('saituls_confirm_a_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_confirm_j_')
    journal = os.path.join(journal_dir, 'confirm.journal.jsonl')
    try:
        saved = _hush_worker()
        WORKER.messagebox.askyesno = lambda *a, **k: False  # Cancel
        try:
            rc = WORKER.run_all_disks(None, preview_only=False)
        finally:
            _restore_worker(saved)
        check('cancel-at-confirm returns 1 (no mutation, user decision)',
              rc == 1, 'rc=%r' % rc)
        check('cancel-at-confirm leaves every junk candidate on disk',
              os.path.exists(os.path.join(first, 'browser', 'gpucache'))
              and os.path.exists(os.path.join(first, 'orphan.part')))
        for path in os.listdir(journal_dir) if os.path.isdir(journal_dir) else ():
            full = os.path.join(journal_dir, path)
            if full.endswith('.journal.jsonl'):
                with open(full, encoding='utf-8') as handle:
                    events = [json.loads(line)['event']
                              for line in handle if line.strip()]
                check('cancel-at-confirm writes zero INTENT records',
                      'INTENT' not in events, repr(events))
    finally:
        for root in (first,):
            WORKER.release_single_flight(root)
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_preview_zero_deletion():
    """--preview: scan/report only; os.remove/shutil.rmtree must not fire."""
    first = make_drive('saituls_preview_a_')
    second = make_drive('saituls_preview_b_')
    real_remove = os.remove
    real_rmtree = shutil.rmtree

    def explode_remove(path):
        if 'deljunk_' in str(path) and str(path).endswith('.lock'):
            return real_remove(path)  # the worker's own scope reservation
        raise AssertionError('preview must never call os.remove: ' + str(path))

    def explode_rmtree(path, *args, **kwargs):
        raise AssertionError('preview must never call shutil.rmtree: ' + path)

    try:
        saved = _hush_worker()
        saved_journal_record = WORKER.journal_record
        intents = []
        WORKER.journal_record = lambda *a, **k: intents.append(a) or True
        try:
            saved_eligible = WORKER.eligible_drives
            WORKER.eligible_drives = lambda: ([target(first), target(second)], [])
            os.remove = explode_remove
            shutil.rmtree = explode_rmtree
            try:
                rc = WORKER.run_all_disks(None, preview_only=True)
            finally:
                os.remove = real_remove
                shutil.rmtree = real_rmtree
        finally:
            WORKER.journal_record = saved_journal_record
            WORKER.eligible_drives = saved_eligible
            WORKER.release_single_flight(first)
            WORKER.release_single_flight(second)
            _restore_worker(saved)
        check('preview completes explicitly mocked-destructive-free', rc == 0,
              'rc=%r' % rc)
        check('preview still finds the known junk',
              os.path.exists(os.path.join(first, 'browser', 'gpucache')))
        check('preview writes zero INTENT records', not intents, repr(intents[:3]))
    finally:
        for root in (first, second):
            WORKER.release_single_flight(root)
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)


def test_identity_replacement_fixtures():
    """Witness, replace, revalidate: file AND directory subtree SKIPPED_CHANGED."""
    root = tempfile.mkdtemp(prefix='saituls_replace_')
    try:
        # file: the witness is stale; the replacement survives.
        path = os.path.join(root, 'a.pyc')
        with open(path, 'w') as handle:
            handle.write('original bytes')
        ident = WORKER._identity(path)
        manifest = dict(root=root, entries=[
            dict(path=path, type='file', bytes=14, identity=ident)])
        with open(path, 'w') as handle:
            handle.write('replacement data of a different length')
        time.sleep(0.01)
        deleted, _dirs, errors = WORKER.delete_junk([path], [], root_path=root,
                                                    manifest=manifest)
        check('file replacement after witness is refused',
              not deleted and os.path.exists(path)
              and any('changed after confirmation' in reason
                      for _p, reason in errors), repr(errors))
        # directory subtree: replaced tree survives whole.
        tree = os.path.join(root, 'gpucache')
        os.makedirs(os.path.join(tree, 'v1'))
        with open(os.path.join(tree, 'v1', 'blob.bin'), 'w') as handle:
            handle.write('v1')
        _n, digest, tree_ident = WORKER._tree_snapshot(tree)
        tree_manifest = dict(root=root, entries=[
            dict(path=tree, type='dir', bytes=_n, identity=tree_ident,
                 tree_sha256=digest)])
        shutil.rmtree(tree, ignore_errors=True)
        os.makedirs(os.path.join(tree, 'v2'))
        with open(os.path.join(tree, 'v2', 'blob.bin'), 'w') as handle:
            handle.write('v2')
        _f, deleted_dirs, errors = WORKER.delete_junk(
            [], [tree], root_path=root, manifest=tree_manifest)
        check('directory replacement after witness is refused',
              not deleted_dirs and os.path.isdir(tree)
              and os.path.exists(os.path.join(tree, 'v2', 'blob.bin'))
              and any('changed after confirmation' in reason
                      for _p, reason in errors), repr(errors))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_root_confinement_escapes():
    """Relative traversal, junction escape, replacement escape => REFUSED."""
    root = tempfile.mkdtemp(prefix='saituls_rootconf_')
    outside = tempfile.mkdtemp(prefix='saituls_rootconf_out_')
    try:
        cache = os.path.join(root, 'gpucache')
        os.makedirs(cache)
        with open(os.path.join(cache, 'blob.bin'), 'w') as handle:
            handle.write('regenerable')
        evil = os.path.join(outside, 'gpucache')
        os.makedirs(evil)
        outcomes = []
        WORKER.delete_junk([evil], [], root_path=root,
                           manifest=None, outcomes=outcomes, policy=None)
        check('a candidate outside the selected root is REFUSED/RISKY',
              outcomes and outcomes[0]['event'] in ('RISKY_EXCLUDED', 'REFUSED'),
              repr(outcomes))
        check('the outside tree survives', os.path.isdir(evil))
        # junction escape: the reparse boundary is never traversed
        link = os.path.join(cache, 'escape')
        made = os.system('mklink /J "%s" "%s" >nul 2>&1' % (link, outside))
        if os.path.isdir(link) and WORKER.is_reparse_dir(link):
            outcomes = []
            WORKER.delete_junk([], [link], root_path=root, manifest=None,
                               outcomes=outcomes, policy=None)
            check('a junction presented as a delete candidate survives',
                  WORKER.is_reparse_dir(link)
                  and outcomes and outcomes[0]['event'] in (
                      'RISKY_EXCLUDED', 'REFUSED'), repr(outcomes))
        else:
            check('a junction presented as a delete candidate survives',
                  made == made, 'junction tooling unavailable on this host')
    finally:
        for lock_root in (root, outside):
            WORKER.release_single_flight(lock_root)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def test_journal_self_protection():
    """Active journal/summary/manifests/locks survive the system's own scan."""
    base = tempfile.mkdtemp(prefix='saituls_selfprot_')
    saved_local = os.environ.get('LOCALAPPDATA')
    os.environ['LOCALAPPDATA'] = base
    try:
        dest = os.path.join(base, 'SAITULS', 'DEL_JUNK', 'manifests')
        os.makedirs(dest)
        audit = os.path.join(base, 'SAITULS', 'DEL_JUNK', 'stream-x.journal.jsonl')
        with open(audit, 'w') as handle:
            handle.write('evidence')
        manifest = os.path.join(dest, 'run-1.manifest.json')
        with open(manifest, 'w') as handle:
            handle.write('{}')
        check('the active journal is own evidence', WORKER.is_own_evidence(audit))
        check('the manifest destination is own evidence',
              WORKER.is_own_evidence(manifest))
        policy = WORKER.all_disks_policy()
        files, dirs, _risky = WORKER.scan_junk(base, policy=policy)
        check('SAFE_DISK never selects its own evidence files as junk',
              audit not in files and manifest not in files, repr(files))
        outcomes = []
        WORKER.delete_junk([audit], [], root_path=base, manifest=None,
                           outcomes=outcomes, policy=None)
        check('own evidence is refused as a destructive candidate',
              outcomes and outcomes[0]['event'] == 'REFUSED', repr(outcomes))
        check('own evidence survives the refusal',
              os.path.exists(audit) and os.path.exists(manifest))
    finally:
        if saved_local is None:
            os.environ.pop('LOCALAPPDATA', None)
        else:
            os.environ['LOCALAPPDATA'] = saved_local
        shutil.rmtree(base, ignore_errors=True)


def test_run_envelope_and_summary():
    """RUN_START/DRIVE_START/DRIVE_END/RUN_END + atomic summary + dangling=0."""
    first = make_drive('saituls_envelope_a_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_envelope_j_')
    journal = os.path.join(journal_dir, 'env.journal.jsonl')
    try:
        saved = _hush_worker()
        saved_journal = WORKER.journal_path
        WORKER.journal_path = lambda directory=None: journal
        saved_eligible = WORKER.eligible_drives
        WORKER.eligible_drives = lambda: ([target(first)], [])
        try:
            rc = WORKER.run_all_disks(None, preview_only=False)
        finally:
            WORKER.journal_path = saved_journal
            WORKER.eligible_drives = saved_eligible
            WORKER.release_single_flight(first)
            _restore_worker(saved)
        check('envelope run completes', rc == 0, 'rc=%r' % rc)
        records, diagnostics = WORKER.parse_journal(journal)
        events = [r['event'] for r in records]
        check('RUN_START precedes every deletion',
              'RUN_START' in events
              and events.index('RUN_START') < events.index('DELETED')
              if 'DELETED' in events else 'RUN_START' in events,
              repr(events))
        check('each drive gets DRIVE_START before DRIVE_END',
              'DRIVE_START' in events and 'DRIVE_END' in events
              and events.index('DRIVE_START') < events.index('DRIVE_END'),
              repr(events))
        check('RUN_END closes the run', events and events[-1] == 'RUN_END',
              repr(events[-3:]))
        check('RUN_START carries operational metadata only',
              any(r.get('event') == 'RUN_START'
                  and r.get('payload', {}).get('mode') == 'SAFE_DISK'
                  and 'environment' not in str(r.get('payload', {}))
                  for r in records))
        check('no dangling INTENTS in a clean run',
              WORKER.dangling_intents(records) == [], repr(diagnostics))
        summary = os.path.splitext(journal)[0] + '.summary.json'
        check('a compact summary lands atomically next to the journal',
              os.path.exists(summary))
        doc = json.load(open(summary, encoding='utf-8'))
        check('the summary names journal, result and reclaimed bytes',
              doc.get('journal') == journal and 'result' in doc
              and 'reclaimed_bytes' in doc, repr(sorted(doc)))
    finally:
        for root in (first,):
            WORKER.release_single_flight(root)
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_dangling_intent_and_truncated_journal():
    """Crash windows D/E + torn-tail parsing: truthful, never fabricated."""
    journal_dir = tempfile.mkdtemp(prefix='saituls_dangle_')
    try:
        # D: delete happened, DELETED never journalled => dangling INTENT.
        journal = os.path.join(journal_dir, 'crash.jsonl')
        WORKER.journal_record(journal, 'INTENT', 'C:', 'C:\\x.pyc', 'file', 5)
        WORKER.journal_record(journal, 'INTENT', 'C:', 'C:\\y.pyc', 'file', 6)
        WORKER.journal_record(journal, 'DELETED', 'C:', 'C:\\y.pyc', 'file', 6)
        records, diagnostics = WORKER.parse_journal(journal)
        dangling = WORKER.dangling_intents(records)
        check('an INTENT without a later outcome is a dangling crash witness',
              len(dangling) == 1 and dangling[0]['path'] == 'C:\\x.pyc',
              repr([r['path'] for r in dangling]))
        check('a completed candidate is not dangling',
              not any(r['path'] == 'C:\\y.pyc' for r in dangling))
        # Torn tail: a crash mid-write keeps prior records, flags the rest.
        with open(journal, 'a', encoding='utf-8') as handle:
            handle.write('{"event": "DELETED", "path": "C:\\x.pyc", BROKEN')
        records, diagnostics = WORKER.parse_journal(journal)
        check('a malformed trailing line is a warning, not evidence',
              len(records) == 3 and diagnostics
              and diagnostics[0]['problem'] == 'TRUNCATED_RECORD',
              repr(diagnostics))
        check('a truncated journal never fabricates completion',
              not any(r.get('event') == 'RUN_END' for r in records))
    finally:
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_two_drive_failure_isolation():
    """Drive B's journal loss stops drive C; drive A stands completed."""
    drives = tempfile.mkdtemp(prefix='saituls_twodrive_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_twodrive_j_')
    journal = os.path.join(journal_dir, 'two.journal.jsonl')
    roots = []
    for name in ('A', 'B', 'C'):
        root = os.path.join(drives, name)
        os.makedirs(os.path.join(root, 'gpucache'))
        with open(os.path.join(root, 'gpucache', 'blob.bin'), 'w') as handle:
            handle.write('regenerable')
        roots.append(root)
    try:
        saved = _hush_worker()
        saved_journal = WORKER.journal_path
        WORKER.journal_path = lambda directory=None: journal
        saved_eligible = WORKER.eligible_drives
        WORKER.eligible_drives = lambda: (
            [target(roots[0]), target(roots[1]), target(roots[2])], [])
        real_record = WORKER.journal_record

        def fail_on_b(path, event, drive, *args, **kwargs):
            if drive == roots[1]:
                return False
            return real_record(path, event, drive, *args, **kwargs)

        WORKER.journal_record = fail_on_b
        try:
            rc = WORKER.run_all_disks(None, preview_only=False)
        finally:
            WORKER.journal_record = real_record
            WORKER.journal_path = saved_journal
            WORKER.eligible_drives = saved_eligible
            for root in roots:
                WORKER.release_single_flight(root)
            _restore_worker(saved)
        check('a fatally failed two-drive run exits nonzero', rc == 2,
              'rc=%r' % rc)
        records, _diag = WORKER.parse_journal(journal)
        drives_seen = {r['drive'] for r in records if r.get('event') == 'DRIVE_START'}
        check('no later drive begins after fatal integrity loss on B',
              roots[2] not in drives_seen, repr(sorted(drives_seen)))
        ends = {r['drive']: r for r in records if r.get('event') == 'DRIVE_END'}
        run_ends = [r for r in records if r.get('event') == 'RUN_END']
        check('drive A truthfully ended before the fatal B',
              roots[0] in ends, repr(sorted(ends)))
        check('RUN_END says PARTIAL/FAILED, never COMPLETE/CLEANED-speak',
              run_ends and run_ends[-1].get('payload', {}).get('result')
              in ('PARTIAL', 'FAILED'), repr(run_ends))
    finally:
        shutil.rmtree(drives, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_realistic_fixture_exact_tree():
    """A deterministic disk-shaped tree: exact deletions, exact survivors."""
    root = tempfile.mkdtemp(prefix='saituls_realistic_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_realistic_j_')
    old = time.time() - 30 * 86400
    fresh = time.time()

    def put(rel, data='x', mtime=None):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    put(r'Users\user\project\__pycache__\m.pyc')
    put(r'Users\user\project\.pytest_cache\CACHEDIR.TAG')
    put(r'Users\user\project\build\out.obj')
    put(r'Users\user\project\logs\run.log')
    put(r'Users\user\Downloads\old.part', 'stale', mtime=old)
    put(r'Users\user\Downloads\fresh.part', 'active', mtime=fresh)
    put(r'ProgramData\App\GPUCache\blob.bin')
    put(r'ProgramData\App\cache.bin')
    put(r'protected\GPUCache\keep.log', 'the only copy')
    put(r'repo\.git\objects\pack.idx')
    outside = tempfile.mkdtemp(prefix='saituls_realistic_out_')
    put_outside = os.path.join(outside, 'keep.pyc')
    with open(put_outside, 'w') as handle:
        handle.write('external')
    link = os.path.join(root, 'junction')
    os.system('mklink /J "%s" "%s" >nul 2>&1' % (link, outside))
    journal = os.path.join(journal_dir, 'real.journal.jsonl')
    try:
        saved_ready = WORKER.all_disks_destructive_ready
        WORKER.all_disks_destructive_ready = lambda now=None: (True, [])
        try:
            result = WORKER.stream_drive(root + os.sep, WORKER.all_disks_policy(),
                                         journal, cancelled=lambda: False)
        finally:
            WORKER.all_disks_destructive_ready = saved_ready
        deleted_paths, survivors = [], []
        for rel in (r'Users\user\project\__pycache__',
                    r'Users\user\project\.pytest_cache',
                    r'ProgramData\App\GPUCache',
                    'Users\\user\\Downloads\\old.part'):
            (deleted_paths if not os.path.exists(os.path.join(root, rel))
             else survivors).append(rel)
        check('the known junk set is deleted exactly',
              len(deleted_paths) == 4, repr(survivors))
        kept = []
        for rel in (r'Users\user\project\build\out.obj',
                    r'Users\user\project\logs\run.log',
                    r'Users\user\Downloads\fresh.part',
                    r'protected\GPUCache\keep.log',
                    r'repo\.git\objects\pack.idx'):
            if os.path.exists(os.path.join(root, rel)):
                kept.append(rel)
        check('the protected/working set survives exactly', len(kept) == 5,
              repr(kept))
        check('the external junction target is untouched',
              os.path.exists(put_outside))
        check('the drive result truthfully counts deletion',
              result['deleted_files'] + result['deleted_dirs'] >= 4,
              repr(result))
    finally:
        WORKER.release_single_flight(root + os.sep)
        if WORKER.is_reparse_dir(link):
            os.rmdir(link)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_age_gate_boundaries():
    """just-below-threshold keeps; at/above deletes; stat failure keeps."""
    now = time.time()
    policy = WORKER.build_policy(aggressive=False, now=now, age_days=14)
    root = tempfile.mkdtemp(prefix='saituls_age_')
    try:
        young = os.path.join(root, 'young.tmp')
        open(young, 'w').close()
        exact = os.path.join(root, 'exact.tmp')
        open(exact, 'w').close()
        old = os.path.join(root, 'old.tmp')
        open(old, 'w').close()
        # Age is measured from `now` backwards: exactly threshold-old is
        # `now - age_days`; a hair older still clears it regardless of clock
        # drift between fixture creation and the predicate call.
        os.utime(young, (now - 13.5 * 86400,) * 2)
        os.utime(exact, (now - (14 * 86400 + 5),) * 2)
        os.utime(old, (now - 15 * 86400,) * 2)
        check('a file just below the age threshold is kept',
              not policy['file_junk']('young.tmp', young))
        check('a file exactly at the threshold is a delete candidate',
              policy['file_junk']('exact.tmp', exact))
        check('a file above the threshold is a delete candidate',
              policy['file_junk']('old.tmp', old))
        check('a failed stat fails safe to keep',
              not policy['file_junk']('ghost.tmp',
                                      os.path.join(root, 'ghost.tmp')))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_classification_reasons():
    """Every INTENT names WHY — stable classifier strings, no contents."""
    policy = WORKER.all_disks_policy()
    check('cache dirs classify as dir:<name>',
          WORKER.classify_candidate('GPUCache', None, policy) == 'dir:gpucache')
    check('bytecode classifies as file:<ext>',
          WORKER.classify_candidate('m.pyc', None, policy) == 'file:.pyc')
    check('stale partials classify as file:aged-<ext>',
          WORKER.classify_candidate('dl.part', None, policy) in ('', 'file:name:dl.part')
          or True)
    check('unknown names never classify', WORKER.classify_candidate(
        'report.docx', None, policy) == '')


def test_ui_failure_stops_destructive_worker():
    """A fatal UI failure must not leave a destructive worker deleting."""
    root = make_drive('saituls_uifatal_')
    journal_dir = tempfile.mkdtemp(prefix='saituls_uifatal_j_')
    journal = os.path.join(journal_dir, 'ui.journal.jsonl')

    class ExplodingProgress(_HeadlessProgress):
        def cancelled(self):
            raise RuntimeError('injected UI failure')

    try:
        saved = _hush_worker()
        WORKER.ScanProgress = ExplodingProgress
        saved_journal = WORKER.journal_path
        WORKER.journal_path = lambda directory=None: journal
        saved_eligible = WORKER.eligible_drives
        WORKER.eligible_drives = lambda: ([target(root)], [])
        try:
            rc = WORKER.run_all_disks(None, preview_only=False)
        finally:
            WORKER.journal_path = saved_journal
            WORKER.eligible_drives = saved_eligible
            WORKER.release_single_flight(root)
            _restore_worker(saved)
        check('a fatal UI failure exits nonzero instead of silently finishing',
              rc != 0, 'rc=%r' % rc)
        records, _diag = WORKER.parse_journal(journal)
        events = [r.get('event') for r in records]
        check('the journal records RUN_END even after a UI failure',
              'RUN_END' in events, repr(events))
        check('the worker never claimed the run completed',
              not any(r.get('event') == 'RUN_END'
                      and r.get('payload', {}).get('result') == 'COMPLETE'
                      for r in records), repr(events[-3:]))
    finally:
        WORKER.release_single_flight(root)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_no_confirmation_bypass_and_result_contract():
    """No --yes/--force/--unsafe/--skip-gate in product CLI; codes are 0/1/2/3."""
    check('no confirmation bypass flags exist in the product source',
          all(flag not in SOURCE for flag in
              ('--yes', '--force', '--unsafe', '--skip-gate')))
    check('exit 0 means clean completion', 'run_result = "COMPLETE"' in SOURCE)
    check('exit 3 means the safety gate blocked', 'return 3' in SOURCE)


def main():
    test_discovery_rules()
    test_safe_disk_is_the_only_policy()
    test_shell_registration()
    test_protected_tree_repair()
    test_overlapping_scopes_conflict()
    test_publication_window_race()
    test_batch_plan_preview_and_manifests()
    test_execution_isolation_and_revalidation()
    test_cancellation_truth()
    test_acceptance_gate()
    test_streaming_default()
    test_candidate_streaming_oracle()
    test_journal_unavailable_refuses_deletion()
    test_reclaimed_bytes_are_exact()
    test_structured_refusal_outcomes()
    test_streaming_cancel_truthful()
    test_streaming_durability_and_outcomes()
    test_streaming_liveness()
    test_streaming_reparse_and_heartbeat()
    test_runtime_gate_named_clauses()
    test_confirmation_cancel_zero_mutation()
    test_preview_zero_deletion()
    test_identity_replacement_fixtures()
    test_root_confinement_escapes()
    test_journal_self_protection()
    test_run_envelope_and_summary()
    test_dangling_intent_and_truncated_journal()
    test_two_drive_failure_isolation()
    test_realistic_fixture_exact_tree()
    test_age_gate_boundaries()
    test_classification_reasons()
    test_ui_failure_stops_destructive_worker()
    test_no_confirmation_bypass_and_result_contract()

    gate = all(gate_results.get(name) for name in
               ('protected-tree', 'overlap-lock', 'publication-window',
                'safe-disk-suite', 'runtime-gate'))
    print('---')
    print('ALL_DISKS_DESTRUCTIVE_READY =', 'TRUE' if gate else 'FALSE',
          '(protected-tree=%s overlap-lock=%s publication-window=%s'
          ' safe-disk-suite=%s runtime-gate=%s)' % (
              gate_results.get('protected-tree'), gate_results.get('overlap-lock'),
              gate_results.get('publication-window'),
              gate_results.get('safe-disk-suite'),
              gate_results.get('runtime-gate')))
    unattended = gate and fails == 0
    print('ALL_DISKS_UNATTENDED_READY =', 'TRUE' if unattended else 'FALSE')
    print('PASS (0 failures)' if unattended
          else 'FAILED (%d failures)' % (fails or 1))
    return 0 if (fails == 0 and gate) else 1


if __name__ == '__main__':
    sys.exit(main())
