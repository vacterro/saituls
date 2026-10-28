"""Real Windows access probes against the Secure Apps privilege boundary.

A descriptor that *says* DENIED and a Windows kernel that *answers* DENIED are
two different claims. Parsing a DACL proves this implementation understands
the ACE it wrote; only attempting the operation proves Windows enforces it. So
everything here is an attempt: open the protected helper for writing, delete
it, rename it, create a file inside the worker bundle, rewrite the pin's DACL,
change the scheduled task, disable it, delete it, rewrite its security
descriptor.

Every attempt runs through a **genuine medium-integrity token**. When this
process is already medium integrity -- which is what the disposable acceptance
run is, by definition -- the attempts happen right here. When it is elevated
(the Windows integration test needs elevation for BitLocker), the probe
re-launches itself with the interactive user's own filtered token, so the
answers still come from a real medium process rather than from a simulation.

Outcomes are exactly three words:

    DENIED     Windows refused the operation
    ALLOWED    Windows performed it -- the boundary is NOT enforced
    ERROR      the attempt could not be made at all (never a pass)

An ALLOWED mutation is a failure AND a mess, so every mutating probe records
what it changed and restores it immediately; anything it could not restore is
reported so privileged cleanup can finish the job.

Nothing here reads, writes or transports a secret: the probes touch public
artifacts (a script, an executable bundle, a JSON pin, a task definition) and
report booleans and fixed strings.
"""
import json
import os
import subprocess
import sys

DENIED = "DENIED"
ALLOWED = "ALLOWED"
ERROR = "ERROR"

PROBE_SCHEMA = "saituls.secure-apps.privilege-boundary-probe/1"

#: Every probe this module can perform. The acceptance run and the Windows
#: integration test both pick from this list by name.
PROBE_OPS = (
    "file_open_write",
    "file_delete",
    "file_rename",
    "dir_create_child",
    "file_dacl_change",
    "task_query",
    "task_run",
    "task_change",
    "task_disable",
    "task_delete",
    "task_sd_change",
    "task_folder_create",
)


def _result(outcome, detail="", mutated=False, restored=True):
    return {"outcome": outcome, "detail": str(detail)[:200],
            "mutated": bool(mutated), "restored": bool(restored)}


def _denied_by(exc):
    """A Windows access-denied answer, whatever API shape it arrived in."""
    winerror = getattr(exc, "winerror", None)
    errno = getattr(exc, "errno", None)
    if winerror in (5, 32, 1314):          # ACCESS_DENIED, SHARING_VIOLATION
        return True
    if errno in (13, 1):                   # EACCES, EPERM
        return True
    text = str(exc).lower()
    return "access is denied" in text or "permission denied" in text


# --------------------------------------------------------------------------
# filesystem probes
# --------------------------------------------------------------------------
def probe_file_open_write(path):
    """Open the protected file for writing. Nothing is ever written."""
    try:
        handle = os.open(path, os.O_WRONLY)
    except OSError as exc:
        return _result(DENIED if _denied_by(exc) else ERROR, exc)
    os.close(handle)
    return _result(ALLOWED, "opened for write", mutated=False)


def probe_file_delete(path):
    """Delete the protected file. Restores it when Windows allows it.

    The backup read is BEST EFFORT: a file the medium account cannot even read
    must still have its deletion attempted, because "I could not read it" is
    not an answer to "can I delete it". An allowed deletion with no backup is
    reported as unrestored, which is a failure the caller must repair.
    """
    keep = None
    try:
        with open(path, "rb") as handle:
            keep = handle.read()
    except OSError:
        keep = None
    try:
        os.remove(path)
    except OSError as exc:
        return _result(DENIED if _denied_by(exc) else ERROR, exc)
    if keep is None:
        return _result(ALLOWED, "deleted; no backup was readable to restore it",
                       mutated=True, restored=False)
    try:
        with open(path, "wb") as handle:
            handle.write(keep)
        return _result(ALLOWED, "deleted", mutated=True, restored=True)
    except OSError as exc:
        return _result(ALLOWED, "deleted and NOT restored: %s" % exc,
                       mutated=True, restored=False)


