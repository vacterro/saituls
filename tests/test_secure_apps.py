"""Secure Apps subsystem regression suite.

Deterministic and disposable. Every lifecycle test runs against the fake
authentication provider and the fake storage backend, with an injected clock
and an injected process adapter, so the whole state machine -- six-hour idle
expiry included -- is exercised without a security key, without a VHDX,
without elevation and without waiting.

Hardware-backed coverage is a separate, explicitly interactive script
(tests/secure_apps_interactive.py). CI must never require a YubiKey.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_acceptance      # noqa: E402
import sa_applife          # noqa: E402
import sa_audit           # noqa: E402
import sa_auth            # noqa: E402
import sa_broker          # noqa: E402
import sa_cli             # noqa: E402
import sa_config          # noqa: E402
import sa_crypto          # noqa: E402
import sa_enroll          # noqa: E402
import sa_migrate         # noqa: E402
import sa_paths           # noqa: E402
import sa_privhelper      # noqa: E402
import sa_privtask        # noqa: E402
import sa_state           # noqa: E402
import sa_storage         # noqa: E402

DEAD_PID = 0x7FFFFFF0      # never a live process on Windows


class FakeClock(object):
    """Monotonic seconds under test control. Six hours costs one assignment."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


def make_sleep(clock):
    """A sleep that moves the fake clock instead of the wall clock.

    Without this, the supervisor's bounded waits spin forever: their deadline
    is read from the same injected clock, and a clock that never advances
    never reaches it.
    """
    def sleep(seconds):
        clock.advance(seconds)
    return sleep


def registry_document(root, mount, mode="default", idle=360, conditions=None,
                      overrides=None, profile_overrides=None):
    profile = {
        "id": "vault",
        "label": "Vault",
        "enabled": True,
        "application": {
            "executable": str(Path(root) / "apps" / "vault" / "app" / "App.exe"),
            "working_directory": str(Path(root) / "apps" / "vault" / "app"),
            "arguments": [],
            "vault_argument_style": "path",
        },
        "storage": {
            "backend": "fake-memory",
            "container": str(Path(root) / "vaults" / "vault" / "vault.vhdx"),
            "container_id": "vault-container-1",
            "mount_path": str(mount),
            "size_gb": 4,
        },
        "authentication": {
            "provider": "fake-auth",
            "credential_profile": "primary",
        },
        "policy": {
            "mode": mode,
            "idle_timeout_minutes": idle,
            "lock_on_windows_lock": True,
            "lock_on_suspend": True,
            "lock_on_logoff": True,
            "unmount_when_app_closes": True,
            "graceful_close_timeout_seconds": 5,
            "process_exit_confirm_timeout_seconds": 5,
            "conditions": conditions if conditions is not None else [],
        },
    }
    if profile_overrides:
        for section, values in profile_overrides.items():
            if isinstance(values, dict):
                profile.setdefault(section, {}).update(values)
            else:
                profile[section] = values
    document = {
        "schema": "saituls.secure-apps/1",
        "schema_version": 1,
        "managed_root": str(root),
        "profiles": [profile],
    }
    if overrides:
        document.update(overrides)
    return document


class Harness(object):
    """One temp managed root + registry + broker wired to fakes."""

    def __init__(self, mode="default", idle=360, conditions=None,
                 document_overrides=None, profile_overrides=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="saituls-secapps-"))
        self.root = self.tmp / "managed"
        self.mount = self.tmp / "mount"
        (self.root / "apps" / "vault" / "app").mkdir(parents=True, exist_ok=True)
        (self.root / "vaults" / "vault").mkdir(parents=True, exist_ok=True)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "apps" / "vault" / "app" / "App.exe").write_bytes(b"MZ fake")
        self.document = registry_document(self.root, self.mount, mode=mode,
                                          idle=idle, conditions=conditions,
                                          overrides=document_overrides,
                                          profile_overrides=profile_overrides)
        self.registry = sa_config.parse_registry(self.document,
                                                 managed_root=str(self.root),
                                                 allow_test_providers=True)
        self.profile = self.registry.get("vault")
        self.clock = FakeClock()
        self.provider = sa_auth.FakeAuthProvider()
        self.backend = sa_storage.FakeStorageBackend()
        self.adapter = sa_applife.FakeProcessAdapter()
        self.audit = sa_audit.AuditLog(self.registry.audit_path)
        self.broker = self._build_broker()

    def _build_broker(self):
        return sa_broker.SecureBroker(
            self.registry,
            audit=self.audit,
            providers={"fake-auth": self.provider},
            backends={"fake-memory": self.backend},
            process_adapter=self.adapter,
            clock=self.clock,
            sleep=make_sleep(self.clock))

    def restart_broker(self, dead_broker=True):
        """Simulate a broker that went away without relocking."""
        if dead_broker:
            store = sa_state.StateStore(self.registry.state_path)
            entry = store.get(self.profile.id)
            if entry is not None:
                entry.broker_pid = DEAD_PID
                store.save()
        self.adapter = sa_applife.FakeProcessAdapter()
        self.broker = self._build_broker()
        return self.broker

    def enroll(self, acknowledge=True):
        result = sa_enroll.create_and_enroll(
            self.broker, self.profile.id, lambda text, profile: acknowledge)
        # Enrollment now detaches its own staging mount, so the container is
        # already DETACHED here. The assignment stays as a guard: a harness
        # that silently started handing out a MOUNTED vault would make every
        # policy test below meaningless.
        if result.ok:
            self.backend.force_state(self.profile, sa_storage.DETACHED)
        return result

    def close(self):
        try:
            self.broker.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def audit_text(self):
        try:
            return Path(self.registry.audit_path).read_text(encoding="utf-8")
        except OSError:
            return ""


class BaseCase(unittest.TestCase):
    def harness(self, **kwargs):
        h = Harness(**kwargs)
        self.addCleanup(h.close)
        return h


