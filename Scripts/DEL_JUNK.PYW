"""DEL JUNK - рекурсивный снос расширений и паттернов откровенного мусора.

Two policies (user directive, whole-drive hardening):

  SAFE_DISK            automatic when the target is a drive root (X:\\).
                       Only unambiguous regenerable caches by name; no
                       build/dist/target/logs/cache/node_modules by name; no
                       *.log/*.dmp/*.etl/*.evtx; age-gated partial/temp files;
                       .git/.svn/.hg + $RECYCLE.BIN + System Volume Information
                       pruned from traversal; junctions/reparse points outside
                       the root never followed; byte preview + manifest before
                       deletion; risky excluded candidates shown.

  AGGRESSIVE_PROJECT   the historical name-based rules, unchanged, for
                       explicitly selected project folders.

Two invocations:

  DEL_JUNK.PYW "<path>"     one selected target; the policy is chosen from the
                            target (drive root -> SAFE_DISK).

  DEL_JUNK.PYW --all-disks  ALL DISKS AVAILABLE: every local fixed, mounted,
                            accessible, writable volume, SAFE_DISK forced for
                            all of them (the system drive included,
                            AGGRESSIVE_PROJECT unreachable). Discovery is
                            native (see del_junk_disks.py).

                            DEFAULT (streaming, unattended): before anything
                            destructive the worker tests the journal
                            destination, runs the RUNTIME destructive gate
                            (all_disks_destructive_ready: every critical
                            invariant proven on throwaway fixtures), inspects
                            the previous run's journal and shows ONE explicit
                            start confirmation. Nothing is deleted before the
                            user presses Start cleanup. After confirmation a
                            durable RUN_START is written and each candidate is
                            preflighted, journalled (RUN_START/DRIVE_START/
                            DRIVE_START/INTENT/DELETED/SKIPPED_CHANGED/
                            RISKY_EXCLUDED/REFUSED/FAILED/CANCELLED/
                            SKIPPED_UNREADABLE/DRIVE_END/RUN_END in a durable
                            .journal.jsonl) and deleted IMMEDIATELY during
                            traversal. Journal loss, destructive I/O failure
                            or an invariant violation STOPS the run before the
                            next candidate/drive (fail-stop). A durable
                            summary file lands next to the journal at run end.

                            --preview is scan/report ONLY: it never deletes,
                            never writes INTENT and never invokes the
                            destructive candidate path. It may scan, save
                            manifests and report risky exclusions.

Both: no Recycle Bin -- deletion is permanent, the confirmation dialog and the
journal are the only witnesses. One destructive pass per scope: a reservation
covers its whole subtree, so a drive-root pass and a folder pass inside it
cannot run at the same time.
Запускается через pyw.exe без консоли.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from del_junk_disks import (
    CANCELLED,
    CLEANED,
    FAILED,
    NOTHING_TO_DO,
    PARTIAL,
    SKIPPED,
    eligible_drives,
)
from del_junk_policy import (
    AGE_GATED_EXTENSIONS,
    PRUNE_ALWAYS_DIRS,
    SAFE_DISK_EXTENSIONS,
    build_policy,
    is_reparse_dir,
    is_reparse_point,
    select_policy,
)
from del_junk_walk import iter_tree, remove_tree_iterative, remove_tree_with_tracking
from del_junk_errors import (
    RecoverableTreeDeleteError,
    classify_delete_error,
    DELETE_FATAL,
    DELETE_CHANGED,
    DELETE_RECOVERABLE_BUSY,
)
from shell_arg import shell_target

# Historical AGGRESSIVE_PROJECT tables, kept verbatim so the aggressive policy
# the worker runs today is the one it always ran.
JUNK_EXTENSIONS = {
    ".tmp", ".temp", ".part", ".crdownload", ".download", ".opdownload",
    ".pyc", ".pyo", ".o", ".obj", ".ilk", ".idb", ".pch", ".sdf",
    ".bsc", ".ncb", ".suo",
    ".dmp", ".mdmp", ".minidump", ".crash", ".etl", ".wpr", ".wpa",
    ".blg", ".evtx",
    ".swp", ".swo", ".swn",
    ".ds_store", ".thumbs.db",
    ".cache",
    ".log",
}

JUNK_DIR_NAMES = {
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".hypothesis", ".cache",
    "cache", "caches", "temp", "tmp", "logs", "log",
    "crashpad", "crashes", "dumps", "minidumps",
    "updater",
    "gpucache", "shadercache", "grshadercache", "dawncache",
    "graphitedawncache", "thumbnailcache", "webcache", "cache2",
    "startupcache", "code cache", "browsermetrics", "local traces",
    "component_crx_cache", "extensions_crx_cache",
    "node_modules", "bower_components",
}

JUNK_BUILD_OUTPUT_DIRS = {
    "target", "build", "dist",
}

JUNK_FILE_PATTERNS = [
    "*.log", "*.tmp", "*.temp", "*.swp", "*.pyc",
    "*.cache", "*.dmp", "*.crash", "*.etl",
    "thumbs.db", ".ds_store",
    "*.exe.tmp", "*.part", "*.crdownload",
    "run-log-*.zip",
]

JUNK_DIR_PATTERNS = [
    "__pycache__", ".pytest_cache", "ruff_cache",
    ".mypy_cache", "node_modules",
]


def should_delete_dir(name):
    name_lower = name.lower()
    if name_lower in JUNK_DIR_NAMES or name_lower in JUNK_BUILD_OUTPUT_DIRS:
        return True
    for pat in JUNK_DIR_PATTERNS:
        if pat.startswith("*") and pat.endswith("*"):
            if pat[1:-1] in name_lower:
                return True
        elif pat.startswith("*"):
            if name_lower.endswith(pat[1:]):
                return True
        elif pat.endswith("*"):
            if name_lower.startswith(pat[:-1]):
                return True
        else:
            if name_lower == pat:
                return True
    return False


def should_delete_file(name):
    name_lower = name.lower()
    ext = os.path.splitext(name_lower)[1]
    if ext in JUNK_EXTENSIONS:
        return True
    base = os.path.splitext(name_lower)[0]
    if base in JUNK_EXTENSIONS:
        return True
    return name_lower in {".ds_store", "thumbs.db"}


def tree_has_protected_boundary(path, policy, progress=None, cancelled=None):
    """Fail closed when a cache tree contains data we promised to protect.

    R-AUDIT-001: a known-junk directory name is not permission to delete the
    directory's *contents* unseen. ``gpucache\\important.log`` is exactly the
    file SAFE_DISK promises to keep, so a single protected file anywhere in the
    subtree disqualifies the whole subtree, which the caller then reports as
    risky_excluded instead of deleting.
    """
    failed = False

    def onerror(_error):
        nonlocal failed
        failed = True

    dir_risky = policy.get("dir_risky", lambda value: False)
    file_risky = (policy.get("file_risky") or
                  policy.get("risky_excluded", lambda name, full: False))

    # Depth-independent traversal (E-149/E-150): os.walk was a recursive
    # generator, one Python frame per directory level -- a junk candidate
    # such as GPUCache may itself be nested deeper than the recursion limit.
    seen_dirs = 0
    seen_files = 0
    for seen_dirs, (_root, dirs, files) in enumerate(
            iter_tree(path, onerror=onerror), start=1):
        if failed:
            return True
        if cancelled is not None and cancelled():
            raise ScanCancelled(seen_dirs - 1)
        seen_files += len(files)
        if progress is not None:
            progress(seen_dirs, seen_files)
        for name in dirs:
            full = os.path.join(_root, name)
            if (name.lower() in PRUNE_ALWAYS_DIRS or is_reparse_dir(full) or
                    dir_risky(name)):
                return True
        for name in files:
            full = os.path.join(_root, name)
            if is_reparse_point(full):
                return True
            if file_risky(name, full):
                return True
    return failed


def scan_junk(root_path, policy=None, progress=None, cancelled=None):
    """Junk files and directories under root_path, without descending into
    a directory whose own name already condemns it.

    policy: a bundle from del_junk_policy.build_policy. None means the
    historical AGGRESSIVE_PROJECT behaviour (used by the regression tests).

    progress(directories_seen, files_seen) is polled for UI feedback; when the
    ``cancelled`` callable returns True the scan stops and raises
    ScanCancelled, leaving everything found so far untouched on disk.
    """
    if policy is None:
        policy = build_policy(aggressive=True, now=time.time())
    junk_files = []
    junk_dirs = []
    risky_excluded = []
    visited = 0
    visited_files = 0

    for root, dirs, files in iter_tree(root_path):
        if cancelled is not None and cancelled():
            raise ScanCancelled(visited)
        visited += 1
        if progress is not None:
            progress(visited, visited_files)

        # Reparse guard: junctions/mount points outside the selected root are
        # never followed (a drive-root walk must not cross into another volume
        # or into the user profile through a junction). The root itself is the
        # user's explicit choice.
        keep_dirs = []
        for d in dirs:
            full = os.path.join(root, d)
            if d.lower() in PRUNE_ALWAYS_DIRS:
                continue
            if is_reparse_dir(full):
                continue
            keep_dirs.append(d)

        # SAFE_DISK risky names protect the WHOLE subtree.  Do not descend and
        # then accidentally let a deeper extension rule punch through build/.
        descend = []
        for d in keep_dirs:
            full = os.path.join(root, d)
            if policy.get("dir_risky", lambda name: False)(d):
                risky_excluded.append(full)
            elif policy["dir_junk"](d):
                if (policy.get("mode") == "SAFE_DISK" and
                        tree_has_protected_boundary(
                            full, policy, cancelled=cancelled)):
                    risky_excluded.append(full)
                else:
                    junk_dirs.append(full)
            else:
                descend.append(d)
        dirs[:] = descend

        for f in files:
            visited_files += 1
            if visited_files % 128 == 0:
                if cancelled is not None and cancelled():
                    raise ScanCancelled(visited)
                if progress is not None:
                    progress(visited, visited_files)
            full = os.path.join(root, f)
            if is_reparse_point(full):
                continue
            if policy.get("mode") == "SAFE_DISK":
                if is_own_evidence(full):
                    continue
                if policy["file_junk"](f, full):
                    junk_files.append(full)
                elif (policy.get("file_risky") or
                      policy.get("risky_excluded", lambda name, path: False))(f, full):
                    risky_excluded.append(full)
            else:
                if policy["file_junk"](f, full):
                    junk_files.append(full)
                elif policy["risky_excluded"](f, full):
                    risky_excluded.append(full)

    junk_files, junk_dirs, risky_excluded = dedupe(junk_files, junk_dirs, risky_excluded)
    return junk_files, junk_dirs, risky_excluded


class ScanCancelled(Exception):
    def __init__(self, visited):
        super().__init__("scan cancelled")
        self.visited = visited


def dedupe(junk_files, junk_dirs, risky_excluded):
    # A file inside a condemned directory is covered by the directory itself.
    kept_files = []
    for f in junk_files:
        covered = any(f.startswith(d + os.sep) for d in junk_dirs)
        if not covered:
            kept_files.append(f)
    return kept_files, junk_dirs, risky_excluded


def _identity(path):
    """Cheap identity used to refuse deleting a post-confirmation replacement."""
    st = os.lstat(path)
    return {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "inode": st.st_ino,
        "device": st.st_dev,
        "attributes": getattr(st, "st_file_attributes", 0),
    }


def _fingerprint_record(hasher, kind, relative_path, identity):
    payload = [kind, relative_path]
    payload.extend(str(identity[name]) for name in
                   ("size", "mtime_ns", "inode", "device", "attributes"))
    hasher.update("\0".join(payload).encode("utf-8", "surrogatepass"))
    hasher.update(b"\n")


def _tree_snapshot(path, progress=None, cancelled=None):
    """Size and identity digest without following any reparse point."""
    total = 0
    seen = 0
    seen_files = 0
    digest = hashlib.sha256()
    root_identity = _identity(path)
    _fingerprint_record(digest, "D", ".", root_identity)

    # Depth-independent traversal (E-149/E-150); deterministic order is
    # preserved: per-directory sorted(dirs)/sorted(files), depth-first in the
    # pruned dirs order -- the same canonical stream os.walk produced.
    for root, dirs, files in iter_tree(path):
        if cancelled is not None and cancelled():
            raise ScanCancelled(seen)
        seen += 1
        if progress is not None:
            progress(seen, seen_files)

        kept_dirs = []
        for name in sorted(dirs, key=str.casefold):
            full = os.path.join(root, name)
            try:
                ident = _identity(full)
            except OSError:
                continue
            rel = os.path.relpath(full, path)
            _fingerprint_record(digest, "D", rel, ident)
            if not is_reparse_point(full):
                kept_dirs.append(name)
        dirs[:] = kept_dirs

        for name in sorted(files, key=str.casefold):
            seen_files += 1
            if seen_files % 128 == 0:
                if cancelled is not None and cancelled():
                    raise ScanCancelled(seen)
                if progress is not None:
                    progress(seen, seen_files)
            full = os.path.join(root, name)
            try:
                ident = _identity(full)
            except OSError:
                continue
            rel = os.path.relpath(full, path)
            _fingerprint_record(digest, "F", rel, ident)
            if not is_reparse_point(full):
                total += ident["size"]

    return total, digest.hexdigest(), root_identity


def manifest_bytes(junk_files, junk_dirs, risky_excluded, root_path=None,
                   mode=None, progress=None, cancelled=None):
    """The complete deletion manifest as bytes -- shown/saved BEFORE deletion.

    Byte accounting lives here so the preview number and the manifest number
    cannot diverge: both come from the same walk over the same list.
    """
    entries = []
    total = 0
    for d in junk_dirs:
        try:
            n, tree_sha256, ident = _tree_snapshot(
                d, progress=progress, cancelled=cancelled)
        except OSError:
            n, tree_sha256, ident = 0, None, None
        total += n
        entries.append({"path": d, "type": "dir", "bytes": n,
                        "identity": ident, "tree_sha256": tree_sha256})
    for f in junk_files:
        try:
            ident = _identity(f)
            n = ident["size"] if not is_reparse_point(f) else 0
        except OSError:
            ident, n = None, 0
        total += n
        entries.append({"path": f, "type": "file", "bytes": n,
                        "identity": ident})
    doc = {
        "schema": 2,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": os.path.abspath(root_path) if root_path else None,
        "mode": mode,
        "total_bytes": total,
        "file_count": len(junk_files),
        "dir_count": len(junk_dirs),
        "risky_excluded": sorted(risky_excluded),
        "entries": entries,
    }
    return total, (json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"))


def save_manifest(manifest, target_dir, directory=None):
    """Durably publish a unique manifest; errors deliberately fail closed."""
    if directory is None:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        directory = os.path.join(base, "SAITULS", "DEL_JUNK", "manifests")
    os.makedirs(directory, exist_ok=True)
    target_key = os.path.normcase(os.path.realpath(os.path.abspath(target_dir)))
    short = hashlib.sha256(target_key.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    nonce = str(time.time_ns())[-9:]
    final_path = os.path.join(
        directory, f"{stamp}-{short}-{os.getpid()}-{nonce}.manifest.json")
    temp_path = final_path + ".tmp"
    fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = None
            fh.write(manifest)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, final_path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return final_path


_lock_handle_cache = {}
_LOCK_PREFIX = "deljunk_"
_LOCK_SUFFIX = ".lock"
_SCOPE_GATE = "scopes.gate"
# Test seam only: a callable invoked while the admission gate is still held,
# between taking the reservation and publishing its scope. Production leaves it
# None; a regression widens exactly that window on purpose.
_publish_barrier = None


def _lock_handle(handle, unlock=False):
    """Take/release the ownership token: byte 0, so the scope payload that
    follows it stays readable by other processes on Windows."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        mode = msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK
        msvcrt.locking(handle.fileno(), mode, 1)
    else:
        import fcntl
        mode = fcntl.LOCK_UN if unlock else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), mode)


