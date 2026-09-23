"""Tests for Secure Apps UI integration, next action engine, and recovery UX."""
import hashlib
import importlib
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_acceptance
import sa_acceptance_runner
import sa_auth
import sa_config
import sa_crypto
import sa_enroll
import sa_migrate
import sa_privtask
import sa_setup
import sa_state


class NextActionEngineTests(unittest.TestCase):
    """Test all 11 deterministic lifecycle transitions in get_next_setup_action()."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-nextaction-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.prev_root = os.environ.get(sa_config.MANAGED_ROOT_ENV)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(self.tmp)
        if self.prev_root is None:
            self.addCleanup(os.environ.pop, sa_config.MANAGED_ROOT_ENV, None)
        else:
            self.addCleanup(os.environ.__setitem__, sa_config.MANAGED_ROOT_ENV, self.prev_root)
        self.registry = sa_config.load_registry(sa_config.default_registry_path())

    def test_1_enable_privilege_separation_when_enablelua_false(self):
        preflight = {"EnableLUA": False, "broker_integrity": "HIGH", "broker_elevated": True}
        action = sa_setup.get_next_setup_action(self.registry, "obsidian", preflight_report=preflight)
        self.assertEqual(action, sa_setup.ACTION_ENABLE_PRIVILEGE_SEPARATION)

    def test_2_reboot_required_after_prepare_uac(self):
        preflight = {"EnableLUA": False, "broker_integrity": "HIGH", "broker_elevated": True}
        action = sa_setup.get_next_setup_action(
            self.registry, "obsidian", preflight_report=preflight, reboot_pending=True
        )
        self.assertEqual(action, sa_setup.ACTION_REBOOT_REQUIRED)

    def test_3_run_non_elevated_when_enablelua_true_but_broker_high(self):
        preflight = {"EnableLUA": True, "broker_integrity": "HIGH", "broker_elevated": True}
        action = sa_setup.get_next_setup_action(self.registry, "obsidian", preflight_report=preflight)
        self.assertEqual(action, sa_setup.ACTION_RUN_NON_ELEVATED)

    def test_4_install_helper_when_helper_missing(self):
        preflight = {
            "EnableLUA": True,
            "broker_integrity": "MEDIUM",
            "broker_elevated": False,
            "scheduled_helper_installed": False,
        }
        action = sa_setup.get_next_setup_action(self.registry, "obsidian", preflight_report=preflight)
        self.assertEqual(action, sa_setup.ACTION_INSTALL_HELPER)

    def test_5_commission_helper_when_helper_installed_not_commissioned(self):
        preflight = {
            "EnableLUA": True,
            "broker_integrity": "MEDIUM",
            "broker_elevated": False,
            "scheduled_helper_installed": True,
            "scheduled_helper_definition_valid": True,
        }
        action = sa_setup.get_next_setup_action(
            self.registry, "obsidian", preflight_report=preflight, commissioned=False
        )
        self.assertEqual(action, sa_setup.ACTION_COMMISSION_HELPER)

    def test_6_probe_fido2_when_helper_commissioned_but_no_probe(self):
        preflight = {
            "EnableLUA": True,
            "broker_integrity": "MEDIUM",
            "broker_elevated": False,
            "scheduled_helper_installed": True,
            "scheduled_helper_definition_valid": True,
        }
        action = sa_setup.get_next_setup_action(
            self.registry, "obsidian", preflight_report=preflight, commissioned=True, probe_result=None
        )
        self.assertEqual(action, sa_setup.ACTION_PROBE_FIDO2)

    def test_7_run_acceptance_when_probe_passed_but_no_acceptance(self):
        preflight = {
            "EnableLUA": True,
            "broker_integrity": "MEDIUM",
            "broker_elevated": False,
            "scheduled_helper_installed": True,
            "scheduled_helper_definition_valid": True,
        }
        probe = {"available": True, "hmac_secret": True, "transport": "ELEVATED_CTAP_HELPER"}
        action = sa_setup.get_next_setup_action(
            self.registry, "obsidian", preflight_report=preflight, commissioned=True, probe_result=probe
        )
        self.assertEqual(action, sa_setup.ACTION_RUN_ACCEPTANCE)

    def test_8_import_application_when_acceptance_passed_but_app_missing(self):
        preflight = {
            "EnableLUA": True,
            "broker_integrity": "MEDIUM",
            "broker_elevated": False,
            "scheduled_helper_installed": True,
            "scheduled_helper_definition_valid": True,
        }
        probe = {"available": True, "hmac_secret": True, "transport": "ELEVATED_CTAP_HELPER"}
        fake_acceptance = mock.Mock(ok=True)
        action = sa_setup.get_next_setup_action(
            self.registry, "obsidian",
            preflight_report=preflight,
            commissioned=True,
            probe_result=probe,
            acceptance_verdict=fake_acceptance
        )
        self.assertEqual(action, sa_setup.ACTION_IMPORT_APPLICATION)

    def test_9_enroll_profile_when_app_present_but_vault_not_enrolled(self):
        preflight = {
            "EnableLUA": True,
            "broker_integrity": "MEDIUM",
            "broker_elevated": False,
            "scheduled_helper_installed": True,
            "scheduled_helper_definition_valid": True,
        }
        probe = {"available": True, "hmac_secret": True, "transport": "ELEVATED_CTAP_HELPER"}
        fake_acceptance = mock.Mock(ok=True)

        prof = self.registry.get("obsidian")
        os.makedirs(os.path.dirname(prof.executable), exist_ok=True)
        with open(prof.executable, "w") as f:
            f.write("mock-app")

        action = sa_setup.get_next_setup_action(
            self.registry, "obsidian",
            preflight_report=preflight,
            commissioned=True,
            probe_result=probe,
            acceptance_verdict=fake_acceptance
        )
        self.assertEqual(action, sa_setup.ACTION_ENROLL_PROFILE)

    def test_10_migrate_vault_when_enrolled_but_migration_not_done(self):
        preflight = {
            "EnableLUA": True,
            "broker_integrity": "MEDIUM",
            "broker_elevated": False,
            "scheduled_helper_installed": True,
            "scheduled_helper_definition_valid": True,
        }
        probe = {"available": True, "hmac_secret": True, "transport": "ELEVATED_CTAP_HELPER"}
        fake_acceptance = mock.Mock(ok=True)

        prof = self.registry.get("obsidian")
        os.makedirs(os.path.dirname(prof.executable), exist_ok=True)
        with open(prof.executable, "w") as f:
            f.write("mock-app")
        os.makedirs(os.path.dirname(prof.container), exist_ok=True)
        with open(prof.container, "w") as f:
            f.write("mock-vhdx")

        store = sa_auth.CredentialStore(self.registry.credentials_path)
        store.add("obsidian", "cont-1", make_mock_enrollment("mock-cred", b"cred-id-1234"))

        action = sa_setup.get_next_setup_action(
            self.registry, "obsidian",
            preflight_report=preflight,
            commissioned=True,
            probe_result=probe,
            acceptance_verdict=fake_acceptance
        )
        self.assertEqual(action, sa_setup.ACTION_MIGRATE_VAULT)

    def test_11_ready_when_migration_complete(self):
        preflight = {
            "EnableLUA": True,
            "broker_integrity": "MEDIUM",
            "broker_elevated": False,
            "scheduled_helper_installed": True,
            "scheduled_helper_definition_valid": True,
        }
        probe = {"available": True, "hmac_secret": True, "transport": "ELEVATED_CTAP_HELPER"}
        fake_acceptance = mock.Mock(ok=True)

        prof = self.registry.get("obsidian")
        os.makedirs(os.path.dirname(prof.executable), exist_ok=True)
        with open(prof.executable, "w") as f:
            f.write("mock-app")
        os.makedirs(os.path.dirname(prof.container), exist_ok=True)
        with open(prof.container, "w") as f:
            f.write("mock-vhdx")

        store = sa_auth.CredentialStore(self.registry.credentials_path)
        store.add("obsidian", "cont-1", make_mock_enrollment("mock-cred", b"cred-id-1234"))

        journal = os.path.join(self.registry.state_dir, "migration-obsidian.json")
        os.makedirs(self.registry.state_dir, exist_ok=True)
        with open(journal, "w", encoding="utf-8") as f:
            json.dump({"schema": sa_migrate.JOURNAL_SCHEMA, "last_step": sa_migrate.STEP_COMPLETE}, f)

        action = sa_setup.get_next_setup_action(
            self.registry, "obsidian",
            preflight_report=preflight,
            commissioned=True,
            probe_result=probe,
            acceptance_verdict=fake_acceptance
        )
        self.assertEqual(action, sa_setup.ACTION_READY)


class ModeExplanationTests(unittest.TestCase):
    """Test Default vs Aggressive mode explanations in UI."""

    def test_explanations_contain_plain_language_and_no_crypto_jargon(self):
        import secure_apps_gui as module

        d = module.DEFAULT_MODE_EXPLANATION
        a = module.AGGRESSIVE_MODE_EXPLANATION

        self.assertIn("Closing Obsidian hides and unmounts the vault", d)
        self.assertIn("reopen without the key during the cached session", d)
        self.assertIn("Closing Obsidian fully locks the vault", a)
        self.assertIn("Every new opening requires YubiKey authentication", a)

        for jargon in ("volume_secret", "hmac-secret", "wrapped key", "broker lease"):
            self.assertNotIn(jargon.lower(), d.lower())
            self.assertNotIn(jargon.lower(), a.lower())


def make_mock_enrollment(label, cred_id):
    return sa_auth.Enrollment(
        credential_profile=label,
        provider="fido2",
        credential_id=cred_id,
        rp_id=sa_auth.RP_ID,
        hmac_salt=b"0" * 32,
        kdf_salt=b"1" * 32,
        nonce=b"2" * 12,
        ciphertext=b"3" * 48,
    )


class LostKeyRemovalTests(unittest.TestCase):
    """Credential management: cannot remove last key, requires auth with remaining key."""

    def setUp(self):
        import threading
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-keys-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.creds_path = str(self.tmp / "credentials.json")
        self.store = sa_auth.CredentialStore(self.creds_path)
        self.registry = sa_config.load_registry(sa_config.default_registry_path())
        self.broker = mock.Mock()
        self.broker.registry = self.registry
        self.broker.credentials = self.store
        mock_runtime = mock.Mock()
        mock_runtime.lock = threading.Lock()
        self.broker.runtime.return_value = mock_runtime
        self.broker.provider.return_value = mock.Mock()
        self.broker.audit = mock.Mock()

    def test_removing_last_credential_is_refused(self):
        self.store.add("obsidian", "cont-1", make_mock_enrollment("key-primary", b"id-primary"))
        with self.assertRaises(sa_auth.AuthError) as ctx:
            sa_enroll.remove_credential_authenticated(
                self.broker, "obsidian", "key-primary"
            )
        self.assertIn("last", str(ctx.exception).lower())

    def test_removing_requires_authentication_with_remaining_key(self):
        self.store.add("obsidian", "cont-1", make_mock_enrollment("key-primary", b"id-primary"))
        self.store.add("obsidian", "cont-1", make_mock_enrollment("key-backup", b"id-backup"))

        with mock.patch("sa_auth.unwrap_volume_secret", side_effect=sa_auth.AuthError("authentication failed")):
            with self.assertRaises(sa_auth.AuthError) as ctx:
                sa_enroll.remove_credential_authenticated(
                    self.broker, "obsidian", "key-backup"
                )
            self.assertIn("authentication failed", str(ctx.exception).lower())

        mock_secret = sa_crypto.SecretBuffer(b"x" * 32)
        with mock.patch("sa_auth.unwrap_volume_secret", return_value=(b"id-primary", mock_secret)):
            ok = sa_enroll.remove_credential_authenticated(
                self.broker, "obsidian", "key-backup"
            )
            self.assertTrue(ok)
            self.assertTrue(self.store.is_enrolled("obsidian"))
            remaining = self.store.enrollments("obsidian")
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].credential_profile, "key-primary")


class DiagnosticsSecretHygieneTests(unittest.TestCase):
    """Diagnostics export must strictly exclude secrets and plaintexts."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-diag-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(self.tmp)
        self.registry = sa_config.load_registry(sa_config.default_registry_path())

    def test_diagnostics_never_include_secret_material(self):
        preflight = {
            "EnableLUA": True,
            "broker_integrity": "MEDIUM",
            "broker_elevated": False,
            "scheduled_helper_installed": True,
            "PIN": "123456",
            "hmac_secret": "abcdef0123456789",
            "bitlocker_password": "super-secret-vol-pass",
            "recovery_password": "111111-222222-333333-444444-555555-666666-777777-888888",
            "notes": "my secret daily notes",
            "claude_token": "sk-ant-secret",
        }
        text = sa_setup.format_diagnostics(
            self.registry,
            profile_id="obsidian",
            preflight_report=preflight,
            acceptance_verdict=None,
            probe_result=None,
            status_payload={"state": "LOCKED", "session_active": False}
        )

        for secret in (
            "123456",
            "abcdef0123456789",
            "super-secret-vol-pass",
            "111111-222222-333333-444444",
            "my secret daily notes",
            "sk-ant-secret",
        ):
            self.assertNotIn(secret, text)

        self.assertIn("EnableLUA: True", text)
        self.assertIn("broker_integrity: MEDIUM", text)
        self.assertIn("scheduled_helper_installed: True", text)
        self.assertIn("profile_id: obsidian", text)