def probe_file_rename(path):
    """Rename the protected file. Renames it straight back when allowed."""
    moved = path + ".mediumprobe"
    try:
        os.rename(path, moved)
    except OSError as exc:
        return _result(DENIED if _denied_by(exc) else ERROR, exc)
    try:
        os.rename(moved, path)
        return _result(ALLOWED, "renamed", mutated=True, restored=True)
    except OSError as exc:
        return _result(ALLOWED, "renamed and NOT restored: %s" % exc,
                       mutated=True, restored=False)


def probe_dir_create_child(path):
    """Create a new file inside the protected directory. Removes it again."""
    child = os.path.join(path, "saituls-medium-probe.tmp")
    try:
        handle = os.open(child, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except OSError as exc:
        return _result(DENIED if _denied_by(exc) else ERROR, exc)
    os.close(handle)
    try:
        os.remove(child)
        return _result(ALLOWED, "created a child", mutated=True, restored=True)
    except OSError as exc:
        return _result(ALLOWED, "created a child and could not remove it: %s"
                       % exc, mutated=True, restored=False)


def probe_file_dacl_change(path):
    """Write the file's own DACL back onto it. Tests WRITE_DAC, changes nothing.

    Re-applying the DACL a file already has is the smallest possible exercise
    of the right that matters: an account that may rewrite the security
    descriptor may grant itself anything else afterwards.
    """
    try:
        import win32security
    except ImportError as exc:
        return _result(ERROR, "pywin32 is not available: %s" % exc)
    try:
        descriptor = win32security.GetFileSecurity(
            path, win32security.DACL_SECURITY_INFORMATION)
    except Exception as exc:
        # Being refused READ_CONTROL is a stronger answer than being refused
        # WRITE_DAC, not a missing one: an account that cannot even open the
        # object's security descriptor certainly cannot rewrite it.
        if _denied_by(exc):
            return _result(DENIED, "the security descriptor cannot even be read")
        return _result(ERROR, "the DACL could not be read: %s" % exc)
    try:
        win32security.SetFileSecurity(
            path, win32security.DACL_SECURITY_INFORMATION, descriptor)
    except Exception as exc:
        return _result(DENIED if _denied_by(exc) else ERROR, exc)
    return _result(ALLOWED, "rewrote the DACL", mutated=False)


# --------------------------------------------------------------------------
# scheduled-task probes
# --------------------------------------------------------------------------
def _schtasks(arguments, timeout=60):
    executable = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                              "System32", "schtasks.exe")
    if not os.path.isfile(executable):
        executable = "schtasks.exe"
    try:
        completed = subprocess.run([executable] + list(arguments),
                                   capture_output=True, timeout=timeout,
                                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    text = (completed.stdout or b"") + (completed.stderr or b"")
    return completed.returncode, text.decode("utf-8", "replace").strip()


def _task_outcome(code, text, allowed_detail):
    if code is None:
        return _result(ERROR, text)
    if code == 0:
        return _result(ALLOWED, allowed_detail, mutated=True)
    return _result(DENIED if "access is denied" in text.lower()
                   else ERROR, text.splitlines()[0] if text else "exit %d" % code)


def probe_task_query(task_path):
    """The medium broker MUST be able to read the task. DENIED here is a bug."""
    code, text = _schtasks(["/Query", "/TN", task_path])
    if code is None:
        return _result(ERROR, text)
    if code == 0:
        return _result(ALLOWED, "queried", mutated=False)
    return _result(DENIED if "access is denied" in text.lower() else ERROR,
                   text.splitlines()[0] if text else "exit %d" % code)


def probe_task_run(task_path):
    """The medium broker MUST be able to start it. DENIED here is a bug."""
    code, text = _schtasks(["/Run", "/TN", task_path])
    if code is None:
        return _result(ERROR, text)
    if code == 0:
        return _result(ALLOWED, "started", mutated=False)
    return _result(DENIED if "access is denied" in text.lower() else ERROR,
                   text.splitlines()[0] if text else "exit %d" % code)


def probe_task_change(task_path):
    """Re-register the task over itself. Must be refused.

    ``schtasks /Change`` is not usable as a probe: on a task with a stored
    principal it asks for the run-as password on the console and, with no
    console, waits. ``/Create /XML <the task's own XML> /F`` is the same
    authority question -- may this account replace the registered definition?
    -- asked in a way that cannot block, and it changes nothing even if
    Windows allows it, because the XML written back is the one already there.
    """
    code, text = _schtasks(["/Query", "/TN", task_path, "/XML"])
    if code != 0:
        return _result(ERROR, "the task definition could not be read: %s" % text)
    start = text.find("<")
    if start < 0:
        return _result(ERROR, "the task definition was unreadable")
    import tempfile
    handle, path = tempfile.mkstemp(suffix=".xml", prefix="saituls-taskprobe-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(text[start:].encode("utf-16"))
        code, text = _schtasks(["/Create", "/TN", task_path, "/XML", path, "/F"])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return _task_outcome(code, text, "re-registered the task definition")


def probe_task_folder_create(folder_path):
    """Create a NEW task in the privileged folder. Must be refused.

    Deleting a registered task needs write access to its FOLDER, not to the
    task object, so the folder is its own boundary question.
    """
    task_path = folder_path.rstrip("\\") + "\\SaitulsMediumProbe"
    xml = ("<?xml version=\"1.0\" encoding=\"UTF-16\"?>"
           "<Task version=\"1.2\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">"
           "<Triggers/><Principals><Principal id=\"Author\">"
           "<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>"
           "</Principal></Principals><Settings><Enabled>false</Enabled></Settings>"
           "<Actions Context=\"Author\"><Exec><Command>cmd.exe</Command></Exec></Actions></Task>")
    import tempfile
    handle, path = tempfile.mkstemp(suffix=".xml", prefix="saituls-folderprobe-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(xml.encode("utf-16"))
        code, text = _schtasks(["/Create", "/TN", task_path, "/XML", path, "/F"])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    outcome = _task_outcome(code, text, "created a task in the privileged folder")
    if outcome["outcome"] == ALLOWED:
        back, _text = _schtasks(["/Delete", "/TN", task_path, "/F"])
        outcome["restored"] = back == 0
    return outcome


def probe_task_disable(task_path):
    """Disable the task. Must be refused; re-enabled if it was not."""
    code, text = _schtasks(["/Change", "/TN", task_path, "/DISABLE"])
    outcome = _task_outcome(code, text, "disabled the task")
    if outcome["outcome"] == ALLOWED:
        back, _text = _schtasks(["/Change", "/TN", task_path, "/ENABLE"])
        outcome["restored"] = back == 0
    return outcome


def probe_task_delete(task_path):
    """Delete the task. Must be refused. A deletion cannot be undone here."""
    code, text = _schtasks(["/Delete", "/TN", task_path, "/F"])
    outcome = _task_outcome(code, text, "deleted the task")
    if outcome["outcome"] == ALLOWED:
        outcome["restored"] = False
    return outcome


def probe_task_sd_change(task_path):
    """Rewrite the task's security descriptor. Must be refused."""
    folder_path, _sep, name = str(task_path).rpartition("\\")
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$service = New-Object -ComObject Schedule.Service\n"
        "$service.Connect()\n"
        "$folder = $service.GetFolder('%s')\n"
        "$task = $folder.GetTask('%s')\n"
        "$sddl = $task.GetSecurityDescriptor(4)\n"
        "$task.SetSecurityDescriptor($sddl, 16)\n"
        "[Console]::Out.Write('REWROTE')\n"
        % ((folder_path or "\\").replace("'", "''"), name.replace("'", "''")))
    # Driven through Windows PowerShell rather than pywin32: the probe must be
    # able to ask this question on a host whose optional COM bindings are
    # missing or damaged, or it would report ERROR where Windows would have
    # said DENIED -- and an ERROR is never evidence of a boundary.
    ok, out, text = _powershell(script)
    if ok and "REWROTE" in out:
        return _result(ALLOWED, "rewrote the task security descriptor",
                       mutated=False)
    lowered = (text or "").lower()
    if "access is denied" in lowered or "e_accessdenied" in lowered or "0x80070005" in lowered:
        return _result(DENIED, (text or "").strip().splitlines()[0]
                       if text else "access denied")
    return _result(ERROR, (text or "the descriptor could not be rewritten"
                           ).strip().splitlines()[0] if text
                   else "the descriptor could not be rewritten")


def _powershell(script, timeout=120):
    """Run *script* through Windows PowerShell. ``(ok, stdout, combined)``."""
    import base64
    host = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                        "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if not os.path.isfile(host):
        return False, "", "Windows PowerShell is not available"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
            [host, "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", str(exc)
    out = (completed.stdout or b"").decode("utf-8", "replace")
    err = (completed.stderr or b"").decode("utf-8", "replace")
    return completed.returncode == 0, out, out + err


_FILE_PROBES = {
    "file_open_write": probe_file_open_write,
    "file_delete": probe_file_delete,
    "file_rename": probe_file_rename,
    "dir_create_child": probe_dir_create_child,
    "file_dacl_change": probe_file_dacl_change,
}
_TASK_PROBES = {
    "task_query": probe_task_query,
    "task_run": probe_task_run,
    "task_change": probe_task_change,
    "task_disable": probe_task_disable,
    "task_delete": probe_task_delete,
    "task_sd_change": probe_task_sd_change,
    "task_folder_create": probe_task_folder_create,
}


# --------------------------------------------------------------------------
# running the probes
# --------------------------------------------------------------------------
def process_is_elevated():
    if os.name != "nt":
        return None
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def run_here(spec):
    """Run every probe in *spec* in THIS process. Returns the result map."""
    results = {}
    for item in spec.get("probes", []):
        name = item.get("op")
        target = item.get("target")
        label = item.get("id") or name
        if name in _FILE_PROBES:
            results[label] = _FILE_PROBES[name](target)
        elif name in _TASK_PROBES:
            results[label] = _TASK_PROBES[name](target)
        else:
            results[label] = _result(ERROR, "unknown probe %r" % (name,))
        results[label]["op"] = name
        results[label]["target"] = str(target)
    return {
        "schema": PROBE_SCHEMA,
        "elevated": process_is_elevated(),
        "integrity_sid": process_integrity_sid(),
        "pid": os.getpid(),
        "results": results,
    }


def process_integrity_sid():
    """This process's own mandatory-label SID, e.g. ``S-1-16-8192``."""
    try:
        import win32api
        import win32con
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                               win32con.TOKEN_QUERY)
        level = win32security.GetTokenInformation(
            token, win32security.TokenIntegrityLevel)
        return str(win32security.ConvertSidToStringSid(level[0]))
    except Exception:
        return None


def _medium_token():
    """A genuine medium-integrity primary token for the interactive user.

    The interactive shell's own token is preferred: it IS the filtered token
    Windows hands every ordinary program this user starts, so a probe running
    on it is asking the exact question the threat model asks. When the shell
    cannot be opened, a filtered token is built the way UAC builds one --
    Administrators made deny-only and every removable privilege dropped --
    and its integrity level is lowered to Medium.
    """
    import win32api
    import win32con
    import win32process
    import win32security

    for name in ("explorer.exe",):
        for pid in _pids_named(name):
            try:
                handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION,
                                              False, pid)
            except Exception:
                continue
            try:
                token = win32security.OpenProcessToken(
                    handle, win32con.TOKEN_DUPLICATE | win32con.TOKEN_QUERY)
                candidate = win32security.DuplicateTokenEx(
                    token, win32security.SecurityImpersonation,
                    win32con.MAXIMUM_ALLOWED, win32security.TokenPrimary)
            except Exception:
                continue
            finally:
                try:
                    win32api.CloseHandle(handle)
                except Exception:
                    pass
            # The shell is not always the medium token: on a desktop whose
            # whole session was started elevated, explorer.exe itself runs at
            # High. A candidate is used only when it really is Medium with
            # Administrators deny-only -- otherwise a filtered token is built.
            if token_is_medium(candidate):
                return candidate

    token = win32security.OpenProcessToken(
        win32process.GetCurrentProcess(),
        win32con.TOKEN_DUPLICATE | win32con.TOKEN_QUERY
        | win32con.TOKEN_ASSIGN_PRIMARY | win32con.TOKEN_ADJUST_DEFAULT)
    admins = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid)
    # Exactly what UAC does to build the filtered token of an administrator:
    # Administrators becomes deny-only and every removable privilege is
    # dropped. Then the integrity label is lowered to Medium -- a token's
    # label may only ever be lowered, which is why this direction is legal.
    restricted = win32security.CreateRestrictedToken(
        token, win32security.DISABLE_MAX_PRIVILEGE, [(admins, 0)], [], [])
    medium = win32security.ConvertStringSidToSid("S-1-16-8192")
    win32security.SetTokenInformation(
        restricted, win32security.TokenIntegrityLevel,
        (medium, win32security.SE_GROUP_INTEGRITY))
    if not token_is_medium(restricted):
        raise OSError("a medium-integrity token could not be constructed")
    return restricted


