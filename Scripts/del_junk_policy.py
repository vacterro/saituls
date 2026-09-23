"""DEL JUNK policy module: SAFE_DISK vs AGGRESSIVE_PROJECT.

SAFE_DISK is the default whenever the target is a drive root (``X:\\``). It is
built for volumes, where a name like ``build`` or ``logs`` means somebody's
work until proven otherwise. It keeps only what the policy can defend:

* directory names fire ONLY for known regenerable tool caches
  (__pycache__, .pytest_cache, gpucache, ...) -- never generic ``build`` /
  ``dist`` / ``target`` / ``cache`` / ``temp`` / ``logs`` / ``node_modules``;
* file extensions fire only for Python bytecode -- never ``*.log`` /
  ``*.dmp`` / ``*.etl`` / ``*.evtx``;
* partial/temp/download files (.part, .crdownload, .tmp, ...) are junk only
  past an age threshold (default 14 days);
* traversal ALWAYS prunes .git/.svn/.hg (repository history is data),
  $RECYCLE.BIN and System Volume Information, and never follows reparse
  points/junctions outside the selected root.

AGGRESSIVE_PROJECT is the historical behaviour for explicitly selected project
folders: the full name-based rule sets apply, unchanged.

Nothing here deletes; the worker consults these predicates. Self-check:
``python Scripts\\del_junk_policy.py`` -> 0 on success.
"""

import os
import stat
import sys

AGE_THRESHOLD_DAYS_DEFAULT = 14

# Directories never traversed under either policy: repository history is user
# data, and the shell's own metadata folders are not ours to clean.
PRUNE_ALWAYS_DIRS = {
    ".git", ".svn", ".hg", "$recycle.bin", "recycle.bin",
    "system volume information",
}

# SAFE_DISK: directory names that are UNAMBIGUOUSLY regenerable caches. Generic
# names (build/dist/target/cache/temp/tmp/logs/log) and dependency trees
# (node_modules/bower_components) are deliberately absent -- on a volume they
# are somebody's work.
SAFE_DISK_DIR_NAMES = {
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".hypothesis", "gpucache", "shadercache", "grshadercache", "dawncache",
    "graphitedawncache", "thumbnailcache", "webcache", "cache2",
    "startupcache", "code cache", "component_crx_cache",
    "extensions_crx_cache",
}

# SAFE_DISK: file extensions that are junk on ANY volume. Logs, crash dumps and
# trace files (*.log *.dmp *.mdmp *.minidump *.crash *.etl *.wpr *.wpa *.blg
# *.evtx) are NOT here -- a root-level *.log can be the only copy of a crash
# story. Partial/temp/download files are handled by the age threshold instead.
# Keep this deliberately tiny.  In particular, ``.obj`` is also a 3D model,
# Vim swap files can be the only recovery copy of unsaved work, and generic
# ``.cache``/IDE extensions are not proof that bytes are disposable.
SAFE_DISK_EXTENSIONS = {".pyc", ".pyo"}

# SAFE_DISK: extensions junk only when STALE. A month-old .crdownload is
# orphaned transfer litter; one from today may be an active download.
AGE_GATED_EXTENSIONS = {
    ".tmp", ".temp", ".part", ".crdownload", ".download", ".opdownload",
    ".exe.tmp",
}

# Names the aggressive policy would remove solely by directory name.  In
# SAFE_DISK they are opaque protected subtrees: do not inspect descendants and
# do not let an extension rule punch through the protection at a deeper level.
SAFE_DISK_RISKY_DIR_NAMES = {
    "logs", "log", "cache", "caches", "temp", "tmp", "build", "dist",
    "target", "node_modules", "bower_components", "dumps", "minidumps",
    "crashes", "crashpad", "updater", "browsermetrics", "local traces",
}

# Files the historical project policy would remove but SAFE_DISK deliberately
# keeps.  They are surfaced in the manifest as risky exclusions.
SAFE_DISK_RISKY_EXTENSIONS = {
    ".o", ".obj", ".ilk", ".idb", ".pch", ".sdf", ".bsc", ".ncb",
    ".suo", ".swp", ".swo", ".swn", ".cache", ".log", ".dmp", ".mdmp",
    ".minidump", ".crash", ".etl", ".wpr", ".wpa", ".blg", ".evtx",
}


def is_drive_root(target):
    """True when target is a drive, UNC-share, or mounted-volume root."""
    target = os.path.abspath(target)
    drive, tail = os.path.splitdrive(target)
    return bool(drive and tail in ("\\", "/", "")) or os.path.ismount(target)


def select_policy(target):
    """SAFE_DISK for drive roots, AGGRESSIVE_PROJECT for explicit folders."""
    return "SAFE_DISK" if is_drive_root(target) else "AGGRESSIVE_PROJECT"


def is_reparse_point(full_path):
    """True for a symlink, junction, mount point, or other reparse entry."""
    try:
        st = os.lstat(full_path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse_flag)


def is_reparse_dir(full_path):
    """Backward-compatible name used by the worker and older tests."""
    return is_reparse_point(full_path)