def _lock_dir():
    return os.path.join(tempfile.gettempdir(), "SAITULS", "DEL_JUNK", "locks")


def _scope_key(target_dir):
    return os.path.normcase(os.path.realpath(os.path.abspath(target_dir)))


def _lock_path(key):
    digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
    return os.path.join(_lock_dir(), _LOCK_PREFIX + digest + _LOCK_SUFFIX)


def _with_sep(path):
    return path if path.endswith(os.sep) else path + os.sep


def scopes_conflict(left, right):
    """True when two destructive scopes overlap.

    R-AUDIT-002: equal paths are the obvious case, but ``R:\\`` and
    ``R:\\Projects`` are the same bytes reached two ways. Containment in either
    direction is a conflict, which is what keeps an ALL DISKS pass and a
    single-folder pass off each other's files.
    """
    if left == right:
        return True
    return (left.startswith(_with_sep(right)) or
            right.startswith(_with_sep(left)))


def _open_lock_file(path, create=False):
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    return os.fdopen(os.open(path, flags, 0o600), "r+b")


def _read_scope(path):
    """The scope another process reserved, read past the locked token byte."""
    try:
        with open(path, "rb") as handle:
            handle.seek(1)
            payload = handle.read(4096)
    except OSError:
        return ""
    lines = payload.decode("utf-8", "replace").split("\n")
    return lines[1].strip() if len(lines) > 1 else ""


def _scope_is_live(path):
    """True while another process still owns this reservation."""
    try:
        handle = _open_lock_file(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        _lock_handle(handle)
    except OSError:
        return True
    finally:
        try:
            handle.close()
        except OSError:
            pass
    return False


def _acquire_gate(timeout=3.0):
    """Serialize admission so two starts cannot both pass the overlap check."""
    try:
        gate = _open_lock_file(os.path.join(_lock_dir(), _SCOPE_GATE), create=True)
    except OSError:
        return None
    gate.seek(0, os.SEEK_END)
    if gate.tell() == 0:
        gate.write(b"0")
        gate.flush()
    deadline = time.monotonic() + timeout
    while True:
        try:
            _lock_handle(gate)
            return gate
        except OSError:
            if time.monotonic() >= deadline:
                gate.close()
                return None
            time.sleep(0.05)


def _release_gate(gate):
    if gate is None:
        return
    try:
        _lock_handle(gate, unlock=True)
    except OSError:
        pass
    try:
        gate.close()
    except OSError:
        pass


def conflicting_scope(key, lock_dir=None):
    """The live reservation that overlaps ``key``, or '' when none does."""
    lock_dir = _lock_dir() if lock_dir is None else lock_dir
    try:
        names = os.listdir(lock_dir)
    except OSError:
        return ""
    for name in names:
        if not (name.startswith(_LOCK_PREFIX) and name.endswith(_LOCK_SUFFIX)):
            continue
        other = os.path.join(lock_dir, name)
        scope = _read_scope(other)
        if not scope or not scopes_conflict(key, scope):
            continue
        if _scope_is_live(other):
            return scope
        try:
            os.remove(other)  # the owner is gone; its reservation is not
        except OSError:
            pass
    return ""


def single_flight_lock(target_dir):
    """Reserve target for one destructive pass; process exit releases it.

    The reservation covers the whole subtree, so an active cleanup of a drive
    root blocks any cleanup inside it and vice versa (R-AUDIT-002).
    """
    key = _scope_key(target_dir)
    if key in _lock_handle_cache:
        return None
    lock = _lock_path(key)
    gate = None
    handle = None
    try:
        os.makedirs(_lock_dir(), exist_ok=True)
        gate = _acquire_gate()
        if gate is None:
            return None
        if conflicting_scope(key):
            return None
        handle = _open_lock_file(lock, create=True)
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        _lock_handle(handle)
        # Publish the scope BEFORE admission reopens. Releasing the gate first
        # left a window where a second start saw a locked reservation whose
        # payload was still empty, read no scope, found no overlap and reserved
        # an overlapping subtree -- the exact R-AUDIT-002 breach the gate exists
        # to prevent.
        if _publish_barrier is not None:
            _publish_barrier()
        handle.seek(1)
        handle.write(
            (str(os.getpid()) + "\n" + key + "\n").encode("utf-8", "surrogatepass"))
        handle.truncate()
        handle.flush()
    except OSError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        return None
    finally:
        _release_gate(gate)
    _lock_handle_cache[key] = handle
    return lock


def release_single_flight(target_dir):
    key = _scope_key(target_dir)
    handle = _lock_handle_cache.pop(key, None)
    if handle is None:
        return
    try:
        _lock_handle(handle, unlock=True)
    except OSError:
        pass
    handle.close()
    try:
        os.remove(_lock_path(key))  # a released reservation must not linger
    except OSError:
        pass


# Self-evidence boundary (§35/36): the active journal, the summary, the lock
# files and the manifests directory are this run's own audit evidence. They
# must survive the run that writes them, so they can never be delete
# candidates -- the whole LOCALAPPDATA subtree is NOT exempt, only these
# SAITULS-owned destinations.
_EVIDENCE_DIR_MARKERS = (
    os.path.join("SAITULS", "DEL_JUNK"),
    os.path.join("SAITULS", "DEL_JUNK", "manifests"),
)
_EVIDENCE_SUFFIXES = (".journal.jsonl", ".summary.json", ".probe",
                      ".manifest.json")


def is_own_evidence(path):
    """True for the worker's own journal/manifest/lock/summary destinations."""
    if path is None:
        return False
    low = os.path.normcase(os.path.abspath(path))
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    own = os.path.normcase(os.path.abspath(os.path.join(
        base, "SAITULS", "DEL_JUNK")))
    if low == own or low.startswith(own + os.sep):
        # Inside our destination: only the narrow evidence artifacts are
        # protected; other files there are not candidates anyway by policy,
        # but never let an extension rule punch through here.
        return True
    if low.endswith(_EVIDENCE_SUFFIXES):
        return True
    locks = os.path.normcase(os.path.abspath(os.path.join(
        tempfile.gettempdir(), "SAITULS", "DEL_JUNK", "locks")))
    return low == locks or low.startswith(locks + os.sep)


def _path_within_root(path, root_path):
    try:
        root_real = os.path.normcase(os.path.realpath(os.path.abspath(root_path)))
        path_real = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        return path_real != root_real and os.path.commonpath(
            (root_real, path_real)) == root_real
    except (OSError, ValueError):
        return False


def _same_identity(left, right):
    return left is not None and right is not None and all(
        left.get(name) == right.get(name)
        for name in ("size", "mtime_ns", "inode", "device", "attributes")
    )


# Bounded retry for file deletion under ordinary Windows contention.
_RETRY_DELAYS = (0.05, 0.15)  # 50 ms, 150 ms


def _delete_file_with_retry(path, identity_before):
    """Delete a file with bounded retry and postcondition check.

    Returns ``(event, error_string)`` where event is one of:
        DELETED          success
        SKIPPED_BUSY     all retries exhausted, file still present with same identity
        SKIPPED_CHANGED  file vanished or identity changed
        FAILED           fatal / unknown I/O failure or postcondition unestablishable
    """
    last_exc = None
    for attempt in range(1 + len(_RETRY_DELAYS)):
        try:
            os.remove(path)
            return ("DELETED", "")
        except FileNotFoundError:
            return ("SKIPPED_CHANGED", "vanished during delete attempt")
        except OSError as exc:
            classification = classify_delete_error(exc)
            if classification == DELETE_FATAL:
                return ("FAILED", str(exc))
            last_exc = exc
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
    # All retries exhausted — establish postcondition.
    try:
        if not os.path.exists(path):
            return ("SKIPPED_CHANGED", "vanished after retries")
        current = _identity(path)
        if _same_identity(identity_before, current):
            return ("SKIPPED_BUSY", str(last_exc))
        return ("SKIPPED_CHANGED", "identity changed after retries")
    except OSError:
        return ("FAILED", "cannot establish postcondition: " + str(last_exc))


def _delete_tree_with_retry(path, identity_before):
    """Delete a directory tree with mutation tracking.

    Returns ``(event, error_string)`` where event is one of:
        DELETED          success
        SKIPPED_BUSY     no mutation before recoverable failure, same identity
        PARTIAL_BUSY     some children deleted before recoverable failure
        SKIPPED_CHANGED  tree vanished or identity changed
        FAILED           fatal / unknown I/O or postcondition unestablishable
    """
    try:
        remove_tree_with_tracking(path)
        return ("DELETED", "")
    except RecoverableTreeDeleteError as exc:
        if exc.mutated:
            return ("PARTIAL_BUSY",
                    "partial deletion; child failed: %s: %s"
                    % (exc.failing_path, exc.original_error))
        # No mutation — check postcondition.
        try:
            if not os.path.isdir(path):
                return ("SKIPPED_CHANGED", "vanished during tree delete")
            current = _identity(path)
            if _same_identity(identity_before, current):
                return ("SKIPPED_BUSY", str(exc.original_error))
            return ("SKIPPED_CHANGED", "identity changed during tree delete")
        except OSError:
            return ("FAILED",
                    "cannot establish postcondition: " + str(exc.original_error))
    except FileNotFoundError:
        return ("SKIPPED_CHANGED", "vanished before tree delete")
    except OSError as exc:
        return ("FAILED", str(exc))

def delete_junk(junk_files, junk_dirs, root_path=None, manifest=None,
                cancelled=None, outcomes=None, policy=None, progress=None):
    """Delete only still-identical manifest entries inside root_path.

    The optional arguments keep the historical test/helper API available, but
    the interactive worker always supplies both safety witnesses.

    ``cancelled`` is polled between candidates: the current file/tree finishes
    or fails atomically, nothing further is started, and the caller compares
    what came back against what it planned to tell the truth about a partial
    run.
    """
    deleted_files = []
    deleted_dirs = []
    errors = []
    stopped = False

    def report(path, event, reason=""):
        if outcomes is not None:
            outcomes.append({"path": path, "event": event, "error": reason})

    if isinstance(manifest, (bytes, bytearray)):
        manifest = json.loads(bytes(manifest).decode("utf-8"))
    entries = {}
    manifest_root_valid = True
    if manifest:
        entries = {os.path.normcase(os.path.abspath(e["path"])): e
                   for e in manifest.get("entries", [])}
        if root_path:
            declared_root = manifest.get("root")
            manifest_root_valid = bool(declared_root) and (
                os.path.normcase(os.path.realpath(os.path.abspath(declared_root))) ==
                os.path.normcase(os.path.realpath(os.path.abspath(root_path)))
            )

    def admissible(path, expected_type):
        if is_own_evidence(path):
            return "REFUSED", "run evidence; never a candidate"
        if not manifest_root_valid:
            return "REFUSED", "manifest belongs to a different selected root; skipped"
        if root_path and not _path_within_root(path, root_path):
            return "RISKY_EXCLUDED", "path escaped selected root; skipped"
        if is_reparse_point(path):
            return "RISKY_EXCLUDED", "path became a reparse point; skipped"
        if (policy and expected_type == "dir" and
                tree_has_protected_boundary(path, policy, progress=progress)):
            return "RISKY_EXCLUDED", "protected subtree boundary"
        if not manifest:
            return "", ""
        entry = entries.get(os.path.normcase(os.path.abspath(path)))
        if not entry or entry.get("type") != expected_type:
            return "REFUSED", "not present in confirmed manifest; skipped"
        try:
            if expected_type == "file":
                unchanged = _same_identity(entry.get("identity"), _identity(path))
            else:
                _n, tree_sha256, ident = _tree_snapshot(path, progress=progress)
                unchanged = (_same_identity(entry.get("identity"), ident) and
                             entry.get("tree_sha256") == tree_sha256)
        except FileNotFoundError as exc:
            return "SKIPPED_CHANGED", str(exc)
        except OSError as exc:
            return "FAILED", str(exc)
        if not unchanged:
            return "SKIPPED_CHANGED", "changed after confirmation; skipped"
        return "", ""

    for f in junk_files:
        if cancelled is not None and cancelled():
            report(f, "CANCELLED", "stop requested")
            stopped = True
            break
        event, reason = admissible(f, "file")
        if event:
            report(f, event, reason)
            errors.append((f, reason))
            continue
        # Structured taxonomy + bounded retry: ordinary Windows contention
        # (locked, in-use, access denied) resolves to SKIPPED_BUSY /
        # SKIPPED_CHANGED candidate-local outcomes; only unknown/fatal I/O
        # reports FAILED. Never classified by message text.
        event, reason = _delete_file_with_retry(f, _identity(f))
        if event == "DELETED":
            deleted_files.append(f)
            report(f, "DELETED")
        else:
            errors.append((f, reason))
            report(f, event, reason)
    for d in sorted(junk_dirs, key=lambda p: p.count(os.sep), reverse=True):
        if stopped:
            break
        if cancelled is not None and cancelled():
            report(d, "CANCELLED", "stop requested")
            stopped = True
            break
        if not os.path.isdir(d):
            if manifest:
                report(d, "SKIPPED_CHANGED", "directory disappeared after confirmation; skipped")
                errors.append((d, "directory disappeared after confirmation; skipped"))
            continue
        event, reason = admissible(d, "dir")
        if event:
            report(d, event, reason)
            errors.append((d, reason))
            continue
        # Depth-independent iterative delete with mutation tracking: a
        # recoverable child failure after some children were already removed
        # reports PARTIAL_BUSY (mutation happened, nothing is rolled back);
        # with no mutation it reports SKIPPED_BUSY. Reparse boundaries and
        # fatal/unknown I/O still propagate as FAILED. (E-149/E-150: never
        # shutil.rmtree -- it recurses once per directory level.)
        event, reason = _delete_tree_with_retry(d, _identity(d))
        if event == "DELETED":
            deleted_dirs.append(d)
            report(d, "DELETED")
        else:
            errors.append((d, reason))
            report(d, event, reason)
    return deleted_files, deleted_dirs, errors


# Bounded age of filesystem progress before the window warns POSSIBLE STALL.
FS_STALL_SECONDS = 20.0


def _scan_failure_report(stage, drive, path, exc):
    """Compact GUI diagnostic + local traceback log for a fatal scan failure.

    E-149/E-150 §22: "maximum recursion depth exceeded" alone told the user
    nothing. A fatal scan failure reports stage/drive/path-when-safely-known/
    exception type + message in the dialog, and the full traceback goes to a
    local diagnostic log under the DEL JUNK evidence tree. No file contents,
    no environment, no secrets.
    """
    detail = "\n".join([
        "Stage: %s" % (stage or "—"),
        "Drive: %s" % (drive or "—"),
        "Path: %s" % (path or "—"),
        "Error: %s: %s" % (type(exc).__name__, exc),
    ])
    if isinstance(exc, RecursionError):
        detail += ("\n\n(This is an internal traversal invariant failure; "
                   "the run stopped safely. Nothing further will be deleted.)")
    try:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        log_dir = os.path.join(base, "SAITULS", "DEL_JUNK", "diagnostics")
        os.makedirs(log_dir, exist_ok=True)
        name = "diag-%s-%d.log" % (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), os.getpid())
        with open(os.path.join(log_dir, name), "w", encoding="utf-8") as handle:
            handle.write(detail + "\n\n")
            handle.write(traceback.format_exc())
    except OSError:
        pass  # the dialog text is already the user-facing witness
    return detail


