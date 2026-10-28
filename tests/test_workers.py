#!/usr/bin/env python3
"""Regression tests for the destructive Explorer workers.

Run:  python tests/test_workers.py
Exit: 0 = all PASS, 1 = failures.

These workers delete without the Recycle Bin, so their failure mode is
unrecoverable. Both cases here were reproduced by an external audit against the
previous implementation:

1. DEL_DUP hashed a file, waited an unbounded amount of time for the user to
   confirm, then deleted the path without looking at it again. A file that
   stopped being a duplicate during that pause was still destroyed.
2. DEL_EMPTY collected per-directory failures, but the ``os.listdir`` that could
   actually fail sat OUTSIDE the try -- so an access-denied directory killed the
   process after earlier directories had already been removed, with no log and
   no summary.

The confirmation dialog is where the interesting mutation has to happen, so
these tests replace ``messagebox.askyesno`` with a function that changes the
filesystem and then answers Yes. That is the real race, executed deterministically.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + os.sep + '..')
SCRIPTS = os.path.join(ROOT, 'Scripts')

fails = 0


def check(name, cond, detail=''):
    global fails
    if cond:
        print('PASS ', name, ('  -> ' + detail) if detail else '')
    else:
        fails += 1
        print('FAIL ', name, '  -> ' + detail)


def load_worker(filename, module_name):
    """Import a .PYW worker by path (the extension blocks a normal import)."""
    path = os.path.join(SCRIPTS, filename)
    spec = importlib.util.spec_from_loader(
        module_name, importlib.machinery.SourceFileLoader(module_name, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


class FakeMessagebox:
    """Stands in for tkinter.messagebox and runs a hook at the confirm point."""

    def __init__(self, answer=True, on_ask=None):
        self.answer = answer
        self.on_ask = on_ask
        self.asked = None
        self.shown = []

    def askyesno(self, title, message, **kw):
        self.asked = message
        if self.on_ask:
            self.on_ask()
        return self.answer

    def showinfo(self, title, message, **kw):
        self.shown.append(message)

    def showerror(self, title, message, **kw):
        self.shown.append(message)


class FakeTk:
    def __init__(self, *a, **kw):
        pass

    def withdraw(self):
        pass


def run_del_dup(work_dir, on_confirm=None, answer=True, box=None):
    """Run DEL_DUP.main() against work_dir with a scripted confirmation."""
    module = load_worker('DEL_DUP.PYW', 'del_dup_under_test')
    if box is None:
        box = FakeMessagebox(answer=answer, on_ask=on_confirm)
    module.messagebox = box
    module.tk = type('tk', (), {'Tk': FakeTk})
    argv = sys.argv
    sys.argv = ['DEL_DUP.PYW', work_dir]
    try:
        module.main()
    finally:
        sys.argv = argv
    return box


def test_del_dup_toctou():
    work = tempfile.mkdtemp(prefix='saituls_deldup_')
    try:
        keep = os.path.join(work, 'a.bin')
        dupe = os.path.join(work, 'b.bin')
        with open(keep, 'wb') as fh:
            fh.write(b'X' * 100)
        shutil.copyfile(keep, dupe)

        # os.walk order decides which of the two is "first" and therefore kept.
        # Whichever one the worker picked as redundant is the one to sabotage, so
        # read the plan out of the dialog text instead of assuming. The box is
        # passed in rather than closed over, because the hook runs from inside
        # the constructor's own askyesno.
        state = {}

        def sabotage():
            target = dupe if dupe in (state['box'].asked or '') else keep
            state['target'] = target
            with open(target, 'wb') as fh:
                fh.write(b'Y' * 100)   # same size, different content

        box = FakeMessagebox(answer=True, on_ask=sabotage)
        state['box'] = box
        run_del_dup(work, box=box)
        survivors = sorted(os.listdir(work))
        check('a candidate that changed during confirmation is NOT deleted',
              len(survivors) == 2, 'survivors=%s' % survivors)
        check('the changed candidate is reported as skipped',
              any('Пропущено' in m for m in box.shown),
              (box.shown or ['<no message>'])[-1].replace('\n', ' | ')[:120])
        check('the changed bytes are still on disk',
              os.path.exists(state['target'])
              and open(state['target'], 'rb').read() == b'Y' * 100,
              'the replacement content was preserved, not destroyed'
              if os.path.exists(state['target'])
              else 'the changed file was DELETED: %s' % state['target'])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_del_dup_still_deletes_real_duplicates():
    work = tempfile.mkdtemp(prefix='saituls_deldup_ok_')
    try:
        keep = os.path.join(work, 'a.bin')
        dupe = os.path.join(work, 'b.bin')
        with open(keep, 'wb') as fh:
            fh.write(b'Z' * 200)
        shutil.copyfile(keep, dupe)
        digest = sha256(keep)

        box = run_del_dup(work)
        survivors = sorted(os.listdir(work))
        check('an unchanged duplicate is still deleted',
              len(survivors) == 1, 'survivors=%s' % survivors)
        check('the surviving copy is the intact one',
              survivors and sha256(os.path.join(work, survivors[0])) == digest,
              'content preserved')
        check('nothing is reported as skipped on the clean path',
              not any('Пропущено' in m for m in box.shown),
              (box.shown or ['<no message>'])[-1].replace('\n', ' | ')[:80])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_del_dup_declined():
    work = tempfile.mkdtemp(prefix='saituls_deldup_no_')
    try:
        for name in ('a.bin', 'b.bin'):
            with open(os.path.join(work, name), 'wb') as fh:
                fh.write(b'Q' * 50)
        run_del_dup(work, answer=False)
        check('answering No deletes nothing',
              len(os.listdir(work)) == 2, 'both files remain')
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_del_empty_error_boundary():
    module = load_worker('DEL_EMPTY.PYW', 'del_empty_under_test')
    work = tempfile.mkdtemp(prefix='saituls_delempty_')
    try:
        for name in ('one', 'two', 'three'):
            os.mkdir(os.path.join(work, name))

        real_listdir = os.listdir
        state = {'calls': 0}

        def flaky_listdir(path):
            # Fail on the second emptiness check: by then one directory has
            # already been removed, which is what made the old crash so bad.
            if os.path.dirname(str(path)) == work:
                state['calls'] += 1
                if state['calls'] == 2:
                    raise PermissionError('fixture-denied')
            return real_listdir(path)

        module.os.listdir = flaky_listdir
        try:
            removed, removed_dirs, failed = module.remove_empty_dirs(work)
        finally:
            module.os.listdir = real_listdir

        check('an enumeration error does not abort the run',
              removed >= 1, 'removed=%d' % removed)
        check('the failing directory is reported, not lost',
              len(failed) == 1 and 'fixture-denied' in failed[0][1],
              'failed=%s' % (failed,))
        check('the remaining directories are still processed',
              removed == 2, 'removed=%d of 3 (one was made to fail)' % removed)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_del_empty_removes_nested():
    module = load_worker('DEL_EMPTY.PYW', 'del_empty_nested')
    work = tempfile.mkdtemp(prefix='saituls_delempty_nested_')
    try:
        deep = os.path.join(work, 'a', 'b', 'c')
        os.makedirs(deep)
        keeper = os.path.join(work, 'kept')
        os.mkdir(keeper)
        with open(os.path.join(keeper, 'file.txt'), 'w') as fh:
            fh.write('x')

        removed, removed_dirs, failed = module.remove_empty_dirs(work)
        check('nested empty directories are removed bottom-up',
              removed == 3 and not os.path.exists(os.path.join(work, 'a')),
              'removed=%d' % removed)
        check('a directory holding a file is kept',
              os.path.isdir(keeper) and not failed,
              'failed=%s' % (failed,))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_del_junk_prunes_condemned_subtrees():
    """DEL_JUNK must not walk into a directory it has already condemned.

    The scan was bottom-up, which cannot prune, so every descendant of a doomed
    ``node_modules`` was visited and each junk file inside it was listed
    separately -- thousands of stat calls and list entries for one ``rmtree``.
    """
    module = load_worker('DEL_JUNK.PYW', 'del_junk_prune')
    work = tempfile.mkdtemp(prefix='saituls_deljunk_')
    try:
        # One condemned directory with a deep, wide subtree ...
        nm = os.path.join(work, 'node_modules')
        descendants = 0
        for i in range(40):
            deep = os.path.join(nm, 'pkg%02d' % i, 'dist', 'chunks')
            os.makedirs(deep)
            descendants += 3
            for j in range(10):
                with open(os.path.join(deep, 'part%02d.log' % j), 'w') as fh:
                    fh.write('x')
        # ... and one junk file that lives OUTSIDE it and must still be found.
        loose = os.path.join(work, 'src')
        os.makedirs(loose)
        loose_log = os.path.join(loose, 'app.log')
        with open(loose_log, 'w') as fh:
            fh.write('keep finding me')

        real_walk = module.os.walk
        visited = []

        def recording_walk(path, **kw):
            # The yielded dirs list must be the SAME object the real walk reads
            # back, or the in-place pruning under test would be invisible here.
            for root, dirs, files in real_walk(path, **kw):
                visited.append(root)
                yield root, dirs, files

        module.os.walk = recording_walk
        try:
            junk_files, junk_dirs, _risky = module.scan_junk(work)
        finally:
            module.os.walk = real_walk

        inside = [p for p in visited if p.startswith(nm + os.sep)]
        check('no descendant of a condemned directory is visited',
              not inside and nm not in visited,
              'visited %d dirs, %d inside node_modules (%d descendants exist)'
              % (len(visited), len(inside), descendants))
        check('the condemned directory itself is one deletion target',
              junk_dirs == [nm], '%d junk_dirs' % len(junk_dirs))
        check('a junk file outside any junk directory is still found',
              junk_files == [loose_log], '%d junk_files' % len(junk_files))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_del_junk_deletes_what_it_found():
    module = load_worker('DEL_JUNK.PYW', 'del_junk_delete')
    work = tempfile.mkdtemp(prefix='saituls_deljunk_del_')
    try:
        cache = os.path.join(work, '__pycache__')
        os.makedirs(os.path.join(cache, 'nested'))
        with open(os.path.join(cache, 'nested', 'x.pyc'), 'w') as fh:
            fh.write('x')
        keep = os.path.join(work, 'notes.txt')
        with open(keep, 'w') as fh:
            fh.write('user data')
        stray = os.path.join(work, 'run.log')
        with open(stray, 'w') as fh:
            fh.write('log')

        junk_files, junk_dirs, _risky = module.scan_junk(work)
        deleted_files, deleted_dirs, errors = module.delete_junk(junk_files, junk_dirs)
        check('pruned scan still removes the whole condemned subtree',
              not os.path.exists(cache) and deleted_dirs == [cache],
              'deleted_dirs=%s errors=%s' % (deleted_dirs, errors))
        check('a loose junk file is removed and user data is not',
              deleted_files == [stray] and os.path.exists(keep),
              '%d deleted_files' % len(deleted_files))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_del_same_never_overwrites_a_collision_target():
    """DEL_SAME must never move onto an existing path.

    The old move_contents_up fell back exactly once to `<name>_copy`, so a
    pre-existing user `a_copy.txt` was silently replaced by the nested file --
    and this worker bypasses the Recycle Bin.
    """
    module = load_worker('DEL_SAME.PYW', 'del_same_collision')
    work = tempfile.mkdtemp(prefix='saituls_delsame_')
    try:
        parent = os.path.join(work, 'Folder')
        inner = os.path.join(parent, 'Folder')
        os.makedirs(inner)
        with open(os.path.join(inner, 'a.txt'), 'w') as fh:
            fh.write('INNER-UNIQUE')
        with open(os.path.join(parent, 'a.txt'), 'w') as fh:
            fh.write('PARENT-KEEP')
        with open(os.path.join(parent, 'a_copy.txt'), 'w') as fh:
            fh.write('SENTINEL-KEEP')

        module.flatten_duplicate_folders(work)

        def content(name):
            p = os.path.join(parent, name)
            return open(p).read() if os.path.exists(p) else '<absent>'

        check('the parent original is untouched',
              content('a.txt') == 'PARENT-KEEP', content('a.txt'))
        check('the pre-existing _copy file is untouched',
              content('a_copy.txt') == 'SENTINEL-KEEP', content('a_copy.txt'))
        survivors = sorted(os.listdir(parent))
        check('the nested file landed under a fresh collision-free name',
              len(survivors) == 3 and content('a_copy_2.txt') == 'INNER-UNIQUE',
              'survivors=%s' % survivors)

        # Multiple occupied names: a_copy and a_copy_2 both taken -> a_copy_3.
        work2 = tempfile.mkdtemp(prefix='saituls_delsame2_')
        try:
            parent2 = os.path.join(work2, 'Folder')
            inner2 = os.path.join(parent2, 'Folder')
            os.makedirs(inner2)
            with open(os.path.join(inner2, 'a.txt'), 'w') as fh:
                fh.write('NESTED')
            for n in ('a.txt', 'a_copy.txt', 'a_copy_2.txt'):
                with open(os.path.join(parent2, n), 'w') as fh:
                    fh.write('KEEP-' + n)
            module.flatten_duplicate_folders(work2)
            check('an already-occupied _copy_N chain gets the next free slot',
                  content2(os.path.join(parent2, 'a_copy_3.txt')) == 'NESTED'
                  and content2(os.path.join(parent2, 'a_copy_2.txt')) == 'KEEP-a_copy_2.txt',
                  'survivors=%s' % sorted(os.listdir(parent2)))
        finally:
            shutil.rmtree(work2, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def content2(path):
    return open(path).read() if os.path.exists(path) else '<absent>'


def test_del_junk_safe_disk_policy():
    """SAFE_DISK (drive-root) vs AGGRESSIVE_PROJECT (explicit folder).

    The user directive: a drive-root run must keep logs, dumps, build output
    dirs and generic cache/temp names, must never cross junctions, must prune
    repository metadata, and the preview byte count must equal the manifest's.
    AGGRESSIVE_PROJECT keeps the historical behaviour on project folders.
    """
    module = load_worker('DEL_JUNK.PYW', 'del_junk_safe')
    with open(os.path.join(SCRIPTS, 'DEL_JUNK.PYW'), encoding='utf-8') as fh:
        worker_source = fh.read()
    check('confirmation explicitly says deletion bypasses the Recycle Bin',
          'Корзина НЕ используется — удаление необратимо.' in worker_source, '')
    import time as _time
    now = _time.time()
    work = tempfile.mkdtemp(prefix='saituls_safedisk_')
    outside = tempfile.mkdtemp(prefix='saituls_safedisk_outside_')
    try:
        # A tree that models a volume root with the exact traps from the
        # directive, plus the repository-history contradiction.
        def put(rel, data='x', mtime=None):
            p = os.path.join(work, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w') as fh:
                fh.write(data)
            if mtime is not None:
                os.utime(p, (mtime, mtime))
            return p

        put(r'Project\target\nested\out.obj')     # whole subtrees must keep
        put(r'Project\run.log')                    # SAFE keeps, aggressive takes
        put(r'Photos\build\nested\img.o')
        put(r'Video\dist\nested\clip.idx')
        put(r'Archive\logs\nested\old.log')
        put(r'.git\logs\HEAD.log')                # .git/logs contradiction
        put(r'.git\config', '[core]')
        put(r'.svn\pristine\artifact.pyc')
        put(r'.hg\store\artifact.pyc')
        put(r'$RECYCLE.BIN\artifact.pyc')
        put(r'System Volume Information\artifact.pyc')
        put(r'site.log', 'root-level log')        # root-level log kept too
        put(r'Models\ship.obj', '3D model')       # .obj is not always build output
        put(r'downloader.part', 'partial', mtime=now - 30 * 86400)   # stale -> junk
        put(r'downloader2.part', 'active', mtime=now - 1 * 3600)     # fresh -> keep
        put(r'browser\gpucache\data.bin')         # known cache -> junk
        put(r'unsafe\gpucache\data.bin')          # junction inside -> keep whole tree
        put(r'nestedrepo\gpucache\.git\logs\keep.pyc')  # nested repo -> keep
        put(r'code\__pycache__\m.pyc')            # known cache -> junk
        with open(os.path.join(outside, 'escape.pyc'), 'w') as fh:
            fh.write('never traversed or deleted')
        linked = os.path.join(work, 'linked')
        os.makedirs(linked)
        cache_link = os.path.join(work, 'unsafe', 'gpucache', 'outside-link')
        os.makedirs(cache_link)
        try:
            os.rmdir(linked)
            os.system('mklink /J "%s" "%s" >nul 2>&1'
                      % (linked, outside))
            os.rmdir(cache_link)
            os.system('mklink /J "%s" "%s" >nul 2>&1'
                      % (cache_link, outside))
        except OSError:
            pass
        has_junction = os.path.isdir(linked) and module.is_reparse_dir(linked)

        files, dirs, risky = module.scan_junk(
            work, policy=module.build_policy(False, now))

        paths = set(files) | set(dirs)

        protected_files = [
            r'Project\target\nested\out.obj', r'Photos\build\nested\img.o',
            r'Video\dist\nested\clip.idx', r'Archive\logs\nested\old.log',
            r'.git\logs\HEAD.log', r'.git\config', r'site.log',
            r'Models\ship.obj', r'downloader2.part', r'Project\run.log',
            r'.svn\pristine\artifact.pyc', r'.hg\store\artifact.pyc',
            r'$RECYCLE.BIN\artifact.pyc',
            r'System Volume Information\artifact.pyc',
            r'unsafe\gpucache\data.bin',
            r'nestedrepo\gpucache\.git\logs\keep.pyc',
        ]
        check('.git/logs is absent from the deletion plan',
              not any(os.path.join(work, r'.git') in p for p in paths), '')
        check('VCS and Windows service trees are absent from the deletion plan',
              not any(os.path.join(work, rel) in p for p in paths for rel in
                      ('.svn', '.hg', '$RECYCLE.BIN',
                       'System Volume Information')), repr(paths))
        for label, rel in (
                ('Project/target subtree is protected', protected_files[0]),
                ('Photos/build subtree is protected', protected_files[1]),
                ('Video/dist subtree is protected', protected_files[2]),
                ('Archive/logs subtree is protected', protected_files[3]),
                ('root-level *.log is protected', protected_files[6]),
                ('3D *.obj is protected', protected_files[7]),
                ('fresh .part passes the age gate', protected_files[8])):
            check(label, os.path.join(work, rel) not in paths, '')
        check('a stale .part is condemned by the age gate',
              os.path.join(work, 'downloader.part') in paths, '')
        check('gpucache is detected', any(p.endswith('gpucache') for p in dirs), '')
        check('__pycache__ is detected', any(p.endswith('__pycache__') for p in dirs), '')
        risky_set = set(risky)
        check('all four protected directory trees are surfaced separately',
              all(os.path.join(work, rel) in risky_set for rel in
                  (r'Project\target', r'Photos\build', r'Video\dist',
                   r'Archive\logs')), '%d risky: %s' % (len(risky), risky))
        check('cache trees containing .git or junctions are surfaced, not deleted',
              os.path.join(work, r'unsafe\gpucache') in risky_set and
              os.path.join(work, r'nestedrepo\gpucache') in risky_set,
              repr(risky))
        if has_junction:
            check('the junction target was never traversed',
                  not any('escape.pyc' in p for p in paths), str(paths))

        # AGGRESSIVE_PROJECT on an explicit folder: same tree, current rules.
        files_a, dirs_a, _risky_a = module.scan_junk(
            os.path.join(work, 'Project'),
            policy=module.build_policy(True, now))
        check('AGGRESSIVE_PROJECT still takes target/ by name',
              any(p.endswith('target') for p in dirs_a), 'dirs=%s' % dirs_a)
        check('AGGRESSIVE_PROJECT still takes loose *.log files',
              os.path.join(work, r'Project\run.log') in files_a,
              'files=%s' % files_a)

        # Byte accounting: preview total == manifest total.
        total, manifest = module.manifest_bytes(
            files, dirs, risky, root_path=work, mode='SAFE_DISK')
        doc = json.loads(manifest.decode('utf-8'))
        check('preview byte count equals manifest byte count',
              doc['total_bytes'] == total and
              sum(e['bytes'] for e in doc['entries']) == total,
              'total=%s doc=%s' % (total, doc['total_bytes']))
        check('manifest counts match list lengths',
              doc['file_count'] == len(files) and doc['dir_count'] == len(dirs), '')
        check('manifest carries the risky excluded list',
              sorted(doc['risky_excluded']) == sorted(risky), '')
        check('manifest binds the root and policy',
              doc['root'] == os.path.abspath(work) and doc['mode'] == 'SAFE_DISK',
              repr((doc['root'], doc['mode'])))

        # Exercise the actual delete against the manifest. Survival assertions
        # made before deletion are worthless theatre.
        _deleted_files, _deleted_dirs, errors = module.delete_junk(
            files, dirs, root_path=work, manifest=manifest)
        check('manifested SAFE_DISK deletion completes without errors',
              not errors, repr(errors))
        check('known caches are actually deleted',
              not os.path.exists(os.path.join(work, r'browser\gpucache')) and
              not os.path.exists(os.path.join(work, r'code\__pycache__')), '')
        check('all protected user data survives the actual delete',
              all(os.path.exists(os.path.join(work, rel)) for rel in protected_files),
              repr([rel for rel in protected_files
                    if not os.path.exists(os.path.join(work, rel))]))
        check('junction target survives the actual delete',
              os.path.exists(os.path.join(outside, 'escape.pyc')), '')
    finally:
        linked = os.path.join(work, 'linked')
        if os.path.lexists(linked) and module.is_reparse_dir(linked):
            os.rmdir(linked)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def test_del_junk_manifest_toctou_and_cancel():
    module = load_worker('DEL_JUNK.PYW', 'del_junk_manifest_safety')
    work = tempfile.mkdtemp(prefix='saituls_deljunk_manifest_')
    try:
        candidate = os.path.join(work, 'old.part')
        with open(candidate, 'w') as fh:
            fh.write('OLD')
        old = __import__('time').time() - 30 * 86400
        os.utime(candidate, (old, old))
        policy = module.build_policy(False, __import__('time').time())
        files, dirs, risky = module.scan_junk(work, policy=policy)
        _total, manifest = module.manifest_bytes(
            files, dirs, risky, root_path=work, mode='SAFE_DISK')
        manifest_dir = os.path.join(work, 'manifests')
        manifest_path = module.save_manifest(manifest, work, directory=manifest_dir)
        with open(manifest_path, 'rb') as fh:
            saved_manifest = fh.read()
        check('complete manifest is durably saved before confirmation',
              os.path.isfile(manifest_path) and saved_manifest == manifest,
              manifest_path)
        with open(candidate, 'w') as fh:
            fh.write('NEW USER DATA')
        deleted_files, deleted_dirs, errors = module.delete_junk(
            files, dirs, root_path=work, manifest=manifest)
        check('a candidate changed after confirmation is not deleted',
              os.path.exists(candidate) and not deleted_files and not deleted_dirs,
              repr(errors))
        check('the changed candidate is reported',
              any('changed after confirmation' in error for _path, error in errors),
              repr(errors))

        deep = os.path.join(work, 'a', 'b', 'c')
        os.makedirs(deep)
        with open(os.path.join(deep, 'x.pyc'), 'w') as fh:
            fh.write('x')
        progress_calls = []
        try:
            module.scan_junk(
                work, policy=policy,
                progress=lambda count, total=None: progress_calls.append(count),
                cancelled=lambda: len(progress_calls) >= 2)
            cancelled = False
        except module.ScanCancelled:
            cancelled = True
        check('large scan supports progress and cancellation',
              cancelled and len(progress_calls) >= 2 and
              os.path.exists(os.path.join(deep, 'x.pyc')),
              repr(progress_calls))

        bad_destination = os.path.join(work, 'not-a-directory')
        with open(bad_destination, 'w') as fh:
            fh.write('collision')
        try:
            module.save_manifest(manifest, work, directory=bad_destination)
            failed_closed = False
        except OSError:
            failed_closed = True
        check('manifest persistence errors fail before confirmation/deletion',
              failed_closed and os.path.exists(candidate), '')
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_del_junk_single_flight_across_processes():
    module = load_worker('DEL_JUNK.PYW', 'del_junk_lock_parent')
    work = tempfile.mkdtemp(prefix='saituls_deljunk_lock_')
    worker_path = os.path.join(SCRIPTS, 'DEL_JUNK.PYW')
    lock_path = None
    child = """import importlib.machinery, importlib.util, sys
