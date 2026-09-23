"""Pre-authorized privilege boundary for SAITULS Secure Apps.

Attaching a VHDX, unlocking a BitLocker volume and driving CTAPHID all need
administrator rights. The GUI, the broker and Obsidian must never have them.
The old answer was one UAC consent dialog per broker session, which in
practice means a consent dialog on every unlock, lock, mount and relock --
and a user who has been trained to click "Yes" without reading it.

This module replaces that runtime prompt with a **narrow, pre-approved
elevation mechanism**: a Windows Scheduled Task, registered once while the
user explicitly approves elevation, whose action is FIXED::

    \\SAITULS\\SecureAppsPrivilegedHelper
        powershell.exe -NoLogo -NoProfile -NonInteractive
                       -ExecutionPolicy Bypass -WindowStyle Hidden
                       -File "<pinned>\\sa_storage_helper.ps1" -ScheduledHelper

After that registration a medium-integrity broker may ask Task Scheduler to
start *that* task -- and nothing else -- without a second consent dialog.

Three properties make this a privilege *boundary* rather than a UAC bypass:

``the action is fixed``
    The task takes no caller-supplied executable, no caller-supplied script
    and no caller-supplied arguments. Ordinary runtime may only ``/Run`` the
    registered task and read its state; ``/Create``, ``/Change`` and
    ``/Delete`` are installation operations that require elevation again.

``the definition is fingerprinted``
    :func:`canonical_definition` reduces a task to the fields that decide
    what it runs, as whom, and at what privilege; :func:`definition_fingerprint`
    hashes that. The fingerprint is pinned at install time and re-checked
    before every silent start. A task whose action, principal, run level or
    arguments moved is :data:`PRIVILEGED_TASK_TAMPERED` -- fail closed, and
    never silently repaired from a medium-integrity process.

``starting is not authenticating``
    Task Scheduler is how the high-integrity process comes into existence.
    It is NOT why the broker trusts it. The broker proves the identity of the
    process on the other end of the pipe from the kernel every time; see
    :mod:`sa_privhelper`.

The runtime parameters the old design passed on the elevated command line
(pipe name, correlation nonce, worker path) cannot ride on a fixed task
action, so they move to a **pin file** written by the elevated installer into
``%ProgramData%\\SAITULS\\secure-apps``, hardened so that only SYSTEM and
Administrators may write it and the intended user may read it.

``the runtime is installed, not run from the repository``
    The elevated helper no longer executes ``sa_storage_helper.ps1`` out of
    the medium-integrity-writable repository, and no longer launches a
    ``python.exe`` (whose import tree the medium user can rewrite) to run
    ``sa_fido_worker.py``. Instead an elevated Install/Repair transaction
    copies a **protected runtime** into
    ``%ProgramData%\\SAITULS\\secure-apps\\privileged``::

        privileged\\
            sa_storage_helper.ps1     the elevated helper
            sa_fido_worker.exe        the FROZEN FIDO worker (no interpreter)
            privileged-runtime.json   a public, non-secret bundle manifest

    That directory carries an explicit ACL -- SYSTEM and Administrators full
    control, the intended user read+execute, everyone else nothing, and
    inheritance disabled -- so a medium-integrity process may execute the
    runtime but can neither replace nor edit any executable, script or the
    manifest. The scheduled task's fixed action names the *installed* helper,
    never the repository copy, and the elevated helper launches only the
    *installed* ``sa_fido_worker.exe`` by absolute path.

``the runtime bundle is fingerprinted``
    :func:`build_runtime_manifest` binds the helper filename and SHA-256, the
    frozen worker filename and SHA-256 and the task action path;
    :func:`runtime_bundle_fingerprint` hashes that canonical content. The
    fingerprint is pinned at install time, re-checked before every silent
    start, and any change to helper, worker, manifest, task definition or
    task security descriptor invalidates the durable acceptance record.

Nothing in this module ever sees key material, and nothing it writes to disk
carries anything but public facts: paths, hashes, a SID and a task name.
"""
import hashlib
import json
import ntpath
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree

# --------------------------------------------------------------------------
# identity of the mechanism
# --------------------------------------------------------------------------
#: Recorded in the acceptance record as ``privileged_launch_mode``. Bump the
#: suffix when the *mechanism* changes (a different elevation carrier), not
#: when the task's arguments change -- that is what the fingerprint is for.
LAUNCH_MODE = "scheduled-task-v1"

TASK_FOLDER = "\\SAITULS"
TASK_NAME = "SecureAppsPrivilegedHelper"
TASK_PATH = TASK_FOLDER + "\\" + TASK_NAME

DEFINITION_SCHEMA = "saituls.secure-apps.privileged-task/1"
DEFINITION_VERSION = 1

#: The hardened pin. Bumped to /2 when the old ``python_exe`` /
#: ``sa_fido_worker.py`` execution contract was replaced by the protected
#: installed runtime; bumped again to /3 when the frozen worker became a
#: PyInstaller ``--onedir`` BUNDLE bound by a recursive fingerprint rather
#: than a single ``--onefile`` executable that unpacked itself into user
#: TEMP. An older pin no longer authorises anything and fails closed (it can
#: be re-created only by an elevated Repair).
PIN_SCHEMA = "saituls.secure-apps.privileged-helper-pin/3"
PIN_VERSION = 3
PIN_FILENAME = "privileged-helper.json"

#: The protected runtime installed by the elevated transaction. These name
#: the installed tree.
PRIVILEGED_DIR_NAME = "privileged"
PRIVILEGED_RUNTIME_VERSION = 2
PRIVILEGED_DIR_ENV = "SAITULS_SECURE_APPS_PRIVILEGED_DIR"
FROZEN_WORKER_NAME = "sa_fido_worker.exe"

#: The ``--onedir`` worker bundle inside the protected runtime. A onefile
#: build is refused for the privileged worker on purpose: PyInstaller's
#: onefile stub extracts its whole executable dependency closure into an
#: ordinary ``%TEMP%\_MEI...`` directory and then runs it from there, which
#: would put every DLL an elevated process loads back inside a directory the
#: medium user owns. The onedir tree lives under the protected ACL instead
#: and nothing is ever extracted at runtime.
FIDO_BUNDLE_DIR_NAME = "fido-worker"
PYINSTALLER_INTERNAL_DIR = "_internal"

#: Canonical document that :func:`bundle_fingerprint` hashes. Bumped when the
#: canonicalisation rules change -- never when a bundle's contents change.
BUNDLE_FINGERPRINT_SCHEMA = "saituls.secure-apps.fido-worker-bundle/1"

#: The privileged scratch root. Command material an elevated process consumes
#: (the DiskPart script) is written here, never in the medium user's TEMP.
PRIVILEGED_TMP_DIR_NAME = "privileged-tmp"
PRIVILEGED_TMP_ENV = "SAITULS_SECURE_APPS_PRIVILEGED_TMP"

RUNTIME_MANIFEST_SCHEMA = "saituls.secure-apps.privileged-runtime/2"
RUNTIME_MANIFEST_FILENAME = "privileged-runtime.json"

HELPER_SCRIPT = "sa_storage_helper.ps1"
#: The Python source of the FIDO worker. Kept as build input; it is NEVER the
#: executed privileged artifact any more -- the frozen ``sa_fido_worker.exe``
#: installed into the protected runtime is.
WORKER_SOURCE = "sa_fido_worker.py"
SCHEDULED_HELPER_SWITCH = "-ScheduledHelper"

#: Default lifetime of an idle elevated helper. The helper exits when no
#: broker has been connected for this long, so the high-integrity process is
#: not sitting there between unlocks; the next unlock starts it again,
#: silently, through the same task.
DEFAULT_IDLE_TIMEOUT_SECONDS = 300
MIN_IDLE_TIMEOUT_SECONDS = 30
MAX_IDLE_TIMEOUT_SECONDS = 3600

#: Surfaced verbatim. A caller that sees one of these must not continue as if
#: the privileged path were merely unavailable-but-fine.
PRIVILEGED_TASK_TAMPERED = "PRIVILEGED_TASK_TAMPERED"
PRIVILEGED_TASK_MISSING = "PRIVILEGED_TASK_MISSING"
PRIVILEGED_HELPER_NOT_INSTALLED = "PRIVILEGED_HELPER_NOT_INSTALLED"
LOCK_PRIVILEGED_HELPER_UNAVAILABLE = "LOCK_PRIVILEGED_HELPER_UNAVAILABLE"
SILENT_PRIVILEGED_START_ACCEPTED = "SILENT_PRIVILEGED_START_ACCEPTED"
REPAIR_REQUIRED = "REPAIR_REQUIRED"
#: The runtime directory ACL does not enforce the protected-runtime rule
#: (a medium-integrity account can write/replace an elevated artifact).
PRIVILEGED_RUNTIME_ACL_INVALID = "PRIVILEGED_RUNTIME_ACL_INVALID"
#: The scheduled task's security descriptor lets the medium account modify,
#: delete or re-own the task -- or could not be read at all. Fail closed.
PRIVILEGED_TASK_SECURITY_INVALID = "PRIVILEGED_TASK_SECURITY_INVALID"

#: The protected scratch root an elevated process writes command material to
#: is missing, reachable through a reparse point, or writable by the medium
#: user. No privileged command file may be consumed from such a directory.
PRIVILEGED_SCRATCH_INVALID = "PRIVILEGED_SCRATCH_INVALID"

#: The pin's OWNER is not Administrators/SYSTEM, or the medium user holds
#: WRITE_DAC / WRITE_OWNER on it. An owner can rewrite a DACL, so a pin the
#: interactive user owns is not protected however tight its DACL reads.
PRIVILEGED_PIN_OWNER_INVALID = "PRIVILEGED_PIN_OWNER_INVALID"

#: The installed worker bundle no longer hashes to the fingerprint pinned at
#: installation: a file inside it was added, removed or modified.
PRIVILEGED_WORKER_BUNDLE_INVALID = "PRIVILEGED_WORKER_BUNDLE_INVALID"

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"

TASK_RIGHT_READ = 0x00020001
TASK_RIGHT_RUN = 0x00100020
TASK_MODIFY_RIGHTS = (
    0x00010000 |  # DELETE
    0x00040000 |  # WRITE_DAC
    0x00080000 |  # WRITE_OWNER
    0x00000002 |  # FILE_WRITE_DATA / TASK_MODIFY
    0x00000004 |  # FILE_APPEND_DATA
    0x00000010 |  # FILE_WRITE_EA
    0x00000100 |  # FILE_WRITE_ATTRIBUTES
    0x40000000 |  # GENERIC_WRITE
    0x10000000    # GENERIC_ALL
)

#: The only host image the fixed action is ever allowed to name. Windows
#: PowerShell ships in the protected system directory; a "powershell.exe"
#: anywhere writable is the attack this pin exists to refuse.
POWERSHELL_RELATIVE = ("System32", "WindowsPowerShell", "v1.0", "powershell.exe")

#: Fixed switches of the action. Order is part of the fingerprint.
HELPER_HOST_SWITCHES = ("-NoLogo", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                        "-File")

#: Test-only override of the pin location. Honoured by this module (which
#: runs at medium integrity and is re-verified against the kernel anyway) and
#: deliberately NOT by the elevated helper, whose pin path is a constant.
PIN_PATH_ENV = "SAITULS_SECURE_APPS_PIN_PATH"


class PrivilegeTaskError(Exception):
    """A privileged-task operation failed. Never carries key material."""

    category = "internal"
    token = ""

    def __init__(self, message, category=None, token=""):
        Exception.__init__(self, message)
        if category:
            self.category = category
        if token:
            self.token = token


class TaskMissing(PrivilegeTaskError):
    category = "privileged_task_missing"
    token = PRIVILEGED_TASK_MISSING


class TaskTampered(PrivilegeTaskError):
    category = "privileged_task_tampered"
    token = PRIVILEGED_TASK_TAMPERED


class TaskUnavailable(PrivilegeTaskError):
    category = "privileged_helper_unavailable"
    token = LOCK_PRIVILEGED_HELPER_UNAVAILABLE


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def canonical_json(document):
    """One byte sequence per value: sorted keys, no insignificant whitespace."""
    return json.dumps(document, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def normalize_path(path):
    """Absolute, case-folded path for comparison. No filesystem access."""
    if not path:
        return ""
    return ntpath.normcase(ntpath.normpath(str(path)))


def file_digest(path):
    """SHA-256 of a file, or None when it cannot be read.

    None is never "close enough": every caller treats an unmeasurable file as
    a refusal, because a pin that cannot be checked protects nothing.
    """
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def powershell_host():
    """Absolute path of the Windows PowerShell host the task action names."""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return ntpath.join(root, *POWERSHELL_RELATIVE)


def program_data_root():
    root = os.environ.get("ProgramData") or r"C:\ProgramData"
    return ntpath.join(root, "SAITULS", "secure-apps")


def privileged_root():
    """The installed protected runtime directory.

    ``%ProgramData%\\SAITULS\\secure-apps\\privileged`` unless the test hook
    :data:`PRIVILEGED_DIR_ENV` overrides it. Honoured by this medium-integrity
    module (re-verified against the kernel anyway) and deliberately NOT by the
    elevated helper, whose runtime path is a constant.
    """
    override = os.environ.get(PRIVILEGED_DIR_ENV)
    if override:
        return ntpath.normpath(override)
    return ntpath.join(program_data_root(), PRIVILEGED_DIR_NAME)


def installed_helper_path(runtime_root=None):
    return ntpath.join(runtime_root or privileged_root(), HELPER_SCRIPT)


def installed_worker_bundle_dir(runtime_root=None):
    """The protected ``--onedir`` FIDO worker tree inside the runtime root."""
    return ntpath.join(runtime_root or privileged_root(), FIDO_BUNDLE_DIR_NAME)


def installed_worker_exe_path(runtime_root=None):
    return ntpath.join(installed_worker_bundle_dir(runtime_root),
                       FROZEN_WORKER_NAME)


def privileged_tmp_root():
    """Protected scratch for privileged command material. Never user TEMP.

    ``%ProgramData%\SAITULS\secure-apps\privileged-tmp`` unless the test hook
    :data:`PRIVILEGED_TMP_ENV` overrides it. The DiskPart script an elevated
    process executes carries no secret, but its INTEGRITY decides what that
    elevated process does, so it may not sit anywhere a medium account can
    write.
    """
    override = os.environ.get(PRIVILEGED_TMP_ENV)
    if override:
        return ntpath.normpath(override)
    return ntpath.join(program_data_root(), PRIVILEGED_TMP_DIR_NAME)


def runtime_manifest_path(runtime_root=None):
    return ntpath.join(runtime_root or privileged_root(), RUNTIME_MANIFEST_FILENAME)


def pin_path():
    """Where the pin record lives. See :data:`PIN_PATH_ENV` for the test hook."""
    override = os.environ.get(PIN_PATH_ENV)
    if override:
        return ntpath.normpath(override)
    return ntpath.join(program_data_root(), PIN_FILENAME)


def helper_root(script_dir=None):
    return script_dir or ntpath.dirname(ntpath.abspath(__file__))


def helper_script_path(script_dir=None):
    return ntpath.join(helper_root(script_dir), HELPER_SCRIPT)


def worker_source_path(script_dir=None):
    """The Python SOURCE of the worker (build input; never executed elevated)."""
    return ntpath.join(helper_root(script_dir), WORKER_SOURCE)


def path_is_within(root, candidate, allow_root=False):
    """True when *candidate* resolves strictly inside *root*.

    Rejects a path that climbs out with ``..``, that is not under the root, or
    that -- on a real filesystem -- reaches the root through a reparse point
    (symlink / junction / mount point). The elevated runtime may live only in
    files the installer put inside the protected directory, so a pinned path
    that resolves elsewhere is refused before it is ever executed.
    """
    if not root or not candidate:
        return False
    try:
        real_root = _real_path(root)
        real_candidate = _real_path(candidate)
    except OSError:
        return False
    root_norm = normalize_path(real_root).rstrip("\\")
    cand_norm = normalize_path(real_candidate).rstrip("\\")
    if cand_norm == root_norm:
        return bool(allow_root)
    return cand_norm.startswith(root_norm + "\\")


def _real_path(path):
    """``realpath`` where the OS resolves reparse points, ``normpath`` else.

    On Windows ``os.path.realpath`` follows junctions and symlinks, which is
    exactly the reparse-point escape this must catch. Off Windows (the
    deterministic test bed) it degrades to a canonical normpath.
    """
    text = str(path)
    try:
        if os.name == "nt" and (os.path.exists(text) or os.path.islink(text)):
            return os.path.realpath(text)
    except OSError:
        pass
    return ntpath.normpath(text)


def contains_reparse_point(path, stop_at=None):
    """True when any component of *path* up to *stop_at* is a reparse point.

    A cheap, explicit guard used before the runtime is trusted: even if the
    final target is inside the root, a junction somewhere on the way turns
    "inside the protected directory" into a lie. None off Windows / on error.
    """
    if os.name != "nt":
        return None
    try:
        import stat
        current = ntpath.normpath(str(path))
        stop = normalize_path(stop_at).rstrip("\\") if stop_at else None
        seen = set()
        while current and current not in seen:
            seen.add(current)
            try:
                attrs = os.lstat(current)
                if attrs.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    return True
            except (OSError, AttributeError):
                pass
            if stop and normalize_path(current).rstrip("\\") == stop:
                break
            parent = ntpath.dirname(current)
            if parent == current:
                break
            current = parent
        return False
    except Exception:
        return None


# --------------------------------------------------------------------------
# recursive worker-bundle fingerprint
# --------------------------------------------------------------------------
# A onefile executable could be bound by one SHA-256. A onedir bundle cannot:
# its DLLs, its Python extension modules and its zipped stdlib are SEPARATE
# files that an elevated process loads, so binding only sa_fido_worker.exe
# would leave every one of them unbound. The fingerprint below therefore
# covers the WHOLE tree.
#
# Canonical rules -- the PowerShell helper reproduces them byte for byte:
#
#   * every regular file under the bundle root is bound, recursively;
#   * the relative path is taken from the bundle root, its separators are
#     normalised to ``/`` and it is lowercased with ASCII case semantics,
#     because NTFS compares names case-insensitively and two entries differing
#     only in case are therefore a collision, not two files;
#   * that relative path must be printable ASCII. A privileged bundle is a
#     generated artifact, not arbitrary user data, and requiring ASCII is what
#     makes "sorted ordinally" and "escaped identically" mean the same thing
#     in Python and in PowerShell instead of almost the same thing. A bundle
#     with a non-ASCII member is refused, never guessed at;
#   * each entry is ``{"path": <rel>, "sha256": <lowercase hex>, "size": <int>}``;
#   * entries are sorted by that relative path, ordinal;
#   * the document is ``{"schema": ..., "file_count": N, "entries": [...]}``
#     serialised as canonical JSON: sorted keys, no insignificant whitespace,
#     ``ensure_ascii`` so a non-ASCII filename escapes identically in both
#     implementations;
#   * no timestamp, no absolute path and no build-machine detail is bound, so
#     the value is reproducible from the installed tree alone.
#
# Any modification, addition or deletion inside the bundle moves the value and
# invalidates installation and acceptance.
MAX_BUNDLE_FILES = 20000


def canonical_bundle_document(entries):
    """The exact byte sequence :func:`bundle_fingerprint` hashes."""
    document = {
        "schema": BUNDLE_FINGERPRINT_SCHEMA,
        "file_count": len(entries),
        "entries": [{"path": entry["path"], "sha256": entry["sha256"],
                     "size": entry["size"]} for entry in entries],
    }
    return json.dumps(document, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))


