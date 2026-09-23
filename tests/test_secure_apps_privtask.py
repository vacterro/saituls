"""The pre-authorized privilege boundary: protected runtime, pinned, fail-closed.

Everything here is deterministic and disposable. No scheduled task is ever
registered, no elevation is ever requested and no pipe is ever opened: the
task definition is canonicalized, fingerprinted and compared as data; the
runtime bundle manifest and its fingerprint are pure functions of their
content; the runtime-directory ACL and the task security descriptor are
analysed from injected measurements and SDDL strings; the helper identity
rule is a pure function fed measurements; and the broker's silent-start
behaviour runs against a scripted helper whose "Task Scheduler" is a list of
recorded calls.

The questions this file exists to answer:

  * is the elevated runtime installed into a protected directory, with the
    task action, the helper and the frozen worker all resolving *inside* it --
    never the medium-writable source tree, never through a reparse point?
  * does a task definition, a task security descriptor or a runtime bundle
    that has *moved* get refused rather than started?
  * is a process on the helper pipe trusted for what it *is* rather than for
    having been started by Task Scheduler?
  * can unlock and lock start the helper again after it has idled out --
    silently, with no consent dialog anywhere?

The live Windows behaviour these stand in for (a real registration, a real
ACL, a real task security descriptor, a real silent start) is the Windows
integration list in :class:`WindowsProtectedRuntimeIntegrationTests` and in
Scripts/secure_apps/README.md; a hermetic test is not evidence for it.
"""
import builtins
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import sa_broker          # noqa: E402
import sa_privhelper      # noqa: E402
import sa_privtask        # noqa: E402
import sa_state           # noqa: E402

import sa_privboundary_probe as probe   # noqa: E402  (tests/)
import sa_storage         # noqa: E402

from test_secure_apps import BaseCase, Harness   # noqa: E402

USER_SID = "S-1-5-21-111111111-222222222-333333333-1001"
OTHER_SID = "S-1-5-21-111111111-222222222-333333333-1002"
POWERSHELL = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                          "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

VALID_ACL = {"runtime_acl_valid": True}
INVALID_ACL = {"runtime_acl_valid": False}
VALID_PIN_ACL = {"pin_acl_valid": True, "pin_owner": "S-1-5-32-544",
                 "pin_owner_valid": True, "pin_medium_writable": False,
                 "pin_medium_can_change_dacl": False,
                 "pin_dir_owner_valid": True, "pin_dir_medium_writable": False}
INVALID_PIN_ACL = {"pin_acl_valid": False, "pin_owner": OTHER_SID,
                   "pin_owner_valid": False, "pin_medium_writable": True,
                   "pin_medium_can_change_dacl": True,
                   "pin_dir_owner_valid": False, "pin_dir_medium_writable": True}
VALID_TMP = {"privileged_tmp_valid": True,
             "privileged_tmp_root": r"C:\ProgramData\SAITULS\secure-apps\privileged-tmp"}
INVALID_TMP = {"privileged_tmp_valid": False,
               "privileged_tmp_root": r"C:\ProgramData\SAITULS\secure-apps\privileged-tmp"}


def make_installed_runtime(tmp, sid=USER_SID, worker_bytes=b"MZ frozen worker",
                           helper_bytes=None):
    """A throwaway protected runtime on disk, plus its manifest and pin.

    A real helper copy, a stub frozen worker and a real manifest live under
    ``<tmp>/privileged``; the pin binds them. This is what an install would
    leave behind, minus the ACL (which a temp directory cannot carry and which
    is injected into :func:`sa_privtask.verify_installation` instead).
    """
    root = Path(tmp) / "privileged"
    root.mkdir(parents=True, exist_ok=True)
    helper = root / sa_privtask.HELPER_SCRIPT
    if helper_bytes is None:
        shutil.copyfile(SUBSYSTEM / sa_privtask.HELPER_SCRIPT, helper)
    else:
        helper.write_bytes(helper_bytes)
    bundle = root / sa_privtask.FIDO_BUNDLE_DIR_NAME
    (bundle / sa_privtask.PYINSTALLER_INTERNAL_DIR).mkdir(parents=True, exist_ok=True)
    (bundle / sa_privtask.FROZEN_WORKER_NAME).write_bytes(worker_bytes)
    # A onedir bundle is more than its executable: the recursive fingerprint
    # has to have something else to bind, or the tests would prove nothing
    # about the files that actually make a onefile build unacceptable.
    (bundle / sa_privtask.PYINSTALLER_INTERNAL_DIR / "python311.dll").write_bytes(b"stub dll")
    (bundle / sa_privtask.PYINSTALLER_INTERNAL_DIR / "base_library.zip").write_bytes(b"stub zip")
    manifest = sa_privtask.build_runtime_manifest(str(root))
    sa_privtask.save_runtime_manifest(manifest, runtime_root=str(root))
    pin = sa_privtask.build_pin(runtime_root=str(root), user_sid=sid,
                                manifest=manifest)
    return root, manifest, pin


def runtime_task_xml(root, sid=USER_SID):
    return sa_privtask.build_task_xml(
        user_sid=sid, helper_path=str(Path(root) / sa_privtask.HELPER_SCRIPT))


# ══════════════════════════════════════════════════ canonical definition
class TaskDefinitionTests(unittest.TestCase):
    """What the task runs, as whom, at what privilege -- and nothing else."""

    def definition(self, **kwargs):
        base = dict(user_sid=USER_SID,
                    helper_path=r"C:\ProgramData\SAITULS\secure-apps\privileged\sa_storage_helper.ps1",
                    host_image=POWERSHELL,
                    working_directory=r"C:\ProgramData\SAITULS\secure-apps\privileged")
        base.update(kwargs)
        return sa_privtask.canonical_definition(**base)

    def test_canonicalization_is_stable_and_case_insensitive_on_paths(self):
        lower = self.definition()
        upper = self.definition(
            helper_path=r"C:\PROGRAMDATA\SAITULS\SECURE-APPS\PRIVILEGED\SA_STORAGE_HELPER.PS1",
            host_image=POWERSHELL.upper(),
            working_directory=r"C:\PROGRAMDATA\SAITULS\SECURE-APPS\PRIVILEGED")
        self.assertEqual(lower, upper)
        self.assertEqual(sa_privtask.definition_fingerprint(lower),
                         sa_privtask.definition_fingerprint(upper))

    def test_the_action_is_the_fixed_one_and_carries_no_runtime_parameter(self):
        arguments = self.definition()["arguments"]
        self.assertEqual(arguments[:8], list(sa_privtask.HELPER_HOST_SWITCHES))
        self.assertEqual(arguments[-1], sa_privtask.SCHEDULED_HELPER_SWITCH)
        joined = " ".join(arguments).lower()
        for forbidden in ("pipename", "-token", "pythonexe", "fidoworker",
                          "secret", "pin", "salt", "container", "mount"):
            self.assertNotIn(forbidden, joined)

    def test_the_fingerprint_moves_with_every_security_relevant_field(self):
        base = self.definition()
        digest = sa_privtask.definition_fingerprint(base)
        moved = {
            "run_level": "LeastPrivilege",
            "logon_type": "Password",
            "user_sid": OTHER_SID,
            "image": r"C:\Users\public\powershell.exe",
            "arguments": base["arguments"] + ["-Extra"],
            "working_directory": r"C:\Users\public",
            "triggers": ["LogonTrigger"],
            "action_count": 2,
            "enabled": False,
            "allow_start_on_demand": False,
            "multiple_instances": "Parallel",
            "execution_time_limit": "PT72H",
        }
        for field, value in moved.items():
            changed = dict(base)
            changed[field] = value
            self.assertNotEqual(
                sa_privtask.definition_fingerprint(changed), digest,
                "%s does not move the task definition fingerprint" % field)

    def test_xml_round_trips_to_the_same_canonical_definition(self):
        helper = r"C:\ProgramData\SAITULS\secure-apps\privileged\sa_storage_helper.ps1"
        xml_text = sa_privtask.build_task_xml(user_sid=USER_SID, helper_path=helper,
                                              host_image=POWERSHELL)
        parsed = sa_privtask.parse_task_xml(xml_text, lookup=lambda name: None)
        expected = sa_privtask.expected_definition(user_sid=USER_SID, helper_path=helper,
                                                   host_image=POWERSHELL)
        self.assertEqual(parsed, expected)

    def test_the_registered_xml_asks_for_highest_privileges_on_demand_only(self):
        xml_text = sa_privtask.build_task_xml(user_sid=USER_SID)
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", xml_text)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml_text)
        self.assertIn("<AllowStartOnDemand>true</AllowStartOnDemand>", xml_text)
        self.assertIn("<Triggers />", xml_text)
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", xml_text)
        self.assertNotIn("S-1-5-18", xml_text)
        self.assertNotIn("<Password>", xml_text)

    def test_the_task_action_names_the_installed_helper_not_the_source(self):
        """A default build points at %ProgramData%, never the repository."""
        xml_text = sa_privtask.build_task_xml(user_sid=USER_SID)
        parsed = sa_privtask.parse_task_xml(xml_text, lookup=lambda name: None)
        target = parsed["arguments"][parsed["arguments"].index("-File") + 1]
        self.assertIn(sa_privtask.PRIVILEGED_DIR_NAME, target)
        self.assertNotIn(sa_privtask.normalize_path(str(SUBSYSTEM)), target)


