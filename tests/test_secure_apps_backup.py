# encoding: utf-8
"""Tests for Secure Apps Encrypted Ciphertext Backups (__STORE.svault)."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_backup
import sa_storage
import sa_crypto


class TestSecureAppsBackup(unittest.TestCase):
    """Test backup publishing, atomic staging, generation rotation, and verification."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="sa_test_backup_")
        self.source_dir = os.path.join(self.temp_dir, "source")
        self.dest_dir = os.path.join(self.temp_dir, "dest")
        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.dest_dir, exist_ok=True)

        self.vault_file = os.path.join(self.source_dir, "__STORE.vhdx")
        with open(self.vault_file, "wb") as f:
            f.write(b"MOCK_BITLOCKER_VHDX_DATA_" * 1024)  # ~25 KB

        self.profile = MagicMock()
        self.profile.id = "obsidian"
        self.profile.container_id = "test-container-id"
        self.profile.container = self.vault_file
        self.profile.backup_destination = self.dest_dir
        self.profile.credential_profile = "primary"
        self.profile.storage_layout_version = 2

        self.backend = sa_storage.FakeStorageBackend()
        self.key = self.backend._key(self.profile)
        self.backend._exists[self.key] = True
        self.backend._state[self.key] = sa_storage.DETACHED
        self.backend._recovery_passwords[self.key] = "000000-111111-222222-333333-444444-555555-666666-777777"

        import sa_layout
        sa_layout.save_layout(
            self.profile,
            daily_id="{fake-daily-id}",
            daily_type="RecoveryPassword",
            break_glass_id="{fake-rec-id}",
            break_glass_type="RecoveryPassword"
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_publish_backup_success(self):
        hmac_key = b"12345678901234567890123456789012"
        res = sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir,
            hmac_key=hmac_key
        )
        self.assertTrue(res.get("ok"))
        target_vault = os.path.join(self.dest_dir, "__STORE.svault")
        meta_file = os.path.join(self.dest_dir, "__STORE.svault.meta")
        doc_file = os.path.join(self.dest_dir, "__STORE-RECOVERY.txt")
        script_file = os.path.join(self.dest_dir, "__STORE-RECOVER.ps1")

        self.assertTrue(os.path.isfile(target_vault))
        self.assertTrue(os.path.isfile(meta_file))
        self.assertTrue(os.path.isfile(doc_file))
        self.assertTrue(os.path.isfile(script_file))

        # Check metadata content and HMAC
        with open(meta_file, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["profile_id"], "obsidian")
        self.assertIn("manifest_hmac", manifest)

        canonical_b = json.dumps({k: v for k, v in manifest.items() if k != "manifest_hmac"},
                                 sort_keys=True, separators=(',', ':')).encode("utf-8")
        self.assertTrue(sa_crypto.verify_manifest_hmac(hmac_key, canonical_b, manifest["manifest_hmac"]))

        # Verify backup
        v_res = sa_backup.verify_backup(
            destination_dir=self.dest_dir,
            hmac_key=hmac_key
        )
        self.assertTrue(v_res.get("ok"), v_res.get("error"))

    def test_publish_backup_generation_rotation(self):
        # Generation 1
        res1 = sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir
        )
        self.assertTrue(res1.get("ok"))
        gen1_sha = res1.get("sha256")

        # Modify source file
        with open(self.vault_file, "wb") as f:
            f.write(b"NEW_MOCK_BITLOCKER_VHDX_DATA_" * 1024)

        # Generation 2
        res2 = sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir
        )
        self.assertTrue(res2.get("ok"))
        gen2_sha = res2.get("sha256")
        self.assertNotEqual(gen1_sha, gen2_sha)

        previous_vault = os.path.join(self.dest_dir, "__STORE.previous.svault")
        current_vault = os.path.join(self.dest_dir, "__STORE.svault")
        self.assertTrue(os.path.isfile(previous_vault))
        self.assertTrue(os.path.isfile(current_vault))

        self.assertEqual(sa_backup.sha256_file(previous_vault), gen1_sha)
        self.assertEqual(sa_backup.sha256_file(current_vault), gen2_sha)

    def test_publish_refused_when_volume_mounted(self):
        self.backend._state[self.key] = sa_storage.MOUNTED
        with self.assertRaises(sa_backup.BackupError) as ctx:
            sa_backup.publish_backup(
                self.profile,
                self.backend,
                destination_dir=self.dest_dir
            )
        self.assertIn("mounted", str(ctx.exception).lower())

    def test_verify_backup_detects_corruption(self):
        hmac_key = b"12345678901234567890123456789012"
        sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir,
            hmac_key=hmac_key
        )
        target_vault = os.path.join(self.dest_dir, "__STORE.svault")
        # Corrupt vault file
        with open(target_vault, "ab") as f:
            f.write(b"CORRUPTION_BYTES")

        v_res = sa_backup.verify_backup(
            destination_dir=self.dest_dir,
            hmac_key=hmac_key
        )
        self.assertFalse(v_res.get("ok"))
        self.assertIn("mismatch", v_res.get("error").lower())

    def test_verify_backup_detects_metadata_tampering(self):
        hmac_key = b"12345678901234567890123456789012"
        sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir,
            hmac_key=hmac_key
        )
        meta_file = os.path.join(self.dest_dir, "__STORE.svault.meta")
        with open(meta_file, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["vhdx_size_bytes"] = 999999
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        v_res = sa_backup.verify_backup(
            destination_dir=self.dest_dir,
            hmac_key=hmac_key
        )
        self.assertFalse(v_res.get("ok"))
        self.assertIn("hmac", v_res.get("error").lower())
        self.assertEqual(v_res.get("sidecar_authenticated"), "HMAC_MISMATCH")

    def test_truthful_metadata_authentication_distinguishes_payload_and_sidecar(self):
        """P1-2: Explicitly report payload SHA-256 vs sidecar authenticity."""
        hmac_key = b"12345678901234567890123456789012"
        sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir,
            hmac_key=hmac_key
        )
        # 1. Verify without HMAC key -> Payload verified, sidecar unverified
        res_no_key = sa_backup.verify_backup(destination_dir=self.dest_dir, hmac_key=None)
        self.assertTrue(res_no_key.get("ok"))
        self.assertTrue(res_no_key.get("payload_sha256_verified"))
        self.assertEqual(res_no_key.get("sidecar_authenticated"), "UNVERIFIED")

        # 2. Verify with valid HMAC key -> Payload verified, sidecar verified
        res_valid = sa_backup.verify_backup(destination_dir=self.dest_dir, hmac_key=hmac_key)
        self.assertTrue(res_valid.get("ok"))
        self.assertTrue(res_valid.get("payload_sha256_verified"))
        self.assertEqual(res_valid.get("sidecar_authenticated"), "VERIFIED")

    def test_real_protector_discovery_no_hardcoded_booleans(self):
        """P1-3: Protectors in manifest must reflect actual BitLocker layout."""
        # FakeStorageBackend without portable password
        sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir
        )
        meta_file = os.path.join(self.dest_dir, "__STORE.svault.meta")
        with open(meta_file, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        # Since self.backend has no password protector registered for key:
        self.assertFalse(manifest["protectors"]["portable_password"])
        self.assertTrue(manifest["protectors"]["recovery_key"])

        # Now simulate having a password protector:
        self.backend._passwords[self.key] = "test-portable-pass"
        import sa_layout
        sa_layout.update_portable_protector(self.profile, "{fake-pw-id}", "Password")
        sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir
        )
        with open(meta_file, "r", encoding="utf-8") as f:
            manifest2 = json.load(f)
        self.assertTrue(manifest2["protectors"]["portable_password"])

    def test_dual_verified_generations_both_have_meta_and_verify(self):
        """P1-4: Both current and previous generations must have matching metadata and verify."""
        # Generation 1
        res1 = sa_backup.publish_backup(self.profile, self.backend, destination_dir=self.dest_dir)
        self.assertTrue(res1.get("ok"))
        gen1_sha = res1.get("sha256")

        # Generation 2
        with open(self.vault_file, "wb") as f:
            f.write(b"GENERATION_TWO_DATA_" * 1024)
        res2 = sa_backup.publish_backup(self.profile, self.backend, destination_dir=self.dest_dir)
        self.assertTrue(res2.get("ok"))
        gen2_sha = res2.get("sha256")

        prev_vault = os.path.join(self.dest_dir, "__STORE.previous.svault")
        prev_meta = os.path.join(self.dest_dir, "__STORE.previous.svault.meta")
        curr_vault = os.path.join(self.dest_dir, "__STORE.svault")
        curr_meta = os.path.join(self.dest_dir, "__STORE.svault.meta")

        self.assertTrue(os.path.isfile(prev_vault))
        self.assertTrue(os.path.isfile(prev_meta))
        self.assertTrue(os.path.isfile(curr_vault))
        self.assertTrue(os.path.isfile(curr_meta))

        # Check hashes
        with open(prev_meta, "r", encoding="utf-8") as f:
            prev_man = json.load(f)
        with open(curr_meta, "r", encoding="utf-8") as f:
            curr_man = json.load(f)

        self.assertEqual(prev_man["vhdx_sha256"], gen1_sha)
        self.assertEqual(curr_man["vhdx_sha256"], gen2_sha)

        # verify_all_generations
        all_res = sa_backup.verify_all_generations(destination_dir=self.dest_dir)
        self.assertTrue(all_res.get("ok"))
        self.assertTrue(all_res["current"].get("ok"))
        self.assertTrue(all_res["previous"].get("ok"))

    def test_portable_enrollments_export_and_ephemeral_store(self):
        """P1-1: Enrollment envelopes exported to sidecar, unwrap via EphemeralCredentialStore."""
        import sa_auth
        cred_path = os.path.join(self.temp_dir, "test_credentials.json")
        cred_store = sa_auth.CredentialStore(cred_path)
        # Create a mock enrollment envelope
        fake_enrollment = sa_auth.Enrollment(
            credential_profile="primary",
            provider="fake-auth",
            credential_id=b"test-cred-id",
            rp_id="saituls.secure-apps.local",
            hmac_salt=b"salt" * 8,
            kdf_salt=b"kdfs" * 8,
            nonce=b"nonce" * 3,
            ciphertext=b"ciphertext_test",
            aad_schema_version=1,
            created="2026-10-05T00:00:00Z"
        )
        cred_store.add(self.profile.id, self.profile.container_id, fake_enrollment)

        sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir,
            cred_store=cred_store
        )

        meta_file = os.path.join(self.dest_dir, "__STORE.svault.meta")
        with open(meta_file, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertIn("portable_enrollments", manifest)
        self.assertEqual(len(manifest["portable_enrollments"]), 1)
        self.assertEqual(manifest["portable_enrollments"][0]["credential_profile"], "primary")
        self.assertTrue(manifest["protectors"]["yubikey_fido2"])

        # Test EphemeralCredentialStore reconstruction
        ephemeral = sa_backup.EphemeralCredentialStore(
            self.profile.id, self.profile.container_id, manifest["portable_enrollments"]
        )
        self.assertTrue(ephemeral.is_enrolled(self.profile.id))
        reconstructed = ephemeral.enrollments(self.profile.id)
        self.assertEqual(len(reconstructed), 1)
        self.assertEqual(reconstructed[0].credential_id, b"test-cred-id")

    def test_generated_recovery_script_hardening_rules(self):
        """P1-6: Validate standalone script adheres to all hardening rules."""
        script_content = sa_backup.generate_recovery_script("__STORE.svault")

        # Rule A: Default VaultPath uses $PSScriptRoot
        self.assertIn("Join-Path $PSScriptRoot", script_content)

        # Rule B: Elevation preserves VaultPath
        self.assertIn('-VaultPath `"$fullPath`"', script_content)

        # Rule C: Fallback when hardlink fails
        self.assertIn("Hard link unavailable", script_content)

        # Rule D: No undefined Resolve-VolumePath
        self.assertNotIn("Resolve-VolumePath", script_content)

        # Rule E: Wrap mount/unlock in try/finally
        self.assertIn("try {", script_content)
        self.assertIn("finally {", script_content)
        self.assertIn("Dismount-DiskImage", script_content)

        # Rule F: Re-verify LockStatus after unlock
        self.assertIn("Failed to unlock volume. Lock status remains:", script_content)

    def test_r1_real_wrap_export_and_ephemeral_unwrap(self):
        """R1: Test real cryptographic wrap -> publish sidecar -> ephemeral unwrap preserves profile identity."""
        import sa_auth
        provider = sa_auth.FakeAuthProvider()
        cred_path = os.path.join(self.temp_dir, "r1_creds.json")
        cred_store = sa_auth.CredentialStore(cred_path)

        secret = sa_crypto.new_volume_secret()
        self.backend._secret[self.key] = secret.bytes()

        cred = provider.create_credential(self.profile.credential_profile)
        enrollment = sa_auth.wrap_volume_secret_for(
            self.profile, provider, cred, secret, container_id=self.profile.container_id
        )
        cred_store.add(self.profile.id, self.profile.container_id, enrollment)

        hmac_key = sa_crypto.derive_manifest_hmac_key(secret, self.profile.container_id)
        pub_res = sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir,
            hmac_key=hmac_key,
            cred_store=cred_store
        )
        self.assertTrue(pub_res.get("ok"))
        target_vault = pub_res["vault_path"]
        target_meta = pub_res["meta_path"]

        # Open portable vault with YubiKey method
        mount_dir = os.path.join(self.temp_dir, "r1_mount")
        mount_res = sa_backup.open_portable_vault(
            self.backend,
            target_vault,
            mount_dir,
            method="yubikey",
            provider=provider,
            meta_path=target_meta
        )
        self.assertEqual(mount_res.get("state"), sa_storage.MOUNTED)
        self.assertEqual(mount_res.get("sidecar_authenticated"), "VERIFIED")

    def test_r1_profile_id_mutation_causes_cryptographic_unwrap_failure(self):
        """R1: Tampering with profile_id in metadata causes authenticated AES-GCM unwrap to fail."""
        import sa_auth
        provider = sa_auth.FakeAuthProvider()
        cred_path = os.path.join(self.temp_dir, "r1_tamper_creds.json")
        cred_store = sa_auth.CredentialStore(cred_path)

        secret = sa_crypto.new_volume_secret()
        self.backend._secret[self.key] = secret.bytes()

        cred = provider.create_credential(self.profile.credential_profile)
        enrollment = sa_auth.wrap_volume_secret_for(
            self.profile, provider, cred, secret, container_id=self.profile.container_id
        )
        cred_store.add(self.profile.id, self.profile.container_id, enrollment)

        pub_res = sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir,
            cred_store=cred_store
        )
        target_meta = pub_res["meta_path"]

        with open(target_meta, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["profile_id"] = "malicious_profile"
        with open(target_meta, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        mount_dir = os.path.join(self.temp_dir, "r1_mount_fail")
        with self.assertRaises((sa_crypto.UnwrapError, sa_auth.AuthError)):
            sa_backup.open_portable_vault(
                self.backend,
                pub_res["vault_path"],
                mount_dir,
                method="yubikey",
                provider=provider,
                meta_path=target_meta,
                allow_unverified=True
            )

    def test_r1_strict_validation_refuses_missing_metadata(self):
        """R1: Portable YubiKey open refuses metadata lacking required identity fields."""
        incomplete_meta = os.path.join(self.dest_dir, "incomplete.meta")
        with open(incomplete_meta, "w", encoding="utf-8") as f:
            json.dump({"container_id": "test"}, f)

        mount_dir = os.path.join(self.temp_dir, "r1_incomplete_mount")
        with self.assertRaises(sa_backup.BackupError) as ctx:
            sa_backup.open_portable_vault(
                self.backend,
                self.vault_file,
                mount_dir,
                method="yubikey",
                meta_path=incomplete_meta
            )
        self.assertEqual(ctx.exception.category, "metadata_missing")

    def test_r10_sidecar_authenticity_verification_post_unwrap(self):
        """R10: Verify sidecar HMAC post-unwrap; detect tampering or require explicit acknowledgement."""
        import sa_auth
        provider = sa_auth.FakeAuthProvider()
        cred_path = os.path.join(self.temp_dir, "r10_creds.json")
        cred_store = sa_auth.CredentialStore(cred_path)

        secret = sa_crypto.new_volume_secret()
        self.backend._secret[self.key] = secret.bytes()

        cred = provider.create_credential(self.profile.credential_profile)
        enrollment = sa_auth.wrap_volume_secret_for(
            self.profile, provider, cred, secret, container_id=self.profile.container_id
        )
        cred_store.add(self.profile.id, self.profile.container_id, enrollment)

        # 1. Tampered HMAC
        hmac_key = sa_crypto.derive_manifest_hmac_key(secret, self.profile.container_id)
        pub_res = sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir,
            hmac_key=hmac_key,
            cred_store=cred_store
        )
        target_meta = pub_res["meta_path"]
        with open(target_meta, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["manifest_hmac"] = "bad0" * 16
        with open(target_meta, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        mount_dir = os.path.join(self.temp_dir, "r10_mount")
        with self.assertRaises(sa_backup.BackupError) as ctx:
            sa_backup.open_portable_vault(
                self.backend,
                pub_res["vault_path"],
                mount_dir,
                method="yubikey",
                provider=provider,
                meta_path=target_meta
            )
        self.assertEqual(ctx.exception.category, "HMAC_MISMATCH")

        # 2. Missing HMAC without acknowledgement refuses mount
        pub_res_no_hmac = sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir,
            hmac_key=None,
            cred_store=cred_store
        )
        with self.assertRaises(sa_backup.BackupError) as ctx:
            sa_backup.open_portable_vault(
                self.backend,
                pub_res_no_hmac["vault_path"],
                mount_dir,
                method="yubikey",
                provider=provider,
                meta_path=pub_res_no_hmac["meta_path"]
            )
        self.assertEqual(ctx.exception.category, "SIDECAR_AUTHENTICITY_UNVERIFIED")

        # 3. Missing HMAC with explicit acknowledgement succeeds
        res_ack = sa_backup.open_portable_vault(
            self.backend,
            pub_res_no_hmac["vault_path"],
            mount_dir,
            method="yubikey",
            provider=provider,
            meta_path=pub_res_no_hmac["meta_path"],
            acknowledge_unverified=True
        )
        self.assertEqual(res_ack.get("sidecar_authenticated"), "SIDECAR_AUTHENTICITY_UNVERIFIED")

    def test_r11_protector_layout_drift_refuses_publish(self):
        """R11: Disagreement between persisted layout record and native BitLocker refuses publication."""
        import sa_layout
        # Create authoritative layout with specific daily and break-glass IDs
        sa_layout.save_layout(
            self.profile,
            daily_id="{expected-daily-id}",
            daily_type="RecoveryPassword",
            break_glass_id="{expected-bg-id}",
            break_glass_type="RecoveryPassword"
        )

        # Backend list_protectors returns different IDs (simulating out-of-band mutation)
        with self.assertRaises(sa_backup.BackupError) as ctx:
            sa_backup.publish_backup(
                self.profile,
                self.backend,
                destination_dir=self.dest_dir
            )
        self.assertEqual(ctx.exception.category, "PROTECTOR_LAYOUT_DRIFT")

    def test_r5_immutable_generation_publication_and_crash_consistency(self):
        """R5: Generation commit uses immutable directories and atomic pointer updates."""
        # 1. Publish generation 1
        res1 = sa_backup.publish_backup(self.profile, self.backend, destination_dir=self.dest_dir)
        self.assertTrue(res1.get("ok"))
        gen1_id = res1.get("generation_id")
        self.assertIsNotNone(gen1_id)

        # Verify generations directory and CURRENT pointer
        gen1_dir = os.path.join(self.dest_dir, "generations", gen1_id)
        self.assertTrue(os.path.isdir(gen1_dir))
        self.assertTrue(os.path.isfile(os.path.join(gen1_dir, "vault.svault")))
        self.assertTrue(os.path.isfile(os.path.join(gen1_dir, "vault.svault.meta")))

        current_ptr = os.path.join(self.dest_dir, "CURRENT")
        self.assertTrue(os.path.isfile(current_ptr))
        with open(current_ptr, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), gen1_id)

        # 2. Publish generation 2
        with open(self.vault_file, "wb") as f:
            f.write(b"NEW_DATA_FOR_GENERATION_2" * 1024)

        res2 = sa_backup.publish_backup(self.profile, self.backend, destination_dir=self.dest_dir)
        self.assertTrue(res2.get("ok"))
        gen2_id = res2.get("generation_id")
        self.assertNotEqual(gen1_id, gen2_id)

        # Verify CURRENT points to gen2, PREVIOUS points to gen1
        with open(current_ptr, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), gen2_id)

        previous_ptr = os.path.join(self.dest_dir, "PREVIOUS")
        self.assertTrue(os.path.isfile(previous_ptr))
        with open(previous_ptr, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), gen1_id)

    def test_v2_backup_without_layout_raises_protector_layout_unknown(self):
        """P0-F: On V2 vaults, missing layout must fail closed with PROTECTOR_LAYOUT_UNKNOWN."""
        import sa_layout
        layout_file = sa_layout.layout_path_for_profile(self.profile)
        if os.path.exists(layout_file):
            os.remove(layout_file)
        self.profile.storage_layout_version = 2
        with self.assertRaises(sa_backup.BackupError) as ctx:
            sa_backup.publish_backup(self.profile, self.backend, destination_dir=self.dest_dir)
        self.assertEqual(ctx.exception.category, "PROTECTOR_LAYOUT_UNKNOWN")

    def test_p1_e_crash_fault_injection_at_filesystem_seams(self):
        """P1-E: Fault injection across critical filesystem seams in publish_backup.

        Invariants:
        1. Fully committed generation remains recoverable after any crash/failure.
        2. CURRENT pointer never identifies an incomplete generation.
        3. Every committed generation directory contains a matching vault.svault + vault.svault.meta pair.
        """
        # Publish Gen 1 to have an established recoverable base
        res1 = sa_backup.publish_backup(self.profile, self.backend, destination_dir=self.dest_dir)
        self.assertTrue(res1.get("ok"))
        gen1_id = res1.get("generation_id")

        # Mutate source for subsequent generation attempts
        with open(self.vault_file, "wb") as f:
            f.write(b"NEW_PAYLOAD_FOR_FAULT_INJECTION_GEN2" * 1024)

        # Seam 1: Fail during payload copy
        orig_copy = sa_backup.copy_file_chunked
        def fail_copy(*args, **kwargs):
            raise IOError("Injected I/O crash during copy_file_chunked")
        sa_backup.copy_file_chunked = fail_copy
        try:
            with self.assertRaises(IOError):
                sa_backup.publish_backup(self.profile, self.backend, destination_dir=self.dest_dir)
            sa_backup.reconcile_generations_and_flat_files(self.dest_dir)
            self._assert_backup_recovery_invariants(gen1_id)
        finally:
            sa_backup.copy_file_chunked = orig_copy

        # Seam 2: Fail during staged hash verification
        orig_sha256 = sa_backup.sha256_file
        def fail_staged_verify(path):
            if "staging" in str(path) or "gen_" in str(path):
                return "0000000000000000000000000000000000000000000000000000000000000000"
            return orig_sha256(path)
        sa_backup.sha256_file = fail_staged_verify
        try:
            with self.assertRaises(sa_backup.BackupError) as ctx:
                sa_backup.publish_backup(self.profile, self.backend, destination_dir=self.dest_dir)
            self.assertEqual(ctx.exception.category, "staged_verify_failed")
            sa_backup.reconcile_generations_and_flat_files(self.dest_dir)
            self._assert_backup_recovery_invariants(gen1_id)
        finally:
            sa_backup.sha256_file = orig_sha256

        # Seams 3-11: Fault injection across os.replace seams
        replace_scenarios = [
            ("gen_", "generation dir os.replace"),
            ("CURRENT", "CURRENT pointer os.replace"),
            ("PREVIOUS", "PREVIOUS pointer os.replace"),
            ("__STORE.svault", "flat vault rotation or publish"),
            ("__STORE.svault.meta", "flat meta rotation or publish"),
            ("RECOVERY.txt", "recovery doc os.replace"),
            ("RECOVER.ps1", "recovery script os.replace"),
        ]

        for token, desc in replace_scenarios:
            orig_replace = os.replace
            def make_fail_replace(match_token):
                def failing_replace(src, dst):
                    if match_token in str(src) or match_token in str(dst):
                        raise OSError(f"Injected crash at seam: {desc} ({src} -> {dst})")
                    return orig_replace(src, dst)
                return failing_replace

            os.replace = make_fail_replace(token)
            try:
                try:
                    sa_backup.publish_backup(self.profile, self.backend, destination_dir=self.dest_dir)
                except OSError:
                    pass
            finally:
                os.replace = orig_replace

            sa_backup.reconcile_generations_and_flat_files(self.dest_dir)
            self._assert_backup_recovery_invariants(gen1_id)

    def _assert_backup_recovery_invariants(self, fallback_gen_id):
        current_ptr = os.path.join(self.dest_dir, "CURRENT")
        self.assertTrue(os.path.isfile(current_ptr), "CURRENT pointer file missing")
        with open(current_ptr, "r", encoding="utf-8") as f:
            active_gen = f.read().strip()
        self.assertTrue(len(active_gen) > 0, "CURRENT pointer is empty")

        active_dir = os.path.join(self.dest_dir, "generations", active_gen)
        self.assertTrue(os.path.isdir(active_dir), f"Active generation dir {active_dir} does not exist")
        self.assertTrue(os.path.isfile(os.path.join(active_dir, "vault.svault")), "vault.svault missing in active generation")
        self.assertTrue(os.path.isfile(os.path.join(active_dir, "vault.svault.meta")), "vault.svault.meta missing in active generation")

        target_vault = os.path.join(self.dest_dir, sa_backup.DEFAULT_VAULT_NAME)
        target_meta = os.path.join(self.dest_dir, sa_backup.DEFAULT_META_NAME)
        self.assertTrue(os.path.isfile(target_vault), "__STORE.svault missing")
        self.assertTrue(os.path.isfile(target_meta), "__STORE.svault.meta missing")


class TestDecoupledBackupBroker(unittest.TestCase):
    """P1-7: Test decoupled backup state in broker."""

    def test_broker_backup_states_and_status(self):
        import sa_broker
        registry = MagicMock()
        registry.state_dir = r"C:\fake\state"
        registry.audit_path = r"C:\fake\audit.log"
        profile = MagicMock()
        profile.id = "obsidian"
        profile.container = r"C:\fake\vault.vhdx"
        profile.backend = "fake"
        profile.policy = MagicMock()
        profile.policy.mode = "default"
        profile.policy.idle_timeout_minutes = 360
        registry.profiles = [profile]
        registry.get_profile.return_value = profile
        registry.ids.return_value = ["obsidian"]

        backend = sa_storage.FakeStorageBackend()
        audit = MagicMock()
        cred_store = MagicMock()
        broker = sa_broker.SecureBroker(
            registry,
            audit=audit,
            credential_store=cred_store,
            backends={"fake": backend}
        )
        broker._backends["obsidian"] = backend

        st = broker.status("obsidian")
        self.assertIn("backup", st)
        self.assertEqual(st["backup"]["state"], sa_broker.BACKUP_NEVER_CREATED)


if __name__ == "__main__":
    unittest.main()