def _liveness_line(metrics, elapsed, now=None, stall_seconds=None):
    """One truthful liveness line for the progress window.

    Heartbeat (the UI pump) and filesystem progress are distinct signals: the
    first says the worker is alive, the second says the walk is still moving.
    When the filesystem has shown no progress for a bounded time the window says
    POSSIBLE STALL -- never "hung".
    """
    if stall_seconds is None:
        stall_seconds = FS_STALL_SECONDS
    now = time.monotonic() if now is None else now
    metrics = dict(metrics)
    fs_age = max(0.0, now - metrics.get("fs_last_progress", now))
    hb_age = max(0.0, now - metrics.get("heartbeat", now))
    rate = (((metrics.get("dirs", 0) + metrics.get("files", 0)) / elapsed)
            if elapsed > 0 else 0.0)
    line = (
        "{drive} | {path} | каталогов {dirs}, файлов {files} | "
        "удалено Ф{df}/Д{dd}, освобождено {reclaimed} | "
        "риск {risky}, ошибок {errs} | скан {rate:.1f}/с, удаление {dr:.1f}/с | "
        "время {elapsed:.1f}с | heartbeat {stamp} ({hb:.1f}с), ФС {fs:.1f}с".format(
            drive=metrics.get("drive", "—"),
            path=metrics.get("path", "—"),
            dirs=metrics.get("dirs", 0),
            files=metrics.get("files", 0),
            df=metrics.get("deleted_files", 0),
            dd=metrics.get("deleted_dirs", 0),
            reclaimed=format(metrics.get("reclaimed", 0), ","),
            risky=metrics.get("risky", 0),
            errs=metrics.get("errors", 0),
            rate=rate, dr=(metrics.get("deleted_files", 0) + metrics.get("deleted_dirs", 0)) / max(elapsed, .001),
            elapsed=elapsed, stamp=metrics.get("heartbeat_timestamp", "—"), hb=hb_age, fs=fs_age))
    if fs_age >= stall_seconds:
        line += " | POSSIBLE STALL (ФС без прогресса %.0fс)" % fs_age
    return line


class ScanProgress:
    """Small responsive progress window for an otherwise synchronous walk."""

    def __init__(self, parent, target_dir, mode,
                 header="Проверяется путь без удаления:"):
        self.parent = parent
        self.cancel_requested = False
        self.stop_event = None
        self.last_pump = 0.0
        self.count = 0
        self.file_count = 0
        self.stage = "Сканирование"
        self.started = time.monotonic()
        self.last_heartbeat = self.started
        self.last_fs_progress = self.started
        self.metrics = {}
        self.window = tk.Toplevel(parent)
        self.window.title(f"DEL JUNK — {mode}")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.status = tk.StringVar()
        ttk.Label(self.window, text=header).pack(
            padx=16, pady=(14, 4), anchor="w")
        ttk.Label(self.window, text=target_dir, width=72).pack(
            padx=16, pady=(0, 8), anchor="w")
        ttk.Label(self.window, textvariable=self.status, wraplength=480, width=72).pack(
            padx=16, pady=(0, 8), anchor="w")
        ttk.Button(self.window, text="Отмена", command=self.cancel).pack(
            padx=16, pady=(0, 14), anchor="e")
        self._pump(force=True)

    def cancel(self):
        self.cancel_requested = True
        if self.stop_event is not None:
            self.stop_event.set()
        # Streaming truth: deletion may already have happened. Never claim an
        # unchanged disk here -- already-deleted data is not restored.
        self.status.set(
            "Отмена запрошена: текущий кандидат завершится, новые не "
            "начнутся; удалённое не восстанавливается (истина в журнале).")

    def set_stage(self, stage):
        self.stage = stage
        self.count = 0
        self.file_count = 0
        self._pump(force=True)

    def update(self, count, total_planned=None):
        self.count = count
        self.file_count = total_planned or 0
        self.note_fs_progress()
        self._pump()

    def note_fs_progress(self):
        now = time.monotonic()
        self.last_fs_progress = now

    def note_path(self, path):
        self.metrics["path"] = path
        self._pump()

    def set_metrics(self, **fields):
        self.metrics.update(fields)
        self._pump()

    def cancelled(self):
        self._pump()
        return self.cancel_requested

    def _pump(self, force=False):
        if self.cancel_requested:
            try:
                self.parent.update()
            except tk.TclError:
                self.cancel_requested = True
            return
        now = time.monotonic()
        if not force and now - self.last_pump < 0.04:
            return
        self.last_pump = now
        self.last_heartbeat = now
        self.metrics["dirs"] = self.count
        self.metrics["files"] = self.file_count
        self.metrics["fs_last_progress"] = self.last_fs_progress
        self.metrics["heartbeat"] = self.last_heartbeat
        self.metrics["heartbeat_timestamp"] = time.strftime("%H:%M:%S")
        self.status.set(
            "%s | %s" % (self.stage,
                         _liveness_line(self.metrics,
                                        max(0.0, now - self.started))))
        try:
            self.parent.update()
        except tk.TclError:
            self.cancel_requested = True

    def close(self):
        try:
            self.window.destroy()
        except tk.TclError:
            pass


ALL_DISKS_FLAG = "--all-disks"
ALL_DISKS_PREVIEW_FLAG = "--preview"

# Streaming journal (T-143 10): append-only durable records, one per decision.
# Crash/cancel leaves truthful partial evidence; nothing here claims rollback for
# permanently deleted junk. RUN_START/DRIVE_START/DRIVE_END/RUN_END are the
# run-level envelope; the rest are candidate outcomes.
JOURNAL_EVENTS = (
    "INTENT", "DELETED", "SKIPPED_CHANGED", "SKIPPED_BUSY", "PARTIAL_BUSY",
    "RISKY_EXCLUDED", "REFUSED", "FAILED", "IO_FAILURE_STOP",
    "JOURNAL_UNAVAILABLE", "CANCELLED", "SKIPPED_UNREADABLE",
    "RUN_START", "DRIVE_START", "DRIVE_END", "RUN_END",
)