def _bundle_relative_key(root, path):
    rel = ntpath.relpath(path, root)
    return rel.replace("\\", "/").lower()


def bundle_file_entries(bundle_dir):
    """``(entries, problem)``. Exactly one is None.

    Refuses a reparse point anywhere inside the bundle -- a junction under the
    protected tree would let a directory the medium user controls answer for a
    file this fingerprint claims to bind.
    """
    root = ntpath.normpath(str(bundle_dir))
    if not os.path.isdir(root):
        return None, "the FIDO worker bundle directory is missing: %s" % root
    try:
        import stat as stat_module
    except ImportError:                                    # pragma: no cover
        return None, "the FIDO worker bundle could not be measured"
    reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    entries = []
    seen = {}
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            listing = sorted(os.listdir(current))
        except OSError:
            return None, ("the FIDO worker bundle directory could not be read: %s"
                          % current)
        for name in listing:
            full = ntpath.join(current, name)
            try:
                info = os.lstat(full)
            except OSError:
                return None, "a FIDO worker bundle entry disappeared: %s" % full
            if os.name == "nt" and (getattr(info, "st_file_attributes", 0)
                                    & reparse):
                return None, ("the FIDO worker bundle contains a reparse point: "
                              "%s (%s)" % (full, PRIVILEGED_WORKER_BUNDLE_INVALID))
            if stat_module.S_ISDIR(info.st_mode):
                pending.append(full)
                continue
            if not stat_module.S_ISREG(info.st_mode):
                return None, ("the FIDO worker bundle contains a non-regular "
                              "file: %s" % full)
            if len(entries) >= MAX_BUNDLE_FILES:
                return None, ("the FIDO worker bundle holds more than %d files"
                              % MAX_BUNDLE_FILES)
            key = _bundle_relative_key(root, full)
            if any(ch < " " or ch > "~" for ch in key):
                return None, ("the FIDO worker bundle path %r is not printable "
                              "ASCII: %s" % (key, PRIVILEGED_WORKER_BUNDLE_INVALID))
            if key in seen:
                return None, ("two FIDO worker bundle entries share the path %r "
                              "under case-insensitive comparison" % key)
            digest = file_digest(full)
            if digest is None:
                return None, "a FIDO worker bundle file could not be hashed: %s" % full
            seen[key] = full
            entries.append({"path": key, "sha256": digest,
                            "size": int(info.st_size)})
    if not entries:
        return None, "the FIDO worker bundle is empty: %s" % root
    worker_key = FROZEN_WORKER_NAME.lower()
    if worker_key not in seen:
        return None, ("the FIDO worker bundle does not contain %s"
                      % FROZEN_WORKER_NAME)
    entries.sort(key=lambda entry: entry["path"])
    return entries, None


def bundle_fingerprint(bundle_dir):
    """``(fingerprint, file_count, problem)``. Fails closed, never guesses."""
    entries, problem = bundle_file_entries(bundle_dir)
    if entries is None:
        return None, None, problem
    payload = canonical_bundle_document(entries)
    return (hashlib.sha256(payload.encode("utf-8")).hexdigest(), len(entries),
            None)


# --------------------------------------------------------------------------
# user identity
# --------------------------------------------------------------------------
def _win32security():
    import win32security
    return win32security


def current_user_sid():
    """SID string of the account this process runs as, or None off Windows."""
    if os.name != "nt":
        return None
    try:
        import win32api
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                               win32security.TOKEN_QUERY)
        try:
            sid, _attrs = win32security.GetTokenInformation(
                token, win32security.TokenUser)
            return str(win32security.ConvertSidToStringSid(sid))
        finally:
            try:
                token.Close()
            except Exception:
                pass
    except Exception:
        return None


