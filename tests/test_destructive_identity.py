"""Live disposable Windows fixtures for confinement and final file identity."""
import hashlib
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Scripts"))
import verified_file as vf

loader = importlib.machinery.SourceFileLoader(
    "del_same_identity", str(Path(__file__).resolve().parents[1] / "Scripts" / "DEL_SAME.PYW"))
spec = importlib.util.spec_from_loader(loader.name, loader)
same = importlib.util.module_from_spec(spec)
loader.exec_module(same)


@unittest.skipUnless(os.name == "nt", "live Windows identity gate")
class DestructiveIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="saituls-identity-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kept, self.candidate = self.root / "kept", self.root / "candidate"
        self.kept.write_bytes(b"verified duplicate")
        self.candidate.write_bytes(self.kept.read_bytes())
        self.digest = hashlib.sha256(self.kept.read_bytes()).hexdigest()

    def test_verified_identity_deleted_keeper_intact(self):
        self.assertEqual(vf.delete_verified_duplicate(self.candidate, self.kept, self.digest), 18)
        self.assertFalse(self.candidate.exists())
        self.assertEqual(self.kept.read_bytes(), b"verified duplicate")

    def test_replacement_at_disposition_seam_is_not_deleted(self):
        replacement = self.root / "replacement"
        replacement.write_bytes(b"VALUABLE REPLACEMENT")
        api = vf.kernel()
        attempts = []

        class AtDisposition:
            def __getattr__(self, name):
                return getattr(api, name)

            def SetFileInformationByHandle(proxy, *args):
                # This runs AFTER both final digests, immediately before the
                # actual OS deletion call, against actual files and handles.
                with self.assertRaises(OSError) as error:
                    os.replace(replacement, self.candidate)
                attempts.append(error.exception.winerror)
                self.assertIn(error.exception.winerror, (5, 32))
                with self.assertRaises(OSError):
                    os.rename(self.kept, self.root / "stolen-keeper")
                return api.SetFileInformationByHandle(*args)

        with patch.object(vf, "kernel", return_value=AtDisposition()):
            vf.delete_verified_duplicate(self.candidate, self.kept, self.digest)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(replacement.read_bytes(), b"VALUABLE REPLACEMENT")
        self.assertEqual(self.kept.read_bytes(), b"verified duplicate")

    def test_old_hash_close_unlink_control_loses_replacement(self):
        replacement = self.root / "replacement"
        replacement.write_bytes(b"VALUABLE REPLACEMENT")
        self.assertEqual(hashlib.sha256(self.candidate.read_bytes()).hexdigest(), self.digest)
        os.replace(replacement, self.candidate)
        os.remove(self.candidate)
        self.assertFalse(replacement.exists())
        self.assertFalse(self.candidate.exists())

    def test_changed_candidate_refused(self):
        self.candidate.write_bytes(b"changed")
        with self.assertRaisesRegex(OSError, "candidate changed"):
            vf.delete_verified_duplicate(self.candidate, self.kept, self.digest)
        self.assertEqual(self.candidate.read_bytes(), b"changed")

    def test_changed_keeper_refused(self):
        self.kept.write_bytes(b"changed")
        with self.assertRaisesRegex(OSError, "kept copy changed"):
            vf.delete_verified_duplicate(self.candidate, self.kept, self.digest)
        self.assertTrue(self.candidate.exists())

    def test_hardlink_cannot_be_its_own_keeper(self):
        self.candidate.unlink()
        os.link(self.kept, self.candidate)
        with self.assertRaises(OSError):
            vf.delete_verified_duplicate(self.candidate, self.kept, self.digest)
        self.assertTrue(self.candidate.exists())

    def junction(self, link, target):
        def quote(path):
            return "'" + str(path).replace("'", "''") + "'"
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(target)} -ErrorAction Stop | Out-Null"],
                       check=True, capture_output=True)
        self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)

    def test_junction_outside_root_is_reported_and_untouched(self):
        selected, external = self.root / "selected", self.root / "external"
        (selected / "A" / "A").mkdir(parents=True)
        (selected / "A" / "A" / "local").write_bytes(b"local")
        (external / "external").mkdir(parents=True)
        (external / "external" / "valuable").write_bytes(b"outside")
        link = selected / "linked-subdir"
        self.junction(link, external)
        before = {str(p.relative_to(external)): p.read_bytes() for p in external.rglob("*") if p.is_file()}
        skipped = []
        self.assertEqual(same.flatten_duplicate_folders(selected, skipped), 1)
        after = {str(p.relative_to(external)): p.read_bytes() for p in external.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(any(str(link) == p and "reparse" in reason for p, reason in skipped))
        self.assertEqual((selected / "A" / "local").read_bytes(), b"local")

    def test_selected_root_itself_cannot_be_junction(self):
        external = self.root / "external"
        (external / "A" / "A").mkdir(parents=True)
        link = self.root / "selected"
        self.junction(link, external)
        skipped = []
        self.assertEqual(same.flatten_duplicate_folders(link, skipped), 0)
        self.assertTrue(skipped)
        self.assertTrue((external / "A" / "A").is_dir())

    def test_ancestor_replacement_is_denied_while_scope_is_held(self):
        selected = self.root / "selected"
        (selected / "A").mkdir(parents=True)
        with vf.confined_directories(selected, selected / "A"):
            with self.assertRaises(OSError):
                os.rename(selected, self.root / "renamed")
        self.assertTrue(selected.exists())

    def test_scoped_verified_duplicate_inside_scope_deletes(self):
        self.assertEqual(vf.delete_verified_duplicate(self.candidate, self.kept, self.digest, scope=self.root), 18)
        self.assertFalse(self.candidate.exists())
        self.assertEqual(self.kept.read_bytes(), b"verified duplicate")

    def test_scoped_verified_duplicate_candidate_outside_refused(self):
        external = self.root / "external"
        external.mkdir()
        outside_candidate = external / "outside_cand"
        outside_candidate.write_bytes(b"verified duplicate")
        scope = self.root / "subscope"
        scope.mkdir()
        with self.assertRaises(OSError):
            vf.delete_verified_duplicate(outside_candidate, self.kept, self.digest, scope=scope)
        self.assertTrue(outside_candidate.exists())

    def test_scoped_verified_duplicate_keeper_outside_refused(self):
        external = self.root / "external"
        external.mkdir()
        outside_kept = external / "outside_kept"
        outside_kept.write_bytes(b"verified duplicate")
        scope = self.root / "subscope"
        scope.mkdir()
        local_cand = scope / "local_cand"
        local_cand.write_bytes(b"verified duplicate")
        with self.assertRaises(OSError):
            vf.delete_verified_duplicate(local_cand, outside_kept, self.digest, scope=scope)
        self.assertTrue(local_cand.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
