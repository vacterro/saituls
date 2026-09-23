"""Privileged storage-helper transport for SAITULS Secure Apps.

Attaching a VHDX and unlocking a BitLocker volume both require administrator
rights; launching Obsidian must NOT be elevated. Running the whole broker
elevated would solve the first problem and create the second, so the two live
in different processes:

    broker (medium integrity)  --named pipe-->  helper (high integrity)
        owns the session, the                      owns diskpart and the
        secret and the app                         BitLocker API calls
        process tree

The helper is started by **Task Scheduler**, through the fixed task
:mod:`sa_privtask` registers once during setup. That is the whole reason this
module no longer raises a UAC consent dialog: the elevation was approved at
installation time, for one fixed action, and ordinary unlock / lock / mount /
relock may start that action and nothing else.

Task Scheduler is the elevation mechanism. It is **not** the authentication
mechanism. A process being started by a scheduled task tells this module
nothing about the process that actually answered the pipe, so the identity is
proven from the kernel, every time, before a single byte of secret is written.

Direction, and why it flipped:

  a fixed task action cannot carry a per-spawn pipe name, so the elevated
  helper now OWNS the pipe (a fixed, per-user name derived from the account
  SID) and the broker connects to it. The verification flips with it: the
  broker asks ``GetNamedPipeServerProcessId`` who is serving, and the helper
  asks Windows -- by impersonating the caller for identification only -- who
  is connected.

Pipe hardening:

  * the helper's pipe carries an explicit DACL: this user, Administrators and
    SYSTEM. A named pipe with an inherited default DACL is reachable by
    anything in the session, and this one carries a volume unlock secret;
  * the server process is identified by the KERNEL before anything is sent:
    ``GetNamedPipeServerProcessId`` for the peer pid, its image path pinned
    to the PowerShell host recorded at installation, its elevation and
    integrity level read from its own token, its token user compared with
    this account and its session compared with this session;
  * the helper's greeting claims (``pid``, ``elevated``, ``user_sid``) are
    cross-checked against those measurements. ``{"elevated": true}`` from the
    peer is a claim, never evidence, and a disagreement closes the pipe;
  * the connection is opened at ``SECURITY_IDENTIFICATION``, so the elevated
    helper may identify this broker but can never impersonate it to act
    elsewhere with the user's rights;
  * the helper exits after an idle timeout with nobody connected, so an
    elevated command channel does not outlive the work it was started for.

Responses never echo a secret. The helper's own diagnostics are category
strings from a fixed table, for the same reason :mod:`sa_audit` uses an
allowlist.
"""
import base64
import json
import os
import threading
import time

import sa_privtask

HELPER_SCRIPT = sa_privtask.HELPER_SCRIPT
#: Extra image path accepted as the helper host. Exists for a packaged host
#: that is not stock powershell.exe; it widens an allow-list, never a denial.
HELPER_IMAGE_ENV = "SAITULS_SECURE_APPS_HELPER_IMAGE"

DEFAULT_CONNECT_TIMEOUT = 45.0
DEFAULT_CALL_TIMEOUT = 600.0
#: How long a silent Task Scheduler start may take to produce a pipe. Long
#: enough for a cold PowerShell host on a busy machine, short enough that a
#: failed start is reported instead of hanging a lock.
DEFAULT_START_TIMEOUT = 30.0
CONNECT_POLL_SECONDS = 0.25

GREETING_SCHEMA = "saituls.secure-apps.privileged-helper-greeting/1"

FILE_FLAG_OVERLAPPED = 0x40000000

SECURITY_SQOS_PRESENT = 0x00100000
SECURITY_IDENTIFICATION = 0x00010000


class HelperError(Exception):
    category = "internal"
    token = ""

    def __init__(self, message, category=None, token=""):
        Exception.__init__(self, message)
        if category:
            self.category = category
        if token:
            self.token = token


class HelperUnavailable(HelperError):
    category = "privileged_helper_unavailable"
    token = sa_privtask.LOCK_PRIVILEGED_HELPER_UNAVAILABLE


class HelperNotInstalled(HelperUnavailable):
    token = sa_privtask.PRIVILEGED_HELPER_NOT_INSTALLED


class HelperTampered(HelperUnavailable):
    category = "privileged_task_tampered"
    token = sa_privtask.PRIVILEGED_TASK_TAMPERED


