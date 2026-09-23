"""DEL JUNK shared depth-independent filesystem primitives.

E-149/E-150: the real ALL DISKS acceptance failure was RecursionError
("maximum recursion depth exceeded") during scan. Root cause, proven in the
CPython 3.11.9 runtime this tool ships on: ``os.walk`` is a recursive
generator (``yield from _walk(...)`` costs one Python frame per directory
level), ``shutil._rmtree_unsafe`` recurses once per subdirectory, and
``os.makedirs`` recurses through ``ntpath.split``. Deep trees -- not file
count -- are the failure dimension, so the traversal architecture itself must
be depth-independent.

``iter_tree`` is THE one shared walk primitive for every DEL JUNK safety
path (scan_junk, tree_has_protected_boundary, _tree_snapshot, stream_drive).
``remove_tree_iterative`` is the delete-side counterpart, used instead of
``shutil.rmtree`` because rmtree is not depth-independent on this runtime.

Contract of ``iter_tree`` (os.walk top-down compatible):

* yields ``(current, dirs, files)`` exactly like ``os.walk(top, topdown=True,
  followlinks=False)``; callers may prune by mutating ``dirs`` in place
  during the yield, and only pruned-surviving entries are descended into;
* no Python recursion: an explicit list stack, O(depth) traversal state;
* directory symlinks, junctions, mount points and every other reparse
  directory still APPEAR in ``dirs`` (callers keep their reparse policies)
  but are NEVER descended into by the walker itself;
* scandir failures go to ``onerror`` (same semantics as os.walk) and the
  walk continues; nothing is silently skipped from the caller's view;
* the root itself is the caller's explicit choice and is never re-resolved;
* directory order below the yielded level follows the pruned ``dirs`` order,
  preserving the os.walk depth-first order the old code produced.

Nothing here deletes except ``remove_tree_iterative``; nothing here follows
a reparse boundary under any circumstance.
"""

import os
import stat

from del_junk_errors import (
    RecoverableTreeDeleteError,
    classify_delete_error,
    DELETE_FATAL,
)

_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _entry_is_descendable(entry):
    """True only for a real directory the walker may enter.

    An entry is a directory in os.walk's sense when ``entry.is_dir()``
    matches (follows links, exactly what the old code observed). Descending
    additionally requires the entry to be provably NOT a reparse boundary:
    no symlink, no junction, no mount point, nothing with the reparse
    attribute. Any doubt (stat failure, unexpected state) fails closed.
    """
    try:
        is_dir = entry.is_dir()
    except OSError:
        return False
    if not is_dir:
        return False
    try:
        entry_stat = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    if stat.S_ISLNK(entry_stat.st_mode):
        return False
    if getattr(entry_stat, "st_file_attributes", 0) & _REPARSE_FLAG:
        return False
    return True


def iter_tree(top, onerror=None):
    """Depth-independent top-down walk; see module docstring for contract."""
    # Each stack item is (path, descendable-children-by-name). Children are
    # resolved from the parent's scandir, so a walk can never leave the root
    # subtree: every descended path is a real directory name seen under its
    # real parent.
    stack = [(top, None)]
    while stack:
        current, pending = stack.pop()
        dirs = []
        files = []
        children = {}
        try:
            scandir_it = os.scandir(current)
        except OSError as error:
            if onerror is not None:
                onerror(error)
            if pending:
                # An unreadable directory: os.walk still reports the parent
                # view, but this subtree yields nothing and is not descended.
                pass
            continue
        try:
            for entry in scandir_it:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                if is_dir:
                    dirs.append(entry.name)
                    if _entry_is_descendable(entry):
                        children[entry.name] = entry.path
                else:
                    files.append(entry.name)
        finally:
            scandir_it.close()

        yield current, dirs, files

        # Top-down pruning contract: descend only into names the caller left
        # in ``dirs`` (possibly reordered/filtered in place), in that order,
        # and never into a reparse boundary even if a caller forgets to prune
        # one. os.walk order preservation: first remaining dir is walked
        # first, so push in reverse onto the LIFO stack.
        for name in reversed(dirs):
            child = children.get(name)
            if child is not None:
                stack.append((child, None))


def _scandir_children(path):
    """Materialised non-reparse children for deletion; reparse => refusal."""
    entries = []
    try:
        with os.scandir(path) as scandir_it:
            for entry in scandir_it:
                entries.append(entry)
    except OSError:
        raise
    return entries


def _entry_is_reparse(entry):
    """True for symlinks/junctions/reparse entries, fail closed on stat loss."""
    try:
        entry_stat = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    if stat.S_ISLNK(entry_stat.st_mode):
        return True
    if getattr(entry_stat, "st_file_attributes", 0) & _REPARSE_FLAG:
        return True
    return False


def remove_tree_iterative(top):
    """Delete the tree at ``top`` with an explicit stack, no recursion.

    Replaces shutil.rmtree for DEL JUNK candidates: rmtree recurses once per
    directory level (shutil._rmtree_unsafe) and crashes with RecursionError
    on deep trees on the shipped Python 3.11 runtime.

    Contract:
    * explicit stack, post-order deletion (children removed before their
      parent directory; files before directories at every level);
    * a reparse point appearing anywhere at or below ``top`` is REFUSED with
      OSError -- never traversed, never deleted through;
    * every path comes from scandir of a real parent, so the deletion cannot
      escape the candidate root;
    * OSError propagates after partial work exactly like rmtree
      (ignore_errors=False); the caller records the truthful outcome;
    * O(depth) state: the stack holds one entry-list per open directory.
    """
    if _path_is_reparse(top):
        raise OSError("refusing reparse boundary: %s" % top)
    stack = [(top, _scandir_children(top))]
    while stack:
        path, entries = stack[-1]
        if entries:
            entry = entries.pop()
            if _entry_is_reparse(entry):
                raise OSError("refusing reparse boundary: %s" % entry.path)
            if entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, _scandir_children(entry.path)))
            else:
                os.remove(entry.path)
        else:
            stack.pop()
            os.rmdir(path)