# ══════════════════════════════════════════════ configuration & paths
class ConfigSchemaTests(BaseCase):
    def test_valid_registry_parses(self):
        h = self.harness()
        self.assertEqual(h.registry.schema_version, 1)
        self.assertEqual(h.registry.ids(), ["vault"])
        self.assertEqual(h.profile.policy.idle_timeout_minutes, 360)

    def test_shipped_registry_is_valid(self):
        registry = sa_config.load_registry(
            str(SUBSYSTEM / "secure_apps.json"),
            managed_root=str(Path(tempfile.gettempdir()) / "saituls-secapps-schema"))
        self.assertIn("obsidian", registry.ids())
        obsidian = registry.get("obsidian")
        self.assertEqual(obsidian.backend, "bitlocker-vhdx")
        self.assertEqual(obsidian.provider, "yubikey-fido2-hmac-secret")
        self.assertEqual(obsidian.policy.mode, "default")
        self.assertEqual(obsidian.policy.idle_timeout_minutes, 360)

    def test_unknown_schema_id_fails_closed(self):
        h = self.harness()
        doc = dict(h.document, schema="saituls.secure-apps/99")
        with self.assertRaises(sa_config.ConfigError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_unknown_schema_version_fails_closed(self):
        h = self.harness()
        doc = dict(h.document, schema_version=2)
        with self.assertRaises(sa_config.ConfigError) as ctx:
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)
        self.assertIn("schema_version", str(ctx.exception))

    def test_malformed_schema_version_fails_closed(self):
        h = self.harness()
        for bad in ("1", 1.5, True, None):
            doc = dict(h.document, schema_version=bad)
            with self.assertRaises(sa_config.ConfigError):
                sa_config.parse_registry(doc, managed_root=str(h.root),
                                         allow_test_providers=True)

    def test_unknown_profile_key_is_rejected(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["lock_on_everything"] = True
        with self.assertRaises(sa_config.ConfigError) as ctx:
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)
        self.assertIn("lock_on_everything", str(ctx.exception))

    def test_unknown_policy_key_is_rejected(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["policy"]["lock_on_windows_locks"] = True
        with self.assertRaises(sa_config.ConfigError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_unknown_condition_type_is_rejected(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["policy"]["conditions"] = [{"type": "run_this_script"}]
        with self.assertRaises(sa_config.ConfigError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_condition_parameter_is_typed(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["policy"]["conditions"] = [
            {"type": "require_auth_after_minutes", "minutes": "soon"}]
        with self.assertRaises(sa_config.ConfigError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_unknown_backend_and_provider_rejected(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["storage"]["backend"] = "truecrypt"
        with self.assertRaises(sa_config.ConfigError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["authentication"]["provider"] = "trust-me"
        with self.assertRaises(sa_config.ConfigError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_test_providers_refused_in_production_mode(self):
        h = self.harness()
        with self.assertRaises(sa_config.ConfigError) as ctx:
            sa_config.parse_registry(h.document, managed_root=str(h.root),
                                     allow_test_providers=False)
        self.assertIn("not available", str(ctx.exception))

    def test_unknown_policy_mode_rejected(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["policy"]["mode"] = "paranoid"
        with self.assertRaises(sa_config.ConfigError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_duplicate_profile_id_rejected(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"].append(json.loads(json.dumps(doc["profiles"][0])))
        with self.assertRaises(sa_config.ConfigError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_unknown_profile_lookup_rejected(self):
        h = self.harness()
        with self.assertRaises(sa_config.ConfigError):
            h.registry.get("not-a-profile")
        with self.assertRaises(sa_broker.BrokerError):
            h.broker.open("not-a-profile")

    def test_require_auth_provider_must_name_an_available_provider(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["policy"]["conditions"] = [
            {"type": "require_auth_provider", "provider": "magic-ring"}]
        with self.assertRaises(sa_config.ConfigError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)


class PathPolicyTests(BaseCase):
    def test_canonicalization(self):
        self.assertEqual(sa_paths.canonical("c:/a/b/../c"), r"C:\a\c")
        self.assertEqual(sa_paths.canonical(r"c:\a\.\b\\"), r"C:\a\b")
        self.assertEqual(sa_paths.canonical("  C:/A/B  "), r"C:\A\B")

    def test_containment_respects_separator_boundary(self):
        self.assertTrue(sa_paths.is_within(r"C:\managed", r"C:\managed\x\y"))
        self.assertTrue(sa_paths.is_within(r"C:\managed", r"C:\managed"))
        self.assertFalse(sa_paths.is_within(r"C:\managed", r"C:\managed-evil\x"))
        self.assertFalse(sa_paths.is_within(r"C:\managed", r"C:\other"))

    def test_escape_is_rejected(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["storage"]["container"] = str(
            h.tmp / "outside" / "vault.vhdx")
        with self.assertRaises(sa_paths.PathPolicyError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_traversal_in_container_is_rejected(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["storage"]["container"] = str(
            h.root / "vaults" / ".." / ".." / "escape.vhdx")
        with self.assertRaises(sa_paths.PathPolicyError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_mount_path_may_not_be_a_drive_root(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["storage"]["mount_path"] = "C:\\"
        with self.assertRaises(sa_paths.PathPolicyError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_reparse_point_inside_managed_root_is_rejected(self):
        h = self.harness()
        junction = h.root / "vaults" / "linked"
        target = h.tmp / "elsewhere"
        target.mkdir(parents=True, exist_ok=True)
        rc = os.system('mklink /J "%s" "%s" >nul 2>&1' % (junction, target))
        if rc != 0 or not sa_paths.is_reparse_point(str(junction)):
            self.skipTest("could not create a junction on this filesystem")
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["storage"]["container"] = str(junction / "vault.vhdx")
        with self.assertRaises(sa_paths.PathPolicyError) as ctx:
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)
        self.assertIn("reparse point", str(ctx.exception))


class ApplicationOwnershipTests(BaseCase):
    """The profile owns the executable it launches -- by default, literally."""

    def test_unmanaged_executable_is_refused_by_default(self):
        h = self.harness()
        outside = h.tmp / "portable" / "App.exe"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"MZ")
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["application"]["executable"] = str(outside)
        doc["profiles"][0]["application"]["working_directory"] = str(outside.parent)
        with self.assertRaises(sa_paths.PathPolicyError):
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)

    def test_unmanaged_executable_allowed_only_when_declared(self):
        h = self.harness()
        outside = h.tmp / "portable" / "App.exe"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"MZ")
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["application"]["executable"] = str(outside)
        doc["profiles"][0]["application"]["working_directory"] = str(outside.parent)
        doc["profiles"][0]["application"]["allow_unmanaged_executable"] = True
        registry = sa_config.parse_registry(doc, managed_root=str(h.root),
                                            allow_test_providers=True)
        profile = registry.get("vault")
        self.assertTrue(profile.allow_unmanaged_executable)
        self.assertEqual(profile.executable, sa_paths.canonical(str(outside)))

    def test_declared_unmanaged_executable_must_actually_exist(self):
        h = self.harness()
        doc = json.loads(json.dumps(h.document))
        doc["profiles"][0]["application"]["executable"] = str(
            h.tmp / "portable" / "Missing.exe")
        doc["profiles"][0]["application"]["allow_unmanaged_executable"] = True
        with self.assertRaises(sa_config.ConfigError) as ctx:
            sa_config.parse_registry(doc, managed_root=str(h.root),
                                     allow_test_providers=True)
        self.assertIn("no file exists", str(ctx.exception))

    def test_import_application_places_the_declared_executable(self):
        h = self.harness()
        source = h.tmp / "portable"
        (source / "resources" / "app").mkdir(parents=True, exist_ok=True)
        (source / "App.exe").write_bytes(b"MZ portable")
        (source / "resources" / "app" / "main.js").write_text("x", encoding="utf-8")
        # Remove the placeholder the harness created, so the import is real.
        shutil.rmtree(h.root / "apps" / "vault" / "app")
        result = sa_enroll.import_application(h.broker, "vault", str(source))
        self.assertTrue(result["ok"])
        self.assertTrue(os.path.isfile(h.profile.executable))
        self.assertTrue(os.path.isfile(
            os.path.join(result["destination"], "resources", "app", "main.js")))
        self.assertGreaterEqual(result["files"], 2)

    def test_import_refuses_to_overwrite_silently(self):
        h = self.harness()
        source = h.tmp / "portable"
        source.mkdir(parents=True, exist_ok=True)
        (source / "App.exe").write_bytes(b"MZ portable")
        with self.assertRaises(sa_enroll.EnrollmentRefused) as ctx:
            sa_enroll.import_application(h.broker, "vault", str(source))
        self.assertIn("already exists", str(ctx.exception))
        result = sa_enroll.import_application(h.broker, "vault", str(source),
                                              overwrite=True)
        self.assertTrue(result["ok"])

    def test_import_refuses_a_missing_source(self):
        h = self.harness()
        with self.assertRaises(sa_enroll.EnrollmentRefused) as ctx:
            sa_enroll.import_application(h.broker, "vault", str(h.tmp / "nope"))
        self.assertEqual(ctx.exception.category, "path_policy")

    def test_import_is_pointless_for_an_unmanaged_profile(self):
        h = self.harness()
        outside = h.tmp / "portable" / "App.exe"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"MZ")
        h.profile.allow_unmanaged_executable = True
        with self.assertRaises(sa_enroll.EnrollmentRefused):
            sa_enroll.import_application(h.broker, "vault", str(outside.parent))

    def test_obsidian_uri_is_built_only_after_the_mount(self):
        h = self.harness(profile_overrides={
            "application": {"vault_argument_style": "obsidian-uri"}})
        h.enroll()
        result = h.broker.open("vault")
        self.assertTrue(result.ok, result.reason)
        _executable, arguments, _cwd = h.adapter.spawned[0]
        self.assertEqual(len(arguments), 1)
        self.assertTrue(arguments[0].startswith("obsidian://open?path="))
        # The URI is handed to the PROFILE's executable, never to the shell,
        # so a globally registered handler cannot redirect the launch.
        self.assertEqual(_executable, h.profile.executable)

    def test_vault_argument_can_be_suppressed(self):
        h = self.harness(profile_overrides={
            "application": {"vault_argument_style": "none",
                            "arguments": ["--fixed"]}})
        h.enroll()
        h.broker.open("vault")
        _executable, arguments, _cwd = h.adapter.spawned[0]
        self.assertEqual(arguments, ["--fixed"])


# ══════════════════════════════════════════════ key hierarchy
class KeyHierarchyTests(BaseCase):
    def _material(self):
        salt = sa_crypto.new_salt()
        kek = sa_crypto.derive_kek(b"k" * 32, salt, "vault", "container-1")
        secret = sa_crypto.new_volume_secret()
        aad = sa_crypto.build_aad("vault", "container-1", "cred-a")
        nonce, ct = sa_crypto.wrap_secret(kek, secret, aad)
        return salt, kek, secret, aad, nonce, ct

    def test_wrap_unwrap_round_trip(self):
        _salt, kek, secret, aad, nonce, ct = self._material()
        out = sa_crypto.unwrap_secret(kek, nonce, ct, aad)
        self.assertEqual(out.bytes(), secret.bytes())

    def test_tampered_ciphertext_fails(self):
        _salt, kek, _secret, aad, nonce, ct = self._material()
        broken = bytearray(ct)
        broken[0] ^= 0xFF
        with self.assertRaises(sa_crypto.UnwrapError):
            sa_crypto.unwrap_secret(kek, nonce, bytes(broken), aad)

    def test_tampered_nonce_fails(self):
        _salt, kek, _secret, aad, nonce, ct = self._material()
        broken = bytearray(nonce)
        broken[0] ^= 0xFF
        with self.assertRaises(sa_crypto.UnwrapError):
            sa_crypto.unwrap_secret(kek, bytes(broken), ct, aad)

    def test_wrong_profile_aad_fails(self):
        _salt, kek, _secret, _aad, nonce, ct = self._material()
        for wrong in (sa_crypto.build_aad("other-profile", "container-1", "cred-a"),
                      sa_crypto.build_aad("vault", "other-container", "cred-a"),
                      sa_crypto.build_aad("vault", "container-1", "cred-b"),
                      sa_crypto.build_aad("vault", "container-1", "cred-a", 99)):
            with self.assertRaises(sa_crypto.UnwrapError):
                sa_crypto.unwrap_secret(kek, nonce, ct, wrong)

    def test_wrong_kek_fails(self):
        salt, _kek, _secret, aad, nonce, ct = self._material()
        other = sa_crypto.derive_kek(b"j" * 32, salt, "vault", "container-1")
        with self.assertRaises(sa_crypto.UnwrapError):
            sa_crypto.unwrap_secret(other, nonce, ct, aad)

    def test_kek_is_profile_scoped(self):
        salt = sa_crypto.new_salt()
        a = sa_crypto.derive_kek(b"k" * 32, salt, "vault-a", "c1").bytes()
        b = sa_crypto.derive_kek(b"k" * 32, salt, "vault-b", "c1").bytes()
        c = sa_crypto.derive_kek(b"k" * 32, salt, "vault-a", "c2").bytes()
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_short_hmac_output_refused(self):
        with self.assertRaises(sa_crypto.CryptoError):
            sa_crypto.derive_kek(b"tooshort", sa_crypto.new_salt(), "v", "c")

    def test_secret_buffer_zeroizes(self):
        buf = sa_crypto.SecretBuffer(b"sensitive-value")
        view = buf.view()
        buf.zeroize()
        self.assertTrue(buf.closed)
        self.assertEqual(len(view), 0)
        self.assertNotIn("sensitive", repr(buf))
        with self.assertRaises(sa_crypto.CryptoError):
            buf.bytes()


# ══════════════════════════════════════════════ enrollment
class EnrollmentTests(BaseCase):
    def test_successful_key_enrollment(self):
        h = self.harness()
        result = h.enroll()
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.recovery_shown)
        self.assertTrue(h.broker.credentials.is_enrolled("vault"))
        self.assertEqual(len(sa_enroll.enrolled_keys(h.broker, "vault")), 1)
        stored = json.loads(Path(h.registry.credentials_path).read_text("utf-8"))
        blob = json.dumps(stored)
        self.assertNotIn("recovery", blob.lower())
        for enrollment in h.broker.credentials.enrollments("vault"):
            self.assertTrue(enrollment.ciphertext)
            self.assertTrue(enrollment.kdf_salt)

    def test_capability_failure_refuses_enrollment(self):
        h = self.harness()
        h.provider.hmac_secret = False
        with self.assertRaises(sa_enroll.EnrollmentRefused) as ctx:
            h.enroll()
        self.assertEqual(ctx.exception.category, "auth_capability")
        self.assertIn("hmac-secret", str(ctx.exception))
        self.assertFalse(h.broker.credentials.is_enrolled("vault"))
        self.assertEqual(h.backend.state(h.profile), sa_storage.MISSING)

    def test_no_authenticator_refuses_enrollment(self):
        h = self.harness()
        h.provider.present = False
        with self.assertRaises(sa_enroll.EnrollmentRefused) as ctx:
            h.enroll()
        self.assertEqual(ctx.exception.category, "auth_unavailable")

    def test_declined_recovery_acknowledgement_rolls_back(self):
        h = self.harness()
        result = h.enroll(acknowledge=False)
        self.assertFalse(result.ok)
        self.assertTrue(result.recovery_shown)
        self.assertFalse(h.broker.credentials.is_enrolled("vault"))
        self.assertFalse(os.path.isfile(h.profile.container))

    def test_second_key_added_without_re_encrypting(self):
        h = self.harness()
        h.enroll()
        first = h.broker.credentials.enrollments("vault")[0]
        # Key 1 is still plugged in (it has to be: it proves ownership), and
        # the next created credential belongs to key 2.
        h.provider.credential_id = b"fake-credential-2"
        result = sa_enroll.enroll_additional(h.broker, "vault")
        self.assertTrue(result.ok)
        enrollments = h.broker.credentials.enrollments("vault")
        self.assertEqual(len(enrollments), 2)
        # Same container, two independently wrapped copies of one secret.
        self.assertNotEqual(enrollments[0].ciphertext, enrollments[1].ciphertext)
        self.assertNotEqual(enrollments[0].credential_id, enrollments[1].credential_id)
        self.assertEqual(first.ciphertext, enrollments[0].ciphertext)
        # Either key ALONE opens the vault, and both yield the same secret --
        # proof the container was not re-encrypted for the second key.
        opened = []
        for credential in (b"fake-credential-1", b"fake-credential-2"):
            h.provider.use_only(credential)
            used, secret = sa_auth.unwrap_volume_secret(
                h.profile, h.provider, h.broker.credentials)
            self.assertEqual(used, credential)
            opened.append(secret.bytes())
            secret.zeroize()
        self.assertEqual(opened[0], opened[1])

    def test_last_credential_cannot_be_removed(self):
        h = self.harness()
        h.enroll()
        only = h.broker.credentials.enrollments("vault")[0]
        with self.assertRaises(sa_auth.AuthError) as ctx:
            sa_enroll.remove_credential(h.broker, "vault", only.credential_id)
        self.assertIn("last enrolled credential", str(ctx.exception))


class AuthFailureTests(BaseCase):
    def test_wrong_credential_is_reported(self):
        h = self.harness()
        h.enroll()
        h.provider.use_only(b"someone-elses-key")
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "auth_wrong_credential")
        self.assertEqual(result.state, sa_state.AUTH_REQUIRED)
        self.assertFalse(h.adapter.spawned)

    def test_cancelled_operation_is_reported_and_not_an_error_state(self):
        h = self.harness()
        h.enroll()
        h.provider.cancel_next = True
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "auth_cancelled")
        self.assertEqual(result.state, sa_state.AUTH_REQUIRED)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)

    def test_capability_loss_at_unlock_time_is_reported(self):
        h = self.harness()
        h.enroll()
        h.provider.hmac_secret = False
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "auth_capability")

    def test_wrapped_key_authentication_failure(self):
        h = self.harness()
        h.enroll()
        # A different authenticator seed produces a different hmac-secret
        # output, so the KEK is wrong and the wrapped secret does not
        # authenticate. Indistinguishable from tampering, on purpose.
        h.provider.seed = b"a-different-authenticator"
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "unwrap_failed")

    def test_tampered_stored_ciphertext_is_refused(self):
        h = self.harness()
        h.enroll()
        document = json.loads(Path(h.registry.credentials_path).read_text("utf-8"))
        blob = document["profiles"]["vault"]["enrollments"][0]
        raw = bytearray(sa_auth.b64d(blob["ciphertext"]))
        raw[0] ^= 0xFF
        blob["ciphertext"] = sa_auth.b64e(bytes(raw))
        Path(h.registry.credentials_path).write_text(json.dumps(document), "utf-8")
        h.broker.credentials.load()
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "unwrap_failed")

    def test_wrong_profile_aad_in_store_is_refused(self):
        h = self.harness()
        h.enroll()
        document = json.loads(Path(h.registry.credentials_path).read_text("utf-8"))
        # Re-point the stored container id: the AAD no longer matches what was
        # sealed, so the ciphertext must refuse to open.
        document["profiles"]["vault"]["container_id"] = "some-other-container"
        Path(h.registry.credentials_path).write_text(json.dumps(document), "utf-8")
        h.broker.credentials.load()
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "unwrap_failed")

    def test_unenrolled_profile_refuses_to_open(self):
        h = self.harness()
        h.backend.seed(h.profile, b"x" * 32, sa_storage.DETACHED)
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "not_enrolled")


# ══════════════════════════════════════════════ storage + launch
class StorageAndLaunchTests(BaseCase):
    def test_happy_path_open(self):
        h = self.harness()
        h.enroll()
        result = h.broker.open("vault")
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.state, sa_state.RUNNING)
        self.assertTrue(result.required_auth)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)
        self.assertEqual(len(h.adapter.spawned), 1)
        executable, arguments, cwd = h.adapter.spawned[0]
        self.assertEqual(executable, h.profile.executable)
        self.assertEqual(arguments, [h.profile.mount_path])
        self.assertEqual(cwd, h.profile.working_directory)

    def test_unlock_failure_leaves_profile_locked_not_mounted(self):
        h = self.harness()
        h.enroll()
        h.backend.fail_unlock = "BitLocker refused the volume secret"
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "storage_unlock_failed")
        self.assertNotEqual(h.backend.state(h.profile), sa_storage.MOUNTED)
        self.assertFalse(h.adapter.spawned)

    def test_mount_failure_is_reported(self):
        h = self.harness()
        h.enroll()
        h.backend.fail_mount = "the volume could not be attached"
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "storage_mount_failed")
        self.assertFalse(h.adapter.spawned)

    def test_missing_mount_root_after_mount_is_refused(self):
        h = self.harness()
        h.enroll()
        original = h.backend.unlock_and_mount

        def lying_mount(profile, secret):
            result = original(profile, secret)
            shutil.rmtree(profile.mount_path, ignore_errors=True)
            return result

        h.backend.unlock_and_mount = lying_mount
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "storage_mount_failed")
        self.assertFalse(h.adapter.spawned)

    def test_launch_failure_after_mount_relocks(self):
        h = self.harness()
        h.enroll()
        h.adapter.fail_spawn = "the application executable is not runnable"
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "app_launch_failed")
        # The vault must not be left mounted behind a failed launch.
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        self.assertEqual(h.broker.status("vault")["state"], sa_state.LOCKED)

    def test_application_close_unmounts(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        events = h.broker.tick()
        self.assertTrue(events)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        self.assertEqual(h.broker.status("vault")["state"], sa_state.SESSION_CACHED)

    def test_surviving_child_blocks_detach(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        # Electron case: the parent exits, a renderer keeps a handle open.
        h.adapter.exit_parent_only(pid)
        h.broker.tick()
        self.assertEqual(h.broker.status("vault")["state"], sa_state.RUNNING)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)

    def test_lock_never_detaches_while_the_app_refuses_to_exit(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        h.adapter.ignore_close = True
        h.profile.policy.force_terminate_after_timeout = False
        results = h.broker.lock_now("vault")
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].error_category, "app_exit_timeout")
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)

    def test_forced_termination_then_detach(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.ignore_close = True
        results = h.broker.lock_now("vault")
        self.assertTrue(results[0].ok, results[0].reason)
        self.assertIn(pid, h.adapter.terminations)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)

    def test_busy_unmount_is_surfaced(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        h.backend.busy_on_unmount = True
        results = h.broker.lock_now("vault")
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].error_category, "storage_busy")