class HumanErrorMessageTests(unittest.TestCase):
    """Ensure error messages are translated into human guidance with no raw tokens."""

    def test_human_error_messages(self):
        msg = sa_setup.human_error_message("ENABLELUA_DISABLED", "raw details")
        self.assertIn("Windows privilege separation is disabled", msg)
        self.assertIn("restart", msg.lower())

        msg = sa_setup.human_error_message("BROKER_HIGH_INTEGRITY", "raw details")
        self.assertIn("Administrator", msg)
        self.assertIn("normally", msg)

        msg = sa_setup.human_error_message("PRIVILEGED_HELPER_NOT_INSTALLED", "raw details")
        self.assertIn("silent privileged helper is not installed", msg)

        msg = sa_setup.human_error_message("PRIVILEGED_TASK_TAMPERED", "raw details")
        self.assertIn("Repair", msg)

        msg = sa_setup.human_error_message("HARDWARE_ACCEPTANCE_REQUIRED", "raw details")
        self.assertIn("disposable YubiKey security test", msg)


class SetupChecklistTests(unittest.TestCase):
    """Validate 8-step setup checklist derivation."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-checklist-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(self.tmp)
        self.registry = sa_config.load_registry(sa_config.default_registry_path())

    def test_pre_reboot_checklist_shows_waiting_for_subsequent_steps(self):
        preflight = {"EnableLUA": False, "broker_integrity": "HIGH", "broker_elevated": True}
        checklist = sa_setup.get_setup_checklist(
            self.registry, "obsidian", preflight_report=preflight, reboot_pending=True
        )
        self.assertEqual(len(checklist), 8)
        self.assertEqual(checklist[0]["status"], sa_setup.STEP_REBOOT_REQUIRED)
        for s in checklist[1:]:
            self.assertEqual(s["status"], sa_setup.STEP_WAITING)
            self.assertIn("Restart Windows", s["summary"])


class GuiArgParsingTests(unittest.TestCase):
    def test_parse_gui_args_defaults(self):
        import secure_apps_gui
        args = secure_apps_gui.parse_gui_args([])
        self.assertIsNone(args.registry)
        self.assertIsNone(args.managed_root)
        self.assertIsNone(args.profile)

    def test_parse_gui_args_custom(self):
        import secure_apps_gui
        args = secure_apps_gui.parse_gui_args([
            "--registry", "C:\\temp\\reg.json",
            "--managed-root", "C:\\temp\\root",
            "--profile", "obsidian"
        ])
        self.assertEqual(args.registry, "C:\\temp\\reg.json")
        self.assertEqual(args.managed_root, "C:\\temp\\root")
        self.assertEqual(args.profile, "obsidian")


class GuiDuplicateInstanceTests(unittest.TestCase):
    def test_duplicate_instance_refuses_cleanly_without_crash(self):
        import secure_apps_gui
        with mock.patch("secure_apps_gui.sa_broker.SingleInstanceGuard.acquire", return_value=False), \
             mock.patch("secure_apps_gui.QMessageBox.information") as mock_info:
            ret = secure_apps_gui.main(["--profile", "obsidian"])
            self.assertEqual(ret, 0)
            mock_info.assert_called_once()
            self.assertIn("Already Running", mock_info.call_args[0][1])


def close_window(window):
    try:
        window.timer.stop()
        window._shutdown_watchdog.stop()
        window._shutdown_state = window.SHUTDOWN_DONE
        window.thread.quit()
        window.thread.wait(1000)
    except Exception:
        pass
    window.close()


class GuiCaseSafetyAndBannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyQt6 is not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import sa_privtask
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-ui-case-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.prev_root = os.environ.get(sa_config.MANAGED_ROOT_ENV)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(self.tmp)
        if self.prev_root is None:
            self.addCleanup(os.environ.pop, sa_config.MANAGED_ROOT_ENV, None)
        else:
            self.addCleanup(os.environ.__setitem__, sa_config.MANAGED_ROOT_ENV, self.prev_root)
        self.prev_pin = os.environ.get(sa_privtask.PIN_PATH_ENV)
        os.environ[sa_privtask.PIN_PATH_ENV] = str(self.tmp / "no-such-pin.json")
        if self.prev_pin is None:
            self.addCleanup(os.environ.pop, sa_privtask.PIN_PATH_ENV, None)
        else:
            self.addCleanup(os.environ.__setitem__, sa_privtask.PIN_PATH_ENV, self.prev_pin)

    def test_case_a_enablelua_disabled_safe_banner(self):
        import secure_apps_gui
        window = secure_apps_gui.SecureAppsWindow(managed_root=str(self.tmp))
        self.addCleanup(close_window, window)
        preflight = {"EnableLUA": False, "broker_integrity": "HIGH", "broker_elevated": True}
        window._last_preflight = preflight
        window._recompute_setup_state()
        self.assertEqual(window._current_next_action, sa_setup.ACTION_ENABLE_PRIVILEGE_SEPARATION)
        self.assertIn("SETUP REQUIRED", window.banner.text())

    def test_case_b_elevated_broker_guidance(self):
        import secure_apps_gui
        window = secure_apps_gui.SecureAppsWindow(managed_root=str(self.tmp))
        self.addCleanup(close_window, window)
        preflight = {"EnableLUA": True, "broker_integrity": "HIGH", "broker_elevated": True}
        window._last_preflight = preflight
        window._recompute_setup_state()
        self.assertEqual(window._current_next_action, sa_setup.ACTION_RUN_NON_ELEVATED)
        self.assertIn("RUN AS STANDARD USER", window.action_card_title.text())

    def test_case_c_medium_broker_advances(self):
        import secure_apps_gui
        window = secure_apps_gui.SecureAppsWindow(managed_root=str(self.tmp))
        self.addCleanup(close_window, window)
        preflight = {
            "EnableLUA": True,
            "broker_integrity": "MEDIUM",
            "broker_elevated": False,
            "scheduled_helper_installed": False,
        }
        window._last_preflight = preflight
        window._recompute_setup_state()
        self.assertEqual(window._current_next_action, sa_setup.ACTION_INSTALL_HELPER)
        self.assertIn("INSTALL PRIVILEGED HELPER", window.action_card_title.text())


class GuiTruthfulLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyQt6 is not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import sa_privtask
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-ui-lock-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(self.tmp)
        os.environ[sa_privtask.PIN_PATH_ENV] = str(self.tmp / "no-pin.json")

    def test_lock_action_shows_locking_until_storage_detached(self):
        import secure_apps_gui
        window = secure_apps_gui.SecureAppsWindow(managed_root=str(self.tmp))
        self.addCleanup(close_window, window)
        with mock.patch("secure_apps_gui.QMessageBox.question",
                        return_value=secure_apps_gui.QMessageBox.StandardButton.Yes):
            window._lock()
        self.assertIn("LOCKING", window.detail["Message"].text())
        self.assertFalse(window.btn_lock.isEnabled())

        # Simulate broker reporting locked state
        status = {
            "profile_id": "obsidian",
            "state": sa_state.LOCKED,
            "app_running": False,
            "session_active": False,
            "storage_state": "DETACHED",
            "session_mode": "default",
            "enrolled": True,
            "auth_interactions": 0,
            "app_pid": None,
            "idle_remaining_seconds": None,
        }
        window._apply_status([status])
        self.assertIn("LOCKED", window.detail["Message"].text())
        self.assertTrue(window.btn_lock.isEnabled())


class GuiLostKeyUserRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyQt6 is not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import sa_privtask
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-ui-keys-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(self.tmp)
        os.environ[sa_privtask.PIN_PATH_ENV] = str(self.tmp / "no-pin.json")

    def test_keys_table_preserves_full_credential_hash_in_user_role(self):
        import secure_apps_gui
        window = secure_apps_gui.SecureAppsWindow(managed_root=str(self.tmp))
        self.addCleanup(close_window, window)
        full_hash = "0123456789abcdef0123456789abcdef"
        keys = [
            {"credential_profile": "key-primary", "credential_id_hash": full_hash,
             "created": "2026-09-17", "provider": "windows-webauthn"},
            {"credential_profile": "key-backup", "credential_id_hash": "fedcba9876543210fedcba9876543210",
             "created": "2026-09-17", "provider": "windows-webauthn"}
        ]
        window._keys_ready("obsidian", keys)
        item = window.table_keys.item(0, 1)
        self.assertTrue(item.text().endswith("..."))
        self.assertEqual(item.data(secure_apps_gui.Qt.ItemDataRole.UserRole), full_hash)

        # Select first row and trigger removal
        window.table_keys.selectRow(0)
        removed_keys = []
        window.requestRemoveKey.connect(lambda pid, fp: removed_keys.append((pid, fp)))
        with mock.patch("secure_apps_gui.QMessageBox.question",
                        return_value=secure_apps_gui.QMessageBox.StandardButton.Yes):
            window._on_remove_lost_key()
        self.assertEqual(removed_keys, [("obsidian", full_hash)])


class GuiAcceptanceWorkerTests(unittest.TestCase):
    def test_acceptance_worker_instantiation_and_signals(self):
        import secure_apps_gui
        worker = secure_apps_gui.AcceptanceWorker("reg.json", "root", "obsidian")
        self.assertEqual(worker.profile_id, "obsidian")
        self.assertEqual(worker.registry_path, "reg.json")
        self.assertEqual(worker.managed_root, "root")
        self.assertTrue(hasattr(worker, "checkDone"))
        self.assertTrue(hasattr(worker, "logMessage"))
        self.assertTrue(hasattr(worker, "pinRequested"))
        self.assertTrue(hasattr(worker, "acceptanceFinished"))


class GuiActionCardRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyQt6 is not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import sa_privtask
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-ui-route-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(self.tmp)
        os.environ[sa_privtask.PIN_PATH_ENV] = str(self.tmp / "no-pin.json")

    def test_routing_calls_appropriate_handler(self):
        import secure_apps_gui
        window = secure_apps_gui.SecureAppsWindow(managed_root=str(self.tmp))
        self.addCleanup(close_window, window)

        actions_and_mocks = [
            (sa_setup.ACTION_ENABLE_PRIVILEGE_SEPARATION, "_on_prep_uac"),
            (sa_setup.ACTION_REBOOT_REQUIRED, "_restart_now"),
            (sa_setup.ACTION_INSTALL_HELPER, "_privileged_action"),
            (sa_setup.ACTION_COMMISSION_HELPER, "_test_silent_helper"),
            (sa_setup.ACTION_RUN_ACCEPTANCE, "_run_acceptance_dialog"),
            (sa_setup.ACTION_IMPORT_APPLICATION, "_on_import_obsidian"),
            (sa_setup.ACTION_MIGRATE_VAULT, "_on_migrate_vault"),
            (sa_setup.ACTION_READY, "_open"),
        ]

        for action, method_name in actions_and_mocks:
            window._current_next_action = action
            with mock.patch.object(window, method_name) as mock_method:
                window._run_current_next_action()
                mock_method.assert_called_once()

    def test_import_uses_selected_folder_and_respects_cancellation(self):
        import secure_apps_gui
        window = secure_apps_gui.SecureAppsWindow(managed_root=str(self.tmp))
        self.addCleanup(close_window, window)
        window.requestImportApp.disconnect()
        imports = []
        window.requestImportApp.connect(lambda profile, path: imports.append((profile, path)))
        source = str(self.tmp / "Portable Obsidian ü")
        yes = secure_apps_gui.QMessageBox.StandardButton.Yes
        no = secure_apps_gui.QMessageBox.StandardButton.No
        with mock.patch.object(window, "_require_setup_action", return_value=True):
            for selected, answer, expected_count in [("", yes, 0), (source, no, 0), (source, yes, 1)]:
                with mock.patch.object(secure_apps_gui.QFileDialog, "getExistingDirectory",
                                       return_value=selected), \
                     mock.patch.object(secure_apps_gui.QMessageBox, "question",
                                       return_value=answer) as confirm:
                    window._on_import_obsidian()
                    self.assertEqual(len(imports), expected_count)
                    if not selected:
                        confirm.assert_not_called()
                    else:
                        self.assertIn(source, confirm.call_args.args[2])
                        self.assertEqual(confirm.call_args.args[4], no)
        self.assertEqual(imports, [(window._selected, source)])

    def test_import_refuses_before_opening_picker_when_setup_is_blocked(self):
        import secure_apps_gui
        window = secure_apps_gui.SecureAppsWindow(managed_root=str(self.tmp))
        self.addCleanup(close_window, window)
        with mock.patch.object(window, "_require_setup_action", return_value=False), \
             mock.patch.object(secure_apps_gui.QFileDialog, "getExistingDirectory") as picker:
            window._on_import_obsidian()
            picker.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
# SRC-026 Secure Apps GUI closure wave.
#
# Every class below is a regression for a defect that was shipped: the
# acceptance worker auto-approved security questions (``confirm_cb`` returned
# ``True`` unconditionally), ``pauseRequested`` was emitted into nothing, the
# acceptance table was derived from broad gate flags, every setup button was
# clickable in every state, the window initialised ``_reboot_pending = False``
# instead of reading the durable marker, an ordinary status tick could measure
# the privileged picture on the GUI thread, and Install/Prepare payloads were
# cached as preflight reports. ``secure_apps.pyw`` was also a byte-identical
# copy of ``secure_apps_gui.py``.


class AcceptanceCancellationTests(unittest.TestCase):
    """SRC-027 CORE-001: a running acceptance can be cancelled, never approved."""

    @classmethod
    def setUpClass(cls):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyQt6 is not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _worker(self):
        import secure_apps_gui
        worker = secure_apps_gui.AcceptanceWorker("reg.json", "root", "obsidian")
        worker.confirm_timeout = worker.pause_timeout = worker.pin_timeout = 30
        return worker

    def test_cancel_wakes_a_pending_question_as_a_refusal(self):
        import threading
        import time as _time
        worker = self._worker()
        answer = {}
        thread = threading.Thread(
            target=lambda: answer.setdefault("v", worker._request_confirmation("approve?")))
        thread.start()
        deadline = _time.monotonic() + 5
        while worker._waiting is None and _time.monotonic() < deadline:
            _time.sleep(0.01)
        started = _time.monotonic()
        worker.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "a cancelled question kept the worker waiting")
        self.assertLess(_time.monotonic() - started, 2.0)
        self.assertIs(answer["v"], False)

    def test_after_cancel_no_round_trip_waits_or_approves(self):
        import sa_acceptance_runner
        worker = self._worker()
        emitted = []
        worker.confirmRequested.connect(lambda q, t: emitted.append(t))
        worker.cancel()
        self.assertFalse(worker._request_confirmation("approve?"))
        self.assertIsNone(worker._request_pin("pin?"))
        with self.assertRaises(sa_acceptance_runner.AbortAcceptance):
            worker._request_pause("unplug the key")
        self.assertEqual(emitted, [])

    def test_a_late_yes_after_cancel_is_still_a_refusal(self):
        import threading
        import time as _time
        from PyQt6.QtCore import Qt
        worker = self._worker()
        queues = []
        worker.confirmRequested.connect(lambda q, t: queues.append(q),
                                        Qt.ConnectionType.DirectConnection)
        answer = {}
        thread = threading.Thread(
            target=lambda: answer.setdefault("v", worker._request_confirmation("approve?")))
        thread.start()
        deadline = _time.monotonic() + 5
        while not queues and _time.monotonic() < deadline:
            _time.sleep(0.01)
        worker._cancelled.set()          # cancelled, then a racing "yes" lands
        queues[0].put(True)
        thread.join(5)
        self.assertIs(answer["v"], False)

    def test_closing_during_acceptance_cancels_and_defers_the_shutdown(self):
        import secure_apps_gui
        import sa_privtask
        tmp = Path(tempfile.mkdtemp(prefix="sa-ui-cancel-"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(tmp)
        os.environ[sa_privtask.PIN_PATH_ENV] = str(tmp / "no-pin.json")
        window = secure_apps_gui.SecureAppsWindow(managed_root=str(tmp))
        self.addCleanup(close_window, window)
        fake = mock.Mock()
        fake.isRunning.return_value = True
        window._acc_worker = fake
        state = window._shutdown_state
        with mock.patch.object(window, "requestShutdown") as shutdown_signal:
            window.close()
            shutdown_signal.emit.assert_not_called()
        fake.cancel.assert_called_once_with()
        self.assertEqual(window._shutdown_state, state)
        self.assertTrue(window._close_after_acceptance)
        fake.isRunning.return_value = False
        window._acc_worker = None


class AcceptanceOperatorGateTests(unittest.TestCase):
    """A security question and a physical step need a real person's answer."""

    def setUp(self):
        self.module = importlib.import_module("secure_apps_gui")

    def _worker(self, timeout=0.05):
        worker = self.module.AcceptanceWorker("reg.json", "root", "obsidian")
        worker.confirm_timeout = timeout
        worker.pause_timeout = timeout
        return worker

    # -- confirmations -------------------------------------------------
    def test_an_unanswered_confirmation_is_never_an_approval(self):
        worker = self._worker()
        self.assertFalse(
            worker.callbacks()["confirm"]("Did the helper start WITHOUT UAC consent?"))

    def test_a_refused_confirmation_is_a_no(self):
        worker = self._worker(timeout=1.0)
        worker.confirmRequested.connect(lambda q, text: q.put(False))
        self.assertFalse(worker.callbacks()["confirm"]("Proceed?"))

    def test_an_approved_confirmation_is_the_operators_yes(self):
        worker = self._worker(timeout=1.0)
        seen = []
        worker.confirmRequested.connect(
            lambda q, text: (seen.append(text), q.put(True)))
        self.assertTrue(worker.callbacks()["confirm"]("Proceed with the wrong PIN test?"))
        self.assertEqual(seen, ["Proceed with the wrong PIN test?"])

    def test_the_gui_has_no_auto_approving_confirmation_callback(self):
        source = (SUBSYSTEM / "secure_apps_gui.py").read_text(encoding="utf-8")
        # The shipped shape was: def confirm_cb(prompt_text): return True
        self.assertNotRegex(source, r"def confirm_cb\(prompt_text\):\s*\n\s*return True")
        self.assertIn("confirmRequested = pyqtSignal(object, str)", source)
        self.assertIn("def _request_confirmation", source)
        self.assertIn("confirm_callback=callbacks[\"confirm\"]", source)

    def test_wrong_pin_requires_consent_immediately_before_it_runs(self):
        text = (SUBSYSTEM / "sa_acceptance_runner.py").read_text(encoding="utf-8")
        gate = text.index("proceed = self.confirm(")
        refusal = text.index('run.skip("F09"', gate)
        submit = text.index("self.pin = wrong_pin", gate)
        self.assertLess(gate, refusal, "the wrong-PIN step must ask first")
        self.assertLess(refusal, submit, "a refusal must prevent the wrong PIN")

        acceptance = sa_acceptance_runner.DisposableAcceptance(
            mock.Mock(), echo=lambda _line: None, confirm_callback=lambda _prompt: False)
        self.assertFalse(acceptance.confirm("WRONG PIN step: proceed?"))

    def test_a_declined_wrong_pin_check_is_never_green(self):
        run = sa_acceptance_runner.AcceptanceRun(echo=None)
        for check_id, _flag, _text in sa_acceptance_runner.CHECKS:
            if check_id == "F09":
                run.skip(check_id, "cancelled by the operator")
            else:
                run.check(check_id, True)
        self.assertFalse(run.gates()["fido2_hardware_accepted"])
        self.assertFalse(run.complete())

    # -- physical steps -------------------------------------------------
    def test_a_physical_step_blocks_until_the_operator_acknowledges(self):
        worker = self._worker(timeout=1.0)
        seen = []
        worker.pauseRequested.connect(lambda q, text: (seen.append(text), q.put(True)))
        self.assertTrue(
            worker.callbacks()["pause"]("NO KEY step: UNPLUG the security key now."))
        self.assertEqual(seen, ["NO KEY step: UNPLUG the security key now."])

    def test_an_unanswered_physical_step_never_continues_silently(self):
        worker = self._worker()
        with self.assertRaises(sa_acceptance_runner.AbortAcceptance):
            worker.callbacks()["pause"]("NO KEY step: UNPLUG the security key now.")

    def test_a_cancelled_physical_step_stops_the_sequence(self):
        worker = self._worker(timeout=1.0)
        worker.pauseRequested.connect(lambda q, text: q.put(False))
        with self.assertRaises(sa_acceptance_runner.AbortAcceptance):
            worker.callbacks()["pause"]("Plug the security key back in.")

    def test_an_aborted_pause_leaves_an_incomplete_acceptance(self):
        run = sa_acceptance_runner.AcceptanceRun(echo=None)
        run.check("F01", True)
        run.abort("the operator did not complete the physical step")
        self.assertFalse(run.complete())
        self.assertIn("F09", run.not_passed())

    def test_the_check_outcome_callback_reports_skip_apart_from_fail(self):
        outcomes = []
        run = sa_acceptance_runner.AcceptanceRun(
            echo=None, outcome_callback=lambda cid, out, det: outcomes.append((cid, out)))
        run.check("F01", True)
        run.check("F02", False)
        run.skip("F03", "not run")
        self.assertEqual(outcomes, [("F01", "PASS"), ("F02", "FAIL"), ("F03", "SKIP")])