spec = importlib.util.spec_from_loader('del_junk_lock_child', importlib.machinery.SourceFileLoader('del_junk_lock_child', sys.argv[1]))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
lock = module.single_flight_lock(sys.argv[2])
print('ACQUIRED' if lock else 'BLOCKED')
if lock:
    module.release_single_flight(sys.argv[2])
"""
    try:
        lock_path = module.single_flight_lock(work)
        blocked = subprocess.check_output(
            [sys.executable, '-c', child, worker_path, work], text=True).strip()
        check('a second process cannot lock the same target',
              bool(lock_path) and blocked == 'BLOCKED', blocked)
        module.release_single_flight(work)
        acquired = subprocess.check_output(
            [sys.executable, '-c', child, worker_path, work], text=True).strip()
        check('the OS releases the target lock for the next process',
              acquired == 'ACQUIRED', acquired)
    finally:
        module.release_single_flight(work)
        shutil.rmtree(work, ignore_errors=True)
        if lock_path and os.path.exists(lock_path):
            try:
                os.remove(lock_path)
            except OSError:
                pass


def main():
    test_del_dup_toctou()
    test_del_dup_still_deletes_real_duplicates()
    test_del_dup_declined()
    test_del_empty_error_boundary()
    test_del_empty_removes_nested()
    test_del_junk_prunes_condemned_subtrees()
    test_del_junk_deletes_what_it_found()
    test_del_junk_safe_disk_policy()
    test_del_junk_manifest_toctou_and_cancel()
    test_del_junk_single_flight_across_processes()
    test_del_same_never_overwrites_a_collision_target()

    print('---')
    print('PASS (0 failures)' if fails == 0 else 'FAILED (%d failures)' % fails)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