# ══════════════════════════════════════════════ policy: default
class DefaultPolicyTests(BaseCase):
    def test_reopen_inside_session_needs_no_second_key_touch(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()
        self.assertEqual(h.broker.status("vault")["state"], sa_state.SESSION_CACHED)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        auth_before = h.broker.status("vault")["auth_interactions"]

        h.clock.advance(3600 * 5)          # five hours: still inside six
        second = h.broker.open("vault")
        self.assertTrue(second.ok, second.reason)
        self.assertFalse(second.required_auth)
        self.assertEqual(h.broker.status("vault")["auth_interactions"], auth_before)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)

    def test_vault_is_not_mounted_merely_because_the_session_is_valid(self):
        """The whole point of Default: two clocks, not one."""
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()
        for _ in range(5):
            h.clock.advance(3600)
            h.broker.tick()
            status = h.broker.status("vault")
            self.assertTrue(status["session_active"])
            self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
            self.assertEqual(status["state"], sa_state.SESSION_CACHED)

    def test_reopen_after_session_expiry_requires_the_key_again(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()
        auth_before = h.broker.status("vault")["auth_interactions"]

        h.clock.advance(3600 * 6 + 60)     # past six hours
        h.broker.tick()
        status = h.broker.status("vault")
        self.assertFalse(status["session_active"])
        self.assertEqual(status["state"], sa_state.LOCKED)

        second = h.broker.open("vault")
        self.assertTrue(second.ok, second.reason)
        self.assertTrue(second.required_auth)
        self.assertEqual(h.broker.status("vault")["auth_interactions"], auth_before + 1)

    def test_idle_expiry_while_the_app_is_open_closes_and_relocks(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.clock.advance(3600 * 6 + 1)
        h.broker.tick()
        status = h.broker.status("vault")
        self.assertEqual(status["state"], sa_state.LOCKED)
        self.assertFalse(status["session_active"])
        self.assertFalse(h.adapter.alive(pid))
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)

    def test_protected_foreground_activity_keeps_the_session_alive(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        for _ in range(8):
            h.clock.advance(3600 * 5)
            h.adapter.set_foreground(pid)
            self.assertEqual(h.broker.poll_foreground(), ["vault"])
            h.broker.tick()
        self.assertEqual(h.broker.status("vault")["state"], sa_state.RUNNING)

    def test_unrelated_foreground_activity_does_not_keep_it_alive(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        h.adapter.set_foreground(999999)     # some other application
        h.clock.advance(3600 * 6 + 1)
        self.assertEqual(h.broker.poll_foreground(), [])
        h.broker.tick()
        self.assertEqual(h.broker.status("vault")["state"], sa_state.LOCKED)

    def test_explicit_interaction_refreshes_the_session(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()
        for _ in range(6):
            h.clock.advance(3600 * 5)
            self.assertTrue(h.broker.note_activity("vault"))
            h.broker.tick()
        self.assertTrue(h.broker.status("vault")["session_active"])

    def test_hard_lifetime_condition_expires_even_with_activity(self):
        h = self.harness(conditions=[{"type": "require_auth_after_minutes",
                                      "minutes": 30}])
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()
        h.clock.advance(60 * 10)
        h.broker.note_activity("vault")
        h.clock.advance(60 * 25)             # 35 min old, still "active"
        h.broker.tick()
        self.assertFalse(h.broker.status("vault")["session_active"])


# ══════════════════════════════════════════════ policy: aggressive
class AggressivePolicyTests(BaseCase):
    def test_reopen_always_requires_the_key(self):
        h = self.harness(mode="aggressive")
        h.enroll()
        first = h.broker.open("vault")
        self.assertTrue(first.ok, first.reason)
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()
        status = h.broker.status("vault")
        self.assertEqual(status["state"], sa_state.LOCKED)
        self.assertFalse(status["session_active"])

        second = h.broker.open("vault")
        self.assertTrue(second.ok, second.reason)
        self.assertTrue(second.required_auth)
        self.assertEqual(h.broker.status("vault")["auth_interactions"], 2)

    def test_session_invalidated_immediately_on_exit(self):
        h = self.harness(mode="aggressive")
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()
        self.assertFalse(h.broker.status("vault")["session_active"])
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)

    def test_already_running_is_not_a_second_prompt(self):
        h = self.harness(mode="aggressive")
        h.enroll()
        h.broker.open("vault")
        again = h.broker.open("vault")
        self.assertTrue(again.ok)
        self.assertEqual(again.error_category, "already_running")
        self.assertEqual(h.broker.status("vault")["auth_interactions"], 1)
        self.assertEqual(len(h.adapter.spawned), 1)

    def test_switching_to_aggressive_drops_a_cached_session(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()
        self.assertTrue(h.broker.status("vault")["session_active"])
        h.broker.set_mode("vault", "aggressive")
        status = h.broker.status("vault")
        self.assertFalse(status["session_active"])
        self.assertEqual(status["mode"], "aggressive")
        self.assertEqual(status["state"], sa_state.LOCKED)


# ══════════════════════════════════════════════ system events
class SystemEventTests(BaseCase):
    def test_manual_lock_now(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        results = h.broker.lock_now("vault")
        self.assertTrue(results[0].ok, results[0].reason)
        self.assertFalse(h.adapter.alive(pid))
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        self.assertFalse(h.broker.status("vault")["session_active"])

    def test_workstation_lock_transition(self):
        h = self.harness(conditions=[{"type": "require_auth_after_windows_lock"}])
        h.enroll()
        h.broker.open("vault")
        results = h.broker.on_system_event(sa_broker.EVENT_WORKSTATION_LOCK)
        self.assertTrue(results[0].ok, results[0].reason)
        status = h.broker.status("vault")
        self.assertEqual(status["state"], sa_state.AUTH_REQUIRED)
        self.assertTrue(status["pending_reauth"])
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        h.broker.open("vault")
        self.assertEqual(h.broker.status("vault")["auth_interactions"], 2)

    def test_suspend_transition(self):
        h = self.harness(conditions=[{"type": "require_auth_after_suspend"}])
        h.enroll()
        h.broker.open("vault")
        results = h.broker.on_system_event(sa_broker.EVENT_SUSPEND)
        self.assertTrue(results[0].ok, results[0].reason)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        self.assertFalse(h.broker.status("vault")["session_active"])

    def test_logoff_transition(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        results = h.broker.on_system_event(sa_broker.EVENT_LOGOFF)
        self.assertTrue(results[0].ok, results[0].reason)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)

    def test_policy_can_decline_to_lock_on_an_event(self):
        h = self.harness(profile_overrides={"policy": {"lock_on_windows_lock": False}})
        h.enroll()
        h.broker.open("vault")
        results = h.broker.on_system_event(sa_broker.EVENT_WORKSTATION_LOCK)
        self.assertTrue(results[0].ok)
        self.assertEqual(h.broker.status("vault")["state"], sa_state.RUNNING)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)

    def test_broker_shutdown_locks(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        h.broker.shutdown()
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        self.assertFalse(h.broker.status("vault")["session_active"])
        self.assertGreaterEqual(h.backend.stopped, 1)

    def test_locking_profile_refuses_new_launches(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        runtime = h.broker.runtime("vault")
        runtime.locking = True
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "concurrent_request")


# ══════════════════════════════════════════════ crash recovery
class RecoveryTests(BaseCase):
    def test_broker_restart_with_unexpectedly_mounted_volume(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)
        # The broker dies. Windows keeps the volume attached.
        h.restart_broker()
        verdict = h.broker.reconcile("vault")
        self.assertEqual(verdict.verdict, sa_state.RECOVERY_REQUIRED)
        self.assertEqual(h.broker.status("vault")["state"], sa_state.RECOVERY_REQUIRED)

    def test_recovery_required_refuses_new_launches(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        h.restart_broker()
        h.broker.reconcile("vault")
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "recovery_required")

    def test_recover_relocks_safely(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        h.restart_broker()
        h.broker.reconcile("vault")
        result = h.broker.recover("vault")
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        self.assertEqual(h.broker.status("vault")["state"], sa_state.LOCKED)

    def test_dead_broker_is_not_read_as_locked(self):
        """A crashed broker does not imply a locked vault."""
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()          # detached, recorded SESSION_CACHED
        store = sa_state.StateStore(h.registry.state_path)
        entry = store.get("vault")
        entry.expected_state = sa_state.RUNNING     # as if it crashed while open
        entry.broker_pid = DEAD_PID
        store.save()
        h.adapter = sa_applife.FakeProcessAdapter()
        h.broker = h._build_broker()
        verdict = h.broker.reconcile("vault")
        self.assertEqual(verdict.verdict, sa_state.RECOVERY_REQUIRED)

    def test_attached_but_locked_container_is_a_recovery_condition(self):
        h = self.harness()
        h.enroll()
        h.backend.force_state(h.profile, sa_storage.ATTACHED_LOCKED)
        h.restart_broker()
        verdict = h.broker.reconcile("vault")
        self.assertEqual(verdict.verdict, sa_state.RECOVERY_REQUIRED)

    def test_corrupt_state_file_does_not_read_as_locked(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        Path(h.registry.state_path).write_text("{ this is not json", "utf-8")
        h.adapter = sa_applife.FakeProcessAdapter()
        h.broker = h._build_broker()
        verdict = h.broker.reconcile("vault")
        self.assertEqual(verdict.verdict, sa_state.RECOVERY_REQUIRED)

    def test_state_file_carries_no_secret(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        text = Path(h.registry.state_path).read_text("utf-8")
        document = json.loads(text)
        entry = document["profiles"][0]
        self.assertEqual(sorted(entry), sorted(sa_state.ENTRY_FIELDS))
        for forbidden in ("secret", "password", "recovery", "hmac", "pin", "key"):
            self.assertNotIn(forbidden, text.lower())


# ══════════════════════════════════════════════ concurrency
class ConcurrencyTests(BaseCase):
    def _blocking_provider(self, harness):
        """Hold the profile lock inside authentication until released."""
        gate = threading.Event()
        entered = threading.Event()
        original = harness.provider.get_key_material

        def blocking(credential_ids, salt):
            entered.set()
            gate.wait(10)
            return original(credential_ids, salt)

        harness.provider.get_key_material = blocking
        return entered, gate

    def test_double_launch_race_produces_one_launch(self):
        h = self.harness()
        h.enroll()
        entered, gate = self._blocking_provider(h)
        results = {}

        def first():
            results["a"] = h.broker.open("vault")

        def second():
            results["b"] = h.broker.open("vault")

        t1 = threading.Thread(target=first)
        t1.start()
        self.assertTrue(entered.wait(10), "first open never reached authentication")
        t2 = threading.Thread(target=second)
        t2.start()
        t2.join(10)
        gate.set()
        t1.join(10)

        self.assertTrue(results["a"].ok, results["a"].reason)
        self.assertFalse(results["b"].ok)
        self.assertEqual(results["b"].error_category, "concurrent_request")
        self.assertEqual(len(h.adapter.spawned), 1)

    def test_two_concurrent_unlock_requests_prompt_once(self):
        h = self.harness()
        h.enroll()
        entered, gate = self._blocking_provider(h)
        outcomes = []

        def worker():
            outcomes.append(h.broker.open("vault"))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        threads[0].start()
        self.assertTrue(entered.wait(10))
        threads[1].start()
        threads[1].join(10)
        gate.set()
        threads[0].join(10)

        self.assertEqual(h.broker.status("vault")["auth_interactions"], 1)
        # create + the staging detach enrollment ends with + one mount.
        self.assertEqual(len(h.backend.calls), 3)
        self.assertEqual(sum(1 for c in h.backend.calls if c[0] == "create"), 1)
        self.assertEqual(sum(1 for c in h.backend.calls if c[0] == "unlock_and_mount"), 1)
        self.assertEqual(len([o for o in outcomes if o.ok]), 1)

    def test_open_while_locking_is_refused(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        released = threading.Event()
        original = h.backend.unmount

        def slow_unmount(profile):
            released.wait(10)
            return original(profile)

        h.backend.unmount = slow_unmount
        locker = threading.Thread(target=lambda: h.broker.lock_now("vault"))
        locker.start()
        time.sleep(0.2)
        result = h.broker.open("vault")
        released.set()
        locker.join(10)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "concurrent_request")


# ══════════════════════════════════════════════ migration
class MigrationTests(BaseCase):
    def _plaintext(self, harness, count=40):
        """Build a plaintext vault AT the final mount path, like the real one."""
        source = Path(harness.profile.mount_path)
        (source / ".obsidian" / "plugins").mkdir(parents=True, exist_ok=True)
        (source / "main" / ".obsidian").mkdir(parents=True, exist_ok=True)
        (source / "main" / "nested" / "deep").mkdir(parents=True, exist_ok=True)
        (source / ".obsidian" / "app.json").write_text('{"x":1}', encoding="utf-8")
        (source / "main" / ".obsidian" / "workspace.json").write_text(
            '{"y":2}', encoding="utf-8")
        for index in range(count):
            note = source / "main" / ("note-%02d - ÕÄÖÜ šž.md" % index)
            note.write_text("# note %d\n%s\n" % (index, "x" * (index * 37)),
                            encoding="utf-8")
        (source / "main" / "nested" / "deep" / "deep.md").write_text(
            "deep", encoding="utf-8")
        return source

    def _prepared(self, **kwargs):
        h = self.harness(**kwargs)
        h.enroll()
        source = self._plaintext(h)
        h.broker.open("vault")       # session provides the volume secret
        return h, source

    def test_migration_verifies_and_keeps_the_plaintext(self):
        h, source = self._prepared()
        before = {p.relative_to(source).as_posix(): p.stat().st_size
                  for p in source.rglob("*") if p.is_file()}
        migrator = sa_migrate.Migrator(h.broker, "vault", str(source),
                                       verify_application=True)
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")
        report = migrator.run(provider)
        self.assertTrue(report.ok, report.reason)
        self.assertEqual(report.status,
                         [sa_migrate.MIGRATION_VERIFIED,
                          sa_migrate.PLAINTEXT_SOURCE_REMAINS])
        self.assertEqual(report.file_count, len(before))
        self.assertGreater(report.hashed, 0)
        self.assertIn(sa_migrate.STEP_VERIFY_TREE, report.steps)
        self.assertIn(sa_migrate.STEP_REVERIFY, report.steps)
        self.assertIn(sa_migrate.STEP_COMPLETE, report.steps)

        archive = Path(report.plaintext_archive)
        self.assertTrue(archive.is_dir(), "the plaintext copy must still exist")
        after = {p.relative_to(archive).as_posix(): p.stat().st_size
                 for p in archive.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "migration must not touch the plaintext")
        self.assertIn("PLAINTEXT", report.reason.upper())

    def test_migration_refuses_an_undersized_container(self):
        h, source = self._prepared()
        h.profile.size_gb = 1
        big = source / "main" / "big.bin"
        big.write_bytes(b"0" * (1024 * 1024))
        migrator = sa_migrate.Migrator(h.broker, "vault", str(source),
                                       verify_application=False)
        migrator.profile.size_gb = 0          # force the precheck to bite
        report = migrator.run(
            sa_migrate.secret_provider_from_session(h.broker, "vault"))
        self.assertFalse(report.ok)
        self.assertEqual(report.error_category, "config_invalid")

    def test_missing_source_is_refused(self):
        h = self.harness()
        h.enroll()
        h.broker.open("vault")
        migrator = sa_migrate.Migrator(
            h.broker, "vault", str(h.tmp / "no-such-vault"),
            verify_application=False)
        report = migrator.run(
            sa_migrate.secret_provider_from_session(h.broker, "vault"))
        self.assertFalse(report.ok)
        self.assertEqual(report.error_category, "path_policy")

    def test_interrupted_before_cutover_keeps_the_plaintext_in_place(self):
        h, source = self._prepared()
        migrator = sa_migrate.Migrator(h.broker, "vault", str(source),
                                       verify_application=False)
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")

        def boom(report, secret_provider):
            raise sa_migrate.MigrationError("interrupted", "internal",
                                            sa_migrate.STEP_CUTOVER_BEGIN)

        migrator.cutover = boom
        report = migrator.run(provider)
        self.assertFalse(report.ok)
        self.assertTrue(source.is_dir(), "the plaintext must be untouched")
        self.assertTrue(any(source.rglob("*.md")))

        state = sa_migrate.inspect(migrator.journal.path)
        self.assertEqual(state["verdict"], "INTERRUPTED_BEFORE_CUTOVER")
        recovery = sa_migrate.recover(h.broker, "vault", migrator.journal.path)
        self.assertEqual(recovery["action"], "resume_available")
        self.assertTrue(source.is_dir())

    def test_interrupted_before_cutover_resumes(self):
        h, source = self._prepared()
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")
        first = sa_migrate.Migrator(h.broker, "vault", str(source),
                                    verify_application=False)
        first.cutover = lambda report, sp: (_ for _ in ()).throw(
            sa_migrate.MigrationError("interrupted", "internal",
                                      sa_migrate.STEP_REVERIFY))
        self.assertFalse(first.run(provider).ok)

        second = sa_migrate.Migrator(h.broker, "vault", str(source),
                                     verify_application=False,
                                     journal_path=first.journal.path)
        report = second.run(provider, resume=True)
        self.assertTrue(report.ok, report.reason)
        self.assertEqual(report.resumed_from, sa_migrate.STEP_REVERIFY)
        self.assertTrue(Path(report.plaintext_archive).is_dir())

    def test_interrupted_during_cutover_rolls_the_plaintext_back(self):
        h, source = self._prepared()
        names_before = sorted(p.name for p in source.rglob("*.md"))
        migrator = sa_migrate.Migrator(h.broker, "vault", str(source),
                                       verify_application=False)
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")

        # Fail AFTER the plaintext has been renamed aside but BEFORE the
        # encrypted volume is mounted at the real path: the one window where
        # the expected path holds neither.
        state = {"calls": 0}
        original_mount = h.backend.unlock_and_mount

        def flaky(profile, secret):
            state["calls"] += 1
            if profile.mount_path == h.profile.mount_path and state["calls"] > 1:
                raise sa_storage.StorageError("interrupted during cutover",
                                              "storage_mount_failed")
            return original_mount(profile, secret)

        h.backend.unlock_and_mount = flaky
        report = migrator.run(provider)
        self.assertFalse(report.ok)

        state_after = sa_migrate.inspect(migrator.journal.path)
        self.assertEqual(state_after["verdict"], "INTERRUPTED_DURING_CUTOVER")
        archive = migrator.journal.data["plaintext_archive"]
        self.assertTrue(Path(archive).is_dir())

        h.backend.unlock_and_mount = original_mount
        recovery = sa_migrate.recover(h.broker, "vault", migrator.journal.path)
        self.assertEqual(recovery["action"], "rolled_back", recovery.get("message"))
        self.assertTrue(source.is_dir())
        self.assertEqual(sorted(p.name for p in source.rglob("*.md")), names_before)

    def test_interrupted_during_cutover_can_be_finished(self):
        h, source = self._prepared()
        migrator = sa_migrate.Migrator(h.broker, "vault", str(source),
                                       verify_application=False)
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")
        state = {"calls": 0}
        original_mount = h.backend.unlock_and_mount

        def flaky(profile, secret):
            state["calls"] += 1
            if profile.mount_path == h.profile.mount_path and state["calls"] > 1:
                raise sa_storage.StorageError("interrupted", "storage_mount_failed")
            return original_mount(profile, secret)

        h.backend.unlock_and_mount = flaky
        self.assertFalse(migrator.run(provider).ok)
        h.backend.unlock_and_mount = original_mount

        recovery = sa_migrate.recover(h.broker, "vault", migrator.journal.path,
                                      volume_secret_provider=provider)
        self.assertEqual(recovery["action"], "finished_cutover")
        self.assertEqual(sa_migrate.inspect(migrator.journal.path)["verdict"],
                         "COMPLETE")
        self.assertTrue(Path(migrator.journal.data["plaintext_archive"]).is_dir())

    def test_representative_sample_is_deterministic(self):
        sizes = {("f%04d" % i): i * 11 for i in range(5000)}
        a = sa_migrate.representative_sample(sizes)
        b = sa_migrate.representative_sample(sizes)
        self.assertEqual(a, b)
        self.assertLessEqual(len(a), sa_migrate.HASH_SAMPLE_MAX)
        self.assertGreater(len(a), 10)

    def test_verification_catches_a_corrupted_copy(self):
        h, source = self._prepared()
        migrator = sa_migrate.Migrator(h.broker, "vault", str(source),
                                       verify_application=False)
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")
        real_verify_tree = migrator.verify_tree

        def corrupt_then_verify(report):
            result = real_verify_tree(report)
            victim = next(Path(migrator.staging_mount).rglob("note-00*.md"))
            victim.write_text("tampered", encoding="utf-8")
            return result

        migrator.verify_tree = corrupt_then_verify
        report = migrator.run(provider)
        self.assertFalse(report.ok)
        self.assertTrue(source.is_dir())


# ══════════════════════════════════════════════ enrollment state machine
class EnrollmentStagingTests(BaseCase):
    """First-run enrollment must not need the profile's real mount path.

    The blocker this suite pins down: enrollment used to create the container
    AT ``storage.mount_path``, which on a first run is the directory holding
    the user's plaintext vault. Windows attaches a volume only to an empty
    directory, and migration refused to start until the container existed --
    so neither step could ever go first.
    """

    def _plaintext_at_final_path(self, harness, names=("note.md", "app.json")):
        final = Path(harness.profile.mount_path)
        final.mkdir(parents=True, exist_ok=True)
        for name in names:
            (final / name).write_text("plaintext " + name, encoding="utf-8")
        return final

    def test_enrollment_runs_with_the_plaintext_still_at_the_mount_path(self):
        h = self.harness()
        final = self._plaintext_at_final_path(h)
        before = sorted(p.name for p in final.iterdir())

        pending, recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        self.addCleanup(lambda: pending.resolved or pending.cancel("test"))
        self.assertTrue(recovery)
        result = pending.accept()

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.state, sa_enroll.ENROLLMENT_READY)
        self.assertEqual(sorted(p.name for p in final.iterdir()), before,
                         "enrollment touched the plaintext vault")

    def test_the_staging_mount_is_beside_the_container_not_at_the_final_path(self):
        h = self.harness()
        expected = Path(h.profile.container).parent / "_enrollment_mount"
        self.assertEqual(Path(sa_enroll.enrollment_mount(h.profile)),
                         Path(sa_paths.canonical(str(expected))))
        self.assertNotEqual(Path(sa_enroll.enrollment_mount(h.profile)),
                            Path(h.profile.mount_path))

    def test_creating_at_a_non_empty_mount_target_is_refused(self):
        """The negative control for the whole staging design."""
        h = self.harness()
        self._plaintext_at_final_path(h)
        secret = sa_crypto.new_volume_secret()
        self.addCleanup(secret.zeroize)
        with self.assertRaises(sa_storage.StorageError) as caught:
            h.backend.create(h.profile, secret)
        self.assertEqual(caught.exception.category, "storage_create_failed")
        self.assertIn("not empty", str(caught.exception))

    def test_enrollment_leaves_the_volume_detached(self):
        h = self.harness()
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        result = pending.accept()
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        self.assertIn(("unmount", "vault"), h.backend.calls)

    def test_the_staging_directory_does_not_survive_a_successful_enrollment(self):
        h = self.harness()
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        staging = Path(pending.staged_profile.mount_path)
        self.assertTrue(staging.is_dir(), "the staging mount was never created")
        pending.accept()
        self.assertFalse(staging.exists(), "the staging mount was left behind")

    def test_migration_can_start_from_a_freshly_enrolled_container(self):
        """The circular dependency, asserted gone in both directions."""
        h = self.harness()
        final = self._plaintext_at_final_path(h, names=("a.md", "b.md", "c.md"))
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        self.assertTrue(pending.accept().ok)

        migrator = sa_migrate.Migrator(h.broker, "vault", str(final),
                                       verify_application=False)
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")
        report = migrator.run(provider)
        self.assertTrue(report.ok, report.reason)
        self.assertEqual(report.status, [sa_migrate.MIGRATION_VERIFIED,
                                         sa_migrate.PLAINTEXT_SOURCE_REMAINS])
        archive = Path(report.plaintext_archive)
        self.assertTrue(archive.is_dir(), "the plaintext copy must still exist")
        self.assertEqual(sorted(p.name for p in archive.iterdir()),
                         ["a.md", "b.md", "c.md"])

    def test_migration_authenticates_without_mounting_at_the_final_path(self):
        h = self.harness()
        self._plaintext_at_final_path(h)
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        pending.accept()
        # No open() anywhere: the secret provider must be able to get a
        # session on its own, because open() would try the occupied path.
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")
        secret = provider()
        try:
            self.assertEqual(len(secret.bytes()), 32)
        finally:
            secret.zeroize()
        self.assertEqual(h.broker.status("vault")["state"], sa_state.SESSION_CACHED)


# ══════════════════════════════════════════════ PIN callback plumbing
class PinPromptingFakeProvider(sa_auth.FakeAuthProvider):
    """A fake key that asks for its PIN through the broker's callback.

    The shape of the elevated CTAP path: the provider itself calls
    ``pin_callback(rp_id)``, and a prompt that returns nothing is a
    cancellation, never an unlock.
    """

    PIN = "4821-fake"

    def __init__(self, **kwargs):
        sa_auth.FakeAuthProvider.__init__(self, **kwargs)
        self.pin_callback = None
        self.prompted_with = []

    def _verify_pin(self):
        if self.pin_callback is None:
            raise sa_auth.AuthCancelledError("no PIN prompt is available")
        self.prompted_with.append(sa_auth.RP_ID)
        entered = self.pin_callback(sa_auth.RP_ID)
        if not entered:
            raise sa_auth.AuthCancelledError("PIN entry was cancelled")
        if entered != self.PIN:
            raise sa_auth.AuthError("the authenticator refused the PIN")

    def create_credential(self, credential_profile, require_hmac_secret=True):
        self._verify_pin()
        return sa_auth.FakeAuthProvider.create_credential(
            self, credential_profile, require_hmac_secret)

    def get_key_material(self, credential_ids, salt):
        self._verify_pin()
        return sa_auth.FakeAuthProvider.get_key_material(self, credential_ids, salt)


class PinCallbackPropagationTests(BaseCase):
    """A replaced PIN callback must reach the provider the next assertion uses."""

    def _harness(self, callback):
        h = self.harness()
        h.provider = PinPromptingFakeProvider()
        h.broker = sa_broker.SecureBroker(
            h.registry, audit=h.audit, providers={"fake-auth": h.provider},
            backends={"fake-memory": h.backend}, process_adapter=h.adapter,
            clock=h.clock, sleep=make_sleep(h.clock), pin_callback=callback)
        self.assertTrue(h.enroll().ok)
        return h

    def _close_app(self, h):
        h.adapter.exit_tree(h.broker.status("vault")["app_pid"])
        h.broker.tick()

    def test_reassigning_the_callback_reaches_the_already_built_provider(self):
        h = self._harness(lambda rp_id: PinPromptingFakeProvider.PIN)
        self.assertTrue(h.broker.open("vault").ok)
        self._close_app(h)
        self.assertEqual(h.broker.status("vault")["state"], sa_state.SESSION_CACHED)

        cancel = lambda rp_id: None                     # noqa: E731
        h.broker.pin_callback = cancel     # the assignment that used to change nothing
        self.assertIs(h.broker.provider(h.profile).pin_callback, cancel)
        refused = h.broker.open("vault", force_auth=True)
        self.assertFalse(refused.ok)
        self.assertEqual(refused.error_category, "auth_cancelled")
        status = h.broker.status("vault")
        self.assertEqual(status["state"], sa_state.AUTH_REQUIRED)
        self.assertFalse(status["session_active"],
                         "a cancelled PIN must not leave the old session usable")
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)

        h.broker.set_pin_callback(lambda rp_id: PinPromptingFakeProvider.PIN)
        again = h.broker.open("vault", force_auth=True)
        self.assertTrue(again.ok, again.reason)
        self.assertTrue(again.required_auth)

    def test_a_wrong_pin_fails_closed(self):
        h = self._harness(lambda rp_id: PinPromptingFakeProvider.PIN)
        h.broker.set_pin_callback(lambda rp_id: "not-the-pin")
        refused = h.broker.open("vault", force_auth=True)
        self.assertFalse(refused.ok)
        self.assertEqual(refused.error_category, "auth_failed")
        self.assertFalse(h.broker.status("vault")["session_active"])
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)

    def test_the_callback_receives_the_relying_party_id(self):
        seen = []
        h = self._harness(lambda rp_id: seen.append(rp_id) or PinPromptingFakeProvider.PIN)
        self.assertTrue(h.broker.open("vault", force_auth=True).ok)
        self.assertTrue(seen)
        self.assertTrue(all(rp_id == sa_auth.RP_ID for rp_id in seen))

    def test_the_gui_pin_callback_accepts_the_relying_party_id(self):
        text = (SUBSYSTEM / "secure_apps_gui.py").read_text(encoding="utf-8")
        self.assertRegex(text, r"def _request_pin\(self, rp_id=None\):")
        self.assertIn("pin_callback=self._request_pin", text)


# ══════════════════════════════════════════════ CLI migration entry point
#: Environment facts the CLI tests pretend to measure. A record built from
#: them is exactly what a green disposable acceptance run would have left.
ACCEPTED_ENVIRONMENT = {
    "host_fingerprint": "a" * 64,
    "windows_build": "10.0.19045.6332",
    "uac_enabled": True,
    "broker_elevated": False,
    "privileged_launch_mode": sa_privtask.LAUNCH_MODE,
    "privileged_task_name": sa_privtask.TASK_PATH,
    "privileged_task_definition_fingerprint": "e" * 64,
    "privileged_task_security_fingerprint": "f0" * 32,
    "privileged_runtime_bundle_fingerprint": "ab" * 32,
    "webauthn_available": True,
    "webauthn_api_version": 2,
    "python_version": "3.11.9",
    "python_fido2_version": "2.2.1",
    "python_fido2_major": 2,
    "transport": sa_auth.TRANSPORT_ELEVATED_CTAP_HELPER,
    "hmac_transport_semantics": sa_auth.DEFAULT_HMAC_TRANSPORT_SEMANTICS,
    "implementation_fingerprint": "b" * 64,
    "acceptance_producer_fingerprint": "c" * 64,
}


def all_gates(value=True):
    return {name: value for name in sa_acceptance.GATE_FLAGS}


class FakeInstanceGuard(object):
    """The real guard is a session-wide mutex; a running GUI must not fail CI."""

    def acquire(self):
        return True

    def release(self):
        pass


def tree_snapshot(root):
    """Every entry below *root* with its size and content hash, or None."""
    root = Path(root)
    if not root.exists():
        return None
    out = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            out.append((rel, "dir"))
        else:
            out.append((rel, path.stat().st_size,
                        hashlib.sha256(path.read_bytes()).hexdigest()))
    return out


class MigrateCliEntrypointTests(BaseCase):
    """``sa_cli.py migrate`` itself, not just the Migrator behind it.

    The first real migration starts with the plaintext vault still sitting at
    the profile's final mount_path. The entry point must authenticate WITHOUT
    mounting there, let the migrator stage the copy elsewhere, and leave the
    cutover rename as the first thing that ever changes the final path.
    """

    def _first_run(self, acceptance=True, environment=None, gates=None):
        h = self.harness()
        final = Path(h.profile.mount_path)
        (final / ".obsidian").mkdir(parents=True)
        (final / ".obsidian" / "app.json").write_text('{"x":1}', encoding="utf-8")
        (final / "notes").mkdir()
        for index in range(6):
            (final / "notes" / ("note-%d ÕÄÖÜ.md" % index)).write_text(
                "# note %d\n%s\n" % (index, "z" * (index * 17)), encoding="utf-8")
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        self.assertTrue(pending.accept().ok)
        if acceptance:
            record = sa_acceptance.build_record(
                environment or ACCEPTED_ENVIRONMENT, h.profile,
                all_gates() if gates is None else gates)
            sa_acceptance.save_record(h.registry.state_dir, record)
        return h, final

    def _run_cli(self, h, argv, broker=None, events=None, environment=None):
        built = []

        def build_broker(registry, echo=False, pin_callback=None):
            built.append(registry)
            if broker is None:
                raise AssertionError("the CLI built a broker although it had to refuse")
            return broker

        stdout = io.StringIO()
        with mock.patch.object(sa_cli, "load", lambda args: h.registry), \
                mock.patch.object(sa_cli, "build_broker", build_broker), \
                mock.patch.object(sa_cli.sa_broker, "SingleInstanceGuard",
                                  FakeInstanceGuard), \
                mock.patch.object(sa_acceptance, "probe_environment",
                                  lambda: dict(environment or ACCEPTED_ENVIRONMENT)), \
                contextlib.redirect_stdout(stdout):
            code = sa_cli.main(argv)
        return code, stdout.getvalue(), built

    def test_cli_migrate_authenticates_without_mounting_and_cutover_is_the_first_mutation(self):
        h, final = self._first_run()
        original = tree_snapshot(final)
        self.assertTrue(original, "the fixture built an empty plaintext vault")
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED,
                         "precondition: no encrypted volume is mounted")
        self.assertTrue(h.broker.credentials.is_enrolled("vault"))

        events = []

        def note(label, **extra):
            events.append((label, tree_snapshot(final), extra))

        broker = h._build_broker()
        real_open = broker.open
        real_authenticate = broker.authenticate_only

        def open_spy(*args, **kwargs):
            note("open")
            return real_open(*args, **kwargs)

        def authenticate_spy(profile_id, force_auth=False):
            note("authenticate_begin", force_auth=force_auth)
            result = real_authenticate(profile_id, force_auth=force_auth)
            note("authenticate_end", ok=result.ok)
            return result

        broker.open = open_spy
        broker.authenticate_only = authenticate_spy

        real_mount = h.backend.unlock_and_mount

        def mount_spy(profile, secret):
            note("mount", mount_path=profile.mount_path)
            return real_mount(profile, secret)

        h.backend.unlock_and_mount = mount_spy
        real_mark = sa_migrate.MigrationJournal.mark

        def mark_spy(journal, step, **fields):
            note("journal:" + step)
            return real_mark(journal, step, **fields)

        real_rename = os.rename

        def rename_spy(src, dst, *args, **kwargs):
            note("rename", src=str(src), dst=str(dst))
            return real_rename(src, dst, *args, **kwargs)

        with mock.patch.object(sa_migrate.MigrationJournal, "mark", mark_spy), \
                mock.patch.object(sa_migrate.os, "rename", rename_spy):
            code, out, built = self._run_cli(
                h, ["migrate", "vault", "--source", str(final)], broker=broker)

        self.assertEqual(code, sa_cli.EXIT_OK, out)
        self.assertEqual(len(built), 1)
        self.assertNotIn(sa_acceptance.MIGRATION_BLOCKED, out)
        labels = [label for label, _tree, _extra in events]
        self.assertNotIn("open", labels,
                         "cmd_migrate called broker.open() before the cutover")

        # authentication: once, forced, successful, and the final path is
        # exactly what it was before and after it
        self.assertEqual(labels.count("authenticate_begin"), 1, labels)
        begin = labels.index("authenticate_begin")
        end = labels.index("authenticate_end")
        self.assertTrue(events[begin][2]["force_auth"])
        self.assertTrue(events[end][2]["ok"])
        self.assertEqual(events[begin][1], original)
        self.assertEqual(events[end][1], original)

        # staging migration begins after authentication, at the staging path
        mounts = [(index, extra["mount_path"])
                  for index, (label, _tree, extra) in enumerate(events)
                  if label == "mount"]
        self.assertTrue(mounts, "the migrator never mounted anything")
        staging = sa_paths.staging_mount_path(h.profile.container).lower()
        self.assertGreater(mounts[0][0], end)
        self.assertEqual(mounts[0][1].lower(), staging)
        for step in (sa_migrate.STEP_PRECHECK, sa_migrate.STEP_CREATE,
                     sa_migrate.STEP_COPY, sa_migrate.STEP_REVERIFY,
                     sa_migrate.STEP_CUTOVER_BEGIN):
            self.assertIn("journal:" + step, labels)
        self.assertGreater(labels.index("journal:" + sa_migrate.STEP_PRECHECK), end)

        # the cutover rename is the first mutation of the final path
        rename = labels.index("rename")
        for label, tree, _extra in events[:rename + 1]:
            self.assertEqual(tree, original,
                             "the final path changed before the cutover (at %s)" % label)
        self.assertLess(labels.index("journal:" + sa_migrate.STEP_CUTOVER_BEGIN), rename)
        renamed = events[rename][2]["src"]
        if renamed.startswith(sa_migrate.LONG_PATH_PREFIX):
            renamed = renamed[len(sa_migrate.LONG_PATH_PREFIX):]
        self.assertEqual(sa_paths.canonical(renamed).lower(),
                         sa_paths.canonical(str(final)).lower())
        final_mounts = [index for index, path in mounts
                        if path.lower() == sa_paths.canonical(str(final)).lower()]
        self.assertTrue(final_mounts and final_mounts[0] > rename,
                        "the volume was mounted at the final path before the rename")

        report = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["status"], [sa_migrate.MIGRATION_VERIFIED,
                                            sa_migrate.PLAINTEXT_SOURCE_REMAINS])
        self.assertEqual(tree_snapshot(report["plaintext_archive"]), original,
                         "the plaintext copy must survive byte for byte")

    def test_cli_migrate_without_acceptance_is_refused_before_a_broker_exists(self):
        h, final = self._first_run(acceptance=False)
        original = tree_snapshot(final)
        calls_before = list(h.backend.calls)
        code, out, built = self._run_cli(
            h, ["migrate", "vault", "--source", str(final)])
        self.assertEqual(code, sa_cli.EXIT_REFUSED, out)
        self.assertEqual(built, [], "a broker was built for a refused migration")
        self.assertEqual(out.splitlines()[0], sa_acceptance.MIGRATION_BLOCKED)
        self.assertIn("--disposable-acceptance", out)
        self.assertIn("secure_apps_interactive.py", out)
        self.assertEqual(tree_snapshot(final), original)
        self.assertEqual(h.backend.calls, calls_before)
        self.assertFalse(Path(h.registry.state_dir, "migration-vault.json").exists())
        self.assertIn('"hardware_acceptance_required"', h.audit_text())

    def test_cli_migrate_with_a_stale_acceptance_is_refused(self):
        stale = dict(ACCEPTED_ENVIRONMENT, implementation_fingerprint="f" * 64)
        h, final = self._first_run(environment=stale)
        code, out, built = self._run_cli(
            h, ["migrate", "vault", "--source", str(final)])
        self.assertEqual(code, sa_cli.EXIT_REFUSED, out)
        self.assertEqual(built, [])
        self.assertEqual(out.splitlines()[0], sa_acceptance.MIGRATION_BLOCKED)
        self.assertIn("Secure Apps implementation changed", out)

    def test_cli_migrate_with_a_changed_acceptance_producer_is_refused(self):
        """A weakened acceptance runner invalidates what it once vouched for."""
        h, final = self._first_run(
            environment=dict(ACCEPTED_ENVIRONMENT,
                             acceptance_producer_fingerprint="d" * 64))
        code, out, built = self._run_cli(
            h, ["migrate", "vault", "--source", str(final)])
        self.assertEqual(code, sa_cli.EXIT_REFUSED, out)
        self.assertEqual(built, [])
        self.assertEqual(out.splitlines()[0], sa_acceptance.MIGRATION_BLOCKED)
        self.assertIn("acceptance producer changed", out)

    def test_cli_migrate_after_a_security_policy_edit_is_refused(self):
        """The record was written for the policy that was in force then."""
        h, final = self._first_run()
        h.profile.policy.lock_on_windows_lock = False
        code, out, built = self._run_cli(
            h, ["migrate", "vault", "--source", str(final)])
        self.assertEqual(code, sa_cli.EXIT_REFUSED, out)
        self.assertEqual(built, [])
        self.assertEqual(out.splitlines()[0], sa_acceptance.MIGRATION_BLOCKED)
        self.assertIn("security-relevant profile configuration changed", out)

    def test_cli_migrate_from_an_elevated_process_is_refused(self):
        """Medium integrity is required of the migration, not merely matched."""
        h, final = self._first_run()
        code, out, built = self._run_cli(
            h, ["migrate", "vault", "--source", str(final)],
            environment=dict(ACCEPTED_ENVIRONMENT, broker_elevated=True))
        self.assertEqual(code, sa_cli.EXIT_REFUSED, out)
        self.assertEqual(built, [])
        self.assertEqual(out.splitlines()[0], sa_acceptance.MIGRATION_BLOCKED)
        self.assertIn(sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY, out)

    def test_cli_migrate_with_an_elevated_record_is_refused(self):
        """Even agreeing on ELEVATED is refused: it is not the architecture."""
        h, final = self._first_run(
            environment=dict(ACCEPTED_ENVIRONMENT, broker_elevated=True))
        record_path = Path(sa_acceptance.record_path(h.registry.state_dir))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertFalse(record["migration_ready"],
                         "an elevated run must never record readiness")
        record["migration_ready"] = True            # forge it anyway
        record_path.write_text(json.dumps(record), encoding="utf-8")
        code, out, built = self._run_cli(
            h, ["migrate", "vault", "--source", str(final)],
            environment=dict(ACCEPTED_ENVIRONMENT, broker_elevated=True))
        self.assertEqual(code, sa_cli.EXIT_REFUSED, out)
        self.assertEqual(built, [])
        self.assertIn(sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY, out)

    def test_cli_migrate_with_a_failed_gate_is_refused(self):
        gates = all_gates()
        gates["helper_failure_accepted"] = False
        h, final = self._first_run(gates=gates)
        code, out, built = self._run_cli(
            h, ["migrate", "vault", "--source", str(final)])
        self.assertEqual(code, sa_cli.EXIT_REFUSED, out)
        self.assertEqual(built, [])
        self.assertIn("helper_failure_accepted was not accepted", out)
        self.assertIn("did not grant migration readiness", out)

    def test_check_acceptance_recognises_the_record_and_touches_nothing(self):
        h, final = self._first_run()
        original = tree_snapshot(final)
        calls_before = list(h.backend.calls)
        code, out, built = self._run_cli(
            h, ["migrate", "vault", "--check-acceptance"])
        self.assertEqual(code, sa_cli.EXIT_OK, out)
        self.assertEqual(built, [])
        lines = out.splitlines()
        for token in sa_acceptance.FINAL_TOKENS:
            self.assertIn(token, lines)
        self.assertEqual(tree_snapshot(final), original)
        self.assertEqual(h.backend.calls, calls_before)

    def test_a_real_migration_still_needs_a_source(self):
        h, _final = self._first_run()
        code, out, built = self._run_cli(h, ["migrate", "vault"])
        self.assertEqual(code, sa_cli.EXIT_USAGE, out)
        self.assertEqual(built, [])


class EnrollmentAcknowledgementTests(BaseCase):
    """PREPARE -> ACCEPT / DECLINE / TIMEOUT / CANCEL, and what each leaves."""

    def test_accept_enrolls_and_reports_enrollment_ready(self):
        h = self.harness()
        pending, recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        self.assertEqual(pending.state, sa_enroll.ENROLL_AWAITING_RECOVERY_ACK)
        self.assertFalse(pending.resolved)
        result = pending.accept()
        self.assertTrue(result.ok)
        self.assertEqual(pending.state, sa_enroll.ENROLLMENT_READY)
        self.assertTrue(result.credential_id_hash)
        self.assertTrue(h.broker.credentials.is_enrolled("vault"))
        self.assertTrue(recovery)

    def test_decline_zeroizes_and_removes_the_container(self):
        h = self.harness()
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        result = pending.decline()
        self.assertFalse(result.ok)
        self.assertEqual(pending.state, sa_enroll.ENROLL_ROLLED_BACK)
        self.assertEqual(result.error_category, "config_invalid")
        self.assertFalse(h.broker.credentials.is_enrolled("vault"))
        self.assertEqual(h.backend.state(h.profile), sa_storage.MISSING)
        self.assertIsNone(pending._secret)
        self.assertIn("enroll_recovery_declined", h.audit_text())

    def test_timeout_is_not_consent(self):
        h = self.harness()
        pending, _recovery = sa_enroll.begin_enrollment(
            h.broker, "vault", timeout_seconds=600, clock=h.clock)
        self.assertFalse(pending.expired)
        self.assertGreater(pending.seconds_remaining(), 0)
        h.clock.advance(601)
        self.assertTrue(pending.expired)
        result = pending.decline(timed_out=True)
        self.assertFalse(result.ok)
        self.assertIsNone(pending._secret)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MISSING)
        self.assertIn("enroll_recovery_timeout", h.audit_text())

    def test_accept_after_the_deadline_declines_instead(self):
        h = self.harness()
        pending, _recovery = sa_enroll.begin_enrollment(
            h.broker, "vault", timeout_seconds=600, clock=h.clock)
        h.clock.advance(601)
        result = pending.accept()
        self.assertFalse(result.ok, "an expired acknowledgement was honoured")
        self.assertFalse(h.broker.credentials.is_enrolled("vault"))

    def test_cancel_rolls_back_like_a_gui_shutdown(self):
        h = self.harness()
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        result = pending.cancel("Secure Apps closed")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "Secure Apps closed")
        self.assertIsNone(pending._secret)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MISSING)
        self.assertIn("enroll_cancelled", h.audit_text())

    def test_a_resolved_enrollment_cannot_be_resolved_twice(self):
        h = self.harness()
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        pending.decline()
        with self.assertRaises(sa_enroll.EnrollmentRefused):
            pending.accept()
        with self.assertRaises(sa_enroll.EnrollmentRefused):
            pending.decline()

    def test_accept_with_attached_staging_is_cleanup_required_not_ready(self):
        """SRC-027 W2-002: READY only once staging is verified detached."""
        h = self.harness()
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        h.backend.fail_unmount = "cannot detach"
        result = pending.accept()
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(result.state, sa_enroll.ENROLL_CLEANUP_REQUIRED)
        self.assertTrue(pending.cleanup_required)
        self.assertNotIn("detached", result.reason.replace("still attached", ""))
        # The enrollment itself is valid and must not be thrown away ...
        self.assertTrue(h.broker.credentials.is_enrolled("vault"))
        # ... and the staging volume is honestly still mounted.
        self.assertEqual(h.backend.state(pending.staged_profile), sa_storage.MOUNTED)
        self.assertNotIn("enrollment_ready", h.audit_text())

    def test_cleanup_retry_reaches_ready_only_after_detach(self):
        h = self.harness()
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        h.backend.fail_unmount = "cannot detach"
        pending.accept()
        again = pending.retry_cleanup()
        self.assertFalse(again.ok)
        self.assertEqual(pending.state, sa_enroll.ENROLL_CLEANUP_REQUIRED)
        h.backend.fail_unmount = None
        done = pending.retry_cleanup()
        self.assertTrue(done.ok, done.to_dict())
        self.assertEqual(pending.state, sa_enroll.ENROLLMENT_READY)
        self.assertEqual(h.backend.state(pending.staged_profile), sa_storage.DETACHED)
        self.assertTrue(done.credential_id_hash)
        with self.assertRaises(sa_enroll.EnrollmentRefused):
            pending.retry_cleanup()

    def test_decline_with_unremovable_container_is_cleanup_required(self):
        h = self.harness()
        pending, _recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        with mock.patch.object(h.backend, "destroy",
                               side_effect=sa_storage.StorageError("in use")):
            result = pending.decline()
        self.assertFalse(result.ok)
        self.assertEqual(result.state, sa_enroll.ENROLL_CLEANUP_REQUIRED)
        self.assertNotEqual(result.state, sa_enroll.ENROLL_ROLLED_BACK)
        self.assertNotIn("was removed", result.reason)
        self.assertIsNone(pending._secret)
        done = pending.retry_cleanup()
        self.assertEqual(done.state, sa_enroll.ENROLL_ROLLED_BACK)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MISSING)

    def test_the_recovery_password_is_not_retained_anywhere(self):
        h = self.harness()
        pending, recovery = sa_enroll.begin_enrollment(h.broker, "vault")
        self.addCleanup(lambda: pending.resolved or pending.cancel("test"))
        self.assertTrue(recovery)
        blob = repr(vars(pending))
        self.assertNotIn(recovery, blob)
        self.assertNotIn(recovery, h.audit_text())

    def test_the_synchronous_wrapper_still_works_for_the_cli(self):
        h = self.harness()
        shown = []
        result = sa_enroll.create_and_enroll(
            h.broker, "vault",
            lambda text, profile: shown.append(text) or True)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(shown), 1)
        self.assertEqual(result.state, sa_enroll.ENROLLMENT_READY)

    def test_a_raising_acknowledgement_rolls_back(self):
        h = self.harness()

        def explode(text, profile):
            raise RuntimeError("the dialog crashed")

        with self.assertRaises(RuntimeError):
            sa_enroll.create_and_enroll(h.broker, "vault", explode)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MISSING)
        self.assertFalse(h.broker.credentials.is_enrolled("vault"))