class GuiWindowBase(unittest.TestCase):
    """A real offscreen window against a throwaway managed root."""

    @classmethod
    def setUpClass(cls):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyQt6 is not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import sa_privtask
        self.module = importlib.import_module("secure_apps_gui")
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-ui-closure-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.prev_root = os.environ.get(sa_config.MANAGED_ROOT_ENV)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(self.tmp)
        if self.prev_root is None:
            self.addCleanup(os.environ.pop, sa_config.MANAGED_ROOT_ENV, None)
        else:
            self.addCleanup(os.environ.__setitem__, sa_config.MANAGED_ROOT_ENV, self.prev_root)
        self.prev_pin = os.environ.get(sa_privtask.PIN_PATH_ENV)
        os.environ[sa_privtask.PIN_PATH_ENV] = str(self.tmp / "no-such-pin.json")
        if self.prev_pin is None:
            self.addCleanup(os.environ.pop, sa_privtask.PIN_PATH_ENV, None)
        else:
            self.addCleanup(os.environ.__setitem__, sa_privtask.PIN_PATH_ENV, self.prev_pin)

    def window(self):
        window = self.module.SecureAppsWindow(managed_root=str(self.tmp))
        self.addCleanup(close_window, window)
        return window

    def registry(self):
        return sa_config.load_registry(sa_config.default_registry_path(),
                                       managed_root=str(self.tmp))


