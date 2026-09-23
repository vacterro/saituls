"""Real disposable-TUI queue + ambient acceptance for SAIPATCH generation 2.x.

Drives the ACTUAL patched OpenCode TUI in its own console window with ordinary
Enter presses (Windows console input records), then verifies strict native FIFO
(cc1..cc5, one promotion at a time) from the durable native history,
and ambient transitions from the SAIPATCH_TEST_TRACE stream
(screen attribute sampling stays secondary visual evidence).

Everything is disposable: redirected XDG/HOME, local fixture provider, a
disposable TUI hosting its own server on --port. The driver kills every child
it started in its finally block; it never touches the live installation.

Usage:
  python tests/saipatch_tui_driver.py --exe <disposable opencode.exe> \
      --evidence <dir> [--timeout 420]

Exit 0 = FIFO PASS with ambient observations recorded.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read_sound_sink(path):
    """Read the T-162 completion-sound sink: one JSON object per playback."""
    if path is None or not Path(path).exists():
        return []
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        return []
    return rows

# --- Win32 console plumbing -------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CREATE_NEW_CONSOLE = 0x00000010
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ATTACH_PARENT_PROCESS = 0xFFFFFFFF

CONSOLE_READFLAGS_NO_PROCESSED = 0  # raw console input: no line/echo/processed


class COORD(ctypes.Structure):
    _fields_ = [("X", wt.SHORT), ("Y", wt.SHORT)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", wt.SHORT), ("Top", wt.SHORT), ("Right", wt.SHORT), ("Bottom", wt.SHORT)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD), ("dwCursorPosition", COORD),
        ("wAttributes", wt.WORD),
        ("srWindow", SMALL_RECT),
        ("dwMaximumWindowSize", COORD),
    ]


class CHAR_INFO(ctypes.Structure):
    class _Char(ctypes.Union):
        _fields_ = [("UnicodeChar", wt.WCHAR), ("AsciiChar", ctypes.c_char)]
    _fields_ = [("Char", _Char), ("Attributes", wt.WORD)]


class KEY_EVENT_RECORD(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("UnicodeChar", wt.WCHAR), ("AsciiChar", ctypes.c_char)]
    _fields_ = [
        ("bKeyDown", wt.BOOL), ("wRepeatCount", wt.WORD),
        ("wVirtualKeyCode", wt.WORD), ("wVirtualScanCode", wt.WORD),
        ("uChar", _U), ("dwControlKeyState", wt.DWORD),
    ]


class INPUT_RECORD(ctypes.Structure):
    class _Event(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]
    _fields_ = [("EventType", wt.WORD), ("Event", _Event)]


# Explicit prototypes BEFORE any call. Handles are void pointers on x64; the
# ctypes default c_int restype would truncate HANDLEs and misreport failure.
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p  # HANDLE
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.FreeConsole.argtypes = []
kernel32.FreeConsole.restype = wt.BOOL
kernel32.AttachConsole.argtypes = [wt.DWORD]
kernel32.AttachConsole.restype = wt.BOOL
kernel32.WriteConsoleInputW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(INPUT_RECORD), wt.DWORD, ctypes.POINTER(wt.DWORD),
]
kernel32.WriteConsoleInputW.restype = wt.BOOL
kernel32.GetConsoleScreenBufferInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
kernel32.GetConsoleScreenBufferInfo.restype = wt.BOOL
kernel32.ReadConsoleOutputW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(CHAR_INFO), COORD, COORD, ctypes.POINTER(SMALL_RECT),
]
kernel32.ReadConsoleOutputW.restype = wt.BOOL
kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, wt.DWORD]
kernel32.SetConsoleMode.restype = wt.BOOL
kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.ReadFile.restype = wt.BOOL
kernel32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD]
kernel32.GetConsoleProcessList.restype = wt.DWORD


def is_bad_handle(handle):
    return (not handle) or handle == INVALID_HANDLE_VALUE


def open_console_file(name):
    """A fresh handle to the attached console buffer (std handles are stale
    after FreeConsole/AttachConsole when the process holds pipes)."""
    handle = kernel32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 3, None, OPEN_EXISTING, 0, None)
    if is_bad_handle(handle):
        raise RuntimeError(f"CreateFileW({name}) failed: {ctypes.get_last_error()}")
    return handle


def build_key_records(text, virtual_key_code=0, control_key_state=0):
    records = []
    for ch in text:
        for down in (True, False):
            rec = INPUT_RECORD()
            rec.EventType = 1  # KEY_EVENT
            rec.Event.KeyEvent.bKeyDown = 1 if down else 0
            rec.Event.KeyEvent.wRepeatCount = 1
            rec.Event.KeyEvent.wVirtualKeyCode = virtual_key_code
            rec.Event.KeyEvent.dwControlKeyState = control_key_state
            rec.Event.KeyEvent.uChar.UnicodeChar = ch
            records.append(rec)
    return records


def write_console_records_chunked(handle, records):
    """WriteConsoleInputW accepts only what the input buffer holds; write in
    bounded chunks until every record is delivered."""
    total = 0
    chunk = 4096
    for start in range(0, len(records), chunk):
        window = records[start:start + chunk]
        total += write_console_records(handle, window)
    return total


def send_key_records(pid: int, records):
    """Attach to the child's console and deliver raw input records."""
    info = {"attach": False, "handle": False, "written": 0, "requested": len(records)}
    try:
        attach_console(pid)
        info["attach"] = True
    except RuntimeError as error:
        info["error"] = str(error)
        return info
    handle = None
    try:
        handle = open_console_file("CONIN$")
        info["handle"] = True
        info["written"] = write_console_records_chunked(handle, records)
    except RuntimeError as error:
        info["error"] = str(error)
    finally:
        if handle is not None and not is_bad_handle(handle):
            kernel32.CloseHandle(handle)
        detach_console()
    return info