def resolve_user_sid(value, lookup=None):
    """Normalise a ``<UserId>`` to a SID string.

    Task Scheduler accepts a SID or an account name and may hand either one
    back. Both are reduced to a SID so that "the task runs as somebody else"
    is answered by comparison rather than by string luck. An unresolvable
    name is returned unchanged, which fails the comparison -- the safe way
    round.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if re.match(r"^S-1-[0-9\-]+$", text, re.IGNORECASE):
        return text.upper()
    if lookup is not None:
        resolved = lookup(text)
        return str(resolved).upper() if resolved else text
    if os.name != "nt":
        return text
    try:
        win32security = _win32security()
        sid, _domain, _kind = win32security.LookupAccountName(None, text)
        return str(win32security.ConvertSidToStringSid(sid)).upper()
    except Exception:
        return text


def user_is_administrator():
    """True when this account is a member of the local Administrators group.

    Asked of the *linked* token as well, because under Admin Approval Mode
    the membership that matters at registration time is deny-only in this
    process's own token.
    """
    if os.name != "nt":
        return None
    try:
        import win32api
        import win32security
        import ntsecuritycon
    except ImportError:
        return None
    admins = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid)
    process = win32api.GetCurrentProcess()
    try:
        token = win32security.OpenProcessToken(
            process, win32security.TOKEN_QUERY | win32security.TOKEN_DUPLICATE)
    except Exception:
        return None
    tokens = [token]
    try:
        try:
            linked = win32security.GetTokenInformation(
                token, ntsecuritycon.TokenLinkedToken)
            if linked:
                tokens.append(linked)
        except Exception:
            pass
        for candidate in tokens:
            try:
                groups = win32security.GetTokenInformation(
                    candidate, win32security.TokenGroups)
            except Exception:
                continue
            for sid, attributes in groups:
                if sid == admins and attributes & 0x00000004:   # SE_GROUP_ENABLED
                    return True
                if sid == admins and attributes & 0x00000010:   # USE_FOR_DENY_ONLY
                    return True
        return False
    finally:
        for candidate in tokens:
            try:
                candidate.Close()
            except Exception:
                pass


def process_is_elevated():
    """True when this process holds an elevated token AND high integrity."""
    if os.name != "nt":
        return False
    try:
        import win32api
        import win32security
        import ntsecuritycon
    except ImportError:
        return False
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                               win32security.TOKEN_QUERY)
    except Exception:
        return False
    try:
        elevated = bool(win32security.GetTokenInformation(
            token, ntsecuritycon.TokenElevation))
        integrity = win32security.GetTokenInformation(
            token, ntsecuritycon.TokenIntegrityLevel)[0]
        rid = integrity.GetSubAuthority(integrity.GetSubAuthorityCount() - 1)
        return bool(elevated and int(rid) >= int(
            ntsecuritycon.SECURITY_MANDATORY_HIGH_RID))
    except Exception:
        return False
    finally:
        try:
            token.Close()
        except Exception:
            pass


def uac_enabled():
    """``EnableLUA``. False means every administrator process is elevated."""
    if os.name != "nt":
        return None
    try:
        import winreg
        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
                0, access) as key:
            value, _kind = winreg.QueryValueEx(key, "EnableLUA")
        return bool(value)
    except OSError:
        return None


def uac_policy_facts():
    """Read-only report of the UAC knobs this feature must NOT change."""
    facts = {"EnableLUA": uac_enabled(),
             "ConsentPromptBehaviorAdmin": None,
             "PromptOnSecureDesktop": None}
    if os.name != "nt":
        return facts
    try:
        import winreg
        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
                0, access) as key:
            for name in ("ConsentPromptBehaviorAdmin", "PromptOnSecureDesktop"):
                try:
                    facts[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    facts[name] = None
    except OSError:
        pass
    return facts


def pipe_name_for_sid(sid_text):
    """The fixed, per-user pipe name both ends derive independently.

    Derived from the SID rather than carried on a command line, because a
    fixed task action has no command line to carry it on. It is not a secret
    and authenticates nothing: it only keeps two users' helpers apart.
    """
    digest = hashlib.sha256(("saituls.secure-apps.privileged-helper/1|%s"
                             % (sid_text or "")).encode("utf-8")).hexdigest()
    return r"\\.\pipe\SAITULS_SECAPP_PRIV_" + digest[:32]


# --------------------------------------------------------------------------
# Windows command-line tokenisation
# --------------------------------------------------------------------------
def split_arguments(text):
    """Split a Windows argument string the way ``CommandLineToArgvW`` does.

    The task's ``<Arguments>`` is one string; whether it carries an extra
    switch is a security question ("unexpected task arguments"), so it is
    compared as tokens and not as text. Implemented here rather than with
    :mod:`shlex`, whose POSIX rules disagree with Windows on backslashes --
    which is exactly where a path lives.
    """
    tokens = []
    current = []
    in_quotes = False
    backslashes = 0
    started = False
    for char in str(text or ""):
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            current.append("\\" * (backslashes // 2))
            if backslashes % 2:
                current.append('"')
            else:
                in_quotes = not in_quotes
            backslashes = 0
            started = True
            continue
        current.append("\\" * backslashes)
        backslashes = 0
        if char in (" ", "\t") and not in_quotes:
            # ``started`` -- not a non-empty buffer -- decides whether a token
            # ended here, so a run of spaces produces no phantom argument
            # while a deliberately empty one ("") still survives.
            if started:
                tokens.append("".join(current))
            current = []
            started = False
            continue
        current.append(char)
        started = True
    current.append("\\" * backslashes)
    if started:
        tokens.append("".join(current))
    return tokens


def quote_argument(token):
    """Quote one token so ``CommandLineToArgvW`` reproduces it exactly."""
    text = str(token)
    if text and not re.search(r'[\s"]', text):
        return text
    out = ['"']
    backslashes = 0
    for char in text:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            out.append("\\" * (backslashes * 2 + 1))
            out.append('"')
            backslashes = 0
            continue
        out.append("\\" * backslashes)
        backslashes = 0
        out.append(char)
    out.append("\\" * (backslashes * 2))
    out.append('"')
    return "".join(out)


def join_arguments(tokens):
    return " ".join(quote_argument(token) for token in tokens)


# --------------------------------------------------------------------------
# canonical definition
# --------------------------------------------------------------------------
def action_arguments(helper_path):
    """The fixed argument vector of the task action."""
    return list(HELPER_HOST_SWITCHES) + [str(helper_path), SCHEDULED_HELPER_SWITCH]


def canonical_definition(user_sid, helper_path, host_image=None,
                         working_directory=None, task_path=TASK_PATH,
                         run_level="HighestAvailable",
                         logon_type="InteractiveToken",
                         arguments=None, triggers=(), action_count=1,
                         enabled=True, allow_start_on_demand=True,
                         allow_hard_terminate=True,
                         multiple_instances="IgnoreNew",
                         execution_time_limit="PT0S",
                         disallow_start_if_on_batteries=False,
                         stop_if_going_on_batteries=False,
                         run_only_if_idle=False,
                         run_only_if_network_available=False,
                         start_when_available=False):
    """Every field that decides *what runs, as whom, at what privilege*.

    Cosmetic settings (description, author, priority, hidden) are deliberately
    absent: they are reported by :func:`check` but must not make a benign
    Windows-side rewrite look like tampering.
    """
    image = host_image or powershell_host()
    argv = list(arguments) if arguments is not None else action_arguments(helper_path)
    # The -File path is compared case-insensitively; everything else is a
    # literal switch whose case is part of the definition.
    normalised = []
    for index, token in enumerate(argv):
        previous = argv[index - 1] if index else ""
        normalised.append(normalize_path(token) if previous == "-File" else str(token))
    return {
        "schema": DEFINITION_SCHEMA,
        "definition_version": DEFINITION_VERSION,
        "task_path": str(task_path),
        "user_sid": resolve_user_sid(user_sid),
        "run_level": str(run_level),
        "logon_type": str(logon_type),
        "image": normalize_path(image),
        "arguments": normalised,
        "working_directory": normalize_path(working_directory or ""),
        "triggers": sorted(str(t) for t in (triggers or ())),
        "action_count": int(action_count),
        "enabled": bool(enabled),
        "allow_start_on_demand": bool(allow_start_on_demand),
        "allow_hard_terminate": bool(allow_hard_terminate),
        "multiple_instances": str(multiple_instances),
        "execution_time_limit": str(execution_time_limit),
        "disallow_start_if_on_batteries": bool(disallow_start_if_on_batteries),
        "stop_if_going_on_batteries": bool(stop_if_going_on_batteries),
        "run_only_if_idle": bool(run_only_if_idle),
        "run_only_if_network_available": bool(run_only_if_network_available),
        "start_when_available": bool(start_when_available),
    }


def expected_definition(user_sid=None, script_dir=None, host_image=None,
                        task_path=TASK_PATH, helper_path=None):
    """The canonical definition this installation must find registered.

    ``helper_path`` is the *installed* protected helper. When omitted it
    defaults to the runtime root's helper, never the source tree -- the task
    action must resolve inside the protected runtime.
    """
    helper = helper_path or installed_helper_path()
    return canonical_definition(
        user_sid if user_sid is not None else current_user_sid(),
        helper,
        host_image=host_image,
        working_directory=ntpath.dirname(helper),
        task_path=task_path)


def definition_fingerprint(definition):
    """SHA-256 over the canonical definition. None when it cannot be built."""
    if not isinstance(definition, dict):
        return None
    try:
        payload = canonical_json(definition)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: Human-readable label per canonical field, so a refusal says which part of
#: the elevation boundary moved instead of "a hash changed".
_FIELD_LABELS = {
    "task_path": "task name",
    "user_sid": "task principal (user)",
    "run_level": "run level",
    "logon_type": "logon type",
    "image": "task action executable",
    "arguments": "task action arguments",
    "working_directory": "task working directory",
    "triggers": "task triggers",
    "action_count": "number of task actions",
    "enabled": "task enabled flag",
    "allow_start_on_demand": "allow start on demand",
    "allow_hard_terminate": "allow hard terminate",
    "multiple_instances": "multiple-instances policy",
    "execution_time_limit": "execution time limit",
    "disallow_start_if_on_batteries": "disallow start on batteries",
    "stop_if_going_on_batteries": "stop when going on batteries",
    "run_only_if_idle": "run only if idle",
    "run_only_if_network_available": "run only if network available",
    "start_when_available": "start when available",
    "schema": "definition schema",
    "definition_version": "definition version",
}


def compare_definitions(expected, actual):
    """Every field that differs, as reasons. Empty list means identical."""
    reasons = []
    if not isinstance(actual, dict):
        return ["the installed task definition could not be read"]
    for field in sorted(set(expected) | set(actual)):
        want = expected.get(field)
        have = actual.get(field)
        if want == have:
            continue
        label = _FIELD_LABELS.get(field, field)
        reasons.append("%s differs from the registered definition (expected %r, "
                       "installed %r)" % (label, want, have))
    return reasons


# --------------------------------------------------------------------------
# task XML
# --------------------------------------------------------------------------
TASK_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{ns}">
  <RegistrationInfo>
    <Description>{description}</Description>
    <URI>{uri}</URI>
  </RegistrationInfo>
  <Triggers />
  <Principals>
    <Principal id="Author">
      <UserId>{user_sid}</UserId>
      <LogonType>{logon_type}</LogonType>
      <RunLevel>{run_level}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>{multiple_instances}</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>{no_batteries}</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>{stop_batteries}</StopIfGoingOnBatteries>
    <AllowHardTerminate>{hard_terminate}</AllowHardTerminate>
    <StartWhenAvailable>{start_when_available}</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>{network}</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>{on_demand}</AllowStartOnDemand>
    <Enabled>{enabled}</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>{only_if_idle}</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_directory}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

TASK_DESCRIPTION = ("SAITULS Secure Apps privileged helper. Fixed action, "
                    "started on demand by the medium-integrity Secure Apps "
                    "broker; performs BitLocker/VHDX and FIDO2 CTAP work only.")


def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _xml_bool(value):
    return "true" if value else "false"


def build_task_xml(user_sid=None, script_dir=None, host_image=None,
                   task_path=TASK_PATH, helper_path=None):
    """The XML registered by the elevated installer.

    Written here, in the same module that verifies it, so the definition that
    is registered and the definition that is checked cannot drift apart. The
    action names the *installed* protected helper, not the repository copy.
    """
    helper = helper_path or installed_helper_path()
    definition = expected_definition(user_sid=user_sid, script_dir=script_dir,
                                     host_image=host_image, task_path=task_path,
                                     helper_path=helper)
    return TASK_XML_TEMPLATE.format(
        ns=TASK_NAMESPACE,
        description=_xml_escape(TASK_DESCRIPTION),
        uri=_xml_escape(task_path),
        user_sid=_xml_escape(definition["user_sid"]),
        logon_type=_xml_escape(definition["logon_type"]),
        run_level=_xml_escape(definition["run_level"]),
        multiple_instances=_xml_escape(definition["multiple_instances"]),
        no_batteries=_xml_bool(definition["disallow_start_if_on_batteries"]),
        stop_batteries=_xml_bool(definition["stop_if_going_on_batteries"]),
        hard_terminate=_xml_bool(definition["allow_hard_terminate"]),
        start_when_available=_xml_bool(definition["start_when_available"]),
        network=_xml_bool(definition["run_only_if_network_available"]),
        on_demand=_xml_bool(definition["allow_start_on_demand"]),
        enabled=_xml_bool(definition["enabled"]),
        only_if_idle=_xml_bool(definition["run_only_if_idle"]),
        time_limit=_xml_escape(definition["execution_time_limit"]),
        command=_xml_escape(host_image or powershell_host()),
        arguments=_xml_escape(join_arguments(
            action_arguments(helper))),
        working_directory=_xml_escape(ntpath.dirname(helper)))


def _text(node, path, default=""):
    found = node.find(path) if node is not None else None
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _flag(node, path, default=False):
    raw = _text(node, path, "").lower()
    if raw in ("true", "1"):
        return True
    if raw in ("false", "0"):
        return False
    return default


def parse_task_xml(text, task_path=TASK_PATH, lookup=None):
    """Reduce a registered task's XML to the canonical definition.

    Only the security-relevant shape is extracted; everything else in the
    document is ignored on purpose (see :func:`canonical_definition`).
    """
    if not text or not str(text).strip():
        raise TaskMissing("the scheduled task returned no definition")
    document = str(text).lstrip("\ufeff")
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise TaskTampered("the registered task definition is not valid XML (%s)"
                           % type(exc).__name__)
    ns = "{%s}" % TASK_NAMESPACE
    if not root.tag.endswith("Task"):
        raise TaskTampered("the registered definition is not a Task document")

    principal = root.find("%sPrincipals/%sPrincipal" % (ns, ns))
    settings = root.find("%sSettings" % ns)
    actions = root.find("%sActions" % ns)
    execs = list(actions.findall("%sExec" % ns)) if actions is not None else []
    triggers_node = root.find("%sTriggers" % ns)
    triggers = ([child.tag.replace(ns, "") for child in list(triggers_node)]
                if triggers_node is not None else [])
    exec_node = execs[0] if execs else None
    action_count = len(list(actions)) if actions is not None else 0

    uri = _text(root, "%sRegistrationInfo/%sURI" % (ns, ns), task_path)
    return canonical_definition(
        resolve_user_sid(_text(principal, "%sUserId" % ns), lookup=lookup),
        None,
        host_image=_text(exec_node, "%sCommand" % ns),
        working_directory=_text(exec_node, "%sWorkingDirectory" % ns),
        task_path=uri or task_path,
        run_level=_text(principal, "%sRunLevel" % ns, "LeastPrivilege"),
        logon_type=_text(principal, "%sLogonType" % ns, ""),
        arguments=split_arguments(_text(exec_node, "%sArguments" % ns)),
        triggers=triggers,
        action_count=action_count,
        enabled=_flag(settings, "%sEnabled" % ns, True),
        allow_start_on_demand=_flag(settings, "%sAllowStartOnDemand" % ns, True),
        allow_hard_terminate=_flag(settings, "%sAllowHardTerminate" % ns, True),
        multiple_instances=_text(settings, "%sMultipleInstancesPolicy" % ns,
                                 "IgnoreNew"),
        execution_time_limit=_text(settings, "%sExecutionTimeLimit" % ns, "PT72H"),
        disallow_start_if_on_batteries=_flag(
            settings, "%sDisallowStartIfOnBatteries" % ns, True),
        stop_if_going_on_batteries=_flag(
            settings, "%sStopIfGoingOnBatteries" % ns, True),
        run_only_if_idle=_flag(settings, "%sRunOnlyIfIdle" % ns, False),
        run_only_if_network_available=_flag(
            settings, "%sRunOnlyIfNetworkAvailable" % ns, False),
        start_when_available=_flag(settings, "%sStartWhenAvailable" % ns, False))


# --------------------------------------------------------------------------
# schtasks transport
# --------------------------------------------------------------------------
class CommandResult(object):
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = int(returncode)
        self.stdout = stdout or ""
        self.stderr = stderr or ""


def _decode(raw):
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ""
    for encoding in ("utf-8", "utf-16-le", "mbcs", "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\x00" in text:
            continue
        return text
    return raw.decode("utf-8", "replace")


def run_schtasks(arguments, runner=None, timeout=60.0):
    """Run ``schtasks.exe`` at the caller's own integrity level.

    Never elevated at runtime: the whole point of the registered task is that
    a medium-integrity process may start it. Only the installer elevates, and
    it elevates a Secure Apps entry point, not this.
    """
    if runner is not None:
        return runner(list(arguments))
    executable = ntpath.join(os.environ.get("SystemRoot") or r"C:\Windows",
                             "System32", "schtasks.exe")
    if not os.path.isfile(executable):
        executable = "schtasks.exe"
    try:
        completed = subprocess.run([executable] + list(arguments),
                                   capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise TaskUnavailable("schtasks.exe is not available on this host")
    except subprocess.TimeoutExpired:
        raise TaskUnavailable("schtasks.exe did not answer in time")
    return CommandResult(completed.returncode, _decode(completed.stdout),
                         _decode(completed.stderr))


def query_task_xml(task_path=TASK_PATH, runner=None):
    """The registered XML, or None when the task does not exist."""
    result = run_schtasks(["/Query", "/TN", task_path, "/XML"], runner=runner)
    if result.returncode != 0:
        return None
    text = result.stdout or ""
    start = text.find("<")
    return text[start:] if start >= 0 else None


def task_exists(task_path=TASK_PATH, runner=None):
    result = run_schtasks(["/Query", "/TN", task_path], runner=runner)
    return result.returncode == 0


def start_task(task_path=TASK_PATH, runner=None):
    """``schtasks /Run``. The one runtime privilege operation. No mutation.

    Note what is NOT here: ``/Create``, ``/Change`` and ``/Delete``. Ordinary
    unlock and lock may start the already-approved task and nothing else.
    """
    result = run_schtasks(["/Run", "/TN", task_path], runner=runner)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        raise TaskUnavailable(
            "Task Scheduler refused to start the privileged helper task %s (%s)"
            % (task_path, message[0] if message else "exit %d" % result.returncode))
    return True


def stop_task(task_path=TASK_PATH, runner=None):
    result = run_schtasks(["/End", "/TN", task_path], runner=runner)
    return result.returncode == 0


def delete_task(task_path=TASK_PATH, runner=None):
    result = run_schtasks(["/Delete", "/TN", task_path, "/F"], runner=runner)
    return result.returncode == 0


def create_task(xml_path, task_path=TASK_PATH, runner=None):
    result = run_schtasks(["/Create", "/TN", task_path, "/XML", xml_path, "/F"],
                          runner=runner)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        raise PrivilegeTaskError(
            "the privileged helper task could not be registered (%s)"
            % (message[0] if message else "exit %d" % result.returncode))
    return True


def task_run_state(task_path=TASK_PATH, runner=None):
    """``Ready`` / ``Running`` / ``Disabled`` / None. Diagnostics only."""
    result = run_schtasks(["/Query", "/TN", task_path, "/FO", "LIST"],
                          runner=runner)
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        if ":" in line and line.split(":", 1)[0].strip().lower() == "status":
            return line.split(":", 1)[1].strip()
    return None


# --------------------------------------------------------------------------
# runtime bundle manifest
# --------------------------------------------------------------------------
#: The public, non-secret bundle manifest fields, in the order they are
#: serialised. ``bundle_fingerprint`` is derived from the others and excluded
#: from its own computation.
RUNTIME_MANIFEST_BODY = ("schema", "runtime_version", "helper_filename",
                         "helper_sha256", "fido_worker_bundle_dir",
                         "fido_worker_filename", "fido_worker_sha256",
                         "fido_worker_bundle_fingerprint",
                         "fido_worker_file_count", "task_action_path")
RUNTIME_MANIFEST_FIELDS = RUNTIME_MANIFEST_BODY + ("bundle_fingerprint",)


def canonical_runtime_manifest(manifest):
    """The canonical body (fingerprint field excluded), sorted keys, no space."""
    body = {name: manifest.get(name) for name in RUNTIME_MANIFEST_BODY}
    return canonical_json(body)


def runtime_bundle_fingerprint(manifest):
    """SHA-256 over the canonical manifest body. None when unbuildable.

    Deliberately over the *content* -- helper name+digest, worker name+digest,
    task action path -- not the file bytes, so it is reproducible from the
    manifest alone and moves whenever any bound value moves.
    """
    if not isinstance(manifest, dict):
        return None
    try:
        payload = canonical_runtime_manifest(manifest)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_runtime_manifest(runtime_root, helper_sha256=None,
                           worker_sha256=None, source_dir=None,
                           worker_bundle_fingerprint=None,
                           worker_file_count=None):
    """Describe the protected runtime that lives under *runtime_root*.

    Digests are measured from the installed files unless supplied (the
    installer measures them in staging before committing). The task action
    path is the installed helper -- the exact ``-File`` the fixed task runs.

    ``fido_worker_bundle_fingerprint`` binds the WHOLE onedir worker tree by
    :func:`bundle_fingerprint`, so a swapped DLL inside ``_internal`` is as
    fatal as a swapped ``sa_fido_worker.exe``. An unmeasurable bundle leaves
    the field None, which can never match a pinned value.
    """
    helper = installed_helper_path(runtime_root)
    worker = installed_worker_exe_path(runtime_root)
    if worker_bundle_fingerprint is None or worker_file_count is None:
        measured, count, _problem = bundle_fingerprint(
            installed_worker_bundle_dir(runtime_root))
        if worker_bundle_fingerprint is None:
            worker_bundle_fingerprint = measured
        if worker_file_count is None:
            worker_file_count = count
    manifest = {
        "schema": RUNTIME_MANIFEST_SCHEMA,
        "runtime_version": PRIVILEGED_RUNTIME_VERSION,
        "helper_filename": HELPER_SCRIPT,
        "helper_sha256": helper_sha256 if helper_sha256 is not None
                         else file_digest(helper),
        "fido_worker_bundle_dir": FIDO_BUNDLE_DIR_NAME,
        "fido_worker_filename": FROZEN_WORKER_NAME,
        "fido_worker_sha256": worker_sha256 if worker_sha256 is not None
                             else file_digest(worker),
        "fido_worker_bundle_fingerprint": worker_bundle_fingerprint,
        "fido_worker_file_count": worker_file_count,
        "task_action_path": normalize_path(helper),
    }
    manifest["bundle_fingerprint"] = runtime_bundle_fingerprint(manifest)
    return manifest


def save_runtime_manifest(manifest, runtime_root=None, path=None):
    """Write the manifest atomically. Elevated callers only."""
    target = path or runtime_manifest_path(runtime_root)
    unknown = sorted(set(manifest) - set(RUNTIME_MANIFEST_FIELDS))
    if unknown:
        raise PrivilegeTaskError("refusing to persist runtime-manifest field(s) "
                                 "outside the allowlist: %s" % ", ".join(unknown))
    document = {name: manifest.get(name) for name in RUNTIME_MANIFEST_FIELDS}
    directory = ntpath.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


def load_runtime_manifest(runtime_root=None, path=None):
    """``(manifest, problem)``. Exactly one is None. Verifies its own digest."""
    target = path or runtime_manifest_path(runtime_root)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None, ("the protected runtime manifest is missing (%s): %s"
                      % (target, PRIVILEGED_HELPER_NOT_INSTALLED))
    except (OSError, ValueError):
        return None, "the protected runtime manifest at %s is unreadable" % target
    if not isinstance(document, dict):
        return None, "the protected runtime manifest at %s is not an object" % target
    if document.get("schema") != RUNTIME_MANIFEST_SCHEMA:
        return None, ("the runtime manifest uses schema %r; this implementation "
                      "requires %r" % (document.get("schema"), RUNTIME_MANIFEST_SCHEMA))
    missing = [name for name in RUNTIME_MANIFEST_FIELDS if name not in document]
    if missing:
        return None, ("the runtime manifest is incomplete (missing %s)"
                      % ", ".join(sorted(missing)))
    if document.get("bundle_fingerprint") != runtime_bundle_fingerprint(document):
        return None, "the runtime manifest bundle fingerprint does not match its body"
    return document, None


# --------------------------------------------------------------------------
# pin record
# --------------------------------------------------------------------------
PIN_FIELDS = ("schema", "pin_version", "created_at", "launch_mode", "task_path",
              "definition_version", "definition_fingerprint", "user_sid",
              "pipe_name", "helper_host_image", "runtime_root",
              "runtime_bundle_fingerprint", "helper_path", "helper_sha256",
              "fido_worker_bundle_dir", "fido_worker_bundle_fingerprint",
              "fido_worker_exe_path", "fido_worker_exe_sha256",
              "privileged_tmp_root", "idle_timeout_seconds")


def build_pin(runtime_root=None, user_sid=None, host_image=None,
              task_path=TASK_PATH, idle_timeout_seconds=None, timestamp=None,
              manifest=None):
    """The elevated half of the fixed action, bound to the installed runtime.

    Every executable path resolves inside *runtime_root*: the installed
    protected helper and the frozen ``sa_fido_worker.exe``. No interpreter is
    recorded any more, because no interpreter is on the elevated runtime path
    -- the worker is a frozen executable inside the protected directory.
    """
    root = runtime_root or privileged_root()
    sid = user_sid or current_user_sid()
    helper = installed_helper_path(root)
    worker = installed_worker_exe_path(root)
    bundle = installed_worker_bundle_dir(root)
    manifest = manifest or build_runtime_manifest(root)
    timeout = int(idle_timeout_seconds or DEFAULT_IDLE_TIMEOUT_SECONDS)
    timeout = max(MIN_IDLE_TIMEOUT_SECONDS, min(MAX_IDLE_TIMEOUT_SECONDS, timeout))
    definition = expected_definition(user_sid=sid, host_image=host_image,
                                     task_path=task_path, helper_path=helper)
    return {
        "schema": PIN_SCHEMA,
        "pin_version": PIN_VERSION,
        "created_at": timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "launch_mode": LAUNCH_MODE,
        "task_path": task_path,
        "definition_version": DEFINITION_VERSION,
        "definition_fingerprint": definition_fingerprint(definition),
        "user_sid": sid,
        "pipe_name": pipe_name_for_sid(sid),
        "helper_host_image": host_image or powershell_host(),
        "runtime_root": normalize_path(root),
        "runtime_bundle_fingerprint": manifest.get("bundle_fingerprint"),
        "helper_path": helper,
        "helper_sha256": manifest.get("helper_sha256"),
        "fido_worker_bundle_dir": bundle,
        "fido_worker_bundle_fingerprint":
            manifest.get("fido_worker_bundle_fingerprint"),
        "fido_worker_exe_path": worker,
        "fido_worker_exe_sha256": manifest.get("fido_worker_sha256"),
        "privileged_tmp_root": normalize_path(privileged_tmp_root()),
        "idle_timeout_seconds": timeout,
    }


def load_pin(path=None):
    """``(pin, problem)``. Exactly one of the two is None."""
    target = path or pin_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None, ("the privileged helper is not installed (no pin record at "
                      "%s): %s" % (target, PRIVILEGED_HELPER_NOT_INSTALLED))
    except (OSError, ValueError):
        return None, "the privileged helper pin record at %s is unreadable" % target
    if not isinstance(document, dict):
        return None, "the privileged helper pin record at %s is not an object" % target
    if document.get("schema") != PIN_SCHEMA:
        return None, ("the pin record uses schema %r; this implementation "
                      "requires %r" % (document.get("schema"), PIN_SCHEMA))
    missing = [name for name in PIN_FIELDS if name not in document]
    if missing:
        return None, ("the pin record is incomplete (missing %s)"
                      % ", ".join(sorted(missing)))
    return document, None


def save_pin(pin, path=None):
    """Write the pin atomically. Elevated callers only -- see :func:`harden_pin`."""
    target = path or pin_path()
    unknown = sorted(set(pin) - set(PIN_FIELDS))
    if unknown:
        raise PrivilegeTaskError("refusing to persist pin field(s) outside the "
                                 "allowlist: %s" % ", ".join(unknown))
    directory = ntpath.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    document = {name: pin.get(name) for name in PIN_FIELDS}
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


def _icacls():
    return ntpath.join(os.environ.get("SystemRoot") or r"C:\Windows",
                       "System32", "icacls.exe")


def _run_icacls(arguments, runner=None):
    if runner is not None:
        return runner([_icacls()] + arguments)
    try:
        completed = subprocess.run([_icacls()] + arguments,
                                   capture_output=True, timeout=60)
        return CommandResult(completed.returncode, _decode(completed.stdout),
                             _decode(completed.stderr))
    except (OSError, subprocess.TimeoutExpired):
        return CommandResult(1, "", "icacls did not run")


def harden_pin(path=None, runner=None, user_sid=None):
    """Admins and SYSTEM write and OWN it; the intended user only reads.

    The pin names what an elevated process will run. If a medium-integrity
    process could write it, the fixed task action would stop being fixed -- so
    the ACL is set explicitly rather than inherited from whatever
    ``%ProgramData%`` happens to grant.

    A DACL alone is not enough. An object's OWNER holds READ_CONTROL and
    WRITE_DAC implicitly and can therefore hand itself write access at any
    time, so ownership of both the pin and its directory is moved to
    Administrators. Read access goes to the intended user's own SID rather
    than to ``Authenticated Users``: every account on the machine is
    authenticated, and only one of them owns this vault.
    """
    target = path or pin_path()
    sid = user_sid or current_user_sid()
    reader = ("*%s" % sid) if sid else "*S-1-5-11"
    directory = ntpath.dirname(target)
    commands = []
    if directory:
        commands.append([directory, "/inheritance:r",
                         "/grant:r", "*S-1-5-32-544:(OI)(CI)F",
                         "/grant:r", "*S-1-5-18:(OI)(CI)F",
                         "/grant:r", "%s:(OI)(CI)R" % reader])
        commands.append([directory, "/setowner", "*S-1-5-32-544"])
    commands.append([target, "/inheritance:r",
                     "/grant:r", "*S-1-5-32-544:F",
                     "/grant:r", "*S-1-5-18:F",
                     "/grant:r", "%s:R" % reader])
    commands.append([target, "/setowner", "*S-1-5-32-544"])
    ok = True
    for arguments in commands:
        ok = _run_icacls(arguments, runner=runner).returncode == 0 and ok
    return ok


def harden_privileged_tmp(tmp_root=None, user_sid=None, runner=None):
    """The protected scratch root: SYSTEM and Administrators do the work.

    The medium user gets exactly two rights on the ROOT and nothing else:
    ``READ_CONTROL`` and ``FILE_READ_ATTRIBUTES``. Not read, not list, not
    traverse into a transaction directory, and certainly not write -- only
    enough to MEASURE the descriptor. That is not a convenience: the
    medium-integrity broker verifies this boundary before every silent
    privileged start, and a directory whose security descriptor the broker
    cannot even open measures as "unknown", which fails closed and takes the
    whole installation down with it. A boundary the defender cannot inspect
    is not a stronger boundary.

    The grant is deliberately NOT inheritable, and every per-transaction
    directory blocks inheritance anyway, so the DiskPart script an elevated
    process is about to run stays unreachable: the medium user cannot open
    it, cannot list the directory holding it, and cannot rewrite it between
    the moment it is written and the moment DiskPart opens it.
    """
    root = tmp_root or privileged_tmp_root()
    sid = user_sid or current_user_sid()
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        return False
    arguments = [root, "/inheritance:r",
                 "/grant:r", "*S-1-5-18:(OI)(CI)F",
                 "/grant:r", "*S-1-5-32-544:(OI)(CI)F"]
    if sid:
        arguments += ["/grant:r", "*%s:(RC,RA)" % sid]
    ok = _run_icacls(arguments, runner=runner).returncode == 0
    ok = _run_icacls([root, "/setowner", "*S-1-5-32-544", "/T", "/C"],
                     runner=runner).returncode == 0 and ok
    return ok


def harden_runtime_dir(runtime_root=None, user_sid=None, runner=None):
    """The explicit ACL that makes the runtime directory a boundary.

    SYSTEM and Administrators full control (inherited to every child), the
    intended user read+execute (inherited), everybody else nothing, and
    inheritance from ``%ProgramData%`` removed. A medium-integrity account may
    therefore run the installed helper and the frozen worker but can neither
    replace nor edit any of them, nor the manifest. Owner is set to
    Administrators so the user cannot rewrite the DACL through ownership.
    """
    root = runtime_root or privileged_root()
    sid = user_sid or current_user_sid()
    if not sid:
        return False
    arguments = [root, "/inheritance:r",
                 "/grant:r", "*S-1-5-18:(OI)(CI)F",
                 "/grant:r", "*S-1-5-32-544:(OI)(CI)F",
                 "/grant:r", "*%s:(OI)(CI)RX" % sid]
    ok = _run_icacls(arguments, runner=runner).returncode == 0
    ok = _run_icacls([root, "/setowner", "*S-1-5-32-544", "/T", "/C"],
                     runner=runner).returncode == 0 and ok
    return ok


#: Rights that mean "this account can change the object": write-data, append,
#: write-EA, write-attributes, and the standard rights that let it replace the
#: object (DELETE, WRITE_DAC, WRITE_OWNER), plus the two generic bits that
#: appear in an unmapped ACE.
#:
#: What is deliberately NOT here: the COMPOSITE rights FILE_GENERIC_WRITE
#: (0x120116) and FILE_ALL_ACCESS (0x1F01FF). Both of them carry READ_CONTROL
#: and SYNCHRONIZE, and this mask is tested with a bitwise AND -- so including
#: them made an ordinary read+execute ACE (0x1200A9, which also carries
#: READ_CONTROL and SYNCHRONIZE) match, and every correctly hardened runtime
#: measured as "writable by a medium account". An account that really holds
#: FILE_ALL_ACCESS holds every specific bit below anyway, so nothing is lost
#: by naming only the rights that actually mean "can change it".
def _writable_rights():
    import ntsecuritycon
    return (ntsecuritycon.FILE_WRITE_DATA | ntsecuritycon.FILE_APPEND_DATA
            | ntsecuritycon.FILE_WRITE_EA | ntsecuritycon.FILE_WRITE_ATTRIBUTES
            | ntsecuritycon.GENERIC_WRITE | ntsecuritycon.GENERIC_ALL
            | ntsecuritycon.DELETE | ntsecuritycon.WRITE_DAC
            | ntsecuritycon.WRITE_OWNER)


def _dacl_control_rights():
    """Rights that let an account REWRITE the object's own security.

    Specific bits only, for the same reason as :func:`_writable_rights`:
    FILE_ALL_ACCESS carries READ_CONTROL, and an account that truly holds
    FILE_ALL_ACCESS holds WRITE_DAC and WRITE_OWNER inside it regardless.
    """
    import ntsecuritycon
    return (ntsecuritycon.WRITE_DAC | ntsecuritycon.WRITE_OWNER
            | ntsecuritycon.GENERIC_ALL)


def _privileged_sids(win32security):
    return {
        str(win32security.ConvertSidToStringSid(
            win32security.CreateWellKnownSid(sid_kind)))
        for sid_kind in (win32security.WinBuiltinAdministratorsSid,
                         win32security.WinLocalSystemSid,
                         win32security.WinCreatorOwnerSid)}


def _non_privileged_holds(path, rights):
    """True when a non-privileged SID holds any of *rights* on *path*.

    Privileged SIDs (Administrators, SYSTEM, CREATOR OWNER) are ignored -- they
    are supposed to own the runtime. Any *other* SID with a matching allow ACE
    means the protected-runtime rule is not being enforced. None off Windows /
    when the DACL cannot be read: an unreadable ACL is never reported as safe.
    """
    if os.name != "nt" or not os.path.exists(path):
        return None
    try:
        import win32security
        descriptor = win32security.GetFileSecurity(
            path, win32security.DACL_SECURITY_INFORMATION)
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:            # a null DACL grants everyone everything
            return True
        privileged = _privileged_sids(win32security)
        for index in range(dacl.GetAceCount()):
            (ace_type, _flags), mask, sid = dacl.GetAce(index)
            if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE:
                continue
            if not (mask & rights):
                continue
            if str(win32security.ConvertSidToStringSid(sid)) in privileged:
                continue
            return True
        return False
    except Exception:
        return None


def path_is_writable_by_user(path):
    """True when a non-privileged account holds any write/replace right."""
    try:
        rights = _writable_rights()
    except Exception:
        return None
    return _non_privileged_holds(path, rights)


def path_dacl_is_changeable_by_user(path):
    """True when a non-privileged account could REWRITE the object's DACL."""
    try:
        rights = _dacl_control_rights()
    except Exception:
        return None
    return _non_privileged_holds(path, rights)