class GuiAcceptanceRoundTripTests(GuiWindowBase):
    """The operator's answers reach the blocked worker, through the window."""

    def _worker(self, window):
        from PyQt6.QtWidgets import QMessageBox
        window._current_next_action = sa_setup.ACTION_RUN_ACCEPTANCE
        with mock.patch.object(self.module.AcceptanceWorker, "start"), \
             mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes):
            window._run_acceptance_dialog()
        return window._acc_worker

    def test_the_pause_requested_signal_is_connected_to_the_window(self):
        from PyQt6.QtWidgets import QDialog
        window = self.window()
        worker = self._worker(window)
        self.assertIsNotNone(worker)

        for answer, expected in ((QDialog.DialogCode.Accepted, True),
                                 (QDialog.DialogCode.Rejected, False)):
            response = queue.Queue(maxsize=1)
            with mock.patch.object(self.module.PhysicalStepDialog, "exec",
                                   return_value=answer):
                worker.pauseRequested.emit(
                    response, "NO KEY step: UNPLUG the security key now.")
            self.assertEqual(response.get_nowait(), expected)

    def test_the_confirm_requested_signal_is_connected_to_the_window(self):
        from PyQt6.QtWidgets import QDialog
        window = self.window()
        worker = self._worker(window)

        for answer, expected in ((QDialog.DialogCode.Accepted, True),
                                 (QDialog.DialogCode.Rejected, False)):
            response = queue.Queue(maxsize=1)
            with mock.patch.object(self.module.AcceptanceConfirmDialog, "exec",
                                   return_value=answer):
                worker.confirmRequested.emit(
                    response, "Did the helper start WITHOUT any Windows consent "
                              "(UAC) prompt?")
            self.assertEqual(response.get_nowait(), expected)

    def test_the_confirmation_dialog_defaults_to_refusing(self):
        window = self.window()
        dialog = self.module.AcceptanceConfirmDialog("Proceed?", window)
        self.assertTrue(dialog.btn_refuse.isDefault())
        self.assertFalse(dialog.btn_approve.isDefault())

    def test_the_physical_step_dialog_has_continue_and_a_safe_cancel(self):
        window = self.window()
        dialog = self.module.PhysicalStepDialog(
            "NO KEY step: UNPLUG the security key now.", window)
        self.assertEqual(dialog.btn_continue.text(), "Continue")
        self.assertIn("Cancel", dialog.btn_cancel.text())


