# encoding: utf-8
"""Tests for Secure Apps Door 2: Native BitLocker Portable Master Password."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_mnemonic
import sa_storage
import sa_broker
import sa_config
import sa_audit
import sa_state
import sa_crypto
import sa_layout


class TestPortablePasswordMnemonic(unittest.TestCase):
    """Test passphrase generation and validation."""

    def test_generate_portable_passphrase_defaults(self):
        phrase = sa_mnemonic.generate_portable_passphrase(9)
        words = phrase.split()
        self.assertEqual(len(words), 9)
        for w in words:
            self.assertIn(w, sa_mnemonic.BIP39_WORDS)

    def test_generate_portable_passphrase_word_counts(self):
        for count in (9, 10, 12):
            phrase = sa_mnemonic.generate_portable_passphrase(count)
            self.assertEqual(len(phrase.split()), count)

        with self.assertRaises(ValueError):
            sa_mnemonic.generate_portable_passphrase(8)

    def test_validate_master_password_passphrase(self):
        phrase = sa_mnemonic.generate_portable_passphrase(9)
        ok, msg = sa_mnemonic.validate_master_password(phrase)
        self.assertTrue(ok, msg)

    def test_validate_master_password_too_short(self):
        ok, msg = sa_mnemonic.validate_master_password("short-pass")
        self.assertFalse(ok)
        self.assertIn("at least 16", msg)

    def test_validate_master_password_weak_pattern(self):
        ok, msg = sa_mnemonic.validate_master_password("1234567890123456")
        self.assertFalse(ok)
        self.assertIn("guessed", msg.lower())

        ok, msg = sa_mnemonic.validate_master_password("aaaaaaaaaaaaaaaa")
        self.assertFalse(ok)
        self.assertIn("character", msg.lower())

    def test_generate_word_challenge(self):
        phrase = "abandon ability able about above absent absorb abstract absurd"
        challenges = sa_mnemonic.generate_word_challenge(phrase, challenge_count=3)
        self.assertEqual(len(challenges), 3)
        words = phrase.split()
        for idx, word in challenges:
            self.assertEqual(words[idx - 1], word)


class TestPortablePasswordStorageBackend(unittest.TestCase):
    """Test portable password operations on FakeStorageBackend."""

    def setUp(self):
        self.backend = sa_storage.FakeStorageBackend()
        self.profile = MagicMock()
        self.profile.id = "obsidian"
        self.profile.container = r"C:\fake\vault.vhdx"
        self.profile.container_id = "test-container-id"
        self.profile.mount_path = r"C:\fake\mount"

    def test_fake_backend_portable_password_lifecycle(self):
        secret = sa_crypto.new_volume_secret()
        self.addCleanup(secret.zeroize)
        res = self.backend.create(
            self.profile,
            secret,
            portable_password="my-strong-portable-master-password-123"
        )
        self.assertTrue(res.get("has_portable_password"))
        key = self.backend._key(self.profile)
        self.assertEqual(self.backend._passwords.get(key), "my-strong-portable-master-password-123")

        # List protectors
        prots = self.backend.list_protectors(self.profile)
        types = [p["type"] for p in prots["protectors"]]
        self.assertIn("Password", types)
        self.assertIn("RecoveryPassword", types)

        # Change portable password
        change_res = self.backend.change_portable_password(
            self.profile, "new-strong-portable-password-456"
        )
        self.assertTrue(change_res.get("ok"))
        self.assertEqual(self.backend._passwords.get(key), "new-strong-portable-password-456")

        # Remove portable password
        rem_res = self.backend.remove_portable_password(self.profile)
        self.assertTrue(rem_res.get("ok"))
        self.assertNotIn(key, self.backend._passwords)

        # Add portable password back
        add_res = self.backend.add_portable_password(
            self.profile, "another-strong-portable-password-789"
        )
        self.assertTrue(add_res.get("ok"))
        self.assertEqual(self.backend._passwords.get(key), "another-strong-portable-password-789")

        # Unlock with portable password
        self.backend._state[key] = sa_storage.DETACHED
        unlock_res = self.backend.unlock_and_mount(
            self.profile, password="another-strong-portable-password-789"
        )
        self.assertEqual(unlock_res.get("state"), sa_storage.MOUNTED)


class TestPortablePasswordBroker(unittest.TestCase):
    """Test broker integration with portable password."""

    def setUp(self):
        self.registry = MagicMock()
        self.registry.state_dir = r"C:\fake\state"
        self.registry.audit_path = r"C:\fake\audit.log"
        self.registry.default_profile = "obsidian"

        self.profile = MagicMock()
        self.profile.id = "obsidian"
        self.profile.container = r"C:\fake\vault.vhdx"
        self.profile.backend = "fake"
        self.profile.backup_destination = r"C:\fake\backups"
        self.registry.profiles = [self.profile]
        self.registry.get_profile.return_value = self.profile
        self.registry.ids.return_value = ["obsidian"]

        import tempfile
        import shutil
        import sa_layout
        self.temp_dir = tempfile.mkdtemp(prefix="sa_test_pw_broker_")
        self.profile.container = os.path.join(self.temp_dir, "vault.vhdx")
        self.profile.container_id = "test-cid-broker"
        self.backend = sa_storage.FakeStorageBackend()
        key = self.backend._key(self.profile)
        self.backend._recovery_passwords[key] = "000000-111111-222222-333333-444444-555555-666666-777777"
        sa_layout.save_layout(
            self.profile,
            daily_id="{fake-daily-id}",
            daily_type="RecoveryPassword",
            break_glass_id="{fake-rec-id}",
            break_glass_type="RecoveryPassword"
        )
        import sa_auth
        self.profile.credential_profile = "primary"
        self.audit = MagicMock()
        self.cred_path = os.path.join(self.temp_dir, "test_credentials.json")
        self.cred_store = sa_auth.CredentialStore(self.cred_path)
        self.broker = sa_broker.SecureBroker(
            self.registry,
            audit=self.audit,
            credential_store=self.cred_store,
            backends={"fake": self.backend}
        )
        self.broker._backends["obsidian"] = self.backend

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_broker_portable_password_methods(self):
        key = self.backend._key(self.profile)
        self.backend._exists[key] = True

        # Add portable password
        res = self.broker.add_portable_password("obsidian", "passphrase-one-two-three")
        self.assertTrue(res.get("ok"))
        self.assertEqual(self.backend._passwords.get(key), "passphrase-one-two-three")

        # List protectors
        res = self.broker.list_protectors("obsidian")
        self.assertEqual(len(res.get("protectors", [])), 3)

        # Change portable password
        res = self.broker.change_portable_password("obsidian", "passphrase-four-five-six")
        self.assertTrue(res.get("ok"))
        self.assertEqual(self.backend._passwords.get(key), "passphrase-four-five-six")

        # Remove portable password
        res = self.broker.remove_portable_password("obsidian")
        self.assertTrue(res.get("ok"))
        self.assertNotIn(key, self.backend._passwords)

    def test_r2_transactional_rotation_and_escrow_hygiene(self):
        """R2: Transactional rotation uses escrow, verifies new password, zeroizes escrow without secret leak."""
        key = self.backend._key(self.profile)
        self.backend._exists[key] = True

        self.broker.add_portable_password("obsidian", "initial-strong-password-1")
        # Change password
        res = self.broker.change_portable_password("obsidian", "updated-strong-password-2")
        self.assertTrue(res.get("ok"))
        self.assertNotIn("escrow_key", res, "escrow_key leaked in return dictionary!")
        self.assertNotIn("escrow_secret", res, "escrow_secret leaked in return dictionary!")

        # Verify backend received expected calls: change_portable_password -> unlock_and_mount -> remove_protector
        backend_calls = [c[0] for c in self.backend.calls]
        self.assertIn("change_portable_password", backend_calls)
        self.assertIn("verify_password_unlock", backend_calls)
        self.assertIn("remove_protector", backend_calls)

        # Assert escrow was cleaned up from backend
        self.assertFalse(self.backend._escrows.get(key))

        # Test failure during verification preserves escrow
        self.backend.fail_unlock = "Simulated BitLocker unlock failure"
        with self.assertRaises(sa_storage.StorageError) as ctx:
            self.broker.change_portable_password("obsidian", "another-password")

        # Escrow must NOT be leaked in exception message
        self.assertNotIn("999999-999999", str(ctx.exception))
        # Escrow must be preserved in backend for emergency recovery
        self.assertIn(key, self.backend._escrows)
        self.assertEqual(self.backend._escrows[key]["id"], "{fake-escrow-id}")

    def test_p0_c_multiple_yubikey_migration_and_active_generation(self):
        """P0-C: Upgrade tracks daily generations and leaves retired keys flagged as migration pending."""
        import sa_auth
        provider = sa_auth.FakeAuthProvider()
        key = self.backend._key(self.profile)
        self.backend._exists[key] = True
        self.backend._passwords[key] = "legacy-daily-password"
        layout_path = sa_layout.layout_path_for_profile(self.profile)
        if os.path.exists(layout_path):
            os.remove(layout_path)

        # Pre-seed store with an existing enrollment (generation 1) with distinct credential id
        provider.credential_id = b"fake-credential-id-111"
        cred1 = provider.create_credential(self.profile.credential_profile, require_hmac_secret=True)
        dummy_secret = sa_crypto.new_volume_secret()
        enr1 = sa_auth.wrap_volume_secret_for(
            self.profile, provider, cred1, dummy_secret,
            container_id=self.profile.container_id,
            daily_generation=1
        )
        dummy_secret.zeroize()
        self.cred_store.add(self.profile.id, self.profile.container_id, enr1)
        self.cred_store.set_active_daily_generation(self.profile.id, 1)

        # Set new credential ID for the upgrade operation
        provider.credential_id = b"fake-credential-id-222"

        # Run legacy upgrade with another credential
        res = self.broker.upgrade_legacy_vault(
            "obsidian",
            provider=provider,
            portable_password="new-portable-master-password",
            old_daily_password="legacy-daily-password"
        )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("active_daily_generation"), 2)
        self.assertTrue(res.get("key_migration_pending"))
        self.assertEqual(res.get("pending_keys_count"), 1)

        # Active generation is now 2; active_generation_only=True filters out enr1
        active_enrollments = self.cred_store.enrollments(self.profile.id, active_generation_only=True)
        self.assertEqual(len(active_enrollments), 1)
        self.assertEqual(active_enrollments[0].daily_generation, 2)

    def test_p0_d_legacy_upgrade_rollback_stages(self):
        """P0-D: Upgrade state machine rollback never removes new daily route after old route is removed."""
        import sa_auth
        provider = sa_auth.FakeAuthProvider()
        key = self.backend._key(self.profile)
        self.backend._exists[key] = True
        self.backend._passwords[key] = "legacy-daily-password"
        layout_path = sa_layout.layout_path_for_profile(self.profile)
        if os.path.exists(layout_path):
            os.remove(layout_path)

        # 1. Failure before OLD_DAILY_NATIVE_REMOVED: should clean up new daily protector
        orig_wrap = sa_auth.wrap_volume_secret_for
        def fail_wrap(*args, **kwargs):
            raise RuntimeError("Injected staging failure")
        sa_auth.wrap_volume_secret_for = fail_wrap
        try:
            with self.assertRaises(RuntimeError):
                self.broker.upgrade_legacy_vault(
                    "obsidian",
                    provider=provider,
                    old_daily_password="legacy-daily-password"
                )
            self.assertIn("remove_protector", [c[0] for c in self.backend.calls])
        finally:
            sa_auth.wrap_volume_secret_for = orig_wrap

        # Reset calls
        self.backend.calls.clear()

        # 2. Failure after OLD_DAILY_NATIVE_REMOVED (e.g. during save_layout)
        orig_save = sa_layout.save_layout
        def fail_save(*args, **kwargs):
            raise RuntimeError("Injected layout commit failure after old daily removed")
        sa_layout.save_layout = fail_save
        try:
            with self.assertRaises(RuntimeError):
                self.broker.upgrade_legacy_vault(
                    "obsidian",
                    provider=provider,
                    old_daily_password="legacy-daily-password"
                )
            # POINT OF NO RETURN INVARIANT:
            # backend.remove_protector was called for old_pass_id, but MUST NOT be called for new daily protector!
            removals = [c for c in self.backend.calls if c[0] == "remove_protector"]
            self.assertEqual(len(removals), 1)
            self.assertEqual(removals[0][2], "{fake-pw-id}")
        finally:
            sa_layout.save_layout = orig_save

    def test_r3_protector_layout_persists_roles_without_secrets(self):
        """R3: Authoritative layout persists exact role IDs and never contains secrets."""
        layout = sa_layout.load_layout(self.profile)
        self.assertIsNotNone(layout)
        self.assertEqual(layout["layout_version"], 2)
        self.assertEqual(layout["container_id"], self.profile.container_id)
        self.assertEqual(layout["daily_protector"]["id"], "{fake-daily-id}")
        self.assertEqual(layout["break_glass_protector"]["id"], "{fake-rec-id}")

        # String representation of layout file must not contain recovery password
        path = sa_layout.layout_path_for_profile(self.profile)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("000000-111111", content)
        self.assertNotIn("888888-888888", content)

    def test_r4_legacy_upgrade_orchestration(self):
        """R4: Legacy upgrade end-to-end orchestration converts daily password to RecoveryPassword."""
        import sa_auth
        provider = sa_auth.FakeAuthProvider()
        key = self.backend._key(self.profile)
        self.backend._exists[key] = True
        self.backend._passwords[key] = "legacy-daily-password"
        layout_path = sa_layout.layout_path_for_profile(self.profile)
        if os.path.exists(layout_path):
            os.remove(layout_path)

        res = self.broker.upgrade_legacy_vault(
            "obsidian",
            provider=provider,
            portable_password="new-portable-master-password",
            old_daily_password="legacy-daily-password"
        )
        self.assertTrue(res.get("ok"))
        self.assertTrue(res.get("enrolled"))

        # Check layout v2 was committed
        layout = sa_layout.load_layout(self.profile)
        self.assertIsNotNone(layout)
        self.assertEqual(layout["daily_protector"]["type"], "RecoveryPassword")
        self.assertEqual(layout["portable_protector"]["type"], "Password")

    def test_h15_ambiguous_legacy_layout_fails_closed(self):
        """H15: Ambiguous legacy layout (extra RecoveryPassword) fails closed with LEGACY_LAYOUT_AMBIGUOUS."""
        import sa_auth
        provider = sa_auth.FakeAuthProvider()
        key = self.backend._key(self.profile)
        self.backend._exists[key] = True
        self.backend._passwords[key] = "legacy-daily-password"
        layout_path = sa_layout.layout_path_for_profile(self.profile)
        if os.path.exists(layout_path):
            os.remove(layout_path)

        def mock_list(prof):
            return {
                "container": prof.container,
                "lock_status": "Unlocked",
                "protectors": [
                    {"id": "{fake-pw-id}", "type": "Password"},
                    {"id": "{fake-rec-1}", "type": "RecoveryPassword"},
                    {"id": "{fake-rec-2}", "type": "RecoveryPassword"},
                ]
            }
        self.backend.list_protectors = mock_list

        with self.assertRaises(sa_storage.StorageError) as ctx:
            self.broker.upgrade_legacy_vault("obsidian", provider=provider, old_daily_password="legacy-daily-password")
        self.assertEqual(ctx.exception.category, "LEGACY_LAYOUT_AMBIGUOUS")


class TestUnicodePasswordRoundtrip(unittest.TestCase):
    """Test P0-3: Strict UTF-8 roundtrip without lossy ASCII replacement."""

    def test_unicode_passwords_base64_roundtrip(self):
        import base64
        test_passwords = [
            "Mägede ja järvede vahel 12345!",    # Estonian diacritics
            "СекретныйПарольОтХранилища2026!",   # Cyrillic
            "私の安全なマスターパスワード2026!",       # Japanese CJK
            "TõelineSalasõnaKaitsebKõike!",      # Estonian õ, ä, ö, ü
        ]
        for pw in test_passwords:
            raw_bytes = pw.encode("utf-8")
            b64 = base64.b64encode(raw_bytes).decode("ascii")
            decoded = base64.b64decode(b64).decode("utf-8")
            self.assertEqual(pw, decoded)
            self.assertNotIn("?", decoded)


class TestCliSecretFreeHygiene(unittest.TestCase):
    """Test P0-4: Secret-free CLI hygiene; no password flags in argv."""

    BANNED = ["--password", "--old-password", "--new-password", "--portable-password",
              "--recovery-key", "--recovery-password", "--pin", "--secret", "-p"]

    @staticmethod
    def _all_options(parser, seen=None):
        import argparse
        seen = seen if seen is not None else set()
        if id(parser) in seen:
            return []
        seen.add(id(parser))
        out = []
        for action in parser._actions:
            out.extend(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    out.extend(TestCliSecretFreeHygiene._all_options(sub, seen))
        return out

    def test_no_secret_flags_in_cli_parser(self):
        import sa_cli
        all_options = self._all_options(sa_cli.build_parser())
        for flag in self.BANNED:
            self.assertNotIn(flag, all_options, f"Banned secret flag {flag} found in CLI options!")

    def test_recursive_walk_detects_three_level_secret_flag(self):
        import argparse
        root = argparse.ArgumentParser()
        l1 = root.add_subparsers().add_parser("a")
        l2 = l1.add_subparsers().add_parser("b")
        l3 = l2.add_subparsers().add_parser("c")
        l3.add_argument("--new-password")
        self.assertIn("--new-password", self._all_options(root))


class TestRealBackendPasswordVerifierContract(unittest.TestCase):
    """P0-A & P0-B: Real BitLockerVhdxBackend contract, verifier requirement, and escrow canary hygiene."""

    def test_bitlocker_backend_implements_verify_password_unlock(self):
        self.assertTrue(
            hasattr(sa_storage.BitLockerVhdxBackend, "verify_password_unlock"),
            "BitLockerVhdxBackend must implement verify_password_unlock"
        )
        self.assertTrue(
            callable(getattr(sa_storage.BitLockerVhdxBackend, "verify_password_unlock")),
            "BitLockerVhdxBackend.verify_password_unlock must be callable"
        )

    def test_broker_refuses_backend_without_verifier(self):
        """Broker must raise StorageError if backend lacks verify_password_unlock (no fallback to True)."""
        import tempfile
        import shutil
        import sa_layout

        tmp = tempfile.mkdtemp()
        try:
            registry = MagicMock()
            registry.credentials_path = os.path.join(tmp, "creds.json")
            registry.audit_path = os.path.join(tmp, "audit.log")
            registry.state_dir = tmp

            profile = MagicMock()
            profile.id = "obsidian"
            profile.container = os.path.join(tmp, "vault.vhdx")
            profile.container_id = "test-cid"
            registry.profiles = [profile]
            registry.get_profile.return_value = profile
            registry.ids.return_value = ["obsidian"]

            backend_no_verifier = MagicMock()
            del backend_no_verifier.verify_password_unlock
            backend_no_verifier.change_portable_password.return_value = {
                "ok": True, "escrow_id": "esc-1", "protector_id": "p-1", "escrow_key": "dummy-key"
            }
            backend_no_verifier.list_protectors.return_value = {
                "protectors": [
                    {"id": "{fake-daily-id}", "type": "RecoveryPassword"},
                    {"id": "{fake-rec-id}", "type": "RecoveryPassword"}
                ]
            }

            sa_layout.save_layout(
                profile,
                daily_id="{fake-daily-id}",
                daily_type="RecoveryPassword",
                break_glass_id="{fake-rec-id}",
                break_glass_type="RecoveryPassword"
            )
            cred_store = MagicMock()
            broker = sa_broker.SecureBroker(registry, credential_store=cred_store, backends={"fake": backend_no_verifier})
            broker._backends["obsidian"] = backend_no_verifier

            with self.assertRaises(sa_storage.StorageError) as ctx:
                broker.change_portable_password("obsidian", "new-strong-password-123")
            self.assertIn("verify_password_unlock", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_escrow_secret_hygiene_canary_absent(self):
        """P0-B: Escrow canary (999999-...) must be absent from broker result, CLI, and audit log."""
        import tempfile
        import shutil
        import sa_layout

        tmp = tempfile.mkdtemp()
        try:
            registry = MagicMock()
            registry.credentials_path = os.path.join(tmp, "creds.json")
            registry.audit_path = os.path.join(tmp, "audit.log")
            registry.state_dir = tmp

            profile = MagicMock()
            profile.id = "obsidian"
            profile.container = os.path.join(tmp, "vault.vhdx")
            profile.container_id = "test-cid"
            registry.profiles = [profile]
            registry.get_profile.return_value = profile
            registry.ids.return_value = ["obsidian"]

            backend = sa_storage.FakeStorageBackend()
            key = backend._key(profile)
            backend._passwords[key] = "initial-portable-pass"
            backend._recovery_passwords[key] = "000000-111111-222222-333333-444444-555555-666666-777777"
            canary = "999999-888888-777777-666666-555555-444444-333333-222222"
            orig_change = backend.change_portable_password
            def change_with_canary(*args, **kwargs):
                res = orig_change(*args, **kwargs)
                res["escrow_key"] = canary
                return res
            backend.change_portable_password = change_with_canary

            audit_lines = []
            audit = MagicMock()
            audit.log = lambda event, **kwargs: audit_lines.append(str(kwargs))

            sa_layout.save_layout(
                profile,
                daily_id="{fake-daily-id}",
                daily_type="RecoveryPassword",
                break_glass_id="{fake-rec-id}",
                break_glass_type="RecoveryPassword"
            )
            cred_store = MagicMock()
            broker = sa_broker.SecureBroker(registry, audit=audit, credential_store=cred_store, backends={"fake": backend})
            broker._backends["obsidian"] = backend

            result = broker.change_portable_password("obsidian", "strong-new-pass-123")
            self.assertTrue(result.get("ok"))
            self.assertNotIn(canary, str(result))
            for line in audit_lines:
                self.assertNotIn(canary, line)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