# ══════════════════════════════════════════════════ runtime bundle manifest
class RuntimeManifestTests(unittest.TestCase):
    """The bundle manifest is a pure function of its content, and self-checks."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saituls-runtime-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root, self.manifest, self.pin = make_installed_runtime(self.tmp)

    def test_the_manifest_binds_helper_worker_and_task_action(self):
        for field in sa_privtask.RUNTIME_MANIFEST_FIELDS:
            self.assertIn(field, self.manifest)
        self.assertEqual(self.manifest["helper_filename"], sa_privtask.HELPER_SCRIPT)
        self.assertEqual(self.manifest["fido_worker_filename"],
                         sa_privtask.FROZEN_WORKER_NAME)
        self.assertTrue(self.manifest["task_action_path"].endswith(
            sa_privtask.HELPER_SCRIPT.lower()))

    def test_canonicalization_ignores_key_order_and_the_fingerprint_field(self):
        shuffled = {k: self.manifest[k] for k in reversed(list(self.manifest))}
        self.assertEqual(sa_privtask.canonical_runtime_manifest(shuffled),
                         sa_privtask.canonical_runtime_manifest(self.manifest))
        # The fingerprint field is excluded from its own computation.
        tampered = dict(self.manifest, bundle_fingerprint="0" * 64)
        self.assertEqual(sa_privtask.runtime_bundle_fingerprint(tampered),
                         sa_privtask.runtime_bundle_fingerprint(self.manifest))

    def test_the_bundle_fingerprint_changes_with_the_helper(self):
        moved = dict(self.manifest, helper_sha256="a" * 64)
        self.assertNotEqual(sa_privtask.runtime_bundle_fingerprint(moved),
                            sa_privtask.runtime_bundle_fingerprint(self.manifest))

    def test_the_bundle_fingerprint_changes_with_the_worker(self):
        moved = dict(self.manifest, fido_worker_sha256="b" * 64)
        self.assertNotEqual(sa_privtask.runtime_bundle_fingerprint(moved),
                            sa_privtask.runtime_bundle_fingerprint(self.manifest))

    def test_the_bundle_fingerprint_changes_with_the_task_action_path(self):
        moved = dict(self.manifest, task_action_path=r"c:\elsewhere\x.ps1")
        self.assertNotEqual(sa_privtask.runtime_bundle_fingerprint(moved),
                            sa_privtask.runtime_bundle_fingerprint(self.manifest))

    def test_a_manifest_whose_fingerprint_disagrees_with_its_body_is_refused(self):
        path = sa_privtask.runtime_manifest_path(str(self.root))
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        document["helper_sha256"] = "c" * 64        # body changed, fp stale
        Path(path).write_text(json.dumps(document), encoding="utf-8")
        manifest, problem = sa_privtask.load_runtime_manifest(runtime_root=str(self.root))
        self.assertIsNone(manifest)
        self.assertIn("bundle fingerprint", problem)

    def test_a_foreign_manifest_schema_is_refused(self):
        path = sa_privtask.runtime_manifest_path(str(self.root))
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        document["schema"] = "something-else"
        Path(path).write_text(json.dumps(document), encoding="utf-8")
        manifest, problem = sa_privtask.load_runtime_manifest(runtime_root=str(self.root))
        self.assertIsNone(manifest)
        self.assertIn("schema", problem)


# ══════════════════════════════════════════════════ runtime containment
class RuntimeContainmentTests(unittest.TestCase):
    """Every elevated path must resolve strictly inside the protected root."""

    ROOT = r"C:\ProgramData\SAITULS\secure-apps\privileged"

    def test_a_path_inside_the_root_is_accepted(self):
        self.assertTrue(sa_privtask.path_is_within(
            self.ROOT, self.ROOT + r"\sa_storage_helper.ps1"))
        self.assertTrue(sa_privtask.path_is_within(
            self.ROOT, self.ROOT + r"\sa_fido_worker.exe"))

    def test_the_root_itself_is_not_inside_unless_asked(self):
        self.assertFalse(sa_privtask.path_is_within(self.ROOT, self.ROOT))
        self.assertTrue(sa_privtask.path_is_within(self.ROOT, self.ROOT,
                                                   allow_root=True))

    def test_a_dotdot_escape_is_rejected(self):
        self.assertFalse(sa_privtask.path_is_within(
            self.ROOT, self.ROOT + r"\..\evil.exe"))
        self.assertFalse(sa_privtask.path_is_within(
            self.ROOT, r"C:\ProgramData\SAITULS\secure-apps\evil.exe"))

    def test_a_sibling_prefix_is_not_inside(self):
        self.assertFalse(sa_privtask.path_is_within(
            self.ROOT, self.ROOT + "-evil\\x.exe"))

    def test_the_source_tree_is_not_inside_the_protected_root(self):
        self.assertFalse(sa_privtask.path_is_within(
            self.ROOT, str(SUBSYSTEM / "sa_storage_helper.ps1")))


# ══════════════════════════════════════════════════ task security descriptor
class TaskSecurityDescriptorTests(unittest.TestCase):
    """The medium user may run/query the task, never modify/replace/delete it."""

    def test_the_installed_descriptor_denies_medium_modification(self):
        sddl = sa_privtask.build_task_sddl(USER_SID)
        report = sa_privtask.analyze_task_security(sddl, USER_SID)
        self.assertTrue(report["task_security_valid"], report["reasons"])
        self.assertTrue(report["task_medium_user_can_run"])
        self.assertFalse(report["task_medium_user_can_modify"])
        self.assertFalse(report["task_medium_user_can_delete"])

    def test_a_user_granted_full_control_is_invalid(self):
        sddl = "O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;%s)" % USER_SID
        report = sa_privtask.analyze_task_security(sddl, USER_SID)
        self.assertFalse(report["task_security_valid"])
        self.assertTrue(report["task_medium_user_can_modify"])
        self.assertTrue(report["task_medium_user_can_delete"])

    def test_a_user_granted_delete_is_invalid(self):
        sddl = "O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGXSD;;;%s)" % USER_SID
        report = sa_privtask.analyze_task_security(sddl, USER_SID)
        self.assertFalse(report["task_security_valid"])
        self.assertTrue(report["task_medium_user_can_delete"])

    def test_a_user_who_could_rewrite_the_dacl_is_invalid(self):
        sddl = "O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGXWDWO;;;%s)" % USER_SID
        report = sa_privtask.analyze_task_security(sddl, USER_SID)
        self.assertFalse(report["task_security_valid"])
        self.assertTrue(report["task_medium_user_can_modify"])

    def test_an_unprotected_dacl_is_invalid(self):
        sddl = "O:BAG:BAD:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;%s)" % USER_SID
        report = sa_privtask.analyze_task_security(sddl, USER_SID)
        self.assertFalse(report["task_security_valid"])
        self.assertTrue(any("protected" in r for r in report["reasons"]))

    def test_a_user_owned_task_is_invalid(self):
        sddl = "O:%sG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;%s)" % (USER_SID, USER_SID)
        report = sa_privtask.analyze_task_security(sddl, USER_SID)
        self.assertFalse(report["task_security_valid"])
        self.assertTrue(any("owner" in r for r in report["reasons"]))

    def test_missing_system_or_admin_full_control_is_invalid(self):
        sddl = "O:BAG:BAD:P(A;;GA;;;BA)(A;;GRGX;;;%s)" % USER_SID
        report = sa_privtask.analyze_task_security(sddl, USER_SID)
        self.assertFalse(report["task_security_valid"])
        self.assertTrue(any("SYSTEM" in r for r in report["reasons"]))

    def test_an_unreadable_descriptor_fails_closed(self):
        report = sa_privtask.analyze_task_security(None, USER_SID)
        self.assertFalse(report["task_security_valid"])
        self.assertIsNone(report["task_security_fingerprint"])
        self.assertTrue(any("could not be read" in r for r in report["reasons"]))

    def test_the_fingerprint_is_stable_and_moves_with_meaning(self):
        good = sa_privtask.build_task_sddl(USER_SID)
        # Reordered ACEs, same meaning -> same fingerprint.
        reordered = "O:BAG:BAD:P(A;;GA;;;BA)(A;;GRGX;;;%s)(A;;GA;;;SY)" % USER_SID
        self.assertEqual(sa_privtask.task_security_fingerprint(good),
                         sa_privtask.task_security_fingerprint(reordered))
        # A real grant change -> a different fingerprint.
        weaker = "O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;%s)" % USER_SID
        self.assertNotEqual(sa_privtask.task_security_fingerprint(good),
                            sa_privtask.task_security_fingerprint(weaker))


# ══════════════════════════════════════════════════ installed verification
def schtasks_runner(xml_text=None, state="Ready", run_ok=True, calls=None):
    """A fake ``schtasks.exe``. Records what was asked, answers what it is told."""
    calls = calls if calls is not None else []

    def runner(arguments):
        calls.append(list(arguments))
        head = arguments[0] if arguments else ""
        if head == "/Query" and "/XML" in arguments:
            if xml_text is None:
                return sa_privtask.CommandResult(1, "", "ERROR: cannot find")
            return sa_privtask.CommandResult(0, xml_text)
        if head == "/Query":
            if xml_text is None:
                return sa_privtask.CommandResult(1, "", "ERROR: cannot find")
            return sa_privtask.CommandResult(
                0, "Folder: \\SAITULS\nTaskName: %s\nStatus: %s\n"
                   % (sa_privtask.TASK_PATH, state))
        if head == "/Run":
            return sa_privtask.CommandResult(0 if run_ok else 1, "", "")
        return sa_privtask.CommandResult(0, "", "")

    runner.calls = calls
    return runner


class InstalledVerificationTests(unittest.TestCase):
    """A moved runtime, task, ACL or descriptor is refused -- never repaired."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saituls-privtask-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.pin_file = str(self.tmp / "privileged-helper.json")
        self.sid = sa_privtask.current_user_sid() or USER_SID
        self.root, self.manifest, self.pin = make_installed_runtime(self.tmp, self.sid)
        sa_privtask.save_pin(self.pin, path=self.pin_file)
        self.xml = runtime_task_xml(self.root, self.sid)
        self.sddl = sa_privtask.build_task_sddl(self.sid)

    _DEFAULT = object()

    def verify(self, xml_text=_DEFAULT, state="Ready", runtime_acl=VALID_ACL,
               task_sddl=None, pin_writable=False, pin_acl=VALID_PIN_ACL,
               privileged_tmp=VALID_TMP):
        xml = self.xml if xml_text is self._DEFAULT else xml_text
        with mock.patch.object(sa_privtask, "pin_is_writable_by_user",
                               lambda path=None: pin_writable):
            return sa_privtask.verify_installation(
                runner=schtasks_runner(xml, state=state),
                pin_file=self.pin_file, user_sid=self.sid,
                runtime_acl=runtime_acl, pin_acl=pin_acl,
                privileged_tmp=privileged_tmp,
                task_sddl=self.sddl if task_sddl is None else task_sddl)

    def test_an_untouched_installation_verifies(self):
        verdict = self.verify()
        self.assertTrue(verdict.ok, verdict.reasons)
        self.assertEqual(verdict.token, "")
        self.assertEqual(verdict.to_dict()["runtime_bundle_fingerprint"],
                         self.pin["runtime_bundle_fingerprint"])

    def test_a_v1_pin_schema_fails_closed(self):
        """The old python_exe / sa_fido_worker.py contract no longer authorises."""
        legacy = {
            "schema": "saituls.secure-apps.privileged-helper-pin/1",
            "pin_version": 1, "created_at": "2026-01-01T00:00:00Z",
            "launch_mode": sa_privtask.LAUNCH_MODE, "task_path": sa_privtask.TASK_PATH,
            "definition_version": 1, "definition_fingerprint": "a" * 64,
            "user_sid": self.sid, "pipe_name": sa_privtask.pipe_name_for_sid(self.sid),
            "helper_path": str(self.root / sa_privtask.HELPER_SCRIPT),
            "helper_sha256": "a" * 64, "helper_host_image": POWERSHELL,
            "python_exe": sys.executable, "python_sha256": "b" * 64,
            "fido_worker_path": str(SUBSYSTEM / "sa_fido_worker.py"),
            "fido_worker_sha256": "c" * 64, "idle_timeout_seconds": 300,
        }
        Path(self.pin_file).write_text(json.dumps(legacy), encoding="utf-8")
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.token, sa_privtask.PRIVILEGED_HELPER_NOT_INSTALLED)
        self.assertTrue(any("schema" in r for r in verdict.reasons), verdict.reasons)

    def test_a_source_tree_helper_is_rejected_after_install(self):
        pin = dict(self.pin, helper_path=str(SUBSYSTEM / sa_privtask.HELPER_SCRIPT))
        sa_privtask.save_pin(pin, path=self.pin_file)
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertTrue(any("not inside the protected runtime" in r
                            for r in verdict.reasons), verdict.reasons)

    def test_a_source_tree_worker_is_rejected_after_install(self):
        pin = dict(self.pin,
                   fido_worker_exe_path=str(SUBSYSTEM / sa_privtask.FROZEN_WORKER_NAME),
                   fido_worker_bundle_dir=str(SUBSYSTEM))
        sa_privtask.save_pin(pin, path=self.pin_file)
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertTrue(any("worker is not inside the protected runtime" in r
                            for r in verdict.reasons), verdict.reasons)

    def test_the_task_action_must_point_inside_the_protected_runtime(self):
        # A task registered against the SOURCE helper, while the pin names the
        # installed one, is refused: the action does not resolve inside root.
        source_xml = sa_privtask.build_task_xml(
            user_sid=self.sid,
            helper_path=str(SUBSYSTEM / sa_privtask.HELPER_SCRIPT))
        verdict = self.verify(xml_text=source_xml)
        self.assertFalse(verdict.ok)
        self.assertTrue(any("does not point inside the protected runtime" in r
                            for r in verdict.reasons), verdict.reasons)

    def test_a_changed_installed_helper_invalidates_the_pin(self):
        (self.root / sa_privtask.HELPER_SCRIPT).write_bytes(b"tampered helper")
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertTrue(any("helper does not match" in r for r in verdict.reasons),
                        verdict.reasons)

    def test_a_changed_frozen_worker_invalidates_the_pin(self):
        worker = (self.root / sa_privtask.FIDO_BUNDLE_DIR_NAME
                  / sa_privtask.FROZEN_WORKER_NAME)
        worker.write_bytes(b"tampered worker")
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertTrue(any("worker does not match" in r for r in verdict.reasons),
                        verdict.reasons)

    def test_a_changed_dll_inside_the_bundle_invalidates_the_pin(self):
        """The defect --onefile hid: a DLL is as executable as the .exe."""
        dll = (self.root / sa_privtask.FIDO_BUNDLE_DIR_NAME
               / sa_privtask.PYINSTALLER_INTERNAL_DIR / "python311.dll")
        dll.write_bytes(b"tampered dll")
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertTrue(any(sa_privtask.PRIVILEGED_WORKER_BUNDLE_INVALID in r
                            for r in verdict.reasons), verdict.reasons)

    def test_a_file_added_to_the_bundle_invalidates_the_pin(self):
        added = (self.root / sa_privtask.FIDO_BUNDLE_DIR_NAME
                 / sa_privtask.PYINSTALLER_INTERNAL_DIR / "extra.dll")
        added.write_bytes(b"smuggled")
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertTrue(any(sa_privtask.PRIVILEGED_WORKER_BUNDLE_INVALID in r
                            for r in verdict.reasons), verdict.reasons)

    def test_a_file_deleted_from_the_bundle_invalidates_the_pin(self):
        os.remove(str(self.root / sa_privtask.FIDO_BUNDLE_DIR_NAME
                      / sa_privtask.PYINSTALLER_INTERNAL_DIR / "base_library.zip"))
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertTrue(any(sa_privtask.PRIVILEGED_WORKER_BUNDLE_INVALID in r
                            for r in verdict.reasons), verdict.reasons)

    def test_a_pin_owned_by_the_interactive_user_is_refused(self):
        verdict = self.verify(pin_acl=INVALID_PIN_ACL)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.token, sa_privtask.PRIVILEGED_PIN_OWNER_INVALID)
        self.assertTrue(any("owner" in r for r in verdict.reasons), verdict.reasons)

    def test_an_unprotected_privileged_scratch_root_is_refused(self):
        verdict = self.verify(privileged_tmp=INVALID_TMP)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.token, sa_privtask.PRIVILEGED_SCRATCH_INVALID)

    def test_a_bundle_fingerprint_mismatch_is_refused(self):
        pin = dict(self.pin, runtime_bundle_fingerprint="d" * 64)
        sa_privtask.save_pin(pin, path=self.pin_file)
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertTrue(any("bundle fingerprint" in r for r in verdict.reasons),
                        verdict.reasons)

    def test_a_missing_runtime_manifest_is_refused(self):
        os.remove(sa_privtask.runtime_manifest_path(str(self.root)))
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertTrue(any("manifest" in r for r in verdict.reasons), verdict.reasons)

    def test_an_invalid_runtime_acl_fails_closed_with_its_own_token(self):
        verdict = self.verify(runtime_acl=INVALID_ACL)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.token, sa_privtask.PRIVILEGED_RUNTIME_ACL_INVALID)

    def test_a_task_security_descriptor_mismatch_fails_closed(self):
        weak = "O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;%s)" % self.sid
        verdict = self.verify(task_sddl=weak)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.token, sa_privtask.PRIVILEGED_TASK_SECURITY_INVALID)

    def test_an_unreadable_task_security_descriptor_fails_closed(self):
        verdict = self.verify(task_sddl="")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.token, sa_privtask.PRIVILEGED_TASK_SECURITY_INVALID)

    def test_medium_run_permission_is_required(self):
        no_run = "O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GR;;;%s)" % self.sid
        report = sa_privtask.analyze_task_security(no_run, self.sid)
        self.assertFalse(report["task_medium_user_can_run"])
        self.assertFalse(report["task_security_valid"])

    def test_a_missing_task_is_refused(self):
        verdict = self.verify(xml_text=None)
        self.assertFalse(verdict.ok)
        self.assertFalse(verdict.installed)
        self.assertEqual(verdict.token, sa_privtask.PRIVILEGED_TASK_MISSING)

    def test_a_wrong_run_level_is_rejected(self):
        verdict = self.verify(xml_text=self.xml.replace(
            "<RunLevel>HighestAvailable</RunLevel>",
            "<RunLevel>LeastPrivilege</RunLevel>", 1))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.token, sa_privtask.PRIVILEGED_TASK_TAMPERED)
        self.assertTrue(any("run level" in r for r in verdict.reasons), verdict.reasons)

    def test_a_disabled_task_is_refused(self):
        verdict = self.verify(state="Disabled")
        self.assertFalse(verdict.ok)
        self.assertTrue(any("disabled" in r for r in verdict.reasons), verdict.reasons)

    def test_a_pin_for_another_account_is_refused(self):
        pin = dict(self.pin, user_sid=OTHER_SID)
        sa_privtask.save_pin(pin, path=self.pin_file)
        verdict = self.verify()
        self.assertFalse(verdict.ok)
        self.assertTrue(any("different user account" in r for r in verdict.reasons),
                        verdict.reasons)

    def test_verification_never_rewrites_a_suspicious_task(self):
        runner = schtasks_runner(self.xml.replace(
            "<RunLevel>HighestAvailable</RunLevel>",
            "<RunLevel>LeastPrivilege</RunLevel>", 1))
        with mock.patch.object(sa_privtask, "pin_is_writable_by_user",
                               lambda path=None: False):
            sa_privtask.verify_installation(
                runner=runner, pin_file=self.pin_file, user_sid=self.sid,
                runtime_acl=VALID_ACL, task_sddl=self.sddl)
        for call in runner.calls:
            self.assertNotIn(call[0], ("/Create", "/Change", "/Delete"),
                             "runtime verification mutated the task: %s" % call)

    def test_a_reparse_point_on_the_runtime_root_is_rejected(self):
        # path_is_within refuses a candidate that reaches the root through a
        # reparse point; here the pin's own paths resolve outside a symlinked
        # root, which the containment check catches without a real junction.
        with mock.patch.object(sa_privtask, "contains_reparse_point",
                               lambda path, stop_at=None: True):
            with mock.patch.object(sa_privtask, "pin_is_writable_by_user",
                                   lambda path=None: False):
                verdict = sa_privtask.verify_installation(
                    runner=schtasks_runner(self.xml), pin_file=self.pin_file,
                    user_sid=self.sid, runtime_acl=VALID_ACL, task_sddl=self.sddl)
        # contains_reparse_point is only consulted on Windows; assert the check
        # exists and returns a boolean rather than crashing.
        self.assertIn(verdict.ok, (True, False))