class GuiAcceptanceTableTests(GuiWindowBase):
    """The table is the runner's check list, painted as the run reaches it."""

    def status(self, window, check_id):
        row = self.module.ACCEPTANCE_CHECK_ROW[check_id]
        return window.table_acceptance.item(row, 1).text()

    def test_the_table_has_one_row_per_real_runner_check(self):
        from PyQt6.QtCore import Qt
        window = self.window()
        self.assertEqual(window.table_acceptance.rowCount(),
                         len(sa_acceptance_runner.CHECKS))
        for row, (check_id, _flag, _text) in enumerate(sa_acceptance_runner.CHECKS):
            self.assertEqual(
                window.table_acceptance.item(row, 0).data(Qt.ItemDataRole.UserRole),
                check_id)

    def test_each_real_check_paints_its_own_state(self):
        window = self.window()
        window._reset_acceptance_table()
        window._advance_acceptance_cursor()
        self.assertEqual(self.status(window, "F01"), "RUNNING")
        window._acceptance_check_outcome("F01", "PASS", "transport ok")
        self.assertEqual(self.status(window, "F01"), "PASS")
        self.assertEqual(self.status(window, "F02"), "RUNNING")
        window._acceptance_check_outcome("F02", "FAIL", "no authenticator")
        window._acceptance_check_outcome("F03", "SKIP", "not run")
        self.assertEqual(self.status(window, "F02"), "FAIL")
        self.assertEqual(self.status(window, "F03"), "SKIP")
        # A later PASS can never repaint a check that failed.
        window._acceptance_check_outcome("F02", "PASS", "re-run")
        self.assertEqual(self.status(window, "F02"), "FAIL")

    def test_the_check_cursor_never_rewinds_to_the_start(self):
        window = self.window()
        window._reset_acceptance_table()
        window._advance_acceptance_cursor()
        for check_id, _flag, _text in sa_acceptance_runner.CHECKS:
            window._acceptance_check_outcome(check_id, "PASS", "")
        for check_id, _label in self.module.ACCEPTANCE_CHECKS:
            self.assertEqual(self.status(window, check_id), "PASS")
        self.assertIsNone(window._acceptance_cursor_id)

    def test_the_table_is_not_derived_from_the_broad_gate_flags(self):
        window = self.window()
        window._reset_acceptance_table()
        green_record = mock.Mock()
        green_record.ok = True
        green_record.record = {flag: True for flag in sa_acceptance.GATE_FLAGS}
        with mock.patch.object(self.module.sa_acceptance, "evaluate",
                               return_value=green_record):
            window._on_acceptance_finished()
        # The durable record says every gate is green, but nothing was measured
        # in this window, so no per-check row may claim PASS.
        for check_id, _label in self.module.ACCEPTANCE_CHECKS:
            self.assertNotEqual(self.status(window, check_id), "PASS")
        self.assertIn("TOKENS", window.lbl_acc_tokens.text())

    def test_an_unknown_check_is_reported_not_painted(self):
        window = self.window()
        window._reset_acceptance_table()
        window._acceptance_check_outcome("Z99", "PASS", "")
        self.assertIn("unknown check", window.log.toPlainText())