def path_owner_sid(path):
    """The object's owner SID as text, or None when it cannot be read."""
    if os.name != "nt" or not os.path.exists(path):
        return None
    try:
        import win32security
        descriptor = win32security.GetFileSecurity(
            path, win32security.OWNER_SECURITY_INFORMATION)
        owner = descriptor.GetSecurityDescriptorOwner()
        if owner is None:
            return None
        return str(win32security.ConvertSidToStringSid(owner))
    except Exception:
        return None


def path_owner_is_privileged(path):
    """True when Administrators or SYSTEM owns *path*. None when unreadable.

    The owner of an object can always rewrite its DACL, so "who owns it" is
    part of the boundary, not a cosmetic detail. An unreadable owner is never
    reported as safe.
    """
    owner = path_owner_sid(path)
    if owner is None:
        return None
    return owner.upper() in ("S-1-5-32-544", "S-1-5-18")


def tree_is_writable_by_user(root, limit=None):
    """True when ANY file or directory in the tree is medium-writable.

    Walks the whole protected bundle: an inherited ACL is not evidence that no
    child carries an explicit ACE of its own. Short-circuits on the first
    writable object; None when any object could not be measured.
    """
    if os.name != "nt":
        return None
    if not os.path.isdir(root):
        return None
    bound = limit or MAX_BUNDLE_FILES
    seen = 0
    pending = [ntpath.normpath(str(root))]
    unmeasurable = False
    while pending:
        current = pending.pop()
        writable = path_is_writable_by_user(current)
        if writable is True:
            return True
        if writable is None:
            unmeasurable = True
        try:
            names = os.listdir(current)
        except OSError:
            unmeasurable = True
            continue
        for name in names:
            full = ntpath.join(current, name)
            seen += 1
            if seen > bound:
                return None
            if os.path.isdir(full):
                pending.append(full)
                continue
            writable = path_is_writable_by_user(full)
            if writable is True:
                return True
            if writable is None:
                unmeasurable = True
    return None if unmeasurable else False


def pin_is_writable_by_user(path=None):
    """True when a non-administrator could rewrite the pin. Diagnostics."""
    target = path or pin_path()
    if os.name != "nt" or not os.path.isfile(target):
        return None
    return path_is_writable_by_user(target)


def pin_acl_report(path=None):
    """Whether the pin is owned and locked down the way the boundary needs.

    Healthy is ``pin_owner_valid`` True with ``pin_medium_writable`` and
    ``pin_medium_can_change_dacl`` both False. An owner that cannot be read,
    or a DACL that cannot be measured, is reported as unhealthy rather than
    as unknown: this is a gate, and it fails closed.
    """
    target = path or pin_path()
    directory = ntpath.dirname(target)
    report = {
        "pin_path": normalize_path(target),
        "pin_present": os.path.isfile(target),
        "pin_owner": None,
        "pin_owner_valid": False,
        "pin_medium_writable": None,
        "pin_medium_can_change_dacl": None,
        "pin_dir_owner_valid": False,
        "pin_dir_medium_writable": None,
        "pin_acl_valid": False,
    }
    if os.name != "nt":
        return report
    if not report["pin_present"]:
        return report
    report["pin_owner"] = path_owner_sid(target)
    report["pin_owner_valid"] = path_owner_is_privileged(target) is True
    report["pin_medium_writable"] = path_is_writable_by_user(target)
    report["pin_medium_can_change_dacl"] = path_dacl_is_changeable_by_user(target)
    if directory and os.path.isdir(directory):
        report["pin_dir_owner_valid"] = path_owner_is_privileged(directory) is True
        report["pin_dir_medium_writable"] = path_is_writable_by_user(directory)
    report["pin_acl_valid"] = bool(
        report["pin_owner_valid"]
        and report["pin_medium_writable"] is False
        and report["pin_medium_can_change_dacl"] is False
        and report["pin_dir_owner_valid"]
        and report["pin_dir_medium_writable"] is False)
    return report


def privileged_tmp_report(tmp_root=None):
    """Whether the privileged scratch root is safe to hand DiskPart a file in."""
    root = tmp_root or privileged_tmp_root()
    report = {
        "privileged_tmp_root": normalize_path(root),
        "privileged_tmp_present": os.path.isdir(root) if os.name == "nt" else None,
        "privileged_tmp_medium_writable": None,
        "privileged_tmp_owner_valid": False,
        "privileged_tmp_reparse_point": (contains_reparse_point(root)
                                         if os.name == "nt" else None),
        "privileged_tmp_valid": False,
    }
    if os.name != "nt" or not report["privileged_tmp_present"]:
        return report
    report["privileged_tmp_medium_writable"] = path_is_writable_by_user(root)
    report["privileged_tmp_owner_valid"] = path_owner_is_privileged(root) is True
    report["privileged_tmp_valid"] = bool(
        report["privileged_tmp_medium_writable"] is False
        and report["privileged_tmp_owner_valid"]
        and report["privileged_tmp_reparse_point"] is False)
    return report


def runtime_acl_report(runtime_root=None, user_sid=None):
    """Whether the protected runtime's ACL enforces the boundary. Measures.

    Returns public booleans only. ``runtime_acl_valid`` is True only when the
    directory and each protected artifact exist and none of them is writable,
    replaceable or deletable by a non-privileged account -- and False (never
    None) as soon as any of them is, so an unmeasurable ACL fails closed at
    the call site.
    """
    root = runtime_root or privileged_root()
    bundle = installed_worker_bundle_dir(root)
    report = {
        "runtime_root": normalize_path(root),
        "runtime_root_present": os.path.isdir(root) if os.name == "nt" else None,
        "runtime_dir_medium_writable": None,
        "helper_medium_writable": None,
        "fido_worker_medium_writable": None,
        "fido_worker_bundle_dir": normalize_path(bundle),
        "fido_worker_bundle_present": (os.path.isdir(bundle)
                                       if os.name == "nt" else None),
        "fido_worker_bundle_medium_writable": None,
        "manifest_medium_writable": None,
        "runtime_owner_valid": False,
        "reparse_point_on_path": (contains_reparse_point(root)
                                  if os.name == "nt" else None),
        "runtime_acl_valid": False,
    }
    if os.name != "nt":
        return report
    targets = {
        "runtime_dir_medium_writable": root,
        "helper_medium_writable": installed_helper_path(root),
        "fido_worker_medium_writable": installed_worker_exe_path(root),
        "manifest_medium_writable": runtime_manifest_path(root),
    }
    measurable = True
    for key, target in targets.items():
        writable = path_is_writable_by_user(target)
        report[key] = writable
        if writable is not False:
            measurable = False
    # The onedir bundle is a TREE: every DLL and every Python extension module
    # inside it is loaded by an elevated process, so the whole tree is walked
    # rather than trusting the root ACL to have been inherited.
    bundle_writable = tree_is_writable_by_user(bundle)
    report["fido_worker_bundle_medium_writable"] = bundle_writable
    if bundle_writable is not False:
        measurable = False
    report["runtime_owner_valid"] = path_owner_is_privileged(root) is True
    report["runtime_acl_valid"] = bool(
        report["runtime_root_present"] and report["fido_worker_bundle_present"]
        and measurable and report["runtime_owner_valid"]
        and report["reparse_point_on_path"] is False)
    return report


# --------------------------------------------------------------------------
# scheduled-task security descriptor
# --------------------------------------------------------------------------
# The task DEFINITION fingerprint answers "what does it run"; it is NOT an
# ACL. A medium account that could /Change the task's action, principal, run
# level or DACL, or /Delete it, would defeat the whole boundary regardless of
# how tightly the definition is fingerprinted. So the elevated installer also
# writes an explicit task security descriptor: SYSTEM and Administrators full
# control, the intended user read + run only, nobody the owner but the
# Administrators group, and the DACL protected from inheritance.
SECURITY_OWNER_INFORMATION = 0x00000001
SECURITY_GROUP_INFORMATION = 0x00000002
SECURITY_DACL_INFORMATION = 0x00000004
#: ``TASK_DONT_ADD_PRINCIPAL_ACE``. Without it Task Scheduler answers a
#: ``SetSecurityDescriptor`` by appending its OWN ACE for the account the task
#: runs as -- and, in doing so, drops the SE_DACL_PROTECTED flag the supplied
#: SDDL carried. The descriptor then reads back as ``D:(A;;FA;;;SY)...
#: (A;;FR;;;<user>)`` with no ``P``: an unprotected DACL plus a grant nobody
#: asked for. With the flag, the descriptor round-trips as ``D:PAI`` with
#: exactly the three ACEs :func:`build_task_sddl` names.
TASK_DONT_ADD_PRINCIPAL_ACE = 0x10

TASK_SD_INFORMATION = (SECURITY_OWNER_INFORMATION | SECURITY_GROUP_INFORMATION
                       | SECURITY_DACL_INFORMATION)

_GENERIC_ALL = 0x10000000
_GENERIC_EXECUTE = 0x20000000
_GENERIC_WRITE = 0x40000000
_GENERIC_READ = 0x80000000
_FILE_ALL = 0x001F01FF
_FILE_GENERIC_READ = 0x00120089
_FILE_GENERIC_WRITE = 0x00120116
_FILE_GENERIC_EXECUTE = 0x001200A0
_FILE_EXECUTE = 0x00000020
_DELETE = 0x00010000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
#: Any of these on a user ACE means "can change the task".
_MODIFY_MASK = (0x00000002 | 0x00000004 | 0x00000010 | 0x00000100  # write data/append/EA/attrs
                | _WRITE_DAC | _WRITE_OWNER)

#: SDDL rights tokens -> access mask. Only the ones our ACEs ever use, plus
#: the file/generic set Task Scheduler may hand back when it expands them.
_SDDL_RIGHTS = {
    "GA": _GENERIC_ALL, "GR": _GENERIC_READ, "GW": _GENERIC_WRITE,
    "GX": _GENERIC_EXECUTE, "FA": _FILE_ALL, "FR": _FILE_GENERIC_READ,
    "FW": _FILE_GENERIC_WRITE, "FX": _FILE_GENERIC_EXECUTE,
    "SD": _DELETE, "WD": _WRITE_DAC, "WO": _WRITE_OWNER,
    "RC": 0x00020000, "CC": 0x00000001, "DC": 0x00000002, "LC": 0x00000004,
    "SW": 0x00000008, "RP": 0x00000010, "WP": 0x00000020, "DT": 0x00000040,
    "LO": 0x00000080, "CR": 0x00000100, "GXGR": _GENERIC_READ | _GENERIC_EXECUTE,
}

