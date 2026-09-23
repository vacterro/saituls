"""RED probe: prove the exact failing call path of the CURRENT DEL JUNK
implementation (pre-repair) under a deliberately small recursion limit.

This file is a one-shot forensic harness, not a test suite member. It is run
from tests/test_del_junk_depth.py in a dedicated subprocess so the reduced
recursion limit never leaks into the main test process.
"""

import importlib.machinery
import importlib.util
import os
import sys
import tempfile

FIXTURE_DEPTH = int(sys.argv[1]) if len(sys.argv) > 1 else 200
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 60

HERE = os.path.dirname(os.path.abspath(__file__))
DEL_JUNK = os.path.normpath(os.path.join(HERE, "..", "Scripts", "DEL_JUNK.PYW"))


def load_del_junk():
    # .PYW has no registered loader; bind SourceFileLoader explicitly.
    loader = importlib.machinery.SourceFileLoader("DEL_JUNK_RED", DEL_JUNK)
    spec = importlib.util.spec_from_loader("DEL_JUNK_RED", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_deep_chain(base, depth, leaf_file="f.bin", leaf_dir=None):
    """Iterative deep-chain builder.

    os.makedirs is itself recursive in depth (each path component costs a
    recursion frame in ntpath.split), so the fixture MUST be built with one
    os.mkdir per level -- otherwise the fixture builder, not the code under
    test, is what hits RecursionError first.
    """
    path = base
    for _ in range(depth):
        path = os.path.join(path, "d")
        os.mkdir(path)
    if leaf_dir is not None:
        os.mkdir(os.path.join(path, leaf_dir))
    if leaf_file:
        with open(os.path.join(path, leaf_file), "wb") as handle:
            handle.write(b"x" * 16)
    return path


def main():
    which = sys.argv[3] if len(sys.argv) > 3 else "scan"
    module = load_del_junk()
    base = tempfile.mkdtemp(prefix="deljunk_red_")
    tail = build_deep_chain(base, FIXTURE_DEPTH)
    print("BUILT depth=%d limit=%d" % (FIXTURE_DEPTH, LIMIT))
    print("TAIL_LEN=%d" % len(tail))
    sys.setrecursionlimit(LIMIT)
    try:
        if which == "scan":
            files, dirs, risky = module.scan_junk(base)
            print("SCAN_OK dirs=%d files=%d risky=%d" % (len(dirs), len(files), len(risky)))
        elif which == "boundary":
            found = module.tree_has_protected_boundary(base, module.all_disks_policy())
            print("BOUNDARY_OK=%s" % found)
        elif which == "snapshot":
            total, digest, ident = module._tree_snapshot(base)
            print("SNAPSHOT_OK bytes=%d" % total)
        elif which == "walk_control":
            # The raw primitive the old implementation delegated to.
            count = sum(1 for _ in os.walk(base))
            print("WALK_OK dirs=%d" % count)
        elif which == "stream":
            journal = os.path.join(base, "red.journal.jsonl")
            # stream_drive expects a trailing separator on the root.
            result = module.stream_drive(base + os.sep, module.all_disks_policy(), journal)
            print("STREAM_OK result=%s" % result.get("result"))
        elif which == "rmtree":
            victim = os.path.join(base, "victim")
            os.mkdir(victim)
            build_deep_chain(victim, FIXTURE_DEPTH, leaf_file=None)
            import shutil
            shutil.rmtree(victim)
            print("RMTREE_OK")
        print("GREEN")
    except RecursionError as exc:
        print("RED_RECURSIONERROR %s" % exc)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