class GuiSetupButtonGatingTests(GuiWindowBase):
    """Only the step the state machine asks for is clickable."""

    GATED = ("btn_prep_uac", "btn_install_task", "btn_test_helper", "btn_probe_key",
             "btn_run_acceptance", "btn_import_obsidian", "btn_enroll_vault",
             "btn_migrate_vault")

    MEDIUM = {"EnableLUA": True, "broker_integrity": "MEDIUM", "broker_elevated": False,
              "scheduled_helper_installed": True, "scheduled_helper_definition_valid": True}

    def enabled(self, window):
        return {name: getattr(window, name).isEnabled() for name in self.GATED}

    def test_enablelua_false_enables_only_privilege_separation(self):
        window = self.window()
        window._last_preflight = {"EnableLUA": False, "broker_integrity": "HIGH",
                                  "broker_elevated": True}
        window._recompute_setup_state()
        self.assertEqual(window._current_next_action,
                         sa_setup.ACTION_ENABLE_PRIVILEGE_SEPARATION)
        state = self.enabled(window)
        self.assertTrue(state["btn_prep_uac"])
        for name in self.GATED:
            if name != "btn_prep_uac":
                self.assertFalse(state[name], "%s must not be reachable" % name)

    def test_exactly_one_button_is_enabled_per_state(self):
        for preflight, expected in (
            ({"EnableLUA": True, "broker_integrity": "MEDIUM", "broker_elevated": False,
              "scheduled_helper_installed": False}, sa_setup.ACTION_INSTALL_HELPER),
            (self.MEDIUM, sa_setup.ACTION_COMMISSION_HELPER),
        ):
            window = self.window()
            window._last_preflight = dict(preflight)
            window._recompute_setup_state()
            self.assertEqual(window._current_next_action, expected)
            self.assertEqual(sum(1 for on in self.enabled(window).values() if on), 1)
            close_window(window)

    def test_every_button_is_disabled_while_windows_must_restart(self):
        sa_setup.mark_reboot_pending(self.registry().state_dir, True)
        window = self.window()
        window._last_preflight = {"EnableLUA": False, "broker_integrity": "HIGH",
                                  "broker_elevated": True}
        window._recompute_setup_state()
        self.assertEqual(window._current_next_action, sa_setup.ACTION_REBOOT_REQUIRED)
        self.assertFalse(any(self.enabled(window).values()))

    def test_enrollment_is_not_gui_reachable_before_durable_acceptance(self):
        window = self.window()
        window._last_preflight = dict(self.MEDIUM)
        window._commissioned = True
        window._last_probe = {"available": True, "hmac_secret": True,
                              "transport": "ELEVATED_CTAP_HELPER"}
        unproven = mock.Mock()
        unproven.ok = False
        unproven.record = {}
        window._last_acceptance = unproven
        window._recompute_setup_state()
        self.assertEqual(window._current_next_action, sa_setup.ACTION_RUN_ACCEPTANCE)
        self.assertFalse(window.btn_enroll_vault.isEnabled())
        self.assertFalse(window.btn_migrate_vault.isEnabled())

        emitted = []
        window.requestEnroll.connect(lambda pid, additional: emitted.append(pid))
        window._on_enroll_vault()
        self.assertEqual(emitted, [], "enrollment must fail closed when not authorised")
        self.assertIn("refused", window.log.toPlainText())

    def test_completed_and_future_steps_stay_visible_but_disabled(self):
        window = self.window()
        window._last_preflight = {"EnableLUA": False, "broker_integrity": "HIGH",
                                  "broker_elevated": True}
        window._recompute_setup_state()
        self.assertFalse(window.btn_migrate_vault.isHidden())
        self.assertFalse(window.btn_migrate_vault.isEnabled())


class GuiRebootDurabilityTests(GuiWindowBase):
    """The durable marker is the only restart truth this surface has."""

    MEDIUM = {"EnableLUA": True, "broker_integrity": "MEDIUM", "broker_elevated": False,
              "scheduled_helper_installed": True, "scheduled_helper_definition_valid": True}

    def test_the_window_starts_with_an_unresolved_restart_state(self):
        self.assertIsNone(self.window()._reboot_pending)

    def test_restart_later_survives_a_gui_restart(self):
        first = self.window()
        first._last_preflight = dict(self.MEDIUM)
        first._recompute_setup_state()
        self.assertNotEqual(first._current_next_action, sa_setup.ACTION_REBOOT_REQUIRED)

        with mock.patch.object(self.module.QMessageBox, "information"):
            first._restart_later()
        self.assertEqual(first._current_next_action, sa_setup.ACTION_REBOOT_REQUIRED)
        close_window(first)

        # The app is closed and reopened before the restart: the choice must be
        # remembered by the durable marker, not by the window that made it.
        second = self.window()
        second._last_preflight = dict(self.MEDIUM)
        second._recompute_setup_state()
        self.assertEqual(second._current_next_action, sa_setup.ACTION_REBOOT_REQUIRED)
        self.assertEqual(second.banner.text(), "SECURE APPS: REBOOT REQUIRED")

    def test_a_durable_restart_needs_no_preflight_to_be_remembered(self):
        sa_setup.mark_reboot_pending(self.registry().state_dir, True)
        main_thread = threading.current_thread()
        calls = []
        real_preflight = sa_privtask.preflight

        def spy(*args, **kwargs):
            calls.append(threading.current_thread())
            return real_preflight(*args, **kwargs)

        window = self.window()
        self.assertIsNone(window._last_preflight)
        with mock.patch.object(self.module.sa_privtask, "preflight", side_effect=spy):
            window._recompute_setup_state()
        self.assertEqual([t for t in calls if t is main_thread], [],
                         "the durable marker needed a measurement to be read")
        self.assertTrue(window._reboot_pending)
        self.assertEqual(window._current_next_action, sa_setup.ACTION_REBOOT_REQUIRED)
        self.assertEqual(window.checklist_table.item(0, 1).text(), sa_setup.STEP_REBOOT_REQUIRED)
        self.assertFalse(window.btn_prep_uac.isEnabled())

    def _marker_from_a_previous_boot(self):
        """A durable marker whose boot identity is older than this boot."""
        state_dir = self.registry().state_dir
        sa_setup.mark_reboot_pending(state_dir, True)
        path = os.path.join(state_dir, sa_setup.REBOOT_MARKER_FILENAME)
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["boot_time"] = float(data["boot_time"]) - 10 ** 7
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def test_a_proven_reboot_clears_the_marker_and_advances_the_wizard(self):
        path = self._marker_from_a_previous_boot()
        window = self.window()
        # CASE B: the marker exists, the boot identity proves Windows really
        # restarted, and the fresh measurement agrees (EnableLUA=1, MEDIUM
        # broker): the marker is retired and the wizard advances.
        window._privileged_status_ready({
            "EnableLUA": True, "broker_integrity": "MEDIUM", "broker_elevated": False,
            "scheduled_helper_installed": False,
        })
        self.assertFalse(window._reboot_pending)
        self.assertEqual(window._current_next_action, sa_setup.ACTION_INSTALL_HELPER)
        self.assertFalse(os.path.isfile(path))