class HelperIdentityError(HelperError):
    """The process on the other end of the pipe is not the helper we expect."""
    category = "internal"


def _win32():
    try:
        import win32file
        import win32pipe
        import pywintypes
    except ImportError as exc:
        raise HelperUnavailable(
            "the privileged storage helper needs pywin32 (pip install pywin32): %s"
            % exc)
    return win32file, win32pipe, pywintypes


def _win32_security():
    """The identity-checking half of pywin32. Same failure shape as _win32."""
    try:
        import ntsecuritycon
        import win32api
        import win32con
        import win32process
        import win32security
    except ImportError as exc:
        raise HelperUnavailable(
            "verifying the privileged helper needs pywin32 (pip install pywin32): %s"
            % exc)
    return win32api, win32con, win32process, win32security, ntsecuritycon


# --------------------------------------------------------------------------
# peer identity
# --------------------------------------------------------------------------
def process_is_elevated(pid):
    """True when *pid* runs with an elevated token AND high integrity.

    Two independent questions, both asked of the kernel rather than of the
    process itself: ``TokenElevation`` (did UAC hand it an elevated token)
    and ``TokenIntegrityLevel`` (is it actually running at high integrity or
    above). A peer that merely *claims* elevation over the pipe proves
    nothing; this is the measurement that does.
    """
    win32api, win32con, _proc, win32security, ntsecuritycon = _win32_security()
    handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                                  False, int(pid))
    try:
        token = win32security.OpenProcessToken(handle, win32security.TOKEN_QUERY)
    except Exception as exc:
        raise HelperIdentityError(
            "could not read the helper token (%s)" % type(exc).__name__)
    try:
        elevated = bool(win32security.GetTokenInformation(
            token, ntsecuritycon.TokenElevation))
        integrity_sid = win32security.GetTokenInformation(
            token, ntsecuritycon.TokenIntegrityLevel)[0]
        # The integrity level is the last sub-authority of S-1-16-<rid>.
        rid = integrity_sid.GetSubAuthority(
            integrity_sid.GetSubAuthorityCount() - 1)
        high = int(rid) >= int(ntsecuritycon.SECURITY_MANDATORY_HIGH_RID)
        return bool(elevated and high)
    finally:
        try:
            token.Close()
        except Exception:
            pass
        try:
            handle.Close()
        except Exception:
            pass


def process_user_sid(pid):
    """SID string of the account *pid* runs as, or None when unreadable."""
    try:
        win32api, win32con, _proc, win32security, _nt = _win32_security()
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, int(pid))
        try:
            token = win32security.OpenProcessToken(handle,
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
        finally:
            try:
                handle.Close()
            except Exception:
                pass
    except Exception:
        return None


def process_session_id(pid):
    """Terminal-services session of *pid*, or None when it cannot be read."""
    try:
        import win32process
        return int(win32process.ProcessIdToSessionId(int(pid)))
    except Exception:
        pass
    try:
        import win32ts
        return int(win32ts.ProcessIdToSessionId(int(pid)))
    except Exception:
        return None


def process_image_path(pid):
    """Full image path of *pid*, canonicalized for comparison.

    ``QueryFullProcessImageName`` is tried first on purpose: it is satisfied
    by ``PROCESS_QUERY_LIMITED_INFORMATION``, which a medium-integrity broker
    really is granted against a high-integrity process of the same user.
    ``GetModuleFileNameEx`` wants ``PROCESS_VM_READ``, which it is not.
    """
    win32api, win32con, win32process, _sec, _nt = _win32_security()
    handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                                  False, int(pid))
    try:
        try:
            path = win32process.QueryFullProcessImageName(handle, 0)
        except Exception:
            path = win32process.GetModuleFileNameEx(handle, 0)
    finally:
        try:
            handle.Close()
        except Exception:
            pass
    return os.path.normcase(os.path.normpath(str(path)))


def expected_helper_images(pinned=None):
    """The only images the helper is ever allowed to be running from.

    The helper is a PowerShell script, so the process on the other end is
    ``powershell.exe`` from the Windows directory -- never a copy somewhere
    writable, which is the whole point of pinning the path. The image
    recorded in the installation pin is included so that a packaged host is
    accepted only because an elevated installer wrote it down.
    """
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidates = [
        os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        os.path.join(root, "SysWOW64", "WindowsPowerShell", "v1.0", "powershell.exe"),
        os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "pwsh.exe"),
    ]
    if pinned:
        candidates.append(pinned)
    extra = os.environ.get(HELPER_IMAGE_ENV)
    if extra:
        candidates.append(extra)
    return tuple(os.path.normcase(os.path.normpath(c)) for c in candidates)