#: SDDL SID aliases we emit or expect, resolved to concrete SIDs so a report
#: means the same thing whether Windows kept the alias or expanded it.
_SDDL_SID_ALIAS = {"SY": "S-1-5-18", "BA": "S-1-5-32-544", "BU": "S-1-5-32-545"}


def build_task_sddl(user_sid):
    """The security descriptor the installer sets on the registered task.

    ``O:BAG:BA`` -- owned by Administrators, so the user cannot rewrite the
    DACL through ownership. ``D:P`` -- protected, no inherited ACEs. SYSTEM and
    Administrators ``GA`` (all); the user ``GRGX`` (read + run) and nothing
    that could modify, disable, replace or delete the task.
    """
    sid = str(user_sid or "").strip()
    return "O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;%s)" % sid


def _map_generic(mask):
    out = int(mask)
    if out & _GENERIC_ALL:
        out |= _FILE_ALL
    if out & _GENERIC_READ:
        out |= _FILE_GENERIC_READ
    if out & _GENERIC_WRITE:
        out |= _FILE_GENERIC_WRITE
    if out & _GENERIC_EXECUTE:
        out |= _FILE_GENERIC_EXECUTE
    return out


def _parse_sddl_rights(text):
    """One SDDL rights field -> access mask. Hex (``0x..``) or 2-letter tokens."""
    text = (text or "").strip()
    if not text:
        return 0
    if text.lower().startswith("0x"):
        try:
            return int(text, 16)
        except ValueError:
            return 0
    mask = 0
    for i in range(0, len(text) - 1, 2):
        mask |= _SDDL_RIGHTS.get(text[i:i + 2].upper(), 0)
    return mask


def _resolve_sddl_sid(text):
    text = (text or "").strip().upper()
    return _SDDL_SID_ALIAS.get(text, text)


def parse_sddl(sddl):
    """``(owner_sid, dacl_protected, {sid: mapped_mask})`` from an SDDL string.

    A small, deterministic parser for the ACE shapes this module uses, so the
    task security descriptor can be analysed without Windows. Generic rights
    are mapped to their file-generic equivalents, so ``GR``/``GX`` and an
    expanded hex mask compare equal.
    """
    owner = None
    protected = False
    aces = {}
    if not sddl:
        return owner, protected, aces
    text = str(sddl)
    owner_match = re.search(r"O:([^\s:GD]+)", text)
    if owner_match:
        owner = _resolve_sddl_sid(owner_match.group(1))
    dacl_match = re.search(r"D:([A-Z]*)(\(.*)?$", text)
    dacl_flags = dacl_match.group(1) if dacl_match else ""
    protected = "P" in dacl_flags
    for ace in re.findall(r"\(([^)]*)\)", text):
        parts = ace.split(";")
        if len(parts) < 6 or parts[0].strip().upper() not in ("A", "AU"):
            continue
        if parts[0].strip().upper() != "A":
            continue
        mask = _map_generic(_parse_sddl_rights(parts[2]))
        sid = _resolve_sddl_sid(parts[5])
        aces[sid] = aces.get(sid, 0) | mask
    return owner, protected, aces