def send_ctrl_v(pid: int):
    """Ctrl+V as the Windows key sequence: VK_CONTROL down, V down (0x16 with
    LEFT_CTRL_PRESSED), V up, VK_CONTROL up."""
    LEFT_CTRL_PRESSED = 0x0008
    VK_CONTROL = 0x11
    VK_V = 0x56
    records = []

    def key(vk, char, down, state):
        rec = INPUT_RECORD()
        rec.EventType = 1
        rec.Event.KeyEvent.bKeyDown = 1 if down else 0
        rec.Event.KeyEvent.wRepeatCount = 1
        rec.Event.KeyEvent.wVirtualKeyCode = vk
        rec.Event.KeyEvent.dwControlKeyState = state
        rec.Event.KeyEvent.uChar.UnicodeChar = char
        records.append(rec)

    key(VK_CONTROL, "\x00", True, LEFT_CTRL_PRESSED)
    key(VK_V, "\x16", True, LEFT_CTRL_PRESSED)
    key(VK_V, "\x16", False, LEFT_CTRL_PRESSED)
    key(VK_CONTROL, "\x00", False, 0)
    return send_key_records(pid, records)


def send_ctrl_y(pid: int):
    """Ctrl+Y diagnostic: same console path as send_ctrl_v, unbound key."""
    LEFT_CTRL_PRESSED = 0x0008
    VK_CONTROL = 0x11
    VK_Y = 0x59
    records = []

    def key(vk, char, down, state):
        rec = INPUT_RECORD()
        rec.EventType = 1
        rec.Event.KeyEvent.bKeyDown = 1 if down else 0
        rec.Event.KeyEvent.wRepeatCount = 1
        rec.Event.KeyEvent.wVirtualKeyCode = vk
        rec.Event.KeyEvent.dwControlKeyState = state
        rec.Event.KeyEvent.uChar.UnicodeChar = char
        records.append(rec)

    key(VK_CONTROL, "\x00", True, LEFT_CTRL_PRESSED)
    key(VK_Y, "\x19", True, LEFT_CTRL_PRESSED)
    key(VK_Y, "\x19", False, LEFT_CTRL_PRESSED)
    key(VK_CONTROL, "\x00", False, 0)
    return send_key_records(pid, records)


def read_screen_text(pid: int):
    """Read every visible cell's character of the child console (screen capture
    for paste and transcript evidence)."""
    try:
        attach_console(pid)
    except RuntimeError:
        return None
    handle = None
    try:
        handle = open_console_file("CONOUT$")
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return None
        left, top = info.srWindow.Left, info.srWindow.Top
        width = info.srWindow.Right - info.srWindow.Left + 1
        height = info.srWindow.Bottom - info.srWindow.Top + 1
        buf = (CHAR_INFO * (width * height))()
        rect = SMALL_RECT(left, top, left + width - 1, top + height - 1)
        if not kernel32.ReadConsoleOutputW(handle, buf, COORD(width, height), COORD(0, 0), ctypes.byref(rect)):
            return None
        return "".join(buf[i].Char.UnicodeChar for i in range(width * height))
    finally:
        if handle is not None and not is_bad_handle(handle):
            kernel32.CloseHandle(handle)
        detach_console()


def write_console_records(handle, records):
    array = (INPUT_RECORD * len(records))(*records)
    written = wt.DWORD(0)
    if not kernel32.WriteConsoleInputW(handle, array, len(records), ctypes.byref(written)):
        raise RuntimeError(f"WriteConsoleInputW failed: {ctypes.get_last_error()}")
    return written.value


def attach_console(pid):
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(wt.DWORD(pid)):
        raise RuntimeError(f"AttachConsole({pid}) failed: {ctypes.get_last_error()}")


def detach_console():
    kernel32.FreeConsole()
    # Reattach to our own parent so later operations keep a console. Failure is
    # tolerable (agent shells may have no parent console); handles were closed.
    kernel32.AttachConsole(wt.DWORD(ATTACH_PARENT_PROCESS))


def send_console_text(pid: int, text: str):
    """Attach to the child's console and type text + Enter as key events.

    Returns the delivery instrumentation: (attach ok, handle valid, written
    count, requested count). Handles are closed in every path.
    """
    info = {"attach": False, "handle": False, "written": 0, "requested": 0}
    try:
        attach_console(pid)
        info["attach"] = True
    except RuntimeError as error:
        info["error"] = str(error)
        return info
    handle = None
    try:
        handle = open_console_file("CONIN$")
        info["handle"] = True
        records = build_key_records(text)
        records += build_key_records("\r", virtual_key_code=0x0D)
        info["requested"] = len(records)
        info["written"] = write_console_records(handle, records)
    except RuntimeError as error:
        info["error"] = str(error)
    finally:
        if handle is not None and not is_bad_handle(handle):
            kernel32.CloseHandle(handle)
        detach_console()
    return info

def console_owner_pids(pid: int):
    try:
        attach_console(pid)
    except RuntimeError:
        return []
    try:
        ids = (wt.DWORD * 64)()
        count = kernel32.GetConsoleProcessList(ids, 64)
        return [int(ids[i]) for i in range(max(0, min(int(count), 64)))]
    finally:
        detach_console()


