"""Disposable injector self-control receiver for the SAIPATCH TUI driver.

A tiny console program that reads its own console input the same way a TUI
does: a raw ReadFile loop over CONIN$. The driver attaches to THIS console and
injects "ABC123" + Enter through WriteConsoleInputW; whatever this receiver
observes is the truth about the injector.

Exit 0 after writing {"observed": "..."} to --out. Started by the driver in a
CREATE_NEW_CONSOLE window; never touches any user terminal.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import time
from pathlib import Path

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ATTACH_PARENT_PROCESS = 0xFFFFFFFF


class COORD(ctypes.Structure):
    _fields_ = [("X", wt.SHORT), ("Y", wt.SHORT)]


kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, wt.DWORD]
kernel32.SetConsoleMode.restype = wt.BOOL
kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.ReadFile.restype = wt.BOOL
kernel32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD]
kernel32.GetConsoleProcessList.restype = wt.DWORD


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    handle = kernel32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE, 3, None, OPEN_EXISTING, 0, None)
    if (not handle) or handle == INVALID_HANDLE_VALUE:
        raise RuntimeError(f"CreateFileW(CONIN$) failed: {ctypes.get_last_error()}")
    try:
        # Raw console input: no line buffering, no echo, no Ctrl+C processing,
        # so ReadFile sees exactly the characters injected into the buffer.
        if not kernel32.SetConsoleMode(handle, 0):
            raise RuntimeError(f"SetConsoleMode failed: {ctypes.get_last_error()}")
        ids = (wt.DWORD * 64)()
        count = kernel32.GetConsoleProcessList(ids, 64)
        owner_pids = [int(ids[i]) for i in range(max(0, min(int(count), 64)))]
        out.with_suffix(".ready").write_text(json.dumps({"ready": True, "pid": os.getpid()}), encoding="utf-8")

        observed = ""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            buf = ctypes.create_string_buffer(4096)
            read = wt.DWORD(0)
            if kernel32.ReadFile(handle, buf, 4096, ctypes.byref(read), None):
                chunk = buf.raw[: read.value].decode("utf-8", errors="replace")
                for ch in chunk:
                    if ch in ("\r", "\n"):
                        if observed:
                            out.write_text(json.dumps({"observed": observed, "owner_pids": owner_pids}),
                                           encoding="utf-8")
                            return 0
                        continue
                    observed += ch
            else:
                time.sleep(0.05)
        out.write_text(json.dumps({"observed": observed, "owner_pids": owner_pids}), encoding="utf-8")
        return 1
    finally:
        kernel32.CloseHandle(handle)
        kernel32.FreeConsole()
        kernel32.AttachConsole(wt.DWORD(ATTACH_PARENT_PROCESS))


if __name__ == "__main__":
    raise SystemExit(main())
