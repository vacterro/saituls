"""Migration integrity: the guarantees the acceptance criteria name.

Each test here exists because the previous implementation could pass the whole
suite while breaking a promise the UI makes:

  * every regular file is hashed, not a representative sample (AC3);
  * corruption that preserves the byte count is still detected (AC4);
  * a source the application still holds open is never migrated (AC5a);
  * the bytes that survived the rename are the bytes that get mounted (AC5b);
  * a link, junction or mount point cannot pull foreign content into the vault
    (AC6).

The specimens are disposable temp vaults. The operator's real Obsidian vault
is never touched, which is the whole point of building them per test.
"""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Scripts" / "secure_apps"))

import sa_migrate  # noqa: E402
import sa_paths  # noqa: E402

from test_secure_apps import BaseCase  # noqa: E402


class MigrationIntegrityTests(BaseCase):
    def _prepared(self, notes=40):
        """A disposable plaintext vault AT the final mount path."""
        h = self.harness()
        h.enroll()
        source = Path(h.profile.mount_path)
        (source / "main").mkdir(parents=True, exist_ok=True)
        for index in range(notes):
            (source / "main" / ("note-%04d.md" % index)).write_text(
                "# note %d\n%s\n" % (index, "x" * (index * 7 + 1)),
                encoding="utf-8")
        h.broker.open("vault")
        return h, source

    def _migrator(self, h, source, **kwargs):
        kwargs.setdefault("verify_application", False)
        return sa_migrate.Migrator(h.broker, "vault", str(source), **kwargs)

    def _provider(self, h):
        return sa_migrate.secret_provider_from_session(h.broker, "vault")

    # -- AC3: every regular file is hashed ------------------------------
    def test_every_file_is_hashed_never_a_representative_sample(self):
        """More files than the old sampling cap, all of them on the manifest.

        The defect this pins: ``HASH_SAMPLE_MAX`` used to bound how much was
        hashed, so a vault of 450 notes was verified on ~400 of them and the
        migration reported success. Anything past the cap simply was not
        evidence.
        """
        count = sa_migrate.HASH_SAMPLE_MAX + 50
        h, source = self._prepared(notes=count)
        migrator = self._migrator(h, source)
        report = migrator.run(self._provider(h))
        self.assertTrue(report.ok, report.reason)
        self.assertEqual(report.file_count, count)
        self.assertEqual(
            report.hashed, count,
            "the hash pass must cover every regular file, not a sample")

    def test_sampling_has_no_authority_over_migration_or_recovery(self):
        """A forbidden sample helper cannot affect any successful gate."""
        h, source = self._prepared(notes=3)
        migrator = self._migrator(h, source)
        with mock.patch.object(
                sa_migrate, "representative_sample",
                side_effect=AssertionError("sampling entered a verdict path")) as sample:
            report = migrator.run(self._provider(h))
            self.assertTrue(report.ok, report.reason)
            for step in (sa_migrate.STEP_VERIFY_HASHES, sa_migrate.STEP_REVERIFY,
                         sa_migrate.STEP_CUTOVER_VERIFY, sa_migrate.STEP_COMPLETE):
                self.assertIn(step, report.steps)
            recovered = sa_migrate.recover(h.broker, "vault")
            self.assertEqual(recovered["verdict"], "COMPLETE")
            sample.assert_not_called()

    def test_sampling_cap_does_not_reduce_hash_coverage_or_progress(self):
        """Both sides of every hash gate cover all files and emit progress."""
        count = sa_migrate.HASH_SAMPLE_MAX + 50
        h, source = self._prepared(notes=count)
        relative_files = {
            os.path.relpath(str(path), str(source))
            for path in source.rglob("note-*.md")
        }
        total_bytes = sum(path.stat().st_size for path in source.rglob("note-*.md"))
        progress = []
        hashes = {}
        current_phase = [None]
        original_hash = sa_migrate.hash_file

        def record_progress(snapshot):
            current_phase[0] = snapshot["phase"]
            progress.append(snapshot)

        def record_hash(path, *args, **kwargs):
            hashes.setdefault(current_phase[0], set()).add(os.path.normcase(path))
            return original_hash(path, *args, **kwargs)

        migrator = self._migrator(h, source, progress=record_progress)
        with mock.patch.object(sa_migrate, "hash_file", side_effect=record_hash):
            report = migrator.run(self._provider(h))
        self.assertTrue(report.ok, report.reason)

        for phase, roots in (
                (sa_migrate.STEP_VERIFY_HASHES, (str(source), migrator.staging_mount)),
                (sa_migrate.STEP_REVERIFY, (str(source), migrator.staging_mount)),
                (sa_migrate.STEP_CUTOVER_VERIFY,
                 (report.plaintext_archive, migrator.staging_mount))):
            with self.subTest(phase=phase):
                expected = {
                    os.path.normcase(os.path.join(sa_migrate.long_path(root), rel))
                    for root in roots for rel in relative_files
                }
                self.assertEqual(hashes[phase], expected)
                updates = [item for item in progress if item["phase"] == phase]
                self.assertTrue(any(0 < item["files_hashed"] < count for item in updates))
                self.assertTrue(any(item["files_hashed"] == count
                                    and item["bytes_hashed"] == total_bytes
                                    for item in updates))
                self.assertTrue(all(item["files_total"] == count for item in updates))

    def test_same_size_corruption_past_the_old_cap_is_detected(self):
        """Same-size corruption in the ENCRYPTED copy, past the old cap.

        This is the direction that matters: the destination is what the user
        will trust afterwards. A size check passes it, and a sampled hash pass
        would never have looked at file 401 at all -- so this is exactly the
        shape of defect that used to reach a "verified" migration.
        """
        count = sa_migrate.HASH_SAMPLE_MAX + 50
        h, source = self._prepared(notes=count)
        migrator = self._migrator(h, source)
        migrator.precheck(sa_migrate.MigrationReport("vault"))
        h.broker.open("vault")
        migrator.create_destination(sa_migrate.MigrationReport("vault"),
                                    self._provider(h))
        migrator.copy_tree(sa_migrate.MigrationReport("vault"))

        staging = Path(migrator.staging_mount)
        victim = sorted(staging.rglob("note-*.md"))[-1]
        self.assertGreater(victim.stat().st_size, 8)
        # Byte-level edit: a text-mode round trip would translate newlines and
        # change the very thing this test is asserting stays constant.
        raw = victim.read_bytes()
        flipped = b"Q" if raw[4:5] != b"Q" else b"Z"
        victim.write_bytes(raw[:4] + flipped + raw[5:])
        self.assertEqual(victim.stat().st_size, len(raw),
                         "the corruption must preserve the byte count")

        report = sa_migrate.MigrationReport("vault")
        source_files, dest_files = migrator.verify_tree(report)
        migrator.verify_sizes(report, source_files, dest_files)
        self.assertIn(sa_migrate.STEP_VERIFY_SIZES, report.steps)
        with self.assertRaises(sa_migrate.MigrationError) as ctx:
            migrator.verify_hashes(report, source_files, dest_files)
        self.assertIn("content hash differs", str(ctx.exception))
        self.assertIn(str(victim.relative_to(staging)).lower(),
                      str(ctx.exception).lower())
        self.assertNotIn(sa_migrate.STEP_VERIFY_HASHES, report.steps)
        failed = migrator.run(self._provider(h))
        self.assertFalse(failed.ok)
        self.assertIn("content hash differs", failed.reason)
        self.assertNotIn(sa_migrate.MIGRATION_VERIFIED, failed.status)
        self.assertNotIn(sa_migrate.STEP_COMPLETE, failed.steps)
        self.assertTrue(source.is_dir(), "a corrupt copy must not cut over")

    def test_a_symlink_surviving_the_copy_cannot_escape_at_verification(self):
        """A link planted AFTER the copy is refused when the tree is verified.

        The copy already succeeded, so this is the second containment gate: the
        destination is scanned again by the verifier, and a link there is as
        fatal as one in the source. Nothing outside the vault is ever read.
        """
        h, source = self._prepared(notes=3)
        migrator = self._migrator(h, source)
        migrator.precheck(sa_migrate.MigrationReport("vault"))
        h.broker.open("vault")
        migrator.create_destination(sa_migrate.MigrationReport("vault"),
                                    self._provider(h))
        migrator.copy_tree(sa_migrate.MigrationReport("vault"))
        outside = Path(h.tmp) / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "secret.md").write_text("NOT IN THE VAULT", encoding="utf-8")
        planted = Path(migrator.staging_mount) / "main" / "planted"
        try:
            os.symlink(str(outside), str(planted), target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest("this machine cannot create symlinks (%s)" % exc)
        with self.assertRaises(sa_migrate.MigrationError) as ctx:
            sa_migrate.scan_tree(migrator.staging_mount)
        self.assertEqual(ctx.exception.category,
                         sa_migrate.MIGRATION_REPARSE_REFUSED)
        self.assertIn("planted", str(ctx.exception))
        self.assertNotIn(str(outside), str(ctx.exception))

    # -- AC5a: quiescence --------------------------------------------------
    def test_a_running_source_application_stops_the_copy(self):
        """The source is not quiesced -> nothing is copied, nothing is claimed.

        Uses a real process with the profile's executable basename, because
        the gate is about what Windows reports, not about a fake's opinion.
        """
        if os.name != "nt":
            self.skipTest("the quiescence gate is Windows-specific")
        h, source = self._prepared(notes=5)
        exe = h.profile.executable
        self.assertTrue(exe)
        name = os.path.basename(exe)
        decoy = Path(h.tmp) / name
        # A copy of the running interpreter keeps the process alive and gives
        # it exactly the executable name the gate looks for.
        decoy.write_bytes(Path(sys.executable).read_bytes())
        child = subprocess.Popen([str(decoy), "-c", "import time; time.sleep(60)"])
        self.addCleanup(self._kill, child)
        time.sleep(0.5)
        migrator = self._migrator(h, source, source_quiesce_timeout=1.0)
        self.assertIn(child.pid, migrator.running_source_pids(),
                      "a live process with the vault app's name must be seen")
        report = migrator.run(self._provider(h))
        self.assertFalse(report.ok)
        self.assertEqual(report.error_category, "source_busy")
        self.assertIn("will not kill it", report.reason)
        self.assertEqual(child.poll(), None,
                         "the gate must never force-kill the user's application")
        self.assertNotIn(sa_migrate.STEP_COPY, report.steps)
        # And nothing was cut over: the vault is still where it was.
        self.assertTrue(source.is_dir(), "the plaintext must still be in place")
        self.assertFalse(sa_migrate.plaintext_copy_remains(
            migrator.journal.data.get("plaintext_archive")))

    @staticmethod
    def _kill(child):
        try:
            child.kill()
            child.wait(10)
        except Exception:
            pass

    # -- AC6: reparse containment -----------------------------------------
    def test_a_symlink_in_the_source_is_refused(self):
        """A link in the vault must stop the migration, not widen its scope.

        Copying through it would import whatever it points at -- a second
        user's documents, an encrypted container, the whole disk. The refusal
        names the path only; the target is never read.
        """
        outside = Path(self.harness().tmp) / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "secret.md").write_text("NOT IN THE VAULT", encoding="utf-8")
        h, source = self._prepared(notes=3)
        link = source / "main" / "shared"
        try:
            os.symlink(str(outside), str(link), target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest("this machine cannot create symlinks (%s); the same "
                          "check runs against junctions in production" % exc)
        migrator = self._migrator(h, source)
        report = migrator.run(self._provider(h))
        self.assertFalse(report.ok, "a link must stop the migration")
        self.assertEqual(report.error_category, sa_migrate.MIGRATION_REPARSE_REFUSED)
        self.assertIn("shared", report.reason)
        self.assertNotIn("NOT IN THE VAULT", report.reason)
        self.assertNotIn(str(outside), report.reason,
                         "the diagnostic must not echo the link target")
        self.assertTrue(source.is_dir(), "the source must be left untouched")

    def test_a_directory_junction_in_the_source_is_refused(self):
        """Same rule, provable on any Windows box: junctions need no privilege.

        A junction is the more dangerous case anyway -- ``os.walk`` follows one
        without being told to, so a separate "is this a symlink" pre-pass would
        miss it entirely while happily traversing the whole target tree.
        """
        if os.name != "nt":
            self.skipTest("junctions are Windows-specific")
        outside = Path(self.harness().tmp) / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "secret.md").write_text("NOT IN THE VAULT", encoding="utf-8")
        h, source = self._prepared(notes=3)
        junction = source / "main" / "linked"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True, text=True)
        if result.returncode != 0 or not sa_paths.is_reparse_point(str(junction)):
            self.skipTest("could not create a junction here: %s"
                          % (result.stderr or result.stdout).strip())
        migrator = self._migrator(h, source)
        report = migrator.run(self._provider(h))
        self.assertFalse(report.ok, "a junction must stop the migration")
        self.assertEqual(report.error_category, sa_migrate.MIGRATION_REPARSE_REFUSED)
        self.assertIn("linked", report.reason)
        self.assertNotIn("NOT IN THE VAULT", report.reason)
        self.assertNotIn(str(outside), report.reason,
                         "the diagnostic must not echo the link target")
        self.assertTrue(source.is_dir(), "the source must be left untouched")

    def test_a_reparse_scan_costs_no_extra_traversal(self):
        """Containment rides along on the walk the migration already does.

        The link check lives inside ``_scan_root`` and ``copy_tree``, so a
        vault of links costs the same number of traversals as a vault without
        them. A separate pre-pass would have doubled SRC-027 PERF-005.
        """
        h, source = self._prepared(notes=3)
        migrator = self._migrator(h, source)
        calls = []
        original = os.walk

        def counted(top, *args, **kwargs):
            calls.append(str(top))
            return original(top, *args, **kwargs)

        os.walk = counted
        try:
            report = migrator.run(self._provider(h))
        finally:
            os.walk = original
        self.assertTrue(report.ok, report.reason)
        # precheck 1 + copy 1 + verify_tree 2 + reverify 2 + the post-rename
        # proof 2. The post-rename comparison is new work this wave added; the
        # link check is not.
        self.assertEqual(len(calls), 8, "unexpected traversal count: %r" % calls)


if __name__ == "__main__":
    unittest.main()