MEDIUM_INTEGRITY_SID = "S-1-16-8192"
SE_GROUP_USE_FOR_DENY_ONLY = 0x00000010


def token_is_medium(token):
    """True when *token* is Medium integrity with Administrators deny-only.

    Both halves matter. A High token with Administrators disabled would still
    be allowed to write a Medium object, and a Medium token that still carried
    an enabled Administrators SID would be granted every right the protected
    runtime hands Administrators -- neither is the account the threat model
    talks about.
    """
    try:
        import win32security
        level = win32security.GetTokenInformation(
            token, win32security.TokenIntegrityLevel)
        if str(win32security.ConvertSidToStringSid(level[0])) != MEDIUM_INTEGRITY_SID:
            return False
        admins = win32security.CreateWellKnownSid(
            win32security.WinBuiltinAdministratorsSid)
        for sid, attributes in win32security.GetTokenInformation(
                token, win32security.TokenGroups):
            if sid == admins:
                return bool(attributes & SE_GROUP_USE_FOR_DENY_ONLY)
        return True                      # not a member at all is fine
    except Exception:
        return False


def _pids_named(image):
    try:
        import win32process
    except ImportError:
        return []
    found = []
    try:
        import win32api
        import win32con
        import win32process as wp
        for pid in wp.EnumProcesses():
            try:
                handle = win32api.OpenProcess(
                    win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            except Exception:
                continue
            try:
                name = os.path.basename(wp.GetModuleFileNameEx(handle, 0))
                if name.lower() == image.lower():
                    found.append(pid)
            except Exception:
                pass
            finally:
                try:
                    win32api.CloseHandle(handle)
                except Exception:
                    pass
    except Exception:
        return found
    return found


def run_medium(spec, python_exe=None, timeout=300):
    """Run *spec* through a real medium-integrity token, whatever we are.

    Already medium: the probes run here. Elevated: a child is started on the
    interactive user's own filtered token and its JSON answer is returned.
    """
    if process_is_elevated() is not True:
        report = run_here(spec)
        report["launched"] = "in-process"
        return report
    import tempfile

    executable = python_exe or sys.executable
    handle, result_path = tempfile.mkstemp(suffix=".json", prefix="saituls-probe-")
    os.close(handle)
    spec_handle, spec_path = tempfile.mkstemp(suffix=".json", prefix="saituls-spec-")
    with os.fdopen(spec_handle, "w", encoding="utf-8") as stream:
        json.dump(spec, stream)
    log_handle, log_path = tempfile.mkstemp(suffix=".log", prefix="saituls-probe-")
    os.close(log_handle)
    try:
        token = _medium_token()
        # The child is started with a plain, explicit interpreter invocation:
        # -I isolates it from PYTHONPATH, PYTHONHOME, user site-packages and
        # whatever the PARENT's test runner put in the environment, so the
        # probe answers the same way whether it was launched from a shell or
        # from inside pytest.
        command = '"%s" -I -u "%s" --spec "%s" --out "%s" --log "%s"' % (
            executable, os.path.abspath(__file__), spec_path, result_path,
            log_path)
        code = _spawn_with_token(token, command, timeout)
        try:
            with open(result_path, "r", encoding="utf-8") as stream:
                report = json.load(stream)
        except (OSError, ValueError) as exc:
            detail = ""
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as stream:
                    detail = stream.read().strip()[-400:]
            except OSError:
                pass
            return {"schema": PROBE_SCHEMA, "launched": "failed",
                    "error": "the medium-integrity probe child wrote no usable "
                             "result (exit %s, %s)%s"
                             % (code, exc, (": " + detail) if detail else ""),
                    "results": {}}
        report["launched"] = "medium-token"
        report["child_exit_code"] = code
        if report.get("elevated") is not False:
            return {"schema": PROBE_SCHEMA, "launched": "failed",
                    "error": "the probe child was not medium integrity",
                    "results": {}}
        return report
    except Exception as exc:
        return {"schema": PROBE_SCHEMA, "launched": "failed",
                "error": "a medium-integrity probe process could not be "
                         "started: %s" % exc, "results": {}}
    finally:
        for path in (spec_path, result_path, log_path):
            try:
                os.remove(path)
            except OSError:
                pass


class _STARTUPINFOW(object):
    pass


def _spawn_with_token(token, command, timeout):
    """CreateProcessWithTokenW, waited on. Returns the child exit code.

    ``CreateProcessWithTokenW`` is the one process-creation API an elevated
    administrator can use without SeAssignPrimaryTokenPrivilege -- it needs
    SeImpersonatePrivilege, which an elevated session holds -- so it is how a
    high-integrity test drops to the user's real medium token.
    """
    import ctypes
    from ctypes import wintypes

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD),
                    ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD),
                    ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                    ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE),
                    ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE),
                    ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    environment = _environment_block()
    _grant_station_and_desktop(token)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.CreateProcessWithTokenW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
        wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
    advapi32.CreateProcessWithTokenW.restype = wintypes.BOOL

    advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID,
        wintypes.LPVOID, wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID,
        wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION)]
    advapi32.CreateProcessAsUserW.restype = wintypes.BOOL

    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(STARTUPINFOW)
    startup.dwFlags = 0x00000001                       # STARTF_USESHOWWINDOW
    startup.wShowWindow = 0                            # SW_HIDE
    startup.lpDesktop = "winsta0\\default"
    info = PROCESS_INFORMATION()
    handle = wintypes.HANDLE(int(token))
    # CREATE_UNICODE_ENVIRONMENT, because the child is given an explicit
    # minimal block rather than the parent's.
    flags = 0x08000000 | 0x00000400

    _enable_privilege("SeIncreaseQuotaPrivilege")
    _enable_privilege("SeAssignPrimaryTokenPrivilege")
    # CreateProcessAsUser first: Windows documents that SeAssignPrimaryToken
    # is NOT required when the token is a RESTRICTED version of the caller's
    # own primary token, which is exactly what _medium_token builds. When the
    # token came from the shell instead, CreateProcessWithTokenW is the path
    # an administrator can take with SeImpersonatePrivilege alone.
    def create(as_user):
        buffer = ctypes.create_unicode_buffer(command)
        target = PROCESS_INFORMATION()
        if as_user:
            ok = advapi32.CreateProcessAsUserW(
                handle, None, buffer, None, None, False, flags, environment,
                None, ctypes.byref(startup), ctypes.byref(target))
        else:
            ok = advapi32.CreateProcessWithTokenW(
                handle, 0, None, buffer, flags, environment, None,
                ctypes.byref(startup), ctypes.byref(target))
        if not ok:
            return None, ctypes.get_last_error()
        return target, 0

    def wait(target):
        try:
            kernel32.WaitForSingleObject(target.hProcess, int(timeout * 1000))
            code = wintypes.DWORD(0)
            kernel32.GetExitCodeProcess(target.hProcess, ctypes.byref(code))
            return int(code.value)
        finally:
            kernel32.CloseHandle(target.hThread)
            kernel32.CloseHandle(target.hProcess)

    # STATUS_DLL_INIT_FAILED. The child was created and then died before
    # Python ran a single line, which on a restricted token means Windows
    # refused it the window station or desktop the creator asked for. It
    # depends on what the CALLING process has already done to its own station
    # -- a test runner that has built and torn down GUI toolkits and worker
    # threads is exactly such a process -- so a probe that gave up here would
    # report "no medium token on this host" for a host that plainly has one,
    # and a real measurement would silently become a skip.
    DLL_INIT_FAILED = 0xC0000142

    attempts = []
    for as_user in (True, False):
        target, error = create(as_user)
        api = "CreateProcessAsUserW" if as_user else "CreateProcessWithTokenW"
        if target is None:
            attempts.append("%s: create failed with %d" % (api, error))
            continue
        code = wait(target)
        if code != DLL_INIT_FAILED:
            return code
        attempts.append("%s: child exited 0x%08X (STATUS_DLL_INIT_FAILED)"
                        % (api, code))
    raise OSError("no medium-integrity process could be started (%s)"
                  % "; ".join(attempts))