def task_security_fingerprint(sddl, user_sid=None):
    """SHA-256 over the SD's *effective* meaning, not its text.

    Owner, DACL-protected flag and the per-SID mapped masks, canonically
    serialised -- so reordered ACEs or an alias vs an expanded mask do not look
    like tampering, but any real change to who-can-do-what does. None when the
    SD is empty/unparseable, which fails closed.
    """
    if not sddl:
        return None
    owner, protected, aces = parse_sddl(sddl)
    if not aces:
        return None
    document = {
        "owner": owner,
        "protected": bool(protected),
        "aces": sorted("%s=%d" % (sid, mask) for sid, mask in aces.items()),
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def analyze_task_security(sddl, user_sid):
    """The public task-security facts, computed from an SDDL string.

    Pure and deterministic: the Windows-only step is *getting* the SDDL off
    the registered task; deciding what it means is done here so it can be
    tested without a task, and so an unreadable descriptor becomes a
    fail-closed verdict rather than an exception.
    """
    report = {
        "task_security_valid": False,
        "task_medium_user_can_run": None,
        "task_medium_user_can_modify": None,
        "task_medium_user_can_delete": None,
        "task_security_fingerprint": None,
        "reasons": [],
    }
    if not sddl:
        report["reasons"].append("the task security descriptor could not be read")
        return report
    owner, protected, aces = parse_sddl(sddl)
    if not aces:
        report["reasons"].append("the task security descriptor has no usable DACL")
        return report
    report["task_security_fingerprint"] = task_security_fingerprint(sddl)
    sid = str(user_sid or "").upper()
    user_mask = aces.get(sid, 0)
    system_mask = aces.get("S-1-5-18", 0)
    admins_mask = aces.get("S-1-5-32-544", 0)
    report["task_medium_user_can_run"] = bool(user_mask & _FILE_EXECUTE)
    report["task_medium_user_can_modify"] = bool(user_mask & _MODIFY_MASK)
    report["task_medium_user_can_delete"] = bool(user_mask & _DELETE)
    reasons = report["reasons"]
    if not protected:
        reasons.append("the task DACL is not protected from inheritance")
    if not (system_mask & _FILE_ALL) == _FILE_ALL:
        reasons.append("SYSTEM does not have full control of the task")
    if not (admins_mask & _FILE_ALL) == _FILE_ALL:
        reasons.append("Administrators do not have full control of the task")
    if owner not in ("S-1-5-32-544", "S-1-5-18"):
        reasons.append("the task owner is %r, not Administrators/SYSTEM" % owner)
    if sid and not report["task_medium_user_can_run"]:
        reasons.append("the intended user cannot run the task")
    if report["task_medium_user_can_modify"]:
        reasons.append("the intended user can modify the task")
    if report["task_medium_user_can_delete"]:
        reasons.append("the intended user can delete the task")
    report["task_security_valid"] = not reasons
    return report


def _task_service():
    import win32com.client
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    return service


#: The Task Scheduler security descriptor is reachable ONLY through the Task
#: Scheduler COM API (``IRegisteredTask::GetSecurityDescriptor`` /
#: ``SetSecurityDescriptor``); ``schtasks.exe`` cannot read or write it. That
#: API is normally driven through pywin32, but pywin32's COM layer is an
#: optional third-party dependency that can be broken, half-upgraded or
#: stripped by security software on a perfectly healthy host -- and a
#: privileged installer that cannot apply the task ACL because an optional
#: package is damaged fails closed on something that has nothing to do with
#: the boundary. So the SAME COM API is also reachable through Windows
#: PowerShell, at the pinned System32 path the task action itself names, with
#: no third-party code in the path at all.
_TASK_SD_SCRIPT_READ = """
$ErrorActionPreference = 'Stop'
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('%(folder)s')
$task = $folder.GetTask('%(name)s')
[Console]::Out.Write($task.GetSecurityDescriptor(%(information)d))
"""

_FOLDER_SD_SCRIPT_READ = """
$ErrorActionPreference = 'Stop'
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('%(folder)s')
[Console]::Out.Write($folder.GetSecurityDescriptor(%(information)d))
"""

_FOLDER_SD_SCRIPT_WRITE = """
$ErrorActionPreference = 'Stop'
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('%(folder)s')
$folder.SetSecurityDescriptor('%(sddl)s', %(flags)d)
"""

_TASK_SD_SCRIPT_WRITE = """
$ErrorActionPreference = 'Stop'
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('%(folder)s')
$task = $folder.GetTask('%(name)s')
$task.SetSecurityDescriptor('%(sddl)s', %(flags)d)
"""


def _ps_literal(text):
    """A PowerShell single-quoted literal: only the quote itself is special."""
    return str(text).replace("'", "''")


def _run_powershell_script(script, timeout=60.0):
    """Run *script* through the pinned Windows PowerShell. ``(ok, stdout)``.

    ``-EncodedCommand`` carries the script as base64 UTF-16LE, so no quoting,
    escaping or shell parsing stands between this process and what runs.
    """
    import base64
    host = powershell_host()
    if not os.path.isfile(host):
        return False, ""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
            [host, "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return completed.returncode == 0, _decode(completed.stdout)


def _task_sd_parts(task_path):
    folder_path, _sep, name = str(task_path).rpartition("\\")
    return {"folder": _ps_literal(folder_path or "\\"), "name": _ps_literal(name)}


def get_task_security_descriptor(task_path=TASK_PATH):
    """The registered task's SDDL, or None when it cannot be read.

    None is a fail-closed answer: a descriptor that cannot be measured cannot
    be trusted, and every caller treats None as :data:`PRIVILEGED_TASK_SECURITY_INVALID`.
    """
    if os.name != "nt":
        return None
    folder_path, _sep, name = str(task_path).rpartition("\\")
    try:
        service = _task_service()
        folder = service.GetFolder(folder_path or "\\")
        task = folder.GetTask(name)
        return task.GetSecurityDescriptor(TASK_SD_INFORMATION)
    except Exception:
        pass
    parts = _task_sd_parts(task_path)
    parts["information"] = TASK_SD_INFORMATION
    ok, out = _run_powershell_script(_TASK_SD_SCRIPT_READ % parts)
    descriptor = (out or "").strip()
    return descriptor if ok and descriptor else None


def apply_task_security_descriptor(task_path=TASK_PATH, user_sid=None,
                                   runner=None):
    """Set the explicit task SD. **Runs elevated, during Install/Repair.**

    ``runner`` is a test seam: a callable ``(task_path, sddl) -> bool``. In
    production the descriptor is set through the Task Scheduler COM API
    (``IRegisteredTask::SetSecurityDescriptor``), never ``schtasks``, whose
    ``/Create`` default DACL this exists precisely not to trust.
    """
    sid = user_sid or current_user_sid()
    return set_task_security_descriptor(task_path, build_task_sddl(sid),
                                        runner=runner)


def set_task_security_descriptor(task_path=TASK_PATH, sddl=None, runner=None):
    """Write an explicit SDDL onto a registered task. **Runs elevated.**

    Split out of :func:`apply_task_security_descriptor` because a rollback
    must restore the descriptor the previous installation really had, not the
    one this implementation would compute now.
    """
    if not sddl:
        return False
    if runner is not None:
        return bool(runner(task_path, sddl))
    if os.name != "nt":
        return False
    folder_path, _sep, name = str(task_path).rpartition("\\")
    try:
        service = _task_service()
        folder = service.GetFolder(folder_path or "\\")
        task = folder.GetTask(name)
        task.SetSecurityDescriptor(sddl, TASK_DONT_ADD_PRINCIPAL_ACE)
        return True
    except Exception:
        pass
    parts = _task_sd_parts(task_path)
    parts["sddl"] = _ps_literal(sddl)
    parts["flags"] = TASK_DONT_ADD_PRINCIPAL_ACE
    ok, _out = _run_powershell_script(_TASK_SD_SCRIPT_WRITE % parts)
    return bool(ok)


def get_task_folder_security_descriptor(folder_path=TASK_FOLDER):
    """The task FOLDER's SDDL, or None. Fail-closed, like the task's own.

    The folder matters as much as the task: deleting a registered task needs
    write access to the folder that contains it, not to the task object. A
    task whose own descriptor denies DELETE can still be deleted out of a
    folder that inherited the Task Scheduler root's permissive ACL -- which is
    exactly what a real medium-integrity ``schtasks /Delete`` proved.
    """
    if os.name != "nt":
        return None
    try:
        service = _task_service()
        folder = service.GetFolder(folder_path or "\\")
        return folder.GetSecurityDescriptor(TASK_SD_INFORMATION)
    except Exception:
        pass
    ok, out = _run_powershell_script(
        _FOLDER_SD_SCRIPT_READ % {"folder": _ps_literal(folder_path or "\\"),
                                  "information": TASK_SD_INFORMATION})
    descriptor = (out or "").strip()
    return descriptor if ok and descriptor else None


def set_task_folder_security_descriptor(folder_path=TASK_FOLDER, sddl=None,
                                        runner=None):
    """Write an explicit SDDL onto the task folder. **Runs elevated.**"""
    if not sddl:
        return False
    if runner is not None:
        return bool(runner(folder_path, sddl))
    if os.name != "nt":
        return False
    try:
        service = _task_service()
        folder = service.GetFolder(folder_path or "\\")
        folder.SetSecurityDescriptor(sddl, TASK_DONT_ADD_PRINCIPAL_ACE)
        return True
    except Exception:
        pass
    ok, _out = _run_powershell_script(
        _FOLDER_SD_SCRIPT_WRITE % {"folder": _ps_literal(folder_path or "\\"),
                                   "sddl": _ps_literal(sddl),
                                   "flags": TASK_DONT_ADD_PRINCIPAL_ACE})
    return bool(ok)


def apply_task_folder_security_descriptor(folder_path=TASK_FOLDER, user_sid=None,
                                          runner=None):
    """Give the folder the same explicit descriptor as the task itself."""
    sid = user_sid or current_user_sid()
    return set_task_folder_security_descriptor(folder_path,
                                               build_task_sddl(sid),
                                               runner=runner)


def analyze_task_folder_security(sddl, user_sid):
    """The folder half of the boundary: read yes, create/delete no."""
    report = {
        "folder_security_valid": False,
        "folder_medium_user_can_read": None,
        "folder_medium_user_can_write": None,
        "folder_security_fingerprint": None,
        "reasons": [],
    }
    if not sddl:
        report["reasons"].append("the task folder security descriptor could not "
                                 "be read")
        return report
    owner, protected, aces = parse_sddl(sddl)
    if not aces:
        report["reasons"].append("the task folder security descriptor has no "
                                 "usable DACL")
        return report
    report["folder_security_fingerprint"] = task_security_fingerprint(sddl)
    sid = str(user_sid or "").upper()
    user_mask = aces.get(sid, 0)
    report["folder_medium_user_can_read"] = bool(user_mask & _FILE_GENERIC_READ)
    report["folder_medium_user_can_write"] = bool(user_mask & (_MODIFY_MASK | _DELETE))
    reasons = report["reasons"]
    if not protected:
        reasons.append("the task folder DACL is not protected from inheritance, "
                       "so the Task Scheduler root's permissive ACL still "
                       "decides who may delete tasks in it")
    if (aces.get("S-1-5-18", 0) & _FILE_ALL) != _FILE_ALL:
        reasons.append("SYSTEM does not have full control of the task folder")
    if (aces.get("S-1-5-32-544", 0) & _FILE_ALL) != _FILE_ALL:
        reasons.append("Administrators do not have full control of the task folder")
    if owner not in ("S-1-5-32-544", "S-1-5-18"):
        reasons.append("the task folder owner is %r, not Administrators/SYSTEM"
                       % owner)
    if report["folder_medium_user_can_write"]:
        reasons.append("the intended user can create or delete tasks in the "
                       "privileged task folder")
    report["folder_security_valid"] = not reasons
    return report


def task_security_report(task_path=TASK_PATH, user_sid=None, sddl=None,
                         folder_sddl=None, folder_path=None):
    """Measure the registered task AND its folder. Fails closed on both."""
    sid = user_sid or current_user_sid()
    descriptor = sddl if sddl is not None else get_task_security_descriptor(task_path)
    report = analyze_task_security(descriptor, sid)
    report["task_path"] = task_path
    folder = folder_path or (str(task_path).rpartition("\\")[0] or TASK_FOLDER)
    report["task_folder"] = folder
    if folder_sddl is None and sddl is None:
        folder_sddl = get_task_folder_security_descriptor(folder)
    folder_report = analyze_task_folder_security(folder_sddl, sid)
    report.update({name: value for name, value in folder_report.items()
                   if name != "reasons"})
    report["reasons"] = list(report["reasons"]) + list(folder_report["reasons"])
    # A task nobody may change, in a folder anybody may empty, is not
    # protected. Both halves decide the one verdict.
    report["task_security_valid"] = bool(report["task_security_valid"]
                                         and folder_report["folder_security_valid"])
    if report["task_security_fingerprint"] and report["folder_security_fingerprint"]:
        report["task_security_fingerprint"] = hashlib.sha256(
            ("%s|%s" % (report["task_security_fingerprint"],
                        report["folder_security_fingerprint"])
             ).encode("utf-8")).hexdigest()
    else:
        report["task_security_fingerprint"] = None
    return report


def installed_task_security_fingerprint(task_path=None, runner=None,
                                        pin_file=None):
    """The registered task's SD fingerprint, or None. For the acceptance record."""
    if os.name != "nt":
        return None
    pin, _problem = load_pin(pin_file)
    path = task_path or (pin or {}).get("task_path") or TASK_PATH
    sid = (pin or {}).get("user_sid") or current_user_sid()
    return task_security_report(path, user_sid=sid).get("task_security_fingerprint")


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------
class TaskVerdict(object):
    """Is the silent elevation mechanism installed, intact and runnable?"""

    __slots__ = ("ok", "installed", "reasons", "token", "definition",
                 "fingerprint", "pin", "run_state", "runtime_acl",
                 "task_security", "pin_acl", "privileged_tmp")

    def __init__(self, ok, installed, reasons, token="", definition=None,
                 fingerprint=None, pin=None, run_state=None, runtime_acl=None,
                 task_security=None, pin_acl=None, privileged_tmp=None):
        self.ok = bool(ok)
        self.installed = bool(installed)
        self.reasons = list(reasons)
        self.token = token
        self.definition = definition
        self.fingerprint = fingerprint
        self.pin = pin
        self.run_state = run_state
        self.runtime_acl = runtime_acl or {}
        self.task_security = task_security or {}
        self.pin_acl = pin_acl or {}
        self.privileged_tmp = privileged_tmp or {}

    def to_dict(self):
        pin = self.pin if isinstance(self.pin, dict) else {}
        return {
            "ok": self.ok,
            "installed": self.installed,
            "token": self.token or (SILENT_PRIVILEGED_START_ACCEPTED
                                    if self.ok else ""),
            "task_path": pin.get("task_path", TASK_PATH),
            "launch_mode": pin.get("launch_mode", LAUNCH_MODE),
            "definition_fingerprint": self.fingerprint,
            "pinned_fingerprint": pin.get("definition_fingerprint"),
            "runtime_root": pin.get("runtime_root"),
            "runtime_bundle_fingerprint": pin.get("runtime_bundle_fingerprint"),
            "runtime_acl_valid": self.runtime_acl.get("runtime_acl_valid"),
            "fido_worker_bundle_fingerprint":
                pin.get("fido_worker_bundle_fingerprint"),
            "pin_owner_valid": self.pin_acl.get("pin_owner_valid"),
            "pin_medium_writable": self.pin_acl.get("pin_medium_writable"),
            "pin_medium_can_change_dacl":
                self.pin_acl.get("pin_medium_can_change_dacl"),
            "privileged_tmp_valid":
                self.privileged_tmp.get("privileged_tmp_valid"),
            "task_security_valid": self.task_security.get("task_security_valid"),
            "task_security_fingerprint":
                self.task_security.get("task_security_fingerprint"),
            "idle_timeout_seconds": pin.get("idle_timeout_seconds"),
            "run_state": self.run_state,
            "reasons": list(self.reasons),
        }


def verify_installation(script_dir=None, runner=None, pin_file=None,
                        user_sid=None, expect=None, runtime_acl=None,
                        task_sddl=None, pin_acl=None, privileged_tmp=None):
    """Compare what is installed against what this installation expects.

    Fails closed on every doubt, and never repairs anything: anything that
    does not match is reported and left exactly as it is, because the repair
    is itself a privileged operation and the consent for it belongs to the
    user, not to the broker.

    ``runtime_acl``, ``pin_acl`` and ``privileged_tmp`` (report dicts) and
    ``task_sddl`` (an SDDL string) are test seams. In production, on Windows,
    the runtime-directory ACL, the pin's owner and DACL, the protected scratch
    root and the task security descriptor are all measured live and required;
    off Windows they are not a release gate (this is a Windows subsystem), so
    they are checked only when injected. Every executable path must resolve inside the pinned
    protected runtime root -- a reparse point or a path escape is refused.
    """
    pin, problem = load_pin(pin_file)
    if pin is None:
        return TaskVerdict(False, False, [problem],
                           token=PRIVILEGED_HELPER_NOT_INSTALLED)
    reasons = []
    token_override = None
    sid = user_sid or current_user_sid()
    pin_sid = pin.get("user_sid")
    if sid and pin_sid and str(pin_sid).upper() != str(sid).upper():
        reasons.append("the privileged helper was installed for a different user "
                       "account than the one running now")
    task_path = pin.get("task_path") or TASK_PATH
    root = pin.get("runtime_root")
    helper = pin.get("helper_path")
    worker = pin.get("fido_worker_exe_path")
    bundle = pin.get("fido_worker_bundle_dir")

    # Every elevated artifact must resolve strictly inside the protected
    # runtime root. A pin that points the task action or the worker at the
    # source tree -- or anywhere a medium account can write -- is refused, and
    # so is a reparse point that would smuggle "inside" past a normpath.
    if not root:
        reasons.append("the pin records no protected runtime root")
    else:
        if not path_is_within(root, helper, allow_root=False):
            reasons.append("the pinned helper is not inside the protected runtime "
                           "root: %s" % helper)
        if not path_is_within(root, worker, allow_root=False):
            reasons.append("the pinned FIDO worker is not inside the protected "
                           "runtime root: %s" % worker)
        if not path_is_within(root, bundle, allow_root=False):
            reasons.append("the pinned FIDO worker bundle is not inside the "
                           "protected runtime root: %s" % bundle)
        elif not path_is_within(bundle, worker, allow_root=False):
            reasons.append("the pinned FIDO worker is not inside its own "
                           "protected bundle: %s" % worker)
        if os.name == "nt" and contains_reparse_point(root) is True:
            reasons.append("the protected runtime root is reached through a "
                           "reparse point")

    expected = expect or expected_definition(
        user_sid=pin_sid or sid, host_image=pin.get("helper_host_image"),
        task_path=task_path, helper_path=helper)
    expected_digest = definition_fingerprint(expected)
    if pin.get("definition_fingerprint") != expected_digest:
        reasons.append("the pinned task definition does not describe this "
                       "installation any more (helper path, host or principal "
                       "changed)")

    if not helper or not os.path.isfile(helper):
        reasons.append("the installed privileged helper is missing: %s" % helper)
    else:
        digest = file_digest(helper)
        if digest is None or digest != pin.get("helper_sha256"):
            reasons.append("the installed privileged helper does not match the "
                           "fingerprint recorded at installation")
    if not worker or not os.path.isfile(worker):
        reasons.append("the installed frozen FIDO worker is missing: %s" % worker)
    else:
        digest = file_digest(worker)
        if digest is None or digest != pin.get("fido_worker_exe_sha256"):
            reasons.append("the installed frozen FIDO worker does not match the "
                           "fingerprint recorded at installation")

    # The onedir worker bundle is measured RECURSIVELY and compared with the
    # value pinned at installation: a modified, added or deleted file anywhere
    # inside the protected bundle moves it.
    if bundle:
        measured, count, bundle_problem = bundle_fingerprint(bundle)
        if measured is None:
            reasons.append("the installed FIDO worker bundle could not be "
                           "measured: %s (%s)"
                           % (bundle_problem, PRIVILEGED_WORKER_BUNDLE_INVALID))
        elif measured != pin.get("fido_worker_bundle_fingerprint"):
            reasons.append("the installed FIDO worker bundle does not match the "
                           "recursive fingerprint recorded at installation "
                           "(%d file(s) measured): %s"
                           % (count or 0, PRIVILEGED_WORKER_BUNDLE_INVALID))

    # The runtime bundle manifest binds helper + worker bundle + task action;
    # its own fingerprint must match the one pinned at installation.
    manifest, manifest_problem = load_runtime_manifest(runtime_root=root)
    if manifest is None:
        reasons.append(manifest_problem)
    else:
        if manifest.get("bundle_fingerprint") != pin.get("runtime_bundle_fingerprint"):
            reasons.append("the runtime bundle fingerprint does not match the one "
                           "pinned at installation")
        if manifest.get("helper_sha256") != pin.get("helper_sha256") or \
                manifest.get("fido_worker_sha256") != pin.get("fido_worker_exe_sha256"):
            reasons.append("the runtime manifest disagrees with the pin about the "
                           "helper or worker fingerprint")
        if manifest.get("fido_worker_bundle_fingerprint") != \
                pin.get("fido_worker_bundle_fingerprint"):
            reasons.append("the runtime manifest disagrees with the pin about the "
                           "FIDO worker bundle fingerprint: %s"
                           % PRIVILEGED_WORKER_BUNDLE_INVALID)

    # The pin: owner AND DACL. An owner holds WRITE_DAC implicitly, so a pin
    # the interactive user owns is unprotected however tight its DACL reads.
    if pin_acl is None:
        pin_acl = pin_acl_report(pin_file) if os.name == "nt" else {}
    if pin_acl and pin_acl.get("pin_acl_valid") is not True:
        detail = []
        if pin_acl.get("pin_owner_valid") is not True:
            detail.append("owner %s is not Administrators/SYSTEM"
                          % (pin_acl.get("pin_owner") or "unreadable"))
        if pin_acl.get("pin_medium_writable") is not False:
            detail.append("a non-administrator can write it")
        if pin_acl.get("pin_medium_can_change_dacl") is not False:
            detail.append("a non-administrator can rewrite its DACL")
        if pin_acl.get("pin_dir_owner_valid") is not True or \
                pin_acl.get("pin_dir_medium_writable") is not False:
            detail.append("its directory is not owned and locked down")
        reasons.append("the pin record is not protected on this host (%s): %s"
                       % ("; ".join(detail) or "unmeasurable",
                          PRIVILEGED_PIN_OWNER_INVALID))
        token_override = PRIVILEGED_PIN_OWNER_INVALID

    # The privileged scratch root: where an elevated process writes the
    # command material it is about to execute.
    tmp_report = privileged_tmp
    if tmp_report is None:
        tmp_report = privileged_tmp_report() if os.name == "nt" else {}
    if tmp_report and tmp_report.get("privileged_tmp_valid") is not True:
        reasons.append("the privileged scratch directory is missing or writable "
                       "by a medium-integrity account (%s): %s"
                       % (tmp_report.get("privileged_tmp_root"),
                          PRIVILEGED_SCRATCH_INVALID))
        token_override = PRIVILEGED_SCRATCH_INVALID

    # The runtime directory ACL: the medium user may read+execute but never
    # write, replace, rename or delete an elevated artifact.
    acl = runtime_acl
    if acl is None and os.name == "nt" and root:
        acl = runtime_acl_report(runtime_root=root, user_sid=sid)
    if acl is not None and acl.get("runtime_acl_valid") is not True:
        reasons.append("the protected runtime directory ACL does not deny "
                       "medium-integrity writes: %s" % PRIVILEGED_RUNTIME_ACL_INVALID)
        token_override = PRIVILEGED_RUNTIME_ACL_INVALID

    xml_text = query_task_xml(task_path, runner=runner)
    if not xml_text:
        reasons.append("the scheduled task %s is not registered" % task_path)
        return TaskVerdict(False, False, reasons, token=PRIVILEGED_TASK_MISSING,
                           pin=pin, runtime_acl=acl or {}, pin_acl=pin_acl,
                           privileged_tmp=tmp_report)
    try:
        installed = parse_task_xml(xml_text, task_path=task_path)
    except PrivilegeTaskError as exc:
        reasons.append(str(exc))
        return TaskVerdict(False, True, reasons, token=PRIVILEGED_TASK_TAMPERED,
                           pin=pin, runtime_acl=acl or {}, pin_acl=pin_acl,
                           privileged_tmp=tmp_report)
    installed_digest = definition_fingerprint(installed)
    differences = compare_definitions(expected, installed)
    if differences:
        reasons.extend(differences)
    elif installed_digest != pin.get("definition_fingerprint"):
        reasons.append("the registered task definition no longer matches the "
                       "fingerprint pinned at installation")
    # The action the task really runs must resolve inside the protected root.
    installed_args = installed.get("arguments") or []
    action_target = ""
    if "-File" in installed_args:
        idx = installed_args.index("-File")
        if idx + 1 < len(installed_args):
            action_target = installed_args[idx + 1]
    if root and action_target and not path_is_within(root, action_target,
                                                     allow_root=False):
        reasons.append("the registered task action does not point inside the "
                       "protected runtime: %s" % action_target)

    # The task security descriptor: the medium user may run/query but never
    # modify, replace, disable, delete or re-own the task.
    sd_report = None
    if task_sddl is not None:
        sd_report = analyze_task_security(task_sddl, pin_sid or sid)
    elif os.name == "nt":
        sd_report = task_security_report(task_path, user_sid=pin_sid or sid)
    if sd_report is not None and sd_report.get("task_security_valid") is not True:
        reasons.append("the scheduled task security descriptor does not deny "
                       "medium-integrity modification: %s; %s"
                       % (PRIVILEGED_TASK_SECURITY_INVALID,
                          "; ".join(sd_report.get("reasons") or [])))
        token_override = PRIVILEGED_TASK_SECURITY_INVALID

    state = task_run_state(task_path, runner=runner)
    if state and state.strip().lower() == "disabled":
        reasons.append("the privileged helper task is disabled")
    token = ""
    if reasons:
        token = token_override or PRIVILEGED_TASK_TAMPERED
    return TaskVerdict(not reasons, True, reasons, token=token,
                       definition=installed, fingerprint=installed_digest,
                       pin=pin, run_state=state, runtime_acl=acl or {},
                       task_security=sd_report or {}, pin_acl=pin_acl,
                       privileged_tmp=tmp_report)


def installed_fingerprint(runner=None, pin_file=None):
    """The registered task's fingerprint, or None. For the acceptance record."""
    pin, _problem = load_pin(pin_file)
    task_path = (pin or {}).get("task_path") or TASK_PATH
    xml_text = query_task_xml(task_path, runner=runner)
    if not xml_text:
        return None
    try:
        return definition_fingerprint(parse_task_xml(xml_text, task_path=task_path))
    except PrivilegeTaskError:
        return None


# --------------------------------------------------------------------------
# install / repair / remove
# --------------------------------------------------------------------------
def preflight(script_dir=None, runner=None, pin_file=None):
    """Everything the UAC diagnostics report needs, measured now.

    Reports; decides nothing. A particular UAC slider position is deliberately
    NOT required: this feature works while ordinary UAC prompts stay on for
    unrelated software.
    """
    verdict = verify_installation(script_dir=script_dir, runner=runner,
                                  pin_file=pin_file)
    pin = verdict.pin if isinstance(verdict.pin, dict) else {}
    facts = uac_policy_facts()
    elevated = process_is_elevated()
    report = {
        "EnableLUA": facts["EnableLUA"],
        "ConsentPromptBehaviorAdmin": facts["ConsentPromptBehaviorAdmin"],
        "PromptOnSecureDesktop": facts["PromptOnSecureDesktop"],
        "broker_integrity": ("HIGH" if elevated else "MEDIUM")
                            if os.name == "nt" else "N/A",
        "broker_elevated": elevated,
        "user_is_administrator": user_is_administrator(),
        "admin_approval_mode": (facts["EnableLUA"] is True
                                and user_is_administrator() is True),
        "scheduled_helper_installed": verdict.installed,
        "scheduled_helper_definition_valid": verdict.ok,
        "scheduled_helper_runnable": bool(verdict.ok and (verdict.run_state or "")
                                          .strip().lower() != "disabled"),
        "scheduled_task_state": verdict.run_state,
        "task_path": pin.get("task_path", TASK_PATH),
        "launch_mode": pin.get("launch_mode", LAUNCH_MODE),
        "definition_fingerprint": verdict.fingerprint,
        "pinned_fingerprint": pin.get("definition_fingerprint"),
        "pin_path": pin_file or pin_path(),
        "pin_writable_by_user": pin_is_writable_by_user(pin_file),
        "pin_owner": verdict.pin_acl.get("pin_owner"),
        "pin_owner_valid": verdict.pin_acl.get("pin_owner_valid"),
        "pin_medium_writable": verdict.pin_acl.get("pin_medium_writable"),
        "pin_medium_can_change_dacl":
            verdict.pin_acl.get("pin_medium_can_change_dacl"),
        "privileged_tmp_root": verdict.privileged_tmp.get("privileged_tmp_root",
                                                          privileged_tmp_root()),
        "privileged_tmp_valid": verdict.privileged_tmp.get("privileged_tmp_valid"),
        "runtime_root": pin.get("runtime_root", privileged_root()),
        "runtime_bundle_fingerprint": pin.get("runtime_bundle_fingerprint"),
        "fido_worker_bundle_dir": pin.get("fido_worker_bundle_dir"),
        "fido_worker_bundle_fingerprint":
            pin.get("fido_worker_bundle_fingerprint"),
        "fido_worker_bundle_medium_writable":
            verdict.runtime_acl.get("fido_worker_bundle_medium_writable"),
        "runtime_acl_valid": verdict.runtime_acl.get("runtime_acl_valid"),
        "runtime_owner_valid": verdict.runtime_acl.get("runtime_owner_valid"),
        "runtime_dir_medium_writable":
            verdict.runtime_acl.get("runtime_dir_medium_writable"),
        "task_security_valid": verdict.task_security.get("task_security_valid"),
        "task_security_fingerprint":
            verdict.task_security.get("task_security_fingerprint"),
        "task_medium_user_can_run":
            verdict.task_security.get("task_medium_user_can_run"),
        "task_medium_user_can_modify":
            verdict.task_security.get("task_medium_user_can_modify"),
        "task_medium_user_can_delete":
            verdict.task_security.get("task_medium_user_can_delete"),
        "idle_timeout_seconds": pin.get("idle_timeout_seconds",
                                        DEFAULT_IDLE_TIMEOUT_SECONDS),
        "helper_integrity": None,
        "fido_worker_integrity": None,
        "pipe_authentication": None,
        "reasons": list(verdict.reasons),
        "token": verdict.token,
    }
    return report


#: Where the elevated installer looks for the frozen worker BUNDLE to install,
#: in order. ``build_frozen_worker.ps1`` writes the ``--onedir`` tree next to
#: the source; a packaged build may place it under ``dist``.
def frozen_worker_bundle_candidates(script_dir=None, worker_bundle=None):
    root = helper_root(script_dir)
    candidates = []
    if worker_bundle:
        candidates.append(worker_bundle)
    candidates.append(ntpath.join(root, FIDO_BUNDLE_DIR_NAME))
    candidates.append(ntpath.join(root, "dist", FIDO_BUNDLE_DIR_NAME))
    return candidates


def locate_frozen_worker_bundle(script_dir=None, worker_bundle=None):
    """The onedir ``fido-worker`` tree to install, or None when unbuilt."""
    for candidate in frozen_worker_bundle_candidates(script_dir, worker_bundle):
        if candidate and os.path.isdir(candidate) and os.path.isfile(
                ntpath.join(candidate, FROZEN_WORKER_NAME)):
            return candidate
    return None


# --------------------------------------------------------------------------
# privileged scratch
# --------------------------------------------------------------------------
def new_privileged_scratch_dir(prefix="tx", tmp_root=None, runner=None,
                              user_sid=None):
    """A fresh, hardened, per-transaction directory under the scratch root.

    Privileged command material -- the task XML ``schtasks /Create`` reads and
    the DiskPart script an elevated DiskPart executes -- goes here. Never
    ``%TEMP%``: that directory belongs to the medium-integrity user, and an
    elevated process that executes a file from it has handed the choice of
    what it does to whoever can write that file.
    """
    root = tmp_root or privileged_tmp_root()
    if not harden_privileged_tmp(tmp_root=root, user_sid=user_sid, runner=runner):
        raise PrivilegeTaskError("the privileged scratch root could not be "
                                 "hardened: %s" % PRIVILEGED_SCRATCH_INVALID)
    directory = ntpath.join(root, "%s-%d-%d" % (prefix, os.getpid(),
                                                int(time.time() * 1000) % 1000000))
    if os.path.isdir(directory):
        _force_rmtree(directory)
    os.makedirs(directory, exist_ok=False)
    if _run_icacls([directory, "/inheritance:r",
                    "/grant:r", "*S-1-5-18:(OI)(CI)F",
                    "/grant:r", "*S-1-5-32-544:(OI)(CI)F"],
                   runner=runner).returncode != 0:
        _quiet_rmtree(directory)
        raise PrivilegeTaskError("the privileged scratch directory could not be "
                                 "hardened: %s" % PRIVILEGED_SCRATCH_INVALID)
    return directory


def set_privileged_owner(path, runner=None):
    """Move an object's OWNER to Administrators. **Runs elevated.**

    An elevated process creates objects owned by the ACCOUNT, not by
    Administrators, unless the machine's default-owner policy says otherwise --
    and an owner holds WRITE_DAC implicitly. A scratch file left owned by the
    interactive user could therefore be re-permissioned by that same user at
    medium integrity, however tight its inherited DACL is.
    """
    return _run_icacls([path, "/setowner", "*S-1-5-32-544"],
                       runner=runner).returncode == 0


def assert_privileged_scratch_file(path, tmp_root=None):
    """Refuse to hand an elevated consumer a file it is not safe to execute.

    Three questions, all of which must be answered before the file is used:
    does it still resolve inside the protected scratch root, did a reparse
    point redirect it out of that root, and can a medium-integrity account
    write it? Any doubt is :data:`PRIVILEGED_SCRATCH_INVALID`.
    """
    root = tmp_root or privileged_tmp_root()
    if not path_is_within(root, path, allow_root=False):
        raise PrivilegeTaskError(
            "the privileged command file is not inside the protected scratch "
            "root: %s (%s)" % (path, PRIVILEGED_SCRATCH_INVALID))
    if os.name == "nt" and contains_reparse_point(path, stop_at=root) is True:
        raise PrivilegeTaskError(
            "the privileged command file is reached through a reparse point: "
            "%s (%s)" % (path, PRIVILEGED_SCRATCH_INVALID))
    if os.name == "nt" and path_is_writable_by_user(path) is not False:
        raise PrivilegeTaskError(
            "the privileged command file is writable by a medium-integrity "
            "account: %s (%s)" % (path, PRIVILEGED_SCRATCH_INVALID))
    if os.name == "nt" and path_dacl_is_changeable_by_user(path) is not False:
        raise PrivilegeTaskError(
            "a medium-integrity account can rewrite the privileged command "
            "file's DACL: %s (%s)" % (path, PRIVILEGED_SCRATCH_INVALID))
    if os.name == "nt" and path_owner_is_privileged(path) is not True:
        raise PrivilegeTaskError(
            "the privileged command file is not owned by Administrators or "
            "SYSTEM: %s (%s)" % (path, PRIVILEGED_SCRATCH_INVALID))
    return True


def _write_privileged_task_xml(xml_text, scratch_dir):
    """The task XML ``schtasks /Create`` will read, inside protected scratch."""
    path = ntpath.join(scratch_dir, "privtask-%d.xml" % os.getpid())
    with open(path, "wb") as handle:
        handle.write(xml_text.encode("utf-16"))
        handle.flush()
        os.fsync(handle.fileno())
    set_privileged_owner(path)
    assert_privileged_scratch_file(path, tmp_root=ntpath.dirname(scratch_dir))
    return path


def _register_task_xml(xml_text, task_path, runner=None, tmp_root=None):
    """Register *xml_text* as *task_path*. True when schtasks accepted it."""
    scratch = new_privileged_scratch_dir(prefix="restore", tmp_root=tmp_root)
    try:
        xml_file = _write_privileged_task_xml(xml_text, scratch)
        create_task(xml_file, task_path=task_path, runner=runner)
        return True
    except (PrivilegeTaskError, OSError):
        return False
    finally:
        _quiet_rmtree(scratch)


def set_task_enabled(task_path=TASK_PATH, enabled=True, runner=None):
    """``schtasks /Change /ENABLE|/DISABLE``. Used only to restore state."""
    flag = "/ENABLE" if enabled else "/DISABLE"
    return run_schtasks(["/Change", "/TN", task_path, flag],
                        runner=runner).returncode == 0


def _stage_runtime(staging_dir, source_helper, source_bundle):
    """Copy the runtime into *staging_dir* and return its verified manifest.

    Hashes are measured from the staged bytes, not the source, and the
    manifest is built from those -- so a copy that silently truncated, lost a
    file or gained one is caught before anything is committed.
    """
    import shutil
    os.makedirs(staging_dir, exist_ok=True)
    staged_helper = installed_helper_path(staging_dir)
    staged_bundle = installed_worker_bundle_dir(staging_dir)
    # Measure the SOURCE bundle first: a reparse point inside it would make
    # copytree copy whatever it points at, and the fingerprint would then bind
    # bytes that never lived in the bundle.
    source_entries, problem = bundle_file_entries(source_bundle)
    if source_entries is None:
        raise PrivilegeTaskError("the FIDO worker bundle to install is not "
                                 "acceptable: %s" % problem, "config_invalid")
    shutil.copyfile(source_helper, staged_helper)
    shutil.copytree(source_bundle, staged_bundle, symlinks=True)
    helper_digest = file_digest(staged_helper)
    if helper_digest is None or helper_digest != file_digest(source_helper):
        raise PrivilegeTaskError("the staged helper does not match its source")
    staged_entries, problem = bundle_file_entries(staged_bundle)
    if staged_entries is None:
        raise PrivilegeTaskError("the staged FIDO worker bundle could not be "
                                 "measured: %s" % problem)
    if staged_entries != source_entries:
        raise PrivilegeTaskError("the staged FIDO worker bundle does not match "
                                 "its source (%d vs %d file(s))"
                                 % (len(staged_entries), len(source_entries)))
    fingerprint = hashlib.sha256(
        canonical_bundle_document(staged_entries).encode("utf-8")).hexdigest()
    worker_digest = file_digest(installed_worker_exe_path(staging_dir))
    if worker_digest is None:
        raise PrivilegeTaskError("the staged FIDO worker executable is missing")
    manifest = build_runtime_manifest(staging_dir, helper_sha256=helper_digest,
                                      worker_sha256=worker_digest,
                                      worker_bundle_fingerprint=fingerprint,
                                      worker_file_count=len(staged_entries))
    save_runtime_manifest(manifest, runtime_root=staging_dir)
    return manifest


def _replace_dir(staging_dir, runtime_root, backup_dir):
    """Move any existing runtime aside, then swap staging into place.

    As close to atomic as a directory move is on Windows: the previous runtime
    is renamed to *backup_dir* (kept for rollback) and the staged tree is
    renamed onto the canonical path. A rename within one volume is atomic; the
    two-step leaves at most a missing directory, never a half-written one.
    """
    if os.path.isdir(backup_dir):
        _force_rmtree(backup_dir)
    had_previous = os.path.isdir(runtime_root)
    if had_previous:
        os.replace(runtime_root, backup_dir)
    try:
        os.replace(staging_dir, runtime_root)
    except OSError:
        if had_previous and not os.path.isdir(runtime_root):
            os.replace(backup_dir, runtime_root)      # put the old one back
        raise
    return had_previous


def _force_rmtree(path):
    import shutil
    def _on_error(func, target, _exc):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_on_error)


def _quiet_rmtree(path):
    try:
        if os.path.isdir(path):
            _force_rmtree(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# the install transaction
# --------------------------------------------------------------------------
#: Every point at which :func:`apply_install` mutates the machine, in order.
#: ``tests/test_secure_apps_privtask.py`` injects a fault after each of them
#: and proves the same two properties every time: a FIRST install leaves no
#: runnable privileged task, no pin and no protected runtime behind, and a
#: REPAIR leaves the previous valid installation exactly as it was.
INSTALL_STAGES = (
    "stage_copy",
    "runtime_acl_apply",
    "runtime_acl_measure",
    "runtime_swap",
    "task_register",
    "task_sd_apply",
    "pin_save",
    "pin_harden",
    "final_verify",
    "commission",
)


class InstallFault(PrivilegeTaskError):
    """A deliberately injected transaction fault. Test seam only."""


def _fault(fault, stage):
    """Raise when the caller asked for a fault after *stage*. Tests only."""
    if not fault:
        return
    stages = {fault} if isinstance(fault, str) else set(fault)
    unknown = sorted(stages - set(INSTALL_STAGES))
    if unknown:
        raise PrivilegeTaskError("unknown install stage(s) requested for fault "
                                 "injection: %s" % ", ".join(unknown))
    if stage in stages:
        raise InstallFault("injected transaction fault after stage %r" % stage,
                           "internal")


def _snapshot_installation(task_path, pin_file=None, runtime_root=None,
                           runner=None):
    """Everything the rollback target needs, measured BEFORE any mutation.

    For a repair this is the previous valid installation. For a first install
    every flag is False, and that is itself the rollback target: no task, no
    pin, no privileged runtime.
    """
    root = runtime_root or privileged_root()
    target = pin_file or pin_path()
    snapshot = {
        "task_present": False,
        "task_xml": None,
        "task_sddl": None,
        "task_state": None,
        "pin_present": False,
        "pin_bytes": None,
        "runtime_present": os.path.isdir(root),
    }
    xml_text = query_task_xml(task_path, runner=runner)
    if xml_text:
        snapshot["task_present"] = True
        snapshot["task_xml"] = xml_text
        snapshot["task_state"] = task_run_state(task_path, runner=runner)
        snapshot["task_sddl"] = get_task_security_descriptor(task_path)
    try:
        with open(target, "rb") as handle:
            snapshot["pin_bytes"] = handle.read()
        snapshot["pin_present"] = True
    except OSError:
        pass
    return snapshot


def _restore_installation(root, backup_dir, staging_dir, task_path, snapshot,
                          runner=None, pin_file=None, task_sd_runner=None,
                          user_sid=None):
    """Put the machine back exactly where the transaction found it.

    Returns ``(restored, reasons)``. ``restored`` is True only when the
    rollback target was reached AND re-verified: for a repair, the previous
    installation verifies again; for a first install, no task, no pin and no
    protected runtime are left. Anything less is REPAIR_REQUIRED at the call
    site, and a runnable task is never left pointing at a runtime whose ACL
    was not proven.
    """
    reasons = []
    ok = True
    target = pin_file or pin_path()

    # 1. Nothing runnable may survive a failed transaction, so the task goes
    #    first -- before the runtime it points at is touched again.
    try:
        stop_task(task_path, runner=runner)
    except PrivilegeTaskError:
        pass
    try:
        if task_exists(task_path, runner=runner):
            if not delete_task(task_path, runner=runner):
                ok = False
                reasons.append("the partially installed privileged task could "
                               "not be deleted")
    except PrivilegeTaskError as exc:
        ok = False
        reasons.append("the privileged task could not be inspected (%s)" % exc)

    # 2. The runtime directory.
    try:
        if os.path.isdir(staging_dir):
            _force_rmtree(staging_dir)
    except OSError:
        reasons.append("the staging directory could not be removed")
    if snapshot.get("runtime_present"):
        if os.path.isdir(backup_dir):
            try:
                if os.path.isdir(root):
                    _force_rmtree(root)
                os.replace(backup_dir, root)
            except OSError:
                ok = False
                reasons.append("the previous protected runtime could not be "
                               "restored; it is kept at %s" % backup_dir)
        elif not os.path.isdir(root):
            ok = False
            reasons.append("the previous protected runtime is gone and no "
                           "backup of it was kept")
    else:
        try:
            if os.path.isdir(root):
                _force_rmtree(root)
        except OSError:
            ok = False
            reasons.append("the partially installed protected runtime could "
                           "not be removed")

    # 3. The pin.
    if snapshot.get("pin_present"):
        try:
            directory = ntpath.dirname(target)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(snapshot["pin_bytes"])
                handle.flush()
                os.fsync(handle.fileno())
            harden_pin(path=pin_file, user_sid=user_sid)
        except OSError:
            ok = False
            reasons.append("the previous pin record could not be restored")
    else:
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
        except OSError:
            ok = False
            reasons.append("the partially installed pin record could not be "
                           "removed")

    # 4. The task definition and its security descriptor.
    if snapshot.get("task_present"):
        if not _register_task_xml(snapshot["task_xml"], task_path, runner=runner):
            ok = False
            reasons.append("the previous privileged task definition could not "
                           "be restored")
        else:
            if snapshot.get("task_sddl"):
                if not set_task_security_descriptor(
                        task_path, snapshot["task_sddl"], runner=task_sd_runner):
                    ok = False
                    reasons.append("the previous privileged task security "
                                   "descriptor could not be restored")
            else:
                ok = False
                reasons.append("the previous privileged task security "
                               "descriptor was not readable and cannot be "
                               "restored")
            if str(snapshot.get("task_state") or "").strip().lower() == "disabled":
                set_task_enabled(task_path, enabled=False, runner=runner)

    # 5. Prove the rollback target was actually reached.
    if snapshot.get("task_present") or snapshot.get("pin_present"):
        verdict = verify_installation(runner=runner, pin_file=pin_file,
                                      user_sid=user_sid)
        if not verdict.ok:
            ok = False
            reasons.append("the restored installation does not verify: %s"
                           % "; ".join(verdict.reasons))
    else:
        if task_exists(task_path, runner=runner):
            ok = False
            reasons.append("a privileged task is still registered after rollback")
        if os.path.isfile(target):
            ok = False
            reasons.append("a pin record is still present after rollback")
        if os.path.isdir(root):
            ok = False
            reasons.append("a protected runtime is still present after rollback")
    return ok, reasons


def apply_install(script_dir=None, user_sid=None, idle_timeout_seconds=None,
                  runner=None, pin_file=None, host_image=None,
                  task_path=TASK_PATH, tmp_dir=None, runtime_root=None,
                  worker_bundle=None, task_sd_runner=None, fault=None):
    """Install the protected runtime transactionally. **Runs elevated, once.**

    The transaction, in order -- every step is an :data:`INSTALL_STAGES`
    member and every one of them is fault-injectable:

      1. snapshot the previous installation (runtime, pin, task XML, task
         security descriptor, task state) and stop the running helper;
      2. ``stage_copy``: stage the new runtime and verify its hashes,
         including the recursive fingerprint of the onedir worker bundle;
      3. ``runtime_acl_apply`` / ``runtime_acl_measure``: apply the protected
         ACL to staging and PROVE it -- a measurement that comes back
         unknown is a failure, never a pass;
      4. ``runtime_swap``: atomically replace the installed runtime, then
         re-apply and RE-PROVE the ACL on the canonical path, before any task
         can point at it;
      5. ``task_register``: register the fixed task against the installed
         helper, from an XML file written in protected scratch;
      6. ``task_sd_apply``: apply the explicit task security descriptor;
      7. ``pin_save`` / ``pin_harden``: write the pin, then take ownership of
         it and prove a medium account can neither write it nor rewrite its
         DACL;
      8. ``final_verify``: verify the whole installation from this elevated
         side;
      9. ``commission``: prove Task Scheduler really starts THIS definition,
         then stop it again.

    A failure at ANY stage after the first mutation rolls the machine back to
    the snapshot and re-verifies the result. A first install rolls back to no
    task, no pin and no protected runtime; a repair rolls back to the exact
    previous installation. If the rollback itself cannot be completed the
    error carries :data:`REPAIR_REQUIRED`. A verification that does not pass
    is a failed transaction, not a warning: no runnable task is ever left
    pointing at a runtime whose ACL or integrity was not proven.
    """
    if os.name != "nt":
        raise PrivilegeTaskError("the privileged helper task is a Windows feature")
    if uac_enabled() is False:
        raise PrivilegeTaskError(
            "EnableLUA is 0 on this host: Windows privilege separation is off, so "
            "there is no medium-integrity broker to protect. Run 'Prepare Windows "
            "privilege separation' first, reboot, then install the helper.",
            "config_invalid")
    if not process_is_elevated():
        raise PrivilegeTaskError(
            "installing the privileged helper task needs one elevation; this "
            "process is not elevated", "config_invalid")
    sid = user_sid or current_user_sid()
    source_helper = helper_script_path(script_dir)
    if not os.path.isfile(source_helper):
        raise PrivilegeTaskError("the privileged helper script is missing: %s"
                                 % source_helper, "config_invalid")
    source_bundle = locate_frozen_worker_bundle(script_dir, worker_bundle)
    if not source_bundle:
        raise PrivilegeTaskError(
            "the frozen FIDO worker bundle %s\\%s is not built; run "
            "Scripts\\secure_apps\\build_frozen_worker.ps1 first. The privileged "
            "worker is a PyInstaller --onedir tree on purpose: a --onefile "
            "executable would unpack its DLLs into the medium user's TEMP "
            "before an elevated process loaded them"
            % (FIDO_BUNDLE_DIR_NAME, FROZEN_WORKER_NAME), "config_invalid")

    root = runtime_root or privileged_root()
    installed_helper = installed_helper_path(root)
    staging_dir = root + ".staging-%d" % os.getpid()
    backup_dir = root + ".backup-%d" % os.getpid()
    os.makedirs(ntpath.dirname(root), exist_ok=True)

    # 1. SNAPSHOT the rollback target, then stop the existing helper/task
    #    before touching its files.
    snapshot = _snapshot_installation(task_path, pin_file=pin_file,
                                      runtime_root=root, runner=runner)
    stop_task(task_path, runner=runner)

    scratch = None
    try:
        if os.path.isdir(staging_dir):
            _force_rmtree(staging_dir)
        # 2. stage + verify hashes and the recursive bundle fingerprint.
        manifest = _stage_runtime(staging_dir, source_helper, source_bundle)
        _fault(fault, "stage_copy")

        # 3. ACL on staging -- applied, then PROVEN.
        if harden_runtime_dir(runtime_root=staging_dir, user_sid=sid) is not True:
            raise PrivilegeTaskError(
                "the protected ACL could not be applied to the staged runtime: "
                "%s" % PRIVILEGED_RUNTIME_ACL_INVALID)
        _fault(fault, "runtime_acl_apply")
        staged_acl = runtime_acl_report(runtime_root=staging_dir, user_sid=sid)
        if staged_acl.get("runtime_acl_valid") is not True:
            raise PrivilegeTaskError(
                "the staged runtime ACL does not deny medium-integrity writes "
                "(or could not be measured): %s"
                % PRIVILEGED_RUNTIME_ACL_INVALID)
        _fault(fault, "runtime_acl_measure")

        # 4. atomic replace, then ACL again on the canonical path -- and
        #    PROVEN again, before a task exists that could run it.
        _replace_dir(staging_dir, root, backup_dir)
        _fault(fault, "runtime_swap")
        if harden_runtime_dir(runtime_root=root, user_sid=sid) is not True:
            raise PrivilegeTaskError(
                "the protected ACL could not be applied to the installed "
                "runtime: %s" % PRIVILEGED_RUNTIME_ACL_INVALID)
        acl = runtime_acl_report(runtime_root=root, user_sid=sid)
        if acl.get("runtime_acl_valid") is not True:
            raise PrivilegeTaskError(
                "the installed runtime ACL does not deny medium-integrity "
                "writes (or could not be measured): %s"
                % PRIVILEGED_RUNTIME_ACL_INVALID)

        # The protected scratch root, before anything privileged needs it.
        if harden_privileged_tmp(user_sid=sid) is not True:
            raise PrivilegeTaskError("the privileged scratch root could not be "
                                     "hardened: %s" % PRIVILEGED_SCRATCH_INVALID)
        tmp_state = privileged_tmp_report()
        if tmp_state.get("privileged_tmp_valid") is not True:
            raise PrivilegeTaskError(
                "the privileged scratch root is not protected: %s"
                % PRIVILEGED_SCRATCH_INVALID)

        # 5. register the fixed task against the INSTALLED helper, from an XML
        #    file that lives in protected scratch rather than user TEMP.
        xml_text = build_task_xml(user_sid=user_sid, host_image=host_image,
                                  task_path=task_path,
                                  helper_path=installed_helper)
        scratch = new_privileged_scratch_dir(prefix="install", tmp_root=tmp_dir,
                                            user_sid=sid)
        xml_file = _write_privileged_task_xml(xml_text, scratch)
        create_task(xml_file, task_path=task_path, runner=runner)
        _fault(fault, "task_register")

        # 6. task security descriptor (not schtasks' default DACL) -- and the
        #    FOLDER's, because deleting a task needs write access to the
        #    folder, not to the task.
        folder_path = str(task_path).rpartition("\\")[0] or TASK_FOLDER
        if apply_task_security_descriptor(task_path=task_path, user_sid=sid,
                                          runner=task_sd_runner) is not True:
            raise PrivilegeTaskError(
                "the task security descriptor could not be applied: %s"
                % PRIVILEGED_TASK_SECURITY_INVALID)
        if apply_task_folder_security_descriptor(
                folder_path=folder_path, user_sid=sid,
                runner=task_sd_runner) is not True:
            raise PrivilegeTaskError(
                "the task FOLDER security descriptor could not be applied, so a "
                "medium-integrity account could still delete the task out of "
                "it: %s" % PRIVILEGED_TASK_SECURITY_INVALID)
        _fault(fault, "task_sd_apply")

        # 7. hardened pin, bound to the installed runtime + manifest.
        pin = build_pin(runtime_root=root, user_sid=sid, host_image=host_image,
                        task_path=task_path,
                        idle_timeout_seconds=idle_timeout_seconds,
                        manifest=manifest)
        target = save_pin(pin, path=pin_file)
        _fault(fault, "pin_save")
        if harden_pin(path=pin_file, user_sid=sid) is not True:
            raise PrivilegeTaskError("the pin record could not be hardened: %s"
                                     % PRIVILEGED_PIN_OWNER_INVALID)
        pin_state = pin_acl_report(pin_file)
        if pin_state.get("pin_acl_valid") is not True:
            raise PrivilegeTaskError(
                "the pin record is not owned by Administrators/SYSTEM or is "
                "still reachable by a medium-integrity account: %s"
                % PRIVILEGED_PIN_OWNER_INVALID)
        _fault(fault, "pin_harden")

        # 8. verify the whole thing from this elevated side. A verdict that is
        #    not ok is a FAILED TRANSACTION, not a repair hint.
        verdict = verify_installation(runner=runner, pin_file=pin_file,
                                      user_sid=sid)
        if not verdict.ok:
            raise PrivilegeTaskError(
                "the finished installation did not verify: %s"
                % ("; ".join(verdict.reasons) or verdict.token
                   or PRIVILEGED_TASK_TAMPERED))
        _fault(fault, "final_verify")

        # 9. commission: Task Scheduler really starts THIS definition. The
        #    medium half of commissioning -- silent start from a medium broker
        #    with a high-integrity helper on the other end -- belongs to
        #    :func:`commission` in the caller; this only proves the registered
        #    task is runnable before the transaction is declared complete.
        started = _commission_task(task_path, runner=runner)
        if not started:
            raise PrivilegeTaskError(
                "the registered privileged task did not start: %s"
                % LOCK_PRIVILEGED_HELPER_UNAVAILABLE)
        _fault(fault, "commission")

        result = {
            "ok": True,
            "task_path": task_path,
            "runtime_root": normalize_path(root),
            "pin_path": target,
            "pin_hardened": True,
            "pin_owner_valid": pin_state.get("pin_owner_valid"),
            "task_security_applied": True,
            "runtime_acl_valid": True,
            "privileged_tmp_root": tmp_state.get("privileged_tmp_root"),
            "had_previous_installation": bool(snapshot.get("task_present")
                                              or snapshot.get("pin_present")),
            "runtime_bundle_fingerprint": pin.get("runtime_bundle_fingerprint"),
            "fido_worker_bundle_fingerprint":
                pin.get("fido_worker_bundle_fingerprint"),
            "fido_worker_file_count": manifest.get("fido_worker_file_count"),
            "definition_fingerprint": pin.get("definition_fingerprint"),
            "idle_timeout_seconds": pin.get("idle_timeout_seconds"),
            "reasons": [],
        }
        _quiet_rmtree(backup_dir)
        return result
    except Exception as exc:
        restored, restore_reasons = _restore_installation(
            root, backup_dir, staging_dir, task_path, snapshot, runner=runner,
            pin_file=pin_file, task_sd_runner=task_sd_runner, user_sid=sid)
        target_text = ("the previous installation" if snapshot.get("task_present")
                       or snapshot.get("pin_present") else "an uninstalled machine")
        if restored:
            detail = "rolled back to %s" % target_text
            category = getattr(exc, "category", "internal")
            token = ""
        else:
            detail = ("could NOT be rolled back to %s (%s); the previous runtime, "
                      "if any, is kept at %s -- %s"
                      % (target_text, "; ".join(restore_reasons) or "no detail",
                         backup_dir, REPAIR_REQUIRED))
            category = "internal"
            token = REPAIR_REQUIRED
        raise PrivilegeTaskError("%s (%s)" % (exc, detail), category, token)
    finally:
        # Staging is either renamed onto the canonical path (success) or removed
        # by the rollback (failure); anything left is scratch. The backup is
        # NEVER removed here -- a failed, unrestorable rollback keeps it.
        _quiet_rmtree(staging_dir)
        if scratch:
            _quiet_rmtree(scratch)


def _commission_task(task_path, runner=None, timeout=20.0):
    """Start the registered task once, confirm it ran, and stop it again."""
    try:
        start_task(task_path, runner=runner)
    except PrivilegeTaskError:
        return False
    deadline = time.time() + timeout
    seen_running = False
    while time.time() < deadline:
        state = (task_run_state(task_path, runner=runner) or "").strip().lower()
        if state == "running":
            seen_running = True
            break
        if state == "disabled":
            return False
        time.sleep(0.25)
    stop_task(task_path, runner=runner)
    return seen_running


def apply_remove(runner=None, pin_file=None, task_path=None):
    """Remove the silent elevation mechanism. **Runs elevated.**

    Removes the task and the pin, and NOTHING else: no vault, no FIDO2
    credential, no Obsidian data, no acceptance record, no recovery material.
    """
    if not process_is_elevated():
        raise PrivilegeTaskError(
            "removing the privileged helper task needs one elevation", "config_invalid")
    pin, _problem = load_pin(pin_file)
    target_task = task_path or (pin or {}).get("task_path") or TASK_PATH
    stop_task(target_task, runner=runner)
    removed_task = delete_task(target_task, runner=runner)
    target = pin_file or pin_path()
    removed_pin = True
    try:
        os.remove(target)
    except FileNotFoundError:
        pass
    except OSError:
        removed_pin = False
    # The protected runtime tree goes too; nothing user-facing lives in it.
    root = (pin or {}).get("runtime_root") or privileged_root()
    removed_runtime = True
    try:
        if os.path.isdir(root):
            _force_rmtree(root)
        removed_runtime = not os.path.isdir(root)
    except OSError:
        removed_runtime = False
    # ...and so does the protected scratch root: it holds privileged COMMAND
    # material and never anything a person would miss.
    scratch = (pin or {}).get("privileged_tmp_root") or privileged_tmp_root()
    removed_scratch = True
    try:
        if os.path.isdir(scratch):
            _force_rmtree(scratch)
        removed_scratch = not os.path.isdir(scratch)
    except OSError:
        removed_scratch = False
    return {
        "ok": bool(removed_task and removed_pin and removed_runtime
                   and removed_scratch),
        "task_removed": removed_task,
        "pin_removed": removed_pin,
        "runtime_removed": removed_runtime,
        "scratch_removed": removed_scratch,
        "task_path": target_task,
        "pin_path": target,
        "runtime_root": normalize_path(root),
        "privileged_tmp_root": normalize_path(scratch),
        "kept": ["encrypted vault", "FIDO2 credential", "Obsidian data",
                 "hardware acceptance record", "recovery material"],
    }


def apply_prepare_uac(confirm=False):
    """Set ``EnableLUA=1`` and nothing else. **Runs elevated, on request.**

    Deliberately does not touch ``ConsentPromptBehaviorAdmin`` or
    ``PromptOnSecureDesktop``: those are the user's own prompt policy for
    every other program on the machine, and Secure Apps has no business
    having an opinion about them.
    """
    if os.name != "nt":
        raise PrivilegeTaskError("EnableLUA is a Windows setting")
    if not confirm:
        raise PrivilegeTaskError("refusing to change UAC policy without an "
                                 "explicit confirmation", "config_invalid")
    if not process_is_elevated():
        raise PrivilegeTaskError("changing EnableLUA needs one elevation",
                                 "config_invalid")
    before = uac_policy_facts()
    if before["EnableLUA"] is True:
        return {"ok": True, "changed": False, "reboot_required": False,
                "before": before, "after": before}
    import winreg
    access = winreg.KEY_SET_VALUE | getattr(winreg, "KEY_WOW64_64KEY", 0)
    with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
            0, access) as key:
        winreg.SetValueEx(key, "EnableLUA", 0, winreg.REG_DWORD, 1)
    after = uac_policy_facts()
    return {"ok": after["EnableLUA"] is True, "changed": True,
            "reboot_required": True, "before": before, "after": after,
            "note": "Windows must be restarted before privilege separation is in "
                    "force. No other UAC setting was changed."}


def commission(script_dir=None, runner=None, pin_file=None, helper=None):
    """Prove the installed mechanism actually works, from medium integrity.

    Steps 5 to 9 of the one-time installation, and the only verdict that
    counts: the elevated installer says what it did, this says whether the
    result is a silent privilege boundary. In order --

      5. the registered definition matches the pinned one;
      6. Task Scheduler starts the helper, with no consent dialog;
      7. the helper independently reports high integrity;
      8. this process -- the broker side -- is still medium integrity;
      9. if any of that fails, the helper is stopped again.

    Step 8 is not paperwork. A "silent privileged start" proven by an
    elevated broker proves nothing: there was no boundary to cross.
    """
    import sa_privhelper
    report = {
        "ok": False,
        "definition_valid": False,
        "silent_start": False,
        "helper_high_integrity": False,
        "broker_medium_integrity": False,
        "helper_pid": None,
        "helper_stopped": False,
        "token": "",
        "reasons": [],
    }
    verdict = verify_installation(script_dir=script_dir, runner=runner,
                                 pin_file=pin_file)
    report["definition_valid"] = verdict.ok
    report["definition_fingerprint"] = verdict.fingerprint
    report["task_path"] = (verdict.pin or {}).get("task_path", TASK_PATH)
    # verify_installation already required, on Windows, that the runtime ACL
    # denies medium writes and the task security descriptor denies medium
    # modification; surface those facts so commissioning proves them too.
    report["runtime_acl_valid"] = verdict.runtime_acl.get("runtime_acl_valid")
    report["task_security_valid"] = verdict.task_security.get("task_security_valid")
    report["runtime_bundle_fingerprint"] = \
        (verdict.pin or {}).get("runtime_bundle_fingerprint")
    report["task_security_fingerprint"] = \
        verdict.task_security.get("task_security_fingerprint")
    if not verdict.ok:
        report["reasons"] = list(verdict.reasons)
        report["token"] = verdict.token or PRIVILEGED_TASK_TAMPERED
        return report
    broker_elevated = process_is_elevated()
    report["broker_medium_integrity"] = broker_elevated is False
    channel = helper or sa_privhelper.PrivilegedHelper(script_dir=script_dir,
                                                       pin_file=pin_file,
                                                       runner=runner)
    try:
        channel.ensure()
        report["silent_start"] = True
        identity = getattr(channel, "identity", None)
        report["helper_pid"] = getattr(identity, "pid", None)
        report["helper_high_integrity"] = getattr(identity, "elevated", None) is True
        try:
            info = channel.helper_info()
            report["helper_reports_elevated"] = bool(info.get("elevated"))
            report["helper_launch_mode"] = info.get("launch_mode")
        except Exception as exc:
            report["reasons"].append("the helper did not answer helper_info (%s)"
                                     % type(exc).__name__)
    except Exception as exc:
        report["reasons"].append(str(exc))
        report["token"] = getattr(exc, "token", "") or LOCK_PRIVILEGED_HELPER_UNAVAILABLE
    report["ok"] = bool(report["definition_valid"] and report["silent_start"]
                        and report["helper_high_integrity"]
                        and report["broker_medium_integrity"])
    if not report["broker_medium_integrity"]:
        report["reasons"].append(
            "this process is elevated, so a silent privileged start proves no "
            "boundary; run Secure Apps as your ordinary user with UAC enabled")
    if report["ok"]:
        report["token"] = SILENT_PRIVILEGED_START_ACCEPTED
    else:
        # Step 9. A validation that did not pass must not leave a
        # high-integrity process running behind it.
        try:
            channel.close()
        except Exception:
            pass
        report["helper_stopped"] = stop_task(report["task_path"], runner=runner)
    try:
        channel.release()
    except Exception:
        pass
    return report


# --------------------------------------------------------------------------
# elevation of the installer itself
# --------------------------------------------------------------------------
def _interpreter_is_acceptable(python_exe):
    """The interpreter the ONE setup elevation runs. Shape checks only.

    This is not the privileged runtime path: nothing elevated at unlock time
    runs an interpreter any more. It is the medium-integrity side raising the
    single Install/Repair/Remove consent dialog, and what it elevates is a
    Secure Apps entry point -- so the shape of that interpreter is still worth
    refusing when it is obviously wrong.
    """
    if not python_exe:
        return "no interpreter path was supplied"
    if not ntpath.isabs(python_exe) or ntpath.normpath(python_exe) != python_exe:
        return "the interpreter path is not absolute and canonical: %s" % python_exe
    if not os.path.isfile(python_exe):
        return "the interpreter does not exist: %s" % python_exe
    if ntpath.splitext(python_exe)[1].lower() != ".exe":
        return "the interpreter is not an executable: %s" % python_exe
    if ntpath.basename(python_exe).lower() not in ("python.exe", "pythonw.exe"):
        return "the interpreter is not a python executable: %s" % python_exe
    return None


def elevate_self(arguments, python_exe=None, cli_path=None, timeout=300.0,
                 spawn=None, result_path=None):
    """Run one Secure Apps entry point elevated, once, with a UAC prompt.

    This is the ONLY place in Secure Apps that raises a consent dialog, and
    it is reached only from Install / Repair / Remove / Prepare. Ordinary
    unlock, lock, mount and relock never come here: they start the registered
    task instead, which is the entire point of the feature.
    """
    if os.name != "nt":
        raise PrivilegeTaskError("elevation is a Windows operation")
    python_exe = sys.executable if python_exe is None else python_exe
    problem = _interpreter_is_acceptable(python_exe)
    if problem:
        raise PrivilegeTaskError("cannot elevate: %s" % problem, "config_invalid")
    cli = cli_path or ntpath.join(helper_root(), "sa_cli.py")
    if not os.path.isfile(cli):
        raise PrivilegeTaskError("cannot elevate: %s is missing" % cli,
                                 "config_invalid")
    argv = [cli] + list(arguments)
    if result_path:
        argv += ["--result", result_path]
    quoted = ",".join("'%s'" % str(item).replace("'", "''") for item in argv)
    command = ("$p = Start-Process -FilePath '%s' -Verb RunAs -Wait -PassThru "
               "-ArgumentList @(%s); exit $p.ExitCode"
               % (str(python_exe).replace("'", "''"), quoted))
    launcher = [powershell_host(), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", command]
    if spawn is not None:
        return spawn(launcher)
    try:
        completed = subprocess.run(launcher, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PrivilegeTaskError("the elevated setup step did not finish in time")
    return CommandResult(completed.returncode, _decode(completed.stdout),
                         _decode(completed.stderr))