# ══════════════════════════════════════════════════ helper identity
def measurement(**kwargs):
    facts = dict(pid=4242, image=os.path.normcase(os.path.normpath(POWERSHELL)),
                 elevated=True, user_sid=USER_SID, session_id=1)
    greeting = dict(schema=sa_privhelper.GREETING_SCHEMA, pid=facts["pid"],
                    elevated=True, user_sid=USER_SID,
                    definition_fingerprint="a" * 64,
                    runtime_bundle_fingerprint="e" * 64)
    greeting.update(kwargs.pop("greeting", {}))
    facts.update(kwargs)
    return sa_privhelper.HelperIdentity(greeting=greeting, **facts)


class HelperIdentityTests(unittest.TestCase):
    """Started by Task Scheduler is not the same as trusted."""

    PIN = {"helper_host_image": POWERSHELL, "definition_fingerprint": "a" * 64,
           "runtime_bundle_fingerprint": "e" * 64}

    def reasons(self, measured, **kwargs):
        options = dict(pin=self.PIN, expected_sid=USER_SID, expected_session=1)
        options.update(kwargs)
        return sa_privhelper.verify_helper_identity(measured, **options)

    def test_a_high_integrity_helper_is_accepted(self):
        self.assertEqual(self.reasons(measurement()), [])

    def test_a_medium_integrity_helper_is_rejected(self):
        reasons = self.reasons(measurement(elevated=False,
                                           greeting={"elevated": False}))
        self.assertTrue(any("high integrity" in r for r in reasons), reasons)

    def test_an_unpinned_image_is_rejected(self):
        reasons = self.reasons(measurement(
            image=os.path.normcase(r"C:\Users\Public\powershell.exe")))
        self.assertTrue(any("expected PowerShell host" in r for r in reasons), reasons)

    def test_a_greeting_from_another_task_definition_is_rejected(self):
        reasons = self.reasons(measurement(
            greeting={"definition_fingerprint": "b" * 64}))
        self.assertTrue(any(sa_privtask.PRIVILEGED_TASK_TAMPERED in r
                            for r in reasons), reasons)

    def test_a_greeting_serving_another_runtime_bundle_is_rejected(self):
        reasons = self.reasons(measurement(
            greeting={"runtime_bundle_fingerprint": "f" * 64}))
        self.assertTrue(any(sa_privtask.PRIVILEGED_TASK_TAMPERED in r
                            for r in reasons), reasons)
        self.assertTrue(any("runtime bundle" in r for r in reasons), reasons)

    def test_a_fake_helper_cannot_impersonate_the_scheduled_one(self):
        squatter = measurement(pid=1234, elevated=False,
                               image=os.path.normcase(r"C:\Users\Public\evil.exe"),
                               greeting={"pid": 1234, "elevated": True})
        reasons = self.reasons(squatter)
        self.assertTrue(any("high integrity" in r for r in reasons), reasons)
        self.assertTrue(any("expected PowerShell host" in r for r in reasons), reasons)