def read_attributes(pid: int):
    """Sample every visible cell's attribute bytes of the child console.
    Returns None when the console cannot be attached or read."""
    try:
        attach_console(pid)
    except RuntimeError:
        return None
    handle = None
    try:
        handle = open_console_file("CONOUT$")
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return None
        left, top = info.srWindow.Left, info.srWindow.Top
        width = info.srWindow.Right - info.srWindow.Left + 1
        height = info.srWindow.Bottom - info.srWindow.Top + 1
        buf = (CHAR_INFO * (width * height))()
        rect = SMALL_RECT(left, top, left + width - 1, top + height - 1)
        if not kernel32.ReadConsoleOutputW(handle, buf, COORD(width, height), COORD(0, 0), ctypes.byref(rect)):
            return None
        attrs = []
        for row in range(height):
            for col in range(width):
                attrs.append(buf[row * width + col].Attributes & 0x00FF)
        return attrs
    finally:
        if handle is not None and not is_bad_handle(handle):
            kernel32.CloseHandle(handle)
        detach_console()


# --- FIFO oracle (also exercised by tests/test_progress_oracle.py) ----------
def classify_history(rows, admitted):
    """Strict native FIFO oracle over the durable history.

    Returns (promoted, complete, state, fault):
      promoted  promoted user message IDs in admission order
      complete  user message IDs whose assistant step ended with finish=stop
      state     "IDLE" | "RUNNING" | "OVERLAP"
      fault     None | "ORDER" | "OVERLAP" | "ENDED_WITHOUT_START" | "FINISH:<x>"

    Invariant (T-143 clause 6): exactly one active assistant step is the valid
    RUNNING state. A second step.started while a step is still active is the
    only OVERLAP. End-of-history with one active step is RUNNING, not a fault.
    """
    expected = [a["id"] for a in admitted]
    promoted, complete = [], []
    active = None
    current = None
    for event in rows:
        kind, data = event["type"], event["data"]
        if kind == "session.next.prompted":
            current = data["messageID"]
            if len(promoted) >= len(expected) or current != expected[len(promoted)]:
                return promoted, complete, "RUNNING" if active else "IDLE", "ORDER"
            promoted.append(current)
        elif kind == "session.next.step.started":
            if active is not None:
                return promoted, complete, "OVERLAP", "OVERLAP"
            active = data["assistantMessageID"]
        elif kind == "session.next.step.ended":
            if active is None:
                return promoted, complete, "IDLE", "ENDED_WITHOUT_START"
            active = None
            if data["finish"] == "stop":
                complete.append(current)
            elif data["finish"] != "tool-calls":
                return promoted, complete, "IDLE", "FINISH:" + str(data["finish"])
        elif kind == "session.next.step.failed":
            if active is None:
                return promoted, complete, "IDLE", "FAILED_WITHOUT_START"
            active = None
            return promoted, complete, "IDLE", "STEP_FAILED"
    return promoted, complete, "RUNNING" if active is not None else "IDLE", None


# --- ConPTY transport -------------------------------------------------------
# The disposable TUI runs under a real ConPTY (winpty/OpenConsole). Screen
# evidence comes from the VT stream the terminal actually renders (strip ANSI
# and search), because ReadConsoleOutputW against a Windows-Terminal-hosted
# console does not reliably reflect the rendered buffer. Input is delivered as
# ordinary keystrokes through the ConPTY input pipe - the same path a real
# keyboard takes, with NO bracketed-paste markers.
ANSI_RE = None


def strip_ansi(text: str) -> str:
    import re
    global ANSI_RE
    if ANSI_RE is None:
        ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[P^_][^\x1b]*\x1b\\|[0-9A-Za-z=><])")
    return ANSI_RE.sub("", text)


class ConptySession:
    """winpty PtyProcess wrapper: pid, a timestamped VT-stream log, write."""

    def __init__(self, deps: Path, argv, cwd, env, log_path, dimensions=(35, 120)):
        import threading
        sys.path.insert(0, str(deps))
        from winpty import PtyProcess
        from winpty.enums import Backend
        self.proc = PtyProcess.spawn([str(a) for a in argv],
                                     cwd=str(cwd), env=env, dimensions=dimensions, backend=Backend.ConPTY)
        self.log_path = log_path
        self.stream = []  # list of (t, chunk)
        self.cursor_position_pending = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self):
        try:
            while self.proc.isalive():
                try:
                    chunk = self.proc.read(65536)
                except (EOFError, OSError):
                    break
                if not chunk:
                    break
                self.stream.append((time.time(), chunk))
                # A real terminal answers the cursor-position request.
                if "\x1b[6n" in chunk:
                    try:
                        self.proc.write("\x1b[1;1R")
                    except Exception:
                        pass
        finally:
            try:
                (self.log_path.parent / self.log_path.name).write_text(
                    "".join(chunk for _, chunk in self.stream[-400:]), encoding="utf-8", errors="replace")
            except Exception:
                pass

    @property
    def pid(self):
        return self.proc.pid

    def isalive(self):
        try:
            return self.proc.isalive()
        except Exception:
            return False

    def write(self, text: str):
        try:
            self.proc.write(text)
            return True
        except Exception:
            return False

    def recent_text(self, limit=40000) -> str:
        tail = []
        size = 0
        for _, chunk in reversed(self.stream):
            tail.append(chunk)
            size += len(chunk)
            if size >= limit:
                break
        return strip_ansi("".join(reversed(tail)))[-limit:]

    def terminate(self):
        try:
            self.proc.terminate(force=True)
        except Exception:
            pass
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.pid)],
                           capture_output=True, timeout=20)
        except Exception:
            pass