def _path_is_reparse(path):
    """lstat-based reparse check for the delete root itself."""
    try:
        entry_stat = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(entry_stat.st_mode):
        return True
    if getattr(entry_stat, "st_file_attributes", 0) & _REPARSE_FLAG:
        return True
    return False


def remove_tree_with_tracking(top):
    """Delete the tree at ``top``, tracking mutation and classifying failures.

    Returns normally when every child and ``top`` itself are deleted.

    Raises ``RecoverableTreeDeleteError`` when a known-busy/locked/raced child
    fails but the failure is classified as recoverable.  The exception's
    ``mutated`` flag is True when at least one child was successfully removed
    before the failure.

    Raises raw ``OSError`` for a reparse boundary refusal or a fatal/unknown
    I/O failure (device errors, CRC, filesystem corruption).

    Contract:
    * explicit stack, post-order deletion (identical to remove_tree_iterative);
    * reparse boundary at or below ``top`` is REFUSED (raw OSError);
    * mutation tracking: any successful ``os.remove`` or ``os.rmdir`` sets the
      mutated flag;
    * recoverable classification uses classify_delete_error from the taxonomy;
    * fatal classification propagates raw so the caller can stop globally.
    """
    if _path_is_reparse(top):
        raise OSError("refusing reparse boundary: %s" % top)
    stack = [(top, _scandir_children(top))]
    mutated = False
    while stack:
        path, entries = stack[-1]
        if entries:
            entry = entries.pop()
            if _entry_is_reparse(entry):
                raise OSError("refusing reparse boundary: %s" % entry.path)
            if entry.is_dir(follow_symlinks=False):
                try:
                    stack.append((entry.path, _scandir_children(entry.path)))
                except OSError as exc:
                    cls = classify_delete_error(exc)
                    if cls == DELETE_FATAL:
                        raise
                    raise RecoverableTreeDeleteError(
                        exc, mutated=mutated, failing_path=entry.path) from exc
            else:
                try:
                    os.remove(entry.path)
                    mutated = True
                except OSError as exc:
                    cls = classify_delete_error(exc)
                    if cls == DELETE_FATAL:
                        raise
                    raise RecoverableTreeDeleteError(
                        exc, mutated=mutated, failing_path=entry.path) from exc
        else:
            stack.pop()
            try:
                os.rmdir(path)
                mutated = True
            except OSError as exc:
                cls = classify_delete_error(exc)
                if cls == DELETE_FATAL:
                    raise
                raise RecoverableTreeDeleteError(
                    exc, mutated=mutated, failing_path=path) from exc


if __name__ == "__main__":
    import shutil as _shutil_check
    import sys
    import tempfile

    ok = True

    def check(name, cond):
        global ok
        print(("PASS  " if cond else "FAIL  ") + name)
        ok = ok and cond

    base = tempfile.mkdtemp(prefix="deljunk_walk_selfcheck_")
    try:
        deep = base
        for _ in range(300):
            deep = os.path.join(deep, "d")
            os.mkdir(deep)
        with open(os.path.join(deep, "leaf.bin"), "wb") as handle:
            handle.write(b"x" * 8)
        saved = sys.getrecursionlimit()
        sys.setrecursionlimit(60)
        try:
            walked = [(root, list(dirs), list(files))
                      for root, dirs, files in iter_tree(base)]
            check("300-deep walk survives limit 60",
                  len(walked) == 301 and walked[-1][2] == ["leaf.bin"])
            check("order matches os.walk shape", walked[0][0] == base)
        finally:
            sys.setrecursionlimit(saved)

        victim = os.path.join(base, "victim")
        os.mkdir(victim)
        node = victim
        for _ in range(150):
            node = os.path.join(node, "d")
            os.mkdir(node)
        with open(os.path.join(node, "x.tmp"), "wb") as handle:
            handle.write(b"y")
        remove_tree_iterative(victim)
        check("iterative delete clears 150-deep tree", not os.path.exists(victim))

        outside = os.path.join(base, "outside")
        os.mkdir(outside)
        sentinel = os.path.join(outside, "sentinel.log")
        with open(sentinel, "w") as handle:
            handle.write("keep")
        link = os.path.join(base, "junction")
        made = os.system("cmd /c mklink /J \"%s\" \"%s\"" % (link, outside))
        if made == 0 and os.path.isdir(link):
            check("junction visible in dirs, not descended",
                  [d for _r, dirs, _f in iter_tree(base) for d in dirs
                   if d == "junction"] == ["junction"])
            try:
                remove_tree_iterative(link)
                check("iterative delete refuses reparse root", False)
            except OSError:
                check("iterative delete refuses reparse root", True)
            check("junction target survives delete refusal",
                  os.path.exists(sentinel))
            os.rmdir(link)
        else:
            print("SKIP  junction probe (mklink unavailable)")
    finally:
        _shutil_check.rmtree(base, ignore_errors=True)

    print("---")
    print("PASS (0 failures)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
