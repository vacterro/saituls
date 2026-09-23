"""Reusable hardware-acceptance engine for SAITULS Secure Apps.

The disposable acceptance run (disposable 1 GiB BitLocker container, never a
configured vault) proves every property the real migration depends on.
This module holds the reusable verification logic, allowing execution from
both the interactive console (tests/secure_apps_interactive.py) and the
Secure Apps GUI (secure_apps_gui.py) without spawning subprocesses or requiring
a hidden console.
"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
if TESTS_DIR not in sys.path:
    sys.path.append(TESTS_DIR)

import sa_acceptance      # noqa: E402
import sa_audit           # noqa: E402
import sa_auth            # noqa: E402
import sa_broker          # noqa: E402
import sa_config          # noqa: E402
import sa_enroll          # noqa: E402
import sa_paths           # noqa: E402
import sa_privtask        # noqa: E402
import sa_state           # noqa: E402
import sa_storage         # noqa: E402

import sa_privboundary_probe as probe   # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
_results = []

EVIDENCE_FIELDS = (
    "timestamp",
    "run_id",
    "profile_id",
    "python",
    "python_fido2_version",
    "provider",
    "transport",
    "windows_build",
    "uac_enabled",
    "broker_elevated",
    "broker_medium_integrity",
    "acceptance_producer_fingerprint",
    "profile_security_fingerprint",
    "windows_webauthn_available",
    "windows_webauthn_api_version",
    "authenticator_detected",
    "hmac_secret_available",
    "user_verification_available",
    "user_verification_policy",
    "enrolled_keys",
    "enrollment",
    "unlock",
    "default_reopen",
    "aggressive_reopen_requires_auth",
    "workstation_lock_relock",
    "vault_detached",
    "audit_log_clean",
    "mode",
    "migration_gate",
    "checks_passed",
    "checks_failed",
    "checks_skipped",
    "checks_missing",
    "fido2_hardware_accepted",
    "storage_accepted",
    "default_mode_accepted",
    "aggressive_mode_accepted",
    "workstation_lock_accepted",
    "helper_failure_accepted",
    "audit_hygiene_accepted",
    "migration_ready",
)

_evidence = {}


def evidence(name, value):
    """Record one allowlisted fact. Silently drops anything else."""
    if name not in EVIDENCE_FIELDS:
        return None
    if isinstance(value, bool) or isinstance(value, int) or value is None:
        _evidence[name] = value
    else:
        _evidence[name] = str(value)[:200]
    return _evidence[name]


def write_evidence(path):
    """Write the record, in EVIDENCE_FIELDS order. Returns the path."""
    record = {name: _evidence[name] for name in EVIDENCE_FIELDS
              if name in _evidence}
    record.setdefault("timestamp",
                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        directory = ntpath.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, indent=2, sort_keys=False)
            handle.write("\n")
    except OSError as exc:
        print("  could not write the evidence record: %s" % type(exc).__name__)
        return None
    print("")
    print("Evidence record (non-secret): %s" % path)
    print(json.dumps(record, indent=2))
    return path


def verdict(name, ok):
    """PASS / FAIL as an evidence value, mirroring report()."""
    return evidence(name, PASS if ok else FAIL)


def fido2_facts():
    """Version and transport, straight from the provider. No hardware needed."""
    version, _major, available, api = sa_acceptance.fido2_facts()
    evidence("python_fido2_version", version)
    evidence("windows_webauthn_available", available)
    evidence("windows_webauthn_api_version", api)
    return available, api


def report(name, ok, detail=""):
    outcome = PASS if ok else FAIL
    _results.append((outcome, name, detail))
    print("  %-4s %s%s" % (outcome, name, ("  " + detail) if detail else ""))
    return ok


def skip(name, detail=""):
    _results.append((SKIP, name, detail))
    print("  %-4s %s  %s" % (SKIP, name, detail))


def summary():
    failed = [r for r in _results if r[0] == FAIL]
    print("")
    print("%d checks, %d failed, %d skipped"
          % (len(_results), len(failed), len([r for r in _results if r[0] == SKIP])))
    return 1 if failed else 0


def load_production_registry(args):
    return sa_config.load_registry(
        getattr(args, "registry", None) or sa_config.default_registry_path(),
        managed_root=getattr(args, "managed_root", None))


def build(args):
    registry = load_production_registry(args)
    os.makedirs(registry.state_dir, exist_ok=True)
    audit = sa_audit.AuditLog(registry.audit_path)
    return registry, sa_broker.SecureBroker(registry, audit=audit,
                                            pin_callback=console_pin)


def _getpass(prompt):
    import getpass
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        return None


def console_pin(rp_id=None):
    try:
        return _getpass("  FIDO2 PIN for %s (not echoed): "
                        % (rp_id or "security key")) or None
    except KeyboardInterrupt:
        return None


def evidence_path(args, registry):
    if getattr(args, "evidence", None):
        return args.evidence
    return ntpath.join(registry.state_dir,
                       "hardware-evidence-%s.json" % time.strftime("%Y%m%d-%H%M%S"))


CHECKS = (
    ("F01", "fido2_hardware_accepted", "transport is the one this host must use"),
    ("F02", "fido2_hardware_accepted", "authenticator detected"),
    ("F03", "fido2_hardware_accepted", "CTAP2 hmac-secret supported"),
    ("F04", "fido2_hardware_accepted", "clientPin / user verification available"),
    ("F05", "fido2_hardware_accepted", "credential creation succeeds"),
    ("F06", "fido2_hardware_accepted", "correct PIN + touch authenticates, nothing mounted"),
    ("F07", "fido2_hardware_accepted", "hmac-secret unwrap reproduces the volume secret"),
    ("F08", "fido2_hardware_accepted", "cancelled PIN fails closed"),
    ("F09", "fido2_hardware_accepted", "wrong PIN fails closed"),
    ("F10", "fido2_hardware_accepted", "no key fails closed"),
    ("F11", "fido2_hardware_accepted", "wrong enrolled credential fails closed"),
    ("P01", "silent_privileged_start_accepted",
     "privileged helper task installed with the expected fixed definition"),
    ("P02", "silent_privileged_start_accepted",
     "Task Scheduler starts the helper with NO Windows consent prompt"),
    ("P03", "silent_privileged_start_accepted",
     "helper arrives at high integrity while this broker stays medium"),
    ("P04", "silent_privileged_start_accepted",
     "a changed task definition is refused as PRIVILEGED_TASK_TAMPERED"),
    ("P05", "privileged_runtime_acl_accepted",
     "Windows REALLY denies this medium process every write to the protected "
     "runtime, the worker bundle, the manifest and the pin"),
    ("P06", "privileged_task_security_accepted",
     "Windows REALLY lets this medium process query and run the task, and "
     "REALLY denies changing, disabling, deleting and re-securing it"),
    ("P07", "privileged_runtime_acl_accepted",
     "the pin is owned by Administrators/SYSTEM and the recursive FIDO worker "
     "bundle fingerprint matches the one pinned at installation"),
    ("S01", "storage_accepted", "BitLocker container with recovery protector, left detached"),
    ("S02", "storage_accepted", "encrypted volume write and read"),
    ("S03", "storage_accepted", "volume detached when the application exits"),
    ("S04", "storage_accepted", "broker shutdown closes the application and detaches storage"),
    ("D01", "default_mode_accepted", "Default reopen uses the cached volume secret, no FIDO interaction"),
    ("A01", "aggressive_mode_accepted", "Aggressive open requires authentication"),
    ("A02", "aggressive_mode_accepted", "Aggressive reopen requires authentication again"),
    ("W01", "workstation_lock_accepted", "real workstation lock notification received"),
    ("W02", "workstation_lock_accepted", "workstation lock destroys the session and detaches storage"),
    ("H01", "helper_failure_accepted", "elevated helper terminated while the vault is mounted"),
    ("H02", "helper_failure_accepted", "a dead helper never reads as LOCKED"),
    ("H03", "helper_failure_accepted", "recovery after the helper's death really relocks"),
    ("X01", "audit_hygiene_accepted", "audit records are allowlisted"),
    ("X02", "audit_hygiene_accepted", "audit output carries no secret"),
)
CHECK_FLAGS = {check_id: flag for check_id, flag, _text in CHECKS}
CHECK_TEXT = {check_id: text for check_id, _flag, text in CHECKS}

DISPOSABLE_PROFILE = "disposable-acceptance"
WRONG_PIN = "saituls-acceptance-deliberately-wrong-pin"
LOCK_WAIT_SECONDS = 300
MIN_PIN_SUBSTRING = 6

APP_SCRIPT = ("import os, sys, time\n"
              "stop = sys.argv[1]\n"
              "while not os.path.exists(stop):\n"
              "    time.sleep(0.2)\n")


class AbortAcceptance(Exception):
    """The sequence cannot meaningfully continue. Never carries a secret."""


class AcceptanceRun(object):
    """PASS / FAIL / SKIP per check, and the gate flags they add up to."""

    def __init__(self, echo=print, check_callback=None, outcome_callback=None):
        self.results = []
        self.aborted = None
        self.echo = echo
        self.check_callback = check_callback
        #: Optional (check_id, outcome, detail) hook. ``check_callback`` keeps
        #: its historical two-value contract (a boolean ``ok``), which cannot
        #: tell SKIP from FAIL; a surface that shows per-check state needs the
        #: real outcome, and it comes from here rather than from a guess.
        self.outcome_callback = outcome_callback

    def _record(self, outcome, check_id, detail):
        if check_id not in CHECK_FLAGS:
            raise KeyError("unknown acceptance check %r" % (check_id,))
        self.results.append((outcome, check_id, detail))
        if self.echo is not None:
            self.echo("  %-4s %s %s%s" % (outcome, check_id, CHECK_TEXT[check_id],
                                          ("  -- " + detail) if detail else ""))
        if self.check_callback is not None:
            try:
                self.check_callback(check_id, outcome == PASS, detail)
            except Exception:
                pass
        if self.outcome_callback is not None:
            try:
                self.outcome_callback(check_id, outcome, detail)
            except Exception:
                pass

    def check(self, check_id, ok, detail=""):
        self._record(PASS if ok else FAIL, check_id, detail)
        return bool(ok)

    def skip(self, check_id, detail=""):
        self._record(SKIP, check_id, detail)
        return False

    def abort(self, reason):
        self.aborted = str(reason)

    def outcomes(self, check_id):
        return [outcome for outcome, cid, _detail in self.results if cid == check_id]

    def passed(self, check_id):
        outcomes = self.outcomes(check_id)
        return bool(outcomes) and all(outcome == PASS for outcome in outcomes)

    def missing(self):
        return [check_id for check_id, _flag, _text in CHECKS
                if not self.outcomes(check_id)]

    def not_passed(self):
        return [check_id for check_id, _flag, _text in CHECKS
                if not self.passed(check_id)]

    def gates(self):
        gates = {}
        for flag in sa_acceptance.GATE_FLAGS:
            mapped = [check_id for check_id, mapped_flag, _t in CHECKS
                      if mapped_flag == flag]
            gates[flag] = bool(mapped) and all(self.passed(check_id)
                                               for check_id in mapped)
        return gates

    def complete(self):
        return self.aborted is None and not self.not_passed()

    def counts(self):
        return {outcome: len([r for r in self.results if r[0] == outcome])
                for outcome in (PASS, FAIL, SKIP)}


class PinPrompt(object):
    """The console PIN prompt, keeping only salted digests of what was typed."""

    def __init__(self, reader=None):
        self._salt = os.urandom(32)
        self._digests = set()
        self._lengths = set()
        self.prompts = 0
        self._reader = reader or _getpass

    def __call__(self, rp_id=None):
        self.prompts += 1
        prompt = "  FIDO2 PIN for %s (not echoed): " % (rp_id or "security key")
        value = self._reader(prompt)
        if value:
            self.remember(value)
        return value or None

    def remember(self, value):
        self._digests.add(self._digest(value))
        self._lengths.add(len(value))

    def _digest(self, text):
        return hashlib.sha256(self._salt + str(text).encode("utf-8")).digest()

    def found_in(self, value, substrings=True):
        text = str(value)
        if self._digest(text) in self._digests:
            return True
        if not substrings:
            return False
        for length in self._lengths:
            if length < MIN_PIN_SUBSTRING or length >= len(text):
                continue
            for start in range(len(text) - length + 1):
                if self._digest(text[start:start + length]) in self._digests:
                    return True
        return False


RECOVERY_SHAPE = re.compile(r"\d{6}(?:-\d{6}){7}")
BLOB_SHAPE = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
CREDENTIAL_HASH_SHAPE = re.compile(r"^[0-9a-f]{32}$")
NOT_BLOB_SCANNED = ("ts", "credential_id_hash", "mount_path")
NOT_PIN_SUBSTRING_SCANNED = ("ts", "credential_id_hash", "mount_path")


def scan_audit(path, pin_prompt=None, recovery_values=(), known_mount_paths=()):
    known = set()
    for mount in known_mount_paths:
        known.add(sa_paths.canonical(str(mount)).lower())
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        return 0, ["the audit log is unreadable (%s)" % type(exc).__name__], []
    records, allow, secret = 0, [], []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        records += 1
        try:
            record = json.loads(line)
        except ValueError:
            allow.append("line %d is not JSON" % number)
            continue
        if not isinstance(record, dict):
            allow.append("line %d is not a JSON object" % number)
            continue
        extra = sorted(set(record) - set(sa_audit.ALLOWED_FIELDS))
        if extra:
            allow.append("line %d carries non-allowlisted field(s) %s" % (number, extra))
        if record.get("result") not in sa_audit.RESULTS:
            allow.append("line %d has an unknown result" % number)
        if record.get("error_category") not in sa_audit.ERROR_CATEGORIES:
            allow.append("line %d has an unknown error_category" % number)
        chash = record.get("credential_id_hash")
        if chash is not None and not CREDENTIAL_HASH_SHAPE.match(str(chash)):
            secret.append("line %d: credential_id_hash is not a 32-hex hash" % number)
        mount = record.get("mount_path")
        if mount is not None and known:
            try:
                expected = sa_paths.canonical(str(mount)).lower() in known
            except sa_paths.PathPolicyError:
                expected = False
            if not expected:
                allow.append("line %d names a mount_path outside this run" % number)
        for key, value in record.items():
            text = str(value)
            if any(r and r in text for r in recovery_values):
                secret.append("line %d: %s carries the BitLocker recovery password"
                              % (number, key))
            elif RECOVERY_SHAPE.search(text):
                secret.append("line %d: %s carries a recovery-password-shaped value"
                              % (number, key))
            if (pin_prompt is not None and not isinstance(value, bool)
                    and pin_prompt.found_in(
                        value, substrings=(isinstance(value, str)
                                           and key not in NOT_PIN_SUBSTRING_SCANNED))):
                secret.append("line %d: %s carries an entered PIN" % (number, key))
            if key not in NOT_BLOB_SCANNED and BLOB_SHAPE.search(text):
                secret.append("line %d: %s carries a key-material-shaped value"
                              % (number, key))
    return records, allow, secret


def disposable_registry_document(template, managed_root, mount_path, stop_file,
                                 python=None):
    policy = template.policy.to_dict()
    policy["mode"] = "default"
    policy["graceful_close_timeout_seconds"] = 2
    policy["force_terminate_after_timeout"] = True
    root = Path(managed_root)
    return {
        "schema": sa_config.SCHEMA_ID,
        "schema_version": sa_config.SCHEMA_VERSION,
        "managed_root": str(root),
        "notes": "Disposable hardware acceptance. Deleted when the run ends.",
        "profiles": [{
            "id": DISPOSABLE_PROFILE,
            "label": "Disposable hardware acceptance",
            "enabled": True,
            "application": {
                "executable": python or sys.executable,
                "working_directory": str(root),
                "arguments": ["-c", APP_SCRIPT, str(stop_file)],
                "vault_argument_style": "none",
                "allow_unmanaged_executable": True,
            },
            "storage": {
                "backend": template.backend,
                "container": str(root / "vaults" / "disposable" / "disposable.vhdx"),
                "container_id": "disposable-acceptance",
                "mount_path": str(mount_path),
                "size_gb": 1,
                "filesystem_label": "SAITULS-ACCEPT",
            },
            "authentication": {
                "provider": template.provider,
                "credential_profile": "acceptance",
                "user_verification": template.user_verification,
            },
            "policy": policy,
        }],
    }


def process_alive(pid):
    if not pid:
        return False
    try:
        import psutil
        proc = psutil.Process(int(pid))
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def process_name(pid):
    try:
        import psutil
        return psutil.Process(int(pid)).name().lower()
    except Exception:
        return None


def terminate_elevated_process(pid, timeout=60.0):
    if os.name != "nt":
        return False, "Windows only"
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    taskkill = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                            "System32", "taskkill.exe")
    code = shell32.ShellExecuteW(None, "runas", taskkill, "/PID %d /F" % int(pid),
                                 None, 0) or 0
    if code <= 32:
        return False, ("the elevated taskkill did not start (ShellExecute %d); was "
                       "elevation declined?" % code)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_alive(pid):
            return True, "helper pid %d terminated" % pid
        time.sleep(0.25)
    return False, "helper pid %d is still alive after %ds" % (pid, timeout)


class WorkstationLockListener(object):
    WM_WTSSESSION_CHANGE = 0x02B1
    WTS_SESSION_LOCK = 0x7
    WTS_SESSION_UNLOCK = 0x8
    NOTIFY_FOR_THIS_SESSION = 0
    WM_QUIT = 0x0012

    def __init__(self):
        self.locked = threading.Event()
        self.unlocked = threading.Event()
        self.ready = threading.Event()
        self.failed = None
        self._thread = None
        self._thread_id = None

    def start(self):
        if os.name != "nt":
            self.failed = "Windows only"
            return False
        self._thread = threading.Thread(target=self._run, name="LockListener",
                                        daemon=True)
        self._thread.start()
        self.ready.wait(5.0)
        return not bool(self.failed)

    def _fail(self, what):
        self.failed = "%s failed (winerror %d)" % (what, ctypes.get_last_error())
        self.ready.set()

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        lresult = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = lresult
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
            wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                       wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
        wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
        wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]

        def wndproc(hwnd, message, wparam, lparam):
            if message == self.WM_WTSSESSION_CHANGE:
                if wparam == self.WTS_SESSION_LOCK:
                    self.locked.set()
                elif wparam == self.WTS_SESSION_UNLOCK:
                    self.unlocked.set()
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        class_name = "SAITULS_LockListener_%d" % int(time.time() * 1000)
        instance = kernel32.GetModuleHandleW(None)
        wndclass = WNDCLASSW()
        wndclass.lpfnWndProc = WNDPROC(wndproc)
        wndclass.hInstance = instance
        wndclass.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(wndclass))
        if not atom:
            return self._fail("RegisterClassW")
        hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0,
                                      0, 0, instance, None)
        if not hwnd:
            user32.UnregisterClassW(class_name, instance)
            return self._fail("CreateWindowExW")
        if not wtsapi32.WTSRegisterSessionNotification(hwnd, self.NOTIFY_FOR_THIS_SESSION):
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(class_name, instance)
            return self._fail("WTSRegisterSessionNotification")
        self._thread_id = kernel32.GetCurrentThreadId()
        self.ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        wtsapi32.WTSUnRegisterSessionNotification(hwnd)
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, instance)

    def stop(self):
        if self._thread_id and os.name == "nt":
            try:
                ctypes.WinDLL("user32").PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(5.0)


def _confirm(prompt, reader=input):
    try:
        return reader("  %s  Type YES to continue: " % prompt).strip().upper() == "YES"
    except (EOFError, KeyboardInterrupt):
        return False


def _pause(prompt, reader=input):
    try:
        reader("  %s  Press Enter when done..." % prompt)
        return True
    except (EOFError, KeyboardInterrupt):
        return False


class DisposableAcceptance(object):
    """The migration gate, proven against a throwaway container."""

    def __init__(self, args, reader=input, echo=print,
                 confirm_callback=None, pause_callback=None,
                 pin_callback=None, check_callback=None,
                 outcome_callback=None):
        self.args = args
        self.reader = reader
        self.say = echo
        self._confirm_cb = confirm_callback
        self._pause_cb = pause_callback
        self._pin_cb = pin_callback
        self._check_cb = check_callback
        self._outcome_cb = outcome_callback

        self.run = AcceptanceRun(echo=echo, check_callback=check_callback,
                                 outcome_callback=outcome_callback)
        self.pin = PinPrompt(reader=self._pin_reader)
        self.recovery_values = []
        self.brokers = []
        self.closed = set()
        self.broker = None
        self.registry = None
        self.profile = None
        self.audit = None
        self.canary = None
        self.run_id = time.strftime("%Y%m%d-%H%M%S")

    def _pin_reader(self, prompt):
        if self._pin_cb is not None:
            return self._pin_cb(prompt)
        return _getpass(prompt)

    def confirm(self, prompt):
        if self._confirm_cb is not None:
            return self._confirm_cb(prompt)
        return _confirm(prompt, self.reader)

    def pause(self, prompt):
        if self._pause_cb is not None:
            return self._pause_cb(prompt)
        return _pause(prompt, self.reader)

    # -- plumbing ---------------------------------------------------------
    def new_broker(self, label):
        broker = sa_broker.SecureBroker(self.registry, audit=self.audit,
                                        pin_callback=self.pin)
        self.brokers.append((label, broker))
        self.broker = broker
        return broker

    def shutdown_broker(self, broker):
        self.closed.add(id(broker))
        return broker.shutdown()

    def storage_state(self, broker=None):
        try:
            return (broker or self.broker).backend(self.profile).state(self.profile)
        except Exception as exc:
            return "unknown (%s)" % type(exc).__name__

    def mount_empty(self):
        return sa_paths.directory_is_empty(self.profile.mount_path)

    def canary_readable(self):
        try:
            with open(os.path.join(self.profile.mount_path, "acceptance-canary.txt"),
                      "r", encoding="utf-8") as handle:
                return self.canary is not None and handle.read() == self.canary
        except OSError:
            return False

    def app_arm(self):
        try:
            os.remove(self.stop_file)
        except FileNotFoundError:
            pass

    def app_stop(self):
        with open(self.stop_file, "w", encoding="utf-8") as handle:
            handle.write("stop\n")

    def wait_app_exit(self, broker=None, timeout=30.0):
        broker = broker or self.broker
        deadline = time.time() + timeout
        while time.time() < deadline and broker.status(DISPOSABLE_PROFILE)["app_running"]:
            time.sleep(0.25)
        return [event for event in broker.tick() if event is not None]

    def detached(self, broker=None):
        state = self.storage_state(broker)
        return (state == sa_storage.DETACHED and self.mount_empty()
                and not self.canary_readable()), state

    def status(self, broker=None):
        return (broker or self.broker).status(DISPOSABLE_PROFILE)

    # -- preflight ----------------------------------------------------------
    def prepare(self):
        say = self.say
        if os.name != "nt":
            say("  the disposable acceptance needs Windows")
            return False
        try:
            self.production = load_production_registry(self.args)
            self.template = self.production.get(getattr(self.args, "profile", "obsidian"))
        except (sa_config.ConfigError, sa_paths.PathPolicyError) as exc:
            say("  cannot load the production registry: %s" % exc)
            return False
        if self.template.provider != sa_auth.Fido2HmacSecretProvider.name:
            say("  profile %r uses provider %r; this acceptance exercises %r only"
                % (self.template.id, self.template.provider,
                   sa_auth.Fido2HmacSecretProvider.name))
            return False
        if self.template.backend != sa_storage.BitLockerVhdxBackend.name:
            say("  profile %r uses backend %r; this acceptance exercises %r only"
                % (self.template.id, self.template.backend,
                   sa_storage.BitLockerVhdxBackend.name))
            return False
        self.environment = sa_acceptance.probe_environment()
        if self.environment.get("broker_elevated") is not False:
            say("  FAIL: %s" % sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY)
            say("  this process is elevated (or its integrity level cannot be "
                "read). Secure Apps keeps the broker, the GUI and the protected "
                "application at medium integrity and elevates only the storage "
                "and FIDO helper, so an elevated run proves nothing about the "
                "architecture it would authorise.")
            if self.environment.get("uac_enabled") is False:
                say("  UAC is disabled on this host (EnableLUA=0), so every "
                    "process of an administrator is high integrity and there is "
                    "no ordinary shell to start this from. Either re-enable UAC "
                    "(EnableLUA=1, then reboot) or run as a standard user; "
                    "without one of those, this host cannot provide the boundary "
                    "Secure Apps protects the vault with.")
            else:
                say("  start it from an ordinary shell; Windows prompts for the "
                    "helper's elevation when the run needs it.")
            say("  nothing was created: no enrollment, no disposable container, "
                "no record.")
            return False
        for key in ("host_fingerprint", "implementation_fingerprint",
                    "acceptance_producer_fingerprint", "python_fido2_major",
                    "broker_elevated"):
            if self.environment.get(key) is None:
                say("  cannot measure %s, so no record could ever match this host"
                    % key)
                return False
        if sa_acceptance.profile_security_fingerprint(self.template) is None:
            say("  cannot measure the security-relevant configuration of profile "
                "%r, so no record could ever match it" % self.template.id)
            return False
        self.root = Path(self.production.managed_root) / "acceptance" / self.run_id
        parent = (Path(self.args.mount_parent) if getattr(self.args, "mount_parent", None)
                  else Path(self.template.mount_path).parent)
        self.mount = parent / ("_saituls_acceptance_" + self.run_id)
        self.stop_file = self.root / "application.stop"
        for path in (self.root, self.mount):
            if path.exists():
                say("  refusing to reuse an existing path: %s" % path)
                return False
        return True

    def plan(self):
        say = self.say
        env = self.environment
        say("== DISPOSABLE HARDWARE ACCEPTANCE (migration gate) ==")
        say("  template profile     %s  (%s, user verification %s, %s)"
            % (self.template.id, self.template.provider,
               self.template.user_verification, self.template.backend))
        say("  expected transport   %s  (Windows WebAuthn API version %s)"
            % (env["transport"], env["webauthn_api_version"]))
        say("  python-fido2         %s" % env["python_fido2_version"])
        say("  disposable container %s  (1 GiB, deleted at the end)"
            % (self.root / "vaults" / "disposable" / "disposable.vhdx"))
        say("  disposable mount     %s  (removed at the end)" % self.mount)
        say("  real vault           %s  -- never touched" % self.template.mount_path)
        say("  broker integrity     medium (not elevated; required, and checked "
            "before anything was created)")
        say("  acceptance producer  %s"
            % (str(env["acceptance_producer_fingerprint"])[:16] + "..."))
        say("  profile policy       %s"
            % (str(sa_acceptance.profile_security_fingerprint(self.template))[:16]
               + "...  -- the record binds it; any later security-policy edit "
                 "invalidates this run"))
        if env.get("uac_enabled") is False:
            say("  NOTE UAC is disabled on this host (EnableLUA=0): no elevation "
                "prompt will appear and the storage helper inherits this process's "
                "integrity level. This run is medium integrity, so it may proceed, "
                "but the migration must be started the same way.")
        say("")
        say("  privileged start     %s  (%s)"
            % (env.get("privileged_launch_mode"),
               (str(env.get("privileged_task_definition_fingerprint"))[:16] + "...")
               if env.get("privileged_task_definition_fingerprint")
               else "NOT INSTALLED -- run Secure Apps setup first"))
        say("")
        say("  You will be asked to:")
        say("    - enter the FIDO2 PIN and touch the key about 10 times")
        say("    - unplug the key once, and plug it back in")
        say("    - lock the workstation once (Win+L) and unlock it again")
        say("    - confirm that NO Windows consent (UAC) prompt appeared when the")
        say("      privileged helper was started")
        say("  One step deliberately sends a WRONG PIN once. That costs one PIN retry;")
        say("  the correct PIN right after it resets the counter.")
        say("")

    def build_disposable(self):
        self.root.mkdir(parents=True)
        document = disposable_registry_document(self.template, self.root,
                                                self.mount, self.stop_file)
        self.registry = sa_config.parse_registry(document, managed_root=str(self.root))
        self.profile = self.registry.get(DISPOSABLE_PROFILE)
        os.makedirs(self.registry.state_dir, exist_ok=True)
        self.audit = sa_audit.AuditLog(self.registry.audit_path)
        self.known_mounts = (self.profile.mount_path,
                             sa_paths.enrollment_mount_path(self.profile.container),
                             sa_paths.staging_mount_path(self.profile.container))
        evidence("run_id", self.run_id)
        evidence("profile_id", self.template.id)
        evidence("provider", self.template.provider)
        evidence("user_verification_policy", self.template.user_verification)
        evidence("windows_build", self.environment.get("windows_build"))
        evidence("uac_enabled", self.environment.get("uac_enabled"))
        evidence("broker_elevated", self.environment.get("broker_elevated"))
        evidence("broker_medium_integrity",
                 self.environment.get("broker_elevated") is False)
        evidence("acceptance_producer_fingerprint",
                 self.environment.get("acceptance_producer_fingerprint"))
        evidence("profile_security_fingerprint",
                 sa_acceptance.profile_security_fingerprint(self.template))

    # -- phases -------------------------------------------------------------
    def phase_privileged_start(self):
        run, say = self.run, self.say
        verdict = sa_privtask.verify_installation()
        run.check("P01", verdict.ok,
                  "task %s, definition %s%s"
                  % (verdict.to_dict()["task_path"],
                     (verdict.fingerprint or "unreadable")[:16],
                     ("; " + "; ".join(verdict.reasons)) if verdict.reasons else ""))
        if not verdict.ok:
            raise AbortAcceptance(
                "the privileged helper task is not installed or does not match "
                "this installation; run Secure Apps setup -> Install privileged "
                "helper first (%s)" % (verdict.token or "PRIVILEGED_TASK_TAMPERED"))

        say("\n>> Starting the privileged helper through Task Scheduler.")
        say("   Watch the screen: NO Windows consent (UAC) dialog may appear.")
        report = sa_privtask.commission()
        run.check("P03", bool(report.get("helper_high_integrity")
                              and report.get("broker_medium_integrity")),
                  "helper pid %s high=%s, broker medium=%s"
                  % (report.get("helper_pid"),
                     report.get("helper_high_integrity"),
                     report.get("broker_medium_integrity")))
        silent = bool(report.get("silent_start"))
        if not silent:
            run.check("P02", False, "; ".join(report.get("reasons") or
                                              ["the helper did not start"]))
            raise AbortAcceptance("the privileged helper could not be started "
                                  "through Task Scheduler")
        no_prompt = self.confirm("Did the helper start WITHOUT any Windows consent "
                                 "(UAC) prompt?")
        run.check("P02", no_prompt,
                  "operator reports %s consent prompt"
                  % ("no" if no_prompt else "a"))

        installed = verdict.definition or {}
        tampered = dict(installed)
        tampered["run_level"] = "LeastPrivilege"
        weakened = dict(installed)
        weakened["arguments"] = list(installed.get("arguments") or ()) + ["-Extra"]
        refused_level = sa_privtask.compare_definitions(installed, tampered)
        refused_args = sa_privtask.compare_definitions(installed, weakened)
        changed_digest = (sa_privtask.definition_fingerprint(tampered)
                          != sa_privtask.definition_fingerprint(installed))
        run.check("P04", bool(refused_level and refused_args and changed_digest),
                  "run level: %d reason(s), arguments: %d reason(s), fingerprint "
                  "changes: %s" % (len(refused_level), len(refused_args),
                                   changed_digest))

    def phase_privilege_boundary(self):
        run, say = self.run, self.say
        verdict = sa_privtask.verify_installation()
        pin = verdict.pin or {}
        root = pin.get("runtime_root") or sa_privtask.privileged_root()
        helper = pin.get("helper_path") or sa_privtask.installed_helper_path(root)
        bundle = (pin.get("fido_worker_bundle_dir")
                  or sa_privtask.installed_worker_bundle_dir(root))
        worker = pin.get("fido_worker_exe_path") or sa_privtask.installed_worker_exe_path(root)
        manifest = sa_privtask.runtime_manifest_path(root)
        pin_file = sa_privtask.pin_path()
        task_path = pin.get("task_path") or sa_privtask.TASK_PATH

        say("\n>> Attempting every forbidden operation from THIS medium broker.")
        say("   Descriptors are not evidence; refusals from Windows are.")

        spec = {"probes": [
            {"id": "helper_overwrite", "op": "file_open_write", "target": helper},
            {"id": "helper_delete", "op": "file_delete", "target": helper},
            {"id": "helper_rename", "op": "file_rename", "target": helper},
            {"id": "worker_write", "op": "file_open_write", "target": worker},
            {"id": "worker_delete", "op": "file_delete", "target": worker},
            {"id": "bundle_add_file", "op": "dir_create_child", "target": bundle},
            {"id": "manifest_write", "op": "file_open_write", "target": manifest},
            {"id": "manifest_delete", "op": "file_delete", "target": manifest},
            {"id": "pin_write", "op": "file_open_write", "target": pin_file},
            {"id": "pin_delete", "op": "file_delete", "target": pin_file},
        ]}
        report = probe.run_medium(spec)
        results = report.get("results") or {}
        if report.get("elevated") is not False:
            run.check("P05", False, "the probe did not run at medium integrity: %s"
                      % (report.get("error") or report.get("launched")))
        else:
            denied = [name for name, item in results.items()
                      if item.get("outcome") == probe.DENIED]
            wrong = ["%s=%s" % (name, item.get("outcome"))
                     for name, item in sorted(results.items())
                     if item.get("outcome") != probe.DENIED]
            unrestored = [name for name, item in results.items()
                          if item.get("mutated") and not item.get("restored")]
            evidence("privilege_probe_integrity_sid", report.get("integrity_sid"))
            evidence("runtime_medium_denied", len(denied))
            evidence("runtime_medium_not_denied", len(wrong))
            run.check("P05", not wrong and len(denied) == len(spec["probes"]),
                      "%d/%d denied by Windows%s%s"
                      % (len(denied), len(spec["probes"]),
                         ("; NOT denied: " + ", ".join(wrong)) if wrong else "",
                         ("; UNRESTORED: " + ", ".join(unrestored))
                         if unrestored else ""))

        spec = {"probes": [
            {"id": "task_query", "op": "task_query", "target": task_path},
            {"id": "task_run", "op": "task_run", "target": task_path},
            {"id": "task_change", "op": "task_change", "target": task_path},
            {"id": "task_disable", "op": "task_disable", "target": task_path},
            {"id": "task_sd_change", "op": "task_sd_change", "target": task_path},
            {"id": "task_delete", "op": "task_delete", "target": task_path},
        ]}
        report = probe.run_medium(spec)
        results = report.get("results") or {}
        must_allow = ("task_query", "task_run")
        must_deny = ("task_change", "task_disable", "task_sd_change", "task_delete")
        problems = []
        for name in must_allow:
            outcome = (results.get(name) or {}).get("outcome")
            if outcome != probe.ALLOWED:
                problems.append("%s=%s (the medium broker must be able to)" % (name, outcome))
        for name in must_deny:
            outcome = (results.get(name) or {}).get("outcome")
            if outcome != probe.DENIED:
                problems.append("%s=%s (must be denied)" % (name, outcome))
        unrestored = [name for name, item in results.items()
                      if item.get("mutated") and not item.get("restored")]
        evidence("task_medium_denied",
                 len([name for name in must_deny
                      if (results.get(name) or {}).get("outcome") == probe.DENIED]))
        run.check("P06", not problems and report.get("elevated") is False,
                   "query/run allowed, change/disable/delete/SD denied"
                   if not problems else "; ".join(problems)
                   + (("; UNRESTORED: " + ", ".join(unrestored)) if unrestored else ""))
        if unrestored:
            say("  !! a task mutation could not be undone; run Secure Apps setup ->")
            say("     Repair privileged helper before using the vault again")

        pin_acl = sa_privtask.pin_acl_report()
        measured, count, problem = sa_privtask.bundle_fingerprint(bundle)
        pinned = pin.get("fido_worker_bundle_fingerprint")
        tmp_report = sa_privtask.privileged_tmp_report()
        evidence("pin_owner", pin_acl.get("pin_owner"))
        evidence("pin_owner_valid", pin_acl.get("pin_owner_valid"))
        evidence("fido_worker_bundle_files", count)
        evidence("fido_worker_bundle_fingerprint", measured)
        evidence("privileged_tmp_valid", tmp_report.get("privileged_tmp_valid"))
        ok = bool(pin_acl.get("pin_acl_valid")
                  and measured and pinned and measured == pinned
                  and tmp_report.get("privileged_tmp_valid"))
        run.check("P07", ok,
                  "pin owner %s (valid=%s, medium write=%s, medium DACL=%s); "
                  "bundle %s file(s) %s; scratch protected=%s"
                  % (pin_acl.get("pin_owner"), pin_acl.get("pin_owner_valid"),
                     pin_acl.get("pin_medium_writable"),
                     pin_acl.get("pin_medium_can_change_dacl"),
                     count, "matches the pin" if measured and measured == pinned
                     else "DOES NOT match the pin (%s)" % (problem or "changed"),
                     tmp_report.get("privileged_tmp_valid")))

    def phase_capabilities(self):
        run = self.run
        broker = self.new_broker("A")
        caps = sa_enroll.capabilities(broker, DISPOSABLE_PROFILE)
        expected = self.environment["transport"]
        count = int(caps.get("authenticators") or 0)
        evidence("transport", caps.get("transport"))
        evidence("authenticator_detected", count >= 1)
        evidence("hmac_secret_available", bool(caps.get("hmac_secret")))
        evidence("user_verification_available", bool(caps.get("user_verification")))
        ok = run.check("F01", caps.get("transport") == expected,
                       "%s (expected %s)" % (caps.get("transport"), expected))
        ok = run.check("F02", count >= 1, "%d authenticator(s) - %s"
                       % (count, caps.get("detail", ""))) and ok
        ok = run.check("F03", bool(caps.get("hmac_secret"))) and ok
        ok = run.check("F04", bool(caps.get("user_verification"))) and ok
        if not ok:
            raise AbortAcceptance("the host or the authenticator does not meet the "
                                  "requirements; nothing was created")

    def phase_enroll(self):
        run, broker = self.run, self.broker
        self.say("\n>> ENROLLMENT: PIN + touch, twice (create the credential, then "
                 "wrap the volume key)")

        def acknowledge(recovery, _profile):
            if recovery:
                self.recovery_values.append(recovery)
            return True

        result = sa_enroll.create_and_enroll(broker, DISPOSABLE_PROFILE, acknowledge,
                                             size_gb=1)
        enrolled = broker.credentials.enrollments(DISPOSABLE_PROFILE)
        count = len(enrolled)
        evidence("enrolled_keys", count)
        evidence("enrollment", result.ok and count == 1)
        ok = run.check("F05", result.ok and count == 1,
                       "credential profile %s, ID %s..."
                       % (getattr(result, "credential_profile", ""),
                          getattr(result, "credential_id_hash", "")[:12]))
        if not ok:
            raise AbortAcceptance("enrollment failed: %s" % result.reason)

        gone, state = self.detached()
        run.check("S01", gone, "container %s, storage %s"
                  % (self.profile.container, state))

    def phase_unlock(self):
        run, broker = self.run, self.broker
        self.say("\n>> FIRST UNLOCK: PIN + touch (prove correct credentials work)")
        self.app_arm()
        auth_only = broker.authenticate_only(DISPOSABLE_PROFILE)
        run.check("F06", auth_only.ok and self.mount_empty(),
                  "authenticated=%s, mount empty=%s, storage %s"
                  % (auth_only.ok, self.mount_empty(), self.storage_state()))
        if not auth_only.ok:
            raise AbortAcceptance("authenticate_only failed: %s" % auth_only.reason)

        provider = sa_migrate.secret_provider_from_session(broker, DISPOSABLE_PROFILE)
        secret = provider()
        run.check("F07", bool(secret and len(secret) == 32),
                  "32-byte secret recovered from live session")
        secret.zeroize()

        opened = broker.open(DISPOSABLE_PROFILE)
        evidence("unlock", opened.ok)
        if not opened.ok:
            raise AbortAcceptance("open failed: %s" % opened.reason)

        self.canary = "acceptance-canary-%s" % self.run_id
        canary_path = os.path.join(self.profile.mount_path, "acceptance-canary.txt")
        with open(canary_path, "w", encoding="utf-8") as handle:
            handle.write(self.canary)
        canary_ok = self.canary_readable()
        evidence("storage_accepted", canary_ok)
        run.check("S02", canary_ok, "canary written and read back from %s"
                  % canary_path)

        self.app_stop()
        self.wait_app_exit()
        broker.reconcile(DISPOSABLE_PROFILE)
        status = self.status()
        gone, state = self.detached()
        evidence("vault_detached", gone)
        run.check("S03", (status["state"] == sa_state.SESSION_CACHED
                          and status["session_active"] and not status["app_running"]
                          and gone),
                  "state=%s session=%s app=%s storage=%s"
                  % (status["state"], status["session_active"],
                     status["app_running"], state))

    def phase_default_mode(self):
        run, broker = self.run, self.broker
        self.say("\n>> DEFAULT REOPEN: no key interaction needed (cached session)")
        prompts_before = self.pin.prompts
        self.app_arm()
        reopened = broker.open(DISPOSABLE_PROFILE)
        prompts_after = self.pin.prompts
        no_pin = prompts_after == prompts_before
        canary_ok = reopened.ok and self.canary_readable()
        evidence("default_reopen", canary_ok and no_pin)
        run.check("D01", canary_ok and no_pin,
                  "reopened=%s, canary=%s, PIN prompts during reopen=%d"
                  % (reopened.ok, canary_ok, prompts_after - prompts_before))

        self.app_stop()
        self.wait_app_exit()
        broker.reconcile(DISPOSABLE_PROFILE)

    def phase_hostile(self):
        run, broker = self.run, self.broker
        self.say("\n>> NEGATIVE TESTS: prove cancellations and wrong credentials fail closed")

        def cancel_pin(_rp_id=None):
            return None

        old_pin = self.pin
        self.pin = cancel_pin
        broker.pin_callback = cancel_pin
        try:
            self.app_arm()
            res = broker.open(DISPOSABLE_PROFILE, force_auth=True)
            gone, state = self.detached()
            run.check("F08", not res.ok and gone,
                      "open refused=%s (%s), storage %s"
                      % (not res.ok, res.error_category, state))
        finally:
            self.pin = old_pin
            broker.pin_callback = old_pin

        proceed = self.confirm("WRONG PIN step: SAITULS sends a deliberately wrong PIN "
                                "once to verify that wrong credentials fail closed. This "
                                "uses ONE retry on your security key; the correct PIN right "
                                "after it resets the counter. Proceed?")
        if not proceed:
            run.skip("F09", "cancelled by the operator")
        else:
            def wrong_pin(_rp_id=None):
                return WRONG_PIN

            self.pin = wrong_pin
            broker.pin_callback = wrong_pin
            try:
                self.app_arm()
                res = broker.open(DISPOSABLE_PROFILE, force_auth=True)
                gone, state = self.detached()
                run.check("F09", not res.ok and gone,
                          "open refused=%s (%s), storage %s"
                          % (not res.ok, res.error_category, state))
            finally:
                self.pin = old_pin
                broker.pin_callback = old_pin

        self.pause("NO KEY step: UNPLUG the security key now.")
        try:
            self.app_arm()
            res = broker.open(DISPOSABLE_PROFILE, force_auth=True)
            gone, state = self.detached()
            run.check("F10", not res.ok and gone,
                      "open refused=%s (%s), storage %s"
                      % (not res.ok, res.error_category, state))
        finally:
            self.pause("Plug the security key back in.")

        store = broker.credentials
        real_enrollments = store.enrollments(DISPOSABLE_PROFILE)
        fake_id = b"fake-credential-id-never-enrolled"
        fake_enrollment = sa_auth.Enrollment(
            credential_profile="fake",
            provider=self.template.provider,
            credential_id=fake_id,
            rp_id=sa_auth.RP_ID,
            hmac_salt=b"\x00" * 32,
            kdf_salt=b"\x00" * 32,
            nonce=b"\x00" * 12,
            ciphertext=b"\x00" * 48,
        )
        fake_store = sa_enroll._FilteredStore(store, [fake_enrollment],
                                              self.profile.container_id)
        old_store = broker.credentials
        broker.credentials = fake_store
        try:
            self.say("  Touch the key if prompted (proving an un-enrolled credential fails closed):")
            self.app_arm()
            res = broker.open(DISPOSABLE_PROFILE, force_auth=True)
            gone, state = self.detached()
            run.check("F11", not res.ok and gone,
                      "open refused=%s (%s), storage %s"
                      % (not res.ok, res.error_category, state))
        finally:
            broker.credentials = old_store

        self.say("  Re-authenticating with the CORRECT PIN to reset the key retry counter:")
        self.app_arm()
        reset_res = broker.open(DISPOSABLE_PROFILE, force_auth=True)
        if not reset_res.ok:
            raise AbortAcceptance("failed to re-authenticate with the correct PIN: %s"
                                  % reset_res.reason)
        self.app_stop()
        self.wait_app_exit()
        broker.reconcile(DISPOSABLE_PROFILE)

    def phase_aggressive(self):
        run, broker = self.run, self.broker
        self.say("\n>> AGGRESSIVE MODE: every open requires authentication")
        broker.set_mode(DISPOSABLE_PROFILE, "aggressive")
        self.app_arm()
        opened = broker.open(DISPOSABLE_PROFILE)
        evidence("aggressive_mode_accepted", opened.ok)
        run.check("A01", opened.ok and self.canary_readable(),
                  "opened=%s under aggressive mode" % opened.ok)
        self.app_stop()
        self.wait_app_exit()
        broker.reconcile(DISPOSABLE_PROFILE)
        status = self.status()
        gone, state = self.detached()
        run.check("A02", (status["state"] == sa_state.AUTH_REQUIRED
                          and not status["session_active"] and not status["app_running"]
                          and gone),
                  "state=%s session=%s app=%s storage=%s"
                  % (status["state"], status["session_active"],
                     status["app_running"], state))
        evidence("aggressive_reopen_requires_auth",
                 status["state"] == sa_state.AUTH_REQUIRED and not status["session_active"])
        broker.set_mode(DISPOSABLE_PROFILE, "default")

    def phase_workstation_lock(self):
        run, broker = self.run, self.broker
        listener = WorkstationLockListener()
        if not listener.start():
            run.check("W01", False, "session notifications unavailable: %s"
                      % listener.failed)
            run.skip("W02", "no lock notification can arrive")
            return
        try:
            self.say("\n>> Opening the vault for the lock step: PIN + touch")
            self.app_arm()
            opened = broker.open(DISPOSABLE_PROFILE)
            mounted = opened.ok and self.canary_readable()
            if not mounted:
                run.skip("W01", "the vault did not open: %s" % opened.reason)
                run.check("W02", False, "the vault did not open")
                return
            self.say("  NOW LOCK THE WORKSTATION (Win+L). Unlock it again after a "
                     "few seconds. Waiting up to %ds for Windows to report the lock..."
                     % LOCK_WAIT_SECONDS)
            received = listener.locked.wait(LOCK_WAIT_SECONDS)
            run.check("W01", received, "WTS_SESSION_LOCK %s"
                      % ("received" if received else "never arrived"))
            if not received:
                run.skip("W02", "no lock notification arrived")
                broker.lock_now(DISPOSABLE_PROFILE)
                return
            results = broker.on_system_event(sa_broker.EVENT_WORKSTATION_LOCK,
                                             DISPOSABLE_PROFILE)
            listener.unlocked.wait(LOCK_WAIT_SECONDS)
            status = self.status()
            gone, state = self.detached()
            relocked = (all(r.ok for r in results) and not status["session_active"]
                        and status["pending_reauth"] and not status["app_running"]
                        and gone)
            verdict("workstation_lock_relock", relocked)
            run.check("W02", relocked,
                      "state=%s session=%s pending_reauth=%s app=%s storage=%s"
                      % (status["state"], status["session_active"],
                         status["pending_reauth"], status["app_running"], state))
        finally:
            listener.stop()

    def phase_broker_shutdown(self):
        run, broker = self.run, self.broker
        self.say("\n>> Opening the vault for the broker-shutdown step: PIN + touch")
        self.app_arm()
        opened = broker.open(DISPOSABLE_PROFILE)
        app_pid = self.status()["app_pid"]
        mounted = opened.ok and self.canary_readable()
        results = self.shutdown_broker(broker)
        app_gone = not process_alive(app_pid)
        empty = self.mount_empty() and not self.canary_readable()
        state = self.storage_state(broker)
        status = self.status(broker)
        run.check("S04", mounted and app_gone and empty and state == sa_storage.DETACHED,
                  "mounted=%s, app_gone=%s, empty=%s, storage=%s, helper=%s, state=%s"
                  % (mounted, app_gone, empty, state, [r.ok for r in results],
                     status["state"]))

    def phase_helper_failure(self):
        run, broker = self.run, self.broker
        self.say("\n>> Opening the vault for the helper-failure step: PIN + touch")
        self.app_arm()
        opened = broker.open(DISPOSABLE_PROFILE, force_auth=True)
        mounted = opened.ok and self.canary_readable()
        helper_pid = broker.helper_process_id
        name = process_name(helper_pid) if helper_pid else None
        if not (mounted and helper_pid and name == "powershell.exe"):
            run.check("H01", False, "mounted=%s helper pid=%s image=%s"
                      % (mounted, helper_pid, name))
            raise AbortAcceptance("no mounted vault with a verified helper to kill")
        self.say("  terminating the elevated helper (pid %d) on purpose; approve the "
                 "elevation prompt if Windows shows one" % helper_pid)
        killed, detail = terminate_elevated_process(helper_pid)
        run.check("H01", killed, detail)
        if not killed:
            raise AbortAcceptance("the elevated helper could not be terminated")

        observed = []
        results = broker.lock_now(DISPOSABLE_PROFILE)
        observed.append(("lock_now", [(r.ok, r.state) for r in results],
                         self.status()["state"]))
        try:
            broker.reconcile(DISPOSABLE_PROFILE)
        except Exception:
            pass
        observed.append(("reconcile", None, self.status()["state"]))
        still_readable = self.canary_readable()
        self.shutdown_broker(broker)
        observed.append(("shutdown", None, self.status()["state"]))
        false_locked = any(state == sa_state.LOCKED for _step, _r, state in observed)
        false_locked = false_locked or any(ok and state == sa_state.LOCKED
                                           for ok, state in observed[0][1])
        run.check("H02", still_readable and not false_locked
                  and not self.status()["session_active"],
                  "volume still readable after the failure=%s; states %s"
                  % (still_readable, [(step, state) for step, _r, state in observed]))

        recovery = self.new_broker("C")
        recovery.reconcile(DISPOSABLE_PROFILE)
        required = self.status(recovery)["state"] == sa_state.RECOVERY_REQUIRED
        result = recovery.recover(DISPOSABLE_PROFILE)
        gone, state = self.detached(recovery)
        run.check("H03", required and result.ok and result.state == sa_state.LOCKED
                  and gone,
                  "reconciled RECOVERY_REQUIRED=%s, recover=%s, storage %s"
                  % (required, result.state, state))

    def phase_audit(self):
        run = self.run
        records, allow, secret = scan_audit(
            self.registry.audit_path, pin_prompt=self.pin,
            recovery_values=self.recovery_values,
            known_mount_paths=self.known_mounts)
        run.check("X01", records > 0 and not allow,
                  "%d record(s)%s" % (records, "; " + "; ".join(allow[:5]) if allow else ""))
        run.check("X02", records > 0 and not secret,
                  "%d finding(s)%s" % (len(secret), "; " + "; ".join(secret[:5])
                                       if secret else ""))
        verdict("audit_log_clean", records > 0 and not allow and not secret)

    PHASES = ("phase_privileged_start", "phase_privilege_boundary",
              "phase_capabilities", "phase_enroll",
              "phase_unlock",
              "phase_default_mode", "phase_hostile", "phase_aggressive",
              "phase_workstation_lock", "phase_broker_shutdown",
              "phase_helper_failure", "phase_audit")

    # -- teardown -------------------------------------------------------------
    def cleanup(self):
        if self.profile is None:
            return
        say = self.say
        try:
            self.app_stop()
        except OSError:
            pass
        destroyed = not os.path.isfile(self.profile.container)
        ordered = ([b for _l, b in reversed(self.brokers) if id(b) not in self.closed]
                   + [b for _l, b in reversed(self.brokers) if id(b) in self.closed])
        for broker in ordered:
            if destroyed:
                break
            try:
                backend = broker.backend(self.profile)
                for mount in self.known_mounts:
                    try:
                        backend.unmount(self.profile.with_mount_path(mount))
                    except Exception:
                        pass
                destroyed = bool(backend.destroy(self.profile))
            except Exception:
                continue
        for _label, broker in self.brokers:
            if id(broker) in self.closed:
                continue
            try:
                self.shutdown_broker(broker)
            except Exception:
                pass
        if not destroyed and os.path.isfile(self.profile.container):
            say("  WARNING the disposable container could not be removed: %s"
                % self.profile.container)
            say("          detach it from an elevated PowerShell with:")
            say("          Dismount-DiskImage -ImagePath '%s'" % self.profile.container)
            return
        for path in self.known_mounts:
            try:
                os.rmdir(path)
            except OSError:
                pass
        if os.path.exists(self.mount):
            say("  WARNING the disposable mount directory is not empty: %s" % self.mount)
        shutil.rmtree(str(self.root), ignore_errors=True)

    # -- driver -----------------------------------------------------------------
    def execute(self):
        if not self.prepare():
            return 2
        self.plan()
        if not self.confirm("Start the disposable hardware acceptance?"):
            self.say("  not started; nothing was created and no record changed")
            return 1
        try:
            self.build_disposable()
            for phase in self.PHASES:
                getattr(self, phase)()
        except AbortAcceptance as exc:
            self.run.abort(exc)
            self.say("  ABORTED: %s" % exc)
        except KeyboardInterrupt:
            self.run.abort("cancelled by the operator")
            self.say("  CANCELLED by the operator")
        except Exception as exc:
            self.run.abort(type(exc).__name__)
            self.say("  ABORTED by an unexpected %s: %s" % (type(exc).__name__, exc))
        finally:
            try:
                self.cleanup()
            except Exception as exc:
                self.say("  cleanup failed: %s" % type(exc).__name__)
        return self.finalize()

    def finalize(self):
        say, run = self.say, self.run
        gates = run.gates()
        complete = run.complete()
        record = sa_acceptance.build_record(self.environment, self.template, gates,
                                            complete=complete)
        text = json.dumps(record)
        if (any(r and r in text for r in self.recovery_values)
                or self.pin.found_in(text)):
            say("  the acceptance record itself carried secret material; refusing it")
            gates["audit_hygiene_accepted"] = False
            record = sa_acceptance.build_record(self.environment, self.template,
                                                gates, complete=False)
        counts = run.counts()
        missing = run.missing()
        evidence("checks_passed", counts[PASS])
        evidence("checks_failed", counts[FAIL])
        evidence("checks_skipped", counts[SKIP])
        evidence("checks_missing", len(missing))
        for flag in sa_acceptance.GATE_FLAGS:
            evidence(flag, PASS if record[flag] else FAIL)
        evidence("migration_ready", PASS if record["migration_ready"] else FAIL)
        write_evidence(evidence_path(self.args, self.production))

        say("")
        for flag in sa_acceptance.GATE_FLAGS:
            say("  %-4s %s" % (PASS if record[flag] else FAIL, flag))
        state_dir = self.production.state_dir
        if not record["migration_ready"]:
            not_passed = run.not_passed()
            try:
                sa_acceptance.save_record(state_dir, record)
            except (OSError, sa_acceptance.AcceptanceError):
                if not sa_acceptance.revoke(state_dir):
                    say("  WARNING an older acceptance record may still exist at %s; "
                        "delete it" % sa_acceptance.record_path(state_dir))
            say("")
            say("  HARDWARE ACCEPTANCE NOT GRANTED -- migration stays blocked.")
            if run.aborted:
                say("  aborted: %s" % run.aborted)
            say("  checks not passed: %s" % (", ".join(not_passed) or "none"))
            return 1
        try:
            path = sa_acceptance.save_record(state_dir, record)
        except (OSError, sa_acceptance.AcceptanceError) as exc:
            sa_acceptance.revoke(state_dir)
            say("  every check passed, but the record could not be written (%s); "
                "migration stays blocked" % type(exc).__name__)
            return 1
        gate = sa_acceptance.evaluate(state_dir, self.template)
        if not gate.ok:
            sa_acceptance.revoke(state_dir)
            say("  every check passed, but the production gate does not accept the "
                "record it was given; migration stays blocked:")
            for reason in gate.reasons:
                say("    - %s" % reason)
            return 1
        say("")
        say("=" * 60)
        for token in sa_acceptance.FINAL_TOKENS:
            say(token)
        say("=" * 60)
        say("  record: %s" % path)
        say("  production check (no key, no mount, no copy):")
        say('  "%s" "%s" migrate %s --check-acceptance'
            % (sys.executable, Path(HERE) / "sa_cli.py", self.template.id))
        return 0


def cmd_disposable_acceptance(args):
    return DisposableAcceptance(args).execute()
