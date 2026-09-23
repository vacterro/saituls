#!/usr/bin/env python3
"""T-165 deterministic tests for the shared Item List / Item Tree engine.

Covers flat mode, tree mode, ordering, Unicode, spaces, output exclusion,
atomic publication, previous-output preservation, reported permission and
disappearance failures, a real Windows junction cycle, and bounded depth.

Windows junction tests use a disposable temporary tree only and never create a
link outside it.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Scripts"))

import item_tools  # noqa: E402


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


class TempTreeCase(unittest.TestCase):
    """Every test gets a disposable root. Targets and outputs are separate
    subdirectories so a previous run's output never pollutes the next scan."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="saituls-t165-")
        self.target = os.path.join(self.tmp, "target")
        self.outdir = os.path.join(self.tmp, "out")
        os.makedirs(self.target)
        os.makedirs(self.outdir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dir(self, *parts):
        path = os.path.join(self.target, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def file(self, *parts, **kw):
        path = os.path.join(self.target, *parts)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(kw.get("content", ""))
        return path

    def out(self, name):
        return os.path.join(self.outdir, name)

    def run_build(self, mode, target, out, max_depth=item_tools.DEFAULT_MAX_DEPTH):
        return item_tools.build(mode, target, out, max_depth)


class FlatMode(TempTreeCase):
    def test_empty_directory(self):
        out = self.out("out.txt")
        code, _ = self.run_build("flat", self.target, out)
        self.assertEqual(code, 0)
        self.assertEqual(read(out).decode("utf-8"), "")

    def test_one_file(self):
        self.file("only.txt")
        out = self.out("out.txt")
        code, _ = self.run_build("flat", self.target, out)
        self.assertEqual(code, 0)
        self.assertEqual(read(out).decode("utf-8").splitlines(), ["only.txt"])

    def test_directories_first_then_files_sorted(self):
        self.file("zeta.txt")
        self.file("alpha.txt")
        self.file("mid", "x.txt")
        self.file("beta", "y.txt")
        out = self.out("out.txt")
        code, _ = self.run_build("flat", self.target, out)
        self.assertEqual(code, 0)
        lines = read(out).decode("utf-8").splitlines()
        self.assertEqual(lines, ["[DIR] beta", "[DIR] mid", "alpha.txt", "zeta.txt"])

    def test_unicode_and_spaces(self):
        self.file("caf\u00e9 \u2013 t\u00e4ht.txt")
        self.file("file with spaces.txt")
        self.dir("nested dir")
        out = self.out("out.txt")
        code, _ = self.run_build("flat", self.target, out)
        self.assertEqual(code, 0)
        text = read(out).decode("utf-8")
        self.assertIn("[DIR] nested dir", text)
        self.assertIn("caf\u00e9 \u2013 t\u00e4ht.txt", text)
        self.assertIn("file with spaces.txt", text)

    def test_output_inside_target_is_excluded(self):
        self.file("a.txt")
        out = os.path.join(self.target, "item_list.txt")
        code, _ = self.run_build("flat", self.target, out)
        self.assertEqual(code, 0)
        self.assertNotIn("item_list.txt", read(out).decode("utf-8"))

    def test_deterministic_repeat(self):
        for name in ("b.txt", "A.txt", "a.txt", "z"):
            self.file(name)
        self.dir("C")
        self.dir("c")
        out1 = self.out("out1.txt")
        out2 = self.out("out2.txt")
        self.run_build("flat", self.target, out1)
        self.run_build("flat", self.target, out2)
        self.assertEqual(read(out1), read(out2))


class TreeMode(TempTreeCase):
    def test_nested_tree_shape(self):
        self.file("src", "a.txt")
        self.file("src", "sub", "b.txt")
        self.file("top.txt")
        out = self.out("out_tree.txt")
        code, _ = self.run_build("tree", self.target, out)
        self.assertEqual(code, 0)
        lines = read(out).decode("utf-8").splitlines()
        self.assertEqual(lines[0], "[target]")
        joined = "\n".join(lines)
        self.assertIn("[src]", joined)
        self.assertIn("a.txt", joined)
        self.assertIn("[sub]", joined)
        self.assertIn("b.txt", joined)
        self.assertIn("top.txt", joined)

    def test_output_inside_target_excluded(self):
        self.file("a.txt")
        out = os.path.join(self.target, "item_tree.txt")
        code, _ = self.run_build("tree", self.target, out)
        self.assertEqual(code, 0)
        self.assertNotIn("item_tree.txt", read(out).decode("utf-8"))

    def test_bounded_depth_reports_and_does_not_crash(self):
        deep = self.target
        for i in range(8):
            deep = os.path.join(deep, "d%d" % i)
        os.makedirs(deep)
        open(os.path.join(deep, "leaf.txt"), "w").close()
        out = self.out("out.txt")
        code, summary = self.run_build("tree", self.target, out, max_depth=3)
        self.assertEqual(code, 2)
        self.assertIn("ERROR", read(out).decode("utf-8"))
        self.assertIn("PARTIAL", summary)

    def test_ascii_only_tree_runs(self):
        self.file("one", "two.txt")
        out = self.out("out.txt")
        self.run_build("tree", self.target, out)
        text = read(out).decode("utf-8")
        self.assertTrue(all(ord(c) < 128 for c in text), "tree output must be ASCII")


class AtomicPublication(TempTreeCase):
    def test_previous_output_preserved_on_write_failure(self):
        self.file("a.txt")
        blocker = self.out("blocker")
        with open(blocker, "w") as fh:
            fh.write("block")
        out = os.path.join(blocker, "out.txt")
        code, summary = self.run_build("flat", self.target, out)
        self.assertEqual(code, 3)
        self.assertIn("WRITE_FAILURE", summary)
        self.assertEqual(read(blocker), b"block")

    def test_previous_output_preserved_on_listing_failure(self):
        out = self.out("out.txt")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("PREVIOUS")
        missing = os.path.join(self.tmp, "does-not-exist")
        code, summary = self.run_build("flat", missing, out)
        self.assertEqual(code, 1)
        self.assertIn("INVALID_TARGET", summary)
        self.assertEqual(read(out).decode("utf-8"), "PREVIOUS")

    def test_replacement_is_not_a_partial_file(self):
        self.file("a.txt")
        out = self.out("out.txt")
        self.run_build("flat", self.target, out)
        first = read(out)
        self.file("b.txt")
        self.run_build("flat", self.target, out)
        second = read(out)
        self.assertNotEqual(first, second)
        leftovers = [n for n in os.listdir(self.outdir) if n.startswith(item_tools.TEMP_PREFIX)]
        self.assertEqual(leftovers, [])


class ErrorReporting(TempTreeCase):
    def test_permission_error_is_reported(self):
        self.file("secret", "x.txt")
        out = self.out("out.txt")
        real_scandir = os.scandir

        def fake_scandir(path):
            if os.path.basename(path) == "secret":
                raise PermissionError(13, "Access is denied")
            return real_scandir(path)

        os.scandir = fake_scandir
        try:
            code, _ = self.run_build("tree", self.target, out)
        finally:
            os.scandir = real_scandir
        self.assertEqual(code, 2)
        text = read(out).decode("utf-8")
        self.assertIn("[ERROR]", text)
        self.assertIn("PermissionError", text)

    def test_disappearing_child_is_reported(self):
        self.file("ghost", "x.txt")
        out = self.out("out.txt")
        real_scandir = os.scandir

        def fake_scandir(path):
            if os.path.basename(path) == "ghost":
                raise FileNotFoundError(2, "gone")
            return real_scandir(path)

        os.scandir = fake_scandir
        try:
            code, _ = self.run_build("tree", self.target, out)
        finally:
            os.scandir = real_scandir
        self.assertEqual(code, 2)
        self.assertIn("disappeared", read(out).decode("utf-8"))


@unittest.skipUnless(os.name == "nt", "Windows junction test")
class ReparseSafety(TempTreeCase):
    def test_junction_cycle_is_not_descended(self):
        self.file("tree", "leaf.txt")
        sub = self.dir("tree", "sub")
        link = os.path.join(sub, "loop")
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, os.path.join(self.target, "tree")],
            capture_output=True, text=True)
        if created.returncode != 0:
            self.skipTest("mklink unavailable: " + (created.stdout + created.stderr).strip())
        try:
            out = self.out("out.txt")
            code, _ = self.run_build("tree", os.path.join(self.target, "tree"), out)
            self.assertIn(code, (0, 2))
            text = read(out).decode("utf-8")
            self.assertIn("[LINK] loop", text)
            self.assertEqual(text.count("[tree]"), 1)
        finally:
            subprocess.run(["cmd", "/c", "rmdir", link], capture_output=True)


class CliEntry(TempTreeCase):
    def test_cli_returns_zero_and_writes(self):
        self.file("a.txt")
        out = self.out("out.txt")
        engine = os.path.join(ROOT, "Scripts", "item_tools.py")
        result = subprocess.run(
            [sys.executable, engine, "--mode", "flat", "--target", self.target, "--out", out],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)
        self.assertTrue(os.path.exists(out))

    def test_wrapper_requires_explicit_target(self):
        wrapper = os.path.join(ROOT, "Scripts", "saipatch", "_ITEM_LIST.py")
        result = subprocess.run([sys.executable, wrapper], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", (result.stdout + result.stderr).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