WINSTA_ALL_ACCESS = 0x0000037F
DESKTOP_ALL_ACCESS = 0x000001FF
_CONTAINER_INHERIT = 0x02
_INHERIT_ONLY = 0x08
_OBJECT_INHERIT = 0x01
_NO_PROPAGATE_INHERIT = 0x04


def _grant_station_and_desktop(token):
    """Let the restricted token use this process's window station and desktop.

    A process created on a token that may not open the creator's window
    station dies at DLL initialisation with STATUS_DLL_INIT_FAILED, before a
    single line of Python runs. Whether that happens depends on the station
    the CALLING process ended up on, which is why the same probe worked from
    a plain shell and failed after a test run that had built and torn down a
    GUI toolkit. The documented remedy is to add the token's own user SID to
    the station and desktop DACLs; a window station needs two ACEs, one
    inherit-only for the desktops inside it and one for the station object
    itself. Best effort: a failure here is not fatal, because the creation
    may well succeed anyway.
    """
    try:
        import win32api
        import win32security
        import win32service
    except ImportError:
        return False
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    except Exception:
        return False
    ok = True
    targets = (
        (win32service.GetProcessWindowStation(),
         ((WINSTA_ALL_ACCESS, _CONTAINER_INHERIT | _INHERIT_ONLY | _OBJECT_INHERIT),
          (WINSTA_ALL_ACCESS, _NO_PROPAGATE_INHERIT))),
        (win32service.GetThreadDesktop(win32api.GetCurrentThreadId()),
         ((DESKTOP_ALL_ACCESS, 0),)),
    )
    for handle, grants in targets:
        try:
            descriptor = win32security.GetSecurityInfo(
                handle, win32security.SE_WINDOW_OBJECT,
                win32security.DACL_SECURITY_INFORMATION)
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                continue
            for rights, flags in grants:
                dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS,
                                           flags, rights, user)
            win32security.SetSecurityInfo(
                handle, win32security.SE_WINDOW_OBJECT,
                win32security.DACL_SECURITY_INFORMATION, None, None, dacl, None)
        except Exception:
            ok = False
    return ok