# ══════════════════════════════════════════════ user verification
class UserVerificationPolicyTests(BaseCase):
    def test_the_default_is_required(self):
        h = self.harness()
        self.assertEqual(h.profile.user_verification, "required")
        self.assertEqual(sa_config.DEFAULT_USER_VERIFICATION, "required")

    def test_an_unknown_value_is_refused(self):
        root = Path(tempfile.mkdtemp(prefix="saituls-secapps-uvcfg-"))
        self.addCleanup(shutil.rmtree, str(root), ignore_errors=True)
        document = registry_document(
            root, root / "mount",
            profile_overrides={"authentication": {"provider": "fake-auth",
                                                  "user_verification": "maybe"}})
        with self.assertRaises(sa_config.ConfigError):
            sa_config.parse_registry(document, managed_root=str(root),
                                     allow_test_providers=True)

    def test_the_shipped_obsidian_profile_requires_user_verification(self):
        registry = sa_config.load_registry(
            str(SUBSYSTEM / "secure_apps.json"),
            managed_root=str(Path(tempfile.gettempdir()) / "saituls-secapps-uv"))
        profile = registry.get("obsidian")
        self.assertEqual(profile.user_verification, "required")
        self.assertIn("user_verification", profile.to_dict()["authentication"])

    def test_the_registry_policy_reaches_the_provider(self):
        h = self.harness()
        provider = h.broker.provider(h.profile)
        self.assertEqual(provider.user_verification, "required")

    def test_enrollment_refuses_when_the_key_has_no_user_verification(self):
        h = self.harness()
        h.provider.user_verification_available = False
        with self.assertRaises(sa_auth.AuthCapabilityError):
            sa_enroll.create_and_enroll(h.broker, "vault",
                                        lambda text, profile: True)
        self.assertFalse(h.broker.credentials.is_enrolled("vault"))
        self.assertEqual(h.backend.state(h.profile), sa_storage.MISSING)

    def test_enrollment_refuses_when_the_key_skips_user_verification(self):
        h = self.harness()
        h.provider.performs_user_verification = False
        with self.assertRaises(sa_auth.AuthCapabilityError):
            sa_enroll.create_and_enroll(h.broker, "vault",
                                        lambda text, profile: True)
        self.assertFalse(h.broker.credentials.is_enrolled("vault"))

    def test_unlocking_refuses_when_user_verification_stops_happening(self):
        """Enrolled with UV, then the key answers with presence only."""
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        h.provider.performs_user_verification = False
        result = h.broker.open("vault")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "auth_capability")

    def test_a_discouraged_profile_still_enrolls_without_verification(self):
        h = self.harness(profile_overrides={
            "authentication": {"user_verification": "discouraged"}})
        h.provider.user_verification_available = False
        h.provider.performs_user_verification = False
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)

    def test_capabilities_report_user_verification_and_transport(self):
        h = self.harness()
        caps = sa_enroll.capabilities(h.broker, "vault")
        self.assertIn("transport", caps)
        self.assertIn("user_verification", caps)
        self.assertIn(caps["transport"], sa_auth.TRANSPORTS)

    def test_the_helper_contract_refuses_an_unproven_uv_claim(self):
        """A helper that will not say UV happened does not get to imply it."""

        class Completed(object):
            def __init__(self, payload):
                self.stdout = json.dumps(payload)
                self.returncode = 0

        payloads = []

        def runner(argv, timeout):
            payloads.append(argv)
            return Completed({"ok": True,
                              "credential_id": sa_auth.b64e(b"cred-1"),
                              "user_id": sa_auth.b64e(b"user"),
                              "output": sa_auth.b64e(b"\x02" * 32)})

        tmp = Path(tempfile.mkdtemp(prefix="saituls-secapps-helper-"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        helper_path = str(tmp / "helper.exe")
        Path(helper_path).write_bytes(b"MZ fake")
        provider = sa_auth.ExternalHelperProvider(
            helper_path, runner=runner,
            user_verification=sa_auth.UV_REQUIRED)
        with self.assertRaises(sa_auth.AuthCapabilityError):
            provider.create_credential("primary")
        with self.assertRaises(sa_auth.AuthCapabilityError):
            provider.get_key_material([b"cred-1"], b"\x00" * 32)
        # The policy travels to the helper on the command line, where it is
        # not a secret; the PIN never does.
        self.assertIn("--user-verification", payloads[0])
        self.assertIn("required", payloads[0])
        self.assertTrue(all("pin" not in str(a).lower() for a in payloads[0]))


# ══════════════════════════════════════════════ GUI threading regressions
#: Hermetic tests must not depend on what this particular machine happens to
#: have installed. When a REAL privileged helper is registered, the GUI worker
#: genuinely tries to reach it -- and, in an elevated test session, the helper
#: correctly refuses an elevated client, so the window sits waiting on a
#: handshake that is designed never to succeed. Pointing the pin at a path
#: that does not exist makes the subsystem report PRIVILEGED_HELPER_NOT_INSTALLED
#: deterministically, which is the state these tests are written against.
def isolate_from_installed_privileged_helper(case):
    patcher = mock.patch.dict(
        os.environ,
        {sa_privtask.PIN_PATH_ENV: str(Path(tempfile.gettempdir())
                                       / "saituls-tests-no-such-pin.json")})
    patcher.start()
    case.addCleanup(patcher.stop)


class GuiThreadingBase(unittest.TestCase):
    """Real QThread, real queued signals. No mocked Qt anywhere.

    These are the regressions for two defects that only exist across the
    GUI/worker thread boundary, so nothing short of a real event loop proves
    them fixed.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyQt6 is not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    def setUp(self):
        isolate_from_installed_privileged_helper(self)

    def gui_module(self):
        # The GUI is imported like any other module. Loading it by path meant
        # the tests could keep passing against a file whose name no user
        # launches, and left two copies of the window free to drift apart.
        import importlib
        return importlib.import_module("secure_apps_gui")

    def application(self):
        from PyQt6.QtWidgets import QApplication
        return QApplication.instance() or QApplication([])

    def pump_until(self, predicate, timeout_seconds=15.0):
        """Run the GUI event loop until *predicate* holds. Returns elapsed s."""
        app = self.application()
        start = time.monotonic()
        while not predicate():
            if time.monotonic() - start > timeout_seconds:
                return None
            app.processEvents()
            time.sleep(0.005)
        return time.monotonic() - start


class GuiEnrollmentAckTests(GuiThreadingBase):
    """The deadlock: the answer could not reach the thread waiting for it."""

    def setUp(self):
        from PyQt6.QtCore import QObject, QThread, pyqtSignal

        self.module = self.gui_module()
        self.app = self.application()
        self.harness = Harness()
        self.addCleanup(self.harness.close)

        class Driver(QObject):
            enroll = pyqtSignal(str, bool)
            ack = pyqtSignal(bool)
            tick = pyqtSignal()
            shutdown = pyqtSignal()

        self.worker = self.module.BrokerWorker(
            str(self.harness.registry.credentials_path))
        # The worker normally builds its own broker in bootstrap(); this one
        # gets the deterministic harness broker instead, so the test needs no
        # security key, no elevation and no disk image.
        self.worker.broker = self.harness.broker
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.driver = Driver()
        self.driver.enroll.connect(self.worker.enroll)
        self.driver.ack.connect(self.worker.acknowledgeRecovery)
        self.driver.tick.connect(self.worker.tick)
        self.driver.shutdown.connect(self.worker.shutdown)

        self.recovery = []
        self.done = []
        self.shutdowns = []
        self.worker.recoveryMaterial.connect(
            lambda pid, text: self.recovery.append((pid, text)))
        self.worker.enrollDone.connect(lambda payload: self.done.append(payload))
        self.worker.shutdownComplete.connect(
            lambda payload: self.shutdowns.append(payload))
        self.thread.start()
        self.addCleanup(self._stop_thread)

    def _stop_thread(self):
        self.thread.quit()
        self.thread.wait(5000)

    def test_acknowledgement_completes_immediately(self):
        """The regression: this used to sit in a ten-minute sleep loop."""
        self.driver.enroll.emit("vault", False)
        self.assertIsNotNone(self.pump_until(lambda: self.recovery),
                             "the recovery material never reached the GUI")
        self.assertEqual(self.recovery[0][0], "vault")
        self.assertTrue(self.recovery[0][1])

        started = time.monotonic()
        self.driver.ack.emit(True)
        elapsed = self.pump_until(lambda: self.done)
        self.assertIsNotNone(elapsed, "the acknowledgement was never processed")
        self.assertLess(time.monotonic() - started, 5.0,
                        "acknowledging the recovery material blocked")
        payload = self.done[0]
        self.assertTrue(payload.get("ok"), payload.get("reason"))
        self.assertEqual(payload.get("state"), sa_enroll.ENROLLMENT_READY)
        self.assertTrue(self.harness.broker.credentials.is_enrolled("vault"))

    def test_the_worker_thread_stays_responsive_while_the_modal_is_open(self):
        """Proof the worker is not blocked: other slots still run."""
        self.driver.enroll.emit("vault", False)
        self.assertIsNotNone(self.pump_until(lambda: self.recovery))
        statuses = []
        self.worker.statusReady.connect(lambda rows: statuses.append(rows))
        self.driver.tick.emit()
        self.assertIsNotNone(self.pump_until(lambda: statuses, 5.0),
                             "the worker thread was blocked by the pending "
                             "acknowledgement")
        self.driver.ack.emit(False)
        self.assertIsNotNone(self.pump_until(lambda: self.done))

    def test_declining_rolls_the_container_back(self):
        self.driver.enroll.emit("vault", False)
        self.assertIsNotNone(self.pump_until(lambda: self.recovery))
        self.driver.ack.emit(False)
        self.assertIsNotNone(self.pump_until(lambda: self.done))
        payload = self.done[0]
        self.assertFalse(payload.get("ok"))
        self.assertFalse(self.harness.broker.credentials.is_enrolled("vault"))
        self.assertEqual(self.harness.backend.state(self.harness.profile),
                         sa_storage.MISSING)

    def test_an_unanswered_dialog_times_out_and_rolls_back(self):
        self.worker._recovery_ack_timeout = 0.2
        self.driver.enroll.emit("vault", False)
        self.assertIsNotNone(self.pump_until(lambda: self.recovery))
        time.sleep(0.3)
        self.driver.tick.emit()                  # the GUI timer drives expiry
        self.assertIsNotNone(self.pump_until(lambda: self.done))
        payload = self.done[0]
        self.assertFalse(payload.get("ok"))
        self.assertIn("not acknowledged", payload.get("reason", "").lower() +
                      payload.get("reason", ""))
        self.assertEqual(self.harness.backend.state(self.harness.profile),
                         sa_storage.MISSING)

    def test_shutdown_cancels_an_unresolved_enrollment(self):
        self.driver.enroll.emit("vault", False)
        self.assertIsNotNone(self.pump_until(lambda: self.recovery))
        self.driver.shutdown.emit()
        self.assertIsNotNone(self.pump_until(lambda: self.shutdowns))
        self.assertTrue(self.done, "the cancelled enrollment was not reported")
        self.assertFalse(self.done[0].get("ok"))
        self.assertEqual(self.harness.backend.state(self.harness.profile),
                         sa_storage.MISSING)

    def test_a_second_enrollment_is_refused_while_one_is_pending(self):
        self.driver.enroll.emit("vault", False)
        self.assertIsNotNone(self.pump_until(lambda: self.recovery))
        self.driver.enroll.emit("vault", False)
        self.assertIsNotNone(self.pump_until(lambda: self.done))
        self.assertFalse(self.done[0].get("ok"))
        self.assertEqual(self.done[0].get("error_category"), "concurrent_request")
        self.assertEqual(len(self.recovery), 1,
                         "a second recovery material was shown")


class GuiShutdownBarrierTests(GuiThreadingBase):
    """Closing the window must not race process termination."""

    def setUp(self):
        from PyQt6.QtCore import QObject, QThread, pyqtSignal

        self.module = self.gui_module()
        self.app = self.application()
        self.harness = Harness()
        self.addCleanup(self.harness.close)

        class Driver(QObject):
            shutdown = pyqtSignal()

        self.worker = self.module.BrokerWorker(
            str(self.harness.registry.credentials_path))
        self.worker.broker = self.harness.broker
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.driver = Driver()
        self.driver.shutdown.connect(self.worker.shutdown)
        self.reports = []
        self.worker.shutdownComplete.connect(lambda r: self.reports.append(r))
        self.thread.start()
        self.addCleanup(self._stop_thread)

    def _stop_thread(self):
        self.thread.quit()
        self.thread.wait(5000)

    def test_shutdown_reports_a_clean_lock(self):
        self.assertTrue(self.harness.enroll().ok)
        self.assertTrue(self.harness.broker.open("vault").ok)
        self.driver.shutdown.emit()
        self.assertIsNotNone(self.pump_until(lambda: self.reports))
        report = self.reports[0]
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["recovery_required"], [])
        # Application closed, volume detached, session gone.
        runtime = self.harness.broker.runtime("vault")
        self.assertFalse(runtime.supervisor.running)
        self.assertEqual(self.harness.backend.state(self.harness.profile),
                         sa_storage.DETACHED)
        self.assertIsNone(runtime.session)

    def test_unremovable_cancelled_enrollment_blocks_a_clean_shutdown(self):
        """SRC-027 W2-002: rollback failure is part of the shutdown verdict."""
        pending, _recovery = sa_enroll.begin_enrollment(self.harness.broker, "vault")
        self.worker._pending = pending
        with mock.patch.object(self.harness.backend, "destroy",
                               side_effect=sa_storage.StorageError("in use")):
            self.driver.shutdown.emit()
            self.assertIsNotNone(self.pump_until(lambda: self.reports))
        report = self.reports[0]
        self.assertFalse(report["ok"], report)
        self.assertIn("vault", report["recovery_required"])
        self.assertIn("enrollment", report["reason"])
        self.assertIs(self.worker._cleanup_pending, pending)
        # The barrier holds; the next close attempt retries and can succeed.
        self.driver.shutdown.emit()
        self.assertIsNotNone(self.pump_until(lambda: len(self.reports) > 1))
        report = self.reports[1]
        self.assertTrue(report["ok"], report)
        self.assertIsNone(self.worker._cleanup_pending)
        self.assertEqual(pending.state, sa_enroll.ENROLL_ROLLED_BACK)

    def test_a_busy_volume_reports_recovery_required_instead_of_ok(self):
        """Storage-busy failure: shutdown must not claim success."""
        self.assertTrue(self.harness.enroll().ok)
        self.assertTrue(self.harness.broker.open("vault").ok)
        self.harness.backend.busy_on_unmount = True
        self.driver.shutdown.emit()
        self.assertIsNotNone(self.pump_until(lambda: self.reports))
        report = self.reports[0]
        self.assertFalse(report["ok"], report)
        self.assertIn("vault", report["recovery_required"])
        self.assertIn("detach", report["reason"])
        # The broker stops at ERROR rather than claiming LOCKED: the volume
        # may still be attached, and the GUI must not let the process go.
        status = self.harness.broker.status("vault")
        self.assertEqual(status["state"], sa_state.ERROR)
        self.assertEqual(status["last_error_category"], "storage_busy")


class GuiWindowCloseTests(GuiThreadingBase):
    """The window itself: close() is a request, and it can be refused."""

    def build_window(self):
        from PyQt6.QtWidgets import QMessageBox

        module = self.gui_module()
        self.app = self.application()
        self.app.setStyleSheet(module.QSS)
        tmp = Path(tempfile.mkdtemp(prefix="saituls-gui-close-"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        previous = os.environ.get(sa_config.MANAGED_ROOT_ENV)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(tmp)
        if previous is None:
            self.addCleanup(os.environ.pop, sa_config.MANAGED_ROOT_ENV, None)
        else:
            self.addCleanup(os.environ.__setitem__,
                            sa_config.MANAGED_ROOT_ENV, previous)
        self.dialogs = []
        original = QMessageBox.critical
        QMessageBox.critical = staticmethod(
            lambda *args, **kwargs: self.dialogs.append(args))
        self.addCleanup(lambda: setattr(QMessageBox, "critical", original))

        window = module.SecureAppsWindow(sa_config.default_registry_path())
        self.addCleanup(self._force_close, window)
        window.show()
        self.pump_until(lambda: window.worker.broker is not None, 15.0)
        return module, window

    def _force_close(self, window):
        window._shutdown_state = window.SHUTDOWN_DONE
        try:
            window.close()
        except Exception:
            pass
        window.thread.quit()
        window.thread.wait(5000)

    def test_close_does_not_quit_the_worker_thread_immediately(self):
        """The regression: quit() used to be the statement after emit()."""
        _module, window = self.build_window()
        accepted = window.close()
        self.assertFalse(accepted, "closeEvent accepted before the vaults were "
                                   "secured")
        self.assertEqual(window._shutdown_state, window.SHUTDOWN_RUNNING)
        self.assertTrue(window.thread.isRunning(),
                        "the worker thread was stopped before shutdown finished")
        self.assertTrue(window.isVisible())
        self.assertIsNotNone(
            self.pump_until(lambda: window._shutdown_state == window.SHUTDOWN_DONE),
            "secure shutdown never completed")
        self.assertFalse(window.isVisible())
        self.assertFalse(window.thread.isRunning())

    def test_a_failed_shutdown_blocks_exit_and_reports_recovery_required(self):
        _module, window = self.build_window()

        def failing_shutdown():
            window.worker.shutdownComplete.emit(
                {"ok": False, "recovery_required": ["obsidian"],
                 "reason": "the volume would not detach"})

        window.requestShutdown.disconnect()
        window.requestShutdown.connect(failing_shutdown)
        self.assertFalse(window.close())
        self.assertIsNotNone(
            self.pump_until(lambda: window._shutdown_state == window.SHUTDOWN_FAILED))
        self.assertTrue(window.isVisible(), "a failed shutdown let the window go")
        self.assertTrue(self.dialogs, "no RECOVERY REQUIRED report was shown")
        self.assertIn("RECOVERY", window.windowTitle().upper())
        # Trying again keeps refusing, and keeps saying why.
        before = len(self.dialogs)
        self.assertFalse(window.close())
        self.assertEqual(len(self.dialogs), before + 1)

    def test_a_shutdown_that_never_answers_times_out_and_blocks_exit(self):
        """Forced-close timeout: a stuck broker must not become a silent exit."""
        _module, window = self.build_window()
        window.SHUTDOWN_TIMEOUT_MS = 300
        window.requestShutdown.disconnect()      # nothing will ever answer
        self.assertFalse(window.close())
        self.assertEqual(window._shutdown_state, window.SHUTDOWN_RUNNING)
        self.assertIsNotNone(
            self.pump_until(lambda: window._shutdown_state == window.SHUTDOWN_FAILED,
                            10.0))
        self.assertTrue(window.isVisible())
        self.assertTrue(self.dialogs)
        self.assertIn("did not finish", window._shutdown_report["reason"])


# ══════════════════════════════════════════════ window
class WindowSmokeTests(unittest.TestCase):
    """Offscreen construction of the real window. Renders nothing visible.

    Skipped where PyQt6 is absent -- CI deliberately does not install it, so
    that the lifecycle suites prove they need no GUI toolkit. Run locally
    with PyQt6 present and this exercises the Qt wiring, the Golden Default
    stylesheet and the worker-thread handshake against a throwaway root.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyQt6 is not installed")

    def test_window_builds_and_renders_the_profile_row(self):
        import importlib
        isolate_from_installed_privileged_helper(self)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication

        tmp = Path(tempfile.mkdtemp(prefix="saituls-gui-smoke-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        previous = os.environ.get(sa_config.MANAGED_ROOT_ENV)
        os.environ[sa_config.MANAGED_ROOT_ENV] = str(tmp)
        if previous is None:
            self.addCleanup(os.environ.pop, sa_config.MANAGED_ROOT_ENV, None)
        else:
            self.addCleanup(os.environ.__setitem__,
                            sa_config.MANAGED_ROOT_ENV, previous)

        module = importlib.import_module("secure_apps_gui")

        app = QApplication.instance() or QApplication([])
        app.setStyleSheet(module.QSS)
        window = module.SecureAppsWindow(sa_config.default_registry_path())
        window.show()
        captured = {}

        def finish():
            captured["rows"] = list(window._rows)
            window.close()
            app.quit()

        QTimer.singleShot(4000, finish)
        app.exec()

        rows = captured.get("rows") or []
        self.assertTrue(rows, "the window rendered no profile rows")
        self.assertEqual(rows[0]["profile_id"], "obsidian")
        self.assertIn(rows[0]["state"], sa_state.ALL_STATES)
        self.assertIn(rows[0]["state"], module.STATE_TEXT)
        # Every state the broker can report must have a WORD in the window.
        for state in sa_state.ALL_STATES:
            self.assertIn(state, module.STATE_TEXT)
            self.assertIn(state, module.VAULT_TEXT)
            self.assertTrue(module.STATE_TEXT[state].startswith(state))


# ══════════════════════════════════════════════ physical state truth (SRC-027 W2-001, W2-006)
class PhysicalStateTruthTests(BaseCase):
    """LOCKED is a physical-protection assertion, not an auth-session state.

    SRC-027 W2-001: a transition to LOCKED must only happen once storage is
    proven detached. W2-006: a failed unmount keeps the privileged-helper lease
    so recovery stays possible.
    """

    def test_idle_expiry_reports_error_not_locked_when_unmount_fails(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        # Close the application: idle expiry then takes the direct detach
        # branch (no app running, must unmount before LOCKED).
        pid = h.adapter.trees[list(h.adapter.trees)[0]][0]
        h.adapter.exit_tree(pid)
        h.backend.fail_unmount = "cannot detach"
        result = h.broker._handle_idle_expiry(h.broker.runtime("vault"))
        self.assertFalse(result.ok)
        self.assertEqual(result.state, sa_state.ERROR)
        status = h.broker.status("vault")
        self.assertEqual(status["state"], sa_state.ERROR, status)
        self.assertNotEqual(status["state"], sa_state.LOCKED)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)

    def test_idle_expiry_succeeds_when_detach_confirmed(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        # Close the app so idle expiry takes the direct detach branch.
        pid = h.adapter.trees[list(h.adapter.trees)[0]][0]
        h.adapter.exit_tree(pid)
        result = h.broker._handle_idle_expiry(h.broker.runtime("vault"))
        if result is not None:
            self.assertTrue(result.ok)
        status = h.broker.status("vault")
        self.assertEqual(status["state"], sa_state.LOCKED, status)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)

    def test_aggressive_switch_reports_error_not_locked_on_unmount_failure(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        # Simulate the "cached session, app closed, volume still mounted"
        # precondition: detach the supervisor without running tick (which
        # would otherwise unmount first), so the aggressive switch itself
        # must perform the detach.
        h.adapter.exit_tree(h.broker.runtime("vault").supervisor.pid)
        h.backend.fail_unmount = "cannot detach"
        result = h.broker.set_mode("vault", "aggressive")
        self.assertFalse(result.ok, result)
        self.assertEqual(result.state, sa_state.ERROR)
        status = h.broker.status("vault")
        self.assertEqual(status["state"], sa_state.ERROR, status)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)

    def test_aggressive_switch_succeeds_when_detach_confirmed(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        pid = h.adapter.trees[list(h.adapter.trees)[0]][0]
        h.adapter.exit_tree(pid)
        h.broker._tick_profile(h.broker.runtime("vault"))
        result = h.broker.set_mode("vault", "aggressive")
        self.assertTrue(result.ok, result)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        status = h.broker.status("vault")
        self.assertEqual(status["state"], sa_state.LOCKED, status)

    def test_failed_unmount_keeps_helper_holding(self):
        """SRC-027 W2-006: a failed unmount must retain the helper lease."""
        h = self.harness()
        # FakeStorageBackend has no real helper lease; assert the real
        # BitLockerVhdxBackend contract instead: unmount releases only on
        # verified detach. Here we prove _ensure_detached does not claim a
        # lease release after a failed unmount (the fake returns False and we
        # surface ERROR rather than LOCKED).
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        h.backend.fail_unmount = "cannot detach"
        ok, cat = h.broker._ensure_detached(h.broker.runtime("vault"), "test")
        self.assertFalse(ok)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)


# ══════════════════════════════════════════════ credential store atomicity (SRC-027 W2-007)
class CredentialStoreAtomicityTests(BaseCase):
    """Memory and disk agree after every refusal and every failed write."""

    def _store(self, count=1):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        store = h.broker.credentials
        first = store.enrollments("vault")[0]
        ids = [first.credential_id]
        for index in range(1, count):
            data = first.to_dict()
            other = bytes([index]) * len(first.credential_id)
            data["credential_id"] = sa_auth.b64e(other)
            store.add("vault", store.container_id("vault"),
                      sa_auth.Enrollment.from_dict(data))
            ids.append(other)
        return h, store, ids

    def _views(self, h, store):
        memory = [e.credential_id for e in store.enrollments("vault")]
        disk = [e.credential_id for e in
                sa_auth.CredentialStore(h.registry.credentials_path).enrollments("vault")]
        return memory, disk

    def test_refusing_the_last_credential_changes_neither_view(self):
        h, store, ids = self._store(1)
        with self.assertRaises(sa_auth.AuthError):
            store.remove("vault", ids[0])
        self.assertEqual(self._views(h, store), (ids, ids))
        # No ghost deletion appears on a retry either.
        with self.assertRaises(sa_auth.AuthError):
            store.remove("vault", ids[0])
        self.assertTrue(store.is_enrolled("vault"))

    def test_unknown_credential_changes_neither_view(self):
        h, store, ids = self._store(2)
        with self.assertRaises(sa_auth.NotEnrolledError):
            store.remove("vault", b"\xff" * len(ids[0]))
        self.assertEqual(self._views(h, store), (ids, ids))

    def test_failed_write_keeps_the_old_document_in_memory_and_on_disk(self):
        h, store, ids = self._store(2)
        with mock.patch.object(sa_auth.os, "replace",
                               side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                store.remove("vault", ids[1])
        self.assertEqual(self._views(h, store), (ids, ids))

    def test_failed_write_on_add_keeps_memory_unchanged(self):
        h, store, ids = self._store(1)
        data = store.enrollments("vault")[0].to_dict()
        data["credential_id"] = sa_auth.b64e(b"\x07" * len(ids[0]))
        with mock.patch.object(sa_auth.os, "replace",
                               side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                store.add("vault", store.container_id("vault"),
                          sa_auth.Enrollment.from_dict(data))
        self.assertEqual(self._views(h, store), (ids, ids))

    def test_legal_removal_updates_both_and_survives_reload(self):
        h, store, ids = self._store(2)
        store.remove("vault", ids[1])
        self.assertEqual(self._views(h, store), ([ids[0]], [ids[0]]))


# ══════════════════════════════════════════════ staged application import (SRC-027 W2-005)
class StagedImportTests(BaseCase):
    """A replacement never costs the known-good managed application."""

    def _installed(self):
        h = self.harness()
        app = h.root / "apps" / "vault" / "app"
        (app / "data").mkdir(parents=True, exist_ok=True)
        (app / "data" / "keep.txt").write_text("retained", encoding="utf-8")
        before = tree_snapshot(app)
        self.assertTrue(before)
        return h, app, before

    def _source(self, h, with_exe=True):
        source = h.tmp / "portable"
        (source / "resources").mkdir(parents=True, exist_ok=True)
        (source / "resources" / "main.js").write_text("new", encoding="utf-8")
        if with_exe:
            (source / "App.exe").write_bytes(b"MZ new build")
        return source

    def _leftovers(self, app):
        return sorted(n for n in os.listdir(app.parent)
                      if n.startswith((sa_enroll.IMPORT_STAGING_PREFIX,
                                       sa_enroll.IMPORT_BACKUP_PREFIX)))

    def test_source_without_the_executable_changes_nothing(self):
        h, app, before = self._installed()
        with self.assertRaises(sa_enroll.EnrollmentRefused):
            sa_enroll.import_application(h.broker, "vault", str(self._source(h, False)),
                                         overwrite=True)
        self.assertEqual(tree_snapshot(app), before)
        self.assertEqual(self._leftovers(app), [])

    def test_mid_copy_failure_changes_nothing(self):
        h, app, before = self._installed()
        source = self._source(h)
        real_copytree = shutil.copytree

        def half_then_fail(src, dst, *args, **kwargs):
            os.makedirs(dst)
            shutil.copy2(os.path.join(src, "App.exe"), dst)
            raise OSError(28, "No space left on device")

        with mock.patch.object(shutil, "copytree", half_then_fail):
            with self.assertRaises(OSError):
                sa_enroll.import_application(h.broker, "vault", str(source),
                                             overwrite=True)
        self.assertEqual(tree_snapshot(app), before)
        self.assertEqual(self._leftovers(app), [])
        self.assertIs(shutil.copytree, real_copytree)

    def test_failed_swap_restores_the_old_tree(self):
        h, app, before = self._installed()
        source = self._source(h)
        real_rename = os.rename

        def rename(src, dst):
            if os.path.basename(src).startswith(sa_enroll.IMPORT_STAGING_PREFIX):
                raise OSError(13, "Access is denied")
            return real_rename(src, dst)

        with mock.patch.object(sa_enroll.os, "rename", rename):
            with self.assertRaises(OSError):
                sa_enroll.import_application(h.broker, "vault", str(source),
                                             overwrite=True)
        self.assertEqual(tree_snapshot(app), before)
        self.assertEqual(self._leftovers(app), [])

    def test_failed_final_validation_restores_the_old_tree(self):
        h, app, before = self._installed()
        source = self._source(h)
        real_isfile = os.path.isfile
        executable = os.path.normcase(sa_paths.canonical(h.profile.executable))
        seen = {"n": 0}

        def isfile(path):
            if os.path.normcase(str(path)) == executable:
                seen["n"] += 1
                return False
            return real_isfile(path)

        with mock.patch.object(sa_enroll.os.path, "isfile", isfile):
            with self.assertRaises(sa_enroll.EnrollmentRefused):
                sa_enroll.import_application(h.broker, "vault", str(source),
                                             overwrite=True)
        self.assertGreater(seen["n"], 0)
        self.assertEqual(tree_snapshot(app), before)
        self.assertEqual(self._leftovers(app), [])

    def test_source_inside_the_destination_is_refused(self):
        h, app, before = self._installed()
        inner = app / "portable"
        inner.mkdir()
        (inner / "App.exe").write_bytes(b"MZ nested")
        before = tree_snapshot(app)
        with self.assertRaises(sa_enroll.EnrollmentRefused) as ctx:
            sa_enroll.import_application(h.broker, "vault", str(inner), overwrite=True)
        self.assertEqual(ctx.exception.category, "path_policy")
        self.assertEqual(tree_snapshot(app), before)

    def test_success_exposes_only_the_new_tree_and_drops_the_backup(self):
        h, app, _before = self._installed()
        source = self._source(h)
        result = sa_enroll.import_application(h.broker, "vault", str(source),
                                              overwrite=True)
        self.assertTrue(result["ok"])
        self.assertEqual(tree_snapshot(app), tree_snapshot(source))
        self.assertEqual(self._leftovers(app), [])

    def test_interrupted_swap_restores_the_backup_deterministically(self):
        h, app, before = self._installed()
        backup = app.parent / (sa_enroll.IMPORT_BACKUP_PREFIX + "deadbeef")
        os.rename(app, backup)                       # crash between the renames
        (app.parent / (sa_enroll.IMPORT_STAGING_PREFIX + "deadbeef")).mkdir()
        with self.assertRaises(sa_enroll.EnrollmentRefused):
            sa_enroll.import_application(h.broker, "vault", str(self._source(h, False)),
                                         overwrite=True)
        self.assertEqual(tree_snapshot(app), before)
        self.assertEqual(self._leftovers(app), [])

    def test_backup_beside_a_live_tree_is_never_chosen_silently(self):
        h, app, before = self._installed()
        backup = app.parent / (sa_enroll.IMPORT_BACKUP_PREFIX + "deadbeef")
        shutil.copytree(app, backup)
        with self.assertRaises(sa_enroll.EnrollmentRefused) as ctx:
            sa_enroll.import_application(h.broker, "vault", str(self._source(h)),
                                         overwrite=True)
        self.assertIn("manually", str(ctx.exception))
        self.assertEqual(tree_snapshot(app), before)
        self.assertTrue(backup.is_dir())


# ══════════════════════════════════════════════ migration recovery truth (SRC-027 W2-004)
class MigrationRecoveryTruthTests(BaseCase):
    """A CUTOVER_MOUNTED marker is history; recovery decides on the present."""

    _plaintext = MigrationTests._plaintext
    _prepared = MigrationTests._prepared

    def _crash_after_mounted_marker(self):
        h, source = self._prepared()
        migrator = sa_migrate.Migrator(h.broker, "vault", str(source),
                                       verify_application=False)
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")
        real_mark = migrator.journal.mark

        def mark(step, **fields):
            if step == sa_migrate.STEP_COMPLETE:
                raise RuntimeError("power lost right after the mount")
            return real_mark(step, **fields)

        migrator.journal.mark = mark
        self.assertFalse(migrator.run(provider).ok)
        journal = sa_migrate.MigrationJournal(migrator.journal.path)
        self.assertEqual(journal.last_step, sa_migrate.STEP_CUTOVER_MOUNTED)
        return h, source, migrator, provider

    def _verdict(self, migrator):
        return sa_migrate.inspect(migrator.journal.path)["verdict"]

    def test_detached_without_secret_rolls_back_instead_of_complete(self):
        h, source, migrator, _provider = self._crash_after_mounted_marker()
        h.backend.unmount(h.profile)                  # reboot / forced detach
        names = sorted(p.name for p in Path(migrator.journal.data["plaintext_archive"]).rglob("*.md"))
        recovery = sa_migrate.recover(h.broker, "vault", migrator.journal.path)
        self.assertNotEqual(recovery["verdict"], "COMPLETE", recovery)
        self.assertEqual(recovery["action"], "rolled_back", recovery)
        self.assertNotEqual(self._verdict(migrator), "COMPLETE")
        self.assertEqual(sorted(p.name for p in source.rglob("*.md")), names)
        # Twice is the same safe answer, never a late COMPLETE.
        again = sa_migrate.recover(h.broker, "vault", migrator.journal.path)
        self.assertNotEqual(again["verdict"], "COMPLETE", again)
        self.assertTrue(source.is_dir())

    def test_detached_with_secret_remounts_and_verifies_before_complete(self):
        h, _source, migrator, provider = self._crash_after_mounted_marker()
        h.backend.unmount(h.profile)
        recovery = sa_migrate.recover(h.broker, "vault", migrator.journal.path,
                                      volume_secret_provider=provider)
        self.assertEqual(recovery["action"], "finished_cutover", recovery)
        self.assertEqual(self._verdict(migrator), "COMPLETE")
        self.assertEqual(h.backend.mounted_at(h.profile), h.profile.mount_path)

    def test_a_remount_that_does_not_land_is_not_complete(self):
        h, _source, migrator, provider = self._crash_after_mounted_marker()
        h.backend.unmount(h.profile)
        with mock.patch.object(h.backend, "unlock_and_mount", lambda profile, secret: {}):
            recovery = sa_migrate.recover(h.broker, "vault", migrator.journal.path,
                                          volume_secret_provider=provider)
        self.assertEqual(recovery["action"], "manual", recovery)
        self.assertEqual(self._verdict(migrator), "INTERRUPTED_DURING_CUTOVER")

    def test_mounted_at_the_wrong_path_is_manual_and_stays_resumable(self):
        h, _source, migrator, provider = self._crash_after_mounted_marker()
        h.backend.unmount(h.profile)
        secret = provider()
        try:
            h.backend.unlock_and_mount(migrator.staged_profile, secret)
        finally:
            secret.zeroize()
        recovery = sa_migrate.recover(h.broker, "vault", migrator.journal.path)
        self.assertEqual(recovery["action"], "manual", recovery)
        self.assertEqual(self._verdict(migrator), "INTERRUPTED_DURING_CUTOVER")

    def test_really_mounted_cutover_completes_idempotently(self):
        h, _source, migrator, _provider = self._crash_after_mounted_marker()
        self.assertEqual(h.backend.mounted_at(h.profile), h.profile.mount_path)
        first = sa_migrate.recover(h.broker, "vault", migrator.journal.path)
        self.assertEqual(first["action"], "completed", first)
        second = sa_migrate.recover(h.broker, "vault", migrator.journal.path)
        self.assertEqual(second["verdict"], "COMPLETE", second)


# ══════════════════════════════════════════════ migration lifecycle (SRC-027 CORE-002)
class MigrationLifecycleTests(BaseCase):
    """CLI and GUI share sa_migrate.migrate, and it always ends relocked."""

    def _first_run(self, acceptance=True):
        return MigrateCliEntrypointTests._first_run(self, acceptance=acceptance)

    def _migrate(self, h, final):
        with mock.patch.object(sa_acceptance, "probe_environment",
                               lambda: dict(ACCEPTED_ENVIRONMENT)):
            return sa_migrate.migrate(h.broker, "vault", str(final),
                                      verify_application=False)

    def test_success_ends_detached_with_the_session_gone(self):
        h, final = self._first_run()
        report = self._migrate(h, final)
        self.assertTrue(report.ok, report.reason)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        self.assertIsNone(h.broker.runtime("vault").session)
        status = h.broker.status("vault")
        self.assertNotIn(status["state"], (sa_state.SESSION_CACHED, sa_state.MOUNTED,
                                           sa_state.RUNNING), status)
        self.assertIn("locked again", report.reason)
        self.assertIn("PLAINTEXT", report.reason.upper())

    def test_failed_relock_is_recovery_required_never_protected(self):
        h, final = self._first_run()
        real_run = sa_migrate.Migrator.run

        def run_then_wedge(migrator, provider, resume=True):
            report = real_run(migrator, provider, resume=resume)
            h.backend.busy_on_unmount = True           # a handle stays open
            return report

        with mock.patch.object(sa_migrate.Migrator, "run", run_then_wedge):
            report = self._migrate(h, final)
        self.assertFalse(report.ok, report.reason)
        self.assertEqual(report.error_category, "recovery_required")
        self.assertIn("relock", report.reason)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)
        self.assertNotIn("locked again", report.reason)

    def test_failed_migration_still_relocks(self):
        h, final = self._first_run()

        def explode(migrator, provider, resume=True):
            secret = provider()
            try:
                h.backend.unlock_and_mount(migrator.staged_profile, secret)
            finally:
                secret.zeroize()
            raise sa_migrate.MigrationError("disk full", "internal",
                                            sa_migrate.STEP_COPY)

        with mock.patch.object(sa_migrate.Migrator, "run", explode):
            with self.assertRaises(sa_migrate.MigrationError):
                self._migrate(h, final)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)
        self.assertIsNone(h.broker.runtime("vault").session)

    def test_refused_acceptance_touches_nothing(self):
        h, final = self._first_run(acceptance=False)
        calls = list(h.backend.calls)
        report = self._migrate(h, final)
        self.assertFalse(report.ok)
        self.assertEqual(report.error_category, "hardware_acceptance_required")
        self.assertEqual(h.backend.calls, calls)

    def test_cli_and_gui_reach_the_same_orchestration_symbol(self):
        for name in ("sa_cli.py", "secure_apps_gui.py"):
            text = (SUBSYSTEM / name).read_text(encoding="utf-8")
            self.assertIn("sa_migrate.migrate(", text, name)
            self.assertNotIn("Migrator(", text, name)


# ══════════════════════════════════════════════ helper lease (SRC-027 W2-006)
class _LeaseStubHelper(object):
    """The elevated helper's lease surface, with a scriptable detach."""

    def __init__(self):
        self.running = True
        self.leases = 0
        self.closed = 0
        self.attached = False
        self.fail_unmount = None
        self.lie_on_unmount = False

    def ensure(self):
        pass

    def acquire(self):
        self.leases += 1
        return self

    def release(self):
        self.leases -= 1
        if self.leases < 0:
            raise AssertionError("lease released twice")

    def close(self):
        self.closed += 1

    def call(self, op, **args):
        if op == "unlock_mount":
            self.attached = True
            return {"ok": True}
        if op == "unmount":
            if self.fail_unmount:
                raise sa_privhelper.HelperError(self.fail_unmount, "storage_busy")
            if not self.lie_on_unmount:
                self.attached = False
            return {"ok": True}
        if op == "state":
            return {"state": sa_storage.MOUNTED if self.attached else sa_storage.DETACHED}
        raise AssertionError("unexpected op %r" % op)


class HelperLeaseTests(unittest.TestCase):
    """Lease lifetime follows verified physical attachment, not RPC completion."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saituls-lease-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        container = self.tmp / "vault.vhdx"
        container.write_bytes(b"vhdx")
        self.profile = types.SimpleNamespace(id="vault", container=str(container),
                                             mount_path=str(self.tmp / "mnt"))
        self.helper = _LeaseStubHelper()
        self.backend = sa_storage.BitLockerVhdxBackend(helper=self.helper)

    def _mount(self):
        self.backend.unlock_and_mount(self.profile, b"secret-bytes")
        self.assertEqual((self.helper.leases, self.backend.holding), (1, True))

    def test_failed_unmount_keeps_the_lease(self):
        self._mount()
        self.helper.fail_unmount = "volume busy"
        with self.assertRaises(sa_storage.StorageError):
            self.backend.unmount(self.profile)
        self.assertEqual((self.helper.leases, self.backend.holding), (1, True))

    def test_retry_releases_exactly_once_after_detach(self):
        self._mount()
        self.helper.fail_unmount = "volume busy"
        with self.assertRaises(sa_storage.StorageError):
            self.backend.unmount(self.profile)
        self.helper.fail_unmount = None
        self.backend.unmount(self.profile)
        self.assertEqual((self.helper.leases, self.backend.holding), (0, False))
        self.backend.unmount(self.profile)          # idempotent, no underflow
        self.assertEqual(self.helper.leases, 0)

    def test_unverified_detach_is_a_failure_and_keeps_the_lease(self):
        self._mount()
        self.helper.lie_on_unmount = True
        with self.assertRaises(sa_storage.StorageError):
            self.backend.unmount(self.profile)
        self.assertEqual((self.helper.leases, self.backend.holding), (1, True))

    def test_stop_never_closes_the_helper_while_attached(self):
        self._mount()
        self.helper.fail_unmount = "volume busy"
        with self.assertRaises(sa_storage.StorageError):
            self.backend.unmount(self.profile)
        self.backend.stop()
        self.assertEqual(self.helper.closed, 0)
        self.assertTrue(self.backend.holding)
        self.helper.fail_unmount = None
        self.backend.unmount(self.profile)
        self.backend.stop()
        self.assertEqual(self.helper.closed, 1)


class BrokerShutdownLeaseTests(BaseCase):
    def test_failed_profile_backend_is_not_stopped_then_second_shutdown_is_clean(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        h.backend.fail_unmount = "cannot detach"
        before = h.backend.stopped
        results = h.broker.shutdown()
        self.assertTrue(any(not r.ok for r in results), results)
        self.assertEqual(h.backend.stopped, before)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)
        h.backend.fail_unmount = None
        results = h.broker.shutdown()
        self.assertTrue(all(r.ok for r in results), results)
        self.assertEqual(h.backend.stopped, before + 1)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)


# ══════════════════════════════════════════════ broker ownership (SRC-027 W2-003)
class BrokerOwnershipTests(BaseCase):
    """A live pid is not ownership: only this broker *instance* owns a mount."""

    def _mounted(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)
        return h

    def test_record_is_bound_to_the_broker_instance(self):
        h = self._mounted()
        entry = sa_state.StateStore(h.registry.state_path).get("vault")
        self.assertEqual(entry.broker_pid, h.broker.broker_pid)
        self.assertEqual(entry.broker_nonce, h.broker.broker_nonce)
        self.assertTrue(entry.broker_nonce)

    def test_reused_live_pid_cannot_own_a_mount(self):
        # A new broker in this very process: same, genuinely live pid, new
        # instance -- exactly what PID reuse after a crash looks like.
        h = self._mounted()
        entry = sa_state.StateStore(h.registry.state_path).get("vault")
        self.assertTrue(sa_state.pid_is_alive(entry.broker_pid))
        h.adapter = sa_applife.FakeProcessAdapter()
        h.broker = h._build_broker()
        self.assertEqual(h.broker.broker_pid, entry.broker_pid)
        verdict = h.broker.reconcile("vault")
        self.assertEqual(verdict.verdict, sa_state.RECOVERY_REQUIRED, verdict)
        self.assertEqual(h.broker.status("vault")["state"], sa_state.RECOVERY_REQUIRED)

    def test_live_pid_check_alone_never_proves_ownership(self):
        h = self._mounted()
        verdict = sa_state.reconcile(h.profile, sa_state.StateStore(h.registry.state_path),
                                     "mounted", broker_alive_check=lambda pid: True)
        self.assertEqual(verdict.verdict, sa_state.RECOVERY_REQUIRED, verdict)
        verdict = sa_state.reconcile(h.profile, sa_state.StateStore(h.registry.state_path),
                                     "mounted", broker_alive_check=lambda pid: True,
                                     broker_nonce="not-the-recorded-instance")
        self.assertEqual(verdict.verdict, sa_state.RECOVERY_REQUIRED, verdict)

    def test_legacy_pid_only_record_requires_recovery(self):
        h = self._mounted()
        store = sa_state.StateStore(h.registry.state_path)
        entry = store.get("vault")
        entry.broker_nonce = ""
        entry.broker_birth_time = 0.0
        store.save()
        h.broker.state_store = sa_state.StateStore(h.registry.state_path)
        verdict = h.broker.reconcile("vault")
        self.assertEqual(verdict.verdict, sa_state.RECOVERY_REQUIRED, verdict)

    def test_foreign_birth_time_breaks_ownership(self):
        h = self._mounted()
        store = sa_state.StateStore(h.registry.state_path)
        entry = store.get("vault")
        entry.broker_birth_time = 12345.0
        store.save()
        h.broker.state_store = sa_state.StateStore(h.registry.state_path)
        if not h.broker.broker_birth_time:
            self.skipTest("process birth time unmeasurable here")
        verdict = h.broker.reconcile("vault")
        self.assertEqual(verdict.verdict, sa_state.RECOVERY_REQUIRED, verdict)

    def test_same_instance_mount_is_projected_into_runtime(self):
        h = self._mounted()
        runtime = h.broker.runtime("vault")
        runtime.state = sa_state.LOCKED      # a stale runtime beside a live mount
        verdict = h.broker.reconcile("vault")
        self.assertIn(verdict.verdict, (sa_state.MOUNTED, sa_state.RUNNING), verdict)
        status = h.broker.status("vault")
        self.assertEqual(status["state"], verdict.verdict, status)
        self.assertNotEqual(status["state"], sa_state.LOCKED)

    def test_successor_broker_owns_its_own_remount(self):
        # The successor rewrites the owner identity as a unit; it must not
        # inherit the dead instance's nonce and then disown its own mount.
        h = self._mounted()
        h.restart_broker()
        self.assertEqual(h.broker.reconcile("vault").verdict, sa_state.RECOVERY_REQUIRED)
        self.assertTrue(h.broker.recover("vault").ok)
        self.assertTrue(h.broker.open("vault").ok)
        verdict = h.broker.reconcile("vault")
        self.assertIn(verdict.verdict, (sa_state.MOUNTED, sa_state.RUNNING), verdict)
        entry = sa_state.StateStore(h.registry.state_path).get("vault")
        self.assertEqual(entry.broker_nonce, h.broker.broker_nonce)

    def test_detached_storage_stays_normally_recoverable(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        h.restart_broker()
        verdict = h.broker.reconcile("vault")
        self.assertEqual(verdict.verdict, sa_state.LOCKED, verdict)


# ══════════════════════════════════════════════ Stop-the-line A & PERF-004 tests
class AppSupervisorRegressionTests(BaseCase):
    def test_single_top_level_app_supervisor_class(self):
        """Structural test: sa_applife.py must declare class AppSupervisor exactly once."""
        path = SUBSYSTEM / "sa_applife.py"
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.startswith("class AppSupervisor")]
        self.assertEqual(len(lines), 1, "found multiple definitions of class AppSupervisor: %r" % lines)

    def test_app_supervisor_instantiation_and_contract(self):
        adapter = sa_applife.FakeProcessAdapter()
        clock = FakeClock()
        sup = sa_applife.AppSupervisor(adapter=adapter, clock=clock)
        self.assertEqual(sup.pid, 0)
        expected_methods = [
            "launch", "attach", "forget", "running", "tree", "observation",
            "running_from", "foreground_is_protected_from", "foreground_is_protected",
            "request_graceful_close", "wait_for_exit", "terminate", "close_sequence"
        ]
        for m in expected_methods:
            self.assertTrue(hasattr(sup, m), "AppSupervisor missing %s" % m)

    def test_broker_constructs_runtime_with_supervisor(self):
        h = self.harness()
        rt = h.broker.runtime("vault")
        self.assertIsInstance(rt.supervisor, sa_applife.AppSupervisor)
        self.assertEqual(rt.supervisor.adapter, h.adapter)

    def test_surviving_child_process_keeps_running_and_refuses_detach(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        rt = h.broker.runtime("vault")
        parent_pid = rt.supervisor.pid
        self.assertGreater(parent_pid, 0)
        child_pid = parent_pid + 10
        h.adapter.trees[parent_pid] = [parent_pid, child_pid]
        h.adapter.alive_pids.add(child_pid)
        # Parent exits, child survives
        h.adapter.exit_parent_only(parent_pid)
        obs = rt.supervisor.observation()
        self.assertTrue(obs.running, "observation says not running despite surviving child")
        self.assertIn(child_pid, obs.tree_pids)
        self.assertTrue(rt.supervisor.running)
        # Detach/exit must refuse while child is alive
        h.broker.tick()
        self.assertEqual(rt.state, sa_state.RUNNING)
        self.assertEqual(h.backend.state(h.profile), sa_storage.MOUNTED)


class Perf004ProcessObservationTests(BaseCase):
    def test_single_tree_and_foreground_lookup_per_periodic_turn(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        rt = h.broker.runtime("vault")
        pid = rt.supervisor.pid

        # Spy on adapter methods
        orig_tree_pids = h.adapter.tree_pids
        orig_foreground_pid = h.adapter.foreground_pid
        tree_calls = []
        fg_calls = []

        def tracked_tree_pids(p):
            tree_calls.append(p)
            return orig_tree_pids(p)

        def tracked_foreground_pid():
            fg_calls.append(True)
            return orig_foreground_pid()

        h.adapter.tree_pids = tracked_tree_pids
        h.adapter.foreground_pid = tracked_foreground_pid

        # Simulate periodic cycle with snapshot
        obs = rt.supervisor.observation()
        snaps = {"vault": obs}
        # In one tick turn using snaps:
        h.broker.poll_foreground(snapshots=snaps)
        h.broker.tick(snapshots=snaps)
        status = h.broker.status("vault", snapshot=snaps.get("vault"))

        self.assertEqual(len(tree_calls), 1, "tree_pids called more than once: %r" % tree_calls)
        self.assertEqual(len(fg_calls), 1, "foreground_pid called more than once: %r" % fg_calls)
        self.assertTrue(status["app_running"])


class IdleExpiryObservationTests(BaseCase):
    def test_idle_expiry_with_app_running_executes_secure_lock(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        rt = h.broker.runtime("vault")
        obs = rt.supervisor.observation()
        # advance beyond idle limit
        h.clock.advance(h.profile.policy.idle_timeout_minutes * 60 + 10)
        res = h.broker._handle_idle_expiry(rt, observation=obs)
        self.assertEqual(res.state, sa_state.LOCKED)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)

    def test_idle_expiry_without_app_detaches_storage(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()
        rt = h.broker.runtime("vault")
        obs = rt.supervisor.observation()
        self.assertFalse(obs.running)
        h.clock.advance(h.profile.policy.idle_timeout_minutes * 60 + 10)
        res = h.broker._handle_idle_expiry(rt, observation=obs)
        self.assertEqual(res.state, sa_state.LOCKED)
        self.assertEqual(h.backend.state(h.profile), sa_storage.DETACHED)

    def test_idle_expiry_unmount_failure_reports_error_not_locked(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        rt = h.broker.runtime("vault")
        obs = rt.supervisor.observation()
        h.backend.fail_unmount = "forced unmount failure"
        h.clock.advance(h.profile.policy.idle_timeout_minutes * 60 + 10)
        res = h.broker._handle_idle_expiry(rt, observation=obs)
        self.assertFalse(res.ok)
        self.assertEqual(res.state, sa_state.ERROR)
        self.assertEqual(rt.state, sa_state.ERROR)

    def test_direct_idle_expiry_without_observation_arg(self):
        h = self.harness()
        self.assertTrue(h.enroll().ok)
        self.assertTrue(h.broker.open("vault").ok)
        rt = h.broker.runtime("vault")
        # Direct call with no observation param must not raise NameError
        h.clock.advance(h.profile.policy.idle_timeout_minutes * 60 + 10)
        res = h.broker._handle_idle_expiry(rt)
        self.assertEqual(res.state, sa_state.LOCKED)


class Perf005MigrationTraversalTests(BaseCase):
    def test_migration_six_total_traversals(self):
        h = self.harness()
        pending, _rec = sa_enroll.begin_enrollment(h.broker, "vault")
        self.assertTrue(pending.accept().ok)
        # populate source vault
        source_dir = h.tmp / "source_vault"
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "note1.md").write_text("hello world", encoding="utf-8")
        (source_dir / "sub").mkdir(parents=True, exist_ok=True)
        (source_dir / "sub" / "note2.md").write_text("nested content", encoding="utf-8")

        migrator = sa_migrate.Migrator(h.broker, "vault", str(source_dir), verify_application=False)
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")

        walk_calls = []
        orig_walk = os.walk
        def counted_walk(top, *args, **kwargs):
            walk_calls.append(str(top))
            return orig_walk(top, *args, **kwargs)

        with mock.patch("os.walk", side_effect=counted_walk):
            res = migrator.run(provider)
        self.assertTrue(res.ok, res.reason)
        # 1 precheck + 1 copy + 2 verify + 2 reverify = 6
        self.assertEqual(len(walk_calls), 6, "expected 6 traversals, got %d: %r" % (len(walk_calls), walk_calls))

    def test_migration_detects_tampering_at_reverify(self):
        h = self.harness()
        pending, _rec = sa_enroll.begin_enrollment(h.broker, "vault")
        self.assertTrue(pending.accept().ok)
        source_dir = h.tmp / "source_vault_tamper"
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "note.md").write_text("initial", encoding="utf-8")

        migrator = sa_migrate.Migrator(h.broker, "vault", str(source_dir), verify_application=False)
        provider = sa_migrate.secret_provider_from_session(h.broker, "vault")

        orig_reverify = sa_migrate.Migrator.reverify
        def tampered_reverify(migrator_self, report):
            # Change file before reverify
            (source_dir / "note.md").write_text("changed_size_different", encoding="utf-8")
            return orig_reverify(migrator_self, report)

        with mock.patch.object(sa_migrate.Migrator, "reverify", tampered_reverify):
            res = migrator.run(provider)
        self.assertFalse(res.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