class HelperIdentity(object):
    """What the kernel says about the process serving the helper pipe."""

    __slots__ = ("pid", "image", "elevated", "user_sid", "session_id",
                 "greeting")

    def __init__(self, pid=None, image=None, elevated=None, user_sid=None,
                 session_id=None, greeting=None):
        self.pid = pid
        self.image = image
        self.elevated = elevated
        self.user_sid = user_sid
        self.session_id = session_id
        self.greeting = greeting or {}

    def to_dict(self):
        return {"pid": self.pid, "image": self.image, "elevated": self.elevated,
                "user_sid": self.user_sid, "session_id": self.session_id,
                "integrity": ("HIGH" if self.elevated else "MEDIUM")
                             if self.elevated is not None else None}


def verify_helper_identity(measured, pin=None, expected_sid=None,
                           expected_session=None):
    """Decide whether *measured* may receive a volume secret. Pure function.

    Separated from the pipe so that the rule -- high integrity, the pinned
    image, this account, this session, and a greeting that agrees with all of
    them -- is testable without a scheduled task, a pipe or elevation.
    Returns the list of reasons it is NOT acceptable; empty means acceptable.
    """
    pin = pin or {}
    reasons = []
    if not measured.pid:
        reasons.append("the process serving the helper pipe could not be identified")
    if measured.elevated is not True:
        reasons.append("the process serving the helper pipe is not running elevated "
                       "at high integrity; no secret will be sent to it")
    allowed = expected_helper_images(pin.get("helper_host_image"))
    if not measured.image or measured.image not in allowed:
        reasons.append("the process serving the helper pipe is not the expected "
                       "PowerShell host: %s" % measured.image)
    if expected_sid and measured.user_sid and \
            str(measured.user_sid).upper() != str(expected_sid).upper():
        reasons.append("the helper runs as a different account than this broker")
    if (expected_session is not None and measured.session_id is not None
            and int(measured.session_id) != int(expected_session)):
        reasons.append("the helper runs in a different Windows session than this "
                       "broker")
    greeting = measured.greeting or {}
    if greeting.get("schema") != GREETING_SCHEMA:
        reasons.append("the helper greeting uses an unexpected schema")
    if measured.pid and int(greeting.get("pid") or -1) != int(measured.pid):
        reasons.append("the helper greeting reports a different process id than "
                       "the one serving the pipe")
    if greeting.get("elevated") is not True:
        reasons.append("the helper reports it is not running elevated")
    if expected_sid and greeting.get("user_sid") and \
            str(greeting["user_sid"]).upper() != str(expected_sid).upper():
        reasons.append("the helper greeting reports a different account")
    pinned_digest = pin.get("definition_fingerprint")
    claimed = greeting.get("definition_fingerprint")
    if pinned_digest and claimed and claimed != pinned_digest:
        reasons.append("the helper was started from a task definition that does "
                       "not match the one pinned at installation: %s"
                       % sa_privtask.PRIVILEGED_TASK_TAMPERED)
    pinned_bundle = pin.get("runtime_bundle_fingerprint")
    claimed_bundle = greeting.get("runtime_bundle_fingerprint")
    if pinned_bundle and claimed_bundle and claimed_bundle != pinned_bundle:
        reasons.append("the helper is serving a protected runtime bundle that does "
                       "not match the one pinned at installation: %s"
                       % sa_privtask.PRIVILEGED_TASK_TAMPERED)
    return reasons