#: The only environment the probe child is given. A probe must answer the
#: same way whatever the PARENT has been doing to its own environment -- and a
#: test runner does a great deal to it. Inheriting a mutated block is how the
#: child ended up failing at DLL initialisation (0xC0000142) only when another
#: suite had run first, which turned a real measurement into a silent skip.
_CHILD_ENVIRONMENT_NAMES = (
    "SystemRoot", "windir", "SystemDrive", "PATHEXT", "COMSPEC",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "USERPROFILE",
    "USERNAME", "USERDOMAIN", "HOMEDRIVE", "HOMEPATH", "APPDATA",
    "LOCALAPPDATA", "ProgramData", "ProgramFiles", "TEMP", "TMP",
)


def _windows_directory():
    """``%SystemRoot%`` asked of Windows, not of the environment block."""
    import ctypes
    buffer = ctypes.create_unicode_buffer(260)
    if ctypes.windll.kernel32.GetSystemWindowsDirectoryW(buffer, 260):
        return buffer.value
    return os.environ.get("SystemRoot") or r"C:\Windows"


def _environment_block():
    """A minimal, explicit UTF-16 environment block for the probe child.

    Every variable an interpreter needs to start at all is supplied from
    Windows itself rather than copied out of ``os.environ``. A test runner
    mutates the process environment constantly -- ``mock.patch.dict(...,
    clear=True)`` is one call away -- and a child handed a block with no
    ``SystemRoot`` dies at DLL initialisation before Python runs, which is
    indistinguishable from "this host has no medium-integrity token" unless
    the block is built defensively.
    """
    import ctypes
    values = {}
    for name in _CHILD_ENVIRONMENT_NAMES:
        value = os.environ.get(name)
        if value:
            values[name] = value
    root = _windows_directory()
    values["SystemRoot"] = root
    values["windir"] = root
    values.setdefault("SystemDrive", os.path.splitdrive(root)[0] or "C:")
    values["COMSPEC"] = os.path.join(root, "System32", "cmd.exe")
    values.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    for name in ("TEMP", "TMP"):
        value = values.get(name)
        if not value or not os.path.isdir(value):
            values[name] = os.path.join(root, "Temp")
    values["PATH"] = ";".join([
        os.path.join(root, "System32"), root,
        os.path.join(root, "System32", "Wbem"),
        os.path.join(root, "System32", "WindowsPowerShell", "v1.0"),
    ])
    text = "".join("%s=%s\0" % item for item in sorted(values.items())) + "\0"
    # The BUFFER itself is returned, never a cast pointer into it: ctypes keeps
    # the object it owns alive, and a pointer into a buffer nobody holds a
    # reference to is a dangling one the moment this frame goes away.
    return ctypes.create_unicode_buffer(text)


def _enable_privilege(name):
    """Enable a privilege the process already holds. False when it has none."""
    try:
        import win32api
        import win32con
        import win32security
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY)
        luid = win32security.LookupPrivilegeValue(None, name)
        win32security.AdjustTokenPrivileges(
            token, False, [(luid, win32con.SE_PRIVILEGE_ENABLED)])
        return True
    except Exception:
        return False


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    spec_path = None
    out_path = None
    log_path = None
    while argv:
        flag = argv.pop(0)
        if flag == "--spec":
            spec_path = argv.pop(0)
        elif flag == "--out":
            out_path = argv.pop(0)
        elif flag == "--log":
            log_path = argv.pop(0)
    if log_path:
        # The child has no console: without this, a failure to import, a
        # missing dependency or an unhandled exception would be invisible and
        # the caller would only see "no usable result".
        try:
            stream = open(log_path, "w", encoding="utf-8")
            sys.stdout = stream
            sys.stderr = stream
        except OSError:
            pass
    if spec_path:
        with open(spec_path, "r", encoding="utf-8") as stream:
            spec = json.load(stream)
    else:
        spec = json.load(sys.stdin)
    report = run_here(spec)
    text = json.dumps(report, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as stream:
            stream.write(text)
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
