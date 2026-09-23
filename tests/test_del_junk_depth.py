"""DEL JUNK deep-tree / wide-tree regression harness (E-149/E-150).

The real ALL DISKS acceptance failed with "maximum recursion depth exceeded".
Root cause (proven on the shipped CPython 3.11.9): os.walk is a recursive
generator (one Python frame per directory level), shutil.rmtree recurses per
subdirectory, and os.makedirs recurses through ntpath.split. The failure
dimension is DIRECTORY DEPTH, not file count.

This harness proves the REPAIRED implementation is independent of Python
recursion depth, using the strategy the repair ticket prescribes:

  A. every depth case runs the real DEL JUNK code paths in a DEDICATED
     SUBPROCESS with sys.setrecursionlimit deliberately reduced far below
     the fixture depth -- so a recursion regression cannot hide behind the
     interpreter's default 1000-frame budget;
  B. a RED control proves the OLD os.walk/shutil.rmtree primitive actually
     crashes on the SAME fixture/limit (otherwise the oracle proves
     nothing -- E-1251 was previously falsely green);
  C. the matrix covers ordinary trees, junk candidates, protected content at
     extreme depth, nested candidates, reparse boundaries, streaming and
     deep-candidate delete, plus a WIDE tree (thousands of files, shallow
     depth) to distinguish the depth axis from the file-count axis.

No real disk root is ever touched: every fixture lives under a disposable
temp directory. The main process never permanently changes its own
recursion limit.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest.mock as mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
PROBE = os.path.join(HERE, "_del_junk_red_probe.py")
SCRIPTS = os.path.join(ROOT, "Scripts")
sys.path.insert(0, SCRIPTS)

RED_PROBE = os.path.join(HERE, "_del_junk_red_probe.py")
PYW = os.path.join(SCRIPTS, "DEL_JUNK.PYW")

# import the worker the same way the sibling suite does
import importlib.machinery
import importlib.util


def load_worker():
    loader = importlib.machinery.SourceFileLoader("DEL_JUNK_DEPTH", PYW)
    spec = importlib.util.spec_from_loader("DEL_JUNK_DEPTH", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WORKER = load_worker()


def run_probe(depth, limit, which):
    """Run the RED/GREEN subprocess oracle; returns (verdict, output)."""
    proc = subprocess.run(
        [sys.executable, RED_PROBE, str(depth), str(limit), which],
        capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    if "GREEN" in proc.stdout:
        return "GREEN", out
    if "RED_RECURSIONERROR" in proc.stdout:
        return "RED", out
    return "CRASH", out


fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(detail[:400]) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def deep_chain(base, depth, leaf=None):
    """Iterative deep-chain builder (os.makedirs is itself recursive)."""
    path = base
    for _ in range(depth):
        path = os.path.join(path, "d")
        os.mkdir(path)
    if leaf is not None:
        os.mkdir(os.path.join(path, leaf))
    return path


def test_red_control():
    """§26: the OLD primitives must crash on the SAME oracle that the new code
    passes. Without this, the depth oracle proves nothing."""
    verdict, _ = run_probe(200, 60, "walk_control")
    check("RED control: os.walk still crashes at depth 200 / limit 60",
          verdict == "RED", verdict)
    verdict, _ = run_probe(200, 60, "rmtree")
    check("RED control: shutil.rmtree still crashes at depth 200 / limit 60",
          verdict == "RED", verdict)


def test_green_paths():
    """§3-9: every migrated DEL JUNK path is depth-independent."""
    for which, label in (("scan", "scan_junk"),
                         ("boundary", "tree_has_protected_boundary"),
                         ("snapshot", "_tree_snapshot"),
                         ("stream", "stream_drive")):
        verdict, out = run_probe(200, 60, which)
        check("GREEN: %s survives depth 200 / limit 60" % label,
              verdict == "GREEN", out)
    verdict, out = run_probe(400, 60, "scan")
    check("GREEN: scan_junk survives depth 400 / limit 60 (beyond default budget)",
          verdict == "GREEN", out)


def _policy():
    return WORKER.all_disks_policy()


def test_case_a_deep_ordinary_tree():
    """§14 CASE A: deep ordinary tree scans completely, nothing deleted."""
    fixture = tempfile.mkdtemp(prefix="deljunk_depth_caseA_")
    try:
        tail = deep_chain(fixture, 200)
        with open(os.path.join(tail, "file.txt"), "w") as handle:
            handle.write("keep")
        files, dirs, risky = WORKER.scan_junk(fixture, policy=_policy())
        check("CASE A: deep ordinary tree finds zero junk",
              not files and not dirs, repr((files[:3], dirs[:3])))
        check("CASE A: nothing was deleted",
              os.path.exists(os.path.join(tail, "file.txt")))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


def test_case_b_deep_junk_candidate():
    """§14 CASE B + §17: deep GPUCache candidate is inspected, witnessed,
    INTENTed and deleted without RecursionError."""
    fixture = tempfile.mkdtemp(prefix="deljunk_depth_caseB_")
    journal_dir = tempfile.mkdtemp(prefix="deljunk_depth_caseB_j_")
    try:
        cache = os.path.join(fixture, "GPUCache")
        os.mkdir(cache)
        tail = deep_chain(cache, 180)
        with open(os.path.join(tail, "data.bin"), "wb") as handle:
            handle.write(b"x" * 10)
        files, dirs, risky = WORKER.scan_junk(fixture, policy=_policy())
        check("CASE B: deep junk candidate found", dirs == [cache], repr(dirs))
        journal = os.path.join(journal_dir, "caseB.journal.jsonl")
        with mock.patch.object(WORKER, "all_disks_destructive_ready",
                               lambda now=None: (True, [])):
            result = WORKER.stream_drive(fixture + os.sep, _policy(), journal)
        events = []
        if os.path.exists(journal):
            with open(journal, encoding="utf-8") as handle:
                events = [json.loads(line)["event"] for line in handle
                          if line.strip()]
        check("CASE B: streaming deletes the deep candidate",
              result["deleted_dirs"] == 1 and not os.path.exists(cache),
              repr(result))
        check("CASE B: INTENT precedes exactly one DELETED for the candidate",
              events.count("INTENT") == 1 and events.count("DELETED") == 1
              and events.index("INTENT") < events.index("DELETED"),
              repr(events))
        check("CASE B: reclaimed bytes match the witness",
              result["reclaimed_bytes"] == 10, repr(result))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_case_c_protected_at_extreme_depth():
    """§14 CASE C: protected file at the bottom of a junk candidate => whole
    candidate RISKY_EXCLUDED, survives."""
    fixture = tempfile.mkdtemp(prefix="deljunk_depth_caseC_")
    try:
        cache = os.path.join(fixture, "GPUCache")
        os.mkdir(cache)
        tail = deep_chain(cache, 180)
        with open(os.path.join(tail, "important.log"), "w") as handle:
            handle.write("the only copy")
        files, dirs, risky = WORKER.scan_junk(fixture, policy=_policy())
        check("CASE C: protected-at-depth => RISKY_EXCLUDED",
              cache in risky and cache not in dirs, repr((dirs, risky[:3])))
        check("CASE C: the whole candidate survives",
              os.path.exists(os.path.join(tail, "important.log")))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


def test_case_d_junk_below_deep_normal_path():
    """§14 CASE D: __pycache__ buried 150 levels down is still found."""
    fixture = tempfile.mkdtemp(prefix="deljunk_depth_caseD_")
    try:
        tail = deep_chain(fixture, 150)
        pycache = os.path.join(tail, "__pycache__")
        os.mkdir(pycache)
        with open(os.path.join(pycache, "x.pyc"), "wb") as handle:
            handle.write(b"x")
        files, dirs, risky = WORKER.scan_junk(fixture, policy=_policy())
        found = pycache in dirs
        check("CASE D: nested candidate below deep normal path found",
              found, repr((dirs, files)))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


def test_case_e_reparse_below_deep_tree():
    """§14 CASE E: junction under a deep tree is not followed; the external
    sentinel survives a streaming pass."""
    fixture = tempfile.mkdtemp(prefix="deljunk_depth_caseE_")
    journal_dir = tempfile.mkdtemp(prefix="deljunk_depth_caseE_j_")
    try:
        root = os.path.join(fixture, "root")
        os.mkdir(root)
        cache = os.path.join(root, "gpucache")
        os.mkdir(cache)
        deep_chain(cache, 150)
        outside = os.path.join(fixture, "outside")
        os.mkdir(outside)
        sentinel = os.path.join(outside, "sentinel.log")
        with open(sentinel, "w") as handle:
            handle.write("external truth")
        link = os.path.join(cache, "boundary")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside],
                              capture_output=True)
        if made.returncode != 0 or not WORKER.is_reparse_dir(link):
            print("SKIP  CASE E: mklink unavailable in this environment")
            return
        journal = os.path.join(journal_dir, "caseE.journal.jsonl")
        with mock.patch.object(WORKER, "all_disks_destructive_ready",
                               lambda now=None: (True, [])):
            result = WORKER.stream_drive(root, _policy(), journal)
        check("CASE E: boundary not followed, sentinel survives",
              os.path.exists(sentinel) and WORKER.is_reparse_dir(link),
              repr(result))
        check("CASE E: the deep junk candidate with a boundary is excluded",
              os.path.isdir(cache) and result["deleted_dirs"] == 0,
              repr(result))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
        shutil.rmtree(journal_dir, ignore_errors=True)


def test_deep_candidate_delete_via_manifest():
    """§17: witness -> durable INTENT -> revalidate -> iterative delete of a
    deep candidate, through the real delete_junk path."""
    fixture = tempfile.mkdtemp(prefix="deljunk_depth_del_")
    try:
        cache = os.path.join(fixture, "gpucache")
        os.mkdir(cache)
        tail = deep_chain(cache, 170)
        with open(os.path.join(tail, "blob.bin"), "wb") as handle:
            handle.write(b"z" * 32)
        total, digest, ident = WORKER._tree_snapshot(cache)
        manifest = dict(root=fixture, entries=[
            dict(path=cache, type="dir", bytes=total, identity=ident,
                 tree_sha256=digest)])
        deleted_files, deleted_dirs, errors = WORKER.delete_junk(
            [], [cache], root_path=fixture, manifest=manifest)
        check("deep candidate deleted exactly once",
              deleted_dirs == [cache] and not errors, repr((deleted_dirs, errors)))
        check("no external deletion (candidate root gone, fixture intact)",
              not os.path.exists(cache) and os.path.isdir(fixture))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


def test_deep_cancel():
    """§20: cancellation during a deep traversal raises ScanCancelled with no
    recursion unwind dependency."""
    fixture = tempfile.mkdtemp(prefix="deljunk_depth_cancel_")
    try:
        deep_chain(fixture, 150)
        calls = {"n": 0}

        def cancelled():
            calls["n"] += 1
            return calls["n"] > 2

        try:
            WORKER.scan_junk(fixture, policy=_policy(), cancelled=cancelled)
            check("deep cancellation raises ScanCancelled", False)
        except WORKER.ScanCancelled:
            check("deep cancellation raises ScanCancelled", True)
        check("deep tree untouched after cancel",
              os.path.isdir(os.path.join(fixture, "d")))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


def test_wide_tree():
    """§18: file COUNT is a separate axis: 6000 files, shallow depth, no
    pathological stack/memory behaviour; scan completes and classifies."""
    fixture = tempfile.mkdtemp(prefix="deljunk_wide_")
    try:
        for i in range(60):
            sub = os.path.join(fixture, "w%02d" % i)
            os.mkdir(sub)
            for j in range(100):
                if (i + j) % 2:
                    name = "junk%04d.pyc" % j
                else:
                    name = "keep%04d.dat" % j
                with open(os.path.join(sub, name), "wb") as handle:
                    handle.write(b"q")
        files, dirs, risky = WORKER.scan_junk(fixture, policy=_policy())
        junk_count = sum(1 for f in files if f.endswith(".pyc"))
        check("wide tree: scan completes with exact junk count",
              junk_count == 3000, repr((len(files), junk_count)))
        check("wide tree: nothing falsely deleted",
              len(os.listdir(os.path.join(fixture, "w00"))) == 100)
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


def test_gate_depth_clauses():
    """§24: runtime gate proves iterative_depth + iterative_delete_depth."""
    ready, failed = WORKER.all_disks_destructive_ready()
    check("runtime gate green incl. iterative_depth/iterative_delete_depth",
          ready and failed == [], repr(failed))


def main():
    test_red_control()
    test_green_paths()
    test_case_a_deep_ordinary_tree()
    test_case_b_deep_junk_candidate()
    test_case_c_protected_at_extreme_depth()
    test_case_d_junk_below_deep_normal_path()
    test_case_e_reparse_below_deep_tree()
    test_deep_candidate_delete_via_manifest()
    test_deep_cancel()
    test_wide_tree()
    test_gate_depth_clauses()
    print("---")
    print("DEL_JUNK_DEPTH_READY = %s" % ("TRUE" if not fails else "FALSE"))
    print("PASS (0 failures)" if not fails else "FAILED (%d failures)" % len(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