# --------------------------------------------------------------------------
# the channel
# --------------------------------------------------------------------------
class PrivilegedHelper(object):
    """Connects to the scheduled helper, proves who answered, and calls it.

    Lifetime is a **lease**, not a process: :meth:`acquire` while privileged
    work is in flight or the volume is mounted, :meth:`release` when it is
    not. Releasing the last lease disconnects the pipe and leaves the helper
    to time out on its own; the next unlock starts it again through the same
    task, silently. Nothing here ever elevates anything.
    """

    def __init__(self, script_dir=None, pin=None, pin_file=None, runner=None,
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 start_timeout=DEFAULT_START_TIMEOUT, verify_server=True,
                 starter=None, clock=time.monotonic, sleep=time.sleep):
        self.script_dir = script_dir or os.path.dirname(os.path.abspath(__file__))
        self.pin_file = pin_file
        self._pin = pin
        self._runner = runner
        self.connect_timeout = connect_timeout
        self.start_timeout = start_timeout
        self.verify_server = verify_server
        self.clock = clock
        self.sleep = sleep
        #: Injected by tests and by the setup validation; production leaves it
        #: None so that starting means exactly ``schtasks /Run``.
        self._starter = starter
        self.identity = HelperIdentity()
        self.pipe_name = None
        self.started_task = False
        self._pipe = None
        self._lock = threading.RLock()
        self._next_id = 0
        self._leases = 0
        self._verified = False

    # -- installation facts -----------------------------------------------
    def pin(self):
        """The installation pin, or a refusal. Cached for this channel."""
        if self._pin is None:
            pin, problem = sa_privtask.load_pin(self.pin_file)
            if pin is None:
                raise HelperNotInstalled(
                    "%s. Run Secure Apps setup -> Install privileged helper."
                    % problem, "privileged_helper_unavailable")
            self._pin = pin
        return self._pin

    @property
    def task_path(self):
        try:
            return self.pin().get("task_path") or sa_privtask.TASK_PATH
        except HelperError:
            return sa_privtask.TASK_PATH

    @property
    def idle_timeout_seconds(self):
        try:
            return int(self.pin().get("idle_timeout_seconds")
                       or sa_privtask.DEFAULT_IDLE_TIMEOUT_SECONDS)
        except (HelperError, TypeError, ValueError):
            return sa_privtask.DEFAULT_IDLE_TIMEOUT_SECONDS

    # -- lifecycle ---------------------------------------------------------
    @property
    def running(self):
        return self._pipe is not None and self._verified

    def _resolve_pipe_name(self):
        pin = self.pin()
        sid = sa_privtask.current_user_sid()
        expected = sa_privtask.pipe_name_for_sid(sid)
        pinned = pin.get("pipe_name")
        if pinned and pinned != expected:
            raise HelperTampered(
                "the pinned helper pipe name does not belong to this account: %s"
                % sa_privtask.PRIVILEGED_TASK_TAMPERED)
        self.pipe_name = pinned or expected
        return self.pipe_name

    def _verify_task_definition(self):
        """Refuse to start a task whose definition has moved. Fail closed."""
        verdict = sa_privtask.verify_installation(
            script_dir=self.script_dir, runner=self._runner,
            pin_file=self.pin_file)
        if verdict.ok:
            return verdict
        if not verdict.installed:
            raise HelperNotInstalled(
                "the privileged helper task is not installed (%s): %s"
                % (verdict.token or sa_privtask.PRIVILEGED_HELPER_NOT_INSTALLED,
                   "; ".join(verdict.reasons)))
        raise HelperTampered(
            "%s: %s" % (sa_privtask.PRIVILEGED_TASK_TAMPERED,
                        "; ".join(verdict.reasons)))

    def _start_task(self):
        if self._starter is not None:
            return self._starter(self.task_path)
        return sa_privtask.start_task(self.task_path, runner=self._runner)

    def ensure(self):
        """Reuse a verified helper, or start the registered task and prove one.

        The silent runtime path in full: if a verified helper is already
        connected, nothing happens. Otherwise the pinned task definition is
        re-checked, Task Scheduler is asked to start that task, the pipe is
        awaited for a bounded time, and the process that answers is verified
        from the kernel before it is used. No consent dialog anywhere.
        """
        with self._lock:
            if self.running:
                return self
            self._resolve_pipe_name()
            self._verify_task_definition()
            handle = self._try_connect(timeout=0.0)
            if handle is None:
                try:
                    self._start_task()
                except sa_privtask.PrivilegeTaskError as exc:
                    raise HelperUnavailable(
                        "%s: %s" % (sa_privtask.LOCK_PRIVILEGED_HELPER_UNAVAILABLE,
                                    exc))
                self.started_task = True
                handle = self._try_connect(timeout=self.start_timeout)
            if handle is None:
                raise HelperUnavailable(
                    "the privileged helper did not answer within %.0fs of being "
                    "started by Task Scheduler: %s"
                    % (self.start_timeout,
                       sa_privtask.LOCK_PRIVILEGED_HELPER_UNAVAILABLE))
            self._pipe = handle
            try:
                greeting = self._handshake()
                if self.verify_server:
                    self._verify_peer(greeting)
                else:
                    self.identity = HelperIdentity(greeting=greeting)
                self._verified = True
            except Exception:
                self._drop()
                raise
            return self

    #: Historical name. Callers that predate the lease API say ``start()``.
    def start(self):
        return self.ensure()

    def _try_connect(self, timeout):
        win32file, win32pipe, pywintypes = _win32()
        deadline = self.clock() + max(0.0, float(timeout))
        while True:
            try:
                handle = win32file.CreateFile(
                    self.pipe_name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None, win32file.OPEN_EXISTING,
                    # Identification only: the elevated helper may learn who
                    # we are, and may never impersonate us to act elsewhere.
                    FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, None)
                return handle
            except pywintypes.error:
                if self.clock() >= deadline:
                    return None
                self.sleep(CONNECT_POLL_SECONDS)

    def _handshake(self):
        raw = self._read_line(timeout=self.connect_timeout)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise HelperUnavailable("the privileged helper sent a malformed greeting")
        if not isinstance(payload, dict):
            raise HelperUnavailable("the privileged helper sent a malformed greeting")
        return payload

    def _measure_peer(self, greeting):
        _file, win32pipe, _types = _win32()
        try:
            pid = int(win32pipe.GetNamedPipeServerProcessId(self._pipe))
        except Exception as exc:
            raise HelperIdentityError(
                "could not identify the process serving the helper pipe (%s)"
                % type(exc).__name__)
        try:
            image = process_image_path(pid)
        except Exception as exc:
            raise HelperIdentityError(
                "could not read the helper image path (%s)" % type(exc).__name__)
        return HelperIdentity(pid=pid, image=image,
                              elevated=process_is_elevated(pid),
                              user_sid=process_user_sid(pid),
                              session_id=process_session_id(pid),
                              greeting=greeting)

    def _verify_peer(self, greeting):
        measured = self._measure_peer(greeting)
        self.identity = measured
        reasons = verify_helper_identity(
            measured, pin=self._pin or {},
            expected_sid=sa_privtask.current_user_sid(),
            expected_session=process_session_id(os.getpid()))
        if reasons:
            raise HelperIdentityError("; ".join(reasons))
        return True

    # -- lease -------------------------------------------------------------
    def acquire(self):
        """Take a lease and guarantee a verified helper for its duration."""
        with self._lock:
            self.ensure()
            self._leases += 1
            return self

    def release(self):
        """Drop a lease. The last one disconnects and lets the helper idle out.

        Disconnecting is not killing: the helper starts its own idle timer and
        exits by itself, and the next unlock silently starts it again through
        Task Scheduler. That is what keeps an elevated process from sitting
        there between unlocks without costing a consent dialog to get it back.
        """
        with self._lock:
            if self._leases > 0:
                self._leases -= 1
            if self._leases == 0:
                self._drop()
            return self._leases

    @property
    def leases(self):
        return self._leases

    def _drop(self):
        """Close the pipe handle. The helper stays alive and times out."""
        win32file = None
        try:
            win32file, _pipe_mod, _types = _win32()
        except HelperError:
            pass
        if self._pipe is not None and win32file is not None:
            try:
                win32file.CloseHandle(self._pipe)
            except Exception:
                pass
        self._pipe = None
        self._verified = False
        self.identity = HelperIdentity()

    def close(self):
        """Ask the helper to exit now, then disconnect. Used at shutdown."""
        with self._lock:
            if self._pipe is not None:
                try:
                    self._write_line(json.dumps({"id": -1, "op": "shutdown",
                                                 "args": {}}))
                except Exception:
                    pass
            self._leases = 0
            self._drop()

    # -- framing ----------------------------------------------------------
    def _write_line(self, text, timeout=DEFAULT_CALL_TIMEOUT):
        import win32event
        win32file, _p, pywintypes = _win32()
        data = (text + "\n").encode("utf-8")
        ol = pywintypes.OVERLAPPED()
        ev = win32event.CreateEvent(None, True, False, None)
        ol.hEvent = ev
        try:
            try:
                rc, _written = win32file.WriteFile(self._pipe, data, ol)
            except pywintypes.error as exc:
                if exc.winerror == 997:
                    rc = 997
                else:
                    self._drop()
                    raise HelperError("privileged helper pipe write failed: %s" % exc,
                                      "storage_unlock_failed")
            if rc == 997:
                timeout_ms = max(0, int(float(timeout) * 1000))
                wait_rc = win32event.WaitForSingleObject(ev, timeout_ms)
                if wait_rc == win32event.WAIT_TIMEOUT:
                    try:
                        win32file.CancelIo(self._pipe)
                    except Exception:
                        pass
                    win32event.WaitForSingleObject(ev, 50)
                    self._drop()
                    raise HelperError("the privileged helper write timed out",
                                      "storage_unlock_failed")
                elif wait_rc != win32event.WAIT_OBJECT_0:
                    self._drop()
                    raise HelperError("privileged helper pipe write failed during wait",
                                      "storage_unlock_failed")
                try:
                    win32file.GetOverlappedResult(self._pipe, ol, False)
                except pywintypes.error as exc:
                    self._drop()
                    raise HelperError("privileged helper pipe write completion failed: %s" % exc,
                                      "storage_unlock_failed")
        finally:
            try:
                win32file.CloseHandle(ev)
            except Exception:
                pass

    def _read_line(self, timeout=DEFAULT_CALL_TIMEOUT):
        """Bounded read of one newline-terminated response (SRC-027 PERF-001).

        The channel is invalidated on timeout, so a hung helper cannot hold
        the broker worker indefinitely. One monotonic deadline covers partial
        chunks; a next call opens a fresh verified connection.
        """
        import win32event
        win32file, _p, pywintypes = _win32()
        deadline = time.monotonic() + max(0.0, float(timeout))
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._cancel_pending_io_and_drop()
                raise HelperError("the privileged helper did not answer in time",
                                  "storage_unlock_failed")
            ol = pywintypes.OVERLAPPED()
            ev = win32event.CreateEvent(None, True, False, None)
            ol.hEvent = ev
            data = b""
            try:
                try:
                    rc, buf = win32file.ReadFile(self._pipe, 4096, ol)
                except pywintypes.error as exc:
                    if exc.winerror == 997:
                        rc = 997
                        buf = getattr(exc, "buffer", None)
                    elif exc.winerror in (109, 233):
                        self._drop()
                        raise HelperUnavailable("the privileged helper disconnected")
                    else:
                        self._drop()
                        raise HelperError("privileged helper pipe read failed: %s" % exc,
                                          "storage_unlock_failed")
                if rc == 997:
                    timeout_ms = max(0, int(remaining * 1000))
                    wait_rc = win32event.WaitForSingleObject(ev, timeout_ms)
                    if wait_rc == win32event.WAIT_TIMEOUT:
                        try:
                            win32file.CancelIo(self._pipe)
                        except Exception:
                            pass
                        win32event.WaitForSingleObject(ev, 50)
                        self._drop()
                        raise HelperError("the privileged helper did not answer in time",
                                          "storage_unlock_failed")
                    elif wait_rc != win32event.WAIT_OBJECT_0:
                        self._drop()
                        raise HelperError("privileged helper pipe wait failed",
                                          "storage_unlock_failed")
                    try:
                        n_bytes = win32file.GetOverlappedResult(self._pipe, ol, False)
                        data = bytes(buf)[:n_bytes]
                    except pywintypes.error as exc:
                        self._drop()
                        if exc.winerror in (109, 233):
                            raise HelperUnavailable("the privileged helper disconnected")
                        raise HelperError("privileged helper read completion failed: %s" % exc,
                                          "storage_unlock_failed")
                elif rc == 0:
                    try:
                        n_bytes = win32file.GetOverlappedResult(self._pipe, ol, False)
                        data = bytes(buf)[:n_bytes]
                    except Exception:
                        data = bytes(buf) if isinstance(buf, (bytes, bytearray, memoryview)) else b""
                else:
                    self._drop()
                    raise HelperError("privileged helper pipe read failed",
                                      "storage_unlock_failed")
            finally:
                try:
                    win32file.CloseHandle(ev)
                except Exception:
                    pass

            if not data:
                self._drop()
                raise HelperError("privileged helper pipe read failed",
                                  "storage_unlock_failed")
            chunks.extend(data)
            if b"\n" in chunks:
                line, _sep, rest = bytes(chunks).partition(b"\n")
                if rest:
                    self._drop()
                    raise HelperError("the privileged helper desynchronised",
                                      "storage_unlock_failed")
                return line.decode("utf-8", "replace")

    def _cancel_pending_io_and_drop(self, overlapped=None):
        if self._pipe is not None:
            try:
                win32file, _p, _t = _win32()
                win32file.CancelIo(self._pipe)
            except Exception:
                pass
        self._drop()

    # -- request ----------------------------------------------------------
    def call(self, op, timeout=DEFAULT_CALL_TIMEOUT, **args):
        """One request, one response, on a pipe whose peer has been proven.

        A helper that has already idled out is silently started again and the
        request is retried EXACTLY once, and only when the failure happened
        while writing -- an answer that was lost after the helper acted on it
        must not be replayed.
        """
        with self._lock:
            if self._pipe is None:
                self.ensure()
            if self.verify_server and not self._verified:
                # Belt and braces: ensure() cannot reach here without having
                # verified, and a future refactor that breaks that must fail
                # closed rather than put a volume secret on an unproven pipe.
                raise HelperIdentityError(
                    "the helper identity was never verified; refusing to send "
                    "anything on this pipe")
            self._next_id += 1
            request = {"id": self._next_id, "op": op, "args": args}
            payload = json.dumps(request, separators=(",", ":"))
            # request is dropped immediately: for unlock/create it holds the
            # base64 volume secret and must not linger in a local.
            del request
            args.clear()
            try:
                self._write_line(payload)
            except HelperError:
                raise
            except Exception:
                self._drop()
                self.ensure()
                self._write_line(payload)
            finally:
                payload = None
            try:
                raw = self._read_line(timeout=timeout)
            finally:
                # No lease means no mount and no transaction in flight, so the
                # channel closes again and the helper is free to reach its
                # idle timeout. Holding an elevated pipe open "just in case"
                # is exactly the long-lived attack surface this design avoids.
                if self._leases == 0:
                    self._drop()
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            raise HelperError("the privileged helper returned malformed JSON",
                              "storage_unlock_failed")
        if not response.get("ok"):
            raise HelperError(str(response.get("message") or "helper reported failure"),
                              str(response.get("error") or "storage_unlock_failed"))
        return response.get("result") or {}

    def helper_info(self, timeout=30.0):
        """The helper's own view of itself. Diagnostics; never authority."""
        return self.call("helper_info", timeout=timeout)

    def fido_capabilities(self, timeout=30.0):
        return self.call("fido_capabilities", timeout=timeout)

    def fido_create(self, rp_id, credential_profile, user_id_b64=None,
                    user_verification="required", require_hmac_secret=True,
                    pin=None, timeout_seconds=60):
        args = {
            "rp_id": rp_id,
            "credential_profile": credential_profile,
            "user_verification": user_verification,
            "require_hmac_secret": require_hmac_secret,
            "timeout_seconds": timeout_seconds,
        }
        if user_id_b64:
            args["user_id_b64"] = user_id_b64
        if pin:
            args["pin"] = pin
        try:
            return self.call("fido_create", timeout=float(timeout_seconds + 30), **args)
        finally:
            pin = None
            args.clear()

    def fido_hmac(self, rp_id, credential_ids_b64, salt_b64,
                  user_verification="required", pin=None, timeout_seconds=60):
        args = {
            "rp_id": rp_id,
            "credential_ids_b64": credential_ids_b64,
            "salt_b64": salt_b64,
            "user_verification": user_verification,
            "timeout_seconds": timeout_seconds,
        }
        if pin:
            args["pin"] = pin
        try:
            return self.call("fido_hmac", timeout=float(timeout_seconds + 30), **args)
        finally:
            pin = None
            args.clear()


def encode_secret(secret_buffer):
    """Base64 for pipe transport. The caller zeroizes the buffer afterwards."""
    raw = secret_buffer.bytes() if hasattr(secret_buffer, "bytes") else bytes(secret_buffer)
    try:
        return base64.b64encode(raw).decode("ascii")
    finally:
        del raw