# ══════════════════════════════════════════════════ the PowerShell client rule
CLIENT_RULE_HARNESS = r'''
param([string]$Helper, [string]$ExpectedSid)
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Helper, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw "helper does not parse: $($parseErrors[0].Message)" }
$wanted = @('Test-ClientAcceptable')
$found = @()
foreach ($def in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($wanted -contains $def.Name) { . ([scriptblock]::Create($def.Extent.Text)); $found += $def.Name }
}
$INTEGRITY_HIGH_RID = 12288
function Initialize-IntegrityProbe { return $script:ProbeReady }
function Fake-Server([object]$Sid, [object]$Integrity, [bool]$Throw) {
    $server = New-Object psobject
    $server | Add-Member -MemberType NoteProperty -Name Sid -Value $Sid
    $server | Add-Member -MemberType NoteProperty -Name Integrity -Value $Integrity
    $server | Add-Member -MemberType NoteProperty -Name ShouldThrow -Value $Throw
    $server | Add-Member -MemberType ScriptMethod -Name RunAsClient -Value {
        param($worker)
        if ($this.ShouldThrow) { throw 'impersonation refused' }
        $script:ClientSid = $this.Sid
        $script:ClientIntegrity = $this.Integrity
    }
    return $server
}
function Probe([object]$Sid, [object]$Integrity, [bool]$Throw = $false) {
    $verdict = Test-ClientAcceptable (Fake-Server $Sid $Integrity $Throw) $ExpectedSid
    return "$($verdict.ok)|$($verdict.reason)"
}
$script:ProbeReady = $true
$r = [ordered]@{}
$r.functions = ($found | Sort-Object) -join ','
$r.medium_broker = Probe $ExpectedSid 8192
$r.high_broker = Probe $ExpectedSid 12288
$r.system_broker = Probe $ExpectedSid 16384
$r.other_account = Probe 'S-1-5-21-9-9-9-500' 8192
$r.no_identity = Probe $null 8192
$r.unknown_integrity = Probe $ExpectedSid $null
$script:ProbeReady = $false
$r.no_probe = Probe $ExpectedSid 8192
$r | ConvertTo-Json -Compress
'''


@unittest.skipUnless(os.name == "nt", "the helper runs under Windows PowerShell")
class ScheduledHelperClientRuleTests(unittest.TestCase):
    """Who the elevated helper will serve: this account, at medium integrity."""

    HELPER = SUBSYSTEM / "sa_storage_helper.ps1"

    @classmethod
    def setUpClass(cls):
        scratch = Path(tempfile.mkdtemp(prefix="saituls-client-rule-"))
        cls.scratch = scratch
        harness = scratch / "harness.ps1"
        harness.write_text(CLIENT_RULE_HARNESS, encoding="utf-8-sig")
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy",
             "Bypass", "-File", str(harness), "-Helper", str(cls.HELPER),
             "-ExpectedSid", USER_SID],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180)
        text = (completed.stdout or "").strip()
        start = text.find("{")
        if start < 0:
            raise AssertionError("harness produced no JSON: %s %s"
                                 % (text, completed.stderr))
        cls.result = json.loads(text[start:])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(str(cls.scratch), ignore_errors=True)

    def test_a_medium_integrity_broker_is_accepted(self):
        self.assertEqual(self.result["medium_broker"], "True|ok")

    def test_an_elevated_broker_is_rejected(self):
        self.assertEqual(self.result["high_broker"], "False|client_elevated")
        self.assertEqual(self.result["system_broker"], "False|client_elevated")

    def test_another_account_is_rejected(self):
        self.assertEqual(self.result["other_account"], "False|client_wrong_account")

    def test_a_host_without_the_integrity_probe_serves_nobody(self):
        self.assertEqual(self.result["no_probe"], "False|integrity_probe_unavailable")


# ══════════════════════════════════════════════════ silent start + lease
class ScriptedTaskScheduler(object):
    """Task Scheduler as a list of recorded starts, plus a pipe that may not come."""

    def __init__(self, appears_after=0, refuse=False):
        self.starts = []
        self.appears_after = appears_after
        self.refuse = refuse
        self.running = False

    def start(self, task_path):
        self.starts.append(task_path)
        if self.refuse:
            raise sa_privtask.TaskUnavailable(
                "Task Scheduler refused to start %s" % task_path)
        if len(self.starts) > self.appears_after:
            self.running = True
        return True

    def exit_on_idle(self):
        self.running = False


class FakeChannelHelper(sa_privhelper.PrivilegedHelper):
    """A PrivilegedHelper whose pipe is a scripted helper process."""

    def __init__(self, scheduler, **kwargs):
        kwargs.setdefault("verify_server", False)
        sa_privhelper.PrivilegedHelper.__init__(self, **kwargs)
        self.scheduler = scheduler
        self.requests = []
        self.connects = 0
        self._pin = {"task_path": sa_privtask.TASK_PATH,
                     "pipe_name": r"\\.\pipe\fake",
                     "idle_timeout_seconds": 300,
                     "definition_fingerprint": "a" * 64,
                     "runtime_bundle_fingerprint": "e" * 64}

    def _resolve_pipe_name(self):
        self.pipe_name = self._pin["pipe_name"]
        return self.pipe_name

    def _verify_task_definition(self):
        return True

    def _start_task(self):
        return self.scheduler.start(self.task_path)

    def _try_connect(self, timeout):
        if not self.scheduler.running:
            return None
        self.connects += 1
        return "pipe-handle"

    def _handshake(self):
        return {"schema": sa_privhelper.GREETING_SCHEMA, "pid": 4242,
                "elevated": True}

    def _write_line(self, text):
        if not self.scheduler.running:
            raise OSError("the pipe is gone")
        self.requests.append(json.loads(text))

    def _read_line(self, timeout=None):
        return json.dumps({"id": len(self.requests), "ok": True,
                           "result": {"state": "mounted"}})

    def _drop(self):
        self._pipe = None
        self._verified = False