# A candidate reached by a durable INTENT whose later terminal outcome was
# never journalled (crash window D). Never reclassified as DELETED from path
# absence alone -- only the journal proves what happened.
TERMINAL_OUTCOMES = (
    "DELETED", "SKIPPED_CHANGED", "SKIPPED_BUSY", "PARTIAL_BUSY",
    "RISKY_EXCLUDED", "REFUSED", "FAILED",
)

# Poison value: an explicit run/candidate refusal is recorded as its own reason,
# never flattened into SKIPPED_CHANGED (that word means the candidate's identity
# or content drifted after its witness was taken).
JOURNAL_UNAVAILABLE = "JOURNAL_UNAVAILABLE"


class JournalUnavailable(Exception):
    """A durable INTENT could not be written; deletion must not proceed."""


def journal_path(directory=None):
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    directory = os.path.join(base, "SAITULS", "DEL_JUNK") if directory is None \
        else directory
    os.makedirs(directory, exist_ok=True)
    return os.path.join(
        directory, "stream-%s-%d.journal.jsonl"
        % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), os.getpid()))


def journal_record(path, event, drive, target, classification="", bytes_=0,
                   error="", identity=None):
    """Append one durable journal line: write, flush, fsync.

    Returns True only when the record is on disk. OSError is reported, never
    raised, so a caller can fail closed -- for a destructive streaming run a
    durable INTENT is a prerequisite to deletion, and the journal is the audit
    witness no manifest replaces.
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "drive": drive,
        "path": target,
        "classification": classification,
        "bytes": bytes_,
        "error": error,
    }
    if identity is not None:
        entry["identity"] = identity
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError:
        return False


def journal_run_record(path, event, payload=None, drive=""):
    """One durable run-envelope record (RUN_START/DRIVE_START/DRIVE_END/
    RUN_END). Operational metadata only: no file contents, no environment, no
    secrets. Returns True only when the record is on disk."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "drive": drive,
        "path": "",
        "classification": "run",
        "bytes": 0,
        "error": "",
    }
    if payload:
        entry["payload"] = payload
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError:
        return False


