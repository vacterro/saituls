"""REAL Windows storage acceptance for SAITULS Secure Apps. Disposable.

Not part of the GitHub workflow: it needs an elevated session, the BitLocker
cmdlets and the ability to attach a VHDX, none of which a hosted runner has.
It needs no security key -- authentication is the deterministic fake provider,
because what is under test here is *storage and path sequencing*, not FIDO2.

    python tests\\test_secure_apps_windows_storage.py

Everything it creates lives in a throwaway directory under TEMP and is
removed again, including the disk image. It never touches a configured
profile, the shipped registry or any real vault.

What it proves, on real Windows, with a genuinely NON-EMPTY final mount
directory -- the exact first-run situation that used to be unreachable:

  1. enrollment succeeds without touching that directory;
  2. migration copies out of it;
  3. the cutover rename is the FIRST moment the directory is touched at all;
  4. the final mount succeeds only after the directory has become empty;
  5. the plaintext backup still exists when everything is done.

Plus the negative control that makes 1..4 meaningful: mounting onto the
non-empty directory is refused BEFORE any mutation starts, rather than
failing half way through Add-PartitionAccessPath.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_audit          # noqa: E402
import sa_auth           # noqa: E402
import sa_broker         # noqa: E402
import sa_config         # noqa: E402
import sa_crypto         # noqa: E402
import sa_enroll         # noqa: E402
import sa_migrate        # noqa: E402
import sa_paths          # noqa: E402
import sa_privtask       # noqa: E402
import sa_storage        # noqa: E402

HELPER = SUBSYSTEM / "sa_storage_helper.ps1"
CONTAINER_GB = 1


def is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def helper_prerequisites():
    """Ask the shipped helper whether this host can do any of this at all."""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(HELPER), "-SelfTest"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
    except Exception as exc:
        return {"ok": False, "missing": [type(exc).__name__]}
    text = (completed.stdout or "").strip()
    start = text.find("{")
    if start < 0:
        return {"ok": False, "missing": ["selftest produced no JSON"]}
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return {"ok": False, "missing": ["selftest produced malformed JSON"]}


def powershell(script):
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600)


def registry_document(root, mount, container):
    return {
        "schema": "saituls.secure-apps/1",
        "schema_version": 1,
        "managed_root": str(root),
        "profiles": [{
            "id": "disposable",
            "label": "Disposable vault",
            "enabled": True,
            "application": {
                "executable": str(root / "apps" / "disposable" / "app" / "App.exe"),
                "working_directory": str(root / "apps" / "disposable" / "app"),
                "arguments": [],
                "vault_argument_style": "path",
            },
            "storage": {
                "backend": "bitlocker-vhdx",
                "container": str(container),
                "container_id": "disposable-container-1",
                "mount_path": str(mount),
                "size_gb": CONTAINER_GB,
                "filesystem_label": "SAITULS-TEST",
            },
            "authentication": {
                "provider": "fake-auth",
                "credential_profile": "primary",
                "user_verification": "discouraged",
            },
            "policy": {
                "mode": "default",
                "idle_timeout_minutes": 60,
                "unmount_when_app_closes": True,
                "conditions": [],
            },
        }],
    }


@unittest.skipUnless(os.name == "nt", "Windows-only")
class RealWindowsStorageTests(unittest.TestCase):
    """One long acceptance scenario, asserted step by step."""

    @classmethod
    def setUpClass(cls):
        if not is_elevated():
            raise unittest.SkipTest(
                "not elevated: attaching a VHDX and enabling BitLocker need "
                "an administrator session")
        prereq = helper_prerequisites()
        if not prereq.get("ok"):
            raise unittest.SkipTest(
                "storage helper prerequisites missing: %s"
                % ", ".join(prereq.get("missing") or ["unknown"]))
        # The elevated helper is started by the registered scheduled task and
        # by nothing else, so a host without that installation cannot run this
        # scenario at all. Skipping says which setup step is missing; pretending
        # would just fail later, in the middle of a real BitLocker volume.
        verdict = sa_privtask.verify_installation()
        if not verdict.ok:
            raise unittest.SkipTest(
                "the privileged helper task is not installed or does not match "
                "this installation (%s): run 'sa_cli.py privileged-helper "
                "install' first. %s"
                % (verdict.token or "invalid", "; ".join(verdict.reasons)))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saituls-secapps-real-"))
        self.root = self.tmp / "managed"
        self.container = self.root / "vaults" / "disposable" / "disposable.vhdx"
        self.final = self.tmp / "plaintext-vault"
        (self.root / "apps" / "disposable" / "app").mkdir(parents=True)
        (self.root / "apps" / "disposable" / "app" / "App.exe").write_bytes(b"MZ")
        (self.root / "vaults" / "disposable").mkdir(parents=True)
        (self.root / "state").mkdir(parents=True)

        # The point of the whole exercise: the final mount path is NOT empty.
        self.final.mkdir(parents=True)
        (self.final / ".obsidian").mkdir()
        (self.final / ".obsidian" / "app.json").write_text('{"x":1}',
                                                           encoding="utf-8")
        for index in range(12):
            (self.final / ("note-%02d - ÕÄÖÜ.md" % index)).write_text(
                "# note %d\n%s\n" % (index, "y" * (index * 11)), encoding="utf-8")
        self.before = sa_migrate.walk_tree(str(self.final))
        self.assertTrue(self.before, "the fixture built an empty vault")

        document = registry_document(self.root, self.final, self.container)
        self.registry = sa_config.parse_registry(
            document, managed_root=str(self.root), allow_test_providers=True)
        self.profile = self.registry.get("disposable")
        self.audit = sa_audit.AuditLog(self.registry.audit_path)
        self.broker = sa_broker.SecureBroker(
            self.registry, audit=self.audit,
            providers={"fake-auth": sa_auth.FakeAuthProvider()})
        self.addCleanup(self._teardown)

    # -- cleanup ----------------------------------------------------------
    def _teardown(self):
        try:
            backend = self.broker.backend(self.profile)
        except Exception:
            backend = None
        for mount in (self.profile.mount_path,
                      sa_paths.enrollment_mount_path(self.profile.container),
                      sa_paths.staging_mount_path(self.profile.container)):
            if backend is None:
                break
            try:
                backend.unmount(self.profile.with_mount_path(mount))
            except Exception:
                pass
        try:
            self.broker.shutdown()
        except Exception:
            pass
        powershell(
            "$p='%s'; if (Test-Path -LiteralPath $p) { "
            "try { Dismount-DiskImage -ImagePath $p -ErrorAction Stop | Out-Null } "
            "catch { } }" % str(self.container).replace("'", "''"))
        time.sleep(1.0)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    def tree(self, root):
        """The vault's own files, by the subsystem's own definition.

        Deliberately sa_migrate.walk_tree rather than a private rglob: a real
        NTFS volume always carries System Volume Information and friends, and
        a test that counted those would be measuring the filesystem instead
        of the migration.
        """
        return sa_migrate.walk_tree(str(root))

    # -- the scenario -----------------------------------------------------
    def test_first_run_on_a_non_empty_final_directory(self):
        backend = self.broker.backend(self.profile)

        # --- 0. negative control: mounting onto the non-empty path is
        #        refused BEFORE anything is created. -----------------------
        secret = sa_crypto.new_volume_secret()
        try:
            with self.assertRaises(sa_storage.StorageError) as caught:
                backend.create(self.profile, secret)
        finally:
            secret.zeroize()
        self.assertIn("not empty", str(caught.exception))
        self.assertFalse(self.container.exists(),
                         "a refused create still produced a disk image")
        self.assertEqual(self.tree(self.final), self.before)

        # --- 1. enrollment succeeds without touching that directory ------
        pending, recovery = sa_enroll.begin_enrollment(self.broker, "disposable")
        self.addCleanup(lambda: pending.resolved or pending.cancel("teardown"))
        self.assertTrue(recovery, "no BitLocker recovery material was produced")
        self.assertRegex(recovery, r"^\d{6}(-\d{6}){7}$",
                         "that does not look like a BitLocker recovery password")
        staging = Path(pending.staged_profile.mount_path)
        self.assertEqual(staging.name, "_enrollment_mount")
        self.assertEqual(self.tree(self.final), self.before,
                         "enrollment touched the plaintext vault")

        result = pending.accept()
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.state, sa_enroll.ENROLLMENT_READY)
        self.assertTrue(self.container.is_file(), "no VHDX was created")
        self.assertEqual(backend.state(self.profile), sa_storage.DETACHED,
                         "enrollment left the volume attached")
        self.assertFalse(staging.exists(), "the staging mount was left behind")
        self.assertEqual(self.tree(self.final), self.before)

        # --- 2..4. migration: copy, verify, relock, cut over --------------
        migrator = sa_migrate.Migrator(self.broker, "disposable",
                                       str(self.final),
                                       verify_application=False)
        provider = sa_migrate.secret_provider_from_session(self.broker,
                                                           "disposable")
        report = migrator.run(provider)
        self.assertTrue(report.ok, report.reason)
        self.assertEqual(report.status, [sa_migrate.MIGRATION_VERIFIED,
                                         sa_migrate.PLAINTEXT_SOURCE_REMAINS])
        self.assertEqual(report.file_count, len(self.before))
        self.assertGreater(report.hashed, 0)

        # 3. the rename is the first and only thing that moved the source.
        archive = Path(report.plaintext_archive)
        self.assertNotEqual(archive, self.final)
        self.assertTrue(archive.is_dir(), "the plaintext backup is gone")
        self.assertEqual(self.tree(archive), self.before,
                         "migration modified the plaintext copy")

        # 4. the final path now carries the encrypted volume, byte for byte.
        self.assertEqual(backend.state(self.profile), sa_storage.MOUNTED)
        self.assertEqual(self.tree(self.final), self.before,
                         "the mounted vault does not match the original")

        # --- 5. lock it again and prove the data goes away with it -------
        results = self.broker.lock_now("disposable")
        self.assertTrue(all(r.ok for r in results),
                        [r.reason for r in results])
        self.assertEqual(backend.state(self.profile), sa_storage.DETACHED)
        self.assertTrue(sa_paths.directory_is_empty(str(self.final)),
                        "the mount point still shows data after detaching")
        self.assertTrue(archive.is_dir(),
                        "PLAINTEXT_SOURCE_REMAINS but the plaintext is gone")

        # --- 6. and it opens again, from the real container ---------------
        again = self.broker.authenticate_only("disposable")
        self.assertTrue(again.ok, again.reason)
        reopen = sa_migrate.secret_provider_from_session(self.broker,
                                                         "disposable")()
        try:
            backend.unlock_and_mount(self.profile, reopen)
        finally:
            reopen.zeroize()
        self.assertEqual(self.tree(self.final), self.before,
                         "the re-unlocked vault does not match the original")


if __name__ == "__main__":
    unittest.main(verbosity=2)