def build_policy(aggressive, now, age_days=AGE_THRESHOLD_DAYS_DEFAULT):
    """The predicate bundle scan_junk consults.

    Returns a dict of callables/data:
      dir_junk(name) -> bool            directory name fires?
      file_junk(name, full_path) -> bool  file fires? (age-aware in SAFE_DISK)
      prune_traversal(name) -> bool     never walk into this dir
      risky_excluded(name, full_path) -> bool  candidates SAFE_DISK deliberately
                                        kept, surfaced separately in the UI
    ``now`` is injected for determinism in tests.
    """
    if aggressive:
        # AGGRESSIVE_PROJECT: the historical rule sets, byte-for-byte in spirit.
        from DEL_JUNK_RULES import (
            JUNK_BUILD_OUTPUT_DIRS,
            JUNK_DIR_NAMES,
            JUNK_DIR_PATTERNS,
            JUNK_EXTENSIONS,
        )

        def dir_junk(name):
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

        def file_junk(name, full_path=None):
            name_lower = name.lower()
            ext = os.path.splitext(name_lower)[1]
            base = os.path.splitext(name_lower)[0]
            if ext in JUNK_EXTENSIONS or base in JUNK_EXTENSIONS:
                return True
            return name_lower in {".ds_store", "thumbs.db"}

        def risky_excluded(name, full_path):
            return False

        return {
            "mode": "AGGRESSIVE_PROJECT",
            "dir_junk": dir_junk,
            "dir_risky": lambda name: False,
            "file_junk": file_junk,
            "file_risky": lambda name, full_path: False,
            "risky_excluded": risky_excluded,
            "prune_traversal": lambda name: name.lower() in PRUNE_ALWAYS_DIRS,
        }

    # SAFE_DISK
    def dir_junk(name):
        return name.lower() in SAFE_DISK_DIR_NAMES

    def dir_risky(name):
        return name.lower() in SAFE_DISK_RISKY_DIR_NAMES

    def file_junk(name, full_path):
        name_lower = name.lower()
        ext = os.path.splitext(name_lower)[1]
        base = os.path.splitext(name_lower)[0]
        if ext in SAFE_DISK_EXTENSIONS or base in SAFE_DISK_EXTENSIONS:
            return True
        if name_lower in {".ds_store", "thumbs.db"}:
            return True
        if ext in AGE_GATED_EXTENSIONS:
            age = _age_days(full_path, now)
            return age >= age_days
        return False

    def file_risky(name, full_path):
        name_lower = name.lower()
        ext = os.path.splitext(name_lower)[1]
        base = os.path.splitext(name_lower)[0]
        if ext in SAFE_DISK_RISKY_EXTENSIONS or base in SAFE_DISK_RISKY_EXTENSIONS:
            return True
        if ext in AGE_GATED_EXTENSIONS:
            return not file_junk(name, full_path)
        return False

    def risky_excluded(name, full_path):
        # Compatibility predicate; new code uses the typed dir/file predicates.
        return dir_risky(name) or file_risky(name, full_path)

    return {
        "mode": "SAFE_DISK",
        "dir_junk": dir_junk,
        "dir_risky": dir_risky,
        "file_junk": file_junk,
        "file_risky": file_risky,
        "risky_excluded": risky_excluded,
        "prune_traversal": lambda name: name.lower() in PRUNE_ALWAYS_DIRS,
        "age_days": age_days,
    }


def _age_days(full_path, now):
    try:
        return max(0.0, (now - os.path.getmtime(full_path)) / 86400.0)
    except OSError:
        return 0.0


if __name__ == "__main__":
    import time

    now = time.time()
    ok = True

    def check(name, cond):
        global ok
        print(("PASS  " if cond else "FAIL  ") + name)
        ok = ok and cond

    check("drive root selects SAFE_DISK", select_policy("G:\\") == "SAFE_DISK")
    check("folder selects AGGRESSIVE_PROJECT", select_policy("V:\\a\\b") == "AGGRESSIVE_PROJECT")
    p = build_policy(False, now)
    check("SAFE_DISK keeps build", not p["dir_junk"]("build"))
    check("SAFE_DISK keeps dist", not p["dir_junk"]("dist"))
    check("SAFE_DISK keeps target", not p["dir_junk"]("target"))
    check("SAFE_DISK keeps node_modules", not p["dir_junk"]("node_modules"))
    check("SAFE_DISK keeps generic logs dir", not p["dir_junk"]("logs"))
    check("SAFE_DISK protects the entire build subtree", p["dir_risky"]("build"))
    check("SAFE_DISK detects gpucache", p["dir_junk"]("gpucache"))
    check("SAFE_DISK detects __pycache__", p["dir_junk"]("__pycache__"))
    check("SAFE_DISK keeps a today .log file", not p["file_junk"]("run.log", None))
    check("SAFE_DISK keeps a .dmp file", not p["file_junk"]("crash.dmp", None))
    check("SAFE_DISK keeps a .evtx file", not p["file_junk"]("sys.evtx", None))
    check("SAFE_DISK keeps a 3D .obj file", not p["file_junk"]("model.obj", None))
    fresh = os.path.join(os.environ.get("TEMP", "."), "saipolicy_fresh.tmp")
    open(fresh, "w").close()
    try:
        check("a fresh .tmp is kept (age gate)", not p["file_junk"]("saipolicy_fresh.tmp", fresh))
    finally:
        os.remove(fresh)
    check(".git is pruned from traversal", p["prune_traversal"](".git"))
    check("$RECYCLE.BIN is pruned", p["prune_traversal"]("$RECYCLE.BIN"))
    a = build_policy(True, now)
    check("AGGRESSIVE still fires node_modules", a["dir_junk"]("node_modules"))
    check("AGGRESSIVE still fires build", a["dir_junk"]("build"))
    check("AGGRESSIVE still fires *.log", a["file_junk"]("run.log", None))

    print('---')
    print('PASS (0 failures)' if ok else 'FAILED')
    sys.exit(0 if ok else 1)