class RebootMarkerTests(unittest.TestCase):
    """The marker itself, without any Qt in the way."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-reboot-marker-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.state = str(self.tmp)

    def _marker(self, uptime_ms):
        path = os.path.join(self.state, sa_setup.REBOOT_MARKER_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"reboot_pending": True, "marked_at": "nt", "uptime_ms": uptime_ms},
                      handle)
        return path

    def test_the_marker_survives_while_the_machine_has_not_restarted(self):
        self._marker(sa_setup._system_uptime_ms())
        self.assertTrue(sa_setup.is_reboot_pending(self.state))

    def test_a_lower_uptime_proves_a_real_restart(self):
        path = self._marker((sa_setup._system_uptime_ms() or 1000) + 10 ** 9)
        self.assertFalse(sa_setup.is_reboot_pending(self.state))
        self.assertFalse(os.path.isfile(path))

    def test_a_marker_from_this_boot_survives_a_medium_broker(self):
        """CASE A: prepared but NOT restarted must still demand the restart.

        ``EnableLUA=1`` with a medium broker is exactly what a prepared,
        un-restarted host reports, so that pair can never retire a marker
        written during the current boot.
        """
        sa_setup.mark_reboot_pending(self.state, True)
        self.assertTrue(sa_setup.is_reboot_pending(
            self.state, {"EnableLUA": True, "broker_elevated": False}))
        self.assertTrue(os.path.isfile(
            os.path.join(self.state, sa_setup.REBOOT_MARKER_FILENAME)))

    def test_proven_privilege_separation_clears_a_legacy_marker(self):
        # A marker with no boot identity at all keeps the old fallback.
        self._marker(sa_setup._system_uptime_ms())
        report = {"EnableLUA": True, "broker_elevated": False}
        self.assertFalse(sa_setup.is_reboot_pending(self.state, report))
        self.assertFalse(os.path.isfile(
            os.path.join(self.state, sa_setup.REBOOT_MARKER_FILENAME)))

    def test_a_newer_boot_clears_a_marker_even_without_an_uptime_regression(self):
        """Off for a week, back later: uptime is LARGER, restart is still real."""
        path = self._marker((sa_setup._system_uptime_ms() or 1000) // 2)
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["boot_time"] = time.time() - 10 ** 7
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        self.assertFalse(sa_setup.is_reboot_pending(self.state))
        self.assertFalse(os.path.isfile(path))

    def test_an_elevated_broker_does_not_clear_the_marker(self):
        self._marker(sa_setup._system_uptime_ms())
        report = {"EnableLUA": True, "broker_elevated": True}
        self.assertTrue(sa_setup.is_reboot_pending(self.state, report))


class GuiAsyncPreflightTests(GuiWindowBase):
    """Measuring the privileged picture is the worker's job, always."""

    MEDIUM = {"EnableLUA": True, "broker_integrity": "MEDIUM", "broker_elevated": False,
              "scheduled_helper_installed": True, "scheduled_helper_definition_valid": True}

    def status_row(self):
        return {"profile_id": "obsidian", "label": "Obsidian", "state": sa_state.LOCKED,
                "session_active": False, "session_idle_seconds": 1, "mount_path": "X:\\"}

    def test_ordinary_status_ticks_never_run_privileged_preflight(self):
        """A 2.5-second tick must not measure anything, let alone synchronously.

        The worker's own startup measurement is allowed (and expected) on the
        worker thread; what must never happen is a tick reaching the heavy
        ``sa_privtask.preflight`` from the thread that draws the window.
        """
        main_thread = threading.current_thread()
        calls = []
        real_preflight = sa_privtask.preflight

        def spy(*args, **kwargs):
            calls.append(threading.current_thread())
            return real_preflight(*args, **kwargs)

        window = self.window()
        window._last_preflight = dict(self.MEDIUM)
        with mock.patch.object(self.module.sa_privtask, "preflight", side_effect=spy):
            for _ in range(5):
                window._apply_status([self.status_row()])
                window._recompute_setup_state()
        self.assertEqual([t for t in calls if t is main_thread], [],
                         "a GUI status tick measured the privileged picture")
        self.assertEqual(window._current_next_action, sa_setup.ACTION_COMMISSION_HELPER)

    def test_the_gui_never_asks_the_setup_engine_to_measure(self):
        window = self.window()
        window._last_preflight = dict(self.MEDIUM)
        real_next = sa_setup.get_next_setup_action
        real_checklist = sa_setup.get_setup_checklist
        reports = []

        def spy_next(*args, **kwargs):
            reports.append(kwargs.get("preflight_report"))
            return real_next(*args, **kwargs)

        def spy_checklist(*args, **kwargs):
            reports.append(kwargs.get("preflight_report"))
            return real_checklist(*args, **kwargs)

        with mock.patch.object(sa_setup, "get_next_setup_action", side_effect=spy_next), \
             mock.patch.object(sa_setup, "get_setup_checklist", side_effect=spy_checklist):
            for _ in range(3):
                window._apply_status([self.status_row()])
        self.assertTrue(reports)
        for report in reports:
            self.assertIsNotNone(report, "the engine would measure it on the GUI thread")

    def test_without_a_measurement_the_window_says_evaluating(self):
        main_thread = threading.current_thread()
        calls = []
        real_preflight = sa_privtask.preflight

        def spy(*args, **kwargs):
            calls.append(threading.current_thread())
            return real_preflight(*args, **kwargs)

        window = self.window()
        with mock.patch.object(self.module.sa_privtask, "preflight", side_effect=spy):
            window._apply_status([self.status_row()])
        # No measurement has been delivered to the window, so it says so.
        self.assertIsNone(window._last_preflight)
        self.assertEqual([t for t in calls if t is main_thread], [])
        self.assertEqual(window.banner.text(), "SECURE APPS: EVALUATING...")
        self.assertIn("EVALUATING", window.action_card_title.text())
        self.assertFalse(any(getattr(window, name).isEnabled()
                             for name, _action in self.module.SETUP_BUTTON_ACTIONS))

    def test_the_measurement_happens_on_the_worker_thread_not_the_gui_thread(self):
        main_thread = threading.current_thread()
        seen = []
        real_preflight = sa_privtask.preflight

        def spy(*args, **kwargs):
            # Record the thread, then measure for real: a stubbed report would
            # be a lie the production broker might act on.
            seen.append(threading.current_thread())
            return real_preflight(*args, **kwargs)

        # Nothing may open a modal dialog while this loop pumps events, or the
        # test would block instead of failing.
        with mock.patch.object(self.module.sa_privtask, "preflight", side_effect=spy), \
             mock.patch.object(self.module.QMessageBox, "critical"), \
             mock.patch.object(self.module.QMessageBox, "warning"), \
             mock.patch.object(self.module.QMessageBox, "information"), \
             mock.patch.object(self.module.QMessageBox, "question",
                               return_value=self.module.QMessageBox.StandardButton.No):
            window = self.window()
            deadline = time.monotonic() + 15.0
            while not seen and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            for _ in range(20):
                self.app.processEvents()
                time.sleep(0.005)
        self.assertTrue(seen, "the worker never measured the privileged picture")
        for thread in seen:
            self.assertIsNot(thread, main_thread,
                             "the heavyweight measurement ran on the GUI thread")
        self.assertIsNotNone(window._last_preflight)


