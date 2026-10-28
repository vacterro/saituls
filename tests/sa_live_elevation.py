# encoding: utf-8
"""Medium-integrity orchestrator for the LIVE D / LIVE E native recovery doors.

Why this file exists
--------------------
BitLocker's ``Mount-DiskImage`` needs a high-integrity token, but the Portable
Vault acceptance runner must stay at medium integrity: gates B, C, F, G, H and I
are only meaningful if the thing driving them lives on the same side of the
privilege boundary as production. So the run borrows privilege for exactly one
disposable job -- the native recovery child in ``sa_native_recovery.ps1`` --
through a single UAC consent, and gives it back when that child exits.

    MEDIUM acceptance runner
        |  runas (one UAC prompt)
        v
    ELEVATED disposable native-recovery child  -> Door 2 + Door 3
        ^
        |  per-run named pipe, DACL = this Windows user only

No production SAITULS component, no production broker and no machine-wide UAC
setting is elevated by this module. The child is disposable and imports nothing
from ``Scripts/secure_apps``.

Secret handling
---------------
The Portable Password and the Recovery Password never touch argv, the
environment, a temporary file, the registry or the clipboard. The only thing
handed to the elevated child on its command line is the pipe name -- not a
secret -- plus the vault path and the expected canary digest. Each door secret
is written into the pipe only after the child has connected and announced
itself, is held in memory by the child for the duration of one unlock, and is
never written to the evidence file; the child reports only a status, the canary
digest it observed and whether the volume detached.

Protocol
--------
One pipe, 4-byte big-endian length prefix then UTF-8 JSON, matching the child:

    child -> parent   {"type":"hello","pid":..,"elevated":true,"user":..,...}
    parent -> child   {"type":"door","door":"Password","secret_b64":"..."}
    child -> parent   {"type":"result","door":..,"status":"PASS|FAIL",...}
    parent -> child   {"type":"close"}
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import json
import os
import secrets
import struct
import subprocess
import sys
import time
from pathlib import Path

__all__ = [
    "NativeRecoveryError",
    "NativeRecoveryDenied",
    "NativeRecoveryWorker",
    "run_live_doors",
    "ELEVATION_PROMPT",
]

#: The single sentence the operator is shown before the UAC dialog appears.
ELEVATION_PROMPT = "Approve the UAC prompt now."

_SCRIPT = Path(__file__).with_name("sa_native_recovery.ps1")

_HELLO_TIMEOUT_S = 600.0     # covers the operator reaching the UAC dialog
_DOOR_TIMEOUT_S = 300.0      # one unlock + canary + lock + detach
_CONNECT_TIMEOUT_S = 300.0

# ShellExecuteExW flags (winuser.h)
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SEE_MASK_NOASYNC = 0x00000100

_ERROR_CANCELLED = 1223
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PIPE_BUSY = 231

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


_shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
_shell32.ShellExecuteExW.restype = wintypes.BOOL

_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.PeekNamedPipe.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_kernel32.PeekNamedPipe.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_WAIT_TIMEOUT = 0x00000102
_STILL_ACTIVE = 259


class NativeRecoveryError(RuntimeError):
    """The elevated child could not be driven to a verdict."""


class NativeRecoveryDenied(NativeRecoveryError):
    """The operator declined the UAC consent dialog."""


class NativeRecoveryWorker:
    """Owns one elevated native-recovery child for the duration of a live run.

    ``start()`` triggers the single UAC prompt; ``door()`` may then be called any
    number of times (the live runner calls it twice: Door 2 then Door 3) so the
    operator consents exactly once for both doors.
    """

    def __init__(
        self,
        vault_path,
        expected_canary_sha256,
        evidence_path=None,
        powershell=None,
        log=None,
        connect_timeout_s=None,
    ) -> None:
        self.vault_path = str(vault_path)
        self.expected_canary_sha256 = (expected_canary_sha256 or "").lower()
        self.evidence_path = str(evidence_path) if evidence_path else ""
        self.powershell = powershell or _find_powershell()
        self.connect_timeout_s = (
            _CONNECT_TIMEOUT_S if connect_timeout_s is None else float(connect_timeout_s)
        )
        self._log = log or (lambda _m: None)

        # Set by tests that launch the child themselves with subprocess: lets the
        # transport notice a dead child instead of waiting out the timeout.
        self.liveness = None

        # Unpredictable per run: nothing can pre-create the pipe or race it by
        # name, and the name is useless to anyone else because the DACL on the
        # server instance admits only this Windows user.
        self.pipe_name = "sa_native_recovery_" + secrets.token_hex(16)
        self.child_pid = None
        self.hello = None

        self._proc_handle = None
        self._fd = None

    # ------------------------------------------------------------- lifecycle
    def start(self, prompt_writer=None) -> dict:
        """Prompt for consent, launch the elevated child, and read its hello.

        The prompt is emitted before ``ShellExecuteExW`` because that call blocks
        inside the UAC dialog until the operator answers it.
        """
        if not _SCRIPT.exists():
            raise NativeRecoveryError(f"native recovery script missing: {_SCRIPT}")
        if not Path(self.vault_path).exists():
            raise NativeRecoveryError(f"disposable vault copy missing: {self.vault_path}")

        parameters = [
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(_SCRIPT),
            "-PipeName", self.pipe_name,
            "-VaultPath", self.vault_path,
            "-ExpectedCanarySha256", self.expected_canary_sha256,
        ]
        if self.evidence_path:
            parameters += ["-EvidencePath", self.evidence_path]

        command_line = subprocess.list2cmdline([self.powershell, *parameters])
        # The gate that matters, asserted rather than assumed: no door secret
        # can reach the elevated process through its command line, because no
        # door secret exists yet at this point in the run.
        for forbidden in ("-Secret", "-Password", "-RecoveryPassword"):
            if forbidden in command_line:
                raise NativeRecoveryError(
                    f"refusing to launch: {forbidden} would ride on the elevated command line"
                )

        if prompt_writer is not None:
            prompt_writer(ELEVATION_PROMPT)

        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC
        info.lpVerb = "runas"
        info.lpFile = self.powershell
        info.lpParameters = subprocess.list2cmdline(parameters)
        info.lpDirectory = str(Path(self.vault_path).parent)
        info.nShow = 1  # SW_SHOWNORMAL: the child needs a console to be disposable

        if not _shell32.ShellExecuteExW(ctypes.byref(info)):
            err = ctypes.get_last_error()
            if err == _ERROR_CANCELLED:
                raise NativeRecoveryDenied(
                    "UAC consent was declined; the operator must approve the prompt "
                    "for gates D and E to run"
                )
            raise NativeRecoveryError(f"ShellExecuteExW(runas) failed: {ctypes.FormatError(err)}")

        self._proc_handle = info.hProcess
        self._fd = self._connect()

        self.hello = self._read_frame(timeout=_HELLO_TIMEOUT_S)
        if self.hello.get("type") != "hello":
            raise NativeRecoveryError(f"unexpected first frame from child: {self.hello!r}")
        self.child_pid = self.hello.get("pid")
        if not self.hello.get("elevated"):
            raise NativeRecoveryError(
                "elevated child reports it is NOT elevated; refusing to claim a live pass"
            )
        return self.hello

    def door(self, method: str, secret: str) -> dict:
        """Run one native recovery door with the given secret.

        The secret is written into the pipe *after* the child connected, and is
        deliberately not stored on this object: it lives in the caller's frame
        for one call and in the child's memory for one unlock.
        """
        if self._fd is None:
            raise NativeRecoveryError("worker not started")
        if method not in ("Password", "RecoveryPassword"):
            raise ValueError(f"unknown door method {method!r}")

        # Taken from the one string that is about to be sent, before anything is
        # written, so the later comparison cannot be fooled by a local cleanup.
        expected_digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        payload = secret.encode("utf-8")
        try:
            frame = {"type": "door", "door": method, "secret_b64": _b64(payload)}
        finally:
            # Clears this frame's local byte copy. The caller's str stays theirs;
            # this method neither keeps nor owns it.
            payload = b""
        try:
            self._write_frame(frame)
        finally:
            frame = None

        reply = self._read_frame(timeout=_DOOR_TIMEOUT_S)
        if reply.get("type") != "result" or reply.get("door") != method:
            raise NativeRecoveryError(f"unexpected reply for {method}: {reply!r}")

        result = {
            "door": method,
            "status": str(reply.get("status", "FAIL")),
            "canary_sha256": str(reply.get("canary_sha256", "")).lower(),
            "detach_confirmed": bool(reply.get("detach_confirmed", False)),
            "mount": str(reply.get("mount", "")),
            "pre_unlock_lock_status": str(reply.get("pre_unlock_lock_status", "")),
            "pre_unlock_protectors": str(reply.get("pre_unlock_protectors", "")),
            "secret_sha256": str(reply.get("secret_sha256", "")),
            "secret_length": reply.get("secret_length"),
            "attempts": reply.get("attempts") or [],
            "detail": str(reply.get("detail", "")),
        }

        # Proves the secret the child used is byte-identical to the one the
        # operator knows. Without this a transport fault would look exactly like
        # a wrong-key rejection, and Door 2 would be blamed for a bug in the pipe.
        result["secret_digest_matches"] = result["secret_sha256"] == expected_digest
        if not result["secret_digest_matches"]:
            result["status"] = "FAIL"
            result["detail"] = (
                "the elevated child received a different secret than the one sent "
                f"(child {result['secret_sha256'][:16]}.. vs parent {expected_digest[:16]}..); "
                + result["detail"]
            )
            return result

        if result["status"] == "PASS":
            if result["canary_sha256"] != self.expected_canary_sha256:
                result["status"] = "FAIL"
                result["detail"] = (
                    "canary digest mismatch: parent expected "
                    f"{self.expected_canary_sha256}, child reported {result['canary_sha256']}"
                )
            elif not result["detach_confirmed"]:
                result["status"] = "FAIL"
                result["detail"] = "child did not confirm the volume detached"
        return result

    def close(self, timeout_s: float = 30.0) -> int | None:
        """Ask the child to exit, then wait for it and reap its exit code."""
        if self._fd is not None:
            try:
                self._write_frame({"type": "close"})
            except OSError:
                pass
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        if self._proc_handle is None:
            return None
        try:
            _kernel32.WaitForSingleObject(self._proc_handle, int(timeout_s * 1000))
            code = wintypes.DWORD()
            if _kernel32.GetExitCodeProcess(self._proc_handle, ctypes.byref(code)):
                return int(code.value)
            return None
        finally:
            _kernel32.CloseHandle(self._proc_handle)
            self._proc_handle = None

    def __enter__(self) -> "NativeRecoveryWorker":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ pipe
    def _connect(self) -> int:
        """Open the child's pipe, waiting for it to publish the server end."""
        path = "\\\\.\\pipe\\" + self.pipe_name
        deadline = time.monotonic() + self.connect_timeout_s
        last_error = None
        while time.monotonic() < deadline:
            if self._child_exited():
                raise NativeRecoveryError(
                    f"elevated child exited (code {self._snapshot_exit_code()}) before connecting"
                )
            handle = _kernel32.CreateFileW(
                path,
                _GENERIC_READ | _GENERIC_WRITE,
                0,
                None,
                _OPEN_EXISTING,
                0,
                None,
            )
            if handle and handle != _INVALID_HANDLE_VALUE:
                import msvcrt

                fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
                if fd < 0:
                    _kernel32.CloseHandle(handle)
                    raise NativeRecoveryError("open_osfhandle failed on the recovery pipe")
                return fd
            last_error = ctypes.get_last_error()
            if last_error not in (_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY):
                raise NativeRecoveryError(
                    f"cannot connect to recovery pipe: {ctypes.FormatError(last_error)}"
                )
            time.sleep(0.05)
        raise NativeRecoveryError(
            f"timed out after {self.connect_timeout_s:.0f}s waiting for the recovery pipe "
            f"(last error {ctypes.FormatError(last_error) if last_error else 'none'})"
        )

    def _write_frame(self, obj: dict) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        os.write(self._fd, struct.pack(">I", len(body)) + body)

    def _read_frame(self, timeout: float) -> dict:
        header = self._read_exact(4, timeout)
        (length,) = struct.unpack(">I", header)
        if length <= 0 or length > 1 << 20:
            raise NativeRecoveryError(f"invalid frame length {length} from child")
        return json.loads(self._read_exact(length, timeout).decode("utf-8"))

    def _read_exact(self, count: int, timeout: float) -> bytes:
        import msvcrt

        handle = msvcrt.get_osfhandle(self._fd)
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while len(buf) < count:
            available = wintypes.DWORD(0)
            if not _kernel32.PeekNamedPipe(
                handle, None, 0, None, ctypes.byref(available), None
            ):
                err = ctypes.get_last_error()
                if err == 109:  # ERROR_BROKEN_PIPE
                    raise NativeRecoveryError("elevated child closed the recovery pipe")
                raise NativeRecoveryError(f"pipe read failed: {ctypes.FormatError(err)}")
            if available.value:
                chunk = os.read(self._fd, count - len(buf))
                if not chunk:
                    raise NativeRecoveryError("recovery pipe closed mid-frame")
                buf += chunk
                continue
            if self._child_exited():
                raise NativeRecoveryError(
                    f"elevated child exited (code {self._snapshot_exit_code()}) "
                    "while a reply was outstanding"
                )
            if time.monotonic() > deadline:
                raise NativeRecoveryError(f"timed out after {timeout:.0f}s waiting for the child")
            time.sleep(0.01)
        return bytes(buf)

    # --------------------------------------------------------------- process
    def _child_exited(self) -> bool:
        if self.liveness is not None and self.liveness():
            return True
        if self._proc_handle is None:
            return False
        return (
            _kernel32.WaitForSingleObject(self._proc_handle, 0) != _WAIT_TIMEOUT
        )

    def _snapshot_exit_code(self):
        if self._proc_handle is None:
            return None
        code = wintypes.DWORD(_STILL_ACTIVE)
        _kernel32.GetExitCodeProcess(self._proc_handle, ctypes.byref(code))
        return int(code.value)


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode("ascii")