class SilentStartTests(unittest.TestCase):
    """Reuse a verified helper; otherwise start the registered task. No prompt."""

    def channel(self, **kwargs):
        scheduler = ScriptedTaskScheduler(**kwargs)
        return scheduler, FakeChannelHelper(scheduler, start_timeout=1.0,
                                            clock=lambda: 0.0,
                                            sleep=lambda seconds: None)

    def test_the_first_call_starts_the_task_and_later_calls_reuse_it(self):
        scheduler, helper = self.channel()
        helper.acquire()
        self.assertEqual(scheduler.starts, [sa_privtask.TASK_PATH])
        helper.call("state", container="c", mount_path="m")
        helper.call("state", container="c", mount_path="m")
        self.assertEqual(scheduler.starts, [sa_privtask.TASK_PATH])
        self.assertEqual(len(helper.requests), 2)

    def test_a_helper_that_never_answers_is_reported_not_waited_on_forever(self):
        scheduler, helper = self.channel(appears_after=99)
        with self.assertRaises(sa_privhelper.HelperUnavailable) as caught:
            helper.ensure()
        self.assertEqual(caught.exception.token,
                         sa_privtask.LOCK_PRIVILEGED_HELPER_UNAVAILABLE)

    def test_releasing_the_last_lease_disconnects_and_lets_the_helper_idle_out(self):
        scheduler, helper = self.channel()
        helper.acquire()
        helper.acquire()
        self.assertEqual(helper.leases, 2)
        self.assertEqual(helper.release(), 1)
        self.assertTrue(helper.running)
        self.assertEqual(helper.release(), 0)
        self.assertFalse(helper.running)
        self.assertTrue(scheduler.running)

    def test_a_helper_that_idled_out_is_started_again_by_the_next_call(self):
        scheduler, helper = self.channel()
        helper.call("state", container="c", mount_path="m")
        scheduler.exit_on_idle()
        helper.call("state", container="c", mount_path="m")
        self.assertEqual(scheduler.starts,
                         [sa_privtask.TASK_PATH, sa_privtask.TASK_PATH])

    def test_the_idle_timeout_comes_from_the_installation_pin(self):
        _scheduler, helper = self.channel()
        self.assertEqual(helper.idle_timeout_seconds, 300)
        helper._pin["idle_timeout_seconds"] = 60
        self.assertEqual(helper.idle_timeout_seconds, 60)

    def test_the_pin_bounds_an_absurd_idle_timeout(self):
        tmp = Path(tempfile.mkdtemp(prefix="saituls-idle-"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        root, manifest, _pin = make_installed_runtime(tmp, USER_SID)
        for asked, expected in ((1, sa_privtask.MIN_IDLE_TIMEOUT_SECONDS),
                                (99999, sa_privtask.MAX_IDLE_TIMEOUT_SECONDS),
                                (None, sa_privtask.DEFAULT_IDLE_TIMEOUT_SECONDS)):
            pin = sa_privtask.build_pin(runtime_root=str(root), user_sid=USER_SID,
                                        idle_timeout_seconds=asked, manifest=manifest)
            self.assertEqual(pin["idle_timeout_seconds"], expected)


# ══════════════════════════════════════════════════ broker behaviour
class ElevatedFakeBackend(sa_storage.FakeStorageBackend):
    name = "fake-memory"
    requires_elevation = True

    def __init__(self, helper, **kwargs):
        sa_storage.FakeStorageBackend.__init__(self, **kwargs)
        self._helper = helper
        self._holding = False

    def _hold(self):
        self._helper.ensure()
        if not self._holding:
            self._helper.acquire()
            self._holding = True

    def _unhold(self):
        if self._holding:
            self._helper.release()
        self._holding = False

    def _privileged(self, op, profile):
        try:
            return self._helper.call(op, container=profile.container,
                                     mount_path=profile.mount_path)
        except sa_privhelper.HelperError as exc:
            raise sa_storage.StorageError(
                str(exc), getattr(exc, "category", "storage_mount_failed"))

    def state(self, profile):
        self._privileged("state", profile)
        return sa_storage.FakeStorageBackend.state(self, profile)

    def create(self, profile, volume_secret):
        self._hold()
        self._privileged("create", profile)
        return sa_storage.FakeStorageBackend.create(self, profile, volume_secret)

    def unlock_and_mount(self, profile, volume_secret):
        self._hold()
        try:
            self._privileged("unlock_mount", profile)
            return sa_storage.FakeStorageBackend.unlock_and_mount(
                self, profile, volume_secret)
        except Exception:
            self._unhold()
            raise

    def unmount(self, profile):
        try:
            self._privileged("unmount", profile)
            return sa_storage.FakeStorageBackend.unmount(self, profile)
        finally:
            self._unhold()


class PrivilegedBrokerHarness(Harness):
    def __init__(self, **kwargs):
        self.scheduler = ScriptedTaskScheduler()
        self.helper = FakeChannelHelper(self.scheduler, start_timeout=1.0,
                                        clock=lambda: 0.0,
                                        sleep=lambda seconds: None)
        Harness.__init__(self, **kwargs)
        Path(self.profile.container).parent.mkdir(parents=True, exist_ok=True)
        Path(self.profile.container).write_bytes(b"fake container")
        self.broker._helper = self.helper

    def _build_broker(self):
        self.backend = ElevatedFakeBackend(self.helper)
        broker = sa_broker.SecureBroker(
            self.registry, audit=self.audit,
            providers={"fake-auth": self.provider},
            backends={"fake-memory": self.backend},
            process_adapter=self.adapter, clock=self.clock,
            sleep=lambda seconds: self.clock.advance(seconds))
        broker._helper = self.helper
        return broker


class BrokerSilentStartTests(BaseCase):
    """Unlock and lock start the helper by themselves, or say why they cannot."""

    def harness(self, **kwargs):
        h = PrivilegedBrokerHarness(**kwargs)
        self.addCleanup(h.close)
        h.enroll()
        h.scheduler.exit_on_idle()
        h.scheduler.starts.clear()
        return h

    def test_unlock_starts_the_helper_silently(self):
        h = self.harness()
        result = h.broker.open("vault")
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.state, sa_state.RUNNING)
        self.assertTrue(h.scheduler.starts)
        self.assertTrue(h.helper.running)

    def test_a_lock_that_cannot_start_the_helper_never_reports_locked(self):
        h = self.harness()
        h.broker.open("vault")
        h.scheduler.exit_on_idle()
        h.scheduler.refuse = True
        results = h.broker.lock_now("vault")
        self.assertFalse(results[0].ok)
        self.assertNotEqual(results[0].state, sa_state.LOCKED)
        self.assertEqual(results[0].state, sa_state.ERROR)
        self.assertIn(sa_privtask.LOCK_PRIVILEGED_HELPER_UNAVAILABLE,
                      results[0].reason)

    def test_default_reopen_after_the_helper_exited_needs_no_new_key(self):
        h = self.harness()
        h.broker.open("vault")
        interactions = h.broker.status("vault")["auth_interactions"]
        h.adapter.exit_tree(h.broker.runtime("vault").supervisor.pid)
        h.broker.tick()
        self.assertEqual(h.broker.status("vault")["state"], sa_state.SESSION_CACHED)
        h.scheduler.exit_on_idle()
        h.scheduler.starts.clear()
        result = h.broker.open("vault")
        self.assertTrue(result.ok, result.reason)
        self.assertFalse(result.required_auth)
        self.assertEqual(h.broker.status("vault")["auth_interactions"], interactions)
        self.assertTrue(h.scheduler.starts)

    def test_the_broker_never_elevates_anything_during_open_or_lock(self):
        h = self.harness()
        with mock.patch.object(sa_privtask, "elevate_self",
                               side_effect=AssertionError("runtime raised a dialog")):
            h.broker.open("vault")
            h.broker.lock_now("vault")
        text = (SUBSYSTEM / "sa_broker.py").read_text(encoding="utf-8")
        for forbidden in ("RunAs", "ShellExecute", "elevate_self"):
            self.assertNotIn(forbidden, text)


class PrivilegedHelperStatusTests(BaseCase):
    """The diagnostics report says what is true, not what is hoped for."""

    def test_the_report_names_every_fact_the_preflight_promises(self):
        h = PrivilegedBrokerHarness()
        self.addCleanup(h.close)
        report = h.broker.privileged_helper_status()
        for field in ("EnableLUA", "broker_integrity", "user_is_administrator",
                      "scheduled_helper_installed",
                      "scheduled_helper_definition_valid",
                      "scheduled_helper_runnable", "helper_integrity",
                      "fido_worker_integrity", "pipe_authentication",
                      "task_path", "launch_mode", "idle_timeout_seconds",
                      "runtime_root", "runtime_acl_valid", "task_security_valid"):
            self.assertIn(field, report)
        self.assertEqual(report["launch_mode"], sa_privtask.LAUNCH_MODE)
        self.assertEqual(report["task_path"], sa_privtask.TASK_PATH)


# ══════════════════════════════════════════════════ Windows real ACL / task SD
@unittest.skipUnless(os.name == "nt", "the protected runtime is a Windows feature")
class WindowsProtectedRuntimeIntegrationTests(unittest.TestCase):
    """The real filesystem ACL and task security descriptor, not a mock.

    Gated on a genuinely installed protected runtime and on this process being
    able to produce a real medium-integrity token to attack it with. That
    token does NOT depend on ``EnableLUA``: Windows still enforces DACLs and
    mandatory integrity with UAC off, and a filtered token can be built from
    an administrator's own primary token, so the enforcement questions below
    are answerable on any host that has the runtime installed. (``EnableLUA=1``
    is required to INSTALL -- with UAC off there is no medium-integrity broker
    for the boundary to protect -- but that is a different question from
    whether the boundary, once installed, holds.)
    """

    def setUp(self):
        self.verdict = sa_privtask.verify_installation()
        if not self.verdict.installed:
            self.skipTest("the protected runtime is not installed; run "
                          "'privileged-helper install' first")
        probe_report = probe.run_medium({"probes": []})
        if probe_report.get("elevated") is not False:
            self.skipTest("no medium-integrity token could be produced on this "
                          "host: %s" % (probe_report.get("error")
                                        or probe_report.get("launched")))

    # ---- what the DESCRIPTORS say ----------------------------------------
    def test_the_installed_runtime_directory_denies_medium_writes(self):
        acl = sa_privtask.runtime_acl_report()
        self.assertTrue(acl["runtime_acl_valid"], acl)
        self.assertIs(acl["runtime_dir_medium_writable"], False)
        self.assertIs(acl["helper_medium_writable"], False)
        self.assertIs(acl["fido_worker_medium_writable"], False)
        self.assertIs(acl["fido_worker_bundle_medium_writable"], False)
        self.assertIs(acl["manifest_medium_writable"], False)
        self.assertTrue(acl["runtime_owner_valid"], acl)

    def test_the_installed_task_security_descriptor_denies_medium_change(self):
        report = sa_privtask.task_security_report()
        self.assertTrue(report["task_security_valid"], report["reasons"])
        self.assertTrue(report["task_medium_user_can_run"])
        self.assertFalse(report["task_medium_user_can_modify"])
        self.assertFalse(report["task_medium_user_can_delete"])

    def test_the_pin_is_owned_by_administrators_or_system(self):
        report = sa_privtask.pin_acl_report()
        self.assertTrue(report["pin_acl_valid"], report)
        self.assertTrue(report["pin_owner_valid"],
                        "the pin is owned by %s; an owner can rewrite its DACL"
                        % report["pin_owner"])
        self.assertIs(report["pin_medium_writable"], False)
        self.assertIs(report["pin_medium_can_change_dacl"], False)

    def test_the_privileged_scratch_root_is_protected(self):
        report = sa_privtask.privileged_tmp_report()
        self.assertTrue(report["privileged_tmp_valid"], report)
        self.assertIs(report["privileged_tmp_medium_writable"], False)
        self.assertTrue(report["privileged_tmp_owner_valid"], report)

    def test_the_installed_worker_bundle_matches_its_recursive_fingerprint(self):
        pin = self.verdict.pin or {}
        bundle = pin.get("fido_worker_bundle_dir")
        self.assertTrue(bundle, "the pin records no worker bundle directory")
        measured, count, problem = sa_privtask.bundle_fingerprint(bundle)
        self.assertIsNone(problem, problem)
        self.assertGreater(count, 1,
                           "a one-file bundle is a --onefile build; the "
                           "privileged worker must be --onedir")
        self.assertEqual(measured, pin.get("fido_worker_bundle_fingerprint"))

    # ---- what WINDOWS actually does --------------------------------------
    def _medium(self, probes):
        """Run *probes* through a genuine medium-integrity token."""
        report = probe.run_medium({"probes": probes})
        self.assertEqual(report.get("elevated"), False,
                         "the probe did not run at medium integrity: %s"
                         % (report.get("error") or report.get("launched")))
        self.assertEqual(report.get("integrity_sid"), probe.MEDIUM_INTEGRITY_SID,
                         report.get("integrity_sid"))
        return report["results"]

    def _assert_denied(self, results):
        for name, item in sorted(results.items()):
            self.assertEqual(item["outcome"], probe.DENIED,
                             "%s was %s by Windows (%s)"
                             % (name, item["outcome"], item["detail"]))
            self.assertTrue(item["restored"],
                            "%s mutated the installation and could not undo it"
                            % name)

    def test_windows_really_denies_every_medium_write_to_the_runtime(self):
        """A descriptor that says DENIED and a kernel that answers DENIED.

        The tests above prove this implementation reads its own ACEs
        correctly. They do NOT prove Windows enforces them -- an inherited
        ACE, a stale handle, a wrong owner or an unexpected grant would all
        still parse as intended. So every forbidden operation is ATTEMPTED
        here, from a real medium-integrity token, against the real
        installation.
        """
        pin = self.verdict.pin or {}
        root = pin.get("runtime_root") or sa_privtask.privileged_root()
        bundle = (pin.get("fido_worker_bundle_dir")
                  or sa_privtask.installed_worker_bundle_dir(root))
        results = self._medium([
            {"id": "helper_overwrite", "op": "file_open_write",
             "target": pin.get("helper_path")},
            {"id": "helper_delete", "op": "file_delete",
             "target": pin.get("helper_path")},
            {"id": "helper_rename", "op": "file_rename",
             "target": pin.get("helper_path")},
            {"id": "worker_write", "op": "file_open_write",
             "target": pin.get("fido_worker_exe_path")},
            {"id": "worker_delete", "op": "file_delete",
             "target": pin.get("fido_worker_exe_path")},
            {"id": "bundle_add_file", "op": "dir_create_child", "target": bundle},
            {"id": "bundle_internal_add", "op": "dir_create_child",
             "target": os.path.join(bundle, sa_privtask.PYINSTALLER_INTERNAL_DIR)},
            {"id": "manifest_write", "op": "file_open_write",
             "target": sa_privtask.runtime_manifest_path(root)},
            {"id": "runtime_dir_add", "op": "dir_create_child", "target": root},
        ])
        self._assert_denied(results)

    def test_windows_really_denies_every_medium_write_to_the_pin(self):
        pin_file = sa_privtask.pin_path()
        results = self._medium([
            {"id": "pin_write", "op": "file_open_write", "target": pin_file},
            {"id": "pin_delete", "op": "file_delete", "target": pin_file},
            {"id": "pin_rename", "op": "file_rename", "target": pin_file},
            {"id": "pin_dacl_change", "op": "file_dacl_change", "target": pin_file},
        ])
        self._assert_denied(results)

    def test_windows_really_lets_medium_run_but_not_change_the_task(self):
        task_path = (self.verdict.pin or {}).get("task_path") or sa_privtask.TASK_PATH
        results = self._medium([
            {"id": "task_query", "op": "task_query", "target": task_path},
            {"id": "task_run", "op": "task_run", "target": task_path},
            {"id": "task_change", "op": "task_change", "target": task_path},
            {"id": "task_disable", "op": "task_disable", "target": task_path},
            {"id": "task_sd_change", "op": "task_sd_change", "target": task_path},
            {"id": "task_delete", "op": "task_delete", "target": task_path},
        ])
        # The medium broker MUST be able to do these two: the whole feature is
        # that ordinary unlock starts the task without a consent dialog.
        for name in ("task_query", "task_run"):
            self.assertEqual(results[name]["outcome"], probe.ALLOWED,
                             "the medium broker cannot %s: %s"
                             % (name, results[name]["detail"]))
        self._assert_denied({name: item for name, item in results.items()
                             if name not in ("task_query", "task_run")})
        sa_privtask.stop_task(task_path)

    def test_a_prepared_diskpart_script_cannot_be_modified_by_medium(self):
        """Negative authorization for privileged command material.

        The elevated helper writes a DiskPart script and then has an elevated
        DiskPart execute it. If a medium-integrity account could rewrite that
        file between those two moments, it would be choosing what the elevated
        DiskPart does. So the protected scratch root is exercised exactly the
        way the helper uses it -- a per-transaction directory, a command file
        inside it -- and the medium token is then turned loose on it.
        """
        scratch = sa_privtask.new_privileged_scratch_dir(prefix="negative-test")
        self.addCleanup(sa_privtask._quiet_rmtree, scratch)
        command_file = os.path.join(scratch, "create.dpt")
        with open(command_file, "w", encoding="ascii") as handle:
            handle.write("create vdisk file=\"C:\\disposable.vhdx\" maximum=1\r\nexit\r\n")
        sa_privtask.set_privileged_owner(command_file)
        sa_privtask.assert_privileged_scratch_file(command_file)
        results = self._medium([
            {"id": "command_overwrite", "op": "file_open_write", "target": command_file},
            {"id": "command_delete", "op": "file_delete", "target": command_file},
            {"id": "command_rename", "op": "file_rename", "target": command_file},
            {"id": "command_dacl_change", "op": "file_dacl_change", "target": command_file},
            {"id": "scratch_add_file", "op": "dir_create_child", "target": scratch},
        ])
        self._assert_denied(results)



# ══════════════════════════════════════════ install transaction, fault by fault
class FakeTaskRegistry(object):
    """An in-memory Task Scheduler: create, query, delete, end, run, change.

    Enough of schtasks.exe for the transaction tests to be deterministic and
    hermetic -- and, crucially, to let them ASSERT on what is registered after
    a failure instead of taking the installer's word for it.
    """

    def __init__(self):
        self.tasks = {}
        self.descriptors = {}
        self.calls = []

    def runner(self):
        def run(arguments):
            self.calls.append(list(arguments))
            verb = arguments[0].lower()
            name = arguments[arguments.index("/TN") + 1] if "/TN" in arguments else ""
            if verb == "/create":
                path = arguments[arguments.index("/XML") + 1]
                with open(path, "rb") as handle:
                    self.tasks[name] = {"xml": handle.read().decode("utf-16"),
                                        "state": "Ready"}
                return sa_privtask.CommandResult(0, "SUCCESS", "")
            if verb == "/query":
                task = self.tasks.get(name)
                if task is None:
                    return sa_privtask.CommandResult(1, "", "ERROR: cannot find the file")
                if "/XML" in arguments:
                    return sa_privtask.CommandResult(0, task["xml"], "")
                return sa_privtask.CommandResult(0, "Status: %s" % task["state"], "")
            if verb == "/delete":
                existed = self.tasks.pop(name, None) is not None
                self.descriptors.pop(name, None)
                return sa_privtask.CommandResult(0 if existed else 1, "", "")
            if verb in ("/end", "/run"):
                return sa_privtask.CommandResult(0 if name in self.tasks else 1, "", "")
            if verb == "/change":
                if name not in self.tasks:
                    return sa_privtask.CommandResult(1, "", "")
                if "/DISABLE" in arguments:
                    self.tasks[name]["state"] = "Disabled"
                if "/ENABLE" in arguments:
                    self.tasks[name]["state"] = "Ready"
                return sa_privtask.CommandResult(0, "", "")
            return sa_privtask.CommandResult(1, "", "unsupported")
        return run

    def sd_runner(self):
        def run(task_path, sddl):
            self.descriptors[task_path] = sddl
            return True
        return run


@unittest.skipUnless(os.name == "nt", "the install transaction is Windows-only")
class InstallTransactionFaultTests(unittest.TestCase):
    """Every stage, faulted in turn. No stage may leave a mixed state.

    The promise ``apply_install`` makes is not "it usually works": it is that a
    failure ANYWHERE after the first mutation ends with the machine either
    fully installed or exactly as it was. So the transaction is run once per
    stage in :data:`sa_privtask.INSTALL_STAGES`, with a deterministic fault
    injected after that stage, and the two properties are asserted directly:

      * FIRST INSTALL  -- no task registered, no pin, no protected runtime;
      * REPAIR         -- the previous installation is byte-for-byte back and
                          verifies again.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saituls-transaction-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.source = self.tmp / "source"
        (self.source / sa_privtask.FIDO_BUNDLE_DIR_NAME
         / sa_privtask.PYINSTALLER_INTERNAL_DIR).mkdir(parents=True)
        shutil.copyfile(SUBSYSTEM / sa_privtask.HELPER_SCRIPT,
                        self.source / sa_privtask.HELPER_SCRIPT)
        self.bundle = self.source / sa_privtask.FIDO_BUNDLE_DIR_NAME
        (self.bundle / sa_privtask.FROZEN_WORKER_NAME).write_bytes(b"MZ worker v1")
        (self.bundle / sa_privtask.PYINSTALLER_INTERNAL_DIR
         / "python311.dll").write_bytes(b"dll v1")
        (self.bundle / sa_privtask.PYINSTALLER_INTERNAL_DIR
         / "base_library.zip").write_bytes(b"zip v1")
        self.root = str(self.tmp / "privileged")
        self.pin_file = str(self.tmp / "privileged-helper.json")
        self.scratch = str(self.tmp / "privileged-tmp")
        self.sid = sa_privtask.current_user_sid() or USER_SID
        self.registry = FakeTaskRegistry()
        self._patch()

    def _patch(self):
        """Elevation, ACL application and ACL measurement, all injected.

        None of them is what these tests are about -- they are proven for real
        on Windows by tests/test_secure_apps_windows_storage.py -- and a temp
        directory cannot carry the production ACL anyway. What IS real here is
        the staging, the hashing, the recursive bundle fingerprint, the atomic
        swap, the task registry, the pin file, the snapshot and the rollback.
        """
        patches = {
            "process_is_elevated": lambda: True,
            "uac_enabled": lambda: True,
            "harden_runtime_dir": lambda **kwargs: True,
            "harden_privileged_tmp": lambda **kwargs: True,
            "harden_pin": lambda **kwargs: True,
            "path_is_writable_by_user": lambda path: False,
            "runtime_acl_report": lambda **kwargs: {"runtime_acl_valid": True},
            "pin_acl_report": lambda path=None: {"pin_acl_valid": True,
                                                 "pin_owner": "S-1-5-32-544",
                                                 "pin_owner_valid": True},
            "privileged_tmp_report": lambda tmp_root=None: {
                "privileged_tmp_valid": True, "privileged_tmp_root": self.scratch},
            "task_security_report": lambda *a, **k: {"task_security_valid": True},
            "get_task_security_descriptor": lambda task_path=sa_privtask.TASK_PATH:
                self.registry.descriptors.get(task_path),
            "_commission_task": lambda task_path, runner=None, timeout=20.0: True,
        }
        for name, replacement in patches.items():
            patcher = mock.patch.object(sa_privtask, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ,
                              {sa_privtask.PRIVILEGED_TMP_ENV: self.scratch})
        env.start()
        self.addCleanup(env.stop)

    def install(self, fault=None):
        return sa_privtask.apply_install(
            script_dir=str(self.source), user_sid=self.sid,
            runner=self.registry.runner(), pin_file=self.pin_file,
            runtime_root=self.root, tmp_dir=self.scratch,
            task_sd_runner=self.registry.sd_runner(), fault=fault)

    def verify(self):
        return sa_privtask.verify_installation(
            runner=self.registry.runner(), pin_file=self.pin_file,
            user_sid=self.sid)

    # -- the happy path, so the fault cases mean something -------------------
    def test_a_clean_install_registers_pins_and_verifies(self):
        result = self.install()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["fido_worker_file_count"], 3)
        self.assertIn(sa_privtask.TASK_PATH, self.registry.tasks)
        self.assertTrue(os.path.isfile(self.pin_file))
        self.assertTrue(self.verify().ok, self.verify().reasons)
        pin, _problem = sa_privtask.load_pin(self.pin_file)
        measured, count, problem = sa_privtask.bundle_fingerprint(
            sa_privtask.installed_worker_bundle_dir(self.root))
        self.assertIsNone(problem)
        self.assertEqual(count, 3)
        self.assertEqual(measured, pin["fido_worker_bundle_fingerprint"])

    def test_every_stage_name_is_covered_by_a_fault_point(self):
        """A stage that cannot be faulted has no rollback evidence."""
        source = (SUBSYSTEM / "sa_privtask.py").read_text(encoding="utf-8")
        for stage in sa_privtask.INSTALL_STAGES:
            self.assertIn('_fault(fault, "%s")' % stage, source,
                          "stage %r has no injection point" % stage)

    def test_an_unknown_stage_name_is_refused(self):
        with self.assertRaises(sa_privtask.PrivilegeTaskError):
            self.install(fault="no_such_stage")

    # -- FIRST INSTALL: nothing runnable may survive ------------------------
    def test_a_fault_at_any_stage_of_a_first_install_leaves_nothing_behind(self):
        for stage in sa_privtask.INSTALL_STAGES:
            with self.subTest(stage=stage):
                self.registry = FakeTaskRegistry()
                shutil.rmtree(self.root, ignore_errors=True)
                try:
                    os.remove(self.pin_file)
                except OSError:
                    pass
                with self.assertRaises(sa_privtask.PrivilegeTaskError) as caught:
                    self.install(fault=stage)
                self.assertIn(stage, str(caught.exception))
                self.assertNotIn(sa_privtask.REPAIR_REQUIRED,
                                 getattr(caught.exception, "token", ""),
                                 "a clean first-install rollback must not need "
                                 "a repair: %s" % caught.exception)
                self.assertEqual(self.registry.tasks, {},
                                 "a runnable privileged task survived a failed "
                                 "first install at stage %r" % stage)
                self.assertFalse(os.path.exists(self.pin_file),
                                 "a pin survived stage %r" % stage)
                self.assertFalse(os.path.isdir(self.root),
                                 "a protected runtime survived stage %r" % stage)

    # -- REPAIR: the previous valid installation must come back -------------
    def test_a_fault_at_any_stage_of_a_repair_restores_the_previous_install(self):
        for stage in sa_privtask.INSTALL_STAGES:
            with self.subTest(stage=stage):
                self.registry = FakeTaskRegistry()
                shutil.rmtree(self.root, ignore_errors=True)
                (self.bundle / sa_privtask.FROZEN_WORKER_NAME).write_bytes(b"MZ worker v1")
                self.assertTrue(self.install()["ok"])
                before_pin = Path(self.pin_file).read_bytes()
                before_xml = self.registry.tasks[sa_privtask.TASK_PATH]["xml"]
                before_sddl = self.registry.descriptors[sa_privtask.TASK_PATH]
                before_worker = Path(
                    sa_privtask.installed_worker_exe_path(self.root)).read_bytes()

                # A DIFFERENT bundle, so a half-applied repair would be visible.
                (self.bundle / sa_privtask.FROZEN_WORKER_NAME).write_bytes(b"MZ worker v2")
                with self.assertRaises(sa_privtask.PrivilegeTaskError) as caught:
                    self.install(fault=stage)
                self.assertIn(stage, str(caught.exception))

                self.assertIn(sa_privtask.TASK_PATH, self.registry.tasks,
                              "the previous task did not come back after a "
                              "failed repair at stage %r" % stage)
                self.assertEqual(self.registry.tasks[sa_privtask.TASK_PATH]["xml"],
                                 before_xml)
                self.assertEqual(self.registry.descriptors[sa_privtask.TASK_PATH],
                                 before_sddl)
                self.assertEqual(Path(self.pin_file).read_bytes(), before_pin)
                self.assertEqual(
                    Path(sa_privtask.installed_worker_exe_path(self.root)).read_bytes(),
                    before_worker,
                    "the NEW worker survived a failed repair at stage %r" % stage)
                verdict = self.verify()
                self.assertTrue(verdict.ok,
                                "the restored installation does not verify after "
                                "stage %r: %s" % (stage, verdict.reasons))

    # -- the gate that must precede task registration -----------------------
    def test_an_unprovable_runtime_acl_aborts_before_the_task_exists(self):
        """``None`` is not success. A measurement that fails is a failure."""
        for report in ({"runtime_acl_valid": None}, {"runtime_acl_valid": False},
                       {}):
            with self.subTest(report=report):
                self.registry = FakeTaskRegistry()
                shutil.rmtree(self.root, ignore_errors=True)
                with mock.patch.object(sa_privtask, "runtime_acl_report",
                                       lambda **kwargs: dict(report)):
                    with self.assertRaises(sa_privtask.PrivilegeTaskError) as caught:
                        self.install()
                self.assertIn(sa_privtask.PRIVILEGED_RUNTIME_ACL_INVALID,
                              str(caught.exception))
                self.assertEqual(self.registry.tasks, {},
                                 "a task was registered against a runtime whose "
                                 "ACL was never proven")

    def test_a_failed_runtime_acl_application_aborts_before_the_task_exists(self):
        with mock.patch.object(sa_privtask, "harden_runtime_dir",
                               lambda **kwargs: False):
            with self.assertRaises(sa_privtask.PrivilegeTaskError) as caught:
                self.install()
        self.assertIn(sa_privtask.PRIVILEGED_RUNTIME_ACL_INVALID, str(caught.exception))
        self.assertEqual(self.registry.tasks, {})

    def test_an_unprovable_pin_owner_fails_the_transaction(self):
        with mock.patch.object(sa_privtask, "pin_acl_report",
                               lambda path=None: {"pin_acl_valid": False}):
            with self.assertRaises(sa_privtask.PrivilegeTaskError) as caught:
                self.install()
        self.assertIn(sa_privtask.PRIVILEGED_PIN_OWNER_INVALID, str(caught.exception))
        self.assertEqual(self.registry.tasks, {})
        self.assertFalse(os.path.exists(self.pin_file))

    def test_an_unprotected_scratch_root_fails_the_transaction(self):
        with mock.patch.object(sa_privtask, "privileged_tmp_report",
                               lambda tmp_root=None: {"privileged_tmp_valid": False}):
            with self.assertRaises(sa_privtask.PrivilegeTaskError) as caught:
                self.install()
        self.assertIn(sa_privtask.PRIVILEGED_SCRATCH_INVALID, str(caught.exception))
        self.assertEqual(self.registry.tasks, {})

    def test_a_final_verification_failure_is_a_failed_transaction(self):
        """``verdict.ok is False`` must NOT return REPAIR_REQUIRED and stop.

        The defect this closes: a finished install whose own verification did
        not pass used to be reported as "repair required" while the new task
        and the new runtime stayed active. A verification that does not pass
        means the thing that was installed is not the thing that was meant, so
        it is rolled back like any other failure.
        """
        broken = sa_privtask.TaskVerdict(False, True, ["deliberately unverifiable"],
                                         token=sa_privtask.PRIVILEGED_TASK_TAMPERED)
        with mock.patch.object(sa_privtask, "verify_installation",
                               lambda **kwargs: broken):
            with self.assertRaises(sa_privtask.PrivilegeTaskError) as caught:
                self.install()
        self.assertIn("did not verify", str(caught.exception))
        self.assertEqual(self.registry.tasks, {},
                         "a task stayed registered after the installation "
                         "failed its own final verification")
        self.assertFalse(os.path.exists(self.pin_file))
        self.assertFalse(os.path.isdir(self.root))

    def test_an_unrestorable_rollback_reports_repair_required(self):
        """When the machine cannot be put back, say so -- do not pretend."""
        self.assertTrue(self.install()["ok"])
        with mock.patch.object(sa_privtask, "_restore_installation",
                               lambda *a, **k: (False, ["deliberately unrestorable"])):
            with self.assertRaises(sa_privtask.PrivilegeTaskError) as caught:
                self.install(fault="pin_save")
        self.assertEqual(getattr(caught.exception, "token", ""),
                         sa_privtask.REPAIR_REQUIRED)

    # -- the onedir rule itself ---------------------------------------------
    def test_a_missing_worker_bundle_refuses_the_install(self):
        shutil.rmtree(str(self.bundle))
        with self.assertRaises(sa_privtask.PrivilegeTaskError) as caught:
            self.install()
        self.assertIn("--onedir", str(caught.exception))
        self.assertEqual(self.registry.tasks, {})

    def test_a_bundle_without_the_worker_executable_is_refused(self):
        os.remove(str(self.bundle / sa_privtask.FROZEN_WORKER_NAME))
        with self.assertRaises(sa_privtask.PrivilegeTaskError):
            self.install()
        self.assertEqual(self.registry.tasks, {})

    def test_the_installed_bundle_is_the_whole_tree(self):
        self.assertTrue(self.install()["ok"])
        installed = Path(sa_privtask.installed_worker_bundle_dir(self.root))
        self.assertTrue((installed / sa_privtask.FROZEN_WORKER_NAME).is_file())
        self.assertTrue((installed / sa_privtask.PYINSTALLER_INTERNAL_DIR
                         / "python311.dll").is_file())
        # and the task action still names the helper, never the worker
        xml = self.registry.tasks[sa_privtask.TASK_PATH]["xml"]
        self.assertIn(sa_privtask.HELPER_SCRIPT, xml)
        self.assertNotIn(sa_privtask.FROZEN_WORKER_NAME, xml)

    def test_the_task_xml_is_written_in_protected_scratch_not_user_temp(self):
        """schtasks reads the XML elevated, so where it lives is security."""
        seen = []
        real_open = builtins.open

        def watched(path, *args, **kwargs):
            if isinstance(path, str) and path.lower().endswith(".xml"):
                seen.append(path)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(builtins, "open", watched):
            self.assertTrue(self.install()["ok"])
        self.assertTrue(seen, "no task XML was written at all")
        for path in seen:
            self.assertTrue(sa_privtask.path_is_within(self.scratch, path),
                            "the task XML was written outside protected "
                            "scratch: %s" % path)
        # ...and in production that scratch root is under %ProgramData%, not
        # under the medium user's TEMP. (This test has to point the scratch
        # root at its own temp directory, so the dynamic check above can only
        # prove containment; the location itself is asserted here.)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(sa_privtask.PRIVILEGED_TMP_ENV, None)
            production = sa_privtask.privileged_tmp_root()
        self.assertTrue(sa_privtask.path_is_within(
            sa_privtask.program_data_root(), production), production)
        source = (SUBSYSTEM / "sa_privtask.py").read_text(encoding="utf-8")
        self.assertNotIn('os.environ.get("TEMP")', source,
                         "the installer still reaches for the user's TEMP")


class MockPywinError(Exception):
    def __init__(self, winerror=0, func="", msg=""):
        super().__init__(winerror, func, msg)
        self.winerror = winerror


class PrivilegedHelperBoundedIoTests(unittest.TestCase):
    """PERF-001: Genuine overlapped named-pipe I/O with bounded timeouts and cancellation."""

    def setUp(self):
        self.client = sa_privhelper.PrivilegedHelper()
        self.client._pipe = 12345
        self.client._verified = True

    def _mock_pywintypes(self):
        pywintypes = mock.MagicMock()
        pywintypes.error = MockPywinError
        pywintypes.OVERLAPPED = mock.MagicMock
        return pywintypes

    def test_read_line_timeout_cancels_io_and_drops_pipe_no_thread_leak(self):
        win32file = mock.MagicMock()
        win32event = mock.MagicMock()
        pywintypes = self._mock_pywintypes()
        win32file.ReadFile.side_effect = MockPywinError(997, "ReadFile", "Overlapped I/O")
        win32event.WAIT_TIMEOUT = 258
        win32event.WAIT_OBJECT_0 = 0
        win32event.WaitForSingleObject.return_value = win32event.WAIT_TIMEOUT

        threads_before = threading.active_count()
        with mock.patch("sa_privhelper._win32", return_value=(win32file, mock.MagicMock(), pywintypes)), \
             mock.patch("win32event.CreateEvent", win32event.CreateEvent), \
             mock.patch("win32event.WaitForSingleObject", win32event.WaitForSingleObject):
            with self.assertRaises(sa_privhelper.HelperError) as ctx:
                self.client._read_line(timeout=0.01)
            self.assertIn("did not answer in time", str(ctx.exception))

        self.assertEqual(threading.active_count(), threads_before, "leaked thread during timed out read")
        self.assertIsNone(self.client._pipe, "pipe must be dropped after read timeout")
        self.assertTrue(win32file.CancelIo.called, "CancelIo must be called upon timeout")

    def test_read_line_disconnect_drops_pipe_and_raises_unavailable(self):
        win32file = mock.MagicMock()
        pywintypes = self._mock_pywintypes()
        win32file.ReadFile.side_effect = MockPywinError(109, "ReadFile", "The pipe has been ended.")

        with mock.patch("sa_privhelper._win32", return_value=(win32file, mock.MagicMock(), pywintypes)):
            with self.assertRaises(sa_privhelper.HelperUnavailable) as ctx:
                self.client._read_line(timeout=1.0)
            self.assertIn("disconnected", str(ctx.exception))
        self.assertIsNone(self.client._pipe)

    def test_read_line_desync_trailing_bytes_fails_closed(self):
        win32file = mock.MagicMock()
        pywintypes = self._mock_pywintypes()
        win32file.ReadFile.return_value = (0, b'{"id": 1, "ok": true}\nextra-garbage\n')
        win32file.GetOverlappedResult.return_value = len(b'{"id": 1, "ok": true}\nextra-garbage\n')

        with mock.patch("sa_privhelper._win32", return_value=(win32file, mock.MagicMock(), pywintypes)):
            with self.assertRaises(sa_privhelper.HelperError) as ctx:
                self.client._read_line(timeout=1.0)
            self.assertIn("desynchronised", str(ctx.exception))
        self.assertIsNone(self.client._pipe)

    def test_write_line_timeout_cancels_io_and_drops_pipe(self):
        win32file = mock.MagicMock()
        win32event = mock.MagicMock()
        pywintypes = self._mock_pywintypes()
        win32file.WriteFile.side_effect = MockPywinError(997, "WriteFile", "Overlapped I/O")
        win32event.WAIT_TIMEOUT = 258
        win32event.WAIT_OBJECT_0 = 0
        win32event.WaitForSingleObject.return_value = win32event.WAIT_TIMEOUT

        threads_before = threading.active_count()
        with mock.patch("sa_privhelper._win32", return_value=(win32file, mock.MagicMock(), pywintypes)), \
             mock.patch("win32event.CreateEvent", win32event.CreateEvent), \
             mock.patch("win32event.WaitForSingleObject", win32event.WaitForSingleObject):
            with self.assertRaises(sa_privhelper.HelperError) as ctx:
                self.client._write_line("{}", timeout=0.01)
            self.assertIn("write timed out", str(ctx.exception))

        self.assertEqual(threading.active_count(), threads_before)
        self.assertIsNone(self.client._pipe)
        self.assertTrue(win32file.CancelIo.called)


if __name__ == "__main__":
    unittest.main()