import base64


def run_ps(script: str, timeout: float = 60, stdin_text: str | None = None):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                          capture_output=True, text=True, timeout=timeout,
                          input=stdin_text, encoding="utf-8")


def clipboard_read():
    result = run_ps("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw")
    return result.stdout


def clipboard_diag():
    """Content-free clipboard diagnostics: counts and emptiness ONLY.
    Never return clipboard text - evidence must not carry secrets."""
    try:
        text = clipboard_read()
    except Exception:
        return {"clipboard_chars": -1, "clipboard_bytes": -1, "clipboard_empty": None}
    return {"clipboard_chars": len(text), "clipboard_bytes": len(text.encode("utf-8")),
            "clipboard_empty": len(text) == 0}


def clipboard_write(text: str):
    # Payload arrives on stdin (large pastes exceed the command-line limit).
    result = run_ps(
        "[Console]::InputEncoding=[Text.Encoding]::UTF8; "
        "$t = [Console]::In.ReadToEnd(); Set-Clipboard -Value $t",
        stdin_text=text,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Set-Clipboard failed: {result.stderr[:200]}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_json(url: str, method: str = "GET", body=None):
    req = urllib.request.Request(url, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read()
        return json.loads(raw) if raw else {"http_status": response.status}


def read_trace_lines(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=420)
    parser.add_argument("--prompt-delay", type=float, default=6.0,
                        help="seconds between Enter presses once the busy barrier is seen")
    parser.add_argument("--skip-control", action="store_true",
                        help="skip the injector self-control (never for acceptance runs)")
    parser.add_argument("--no-ambient", action="store_true",
                        help="diagnostic: disable the ambient tint module in the disposable TUI")
    parser.add_argument("--debug", action="store_true",
                        help="diagnostic: SAIPATCH_QUEUE_DEBUG plugin console trace into tui-console.log")
    parser.add_argument("--paste-gate", action="store_true",
                        help="clause 26: cc2 is delivered by pasting via Ctrl+V while cc1 runs; the "
                             "clipboard is saved and restored around the gate")
    parser.add_argument("--sound-sink", type=Path, default=None,
                        help="T-162 acceptance: enable the completion sound in the disposable TUI "
                             "(settings under its redirected LOCALAPPDATA), capture one JSON line per "
                             "playback request into this sink, and gate on zero before the final DONE "
                             "and exactly one for the cc1..cc5 drain")
    parser.add_argument("--deps", type=Path, default=None,
                        help="directory carrying the winpty ConPTY package (default: kitchen deps)")
    parser.add_argument("--seed-plugin", action="store_true",
                        help="HOST-SEAM PROTOTYPE ONLY: stage the package TUI modules directly instead of running "
                             "patcher Apply, because a deliberately patched prototype binary fails the installer's "
                             "exact-host-hash gate. Never used to accept the shipped patch.")
    args = parser.parse_args()
    deps = args.deps or (HERE.parent / ".saipen" / "kitchen" / "T-144-paste" / "deps")
    if not deps.exists():
        raise RuntimeError(f"winpty deps directory missing: {deps}")

    exe = Path(args.exe).resolve()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    home = evidence / "home"
    if (home).exists():
        import shutil
        shutil.rmtree(home, ignore_errors=True)
    config_dir = home / "xdgconfig"
    data_dir = home / "xdgdata"
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 1. Apply generation 2.x into the disposable config tree. The patcher
    # resolves OpenCode through PATH and expects the npm shim shape (a quoted
    # path ending in opencode.exe, %dp0%-relative), so fabricate exactly that
    # shape around the disposable executable.
    patcher = HERE.parent / "Scripts" / "saipatch" / "patcher.ps1"
    shim_dir = home / "bin"
    pkg_bin = home / "pkg" / "bin"
    pkg_bin.mkdir(parents=True, exist_ok=True)
    shim_dir.mkdir(parents=True, exist_ok=True)
    disposable = pkg_bin / "opencode.exe"
    if not disposable.exists():
        try:
            subprocess.run(["cmd", "/c", "mklink", "/H", str(disposable), str(exe)], capture_output=True, check=True)
        except Exception:
            import shutil
            shutil.copyfile(exe, disposable)
    (shim_dir / "opencode.cmd").write_text(
        "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\nCALL :find_dp0\r\n"
        + f'"%dp0%..\\pkg\\bin\\opencode.exe"   %*\r\n', encoding="ascii")
    if args.seed_plugin:
        # HOST-SEAM PROTOTYPE ONLY: stage the package TUI modules directly,
        # bypassing the production installer whose exact-host-hash gate must
        # reject a deliberately patched prototype binary. The installer path is
        # already proven by test_saipatch.ps1; this path never accepts the
        # shipped patch.
        import shutil
        package = HERE.parent / "Scripts" / "saipatch" / "patches" / "opencode-queue-mode"
        plugin_dir = home / ".config" / "opencode" / "tui-modules"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        for name in ["saipatch-native-queue-2x.js", "saipatch-completion-sound.js", "saipatch-sound-runtime.js"]:
            shutil.copyfile(package / name, plugin_dir / name)
        sound_dir = plugin_dir / "sounds"
        sound_dir.mkdir(parents=True, exist_ok=True)
        for name in ["PICKUP01.wav", "PICKUP02.wav", "PICKUP03.wav", "PICKUP04.wav",
                     "PICKUP05.wav", "PICKUP06.wav", "PICKUP07.wav"]:
            shutil.copyfile(HERE.parent / "Scripts" / "saipatch" / name, sound_dir / name)
        entry_uri = "file:///" + str(plugin_dir / "saipatch-native-queue-2x.js").replace("\\", "/")
        (home / ".config" / "opencode" / "tui.json").write_text(
            json.dumps({"plugin": [entry_uri]}, indent=2), encoding="utf-8")
        (evidence / "apply.log").write_text(f"seed-plugin: staged {entry_uri}\n", encoding="utf-8")
    else:
        env_apply = os.environ.copy()
        # Windows PowerShell must not import PowerShell 7 modules inherited from
        # the parent shell after USERPROFILE is redirected into the fixture.
        env_apply["PSModulePath"] = str(Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "Modules")
        env_apply.update({"USERPROFILE": str(home), "LOCALAPPDATA": str(home / "local"),
                          "XDG_DATA_HOME": str(data_dir),
                          "PATH": str(shim_dir) + os.pathsep + env_apply.get("PATH", "")})
        apply_result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(patcher), "Apply"],
            env=env_apply, cwd=str(evidence), capture_output=True, text=True, timeout=300)
        (evidence / "apply.log").write_text(apply_result.stdout + apply_result.stderr, encoding="utf-8")
        if apply_result.returncode != 0:
            raise RuntimeError(f"disposable Apply failed rc={apply_result.returncode}; see apply.log")
        # T-144 seam contract: after a successful Apply the TUI MUST run the
        # PATCHED disposable copy under home/pkg/bin (the binary seam target).
        # Launching the untouched baseline while the installer patched a copy
        # the driver never executed would test nothing: the seam never fires
        # and the run looks broken for a reason that does not exist. Fail
        # closed on the descriptor image hash instead of silently proceeding.
        disposable_exe = pkg_bin / "opencode.exe"
        if not disposable_exe.is_file():
            raise RuntimeError(f"patched disposable exe missing after Apply: {disposable_exe}")
        descriptor = json.loads(
            (HERE.parent / "Scripts" / "saipatch" / "patches" / "opencode-queue-mode"
             / "host-patch.json").read_text(encoding="utf-8"))
        src_hash = hashlib.sha256(exe.read_bytes()).hexdigest()
        expected_patched = None
        for _ver, _build in descriptor["builds"].items():
            if src_hash == _build["target"]["sha256"]:
                expected_patched = _build["patched_sha256"]
                break
        if expected_patched is None:
            raise RuntimeError(
                "disposable exe build is not covered by the descriptor builds: " + src_hash)
        observed = hashlib.sha256(disposable_exe.read_bytes()).hexdigest()
        if observed != expected_patched:
            raise RuntimeError(
                "disposable exe is not the descriptor patched image "
                f"(observed {observed}, expected {expected_patched})")
        exe = disposable_exe

    # T-162 acceptance: opt-in completion sound. The plugin reads its settings
    # from the DISPOSABLE LOCALAPPDATA (home/local), so seed an enabled settings
    # file there; the sink file replaces real playback.
    if args.sound_sink:
        settings_dir = home / "local" / "SAITULS" / "SAIPATCH"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "opencode-queue-mode.settings.json").write_text(json.dumps({
            "enabled": True, "volume": 70, "mode": "ordered",
            "disabledSounds": [], "minimumRunSeconds": 0,
        }, indent=2), encoding="utf-8")
        args.sound_sink.parent.mkdir(parents=True, exist_ok=True)
        if args.sound_sink.exists():
            args.sound_sink.unlink()

    plugin_file = home / ".config" / "opencode" / "tui-modules" / "saipatch-native-queue-2x.js"
    tui_json = home / ".config" / "opencode" / "tui.json"
    if not plugin_file.is_file():
        raise RuntimeError(f"patched plugin missing in disposable config: {plugin_file}")
    try:
        import json as _json
        tui_config = _json.loads(tui_json.read_text(encoding="utf-8"))
    except Exception as error:
        raise RuntimeError(f"tui.json unreadable after Apply: {error}")
    expected_uri = "file:///" + str(plugin_file).replace("\\", "/")
    if expected_uri not in [str(x) for x in (tui_config.get("plugin") or [])]:
        raise RuntimeError(f"tui.json does not reference the SAIPATCH module: {expected_uri}")

    # 2. Fixture provider (>= 8 s per turn, clause 13).
    provider_port = free_port()
    tui_port = free_port()
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": "fixture/queue-fixture",
        "small_model": "fixture/queue-fixture",
        "enabled_providers": ["fixture"],
        "provider": {"fixture": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Local queue fixture",
            "options": {"baseURL": f"http://127.0.0.1:{provider_port}/v1",
                        "apiKey": "local-fixture-not-a-secret"},
            "models": {"queue-fixture": {"name": "Queue fixture",
                                          "limit": {"context": 32000, "output": 1024}}},
        }},
    }
    (home / ".config" / "opencode" / "opencode.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    env = os.environ.copy()
    env.update({"HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(home / "local"),
                "XDG_DATA_HOME": str(data_dir)})
    ambient_trace = evidence / "ambient-trace.jsonl"
    env["SAIPATCH_TEST_TRACE"] = str(ambient_trace)
    key_trace = evidence / "key-trace.jsonl"
    env["SAIPATCH_KEY_TRACE"] = str(key_trace)
    if args.sound_sink:
        env["SAIPATCH_SOUND_SINK"] = str(args.sound_sink)
    if args.no_ambient:
        env["SAIPATCH_AMBIENT_DISABLE"] = "1"
    if args.debug:
        env["SAIPATCH_QUEUE_DEBUG"] = str(evidence / "plugin-debug.log")

    # 3. Injector self-control: a disposable receiver under the SAME ConPTY
    # transport must observe exactly ABC123 + Enter. If this fails the driver
    # is broken, not OpenCode.
    timeline = []
    if not args.skip_control:
        control_out = evidence / "driver-control.json"
        control = ConptySession(deps,
                                [sys.executable, str(HERE / "saipatch_tui_receiver.py"), "--out", str(control_out)],
                                evidence, {}, evidence / "control-console.log")
        try:
            ready = control_out.with_suffix(".ready")
            deadline = time.monotonic() + 30
            while not ready.exists():
                if not control.isalive():
                    raise RuntimeError("receiver exited before ready")
                if time.monotonic() > deadline:
                    raise RuntimeError("receiver never became ready")
                time.sleep(0.1)
            start = time.monotonic()
            control.write("ABC123\r")
            written = len("ABC123\r")
            deadline = time.monotonic() + 20
            while not control_out.exists():
                if not control.isalive() and not control_out.exists():
                    raise RuntimeError("receiver exited without observation")
                if time.monotonic() > deadline:
                    raise RuntimeError("receiver never observed input")
                time.sleep(0.1)
            observed = json.loads(control_out.read_text(encoding="utf-8"))
            delivery = {"conpty_written": written, "observed": observed.get("observed"),
                        "owner_pids": observed.get("owner_pids"),
                        "elapsed_ms": round((time.monotonic() - start) * 1000, 1)}
            (evidence / "driver-control.json").write_text(json.dumps(delivery, indent=2), encoding="utf-8")
            if observed.get("observed") != "ABC123":
                print(json.dumps({"verdict": "STOP:DRIVER_BROKEN", "delivery": delivery}, indent=2), flush=True)
                return 2
            print("injector control PASS (conpty write -> receiver observed ABC123)", flush=True)
            timeline.append({"t": round(time.time(), 3), "event": "injector-control-pass"})
        finally:
            control.terminate()

    provider = tui = None
    verdict = {"fifo": None, "ambient": [], "ambient_transitions": [], "notes": []}
    saved_clipboard = None
    _clipboard_fixture = None
    if args.paste_gate:
        try:
            saved_clipboard = clipboard_read()
        except Exception:
            saved_clipboard = None
    try:
        provider = subprocess.Popen(
            [sys.executable, str(HERE / "native_queue_fixture.py"), "--port", str(provider_port), "--delay", "8"],
            stdout=open(evidence / "provider.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while True:
            try:
                http_json(f"http://127.0.0.1:{provider_port}/v1/models")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError("fixture provider never became ready")
                time.sleep(0.4)
        print(f"provider ready :{provider_port}", flush=True)

        # 4. The real patched TUI under the same real-ConPTY transport, server
        # on a fixed port. Input = ordinary keystrokes through the ConPTY
        # input pipe; screen evidence = the VT stream the terminal renders.
        tui = ConptySession(deps, [exe, "--port", tui_port, "--hostname", "127.0.0.1"],
                            evidence, env, evidence / "tui-console.log")
        deadline = time.monotonic() + 180
        while True:
            try:
                http_json(f"http://127.0.0.1:{tui_port}/config")
                break
            except Exception:
                if not tui.isalive():
                    raise RuntimeError("disposable TUI exited before its server became ready (see tui-console.log)")
                if time.monotonic() > deadline:
                    raise RuntimeError("disposable TUI server never became ready")
                time.sleep(0.8)
        print(f"tui ready      :{tui_port} pid={tui.pid}", flush=True)
        time.sleep(3)

        def observe(tag):
            # Secondary visual evidence: the rendered stream tail (attributes
            # come from the authoritative SAIPATCH_TEST_TRACE, clause 16).
            entry = {"t": round(time.time(), 3), "tag": tag,
                     "stream_tail": tui.recent_text(600)[-200:]}
            if args.sound_sink:
                entry["sound"] = len(read_sound_sink(args.sound_sink))
            verdict["ambient"].append(entry)
            timeline.append({"t": round(time.time(), 3), "event": tag})

        def sync_trace():
            rows = read_trace_lines(ambient_trace)
            verdict["ambient_transitions"] = rows
            return rows

        observe("startup")
        sync_trace()

        def send(text, tag):
            ok = tui.write(text + "\r")
            timeline.append({"t": round(time.time(), 3), "event": tag, "conpty_write": ok})
            print(f"typed {tag} conpty={ok}", flush=True)
            if not ok:
                verdict["notes"].append(f"input-write-failed:{tag}")

        send("cc1", "cc1-enter")
        session_id = None
        deadline = time.monotonic() + args.timeout
        while session_id is None:
            try:
                sessions = http_json(f"http://127.0.0.1:{tui_port}/api/session")["data"]
                if sessions:
                    session_id = sessions[-1]["id"]
            except Exception:
                pass
            if session_id is None and time.monotonic() > deadline:
                # Instrument ONLY the driver boundary (clause 7) before blaming
                # the host: ConPTY stream truth plus process ownership.
                instrument = {
                    "conpty_alive": tui.isalive(),
                    "conpty_pid": tui.pid,
                    "stream_tail": tui.recent_text(2000)[-800:],
                    "stream_chunks": len(tui.stream),
                }
                (evidence / "driver-instrument.json").write_text(
                    json.dumps(instrument, indent=2, ensure_ascii=False), encoding="utf-8")
                raise RuntimeError("no session appeared after cc1 (driver-instrument.json recorded)")
            if session_id is None:
                time.sleep(0.5)
        prefix = f"http://127.0.0.1:{tui_port}/api/session/{session_id}"
        print(f"session {session_id}", flush=True)

        # 5. Busy barrier on cc1 (step.started), then queue cc2..cc5 by ordinary
        # Enter while earlier work stays active.
        def history():
            return http_json(prefix + "/history")["data"]

        def messages():
            return http_json(prefix + "/message")["data"]

        barrier = False
        cc_n = 2
        last_type = 0.0
        seen_order_fault = None
        while time.monotonic() < deadline:
            rows = history()
            # prompt.admitted is a durable EVENT: the admission's prompt message
            # id lives in data.messageID (the row id is the event id).
            admitted = [{"id": e["data"]["messageID"]}
                        for e in rows
                        if e["type"] == "session.next.prompt.admitted" and e.get("data", {}).get("messageID")]
            promoted, complete, state, fault = classify_history(rows, admitted)
            (evidence / "tui-history.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
            if fault in ("OVERLAP", "ORDER"):
                seen_order_fault = fault
                verdict["fifo"] = f"FAIL:{fault}"
                break
            if fault == "STEP_FAILED":
                verdict["fifo"] = "FAIL:STEP_FAILED"
                break
            if not barrier:
                busy = state == "RUNNING"
                if busy:
                    barrier = True
                    timeline.append({"t": round(time.time(), 3), "event": "cc1-step.started-busy-barrier"})
                    print("BUSY_BARRIER cc1 step.started", flush=True)
                    observe("cc1-running")
            else:
                now = time.monotonic()
                if cc_n <= 5 and now - last_type > args.prompt_delay:
                    if args.paste_gate and cc_n == 2:
                        # Clause 26 combined gate: cc2 arrives as a LARGE paste
                        # through Ctrl+V while cc1 still executes; Enter is a
                        # separate, explicit keystroke (paste never submits).
                        marker = "SAIPATCH-PASTE-CC2"
                        unit = f"safe fixture line %d {marker} " + "Текст\t漢字 line\r\n"
                        core = (unit * (16384 // len(unit) + 2))[:16384 - len(unit)] + marker + " END"
                        payload = marker + "\r\n" + core + "\r\n" + marker + "-TAIL"
                        clipboard_write(payload)
                        _clipboard_fixture = payload
                        start = time.monotonic()
                        _paste_diag = send_ctrl_v(tui.pid)
                        paste_ok = _paste_diag.get("written", 0) == _paste_diag.get("requested", 0) and _paste_diag.get("handle") and _paste_diag.get("attach")
                        # Diagnostic: ctrl+y (unbound) proves whether plugin layer bindings fire at all.
                        time.sleep(0.5)
                        _diag_diag = send_ctrl_y(tui.pid)
                        seen = False
                        deadline2 = time.monotonic() + 30
                        screen_history = []
                        while time.monotonic() < deadline2:
                            rendered = tui.recent_text(6000)
                            # The paste oracle is the rendered editor text carrying the
                            # payload head marker (host shows no "[Pasted ~]" chip when the
                            # editor renders the buffer directly).
                            if marker in rendered:
                                seen = True
                                break
                            if int((time.monotonic() - start) * 2) != len(screen_history):
                                screen_history.append({"t": round(time.monotonic() - start, 2),
                                                       "tail": rendered[-160:]})
                            time.sleep(0.05)
                        (evidence / "paste-screen-history.json").write_text(
                            json.dumps(screen_history, indent=2, ensure_ascii=False), encoding="utf-8")
                        paste_ms = (time.monotonic() - start) * 1000
                        if not seen:
                            (evidence / "paste-failure.json").write_text(
                                json.dumps({"conpty_write": paste_ok, "paste_ms": paste_ms,
                                            **clipboard_diag(),
                                            "stream_tail": tui.recent_text(3000)[-1200:]}, indent=2,
                                           ensure_ascii=False),
                                encoding="utf-8")
                        timeline.append({"t": round(time.time(), 3),
                                         "event": "cc2-paste-ctrlv",
                                         "paste_visible_ms": round(paste_ms, 1),
                                         "visible": seen,
                                         "conpty_write": paste_ok,
                                         "paste_diag": {k: v for k, v in _paste_diag.items() if k != "error"},
                                         "paste_error": _paste_diag.get("error"),
                                         "ctrl_y_diag": {k: v for k, v in _diag_diag.items() if k != "error"}})
                        print(f"cc2 pasted via ctrl+v visible={seen} in {paste_ms:.0f}ms", flush=True)
                        if not seen:
                            verdict["fifo"] = "FAIL:paste-not-visible"
                            break
                        tui.write("\r")  # explicit Enter: paste must NOT self-submit
                        verdict["paste_payload"] = {"head": marker, "tail": marker + "-TAIL",
                                                    "bytes": len(payload.encode("utf-8"))}
                        observe("cc2-pasted-queued")
                        cc_n += 1
                        last_type = now
                        continue
                    send(f"cc{cc_n}", f"cc{cc_n}-enter-while-busy")
                    observe(f"cc{cc_n}-queued")
                    cc_n += 1
                    last_type = now
            sync_trace()
            if cc_n > 5 and len(complete) == 5 and fault is None:
                verdict["fifo"] = "PASS:5/5-strict-native-FIFO"
                observe("final-done")
                break
            time.sleep(1.0)
        else:
            if verdict["fifo"] is None:
                verdict["fifo"] = "FAIL:timeout"
        (evidence / "tui-history.json").write_text(json.dumps(history() if session_id else [], indent=2), encoding="utf-8")
        # Clause 26 payload oracle: the cc2 admission prompt must carry the full
        # pasted payload (head AND tail markers), never truncated, and the
        # admission must be exactly one.
        if args.paste_gate and verdict.get("paste_payload"):
            try:
                rows = history() if session_id else []
                admitted_cc2 = [e for e in rows
                                if e["type"] == "session.next.prompt.admitted"
                                and verdict["paste_payload"]["head"] in (e.get("data", {}).get("prompt", {}).get("text") or "")]
                text = (admitted_cc2[0].get("data", {}).get("prompt", {}).get("text") or "") if admitted_cc2 else ""
                complete = (verdict["paste_payload"]["tail"] in text and
                            verdict["paste_payload"]["head"] in text)
                verdict["paste_gate"] = {"admissions": len(admitted_cc2), "payload_complete": complete,
                                         "admitted_len": len(text)}
                if len(admitted_cc2) != 1 or not complete:
                    verdict["fifo"] = "FAIL:paste-gate"
            except Exception as error:
                verdict["paste_gate"] = {"error": str(error)}
                verdict["fifo"] = "FAIL:paste-gate"
        # Server history only: this endpoint cannot prove local-store mutation
        # or rendering. Never label its result visible transcript evidence.
        try:
            if session_id:
                projected = messages()
                (evidence / "tui-messages.json").write_text(json.dumps(projected, indent=2), encoding="utf-8")
                texts = []
                for message in projected:
                    if message.get("type") == "text" and isinstance(message.get("text"), str):
                        texts.append(message["text"][:80])
                verdict["native_history_texts"] = texts
                verdict["live_projection"] = "UNPROVEN: HTTP history is not a local-store oracle"
        except Exception as error:
            verdict["notes"].append(f"projection-fetch-failed:{error}")
        sync_trace()
    finally:
        if args.paste_gate and saved_clipboard is not None and _clipboard_fixture is not None:
            try:
                _cur = clipboard_read()
                if _cur == _clipboard_fixture:
                    clipboard_write(saved_clipboard)
            except Exception:
                pass
        # Kill the whole ConPTY tree: the winpty agent, the console host and
        # the opencode child must never outlive the driver. The fixture
        # provider is a direct child process.
        if tui is not None:
            tui.terminate()
        if provider is not None:
            try:
                provider.terminate()
                provider.wait(timeout=15)
            except Exception:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(provider.pid)],
                                   capture_output=True, timeout=20)
                except Exception:
                    pass
    # Ambient gate (clauses 15/16): trace is authoritative, attributes secondary.
    transitions = sync_trace()
    states = [row.get("state") for row in transitions if isinstance(row, dict) and "state" in row]
    verdict["ambient_states"] = states
    verdict["done_flash_between_turns"] = False
    if verdict["fifo"] and verdict["fifo"].startswith("PASS"):
        if not states:
            verdict["notes"].append("ambient-trace-missing")
        else:
            first_running = next((i for i, s in enumerate(states) if s == "RUNNING"), None)
            final_done = next((i for i, s in enumerate(states) if s == "DONE" and i > first_running), None)
            if first_running is None:
                verdict["fifo"] = "FAIL:ambient-never-RUNNING"
            elif final_done is None:
                verdict["fifo"] = "FAIL:ambient-never-DONE"
            else:
                premature = [s for s in states[:final_done] if s == "DONE"]
                if premature:
                    verdict["done_flash_between_turns"] = True
                    verdict["fifo"] = "FAIL:DONE-flash-before-final"
    # T-162 sound gate (clauses 21/22): the completion sound is a projection of
    # the same DONE truth, so require zero playback before the final DONE and
    # exactly one for the cc1..cc5 drain.
    if args.sound_sink:
        rows = read_sound_sink(args.sound_sink)
        verdict["sound_sink"] = {"path": str(args.sound_sink), "entries": len(rows),
                                 "preview": sum(1 for r in rows if r.get("preview"))}
        if verdict["fifo"] and verdict["fifo"].startswith("PASS"):
            premature_sound = [e for e in verdict["ambient"]
                               if e.get("tag") != "final-done" and e.get("sound")]
            if premature_sound:
                verdict["fifo"] = "FAIL:sound-before-final"
            elif len(rows) != 1:
                verdict["fifo"] = f"FAIL:sound-sink-count-{len(rows)}"
            elif rows[0].get("preview"):
                verdict["fifo"] = "FAIL:sound-sink-preview"
    verdict["timeline"] = timeline
    (evidence / "tui-verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    print(json.dumps({"fifo": verdict["fifo"], "ambient_states": states,
                      "timeline_events": len(timeline)}, indent=2), flush=True)
    return 0 if verdict["fifo"] and verdict["fifo"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
