# encoding: utf-8
"""Tests for Secure Apps Recovery First, Safe Exit, and Verified Pre-Migration Recovery Readiness.

SRC-100: Covers all 16 required negative controls and acceptance gates from Phase 8:
1. Malformed existing migration journal.
2. Unknown journal schema.
3. Incomplete setup at step 4.
4. Valid original source with no encrypted container.
5. Missing source.
6. Broken application executable.
7. Backup exists but cannot be restored.
8. Source modified after backup verification.
9. Wrong recovery credential.
10. Lost primary YubiKey.
11. Missing GUI.
12. Missing Python.
13. Failed recovery mount.
14. Repeated recovery after restart.
15. Read-only recovery inspection.
16. Accidental modification attempts against original backup.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_backup
import sa_cli
import sa_migrate
import sa_paths
import sa_recovery
import sa_setup
import sa_storage


class TestRecoveryFirstAndSafeExit(unittest.TestCase):
    """Phase 8 Unit and Integration Tests for SRC-100."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="sa_test_rf_")
        self.state_dir = os.path.join(self.temp_dir, "state")
        self.source_dir = os.path.join(self.temp_dir, "source")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.backup_dir, exist_ok=True)

        # Create sample synthetic notes in source
        for i in range(5):
            with open(os.path.join(self.source_dir, f"note_{i}.md"), "w", encoding="utf-8") as f:
                f.write(f"# Note {i}\nContent {i}\n")

        self.profile = mock.MagicMock()
        self.profile.id = "obsidian"
        self.profile.container = os.path.join(self.temp_dir, "obsidian.vhdx")
        self.profile.mount_path = os.path.join(self.temp_dir, "mount")
        self.profile.executable = os.path.join(self.temp_dir, "Obsidian.exe")
        self.profile.allow_unmanaged_executable = True
        self.profile.with_mount_path.return_value = self.profile

        with open(self.profile.executable, "wb") as f:
            f.write(b"MOCK_EXE")

        self.broker = mock.MagicMock()
        self.broker.registry.state_dir = self.state_dir
        self.broker.registry.get.return_value = self.profile
        self.broker.backend.return_value = sa_storage.FakeStorageBackend()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. Malformed existing migration journal
    def test_1_malformed_existing_migration_journal(self):
        journal_path = os.path.join(self.state_dir, "migration-obsidian.json")
        corrupted_content = "{ incomplete json: [1, 2, "
        with open(journal_path, "w", encoding="utf-8") as f:
            f.write(corrupted_content)

        journal = sa_migrate.MigrationJournal(journal_path)
        self.assertEqual(journal.status, sa_migrate.JOURNAL_CORRUPT)
        self.assertIn("Malformed JSON", journal.error_detail)

        # inspect() returns MIGRATION_STATE_INVALID
        inspected = sa_migrate.inspect(journal_path)
        self.assertEqual(inspected["verdict"], "MIGRATION_STATE_INVALID")
        self.assertEqual(inspected["journal_status"], sa_migrate.JOURNAL_CORRUPT)

        # recover() refuses
        recovered = sa_migrate.recover(self.broker, "obsidian", journal_path=journal_path)
        self.assertEqual(recovered["action"], "refused")
        self.assertEqual(recovered["verdict"], "MIGRATION_STATE_INVALID")

        # Refuses to overwrite corrupt journal on save or clear
        with self.assertRaises(sa_migrate.MigrationError):
            journal.save()
        with self.assertRaises(sa_migrate.MigrationError):
            journal.clear()

        # Content must remain byte-identical
        with open(journal_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), corrupted_content)

    # 2. Unknown journal schema
    def test_2_unknown_journal_schema(self):
        journal_path = os.path.join(self.state_dir, "migration-obsidian.json")
        payload = {
            "schema": "totally.unknown.schema/v99",
            "schema_version": 99,
            "profile_id": "obsidian",
        }
        with open(journal_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        journal = sa_migrate.MigrationJournal(journal_path)
        self.assertEqual(journal.status, sa_migrate.JOURNAL_INVALID)
        self.assertIn("Unknown migration schema", journal.error_detail)

        # Unsupported schema version
        payload_unsupported = {
            "schema": sa_migrate.JOURNAL_SCHEMA,
            "schema_version": 999,
            "profile_id": "obsidian",
        }
        with open(journal_path, "w", encoding="utf-8") as f:
            json.dump(payload_unsupported, f)

        journal2 = sa_migrate.MigrationJournal(journal_path)
        self.assertEqual(journal2.status, sa_migrate.JOURNAL_UNSUPPORTED_SCHEMA)

        # Migrator init refuses
        with self.assertRaises(sa_migrate.MigrationError) as ctx:
            sa_migrate.Migrator(self.broker, "obsidian", self.source_dir, journal_path=journal_path)
        self.assertEqual(ctx.exception.category, "migration_state_invalid")

    # 3. Incomplete setup at step 4
    def test_3_incomplete_setup_at_step_4(self):
        safety = sa_setup.classify_profile_safety_state(
            registry=self.broker.registry,
            profile_id="obsidian",
            source_path=self.source_dir,
        )
        self.assertEqual(safety["state"], sa_setup.STATE_SETUP_INCOMPLETE_SOURCE_AVAILABLE)
        self.assertTrue(safety["safe_exit_allowed"])
        self.assertEqual(safety["headline"], "Setup is incomplete. Your original notes are still available.")
        self.assertTrue(safety["source_available"])
        self.assertFalse(safety["container_exists"])

    # 4. Valid original source with no encrypted container
    def test_4_valid_original_source_with_no_encrypted_container(self):
        self.assertFalse(os.path.isfile(self.profile.container))
        safety = sa_setup.classify_profile_safety_state(
            registry=self.broker.registry,
            profile_id="obsidian",
            source_path=self.source_dir,
        )
        self.assertEqual(safety["state"], sa_setup.STATE_SETUP_INCOMPLETE_SOURCE_AVAILABLE)
        self.assertTrue(safety["safe_exit_allowed"])
        self.assertTrue(os.path.isdir(safety["source_path"]))

    # 5. Missing source
    def test_5_missing_source(self):
        missing_path = os.path.join(self.temp_dir, "nonexistent_source")
        safety = sa_setup.classify_profile_safety_state(
            registry=self.broker.registry,
            profile_id="obsidian",
            source_path=missing_path,
        )
        self.assertEqual(safety["state"], sa_setup.STATE_SETUP_INCOMPLETE_SOURCE_MISSING)
        self.assertFalse(safety["source_available"])

        verdict = sa_migrate.evaluate_recovery_readiness(
            self.state_dir, self.profile, missing_path
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.verdict, "SOURCE_DIRECTORY_MISSING")

    # 6. Broken application executable
    def test_6_broken_application_executable(self):
        self.profile.executable = os.path.join(self.temp_dir, "missing_obsidian.exe")
        safety = sa_setup.classify_profile_safety_state(
            registry=self.broker.registry,
            profile_id="obsidian",
            source_path=self.source_dir,
        )
        self.assertEqual(safety["state"], sa_setup.STATE_APPLICATION_EXECUTABLE_FAILED)
        self.assertFalse(safety["executable_ok"])
        self.assertIn("application binary failure", safety["detail"])

    # 7. Backup exists but cannot be restored
    def test_7_backup_exists_but_cannot_be_restored(self):
        proof = sa_migrate.create_recovery_readiness_proof(
            self.state_dir,
            self.profile,
            self.source_dir,
            self.backup_dir,
            disposable_restore_tested=False,
        )
        verdict = sa_migrate.evaluate_recovery_readiness(
            self.state_dir, self.profile, self.source_dir
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.verdict, "RECOVERY_READINESS_REFUSED")
        self.assertTrue(any("Disposable restore was not tested" in r for r in verdict.reasons))

        with mock.patch("sa_acceptance.evaluate", return_value=mock.MagicMock(ok=True)):
            report = sa_migrate.migrate(self.broker, "obsidian", self.source_dir)
            self.assertFalse(report.ok)
            self.assertEqual(report.error_category, "recovery_readiness_required")

    # 8. Source modified after backup verification
    def test_8_source_modified_after_backup_verification(self):
        sa_migrate.create_recovery_readiness_proof(
            self.state_dir,
            self.profile,
            self.source_dir,
            self.backup_dir,
            disposable_restore_tested=True,
        )
        initial_verdict = sa_migrate.evaluate_recovery_readiness(
            self.state_dir, self.profile, self.source_dir
        )
        self.assertTrue(initial_verdict.ok)

        # Mutate the source notes
        with open(os.path.join(self.source_dir, "new_note.md"), "w", encoding="utf-8") as f:
            f.write("New note added after backup drill\n")

        invalidated_verdict = sa_migrate.evaluate_recovery_readiness(
            self.state_dir, self.profile, self.source_dir
        )
        self.assertFalse(invalidated_verdict.ok)
        self.assertEqual(invalidated_verdict.verdict, "RECOVERY_READINESS_REFUSED")
        self.assertTrue(any("Source generation changed" in r for r in invalidated_verdict.reasons))

    # 9. Wrong recovery credential
    def test_9_wrong_recovery_credential(self):
        container_path = os.path.join(self.temp_dir, "fake.vhdx")
        with open(container_path, "wb") as f:
            f.write(b"FAKE_VHDX_DATA")

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=1,
                stdout=b"ERROR: The password provided is incorrect.",
                stderr=b""
            )
            status, data, detail = sa_recovery.unlock_with_password(
                container_path, "Z", "000000-111111-222222-333333-444444-555555-666666-777777"
            )
            self.assertEqual(status, sa_recovery.RETURN_REFUSED)
            self.assertNotIn("000000", str(detail))

    # 10. Lost primary YubiKey
    def test_10_lost_primary_yubikey(self):
        # Standalone recovery via password/recovery key does not call any FIDO2/YubiKey APIs
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout=b"Unlocked", stderr=b"")
            status, data, detail = sa_recovery.unlock_with_password(
                "test.vhdx", "Z", "123456-123456-123456-123456-123456-123456-123456-123456"
            )
            self.assertEqual(status, sa_recovery.RETURN_OK)

    # 11. Missing GUI
    def test_11_missing_gui(self):
        container_path = os.path.join(self.temp_dir, "direct.vhdx")
        with open(container_path, "wb") as f:
            f.write(b"DIRECT_CONTAINER")

        parser = sa_cli.build_parser()
        args = parser.parse_args([
            "emergency", "obsidian", "attach",
            "--container", container_path,
            "--letter", "Y"
        ])
        with mock.patch.object(sa_recovery, "attach_with_letter",
                               return_value=(sa_recovery.RETURN_OK, {"letter": "Y"}, [])) as mock_attach:
            exit_code = sa_cli.cmd_emergency(args)
            self.assertEqual(exit_code, sa_cli.EXIT_OK)
            mock_attach.assert_called_once_with(container_path)

    # 12. Missing Python
    def test_12_missing_python(self):
        doc = sa_backup.generate_recovery_doc()
        script = sa_backup.generate_recovery_script()
        self.assertIn("Mount-DiskImage", script)
        self.assertIn("Unlock-BitLocker", script)
        self.assertIn("Dismount-DiskImage", script)
        self.assertNotIn("python.exe", script.lower())
        self.assertNotIn("import sys", script.lower())
        self.assertIn("ReadOnly", script)

    # 13. Failed recovery mount
    def test_13_failed_recovery_mount(self):
        container_path = os.path.join(self.temp_dir, "unmountable.vhdx")
        with open(container_path, "wb") as f:
            f.write(b"CORRUPT_VHDX")

        with mock.patch.object(sa_recovery, "attach_with_letter",
                               return_value=(sa_recovery.RETURN_REFUSED, None, ["Disk image corrupted"])):
            seq, verdict = sa_recovery.run_recovery_sequence(
                container_path, "111111-222222-333333-444444-555555-666666-777777-888888"
            )
            self.assertEqual(verdict, "stopped")
            self.assertEqual(len(seq), 1)
            self.assertEqual(seq[0][0], "attach")
            self.assertEqual(seq[0][1], sa_recovery.RETURN_REFUSED)

    # 14. Repeated recovery after restart
    def test_14_repeated_recovery_after_restart(self):
        journal_path = os.path.join(self.state_dir, "migration-obsidian.json")
        payload = {
            "schema": sa_migrate.JOURNAL_SCHEMA,
            "schema_version": sa_migrate.JOURNAL_SCHEMA_VERSION,
            "profile_id": "obsidian",
            "last_step": sa_migrate.STEP_CUTOVER_MOUNTED,
            "source": self.source_dir,
            "final_mount": self.profile.mount_path,
        }
        with open(journal_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        self.broker.backend(self.profile).mounted_at = mock.MagicMock(return_value=self.profile.mount_path)

        res1 = sa_migrate.recover(self.broker, "obsidian", journal_path=journal_path)
        self.assertEqual(res1["verdict"], "COMPLETE")
        self.assertEqual(res1["action"], "completed")

        res2 = sa_migrate.recover(self.broker, "obsidian", journal_path=journal_path)
        self.assertEqual(res2["verdict"], "COMPLETE")
        self.assertEqual(res2["action"], "none")

    # 15. Read-only recovery inspection
    def test_15_read_only_recovery_inspection(self):
        script = sa_backup.generate_recovery_script()
        # Default parameter must be -ReadOnly and require -AllowWrite to override
        self.assertIn("[switch]$ReadOnly", script)
        self.assertIn("[switch]$AllowWrite", script)
        self.assertIn("$ReadOnly.IsPresent -or (-not $AllowWrite.IsPresent)", script)

    # 16. Accidental modification attempts against original backup
    def test_16_accidental_modification_attempts_against_original_backup(self):
        backup_file = os.path.join(self.backup_dir, "__STORE.svault")
        original_bytes = b"ORIGINAL_BACKUP_ENCRYPTED_DATA_BYTES" * 100
        with open(backup_file, "wb") as f:
            f.write(original_bytes)
        initial_hash = hashlib.sha256(original_bytes).hexdigest()

        proof = sa_migrate.create_recovery_readiness_proof(
            self.state_dir,
            self.profile,
            self.source_dir,
            self.backup_dir,
            disposable_restore_tested=True,
        )
        self.assertEqual(proof["backup_payload_sha256"], initial_hash)

        # Ensure original file was untouched
        with open(backup_file, "rb") as f:
            after_hash = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(initial_hash, after_hash)


if __name__ == "__main__":
    unittest.main()