def _find_powershell() -> str:
    """Resolve the Windows PowerShell host used for the elevated child."""
    for candidate in (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return "powershell.exe"


def run_live_doors(
    vault_path,
    expected_canary_sha256,
    portable_password,
    recovery_password,
    evidence_path=None,
    prompt_writer=None,
    log=None,
) -> dict:
    """Drive Door 2 then Door 3 with a single UAC consent.

    Mirrors the live runner's D/E contract: the same disposable vault copy is
    used for both doors, no volume stays mounted between them, and neither door
    starts until the previous one has proved its detach.
    """
    log = log or (lambda _m: None)
    worker = NativeRecoveryWorker(
        vault_path=vault_path,
        expected_canary_sha256=expected_canary_sha256,
        evidence_path=evidence_path,
        log=log,
    )
    outcome = {
        "elevation_prompt": ELEVATION_PROMPT,
        "child_pid": None,
        "child_user": None,
        "child_client_user": None,
        "D": {"door": "Password", "status": "FAIL", "detail": "not attempted"},
        "E": {"door": "RecoveryPassword", "status": "FAIL", "detail": "not attempted"},
        "child_exit_code": None,
    }
    try:
        hello = worker.start(prompt_writer=prompt_writer)
        outcome["child_pid"] = hello.get("pid")
        outcome["child_user"] = hello.get("user")
        outcome["child_client_user"] = hello.get("client_user")
        log(
            "elevated native recovery child pid=%s user=%s client=%s"
            % (hello.get("pid"), hello.get("user"), hello.get("client_user"))
        )

        outcome["D"] = worker.door("Password", portable_password)
        log(f"door 2 (portable password): {outcome['D']['status']}")
        # No volume is left mounted: the child locks and detaches inside each
        # door and proves it before replying, which is what clears Door 3.
        outcome["E"] = worker.door("RecoveryPassword", recovery_password)
        log(f"door 3 (recovery password): {outcome['E']['status']}")
    except NativeRecoveryDenied as exc:
        outcome["denied"] = str(exc)
        outcome["D"] = {"door": "Password", "status": "BLOCKED", "detail": str(exc)}
        outcome["E"] = {"door": "RecoveryPassword", "status": "BLOCKED", "detail": str(exc)}
    except NativeRecoveryError as exc:
        outcome["error"] = str(exc)
        for key in ("D", "E"):
            if outcome[key]["status"] == "FAIL" and outcome[key].get("detail") == "not attempted":
                outcome[key]["detail"] = str(exc)
    finally:
        try:
            outcome["child_exit_code"] = worker.close()
        except Exception as exc:  # never let cleanup mask the real verdict
            outcome["cleanup_error"] = str(exc)

    outcome["both_passed"] = (
        outcome["D"]["status"] == "PASS" and outcome["E"]["status"] == "PASS"
    )
    return outcome


if __name__ == "__main__":  # pragma: no cover - operator smoke check
    print(__doc__.strip().splitlines()[0])
    print(f"script: {_SCRIPT} exists={_SCRIPT.exists()}")
    print(f"powershell: {_find_powershell()}")
    sys.exit(0 if _SCRIPT.exists() else 1)