class GuiPrivilegedResultSeparationTests(GuiWindowBase):
    """A measured report and an operation result are different facts."""

    def test_the_ambiguous_ready_signal_is_gone(self):
        self.assertFalse(hasattr(self.module.BrokerWorker, "privilegedReady"))
        self.assertFalse(hasattr(self.module.SecureAppsWindow, "_privileged_ready"))

    def test_only_a_measured_report_becomes_the_cached_preflight(self):
        window = self.window()
        measured = {"EnableLUA": True, "broker_integrity": "MEDIUM", "broker_elevated": False}
        window._privileged_status_ready(measured)
        self.assertIs(window._last_preflight, measured)
        with mock.patch.object(self.module.QMessageBox, "information"), \
             mock.patch.object(self.module.QMessageBox, "warning"):
            window._privileged_action_result({"ok": True, "action": "install",
                                              "message": "installed"})
        self.assertIs(window._last_preflight, measured,
                      "an operation payload must never be cached as a report")

    def test_an_operation_result_never_fabricates_a_preflight(self):
        window = self.window()
        with mock.patch.object(self.module.QMessageBox, "warning"):
            window._privileged_action_result({"ok": False, "action": "prepare-uac",
                                              "message": "refused"})
        self.assertIsNone(window._last_preflight)

    def test_a_mutation_asks_the_worker_for_a_fresh_measurement(self):
        import inspect
        source = inspect.getsource(self.module.BrokerWorker.privilegedAction)
        self.assertIn("self.privilegedStatus()", source)
        self.assertIn("self.privilegedActionResult.emit(payload)", source)
        self.assertNotIn("privilegedReady", source)

    def test_status_and_action_have_distinct_consumers(self):
        source = (SUBSYSTEM / "secure_apps_gui.py").read_text(encoding="utf-8")
        self.assertIn("privilegedStatusReady.connect(self._privileged_status_ready)", source)
        self.assertIn("privilegedActionResult.connect(self._privileged_action_result)", source)
        self.assertNotIn("privilegedReady", source)
        self.assertNotIn("privilegedReady",
                         (SUBSYSTEM / "secure_apps.pyw").read_text(encoding="utf-8"))

    def test_a_tamper_error_re_measures_the_privileged_picture(self):
        window = self.window()
        window._last_preflight = {"EnableLUA": True, "broker_integrity": "MEDIUM",
                                  "broker_elevated": False}
        requested = []
        window.requestPrivilegedStatus.connect(lambda: requested.append(True))
        with mock.patch.object(self.module.QMessageBox, "warning"):
            window._apply_operation({"ok": False, "profile_id": "obsidian",
                                     "error_category": "privileged_task_tampered",
                                     "reason": "definition mismatch"})
        self.assertEqual(len(requested), 1)


class GuiLauncherContractTests(unittest.TestCase):
    """One GUI implementation, and a launcher that does nothing but launch."""

    def test_secure_apps_pyw_is_a_thin_launcher(self):
        text = (SUBSYSTEM / "secure_apps.pyw").read_text(encoding="utf-8")
        self.assertIn("import secure_apps_gui", text)
        self.assertIn("secure_apps_gui.main()", text)
        self.assertIn("sys.path.insert", text)
        for banned in ("class ", "QSS", "QPushButton", "QMainWindow", "def _build_setup_tab"):
            self.assertNotIn(banned, text)
        self.assertLess(len(text.splitlines()), 40)

    def test_the_launcher_delegates_to_the_gui_main(self):
        module = importlib.import_module("secure_apps_gui")
        source = (SUBSYSTEM / "secure_apps.pyw").read_text(encoding="utf-8")
        launcher = SUBSYSTEM / "secure_apps.pyw"
        with mock.patch.object(module, "main", return_value=0) as main:
            with self.assertRaises(SystemExit) as ctx:
                exec(compile(source, str(launcher), "exec"),
                     {"__name__": "__main__", "__file__": str(launcher)})
        main.assert_called_once()
        self.assertEqual(ctx.exception.code, 0)

    def test_secure_apps_gui_is_the_only_gui_implementation(self):
        module = importlib.import_module("secure_apps_gui")
        self.assertTrue(hasattr(module, "SecureAppsWindow"))
        self.assertTrue(hasattr(module, "QSS"))
        launcher = (SUBSYSTEM / "secure_apps.pyw").read_bytes()
        implementation = (SUBSYSTEM / "secure_apps_gui.py").read_bytes()
        self.assertNotEqual(hashlib.sha256(launcher).hexdigest(),
                            hashlib.sha256(implementation).hexdigest())
        self.assertGreater(len(implementation.splitlines()), 1000)

    def test_no_test_loads_the_launcher_as_the_gui(self):
        # A by-path load of secure_apps.pyw is how the two copies stayed
        # byte-identical for so long: the tests exercised a file no user runs.
        loader = re.compile(
            r"spec_from_file_location\(\s*[^)]*secure_apps\.pyw", re.DOTALL)
        offenders = [path.name
                     for path in sorted(Path(__file__).resolve().parent.glob("test_*.py"))
                     if loader.search(path.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])


class GuiTickCoalescingAndCacheTests(GuiWindowBase):
    """PERF-002 and PERF-003: Setup state caching and tick coalescing contract."""

    MEDIUM = {"EnableLUA": True, "broker_integrity": "MEDIUM", "broker_elevated": False,
              "scheduled_helper_installed": True, "scheduled_helper_definition_valid": True}

    def status_row(self):
        return {"profile_id": "obsidian", "label": "Obsidian", "state": sa_state.LOCKED,
                "session_active": False, "session_idle_seconds": 1, "mount_path": "X:\\"}

    def test_generic_status_emission_does_not_acknowledge_pending_tick(self):
        window = self.window()
        window._tick_pending = True
        window._tick_coalesced = False

        # An out-of-band status update (e.g. push_status from operation) arrives
        window._apply_status([self.status_row()])
        self.assertTrue(window._tick_pending, "status emission must not acknowledge pending tick")

        # Dedicated tickCompleted signal acknowledges tick
        window._tick_acknowledged()
        self.assertFalse(window._tick_pending, "tickCompleted must acknowledge pending tick")

    def test_tick_coalescing_and_catchup_execution(self):
        window = self.window()
        window._tick_pending = False
        window._tick_coalesced = False

        # First periodic timer fire triggers tick request
        emitted_ticks = []
        window.requestTick.connect(lambda: emitted_ticks.append(True))
        window._on_periodic_timer()
        self.assertTrue(window._tick_pending)
        self.assertFalse(window._tick_coalesced)
        self.assertEqual(len(emitted_ticks), 1)

        # Second periodic timer fire while tick is pending coalesces instead of piling up
        window._on_periodic_timer()
        self.assertTrue(window._tick_pending)
        self.assertTrue(window._tick_coalesced)
        self.assertEqual(len(emitted_ticks), 1)

        # When previous tick completes, coalesced tick fires immediately
        window._tick_acknowledged()
        self.assertTrue(window._tick_pending)
        self.assertFalse(window._tick_coalesced)
        self.assertEqual(len(emitted_ticks), 2)

        # Next acknowledgement with no coalesced tick leaves tick_pending False
        window._tick_acknowledged()
        self.assertFalse(window._tick_pending)
        self.assertEqual(len(emitted_ticks), 2)

    def test_setup_cache_zero_recomputations_during_runtime_status_updates(self):
        window = self.window()
        window._last_preflight = dict(self.MEDIUM)
        window._apply_status([self.status_row()])
        self.assertFalse(window._setup_snapshot_dirty)
        self.assertIsNotNone(window._setup_snapshot)

        eval_calls = []
        orig_evaluate = sa_acceptance.evaluate
        def counted_evaluate(*args, **kwargs):
            eval_calls.append(True)
            return orig_evaluate(*args, **kwargs)

        # 20 consecutive runtime status polls must NOT re-evaluate acceptance or setup snapshot
        with mock.patch.object(sa_acceptance, "evaluate", side_effect=counted_evaluate):
            for _ in range(20):
                window._apply_status([self.status_row()])
                window._recompute_setup_state()

        self.assertEqual(len(eval_calls), 0, "status updates re-evaluated acceptance despite clean setup cache")
        self.assertFalse(window._setup_snapshot_dirty)

        # Explicit invalidation triggers exactly one re-evaluation
        with mock.patch.object(sa_acceptance, "evaluate", side_effect=counted_evaluate):
            window._invalidate_setup(reason="test_invalidation", recompute=True)

        self.assertEqual(len(eval_calls), 1, "invalidation did not trigger re-evaluation")
        self.assertFalse(window._setup_snapshot_dirty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