def parse_journal(path):
    """Parse a streaming journal truthfully, including its damage.

    Returns ``(records, diagnostics)``: complete JSONL records only, plus
    diagnostics for a trailing malformed/truncated line. A crash can land
    mid-write despite flush semantics; the parser retains everything before it
    and classifies the remainder TRUNCATED_RECORD -- it never fabricates a
    successful completion from a broken file.
    """
    records = []
    diagnostics = []
    try:
        with open(path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    diagnostics.append({"line": lineno,
                                        "problem": "TRUNCATED_RECORD"})
                    break  # a torn tail: nothing after it is trustworthy
    except OSError as exc:
        diagnostics.append({"line": 0, "problem": "UNREADABLE",
                            "error": str(exc)})
    return records, diagnostics


def dangling_intents(records):
    """INTENT records without a later terminal outcome for the same path.

    These are crash witnesses: state after the INTENT is UNKNOWN_AFTER_INTENT.
    Path absence proves nothing -- the file may have been deleted by something
    else, or never been reached. Never guessed, always reported.
    """
    terminal = {os.path.normcase(os.path.abspath(r["path"]))
                for r in records if r.get("event") in TERMINAL_OUTCOMES}
    pending = {}
    for record in records:
        if record.get("event") != "INTENT":
            continue
        key = os.path.normcase(os.path.abspath(record.get("path", "")))
        if key not in terminal:
            pending[key] = record
    return sorted(pending.values(),
                  key=lambda item: item.get("ts", ""), reverse=False)


def incomplete_previous_run(directory=None):
    """The latest previous streaming journal that did not end cleanly.

    Returns a dict with journal path, whether RUN_START has a matching RUN_END,
    the dangling-intent count and parser diagnostics -- or None. Informational
    only: nothing is repaired or deleted from it.
    """
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    journal_dir = os.path.join(base, "SAITULS", "DEL_JUNK") if directory is None \
        else directory
    try:
        names = sorted(
            (name for name in os.listdir(journal_dir)
             if name.startswith("stream-") and name.endswith(".journal.jsonl")),
            reverse=True)
    except OSError:
        return None
    for name in names:
        path = os.path.join(journal_dir, name)
        records, diagnostics = parse_journal(path)
        if not records:
            if diagnostics:
                return {"journal": path, "run_start": False,
                        "dangling": 0, "diagnostics": diagnostics}
            continue
        has_start = any(r.get("event") == "RUN_START" for r in records)
        has_end = any(r.get("event") == "RUN_END" for r in records)
        dangling = dangling_intents(records)
        if (has_start and not has_end) or dangling or diagnostics:
            return {"journal": path, "run_start": has_start,
                    "dangling": len(dangling), "diagnostics": diagnostics}
    return None


def write_run_summary(journal, stats, dangling_count=0, diagnostics=()):
    """The compact convenience summary next to the journal; journal stays
    authority. Atomic: temp file, flush, fsync, os.replace."""
    summary_path = os.path.splitext(journal)[0] + ".summary.json"
    doc = {
        "schema": 1,
        "kind": "DEL_JUNK_STREAM_SUMMARY",
        "start": stats.get("start"),
        "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disks": stats.get("disks", []),
        "result": stats.get("result"),
        "deleted_files": stats.get("deleted_files", 0),
        "deleted_dirs": stats.get("deleted_dirs", 0),
        "reclaimed_bytes": stats.get("reclaimed_bytes", 0),
        "risky_count": stats.get("risky_count", 0),
        "errors_count": stats.get("errors_count", 0),
        "completed_with_warnings": stats.get("completed_with_warnings", False),
        "stopped_early": stats.get("stopped_early", False),
        "journal": journal,
        "dangling_intent_count": dangling_count,
        "truncated_records": len(list(diagnostics)),
    }
    temp_path = summary_path + ".tmp"
    fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(json.dumps(doc, ensure_ascii=False,
                                    indent=1).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, summary_path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return summary_path

# The acceptance gate for destructive ALL DISKS execution. The runtime-critical
# clauses are proven at runtime on throwaway fixtures by
# all_disks_destructive_ready() immediately before offering destructive ALL
# DISKS; the repository test suite proves the broader regression matrix.
# Neither trusts the other: a CI pass from yesterday does not protect a broken
# executable today, and the runtime gate stays bounded to seconds.
ALL_DISKS_ACCEPTANCE_GATE = (
    "ALL_DISKS_UNATTENDED_READY = runtime destructive gate PASS (all clauses, "
    "this process) AND DEL JUNK regression suite PASS (repository tests)")

# Runtime gate clause names -- the shared vocabulary between the production
# gate and the test suite. The test suite drives the SAME named clauses; it may
# add exhaustive cases, but never a divergent oracle.
GATE_CLAUSES = (
    "safe_disk", "protected_tree", "identity_file", "identity_tree",
    "reparse_boundary", "overlap_lock", "journal_durable", "intent_order",
    "outcome_fail_stop", "cancel_truth", "iterative_depth",
    "iterative_delete_depth",
)

# The depth clause: the shared walker must survive a fixture deliberately
# deeper than a temporarily reduced recursion oracle. E-149/E-150: the real
# machine crashed os.walk-based traversal exactly here, so the runtime gate
# now proves depth independence every run -- bounded, seconds not minutes.
_DEPTH_CLAUSE_DIRS = 140
_DEPTH_CLAUSE_RECURSION_LIMIT = 50


def _depth_clause():
    """iterative_depth + iterative_delete_depth, on one throwaway fixture."""
    from del_junk_walk import iter_tree as _it, remove_tree_iterative as _rm
    clauses = {"iterative_depth": True, "iterative_delete_depth": True}
    fixture = tempfile.mkdtemp(prefix="saituls_deljunk_gate_depth_")
    try:
        node = fixture
        for _ in range(_DEPTH_CLAUSE_DIRS):
            node = os.path.join(node, "d")
            os.mkdir(node)
        with open(os.path.join(node, "leaf.bin"), "wb") as handle:
            handle.write(b"x" * 8)
        saved = sys.getrecursionlimit()
        sys.setrecursionlimit(_DEPTH_CLAUSE_RECURSION_LIMIT)
        try:
            walked = list(_it(fixture))
            if (len(walked) != _DEPTH_CLAUSE_DIRS + 1 or
                    not walked[-1][2] == ["leaf.bin"]):
                clauses["iterative_depth"] = False
            victim = os.path.join(fixture, "victim")
            os.mkdir(victim)
            v_node = victim
            for _ in range(60):
                v_node = os.path.join(v_node, "d")
                os.mkdir(v_node)
            with open(os.path.join(v_node, "x.bin"), "wb") as handle:
                handle.write(b"y")
            _rm(victim)
            if os.path.exists(victim):
                clauses["iterative_delete_depth"] = False
        except RecursionError:
            clauses["iterative_depth"] = False
            clauses["iterative_delete_depth"] = False
        except OSError:
            clauses["iterative_delete_depth"] = False
        finally:
            sys.setrecursionlimit(saved)
    except OSError:
        clauses["iterative_depth"] = False
        clauses["iterative_delete_depth"] = False
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
    return clauses


def classify_candidate(name, full_path, policy, now=None):
    """Stable reason a candidate is considered safe junk (auditability).

    dir:__pycache__ / dir:gpucache / file:.pyc / file:aged-.tmp / file:name.
    The policy predicates stay the single source of truth; this only names the
    rule that fired.
    """
    name_lower = name.lower()
    if policy.get("dir_junk")(name):
        return "dir:" + name_lower
    probe = full_path if isinstance(full_path, str) else name
    if policy.get("file_junk")(name, probe):
        ext = os.path.splitext(name_lower)[1]
        base = os.path.splitext(name_lower)[0]
        if ext in SAFE_DISK_EXTENSIONS:
            return "file:" + ext
        if base in SAFE_DISK_EXTENSIONS:
            return "file:" + base
        if ext in AGE_GATED_EXTENSIONS:
            return "file:aged-" + ext
        return "file:name:" + name_lower
    return ""


def wants_all_disks(argv):
    """True when the shell asked for the batch, not for one selected target."""
    return any(str(arg).strip('"').strip().lower() == ALL_DISKS_FLAG
               for arg in list(argv)[1:])


def wants_preview_only(argv):
    """--preview: keep the scan-only whole-machine behaviour on request."""
    return any(str(arg).strip('"').strip().lower() == ALL_DISKS_PREVIEW_FLAG
               for arg in list(argv)[1:])


def all_disks_policy(now=None):
    """ALL DISKS is SAFE_DISK, always.

    The policy is built here with aggressive=False and the batch never consults
    select_policy, so no drive root -- least of all the system drive -- can be
    talked into AGGRESSIVE_PROJECT.
    """
    return build_policy(aggressive=False,
                        now=time.time() if now is None else now)


def _gate_journal_probe():
    """journal_durable + intent_order + outcome_fail_stop + cancel_truth.

    Bounded throwaway fixtures; every destructive-critical journal behaviour is
    proven against the REAL journal_record()/stream_drive machinery. The only
    deletions are of the gate's own fixture files.
    """
    import unittest.mock as mock
    clauses = {"journal_durable": True, "intent_order": True,
               "outcome_fail_stop": True, "cancel_truth": True}
    this = sys.modules.get(__name__)
    if this is None:
        # Loaded by a test loader that does not register the module: patching
        # the live module object is unavailable, so patch the module-level
        # name directly in this namespace.
        class _ModuleProxy:
            def __init__(self, namespace):
                self.__dict__ = namespace

        this = _ModuleProxy(globals())
    fixture = tempfile.mkdtemp(prefix="saituls_deljunk_gate_j_")
    try:
        journal = os.path.join(fixture, "gate.journal.jsonl")
        # journal_durable: a real canary through the REAL journal_record().
        if not journal_record(journal, "INTENT", "GATE", "canary", "run", 7):
            return dict(clauses, journal_durable=False)
        with open(journal, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        if not (len(lines) == 1 and lines[0]["event"] == "INTENT"
                and lines[0]["path"] == "canary" and lines[0]["bytes"] == 7):
            return dict(clauses, journal_durable=False)

        # intent_order: without a durable INTENT nothing may be removed.
        removed = []
        real_remove = os.remove

        def watched_remove(path):
            removed.append(path)
            real_remove(path)

        # intent_order part 1: durable journal => fsync (the INTENT record)
        # happens before the first irreversible delete.
        order = []
        real_fsync = os.fsync

        def order_fsync(fd):
            order.append("fsync")
            real_fsync(fd)

        def order_remove(path):
            order.append("delete")
            real_remove(path)

        intent_dir = os.path.join(fixture, "order")
        os.makedirs(intent_dir)
        victim = os.path.join(intent_dir, "stale.pyc")
        with open(victim, "wb") as handle:
            handle.write(b"v" * 3)
        with mock.patch.object(os, "fsync", order_fsync), \
                mock.patch.object(os, "remove", order_remove):
            stream_drive(intent_dir + os.sep, all_disks_policy(),
                         os.path.join(fixture, "order.journal.jsonl"))
        if order[:2] != ["fsync", "delete"] or os.path.exists(victim):
            clauses["intent_order"] = False

        # intent_order part 2: the durable INTENT write fails => the candidate
        # is never removed (fsync-fail window).
        fail_dir = os.path.join(fixture, "order-fail")
        os.makedirs(fail_dir)
        fail_victim = os.path.join(fail_dir, "stale.pyc")
        with open(fail_victim, "wb") as handle:
            handle.write(b"v" * 3)
        fail_removed = []
        real_remove2 = os.remove

        def fail_remove(path):
            fail_removed.append(path)
            real_remove2(path)

        with mock.patch.object(this, "journal_record",
                               return_value=False), \
                mock.patch.object(os, "remove", fail_remove):
            result = stream_drive(fail_dir + os.sep, all_disks_policy(),
                                  os.path.join(fail_dir, "f.journal.jsonl"))
        if fail_removed or not os.path.exists(fail_victim) or \
                result.get("result") != JOURNAL_UNAVAILABLE:
            clauses["intent_order"] = False

        # outcome_fail_stop / cancel_truth: two candidates; candidate A
        # completes, then either its DELETED outcome write fails or a stop is
        # requested; candidate B must survive in both cases and the run must
        # stop truthfully.
        for case, outcome_fails, cancel_after in (
                ("outcome_fail_stop", True, False),
                ("cancel_truth", False, True)):
            case_dir = os.path.join(fixture, case)
            os.makedirs(case_dir)
            paths = []
            for n in ("a.pyc", "b.pyc"):
                path = os.path.join(case_dir, n)
                with open(path, "wb") as handle:
                    handle.write(b"x")
                paths.append(path)
            real_record = journal_record
            stop = threading.Event()

            def cancelled():
                return stop.is_set()

            def record(path, event, *args, _real=real_record,
                       _fails=outcome_fails, **kwargs):
                ok = _real(path, event, *args, **kwargs)
                if ok and event == "DELETED":
                    if _fails:
                        return False  # the durable outcome write is lost
                    stop.set()        # cancel right after candidate A
                return ok

            case_journal = os.path.join(fixture, case + ".journal.jsonl")
            try:
                with mock.patch.object(os, "remove", watched_remove), \
                        mock.patch.object(this,
                                          "journal_record", record):
                    result = stream_drive(case_dir + os.sep,
                                          all_disks_policy(), case_journal,
                                          cancelled=cancelled)
            except OSError:
                clauses[case] = False
                continue
            deleted_a = not os.path.exists(paths[0])
            deleted_b = not os.path.exists(paths[1])
            if outcome_fails:
                if not (deleted_a and not deleted_b
                        and result.get("reason") == JOURNAL_UNAVAILABLE):
                    clauses[case] = False
            else:
                if not (deleted_a and not deleted_b
                        and result.get("result") == PARTIAL):
                    clauses[case] = False
    except OSError:
        return {name: False for name in clauses}
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
    return clauses


def all_disks_destructive_ready(now=None):
    """Runtime destructive gate: every critical invariant, proven NOW.

    Bounded throwaway fixtures, seconds not minutes, no pytest history, no
    internet, no user data. Returns ``(ready, failed_clauses)`` where the
    clause names are the shared vocabulary of GATE_CLAUSES. A single failure
    downgrades ALL DISKS to refuse-to-delete -- no override, no "continue
    anyway".
    """
    failed = []
    results = {name: True for name in GATE_CLAUSES}
    policy = all_disks_policy(now)

    # ---- safe_disk: policy integrity, from the policy source of truth. ----
    try:
        if policy.get("mode") != "SAFE_DISK":
            results["safe_disk"] = False
        else:
            for name in ("build", "dist", "target", "node_modules", "logs"):
                if policy["dir_junk"](name) or not policy["dir_risky"](name):
                    results["safe_disk"] = False
            for ext in (".log", ".dmp", ".etl", ".evtx", ".obj"):
                if policy["file_junk"]("x" + ext, None):
                    results["safe_disk"] = False
            for name in (".git", ".svn", ".hg", "$recycle.bin",
                         "system volume information"):
                if not policy["prune_traversal"](name):
                    results["safe_disk"] = False
    except Exception:  # noqa: BLE001 - gate clause boundary
        results["safe_disk"] = False

    # ---- protected_tree + identity_file + identity_tree + reparse + lock ----
    fixture = tempfile.mkdtemp(prefix="saituls_deljunk_gate_")
    try:
        # protected_tree: protected file inside a junk-named dir => RISKY_EXCLUDED
        # and the pure cache next to it stays eligible.
        cache = os.path.join(fixture, "gpucache")
        os.makedirs(cache)
        with open(os.path.join(cache, "data.bin"), "w") as handle:
            handle.write("regenerable")
        with open(os.path.join(cache, "important.log"), "w") as handle:
            handle.write("the only copy of a crash story")
        plain = os.path.join(fixture, "other", "gpucache")
        os.makedirs(plain)
        with open(os.path.join(plain, "data.bin"), "w") as handle:
            handle.write("regenerable")
        files, dirs, risky = scan_junk(fixture, policy=policy)
        if cache not in risky or cache in dirs or plain not in dirs:
            results["protected_tree"] = False
    except OSError:
        results["protected_tree"] = False
    finally:
        shutil.rmtree(fixture, ignore_errors=True)

    # identity replacement: file + directory subtree, witness then replace.
    for clause, kind in (("identity_file", "file"), ("identity_tree", "dir")):
        fixture = tempfile.mkdtemp(prefix="saituls_deljunk_gate_%s_" % clause)
        try:
            if kind == "file":
                path = os.path.join(fixture, "stale.part")
                with open(path, "w") as handle:
                    handle.write("old transfer")
                os.utime(path, (time.time() - 30 * 86400,) * 2)
                manifest = dict(root=fixture, entries=[
                    dict(path=path, type="file", bytes=12,
                         identity=_identity(path))])
                # Replace the candidate after the witness: different bytes.
                with open(path, "w") as handle:
                    handle.write("REPLACED USER DATA")
                deleted = delete_junk([path], [], root_path=fixture,
                                      manifest=manifest)
                survived = os.path.exists(path)
                skipped = (not deleted[0] and
                           any("changed after confirmation" in reason
                               for _p, reason in deleted[2]))
                results[clause] = bool(survived and skipped)
            else:
                tree = os.path.join(fixture, "gpucache")
                os.makedirs(os.path.join(tree, "inner"))
                with open(os.path.join(tree, "inner", "blob.bin"), "w") as h:
                    h.write("regenerable")
                _n, digest, ident = _tree_snapshot(tree)
                manifest = dict(root=fixture, entries=[
                    dict(path=tree, type="dir", bytes=_n, identity=ident,
                         tree_sha256=digest)])
                # replace the whole subtree after the witness
                shutil.rmtree(tree, ignore_errors=True)
                os.makedirs(os.path.join(tree, "newcontent"))
                with open(os.path.join(tree, "newcontent", "x.bin"), "w") as h:
                    h.write("different")
                deleted = delete_junk([], [tree], root_path=fixture,
                                      manifest=manifest)
                survived = os.path.exists(tree)
                skipped = (not deleted[1] and
                           any("changed after confirmation" in reason
                               for _p, reason in deleted[2]))
                results[clause] = bool(survived and skipped)
        except OSError:
            results[clause] = False
        finally:
            shutil.rmtree(fixture, ignore_errors=True)

    # reparse_boundary: a real disposable junction; the outside sentinel must
    # survive and the junction never traversed. If this environment cannot
    # create a junction the clause fails closed.
    fixture = tempfile.mkdtemp(prefix="saituls_deljunk_gate_reparse_")
    link = None
    try:
        outside = os.path.join(fixture, "outside")
        os.makedirs(os.path.join(fixture, "root", "gpucache"))
        os.makedirs(outside)
        sentinel = os.path.join(outside, "sentinel.log")
        with open(sentinel, "w") as handle:
            handle.write("outside the boundary")
        link = os.path.join(fixture, "root", "gpucache", "boundary")
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, outside],
            capture_output=True)
        if made.returncode != 0 or not is_reparse_dir(link):
            results["reparse_boundary"] = False
        else:
            files, dirs, risky = scan_junk(
                os.path.join(fixture, "root"), policy=policy)
            gpucache = os.path.join(fixture, "root", "gpucache")
            if (gpucache in dirs or not os.path.exists(sentinel)
                    or not is_reparse_dir(link)):
                results["reparse_boundary"] = False
    except OSError:
        results["reparse_boundary"] = False
    finally:
        if link is not None and is_reparse_dir(link):
            try:
                os.rmdir(link)
            except OSError:
                pass
        shutil.rmtree(fixture, ignore_errors=True)

    # overlap_lock: containment both directions + equality + sibling coexist.
    root = os.path.normcase(os.path.abspath("R:" + os.sep))
    child = os.path.normcase(os.path.abspath("R:" + os.sep + "Projects"))
    sibling = os.path.normcase(os.path.abspath("R:" + os.sep + "Other"))
    if not (scopes_conflict(child, root) and scopes_conflict(root, child) and
            scopes_conflict(root, root) and
            not scopes_conflict(child, sibling)):
        results["overlap_lock"] = False

    # journal cluster: durable, INTENT-before-delete, outcome fail-stop, cancel.
    for name, ok in _gate_journal_probe().items():
        results[name] = bool(ok)

    # depth cluster (E-149/E-150): the shared iterative walker and the
    # iterative delete primitive must both survive a fixture deeper than a
    # deliberately reduced recursion oracle. A failure here BLOCKS the
    # destructive ALL DISKS run with zero deletion.
    for name, ok in _depth_clause().items():
        results[name] = bool(ok)

    failed = [name for name in GATE_CLAUSES if not results[name]]
    return not failed, failed


def plan_all_disks(targets, now=None, progress=None, cancelled=None,
                   lock=None, stage=None):
    """Scan every eligible drive under SAFE_DISK and return one combined plan.

    ``(sections, skipped)``. Each section is one drive's confirmed candidate set
    plus its own manifest bytes; the reservation taken for it is held until the
    caller releases it, so nothing else can clean that scope in between. A drive
    that fails or vanishes is recorded and the batch continues -- only user
    cancellation (ScanCancelled) abandons the whole pass, with nothing deleted
    and every reservation handed back.
    """
    lock = single_flight_lock if lock is None else lock
    policy = all_disks_policy(now)
    sections = []
    skipped = []
    held = []
    try:
        for record in targets:
            root = record.get("root", "")
            if cancelled is not None and cancelled():
                raise ScanCancelled(len(sections))
            entry = dict(record)
            entry["capacity_bytes"] = record.get("total_bytes")
            if not os.path.isdir(root):
                entry.update({"result": SKIPPED,
                              "reason": "became unavailable before the scan"})
                skipped.append(entry)
                continue
            if not lock(root):
                scope = conflicting_scope(_scope_key(root))
                entry.update({
                    "result": SKIPPED,
                    "reason": ("scope is already owned by an active cleanup of %s"
                               % scope) if scope else
                              "scope is already owned by another DEL JUNK run",
                })
                skipped.append(entry)
                continue
            entry["locked"] = True
            held.append(entry)
            if stage is not None:
                stage(root)
            try:
                junk_files, junk_dirs, risky = scan_junk(
                    root, policy=policy, progress=progress, cancelled=cancelled)
                total_bytes, manifest = manifest_bytes(
                    junk_files, junk_dirs, risky, root_path=root,
                    mode="SAFE_DISK", progress=progress, cancelled=cancelled)
            except ScanCancelled:
                raise
            except (OSError, ValueError) as exc:
                entry.update({"result": FAILED, "reason": str(exc)})
                skipped.append(entry)
                continue
            entry.update({
                "junk_files": junk_files,
                "junk_dirs": junk_dirs,
                "risky_excluded": risky,
                "file_count": len(junk_files),
                "dir_count": len(junk_dirs),
                "risky_count": len(risky),
                "total_bytes": total_bytes,
                "manifest": manifest,
                "manifest_path": None,
            })
            sections.append(entry)
    except ScanCancelled:
        release_all_disks(held)
        raise
    return sections, skipped


def release_all_disks(records):
    """Give every reservation this batch took back to the next process."""
    for record in records:
        if record.get("locked"):
            release_single_flight(record["root"])
            record["locked"] = False


def batch_manifest(sections, skipped, mode="SAFE_DISK"):
    """The master manifest: one auditable document for the whole batch.

    Counts and bytes come from the per-drive manifests, so the preview the user
    confirms and the manifests the delete phase revalidates against cannot
    disagree.
    """
    drives = []
    totals = {
        "drives_scanned": len(sections),
        "drives_skipped": len(skipped),
        "file_count": 0,
        "dir_count": 0,
        "total_bytes": 0,
        "risky_excluded": 0,
    }
    for section in sections:
        drives.append({
            "root": section["root"],
            "drive_type": section.get("drive_type_name", "FIXED"),
            "is_system": bool(section.get("is_system")),
            "capacity_bytes": section.get("capacity_bytes"),
            "free_bytes_before": section.get("free_bytes"),
            "file_count": section["file_count"],
            "dir_count": section["dir_count"],
            "total_bytes": section["total_bytes"],
            "risky_excluded": section["risky_count"],
            "manifest_path": section.get("manifest_path"),
        })
        totals["file_count"] += section["file_count"]
        totals["dir_count"] += section["dir_count"]
        totals["total_bytes"] += section["total_bytes"]
        totals["risky_excluded"] += section["risky_count"]
    doc = {
        "schema": 1,
        "kind": "DEL_JUNK_ALL_DISKS",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": mode,
        "policy": mode,
        "recycle_bin_used": False,
        "acceptance_gate": ALL_DISKS_ACCEPTANCE_GATE,
        "totals": totals,
        "drives": drives,
        "skipped": [{"root": item.get("root"),
                     "drive_type": item.get("drive_type_name", "UNKNOWN"),
                     "result": item.get("result", SKIPPED),
                     "reason": item.get("reason", "")}
                    for item in skipped],
    }
    return doc, json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")


def all_disks_preview(doc, manifest_path, gate_failures=()):
    """The one confirmation the user gets, built from the master manifest."""
    totals = doc["totals"]
    lines = ["DEL JUNK ALL DISKS — %s" % doc["mode"], "", "Drives:"]
    for drive in doc["drives"]:
        lines.append("    %-6s candidates %d   reclaim %s%s" % (
            drive["root"],
            drive["file_count"] + drive["dir_count"],
            format(drive["total_bytes"], ","),
            "   [system]" if drive["is_system"] else ""))
    if not doc["drives"]:
        lines.append("    (нет)")
    lines += [
        "",
        "TOTAL:",
        "    drives scanned      %d" % totals["drives_scanned"],
        "    drives skipped      %d" % totals["drives_skipped"],
        "    files               %d" % totals["file_count"],
        "    directories         %d" % totals["dir_count"],
        "    reclaimable bytes   %s" % format(totals["total_bytes"], ","),
        "    excluded/risky      %d" % totals["risky_excluded"],
    ]
    if doc["skipped"]:
        lines += ["", "Skipped:"]
        for item in doc["skipped"][:8]:
            lines.append("    %-6s %s — %s" % (item["root"], item["result"],
                                               item["reason"]))
        if len(doc["skipped"]) > 8:
            lines.append("    ... и ещё %d" % (len(doc["skipped"]) - 8))
    lines += [
        "",
        "SAFE_DISK ONLY",
        "RECYCLE BIN IS NOT USED",
        "DELETION IS PERMANENT",
        "",
        "Master manifest: %s" % manifest_path,
    ]
    if gate_failures:
        lines += ["", "УДАЛЕНИЕ ЗАБЛОКИРОВАНО (%s): %s" % (
            "ALL_DISKS_DESTRUCTIVE_READY", ", ".join(gate_failures))]
    return "\n".join(lines)


def execute_all_disks(sections, cancelled=None, progress=None):
    """Delete drive by drive, sequentially, from the confirmed manifests.

    One drive's failure never cancels the others, and the per-drive result state
    is derived from what actually happened -- a cancelled or partial pass is
    never reported as CLEANED.
    """
    results = []
    for section in sections:
        root = section["root"]
        planned = section["file_count"] + section["dir_count"]
        result = {"root": root, "planned": planned, "planned_bytes":
                  section["total_bytes"], "deleted_files": 0, "deleted_dirs": 0,
                  "errors": [], "result": NOTHING_TO_DO}
        if cancelled is not None and cancelled():
            result["result"] = NOTHING_TO_DO if planned == 0 else CANCELLED
            result["errors"] = [(root, "cancelled before this drive was touched")]
            results.append(result)
            continue
        if progress is not None:
            progress(root)
        try:
            deleted_files, deleted_dirs, errors = delete_junk(
                section["junk_files"], section["junk_dirs"], root_path=root,
                manifest=section["manifest"], cancelled=cancelled)
        except OSError as exc:
            result["result"] = FAILED
            result["errors"] = [(root, str(exc))]
            results.append(result)
            continue
        deleted = len(deleted_files) + len(deleted_dirs)
        stopped = bool(cancelled is not None and cancelled())
        result.update({"deleted_files": len(deleted_files),
                       "deleted_dirs": len(deleted_dirs),
                       "errors": errors,
                       "deleted_file_paths": deleted_files,
                       "deleted_dir_paths": deleted_dirs})
        if planned == 0:
            result["result"] = NOTHING_TO_DO
        elif stopped:
            result["result"] = PARTIAL if deleted else CANCELLED
        elif deleted == planned and not errors:
            result["result"] = CLEANED
        elif deleted:
            result["result"] = PARTIAL
        else:
            result["result"] = FAILED
        results.append(result)
    return results


def write_batch_log(manifest_path, doc, results, gate_failures=()):
    """Human-readable truth next to the master manifest."""
    log_path = os.path.splitext(manifest_path)[0] + ".log.txt"
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write("DEL JUNK ALL DISKS — %s\n" % doc["mode"])
        handle.write("%s\n" % ALL_DISKS_ACCEPTANCE_GATE)
        handle.write("Gate: %s\n" % ("PASS" if not gate_failures else
                                     "BLOCKED (%s)" % ", ".join(gate_failures)))
        totals = doc["totals"]
        handle.write("Дисков просканировано: %d, пропущено: %d\n" % (
            totals["drives_scanned"], totals["drives_skipped"]))
        handle.write("Запланировано файлов: %d, папок: %d, байт: %d\n\n" % (
            totals["file_count"], totals["dir_count"], totals["total_bytes"]))
        for item in doc["skipped"]:
            handle.write("%s %s — %s\n" % (item["root"], item["result"],
                                           item["reason"]))
        for result in results:
            handle.write("%s %s — удалено файлов %d, папок %d из %d\n" % (
                result["root"], result["result"], result["deleted_files"],
                result["deleted_dirs"], result["planned"]))
            for path, error in result["errors"][:200]:
                handle.write("    ПРОПУЩЕНО: %s — %s\n" % (path, error))
    return log_path


def run_all_disks(tk_root, preview_only=False):
    """ALL DISKS AVAILABLE: one SAFE_DISK pass over every eligible local disk.

    ``preview_only`` keeps the historical scan-everything-then-confirm-once
    behaviour. The DEFAULT (no --preview) is the long-requested STREAMING flow:
    each eligible drive is traversed with candidate-local preflight and deletion
    immediately before the next drive starts, so the user sees reclaimed bytes
    grow before the whole machine scan ends.
    """
    targets, skipped = eligible_drives()
    if not targets:
        detail = "\n".join("%s — %s" % (item["root"], item["reason"])
                           for item in skipped[:12])
        messagebox.showinfo(
            "DEL JUNK ALL DISKS",
            "Подходящих дисков не найдено. Ничего не изменено.\n\n"
            "SAFE_DISK ONLY\n\n" + (detail or "Локальные фиксированные диски не обнаружены."))
        return 0
    if preview_only:
        return run_all_disks_preview(tk_root, targets, skipped)
    previous_run = incomplete_previous_run()
    return stream_all_disks(tk_root, targets, skipped,
                            previous_run=previous_run)


def run_all_disks_preview(tk_root, targets, skipped):
    """--preview: scan/report ONLY. Never delete, never write INTENT, never
    invoke the destructive candidate path.

    It generates drives' manifests and a master batch manifest, reports the
    exact candidate set and risky exclusions, and returns. The user may keep
    it for a complete scan-first review; a separate confirmation-free run is
    NOT offered -- the destructive path is the streaming run behind an explicit
    start confirmation.
    """
    sections = []
    progress = ScanProgress(
        tk_root, ", ".join(item["root"] for item in targets), "SAFE_DISK",
        header="Проверяются все доступные диски без удаления:")
    try:
        try:
            sections, drive_skips = plan_all_disks(
                targets, progress=progress.update, cancelled=progress.cancelled,
                stage=lambda root: progress.set_stage("Сканирование " + root))
            skipped = skipped + drive_skips
        except ScanCancelled:
            release_all_disks(sections)
            messagebox.showinfo(
                "DEL JUNK ALL DISKS",
                "Сканирование отменено. Ни один диск не изменён.")
            return 0
        except Exception as exc:  # noqa: BLE001 - top-level UI failure boundary
            release_all_disks(sections)
            messagebox.showerror(
                "DEL JUNK ALL DISKS",
                "Сканирование остановлено:\n" +
                _scan_failure_report("all-disks preview scan",
                                     ", ".join(item["root"] for item in targets),
                                     getattr(progress, "metrics", {}).get("path"),
                                     exc))
            return 2
    finally:
        progress.close()

    try:
        # Manifests are written for inspection/reuse but NEVER acted on here.
        for section in sections:
            try:
                section["manifest_path"] = save_manifest(
                    section["manifest"], section["root"])
            except OSError:
                section["manifest_path"] = None
        doc, master = batch_manifest(sections, skipped)
        manifest_path = None
        try:
            manifest_path = save_manifest(master, "ALL-DISKS")
        except OSError as exc:
            messagebox.showerror(
                "DEL JUNK ALL DISKS",
                f"Не удалось сохранить общий манифест:\n\n{exc}")
            return 2

        preview = all_disks_preview(doc, manifest_path)
        write_batch_log(manifest_path, doc, [], ())
        messagebox.showinfo("DEL JUNK ALL DISKS — предпросмотр (без удаления)",
                           preview)
        return 0
    finally:
        release_all_disks(sections)


def stream_drive(root, policy, journal, cancelled=None, progress=None,
                 gate_failures=(), stop_fatal=None):
    """Traverse once globally; witness, commit intent and finish one candidate.

    ``stop_fatal`` is a shared mutable stop signal for the whole run: a
    destructive I/O failure after a durable INTENT sets it, because an
    unexplained mutation failure during unattended whole-machine cleanup must
    stop the run rather than march on through thousands more candidates.
    """
    result = dict(root=root, result=NOTHING_TO_DO, planned=0,
                  deleted_files=0, deleted_dirs=0, reclaimed_bytes=0,
                  errors=[], risky_count=0, scanned_dirs=0, scanned_files=0,
                  unreadable=0, busy_skips=0)
    metrics = dict(drive=root, path=root, dirs=0, files=0, reclaimed=0,
                   deleted_files=0, deleted_dirs=0, risky=0, errors=0,
                   fs_last_progress=time.monotonic())
    stopped = cancelled or (lambda: False)

    def fatal_stop():
        return bool(stop_fatal is not None and stop_fatal.get("stop"))

    def publish(path=None):
        if path is not None:
            metrics["path"] = path
            metrics["fs_last_progress"] = time.monotonic()
        if progress:
            progress(dict(metrics))

    def record(event, path, kind="", size=0, error="", identity=None):
        if not journal_record(journal, event, root, path, kind, size,
                              error, identity=identity):
            raise JournalUnavailable("JOURNAL_UNAVAILABLE: " + journal)

    def local_progress(dirs, files):
        # Local witness/revalidation work counts as filesystem progress, too.
        publish(metrics["path"])

    def exclude(path):
        result["risky_count"] += 1
        metrics["risky"] += 1
        record("RISKY_EXCLUDED", path, "risky")
        publish(path)

    def candidate(path, kind):
        if stopped() or fatal_stop():
            raise ScanCancelled(metrics["dirs"])
        publish(path)
        if (is_own_evidence(path) or not _path_within_root(path, root)
                or is_reparse_point(path)):
            exclude(path)
            return
        reason = classify_candidate(os.path.basename(path), path, policy)
        if kind == "dir":
            if tree_has_protected_boundary(path, policy,
                    progress=local_progress, cancelled=stopped):
                exclude(path)
                return
            size, digest, ident = _tree_snapshot(
                path, progress=local_progress, cancelled=stopped)
            entry = dict(path=path, type=kind, bytes=size,
                         identity=ident, tree_sha256=digest)
        else:
            ident = _identity(path)
            size = ident["size"]
            entry = dict(path=path, type=kind, bytes=size, identity=ident)
        if stopped() or fatal_stop():
            raise ScanCancelled(metrics["dirs"])
        result["planned"] += 1
        # Persist the complete candidate witness, including subtree digest.
        record("INTENT", path, reason or kind, size, identity=entry)
        outcomes = []
        delete_junk([path] if kind == "file" else [],
                    [path] if kind == "dir" else [], root_path=root,
                    manifest=dict(root=root, entries=[entry]),
                    outcomes=outcomes, policy=policy, progress=local_progress)
        outcome = outcomes[0]
        event = outcome["event"]
        if event == "DELETED":
            key = "deleted_files" if kind == "file" else "deleted_dirs"
            result[key] += 1
            metrics[key] += 1
            # No stat after deletion: these bytes belong to this witness.
        elif event == "RISKY_EXCLUDED":
            result["risky_count"] += 1
            metrics["risky"] += 1
        else:
            result["errors"].append((path, outcome["error"]))
            metrics["errors"] += 1
            if event in ("SKIPPED_BUSY", "PARTIAL_BUSY"):
                # Recoverable contention: a candidate-local warning only.
                # It must never arm stop_fatal and never end the traversal.
                result["busy_skips"] += 1
        record(event, path, reason or kind,
               size if event == "DELETED" else 0, outcome["error"])
        if event == "DELETED":
            result["reclaimed_bytes"] += size
            metrics["reclaimed"] += size
        elif event == "FAILED":
            # Unknown destructive I/O failure after a durable INTENT. The
            # structured taxonomy (del_junk_errors.classify_delete_error) has
            # already drained every recoverable contention outcome into
            # SKIPPED_BUSY / PARTIAL_BUSY / SKIPPED_CHANGED, so a FAILED here
            # is by construction unexplained: the candidate stops with its
            # durable FAILED outcome -- and the whole unattended run must not
            # continue. Classification is by exception type/errno/winerror,
            # never by message text.
            journal_record(journal, "IO_FAILURE_STOP", root, root, "run",
                           error="destructive I/O failure; run stopped")
            result["reason"] = "IO_FAILURE_STOP"
            if stop_fatal is not None:
                stop_fatal["stop"] = True
        publish(path)

    try:
        if policy.get("mode") != "SAFE_DISK":
            gate_failures = ("SAFE_DISK required",)
        if gate_failures:
            record("REFUSED", root, "run", error=", ".join(gate_failures))
            result["result"] = "REFUSED"
            return result

        def walk_error(error):
            # Scan-only access failure BEFORE any candidate acceptance: a
            # truthful non-destructive skip, never INTENT, never deletion,
            # never counted as CLEANED. Does not stop the run.
            result["unreadable"] += 1
            journal_record(journal, "SKIPPED_UNREADABLE", root,
                           error.filename or root, "scan", 0, str(error))

        for current, dirs, files in iter_tree(root, onerror=walk_error):
            if stopped() or fatal_stop():
                raise ScanCancelled(metrics["dirs"])
            metrics["dirs"] += 1
            publish(current)
            descend = []
            for name in dirs:
                if stopped() or fatal_stop():
                    raise ScanCancelled(metrics["dirs"])
                path = os.path.join(current, name)
                if (name.lower() in PRUNE_ALWAYS_DIRS or is_reparse_dir(path)
                        or policy["dir_risky"](name)):
                    exclude(path)
                elif policy["dir_junk"](name):
                    try:
                        candidate(path, "dir")
                    except OSError as exc:
                        walk_error(exc)
                else:
                    descend.append(name)
            dirs[:] = descend
            for name in files:
                if stopped() or fatal_stop():
                    raise ScanCancelled(metrics["dirs"])
                path = os.path.join(current, name)
                metrics["files"] += 1
                publish(path)
                if is_reparse_point(path):
                    exclude(path)
                elif policy["file_junk"](name, path):
                    try:
                        candidate(path, "file")
                    except OSError as exc:
                        walk_error(exc)
                elif policy["file_risky"](name, path):
                    exclude(path)
        if stopped() or fatal_stop():
            raise ScanCancelled(metrics["dirs"])
        deleted = result["deleted_files"] + result["deleted_dirs"]
        if result.get("reason") == "IO_FAILURE_STOP":
            result["result"] = PARTIAL if deleted else FAILED
        elif result["errors"]:
            # Recoverable candidate outcomes (busy/locked contentions and
            # post-INTENT races; scan-permission skips never get here) keep
            # the drive from claiming CLEANED, but they are warnings, not a
            # fatal claim: FAILED is reserved for IO_FAILURE_STOP.
            result["result"] = PARTIAL
        elif deleted:
            result["result"] = CLEANED if not result.get("unreadable") else PARTIAL
        else:
            result["result"] = (PARTIAL if result.get("unreadable")
                                else NOTHING_TO_DO)
    except ScanCancelled:
        result["result"] = PARTIAL if (
            result["deleted_files"] or result["deleted_dirs"]) else CANCELLED
        if result.get("reason") != "IO_FAILURE_STOP":
            if not journal_record(journal, "CANCELLED", root, root, "run",
                                  error="stop requested; deleted data is not restored"):
                result["errors"].append((root, JOURNAL_UNAVAILABLE))
                result["reason"] = JOURNAL_UNAVAILABLE
    except JournalUnavailable as exc:
        result["reason"] = JOURNAL_UNAVAILABLE
        result["result"] = PARTIAL if (
            result["deleted_files"] or result["deleted_dirs"]) else JOURNAL_UNAVAILABLE
        result["errors"].append((root, str(exc)))
        journal_record(journal, JOURNAL_UNAVAILABLE, root, root, "run",
                       error=str(exc))
    finally:
        result["scanned_dirs"] = metrics["dirs"]
        result["scanned_files"] = metrics["files"]
        publish()
    return result


def _summary_journal_dir(directory=None):
    return os.path.dirname(journal_path(directory))


def start_confirmation(tk_root, targets, skipped, ready, failures, journal,
                       previous=None):
    """The ONE pre-destructive confirmation. Built from eligible disks + the
    runtime gate + policy summary -- never from a full-disk scan.

    Returns True only when the user explicitly starts the cleanup. Cancel
    means zero deletions, zero INTENT records, no disk mutation.
    """
    drive_lines = []
    for item in targets:
        drive_lines.append("    %s%s" % (
            item["root"],
            "   [SYSTEM DRIVE]" if item.get("is_system") else ""))
    text = "\n".join([
        "DEL JUNK ALL DISKS — start confirmation",
        "",
        "SAFE_DISK ONLY — the conservative disk policy.",
        "Deletion is PERMANENT: no Recycle Bin, no rollback.",
        "",
        "Eligible disks:",
    ] + drive_lines + [
        "",
        "Skipped disks: %d" % len(skipped),
        "Runtime safety gate: %s" % (
            "PASS" if ready else "BLOCKED (%s)" % ", ".join(failures)),
        "Journal: %s" % journal,
        "",
        "Deletion starts IMMEDIATELY after confirmation and continues",
        "unattended (drive by drive, streaming). You may walk away.",
        "Cancel stays available; cancellation is not rollback.",
    ])
    if previous is not None:
        text += ("\n\nPREVIOUS CLEANUP DID NOT END CLEANLY.\n"
                 "Journal: %s\nDangling intents: %d%s" % (
                     previous["journal"], previous["dangling"],
                     ("  (%s)" % previous["diagnostics"][0]["problem"])
                     if previous.get("diagnostics") else ""))
    text += "\n\nStart the cleanup now?"
    return messagebox.askyesno("DEL JUNK ALL DISKS — confirm start", text)


def stream_all_disks(tk_root, targets, skipped, confirm=None,
                     previous_run=None, journal=None):
    """One filesystem worker; the Tk thread keeps heartbeat and stop responsive.

    Sequence (SAFETY FIRST): journal destination test -> runtime gate ->
    incomplete-run notice -> explicit start confirmation -> durable RUN_START ->
    drive 1. After RUN_START every drive gets DRIVE_START/DRIVE_END, and the
    run gets a durable RUN_END plus a summary file. Fatal integrity failures
    (journal loss, destructive I/O failure, invariant violation) stop the run
    before the next candidate and before any later drive.
    """
    # Confirmator seam: production uses the Tk dialog; tests inject their own.
    if confirm is None:
        def confirm(targets, skipped, ready, failures, journal, previous):
            return start_confirmation(
                tk_root, targets, skipped, ready, failures, journal, previous)
    try:
        journal = journal or journal_path()
        # Journal destination test: one canary write through the REAL
        # machinery, then a fresh clean journal so the run envelope starts at
        # exactly one RUN_START.
        probe = journal + ".probe"
        if not journal_record(probe, "INTENT", "", "canary", "run", 0):
            raise OSError(JOURNAL_UNAVAILABLE + ": " + journal)
        try:
            os.remove(probe)
        except OSError:
            pass
    except OSError as exc:
        messagebox.showerror("DEL JUNK", "JOURNAL_UNAVAILABLE: " + str(exc))
        return 2

    # The runtime destructive gate runs BEFORE the confirmation so the user is
    # never asked to confirm first and then told the gate is broken.
    ready, failures = all_disks_destructive_ready()
    if not ready:
        # A blocked gate is zero deletion: no confirmation, no RUN_START.
        journal_run_record(journal, "RUN_END", {
            "result": "BLOCKED", "gate": failures})
        text = ("ALL DISKS deletion BLOCKED — runtime safety gate failed.\n"
                "Failed clauses: %s\n\nZero deletion. Journal: %s"
                % (", ".join(failures), journal))
        if previous_run is None:
            previous_run = incomplete_previous_run(
                os.path.dirname(journal))
        if previous_run is not None:
            text += ("\n\nPrevious cleanup did not end cleanly.\n"
                     "Journal: %s\nDangling intents: %d"
                     % (previous_run["journal"], previous_run["dangling"]))
        messagebox.showwarning("DEL JUNK", text)
        return 3
    if not confirm(targets, skipped, True, (), journal, previous_run):
        # User cancel: zero deletions, zero INTENT records, no mutation.
        return 1

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    gate_clause_results = {name: True for name in GATE_CLAUSES}
    if not journal_run_record(journal, "RUN_START", {
            "schema": 1, "mode": "SAFE_DISK",
            "roots": [item["root"] for item in targets],
            "gate": gate_clause_results, "started": started}):
        messagebox.showerror("DEL JUNK", "JOURNAL_UNAVAILABLE: " + journal)
        return 2

    progress = ScanProgress(tk_root, ", ".join(r["root"] for r in targets),
                            "SAFE_DISK",
                            header="Потоковая очистка: удаление необратимо")
    stop = threading.Event()
    progress.stop_event = stop
    try:
        if progress.cancelled():
            stop.set()
    except Exception:  # noqa: BLE001 - fatal UI failure boundary
        stop.set()
    stop_fatal = {"stop": False}
    results = []
    latest = {}
    fatal = []
    current_root = [""]
    totals = dict(deleted_files=0, deleted_dirs=0, reclaimed=0,
                  risky=0, errors=0, dirs=0, files=0)

    def update(fields):
        nonlocal latest
        snapshot = dict(fields)
        for key, value in totals.items():
            snapshot[key] = snapshot.get(key, 0) + value
        latest = snapshot

    def work():
        try:
            for item in targets:
                root = item["root"]
                current_root[0] = root
                if stop.is_set() or stop_fatal["stop"]:
                    results.append(dict(root=root, result=CANCELLED))
                    journal_record(journal, "CANCELLED", root, root, "run")
                    break
                if not os.path.isdir(root) or not single_flight_lock(root):
                    results.append(dict(root=root, result=SKIPPED,
                                        reason="unavailable or scope already owned"))
                    continue
                journal_run_record(journal, "DRIVE_START", {"root": root},
                                   drive=root)
                try:
                    result = stream_drive(root, all_disks_policy(), journal,
                        cancelled=stop.is_set, progress=update,
                        stop_fatal=stop_fatal)
                    results.append(result)
                    journal_run_record(journal, "DRIVE_END", {
                        "root": root, "scanned_dirs": result.get("scanned_dirs", 0),
                        "scanned_files": result.get("scanned_files", 0),
                        "deleted_files": result.get("deleted_files", 0),
                        "deleted_dirs": result.get("deleted_dirs", 0),
                        "reclaimed_bytes": result.get("reclaimed_bytes", 0),
                        "risky_count": result.get("risky_count", 0),
                        "errors": len(result.get("errors", [])),
                        "result": result.get("result")}, drive=root)
                    for key, source in (("deleted_files", "deleted_files"),
                            ("deleted_dirs", "deleted_dirs"),
                            ("reclaimed", "reclaimed_bytes"), ("risky", "risky_count"),
                            ("dirs", "scanned_dirs"), ("files", "scanned_files")):
                        totals[key] += result.get(source, 0)
                    totals["errors"] += len(result.get("errors", []))
                    if (result.get("reason") == JOURNAL_UNAVAILABLE
                            or stop.is_set() or stop_fatal["stop"]):
                        break
                finally:
                    release_single_flight(root)
        except Exception as exc:  # noqa: BLE001 - worker failure boundary
            fatal.append(str(exc))

    worker = threading.Thread(target=work, name="DEL_JUNK-stream")
    worker.start()
    user_cancelled = {"flag": False}
    try:
        while worker.is_alive():
            try:
                ui_died = progress.cancelled()
            except Exception:  # noqa: BLE001 - fatal UI failure boundary
                # A crashed UI must not leave the worker deleting invisibly:
                # propagate the stop, let the current candidate finish
                # safely, finalize the journal and exit nonzero.
                ui_died = True
            if ui_died:
                user_cancelled["flag"] = True
                stop.set()
            fields = latest
            if fields and hasattr(progress, "set_metrics"):
                progress.count = fields.get("dirs", 0)
                progress.file_count = fields.get("files", 0)
                progress.last_fs_progress = fields["fs_last_progress"]
                progress.set_metrics(**fields)
            worker.join(.04)
    finally:
        stop.set()
        worker.join()
        progress.close()

    # Run-level result: COMPLETE / CANCELLED / PARTIAL / FAILED.
    mutated = totals["deleted_files"] + totals["deleted_dirs"]
    journal_lost = any(r.get("reason") == JOURNAL_UNAVAILABLE for r in results)
    io_stopped = any(r.get("reason") == "IO_FAILURE_STOP" for r in results)
    refused = any(r.get("result") == "REFUSED" for r in results)
    cancelled_mid_run = any(r.get("result") == CANCELLED for r in results)
    stopped_early = bool(fatal or journal_lost or io_stopped
                         or user_cancelled["flag"] or cancelled_mid_run)
    # Recoverable candidate warnings (busy/locked/raced) recorded while every
    # planned drive was still traversed to the end: the run is finished, not
    # stopped early, and tells the truth about what it could not delete.
    completed_with_warnings = (not stopped_early and not refused
                               and totals["errors"] > 0)
    run_result = "FAILED"
    if fatal or journal_lost or io_stopped:
        run_result = "PARTIAL" if mutated else "FAILED"
    elif user_cancelled["flag"] or cancelled_mid_run:
        run_result = "CANCELLED" if not mutated else "PARTIAL"
    elif refused:
        run_result = "FAILED"
    elif completed_with_warnings:
        run_result = "PARTIAL"
    else:
        run_result = "COMPLETE"
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run_end_ok = journal_run_record(journal, "RUN_END", {
        "result": run_result, "started": started, "ended": end,
        "deleted_files": totals["deleted_files"],
        "deleted_dirs": totals["deleted_dirs"],
        "reclaimed_bytes": totals["reclaimed"],
        "risky_count": totals["risky"],
        "errors": totals["errors"],
        "completed_with_warnings": completed_with_warnings,
        "stopped_early": stopped_early})

    summary_path = None
    dangling = []
    diagnostics = []
    try:
        records, diagnostics = parse_journal(journal)
        dangling = dangling_intents(records)
        summary_path = write_run_summary(journal, {
            "start": started, "end": end,
            "disks": [{"root": r["root"], "result": r.get("result"),
                       "deleted_files": r.get("deleted_files", 0),
                       "deleted_dirs": r.get("deleted_dirs", 0),
                       "reclaimed_bytes": r.get("reclaimed_bytes", 0),
                       "reason": r.get("reason", "")}
                      for r in results],
            "result": run_result,
            "deleted_files": totals["deleted_files"],
            "deleted_dirs": totals["deleted_dirs"],
            "reclaimed_bytes": totals["reclaimed"],
            "risky_count": totals["risky"],
            "errors_count": totals["errors"],
            "completed_with_warnings": completed_with_warnings,
            "stopped_early": stopped_early},
            dangling_count=len(dangling), diagnostics=diagnostics)
    except OSError:
        pass  # summary is convenience; journal remains authority

    if fatal:
        messagebox.showerror(
            "DEL JUNK",
            "Поток остановлен: " +
            _scan_failure_report("streaming run", current_root[0], "", fatal[0]) +
            "\nУдалённое не восстанавливается. Журнал: " + journal)
        return 2
    if journal_lost or io_stopped:
        messagebox.showerror(
            "DEL JUNK",
            "Run stopped: %s\nDeleted data is not restored.\nJournal: %s%s" % (
                "journal unavailable" if journal_lost else
                "destructive I/O failure", journal,
                ("\nSummary: " + summary_path) if summary_path else ""))
        return 2
    if not run_end_ok:
        messagebox.showerror(
            "DEL JUNK",
            "RUN_END could not be recorded durably: the journal is unavailable "
            "and the final state is NOT witnessed.\nJournal: " + journal)
        return 2
    summary = "\n".join("%s %s — файлов %d, папок %d" % (
        item["root"], item["result"], item.get("deleted_files", 0),
        item.get("deleted_dirs", 0)) for item in results + list(skipped))
    messagebox.showinfo(
        "DEL JUNK — результат (%s)" % run_result,
        "%s\nОсвобождено байт: %d\nЖурнал: %s%s%s\nУдалённое не восстанавливается." % (
            summary, totals["reclaimed"], journal,
            ("\nСводка: " + summary_path) if summary_path else "",
            ("\nDangling intents: %d" % len(dangling)) if dangling else ""))
    # Exit code contract: 0 complete/nothing to do; 1 user cancelled before
    # mutation; 2 fatal/partial destructive run; 3 safety gate blocked.
    if run_result in (PARTIAL, FAILED):
        return 2
    if run_result == CANCELLED:
        return 1
    return 0


def _safe_size(path):
    """Best-effort file size for the journal; never fatal on a broken path."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if wants_all_disks(argv):
        preview_only = wants_preview_only(argv)
        root = tk.Tk()
        root.withdraw()
        try:
            return run_all_disks(root, preview_only=preview_only)
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass
    if len(argv) > 1:
        target_dir = shell_target(argv[1])
    else:
        target_dir = os.getcwd()

    root = tk.Tk()
    root.withdraw()

    if not os.path.isdir(target_dir):
        messagebox.showerror("DEL JUNK", f"Каталог не существует:\n{target_dir}")
        root.destroy()
        return 2

    # Single-flight: one scan/delete per target.
    lock = single_flight_lock(target_dir)
    if lock is None:
        messagebox.showerror(
            "DEL JUNK",
            "Для этой папки уже запущена очистка либо не удалось создать блокировку.")
        root.destroy()
        return 1

    try:
        # Policy: drive root -> SAFE_DISK, else the historical aggressive set.
        mode = select_policy(target_dir)
        aggressive = (mode == "AGGRESSIVE_PROJECT")
        policy = build_policy(aggressive=aggressive, now=time.time())

        progress = ScanProgress(root, target_dir, mode)
        try:
            junk_files, junk_dirs, risky_excluded = scan_junk(
                target_dir, policy=policy, progress=progress.update,
                cancelled=progress.cancelled)

            if not junk_files and not junk_dirs:
                progress.close()
                if mode == "SAFE_DISK" and risky_excluded:
                    messagebox.showinfo(
                        "Результат (SAFE_DISK)",
                        "Мусор не найден.\n\n"
                        f"Осторожных кандидатов пропущено: {len(risky_excluded)} "
                        "(логи, build-каталоги и т.п. — защищено политикой диска)."
                    )
                else:
                    messagebox.showinfo("Результат", "Мусор не найден.")
                return 0

            progress.set_stage("Подсчёт размера")
            total_bytes, manifest = manifest_bytes(
                junk_files, junk_dirs, risky_excluded,
                root_path=target_dir, mode=mode, progress=progress.update,
                cancelled=progress.cancelled)
        except ScanCancelled:
            messagebox.showinfo("DEL JUNK", "Сканирование отменено. Диск не изменён.")
            return 0
        except Exception as exc:  # noqa: BLE001 - top-level UI failure boundary
            messagebox.showerror(
                "DEL JUNK",
                "Сканирование остановлено:\n" +
                _scan_failure_report("scan (%s)" % mode, target_dir,
                                     getattr(progress, "metrics", {}).get("path"),
                                     exc))
            return 2
        finally:
            progress.close()

        # Fail closed: without a durable complete manifest there is no delete.
        try:
            manifest_path = save_manifest(manifest, target_dir)
        except OSError as exc:
            messagebox.showerror(
                "DEL JUNK",
                f"Удаление отменено: не удалось сохранить манифест.\n\n{exc}"
            )
            return 2

        preview = (junk_files + junk_dirs)[:10]
        preview_text = "\n".join(preview)
        if len(junk_files) + len(junk_dirs) > len(preview):
            preview_text += (f"\n... и ещё "
                             f"{len(junk_files) + len(junk_dirs) - len(preview)}")

        policy_note = (
            f"Режим: {mode}.\n"
            "Корзина НЕ используется — удаление необратимо.\n\n"
        )
        if mode == "SAFE_DISK":
            policy_note += (
                "SAFE_DISK: build/dist/target/logs/cache/temp/node_modules "
                "защищены целиком; журналы и дампы сохранены; репозитории, "
                "служебные каталоги и reparse/junction пропущены.\n\n"
            )
            if risky_excluded:
                policy_note += (
                    f"Осторожных кандидатов ПРОПУЩЕНО: {len(risky_excluded)} "
                    "(полный список в манифесте).\n\n"
                )

        confirmed = messagebox.askyesno(
            "Удалить найденный мусор?",
            f"Папка: {target_dir}\n\n"
            f"Файлов: {len(junk_files)}\nПапок: {len(junk_dirs)}\n"
            f"Байт к освобождению: {total_bytes:,}\n\n"
            + policy_note +
            f"Полный манифест: {manifest_path}\n\n"
            f"Первые найденные пути:\n{preview_text}\n\nПродолжить?",
        )
        if not confirmed:
            return 0

        deleted_files, deleted_dirs, errors = delete_junk(
            junk_files, junk_dirs, root_path=target_dir, manifest=manifest)
        log_path = os.path.splitext(manifest_path)[0] + ".log.txt"
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"DEL JUNK — {target_dir} [{mode}]\n")
                f.write(f"Найдено файлов: {len(junk_files)}, папок: {len(junk_dirs)}\n")
                f.write(f"Байт (манифест): {total_bytes}\n")
                f.write(f"Удалено файлов: {len(deleted_files)}, папок: {len(deleted_dirs)}\n\n")
                if deleted_files:
                    f.write("--- Файлы ---\n")
                    f.writelines(path + "\n" for path in deleted_files)
                if deleted_dirs:
                    f.write("--- Папки ---\n")
                    f.writelines(path + "\n" for path in deleted_dirs)
                if risky_excluded:
                    f.write("--- Пропущено политикой (risky excluded) ---\n")
                    f.writelines(path + "\n" for path in risky_excluded)
                if errors:
                    f.write("--- Ошибки / изменившиеся кандидаты ---\n")
                    f.writelines(
                        f"ПРОПУЩЕНО: {path} — {error}\n"
                        for path, error in errors)
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось записать лог:\n{exc}")

        result = (
            f"Удалено файлов: {len(deleted_files)}\n"
            f"Удалено папок: {len(deleted_dirs)}\n"
            f"Запланировано байт: {total_bytes:,}\n"
            f"Манифест: {manifest_path}\nЛог: {log_path}"
        )
        if errors:
            result += f"\nПропущено из-за ошибок/изменений: {len(errors)}"
        messagebox.showinfo("Готово", result)
        return 0
    finally:
        release_single_flight(target_dir)
        try:
            root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
